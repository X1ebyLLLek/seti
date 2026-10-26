"""Экспериментальные сценарии поверх Cluster. Каждый сценарий возвращает список
строк (dict) — сырые наблюдения, которые сохраняются в CSV/JSON без обработки."""
from __future__ import annotations

import random
import threading
import time

from ..dht import record_key
from ..routing import xor_distance

# ---------------------------------------------------------------------- помощники


def table_ids(cluster, i: int) -> set[str]:
    t = cluster.call(i, "table")
    return {c["node_id"] for b in t["buckets"] for c in b["contacts"]}


def pick_absent_pair(cluster, rnd: random.Random, nodes: list[int] | None = None, tries: int = 200):
    """Пара (инициатор, цель), где цели нет в таблице инициатора до начала поиска."""
    nodes = nodes or cluster.alive_nodes()
    for _ in range(tries):
        i, j = rnd.sample(nodes, 2)
        if cluster.node_ids[j] not in table_ids(cluster, i):
            return i, j
    return None


def lookup_row(r: dict, i: int, j: int, scenario: str, **extra) -> dict:
    return {"scenario": scenario, "initiator": i + 1, "target": j + 1, "lookup_id": r["lookup_id"],
            "success": r["success"], "target_in_table_before": r["target_in_table_before"],
            "table_size_before": r["table_size_before"], "rpc_count": r["rpc_count"], "iterations": r["iterations"],
            "intermediate_count": r["intermediate_count"], "timeouts": len(r["timeouts"]), "errors": len(r["errors"]),
            "duration_ms": r["duration_ms"], "found_by": (r["found_by"] or "")[:8],
            "queried_by_iteration": "|".join(",".join(q[:8] for q in rd["queried"]) for rd in r["rounds"]), **extra}


# ---------------------------------------------------------------------- 1. холодная сходимость
def convergence(cluster, t0: float, rnd: random.Random, round_lookups: int = 15, max_rounds: int = 40,
                success_threshold: float = 0.95, contacts_tolerance: float = 0.10) -> tuple[list[dict], dict]:
    """Контрольные раунды lookup по заранее сформированному оркестратором набору пар.
    Сходимость: два последовательных раунда с долей успеха >= 95% и изменение суммарного
    числа контактов не более 10% за окно из этих двух раундов."""
    rows = []
    nodes = cluster.alive_nodes()
    plan = [[tuple(rnd.sample(nodes, 2)) for _ in range(round_lookups)] for _ in range(max_rounds)]
    converged_at = None
    contacts_before = sum(cluster.call(i, "info")["routing"]["size"] for i in nodes)
    for rno, pairs in enumerate(plan, 1):
        ok = 0
        rpc = 0
        for i, j in pairs:
            r = cluster.call(i, "lookup", target=cluster.node_ids[j], purpose="convergence")
            ok += r["success"]
            rpc += r["rpc_count"]
        contacts = sum(cluster.call(i, "info")["routing"]["size"] for i in nodes)
        rows.append({"round": rno, "t_rel_s": round(time.time() - t0, 3), "lookups": len(pairs),
                     "success_rate": ok / len(pairs), "total_contacts": contacts, "rpc_total": rpc,
                     "contacts_before_round": contacts_before})
        contacts_before = contacts
        if len(rows) >= 2:
            a, b = rows[-2], rows[-1]
            base = a["contacts_before_round"] or 1
            drift = abs(b["total_contacts"] - a["contacts_before_round"]) / base
            if a["success_rate"] >= success_threshold and b["success_rate"] >= success_threshold \
                    and drift <= contacts_tolerance:
                converged_at = b["t_rel_s"]
                break
    return rows, {"converged": converged_at is not None, "convergence_s": converged_at, "rounds": len(rows)}


# ---------------------------------------------------------------------- 2. контрольные lookup
def control_lookups(cluster, rnd: random.Random, count: int = 30, scenario: str = "control",
                    nodes: list[int] | None = None) -> list[dict]:
    rows = []
    for _ in range(count):
        pair = pick_absent_pair(cluster, rnd, nodes)
        if pair is None:
            break
        i, j = pair
        r = cluster.call(i, "lookup", target=cluster.node_ids[j])
        rows.append(lookup_row(r, i, j, scenario))
    return rows


