"""Зеркала группой копий (`mirror_groups`): перенос прежних зеркал, слияние строк одной папки, состояния пар."""
import hashlib
import json
import logging

import pytest
from cryptography.fernet import Fernet, InvalidToken

import mirror_groups as mg
import security
import state
from conftest import make_user
from fake_bridge import enroll

ROOT = "0123456789abcdef"
OTHER_ROOT = "fedcba9876543210"
SKIP = ["skip", "", 0, 0]
V1 = ["file", "1" * 64, 10, 0]
V2 = ["file", "2" * 64, 20, 1]


def computers(s, *names):
    uid = make_user(s, "owner")
    return uid, [enroll(s, name=name)[0] for name in names]


def legacy(source, project, destination, root, name, folder, *, exclude=None, baseline=None, synced=None):
    """Строка `project_mirrors`, как её пишет прежний код."""
    location = {"name": name, "folder": folder, **({"exclude": exclude} if exclude is not None else {})}
    return state.DB.execute(
        "INSERT INTO project_mirrors(source, project, destination, root, name_key, location, baseline, synced)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (source, project, destination, root, mg.name_key(name), security.encrypt(json.dumps(location)),
         baseline, synced)).lastrowid


def count(table):
    return state.DB.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_adopt_groups_mirrors_by_source_and_carries_baseline_and_exclusions(studio, caplog):
    _, (a, b, c) = computers(studio, "A", "B", "C")
    baseline = security.encrypt(json.dumps({"f": V1}))
    legacy(a, "p", b, ROOT, "Proj", "/home/u/Proj", exclude=[".venv"], baseline=baseline, synced=5.0)
    legacy(a, "p", c, ROOT, "Proj", "C:/Proj", exclude=["node_modules", ".venv"])
    with caplog.at_level(logging.INFO, logger="studio"):
        assert mg.adopt() == 2
    assert mg.adopt() == 0
    assert "Proj" not in caplog.text and ".venv" not in caplog.text

    source = mg.find(a, "p")
    group = source["grp"]
    copies = mg.members(group)
    assert [row["agent"] for row in copies] == [a, b, c]
    assert [row["computer"] for row in copies] == ["A", "B", "C"]
    assert (source["project"], source["root"]) == ("p", None)
    copy_b, copy_c = copies[1], copies[2]
    for copy in (copy_b, copy_c):
        assert copy["project"] is None and copy["root"] == ROOT and copy["name_key"] == mg.name_key("proj")
    assert mg.exclusions(group) == [".venv", "node_modules"]
    assert mg.location(copy_b) == {"name": "Proj", "folder": "/home/u/Proj"}
    assert mg.location(source) is None
    assert [row["id"] for row in mg.unlocated(a)] == [source["id"]]
    assert [row["id"] for row in mg.unlocated(b)] == [copy_b["id"]]

    pair = mg.pair(source["id"], copy_b["id"])
    assert pair.state_y == {"f": V1} and pair.state_x is None and pair.synced == 5.0
    assert pair.mark == hashlib.sha256(baseline).hexdigest()
    assert mg.pair(copy_b["id"], source["id"]).state_x == {"f": V1}
    assert mg.synced(copy_b["id"], source["id"]) == 5.0
    assert mg.pair(source["id"], copy_c["id"]) == (None, None, None, None)

    # прежний код после отката: вторая папка на C и зеркало со сменённым ключом
    legacy(a, "p", c, OTHER_ROOT, "Proj", "/x")
    stale = Fernet(Fernet.generate_key()).encrypt(json.dumps({"name": "Q", "folder": "/q"}).encode())
    state.DB.execute("INSERT INTO project_mirrors(source, project, destination, root, name_key, location)"
                     " VALUES(?,?,?,?,?,?)", (b, "q", c, ROOT, mg.name_key("Q"), stale))
    assert mg.adopt() == 2
    assert len(mg.members(group)) == 4
    unreadable = state.DB.execute("SELECT * FROM mirror_copies WHERE agent=? AND name_key=?",
                                  (c, mg.name_key("Q"))).fetchone()
    assert unreadable["location"] == stale
    with pytest.raises(InvalidToken):
        mg.location(unreadable)
    assert mg.exclusions(unreadable["grp"]) == []


