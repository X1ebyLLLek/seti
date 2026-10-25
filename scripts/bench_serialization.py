"""Сравнение форматов сериализации payload на реальных сообщениях протокола:
JSON (bytes -> base64), MessagePack (выбран), CBOR (если установлен cbor2).

    python scripts/bench_serialization.py [--out docs/serialization_benchmark.json]

Измеряется размер закодированного payload и время кодирования/декодирования
(медиана по повторам). Результат используется в PROTOCOL.md для обоснования выбора.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import msgpack  # noqa: E402

from overlay.dht import make_record  # noqa: E402
from overlay.identity import Identity  # noqa: E402
from overlay.routing import Contact  # noqa: E402


def samples() -> dict:
    ids = [Identity.ephemeral() for _ in range(4)]
    contacts = [Contact(i.node_id, i.public_key, "127.0.0.1", 9101 + n).to_wire() for n, i in enumerate(ids)]
    rec = make_record(ids[0], [("127.0.0.1", 9101)], 7, 180, 1_790_000_000_000)
    return {
        "PING": {"sender": contacts[0], "timestamp_ms": 1_790_000_000_000},
        "FIND_NODE_RESPONSE (3 контакта)": {"responder": contacts[0], "target_node_id": ids[3].node_id,
                                            "contacts": contacts[1:4]},
        "STORE_REQUEST (NodeRecord)": {"sender": contacts[1], "key": ids[2].node_id, "record": rec},
        "TUNNEL_DATA (32 КиБ)": {"tunnel_id": os.urandom(16), "seq": 12345, "data": os.urandom(32768)},
    }


def to_jsonable(o):
    if isinstance(o, bytes):
        return {"$b64": base64.b64encode(o).decode()}
    if isinstance(o, dict):
        return {k: to_jsonable(v) for k, v in sorted(o.items())}
    if isinstance(o, list):
        return [to_jsonable(x) for x in o]
    return o


def from_jsonable(o):
    if isinstance(o, dict):
        if set(o) == {"$b64"}:
            return base64.b64decode(o["$b64"])
        return {k: from_jsonable(v) for k, v in o.items()}
    if isinstance(o, list):
        return [from_jsonable(x) for x in o]
    return o


def codecs() -> dict:
    out = {
        "JSON (+base64)": (lambda o: json.dumps(to_jsonable(o), separators=(",", ":")).encode(),
                           lambda b: from_jsonable(json.loads(b))),
        "MessagePack": (lambda o: msgpack.packb(o, use_bin_type=True),
                        lambda b: msgpack.unpackb(b, raw=False)),
    }
    try:
        import cbor2
        out["CBOR (canonical)"] = (lambda o: cbor2.dumps(o, canonical=True), lambda b: cbor2.loads(b))
    except ImportError:
        pass
    return out


def timeit(fn, arg, repeat=300) -> float:
    ts = []
    for _ in range(5):
        t0 = time.perf_counter()
        for _ in range(repeat):
            fn(arg)
        ts.append((time.perf_counter() - t0) / repeat * 1e6)
    return statistics.median(ts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "serialization_benchmark.json"))
    a = ap.parse_args()
    rows = []
    for name, obj in samples().items():
        for cname, (enc, dec) in codecs().items():
            data = enc(obj)
            assert dec(data) == obj, f"{cname} roundtrip failed for {name}"
            rep = 30 if len(data) > 10000 else 300
            rows.append({"message": name, "format": cname, "bytes": len(data),
                         "encode_us": round(timeit(enc, obj, rep), 2), "decode_us": round(timeit(dec, data, rep), 2)})
    print(f"{'сообщение':34s} {'формат':18s} {'байт':>7s} {'enc, мкс':>9s} {'dec, мкс':>9s}")
    for r in rows:
        print(f"{r['message']:34s} {r['format']:18s} {r['bytes']:7d} {r['encode_us']:9.2f} {r['decode_us']:9.2f}")
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump({"python": sys.version.split()[0], "msgpack": ".".join(map(str, msgpack.version)), "rows": rows},
                  fh, indent=1, ensure_ascii=False)
    print("сохранено:", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
