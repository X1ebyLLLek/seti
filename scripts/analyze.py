"""Обработка сырых данных серии: таблицы статистики и графики строятся ТОЛЬКО из CSV/JSON.

    python scripts/analyze.py results/experiments-<время>

Результат: <серия>/analysis/ — tables.md, stats.csv, figures/*.png.
Статистика: n, min, max, медиана, среднее; p95 только при n >= 20 (ТЗ, раздел 15.3).
"""
from __future__ import annotations

import csv
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from overlay.experiment.report import read_csv, read_json, truthy  # noqa: E402
from overlay.experiment.stats import HEADER, describe, md_row  # noqa: E402

# Палитра: категориальные слоты 1–3 (проверены на различимость, в т.ч. при дальтонизме),
# текст — нейтральными чернилами, сетка — приглушённая.
SERIES = {"star": "#2a78d6", "ring": "#eb6834", "tree": "#1baf7a", "multi-seed": "#6250d6"}
FAULT = {"kill": "#2a78d6", "freeze": "#eb6834"}
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e6e5e0", "#ffffff"
SCHEME_RU = {"star": "star", "ring": "ring", "tree": "tree", "multi-seed": "multi-seed"}


def style():
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
        "text.color": INK, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
        "axes.spines.top": False, "axes.spines.right": False, "font.size": 10, "axes.titlesize": 11,
        "axes.titleweight": "bold", "legend.frameon": False, "figure.dpi": 110, "savefig.dpi": 160,
        "lines.linewidth": 2,
    })


def runs_of(series: str, scheme: str) -> list[str]:
    return sorted(glob.glob(os.path.join(series, scheme, "run-*")))


def concat(series: str, scheme: str, name: str) -> list[dict]:
    rows = []
    for run in runs_of(series, scheme):
        for r in read_csv(os.path.join(run, name)):
            r["run"] = os.path.basename(run)
            rows.append(r)
    return rows


def save(fig, path):
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------------------ графики
def fig_convergence(series, schemes, figdir):
    fig, ax = plt.subplots(figsize=(6.4, 3.2))
    for y, sch in enumerate(schemes):
        vals = [read_json(os.path.join(r, "summary.json"), {}).get("convergence", {}).get("convergence_s")
                for r in runs_of(series, sch)]
        vals = [v for v in vals if v is not None]
        ax.scatter(vals, [y] * len(vals), s=64, color=SERIES[sch], edgecolor=SURFACE, linewidth=2, zorder=3,
                   label=f"{sch} (n={len(vals)})")
        if vals:
            med = describe(vals)["median"]
            ax.plot([med, med], [y - 0.25, y + 0.25], color=INK, linewidth=2, zorder=4)
            ax.annotate(f"медиана {med:.1f} с", (med, y + 0.3), color=INK2, fontsize=9, ha="center")
    ax.set_yticks(range(len(schemes)), schemes)
    ax.set_ylim(-0.7, len(schemes) - 0.3)
    ax.set_xlabel("время холодной сходимости от старта первого узла, с")
    ax.set_title("Холодная сходимость по схемам bootstrap")
    ax.legend(loc="lower right", fontsize=9)
    save(fig, os.path.join(figdir, "convergence.png"))


