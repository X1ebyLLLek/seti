"""Локальный интерфейс управления узлом (JSON Lines поверх TCP, только 127.0.0.1).

Нужен консольному интерфейсу и тестовому оркестратору. Это не часть оверлея:
команды принимаются только с токеном из NODE_STATE_DIR/control.json, который
доступен лишь владельцу каталога состояния. Рабочий узел через этот интерфейс не
получает никаких сведений о других узлах — оркестратор может знать все NodeID,
но передаёт узлу только цель конкретного контрольного запроса.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets

from ..dht import record_key
from ..dht.records import describe
from ..metrics import now_ms
from ..protocol.codec import DecodeError
from ..transport import RpcError
from ..tunnel import TunnelError


def _id(value: str) -> bytes:
    b = bytes.fromhex(value)
    if len(b) != 32:
        raise ValueError("node id must be 64 hex characters")
    return b


class ControlServer:
    def __init__(self, node, on_shutdown=None):
        self.node = node
        self.token = secrets.token_hex(16)
        self.server = None
        self.on_shutdown = on_shutdown
        self.commands = {
            "info": self.c_info, "table": self.c_table, "lookup": self.c_lookup, "get_record": self.c_get_record,
            "publish": self.c_publish, "storage": self.c_storage, "send": self.c_send, "tunnels": self.c_tunnels,
            "build_tunnel": self.c_build_tunnel, "close_tunnel": self.c_close_tunnel, "inbox": self.c_inbox,
            "history": self.c_history, "freeze": self.c_freeze, "refresh": self.c_refresh, "join": self.c_join,
            "save": self.c_save, "shutdown": self.c_shutdown, "ping": self.c_ping,
        }

    async def start(self) -> None:
        cfg = self.node.cfg
        self.server = await asyncio.start_server(self._client, cfg.CONTROL_HOST, cfg.control_port)
        port = self.server.sockets[0].getsockname()[1]
        info = {"host": cfg.CONTROL_HOST, "port": port, "token": self.token,
                "node_id": self.node.identity.node_id.hex(), "listen_port": cfg.LISTEN_PORT, "pid": os.getpid()}
        path = os.path.join(cfg.NODE_STATE_DIR, "control.json")
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(info, fh)
        os.replace(path + ".tmp", path)

    async def stop(self) -> None:
        if self.server:
            self.server.close()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                if len(line) > 65536:
                    break
                resp = await self.handle(line)
                writer.write((json.dumps(resp, ensure_ascii=False, default=_jsonable) + "\n").encode("utf-8"))
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def handle(self, line: bytes) -> dict:
        try:
            req = json.loads(line)
            if not hmac.compare_digest(str(req.get("token", "")), self.token):
                return {"ok": False, "error": "bad token"}
            cmd = self.commands.get(req.get("cmd"))
            if cmd is None:
                return {"ok": False, "error": f"unknown command {req.get('cmd')!r}"}
            return {"ok": True, "result": await cmd(**(req.get("args") or {}))}
        except (ValueError, TypeError, KeyError, RpcError, TunnelError, DecodeError) as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # ------------------------------------------------------------------ команды
    async def c_info(self):
        return self.node.get_network_state()

    async def c_table(self):
        return self.node.table.export()

    async def c_lookup(self, target: str, purpose: str = "control"):
        return (await self.node.lookup_node(_id(target), purpose)).to_dict()

    async def c_get_record(self, node_id: str, purpose: str = "control"):
        res = await self.node.get_record(record_key(_id(node_id)), purpose)
        out = res.to_dict()
        out["record"] = describe(res.record) if res.record else None
        return out

    async def c_publish(self):
        return (await self.node.publish_self()).to_dict()

    async def c_storage(self):
        return self.node.store.export()

    async def c_send(self, to: str, text: str, keepalive: bool = True):
        return await self.node.messaging.send(_id(to), text, keepalive=keepalive)

    async def c_tunnels(self):
        m = self.node.messaging
        return {"active": self.node.tunnels.summary(), "history": self.node.tunnels.history[-50:],
                "recoveries": m.recoveries[-50:] if m else []}

    async def c_build_tunnel(self, to: str, relays: int | None = None):
        cons = {"relays": relays} if relays else {}
        tun = await self.node.build_tunnel(_id(to), cons)
        return tun.describe(self.node.identity.node_id)

    async def c_close_tunnel(self, tunnel_id: str):
        await self.node.close_tunnel(bytes.fromhex(tunnel_id))
        return "closed"

    async def c_inbox(self, limit: int = 20):
        return self.node.messaging.inbox[-limit:]

    async def c_history(self, limit: int = 50):
        path = self.node.messaging.history_path
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()[-limit:]
        return [json.loads(x) for x in lines]

    async def c_freeze(self, seconds: float):
        self.node.transport.freeze(float(seconds))
        return {"frozen_until_ms": now_ms() + int(float(seconds) * 1000)}

    async def c_refresh(self):
        return {"lookups": await self.node.refresh_buckets(force=True)}

    async def c_join(self, peers: list | None = None):
        return (await self.node.join(peers)).to_dict()

    async def c_save(self):
        self.node.save_state()
        return "saved"

    async def c_ping(self, host: str, port: int):
        r = await self.node.rpc.ping(host, int(port))
        return {"node_id": r.contact.node_id.hex(), "rtt_ms": r.rtt_ms}

    async def c_shutdown(self):
        if self.on_shutdown:
            asyncio.get_event_loop().call_later(0.1, self.on_shutdown)
        return "bye"


def _jsonable(obj):
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, set):
        return list(obj)
    return str(obj)


# ---------------------------------------------------------------------- клиент
class ControlClient:
    """Синхронный клиент для скриптов и консоли: ControlClient.from_state_dir(path).call('info')."""

    def __init__(self, host: str, port: int, token: str, timeout: float = 120.0):
        self.host, self.port, self.token, self.timeout = host, port, token, timeout
        self._sock = None
        self._file = None

    @classmethod
    def from_state_dir(cls, state_dir: str, timeout: float = 120.0) -> "ControlClient":
        with open(os.path.join(state_dir, "control.json"), encoding="utf-8") as fh:
            info = json.load(fh)
        return cls(info["host"], info["port"], info["token"], timeout)

    def _connect(self):
        import socket
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._file = self._sock.makefile("rwb")

    def call(self, cmd: str, **args):
        if self._sock is None:
            self._connect()
        req = json.dumps({"token": self.token, "cmd": cmd, "args": args}) + "\n"
        self._file.write(req.encode("utf-8"))
        self._file.flush()
        line = self._file.readline()
        if not line:
            self.close()
            raise ConnectionError("control connection closed")
        resp = json.loads(line)
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error"))
        return resp["result"]

    def close(self):
        try:
            if self._file:
                self._file.close()
            if self._sock:
                self._sock.close()
        finally:
            self._sock = self._file = None
