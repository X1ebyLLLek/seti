"""Этап 2: идентичность, XOR-метрика, k-bucket (T2.1–T2.9)."""
import asyncio
import json
import os

import pytest

from conftest import ROOT, run
from overlay.identity import Identity, canonical_encode, node_id_from_public_key
from overlay.routing import (AddStatus, Contact, InvalidContact, RoutingManager, RoutingTable, bucket_index,
                             contact_from_wire, xor_distance)

VECTORS = os.path.join(ROOT, "test_vectors")


def load(name):
    with open(os.path.join(VECTORS, name), encoding="utf-8") as fh:
        return json.load(fh)["vectors"]


# ---------------------------------------------------------------- идентичность
def test_t2_1_identity_persists_across_restart(tmp_path):
    a = Identity.load_or_create(str(tmp_path / "n1"))
    b = Identity.load_or_create(str(tmp_path / "n1"))
    assert a.created and not b.created
    assert a.node_id == b.node_id and a.public_key == b.public_key


def test_t2_2_distinct_nodes_distinct_ids(tmp_path):
    ids = {Identity.load_or_create(str(tmp_path / f"n{i}")).node_id for i in range(10)}
    assert len(ids) == 10


def test_t2_3_node_id_recomputed_from_public_key(tmp_path):
    ident = Identity.load_or_create(str(tmp_path / "n"))
    assert node_id_from_public_key(ident.public_key) == ident.node_id
    assert len(ident.node_id) == 32
    assert canonical_encode(ident.public_key) == b"ed25519\x00" + ident.public_key


def test_node_id_reference_vectors():
    for v in load("node_id.json"):
        assert node_id_from_public_key(bytes.fromhex(v["public_key"])).hex() == v["node_id"]


def test_private_key_not_in_repr_and_only_in_state_dir(tmp_path):
    ident = Identity.load_or_create(str(tmp_path / "n"))
    assert "PRIVATE" not in repr(ident)
    files = os.listdir(tmp_path / "n")
    assert "identity_ed25519.pem" in files


def wire(ident: Identity, host="127.0.0.1", port=9000):
    return {"node_id": ident.node_id, "identity_algorithm": "ed25519", "identity_public_key": ident.public_key,
            "host": host, "port": port}


def test_t2_4_contact_with_foreign_key_rejected():
    a, b = Identity.ephemeral(), Identity.ephemeral()
    w = wire(a)
    w["identity_public_key"] = b.public_key           # подмена ключа
    with pytest.raises(InvalidContact):
        contact_from_wire(w)
    w = wire(a)
    w["node_id"] = bytes(32)                          # подмена NodeID
    with pytest.raises(InvalidContact):
        contact_from_wire(w)
    assert contact_from_wire(wire(a)).node_id == a.node_id


def test_contact_structural_checks():
    a = Identity.ephemeral()
    with pytest.raises(InvalidContact):
        contact_from_wire(wire(a, host="bad host!"))
    with pytest.raises(InvalidContact):
        contact_from_wire(wire(a, port=0))
    with pytest.raises(InvalidContact):
        contact_from_wire(wire(a), local_id=a.node_id)   # ссылка на локальный узел
    w = wire(a)
    w["identity_public_key"] = b"\x00" * 31
    with pytest.raises(InvalidContact):
        contact_from_wire(w)


# ---------------------------------------------------------------- XOR-метрика
def test_t2_5_xor_reference_vectors():
    for v in load("xor_distance.json"):
        a, b = bytes.fromhex(v["a"]), bytes.fromhex(v["b"])
        assert xor_distance(a, b) == int(v["distance_hex"], 16)
        if v["bucket_index"] >= 0:
            assert bucket_index(a, b) == v["bucket_index"]


def test_t2_5_sorting_reference_vectors():
    for v in load("xor_sorting.json"):
        target = bytes.fromhex(v["target"])
        ids = [bytes.fromhex(x) for x in v["ids"]]
        got = sorted(ids, key=lambda x: xor_distance(x, target))
        assert [x.hex() for x in got] == v["expected_order"]


def test_routing_table_closest_matches_vectors():
    v = load("xor_sorting.json")[0]
    target = bytes.fromhex(v["target"])
    table = RoutingTable(b"\x00" * 31 + b"\x01", k=20)
    for x in v["ids"]:
        table.buckets[bucket_index(table.local_id, bytes.fromhex(x))].contacts.append(
            Contact(bytes.fromhex(x), b"k" * 32, "127.0.0.1", 1))
    assert [c.node_id.hex() for c in table.closest(target, 3)] == v["expected_order"][:3]


