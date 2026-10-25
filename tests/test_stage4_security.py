"""Этап 4: TLS 1.3 со взаимной аутентификацией и отрицательные тесты (раздел 11.4 ТЗ).

Успешный отрицательный тест = сообщение отклонено и прикладное действие не выполнено.
Активный нарушитель моделируется TCP-tap'ом в разрыве соединения (overlay.experiment.tap).
"""
import asyncio
import collections
import socket
import ssl

import pytest

from conftest import run, start_nodes, stop_nodes
from overlay.experiment.tap import TcpTap, pcap_contains
from overlay.protocol import Frame, MsgType, encode_frame, encode_payload, new_request_id
from overlay.transport import RpcError

TLS_HANDSHAKE, TLS_APPDATA = 0x16, 0x17


class RecordMutator:
    """Разбирает поток одного направления: сначала открытый HELLO-кадр, затем записи TLS.
    on_record(content_type, index, record) -> список записей для пересылки."""

    def __init__(self, on_record=None, on_hello=None):
        self.buf = b""
        self.hello_done = False
        self.counts = collections.Counter()
        self.on_record = on_record or (lambda t, i, r: [r])
        self.on_hello = on_hello or (lambda b: b)

    def __call__(self, data, conn_idx):
        self.buf += data
        out = b""
        if not self.hello_done:
            if len(self.buf) < 24:
                return b""
            total = 24 + int.from_bytes(self.buf[20:24], "big")
            if len(self.buf) < total:
                return b""
            out += self.on_hello(self.buf[:total])
            self.buf = self.buf[total:]
            self.hello_done = True
        while len(self.buf) >= 5:
            total = 5 + int.from_bytes(self.buf[3:5], "big")
            if len(self.buf) < total:
                break
            rec, self.buf = self.buf[:total], self.buf[total:]
            idx = self.counts[rec[0]]
            self.counts[rec[0]] += 1
            out += b"".join(self.on_record(rec[0], idx, rec))
        return out


def flip(b: bytes, pos: int) -> bytes:
    return b[:pos] + bytes([b[pos] ^ 0x01]) + b[pos + 1:]


async def secure_pair(base, **over):
    nodes = await start_nodes(base, 2, mode="tls", join=False, **over)
    server, client = nodes
    calls = []
    orig = server.dispatcher.get(MsgType.ECHO_REQUEST).func

    async def counting_echo(conn, frame, body):
        calls.append(body["data"])                    # «прикладное действие»
        return await orig(conn, frame, body)

    server.dispatcher.register(MsgType.ECHO_REQUEST, counting_echo)
    return server, client, calls


async def echo_via(client, port, expected, data=b"payload"):
    return await client.rpc.echo("127.0.0.1", port, data, expected)


def test_mutual_tls13_established_and_bound_to_node_ids(base):
    async def go():
        server, client, calls = await secure_pair(base)
        await client.rpc.ping("127.0.0.1", server.cfg.LISTEN_PORT, server.identity.node_id)
        out = client.transport.outbound[("127.0.0.1", server.cfg.LISTEN_PORT)]
        inc = next(iter(server.transport.inbound))
        await stop_nodes([server, client])
        return out, inc, server, client

    out, inc, server, client = run(go())
    assert out.peer_node_id == server.identity.node_id        # клиент проверил сервер
    assert inc.peer_node_id == client.identity.node_id        # сервер проверил клиента (mTLS)
    assert out.stream.version == "TLSv1.3" and inc.stream.version == "TLSv1.3"
    assert out.session_id == inc.session_id


def test_identity_public_key_substitution_rejected(base):
    async def go():
        nodes = await start_nodes(base, 3, mode="tls", join=False)
        victim, attacker, client = nodes
        # Нарушитель объявляет NodeID жертвы, но предъявляет сертификат своего ключа.
        attacker.identity.node_id = victim.identity.node_id
        try:
            await client.rpc.ping("127.0.0.1", attacker.cfg.LISTEN_PORT, victim.identity.node_id)
            outcome = "accepted"
        except RpcError as exc:
            outcome = exc.kind
        await stop_nodes(nodes)
        return outcome

    assert run(go()) == "identity"


def test_node_id_mismatch_with_expected_rejected(base):
    async def go():
        server, client, calls = await secure_pair(base)
        try:
            await echo_via(client, server.cfg.LISTEN_PORT, b"\x11" * 32)
            outcome = "accepted"
        except RpcError as exc:
            outcome = exc.kind
        await stop_nodes([server, client])
        return outcome, calls

    outcome, calls = run(go())
    assert outcome == "identity" and calls == []


def _with_tap(base, c2s=None, s2c=None, pcap=None, action=None):
    async def go():
        server, client, calls = await secure_pair(base)
        tap = TcpTap(0, server.cfg.LISTEN_PORT, c2s=c2s, s2c=s2c, pcap_path=pcap)
        await tap.start()
        outcome = "accepted"
        try:
            if action:
                outcome = await action(client, tap, server)
            else:
                await echo_via(client, tap.listen_port, server.identity.node_id)
        except RpcError as exc:
            outcome = exc.kind
        await asyncio.sleep(0.2)
        inbound_open = len(server.transport.inbound)
        await tap.stop()
        await stop_nodes([server, client])
        return outcome, calls, inbound_open, tap
    return run(go())


