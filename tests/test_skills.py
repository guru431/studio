"""Библиотека навыков (`skills.py`, спека 2026-11-25-studio-skills-status-local-models, п. 1, 4–6): типовой набор на
двух языках, посев один раз языком страницы с переносом своих быстрых действий «Агентов», область, пределы, порядок,
только свои навыки, умолчания администратора — только новым учёткам, история, выгрузка и журнал."""
import io
import json
import logging
import zipfile

import pytest
from fastapi import HTTPException

import code_actions
import skills
import state
from conftest import erase_history, make_user, signin

KEYS = ["spec", "split", "queue", "acceptance", "close-journals", "audit", "critics", "parts", "minutes"]
ANY = {"spec": ["tasks/todo/", "tasks/index.md", "NeedInfo", "Todo"], "split": ["tasks/todo/"], "parts": ["todo_write",
       "ask_user", "run_part", "7"], "queue": ["tasks/index.md", "Todo", "NeedInfo", "Errors", "Done", "tasks/done/", "git add"]}
RU = {"spec": ["финализируй", "вариант", "рекоменд"], "queue": ["подзадач", "коммит", "новый чат"], "split": ["Цель",
      "Владение", "Зависимости", "Критерии приёмки", "Команда проверки", "Ограничения"],
      "acceptance": ["прошло", "не прошло", "неприменимо"], "minutes": ["Решения", "Поручения", "Открытые вопросы"]}
EN = {"spec": ["finalize", "option", "recommend"], "queue": ["subtask", "commit", "new chat"], "split": ["Goal",
      "Ownership", "Dependencies", "Acceptance criteria", "Check command", "Constraints"],
      "acceptance": ["passed", "failed", "not applicable"], "minutes": ["Decisions", "Action items", "Open questions"]}
RU_NAMES = [b.name for b in skills.BUILTIN["ru"]]
EN_NAMES = [b.name for b in skills.BUILTIN["en"]]
BASE = {"name": "Навык", "text": "текст навыка", "scope": "agents"}


def names(items) -> list:
    return [i["name"] for i in items]


def listed(studio, path="/api/skills") -> list:
    response = studio.c.get(path)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def count(uid: int) -> int:
    return state.DB.execute("SELECT COUNT(*) FROM skills WHERE user_id = ?", (uid,)).fetchone()[0]


def test_builtin_set_has_nine_skills_with_required_content():
    for lang, words in (("ru", RU), ("en", EN)):
        items = {b.key: b for b in skills.BUILTIN[lang]}
        assert list(items) == KEYS and all(skills.KEY.fullmatch(k) for k in KEYS)
        assert {k: b.scope for k, b in items.items()} == {**dict.fromkeys(KEYS[:7], "agents"), "parts": "chat", "minutes": "chat"}
        assert [k for k in KEYS if items[k].quick] == list(skills.QUICK_KEYS) and [k for k in KEYS if items[k].parts] == ["parts"]
        assert all(0 < len(b.name) <= 80 and len(b.description) <= 200 and 0 < len(b.text) <= 100_000 for b in items.values())
        assert [(k, w) for k in KEYS for w in ANY.get(k, []) + words.get(k, []) if w not in items[k].text] == []
    ru = {b.key: b for b in skills.BUILTIN["ru"]}                    # три быстрых — слово в слово прежние действия
    assert [(ru[k].name, ru[k].text) for k in skills.QUICK_KEYS] == [(a["title"], a["text"]) for a in code_actions.DEFAULTS]
    assert [b.name for b in skills.BUILTIN["en"][4:7]] == ["Resolve issues and ideas", "Deep project audit", "Independent critics"]
    # описание — одна фраза одной строкой; тексты проходят те же проверки, что и навык из настроек
    for lang in skills.LANGS:
        for b in skills.BUILTIN[lang]:
            skills.SkillIn(name=b.name, description=b.description, text=b.text, scope=b.scope, quick=b.quick, parts=b.parts)


