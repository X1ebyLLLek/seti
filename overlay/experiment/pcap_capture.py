"""Контрольный pcap: сеть из нескольких узлов, перед КАЖДЫМ узлом стоит tap, все
межузловые TCP-потоки (DHT, построение туннеля, ячейки туннеля) пишутся в один pcap.
Затем в pcap ищется секретный маркер сообщения (должен отсутствовать) и — как
положительный контроль прибора — открытые служебные строки, которые в режиме plain
обязаны находиться."""
from __future__ import annotations

import asyncio
import json
import os

from ..config import NodeConfig
from ..node import Node
from .tap import PcapWriter, TcpTap, pcap_contains

MARKER = "CONFIDENTIAL-MESSAGE-MARKER-7f3a"


async def capture(out_dir: str, state_root: str, mode: str = "tls", n: int = 6, base_port: int = 9700) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    pcap_path = os.path.join(out_dir, f"control_{mode}.pcap")
    writer = PcapWriter(pcap_path)
    nodes, taps = [], []
    for i in range(n):
        real, public = base_port + 2 * i, base_port + 2 * i + 1
        cfg = NodeConfig(NODE_STATE_DIR=os.path.join(state_root, f"{mode}-node-{i + 1:02d}"), LISTEN_PORT=real,
                         ADVERTISE_PORT=public, SECURITY_MODE=mode, LOG_LEVEL="INFO",
                         BOOTSTRAP_PEERS=[] if i == 0 else [f"127.0.0.1:{base_port + 1}"])
        tap = TcpTap(public, real)
        tap.pcap = writer                               # общий файл для всех потоков
        await tap.start()
        node = Node(cfg)
        await node.start()
        nodes.append(node)
        taps.append(tap)
    for node in nodes:
        await node.join()
    src, dst = nodes[1], nodes[-1]
    result = await src.send_message(dst.identity.node_id, MARKER)
    second = await src.send_message(dst.identity.node_id, MARKER + "-2")
    await asyncio.sleep(0.3)
    inbox = [m["text"] for m in dst.messaging.inbox]
    for node in nodes:
        await node.stop()
    for tap in taps:
        tap.pcap = None                                 # общий writer закрывается один раз ниже
        await tap.stop()
    await asyncio.sleep(0.1)                            # завершение потоков tap (FIN в pcap)
    writer.close()
    size = os.path.getsize(pcap_path)
    summary = {
        "mode": mode, "pcap": os.path.basename(pcap_path), "pcap_bytes": size, "nodes": n,
        "message_status": [result["status"], second["status"]],
        "route": [x[:8] for x in result.get("route") or []],
        "delivered_text_matches": inbox == [MARKER, MARKER + "-2"],
        "marker_in_pcap": pcap_contains(pcap_path, MARKER.encode()),
        # Положительный контроль: в plain эти служебные поля идут открыто.
        "control_strings_in_pcap": {s: pcap_contains(pcap_path, s.encode())
                                    for s in ("identity_public_key", "target_node_id", "127.0.0.1")},
        "bytes_c2s": sum(t.total["c2s"] for t in taps), "bytes_s2c": sum(t.total["s2c"] for t in taps),
        "flows": sum(t.conn_count for t in taps),
    }
    with open(os.path.join(out_dir, f"pcap_check_{mode}.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1)
    return summary
