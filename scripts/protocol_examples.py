"""Печать примеров кадров и payload для PROTOCOL.md (детерминированные демонстрационные ключи).

    python scripts/protocol_examples.py > docs/protocol_examples.md
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: E402

from overlay.identity import Identity  # noqa: E402
from overlay.protocol import Frame, MsgType, encode_frame, encode_payload  # noqa: E402
from overlay.routing import Contact  # noqa: E402


def demo_identity(seed: int) -> Identity:
    # Только для документации: фиксированный закрытый ключ, чтобы примеры воспроизводились.
    return Identity(ed25519.Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32), None, created=True)


def show(o):
    if isinstance(o, bytes):
        return f"<{len(o)} bytes {o.hex()[:16]}…>" if len(o) > 8 else f"<bytes {o.hex()}>"
    if isinstance(o, dict):
        return {k: show(v) for k, v in o.items()}
    if isinstance(o, list):
        return [show(x) for x in o]
    return o


def main():
    a, b, c, t = (demo_identity(i) for i in (1, 2, 3, 4))
    ca = Contact(a.node_id, a.public_key, "127.0.0.1", 9101).to_wire()
    cb = Contact(b.node_id, b.public_key, "127.0.0.1", 9102).to_wire()
    cc = Contact(c.node_id, c.public_key, "127.0.0.1", 9103).to_wire()
    rid = bytes.fromhex("5f1c0e2a9b7d4c3e8a6f1d2b3c4e5f60")
    examples = [
        (MsgType.PING, {"sender": ca, "timestamp_ms": 1792957896766}),
        (MsgType.PONG, {"responder": cb, "ping_timestamp_ms": 1792957896766, "responder_timestamp_ms": 1792957896768}),
        (MsgType.FIND_NODE_REQUEST, {"sender": ca, "target_node_id": t.node_id}),
        (MsgType.FIND_NODE_RESPONSE, {"responder": cb, "target_node_id": t.node_id, "contacts": [cc]}),
        (MsgType.ERROR, {"code": "FRAME_TOO_LARGE", "message": "payload_length 65537 > 65536"}),
    ]
    print("# Примеры кадров (сгенерированы scripts/protocol_examples.py)\n")
    for mtype, body in examples:
        payload = encode_payload(mtype, body)
        frame = encode_frame(Frame(int(mtype), rid, payload))
        print(f"## {mtype.name} (0x{int(mtype):02X})\n")
        print("Логическое содержимое payload:\n\n```json")
        print(json.dumps(show(body), ensure_ascii=False, indent=1))
        print("```\n")
        print(f"Кадр: заголовок 24 байта + payload {len(payload)} байт (MessagePack). Заголовок:\n")
        print("```text")
        h = frame[:24]
        print(f"{h[0]:02x} | {h[1]:02x} | {h[2:4].hex()} | {h[4:20].hex()} | {h[20:24].hex()}")
        print("ver  type flags request_id                         payload_length")
        print("```\n")
        print("Начало payload (hex):\n\n```text\n" + payload[:64].hex(" ") + (" …" if len(payload) > 64 else "")
              + "\n```\n")


if __name__ == "__main__":
    main()