def test_first_list_seeds_once_in_page_language(env, schema_db, request):
    env.db_path.write_bytes(schema_db)       # настоящий первый запуск без готовых умолчаний
    studio = request.getfixturevalue("studio")
    uid = make_user(studio, "owner"); signin(studio, "owner")
    assert state.DB.execute("SELECT skills_seeded FROM users WHERE id = ?", (uid,)).fetchone()[0] is None
    items = listed(studio, "/api/skills?lang=ru")
    assert [i["origin"] for i in items] == KEYS and names(items) == RU_NAMES
    assert state.DB.execute("SELECT skills_seeded FROM users WHERE id = ?", (uid,)).fetchone()[0] is not None
    again = listed(studio, "/api/skills?lang=en")                    # засеяна — язык страницы её не меняет
    assert [i["id"] for i in again] == [i["id"] for i in items] and names(again) == RU_NAMES
    assert count(uid) == 9
    # встроенный набор в умолчаниях — один раз на базу: повторный старт службы не дублирует
    assert state.DB.execute("SELECT COUNT(*) FROM skill_defaults").fetchone()[0] == 18
    skills.seed_defaults()
    assert state.DB.execute("SELECT COUNT(*) FROM skill_defaults").fetchone()[0] == 18
    assert studio.c.get("/api/skills?lang=de").status_code == 422
    assert studio.c.get("/api/skills?scope=x").status_code == 422
    english = make_user(studio, "english")
    assert names(skills.list_for(english, "en")) == EN_NAMES


def test_scope_filter_and_text_only_by_id(studio):
    make_user(studio, "owner"); signin(studio, "owner")
    chat = listed(studio, "/api/skills?scope=chat")
    agents = listed(studio, "/api/skills?scope=agents")
    assert [i["origin"] for i in chat] == ["parts", "minutes"] and [i["origin"] for i in agents] == KEYS[:7]
    item = chat[0]
    assert set(item) == {"id", "name", "description", "scope", "quick", "parts", "origin"}
    assert item["parts"] is True and item["quick"] is False
    by_id = studio.c.get(f"/api/skills/{item['id']}").json()
    assert "text" in by_id and by_id["text"] == skills.BUILTIN["ru"][7].text
    assert {k: v for k, v in by_id.items() if k != "text"} == item
    both = studio.c.post("/api/skills", json={**BASE, "scope": "both"}).json()
    assert both["id"] in [i["id"] for i in listed(studio, "/api/skills?scope=chat")]
    assert both["id"] in [i["id"] for i in listed(studio, "/api/skills?scope=agents")]
    assert len(listed(studio)) == 10


def test_own_quick_actions_become_skills_once(studio):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    custom = {"title": "Моё", "text": "Мой текст"}
    saved = [custom, code_actions.DEFAULTS[1], code_actions.LEGACY_VIDEO_ACTION]
    assert studio.c.patch("/api/me", json={"code_quick_actions": saved}).status_code == 200
    quick = [s for s in listed(studio, "/api/skills?scope=agents") if s["quick"]]
    assert [(s["name"], s["origin"]) for s in quick] == [("Моё", None), ("Глубокий аудит проекта", "audit")]
    assert all(s["scope"] == "agents" and s["description"] == "" for s in quick)
    assert skills.get(uid, quick[0]["id"])["text"] == "Мой текст"
    assert names(listed(studio))[:2] == ["Моё", "Глубокий аудит проекта"] and len(listed(studio)) == 2 + 6
    me = studio.c.get("/api/me").json()
    assert me["code_quick_actions"] == [custom, code_actions.DEFAULTS[1]] and me["code_quick_actions_default"] is False
    assert json.loads(state.DB.execute("SELECT code_quick_actions FROM users").fetchone()[0])[0] == custom     # не стёрт
    # перенос один раз: удалённый перенесённый навык следующее чтение не возвращает
    assert studio.c.delete(f"/api/skills/{quick[0]['id']}").status_code == 204
    assert "Моё" not in names(listed(studio))
    added = studio.c.post("/api/skills/defaults").json()["items"]   # вернулись только недостающие
    assert sorted(s["origin"] for s in added if s["quick"] and s["origin"]) == ["audit", "close-journals", "critics"]
    assert len(added) == 1 + 6 + 2 and "Моё" not in names(added)


