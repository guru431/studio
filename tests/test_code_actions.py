"""Прежний список быстрых действий «Агентов» (`users.code_quick_actions`) читает только посев навыков: ролик из
первого набора из него выпадает; подписи набора совпадают с английскими навыками; страница профиль не читает."""
import json
from pathlib import Path

import code_actions
import skills

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"


def test_only_the_original_video_preset_leaves_agent_actions():
    assert all(action["title"] != "Скачать ролик" for action in code_actions.DEFAULTS)
    custom = {"title": "Скачать ролик", "text": "Моя команда для скачивания"}
    assert code_actions.for_account(json.dumps([code_actions.LEGACY_VIDEO_ACTION, custom])) == [custom]
    assert code_actions.for_account(None) == code_actions.DEFAULTS


def test_page_no_longer_reads_quick_actions_from_the_profile():
    sources = "".join(p.read_text("utf-8") for p in (STATIC / "js").glob("*.js"))
    assert "code_quick_actions" not in sources and not (STATIC / "js" / "code-quick-actions.js").exists()


def test_set_titles_have_english_matching_the_english_skills():
    """Кнопки пустого чата переводят заголовки набора (`tr`), а английский посев — навыки `skills.BUILTIN["en"]`:
    подпись одна и та же. Прежний редактор быстрых действий заменён «Настройками → Навыками»."""
    en = (STATIC / "en.js").read_text(encoding="utf-8")
    english = {b.key: b.name for b in skills.BUILTIN["en"]}
    for key, action in zip(skills.QUICK_KEYS, code_actions.DEFAULTS):
        assert f'"{action["title"]}": "{english[key]}",' in en, action["title"]
