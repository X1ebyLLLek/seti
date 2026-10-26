"""Асинхронное обслуживание таблицы: проверка LRU перед вытеснением, смена адреса."""
from __future__ import annotations

import asyncio

from ..metrics import NULL_LOG, now_ms
from .contact import Contact
from .table import AddStatus, RoutingTable


class RoutingManager:
    def __init__(self, table: RoutingTable, ping, log=NULL_LOG, max_fails: int = 2):
        """ping: async (Contact) -> bool — True, если получен корректный PONG от этого NodeID."""
        self.table = table
        self.ping = ping
        self.log = log
        self.max_fails = max_fails
        self._tasks: set[asyncio.Task] = set()
        self._addr_checks: set[bytes] = set()
        self.on_added: list = []        # callbacks (Contact) — узнали о новом узле (в таблице или нет)

    def observe(self, contact: Contact, verified: bool = True, reason: str = "message") -> asyncio.Task | None:
        """Применяет правила bucket. Если требуется сетевой шаг (PING LRU или проверка
        нового адреса), запускает его в фоне и возвращает задачу."""
        res = self.table.observe(contact, now_ms(), verified)
        st = res.status
        if st is AddStatus.ADDED:
            self.log.emit("route.add", peer=contact.short(), bucket=res.bucket, reason=reason,
                          size=self.table.size())
            self._notify_added(contact)
        elif st is AddStatus.UPDATED:
            self.log.emit("route.update", level="DEBUG", peer=contact.short(), bucket=res.bucket, reason=reason)
        elif st is AddStatus.CONFLICT:
            self.log.emit("route.reject", level="WARNING", peer=contact.short(), bucket=res.bucket,
                          reason="same NodeID with different public key")
        elif st is AddStatus.ADDRESS_CHANGED:
            if contact.node_id not in self._addr_checks:
                self._addr_checks.add(contact.node_id)
                return self._spawn(self._verify_new_address(contact, res.lru))
        elif st is AddStatus.BUCKET_FULL:
            self._notify_added(contact)            # узел новый, даже если в таблицу пока не попал
            bucket = self.table.buckets[res.bucket]
            if bucket.check_in_progress:
                self.log.emit("route.drop", level="DEBUG", peer=contact.short(), bucket=res.bucket,
                              reason="bucket full, LRU check already in progress")
                return None
            bucket.check_in_progress = True
            return self._spawn(self._check_lru(res.bucket, res.lru, contact))
        return None

    def _notify_added(self, contact: Contact) -> None:
        for cb in list(self.on_added):
            try:
                cb(contact)
            except Exception as exc:
                self.log.emit("route.callback_error", level="ERROR", error=type(exc).__name__)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _check_lru(self, index: int, lru: Contact, new: Contact) -> None:
        bucket = self.table.buckets[index]
        self.log.emit("route.check_lru", bucket=index, lru=lru.short(), candidate=new.short())
        try:
            alive = await self.ping(lru)
        except Exception:
            alive = False
        finally:
            bucket.check_in_progress = False
        if alive:
            self.table.keep_lru(lru.node_id, now_ms())
            self.log.emit("route.keep_lru", bucket=index, lru=lru.short(), dropped=new.short(),
                          reason="LRU answered PING")
        else:
            added = self.table.replace_lru(lru.node_id, new, now_ms())
            self.log.emit("route.evict", bucket=index, evicted=lru.short(), added=new.short() if added else "-",
                          reason="LRU PING timeout", size=self.table.size())

    async def _verify_new_address(self, contact: Contact, old: Contact) -> None:
        try:
            ok = await self.ping(contact)
        except Exception:
            ok = False
        finally:
            self._addr_checks.discard(contact.node_id)
        if ok:
            self.table.update_address(contact.node_id, contact.host, contact.port, now_ms())
            self.log.emit("route.address_update", peer=contact.short(), old=f"{old.host}:{old.port}",
                          new=f"{contact.host}:{contact.port}")
        else:
            self.log.emit("route.address_reject", level="WARNING", peer=contact.short(),
                          claimed=f"{contact.host}:{contact.port}", reason="new address not verified")

    def record_failure(self, contact: Contact, reason: str) -> None:
        if self.table.record_failure(contact.node_id, self.max_fails):
            self.log.emit("route.remove", peer=contact.short(), reason=reason, size=self.table.size())

    async def idle(self) -> None:
        """Дождаться завершения фоновых проверок (для тестов и экспорта состояния)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