def test_place_merges_two_rows_of_one_folder_and_their_groups(studio):
    uid, (a, b) = computers(studio, "A", "B")
    g1 = mg.create_group(uid)
    ap = mg.add(g1, a, "p", None, None, None)
    bf = mg.add(g1, b, None, ROOT, "F", "/f")
    g2 = mg.create_group(uid)
    bq = mg.add(g2, b, "q", None, None, None)
    ag = mg.add(g2, a, None, ROOT, "G", "/g")                      # обратное зеркало B→A
    mg.set_exclusions(g1, [".venv"])
    mg.set_exclusions(g2, ["dist", ".venv"])
    mg.save(ap, bf, {"x": V1}, {"x": V2}, 1.0)
    mg.save(bq, ag, {"y": V2}, {"y": V1}, 2.0)

    with mg.transaction():
        assert mg.place(ap, root=ROOT, name="G", folder="/home/g") == ag
    assert [row["id"] for row in state.DB.execute("SELECT id FROM mirror_copies WHERE agent=?", (a,))] == [ag]
    kept = mg.get(ag)
    assert kept["project"] == "p" and mg.location(kept) == {"name": "G", "folder": "/home/g"}
    assert count("mirror_groups") == 1
    group = kept["grp"]
    assert {row["id"] for row in mg.members(group)} == {ag, bf, bq}
    assert mg.exclusions(group) == ["dist", ".venv"]
    moved = mg.pair(ag, bf)                                         # стороны перевёрнуты: ag > bf
    assert moved == ({"x": V1}, {"x": V2}, 1.0, moved.mark)
    assert mg._kept(ag) == {"x": [V1], "y": [V1]}                   # накопленное обеих строк папки

    with mg.transaction():
        assert mg.place(bf, project="q") == bf
    members = mg.members(group)
    assert [row["id"] for row in members] == [bf, ag] and {row["agent"] for row in members} == {a, b}
    assert mg.get(bf)["project"] == "q" and mg.get(bf)["root"] == ROOT
    assert mg.pair(ag, bf)[:3] == ({"y": V1}, {"y": V2}, 2.0)       # из двух пар — более поздняя
    assert count("mirror_pairs") == 1


def test_clashes_block_merging_groups_with_copies_on_one_computer(studio):
    uid, (a, b, c) = computers(studio, "A", "B", "C")
    g1, g2 = mg.create_group(uid), mg.create_group(uid)
    mg.add(g1, a, "p", None, None, None)
    mg.add(g1, b, None, ROOT, "B1", "/b1")
    mg.add(g2, b, None, ROOT, "B2", "/b2")
    mg.add(g2, c, None, ROOT, "C", "/c")
    assert mg.clashes(g1, g2) == ["B"]
    assert mg.clashes(g1, mg.create_group(uid)) == []


def test_held_returns_every_entry_the_copy_kept_in_any_pair(studio):
    uid, (a, b, c) = computers(studio, "A", "B", "C")
    g = mg.create_group(uid)
    ca = mg.add(g, a, "p", None, None, None)
    cb = mg.add(g, b, None, ROOT, "P", "/p")
    cc = mg.add(g, c, None, ROOT, "P", "/p")
    mg.save(ca, cb, {"f": V1}, {"f": V1}, 1.0)
    mg.save(cc, ca, {}, {"f": V2, "d": SKIP}, 2.0)                  # ca — вторая сторона
    assert mg.held(ca) == {"f": {tuple(V1), tuple(V2)}}
    assert mg.held(cb) == {"f": {tuple(V1)}}
    assert mg.held(cc) == {}


