import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from overlay.config import NodeConfig  # noqa: E402
from overlay.node import Node  # noqa: E402


def run(coro, timeout: float = 90):
    """Запуск асинхронного теста без внешних плагинов."""
    return asyncio.run(asyncio.wait_for(coro, timeout))


def node_config(base, i: int, mode: str = "plain", **over) -> NodeConfig:
    values = dict(NODE_STATE_DIR=os.path.join(str(base), f"node-{i:02d}"), LISTEN_PORT=0, SECURITY_MODE=mode,
                  LOG_LEVEL="DEBUG", READ_TIMEOUT_MS=2000, PING_TIMEOUT_MS=1500, CONNECT_TIMEOUT_MS=1500)
    values.update(over)
    return NodeConfig(**values)


async def start_nodes(base, n: int, mode: str = "plain", scheme: str = "star", join: bool = True, **over):
    nodes = []
    for i in range(n):
        node = Node(node_config(base, i, mode, **over))
        await node.start()
        nodes.append(node)
    if join:
        for i, node in enumerate(nodes):
            if i == 0:
                await node.join([])
                continue
            seed = nodes[0] if scheme == "star" else nodes[i - 1]
            res = await node.join([f"127.0.0.1:{seed.cfg.LISTEN_PORT}"])
            assert res.ok, f"node {i} failed to join"
    return nodes


async def stop_nodes(nodes):
    for n in nodes:
        try:
            await n.stop()
        except Exception:
            pass


@pytest.fixture
def base(tmp_path):
    return tmp_path
