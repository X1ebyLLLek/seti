"""Этап 1: TCP-транспорт — реальные сокеты, разбиение потока, ошибки протокола (T1.5–T1.10)."""
import asyncio
import random
import socket

import pytest

from conftest import node_config, run
from overlay.config import NodeConfig
from overlay.metrics import EventLog
from overlay.node import Node
from overlay.protocol import (NEED_MORE_DATA, ErrorCode, Frame, FrameDecoder, MsgType, decode_payload, encode_frame,
                              encode_payload, new_request_id)
from overlay.transport import Dispatcher, RpcError, Transport


async def echo_server(base):
    calls = []

    async def on_echo(conn, frame, body):
        calls.append(frame.request_id)
        return MsgType.ECHO_RESPONSE, {"data": body["data"]}

    d = Dispatcher()
    d.register(MsgType.ECHO_REQUEST, on_echo)
    cfg = NodeConfig(NODE_STATE_DIR=str(base), LISTEN_PORT=0, SECURITY_MODE="plain", READ_TIMEOUT_MS=1500)
    t = Transport(cfg, d, EventLog(str(base), "DEBUG"))
    await t.start()
    return t, calls


async def read_frames(reader, count, timeout=5.0):
    dec, out = FrameDecoder(), []
    while len(out) < count:
        data = await asyncio.wait_for(reader.read(65536), timeout)
        if not data:
            break
        dec.feed(data)
        out.extend(dec.drain())
    return out


def echo_frame(data: bytes, rid=None) -> bytes:
    return encode_frame(Frame(int(MsgType.ECHO_REQUEST), rid or new_request_id(),
                              encode_payload(MsgType.ECHO_REQUEST, {"data": data})))


def test_t1_5_hundred_variable_frames_over_tcp_random_chunks(base):
    async def go():
        t, calls = await echo_server(base)
        rnd = random.Random(42)
        sent = {}
        stream = b""
        for _ in range(100):
            data = rnd.randbytes(rnd.choice([0, 1, 100, rnd.randint(0, 60000)]))
            rid = new_request_id()
            sent[rid] = data
            stream += echo_frame(data, rid)
        reader, writer = await asyncio.open_connection("127.0.0.1", t.cfg.LISTEN_PORT)
        pos = 0
        while pos < len(stream):                      # произвольное разбиение и склейка
            n = rnd.randint(1, 9000)
            writer.write(stream[pos:pos + n])
            await writer.drain()
            pos += n
            if rnd.random() < 0.2:
                await asyncio.sleep(0.001)
        frames = await read_frames(reader, 100)
        writer.close()
        await t.stop()
        return sent, frames, calls

    sent, frames, calls = run(go())
    assert len(frames) == 100 and len(calls) == 100
    for f in frames:
        assert f.type == MsgType.ECHO_RESPONSE
        assert decode_payload(MsgType.ECHO_RESPONSE, f.payload)["data"] == sent[f.request_id]


@pytest.mark.parametrize("mutate,code", [
    (lambda raw: bytes([2]) + raw[1:], ErrorCode.BAD_VERSION),             # T1.7
    (lambda raw: raw[:1] + bytes([0x55]) + raw[2:], ErrorCode.UNKNOWN_TYPE),  # T1.8
    (lambda raw: raw[:20] + (70000).to_bytes(4, "big"), ErrorCode.FRAME_TOO_LARGE),  # T1.6 по TCP
    (lambda raw: raw[:24] + b"\xc1garbage", ErrorCode.DECODE_ERROR),
])
def test_bad_frames_get_error_and_close_but_server_survives(base, mutate, code):
    async def go():
        t, calls = await echo_server(base)
        raw = echo_frame(b"hello")
        bad = mutate(raw)
        if code == ErrorCode.DECODE_ERROR:
            bad = bad[:20] + (len(bad) - 24).to_bytes(4, "big") + bad[24:]
        reader, writer = await asyncio.open_connection("127.0.0.1", t.cfg.LISTEN_PORT)
        writer.write(bad)
        await writer.drain()
        frames = await read_frames(reader, 1)
        tail = await asyncio.wait_for(reader.read(100), 3)        # затем соединение закрывается
        writer.close()
        # Сервер продолжает работать для других соединений.
        r2, w2 = await asyncio.open_connection("127.0.0.1", t.cfg.LISTEN_PORT)
        w2.write(echo_frame(b"still alive"))
        ok = await read_frames(r2, 1)
        w2.close()
        await t.stop()
        return frames, tail, ok, calls

    frames, tail, ok, calls = run(go())
    assert frames and frames[0].type == MsgType.ERROR
    assert decode_payload(MsgType.ERROR, frames[0].payload)["code"] == code
    assert tail == b""
    assert ok and decode_payload(MsgType.ECHO_RESPONSE, ok[0].payload)["data"] == b"still alive"
    assert len(calls) == 1                         # плохой кадр не дошёл до обработчика


