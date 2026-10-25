"""Двоичный кадр протокола и потоковый декодер.

Формат заголовка (24 байта, big-endian):
    version u8 | type u8 | flags u16 | request_id 16 байт | payload_length u32
TCP не сохраняет границы сообщений, поэтому декодер работает с произвольно
нарезанным потоком байтов: частичный заголовок, частичный payload и несколько
кадров в одном чтении обрабатываются одинаково.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass

from .constants import (ALLOWED_FLAGS_MASK, DEFAULT_MAX_FRAME_PAYLOAD, HEADER_SIZE, KNOWN_TYPES,
                        PROTOCOL_VERSION, REQUEST_ID_SIZE, ErrorCode, MsgType)

_HEADER = struct.Struct("!BBH16sI")
assert _HEADER.size == HEADER_SIZE


def new_request_id() -> bytes:
    """request_id генерируется криптографически стойким ГСЧ (os.urandom)."""
    return os.urandom(REQUEST_ID_SIZE)


@dataclass(frozen=True)
class Frame:
    type: int
    request_id: bytes
    payload: bytes = b""
    version: int = PROTOCOL_VERSION
    flags: int = 0

    @property
    def type_name(self) -> str:
        try:
            return MsgType(self.type).name
        except ValueError:
            return f"0x{self.type:02X}"


class NeedMoreData:
    """Маркер: в буфере пока нет полного кадра."""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "NeedMoreData"


NEED_MORE_DATA = NeedMoreData()


@dataclass(frozen=True)
class ProtocolError:
    """Нарушение формата кадра. После него поток рассинхронизирован — соединение закрывается."""
    code: str
    message: str
    request_id: bytes | None = None
    type: int | None = None


def encode_frame(frame: Frame, max_payload: int = DEFAULT_MAX_FRAME_PAYLOAD) -> bytes:
    if len(frame.request_id) != REQUEST_ID_SIZE:
        raise ValueError("request_id must be 16 bytes")
    if len(frame.payload) > max_payload:
        raise ValueError(f"payload {len(frame.payload)} > MAX_FRAME_PAYLOAD {max_payload}")
    return _HEADER.pack(frame.version, frame.type, frame.flags, frame.request_id,
                        len(frame.payload)) + frame.payload


class FrameDecoder:
    """Инкрементальный декодер: feed() добавляет прочитанные байты, next() отдаёт
    Frame | NEED_MORE_DATA | ProtocolError.

    Длина payload проверяется сразу после получения 24 байтов заголовка, до
    ожидания и накопления самого payload, поэтому завышенная длина не приводит к
    выделению буфера соответствующего размера. В буфере никогда не лежит больше
    HEADER_SIZE + max_payload + размер одного чтения.
    """

    def __init__(self, max_payload: int = DEFAULT_MAX_FRAME_PAYLOAD,
                 expected_version: int = PROTOCOL_VERSION):
        self.max_payload = max_payload
        self.expected_version = expected_version
        self._buf = bytearray()
        self._header: tuple | None = None
        self.failed: ProtocolError | None = None

    @property
    def buffered(self) -> int:
        return len(self._buf)

    @property
    def mid_frame(self) -> bool:
        """True, если в буфере есть начало незавершённого кадра (для T1.9)."""
        return bool(self._buf) or self._header is not None

    def feed(self, data: bytes) -> None:
        if self.failed is None:
            self._buf += data

    def next(self):
        if self.failed is not None:
            return self.failed
        if self._header is None:
            if len(self._buf) < HEADER_SIZE:
                return NEED_MORE_DATA
            version, mtype, flags, request_id, length = _HEADER.unpack_from(self._buf, 0)
            err = self._check_header(version, mtype, flags, request_id, length)
            if err is not None:
                self.failed = err
                self._buf.clear()
                return err
            del self._buf[:HEADER_SIZE]
            self._header = (version, mtype, flags, request_id, length)
        version, mtype, flags, request_id, length = self._header
        if len(self._buf) < length:
            return NEED_MORE_DATA
        payload = bytes(self._buf[:length])
        del self._buf[:length]
        self._header = None
        return Frame(type=mtype, request_id=request_id, payload=payload, version=version, flags=flags)

    def drain(self) -> list:
        """Все кадры, полностью находящиеся в буфере (и ошибка, если она случилась)."""
        out = []
        while True:
            item = self.next()
            if item is NEED_MORE_DATA:
                return out
            out.append(item)
            if isinstance(item, ProtocolError):
                return out

    def _check_header(self, version, mtype, flags, request_id, length):
        if version != self.expected_version:
            return ProtocolError(ErrorCode.BAD_VERSION, f"unsupported version {version}", request_id, mtype)
        if length > self.max_payload:
            return ProtocolError(ErrorCode.FRAME_TOO_LARGE,
                                 f"payload_length {length} > {self.max_payload}", request_id, mtype)
        if mtype not in KNOWN_TYPES:
            return ProtocolError(ErrorCode.UNKNOWN_TYPE, f"unknown type 0x{mtype:02X}", request_id, mtype)
        if flags & ~ALLOWED_FLAGS_MASK:
            return ProtocolError(ErrorCode.BAD_FLAGS, f"reserved flags set 0x{flags:04X}", request_id, mtype)
        return None


def decode_frame(byte_stream: bytes, max_payload: int = DEFAULT_MAX_FRAME_PAYLOAD):
    """Функциональная форма интерфейса из требований: разбирает первый кадр из байтов.
    Возвращает (Frame | NEED_MORE_DATA | ProtocolError, число использованных байтов)."""
    dec = FrameDecoder(max_payload)
    dec.feed(byte_stream)
    item = dec.next()
    used = len(byte_stream) - dec.buffered if isinstance(item, Frame) else 0
    return item, used
