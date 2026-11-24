"""Набор быстрых действий «Агентов» виден и при своём списке учётки («Добавить из набора»)."""
from pathlib import Path

import code_actions
from conftest import make_user, signin

ROOT = Path(__file__).resolve().parent.parent


def test_only_the_original_video_preset_leaves_agent_actions(studio):
    make_user(studio, "owner")
    signin(studio, "owner")
    profile = studio.c.get("/api/me").json()
    assert all(action["title"] != "Скачать ролик" for field in ("code_quick_actions", "code_quick_actions_set")
               for action in profile[field])
    custom = {"title": "Скачать ролик", "text": "Моя команда для скачивания"}
    response = studio.c.patch("/api/me", json={"code_quick_actions": [code_actions.LEGACY_VIDEO_ACTION, custom]})
    assert response.status_code == 200
    assert response.json()["code_quick_actions"] == [custom]
    assert studio.c.get("/api/me").json()["code_quick_actions"] == [custom]


def test_the_set_is_on_the_page_with_a_custom_list(studio):
    make_user(studio, "owner")
    signin(studio, "owner")
    assert studio.c.get("/api/me").json()["code_quick_actions_set"] == code_actions.DEFAULTS
    saved = [{"title": "Своё", "text": "Свой текст"}]
    answer = studio.c.patch("/api/me", json={"code_quick_actions": saved}).json()
    assert answer["code_quick_actions"] == saved and answer["code_quick_actions_set"] == code_actions.DEFAULTS


def test_set_titles_and_editor_words_have_english():
    en = (ROOT / "app" / "static" / "en.js").read_text(encoding="utf-8")
    for words in [a["title"] for a in code_actions.DEFAULTS] + ["Добавить из набора"]:
        assert f'"{words}":' in en, words
    editor = (ROOT / "app" / "static" / "js" / "code-quick-actions.js").read_text(encoding="utf-8")
    assert "code_quick_actions_set" in editor and 'tr("Добавить из набора")' in editor
