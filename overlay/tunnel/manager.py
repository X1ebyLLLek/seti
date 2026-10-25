"""Туннели: построение через цепочку ретрансляторов, пересылка, контроль, закрытие.

Туннель — фиксированная последовательность различных узлов I -> R1 -> ... -> Rm -> D.
Построение (source-routed): I отправляет R1 запрос TUNNEL_BUILD с оставшимся путём;
каждый ретранслятор проверяет запрос (уникальный tunnel_id, отсутствие петель и
повторов, max_hops, TTL), подключается к следующему узлу и пересылает BUILD дальше.
TUNNEL_BUILD_OK возвращается обратно, и каждый узел добавляет в него подписанное
identity-ключом подтверждение включения (tunnel_id, свой NodeID, номер хопа,
соседи). Инициатор проверяет все подписи — это и есть журнал фактической
последовательности узлов.

Данные: TUNNEL_DATA пересылается по hop-by-hop каналам (TLS), направление
определяется соединением, по которому пришла ячейка. Содержимое ячеек — байты
end-to-end TLS-сессии I<->D, поэтому ретрансляторы не видят прикладной текст.
Контроль: D подтверждает каждую прямую ячейку TUNNEL_ACK; I шлёт пустые ячейки-зонды
при простое. Пропуск подтверждения -> DEGRADED, TUNNEL_DEAD_AFTER_MISSES пропусков,
TUNNEL_CLOSE от соседа или разрыв канала к первому хопу -> DEAD.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field

from ..identity import verify_signature
from ..metrics import now_ms, short
from ..protocol import ErrorCode, MsgType
from ..routing import Contact, InvalidContact, contact_from_wire
from ..transport import Connection, ProtocolViolation, RpcError
from ..transport.streams import ByteStream

CONFIRM_DOMAIN = b"overlay-tunnel-confirm-v1\x00"
ZERO_ID = bytes(32)

BUILDING, ACTIVE, DEGRADED, CLOSING, DEAD = "BUILDING", "ACTIVE", "DEGRADED", "CLOSING", "DEAD"


def confirm_bytes(tunnel_id: bytes, node_id: bytes, hop_index: int, prev_id: bytes, next_id: bytes) -> bytes:
    return CONFIRM_DOMAIN + tunnel_id + node_id + bytes([hop_index]) + prev_id + next_id


class TunnelError(Exception):
    def __init__(self, message: str, failed_node: bytes | None = None):
        super().__init__(message)
        self.failed_node = failed_node


class TunnelStream(ByteStream):
    """Байтовый поток через туннель (для end-to-end TLS)."""

    def __init__(self, sender, drainer, desc: str):
        self._sender, self._drainer = sender, drainer
        self.queue: asyncio.Queue = asyncio.Queue()
        self.ended = False
        self.peer_desc = desc

    def feed(self, data: bytes) -> None:
        if not self.ended and data:
            self.queue.put_nowait(data)

    def end(self) -> None:
        if not self.ended:
            self.ended = True
            self.queue.put_nowait(b"")

    def write(self, data: bytes) -> None:
        if not self.ended:
            self._sender(data)

    async def drain(self) -> None:
        if not self.ended:
            await self._drainer()

    async def recv(self) -> bytes:
        if self.ended and self.queue.empty():
            return b""
        return await self.queue.get()

    def close(self) -> None:
        self.end()

    def is_closing(self) -> bool:
        return self.ended


@dataclass
class OutTunnel:
    tunnel_id: bytes
    path: list                      # [R1, ..., Rm, D] — Contact
    state: str = BUILDING
    created_ms: int = 0
    built_ms: int = 0
    build_ms: float = 0.0
    conn: Connection | None = None
    seq: int = 0
    unacked: dict = field(default_factory=dict)
    misses: int = 0
    last_send: float = 0.0
    last_ack_ms: int = 0
    stream: TunnelStream | None = None
    failed_node: bytes | None = None
    close_reason: str = ""
    dead_ms: int = 0
    acks: int = 0
    ack_rtt_ms: list = field(default_factory=list)
    on_dead: list = field(default_factory=list)
    monitor: asyncio.Task | None = None

    @property
    def relays(self) -> list:
        return self.path[:-1]

    @property
    def dest(self) -> Contact:
        return self.path[-1]

    def route(self, local: bytes) -> list[str]:
        return [local.hex()] + [c.node_id.hex() for c in self.path]

    def describe(self, local: bytes) -> dict:
        return {"tunnel_id": self.tunnel_id.hex(), "role": "initiator", "state": self.state,
                "route": [r[:8] for r in self.route(local)], "route_full": self.route(local),
                "relays": len(self.relays), "created_ms": self.created_ms, "built_ms": self.built_ms,
                "build_ms": round(self.build_ms, 2), "acks": self.acks, "misses": self.misses,
                "failed_node": self.failed_node.hex() if self.failed_node else None,
                "close_reason": self.close_reason, "dead_ms": self.dead_ms}


@dataclass
class RelayEntry:
    tunnel_id: bytes
    prev_conn: Connection
    next_conn: Connection
    prev_id: bytes
    next_id: bytes
    hop_index: int
    expires: float
    cells_fwd: int = 0
    cells_back: int = 0


@dataclass
class EndTunnel:
    tunnel_id: bytes
    prev_conn: Connection
    prev_id: bytes
    hop_index: int
    expires: float
    stream: TunnelStream
    seq_back: int = 0
    cells: int = 0


class TunnelManager:
    def __init__(self, node):
        self.node = node
        self.cfg = node.cfg
        self.log = node.log
        self.local = node.identity.node_id
        self.out: dict[bytes, OutTunnel] = {}
        self.relay: dict[bytes, RelayEntry] = {}
        self.end: dict[bytes, EndTunnel] = {}
        self._conn_tunnels: dict[int, set] = {}
        self._sweeper: asyncio.Task | None = None
        self.history: list[dict] = []           # завершённые туннели инициатора

    def register(self, dispatcher) -> None:
        dispatcher.register(MsgType.TUNNEL_BUILD, self.on_build)
        dispatcher.register(MsgType.TUNNEL_DATA, self.on_data, ordered=True)
        dispatcher.register(MsgType.TUNNEL_ACK, self.on_ack, ordered=True)
        dispatcher.register(MsgType.TUNNEL_CLOSE, self.on_close, ordered=True)
        self._sweeper = asyncio.ensure_future(self._sweep_loop())

    # ================================================================== общие помощники
    def _sign_confirm(self, tid: bytes, hop: int, prev_id: bytes, next_id: bytes) -> dict:
        sig = self.node.identity.sign(confirm_bytes(tid, self.local, hop, prev_id, next_id))
        return {"node_id": self.local, "hop_index": hop, "prev_node_id": prev_id, "next_node_id": next_id,
                "signature": sig}

    def _watch(self, conn: Connection, tid: bytes) -> None:
        key = id(conn)
        if key not in self._conn_tunnels:
            self._conn_tunnels[key] = set()
            conn.on_close.append(self._on_conn_closed)
        self._conn_tunnels[key].add(tid)
        conn.tunnel_refs = len(self._conn_tunnels[key])

    def _unwatch(self, conn: Connection | None, tid: bytes) -> None:
        if conn is None:
            return
        s = self._conn_tunnels.get(id(conn))
        if s is not None:
            s.discard(tid)
            conn.tunnel_refs = len(s)

    def _send_close(self, conn: Connection | None, tid: bytes, reason: str, failed: bytes | None) -> None:
        if conn is None or conn.closed:
            return
        body = {"tunnel_id": tid, "reporter": self.local, "reason": reason[:128]}
        if failed:
            body["failed_node_id"] = failed
        try:
            conn.send_nowait(MsgType.TUNNEL_CLOSE, body)
        except RpcError:
            pass

    def _on_conn_closed(self, conn: Connection) -> None:
        tids = self._conn_tunnels.pop(id(conn), set())
        for tid in list(tids):
            if tid in self.relay:
                e = self.relay.pop(tid)
                if conn is e.prev_conn:
                    self._send_close(e.next_conn, tid, "previous hop lost", e.prev_id)
                    self._unwatch(e.next_conn, tid)
                    failed = e.prev_id
                else:
                    self._send_close(e.prev_conn, tid, "next hop lost", e.next_id)
                    self._unwatch(e.prev_conn, tid)
                    failed = e.next_id
                self.log.emit("tunnel.relay_close", tunnel=short(tid), reason="link lost", failed=short(failed))
            elif tid in self.end:
                e = self.end.pop(tid)
                e.stream.end()
                self.log.emit("tunnel.end_close", tunnel=short(tid), reason="link lost", failed=short(e.prev_id))
            elif tid in self.out:
                tun = self.out[tid]
                self._set_dead(tun, "first hop link lost", tun.path[0].node_id)

    # ================================================================== инициатор
    def select_relays(self, dest_id: bytes, count: int, exclude: set) -> list[Contact]:
        pool = [c for c in self.node.table.contacts()
                if c.node_id not in exclude and c.node_id not in (dest_id, self.local)]
        if len(pool) < count:
            return []
        return self.node.rng.sample(pool, count)

    async def build_to(self, dest_id: bytes, constraints: dict) -> OutTunnel:
        """Разрешение получателя через DHT, выбор ретрансляторов, построение с повторами."""
        relays_n = int(constraints.get("relays", self.cfg.TUNNEL_RELAYS))
        if relays_n < self.cfg.TUNNEL_RELAYS:
            raise TunnelError(f"at least {self.cfg.TUNNEL_RELAYS} relays required")
        exclude = set(bytes.fromhex(x) if isinstance(x, str) else x for x in constraints.get("exclude", []))
        dest = constraints.get("dest_contact") or await self.node.resolve(dest_id)
        if dest is None:
            raise TunnelError("destination record not found in DHT")
        last_error: TunnelError | None = None
        for attempt in range(1, 4):
            relays = self.select_relays(dest_id, relays_n, exclude)
            if not relays:
                await self.node.refresh_buckets(force=True)
                relays = self.select_relays(dest_id, relays_n, exclude)
            if not relays:
                raise TunnelError("not enough relay candidates")
            try:
                return await self.build(relays + [dest])
            except TunnelError as exc:
                last_error = exc
                if exc.failed_node and exc.failed_node != dest_id:
                    exclude.add(exc.failed_node)
                else:
                    exclude.update(c.node_id for c in relays)
                self.log.emit("tunnel.retry", level="WARNING", attempt=attempt, error=str(exc)[:100])
        raise last_error or TunnelError("tunnel build failed")

    async def build(self, path: list[Contact]) -> OutTunnel:
        ids = [c.node_id for c in path]
        if len(set(ids)) != len(ids) or self.local in ids:
            raise TunnelError("path contains repeated nodes or the initiator")
        if len(path) - 1 < self.cfg.TUNNEL_RELAYS or len(path) > self.cfg.TUNNEL_MAX_HOPS:
            raise TunnelError("path length violates TUNNEL_RELAYS / TUNNEL_MAX_HOPS")
        tid = os.urandom(16)
        tun = OutTunnel(tid, list(path), created_ms=now_ms())
        self.out[tid] = tun
        route = "->".join([self.local.hex()[:8]] + [c.short() for c in path])
        self.log.emit("tunnel.build_start", tunnel=short(tid), route=route)
        t0 = time.perf_counter()
        first = path[0]
        try:
            conn = await self.node.transport.get_connection(first.host, first.port, first.node_id)
            body = {"tunnel_id": tid, "sender": self.node.self_contact().to_wire(), "hop_index": 1,
                    "max_hops": self.cfg.TUNNEL_MAX_HOPS, "ttl_s": self.cfg.TUNNEL_TTL_S,
                    "remaining": [c.to_wire() for c in path[1:]]}
            frame, resp = await conn.request(MsgType.TUNNEL_BUILD, body, self.cfg.TUNNEL_BUILD_TIMEOUT_MS / 1000)
        except RpcError as exc:
            self._set_dead(tun, f"build failed: {exc}", first.node_id, notify=False)
            raise TunnelError(f"first hop unreachable: {exc.kind}", first.node_id) from None
        if frame.type == MsgType.TUNNEL_BUILD_FAIL:
            failed = resp.get("failed_node_id")
            self._set_dead(tun, f"build refused: {resp['reason']}", failed, notify=False)
            raise TunnelError(f"build refused by {short(resp['reporter'])}: {resp['reason']}", failed)
        problem = self._verify_confirmations(tid, path, resp["confirmations"])
        if problem:
            self._set_dead(tun, problem, None, notify=False)
            self._send_close(conn, tid, "bad confirmation", None)
            raise TunnelError(problem)
        tun.conn = conn
        tun.build_ms = (time.perf_counter() - t0) * 1000
        tun.built_ms = now_ms()
        tun.last_send = time.monotonic()
        tun.stream = TunnelStream(lambda d, t=tun: self._send_forward(t, d), conn.stream.drain,
                                  f"tunnel:{tid.hex()[:8]}")
        self._watch(conn, tid)
        tun.state = ACTIVE
        tun.monitor = asyncio.ensure_future(self._monitor(tun))
        self.log.emit("tunnel.active", tunnel=short(tid), route=route, relays=len(path) - 1,
                      build_ms=round(tun.build_ms, 1), confirmed=len(resp["confirmations"]))
        return tun

    def _verify_confirmations(self, tid: bytes, path: list[Contact], confs: list[dict]) -> str | None:
        if len(confs) != len(path):
            return f"expected {len(path)} confirmations, got {len(confs)}"
        for i, (c, conf) in enumerate(zip(path, confs)):
            prev_id = self.local if i == 0 else path[i - 1].node_id
            next_id = path[i + 1].node_id if i + 1 < len(path) else ZERO_ID
            if (conf["node_id"] != c.node_id or conf["hop_index"] != i + 1 or conf["prev_node_id"] != prev_id
                    or conf["next_node_id"] != next_id):
                return f"confirmation {i + 1} does not match the requested route"
            if not verify_signature(c.identity_public_key, conf["signature"],
                                    confirm_bytes(tid, c.node_id, i + 1, prev_id, next_id)):
                return f"confirmation {i + 1} has an invalid signature"
        return None

    def _send_forward(self, tun: OutTunnel, data: bytes) -> None:
        if tun.state in (DEAD, CLOSING) or tun.conn is None:
            return
        step = self.cfg.TUNNEL_CELL_BYTES
        for off in range(0, max(len(data), 1), step):
            self._send_cell(tun, data[off:off + step])

    def _send_cell(self, tun: OutTunnel, chunk: bytes) -> None:
        tun.seq += 1
        tun.unacked[tun.seq] = time.monotonic()
        tun.last_send = time.monotonic()
        try:
            tun.conn.send_nowait(MsgType.TUNNEL_DATA, {"tunnel_id": tun.tunnel_id, "seq": tun.seq, "data": chunk})
        except RpcError:
            self._set_dead(tun, "first hop link lost", tun.path[0].node_id)

    async def _monitor(self, tun: OutTunnel) -> None:
        probe = self.cfg.TUNNEL_PROBE_INTERVAL_MS / 1000
        ack_to = self.cfg.TUNNEL_ACK_TIMEOUT_MS / 1000
        try:
            while tun.state in (ACTIVE, DEGRADED):
                await asyncio.sleep(min(probe, ack_to) / 2)
                now = time.monotonic()
                if now - tun.last_send >= probe:
                    self._send_cell(tun, b"")                 # зонд доступности
                late = [s for s, t in tun.unacked.items() if now - t > ack_to]
                if late:
                    for s in late:
                        tun.unacked.pop(s, None)
                    tun.misses += 1
                    if tun.misses >= self.cfg.TUNNEL_DEAD_AFTER_MISSES:
                        self._set_dead(tun, f"no TUNNEL_ACK for {tun.misses} intervals", None)
                    elif tun.state == ACTIVE:
                        tun.state = DEGRADED
                        self.log.emit("tunnel.state", level="WARNING", tunnel=short(tun.tunnel_id), state=DEGRADED,
                                      reason="ack timeout", misses=tun.misses)
                if (now_ms() - tun.built_ms) / 1000 > self.cfg.TUNNEL_TTL_S:
                    await self.close(tun.tunnel_id, "tunnel ttl expired")
        except asyncio.CancelledError:
            pass

    def _on_out_ack(self, tun: OutTunnel, seq: int) -> None:
        sent = tun.unacked.pop(seq, None)
        tun.acks += 1
        tun.last_ack_ms = now_ms()
        if sent is not None and len(tun.ack_rtt_ms) < 1000:
            tun.ack_rtt_ms.append((time.monotonic() - sent) * 1000)
        if tun.misses or tun.state == DEGRADED:
            tun.misses = 0
            if tun.state == DEGRADED:
                tun.state = ACTIVE
                self.log.emit("tunnel.state", tunnel=short(tun.tunnel_id), state=ACTIVE, reason="ack received")

    def _set_dead(self, tun: OutTunnel, reason: str, failed: bytes | None, notify: bool = True) -> None:
        if tun.state == DEAD:
            return
        tun.state = DEAD
        tun.close_reason = reason
        tun.failed_node = failed
        tun.dead_ms = now_ms()
        if tun.stream:
            tun.stream.end()
        if tun.monitor and tun.monitor is not asyncio.current_task():
            tun.monitor.cancel()
        self._unwatch(tun.conn, tun.tunnel_id)
        self.out.pop(tun.tunnel_id, None)
        self.history.append(tun.describe(self.local))
        self.log.emit("tunnel.state", level="WARNING", tunnel=short(tun.tunnel_id), state=DEAD, reason=reason,
                      failed=short(failed))
        if notify:
            for cb in list(tun.on_dead):
                try:
                    cb(tun)
                except Exception:
                    pass

    async def send(self, tid: bytes, payload: bytes) -> None:
        tun = self.out.get(tid)
        if tun is None or tun.state not in (ACTIVE, DEGRADED):
            raise TunnelError("tunnel is not active")
        tun.stream.write(payload)
        await tun.stream.drain()

    async def close(self, tid: bytes, reason: str) -> None:
        tun = self.out.get(tid)
        if tun is None:
            return
        tun.state = CLOSING
        self.log.emit("tunnel.state", tunnel=short(tid), state=CLOSING, reason=reason)
        self._send_close(tun.conn, tid, reason, None)
        self._set_dead(tun, reason, None, notify=False)

    # ================================================================== обработчики
    async def on_build(self, conn: Connection, frame, body):
        tid = body["tunnel_id"]
        hop = body["hop_index"]
        try:
            sender = contact_from_wire(body["sender"], self.local)
            remaining = [contact_from_wire(w, self.local) for w in body["remaining"]]
        except InvalidContact as exc:
            return self._fail(tid, f"invalid contact: {exc}")
        if conn.peer_node_id is not None and conn.peer_node_id != sender.node_id:
            raise ProtocolViolation(ErrorCode.IDENTITY_MISMATCH, "TUNNEL_BUILD sender differs from link peer")
        ids = [c.node_id for c in remaining]
        if tid in self.relay or tid in self.end or tid in self.out:
            return self._fail(tid, "duplicate tunnel_id (loop)")
        if len(set(ids)) != len(ids) or sender.node_id in ids:
            return self._fail(tid, "repeated node in path (loop)")
        if hop < 1 or hop + len(remaining) > min(body["max_hops"], self.cfg.TUNNEL_MAX_HOPS):
            return self._fail(tid, "max_hops exceeded")
        if body["ttl_s"] == 0 or body["ttl_s"] > self.cfg.TUNNEL_TTL_S:
            return self._fail(tid, "ttl out of range")
        expires = time.monotonic() + body["ttl_s"]
        if not remaining:
            stream = TunnelStream(lambda d, t=tid: self._send_backward(t, d), conn.stream.drain,
                                  f"tunnel:{tid.hex()[:8]}")
            self.end[tid] = EndTunnel(tid, conn, sender.node_id, hop, expires, stream)
            self._watch(conn, tid)
            self.log.emit("tunnel.endpoint", tunnel=short(tid), prev=sender.short(), hop=hop)
            if self.node.messaging is not None:
                self.node.messaging.accept_tunnel(self.end[tid])
            return MsgType.TUNNEL_BUILD_OK, {"tunnel_id": tid,
                                             "confirmations": [self._sign_confirm(tid, hop, sender.node_id, ZERO_ID)]}
        nxt = remaining[0]
        fwd = {"tunnel_id": tid, "sender": self.node.self_contact().to_wire(), "hop_index": hop + 1,
               "max_hops": body["max_hops"], "ttl_s": body["ttl_s"], "remaining": [c.to_wire() for c in remaining[1:]]}
        timeout = max(1.0, self.cfg.TUNNEL_BUILD_TIMEOUT_MS / 1000 - 1.0 * hop)
        try:
            next_conn = await self.node.transport.get_connection(nxt.host, nxt.port, nxt.node_id)
            rframe, resp = await next_conn.request(MsgType.TUNNEL_BUILD, fwd, timeout)
        except RpcError as exc:
            self.log.emit("tunnel.relay_fail", level="WARNING", tunnel=short(tid), next=nxt.short(), error=exc.kind)
            return self._fail(tid, f"next hop unreachable ({exc.kind})", nxt.node_id)
        if rframe.type == MsgType.TUNNEL_BUILD_FAIL:
            return MsgType.TUNNEL_BUILD_FAIL, resp            # передаём отказ нижележащего узла
        if conn.closed:
            self._send_close(next_conn, tid, "previous hop lost during build", sender.node_id)
            return None
        self.relay[tid] = RelayEntry(tid, conn, next_conn, sender.node_id, nxt.node_id, hop, expires)
        self._watch(conn, tid)
        self._watch(next_conn, tid)
        self.log.emit("tunnel.relay", tunnel=short(tid), prev=sender.short(), next=nxt.short(), hop=hop)
        confs = [self._sign_confirm(tid, hop, sender.node_id, nxt.node_id)] + resp["confirmations"]
        return MsgType.TUNNEL_BUILD_OK, {"tunnel_id": tid, "confirmations": confs}

    def _fail(self, tid: bytes, reason: str, failed: bytes | None = None):
        self.log.emit("tunnel.build_refused", level="WARNING", tunnel=short(tid), reason=reason)
        body = {"tunnel_id": tid, "reporter": self.local, "reason": reason[:128]}
        if failed:
            body["failed_node_id"] = failed
        return MsgType.TUNNEL_BUILD_FAIL, body

    def _send_backward(self, tid: bytes, data: bytes) -> None:
        e = self.end.get(tid)
        if e is None or e.prev_conn.closed:
            return
        step = self.cfg.TUNNEL_CELL_BYTES
        for off in range(0, max(len(data), 1), step):
            e.seq_back += 1
            try:
                e.prev_conn.send_nowait(MsgType.TUNNEL_DATA,
                                        {"tunnel_id": tid, "seq": e.seq_back, "data": data[off:off + step]})
            except RpcError:
                return

    async def on_data(self, conn: Connection, frame, body):
        tid, seq, data = body["tunnel_id"], body["seq"], body["data"]
        e = self.relay.get(tid)
        if e is not None:
            if conn is e.prev_conn:
                e.cells_fwd += 1
                self._relay_send(e.next_conn, MsgType.TUNNEL_DATA, body)
            elif conn is e.next_conn:
                e.cells_back += 1
                self._relay_send(e.prev_conn, MsgType.TUNNEL_DATA, body)
            return None
        end = self.end.get(tid)
        if end is not None and conn is end.prev_conn:
            end.cells += 1
            try:
                conn.send_nowait(MsgType.TUNNEL_ACK, {"tunnel_id": tid, "seq": seq})
            except RpcError:
                return None
            if data:
                end.stream.feed(data)
            return None
        tun = self.out.get(tid)
        if tun is not None and conn is tun.conn:
            if data and tun.stream:
                tun.stream.feed(data)
            return None
        self.log.emit("tunnel.unknown", level="WARNING", tunnel=short(tid), conn=conn.conn_id)
        self._send_close(conn, tid, "unknown tunnel", None)
        return None

    def _relay_send(self, conn: Connection, mtype: int, body: dict) -> None:
        try:
            conn.send_nowait(mtype, body)
        except RpcError:
            pass

    async def on_ack(self, conn: Connection, frame, body):
        tid = body["tunnel_id"]
        e = self.relay.get(tid)
        if e is not None and conn is e.next_conn:
            self._relay_send(e.prev_conn, MsgType.TUNNEL_ACK, body)
            return None
        tun = self.out.get(tid)
        if tun is not None and conn is tun.conn:
            self._on_out_ack(tun, body["seq"])
        return None

    async def on_close(self, conn: Connection, frame, body):
        tid = body["tunnel_id"]
        failed = body.get("failed_node_id")
        e = self.relay.pop(tid, None)
        if e is not None:
            other = e.next_conn if conn is e.prev_conn else e.prev_conn
            self._relay_send(other, MsgType.TUNNEL_CLOSE, body)
            self._unwatch(e.prev_conn, tid)
            self._unwatch(e.next_conn, tid)
            self.log.emit("tunnel.relay_close", tunnel=short(tid), reason=body["reason"], failed=short(failed))
            return None
        end = self.end.pop(tid, None)
        if end is not None:
            end.stream.end()
            self._unwatch(end.prev_conn, tid)
            self.log.emit("tunnel.end_close", tunnel=short(tid), reason=body["reason"])
            return None
        tun = self.out.get(tid)
        if tun is not None and conn is tun.conn:
            self._set_dead(tun, f"TUNNEL_CLOSE from {short(body['reporter'])}: {body['reason']}", failed)
        return None

    # ================================================================== обслуживание
    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()
            for tid, e in list(self.relay.items()):
                if now > e.expires:
                    self.relay.pop(tid, None)
                    self._send_close(e.prev_conn, tid, "relay ttl expired", None)
                    self._send_close(e.next_conn, tid, "relay ttl expired", None)
                    self._unwatch(e.prev_conn, tid)
                    self._unwatch(e.next_conn, tid)
            for tid, e in list(self.end.items()):
                if now > e.expires:
                    self.end.pop(tid, None)
                    e.stream.end()
                    self._unwatch(e.prev_conn, tid)

    def summary(self) -> list[dict]:
        out = [t.describe(self.local) for t in self.out.values()]
        out += [{"tunnel_id": e.tunnel_id.hex(), "role": "relay", "hop": e.hop_index,
                 "prev": e.prev_id.hex()[:8], "next": e.next_id.hex()[:8], "cells_fwd": e.cells_fwd,
                 "cells_back": e.cells_back} for e in self.relay.values()]
        out += [{"tunnel_id": e.tunnel_id.hex(), "role": "endpoint", "hop": e.hop_index,
                 "prev": e.prev_id.hex()[:8], "cells": e.cells} for e in self.end.values()]
        return out

    async def shutdown(self) -> None:
        if self._sweeper:
            self._sweeper.cancel()
        for tid in list(self.out):
            await self.close(tid, "node shutdown")
