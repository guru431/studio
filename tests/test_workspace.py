"""Рабочее место раздела «Claude Code» (app/workspace.py; спека 2026-10-29-studio-code-workspace-design, 6.8): компьютер
панели, открытый проект и вид, вкладки каждого проекта — в учётке, только номерами; запись — действиями, к мосту не
ходит. Мост — подставной (tests/fake_bridge.py)."""
import json
import logging
import uuid

import state
from conftest import make_user, signin
from fake_bridge import FakeMachine, connect, enroll, pid

A = "0f8e2a52-5d0e-4b8e-9a51-2f3c4d5e6f70"
B = "11111111-2222-4333-8444-555555555555"
C = "22222222-3333-4444-8555-666666666666"
ALPHA, BETA = pid("alpha"), pid("beta")
URL = "/api/code/workspace"
EMPTY = {"panel": None, "place": None, "projects": []}


def owner(s) -> int:
    """Владелец вошёл, у него компьютер (не на связи)."""
    make_user(s, "owner")
    aid, _ = enroll(s)
    signin(s, "owner")
    return aid


def act(s, op, **fields):
    return s.c.patch(URL, json={"op": op, **fields})


def done(r) -> dict:
    assert r.status_code == 200, r.text
    return r.json()


def project(doc, aid, project_id) -> dict:
    [found] = [p for p in doc["projects"] if (p["aid"], p["pid"]) == (aid, project_id)]
    return found


def chat_ids(n) -> list[str]:
    return [str(uuid.UUID(int=i + 1)) for i in range(n)]


def counts() -> list[int]:
    return [state.DB.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("code_tabs", "code_place")]


def test_empty_workspace(studio):
    owner(studio)
    assert done(studio.c.get(URL)) == EMPTY


def test_open_appends_activates_and_places(studio):
    aid = owner(studio)
    done(act(studio, "open", aid=aid, pid=ALPHA, chat=A))
    doc = done(act(studio, "open", aid=aid, pid=ALPHA, chat=B))
    assert doc["projects"] == [{"aid": aid, "pid": ALPHA, "tabs": [A, B], "active": B}]
    assert doc["place"] == {"aid": aid, "pid": ALPHA, "view": "chat"} and doc["panel"] is None
    assert done(studio.c.get(URL)) == doc


def test_open_again_activates_without_duplicate(studio):
    aid = owner(studio)
    for chat in (A, B, A):
        doc = done(act(studio, "open", aid=aid, pid=ALPHA, chat=chat))
    assert project(doc, aid, ALPHA) == {"aid": aid, "pid": ALPHA, "tabs": [A, B], "active": A}


def test_twenty_first_tab_pushes_out_the_leftmost(studio):
    aid = owner(studio)
    chats = chat_ids(21)
    for chat in chats[:20]:
        doc = done(act(studio, "open", aid=aid, pid=ALPHA, chat=chat))
    assert project(doc, aid, ALPHA)["tabs"] == chats[:20]
    doc = done(act(studio, "open", aid=aid, pid=ALPHA, chat=chats[20]))
    tabs = project(doc, aid, ALPHA)
    assert tabs["tabs"] == chats[1:] and tabs["active"] == chats[20]
    # уже открытая при двадцати — только активна, никто не уходит
    doc = done(act(studio, "open", aid=aid, pid=ALPHA, chat=chats[5]))
    assert project(doc, aid, ALPHA) == {"aid": aid, "pid": ALPHA, "tabs": chats[1:], "active": chats[5]}