# ---------------------------------------------------------------- k-bucket
def make_contacts_in_bucket(local: bytes, index: int, n: int):
    """Контакты с XOR-расстоянием из диапазона bucket index."""
    out = []
    base = int.from_bytes(local, "big")
    for i in range(n):
        d = (1 << index) | (i + 1)
        nid = (base ^ d).to_bytes(32, "big")
        out.append(Contact(nid, bytes([i]) * 32, "127.0.0.1", 10000 + i))
    return out


def test_t2_6_insert_into_non_full_bucket_appends_to_tail():
    local = bytes(32)
    t = RoutingTable(local, k=3)
    c1, c2 = make_contacts_in_bucket(local, 100, 2)
    assert t.observe(c1, 1).status is AddStatus.ADDED
    assert t.observe(c2, 2).status is AddStatus.ADDED
    assert [c.node_id for c in t.buckets[100].contacts] == [c1.node_id, c2.node_id]


def test_t2_7_reobserved_contact_moves_to_tail():
    local = bytes(32)
    t = RoutingTable(local, k=3)
    c1, c2, c3 = make_contacts_in_bucket(local, 50, 3)
    for i, c in enumerate((c1, c2, c3)):
        t.observe(c, i)
    res = t.observe(Contact(c1.node_id, c1.identity_public_key, c1.host, c1.port), 10)
    assert res.status is AddStatus.UPDATED
    assert [c.node_id for c in t.buckets[50].contacts] == [c2.node_id, c3.node_id, c1.node_id]


def test_bucket_capacity_and_conflicts():
    local = bytes(32)
    t = RoutingTable(local, k=3)
    cs = make_contacts_in_bucket(local, 7, 4)
    for c in cs[:3]:
        t.observe(c, 1)
    res = t.observe(cs[3], 2)
    assert res.status is AddStatus.BUCKET_FULL and res.lru.node_id == cs[0].node_id
    assert len(t.buckets[7].contacts) == 3                       # ёмкость не превышена
    fake = Contact(cs[1].node_id, b"\xee" * 32, "127.0.0.1", 1)
    assert t.observe(fake, 3).status is AddStatus.CONFLICT        # тот же NodeID, другой ключ
    moved = Contact(cs[1].node_id, cs[1].identity_public_key, "127.0.0.1", 5555)
    assert t.observe(moved, 3).status is AddStatus.ADDRESS_CHANGED
    assert t.find(cs[1].node_id).port == cs[1].port              # адрес без проверки не меняется
    assert t.observe(Contact(local, b"x" * 32, "h", 1), 4).status is AddStatus.SELF


def _bucket_scenario(alive: bool):
    async def go():
        local = bytes(32)
        t = RoutingTable(local, k=3)
        pinged = []

        async def ping(contact):
            pinged.append(contact.node_id)
            await asyncio.sleep(0.01)
            return alive

        mgr = RoutingManager(t, ping)
        cs = make_contacts_in_bucket(local, 30, 4)
        for c in cs[:3]:
            mgr.observe(c)
        task = mgr.observe(cs[3])
        assert task is not None
        await mgr.idle()
        return t, cs, pinged
    return run(go())


def test_t2_8_full_bucket_live_lru_kept_new_dropped():
    t, cs, pinged = _bucket_scenario(alive=True)
    ids = [c.node_id for c in t.buckets[30].contacts]
    assert pinged == [cs[0].node_id]                              # проверен именно LRU
    assert ids == [cs[1].node_id, cs[2].node_id, cs[0].node_id]   # LRU перемещён в конец
    assert cs[3].node_id not in ids


def test_t2_9_full_bucket_dead_lru_evicted_new_added():
    t, cs, pinged = _bucket_scenario(alive=False)
    ids = [c.node_id for c in t.buckets[30].contacts]
    assert pinged == [cs[0].node_id]
    assert ids == [cs[1].node_id, cs[2].node_id, cs[3].node_id]   # новый — в конец


def test_single_failure_does_not_wipe_bucket():
    local = bytes(32)
    t = RoutingTable(local, k=3)
    cs = make_contacts_in_bucket(local, 12, 3)
    for c in cs:
        t.observe(c, 1)
    assert not t.record_failure(cs[0].node_id, max_fails=2)       # один тайм-аут — не удаление
    assert t.size() == 3
    assert t.record_failure(cs[0].node_id, max_fails=2)           # второй подряд — удаляется только он
    assert t.size() == 2
