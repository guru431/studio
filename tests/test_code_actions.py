"""Набор быстрых действий «Агентов» виден и при своём списке учётки (`code_quick_actions_set` для страницы прежней
выкладки); подписи набора совпадают с английскими навыками."""
from pathlib import Path

import code_actions
import skills
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


def test_set_titles_have_english_matching_the_english_skills():
    """Кнопки пустого чата переводят заголовки набора (`tr`), а английский посев — навыки `skills.BUILTIN["en"]`:
    подпись одна и та же. Прежний редактор быстрых действий заменён «Настройками → Навыками»."""
    en = (ROOT / "app" / "static" / "en.js").read_text(encoding="utf-8")
    english = {b.key: b.name for b in skills.BUILTIN["en"]}
    for key, action in zip(skills.QUICK_KEYS, code_actions.DEFAULTS):
        assert f'"{action["title"]}": "{english[key]}",' in en, action["title"]
    assert not (ROOT / "app" / "static" / "js" / "code-quick-actions.js").exists()