def test_close_moves_active_right_then_left(studio):
    aid = owner(studio)
    for chat in (A, B, C):
        done(act(studio, "open", aid=aid, pid=ALPHA, chat=chat))
    done(act(studio, "open", aid=aid, pid=ALPHA, chat=B))
    doc = done(act(studio, "close", aid=aid, pid=ALPHA, chat=B))
    assert project(doc, aid, ALPHA) == {"aid": aid, "pid": ALPHA, "tabs": [A, C], "active": C}
    doc = done(act(studio, "close", aid=aid, pid=ALPHA, chat=C))
    assert project(doc, aid, ALPHA)["tabs"] == [A] and project(doc, aid, ALPHA)["active"] == A
    doc = done(act(studio, "close", aid=aid, pid=ALPHA, chat=A))
    assert doc["projects"] == [] and doc["place"] == {"aid": aid, "pid": ALPHA, "view": "chat"}
    # неактивная уходит, активная остаётся
    for chat in (A, B, C):
        done(act(studio, "open", aid=aid, pid=ALPHA, chat=chat))
    doc = done(act(studio, "close", aid=aid, pid=ALPHA, chat=A))
    assert project(doc, aid, ALPHA) == {"aid": aid, "pid": ALPHA, "tabs": [B, C], "active": C}


def test_close_of_unknown_tab_is_a_no_op(studio):
    aid = owner(studio)
    before = done(act(studio, "open", aid=aid, pid=ALPHA, chat=A))
    assert done(act(studio, "close", aid=aid, pid=ALPHA, chat=B)) == before
    assert done(act(studio, "close", aid=aid, pid=BETA, chat=A)) == before


def test_place_and_panel(studio):
    aid = owner(studio)
    doc = done(act(studio, "place", aid=aid, pid=BETA, view="files"))
    assert doc == {"panel": None, "place": {"aid": aid, "pid": BETA, "view": "files"}, "projects": []}
    doc = done(act(studio, "panel", aid=aid))
    assert doc["panel"] == aid and doc["place"] == {"aid": aid, "pid": BETA, "view": "files"}
    doc = done(act(studio, "place", aid=aid, pid=BETA, view="git"))
    assert doc["panel"] == aid and doc["place"]["view"] == "git"
    for body in ({"op": "place", "aid": aid, "pid": BETA, "view": "x"}, {"op": "place", "aid": aid, "pid": BETA},
                 {"op": "open", "aid": aid, "pid": ALPHA}, {"op": "close", "aid": aid, "chat": A},
                 {"op": "panel"}, {"op": "panel", "aid": str(aid)}, {"op": "drop", "aid": aid}):
        assert studio.c.patch(URL, json=body).status_code == 422, body
    assert done(studio.c.get(URL)) == doc


def test_hundred_and_first_project_drops_the_oldest(studio):
    aid = owner(studio)
    for i in range(101):
        studio.clock[0] += 1
        doc = done(act(studio, "open", aid=aid, pid=pid(f"p{i}"), chat=A))
    assert len(doc["projects"]) == 100
    assert [p["pid"] for p in doc["projects"]] == [pid(f"p{i}") for i in range(100, 0, -1)]
    assert state.DB.execute("SELECT count(*) FROM code_tabs").fetchone()[0] == 100


def test_someone_elses_or_missing_computer_is_404(studio):
    make_user(studio, "friend")
    theirs, _ = enroll(studio, login="friend", name="Чужой ПК")
    owner(studio)
    for aid in (theirs, theirs + 100):
        for body in ({"op": "open", "pid": ALPHA, "chat": A}, {"op": "close", "pid": ALPHA, "chat": A},
                     {"op": "place", "pid": ALPHA, "view": "chat"}, {"op": "panel"}):
            r = studio.c.patch(URL, json={**body, "aid": aid})
            assert (r.status_code, r.json()["detail"]["code"]) == (404, "not_found"), body
    assert counts() == [0, 0]


def test_bad_pid_and_chat_are_422(studio):
    aid = owner(studio)
    for body in ({"op": "open", "pid": "ALPHA", "chat": A}, {"op": "place", "pid": ALPHA[:15], "view": "chat"},
                 {"op": "open", "pid": ALPHA, "chat": A.upper()}, {"op": "close", "pid": ALPHA, "chat": "x"}):
        assert studio.c.patch(URL, json={**body, "aid": aid}).status_code == 422, body
    assert counts() == [0, 0]


