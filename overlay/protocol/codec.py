"""Сериализация payload: MessagePack с ограничениями размеров и канонизацией.

Выбор MessagePack обоснован в PROTOCOL.md (сравнение с JSON и CBOR, скрипт
scripts/bench_serialization.py). Безопасность декодирования:
  * распаковываются только данные (map/array/str/bin/int/bool/nil), ext-типы
    отклоняются — никакой десериализации объектов языка (в отличие от pickle);
  * длины строк, bin, массивов и map ограничены на уровне распаковщика;
  * глубина вложенности и допустимые типы проверяются отдельно;
  * ключи map — только строки.
Детерминированность: ключи map сортируются при кодировании, целые числа msgpack
всегда кодирует минимальным представлением, поэтому одинаковые данные дают
одинаковые байты на всех узлах.
"""
from __future__ import annotations

import msgpack

MAX_DEPTH = 6
LIMITS = dict(max_str_len=16 * 1024, max_bin_len=65536, max_array_len=64, max_map_len=32)


class DecodeError(Exception):
    pass


def _reject_ext(code, data):
    raise DecodeError(f"ext type {code} is not allowed")


def canonical(obj):
    if isinstance(obj, dict):
        return {k: canonical(obj[k]) for k in sorted(obj)}
    if isinstance(obj, (list, tuple)):
        return [canonical(x) for x in obj]
    return obj


def pack(obj) -> bytes:
    return msgpack.packb(canonical(obj), use_bin_type=True)


def _check_tree(obj, depth=0):
    if depth > MAX_DEPTH:
        raise DecodeError("nesting too deep")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise DecodeError("map keys must be strings")
            _check_tree(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _check_tree(v, depth + 1)
    elif obj is None or isinstance(obj, (bool, int, str, bytes)):
        return
    else:
        raise DecodeError(f"type {type(obj).__name__} is not allowed")


def unpack(data: bytes):
    try:
        obj = msgpack.unpackb(data, raw=False, strict_map_key=True, use_list=True,
                              ext_hook=_reject_ext, timestamp=0, **LIMITS)
    except DecodeError:
        raise
    except Exception as exc:  # msgpack поднимает разные исключения на мусорных данных
        raise DecodeError(f"malformed payload: {type(exc).__name__}") from None
    _check_tree(obj)
    return obj
