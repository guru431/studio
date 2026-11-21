"""Роли диалога (`roles.py`, спека 2026-11-21-studio-source-foundation-design §3): посев библиотеки один раз и
языком страницы, умолчания администратора — только новым учёткам, только свои роли, пределы, журнал, выгрузка.
Роль в системном сообщении — `tests/test_chat_system.py` (задача B1)."""
import io
import json
import logging
import zipfile

import roles
import state
from conftest import erase_history, make_user, signin

RU = [name for _, name, _ in roles.BUILTIN["ru"]]
EN = [name for _, name, _ in roles.BUILTIN["en"]]


def names(items) -> list:
    return [i["name"] for i in items]


def test_builtin_roles_cover_six_professions_in_both_languages():
    assert RU == ["Юрист", "Медик", "Переводчик", "Редактор", "Программист", "Аналитик"]
    assert EN == ["Lawyer", "Medic", "Translator", "Editor", "Programmer", "Analyst"]
    for lang in roles.LANGS:
        keys = [key for key, _, _ in roles.BUILTIN[lang]]
        assert keys == ["lawyer", "medic", "translator", "editor", "programmer", "analyst"]
        assert all(0 < len(text) <= roles.TEXT_MAX for _, _, text in roles.BUILTIN[lang])


def test_first_list_seeds_library_once(studio):
    signin_uid = make_user(studio, "owner"); signin(studio, "owner")
    assert state.DB.execute("SELECT roles_seeded FROM users WHERE id = ?", (signin_uid,)).fetchone()[0] is None
    first = studio.c.get("/api/roles")
    assert first.status_code == 200
    items = first.json()["items"]
    assert names(items) == RU and all(i["origin"] is not None for i in items)
    assert state.DB.execute("SELECT roles_seeded FROM users WHERE id = ?", (signin_uid,)).fetchone()[0] is not None
    again = studio.c.get("/api/roles").json()["items"]
    assert [i["id"] for i in again] == [i["id"] for i in items]
    assert state.DB.execute("SELECT COUNT(*) FROM chat_roles WHERE user_id = ?", (signin_uid,)).fetchone()[0] == 6
    # встроенный набор в умолчаниях — один раз на базу: повторный старт службы не дублирует
    roles.seed_defaults()
    assert state.DB.execute("SELECT COUNT(*) FROM role_defaults").fetchone()[0] == 12


def test_emptied_library_is_not_reseeded_and_defaults_come_back_on_request(studio):
    make_user(studio, "owner"); signin(studio, "owner")
    items = studio.c.get("/api/roles").json()["items"]
    for item in items:
        assert studio.c.delete(f"/api/roles/{item['id']}").status_code == 204
    assert studio.c.get("/api/roles").json()["items"] == []
    own = studio.c.post("/api/roles", json={"name": "Свой", "text": "мой текст"}).json()
    restored = studio.c.post("/api/roles/defaults").json()["items"]
    assert names(restored) == ["Свой", *RU]
    # правленое умолчание не дублируется: «Добавить умолчания» сверяет по origin
    lawyer = next(i for i in restored if i["name"] == "Юрист")
    studio.c.patch(f"/api/roles/{lawyer['id']}", json={"name": "Мой юрист"})
    again = studio.c.post("/api/roles/defaults").json()["items"]
    assert names(again) == ["Свой", "Мой юрист", *RU[1:]] and again[0]["id"] == own["id"]


def test_seed_follows_page_language(studio):
    make_user(studio, "owner"); signin(studio, "owner")
    assert names(studio.c.get("/api/roles?lang=en").json()["items"]) == EN
    assert names(studio.c.get("/api/roles?lang=ru").json()["items"]) == EN       # засеяна — язык не меняет
    assert studio.c.get("/api/roles?lang=de").status_code == 422


def test_admin_defaults_reach_new_accounts_only(studio):
    old = make_user(studio, "old")
    before = roles.list_for(old, "ru")
    make_user(studio, "admin", is_admin=True); signin(studio, "admin")
    listed = studio.c.get("/api/admin/roles?lang=ru").json()["items"]
    assert names(listed) == RU and {i["builtin"] for i in listed} >= {"lawyer", "analyst"}
    lawyer, analyst = listed[0], listed[-1]
    created = studio.c.post("/api/admin/roles", json={"lang": "ru", "name": "Историк", "text": "о прошлом"})
    assert created.status_code == 201 and created.json()["builtin"] is None
    patched = studio.c.patch(f"/api/admin/roles/{lawyer['id']}", json={"text": "новый текст юриста"})
    assert patched.status_code == 200 and patched.json()["name"] == "Юрист"
    assert studio.c.delete(f"/api/admin/roles/{analyst['id']}").status_code == 204
    assert studio.c.delete(f"/api/admin/roles/{analyst['id']}").status_code == 404
    assert names(studio.c.get("/api/admin/roles?lang=en").json()["items"]) == EN
    # засеянная библиотека остаётся как была
    assert roles.list_for(old, "ru") == before
    fresh = make_user(studio, "fresh")
    seeded = roles.list_for(fresh, "ru")
    assert names(seeded) == [*RU[:-1], "Историк"]
    assert seeded[0]["text"] == "новый текст юриста"
    # удалённое администратором встроенное умолчание повторный старт службы не возвращает
    roles.seed_defaults()
    assert "Аналитик" not in names(roles.defaults("ru"))


