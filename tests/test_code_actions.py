"""Набор быстрых действий «Агентов» (спека 2026-11-21-studio-source-capabilities-design, п. 24): «Скачать ролик» —
агент на компьютере пользователя скачивает ролик yt-dlp с запросом разрешения; набор виден странице и при своём
списке учётки («Добавить из набора»)."""
import re
from pathlib import Path

import code_actions
from conftest import make_user, signin

ROOT = Path(__file__).resolve().parent.parent


def video_action() -> str:
    found = [a for a in code_actions.DEFAULTS if a["title"] == "Скачать ролик"]
    assert len(found) == 1
    return found[0]["text"]


def test_download_video_action_follows_the_video_rules():
    text = video_action()
    assert "yt-dlp" in text and "разрешени" in text                  # запуск — только после согласия
    assert "avc1" in text and "m4a" in text and "--merge-output-format mp4" in text
    assert re.search(r" -- <адрес>", text)                            # адрес — после `--`
    assert "--cookies-from-browser" in text                         # cookies браузера — при отказе сайта
    assert "Загрузки" in text and "after_move:filepath" in text       # файл — в «Загрузки», в ответе — путь
    assert all(len(a["title"]) <= 80 and a["text"].strip() for a in code_actions.DEFAULTS)


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