def fig_lookup(series, schemes, figdir):
    fig, axes = plt.subplots(1, 2, figsize=(9.6, 3.4))
    width = 0.8 / len(schemes)
    allr = {s: concat(series, s, "lookups.csv") for s in schemes}
    maxrpc = max((int(r["rpc_count"]) for rows in allr.values() for r in rows), default=1)
    xs = list(range(1, maxrpc + 1))
    for k, sch in enumerate(schemes):
        rows = allr[sch]
        counts = [sum(int(r["rpc_count"]) == x for r in rows) / max(len(rows), 1) * 100 for x in xs]
        axes[0].bar([x + (k - (len(schemes) - 1) / 2) * width for x in xs], counts, width=width * 0.92,
                    color=SERIES[sch], label=f"{sch} (n={len(rows)})")
        d = sorted(float(r["duration_ms"]) for r in rows)
        axes[1].step(d, [(i + 1) / len(d) * 100 for i in range(len(d))], where="post", color=SERIES[sch],
                     label=f"{sch} (n={len(d)})")
    axes[0].set_xlabel("число RPC на один lookup")
    axes[0].set_ylabel("доля поисков, %")
    axes[0].set_title("Стоимость поиска узла (FIND_NODE)")
    axes[0].set_xticks(xs)
    axes[0].legend(fontsize=9)
    axes[1].set_xlabel("длительность lookup, мс")
    axes[1].set_ylabel("накопленная доля, %")
    axes[1].set_title("Распределение длительности lookup")
    axes[1].legend(fontsize=9, loc="lower right")
    save(fig, os.path.join(figdir, "lookup_cost.png"))


def fig_tables(series, schemes, n, k, figdir):
    fig, axes = plt.subplots(1, 2, figsize=(9.6, 3.4))
    width = 0.8 / len(schemes)
    for j, sch in enumerate(schemes):
        sizes, fills = [], {}
        for run in runs_of(series, sch):
            snap = read_json(os.path.join(run, "tables.json"), {})
            for v in snap.values():
                sizes.append(v["size"])
                for b, f in v["buckets"].items():
                    fills.setdefault(int(b), []).append(f)
        hist = list(range(min(sizes, default=0), max(sizes, default=0) + 1))
        axes[0].bar([h + (j - (len(schemes) - 1) / 2) * width for h in hist],
                    [sizes.count(h) / max(len(sizes), 1) * 100 for h in hist], width=width * 0.92,
                    color=SERIES[sch], label=f"{sch} (узлов·запусков={len(sizes)})")
        idx = sorted(fills, reverse=True)[:8]
        nodes_total = max(len(sizes), 1)
        axes[1].bar([255 - i + (j - (len(schemes) - 1) / 2) * width for i in idx],
                    [sum(fills[i]) / nodes_total for i in idx], width=width * 0.92, color=SERIES[sch],
                    label=sch)
    axes[0].axvline(n - 1, color=INK2, linestyle="--", linewidth=1)
    axes[0].annotate(f"N−1 = {n - 1}", (n - 1, axes[0].get_ylim()[1] * 0.9), color=INK2, fontsize=9,
                     ha="right", xytext=(-4, 0), textcoords="offset points")
    axes[0].set_xlabel("уникальных контактов в таблице узла")
    axes[0].set_ylabel("доля узлов, %")
    axes[0].set_title("Размер таблиц после сходимости")
    axes[0].legend(fontsize=8, loc="upper left")
    axes[1].axhline(k, color=INK2, linestyle="--", linewidth=1)
    axes[1].annotate(f"k = {k}", (0.02, k), xycoords=("axes fraction", "data"), color=INK2, fontsize=9,
                     va="bottom")
    axes[1].set_xlabel("номер bucket от старшего: 0 → bucket 255, 1 → 254, …")
    axes[1].set_ylabel("среднее заполнение bucket")
    axes[1].set_title("Заполнение k-bucket")
    axes[1].legend(fontsize=9)
    save(fig, os.path.join(figdir, "routing_tables.png"))


def fig_messages(series, schemes, figdir):
    fig, axes = plt.subplots(1, 2, figsize=(9.6, 3.3))
    for col, (kind, field, title) in enumerate([
            ("first_message", "total_ms", "Первое сообщение: DHT-разрешение + туннель + E2E TLS"),
            ("established_session", "rtt_ms", "Сообщение по установленной сессии")]):
        ax = axes[col]
        for y, sch in enumerate(schemes):
            rows = [r for r in concat(series, sch, "messages.csv")
                    if r["kind"] == kind and r[field] and r["status"] == "delivered"]
            vals = [float(r[field]) for r in rows]
            ax.scatter(vals, [y + (i % 5 - 2) * 0.05 for i in range(len(vals))], s=30, color=SERIES[sch],
                       edgecolor=SURFACE, linewidth=1, label=f"{sch} (n={len(vals)})", zorder=3)
            if vals:
                med = describe(vals)["median"]
                ax.plot([med, med], [y - 0.3, y + 0.3], color=INK, linewidth=2, zorder=4)
        ax.set_yticks(range(len(schemes)), schemes)
        ax.set_ylim(-0.7, len(schemes) - 0.3)
        ax.set_xlabel("задержка до APP_ACK, мс (черта — медиана)")
        ax.set_title(title, fontsize=10)
        ax.legend(fontsize=8, loc="lower right")
    save(fig, os.path.join(figdir, "messages.png"))