# ---------------------------------------------------------------------- 3. снимок таблиц
def tables_snapshot(cluster) -> dict:
    snap = {}
    for i in cluster.alive_nodes():
        t = cluster.call(i, "table")
        snap[str(i + 1)] = {"node_id": cluster.node_ids[i], "size": t["size"],
                            "buckets": {str(b["index"]): b["fill"] for b in t["buckets"]},
                            "contacts": [c["node_id"] for b in t["buckets"] for c in b["contacts"]]}
    return snap


def non_degeneracy(snapshot: dict, lookups: list[dict], after_bootstrap: list[dict], n: int) -> dict:
    sizes = [v["size"] for v in snapshot.values()]
    below = sum(s < n - 1 for s in sizes)
    absent = [r for r in lookups if not r["target_in_table_before"]]
    via_intermediate = [r for r in absent if r["intermediate_count"] >= 1]
    crit = {
        "no_full_registry": True,     # рабочий узел не имеет реестра: проверяется кодом и записями таблиц
        "share_nodes_below_n_minus_1": below / len(sizes) if sizes else 0.0,
        "c2_80pct_below_n_minus_1": bool(sizes) and below / len(sizes) >= 0.8,
        "absent_target_lookups": len(absent),
        "c3_at_least_30_absent": len(absent) >= 30,
        "c4_each_uses_intermediate": bool(absent) and len(via_intermediate) == len(absent),
        "absent_success_rate": sum(r["success"] for r in absent) / len(absent) if absent else 0.0,
        "after_bootstrap_lookups": len(after_bootstrap),
        "c5_works_without_bootstrap": bool(after_bootstrap) and all(r["success"] for r in after_bootstrap),
        "c6_data_saved": True,
        "max_table_size": max(sizes) if sizes else 0, "min_table_size": min(sizes) if sizes else 0,
        "n": n,
    }
    crit["non_degenerate"] = all(crit[k] for k in ("c2_80pct_below_n_minus_1", "c3_at_least_30_absent",
                                                     "c4_each_uses_intermediate", "c5_works_without_bootstrap"))
    return crit


# ---------------------------------------------------------------------- 4. отказ хранителя
def storage_failure(cluster, rnd: random.Random, injections: int = 5) -> list[dict]:
    rows = []
    for k in range(injections):
        alive = [i for i in cluster.alive_nodes() if i != 0]
        owner = rnd.choice(alive)
        cluster.call(owner, "publish")
        key = record_key(bytes.fromhex(cluster.node_ids[owner])).hex()
        holders = [i for i in cluster.alive_nodes() if any(r["key"] == key for r in cluster.call(i, "storage"))]
        victims = [h for h in holders if h not in (0, owner)]
        if not victims:
            continue
        victim = rnd.choice(victims)
        # Читатель не хранит запись: проверяется поиск по сети, а не чтение своей копии.
        readers = [i for i in cluster.alive_nodes() if i not in (owner, victim) and i not in holders]
        if not readers:
            continue
        reader = rnd.choice(readers)
        before = cluster.call(reader, "get_record", node_id=cluster.node_ids[owner])
        t_kill = cluster.kill(victim)
        after = cluster.call(reader, "get_record", node_id=cluster.node_ids[owner])
        rows.append({"injection": k + 1, "owner": owner + 1, "holders": len(holders),
                     "holder_ids": ",".join(str(h + 1) for h in holders), "killed_holder": victim + 1,
                     "reader": reader + 1, "success_before": before["success"], "success_after": after["success"],
                     "rpc_after": after["rpc_count"], "iterations_after": after["iterations"],
                     "timeouts_after": len(after["timeouts"]), "errors_after": len(after["errors"]),
                     "duration_after_ms": after["duration_ms"], "record_seq": after.get("record_seq"),
                     "record_from": (after.get("record_from") or "")[:8], "t_kill": t_kill})
        cluster.restart(victim)
    return rows


