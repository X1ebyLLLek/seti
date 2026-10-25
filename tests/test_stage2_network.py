"""Этап 2: PING/FIND_NODE, bootstrap, многопереходный lookup, отказ bootstrap (T2.10–T2.14)."""
import pytest

from conftest import run, start_nodes, stop_nodes
from overlay.protocol import MsgType
from overlay.routing import xor_distance


def test_t2_10_ping_pong_same_request_id(base):
    async def go():
        a, b = await start_nodes(base, 2, join=False)
        conn = await a.transport.get_connection("127.0.0.1", b.cfg.LISTEN_PORT)
        body = {"sender": a.self_contact().to_wire(), "timestamp_ms": 777}
        rid_holder = {}
        orig_send = conn.send

        async def spy(mtype, body, request_id=None):
            rid_holder["rid"] = request_id
            return await orig_send(mtype, body, request_id)

        conn.send = spy
        frame, pong = await conn.request(MsgType.PING, body, 2)
        await stop_nodes([a, b])
        return rid_holder["rid"], frame, pong, b

    rid, frame, pong, b = run(go())
    assert frame.type == MsgType.PONG and frame.request_id == rid
    assert pong["responder"]["node_id"] == b.identity.node_id and pong["ping_timestamp_ms"] == 777


def test_t2_11_find_node_sorted_limited_unique(base):
    async def go():
        nodes = await start_nodes(base, 10, K_BUCKET_SIZE=3)
        target = nodes[5].identity.node_id
        responder = nodes[0]
        contacts = await nodes[3].rpc.find_node(responder.self_contact(), target)
        await stop_nodes(nodes)
        return contacts, target, nodes[3].identity.node_id

    contacts, target, requester = run(go())
    ids = [c.node_id for c in contacts]
    assert 0 < len(ids) <= 3 and len(set(ids)) == len(ids)
    dists = [xor_distance(i, target) for i in ids]
    assert dists == sorted(dists)
    assert requester not in ids


def test_t2_12_bootstrap_gives_verified_contact(base):
    async def go():
        nodes = await start_nodes(base, 2, join=False)
        await nodes[0].join([])
        res = await nodes[1].join([f"127.0.0.1:{nodes[0].cfg.LISTEN_PORT}"])
        c = nodes[1].table.find(nodes[0].identity.node_id)
        await stop_nodes(nodes)
        return res, c

    res, c = run(go())
    assert res.ok and res.peers_ok and res.self_lookup is not None
    assert c is not None and c.last_verified_ms > 0


def test_bootstrap_with_pinned_node_id(base):
    async def go():
        nodes = await start_nodes(base, 2, join=False, mode="tls")
        await nodes[0].join([])
        good = await nodes[1].join([f"{nodes[0].identity.node_id.hex()}@127.0.0.1:{nodes[0].cfg.LISTEN_PORT}"])
        wrong = await nodes[1].join([f"{'ab' * 32}@127.0.0.1:{nodes[0].cfg.LISTEN_PORT}"])
        await stop_nodes(nodes)
        return good, wrong

    good, wrong = run(go())
    assert good.ok
    assert not wrong.peers_ok                       # узел с другим NodeID отвергнут


def _absent_pairs(nodes):
    pairs = []
    for i, a in enumerate(nodes):
        for j, b in enumerate(nodes):
            if i != j and b.identity.node_id not in a.table:
                pairs.append((i, j))
    return pairs


@pytest.mark.parametrize("scheme", ["star", "ring"])
def test_t2_13_multihop_lookup_target_absent(base, scheme):
    async def go():
        nodes = await start_nodes(base, 16, K_BUCKET_SIZE=3, scheme=scheme)
        pairs = _absent_pairs(nodes)
        assert pairs, "в сети из 16 узлов с k=3 должны быть пары с неизвестной целью"
        results = []
        for i, j in pairs[:5]:
            results.append(await nodes[i].lookup_node(nodes[j].identity.node_id))
        sizes = [n.table.size() for n in nodes]
        await stop_nodes(nodes)
        return results, sizes

    results, sizes = run(go(), timeout=120)
    assert sum(s < 15 for s in sizes) >= 0.8 * len(sizes)          # таблицы не полные
    for r in results:
        assert not r.target_in_table_before
        assert r.success
        assert len(r.intermediate_nodes) >= 1                        # через промежуточный узел
        assert r.rpc_count >= 2


def test_t2_14_lookup_works_after_bootstrap_stops(base):
    async def go():
        nodes = await start_nodes(base, 12, K_BUCKET_SIZE=3)
        await nodes[0].stop()                                        # bootstrap-узел отключён
        rest = nodes[1:]
        pairs = [(i, j) for i, j in _absent_pairs(rest)]
        out = []
        for i, j in pairs[:4]:
            out.append(await rest[i].lookup_node(rest[j].identity.node_id))
        await stop_nodes(rest)
        return out

    out = run(go(), timeout=120)
    assert out and all(r.success for r in out)


def test_lookup_survives_frozen_intermediate(base):
    async def go():
        nodes = await start_nodes(base, 12, K_BUCKET_SIZE=3, READ_TIMEOUT_MS=800)
        pairs = _absent_pairs(nodes)
        i, j = pairs[0]
        initiator, target = nodes[i], nodes[j].identity.node_id
        first = initiator.table.closest(target, 3)
        victim = next(n for n in nodes if n.identity.node_id == first[0].node_id and n is not nodes[j])
        victim.transport.freeze(30)                                  # промежуточный кандидат «завис»
        r = await initiator.lookup_node(target)
        await stop_nodes(nodes)
        return r, victim.identity.node_id

    r, victim = run(go(), timeout=120)
    assert r.success
    assert any(t.startswith(victim.hex()[:8]) for t in r.timeouts)
