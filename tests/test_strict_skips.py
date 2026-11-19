"""Строгий режим полного уровня: пропуск из-за отсутствующего инструмента — отказ, осознанный — пропуск."""
from types import SimpleNamespace

import pytest

from conftest import DELIBERATE_SKIPS, ROOT
from tools.pytest_support import STRICT, strict_skip


def _report(reason: str, **extra):
    return pytest.TestReport("t.py::t", ("t.py", 1, "t"), {}, "skipped", ("t.py", 1, f"Skipped: {reason}"), "call",
                             **extra)


def _item(*marks):
    return SimpleNamespace(iter_markers=lambda: [m.mark for m in marks])


def test_without_strict_mode_every_skip_stays_a_skip(monkeypatch):
    monkeypatch.delenv(STRICT, raising=False)
    assert strict_skip(_report("нет bash"), DELIBERATE_SKIPS, _item()).skipped


def test_strict_mode_turns_a_missing_tool_into_a_failure(monkeypatch):
    monkeypatch.setenv(STRICT, "1")
    report = strict_skip(_report("нет WebKit: .venv/Scripts/python -m playwright install webkit"), DELIBERATE_SKIPS,
                         _item())
    assert report.failed and "нет WebKit" in str(report.longrepr) and STRICT in str(report.longrepr)


def test_strict_mode_keeps_deliberate_and_declared_skips(monkeypatch):
    monkeypatch.setenv(STRICT, "1")
    wav = "Windows WebKit cannot play an independent PCM WAV Blob; run playback on Linux/macOS"
    assert strict_skip(_report(wav), DELIBERATE_SKIPS, _item()).skipped
    declared = _item(pytest.mark.skipif(True, reason="Windows IOCP"))
    assert strict_skip(_report("Windows IOCP"), DELIBERATE_SKIPS, declared).skipped
    xfail = _report("ожидаемо")
    xfail.wasxfail = "ожидаемо"
    assert strict_skip(xfail, DELIBERATE_SKIPS, _item()).skipped


def test_strict_mode_fails_a_module_skipped_at_collection(monkeypatch):
    monkeypatch.setenv(STRICT, "1")
    report = pytest.CollectReport("t.py", "skipped", ("t.py", 1, "could not import 'playwright.sync_api'"), [])
    assert strict_skip(report, DELIBERATE_SKIPS).failed


@pytest.mark.parametrize("folder,reasons", [("tests", DELIBERATE_SKIPS), ("agent/tests", None)])
def test_every_deliberate_reason_is_still_used(folder, reasons):
    """Устаревшая строка списка молча разрешала бы пропуск, которого в наборе уже нет."""
    if reasons is None:
        import ast
        tree = ast.parse((ROOT / "agent" / "tests" / "conftest.py").read_text(encoding="utf-8"))
        reasons = next(ast.literal_eval(node.value) for node in tree.body
                       if isinstance(node, ast.Assign) and node.targets[0].id == "DELIBERATE_SKIPS")
    sources = "".join(p.read_text(encoding="utf-8") for p in (ROOT / folder).glob("test_*.py"))
    for reason in reasons:
        assert reason in sources or reason.startswith("could not import"), reason