def test_held_keeps_what_a_copy_held_after_a_later_sync_rewrites_its_pair_state(studio):
    """B→A переписывает состояние B в паре A–B, но B по-прежнему «держал» версию A: она в накопленном B (шифром),
    последние `HELD_MAX` различных записей на путь."""
    uid, (a, b, c) = computers(studio, "A", "B", "C")
    g = mg.create_group(uid)
    ca = mg.add(g, a, "p", None, None, None)
    cb = mg.add(g, b, None, ROOT, "P", "/p")
    cc = mg.add(g, c, None, ROOT, "P", "/p")
    mg.save(ca, cb, {"f": V1}, {"f": V1, "d": SKIP}, 1.0)          # A→B: B держит V1
    mg.save(cb, ca, {"f": V2}, {"f": V2}, 2.0)                     # B→A: пара A–B переписана
    assert mg.pair(cb, ca).state_x == {"f": V2}
    assert mg.held(cb) == {"f": {tuple(V1), tuple(V2)}}
    assert mg.held(ca) == {"f": {tuple(V1), tuple(V2)}} and mg.held(cc) == {}
    blob = state.DB.execute("SELECT held FROM mirror_copies WHERE id=?", (cb,)).fetchone()[0]
    assert json.loads(security.decrypt(blob)) == {"f": [V1, V2]}

    versions = [["file", f"{n:064x}", n, 0] for n in range(mg.HELD_MAX + 2)]
    for n, entry in enumerate(versions):
        mg.save(cb, cc, {"f": entry}, {}, 3.0 + n)
    assert mg._kept(cb) == {"f": versions[-mg.HELD_MAX:]}
    assert mg.held(cb)["f"] == {tuple(entry) for entry in versions[-mg.HELD_MAX:]} | {tuple(V2)}  # V2 — пара A–B

    # Первая сверка пополняет накопленное только когда записала пару; копии нет — ничего.
    cd = mg.add(g, c, None, OTHER_ROOT, "P", "/q")
    mg.seal(cd, ca, {"g": V1}, {"g": V1})
    mg.seal(cd, ca, {"g": V2}, {"g": V2})
    assert mg._kept(cd) == {"g": [V1]}
    mg.remove(cd)
    mg.save(ca, cd, {"h": V1}, {"h": V1}, 9.0)
    assert "h" not in mg._kept(ca)


def test_seal_writes_only_while_the_other_side_has_no_state(studio):
    uid, (a, b) = computers(studio, "A", "B")
    g = mg.create_group(uid)
    ca = mg.add(g, a, "p", None, None, None)
    cb = mg.add(g, b, None, ROOT, "P", "/p")
    mg.seal(cb, ca, {"f": V2}, {"f": V1})
    assert mg.pair(ca, cb)[:3] == ({"f": V1}, {"f": V2}, None)
    mg.seal(cb, ca, {"f": V1}, {"f": V2})
    assert mg.pair(ca, cb)[:3] == ({"f": V1}, {"f": V2}, None)


def test_revoked_computer_leaves_a_single_copy_group_that_can_grow_again(studio):
    uid, (a, b, c) = computers(studio, "A", "B", "C")
    g = mg.create_group(uid)
    ca = mg.add(g, a, "p", None, None, None)
    cb = mg.add(g, b, None, ROOT, "P", "/p")
    mg.save(ca, cb, {}, {}, 1.0)
    state.DB.execute("DELETE FROM agents WHERE id = ?", (b,))
    assert [row["id"] for row in mg.members(g)] == [ca]
    assert count("mirror_pairs") == 0
    cc = mg.add(g, c, None, ROOT, "P", "/p")
    assert [row["id"] for row in mg.members(g)] == [ca, cc]


def test_save_after_a_copy_is_gone_is_a_no_op(studio):
    uid, (a, b, c) = computers(studio, "A", "B", "C")
    g = mg.create_group(uid)
    ca = mg.add(g, a, "p", None, None, None)
    cb = mg.add(g, b, None, ROOT, "P", "/p")
    mg.add(g, c, None, ROOT, "P", "/p")
    mg.remove(cb)
    mg.save(ca, cb, {"f": V1}, {"f": V1}, 1.0)
    assert count("mirror_pairs") == 0
    assert len(mg.members(g)) == 2


def test_removing_the_last_other_copy_drops_the_group_and_its_exclusions(studio):
    uid, (a, b) = computers(studio, "A", "B")
    g = mg.create_group(uid)
    mg.add(g, a, "p", None, None, None)
    cb = mg.add(g, b, None, ROOT, "P", "/p")
    mg.set_exclusions(g, [".venv"])
    mg.remove(cb)
    assert count("mirror_groups") == count("mirror_copies") == 0
