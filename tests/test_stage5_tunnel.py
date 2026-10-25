"""Этап 5: туннели через ретрансляторы, end-to-end защита, повтор сообщений, восстановление."""
import asyncio
import os

import pytest

from conftest import run, start_nodes, stop_nodes
from overlay.experiment.tap import TcpTap, pcap_contains
from overlay.metrics import now_ms
from overlay.protocol import MsgType
from overlay.tunnel import DEAD, TunnelError


def by_id(nodes, hex_id):
    return next(n for n in nodes if n.identity.node_id.hex() == hex_id)


def test_message_goes_through_two_relays(base):
    async def go():
        nodes = await start_nodes(base, 8, mode="tls")
        src, dst = nodes[1], nodes[6]
        r = await src.send_message(dst.identity.node_id, "hello through the tunnel")
        relays = [by_id(nodes, h) for h in r["route"][1:-1]]
        relay_cells = [sum(x.get("cells_fwd", 0) for x in n.tunnels.summary() if x["role"] == "relay")
                       for n in relays]
        inbox = list(dst.messaging.inbox)
        await stop_nodes(nodes)
        return r, relay_cells, inbox, src, dst

    r, relay_cells, inbox, src, dst = run(go(), timeout=120)
    assert r["status"] == "delivered"
    route = r["route"]
    assert route[0] == src.identity.node_id.hex() and route[-1] == dst.identity.node_id.hex()
    assert len(route) == 4 and len(set(route)) == 4            # 2 ретранслятора, без повторов
    assert all(c > 0 for c in relay_cells)                      # данные реально прошли через них
    assert [m["text"] for m in inbox] == ["hello through the tunnel"]


def test_relays_see_only_ciphertext_and_pcap_has_no_plaintext(base, tmp_path):
    marker = "TOP-SECRET-MARKER-42"
    pcap = str(tmp_path / "tunnel.pcap")

    async def go():
        nodes = await start_nodes(base, 7, mode="plain")      # даже без hop-by-hop TLS
        src, dst = nodes[1], nodes[5]
        seen_by_relays = []
        for n in nodes:
            orig = n.tunnels.on_data

            async def spy(conn, frame, body, _orig=orig, _n=n):
                if body["tunnel_id"] in _n.tunnels.relay:
                    seen_by_relays.append(body["data"])
                return await _orig(conn, frame, body)
            n.dispatcher.register(MsgType.TUNNEL_DATA, spy, ordered=True)
        # Наблюдение входящего трафика получателя через tap: dst объявляет адрес tap-а.
        tap = TcpTap(0, dst.cfg.LISTEN_PORT, pcap_path=pcap)
        await tap.start()
        dst.cfg.ADVERTISE_PORT = tap.listen_port
        await dst.publish_self()
        r = await src.send_message(dst.identity.node_id, marker)
        await asyncio.sleep(0.1)
        await tap.stop()
        await stop_nodes(nodes)
        return r, seen_by_relays, tap

    r, seen, tap = run(go(), timeout=120)
    assert r["status"] == "delivered"
    assert seen and not any(marker.encode() in d for d in seen)
    assert tap.total["c2s"] > 0
    assert not pcap_contains(pcap, marker.encode())


def test_app_replay_in_session_is_deduplicated(base):
    async def go():
        nodes = await start_nodes(base, 7, mode="tls")
        src, dst = nodes[2], nodes[4]
        r = await src.send_message(dst.identity.node_id, "pay 100")
        session = src.messaging.sessions[dst.identity.node_id]
        body = {"message_id": bytes.fromhex(r["message_id"]), "sender_node_id": src.identity.node_id,
                "recipient_node_id": dst.identity.node_id, "timestamp_ms": now_ms(), "kind": "text",
                "body": "pay 100"}
        _, ack = await session.conn.request(MsgType.APP_MESSAGE, body, 5)     # повтор того же сообщения
        inbox = [m["text"] for m in dst.messaging.inbox]
        await stop_nodes(nodes)
        return r, ack, inbox

    r, ack, inbox = run(go(), timeout=120)
    assert r["status"] == "delivered"
    assert ack["status"] == "duplicate"
    assert inbox == ["pay 100"]                                # прикладное действие выполнено один раз


