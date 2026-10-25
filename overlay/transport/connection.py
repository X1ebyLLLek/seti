"""Соединение: кадрирование поверх ByteStream, RPC-корреляция и диспетчеризация.

Обработчики прикладного уровня не читают сокет и не разбирают заголовок — они
получают уже проверенный по схеме payload через Dispatcher.
"""
from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field

from ..metrics import NULL_LOG, short
from ..protocol import (RESPONSE_FOR, RESPONSE_TYPES, ErrorCode, Frame, FrameDecoder, MsgType, ProtocolError,
                        decode_payload, encode_frame, encode_payload, new_request_id)
from ..protocol.messages import DecodeError, SchemaError
from .streams import ByteStream

_conn_ids = itertools.count(1)


class RpcError(Exception):
    """Неуспешный RPC: kind = timeout | connect | closed | protocol | remote | identity."""

    def __init__(self, kind: str, message: str = "", code: str | None = None):
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind, self.code = kind, code


@dataclass
class Handler:
    func: object               # async def handler(conn, frame, body) -> (type, body) | None
    ordered: bool = False      # True: выполняется в цикле чтения (сохраняет порядок кадров)


class Dispatcher:
    """Выбор обработчика только по полю type. Логики Kademlia здесь нет."""

    def __init__(self):
        self._handlers: dict[int, Handler] = {}

    def register(self, mtype: int, func, ordered: bool = False) -> None:
        self._handlers[int(mtype)] = Handler(func, ordered)

    def get(self, mtype: int) -> Handler | None:
        return self._handlers.get(int(mtype))


@dataclass
class _Pending:
    future: asyncio.Future
    request_type: int
    expected: set = field(default_factory=set)


