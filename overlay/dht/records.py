"""Подписанные адресные записи NodeRecord и их проверка.

Ключ записи (упрощённый уровень, поиск по NodeID): key = SHA-256("node:" || NodeID).
Подписываемые байты (фиксированный порядок полей, канонический MessagePack массива):
    "overlay-noderecord-v1" || 0x00 ||
    msgpack([node_id, identity_algorithm, identity_public_key,
             [[host, port], ...], sequence_number, issued_at, expires_at])
Подпись — Ed25519 identity-ключом владельца. Времена — миллисекунды Unix UTC.
"""
from __future__ import annotations

import hashlib

from ..identity import ALGORITHM, Identity, is_valid_ed25519_public_key, node_id_from_public_key, verify_signature
from ..protocol.codec import pack
from ..protocol.messages import RECORD, SchemaError
from ..routing.contact import valid_host

DOMAIN = b"overlay-noderecord-v1\x00"
CLOCK_SKEW_MS = 30_000


class InvalidRecord(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def record_key(node_id: bytes) -> bytes:
    return hashlib.sha256(b"node:" + node_id).digest()


def signing_bytes(rec: dict) -> bytes:
    addrs = [[a["host"], a["port"]] for a in rec["addresses"]]
    return DOMAIN + pack([rec["node_id"], rec["identity_algorithm"], rec["identity_public_key"], addrs,
                          rec["sequence_number"], rec["issued_at"], rec["expires_at"]])


def make_record(identity: Identity, addresses: list[tuple[str, int]], sequence_number: int, ttl_s: int,
                now_ms: int) -> dict:
    rec = {
        "node_id": identity.node_id,
        "identity_algorithm": ALGORITHM,
        "identity_public_key": identity.public_key,
        "addresses": [{"host": h, "port": p} for h, p in addresses],
        "sequence_number": sequence_number,
        "issued_at": now_ms,
        "expires_at": now_ms + ttl_s * 1000,
    }
    rec["signature"] = identity.sign(signing_bytes(rec))
    return rec


def validate_record(rec: dict, now_ms: int, *, key: bytes | None = None, max_bytes: int = 1024,
                    max_ttl_s: int = 300) -> None:
    """Проверки до сохранения и использования: схема, размер, привязка node_id к ключу,
    подпись, срок действия, формат адресов, соответствие ключу хранения."""
    try:
        RECORD.check(rec, "record")
    except SchemaError as exc:
        raise InvalidRecord("SCHEMA", str(exc)) from None
    if len(pack(rec)) > max_bytes:
        raise InvalidRecord("SIZE", "record too large")
    if rec["identity_algorithm"] != ALGORITHM or not is_valid_ed25519_public_key(rec["identity_public_key"]):
        raise InvalidRecord("KEY", "bad identity key")
    if node_id_from_public_key(rec["identity_public_key"]) != rec["node_id"]:
        raise InvalidRecord("NODE_ID", "node_id does not match public key")
    if key is not None and record_key(rec["node_id"]) != key:
        raise InvalidRecord("KEY_MISMATCH", "record stored under a foreign key")
    if not verify_signature(rec["identity_public_key"], rec["signature"], signing_bytes(rec)):
        raise InvalidRecord("SIGNATURE", "invalid signature")
    if rec["issued_at"] > now_ms + CLOCK_SKEW_MS:
        raise InvalidRecord("TIME", "issued in the future")
    if rec["expires_at"] <= now_ms:
        raise InvalidRecord("EXPIRED", "record expired")
    if rec["expires_at"] <= rec["issued_at"] or rec["expires_at"] - rec["issued_at"] > max_ttl_s * 1000:
        raise InvalidRecord("TTL", "lifetime out of range")
    for a in rec["addresses"]:
        if not valid_host(a["host"]) or not 1 <= a["port"] <= 65535:
            raise InvalidRecord("ADDRESS", "bad address")


def describe(rec: dict) -> dict:
    return {"node_id": rec["node_id"].hex(), "addresses": [f"{a['host']}:{a['port']}" for a in rec["addresses"]],
            "sequence_number": rec["sequence_number"], "issued_at": rec["issued_at"],
            "expires_at": rec["expires_at"]}
