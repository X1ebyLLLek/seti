"""Схемы payload всех типов сообщений и их строгая проверка.

Правило для неизвестных полей: payload с полем, которого нет в схеме, отклоняется
(SCHEMA_ERROR). Необязательные поля перечислены в схеме явно.
"""
from __future__ import annotations

from .codec import DecodeError, pack, unpack
from .constants import MsgType


class SchemaError(Exception):
    pass


class Field:
    def check(self, value, path: str):
        raise NotImplementedError


class Bytes(Field):
    def __init__(self, size: int | None = None, max_len: int | None = None, min_len: int = 0):
        self.size, self.max_len, self.min_len = size, max_len, min_len

    def check(self, value, path):
        if not isinstance(value, bytes):
            raise SchemaError(f"{path}: expected bytes")
        if self.size is not None and len(value) != self.size:
            raise SchemaError(f"{path}: expected {self.size} bytes, got {len(value)}")
        if self.max_len is not None and len(value) > self.max_len:
            raise SchemaError(f"{path}: longer than {self.max_len}")
        if len(value) < self.min_len:
            raise SchemaError(f"{path}: shorter than {self.min_len}")
        return value


class Str(Field):
    def __init__(self, max_len: int, choices: set[str] | None = None, min_len: int = 0):
        self.max_len, self.choices, self.min_len = max_len, choices, min_len

    def check(self, value, path):
        if not isinstance(value, str):
            raise SchemaError(f"{path}: expected string")
        if len(value) > self.max_len or len(value) < self.min_len:
            raise SchemaError(f"{path}: bad string length {len(value)}")
        if self.choices is not None and value not in self.choices:
            raise SchemaError(f"{path}: value not allowed")
        return value


class UInt(Field):
    def __init__(self, bits: int):
        self.max = (1 << bits) - 1

    def check(self, value, path):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= self.max:
            raise SchemaError(f"{path}: expected uint <= {self.max}")
        return value


class Bool(Field):
    def check(self, value, path):
        if not isinstance(value, bool):
            raise SchemaError(f"{path}: expected bool")
        return value


class ListOf(Field):
    def __init__(self, item: Field, max_len: int, min_len: int = 0):
        self.item, self.max_len, self.min_len = item, max_len, min_len

    def check(self, value, path):
        if not isinstance(value, list):
            raise SchemaError(f"{path}: expected list")
        if not self.min_len <= len(value) <= self.max_len:
            raise SchemaError(f"{path}: list length {len(value)} out of [{self.min_len},{self.max_len}]")
        return [self.item.check(v, f"{path}[{i}]") for i, v in enumerate(value)]


class Struct(Field):
    def __init__(self, fields: dict[str, Field], optional: tuple[str, ...] = ()):
        self.fields, self.optional = fields, set(optional)

    def check(self, value, path):
        if not isinstance(value, dict):
            raise SchemaError(f"{path}: expected map")
        unknown = set(value) - set(self.fields)
        if unknown:
            raise SchemaError(f"{path}: unknown fields {sorted(unknown)}")
        out = {}
        for name, field in self.fields.items():
            if name not in value:
                if name in self.optional:
                    continue
                raise SchemaError(f"{path}.{name}: missing")
            out[name] = field.check(value[name], f"{path}.{name}")
        return out


U8, U16, U32, U64 = UInt(8), UInt(16), UInt(32), UInt(64)
NODE_ID = Bytes(32)
TUNNEL_ID = Bytes(16)
MESSAGE_ID = Bytes(16)

# Контакт в сетевом виде: только адресные и идентификационные поля. Метки
# last_seen_ms/last_verified_ms — локальные метаданные и по сети не передаются.
CONTACT = Struct({
    "node_id": NODE_ID,
    "identity_algorithm": Str(16, choices={"ed25519"}),
    "identity_public_key": Bytes(32),
    "host": Str(253, min_len=1),
    "port": U16,
})
MAX_CONTACT_WIRE = 256          # документированное ограничение размера контакта, байт
MAX_CONTACTS_IN_RESPONSE = 20   # верхняя граница схемы; узел дополнительно требует <= K_BUCKET_SIZE

ADDRESS = Struct({"host": Str(253, min_len=1), "port": U16})
RECORD = Struct({
    "node_id": NODE_ID,
    "identity_algorithm": Str(16, choices={"ed25519"}),
    "identity_public_key": Bytes(32),
    "addresses": ListOf(ADDRESS, max_len=4, min_len=1),
    "sequence_number": U64,
    "issued_at": U64,
    "expires_at": U64,
    "signature": Bytes(64),
})
CONFIRMATION = Struct({
    "node_id": NODE_ID,
    "hop_index": U8,
    "prev_node_id": NODE_ID,
    "next_node_id": NODE_ID,
    "signature": Bytes(64),
})

