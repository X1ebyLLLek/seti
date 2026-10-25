"""Журнал событий узла: человекочитаемый node.log и машинно-читаемый events.jsonl.

Каждая запись содержит время (UTC, мс), уровень, сокращённый локальный NodeID,
имя события и поля (направление, идентификатор соединения/RPC и т.п.).
Полный payload сообщений по умолчанию не пишется; закрытые ключи не пишутся никогда —
модуль identity их сюда не передаёт, а bytes-поля сокращаются до префикса.
"""
from __future__ import annotations

import collections
import json
import os
import sys
import threading
import time

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


def now_ms() -> int:
    return int(time.time() * 1000)


def short(b: bytes | None, n: int = 8) -> str:
    return b.hex()[:n] if b else "-"


def _fmt(v):
    if isinstance(v, (bytes, bytearray)):
        return short(bytes(v), 16)
    return v


class EventLog:
    def __init__(self, state_dir: str | None, level: str = "INFO", echo: bool = False):
        self.level = LEVELS.get(level.upper(), 20)
        self.node = "--------"
        self.echo = echo
        self._lock = threading.Lock()
        self._text = self._json = None
        if state_dir:
            log_dir = os.path.join(state_dir, "logs")
            os.makedirs(log_dir, exist_ok=True)
            self._text = open(os.path.join(log_dir, "node.log"), "a", encoding="utf-8")
            self._json = open(os.path.join(log_dir, "events.jsonl"), "a", encoding="utf-8")
        self.counters: collections.Counter = collections.Counter()
        self.listeners: list = []

    def set_node(self, node_id: bytes) -> None:
        self.node = short(node_id)

    def emit(self, event: str, level: str = "INFO", **fields) -> None:
        self.counters[event] += 1
        for cb in self.listeners:
            cb(event, fields)
        if LEVELS.get(level, 20) < self.level:
            return
        ts = now_ms()
        rec = {"ts_ms": ts, "level": level, "node": self.node, "event": event}
        rec.update({k: _fmt(v) for k, v in fields.items()})
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts / 1000)) + f".{ts % 1000:03d}Z"
        line = f"{stamp} {level:<7} [{self.node}] {event} " + " ".join(
            f"{k}={rec[k]}" for k in fields)
        with self._lock:
            if self._json:
                self._json.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                self._json.flush()
            if self._text:
                self._text.write(line + "\n")
                self._text.flush()
            if self.echo:
                print(line, file=sys.stderr, flush=True)

    def close(self) -> None:
        with self._lock:
            for fh in (self._text, self._json):
                if fh:
                    fh.close()
            self._text = self._json = None


NULL_LOG = EventLog(None, level="ERROR")
