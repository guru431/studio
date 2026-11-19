"""Имена уровня модуля в тестах не переопределяются: второй `def visibility` в test_ui_browser.py молча подменил
помощник `hide()`/`show()` для всех сценариев скрытой страницы (2026-11-19)."""
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: определение без отступа — уровень модуля (строки внутри тройных кавычек тестов так не начинаются)
TOP = re.compile(r"^(?:async\s+def|def|class)\s+(\w+)", re.M)


def _redefined(path: Path) -> list:
    names = Counter(TOP.findall(path.read_text(encoding="utf-8")))
    return sorted(name for name, count in names.items() if count > 1)


def test_test_modules_define_each_top_level_function_once():
    files = [*(ROOT / "tests").glob("*.py"), *(ROOT / "agent" / "tests").glob("*.py")]
    assert {path.name: names for path in files if (names := _redefined(path))} == {}


def test_redefinition_is_found(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text("def visibility(state):\n    pass\n\n\ndef visibility(hidden):\n    def inner():\n        pass\n",
                      encoding="utf-8")
    assert _redefined(sample) == ["visibility"]
