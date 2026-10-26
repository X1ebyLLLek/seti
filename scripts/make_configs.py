"""Генерация примеров конфигураций узлов в configs/ (по одному JSON на узел).

    python scripts/make_configs.py            # configs/example-5-star, n15-star, n15-ring

Те же параметры оркестратор (overlay/experiment/cluster.py) записывает в каждый прогон
(configs/ внутри node_logs.zip). Узел из примера запускается вручную:
    python -m overlay run --config configs/example-5-star/node-01.json
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from overlay.experiment.cluster import bootstrap_targets  # noqa: E402

VARIANTS = {"example-5-star": (5, "star"), "n15-star": (15, "star"), "n15-ring": (15, "ring")}
BASE_PORT = 9101


def node_config(name: str, i: int, scheme: str) -> dict:
    return {"NODE_STATE_DIR": f"state/{name}/node-{i + 1:02d}", "LISTEN_HOST": "127.0.0.1",
            "LISTEN_PORT": BASE_PORT + i, "CONTROL_PORT": BASE_PORT + 1000 + i,
            "BOOTSTRAP_PEERS": [f"127.0.0.1:{BASE_PORT + j}" for j in bootstrap_targets(scheme, i)],
            "NODE_ID_BITS": 256, "K_BUCKET_SIZE": 3, "ALPHA": 3, "REPLICATION_FACTOR": 3,
            "CONNECT_TIMEOUT_MS": 3000, "READ_TIMEOUT_MS": 5000, "PING_TIMEOUT_MS": 5000,
            "MAX_FRAME_PAYLOAD": 65536, "PROTOCOL_VERSION": 1, "LOG_LEVEL": "INFO", "SECURITY_MODE": "tls",
            "RECORD_TTL_S": 180, "REPUBLISH_INTERVAL_S": 90, "REFRESH_INTERVAL_S": 60, "TUNNEL_RELAYS": 2}


def main() -> int:
    for name, (n, scheme) in VARIANTS.items():
        d = os.path.join(ROOT, "configs", name)
        os.makedirs(d, exist_ok=True)
        for i in range(n):
            with open(os.path.join(d, f"node-{i + 1:02d}.json"), "w", encoding="utf-8", newline="\n") as fh:
                json.dump(node_config(name, i, scheme), fh, indent=1)
                fh.write("\n")
        print(f"configs/{name}: {n} узлов, схема {scheme}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
