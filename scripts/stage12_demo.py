"""Демонстрация и контрольная точка этапов 1–2 (разделы 13.2 и 25 требований).

    python scripts/stage12_demo.py                  # 10 узлов, star, режим без TLS (как на этапах 1–2)
    python scripts/stage12_demo.py --nodes 6 --scheme ring

Этап 1 (два независимых процесса): серверы на разных портах, исходящее соединение,
PING/PONG с одинаковым request_id, 100 кадров переменной длины, склеенные и
разделённые кадры, отклонение завышенной длины, журнал.
Этап 2 (сеть процессов): seed без bootstrap, присоединение по схеме, PING bootstrap-узлу
и self-lookup каждого узла, lookup цели, отсутствующей в таблице инициатора,
отключение bootstrap-узла и повторный lookup, >= 10 контрольных lookup, выгрузка таблиц.
Результаты: results/stage12-<время>/ (stage12_report.md, JSON/CSV, журналы узлов).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import socket
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from overlay.experiment import scenarios as sc  # noqa: E402
from overlay.experiment.cluster import Cluster  # noqa: E402
from overlay.experiment.runner import collect_logs, git_version, write_csv, write_json  # noqa: E402
from overlay.protocol import (Frame, FrameDecoder, MsgType, decode_payload, encode_frame,  # noqa: E402
                              encode_payload, new_request_id)

OUT_LINES: list[str] = []


def say(line: str = "") -> None:
    print(line, flush=True)
    OUT_LINES.append(line)


def raw_exchange(port: int, chunks: list[bytes], expect: int, pause: float = 0.05, timeout: float = 5.0):
    """Отправка байтов заданными порциями и приём expect кадров (или до закрытия)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    for c in chunks:
        s.sendall(c)
        time.sleep(pause)
    dec, frames, closed = FrameDecoder(), [], False
    s.settimeout(timeout)
    while len(frames) < expect:
        try:
            data = s.recv(65536)
        except socket.timeout:
            break
        if not data:
            closed = True
            break
        dec.feed(data)
        frames += dec.drain()
    if not closed:
        try:
            s.settimeout(1.0)
            closed = s.recv(10) == b""
        except (socket.timeout, OSError):
            pass
    s.close()
    return frames, closed


def echo(data: bytes, rid: bytes | None = None) -> bytes:
    return encode_frame(Frame(int(MsgType.ECHO_REQUEST), rid or new_request_id(),
                              encode_payload(MsgType.ECHO_REQUEST, {"data": data})))


def grep_log(state_dir: str, *needles: str, limit: int = 6) -> list[str]:
    path = os.path.join(state_dir, "logs", "node.log")
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if all(n in line for n in needles):
                out.append(line.rstrip())
                if len(out) >= limit:
                    break
    return out