def test_unseeded_and_empty_quick_actions(studio):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    me = studio.c.get("/api/me").json()
    assert me["code_quick_actions"] == code_actions.DEFAULTS and me["code_quick_actions_default"] is True
    assert skills.quick_actions(uid) is None
    listed(studio)                                                  # посев без своего списка — три быстрых набора
    me = studio.c.get("/api/me").json()
    assert me["code_quick_actions"] == code_actions.DEFAULTS and me["code_quick_actions_default"] is False
    empty = make_user(studio, "empty")
    state.DB.execute("UPDATE users SET code_quick_actions = '[]' WHERE id = ?", (empty,))
    signin(studio, "empty")
    assert studio.c.get("/api/me").json()["code_quick_actions"] == []
    items = listed(studio)
    assert [s for s in items if s["quick"]] == [] and len(items) == 6
    assert skills.quick_actions(empty) == []
    me = studio.c.get("/api/me").json()
    assert me["code_quick_actions"] == [] and me["code_quick_actions_default"] is False


def test_create_replace_reorder_delete_and_limits(studio):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    for body in ({**BASE, "name": "x" * 81}, {**BASE, "name": "две\nстроки"}, {**BASE, "description": "d" * 201},
                 {**BASE, "description": "две\nстроки"}, {**BASE, "text": "t" * 100_001}, {**BASE, "name": ""},
                 {**BASE, "name": "   "}, {**BASE, "text": ""}, {**BASE, "text": "nul\x00"}, {**BASE, "scope": "all"},
                 {**BASE, "extra": 1}, {"name": "x", "text": "t"}, {**BASE, "quick": "maybe"}):
        r = studio.c.post("/api/skills", json=body)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "input", body
    assert listed(studio) and count(uid) == 9
    big = studio.c.post("/api/skills", json={"name": "x" * 80, "description": "d" * 200, "text": "t" * 100_000,
                                             "scope": "both", "quick": True, "parts": True})
    assert big.status_code == 201
    created = big.json()
    assert created["quick"] is True and created["parts"] is True and created["origin"] is None
    assert len(created["text"]) == 100_000
    # PUT — навык целиком: неприсланные поля — умолчания
    put = studio.c.put(f"/api/skills/{created['id']}", json={"name": "Новое", "text": "новый текст", "scope": "chat"})
    assert put.status_code == 200
    assert put.json() == {"id": created["id"], "name": "Новое", "description": "", "text": "новый текст", "scope": "chat",
                          "quick": False, "parts": False, "origin": None}
    bad = studio.c.put(f"/api/skills/{created['id']}", json={"name": "Без текста", "scope": "chat"})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "input"
    # порядок — перестановка всех своих id
    ids = [i["id"] for i in listed(studio)]
    order = studio.c.put("/api/skills/order", json={"ids": ids[::-1]})
    assert order.status_code == 200 and [i["id"] for i in order.json()["items"]] == ids[::-1]
    assert [i["id"] for i in listed(studio)] == ids[::-1]
    for wrong in (ids[1:], ids + ["foreign"], ids[:-1] + ["foreign"], ids[:-1] + ids[:1]):
        r = studio.c.put("/api/skills/order", json={"ids": wrong})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "order"
    assert [i["id"] for i in listed(studio)] == ids[::-1]
    assert studio.c.delete(f"/api/skills/{created['id']}").status_code == 204
    gone = studio.c.delete(f"/api/skills/{created['id']}")
    assert gone.status_code == 404 and gone.json()["detail"]["code"] == "gone"
    # предел учётки — 100
    while count(uid) < skills.MAX_SKILLS:
        skills.create(uid, skills.SkillIn(name="r", text="t", scope="chat"))
    full = studio.c.post("/api/skills", json=BASE)
    assert full.status_code == 422 and full.json()["detail"]["code"] == "skills"
    # «Добавить умолчания» не выходит за предел: место одно — недостающих умолчаний два
    seeded = [i for i in listed(studio) if i["origin"] is not None][:2]
    for item in seeded:
        assert studio.c.delete(f"/api/skills/{item['id']}").status_code == 204
    skills.create(uid, skills.SkillIn(name="r", text="t", scope="chat"))
    assert studio.c.post("/api/skills/defaults").status_code == 200
    assert count(uid) == skills.MAX_SKILLS


