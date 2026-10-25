"""Константы протокола версии 1: размеры заголовка, коды типов сообщений."""
from __future__ import annotations

import enum

PROTOCOL_VERSION = 1
HEADER_SIZE = 24                 # version(1) + type(1) + flags(2) + request_id(16) + payload_length(4)
REQUEST_ID_SIZE = 16
DEFAULT_MAX_FRAME_PAYLOAD = 65536
NODE_ID_SIZE = 32                # 256 бит
NODE_ID_BITS = 256


class MsgType(enum.IntEnum):
    """Коды типов сообщений. 0x01–0x04 и 0x7F зафиксированы требованиями этапов 1–2,
    0x05–0x3F заняты последующими этапами из зарезервированного диапазона."""
    PING = 0x01
    PONG = 0x02
    FIND_NODE_REQUEST = 0x03
    FIND_NODE_RESPONSE = 0x04
    STORE_REQUEST = 0x05
    STORE_RESPONSE = 0x06
    FIND_VALUE_REQUEST = 0x07
    FIND_VALUE_RESPONSE = 0x08
    SECURE_HELLO = 0x09
    SECURE_HELLO_ACK = 0x0A
    TUNNEL_BUILD = 0x10
    TUNNEL_BUILD_OK = 0x11
    TUNNEL_BUILD_FAIL = 0x12
    TUNNEL_DATA = 0x13
    TUNNEL_ACK = 0x14
    TUNNEL_CLOSE = 0x15
    APP_MESSAGE = 0x20
    APP_ACK = 0x21
    ECHO_REQUEST = 0x70          # диагностический: передача кадров переменной длины (этап 1)
    ECHO_RESPONSE = 0x71
    ERROR = 0x7F


KNOWN_TYPES = frozenset(int(t) for t in MsgType)

# Пары «запрос -> ожидаемый ответ» для RPC-корреляции по request_id.
RESPONSE_FOR = {
    MsgType.PING: MsgType.PONG,
    MsgType.FIND_NODE_REQUEST: MsgType.FIND_NODE_RESPONSE,
    MsgType.STORE_REQUEST: MsgType.STORE_RESPONSE,
    MsgType.FIND_VALUE_REQUEST: MsgType.FIND_VALUE_RESPONSE,
    MsgType.SECURE_HELLO: MsgType.SECURE_HELLO_ACK,
    MsgType.TUNNEL_BUILD: MsgType.TUNNEL_BUILD_OK,
    MsgType.APP_MESSAGE: MsgType.APP_ACK,
    MsgType.ECHO_REQUEST: MsgType.ECHO_RESPONSE,
}
# Типы, которые допустимы только как ответ на ожидающий запрос.
RESPONSE_TYPES = frozenset({
    MsgType.PONG, MsgType.FIND_NODE_RESPONSE, MsgType.STORE_RESPONSE,
    MsgType.FIND_VALUE_RESPONSE, MsgType.SECURE_HELLO_ACK, MsgType.TUNNEL_BUILD_OK,
    MsgType.TUNNEL_BUILD_FAIL, MsgType.APP_ACK, MsgType.ECHO_RESPONSE,
})
# Флаги: в версии 1 тип сообщения однозначно задаёт направление, поэтому поле flags
# зарезервировано (признаки ответа/ошибки/фрагментации) и обязано быть равно 0.
ALLOWED_FLAGS_MASK = 0x0000


class ErrorCode:
    BAD_VERSION = "BAD_VERSION"
    UNKNOWN_TYPE = "UNKNOWN_TYPE"
    BAD_FLAGS = "BAD_FLAGS"
    FRAME_TOO_LARGE = "FRAME_TOO_LARGE"
    DECODE_ERROR = "DECODE_ERROR"
    SCHEMA_ERROR = "SCHEMA_ERROR"
    BAD_DIRECTION = "BAD_DIRECTION"
    UNEXPECTED_RESPONSE = "UNEXPECTED_RESPONSE"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    INVALID_CONTACT = "INVALID_CONTACT"
    INVALID_RECORD = "INVALID_RECORD"
    ROLLBACK = "ROLLBACK"
    TUNNEL_REFUSED = "TUNNEL_REFUSED"
    INTERNAL = "INTERNAL"