def test_tampered_server_ephemeral_key_rejected(base):
    # Последние 32 байта ServerHello — открытый X25519-ключ сервера (key_share).
    def on_record(ctype, idx, rec):
        if ctype == TLS_HANDSHAKE and idx == 0:
            hs_end = 5 + 4 + int.from_bytes(rec[6:9], "big")
            return [flip(rec, hs_end - 1)]
        return [rec]

    outcome, calls, _, _ = _with_tap(base, s2c=RecordMutator(on_record))
    assert outcome != "accepted" and calls == []


def test_tampered_client_hello_random_rejected(base):
    def on_record(ctype, idx, rec):
        if ctype == TLS_HANDSHAKE and idx == 0:
            return [flip(rec, 5 + 4 + 2 + 7)]          # байт client random
        return [rec]

    outcome, calls, _, _ = _with_tap(base, c2s=RecordMutator(on_record))
    assert outcome != "accepted" and calls == []


def test_tampered_plaintext_hello_nonce_detected(base):
    def on_hello(frame):
        i = frame.find(b"\xa5nonce\xc4\x10")
        return flip(frame, i + 8)

    outcome, calls, _, _ = _with_tap(base, c2s=RecordMutator(on_hello=on_hello))
    assert outcome != "accepted" and calls == []


def _first_app_record(idx):
    # Записи 0x17 клиента: 0 — Finished, 1 — подтверждение транскрипта, 2.. — прикладные кадры.
    return idx >= 2


def test_modified_ciphertext_rejected(base):
    def on_record(ctype, idx, rec):
        if ctype == TLS_APPDATA and idx == 2:
            return [flip(rec, len(rec) - 3)]
        return [rec]

    outcome, calls, inbound_open, _ = _with_tap(base, c2s=RecordMutator(on_record))
    assert outcome != "accepted"
    assert calls == []                     # изменённая запись не дошла до обработчика
    assert inbound_open == 0               # соединение закрыто


def test_replay_from_finished_session_rejected(base):
    async def action(client, tap, server):
        await echo_via(client, tap.listen_port, server.identity.node_id, b"original")
        client.transport.drop_connection("127.0.0.1", tap.listen_port, "test done")
        await asyncio.sleep(0.2)
        recorded = b"".join(d for direction, d in tap.recorded[0] if direction == "c2s")
        # Нарушитель воспроизводит весь записанный поток клиента в новом соединении.
        r, w = await asyncio.open_connection("127.0.0.1", server.cfg.LISTEN_PORT)
        w.write(recorded)
        await w.drain()
        try:
            await asyncio.wait_for(r.read(65536), 2)
            await asyncio.wait_for(r.read(65536), 2)
        except (asyncio.TimeoutError, ConnectionError):
            pass
        w.close()
        return "replayed"

    outcome, calls, _, _ = _with_tap(base, action=action)
    assert calls == [b"original"]          # повтор не вызвал второго прикладного действия


def test_tls12_and_plain_clients_refused(base):
    async def go():
        server, client, calls = await secure_pair(base)
        port = server.cfg.LISTEN_PORT
        # 1) клиент без HELLO сразу начинает TLS 1.2
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        refused = []
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port, ssl=ctx), 3)
            w.close()
            refused.append(False)
        except (ssl.SSLError, ConnectionError, asyncio.TimeoutError, OSError):
            refused.append(True)
        # 2) открытый кадр ECHO без защищённой сессии
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(encode_frame(Frame(int(MsgType.ECHO_REQUEST), new_request_id(),
                                   encode_payload(MsgType.ECHO_REQUEST, {"data": b"plain"}))))
        await w.drain()
        data = await asyncio.wait_for(r.read(100), 3)
        refused.append(data == b"")
        w.close()
        await stop_nodes([server, client])
        return refused, calls

    refused, calls = run(go())
    assert refused == [True, True] and calls == []


def test_tls_context_disables_tickets_and_old_versions(base):
    async def go():
        server, client, _ = await secure_pair(base)
        ctx = server.security._context(True, client.security.cert_der)
        await stop_nodes([server, client])
        return ctx

    ctx = run(go())
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_3
    assert ctx.options & ssl.OP_NO_TICKET           # нет session tickets -> нет возобновления и 0-RTT
    assert ctx.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize("mode,expect_visible", [("plain", True), ("tls", False)])
def test_pcap_plaintext_visibility(base, tmp_path, mode, expect_visible):
    """Контроль прибора: в режиме plain поиск по pcap находит открытые поля контакта,
    в режиме tls — нет; секретный маркер прикладных данных не виден никогда."""
    pcap = str(tmp_path / f"{mode}.pcap")

    async def go():
        nodes = await start_nodes(base, 2, mode=mode, join=False)
        server, client = nodes
        tap = TcpTap(0, server.cfg.LISTEN_PORT, pcap_path=pcap)
        await tap.start()
        await client.rpc.ping("127.0.0.1", tap.listen_port, server.identity.node_id)
        await client.rpc.echo("127.0.0.1", tap.listen_port, b"ECHO-MARKER-123", server.identity.node_id)
        await asyncio.sleep(0.1)
        await tap.stop()
        await stop_nodes(nodes)

    run(go())
    assert pcap_contains(pcap, b"127.0.0.1") == expect_visible
    assert pcap_contains(pcap, b"ECHO-MARKER-123") == expect_visible
