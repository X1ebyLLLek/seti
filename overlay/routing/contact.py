"""Контакт узла и XOR-метрика Kademlia."""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from ..identity import ALGORITHM, is_valid_ed25519_public_key, node_id_from_public_key
from ..protocol.codec import pack
from ..protocol.messages import MAX_CONTACT_WIRE

_HOSTNAME = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


def xor_distance(a: bytes, b: bytes) -> int:
    """distance = integer_from_big_endian(a XOR b): беззнаковое 256-битное число."""
    if len(a) != 32 or len(b) != 32:
        raise ValueError("NodeID must be 32 bytes")
    return int.from_bytes(a, "big") ^ int.from_bytes(b, "big")


def bucket_index(local_id: bytes, other_id: bytes) -> int:
    """Индекс старшего установленного бита XOR-расстояния: 0..255 (ближе — меньше)."""
    d = xor_distance(local_id, other_id)
    if d == 0:
        raise ValueError("same node id")
    return d.bit_length() - 1


def valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(_HOSTNAME.match(host))


@dataclass
class Contact:
    node_id: bytes
    identity_public_key: bytes
    host: str
    port: int
    identity_algorithm: str = ALGORITHM
    last_seen_ms: int = 0          # локальные метаданные: устанавливаются получателем
    last_verified_ms: int = 0
    fail_count: int = 0

    @property
    def addr(self) -> tuple[str, int]:
        return self.host, self.port

    def to_wire(self) -> dict:
        return {"node_id": self.node_id, "identity_algorithm": self.identity_algorithm,
                "identity_public_key": self.identity_public_key, "host": self.host, "port": self.port}

    def short(self) -> str:
        return self.node_id.hex()[:8]

    def describe(self) -> dict:
        return {"node_id": self.node_id.hex(), "host": self.host, "port": self.port,
                "last_seen_ms": self.last_seen_ms, "last_verified_ms": self.last_verified_ms,
                "fail_count": self.fail_count}


class InvalidContact(Exception):
    pass


def contact_from_wire(wire: dict, local_id: bytes | None = None) -> Contact:
    """Проверка контакта по разделу 15.3: длина node_id, формат ключа, привязка
    NodeID к ключу, формат адреса и порта, не локальный узел, размер."""
    node_id = wire["node_id"]
    pk = wire["identity_public_key"]
    alg = wire["identity_algorithm"]
    if len(node_id) != 32:
        raise InvalidContact("node_id must be 32 bytes")
    if alg != ALGORITHM or not is_valid_ed25519_public_key(pk):
        raise InvalidContact("bad identity public key")
    if node_id_from_public_key(pk, alg) != node_id:
        raise InvalidContact("node_id does not match public key")
    if not valid_host(wire["host"]) or not 1 <= wire["port"] <= 65535:
        raise InvalidContact("bad address")
    if local_id is not None and node_id == local_id:
        raise InvalidContact("contact refers to the local node")
    if len(pack(wire)) > MAX_CONTACT_WIRE:
        raise InvalidContact("contact too large")
    return Contact(node_id=node_id, identity_public_key=pk, host=wire["host"], port=wire["port"],
                   identity_algorithm=alg)
