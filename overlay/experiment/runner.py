"""Прогон серии испытаний на кластере процессов и сохранение сырых данных."""
from __future__ import annotations

import csv
import json
import os
import random
import shutil
import subprocess
import time
import zipfile

from ..config import NodeConfig
from . import scenarios as sc
from .cluster import ROOT, Cluster

LOG_FILES = ("logs/node.log", "logs/events.jsonl", "lookups.jsonl", "routing_table.json", "bootstrap.json",
             "messages.jsonl", "effective_config.json", "stdout.log")


def git_version() -> str:
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                             timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "overlay"], cwd=ROOT, capture_output=True,
                               text=True, timeout=10).stdout.strip()
        return (rev or "unknown") + ("-dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def write_csv(path: str, rows: list[dict]) -> None:
    if not rows:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("")
        return
    keys = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def write_json(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, ensure_ascii=False, default=str)


def run_meta(cluster: Cluster, seed: int, started: float, extra: dict) -> dict:
    defaults = NodeConfig()
    cfg = cluster.config(1)
    return {
        "code_version": git_version(), "seed": seed, "start_time_unix": started,
        "start_time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "time_base": "wall clock time.time() одного хоста (все процессы на одной машине); длительности — perf_counter",
        "N": cluster.n, "K_BUCKET_SIZE": cluster.k, "ALPHA": cluster.alpha,
        "REPLICATION_FACTOR": cfg.get("REPLICATION_FACTOR", defaults.REPLICATION_FACTOR),
        "SECURITY_MODE": cluster.security, "bootstrap_scheme": cluster.scheme,
        "start_delay_s": cluster.start_delay_s,
        "timeouts_ms": {k: cfg.get(k, getattr(defaults, k)) for k in
                        ("CONNECT_TIMEOUT_MS", "READ_TIMEOUT_MS", "PING_TIMEOUT_MS", "TUNNEL_ACK_TIMEOUT_MS",
                         "TUNNEL_BUILD_TIMEOUT_MS", "MESSAGE_ACK_TIMEOUT_MS")},
        "record_ttl_s": cfg.get("RECORD_TTL_S", defaults.RECORD_TTL_S),
        "republish_interval_s": cfg.get("REPUBLISH_INTERVAL_S", defaults.REPUBLISH_INTERVAL_S),
        "refresh_interval_s": cfg.get("REFRESH_INTERVAL_S", defaults.REFRESH_INTERVAL_S),
        "tunnel_relays": cfg.get("TUNNEL_RELAYS", defaults.TUNNEL_RELAYS),
        "netem": "не применялся (упрощённый уровень; underlay — loopback одного хоста)",
        "underlay": "все процессы на 127.0.0.1, полная IP-достижимость; star/ring — схемы bootstrap, не топологии",
        **extra,
    }


def collect_logs(cluster: Cluster, out_dir: str) -> str:
    """Журналы узлов (без закрытых ключей) в архив node_logs.zip."""
    path = os.path.join(out_dir, "node_logs.zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(cluster.n):
            sd = cluster.state_dir(i)
            for rel in LOG_FILES:
                src = os.path.join(sd, rel)
                if os.path.exists(src):
                    zf.write(src, f"node-{i + 1:02d}/{rel}")
        for name in os.listdir(os.path.join(cluster.run_dir, "configs")):
            zf.write(os.path.join(cluster.run_dir, "configs", name), f"configs/{name}")
    for name in zipfile.ZipFile(path).namelist():
        assert not name.endswith(".pem"), "закрытые ключи не должны попадать в результаты"
    return path


def run_battery(out_dir: str, run_dir: str, n: int = 15, scheme: str = "star", seed: int = 1,
                security: str = "tls", k: int = 3, full: bool = True, base_port: int = 9101,
                lookups: int = 30, keep_logs: bool = False, extra_cfg: dict | None = None,
                relay_freeze: bool = False, log=print) -> dict:
    """Один прогон: холодный старт, сходимость, контрольные lookup и (full) все
    сценарии отказов. Возвращает summary и пишет сырые данные в out_dir."""
    os.makedirs(out_dir, exist_ok=True)
    rnd = random.Random(seed)
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir, ignore_errors=True)
    summary: dict = {"scheme": scheme, "seed": seed, "n": n, "k": k, "full": full}
    with Cluster(run_dir, n, scheme, base_port=base_port, security=security, k=k, seed=seed,
                 extra=extra_cfg) as cluster:
        started = time.time()
        log(f"[{scheme} seed={seed}] cold start of {n} nodes")
        timing = cluster.start_all()
        summary["join_all_s"] = round(timing["join_s"], 3)
        write_json(os.path.join(out_dir, "run_meta.json"), run_meta(cluster, seed, started,
                                                                    {"cluster_join_s": timing["join_s"]}))
        write_json(os.path.join(out_dir, "bootstrap_graph.json"), cluster.bootstrap_graph())
        log(f"[{scheme} seed={seed}] all joined in {timing['join_s']:.1f}s; convergence rounds")
        rounds, conv = sc.convergence(cluster, started, rnd)
        write_csv(os.path.join(out_dir, "convergence_rounds.csv"), rounds)
        summary["convergence"] = conv
        snap = sc.tables_snapshot(cluster)
        write_json(os.path.join(out_dir, "tables.json"), snap)
        write_csv(os.path.join(out_dir, "table_sizes.csv"),
                  [{"node": k_, "size": v["size"], "buckets": json.dumps(v["buckets"])} for k_, v in snap.items()])
        log(f"[{scheme} seed={seed}] converged={conv['converged']} in {conv['convergence_s']}s; control lookups")
        lk = sc.control_lookups(cluster, rnd, lookups)
        write_csv(os.path.join(out_dir, "lookups.csv"), lk)
        summary["lookups"] = {"count": len(lk), "success": sum(r["success"] for r in lk)}
        after_bs: list[dict] = []
        if full:
            log(f"[{scheme} seed={seed}] storage failure injections")
            st = sc.storage_failure(cluster, rnd, 5)
            write_csv(os.path.join(out_dir, "storage_failure.csv"), st)
            log(f"[{scheme} seed={seed}] intermediate failure injections")
            im = sc.intermediate_failure(cluster, rnd, 5)
            write_csv(os.path.join(out_dir, "intermediate_failure.csv"), im)
            log(f"[{scheme} seed={seed}] messaging")
            ms = sc.messaging(cluster, rnd, 10, 30)
            write_csv(os.path.join(out_dir, "messages.csv"), ms)
            log(f"[{scheme} seed={seed}] relay failure injections")
            rf = sc.relay_failure(cluster, rnd, 5)
            write_csv(os.path.join(out_dir, "relay_failure.csv"), rf)
            if relay_freeze:
                log(f"[{scheme} seed={seed}] hung relay (freeze) injections")
                rz = sc.relay_failure(cluster, rnd, 5, fault="freeze")
                write_csv(os.path.join(out_dir, "relay_freeze.csv"), rz)
                summary["relay_freeze"] = {"injections": len(rz),
                                           "recovered": sum(r["delivery_recovery_ms"] is not None for r in rz)}
            log(f"[{scheme} seed={seed}] bootstrap shutdown")
            after_bs = sc.bootstrap_shutdown(cluster, rnd, 10)
            write_csv(os.path.join(out_dir, "bootstrap_shutdown.csv"), after_bs)
            summary.update({
                "storage_failure": {"injections": len(st), "success_after": sum(r["success_after"] for r in st)},
                "intermediate_failure": {"injections": len(im), "success": sum(r["success"] for r in im)},
                "messages": {"count": len(ms), "delivered": sum(r["status"] == "delivered" for r in ms),
                             "min_relays": min((r["relays"] for r in ms if r["relays"] is not None), default=None)},
                "relay_failure": {"injections": len(rf),
                                  "recovered": sum(r["delivery_recovery_ms"] is not None for r in rf),
                                  "new_route_excludes_failed": sum(r["new_route_excludes_failed"] for r in rf)},
                "bootstrap_shutdown": {"count": len(after_bs), "success": sum(r["success"] for r in after_bs)},
            })
            nd = sc.non_degeneracy(snap, lk, after_bs, n)
            write_json(os.path.join(out_dir, "non_degeneracy.json"), nd)
            summary["non_degeneracy"] = nd
        write_json(os.path.join(out_dir, "cluster_events.json"), cluster.events)
        if keep_logs:
            for i in cluster.alive_nodes():
                try:
                    cluster.call(i, "save")
                except Exception:
                    pass
            cluster.stop_all()
            summary["logs"] = os.path.basename(collect_logs(cluster, out_dir))
    write_json(os.path.join(out_dir, "summary.json"), summary)
    return summary
