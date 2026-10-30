"""Палитра окна — токены студии (спека §5.3) и читаемость пар текст/фон; Tk не поднимается."""

import pytest

from studio_agent import theme

PAPER = {"bg": "#fbfaf7", "panel": "#ffffff", "text": "#1f1d1a", "muted": "#7b766d", "border": "#e3dfd6",
         "accent": "#c96442", "on_accent": "#ffffff", "ok": "#2f9e6f", "danger": "#b42318"}
GRAPHITE = {"bg": "#1b1b1b", "panel": "#262626", "text": "#ececec", "muted": "#8f8f8f", "border": "#2a2a2a",
            "accent": "#ececec", "on_accent": "#111111", "ok": "#3ecf8e", "danger": "#f87171"}


def contrast(a: str, b: str) -> float:
    def lum(c: str) -> float:
        rgb = [int(c[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b_ = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in rgb]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b_

    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_palette_matches_studio_tokens():
    assert theme.palette(False).items() >= PAPER.items()
    assert theme.palette(True).items() >= GRAPHITE.items()
    assert theme.palette(False) is not theme.palette(False)       # копия: окно не портит общую


def test_explicit_choice_overrides_windows_and_legacy_config_follows_it(monkeypatch):
    monkeypatch.setattr(theme, "is_dark", lambda: True)
    assert theme.dark_for("auto") is True and theme.dark_for("light") is False
    monkeypatch.setattr(theme, "is_dark", lambda: False)
    assert theme.dark_for("auto") is False and theme.dark_for("dark") is True


@pytest.mark.parametrize("dark", [False, True])
def test_palette_pairs_are_readable(dark):
    c = theme.palette(dark)
    for surface in ("bg", "panel", "field"):
        assert contrast(c["text"], c[surface]) >= 7, surface
        for token in ("muted", "ok", "danger", "warn"):
            assert contrast(c[token], c[surface]) >= 3, (token, surface)
    assert contrast(c["on_accent"], c["accent"]) >= 3  # кнопка — полужирный текст; токены студии как есть