def test_t1_9_disconnect_inside_frame_releases_resources(base):
    async def go():
        t, calls = await echo_server(base)
        raw = echo_frame(b"x" * 5000)
        reader, writer = await asyncio.open_connection("127.0.0.1", t.cfg.LISTEN_PORT)
        writer.write(raw[:24 + 1000])               # заголовок и часть payload
        await writer.drain()
        await asyncio.sleep(0.2)
        inbound_before = len(t.inbound)
        writer.close()
        await asyncio.sleep(0.3)
        inbound_after = len(t.inbound)
        await t.stop()
        return calls, inbound_before, inbound_after

    calls, before, after = run(go())
    assert before == 1 and after == 0              # соединение закрыто и удалено
    assert calls == []                             # частичный кадр не стал сообщением
    log = open(f"{base}/logs/node.log", encoding="utf-8").read()
    assert "peer closed mid-frame" in log


def test_t1_10_response_with_other_request_id_not_accepted(base):
    async def fake_server(reader, writer):
        dec = FrameDecoder()
        while True:
            data = await reader.read(65536)
            if not data:
                return
            dec.feed(data)
            for f in dec.drain():
                if f.type != MsgType.PING:
                    continue
                req = decode_payload(MsgType.PING, f.payload)
                body = {"responder": req["sender"], "ping_timestamp_ms": req["timestamp_ms"],
                        "responder_timestamp_ms": 1}
                # Ответ с ЧУЖИМ request_id.
                writer.write(encode_frame(Frame(int(MsgType.PONG), new_request_id(),
                                                encode_payload(MsgType.PONG, body))))
                await writer.drain()

    async def go():
        server = await asyncio.start_server(fake_server, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        node = Node(node_config(base, 0, READ_TIMEOUT_MS=1500, PING_TIMEOUT_MS=1500))
        await node.start()
        try:
            await node.rpc.ping("127.0.0.1", port)
            outcome = "accepted"
        except RpcError as exc:
            outcome = exc.kind
        await node.stop()
        server.close()
        return outcome

    assert run(go()) in ("closed", "timeout")      # ответ с другим request_id не принят


def test_ping_pong_same_request_id_raw(base):
    async def go():
        node = Node(node_config(base, 0))
        await node.start()
        other = Node(node_config(base, 1, LISTEN_PORT=40001))      # нужен только его контакт
        reader, writer = await asyncio.open_connection("127.0.0.1", node.cfg.LISTEN_PORT)
        rid = new_request_id()
        body = {"sender": other.self_contact().to_wire(), "timestamp_ms": 12345}
        writer.write(encode_frame(Frame(int(MsgType.PING), rid, encode_payload(MsgType.PING, body))))
        await writer.drain()
        frames = await read_frames(reader, 1)
        writer.close()
        await node.stop()
        other.log.close()
        return rid, frames, node

    rid, frames, node = run(go())
    assert frames[0].type == MsgType.PONG and frames[0].request_id == rid
    pong = decode_payload(MsgType.PONG, frames[0].payload)
    assert pong["ping_timestamp_ms"] == 12345
    assert pong["responder"]["node_id"] == node.identity.node_id


def test_read_timeout_and_connect_refused(base):
    async def silent(reader, writer):
        await asyncio.sleep(10)

    async def go():
        server = await asyncio.start_server(silent, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        node = Node(node_config(base, 0, READ_TIMEOUT_MS=500, PING_TIMEOUT_MS=500))
        await node.start()
        kinds = []
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        try:
            await node.rpc.ping("127.0.0.1", port)
        except RpcError as exc:
            kinds.append(exc.kind)
        elapsed = loop.time() - t0
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
        s.close()
        try:
            await node.rpc.ping("127.0.0.1", free_port)
        except RpcError as exc:
            kinds.append(exc.kind)
        await node.stop()
        server.close()
        return kinds, elapsed

    kinds, elapsed = run(go())
    # Windows отвечает отказом на loopback-порт с задержкой ~2 с, поэтому закрытый порт
    # может проявиться и как тайм-аут подключения — оба исхода ограничены по времени.
    assert kinds[0] == "timeout" and kinds[1] in ("connect", "timeout")
    assert elapsed < 2.0                           # ожидание ограничено READ/PING_TIMEOUT


def test_decoder_needs_more_after_partial_reads():
    dec = FrameDecoder()
    raw = echo_frame(b"abc")
    dec.feed(raw[:10])
    assert dec.next() is NEED_MORE_DATA and dec.mid_frame