def fig_relay_failure(series, schemes, figdir):
    metrics = [("detection_ms", "обнаружение"), ("reroute_ms", "новый маршрут"),
               ("delivery_recovery_ms", "доставка восстановлена")]
    fig, axes = plt.subplots(1, 2, figsize=(9.6, 3.3))
    for col, (fname, title) in enumerate([("relay_failure.csv", "Ретранслятор завершён (kill)"),
                                          ("relay_freeze.csv", "Ретранслятор завис (freeze)")]):
        ax = axes[col]
        fault = "kill" if col == 0 else "freeze"
        rows = [r for s in schemes for r in concat(series, s, fname)]
        for y, (m, label) in enumerate(metrics):
            vals = [float(r[m]) for r in rows if r.get(m)]
            ax.scatter(vals, [y] * len(vals), s=40, color=FAULT[fault], edgecolor=SURFACE, linewidth=1.5,
                       zorder=3)
            if vals:
                med = describe(vals)["median"]
                ax.plot([med, med], [y - 0.3, y + 0.3], color=INK, linewidth=2, zorder=4)
                ax.annotate(f"{med:.0f} мс", (med, y + 0.32), color=INK2, fontsize=8, ha="center")
        ax.set_yticks(range(len(metrics)), [lbl for _, lbl in metrics])
        ax.set_ylim(-0.6, len(metrics) - 0.2)
        ax.set_xlabel(f"мс от инъекции отказа (n={len(rows)})")
        ax.set_title(title, fontsize=10)
    save(fig, os.path.join(figdir, "relay_failure.png"))


def fig_graphs(series, schemes, figdir):
    """Bootstrap-граф и DHT-overlay одного запуска: узлы на окружности в порядке NodeID."""
    import math
    fig, axes = plt.subplots(len(schemes), 2, figsize=(8.0, 4.0 * len(schemes)))
    if len(schemes) == 1:
        axes = [axes]
    for row, sch in enumerate(schemes):
        run = runs_of(series, sch)[0]
        snap = read_json(os.path.join(run, "tables.json"), {})
        boot = read_json(os.path.join(run, "bootstrap_graph.json"), [])
        ids = {k: int(v["node_id"], 16) for k, v in snap.items()}
        by_hex = {v["node_id"]: k for k, v in snap.items()}
        # Равномерно по окружности в порядке возрастания NodeID: порядок XOR-пространства
        # сохраняется, а узлы с близкими идентификаторами не накладываются друг на друга.
        order = sorted(ids, key=ids.get)
        pos = {k: (math.cos(2 * math.pi * r / len(order)), math.sin(2 * math.pi * r / len(order)))
               for r, k in enumerate(order)}
        for col, title in enumerate(["bootstrap-граф (начальные контакты)", "DHT-overlay (содержимое k-bucket)"]):
            ax = axes[row][col]
            ax.set_aspect("equal")
            ax.axis("off")
            edges = []
            if col == 0:
                edges = [(str(e["node"]), str(b)) for e in boot for b in e["bootstrap"]]
            else:
                edges = [(k, by_hex[c]) for k, v in snap.items() for c in v["contacts"] if c in by_hex]
            for a, b in edges:
                if a in pos and b in pos:
                    ax.plot([pos[a][0], pos[b][0]], [pos[a][1], pos[b][1]], color=INK2, linewidth=0.7,
                            alpha=0.55 if col else 0.9, zorder=1)
            for k_, (x, y) in pos.items():
                ax.scatter([x], [y], s=150, color=SERIES[sch], edgecolor=SURFACE, linewidth=2, zorder=3)
                ax.annotate(k_, (x, y), color=SURFACE, fontsize=7, ha="center", va="center", zorder=4,
                            fontweight="bold")
            pairs = len({frozenset(e) for e in edges})
            count = f"рёбер: {len(edges)}" if col == 0 else f"контактов в таблицах: {len(edges)}, пар узлов: {pairs}"
            ax.set_title(f"{sch}: {title}\n{count}", fontsize=9)
    fig.suptitle("Узлы равномерно расположены по окружности в порядке возрастания NodeID", fontsize=9,
                 color=INK2)
    save(fig, os.path.join(figdir, "graphs.png"))