def test_path_validation_and_loops(base):
    async def go():
        nodes = await start_nodes(base, 6, mode="tls")
        a, b, c = nodes[1], nodes[2], nodes[3]
        errors = []
        for path in ([b.self_contact(), b.self_contact(), c.self_contact()],      # повтор узла
                     [b.self_contact(), a.self_contact(), c.self_contact()],      # инициатор в пути
                     [c.self_contact()]):                                         # мало ретрансляторов
            try:
                await a.tunnels.build(path)
                errors.append(None)
            except TunnelError as exc:
                errors.append(str(exc))
        await stop_nodes(nodes)
        return errors

    errors = run(go())
    assert all(e is not None for e in errors)


def test_relay_refuses_duplicate_tunnel_id(base):
    async def go():
        nodes = await start_nodes(base, 6, mode="tls")
        a, r1, r2, d = nodes[1], nodes[2], nodes[3], nodes[4]
        tun = await a.tunnels.build([r1.self_contact(), r2.self_contact(), d.self_contact()])
        conn = await a.transport.get_connection(r1.cfg.LISTEN_PORT and "127.0.0.1", r1.cfg.LISTEN_PORT,
                                                r1.identity.node_id)
        body = {"tunnel_id": tun.tunnel_id, "sender": a.self_contact().to_wire(), "hop_index": 1, "max_hops": 8,
                "ttl_s": 60, "remaining": [r2.self_contact().to_wire(), d.self_contact().to_wire()]}
        frame, resp = await conn.request(MsgType.TUNNEL_BUILD, body, 5)
        too_long = dict(body, tunnel_id=os.urandom(16), max_hops=2)
        frame2, resp2 = await conn.request(MsgType.TUNNEL_BUILD, too_long, 5)
        await stop_nodes(nodes)
        return frame.type, resp, frame2.type, resp2

    t1, r1, t2, r2 = run(go())
    assert t1 == MsgType.TUNNEL_BUILD_FAIL and "loop" in r1["reason"]
    assert t2 == MsgType.TUNNEL_BUILD_FAIL and "max_hops" in r2["reason"]


@pytest.mark.parametrize("fault", ["stop", "freeze"])
def test_relay_failure_triggers_new_route(base, fault):
    async def go():
        nodes = await start_nodes(base, 9, mode="tls", TUNNEL_ACK_TIMEOUT_MS=800, TUNNEL_PROBE_INTERVAL_MS=300)
        src, dst = nodes[1], nodes[7]
        r1 = await src.send_message(dst.identity.node_id, "before failure")
        old_tunnel = src.messaging.sessions[dst.identity.node_id].tunnel
        victim = by_id(nodes, r1["route"][1 + (1 if fault == "stop" else 0)])
        if fault == "stop":
            await victim.stop()
        else:
            victim.transport.freeze(60)
        for _ in range(100):                                   # ждём автоматического восстановления
            await asyncio.sleep(0.1)
            if src.messaging.recoveries:
                break
        r2 = await src.send_message(dst.identity.node_id, "after failure")
        rec = src.messaging.recoveries[0] if src.messaging.recoveries else None
        inbox = [m["text"] for m in dst.messaging.inbox]
        await stop_nodes([n for n in nodes if n is not victim])
        if fault == "freeze":
            await victim.stop()
        return r1, r2, old_tunnel, victim, rec, inbox

    r1, r2, old, victim, rec, inbox = run(go(), timeout=150)
    assert old.state == DEAD
    assert rec is not None, "новый маршрут не построен автоматически"
    assert victim.identity.node_id.hex() not in rec["new_route"]
    assert r2["status"] == "delivered" and victim.identity.node_id.hex() not in r2["route"]
    assert inbox == ["before failure", "after failure"]


def test_no_direct_fallback_without_relays(base):
    async def go():
        nodes = await start_nodes(base, 3, mode="tls", MESSAGE_MAX_ATTEMPTS=2)
        src, dst = nodes[1], nodes[2]                    # кандидат в ретрансляторы только один
        r = await src.send_message(dst.identity.node_id, "must not bypass the tunnel")
        inbox = list(dst.messaging.inbox)
        await stop_nodes(nodes)
        return r, inbox

    r, inbox = run(go(), timeout=120)
    assert r["status"] == "failed" and inbox == []