def test_workspace_never_asks_the_bridge(studio):
    aid = owner(studio)
    m = FakeMachine({"alpha": {"files": {}, "git": None, "chats": {}}})
    connect(studio, aid, m)
    for op, fields in (("open", {"pid": ALPHA, "chat": A}), ("place", {"pid": ALPHA, "view": "git"}),
                       ("panel", {}), ("close", {"pid": ALPHA, "chat": A})):
        done(act(studio, op, aid=aid, **fields))
    done(studio.c.get(URL))
    assert m.requests == []


def test_revoking_a_computer_takes_its_part(studio):
    aid = owner(studio)
    other, _ = enroll(studio, name="Второй ПК")
    done(act(studio, "open", aid=aid, pid=ALPHA, chat=A))
    done(act(studio, "open", aid=other, pid=BETA, chat=B))
    done(act(studio, "open", aid=aid, pid=ALPHA, chat=C))
    done(act(studio, "panel", aid=aid))
    assert studio.c.delete(f"/api/agents/{aid}").status_code == 204
    doc = done(studio.c.get(URL))
    assert doc == {"panel": None, "place": None, "projects": [{"aid": other, "pid": BETA, "tabs": [B], "active": B}]}


def test_erase_history_clears_it_and_a_stale_action_does_not_resurrect(studio):
    aid = owner(studio)
    done(act(studio, "open", aid=aid, pid=ALPHA, chat=A))
    done(act(studio, "open", aid=aid, pid=ALPHA, chat=B))
    done(act(studio, "panel", aid=aid))
    assert studio.c.delete("/api/history").status_code == 200
    assert done(studio.c.get(URL)) == EMPTY and counts() == [0, 0]
    doc = done(act(studio, "open", aid=aid, pid=ALPHA, chat=C))
    assert doc["projects"] == [{"aid": aid, "pid": ALPHA, "tabs": [C], "active": C}] and doc["panel"] is None


def test_deleting_the_account_takes_the_workspace(studio):
    aid = owner(studio)
    done(act(studio, "open", aid=aid, pid=ALPHA, chat=A))
    done(act(studio, "panel", aid=aid))
    state.DB.execute("DELETE FROM users WHERE login = 'owner'")
    assert counts() == [0, 0]


def test_takeout_carries_the_workspace(studio):
    import io
    import zipfile
    aid = owner(studio)
    done(act(studio, "open", aid=aid, pid=ALPHA, chat=A))
    doc = done(act(studio, "place", aid=aid, pid=ALPHA, view="files"))
    r = studio.c.post("/api/me/export", json={"sections": ["code"]})
    assert r.status_code == 200, r.text
    zf = zipfile.ZipFile(io.BytesIO(studio.c.get(f"/api/me/export/{r.json()['id']}").content))
    assert json.loads(zf.read("code_chats/workspace.json")) == doc
    assert "code_chats/workspace.json" in [e["path"] for e in json.loads(zf.read("manifest.json"))["entries"]]
    r = studio.c.post("/api/me/export", json={"sections": ["agents"]})
    zf = zipfile.ZipFile(io.BytesIO(studio.c.get(f"/api/me/export/{r.json()['id']}").content))
    assert "code_chats/workspace.json" not in zf.namelist()


def test_workspace_log_holds_no_ids(studio, caplog):
    aid = owner(studio)
    with caplog.at_level(logging.DEBUG):
        done(act(studio, "open", aid=aid, pid=ALPHA, chat=A))
        done(act(studio, "place", aid=aid, pid=ALPHA, view="git"))
        done(act(studio, "panel", aid=aid))
        done(act(studio, "close", aid=aid, pid=ALPHA, chat=A))
        done(studio.c.get(URL))
        studio.c.patch(URL, json={"op": "open", "aid": aid, "pid": BETA, "chat": "x"})
    assert not any(s in caplog.text for s in (ALPHA, BETA, A))