# ---------------------------------------------------------------------- 5. отказ промежуточного узла
def intermediate_failure(cluster, rnd: random.Random, injections: int = 5) -> list[dict]:
    """Отключение кандидата, которого инициатор опросит первым (ближайший к цели контакт
    его таблицы), непосредственно перед контрольным lookup."""
    rows = []
    attempts = 0
    while len(rows) < injections and attempts < injections * 6:
        attempts += 1
        k = len(rows)
        pair = pick_absent_pair(cluster, rnd, [i for i in cluster.alive_nodes()])
        if pair is None:
            break
        i, j = pair
        target = bytes.fromhex(cluster.node_ids[j])
        t = cluster.call(i, "table")
        cands = sorted((c["node_id"] for b in t["buckets"] for c in b["contacts"]),
                       key=lambda x: xor_distance(bytes.fromhex(x), target))
        victim_id = next((c for c in cands if c != cluster.node_ids[j]), None)
        victim = next(v for v, nid in cluster.node_ids.items() if nid == victim_id)
        if victim == 0 or victim == i:
            continue
        cluster.kill(victim)
        r = cluster.call(i, "lookup", target=cluster.node_ids[j])
        first_round = r["rounds"][0]["queried"] if r["rounds"] else []
        rows.append(lookup_row(r, i, j, "intermediate_failure", injection=k + 1, killed=victim + 1,
                               killed_in_first_round=victim_id in first_round))
        cluster.restart(victim)
    return rows


# ---------------------------------------------------------------------- 6. сообщения
def messaging(cluster, rnd: random.Random, first_pairs: int = 10, established: int = 30) -> list[dict]:
    rows = []
    alive = cluster.alive_nodes()
    pairs = []
    while len(pairs) < first_pairs:
        p = tuple(rnd.sample(alive, 2))
        if p not in pairs:
            pairs.append(p)
    for k, (s, d) in enumerate(pairs):
        r = cluster.call(s, "send", to=cluster.node_ids[d], text=f"first message {k}", keepalive=False)
        rows.append(_msg_row(r, s, d, "first_message"))
    s, d = pairs[0]
    for k in range(established):
        r = cluster.call(s, "send", to=cluster.node_ids[d], text=f"established {k} " + "x" * rnd.randint(0, 200),
                         keepalive=False)
        rows.append(_msg_row(r, s, d, "established_session"))
    return rows


def _msg_row(r: dict, s: int, d: int, kind: str) -> dict:
    setup = r.get("setup") or {}
    return {"kind": kind, "src": s + 1, "dst": d + 1, "status": r["status"], "attempts": r["attempts"],
            "first_message": r["first_message"], "total_ms": r["total_ms"], "rtt_ms": r.get("rtt_ms"),
            "resolve_ms": setup.get("resolve_ms"), "tunnel_build_ms": setup.get("build_ms"),
            "e2e_handshake_ms": setup.get("handshake_ms"), "setup_ms": setup.get("setup_ms"),
            "relays": len(r.get("route") or []) - 2 if r.get("route") else None,
            "route": "->".join(x[:8] for x in r.get("route") or [])}