# ------------------------------------------------------------------------ таблицы
def tables(series, schemes, plan) -> tuple[str, list[dict]]:
    out = [f"# Результаты серии `{os.path.basename(series)}`", "",
           f"Версия кода: `{plan.get('code_version')}`; N={plan.get('nodes')}, k={plan.get('k')}, ALPHA=3, R=3; "
           f"исследуемый фактор — схема bootstrap ({', '.join(schemes)}); {plan.get('runs_per_scheme')} "
           f"холодных запусков на схему; полная батарея отказов — в запуске 1 каждой схемы.", ""]
    stats_rows = []

    def add(scheme, name, values, digits=1):
        d = describe(values)
        stats_rows.append({"scheme": scheme, "metric": name, **d})
        return md_row(name, values, digits)

    for sch in schemes:
        runs = runs_of(series, sch)
        sums = [read_json(os.path.join(r, "summary.json"), {}) for r in runs]
        lk = concat(series, sch, "lookups.csv")
        ms_all = concat(series, sch, "messages.csv")
        ms = [r for r in ms_all if r["status"] == "delivered"]       # задержки — только доставленных
        sizes = [v["size"] for r in runs for v in read_json(os.path.join(r, "tables.json"), {}).values()]
        out += [f"## Схема {sch}", "", HEADER,
                add(sch, "Холодная сходимость, с", [s.get("convergence", {}).get("convergence_s") for s in sums], 2),
                add(sch, "Присоединение всех узлов, с", [s.get("join_all_s") for s in sums], 2),
                add(sch, "Раундов до сходимости", [s.get("convergence", {}).get("rounds") for s in sums], 0),
                add(sch, "Контактов в таблице узла", sizes, 1),
                add(sch, "Lookup: длительность, мс", [r["duration_ms"] for r in lk]),
                add(sch, "Lookup: RPC", [r["rpc_count"] for r in lk]),
                add(sch, "Lookup: итераций", [r["iterations"] for r in lk]),
                add(sch, "Lookup: промежуточных узлов", [r["intermediate_count"] for r in lk]),
                add(sch, "Отказ промежуточного узла: lookup, мс",
                    [r["duration_ms"] for r in concat(series, sch, "intermediate_failure.csv")]),
                add(sch, "Отказ хранителя: FIND_VALUE, мс",
                    [r["duration_after_ms"] for r in concat(series, sch, "storage_failure.csv")]),
                add(sch, "Построение туннеля, мс", [r["tunnel_build_ms"] for r in ms if r["kind"] == "first_message"]),
                add(sch, "Первое сообщение, мс", [r["total_ms"] for r in ms if r["kind"] == "first_message"]),
                add(sch, "Сообщение по установленной сессии, мс",
                    [r["rtt_ms"] for r in ms if r["kind"] == "established_session"], 2),
                add(sch, "Отказ ретранслятора (kill): обнаружение, мс",
                    [r["detection_ms"] for r in concat(series, sch, "relay_failure.csv")]),
                add(sch, "Отказ ретранслятора (kill): восстановление доставки, мс",
                    [r["delivery_recovery_ms"] for r in concat(series, sch, "relay_failure.csv")]),
                add(sch, "Зависание ретранслятора: обнаружение, мс",
                    [r["detection_ms"] for r in concat(series, sch, "relay_freeze.csv")]),
                add(sch, "Зависание ретранслятора: восстановление доставки, мс",
                    [r["delivery_recovery_ms"] for r in concat(series, sch, "relay_freeze.csv")]),
                ""]
        ok = sum(truthy(r["success"]) for r in lk)
        absent = sum(not truthy(r["target_in_table_before"]) for r in lk)
        nd = read_json(os.path.join(runs[0], "non_degeneracy.json"), {}) if runs else {}
        sf = concat(series, sch, "storage_failure.csv")
        bs = concat(series, sch, "bootstrap_shutdown.csv")
        rf = concat(series, sch, "relay_failure.csv")
        out += [f"- Успешных контрольных lookup: **{ok}/{len(lk)}**; цель отсутствовала в таблице инициатора "
                f"во всех {absent} из {len(lk)}.",
                f"- Невырожденность (запуск 1): {'выполнена' if nd.get('non_degenerate') else 'НЕ выполнена'}; "
                f"узлов с < N−1 контактов: {nd.get('share_nodes_below_n_minus_1', 0):.0%}; "
                f"максимальная таблица {nd.get('max_table_size')} при N−1={nd.get('n', 0) - 1}.",
                f"- Доставлено сообщений: {len(ms)}/{len(ms_all)} (недоставленные в статистику задержек "
                f"не входят).",
                f"- FIND_VALUE после отказа хранителя: {sum(truthy(r['success_after']) for r in sf)}/{len(sf)}; "
                f"lookup после отключения bootstrap: {sum(truthy(r['success']) for r in bs)}/{len(bs)}.",
                f"- Отказ ретранслятора: доставка восстановлена в {sum(bool(r['delivery_recovery_ms']) for r in rf)}"
                f"/{len(rf)} инъекций, новый маршрут без отказавшего узла — "
                f"{sum(truthy(r['new_route_excludes_failed']) for r in rf)}/{len(rf)}.", ""]
    out += ["Примечание. Длительности внутри узла (lookup, построение туннеля, RTT сообщения) измерены "
            "монотонными часами высокого разрешения. Времена отказа ретранслятора сравнивают системные часы "
            "оркестратора и узла (разрешение около 1 мс): значения 0 ± 1 мс означают обнаружение в пределах "
            "разрешения часов.", ""]
    return "\n".join(out), stats_rows


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    series = os.path.abspath(sys.argv[1])
    plan = read_json(os.path.join(series, "plan.json"), {})
    schemes = [s for s in plan.get("schemes", ["star", "ring"]) if os.path.isdir(os.path.join(series, s))]
    adir = os.path.join(series, "analysis")
    figdir = os.path.join(adir, "figures")
    os.makedirs(figdir, exist_ok=True)
    style()
    md, rows = tables(series, schemes, plan)
    figs = [("convergence.png", "Холодная сходимость"), ("lookup_cost.png", "Стоимость и длительность lookup"),
            ("routing_tables.png", "Таблицы маршрутизации"), ("messages.png", "Задержка сообщений"),
            ("relay_failure.png", "Отказ ретранслятора"), ("graphs.png", "Bootstrap-граф и DHT-overlay")]
    fig_convergence(series, schemes, figdir)
    fig_lookup(series, schemes, figdir)
    fig_tables(series, schemes, plan.get("nodes", 15), plan.get("k", 3), figdir)
    fig_messages(series, schemes, figdir)
    fig_relay_failure(series, schemes, figdir)
    fig_graphs(series, schemes, figdir)
    md += "\n## Графики\n\n" + "\n".join(f"![{t}](figures/{f})" for f, t in figs) + "\n"
    with open(os.path.join(adir, "tables.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    with open(os.path.join(adir, "stats.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["scheme", "metric", "n", "min", "max", "median", "mean", "p95"])
        w.writeheader()
        w.writerows(rows)
    print(md)
    print(f"\nТаблицы и графики: {adir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