def stage1(out: str, run_dir: str, base_port: int) -> dict:
    say("## Этап 1. Транспорт и кадрирование (два независимых процесса)")
    res = {}
    with Cluster(os.path.join(run_dir, "stage1"), 2, "star", base_port=base_port, security="plain",
                 log_level="INFO") as c:
        c.start_all()
        a, b = c.node_ids[0], c.node_ids[1]
        say(f"1. Узел A {a[:16]}… слушает 127.0.0.1:{c.port(0)}, узел B {b[:16]}… — 127.0.0.1:{c.port(1)}")
        r = c.call(0, "ping", host="127.0.0.1", port=c.port(1))
        say(f"2. Исходящее соединение A -> B установлено, B ответил как {r['node_id'][:16]}… (RTT {r['rtt_ms']:.2f} мс)")
        sent = grep_log(c.state_dir(0), "frame.send", "type=PING")
        recv = grep_log(c.state_dir(0), "frame.recv", "type=PONG")
        rid_s = sent[-1].split("rid=")[1].split()[0] if sent else "?"
        rid_r = [x.split("rid=")[1].split()[0] for x in recv]
        res["ping_pong_same_rid"] = rid_s in rid_r
        say(f"3. PING request_id={rid_s}…, PONG request_id={rid_r[-1] if rid_r else '?'}… -> совпадают: "
            f"{res['ping_pong_same_rid']}")
        burst = c.call(0, "echo_burst", host="127.0.0.1", port=c.port(1), count=100, seed=7)
        res["burst"] = burst
        say(f"4. A -> B: {burst['sent']} кадров ECHO переменной длины ({burst['min_size']}..{burst['max_size']} байт "
            f"payload, всего {burst['total_payload_bytes']} байт), совпало ответов: {burst['matched']}")
        f1, f2, f3 = echo(b"glued-1"), echo(b"glued-2"), echo(b"split" * 300)
        frames, _ = raw_exchange(c.port(1), [f1 + f2], 2)
        glued = [decode_payload(MsgType.ECHO_RESPONSE, f.payload)["data"] for f in frames]
        frames, _ = raw_exchange(c.port(1), [f3[:10], f3[10:24], f3[24:700], f3[700:]], 1)
        split_ok = bool(frames) and decode_payload(MsgType.ECHO_RESPONSE, frames[0].payload)["data"] == b"split" * 300
        res["glued_ok"] = glued == [b"glued-1", b"glued-2"]
        res["split_ok"] = split_ok
        say(f"5. Два кадра одной записью в сокет -> ответы {glued}; кадр, разрезанный на 4 записи "
            f"(в т.ч. внутри заголовка) -> принят целиком: {split_ok}")
        oversized = bytes([1, int(MsgType.ECHO_REQUEST), 0, 0]) + new_request_id() + (65537).to_bytes(4, "big")
        frames, closed = raw_exchange(c.port(1), [oversized], 1)
        err = decode_payload(MsgType.ERROR, frames[0].payload) if frames and frames[0].type == MsgType.ERROR else None
        res["oversized_rejected"] = bool(err and err["code"] == "FRAME_TOO_LARGE" and closed)
        say(f"6. Заголовок с payload_length=65537 (payload не отправлялся) -> ERROR {err['code'] if err else '?'}, "
            f"соединение закрыто: {closed}")
        say("7. Журнал узла B (выдержка):")
        for line in grep_log(c.state_dir(1), "protocol.error", limit=2) + grep_log(c.state_dir(1), "conn.close",
                                                                                   "protocol", limit=2):
            say("   " + line)
        c.stop_all()
        res["logs"] = collect_logs(c, os.path.join(out, "stage1"))
    return res