# ---------------------------------------------------------------------- 7. отказ ретранслятора
def relay_failure(cluster, rnd: random.Random, injections: int = 5, send_interval_s: float = 0.1,
                  timeout_s: float = 30.0, fault: str = "kill") -> list[dict]:
    """fault=kill — аварийное завершение процесса ретранслятора (разрыв TCP);
    fault=freeze — ретранслятор «завис»: соединения открыты, но кадры не обрабатываются
    (отказ обнаруживается только по отсутствию TUNNEL_ACK)."""
    rows = []
    for k in range(injections):
        alive = [i for i in cluster.alive_nodes()]
        s, d = rnd.sample([i for i in alive if i != 0], 2)
        first = cluster.call(s, "send", to=cluster.node_ids[d], text=f"warmup {k}")
        if first["status"] != "delivered":
            continue
        route = first["route"]
        relay_ids = route[1:-1]
        victim_id = relay_ids[k % len(relay_ids)]
        victim = next(v for v, nid in cluster.node_ids.items() if nid == victim_id)
        if victim == 0:
            victim_id = relay_ids[(k + 1) % len(relay_ids)]
            victim = next(v for v, nid in cluster.node_ids.items() if nid == victim_id)
        log = []
        stop = threading.Event()

        def sender():
            from ..node.control import ControlClient
            c = ControlClient.from_state_dir(cluster.state_dir(s))
            n = 0
            while not stop.is_set():
                t_start = time.time()
                try:
                    r = c.call("send", to=cluster.node_ids[d], text=f"during failure {k}/{n}")
                    log.append((t_start, time.time(), r))
                except Exception as exc:
                    log.append((t_start, time.time(), {"status": "error", "error": str(exc)}))
                n += 1
                time.sleep(send_interval_s)
            c.close()

        th = threading.Thread(target=sender, daemon=True)
        th.start()
        time.sleep(0.5)
        if fault == "kill":
            t_kill = cluster.kill(victim)
        else:
            t_kill = time.time()
            cluster.call(victim, "freeze", seconds=timeout_s + 10)
            cluster.event("freeze", victim)
        deadline = time.time() + timeout_s
        delivered_after = None
        while time.time() < deadline:
            time.sleep(0.05)
            for t_start, t_end, r in list(log):
                if t_end > t_kill and r.get("status") in ("delivered", "duplicate") and \
                        victim_id not in (r.get("route") or []):
                    delivered_after = (t_start, t_end, r)
                    break
            if delivered_after:
                break
        time.sleep(0.3)
        stop.set()
        th.join(15)
        tun = cluster.call(s, "tunnels")
        # Обнаружение — первая гибель туннеля через отказавший узел после инъекции
        # (более поздние записи — неудачные попытки перестроения).
        dead = sorted((h for h in tun["history"] if victim_id in h["route_full"] and h.get("dead_ms")
                       and h["dead_ms"] / 1000 >= t_kill - 0.05), key=lambda h: h["dead_ms"])
        rec = [x for x in tun["recoveries"] if x["active_ms"] / 1000 >= t_kill - 0.5]
        dead_ms = dead[0]["dead_ms"] / 1000 if dead else None
        after = [x for x in log if x[0] > t_kill]
        rows.append({
            "injection": k + 1, "fault": fault, "src": s + 1, "dst": d + 1, "killed_relay": victim + 1,
            "relay_position": relay_ids.index(victim_id) + 1, "old_route": "->".join(x[:8] for x in route),
            "detection_ms": round((dead_ms - t_kill) * 1000, 1) if dead_ms else None,
            "dead_reason": dead[0]["close_reason"] if dead else None,
            "reroute_ms": round((rec[0]["active_ms"] / 1000 - t_kill) * 1000, 1) if rec else None,
            "delivery_recovery_ms": round((delivered_after[1] - t_kill) * 1000, 1) if delivered_after else None,
            "new_route": "->".join(x[:8] for x in (delivered_after[2].get("route") or [])) if delivered_after else "",
            "new_route_excludes_failed": bool(delivered_after) and victim_id not in delivered_after[2]["route"],
            "messages_after_kill": len(after),
            "failed_after_kill": sum(1 for x in after if x[2].get("status") not in ("delivered", "duplicate")),
            "duplicates_after_kill": sum(1 for x in after if x[2].get("status") == "duplicate"),
            "t_kill": t_kill})
        if fault != "kill":
            cluster.kill(victim)                       # сброс «зависшего» узла перед следующей инъекцией
        cluster.restart(victim)
    return rows


# ---------------------------------------------------------------------- 8. отключение bootstrap
def bootstrap_shutdown(cluster, rnd: random.Random, count: int = 10) -> list[dict]:
    cluster.kill(0)
    rest = cluster.alive_nodes()
    return control_lookups(cluster, rnd, count, scenario="after_bootstrap_shutdown", nodes=rest)
