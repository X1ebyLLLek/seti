"""TCP-транспорт узла: сервер, исходящие соединения, тайм-ауты, пул соединений.

Выбранный подход (описан в ARCHITECTURE.md): соединения переиспользуются.
Исходящие соединения хранятся в пуле по адресу host:port; по одному соединению
мультиплексируются RPC (корреляция по request_id) и туннельные кадры. Простаивающие
соединения закрываются через IDLE_CONNECTION_S.
"""
from __future__ import annotations

import asyncio

from ..config import NodeConfig
from ..metrics import NULL_LOG, short
from ..protocol import MsgType
from .connection import Connection, Dispatcher, RpcError
from .streams import PlainStream


class Transport:
    def __init__(self, cfg: NodeConfig, dispatcher: Dispatcher, log=NULL_LOG, security=None):
        self.cfg = cfg
        self.dispatcher = dispatcher
        self.log = log
        self.security = security                  # SecurityProvider или None (plain)
        self.server: asyncio.base_events.Server | None = None
        self.outbound: dict[tuple[str, int], Connection] = {}
        self._connecting: dict[tuple[str, int], asyncio.Future] = {}
        self.inbound: set[Connection] = set()
        self.frozen_until = 0.0
        self._janitor: asyncio.Task | None = None
        self._handshakes: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ состояние
    def is_frozen(self) -> bool:
        return asyncio.get_event_loop().time() < self.frozen_until

    def freeze(self, seconds: float) -> None:
        """Инъекция отказа: узел перестаёт обрабатывать входящие кадры (тайм-ауты у собеседников)."""
        self.frozen_until = asyncio.get_event_loop().time() + seconds
        self.log.emit("fault.freeze", level="WARNING", seconds=seconds)

    def connections(self) -> list[Connection]:
        return list(self.outbound.values()) + list(self.inbound)

    # ------------------------------------------------------------------ сервер
    async def start(self) -> None:
        self.server = await asyncio.start_server(self._on_inbound, self.cfg.LISTEN_HOST, self.cfg.LISTEN_PORT)
        if self.cfg.LISTEN_PORT == 0:                 # порт выбран ОС (используется в тестах)
            self.cfg.LISTEN_PORT = self.server.sockets[0].getsockname()[1]
        self.log.emit("transport.listen", host=self.cfg.LISTEN_HOST, port=self.cfg.LISTEN_PORT,
                      security=self.cfg.SECURITY_MODE)
        self._janitor = asyncio.ensure_future(self._idle_janitor())

    async def stop(self) -> None:
        if self._janitor:
            self._janitor.cancel()
        if self.server:
            self.server.close()
        for task in list(self._handshakes):
            task.cancel()
        for conn in self.connections():
            conn.close("shutdown")
        if self.server:
            try:
                await asyncio.wait_for(self.server.wait_closed(), 2)
            except asyncio.TimeoutError:
                pass

    async def _on_inbound(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        stream = PlainStream(reader, writer)
        self.log.emit("conn.accept", dir="in", peer_addr=stream.peer_desc)
        if self.is_frozen():
            # «Зависший» узел принимает TCP, но ничего не отвечает.
            await asyncio.sleep(max(0.0, self.frozen_until - asyncio.get_event_loop().time()))
            stream.close()
            return
        peer_id = session_id = None
        task = asyncio.current_task()
        self._handshakes.add(task)
        try:
            if self.security is not None:
                try:
                    res = await asyncio.wait_for(
                        self.security.secure_server(stream, purpose="link"),
                        self.cfg.READ_TIMEOUT_MS / 1000)
                except Exception as exc:
                    self.log.emit("secure.reject", level="WARNING", dir="in", peer_addr=stream.peer_desc,
                                  reason=f"{type(exc).__name__}: {str(exc)[:100]}")
                    stream.close()
                    return
                stream, peer_id, session_id = res.stream, res.peer_node_id, res.session_id
        finally:
            self._handshakes.discard(task)
        conn = self._make_connection(stream, "in", peer_id, session_id)
        self.inbound.add(conn)
        conn.on_close.append(lambda c: self.inbound.discard(c))
        self.log.emit("conn.open", dir="in", conn=conn.conn_id, peer=short(peer_id), peer_addr=stream.peer_desc)
        await conn.start()

    def _make_connection(self, stream, direction, peer_id, session_id) -> Connection:
        conn = Connection(stream, direction, self.dispatcher, max_payload=self.cfg.MAX_FRAME_PAYLOAD,
                          log=self.log, peer_node_id=peer_id, session_id=session_id, owner=self,
                          label="o" if direction == "out" else "i")
        conn.frozen = self.is_frozen
        return conn

    # ------------------------------------------------------------------ исходящие
    async def get_connection(self, host: str, port: int, expected_node_id: bytes | None = None) -> Connection:
        if self.is_frozen():
            raise RpcError("closed", "node is frozen")
        key = (host, port)
        conn = self.outbound.get(key)
        if conn is not None and not conn.closed:
            if expected_node_id and conn.peer_node_id and conn.peer_node_id != expected_node_id:
                # По адресу теперь отвечает другой узел — старое соединение не используем.
                conn.close("peer identity changed")
            else:
                return conn
        fut = self._connecting.get(key)
        if fut is not None:
            conn = await asyncio.shield(fut)
            if expected_node_id and conn.peer_node_id and conn.peer_node_id != expected_node_id:
                raise RpcError("identity", "unexpected peer identity")
            return conn
        fut = asyncio.get_event_loop().create_future()
        self._connecting[key] = fut
        try:
            conn = await self._connect(host, port, expected_node_id)
            fut.set_result(conn)
            return conn
        except BaseException as exc:
            err = exc if isinstance(exc, RpcError) else RpcError("connect", type(exc).__name__)
            fut.set_exception(err)
            fut.exception()                        # помечаем как полученное
            raise err from None
        finally:
            self._connecting.pop(key, None)

    async def _connect(self, host: str, port: int, expected_node_id: bytes | None) -> Connection:
        timeout = self.cfg.CONNECT_TIMEOUT_MS / 1000
        self.log.emit("conn.connect", dir="out", peer_addr=f"{host}:{port}", expect=short(expected_node_id))
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except asyncio.TimeoutError:
            self.log.emit("conn.connect_failed", level="WARNING", dir="out", peer_addr=f"{host}:{port}",
                          reason="timeout")
            raise RpcError("timeout", f"connect to {host}:{port}") from None
        except OSError as exc:
            self.log.emit("conn.connect_failed", level="WARNING", dir="out", peer_addr=f"{host}:{port}",
                          reason=type(exc).__name__)
            raise RpcError("connect", f"{host}:{port} {type(exc).__name__}") from None
        stream = PlainStream(reader, writer)
        peer_id = session_id = None
        if self.security is not None:
            try:
                res = await asyncio.wait_for(
                    self.security.secure_client(stream, expected_node_id, purpose="link"), timeout)
            except Exception as exc:
                stream.close()
                kind = "identity" if "identity" in str(exc).lower() or "mismatch" in str(exc).lower() else "connect"
                self.log.emit("secure.reject", level="WARNING", dir="out", peer_addr=f"{host}:{port}",
                              reason=f"{type(exc).__name__}: {str(exc)[:100]}")
                raise RpcError(kind, f"secure handshake with {host}:{port} failed: {exc}") from None
            stream, peer_id, session_id = res.stream, res.peer_node_id, res.session_id
        conn = self._make_connection(stream, "out", peer_id, session_id)
        key = (host, port)
        self.outbound[key] = conn
        conn.on_close.append(lambda c: self.outbound.pop(key, None) if self.outbound.get(key) is c else None)
        self.log.emit("conn.open", dir="out", conn=conn.conn_id, peer=short(peer_id), peer_addr=f"{host}:{port}",
                      result="ok")
        conn.start()
        return conn

    async def send_request(self, host: str, port: int, mtype: int, body: dict, timeout: float | None = None,
                           expected_node_id: bytes | None = None):
        """RPC к адресу: (Frame, body) или RpcError (connect/timeout/closed/remote/identity)."""
        conn = await self.get_connection(host, port, expected_node_id)
        if timeout is None:
            timeout = (self.cfg.PING_TIMEOUT_MS if mtype == MsgType.PING else self.cfg.READ_TIMEOUT_MS) / 1000
        return await conn.request(mtype, body, timeout)

    def drop_connection(self, host: str, port: int, reason: str) -> None:
        conn = self.outbound.pop((host, port), None)
        if conn:
            conn.close(reason)

    async def _idle_janitor(self) -> None:
        while True:
            await asyncio.sleep(5)
            now = asyncio.get_event_loop().time()
            for conn in list(self.outbound.values()):
                busy = conn.pending or getattr(conn, "tunnel_refs", 0)
                if not busy and now - conn.last_activity > self.cfg.IDLE_CONNECTION_S:
                    conn.close("idle")
