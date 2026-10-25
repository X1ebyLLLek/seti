"""Тестовый оркестратор: запуск N независимых процессов-узлов, bootstrap-схемы, инъекция отказов.

Оркестратор знает все NodeID (читает control.json узлов), но рабочим узлам эта
информация не передаётся: узел получает только адрес(а) своего bootstrap-контакта
и — в контрольных испытаниях — NodeID цели конкретного запроса.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from ..node.control import ControlClient

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCHEMES = ("star", "ring", "tree", "multi-seed")


def bootstrap_targets(scheme: str, i: int) -> list[int]:
    """Индексы узлов, адреса которых получает узел i при присоединении (bootstrap-граф)."""
    if i == 0:
        return []
    if scheme == "star":
        return [0]
    if scheme == "ring":
        return [i - 1]
    if scheme == "tree":
        return [(i - 1) // 2]
    if scheme == "multi-seed":
        seeds = [0, 1, 2]
        return [s for s in seeds if s < i] if i < 3 else seeds
    raise ValueError(f"unknown scheme {scheme}")


class Cluster:
    def __init__(self, run_dir: str, n: int, scheme: str = "star", base_port: int = 9101, security: str = "tls",
                 k: int = 3, alpha: int = 3, start_delay_s: float = 0.3, seed: int = 1, extra: dict | None = None,
                 log_level: str = "INFO"):
        if scheme not in SCHEMES:
            raise ValueError(f"scheme must be one of {SCHEMES}")
        self.run_dir = os.path.abspath(run_dir)
        self.n, self.scheme, self.base_port = n, scheme, base_port
        self.security, self.k, self.alpha = security, k, alpha
        self.start_delay_s, self.seed = start_delay_s, seed
        self.extra = extra or {}
        self.log_level = log_level
        self.procs: dict[int, subprocess.Popen] = {}
        self.clients: dict[int, ControlClient] = {}
        self.node_ids: dict[int, str] = {}
        self.start_times: dict[int, float] = {}
        self.ready_times: dict[int, float] = {}
        self.events: list[dict] = []
        os.makedirs(os.path.join(self.run_dir, "configs"), exist_ok=True)
        os.makedirs(os.path.join(self.run_dir, "state"), exist_ok=True)

    # ------------------------------------------------------------------ конфигурация
    def state_dir(self, i: int) -> str:
        return os.path.join(self.run_dir, "state", f"node-{i + 1:02d}")

    def port(self, i: int) -> int:
        return self.base_port + i

    def config(self, i: int) -> dict:
        peers = []
        for j in bootstrap_targets(self.scheme, i):
            nid = self.node_ids.get(j)
            addr = f"127.0.0.1:{self.port(j)}"
            peers.append(f"{nid}@{addr}" if nid else addr)
        cfg = {"NODE_STATE_DIR": self.state_dir(i), "LISTEN_HOST": "127.0.0.1", "LISTEN_PORT": self.port(i),
               "BOOTSTRAP_PEERS": peers, "K_BUCKET_SIZE": self.k, "ALPHA": self.alpha,
               "SECURITY_MODE": self.security, "LOG_LEVEL": self.log_level, "CONTROL_PORT": self.port(i) + 1000,
               "SEED": self.seed * 1000 + i + 1}
        cfg.update(self.extra)
        return cfg

    def bootstrap_graph(self) -> list[dict]:
        return [{"node": i + 1, "bootstrap": [j + 1 for j in bootstrap_targets(self.scheme, i)]} for i in range(self.n)]

    # ------------------------------------------------------------------ процессы
    def start_node(self, i: int, wait: bool = True, timeout: float = 30.0) -> None:
        cfg = self.config(i)
        path = os.path.join(self.run_dir, "configs", f"node-{i + 1:02d}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=1)
        os.makedirs(self.state_dir(i), exist_ok=True)
        ctl = os.path.join(self.state_dir(i), "control.json")
        if os.path.exists(ctl):
            os.remove(ctl)
        out = open(os.path.join(self.state_dir(i), "stdout.log"), "ab")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        self.procs[i] = subprocess.Popen([sys.executable, "-m", "overlay", "run", "--config", path], cwd=ROOT,
                                         stdout=out, stderr=subprocess.STDOUT, env=env)
        self.start_times[i] = time.time()
        self.event("start", i)
        if wait:
            self.wait_ready(i, timeout)

    def wait_ready(self, i: int, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        ctl = os.path.join(self.state_dir(i), "control.json")
        while time.time() < deadline:
            if self.procs[i].poll() is not None:
                raise RuntimeError(f"node {i + 1} exited with code {self.procs[i].returncode}")
            if os.path.exists(ctl):
                try:
                    c = ControlClient.from_state_dir(self.state_dir(i))
                    info = c.call("info")
                    if info["joined"]:
                        self.clients[i] = c
                        self.node_ids[i] = info["node_id"]
                        self.ready_times[i] = time.time()
                        return
                    c.close()
                except (OSError, ValueError, RuntimeError, ConnectionError):
                    pass
            time.sleep(0.1)
        raise TimeoutError(f"node {i + 1} not ready in {timeout}s")

    def start_all(self) -> dict:
        t0 = time.time()
        self.event("cluster_start", -1, scheme=self.scheme, n=self.n)
        for i in range(self.n):
            self.start_node(i, wait=True)
            if i + 1 < self.n:
                time.sleep(self.start_delay_s)
        return {"t0": t0, "all_joined": time.time(), "join_s": time.time() - t0}

    def call(self, i: int, cmd: str, **args):
        c = self.clients.get(i)
        if c is None:
            c = self.clients[i] = ControlClient.from_state_dir(self.state_dir(i))
        try:
            return c.call(cmd, **args)
        except (ConnectionError, OSError):
            self.clients.pop(i, None)
            raise

    def alive(self, i: int) -> bool:
        p = self.procs.get(i)
        return p is not None and p.poll() is None

    def alive_nodes(self) -> list[int]:
        return [i for i in range(self.n) if self.alive(i)]

    def kill(self, i: int) -> float:
        """Аварийный отказ: процесс завершается без закрытия соединений протокола."""
        c = self.clients.pop(i, None)
        if c:
            c.close()
        t = time.time()
        self.procs[i].kill()
        self.procs[i].wait(10)
        self.event("kill", i)
        return t

    def stop(self, i: int) -> None:
        c = self.clients.pop(i, None)
        try:
            if c:
                c.call("shutdown")
                c.close()
            self.procs[i].wait(10)
        except Exception:
            self.procs[i].kill()
            self.procs[i].wait(10)
        self.event("stop", i)

    def restart(self, i: int) -> None:
        self.start_node(i, wait=True)
        self.event("restart", i)

    def stop_all(self) -> None:
        for i in list(self.procs):
            if self.alive(i):
                self.stop(i)
        for c in self.clients.values():
            c.close()
        self.clients.clear()

    def event(self, kind: str, i: int, **extra) -> None:
        self.events.append({"ts": time.time(), "event": kind, "node": i + 1 if i >= 0 else None, **extra})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop_all()
        for p in self.procs.values():
            if p.poll() is None:
                p.kill()
