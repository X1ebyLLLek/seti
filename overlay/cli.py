"""Консольный интерфейс.

  python -m overlay run   [--config FILE] [--set KEY=VALUE ...] [--no-join] [--interactive] [--echo-log]
  python -m overlay shell --state-dir DIR          интерактивная консоль к запущенному узлу
  python -m overlay ctl   --state-dir DIR CMD [key=value ...]   одна команда (вывод JSON)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import signal
import sys
import threading

from .config import NodeConfig
from .node import Node
from .node.control import ControlClient, ControlServer

HELP = """Команды консоли:
  info                      состояние узла (NodeID, адрес, таблица, туннели)
  table                     содержимое k-bucket
  lookup <node_id>          итеративный FIND_NODE (поиск узла)
  get <node_id>             FIND_VALUE адресной записи узла
  publish                   переопубликовать собственную подписанную запись
  storage                   записи DHT, хранящиеся на этом узле
  send <node_id> <текст>    сообщение через туннель (end-to-end TLS)
  tunnels                   туннели: свои, ретранслируемые, конечные; восстановления
  inbox                     принятые сообщения
  history                   история отправки и приёма
  refresh                   принудительное обновление bucket
  freeze <секунды>          (испытания) узел перестаёт отвечать
  help, quit
NodeID можно указывать префиксом, если узел есть в собственной таблице или истории."""


def _parse_sets(items: list[str]) -> dict:
    out = {}
    for item in items or []:
        key, _, value = item.partition("=")
        out[key.strip()] = value.strip()
    return out


# ====================================================================== run
async def _run_node(cfg: NodeConfig, join: bool, interactive: bool, echo: bool) -> None:
    node = Node(cfg, echo_log=echo)
    stop = asyncio.Event()
    await node.start()
    control = ControlServer(node, on_shutdown=stop.set)
    await control.start()
    loop = asyncio.get_event_loop()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is not None:
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass                                        # Windows: KeyboardInterrupt ниже
    print(f"READY node_id={node.identity.node_id.hex()} listen={cfg.advertise_host}:{cfg.LISTEN_PORT} "
          f"control={cfg.CONTROL_HOST}:{control.server.sockets[0].getsockname()[1]} security={cfg.SECURITY_MODE}",
          flush=True)
    if join:
        res = await node.join()
        print(f"JOINED ok={res.ok} table={res.table_size} peers_ok={len(res.peers_ok)} "
              f"duration_ms={res.duration_ms:.0f}", flush=True)
    else:
        print("JOIN skipped (--no-join)", flush=True)
    if interactive:
        threading.Thread(target=_console_thread, args=(cfg.NODE_STATE_DIR, loop, stop), daemon=True).start()
    try:
        await stop.wait()
    finally:
        await control.stop()
        await node.stop()
        print("STOPPED", flush=True)


def _console_thread(state_dir: str, loop, stop: asyncio.Event) -> None:
    client = ControlClient.from_state_dir(state_dir)
    _repl(client)
    loop.call_soon_threadsafe(stop.set)


# ====================================================================== консоль
class _Resolver:
    def __init__(self, client: ControlClient):
        self.client = client

    def __call__(self, text: str) -> str:
        text = text.lower()
        if len(text) == 64:
            return text
        known = set()
        for b in self.client.call("table")["buckets"]:
            known.update(c["node_id"] for c in b["contacts"])
        for h in self.client.call("history", limit=500):
            known.add(h["peer"])
        matches = sorted(k for k in known if k.startswith(text))
        if len(matches) != 1:
            raise ValueError(f"префикс {text!r}: найдено {len(matches)} совпадений — укажите NodeID целиком")
        return matches[0]


def _print_lookup(r: dict) -> None:
    print(f"  успех={r['success']}  RPC={r['rpc_count']}  итераций={r['iterations']}  "
          f"длительность={r['duration_ms']:.1f} мс  цель в таблице до поиска={r.get('target_in_table_before')}")
    for rd in r["rounds"]:
        print(f"  итерация {rd['iteration']}: опрошены {', '.join(x[:8] for x in rd['queried'])}; "
              f"ответили {len(rd['responded'])}, отказ {len(rd['failed'])}, новых кандидатов {rd['new']}")
    print("  ближайшие:", ", ".join(x[:8] for x in r["closest"]))
    if r.get("timeouts"):
        print("  тайм-ауты:", ", ".join(r["timeouts"]))


def _execute(client: ControlClient, resolve, line: str) -> bool:
    parts = shlex.split(line)
    if not parts:
        return True
    cmd, args = parts[0].lower(), parts[1:]
    if cmd in ("quit", "exit"):
        return False
    if cmd == "help":
        print(HELP)
    elif cmd == "info":
        print(json.dumps(client.call("info"), indent=1, ensure_ascii=False))
    elif cmd == "table":
        t = client.call("table")
        print(f"  контактов: {t['size']}, k={t['k']}")
        for b in t["buckets"]:
            print(f"  bucket {b['index']:3d} [{b['fill']}/{t['k']}]: " +
                  ", ".join(f"{c['node_id'][:8]}@{c['host']}:{c['port']}" for c in b["contacts"]))
    elif cmd == "lookup" and args:
        _print_lookup(client.call("lookup", target=resolve(args[0])))
    elif cmd == "get" and args:
        r = client.call("get_record", node_id=resolve(args[0]))
        print(f"  найдено={r['success']}  RPC={r['rpc_count']}  итераций={r['iterations']}  от={str(r['record_from'])[:8]}")
        if r["record"]:
            print("  запись:", json.dumps(r["record"], ensure_ascii=False))
    elif cmd == "publish":
        r = client.call("publish")
        print(f"  ok={r['ok']} реплики: {', '.join(a[:8] for a in r['acks'])}")
    elif cmd == "storage":
        for rec in client.call("storage"):
            print(f"  key={rec['key'][:16]} владелец={rec['node_id'][:8]} seq={rec['sequence_number']} "
                  f"адреса={rec['addresses']}")
    elif cmd == "send" and len(args) >= 2:
        r = client.call("send", to=resolve(args[0]), text=" ".join(args[1:]))
        route = " -> ".join(x[:8] for x in r.get("route") or [])
        print(f"  статус={r['status']} попыток={r['attempts']} время={r['total_ms']:.1f} мс маршрут: {route}")
        if r.get("setup"):
            print(f"  новая сессия: {r['setup']}")
        if r.get("error") and r["status"] == "failed":
            print("  ошибка:", r["error"])
    elif cmd == "tunnels":
        t = client.call("tunnels")
        for x in t["active"]:
            print("  ", json.dumps({k: v for k, v in x.items() if k != "route_full"}, ensure_ascii=False))
        for rec in t["recoveries"]:
            print(f"   восстановление: {rec['recovery_ms']} мс, отказавший={str(rec['failed_node'])[:8]}, "
                  f"новый маршрут {' -> '.join(x[:8] for x in rec['new_route'])}")
    elif cmd == "inbox":
        for m in client.call("inbox"):
            print(f"  от {m['peer'][:8]}: {m['text']}")
    elif cmd == "history":
        for h in client.call("history"):
            extra = h.get("status") or ""
            print(f"  {h['dir']:8s} {h['peer'][:8]} {h['message_id'][:8]} {extra}")
    elif cmd == "refresh":
        print(" ", client.call("refresh"))
    elif cmd == "freeze" and args:
        print(" ", client.call("freeze", seconds=float(args[0])))
    else:
        print("неизвестная команда; help — список команд")
    return True


def _repl(client: ControlClient) -> None:
    resolve = _Resolver(client)
    print("Консоль узла. help — список команд.", flush=True)
    while True:
        try:
            line = input("overlay> ")
        except (EOFError, KeyboardInterrupt):
            break
        try:
            if not _execute(client, resolve, line):
                break
        except Exception as exc:
            print("  ошибка:", exc)
    client.close()


# ====================================================================== main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m overlay")
    sub = ap.add_subparsers(dest="mode", required=True)
    r = sub.add_parser("run", help="запустить узел")
    r.add_argument("--config")
    r.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    r.add_argument("--no-join", action="store_true")
    r.add_argument("--interactive", action="store_true")
    r.add_argument("--echo-log", action="store_true")
    s = sub.add_parser("shell", help="консоль к запущенному узлу")
    s.add_argument("--state-dir", required=True)
    c = sub.add_parser("ctl", help="одна команда управления")
    c.add_argument("--state-dir", required=True)
    c.add_argument("cmd")
    c.add_argument("args", nargs="*", metavar="KEY=VALUE")
    a = ap.parse_args(argv)

    if a.mode == "run":
        cfg = NodeConfig.load(a.config, _parse_sets(a.set))
        try:
            asyncio.run(_run_node(cfg, not a.no_join, a.interactive, a.echo_log))
        except KeyboardInterrupt:
            pass
        return 0
    client = ControlClient.from_state_dir(a.state_dir)
    if a.mode == "shell":
        _repl(client)
        return 0
    args = {}
    for item in a.args:
        k, _, v = item.partition("=")
        args[k] = int(v) if v.isdigit() and k in ("limit", "relays", "port") else v
    print(json.dumps(client.call(a.cmd, **args), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
