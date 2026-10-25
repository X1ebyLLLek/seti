"""Таблица маршрутизации Kademlia: 256 фиксированных k-bucket.

Bucket i содержит контакты с XOR-расстоянием в [2^i, 2^(i+1)), т.е. индекс —
номер старшего установленного бита расстояния. Ёмкость каждого bucket —
K_BUCKET_SIZE (k относится к одному bucket, а не ко всей таблице). Внутри bucket
контакты упорядочены по последнему успешному наблюдению: первый — наименее
недавно подтверждённый (LRU), последний — самый свежий.
Таблица синхронная и не делает сетевых вызовов; проверка LRU перед вытеснением
выполняется RoutingManager.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field

from .contact import Contact, bucket_index, xor_distance

NUM_BUCKETS = 256


class AddStatus(enum.Enum):
    ADDED = "added"
    UPDATED = "updated"
    BUCKET_FULL = "bucket_full"
    CONFLICT = "conflict"                 # тот же NodeID, другой ключ — нарушение идентичности
    ADDRESS_CHANGED = "address_changed"   # тот же NodeID, другой адрес — нужна проверка
    SELF = "self"


@dataclass
class AddResult:
    status: AddStatus
    bucket: int = -1
    lru: Contact | None = None


@dataclass
class KBucket:
    index: int
    capacity: int
    contacts: list = field(default_factory=list)
    check_in_progress: bool = False
    last_changed_ms: int = 0
    last_lookup_ms: int = 0

    def get(self, node_id: bytes) -> Contact | None:
        for c in self.contacts:
            if c.node_id == node_id:
                return c
        return None

    def move_to_tail(self, contact: Contact) -> None:
        self.contacts.remove(contact)
        self.contacts.append(contact)

    @property
    def full(self) -> bool:
        return len(self.contacts) >= self.capacity


class RoutingTable:
    def __init__(self, local_id: bytes, k: int):
        self.local_id = local_id
        self.k = k
        self.buckets = [KBucket(i, k) for i in range(NUM_BUCKETS)]

    # ------------------------------------------------------------------ запросы
    def bucket_for(self, node_id: bytes) -> KBucket:
        return self.buckets[bucket_index(self.local_id, node_id)]

    def find(self, node_id: bytes) -> Contact | None:
        if node_id == self.local_id:
            return None
        return self.bucket_for(node_id).get(node_id)

    def __contains__(self, node_id: bytes) -> bool:
        return self.find(node_id) is not None

    def contacts(self) -> list[Contact]:
        return [c for b in self.buckets for c in b.contacts]

    def size(self) -> int:
        return sum(len(b.contacts) for b in self.buckets)

    def closest(self, target: bytes, count: int, exclude: set | None = None) -> list[Contact]:
        """До count известных контактов, отсортированных по возрастанию XOR-расстояния до target."""
        exclude = exclude or set()
        pool = [c for c in self.contacts() if c.node_id not in exclude]
        pool.sort(key=lambda c: xor_distance(c.node_id, target))
        return pool[:count]

    def bucket_fill(self) -> dict[int, int]:
        return {b.index: len(b.contacts) for b in self.buckets if b.contacts}

    # ------------------------------------------------------------------ изменения
    def observe(self, contact: Contact, now_ms: int, verified: bool = True) -> AddResult:
        """Применение правил раздела 18.2/18.3 к непосредственно наблюдённому контакту."""
        if contact.node_id == self.local_id:
            return AddResult(AddStatus.SELF)
        bucket = self.bucket_for(contact.node_id)
        existing = bucket.get(contact.node_id)
        if existing is not None:
            if existing.identity_public_key != contact.identity_public_key:
                return AddResult(AddStatus.CONFLICT, bucket.index)
            if existing.addr != contact.addr:
                return AddResult(AddStatus.ADDRESS_CHANGED, bucket.index, existing)
            existing.last_seen_ms = now_ms
            if verified:
                existing.last_verified_ms = now_ms
            existing.fail_count = 0
            bucket.move_to_tail(existing)
            return AddResult(AddStatus.UPDATED, bucket.index)
        if not bucket.full:
            contact.last_seen_ms = now_ms
            contact.last_verified_ms = now_ms if verified else 0
            contact.fail_count = 0
            bucket.contacts.append(contact)
            bucket.last_changed_ms = now_ms
            return AddResult(AddStatus.ADDED, bucket.index)
        return AddResult(AddStatus.BUCKET_FULL, bucket.index, bucket.contacts[0])

    def keep_lru(self, lru_id: bytes, now_ms: int) -> None:
        """LRU ответил на PING: перемещается в конец, новый контакт не добавляется."""
        bucket = self.bucket_for(lru_id)
        c = bucket.get(lru_id)
        if c is not None:
            c.last_seen_ms = c.last_verified_ms = now_ms
            c.fail_count = 0
            bucket.move_to_tail(c)

    def replace_lru(self, lru_id: bytes, new: Contact, now_ms: int) -> bool:
        """LRU не ответил: удаляется, новый контакт добавляется в конец."""
        bucket = self.bucket_for(new.node_id)
        old = bucket.get(lru_id)
        if old is not None:
            bucket.contacts.remove(old)
        if bucket.get(new.node_id) is None and not bucket.full:
            new.last_seen_ms = new.last_verified_ms = now_ms
            new.fail_count = 0
            bucket.contacts.append(new)
            bucket.last_changed_ms = now_ms
            return True
        return False

    def update_address(self, node_id: bytes, host: str, port: int, now_ms: int) -> None:
        c = self.find(node_id)
        if c is not None:
            c.host, c.port = host, port
            c.last_seen_ms = c.last_verified_ms = now_ms
            c.fail_count = 0
            self.bucket_for(node_id).move_to_tail(c)

    def remove(self, node_id: bytes) -> bool:
        bucket = self.bucket_for(node_id)
        c = bucket.get(node_id)
        if c is None:
            return False
        bucket.contacts.remove(c)
        return True

    def record_failure(self, node_id: bytes, max_fails: int) -> bool:
        """Неудачный RPC к контакту. Удаляется только этот контакт и только после
        max_fails подряд неудач; остальные контакты bucket не затрагиваются."""
        c = self.find(node_id)
        if c is None:
            return False
        c.fail_count += 1
        if c.fail_count >= max_fails:
            self.remove(node_id)
            return True
        return False

    # ------------------------------------------------------------------ экспорт
    def export(self) -> dict:
        return {
            "local_node_id": self.local_id.hex(),
            "k": self.k,
            "size": self.size(),
            "buckets": [{"index": b.index, "fill": len(b.contacts),
                         "contacts": [c.describe() for c in b.contacts]}
                        for b in self.buckets if b.contacts],
        }
