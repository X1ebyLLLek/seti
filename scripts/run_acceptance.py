"""Основная команда приёмки (README): создаёт сеть выбранного размера, ждёт готовности,
выполняет приёмочные сценарии и сохраняет результаты в НОВЫЙ каталог.

    python scripts/run_acceptance.py                 # N=15, star, TLS, с автотестами
    python scripts/run_acceptance.py --nodes 12 --scheme ring --no-tests

Результат: results/acceptance-<время>/ — acceptance_report.md, acceptance.json, сырые CSV/JSON,
node_logs.zip (журналы узлов без ключей), control_tls.pcap / control_plain.pcap, junit.xml.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from overlay.experiment.pcap_capture import capture  # noqa: E402
from overlay.experiment.report import acceptance, run_tables  # noqa: E402
from overlay.experiment.runner import git_version, run_battery, write_json  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nodes", type=int, default=15)
    ap.add_argument("--scheme", default="star", choices=["star", "ring", "tree", "multi-seed"])
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--base-port", type=int, default=9101)
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-tests", action="store_true", help="не запускать pytest")
    a = ap.parse_args()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.abspath(a.out or os.path.join(ROOT, "results", f"acceptance-{stamp}"))
    if os.path.exists(out) and os.listdir(out):
        print(f"каталог {out} уже существует и не пуст — укажите новый --out")
        return 2
    os.makedirs(out, exist_ok=True)
    run_dir = os.path.join(ROOT, "runs", f"acceptance-{stamp}")          # состояние узлов (ключи) — вне results
    t0 = time.time()
    print(f"== Приёмка: N={a.nodes}, k={a.k}, схема {a.scheme}, результаты -> {out}")

    junit = None
    if not a.no_tests:
        print("== 1/3 Автоматические тесты (unit, integration, negative)")
        junit = os.path.join(out, "junit.xml")
        with open(os.path.join(out, "tests_output.txt"), "w", encoding="utf-8") as fh:
            rc = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", f"--junitxml={junit}"], cwd=ROOT,
                                stdout=fh, stderr=subprocess.STDOUT).returncode
        print(f"   pytest завершён, код {rc}")

    print("== 2/3 Сеть процессов: сходимость, lookup, отказы, туннели")
    battery = os.path.join(out, "battery")
    summary = run_battery(battery, run_dir, n=a.nodes, scheme=a.scheme, seed=a.seed, k=a.k, full=True,
                          base_port=a.base_port, keep_logs=True, relay_freeze=True,
                          log=lambda m: print("   " + m, flush=True))

    print("== 3/3 Контрольный pcap (TLS) и контроль прибора (plain)")
    pcap_tls = asyncio.run(capture(out, os.path.join(run_dir, "pcap"), "tls"))
    pcap_plain = asyncio.run(capture(out, os.path.join(run_dir, "pcap"), "plain"))

    rows, passed = acceptance(out, battery, junit, pcap_tls, pcap_plain)
    write_json(os.path.join(out, "acceptance.json"), {"passed": passed, "checks": rows, "summary": summary,
                                                      "code_version": git_version(),
                                                      "duration_s": round(time.time() - t0, 1)})
    lines = [f"# Приёмочный прогон {stamp}", "",
             f"Версия кода: `{git_version()}`; N={a.nodes}, K_BUCKET_SIZE={a.k}, ALPHA=3, R=3, "
             f"bootstrap: {a.scheme}, защита каналов: TLS 1.3 (mTLS), туннель: 2 ретранслятора.", "",
             f"**Итог: {'ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ' if passed else 'ЕСТЬ НЕПРОЙДЕННЫЕ ПРОВЕРКИ'}** "
             f"(время прогона {time.time() - t0:.0f} с)", "",
             "| Область | Проверка | Результат | Основание |", "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['area']} | {r['check']} | {'✅' if r['ok'] else '❌'} | {r['evidence']} |")
    lines += ["", "## Измерения этого прогона", "", run_tables(battery), "",
              "Сырые данные: каталог `battery/` (CSV/JSON), журналы узлов: `battery/node_logs.zip`, "
              "pcap: `control_tls.pcap` (контроль конфиденциальности) и `control_plain.pcap` "
              "(положительный контроль прибора: в нём служебные поля протокола видны открыто)."]
    with open(os.path.join(out, "acceptance_report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines[:len(rows) + 8]))
    print(f"\nОтчёт: {os.path.join(out, 'acceptance_report.md')}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
