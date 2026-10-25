"""TCP-«отвод» (tap): прозрачный ретранслятор в разрыве соединения.

Назначение:
  * запись контрольного pcap там, где нет libpcap/Npcap (Windows без Wireshark; на
    loopback сырые сокеты Windows трафик не видят). В pcap попадают ровно те байты,
    которые узлы передали в TCP; заголовки Ethernet/IPv4/TCP синтезируются с
    корректными номерами последовательности, поэтому Wireshark собирает потоки
    («Follow TCP stream») и распознаёт TLS. На Linux тот же pcap можно снять
    tcpdump -i lo (scripts/capture_linux.sh);
  * активные негативные испытания (MITM): изменение, дублирование и повтор байтов.

Узел, чей входящий трафик нужно наблюдать, объявляет адрес tap-а
(ADVERTISE_PORT), а tap пересылает байты на реальный LISTEN_PORT.
"""
from __future__ import annotations

import asyncio
import struct
import time

PCAP_MAGIC = 0xA1B2C3D4
LINKTYPE_ETHERNET = 1


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    s = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


class PcapWriter:
    def __init__(self, path: str):
        self.fh = open(path, "wb")
        self.fh.write(struct.pack("<IHHiIII", PCAP_MAGIC, 2, 4, 0, 0, 65535, LINKTYPE_ETHERNET))
        self.ip_id = 0

    def packet(self, src: tuple[str, int], dst: tuple[str, int], seq: int, ack: int, flags: int,
               payload: bytes = b"", ts: float | None = None) -> None:
        if self.fh.closed:
            return
        ts = time.time() if ts is None else ts
        sip = bytes(int(x) for x in src[0].split("."))
        dip = bytes(int(x) for x in dst[0].split("."))
        tcp_len = 20 + len(payload)
        tcp = struct.pack("!HHIIBBHHH", src[1], dst[1], seq & 0xFFFFFFFF, ack & 0xFFFFFFFF, 5 << 4, flags,
                          65535, 0, 0)
        pseudo = sip + dip + struct.pack("!BBH", 0, 6, tcp_len)
        csum = _checksum(pseudo + tcp + payload)
        tcp = tcp[:16] + struct.pack("!H", csum) + tcp[18:]
        self.ip_id = (self.ip_id + 1) & 0xFFFF
        ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + tcp_len, self.ip_id, 0x4000, 64, 6, 0, sip, dip)
        ip = ip[:10] + struct.pack("!H", _checksum(ip)) + ip[12:]
        eth = b"\x02\x00\x00\x00\x00\x02" + b"\x02\x00\x00\x00\x00\x01" + b"\x08\x00"
        frame = eth + ip + tcp + payload
        sec = int(ts)
        self.fh.write(struct.pack("<IIII", sec, int((ts - sec) * 1e6), len(frame), len(frame)))
        self.fh.write(frame)
        self.fh.flush()

    def close(self) -> None:
        self.fh.close()


SYN, ACK, PSH, FIN = 0x02, 0x10, 0x08, 0x01


class _Flow:
    """Синтез TCP-потока для pcap: рукопожатие, данные сегментами, FIN."""

    def __init__(self, pcap: PcapWriter, client: tuple[str, int], server: tuple[str, int]):
        self.pcap, self.c, self.s = pcap, client, server
        self.cseq, self.sseq = 1000, 5000
        pcap.packet(client, server, self.cseq, 0, SYN)
        pcap.packet(server, client, self.sseq, self.cseq + 1, SYN | ACK)
        self.cseq += 1
        self.sseq += 1
        pcap.packet(client, server, self.cseq, self.sseq, ACK)

    def data(self, from_client: bool, payload: bytes) -> None:
        for off in range(0, len(payload), 16000):
            chunk = payload[off:off + 16000]
            if from_client:
                self.pcap.packet(self.c, self.s, self.cseq, self.sseq, PSH | ACK, chunk)
                self.cseq += len(chunk)
            else:
                self.pcap.packet(self.s, self.c, self.sseq, self.cseq, PSH | ACK, chunk)
                self.sseq += len(chunk)

    def fin(self) -> None:
        self.pcap.packet(self.c, self.s, self.cseq, self.sseq, FIN | ACK)
        self.pcap.packet(self.s, self.c, self.sseq, self.cseq + 1, FIN | ACK)


class TcpTap:
    def __init__(self, listen_port: int, target_port: int, target_host: str = "127.0.0.1",
                 pcap_path: str | None = None, c2s=None, s2c=None, listen_host: str = "127.0.0.1"):
        """c2s/s2c: необязательные функции (bytes, conn_index) -> bytes для модификации трафика."""
        self.listen_host, self.listen_port = listen_host, listen_port
        self.target_host, self.target_port = target_host, target_port
        self.pcap = PcapWriter(pcap_path) if pcap_path else None
        self.c2s, self.s2c = c2s, s2c
        self.server = None
        self.conn_count = 0
        self.recorded: list[list[tuple[str, bytes]]] = []     # по соединениям: [("c2s"|"s2c", bytes)]
        self.total = {"c2s": 0, "s2c": 0}
        self._tasks: set = set()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._on_client, self.listen_host, self.listen_port)
        if self.listen_port == 0:
            self.listen_port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
        for t in list(self._tasks):
            t.cancel()
        if self.pcap:
            self.pcap.close()
            self.pcap = None

    async def _on_client(self, creader, cwriter) -> None:
        idx = self.conn_count
        self.conn_count += 1
        self.recorded.append([])
        try:
            sreader, swriter = await asyncio.open_connection(self.target_host, self.target_port)
        except OSError:
            cwriter.close()
            return
        peer = cwriter.get_extra_info("peername")
        client = ("10.0.0.1", peer[1] if peer else 40000 + idx)
        server = ("10.0.0.2", self.target_port)
        flow = _Flow(self.pcap, client, server) if self.pcap else None
        a = asyncio.ensure_future(self._pump(creader, swriter, "c2s", idx, flow))
        b = asyncio.ensure_future(self._pump(sreader, cwriter, "s2c", idx, flow))
        self._tasks.update((a, b))
        await asyncio.wait([a, b], return_when=asyncio.FIRST_COMPLETED)
        for t in (a, b):
            t.cancel()
            self._tasks.discard(t)
        cwriter.close()
        swriter.close()
        if flow:
            flow.fin()

    async def _pump(self, reader, writer, direction: str, idx: int, flow) -> None:
        mutate = self.c2s if direction == "c2s" else self.s2c
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    return
                if mutate is not None:
                    data = mutate(data, idx)
                if not data:
                    continue
                self.recorded[idx].append((direction, data))
                self.total[direction] += len(data)
                if flow:
                    flow.data(direction == "c2s", data)
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError, asyncio.CancelledError):
            return


def pcap_contains(path: str, needle: bytes) -> bool:
    """Поиск открытого текста в pcap (по всем байтам файла, включая заголовки)."""
    with open(path, "rb") as fh:
        return needle in fh.read()
