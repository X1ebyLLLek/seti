"""Обработчики и клиентские вызовы DHT RPC: PING, FIND_NODE, STORE, FIND_VALUE, ECHO."""
from __future__ import annotations

import time
from dataclasses import dataclass

from ..dht.records import InvalidRecord, validate_record
from ..metrics import now_ms, short
from ..protocol import ErrorCode, MsgType
from ..routing import Contact, InvalidContact, contact_from_wire
from ..transport import ProtocolViolation, RpcError


@dataclass
class PingResult:
    contact: Contact
    rtt_ms: float


class DhtRpc:
    def __init__(self, node):
        self.node = node
        self.cfg = node.cfg
        self.log = node.log

    def register(self, dispatcher) -> None:
        dispatcher.register(MsgType.PING, self.on_ping)
        dispatcher.register(MsgType.FIND_NODE_REQUEST, self.on_find_node)
        dispatcher.register(MsgType.STORE_REQUEST, self.on_store)
        dispatcher.register(MsgType.FIND_VALUE_REQUEST, self.on_find_value)
        dispatcher.register(MsgType.ECHO_REQUEST, self.on_echo)

    # ================================================================ серверная сторона
    def _sender(self, conn, wire: dict) -> Contact:
        try:
            contact = contact_from_wire(wire, self.node.identity.node_id)
        except InvalidContact as exc:
            self.log.emit("contact.reject", level="WARNING", conn=conn.conn_id, reason=str(exc))
            raise ProtocolViolation(ErrorCode.INVALID_CONTACT, str(exc)) from None
        if conn.peer_node_id is not None and conn.peer_node_id != contact.node_id:
            # При TLS идентичность канала аутентифицирована — заявленный отправитель обязан совпасть.
            raise ProtocolViolation(ErrorCode.IDENTITY_MISMATCH, "sender differs from authenticated peer")
        self.node.routing.observe(contact, verified=False, reason="inbound " + "request")
        return contact

    async def on_ping(self, conn, frame, body):
        self._sender(conn, body["sender"])
        return MsgType.PONG, {"responder": self.node.self_contact().to_wire(),
                              "ping_timestamp_ms": body["timestamp_ms"],
                              "responder_timestamp_ms": now_ms()}

    def _closest_for(self, target: bytes, requester: bytes) -> list[dict]:
        # До k уникальных контактов по неубыванию XOR-расстояния; без локального узла и запросившего.
        found = self.node.table.closest(target, self.cfg.K_BUCKET_SIZE, exclude={requester})
        return [c.to_wire() for c in found]

    async def on_find_node(self, conn, frame, body):
        sender = self._sender(conn, body["sender"])
        return MsgType.FIND_NODE_RESPONSE, {"responder": self.node.self_contact().to_wire(),
                                            "target_node_id": body["target_node_id"],
                                            "contacts": self._closest_for(body["target_node_id"], sender.node_id)}

    async def on_store(self, conn, frame, body):
        self._sender(conn, body["sender"])
        key, rec, now = body["key"], body["record"], now_ms()
        try:
            validate_record(rec, now, key=key, max_bytes=self.cfg.MAX_RECORD_BYTES, max_ttl_s=300)
        except InvalidRecord as exc:
            self.log.emit("store.reject", level="WARNING", key=short(key), code=exc.code, reason=str(exc))
            return MsgType.STORE_RESPONSE, {"responder": self.node.self_contact().to_wire(), "key": key,
                                            "stored": False, "reason": exc.code}
        stored, reason = self.node.store.put(key, rec, now)
        self.log.emit("store.put" if stored else "store.reject", level="INFO" if stored else "WARNING",
                      key=short(key), owner=short(rec["node_id"]), seq=rec["sequence_number"], result=reason)
        return MsgType.STORE_RESPONSE, {"responder": self.node.self_contact().to_wire(), "key": key,
                                        "stored": stored, "reason": reason}

    async def on_find_value(self, conn, frame, body):
        sender = self._sender(conn, body["sender"])
        key = body["key"]
        rec = self.node.store.get(key, now_ms())
        own = self.node.own_record
        if rec is None and own is not None and key == self.node.own_record_key and own["expires_at"] > now_ms():
            rec = own                                  # владелец — авторитетный источник своей записи
        resp = {"responder": self.node.self_contact().to_wire(), "key": key, "contacts": []}
        if rec is not None:
            resp["record"] = rec
        else:
            resp["contacts"] = self._closest_for(key, sender.node_id)
        return MsgType.FIND_VALUE_RESPONSE, resp

    async def on_echo(self, conn, frame, body):
        return MsgType.ECHO_RESPONSE, {"data": body["data"]}

    # ================================================================ клиентская сторона
    def _check_responder(self, wire: dict, expected: bytes | None, used_addr: tuple[str, int]) -> Contact:
        try:
            c = contact_from_wire(wire, self.node.identity.node_id)
        except InvalidContact as exc:
            raise RpcError("protocol", f"invalid responder contact: {exc}", ErrorCode.INVALID_CONTACT) from None
        if expected is not None and c.node_id != expected:
            raise RpcError("identity", "responder NodeID differs from expected", ErrorCode.IDENTITY_MISMATCH)
        if c.addr != used_addr:
            self.log.emit("contact.addr_differs", level="DEBUG", peer=c.short(), claimed=f"{c.host}:{c.port}",
                          used=f"{used_addr[0]}:{used_addr[1]}")
            c.host, c.port = used_addr                 # достоверен адрес, по которому получен ответ
        return c

    async def _call(self, host, port, mtype, body, expected, timeout=None):
        try:
            return await self.node.transport.send_request(host, port, mtype, body, timeout=timeout,
                                                          expected_node_id=expected)
        except RpcError:
            if expected is not None:
                c = self.node.table.find(expected)
                if c is not None:
                    self.node.routing.record_failure(c, f"{MsgType(mtype).name} failed")
            raise

    async def ping(self, host: str, port: int, expected: bytes | None = None, observe: bool = True) -> PingResult:
        t0 = time.perf_counter()
        sent_ts = now_ms()
        frame, body = await self._call(host, port, MsgType.PING,
                                       {"sender": self.node.self_contact().to_wire(), "timestamp_ms": sent_ts},
                                       expected)
        rtt = (time.perf_counter() - t0) * 1000
        if body["ping_timestamp_ms"] != sent_ts:
            raise RpcError("protocol", "PONG echoes a different timestamp")
        c = self._check_responder(body["responder"], expected, (host, port))
        self.log.emit("rpc.ping", peer=c.short(), rtt_ms=round(rtt, 2), rid=short(frame.request_id))
        if observe:
            self.node.routing.observe(c, verified=True, reason="pong")
        return PingResult(c, rtt)

    async def ping_contact(self, contact: Contact) -> bool:
        """Для проверки LRU: True, если этот NodeID ответил по своему адресу."""
        try:
            await self.ping(contact.host, contact.port, contact.node_id, observe=False)
            return True
        except RpcError:
            return False

    async def find_node(self, contact: Contact, target: bytes) -> list[Contact]:
        frame, body = await self._call(contact.host, contact.port, MsgType.FIND_NODE_REQUEST,
                                       {"sender": self.node.self_contact().to_wire(), "target_node_id": target},
                                       contact.node_id)
        if body["target_node_id"] != target:
            raise RpcError("protocol", "FIND_NODE_RESPONSE for another target")
        responder = self._check_responder(body["responder"], contact.node_id, contact.addr)
        result = self._contacts(body["contacts"], responder)
        self.node.routing.observe(responder, verified=True, reason="find_node response")
        return result

    def _contacts(self, wires: list[dict], responder: Contact) -> list[Contact]:
        if len(wires) > self.cfg.K_BUCKET_SIZE:
            raise RpcError("protocol", f"{len(wires)} contacts > K_BUCKET_SIZE")
        out, seen = [], set()
        for w in wires:
            try:
                c = contact_from_wire(w, self.node.identity.node_id)
            except InvalidContact as exc:
                self.log.emit("contact.reject", level="WARNING", source=responder.short(), reason=str(exc))
                continue
            if c.node_id in seen:
                continue
            seen.add(c.node_id)
            out.append(c)                  # last_seen_ms от удалённой стороны не принимается
        return out

    async def store(self, contact: Contact, key: bytes, record: dict) -> tuple[bool, str]:
        frame, body = await self._call(contact.host, contact.port, MsgType.STORE_REQUEST,
                                       {"sender": self.node.self_contact().to_wire(), "key": key, "record": record},
                                       contact.node_id)
        if body["key"] != key:
            raise RpcError("protocol", "STORE_RESPONSE for another key")
        responder = self._check_responder(body["responder"], contact.node_id, contact.addr)
        self.node.routing.observe(responder, verified=True, reason="store response")
        return body["stored"], body["reason"]

    async def find_value(self, contact: Contact, key: bytes) -> tuple[dict | None, list[Contact]]:
        frame, body = await self._call(contact.host, contact.port, MsgType.FIND_VALUE_REQUEST,
                                       {"sender": self.node.self_contact().to_wire(), "key": key},
                                       contact.node_id)
        if body["key"] != key:
            raise RpcError("protocol", "FIND_VALUE_RESPONSE for another key")
        responder = self._check_responder(body["responder"], contact.node_id, contact.addr)
        self.node.routing.observe(responder, verified=True, reason="find_value response")
        return body.get("record"), self._contacts(body["contacts"], responder)

    async def echo(self, host: str, port: int, data: bytes, expected: bytes | None = None) -> bytes:
        frame, body = await self._call(host, port, MsgType.ECHO_REQUEST, {"data": data}, expected)
        return body["data"]