def test_foreign_skill_is_404_and_admin_sees_only_defaults(studio):
    bob = make_user(studio, "bob")
    theirs = skills.create(bob, skills.SkillIn(name="Чужой", text="чужой текст", scope="chat"))
    admin = make_user(studio, "admin", is_admin=True); signin(studio, "admin")
    for method in ("GET", "DELETE"):
        r = studio.c.request(method, f"/api/skills/{theirs['id']}")
        assert r.status_code == 404 and r.json()["detail"]["code"] == "gone", method
    r = studio.c.put(f"/api/skills/{theirs['id']}", json=BASE)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "gone"
    assert theirs["id"] not in [i["id"] for i in listed(studio)]
    assert studio.c.put("/api/skills/order", json={"ids": [theirs["id"]]}).status_code == 409
    with pytest.raises(HTTPException) as refused:
        skills.get(admin, theirs["id"])
    assert refused.value.status_code == 404
    assert skills.get(bob, theirs["id"])["text"] == "чужой текст"
    defaults = listed(studio, "/api/admin/skills?lang=ru")
    assert [i["builtin"] for i in defaults] == KEYS and {i["lang"] for i in defaults} == {"ru"}
    assert {"lang", "builtin", "position", "text", "scope", "quick", "parts"} <= set(defaults[0])
    every = listed(studio, "/api/admin/skills")
    assert len(every) == 18 and "Чужой" not in names(every) and "чужой текст" not in json.dumps(every, ensure_ascii=False)
    signin(studio, "bob")
    assert studio.c.get("/api/admin/skills").status_code == 403
    assert studio.c.post("/api/admin/skills", json={**BASE, "lang": "ru"}).status_code == 403
    assert studio.c.put(f"/api/admin/skills/{defaults[0]['id']}", json=BASE).status_code == 403
    assert studio.c.delete(f"/api/admin/skills/{defaults[0]['id']}").status_code == 403


def test_admin_defaults_reach_new_accounts_only(studio):
    old = make_user(studio, "old")
    before = skills.list_for(old, "ru")
    make_user(studio, "admin", is_admin=True); signin(studio, "admin")
    defaults = listed(studio, "/api/admin/skills?lang=ru")
    audit = next(i for i in defaults if i["builtin"] == "audit")
    minutes = next(i for i in defaults if i["builtin"] == "minutes")
    response = studio.c.post("/api/admin/skills", json={"lang": "ru", "name": "Свой набор", "description": "о своём",
                                                        "text": "текст набора", "scope": "both", "parts": True})
    assert response.status_code == 201
    created = response.json()
    assert created["builtin"] is None and created["lang"] == "ru" and created["parts"] is True
    assert studio.c.post("/api/admin/skills", json={**BASE, "lang": "de"}).status_code == 422
    patched = studio.c.put(f"/api/admin/skills/{audit['id']}", json={"name": audit["name"], "text": "новый текст аудита",
                                                                    "scope": "agents", "quick": True})
    assert patched.status_code == 200 and patched.json()["text"] == "новый текст аудита"
    assert patched.json()["builtin"] == "audit" and patched.json()["description"] == ""
    assert studio.c.put(f"/api/admin/skills/{audit['id']}", json={"name": "x"}).status_code == 422
    assert studio.c.delete(f"/api/admin/skills/{minutes['id']}").status_code == 204
    assert studio.c.delete(f"/api/admin/skills/{minutes['id']}").status_code == 404
    assert names(listed(studio, "/api/admin/skills?lang=en")) == EN_NAMES
    assert skills.list_for(old, "ru") == before                     # библиотека прежней учётки не изменилась
    fresh = make_user(studio, "fresh")
    seeded = skills.list_for(fresh, "ru")
    assert names(seeded) == [*RU_NAMES[:8], "Свой набор"]
    assert seeded[-1]["origin"] == f"default-{created['id']}"
    assert skills.get(fresh, seeded[5]["id"])["text"] == "новый текст аудита"
    # удалённое администратором встроенное умолчание повторный старт службы не возвращает
    skills.seed_defaults()
    assert "Протокол встречи" not in names(skills.defaults("ru"))


