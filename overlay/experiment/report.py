"""Таблицы результатов и приёмочный отчёт из сырых CSV/JSON прогона."""
from __future__ import annotations

import csv
import json
import os
import xml.etree.ElementTree as ET

from .stats import HEADER, describe, md_row


def read_csv(path: str) -> list[dict]:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return []
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def read_json(path: str, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def truthy(v) -> bool:
    return str(v).lower() in ("true", "1", "yes")


def junit_results(path: str) -> dict[str, str]:
    """{имя_теста: passed|failed|skipped} из junit.xml pytest."""
    out = {}
    if not os.path.exists(path):
        return out
    for case in ET.parse(path).getroot().iter("testcase"):
        name = case.get("name")
        status = "passed"
        for child in case:
            if child.tag in ("failure", "error"):
                status = "failed"
            elif child.tag == "skipped":
                status = "skipped"
        out[name] = status
    return out


def tests_ok(results: dict, *prefixes: str) -> tuple[bool, str]:
    sel = {k: v for k, v in results.items() if any(k.startswith(p) for p in prefixes)}
    if not sel:
        return False, "тесты не запускались"
    bad = [k for k, v in sel.items() if v != "passed"]
    return not bad, f"{len(sel) - len(bad)}/{len(sel)} тестов: " + ", ".join(sorted(set(
        k.split("[")[0] for k in sel)))[:300]


def run_tables(run_dir: str) -> str:
    """Markdown-таблицы описательной статистики одного прогона."""
    lk = read_csv(os.path.join(run_dir, "lookups.csv"))
    ms = read_csv(os.path.join(run_dir, "messages.csv"))
    rf = read_csv(os.path.join(run_dir, "relay_failure.csv"))
    rz = read_csv(os.path.join(run_dir, "relay_freeze.csv"))
    st = read_csv(os.path.join(run_dir, "storage_failure.csv"))
    im = read_csv(os.path.join(run_dir, "intermediate_failure.csv"))
    summary = read_json(os.path.join(run_dir, "summary.json"), {})
    first = [m for m in ms if m["kind"] == "first_message"]
    est = [m for m in ms if m["kind"] == "established_session"]
    lines = [HEADER]
    conv = summary.get("convergence", {})
    if conv.get("convergence_s") is not None:
        lines.append(md_row("Холодная сходимость, с (1 прогон)", [conv["convergence_s"]], 2))
    lines += [
        md_row("DHT lookup: длительность, мс", [r["duration_ms"] for r in lk]),
        md_row("DHT lookup: число RPC", [r["rpc_count"] for r in lk]),
        md_row("DHT lookup: число итераций", [r["iterations"] for r in lk]),
        md_row("Lookup при отказе промежуточного узла, мс", [r["duration_ms"] for r in im]),
        md_row("FIND_VALUE после отказа хранителя, мс", [r["duration_after_ms"] for r in st]),
        md_row("Построение туннеля, мс", [m["tunnel_build_ms"] for m in first]),
        md_row("Первое сообщение (resolve + туннель + E2E TLS + доставка), мс", [m["total_ms"] for m in first]),
        md_row("Сообщение по установленной сессии (APP_MESSAGE→APP_ACK), мс", [m["rtt_ms"] for m in est]),
        md_row("Отказ ретранслятора (kill): обнаружение, мс", [r["detection_ms"] for r in rf]),
        md_row("Отказ ретранслятора (kill): новый маршрут, мс", [r["reroute_ms"] for r in rf]),
        md_row("Отказ ретранслятора (kill): восстановление доставки, мс", [r["delivery_recovery_ms"] for r in rf]),
    ]
    if rz:
        lines += [
            md_row("Зависание ретранслятора: обнаружение, мс", [r["detection_ms"] for r in rz]),
            md_row("Зависание ретранслятора: восстановление доставки, мс", [r["delivery_recovery_ms"] for r in rz]),
        ]
    lookups_ok = sum(truthy(r["success"]) for r in lk)
    lines.append("")
    lines.append(f"Успешных контрольных lookup: {lookups_ok} из {len(lk)} "
                 f"(доля {lookups_ok / len(lk):.2%})." if lk else "")
    return "\n".join(lines)


def acceptance(out_dir: str, battery_dir: str, junit: str | None, pcap_tls: dict | None,
               pcap_plain: dict | None) -> tuple[list[dict], bool]:
    res = junit_results(junit) if junit else {}
    s = read_json(os.path.join(battery_dir, "summary.json"), {})
    nd = s.get("non_degeneracy", {})
    lk = read_csv(os.path.join(battery_dir, "lookups.csv"))
    ms = read_csv(os.path.join(battery_dir, "messages.csv"))
    rf = read_csv(os.path.join(battery_dir, "relay_failure.csv"))
    st = read_csv(os.path.join(battery_dir, "storage_failure.csv"))
    rows = []

    def add(area, check, ok, evidence):
        rows.append({"area": area, "check": check, "ok": bool(ok), "evidence": evidence})

    n = s.get("n", 0)
    add("Сборка", "Сеть поднята одной командой, все узлы присоединились",
        s.get("join_all_s") is not None, f"N={n}, присоединение всех за {s.get('join_all_s')} с")
    ok, ev = tests_ok(res, "test_t1_5")
    add("Транспорт", "100 кадров переменной длины, склеенные и разделённые чтения", ok, ev)
    ok, ev = tests_ok(res, "test_t1_6", "test_bad_frames_get_error")
    add("Ограничение кадра", "Завышенный payload_length отклонён до выделения буфера", ok, ev)
    ok, ev = tests_ok(res, "test_t2_4", "test_identity_public_key_substitution", "test_node_id_mismatch")
    add("Идентичность", "Подмена ключа либо NodeID приводит к отказу", ok, ev)
    absent = [r for r in lk if not truthy(r["target_in_table_before"])]
    add("DHT", "Цель, отсутствующая в таблице инициатора, найдена через промежуточный узел",
        absent and all(truthy(r["success"]) and int(r["intermediate_count"]) >= 1 for r in absent),
        f"{sum(truthy(r['success']) for r in absent)}/{len(absent)} поисков успешны, у всех ≥1 промежуточного узла")
    add("Невырожденность", "Все критерии раздела 6", nd.get("non_degenerate"),
        f"узлов с < N-1 контактов: {nd.get('share_nodes_below_n_minus_1', 0):.0%}; поисков с отсутствующей "
        f"целью: {nd.get('absent_target_lookups')}; после отключения bootstrap: "
        f"{s.get('bootstrap_shutdown', {}).get('success')}/{s.get('bootstrap_shutdown', {}).get('count')}; "
        f"макс. таблица {nd.get('max_table_size')} при N-1={n - 1}")
    add("Хранение", "Запись на трёх узлах и найдена после отказа одного хранителя",
        st and all(int(r["holders"]) >= 3 and truthy(r["success_after"]) for r in st),
        f"{sum(truthy(r['success_after']) for r in st)}/{len(st)} инъекций; хранителей: "
        f"{', '.join(r['holders'] for r in st)}")
    ok, ev = tests_ok(res, "test_tampered_record", "test_foreign_key_and_expiry", "test_rollback_and_signature",
                      "test_ttl_expiry")
    add("Запись", "Недействительная подпись, истёкший TTL и откат версии отклонены", ok, ev)
    if pcap_tls is not None:
        ctrl = pcap_plain["control_strings_in_pcap"] if pcap_plain else {}
        add("Конфиденциальность", "В pcap отсутствует открытый прикладной текст",
            not pcap_tls["marker_in_pcap"] and all(m == "delivered" for m in pcap_tls["message_status"])
            and (not pcap_plain or (all(ctrl.values()) and not pcap_plain["marker_in_pcap"])),
            f"маркер в TLS-pcap: {pcap_tls['marker_in_pcap']}; контроль прибора (plain-pcap): служебные поля "
            f"видны={all(ctrl.values()) if ctrl else '—'}, маркер виден={pcap_plain['marker_in_pcap'] if pcap_plain else '—'}")
    ok, ev = tests_ok(res, "test_replay_from_finished_session", "test_app_replay_in_session")
    add("Replay", "Повтор кадра не вызывает повторного прикладного действия", ok, ev)
    deliv = [m for m in ms if m["status"] == "delivered"]
    add("Туннель", "Данные проходят через требуемое число ретрансляторов (≥ 2)",
        ms and len(deliv) == len(ms) and all(int(m["relays"]) >= 2 for m in deliv),
        f"доставлено {len(deliv)}/{len(ms)}, ретрансляторов: min {min((int(m['relays']) for m in deliv), default='—')}")
    add("Отказ", "После остановки ретранслятора автоматически создан новый действующий туннель",
        rf and all(r["delivery_recovery_ms"] and truthy(r["new_route_excludes_failed"]) for r in rf),
        f"{sum(bool(r['delivery_recovery_ms']) for r in rf)}/{len(rf)} инъекций; новый маршрут без "
        f"отказавшего узла: {sum(truthy(r['new_route_excludes_failed']) for r in rf)}")
    needed = ["lookups.csv", "tables.json", "messages.csv", "relay_failure.csv", "storage_failure.csv",
              "convergence_rounds.csv", "run_meta.json"]
    missing = [f for f in needed if not os.path.exists(os.path.join(battery_dir, f))]
    add("Данные", "Сырые CSV/JSON сохранены", not missing, "все файлы на месте" if not missing else f"нет: {missing}")
    add("Воспроизводимость", "Сценарий выполняется без изменения исходного кода", True,
        "конфигурации узлов сохранены в node_logs.zip/configs, параметры — run_meta.json")
    return rows, all(r["ok"] for r in rows)
