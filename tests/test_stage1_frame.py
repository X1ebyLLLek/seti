"""Этап 1: кодек кадра и сериализация (T1.1–T1.8 на уровне декодера)."""
import random

import msgpack
import pytest

from overlay.protocol import (HEADER_SIZE, NEED_MORE_DATA, ErrorCode, Frame, FrameDecoder, MsgType, ProtocolError,
                              decode_frame, decode_payload, encode_frame, encode_payload, new_request_id)
from overlay.protocol.codec import DecodeError
from overlay.protocol.messages import SchemaError

MAX = 65536


def frame(mtype=MsgType.ECHO_REQUEST, size=10, rid=None):
    return Frame(type=int(mtype), request_id=rid or new_request_id(), payload=bytes(range(256)) * (size // 256) +
                 bytes(size % 256))


def test_t1_1_roundtrip_all_fields():
    for mtype in MsgType:
        f = Frame(type=int(mtype), request_id=new_request_id(), payload=b"\x01\x02payload")
        got, used = decode_frame(encode_frame(f))
        assert isinstance(got, Frame)
        assert (got.version, got.type, got.flags, got.request_id, got.payload) == \
               (1, int(mtype), 0, f.request_id, f.payload)
        assert used == HEADER_SIZE + len(f.payload)


def test_header_is_24_bytes_big_endian():
    f = Frame(type=0x03, request_id=bytes(range(16)), payload=b"abc")
    raw = encode_frame(f)
    assert raw[:24] == bytes([1, 3, 0, 0]) + bytes(range(16)) + (3).to_bytes(4, "big")


def test_t1_2_partial_header():
    raw = encode_frame(frame(size=0))
    dec = FrameDecoder(MAX)
    for i in range(HEADER_SIZE - 1):
        dec.feed(raw[i:i + 1])
        assert dec.next() is NEED_MORE_DATA
    dec.feed(raw[HEADER_SIZE - 1:])
    assert isinstance(dec.next(), Frame)


def test_t1_3_partial_payload():
    f = frame(size=1000)
    raw = encode_frame(f)
    dec = FrameDecoder(MAX)
    dec.feed(raw[:HEADER_SIZE + 500])
    assert dec.next() is NEED_MORE_DATA
    assert dec.next() is NEED_MORE_DATA
    dec.feed(raw[HEADER_SIZE + 500:])
    got = dec.next()
    assert isinstance(got, Frame) and got.payload == f.payload


def test_t1_4_glued_frames_in_order():
    frames = [frame(size=s) for s in (0, 5, 300, 70)]
    dec = FrameDecoder(MAX)
    dec.feed(b"".join(encode_frame(f) for f in frames))
    got = dec.drain()
    assert [g.request_id for g in got] == [f.request_id for f in frames]
    assert [g.payload for g in got] == [f.payload for f in frames]


def test_t1_5_hundred_frames_random_split():
    rnd = random.Random(7)
    frames = [frame(size=rnd.choice([0, 1, 23, 24, 25, rnd.randint(0, 2000), rnd.randint(0, MAX)]))
              for _ in range(100)]
    stream = b"".join(encode_frame(f) for f in frames)
    dec = FrameDecoder(MAX)
    got, pos = [], 0
    while pos < len(stream):
        n = rnd.randint(1, 5000)
        dec.feed(stream[pos:pos + n])
        pos += n
        got.extend(dec.drain())
    assert len(got) == 100
    assert all(isinstance(g, Frame) for g in got)
    assert [(g.request_id, g.payload) for g in got] == [(f.request_id, f.payload) for f in frames]


def test_t1_6_oversized_length_rejected_before_payload():
    header = bytes([1, int(MsgType.ECHO_REQUEST), 0, 0]) + new_request_id() + (MAX + 1).to_bytes(4, "big")
    dec = FrameDecoder(MAX)
    dec.feed(header)                       # ни одного байта payload ещё не пришло
    err = dec.next()
    assert isinstance(err, ProtocolError) and err.code == ErrorCode.FRAME_TOO_LARGE
    assert dec.buffered == 0               # буфер под payload не создавался и не копится
    dec.feed(b"x" * 1000)
    assert dec.buffered == 0               # после ошибки поток не принимается


def test_exact_max_payload_accepted():
    f = Frame(type=int(MsgType.ECHO_REQUEST), request_id=new_request_id(), payload=b"z" * MAX)
    got, _ = decode_frame(encode_frame(f, MAX), MAX)
    assert isinstance(got, Frame) and len(got.payload) == MAX
    with pytest.raises(ValueError):
        encode_frame(Frame(type=1, request_id=new_request_id(), payload=b"z" * (MAX + 1)), MAX)


def test_t1_7_unknown_version():
    raw = bytearray(encode_frame(frame()))
    raw[0] = 2
    err, _ = decode_frame(bytes(raw))
    assert isinstance(err, ProtocolError) and err.code == ErrorCode.BAD_VERSION


def test_t1_8_unknown_type():
    raw = bytearray(encode_frame(frame()))
    raw[1] = 0x55
    err, _ = decode_frame(bytes(raw))
    assert isinstance(err, ProtocolError) and err.code == ErrorCode.UNKNOWN_TYPE


def test_reserved_flags_rejected():
    raw = bytearray(encode_frame(frame()))
    raw[3] = 1
    err, _ = decode_frame(bytes(raw))
    assert isinstance(err, ProtocolError) and err.code == ErrorCode.BAD_FLAGS


def test_request_ids_are_random_and_unique():
    ids = {new_request_id() for _ in range(10000)}
    assert len(ids) == 10000


# ---------------------------------------------------------------- сериализация payload
def test_payload_schema_roundtrip_and_determinism():
    body = {"code": "X", "message": "m"}
    a = encode_payload(MsgType.ERROR, body)
    b = encode_payload(MsgType.ERROR, {"message": "m", "code": "X"})
    assert a == b                                   # канонический порядок ключей
    assert decode_payload(MsgType.ERROR, a) == body


def test_payload_unknown_field_rejected():
    raw = msgpack.packb({"code": "X", "message": "m", "extra": 1})
    with pytest.raises(SchemaError):
        decode_payload(MsgType.ERROR, raw)


def test_payload_missing_field_and_wrong_type_rejected():
    with pytest.raises(SchemaError):
        decode_payload(MsgType.ERROR, msgpack.packb({"code": "X"}))
    with pytest.raises(SchemaError):
        decode_payload(MsgType.PING, msgpack.packb({"sender": "not a contact", "timestamp_ms": 1}))


def test_payload_ext_types_and_garbage_rejected():
    ext = msgpack.packb({"code": msgpack.ExtType(5, b"evil"), "message": "m"})
    with pytest.raises(DecodeError):
        decode_payload(MsgType.ERROR, ext)
    with pytest.raises(DecodeError):
        decode_payload(MsgType.ERROR, b"\xc1\xff\x00garbage")


def test_payload_size_limits():
    huge = msgpack.packb({"code": "X", "message": "a" * 20000})
    with pytest.raises((DecodeError, SchemaError)):
        decode_payload(MsgType.ERROR, huge)
    deep = {"code": "X", "message": "m"}
    nested = [[[[[[[[1]]]]]]]]
    with pytest.raises(DecodeError):
        decode_payload(MsgType.ERROR, msgpack.packb({**deep, "x": nested}))