def test_history_wipe_keeps_skills_and_takeout_contains_them(studio):
    bob = make_user(studio, "bob")
    skills.create(bob, skills.SkillIn(name="Чужой", text="чужой текст", scope="chat"))
    uid = make_user(studio, "owner"); signin(studio, "owner")
    assert len(listed(studio)) == 9
    assert erase_history(studio).status_code == 200
    assert len(listed(studio)) == 9 and count(uid) == 9
    mine = studio.c.post("/api/skills", json={"name": "Мой", "description": "о моём", "text": "мой текст",
                                              "scope": "agents", "quick": True}).json()
    r = studio.c.post("/api/me/export", json={"sections": ["photo"]})
    assert r.status_code == 200, r.text
    z = studio.c.get(f"/api/me/export/{r.json()['id']}")
    settings = json.loads(zipfile.ZipFile(io.BytesIO(z.content)).read("settings.json"))
    assert [s["origin"] for s in settings["skills"]] == [*KEYS, None]
    assert settings["skills"][0]["text"] == skills.BUILTIN["ru"][0].text
    row = state.DB.execute("SELECT updated FROM skills WHERE id = ?", (mine["id"],)).fetchone()
    assert settings["skills"][-1] == {"id": mine["id"], "origin": None, "name": "Мой", "description": "о моём",
                                      "text": "мой текст", "scope": "agents", "quick": 1, "parts": 0, "position": 9,
                                      "updated": row["updated"]}
    assert "чужой текст" not in json.dumps(settings, ensure_ascii=False)


def test_skill_text_is_not_logged(studio, caplog):
    caplog.set_level(logging.INFO, logger="studio")
    make_user(studio, "admin", is_admin=True); signin(studio, "admin")
    studio.c.patch("/api/me", json={"code_quick_actions": [{"title": "secret-quick", "text": "secret-quick-text"}]})
    listed(studio)
    skill = studio.c.post("/api/skills", json={"name": "secret-name", "description": "secret-description",
                                               "text": "secret-text", "scope": "both"}).json()
    studio.c.put(f"/api/skills/{skill['id']}", json={"name": "secret-renamed", "text": "secret-edited", "scope": "chat"})
    studio.c.post("/api/skills", json={"name": "secret-bad\n", "text": "secret-refused", "scope": "chat"})
    studio.c.put("/api/skills/order", json={"ids": [i["id"] for i in listed(studio)]})
    studio.c.delete(f"/api/skills/{skill['id']}")
    studio.c.post("/api/skills/defaults")
    did = studio.c.post("/api/admin/skills", json={"lang": "en", "name": "secret-default", "text": "secret-d",
                                                   "scope": "chat"}).json()["id"]
    studio.c.put(f"/api/admin/skills/{did}", json={"name": "secret-default", "text": "secret-d2", "scope": "chat"})
    studio.c.delete(f"/api/admin/skills/{did}")
    assert skill["id"][:8] in caplog.text
    assert "secret" not in caplog.text and "Подготовить ТЗ" not in caplog.text and "tasks/" not in caplog.text
