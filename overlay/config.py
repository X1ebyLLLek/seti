"""Конфигурация узла.

Порядок приоритета (каждый следующий источник перекрывает предыдущий):
значения по умолчанию < JSON-файл (--config) < переменные окружения < аргументы
командной строки (--set KEY=VALUE). Имена параметров совпадают с требованиями
(NODE_STATE_DIR, LISTEN_PORT, K_BUCKET_SIZE, ...). Изменение любого параметра
не требует правки исходного кода.
"""
from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field, fields


@dataclass
class NodeConfig:
    NODE_STATE_DIR: str = "./state/node-01"
    LISTEN_HOST: str = "127.0.0.1"
    LISTEN_PORT: int = 9101
    ADVERTISE_HOST: str = ""            # пусто -> LISTEN_HOST (или 127.0.0.1 для 0.0.0.0)
    ADVERTISE_PORT: int = 0             # 0 -> LISTEN_PORT
    BOOTSTRAP_PEERS: list = field(default_factory=list)   # "host:port" или "<node_id_hex>@host:port"
    NODE_ID_BITS: int = 256
    K_BUCKET_SIZE: int = 3
    ALPHA: int = 3
    REPLICATION_FACTOR: int = 3
    STORE_MIN_ACKS: int = 2
    CONNECT_TIMEOUT_MS: int = 3000
    READ_TIMEOUT_MS: int = 5000
    PING_TIMEOUT_MS: int = 5000
    MAX_FRAME_PAYLOAD: int = 65536
    PROTOCOL_VERSION: int = 1
    LOG_LEVEL: str = "INFO"
    LOG_PAYLOADS: bool = False          # расширенная диагностика; закрытые ключи не пишутся никогда
    SECURITY_MODE: str = "tls"          # tls — TLS 1.3 mTLS; plain — без защиты (этапы 1–2)
    RECORD_TTL_S: int = 180             # 120–300 с
    REPUBLISH_INTERVAL_S: int = 90      # <= TTL/2
    REFRESH_INTERVAL_S: int = 60
    JOIN_REFRESH: bool = True           # после self-lookup обновить bucket дальше ближайшего соседа
    CONTACT_MAX_FAILS: int = 2          # подряд неудачных RPC до удаления контакта
    IDLE_CONNECTION_S: int = 120
    MAX_RECORD_BYTES: int = 1024
    MAX_LOOKUP_ITERATIONS: int = 32
    TUNNEL_RELAYS: int = 2              # упрощённый уровень: не менее 2
    TUNNEL_MAX_HOPS: int = 8
    TUNNEL_TTL_S: int = 600
    TUNNEL_PROBE_INTERVAL_MS: int = 1000
    TUNNEL_ACK_TIMEOUT_MS: int = 2000
    TUNNEL_DEAD_AFTER_MISSES: int = 2   # DEGRADED после 1 пропуска, DEAD после N
    TUNNEL_BUILD_TIMEOUT_MS: int = 8000
    TUNNEL_CELL_BYTES: int = 32768
    MESSAGE_MAX_BYTES: int = 16384
    MESSAGE_ACK_TIMEOUT_MS: int = 6000
    MESSAGE_MAX_ATTEMPTS: int = 4
    CONTROL_HOST: str = "127.0.0.1"
    CONTROL_PORT: int = 0               # 0 -> LISTEN_PORT + 1000
    SEED: int = 0                       # seed ГПСЧ для выбора ретрансляторов (0 -> системный)

    # ---------- производные значения ----------
    @property
    def advertise_host(self) -> str:
        if self.ADVERTISE_HOST:
            return self.ADVERTISE_HOST
        return "127.0.0.1" if self.LISTEN_HOST in ("0.0.0.0", "") else self.LISTEN_HOST

    @property
    def advertise_port(self) -> int:
        return self.ADVERTISE_PORT or self.LISTEN_PORT

    @property
    def control_port(self) -> int:
        return self.CONTROL_PORT or self.LISTEN_PORT + 1000

    def validate(self) -> None:
        if self.NODE_ID_BITS != 256:
            raise ValueError("NODE_ID_BITS must be 256")
        if self.K_BUCKET_SIZE < 1 or self.ALPHA < 1:
            raise ValueError("K_BUCKET_SIZE and ALPHA must be >= 1")
        if not 0 < self.MAX_FRAME_PAYLOAD <= 0xFFFFFFFF:
            raise ValueError("bad MAX_FRAME_PAYLOAD")
        if self.SECURITY_MODE not in ("tls", "plain"):
            raise ValueError("SECURITY_MODE must be tls or plain")
        if self.REPUBLISH_INTERVAL_S * 2 > self.RECORD_TTL_S:
            raise ValueError("REPUBLISH_INTERVAL_S must not exceed RECORD_TTL_S / 2")
        if self.STORE_MIN_ACKS > self.REPLICATION_FACTOR:
            raise ValueError("STORE_MIN_ACKS > REPLICATION_FACTOR")
        if self.TUNNEL_RELAYS + 1 > self.TUNNEL_MAX_HOPS:
            raise ValueError("TUNNEL_RELAYS exceeds TUNNEL_MAX_HOPS")
        if self.TUNNEL_CELL_BYTES > 60000:
            raise ValueError("TUNNEL_CELL_BYTES must be <= 60000")

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    # ---------- загрузка ----------
    def apply(self, values: dict, source: str) -> None:
        names = {f.name: f for f in fields(self)}
        for key, raw in values.items():
            key = key.upper()
            if key not in names:
                raise ValueError(f"{source}: unknown parameter {key}")
            setattr(self, key, _coerce(names[key], raw))

    @classmethod
    def load(cls, config_file: str | None = None, overrides: dict | None = None,
             environ: dict | None = None) -> "NodeConfig":
        cfg = cls()
        if config_file:
            with open(config_file, encoding="utf-8") as fh:
                cfg.apply(json.load(fh), config_file)
        env = os.environ if environ is None else environ
        env_values = {f.name: env[f.name] for f in fields(cls) if f.name in env}
        cfg.apply(env_values, "environment")
        if overrides:
            cfg.apply(overrides, "command line")
        cfg.validate()
        return cfg


def _coerce(f, raw):
    default = f.default if f.default is not dataclasses.MISSING else f.default_factory()
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, list):
        if isinstance(raw, list):
            return [str(x) for x in raw]
        return [p.strip() for p in str(raw).split(",") if p.strip()]
    return str(raw)


def parse_peer(spec: str) -> tuple[bytes | None, str, int]:
    """'host:port' или '<node_id_hex>@host:port' -> (node_id | None, host, port)."""
    node_id = None
    if "@" in spec:
        id_hex, spec = spec.split("@", 1)
        node_id = bytes.fromhex(id_hex)
        if len(node_id) != 32:
            raise ValueError("bootstrap node id must be 32 bytes")
    host, _, port = spec.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"bad peer address {spec!r}")
    return node_id, host, int(port)
