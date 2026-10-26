"""Ручной стенд: запуск и остановка сети независимых процессов-узлов (USERGUIDE, демонстрация).

    python scripts/stand.py up --nodes 5 --scheme star     # узлы остаются работать в фоне
    python scripts/stand.py status                         # NodeID, порт, размер таблицы каждого узла
    python scripts/stand.py stop 3                         # штатная остановка узла 3
    python scripts/stand.py kill 3                         # аварийный отказ узла 3 (без закрытия соединений)
    python scripts/stand.py start 3                        # повторный запуск узла 3 с тем же ключом
    python scripts/stand.py down                           # остановить все узлы

Консоль к узлу:  python -m overlay shell --state-dir runs/stand/state/node-01
Каталоги состояния (закрытые ключи) лежат в runs/stand/ и в git не попадают. Повторный up
использует те же ключи, поэтому NodeID узлов сохраняются между запусками.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from overlay.experiment.cluster import SCHEMES, Cluster  # noqa: E402
from overlay.node.control import ControlClient  # noqa: E402

STAND = os.path.join(ROOT, "runs", "stand")
META = os.path.join(STAND, "stand.json")


def load_meta() -> dict | None:
    if not os.path.exists(META):
        return None
    with open(META, encoding="utf-8") as fh:
        return json.load(fh)


def save_meta(meta: dict) -> None:
    with open(META, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1, ensure_ascii=False)


def client(state_dir: str) -> ControlClient | None:
    try:
        c = ControlClient.from_state_dir(state_dir, timeout=5.0)
        c.call("info")
        return c
    except (OSError, ValueError, RuntimeError, ConnectionError):
        return None


def cluster_for(meta: dict) -> Cluster:
    c = Cluster(STAND, meta["nodes"], meta["scheme"], base_port=meta["base_port"], security=meta["security"],
                k=meta["k"])
    for n in meta["members"]:
        if n.get("node_id"):
            c.node_ids[n["node"] - 1] = n["node_id"]
    return c


def up(a) -> int:
    meta = load_meta()
    if meta and any(client(n["state_dir"]) for n in meta["members"]):
        print("стенд уже запущен (python scripts/stand.py status); сначала python scripts/stand.py down")
        return 2
    c = Cluster(STAND, a.nodes, a.scheme, base_port=a.base_port, security=a.security, k=a.k)
    t0 = time.time()
    print(f"запуск {a.nodes} узлов, bootstrap-схема {a.scheme}, защита {a.security}, k={a.k} ...")
    c.start_all()
    meta = {"nodes": a.nodes, "scheme": a.scheme, "base_port": a.base_port, "security": a.security, "k": a.k,
            "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "members": [{"node": i + 1, "node_id": c.node_ids[i], "port": c.port(i), "pid": c.procs[i].pid,
                         "state_dir": c.state_dir(i)} for i in range(a.nodes)]}
    save_meta(meta)
    for cl in c.clients.values():
        cl.close()
    print(f"все узлы присоединились за {time.time() - t0:.1f} с")
    return status(a)


def status(a) -> int:
    meta = load_meta()
    if not meta:
        print("стенд не создавался: python scripts/stand.py up")
        return 1
    print(f"схема {meta['scheme']}, N={meta['nodes']}, k={meta['k']}, защита {meta['security']}")
    print(" узел  NodeID            порт   контактов  записей DHT  состояние")
    for n in meta["members"]:
        c = client(n["state_dir"])
        if c is None:
            print(f"  {n['node']:3d}  {n['node_id'][:16]}  {n['port']}  {'—':>9}  {'—':>11}  не отвечает")
            continue
        info = c.call("info")
        c.close()
        print(f"  {n['node']:3d}  {n['node_id'][:16]}  {n['port']}  {info['routing']['size']:9d}  "
              f"{info['storage']['records']:11d}  работает")
    print("консоль узла: python -m overlay shell --state-dir " +
          os.path.relpath(meta["members"][0]["state_dir"], ROOT).replace(os.sep, "/"))
    return 0


def _member(meta: dict, number: int) -> dict:
    for n in meta["members"]:
        if n["node"] == number:
            return n
    raise SystemExit(f"узла {number} на стенде нет (1..{meta['nodes']})")


def stop_one(n: dict) -> bool:
    c = client(n["state_dir"])
    if c is None:
        return False
    try:
        c.call("shutdown")
    except (ConnectionError, OSError, RuntimeError):
        pass
    c.close()
    return True


def stop(a) -> int:
    meta = load_meta() or {}
    n = _member(meta, a.node)
    print(f"узел {a.node}: " + ("остановлен" if stop_one(n) else "уже не отвечал"))
    return 0


def kill(a) -> int:
    meta = load_meta() or {}
    n = _member(meta, a.node)
    try:
        os.kill(n["pid"], signal.SIGTERM)          # Windows: TerminateProcess — соединения не закрываются штатно
        print(f"узел {a.node} (pid {n['pid']}) аварийно завершён")
    except OSError as exc:
        print(f"узел {a.node}: процесс не найден ({exc})")
    return 0


def start(a) -> int:
    meta = load_meta() or {}
    n = _member(meta, a.node)
    if client(n["state_dir"]):
        print(f"узел {a.node} уже работает")
        return 0
    c = cluster_for(meta)
    c.start_node(a.node - 1, wait=True)
    n["pid"] = c.procs[a.node - 1].pid
    n["node_id"] = c.node_ids[a.node - 1]
    save_meta(meta)
    for cl in c.clients.values():
        cl.close()
    print(f"узел {a.node} запущен снова, NodeID {n['node_id'][:16]}… (тот же ключ)")
    return 0


def down(a) -> int:
    meta = load_meta()
    if not meta:
        print("стенд не создавался")
        return 0
    stopped = sum(stop_one(n) for n in meta["members"])
    print(f"остановлено узлов: {stopped}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("up", help="запустить сеть")
    u.add_argument("--nodes", type=int, default=5)
    u.add_argument("--scheme", default="star", choices=SCHEMES)
    u.add_argument("--k", type=int, default=3)
    u.add_argument("--security", default="tls", choices=["tls", "plain"])
    u.add_argument("--base-port", type=int, default=9101)
    sub.add_parser("status", help="состояние узлов")
    sub.add_parser("down", help="остановить все узлы")
    for name, text in (("stop", "штатно остановить узел"), ("kill", "аварийно завершить узел"),
                       ("start", "снова запустить узел")):
        p = sub.add_parser(name, help=text)
        p.add_argument("node", type=int)
    a = ap.parse_args()
    return {"up": up, "status": status, "down": down, "stop": stop, "kill": kill, "start": start}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
