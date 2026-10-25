"""Локальное хранилище DHT-записей: TTL, монотонность sequence_number, откат.

Хранилище сохраняется в NODE_STATE_DIR/storage.json, поэтому перезапущенный узел
сохраняет принятые реплики; при загрузке каждая запись повторно проверяется
(подпись, срок действия), просроченные отбрасываются.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .records import InvalidRecord, describe, validate_record

_BYTES_FIELDS = ("node_id", "identity_public_key", "signature")


@dataclass
class Stored:
    record: dict
    stored_at_ms: int


class RecordStore:
    def __init__(self, path: str | None = None):
        self._items: dict[bytes, Stored] = {}
        self._max_seq: dict[bytes, int] = {}       # помним максимум и после истечения записи
        self.path = path

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
        self.save()
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

    # ------------------------------------------------------------------ персистентность
    def save(self) -> None:
        if not self.path:
            return
        data = {"max_seq": {k.hex(): v for k, v in self._max_seq.items()},
                "records": [{"key": k.hex(), "stored_at_ms": st.stored_at_ms,
                             "record": {f: (v.hex() if f in _BYTES_FIELDS else v) for f, v in st.record.items()}}
                            for k, st in self._items.items()]}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.path)

    def load(self, now_ms: int, max_bytes: int = 1024) -> int:
        if not self.path or not os.path.exists(self.path):
            return 0
        with open(self.path, encoding="utf-8") as fh:
            data = json.load(fh)
        self._max_seq.update({bytes.fromhex(k): v for k, v in data.get("max_seq", {}).items()})
        loaded = 0
        for item in data.get("records", []):
            rec = {f: (bytes.fromhex(v) if f in _BYTES_FIELDS else v) for f, v in item["record"].items()}
            key = bytes.fromhex(item["key"])
            try:
                validate_record(rec, now_ms, key=key, max_bytes=max_bytes)
            except InvalidRecord:
                continue
            self._items[key] = Stored(rec, item["stored_at_ms"])
            loaded += 1
        return loaded