def stage2(out: str, run_dir: str, n: int, scheme: str, base_port: int, seed: int) -> dict:
    say("")
    say(f"## Этап 2. Идентичность, Kademlia, bootstrap ({n} процессов, схема {scheme}, без TLS)")
    rnd = random.Random(seed)
    res = {}
    with Cluster(os.path.join(run_dir, "stage2"), n, scheme, base_port=base_port + 10, security="plain",
                 seed=seed) as c:
        t0 = time.time()
        c.start_all()
        say(f"1–4. Seed-узел 1 запущен без BOOTSTRAP_PEERS; узлы 2..{n} присоединились за {time.time() - t0:.1f} с.")
        boot = []
        for i in range(n):
            with open(os.path.join(c.state_dir(i), "bootstrap.json"), encoding="utf-8") as fh:
                b = json.load(fh) if i else {}
            if i == 0:
                continue
            sl = b["self_lookup"]
            boot.append({"node": i + 1, "bootstrap_peers": ",".join(str(j + 1) for j in
                                                                    sc_bootstrap(scheme, i)),
                         "ping_ok": len(b["peers_ok"]), "self_lookup_rpc": sl["rpc_count"],
                         "self_lookup_iterations": sl["iterations"], "table_after_join": b["table_size"],
                         "join_ms": round(b["duration_ms"], 1)})
            say(f"   узел {i + 1:2d}: PING -> узел(ы) {boot[-1]['bootstrap_peers']} ok; self-lookup: RPC={sl['rpc_count']}, "
                f"итераций={sl['iterations']}; таблица после входа: {b['table_size']}")
        write_csv(os.path.join(out, "bootstrap_log.csv"), boot)
        snap = sc.tables_snapshot(c)
        write_json(os.path.join(out, "routing_tables.json"), snap)
        write_csv(os.path.join(out, "routing_tables.csv"),
                  [{"node": k, "node_id": v["node_id"], "unique_contacts": v["size"],
                    "bucket_fill": json.dumps(v["buckets"])} for k, v in snap.items()])
        sizes = [v["size"] for v in snap.values()]
        say(f"   Уникальных контактов в таблицах: {sizes} (N-1 = {n - 1}); k-bucket заполнены не более чем на k=3.")
        pair = sc.pick_absent_pair(c, rnd)
        i, j = pair
        r = c.call(i, "lookup", target=c.node_ids[j])
        res["demo_lookup"] = r
        say(f"5–8. Инициатор {i + 1}, цель {j + 1} (цели нет в таблице инициатора: {not r['target_in_table_before']}):")
        for rd in r["rounds"]:
            say(f"   итерация {rd['iteration']}: опрошены {', '.join(q[:8] for q in rd['queried'])}; "
                f"ответили {len(rd['responded'])}, новых кандидатов {rd['new']}")
        say(f"   успех={r['success']}, RPC={r['rpc_count']}, итераций={r['iterations']}, "
            f"длительность={r['duration_ms']:.1f} мс, промежуточных узлов: {r['intermediate_count']}, "
            f"контакт цели вернул узел {str(r['found_by'])[:8]}")
        control = sc.control_lookups(c, rnd, 12)
        write_csv(os.path.join(out, "control_lookups.csv"), control)
        ok = sum(x["success"] for x in control)
        say(f"   Контрольные lookup (цель отсутствует у инициатора): {ok}/{len(control)} успешны, "
            f"RPC: {[x['rpc_count'] for x in control]}, итераций: {[x['iterations'] for x in control]}")
        after = sc.bootstrap_shutdown(c, rnd, 5)
        write_csv(os.path.join(out, "lookups_after_bootstrap_stop.csv"), after)
        say(f"9–10. Bootstrap-узел 1 остановлен; повторные lookup между ранее присоединившимися узлами: "
            f"{sum(x['success'] for x in after)}/{len(after)} успешны")
        res.update({"tables": sizes, "control_ok": ok, "control_n": len(control),
                    "after_bootstrap_ok": sum(x["success"] for x in after), "after_bootstrap_n": len(after),
                    "all_absent": all(not x["target_in_table_before"] for x in control),
                    "all_intermediate": all(x["intermediate_count"] >= 1 for x in control)})
        c.stop_all()
        collect_logs(c, out)
    return res


def sc_bootstrap(scheme: str, i: int) -> list[int]:
    from overlay.experiment.cluster import bootstrap_targets
    return bootstrap_targets(scheme, i)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nodes", type=int, default=10)
    ap.add_argument("--scheme", default="star", choices=["star", "ring"])
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("--base-port", type=int, default=9401)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    if a.nodes < 5:
        print("для этапа 2 требуется не менее 5 узлов")
        return 2
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.abspath(a.out or os.path.join(ROOT, "results", f"stage12-{stamp}"))
    os.makedirs(os.path.join(out, "stage1"), exist_ok=True)
    run_dir = os.path.join(ROOT, "runs", f"stage12-{stamp}")
    say(f"# Контрольная точка этапов 1–2 ({stamp}), версия кода {git_version()}")
    say("")
    r1 = stage1(out, run_dir, a.base_port)
    r2 = stage2(out, run_dir, a.nodes, a.scheme, a.base_port, a.seed)
    ok = (r1["ping_pong_same_rid"] and r1["burst"]["matched"] == 100 and r1["glued_ok"] and r1["split_ok"]
          and r1["oversized_rejected"] and r2["control_ok"] == r2["control_n"] and r2["all_absent"]
          and r2["all_intermediate"] and r2["after_bootstrap_ok"] == r2["after_bootstrap_n"])
    say("")
    say(f"**Итог: {'все пункты демонстрации выполнены' if ok else 'есть невыполненные пункты'}.**")
    write_json(os.path.join(out, "stage12_summary.json"), {"stage1": {k: v for k, v in r1.items() if k != "logs"},
                                                            "stage2": {k: v for k, v in r2.items()
                                                                       if k != "demo_lookup"},
                                                            "demo_lookup": r2.get("demo_lookup"), "passed": ok})
    with open(os.path.join(out, "stage12_report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(OUT_LINES) + "\n")
    print(f"\nРезультаты: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
