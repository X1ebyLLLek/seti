"""Этап 3: подписанные записи, STORE/FIND_VALUE, репликация, TTL, откат версии."""
import asyncio
import time

import pytest

from conftest import run, start_nodes, stop_nodes
from overlay.dht import InvalidRecord, make_record, record_key, validate_record
from overlay.identity import Identity
from overlay.metrics import now_ms


def rec(ident, seq=1, ttl=180, now=None, host="127.0.0.1", port=9000):
    return make_record(ident, [(host, port)], seq, ttl, now or now_ms())


def test_valid_record_passes():
    ident = Identity.ephemeral()
    validate_record(rec(ident), now_ms(), key=record_key(ident.node_id))


@pytest.mark.parametrize("field,value,code", [
    ("signature", bytes(64), "SIGNATURE"),
    ("sequence_number", 99, "SIGNATURE"),           # подписанное поле изменено после подписи
    ("addresses", [{"host": "10.0.0.1", "port": 1}], "SIGNATURE"),
    ("node_id", bytes(32), "NODE_ID"),
])
def test_tampered_record_rejected(field, value, code):
    ident = Identity.ephemeral()
    r = rec(ident)
    r[field] = value
    with pytest.raises(InvalidRecord) as e:
        validate_record(r, now_ms())
    assert e.value.code == code


def test_foreign_key_and_expiry_rejected():
    a, b = Identity.ephemeral(), Identity.ephemeral()
    with pytest.raises(InvalidRecord) as e:
        validate_record(rec(a), now_ms(), key=record_key(b.node_id))
    assert e.value.code == "KEY_MISMATCH"
    old = rec(a, ttl=120, now=now_ms() - 200_000)
    with pytest.raises(InvalidRecord) as e:
        validate_record(old, now_ms())
    assert e.value.code == "EXPIRED"
    long = rec(a, ttl=3600)
    with pytest.raises(InvalidRecord) as e:
        validate_record(long, now_ms())
    assert e.value.code == "TTL"


def test_store_and_three_replicas_survive_holder_failure(base):
    async def go():
        nodes = await start_nodes(base, 10, K_BUCKET_SIZE=3)
        owner = nodes[4]
        key = record_key(owner.identity.node_id)
        res = await owner.publish_self()
        holders = [n for n in nodes if n.store.get(key, now_ms())]
        victim = holders[0]
        await victim.stop()                                   # отказ одного хранителя
        reader = next(n for n in nodes if n is not owner and n not in holders)
        lk = await reader.get_record(key)
        rest = [n for n in nodes if n is not victim]
        await stop_nodes(rest)
        return res, holders, lk, owner

    res, holders, lk, owner = run(go(), timeout=120)
    assert res.ok and len(res.acks) == 3
    assert len(holders) >= 3
    assert lk.success and lk.record["node_id"] == owner.identity.node_id


def test_rollback_and_signature_substitution_rejected_by_store(base):
    async def go():
        nodes = await start_nodes(base, 4)
        owner, holder = nodes[1], nodes[2]
        key = record_key(owner.identity.node_id)
        contact = holder.self_contact()
        newer = rec(owner.identity, seq=10)
        older = rec(owner.identity, seq=9)
        forged = rec(owner.identity, seq=11)
        forged["signature"] = bytes(64)
        r1 = await owner.rpc.store(contact, key, newer)
        r2 = await owner.rpc.store(contact, key, older)           # откат версии
        r3 = await owner.rpc.store(contact, key, forged)          # подменённая подпись
        stored = holder.store.get(key, now_ms())
        await stop_nodes(nodes)
        return r1, r2, r3, stored

    r1, r2, r3, stored = run(go())
    assert r1 == (True, "STORED")
    assert r2 == (False, "ROLLBACK")
    assert r3 == (False, "SIGNATURE")
    assert stored["sequence_number"] == 10


def test_republish_increases_sequence(base):
    async def go():
        nodes = await start_nodes(base, 6)
        owner = nodes[3]
        key = record_key(owner.identity.node_id)
        s1 = owner.own_record["sequence_number"]
        await owner.publish_self()
        res = await owner.publish_self()
        s2 = owner.own_record["sequence_number"]
        replicas = [n for n in nodes if n.identity.node_id.hex() in res.acks]
        seqs = [n.store.get(key, now_ms())["sequence_number"] for n in replicas]
        reader = next(n for n in nodes if n is not owner)
        lk = await reader.get_record(key)
        await stop_nodes(nodes)
        return s1, s2, seqs, lk

    s1, s2, seqs, lk = run(go())
    assert s2 == s1 + 2 and len(seqs) == 3 and all(s == s2 for s in seqs)
    # устаревшие копии с меньшим номером могут оставаться до истечения TTL,
    # но FIND_VALUE выбирает запись с наибольшим sequence_number
    assert lk.success and lk.record["sequence_number"] == s2


def test_ttl_expiry_removes_record(base):
    async def go():
        nodes = await start_nodes(base, 5)
        owner, holder = nodes[1], nodes[2]
        key = record_key(owner.identity.node_id)
        short = make_record(owner.identity, [("127.0.0.1", 1)], 1000, 1, now_ms())   # TTL 1 с
        holder.store._items.pop(key, None)
        holder.store._max_seq.pop(key, None)
        ok = await owner.rpc.store(holder.self_contact(), key, short)
        present = holder.store.get(key, now_ms()) is not None
        await asyncio.sleep(1.3)
        after = holder.store.get(key, now_ms())
        await stop_nodes(nodes)
        return ok, present, after

    ok, present, after = run(go())
    assert ok[0] and present and after is None


def test_find_value_rejects_forged_record(base):
    async def go():
        nodes = await start_nodes(base, 6)
        victim = nodes[2]
        key = record_key(victim.identity.node_id)
        for n in nodes:                                       # все копии заменены подделкой
            if key in n.store._items:
                forged = dict(n.store._items[key].record)
                forged["addresses"] = [{"host": "10.6.6.6", "port": 6666}]
                n.store._items[key].record = forged
        reader = nodes[5] if nodes[5] is not victim else nodes[4]
        lk = await reader.get_record(key)
        await stop_nodes(nodes)
        return lk

    lk = run(go())
    assert not lk.success                       # поддельная запись не принята


def test_publish_timestamps_and_lifetime(base):
    async def go():
        nodes = await start_nodes(base, 3, RECORD_TTL_S=150, REPUBLISH_INTERVAL_S=75)
        r = nodes[1].own_record
        await stop_nodes(nodes)
        return r

    r = run(go())
    assert r["expires_at"] - r["issued_at"] == 150_000
    assert abs(r["issued_at"] - int(time.time() * 1000)) < 60_000
