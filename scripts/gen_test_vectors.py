"""Генерация эталонных тест-векторов (JSON) для XOR-метрики, NodeID и сортировки контактов.

Векторы вычисляются независимо от кода overlay (только hashlib и целые числа Python),
чтобы тесты сравнивали реализацию с внешним эталоном, а не саму с собой.
Запуск: python scripts/gen_test_vectors.py  -> test_vectors/*.json
"""
import hashlib
import json
import os
import random

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "test_vectors")


def h(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    rnd = random.Random(20261025)

    # 1. XOR-расстояние и индекс bucket (номер старшего бита).
    pairs = []
    specials = [
        (bytes(32), bytes(31) + b"\x01"),                      # расстояние 1 -> bucket 0
        (bytes(32), b"\x80" + bytes(31)),                      # старший бит -> bucket 255
        (b"\xff" * 32, b"\x7f" + b"\xff" * 31),                # отличаются только старшим битом
        (b"\x01" * 32, b"\x01" * 31 + b"\x03"),                # 0x02 -> bucket 1
    ]
    for _ in range(20):
        specials.append((rnd.randbytes(32), rnd.randbytes(32)))
    for a, b in specials:
        d = int.from_bytes(a, "big") ^ int.from_bytes(b, "big")
        pairs.append({"a": a.hex(), "b": b.hex(), "distance_hex": f"{d:064x}", "bucket_index": d.bit_length() - 1})

    # 2. Сортировка по XOR-расстоянию (в т.ч. случаи, где лексикографический порядок hex
    #    или знаковое сравнение дали бы другой результат).
    sorting = []
    for _ in range(5):
        target = rnd.randbytes(32)
        ids = [rnd.randbytes(32) for _ in range(8)]
        ids.append(bytes(b ^ 0xFF if i == 0 else b for i, b in enumerate(target)))   # далеко: старший байт
        ids.append(target[:-1] + bytes([target[-1] ^ 1]))                            # очень близко
        order = sorted(ids, key=lambda x: int.from_bytes(x, "big") ^ int.from_bytes(target, "big"))
        sorting.append({"target": target.hex(), "ids": [x.hex() for x in ids],
                        "expected_order": [x.hex() for x in order]})

    # 3. NodeID = SHA-256("ed25519" || 0x00 || pk) для фиксированных 32-байтовых ключей.
    #    Ключи — открытые ключи RFC 8032 (тест-векторы 1–3) и произвольные байты.
    rfc8032 = ["d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
               "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
               "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025"]
    node_ids = [{"public_key": pk, "node_id": h(b"ed25519\x00" + bytes.fromhex(pk)).hex()} for pk in rfc8032]

    with open(os.path.join(OUT, "xor_distance.json"), "w", encoding="utf-8") as fh:
        json.dump({"description": "distance = int_be(a) XOR int_be(b); bucket_index = bit_length(distance) - 1",
                   "vectors": pairs}, fh, indent=1)
    with open(os.path.join(OUT, "xor_sorting.json"), "w", encoding="utf-8") as fh:
        json.dump({"description": "ids sorted by ascending unsigned 256-bit XOR distance to target",
                   "vectors": sorting}, fh, indent=1)
    with open(os.path.join(OUT, "node_id.json"), "w", encoding="utf-8") as fh:
        json.dump({"description": "NodeID = SHA-256(ASCII('ed25519') || 0x00 || raw_public_key_32)",
                   "vectors": node_ids}, fh, indent=1)
    print("written to", OUT)


if __name__ == "__main__":
    main()