def test_admin_role_routes_need_admin(studio):
    make_user(studio, "owner"); signin(studio, "owner")
    assert studio.c.get("/api/admin/roles").status_code == 403
    assert studio.c.post("/api/admin/roles", json={"lang": "ru", "name": "x", "text": "y"}).status_code == 403


def test_foreign_role_is_404(studio):
    bob = make_user(studio, "bob")
    theirs = roles.create(bob, "Чужая", "чужой текст")
    owner = make_user(studio, "owner"); signin(studio, "owner")
    assert studio.c.patch(f"/api/roles/{theirs['id']}", json={"name": "моя"}).status_code == 404
    assert studio.c.delete(f"/api/roles/{theirs['id']}").status_code == 404
    assert roles.get(owner, theirs["id"]) is None
    assert roles.get(bob, theirs["id"])["name"] == "Чужая"
    assert theirs["id"] not in [i["id"] for i in studio.c.get("/api/roles").json()["items"]]


def test_role_limits_are_422(studio):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    for body in ({"name": "", "text": "t"}, {"name": "   ", "text": "t"}, {"name": "x" * 61, "text": "t"},
                 {"name": "две\nстроки", "text": "t"}, {"name": "x", "text": ""}, {"name": "x", "text": "t" * 8001},
                 {"name": "x", "text": "nul\x00"}, {"name": "x"}, {"name": "x", "text": "t", "extra": 1}):
        r = studio.c.post("/api/roles", json=body)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "input", body
    assert studio.c.post("/api/roles", json={"name": "x" * 60, "text": "t" * 8000}).status_code == 201
    role = studio.c.get("/api/roles").json()["items"][0]
    assert studio.c.patch(f"/api/roles/{role['id']}", json={"text": "t" * 8001}).status_code == 422
    while state.DB.execute("SELECT COUNT(*) FROM chat_roles WHERE user_id = ?", (uid,)).fetchone()[0] < roles.MAX_ROLES:
        roles.create(uid, "r", "t")
    full = studio.c.post("/api/roles", json={"name": "51", "text": "t"})
    assert full.status_code == 422 and full.json()["detail"]["code"] == "roles"
    # «Добавить умолчания» не выходит за предел: место одно — недостающих умолчаний два
    for seeded in studio.c.get("/api/roles").json()["items"][1:3]:
        assert seeded["origin"] is not None
        studio.c.delete(f"/api/roles/{seeded['id']}")
    roles.create(uid, "r", "t")
    studio.c.post("/api/roles/defaults")
    assert state.DB.execute("SELECT COUNT(*) FROM chat_roles WHERE user_id = ?", (uid,)).fetchone()[0] == roles.MAX_ROLES


def test_deleted_role_leaves_dialogs_without_role_and_history_wipe_keeps_roles(studio):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    role = studio.c.post("/api/roles", json={"name": "Роль", "text": "текст"}).json()
    keep = studio.c.post("/api/roles", json={"name": "Остаётся", "text": "текст"}).json()
    state.DB.execute("INSERT INTO chats(id, user_id, title, created, updated, role_id) VALUES ('c', ?, 't', 0, 0, ?)",
                     (uid, role["id"]))
    assert studio.c.delete(f"/api/roles/{role['id']}").status_code == 204
    assert state.DB.execute("SELECT role_id FROM chats WHERE id = 'c'").fetchone()[0] is None
    assert erase_history(studio).status_code == 200
    assert roles.get(uid, keep["id"]) is not None


def test_role_text_is_not_logged(studio, caplog):
    caplog.set_level(logging.INFO, logger="studio")
    make_user(studio, "admin", is_admin=True); signin(studio, "admin")
    studio.c.get("/api/roles")
    role = studio.c.post("/api/roles", json={"name": "secret-name", "text": "secret-text"}).json()
    studio.c.patch(f"/api/roles/{role['id']}", json={"name": "secret-renamed", "text": "secret-edited"})
    studio.c.post("/api/roles", json={"name": "secret-bad\n", "text": "secret-refused"})
    studio.c.delete(f"/api/roles/{role['id']}")
    did = studio.c.post("/api/admin/roles", json={"lang": "en", "name": "secret-default", "text": "secret-d"}).json()["id"]
    studio.c.patch(f"/api/admin/roles/{did}", json={"text": "secret-d2"})
    studio.c.delete(f"/api/admin/roles/{did}")
    assert role["id"][:8] in caplog.text
    assert "secret" not in caplog.text and "Юрист" not in caplog.text


def test_takeout_contains_own_roles(studio):
    bob = make_user(studio, "bob")
    roles.create(bob, "Чужая", "чужой текст")
    make_user(studio, "owner"); signin(studio, "owner")
    mine = studio.c.post("/api/roles", json={"name": "Моя", "text": "мой текст"}).json()
    r = studio.c.post("/api/me/export", json={"sections": ["photo"]})
    assert r.status_code == 200, r.text
    z = studio.c.get(f"/api/me/export/{r.json()['id']}")
    settings = json.loads(zipfile.ZipFile(io.BytesIO(z.content)).read("settings.json"))
    assert settings["roles"] == [{"id": mine["id"], "name": "Моя", "text": "мой текст", "position": 0,
                                  "origin": None, "created": mine["created"], "updated": mine["updated"]}]
