"""Прикладной сервис текстовых сообщений.

Отправка: разрешение получателя по NodeID через подписанную запись DHT ->
туннель через TUNNEL_RELAYS ретрансляторов -> end-to-end TLS 1.3 сессия I<->D
поверх туннеля (ретрансляторы видят только шифртекст) -> APP_MESSAGE -> APP_ACK.
Каждое сообщение имеет случайный 128-битный message_id; при повторной отправке
(после отказа туннеля) message_id сохраняется, а получатель по паре
(отправитель, message_id) распознаёт повтор и не выполняет прикладное действие
второй раз. Прямой доставки в обход туннеля нет.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass

from ..metrics import now_ms, short
from ..protocol import ErrorCode, MsgType
from ..routing import Contact
from ..transport import Connection, Dispatcher, ProtocolViolation, RpcError
from ..tunnel import ACTIVE, DEGRADED, OutTunnel, TunnelError


@dataclass
class E2ESession:
    dest_id: bytes
    tunnel: OutTunnel
    conn: Connection
    session_id: bytes
    established_ms: int

    @property
    def alive(self) -> bool:
        return not self.conn.closed and self.tunnel.state in (ACTIVE, DEGRADED)


class MessagingService:
    def __init__(self, node):
        self.node = node
        self.cfg = node.cfg
        self.log = node.log
        self.local = node.identity.node_id
        self.dispatcher = Dispatcher()
        self.dispatcher.register(MsgType.APP_MESSAGE, self.on_app_message)
        self.sessions: dict[bytes, E2ESession] = {}
        self._pending_setup: dict[bytes, asyncio.Future] = {}
        self._dest_cache: dict[bytes, Contact] = {}
        self.keepalive: set[bytes] = set()          # получатели, туннель к которым перестраивается сразу
        self.inbox: list[dict] = []
        self.recoveries: list[dict] = []
        self._tasks: set[asyncio.Task] = set()
        self.history_path = os.path.join(self.cfg.NODE_STATE_DIR, "messages.jsonl")
        self.seen: set[tuple[str, str]] = set()
        self._load_seen()

    # ================================================================== история
    def _load_seen(self) -> None:
        if not os.path.exists(self.history_path):
            return
        with open(self.history_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("dir") == "received":
                    self.seen.add((rec["peer"], rec["message_id"]))

    def _history(self, rec: dict) -> None:
        with open(self.history_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts_ms": now_ms(), **rec}, ensure_ascii=False) + "\n")

    # ================================================================== сессии
    async def _resolve(self, dest_id: bytes, refresh: bool = False) -> tuple[Contact, float]:
        if not refresh and dest_id in self._dest_cache:
            return self._dest_cache[dest_id], 0.0
        t0 = time.perf_counter()
        contact = await self.node.resolve(dest_id)
        if contact is None:
            raise TunnelError("recipient record not found in DHT")
        self._dest_cache[dest_id] = contact
        return contact, (time.perf_counter() - t0) * 1000

    async def ensure_session(self, dest_id: bytes) -> tuple[E2ESession, dict | None]:
        s = self.sessions.get(dest_id)
        if s is not None and s.alive:
            return s, None
        fut = self._pending_setup.get(dest_id)
        if fut is not None:
            return await asyncio.shield(fut), None
        fut = asyncio.get_event_loop().create_future()
        self._pending_setup[dest_id] = fut
        try:
            result = await self._setup(dest_id)
            fut.set_result(result[0])
            return result
        except BaseException as exc:
            fut.set_exception(exc)
            fut.exception()
            raise
        finally:
            self._pending_setup.pop(dest_id, None)

    async def _setup(self, dest_id: bytes, exclude: set | None = None) -> tuple[E2ESession, dict]:
        t0 = time.perf_counter()
        dest, resolve_ms = await self._resolve(dest_id)
        try:
            tun = await self.node.tunnels.build_to(dest_id, {"dest_contact": dest, "exclude": exclude or set()})
        except TunnelError:
            dest, more = await self._resolve(dest_id, refresh=True)     # адрес мог измениться
            resolve_ms += more
            tun = await self.node.tunnels.build_to(dest_id, {"dest_contact": dest, "exclude": exclude or set()})
        t1 = time.perf_counter()
        try:
            res = await asyncio.wait_for(self.node.security.secure_client(tun.stream, dest_id, purpose="e2e"),
                                         self.cfg.READ_TIMEOUT_MS / 1000)
        except Exception as exc:
            await self.node.tunnels.close(tun.tunnel_id, "e2e handshake failed")
            raise TunnelError(f"end-to-end handshake failed: {type(exc).__name__}: {exc}") from None
        handshake_ms = (time.perf_counter() - t1) * 1000
        conn = Connection(res.stream, "out", self.dispatcher, max_payload=self.cfg.MAX_FRAME_PAYLOAD, log=self.log,
                          peer_node_id=dest_id, session_id=res.session_id, label="e2e")
        conn.start()
        session = E2ESession(dest_id, tun, conn, res.session_id, now_ms())
        self.sessions[dest_id] = session
        tun.on_dead.append(self._on_tunnel_dead)
        info = {"resolve_ms": round(resolve_ms, 2), "build_ms": round(tun.build_ms, 2),
                "handshake_ms": round(handshake_ms, 2), "setup_ms": round((time.perf_counter() - t0) * 1000, 2),
                "session": res.session_id.hex()[:8]}
        self.log.emit("e2e.session", peer=short(dest_id), tunnel=short(tun.tunnel_id), **info)
        return session, info

    def _on_tunnel_dead(self, tun: OutTunnel) -> None:
        dest_id = tun.dest.node_id
        s = self.sessions.get(dest_id)
        if s is not None and s.tunnel is tun:
            s.conn.close("tunnel dead")
            self.sessions.pop(dest_id, None)
        if dest_id in self.keepalive:
            task = asyncio.ensure_future(self._recover(tun))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _recover(self, dead: OutTunnel) -> None:
        """Автоматическое построение нового маршрута после отказа (исключая отказавший узел)."""
        dest_id = dead.dest.node_id
        exclude = {dead.failed_node} if dead.failed_node and dead.failed_node != dest_id else set()
        self.log.emit("recovery.start", peer=short(dest_id), dead_tunnel=short(dead.tunnel_id),
                      failed=short(dead.failed_node), reason=dead.close_reason)
        for attempt in range(1, self.cfg.MESSAGE_MAX_ATTEMPTS + 1):
            if dest_id in self.sessions and self.sessions[dest_id].alive:
                return
            try:
                if dest_id in self._pending_setup:
                    await asyncio.shield(self._pending_setup[dest_id])
                    return
                fut = asyncio.get_event_loop().create_future()
                self._pending_setup[dest_id] = fut
                try:
                    session, info = await self._setup(dest_id, exclude)
                    fut.set_result(session)
                finally:
                    self._pending_setup.pop(dest_id, None)
                    if not fut.done():
                        fut.set_exception(TunnelError("recovery failed"))
                        fut.exception()
                rec = {"dead_tunnel": dead.tunnel_id.hex(), "dead_ms": dead.dead_ms,
                       "failed_node": dead.failed_node.hex() if dead.failed_node else None,
                       "reason": dead.close_reason, "new_tunnel": session.tunnel.tunnel_id.hex(),
                       "active_ms": now_ms(), "recovery_ms": now_ms() - dead.dead_ms, "attempt": attempt,
                       "new_route": session.tunnel.route(self.local), **info}
                self.recoveries.append(rec)
                self.log.emit("recovery.done", peer=short(dest_id), new_tunnel=short(session.tunnel.tunnel_id),
                              recovery_ms=rec["recovery_ms"], attempt=attempt,
                              route="->".join(r[:8] for r in rec["new_route"]))
                return
            except (TunnelError, RpcError) as exc:
                self.log.emit("recovery.retry", level="WARNING", peer=short(dest_id), attempt=attempt,
                              error=str(exc)[:100])
                await asyncio.sleep(0.3 * attempt)
        self.log.emit("recovery.failed", level="ERROR", peer=short(dest_id))

    # ================================================================== отправка
    async def send(self, dest_id: bytes, text: str, keepalive: bool = True) -> dict:
        if len(text.encode("utf-8")) > self.cfg.MESSAGE_MAX_BYTES:
            raise ValueError("message too long")
        if dest_id == self.local:
            raise ValueError("cannot send to self")
        if keepalive:
            self.keepalive.add(dest_id)
        message_id = os.urandom(16)
        t0 = time.perf_counter()
        result = {"message_id": message_id.hex(), "to": dest_id.hex(), "status": "failed", "attempts": 0,
                  "first_message": False, "setup": None, "error": None}
        for attempt in range(1, self.cfg.MESSAGE_MAX_ATTEMPTS + 1):
            result["attempts"] = attempt
            try:
                session, setup = await self.ensure_session(dest_id)
                if setup is not None:
                    result["first_message"] = True
                    result["setup"] = setup
                body = {"message_id": message_id, "sender_node_id": self.local, "recipient_node_id": dest_id,
                        "timestamp_ms": now_ms(), "kind": "text", "body": text}
                t_req = time.perf_counter()
                frame, ack = await session.conn.request(MsgType.APP_MESSAGE, body,
                                                        self.cfg.MESSAGE_ACK_TIMEOUT_MS / 1000)
                if ack["message_id"] != message_id:
                    raise RpcError("protocol", "APP_ACK for another message")
                result.update(status=ack["status"], rtt_ms=round((time.perf_counter() - t_req) * 1000, 3),
                              tunnel_id=session.tunnel.tunnel_id.hex(), route=session.tunnel.route(self.local),
                              session=session.session_id.hex()[:8], error=None)
                break
            except (TunnelError, RpcError) as exc:
                result["error"] = str(exc)[:200]
                self.log.emit("app.send_retry", level="WARNING", peer=short(dest_id), message=short(message_id),
                              attempt=attempt, error=str(exc)[:100])
                s = self.sessions.get(dest_id)
                if s is not None and not s.alive:
                    self.sessions.pop(dest_id, None)
                await asyncio.sleep(0.2 * attempt)
        result["total_ms"] = round((time.perf_counter() - t0) * 1000, 3)
        route = result.get("route") or []
        self.log.emit("app.sent", peer=short(dest_id), message=short(message_id), status=result["status"],
                      attempts=result["attempts"], total_ms=result["total_ms"],
                      route="->".join(r[:8] for r in route))
        self._history({"dir": "sent", "peer": dest_id.hex(), "message_id": message_id.hex(),
                       "status": result["status"], "attempts": result["attempts"], "total_ms": result["total_ms"],
                       "rtt_ms": result.get("rtt_ms"), "first_message": result["first_message"],
                       "tunnel_id": result.get("tunnel_id"), "route": route})
        return result

    # ================================================================== приём
    def accept_tunnel(self, end) -> None:
        task = asyncio.ensure_future(self._serve_tunnel(end))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve_tunnel(self, end) -> None:
        try:
            res = await asyncio.wait_for(self.node.security.secure_server(end.stream, purpose="e2e"),
                                         self.cfg.READ_TIMEOUT_MS / 1000)
        except Exception as exc:
            self.log.emit("e2e.reject", level="WARNING", tunnel=short(end.tunnel_id),
                          reason=f"{type(exc).__name__}: {str(exc)[:100]}")
            end.stream.end()
            return
        conn = Connection(res.stream, "in", self.dispatcher, max_payload=self.cfg.MAX_FRAME_PAYLOAD, log=self.log,
                          peer_node_id=res.peer_node_id, session_id=res.session_id, label="e2e")
        self.log.emit("e2e.accept", peer=short(res.peer_node_id), tunnel=short(end.tunnel_id),
                      session=short(res.session_id))
        await conn.start()

    async def on_app_message(self, conn: Connection, frame, body):
        sender = body["sender_node_id"]
        if conn.peer_node_id != sender:
            raise ProtocolViolation(ErrorCode.IDENTITY_MISMATCH, "APP_MESSAGE sender differs from e2e peer")
        mid = body["message_id"]
        if body["recipient_node_id"] != self.local or len(body["body"].encode("utf-8")) > self.cfg.MESSAGE_MAX_BYTES:
            return MsgType.APP_ACK, {"message_id": mid, "status": "rejected", "received_at_ms": now_ms()}
        key = (sender.hex(), mid.hex())
        if key in self.seen:
            # Повтор: подтверждаем, но прикладное действие не выполняется повторно.
            self.log.emit("app.duplicate", peer=short(sender), message=short(mid), session=short(conn.session_id))
            return MsgType.APP_ACK, {"message_id": mid, "status": "duplicate", "received_at_ms": now_ms()}
        self.seen.add(key)
        entry = {"dir": "received", "peer": sender.hex(), "message_id": mid.hex(), "sent_ts_ms": body["timestamp_ms"],
                 "text": body["body"], "session": conn.session_id.hex()[:8] if conn.session_id else None}
        self._history(entry)
        self.inbox.append({"ts_ms": now_ms(), **entry})
        del self.inbox[:-200]
        self.log.emit("app.received", peer=short(sender), message=short(mid), size=len(body["body"]),
                      session=short(conn.session_id))
        return MsgType.APP_ACK, {"message_id": mid, "status": "delivered", "received_at_ms": now_ms()}
