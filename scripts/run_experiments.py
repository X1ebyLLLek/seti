"""Экспериментальная серия для пояснительной записки.

    python scripts/run_experiments.py                     # N=15, star и ring, по 5 холодных запусков
    python scripts/run_experiments.py --runs 2 --schemes star

Исследуемый фактор — схема bootstrap; остальные факторы (N, k, ALPHA, R, тайм-ауты,
seed оркестратора для запуска с одинаковым номером) неизменны. Каждый запуск —
холодный старт новой сети с новыми ключами. В первом запуске каждой схемы
дополнительно выполняется полная батарея отказов (хранитель, промежуточный узел,
ретранслятор kill/freeze, отключение bootstrap) и измерения сообщений.
Сырые данные: results/experiments-<время>/<схема>/run-XX/*.csv|json;
затем: python scripts/analyze.py results/experiments-<время>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from overlay.experiment.runner import git_version, run_battery, write_json  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nodes", type=int, default=15)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--schemes", default="star,ring")
    ap.add_argument("--lookups", type=int, default=30)
    ap.add_argument("--base-port", type=int, default=9101)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.abspath(a.out or os.path.join(ROOT, "results", f"experiments-{stamp}"))
    os.makedirs(out, exist_ok=True)
    schemes = [s.strip() for s in a.schemes.split(",") if s.strip()]
    plan = {"code_version": git_version(), "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "nodes": a.nodes, "k": a.k, "alpha": 3, "replication": 3, "runs_per_scheme": a.runs,
            "schemes": schemes, "lookups_per_run": a.lookups, "factor": "bootstrap scheme",
            "full_battery": "run 1 of each scheme"}
    write_json(os.path.join(out, "plan.json"), plan)
    t0 = time.time()
    summaries = []
    for r in range(1, a.runs + 1):                 # схемы чередуются: дрейф условий хоста не копится в одной
        for scheme in schemes:
            run_out = os.path.join(out, scheme, f"run-{r:02d}")
            run_dir = os.path.join(ROOT, "runs", f"experiments-{stamp}", scheme, f"run-{r:02d}")
            print(f"== {scheme} run {r}/{a.runs} ({time.time() - t0:.0f} s elapsed)", flush=True)
            s = run_battery(run_out, run_dir, n=a.nodes, scheme=scheme, seed=r, k=a.k, full=(r == 1),
                            base_port=a.base_port, lookups=a.lookups, keep_logs=(r == 1), relay_freeze=(r == 1),
                            log=lambda m: print("   " + m, flush=True))
            summaries.append({"scheme": scheme, "run": r, **s})
            write_json(os.path.join(out, "summaries.json"), summaries)
    plan["duration_s"] = round(time.time() - t0, 1)
    write_json(os.path.join(out, "plan.json"), plan)
    print(json.dumps([{k: v for k, v in s.items() if k in ("scheme", "run", "convergence", "lookups")}
                      for s in summaries], ensure_ascii=False, indent=1))
    print(f"Готово за {time.time() - t0:.0f} с. Анализ: python scripts/analyze.py {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