SCHEMAS: dict[int, Struct] = {
    MsgType.PING: Struct({"sender": CONTACT, "timestamp_ms": U64}),
    MsgType.PONG: Struct({"responder": CONTACT, "ping_timestamp_ms": U64, "responder_timestamp_ms": U64}),
    MsgType.FIND_NODE_REQUEST: Struct({"sender": CONTACT, "target_node_id": NODE_ID}),
    MsgType.FIND_NODE_RESPONSE: Struct({"responder": CONTACT, "target_node_id": NODE_ID,
                                        "contacts": ListOf(CONTACT, MAX_CONTACTS_IN_RESPONSE)}),
    MsgType.STORE_REQUEST: Struct({"sender": CONTACT, "key": NODE_ID, "record": RECORD}),
    MsgType.STORE_RESPONSE: Struct({"responder": CONTACT, "key": NODE_ID, "stored": Bool(),
                                    "reason": Str(64)}),
    MsgType.FIND_VALUE_REQUEST: Struct({"sender": CONTACT, "key": NODE_ID}),
    MsgType.FIND_VALUE_RESPONSE: Struct({"responder": CONTACT, "key": NODE_ID, "record": RECORD,
                                         "contacts": ListOf(CONTACT, MAX_CONTACTS_IN_RESPONSE)},
                                        optional=("record",)),
    MsgType.SECURE_HELLO: Struct({"node_id": NODE_ID, "certificate": Bytes(max_len=2048, min_len=1),
                                  "nonce": Bytes(16), "purpose": Str(8, choices={"link", "e2e"})}),
    MsgType.SECURE_HELLO_ACK: Struct({"node_id": NODE_ID, "certificate": Bytes(max_len=2048, min_len=1),
                                      "nonce": Bytes(16)}),
    MsgType.TUNNEL_BUILD: Struct({"tunnel_id": TUNNEL_ID, "sender": CONTACT, "hop_index": U8,
                                  "max_hops": U8, "ttl_s": U32,
                                  "remaining": ListOf(CONTACT, max_len=16)}),
    MsgType.TUNNEL_BUILD_OK: Struct({"tunnel_id": TUNNEL_ID,
                                     "confirmations": ListOf(CONFIRMATION, max_len=17, min_len=1)}),
    MsgType.TUNNEL_BUILD_FAIL: Struct({"tunnel_id": TUNNEL_ID, "reporter": NODE_ID, "reason": Str(128),
                                       "failed_node_id": NODE_ID}, optional=("failed_node_id",)),
    MsgType.TUNNEL_DATA: Struct({"tunnel_id": TUNNEL_ID, "seq": U64, "data": Bytes(max_len=60000)}),
    MsgType.TUNNEL_ACK: Struct({"tunnel_id": TUNNEL_ID, "seq": U64}),
    MsgType.TUNNEL_CLOSE: Struct({"tunnel_id": TUNNEL_ID, "reporter": NODE_ID, "reason": Str(128),
                                  "failed_node_id": NODE_ID}, optional=("failed_node_id",)),
    MsgType.APP_MESSAGE: Struct({"message_id": MESSAGE_ID, "sender_node_id": NODE_ID,
                                 "recipient_node_id": NODE_ID, "timestamp_ms": U64,
                                 "kind": Str(8, choices={"text"}), "body": Str(16384)}),
    MsgType.APP_ACK: Struct({"message_id": MESSAGE_ID, "status": Str(16, choices={"delivered", "duplicate",
                                                                                  "rejected"}),
                             "received_at_ms": U64}),
    MsgType.ECHO_REQUEST: Struct({"data": Bytes(max_len=65000)}),
    MsgType.ECHO_RESPONSE: Struct({"data": Bytes(max_len=65000)}),
    MsgType.ERROR: Struct({"code": Str(64), "message": Str(256)}),
}


def encode_payload(mtype: int, body: dict) -> bytes:
    SCHEMAS[mtype].check(body, MsgType(mtype).name)
    return pack(body)


def decode_payload(mtype: int, payload: bytes) -> dict:
    """Декодирует и проверяет payload. Поднимает DecodeError или SchemaError."""
    obj = unpack(payload)
    return SCHEMAS[mtype].check(obj, MsgType(mtype).name)


__all__ = ["SchemaError", "DecodeError", "SCHEMAS", "CONTACT", "RECORD", "encode_payload",
           "decode_payload", "MAX_CONTACT_WIRE", "MAX_CONTACTS_IN_RESPONSE"]