class Connection:
    def __init__(self, stream: ByteStream, direction: str, dispatcher: Dispatcher, *,
                 max_payload: int = 65536, log=NULL_LOG, peer_node_id: bytes | None = None,
                 session_id: bytes | None = None, owner=None, label: str = "conn"):
        self.stream = stream
        self.direction = direction                 # "in" | "out"
        self.dispatcher = dispatcher
        self.max_payload = max_payload
        self.log = log
        self.peer_node_id = peer_node_id           # аутентифицирован при TLS
        self.session_id = session_id
        self.owner = owner                         # Transport (или None в тестах)
        self.conn_id = f"{label}{next(_conn_ids)}"
        self.decoder = FrameDecoder(max_payload)
        self.pending: dict[bytes, _Pending] = {}
        self.closed = False
        self.close_reason = ""
        self.on_close: list = []
        self.frames_in = self.frames_out = 0
        self.bytes_in = self.bytes_out = 0
        self.frozen = lambda: False                # инъекция отказа «узел завис»
        self._tasks: set[asyncio.Task] = set()
        self._reader_task: asyncio.Task | None = None
        self.last_activity = asyncio.get_event_loop().time()

    # ------------------------------------------------------------------ отправка
    def send_nowait(self, mtype: int, body: dict, request_id: bytes | None = None) -> bytes:
        if self.closed:
            raise RpcError("closed", self.close_reason)
        rid = request_id or new_request_id()
        payload = encode_payload(mtype, body)
        data = encode_frame(Frame(type=int(mtype), request_id=rid, payload=payload), self.max_payload)
        self.stream.write(data)
        self.frames_out += 1
        self.bytes_out += len(data)
        self.last_activity = asyncio.get_event_loop().time()
        self.log.emit("frame.send", level="DEBUG" if mtype in (MsgType.TUNNEL_DATA, MsgType.TUNNEL_ACK) else "INFO",
                      dir="out", conn=self.conn_id, type=MsgType(mtype).name, rid=short(rid),
                      size=len(data), result="ok")
        return rid

    async def send(self, mtype: int, body: dict, request_id: bytes | None = None) -> bytes:
        rid = self.send_nowait(mtype, body, request_id)
        try:
            await self.stream.drain()
        except (ConnectionError, OSError) as exc:
            self.close(f"write failed: {type(exc).__name__}")
            raise RpcError("closed", "write failed") from None
        return rid

    async def request(self, mtype: int, body: dict, timeout: float) -> tuple[Frame, dict]:
        """RPC: отправка запроса и ожидание ответа с тем же request_id."""
        if self.closed:
            raise RpcError("closed", self.close_reason)
        rid = new_request_id()
        fut = asyncio.get_event_loop().create_future()
        expected = {int(RESPONSE_FOR[MsgType(mtype)])}
        if mtype == MsgType.TUNNEL_BUILD:
            expected.add(int(MsgType.TUNNEL_BUILD_FAIL))
        self.pending[rid] = _Pending(fut, int(mtype), expected)
        try:
            await self.send(mtype, body, rid)
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self.log.emit("rpc.timeout", level="WARNING", conn=self.conn_id, type=MsgType(mtype).name,
                          rid=short(rid), timeout_ms=int(timeout * 1000))
            raise RpcError("timeout", f"{MsgType(mtype).name} after {timeout:.1f}s") from None
        finally:
            self.pending.pop(rid, None)

    # ------------------------------------------------------------------ приём
    def start(self) -> asyncio.Task:
        self._reader_task = asyncio.ensure_future(self._read_loop())
        return self._reader_task

    async def _read_loop(self) -> None:
        reason = "eof"
        try:
            while not self.closed:
                data = await self.stream.recv()
                if not data:
                    reason = "peer closed" if not self.decoder.mid_frame else "peer closed mid-frame"
                    break
                self.bytes_in += len(data)
                self.last_activity = asyncio.get_event_loop().time()
                self.decoder.feed(data)
                for item in self.decoder.drain():
                    if isinstance(item, ProtocolError):
                        await self._protocol_violation(item.code, item.message, item.request_id)
                        return
                    if not await self._on_frame(item):
                        return
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        except Exception as exc:  # ssl.SSLError, ConnectionError и т.п.
            reason = f"{type(exc).__name__}: {str(exc)[:80]}"
        finally:
            self.close(reason)

    async def _on_frame(self, frame: Frame) -> bool:
        """Возвращает False, если соединение закрыто из-за нарушения."""
        self.frames_in += 1
        mtype = frame.type
        if self.frozen():
            self.log.emit("frame.drop", level="DEBUG", dir="in", conn=self.conn_id, type=frame.type_name,
                          rid=short(frame.request_id), result="frozen")
            return True
        if mtype in RESPONSE_TYPES or mtype == MsgType.ERROR:
            return await self._on_response(frame)
        try:
            body = decode_payload(mtype, frame.payload)
        except (DecodeError, SchemaError) as exc:
            code = ErrorCode.DECODE_ERROR if isinstance(exc, DecodeError) else ErrorCode.SCHEMA_ERROR
            await self._protocol_violation(code, str(exc), frame.request_id)
            return False
        self.log.emit("frame.recv", level="DEBUG" if mtype in (MsgType.TUNNEL_DATA, MsgType.TUNNEL_ACK) else "INFO",
                      dir="in", conn=self.conn_id, type=frame.type_name, rid=short(frame.request_id),
                      size=len(frame.payload) + 24, result="ok")
        handler = self.dispatcher.get(mtype)
        if handler is None:
            await self._protocol_violation(ErrorCode.BAD_DIRECTION, f"no handler for {frame.type_name}",
                                           frame.request_id)
            return False
        if handler.ordered:
            await self._run_handler(handler, frame, body)
        else:
            task = asyncio.ensure_future(self._run_handler(handler, frame, body))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return not self.closed

    async def _run_handler(self, handler: Handler, frame: Frame, body: dict) -> None:
        try:
            result = await handler.func(self, frame, body)
        except RpcError:
            return
        except ProtocolViolation as exc:
            await self._protocol_violation(exc.code, exc.message, frame.request_id)
            return
        except Exception as exc:
            self.log.emit("handler.error", level="ERROR", conn=self.conn_id, type=frame.type_name,
                          error=type(exc).__name__, detail=str(exc)[:120])
            result = (MsgType.ERROR, {"code": ErrorCode.INTERNAL, "message": "internal error"})
        if result is not None and not self.closed:
            rtype, rbody = result
            try:
                await self.send(rtype, rbody, frame.request_id)
            except RpcError:
                pass

    async def _on_response(self, frame: Frame) -> bool:
        pending = self.pending.get(frame.request_id)
        if pending is None:
            if frame.type == MsgType.ERROR:
                # ERROR без ожидающего запроса — уведомление; фиксируем и продолжаем.
                self.log.emit("frame.recv", level="WARNING", dir="in", conn=self.conn_id, type="ERROR",
                              rid=short(frame.request_id), size=len(frame.payload) + 24, result="notification")
                return True
            await self._protocol_violation(ErrorCode.UNEXPECTED_RESPONSE,
                                           f"{frame.type_name} does not match any pending request",
                                           frame.request_id)
            return False
        if frame.type != MsgType.ERROR and frame.type not in pending.expected:
            await self._protocol_violation(ErrorCode.BAD_DIRECTION,
                                           f"{frame.type_name} is not a response to "
                                           f"{MsgType(pending.request_type).name}", frame.request_id)
            return False
        try:
            body = decode_payload(frame.type, frame.payload)
        except (DecodeError, SchemaError) as exc:
            code = ErrorCode.DECODE_ERROR if isinstance(exc, DecodeError) else ErrorCode.SCHEMA_ERROR
            if not pending.future.done():
                pending.future.set_exception(RpcError("protocol", str(exc), code))
            await self._protocol_violation(code, str(exc), frame.request_id)
            return False
        self.log.emit("frame.recv", dir="in", conn=self.conn_id, type=frame.type_name,
                      rid=short(frame.request_id), size=len(frame.payload) + 24, result="response")
        if not pending.future.done():
            if frame.type == MsgType.ERROR:
                pending.future.set_exception(RpcError("remote", body["message"], body["code"]))
            else:
                pending.future.set_result((frame, body))
        return True

    async def _protocol_violation(self, code: str, message: str, request_id: bytes | None) -> None:
        self.log.emit("protocol.error", level="WARNING", dir="in", conn=self.conn_id, code=code,
                      rid=short(request_id), detail=message[:120])
        if request_id is not None and not self.closed:
            try:
                await asyncio.wait_for(self.send(MsgType.ERROR, {"code": code, "message": message[:200]},
                                                 request_id), 1.0)
            except (RpcError, asyncio.TimeoutError, ConnectionError, OSError):
                pass
        self.close(f"protocol violation: {code}")

    # ------------------------------------------------------------------ закрытие
    def close(self, reason: str = "closed") -> None:
        if self.closed:
            return
        self.closed = True
        self.close_reason = reason
        for p in self.pending.values():
            if not p.future.done():
                p.future.set_exception(RpcError("closed", reason))
        self.pending.clear()
        try:
            self.stream.close()
        except Exception:
            pass
        current = asyncio.current_task() if asyncio.get_event_loop().is_running() else None
        if self._reader_task is not None and self._reader_task is not current and not self._reader_task.done():
            self._reader_task.cancel()
        self.log.emit("conn.close", dir=self.direction, conn=self.conn_id, peer=short(self.peer_node_id),
                      reason=reason, frames_in=self.frames_in, frames_out=self.frames_out)
        for cb in list(self.on_close):
            try:
                cb(self)
            except Exception:
                pass


class ProtocolViolation(Exception):
    """Обработчик сообщает о нарушении протокола (соединение будет закрыто)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message
