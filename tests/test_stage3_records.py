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
        reader = nodes[5]
        await victim.stop()                     # владелец недоступен: в сети только подделки
        lk = await reader.get_record(key)
        await stop_nodes([n for n in nodes if n is not victim])
        return lk

    lk = run(go())
    assert not lk.success                       # поддельная запись не принята


def test_owner_serves_own_record_and_store_survives_restart(base):
    async def go():
        nodes = await start_nodes(base, 6)
        owner = nodes[3]
        key = record_key(owner.identity.node_id)
        rec = await nodes[1].rpc.find_value(owner.self_contact(), key)        # запрос самому владельцу
        holder = next(n for n in nodes if n.store.get(key, now_ms()))
        await holder.stop()
        from overlay.node import Node
        again = Node(holder.cfg)                                              # перезапуск с тем же каталогом
        kept = again.store.get(key, now_ms())
        again.log.close()
        await stop_nodes([n for n in nodes if n is not holder])
        return rec, kept, again, holder

    (record, contacts), kept, again, holder = run(go())
    assert record is not None and record["node_id"] is not None
    assert again.identity.node_id == holder.identity.node_id
    assert kept is not None                                                    # реплика пережила перезапуск


def test_publish_timestamps_and_lifetime(base):
    async def go():
        nodes = await start_nodes(base, 3, RECORD_TTL_S=150, REPUBLISH_INTERVAL_S=75)
        r = nodes[1].own_record
        await stop_nodes(nodes)
        return r

    r = run(go())
    assert r["expires_at"] - r["issued_at"] == 150_000
    assert abs(r["issued_at"] - int(time.time() * 1000)) < 60_000


@pytest.mark.parametrize("handoff", [True, False])
def test_record_handed_off_to_closer_node_joining_later(base, handoff):
    """Узел, присоединившийся после публикации и ставший ближайшим к ключу, получает запись
    сразу (передача записей новому контакту). handoff=False — отрицательный контроль:
    без передачи у нового узла записи нет, то есть проверка имеет силу."""
    import os
    from conftest import node_config
    from overlay.identity import Identity
    from overlay.node import Node
    from overlay.routing import xor_distance

    async def go():
        nodes = await start_nodes(base, 6)
        if not handoff:
            for n in nodes:
                n.routing.on_added.clear()
        # Новый узел должен оказаться ближе всех к ключу чьей-то записи: генерируем ключ нового
        # узла и выбираем владельца, для ключа записи которого это выполняется (~60% попыток).
        owner = None
        for attempt in range(100):
            state = os.path.join(str(base), f"late-{attempt}")
            late_id = Identity.load_or_create(state).node_id
            for n in nodes:
                key = record_key(n.identity.node_id)
                if xor_distance(late_id, key) < min(xor_distance(m.identity.node_id, key) for m in nodes):
                    owner = n
                    break
            if owner is not None:
                break
        assert owner is not None, "предусловие теста: не найден ключ нового узла, ближайший к записи"
        key = record_key(owner.identity.node_id)
        res = await owner.publish_self()
        late = Node(node_config(base, 0, NODE_STATE_DIR=state))
        await late.start()
        await late.join([f"127.0.0.1:{nodes[0].cfg.LISTEN_PORT}"])
        for _ in range(50):                            # передача идёт фоновыми STORE
            if late.store.get(key, now_ms()) or not handoff:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
        got = late.store.get(key, now_ms())
        holder = next(n for n in nodes if n is not owner and n.store.get(key, now_ms()))
        local = await holder.get_record(key)
        await stop_nodes(nodes + [late])
        return res, got, local

    res, got, local = run(go(), timeout=120)
    assert res.ok
    if handoff:
        assert got is not None
    else:
        assert got is None
    # Хранитель учитывает свою копию, но побеждает копия с наибольшим sequence_number.
    assert local.success and any(src == "local" for src, _ in local.record_copies)
    assert local.record["sequence_number"] == max(seq for _, seq in local.record_copies)
