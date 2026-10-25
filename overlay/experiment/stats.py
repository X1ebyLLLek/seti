"""Описательная статистика по правилам ТЗ: n, минимум, максимум, медиана, среднее;
p95 — только при n >= 20; p99 по малым выборкам не вычисляется."""
from __future__ import annotations

import math
import statistics


def _num(v):
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


def percentile(sorted_vals: list[float], q: float) -> float:
    """Линейная интерполяция между порядковыми статистиками (метод 7, как в numpy по умолчанию)."""
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def describe(values) -> dict:
    vals = sorted(x for x in (_num(v) for v in values) if x is not None)
    n = len(vals)
    if n == 0:
        return {"n": 0, "min": None, "max": None, "median": None, "mean": None, "p95": None}
    return {"n": n, "min": vals[0], "max": vals[-1], "median": statistics.median(vals),
            "mean": statistics.fmean(vals), "p95": percentile(vals, 0.95) if n >= 20 else None}


def fmt(d: dict, digits: int = 1) -> list[str]:
    def f(x):
        return "—" if x is None else f"{x:.{digits}f}"
    return [str(d["n"]), f(d["min"]), f(d["max"]), f(d["median"]), f(d["mean"]),
            f(d["p95"]) if d["n"] >= 20 else "n<20"]


HEADER = "| Показатель | n | min | max | медиана | среднее | p95 |\n|---|---:|---:|---:|---:|---:|---:|"


def md_row(name: str, values, digits: int = 1) -> str:
    return "| " + name + " | " + " | ".join(fmt(describe(values), digits)) + " |"
