"""Итеративный поиск Kademlia (FIND_NODE / FIND_VALUE) с параллелизмом ALPHA.

Условие остановки (PROTOCOL.md §6.3): «окно» W — k ближайших к цели кандидатов,
не помеченных как отказавшие. На каждой итерации опрашиваются до ALPHA ещё не
опрошенных кандидатов из W (ближайшие первыми), ответы объединяются, дубликаты
удаляются. Поиск завершается, когда все кандидаты W ответили (опрос остальных
не может улучшить список k ближайших), либо пул кандидатов исчерпан, либо (для
FIND_VALUE) получена валидная запись. Отказавшие кандидаты повторно не опрашиваются;
если из-за отказов в пуле меньше k живых кандидатов, он пополняется следующими по
близости контактами собственной таблицы.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field

from ..metrics import now_ms, short
from ..routing import Contact, bucket_index, xor_distance
from ..transport import RpcError
from .records import InvalidRecord, validate_record

NEW, INFLIGHT, OK, FAILED = "new", "inflight", "ok", "failed"


@dataclass
class _Cand:
    contact: Contact
    distance: int
    state: str = NEW
    source: str = "table"


@dataclass
class LookupResult:
    lookup_id: str
    kind: str
    initiator: bytes
    target: bytes
    purpose: str = "control"
    success: bool = False
    closest: list = field(default_factory=list)
    record: dict | None = None
    record_from: str | None = None
    rpc_count: int = 0
    iterations: int = 0
    rounds: list = field(default_factory=list)
    timeouts: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    duration_ms: float = 0.0
    target_in_table_before: bool = False
    table_size_before: int = 0
    found_by: str | None = None
    intermediate_nodes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "lookup_id": self.lookup_id, "kind": self.kind, "purpose": self.purpose, "initiator": self.initiator.hex(),
            "target": self.target.hex(), "success": self.success,
            "closest": [c.node_id.hex() for c in self.closest],
            "record_found": self.record is not None, "record_from": self.record_from,
            "rpc_count": self.rpc_count, "iterations": self.iterations, "rounds": self.rounds,
            "timeouts": self.timeouts, "errors": self.errors, "duration_ms": round(self.duration_ms, 3),
            "target_in_table_before": self.target_in_table_before, "table_size_before": self.table_size_before,
            "found_by": self.found_by, "intermediate_nodes": self.intermediate_nodes,
            "intermediate_count": len(self.intermediate_nodes),
        }


class Lookup:
    def __init__(self, node, target: bytes, kind: str = "node", purpose: str = "control"):
        assert kind in ("node", "value")
        self.node = node
        self.target = target
        self.kind = kind
        self.k = node.cfg.K_BUCKET_SIZE
        self.alpha = node.cfg.ALPHA
        self.local = node.identity.node_id
        self.cands: dict[bytes, _Cand] = {}
        self.res = LookupResult(uuid.uuid4().hex[:12], kind, self.local, target, purpose)

    def _add(self, contact: Contact, source: str) -> bool:
        if contact.node_id == self.local or contact.node_id in self.cands:
            return False
        self.cands[contact.node_id] = _Cand(contact, xor_distance(contact.node_id, self.target), NEW, source)
        return True

    def _alive_sorted(self) -> list[_Cand]:
        return sorted((c for c in self.cands.values() if c.state != FAILED), key=lambda c: c.distance)

    def _refill(self) -> None:
        alive = len(self._alive_sorted())
        if alive >= self.k:
            return
        for c in self.node.table.closest(self.target, self.k + len(self.cands), exclude=set(self.cands)):
            if alive >= self.k:
                break
            if self._add(Contact(c.node_id, c.identity_public_key, c.host, c.port), "table-refill"):
                alive += 1

    async def run(self) -> LookupResult:
        res = self.res
        table = self.node.table
        t0 = time.perf_counter()
        res.target_in_table_before = self.target in table if self.kind == "node" else False
        res.table_size_before = table.size()
        if self.target != self.local:
            table.buckets[bucket_index(self.local, self.target)].last_lookup_ms = now_ms()
        for c in table.closest(self.target, self.k):
            self._add(Contact(c.node_id, c.identity_public_key, c.host, c.port), "table")
        self.node.log.emit("lookup.start", lookup=res.lookup_id, kind=self.kind, purpose=res.purpose,
                           target=short(self.target),
                           seeds=len(self.cands), target_known=res.target_in_table_before)
        while res.iterations < self.node.cfg.MAX_LOOKUP_ITERATIONS:
            self._refill()
            window = self._alive_sorted()[:self.k]
            to_query = [c for c in window if c.state == NEW][:self.alpha]
            if not to_query:
                break
            res.iterations += 1
            for c in to_query:
                c.state = INFLIGHT
            outcomes = await asyncio.gather(*(self._query(c) for c in to_query))
            round_info = {"iteration": res.iterations, "queried": [], "responded": [], "failed": [], "new": 0}
            for cand, (ok, payload, err) in zip(to_query, outcomes):
                nid = cand.contact.node_id.hex()
                round_info["queried"].append(nid)
                res.rpc_count += 1
                if not ok:
                    cand.state = FAILED
                    round_info["failed"].append(nid)
                    (res.timeouts if err == "timeout" else res.errors).append(f"{nid[:8]}:{err}")
                    continue
                cand.state = OK
                round_info["responded"].append(nid)
                if cand.contact.node_id != self.target:
                    res.intermediate_nodes.append(nid)
                record, contacts = payload
                for nc in contacts:
                    if self._add(nc, cand.contact.node_id.hex()):
                        round_info["new"] += 1
                        if nc.node_id == self.target and res.found_by is None:
                            res.found_by = nid
                if record is not None and res.record is None:
                    res.record, res.record_from = record, nid
            res.rounds.append(round_info)
            self.node.log.emit("lookup.iteration", lookup=res.lookup_id, iteration=res.iterations,
                               queried=",".join(q[:8] for q in round_info["queried"]),
                               responded=len(round_info["responded"]), failed=len(round_info["failed"]),
                               new=round_info["new"])
            if self.kind == "value" and res.record is not None:
                break
        ok_sorted = sorted((c for c in self.cands.values() if c.state == OK), key=lambda c: c.distance)
        res.closest = [c.contact for c in ok_sorted[:self.k]]
        if self.kind == "node":
            res.success = self.target in self.cands and self.cands[self.target].state == OK
        else:
            res.success = res.record is not None
        res.duration_ms = (time.perf_counter() - t0) * 1000
        self.node.log.emit("lookup.done", lookup=res.lookup_id, kind=self.kind, purpose=res.purpose,
                           target=short(self.target),
                           success=res.success, rpc=res.rpc_count, iterations=res.iterations,
                           timeouts=len(res.timeouts), duration_ms=round(res.duration_ms, 1),
                           closest=",".join(c.short() for c in res.closest))
        self._persist()
        return res

    async def _query(self, cand: _Cand):
        try:
            if self.kind == "node":
                contacts = await self.node.rpc.find_node(cand.contact, self.target)
                return True, (None, contacts), None
            record, contacts = await self.node.rpc.find_value(cand.contact, self.target)
            if record is not None:
                try:
                    validate_record(record, now_ms(), key=self.target, max_bytes=self.node.cfg.MAX_RECORD_BYTES)
                except InvalidRecord as exc:
                    self.node.log.emit("lookup.bad_record", level="WARNING", source=cand.contact.short(),
                                       code=exc.code)
                    record = None
            return True, (record, contacts), None
        except RpcError as exc:
            return False, None, exc.kind
        except Exception as exc:                       # не даём одному сбою оборвать весь поиск
            return False, None, type(exc).__name__

    def _persist(self) -> None:
        state_dir = self.node.cfg.NODE_STATE_DIR
        if not state_dir or not os.path.isdir(state_dir):
            return
        with open(os.path.join(state_dir, "lookups.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts_ms": now_ms(), **self.res.to_dict()}) + "\n")
