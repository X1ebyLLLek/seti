"""Локальное хранилище DHT-записей: TTL, монотонность sequence_number, откат."""
from __future__ import annotations

from dataclasses import dataclass

from .records import describe


@dataclass
class Stored:
    record: dict
    stored_at_ms: int


class RecordStore:
    def __init__(self):
        self._items: dict[bytes, Stored] = {}
        self._max_seq: dict[bytes, int] = {}       # помним максимум и после истечения записи

    def put(self, key: bytes, rec: dict, now_ms: int) -> tuple[bool, str]:
        """Запись уже проверена validate_record. Возвращает (stored, reason)."""
        seq = rec["sequence_number"]
        known = self._max_seq.get(key)
        if known is not None and seq < known:
            return False, "ROLLBACK"
        cur = self._items.get(key)
        if cur is not None and cur.record["sequence_number"] == seq:
            if cur.record == rec:
                cur.stored_at_ms = now_ms
                return True, "SAME"
            return False, "SEQ_CONFLICT"
        self._items[key] = Stored(rec, now_ms)
        self._max_seq[key] = max(seq, known or 0)
        return True, "STORED"

    def get(self, key: bytes, now_ms: int) -> dict | None:
        cur = self._items.get(key)
        if cur is None:
            return None
        if cur.record["expires_at"] <= now_ms:
            del self._items[key]
            return None
        return cur.record

    def expire(self, now_ms: int) -> int:
        dead = [k for k, s in self._items.items() if s.record["expires_at"] <= now_ms]
        for k in dead:
            del self._items[k]
        return len(dead)

    def keys(self) -> list[bytes]:
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def export(self) -> list[dict]:
        return [{"key": k.hex(), "stored_at_ms": s.stored_at_ms, **describe(s.record)}
                for k, s in self._items.items()]
