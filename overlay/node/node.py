"""Узел оверлейной сети: сборка подсистем и фасад для прикладного уровня.

Фасад (раздел 7 ТЗ):
    join(bootstrap_peers)            lookup_node(target_node_id)
    put_record(key, record, ttl)     get_record(key)
    build_tunnel(dest, constraints)  send_via_tunnel(tunnel_id, payload)
    close_tunnel(tunnel_id)          get_network_state()
Прикладной слой не обращается к k-bucket и хранилищу DHT напрямую.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from dataclasses import dataclass, field

from ..config import NodeConfig, parse_peer
from ..dht import Lookup, LookupResult, RecordStore, make_record, record_key, validate_record
from ..dht.records import InvalidRecord, describe
from ..identity import Identity
from ..metrics import EventLog, now_ms, short
from ..routing import Contact, RoutingManager, RoutingTable, xor_distance
from ..rpc import DhtRpc
from ..security import SecurityProvider
from ..transport import Dispatcher, RpcError, Transport


@dataclass
class BootstrapResult:
    ok: bool
    peers_tried: list = field(default_factory=list)
    peers_ok: list = field(default_factory=list)
    self_lookup: dict | None = None
    table_size: int = 0
    duration_ms: float = 0.0
    refresh_lookups: int = 0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class StoreResult:
    key: bytes
    ok: bool
    acks: list = field(default_factory=list)
    refused: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    lookup: dict | None = None
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["key"] = self.key.hex()
        return d


class Node:
    def __init__(self, cfg: NodeConfig, echo_log: bool = False):
        self.cfg = cfg
        self.log = EventLog(cfg.NODE_STATE_DIR, cfg.LOG_LEVEL, echo=echo_log)
        self.identity = Identity.load_or_create(cfg.NODE_STATE_DIR)
        self.log.set_node(self.identity.node_id)
        self.log.emit("identity.created" if self.identity.created else "identity.loaded",
                      node_id=self.identity.node_id.hex(), algorithm=self.identity.algorithm)
        self.table = RoutingTable(self.identity.node_id, cfg.K_BUCKET_SIZE)
        self.dispatcher = Dispatcher()
        self.rpc = DhtRpc(self)
        self.routing = RoutingManager(self.table, self.rpc.ping_contact, self.log, cfg.CONTACT_MAX_FAILS)
        self.routing.on_added.append(self._hand_off_records)
        self.store = RecordStore(os.path.join(cfg.NODE_STATE_DIR, "storage.json"))
        restored = self.store.load(now_ms(), cfg.MAX_RECORD_BYTES)
        if restored:
            self.log.emit("store.restored", records=restored)
        # Провайдер TLS нужен всегда: end-to-end сессия поверх туннеля защищается TLS 1.3
        # и в режиме plain (там без защиты остаются только межузловые соединения).
        self.security = SecurityProvider(self.identity, self.log)
        self.transport = Transport(cfg, self.dispatcher, self.log,
                                   self.security if cfg.SECURITY_MODE == "tls" else None)
        self.rpc.register(self.dispatcher)
        self.rng = random.Random(cfg.SEED or None)
        self.tunnels = None          # TunnelManager (overlay.tunnel)
        self.messaging = None        # MessagingService (overlay.app)
        self._bg: list[asyncio.Task] = []
        self._handoffs: set[asyncio.Task] = set()
        self.joined = False
        self.started_ms = now_ms()
        self._seq_path = os.path.join(cfg.NODE_STATE_DIR, "record_seq.json")
        self.own_record: dict | None = None
        self.publish_acks = 0
        self._last_publish_try = 0.0
        self.own_record_key = record_key(self.identity.node_id)

    # ================================================================== жизненный цикл
    def self_contact(self) -> Contact:
        return Contact(self.identity.node_id, self.identity.public_key, self.cfg.advertise_host,
                       self.cfg.advertise_port)

    async def start(self) -> None:
        from ..app.messaging import MessagingService
        from ..tunnel.manager import TunnelManager
        self.tunnels = TunnelManager(self)
        self.tunnels.register(self.dispatcher)
        self.messaging = MessagingService(self)
        await self.transport.start()
        self._save_config()
        self.log.emit("node.start", node_id=self.identity.node_id.hex(),
                      addr=f"{self.cfg.advertise_host}:{self.cfg.advertise_port}", k=self.cfg.K_BUCKET_SIZE,
                      alpha=self.cfg.ALPHA, security=self.cfg.SECURITY_MODE)
        self._bg = [asyncio.ensure_future(self._maintenance_loop())]

    async def stop(self) -> None:
        for t in self._bg + list(self._handoffs):
            t.cancel()
        if self.tunnels:
            await self.tunnels.shutdown()
        self.save_state()
        await self.transport.stop()
        self.log.emit("node.stop")
        self.log.close()

    def _save_config(self) -> None:
        with open(os.path.join(self.cfg.NODE_STATE_DIR, "effective_config.json"), "w", encoding="utf-8") as fh:
            json.dump(self.cfg.to_dict(), fh, indent=2)

    def save_state(self) -> None:
        path = os.path.join(self.cfg.NODE_STATE_DIR, "routing_table.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"ts_ms": now_ms(), **self.table.export()}, fh, indent=1)
        os.replace(tmp, path)

    # ================================================================== bootstrap
    async def join(self, bootstrap_peers: list[str] | None = None) -> BootstrapResult:
        peers = self.cfg.BOOTSTRAP_PEERS if bootstrap_peers is None else bootstrap_peers
        t0 = time.perf_counter()
        res = BootstrapResult(ok=False)
        if not peers:
            # Первый seed-узел: пустая таблица — допустимое состояние.
            self.log.emit("bootstrap.seed", reason="empty BOOTSTRAP_PEERS")
            res.ok = True
            self.joined = True
            await self.publish_self()
            return res
        for spec in peers:
            expected, host, port = parse_peer(spec)
            res.peers_tried.append(spec)
            self.log.emit("bootstrap.connect", peer_addr=f"{host}:{port}", pinned=short(expected))
            try:
                pr = await self.rpc.ping(host, port, expected)
            except RpcError as exc:
                self.log.emit("bootstrap.ping_failed", level="WARNING", peer_addr=f"{host}:{port}", error=str(exc))
                continue
            self.log.emit("bootstrap.ping_ok", peer=pr.contact.short(), rtt_ms=round(pr.rtt_ms, 2),
                          table=self.table.size())
            res.peers_ok.append(pr.contact.node_id.hex())
        if not res.peers_ok:
            res.duration_ms = (time.perf_counter() - t0) * 1000
            self.log.emit("bootstrap.failed", level="ERROR", tried=len(peers))
            return res
        await self.routing.idle()
        lk = await Lookup(self, self.identity.node_id, "node", "self").run()
        res.self_lookup = lk.to_dict()
        if self.cfg.JOIN_REFRESH:
            res.refresh_lookups = await self.refresh_buckets(force=True)
        await self.routing.idle()
        res.table_size = self.table.size()
        res.ok = res.table_size > 0
        res.duration_ms = (time.perf_counter() - t0) * 1000
        self.joined = res.ok
        self.save_state()
        with open(os.path.join(self.cfg.NODE_STATE_DIR, "bootstrap.json"), "w", encoding="utf-8") as fh:
            json.dump(res.to_dict(), fh, indent=1)
        self.log.emit("bootstrap.done", ok=res.ok, table=res.table_size, buckets=json.dumps(self.table.bucket_fill()),
                      duration_ms=round(res.duration_ms, 1), self_lookup_rpc=lk.rpc_count)
        await self.publish_self()
        return res

    # ================================================================== DHT-поиск
    async def lookup_node(self, target_node_id: bytes, purpose: str = "control") -> LookupResult:
        return await Lookup(self, target_node_id, "node", purpose).run()

    async def refresh_buckets(self, force: bool = False) -> int:
        """Обновление bucket: случайный поиск в диапазоне каждого «устаревшего» bucket,
        начиная с bucket сразу под ближайшим непустым и до 255."""
        nonempty = [b.index for b in self.table.buckets if b.contacts]
        if not nonempty:
            return 0
        lowest = max(0, min(nonempty) - 1)
        now = now_ms()
        count = 0
        for b in self.table.buckets[lowest:]:
            if not force and now - max(b.last_lookup_ms, b.last_changed_ms) < self.cfg.REFRESH_INTERVAL_S * 1000:
                continue
            offset = (1 << b.index) | self.rng.getrandbits(b.index) if b.index else 1
            target = (int.from_bytes(self.identity.node_id, "big") ^ offset).to_bytes(32, "big")
            await Lookup(self, target, "node", "refresh").run()
            count += 1
        if count:
            self.log.emit("refresh.done", lookups=count, table=self.table.size())
        return count

    # ================================================================== записи DHT
    def _next_seq(self) -> int:
        seq = 0
        if os.path.exists(self._seq_path):
            with open(self._seq_path, encoding="utf-8") as fh:
                seq = json.load(fh)["sequence_number"]
        seq += 1
        with open(self._seq_path, "w", encoding="utf-8") as fh:
            json.dump({"sequence_number": seq}, fh)
        return seq

    async def publish_self(self) -> StoreResult:
        """Публикация (и переопубликование) собственной NodeRecord с новым sequence_number."""
        rec = make_record(self.identity, [(self.cfg.advertise_host, self.cfg.advertise_port)], self._next_seq(),
                          self.cfg.RECORD_TTL_S, now_ms())
        self.own_record = rec
        self._last_publish_try = time.monotonic()
        res = await self.put_record(record_key(self.identity.node_id), rec, self.cfg.RECORD_TTL_S)
        self.publish_acks = len(res.acks)
        self.log.emit("record.publish", seq=rec["sequence_number"], ok=res.ok, acks=len(res.acks),
                      replicas=",".join(a[:8] for a in res.acks))
        return res

    async def put_record(self, key: bytes, signed_record: dict, ttl: int | None = None) -> StoreResult:
        t0 = time.perf_counter()
        validate_record(signed_record, now_ms(), key=key, max_bytes=self.cfg.MAX_RECORD_BYTES)
        if ttl is not None and signed_record["expires_at"] - signed_record["issued_at"] > ttl * 1000:
            raise InvalidRecord("TTL", "record lifetime exceeds requested ttl")
        res = StoreResult(key, False)
        if self.table.size() == 0:
            res.duration_ms = (time.perf_counter() - t0) * 1000
            return res
        lk = await Lookup(self, key, "node", "store").run()
        res.lookup = {"rpc_count": lk.rpc_count, "iterations": lk.iterations, "lookup_id": lk.lookup_id}
        r = self.cfg.REPLICATION_FACTOR
        targets = list(lk.closest)
        if len(targets) < r:
            extra = self.table.closest(key, r, exclude={c.node_id for c in targets})
            targets += extra[:r - len(targets)]
        tried: set[bytes] = set()
        while len(res.acks) < r and targets:
            batch, targets = targets[:r - len(res.acks)], targets[r - len(res.acks):]
            outcomes = await asyncio.gather(*(self._store_one(c, key, signed_record) for c in batch))
            for c, (ok, reason) in zip(batch, outcomes):
                tried.add(c.node_id)
                if ok:
                    res.acks.append(c.node_id.hex())
                elif reason == "rpc":
                    res.failed.append(c.node_id.hex())
                else:
                    res.refused.append(f"{c.node_id.hex()[:8]}:{reason}")
            if len(res.acks) < r and not targets:
                # запасные кандидаты: следующие по близости контакты таблицы
                targets = [c for c in self.table.closest(key, r + len(tried), exclude=tried)][:r - len(res.acks)]
                if not targets:
                    break
        res.ok = len(res.acks) >= self.cfg.STORE_MIN_ACKS
        res.duration_ms = (time.perf_counter() - t0) * 1000
        self.log.emit("store.done", key=short(key), ok=res.ok, acks=len(res.acks), refused=len(res.refused),
                      failed=len(res.failed), duration_ms=round(res.duration_ms, 1))
        return res

    async def _store_one(self, contact: Contact, key: bytes, rec: dict) -> tuple[bool, str]:
        try:
            stored, reason = await self.rpc.store(contact, key, rec)
            return stored, reason
        except RpcError:
            return False, "rpc"

    async def get_record(self, key: bytes, purpose: str = "control") -> LookupResult:
        return await Lookup(self, key, "value", purpose).run()

    def local_record(self, key: bytes) -> dict | None:
        """Действительная запись по ключу из собственного хранилища (или собственная запись владельца)."""
        rec = self.store.get(key, now_ms())
        own = self.own_record
        if rec is None and own is not None and key == self.own_record_key and own["expires_at"] > now_ms():
            rec = own
        return rec

    def _hand_off_records(self, contact: Contact) -> None:
        """Передача записей новому контакту (Kademlia, §2.5 статьи Maymounkov–Mazières): если новый
        узел входит в R ближайших к ключу хранимой записи, он получает STORE сразу, а не после
        переопубликования. Иначе поиск, сходящийся на присоединившихся позже ближайших узлах,
        не находил бы запись, хранящуюся на более дальних узлах."""
        if not self.joined:
            return
        keys = set(self.store.keys())
        if self.own_record is not None:
            keys.add(self.own_record_key)
        r = self.cfg.REPLICATION_FACTOR
        for key in keys:
            rec = self.local_record(key)
            if rec is None or rec["node_id"] == contact.node_id:
                continue
            nearest = [c.node_id for c in self.table.closest(key, r)] + [self.identity.node_id]
            nearest.sort(key=lambda nid: xor_distance(nid, key))
            if contact.node_id in nearest[:r]:
                task = asyncio.ensure_future(self._hand_off_one(contact, key, rec))
                self._handoffs.add(task)
                task.add_done_callback(self._handoffs.discard)

    async def _hand_off_one(self, contact: Contact, key: bytes, rec: dict) -> None:
        ok, reason = await self._store_one(contact, key, rec)
        self.log.emit("record.handoff", key=short(key), owner=short(rec["node_id"]), to=contact.short(),
                      seq=rec["sequence_number"], ok=ok, result=reason)

    async def resolve(self, node_id: bytes) -> Contact | None:
        """NodeID -> проверенный контакт через подписанную запись DHT."""
        lk = await self.get_record(record_key(node_id), purpose="resolve")
        if not lk.success or lk.record["node_id"] != node_id:
            return None
        rec = lk.record
        addr = rec["addresses"][0]
        return Contact(rec["node_id"], rec["identity_public_key"], addr["host"], addr["port"])

    # ================================================================== туннели (фасад)
    async def build_tunnel(self, destination_node_id: bytes, constraints: dict | None = None):
        return await self.tunnels.build_to(destination_node_id, constraints or {})

    async def send_via_tunnel(self, tunnel_id: bytes, payload: bytes) -> None:
        await self.tunnels.send(tunnel_id, payload)

    async def close_tunnel(self, tunnel_id: bytes) -> None:
        await self.tunnels.close(tunnel_id, "closed by application")

    async def send_message(self, destination_node_id: bytes, text: str) -> dict:
        return await self.messaging.send(destination_node_id, text)

    # ================================================================== состояние
    def get_network_state(self) -> dict:
        return {
            "node_id": self.identity.node_id.hex(),
            "address": f"{self.cfg.advertise_host}:{self.cfg.advertise_port}",
            "security": self.cfg.SECURITY_MODE,
            "joined": self.joined,
            "uptime_s": round((now_ms() - self.started_ms) / 1000, 1),
            "routing": {"size": self.table.size(), "buckets": self.table.bucket_fill()},
            "storage": {"records": len(self.store)},
            "connections": {"outbound": len(self.transport.outbound), "inbound": len(self.transport.inbound)},
            "tunnels": self.tunnels.summary() if self.tunnels else [],
            "own_record": describe(self.own_record) if self.own_record else None,
            "counters": dict(self.log.counters),
        }

    # ================================================================== фоновые задачи
    async def _maintenance_loop(self) -> None:
        last_refresh = last_republish = time.monotonic()
        while True:
            await asyncio.sleep(1.0)
            try:
                now = time.monotonic()
                expired = self.store.expire(now_ms())
                if expired:
                    self.log.emit("store.expire", removed=expired)
                if self.joined and now - last_refresh >= self.cfg.REFRESH_INTERVAL_S:
                    last_refresh = now
                    await self.refresh_buckets()
                if self.joined and now - last_republish >= self.cfg.REPUBLISH_INTERVAL_S:
                    last_republish = now
                    await self.publish_self()
                elif (self.joined and self.publish_acks < self.cfg.REPLICATION_FACTOR
                      and now - self._last_publish_try >= 5.0 and self.table.size() > 0):
                    # Запись не размещена на R узлах (например, seed публиковал при пустой
                    # таблице) — повторяем публикацию, не дожидаясь интервала переопубликования.
                    await self.publish_self()
                if int(now) % 10 == 0:
                    self.save_state()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.emit("maintenance.error", level="ERROR", error=type(exc).__name__, detail=str(exc)[:120])
