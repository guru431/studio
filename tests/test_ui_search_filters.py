"""Быстрые периоды и точная фраза в выдаче общего поиска."""
from datetime import datetime

import pytest

from test_ui_browser import (READY, browser, go, live, live_sql, problems, search_ready, set_value, signin)  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


def test_fast_search_dates_use_the_accounts_day_and_work_with_quotes(browser, live):
    login = "search-period"
    uid = live.add_user(login)
    live_sql(live, "UPDATE users SET timezone = 'America/New_York' WHERE id = ?", (uid,))
    for name, prompt, date in [("today", "Белый кот", "2026-03-08T05:15:00+00:00"),
                               ("earlier", "Рисунок белый кот", "2026-03-03T03:12:00+00:00"),
                               ("scattered", "Белый пушистый кот", "2026-03-08T18:34:00+00:00")]:
        stamp = datetime.fromisoformat(date).timestamp()
        live_sql(live, "INSERT INTO images(id, user_id, kind, model, prompt, size, seed, state, detail, created)"
                       " VALUES (?, ?, 'generation', 'flux', ?, '1024x1024', 1, 'done', '', ?)",
                 (name, uid, prompt, stamp))
    dates = """[document.querySelector('#search-since').value, document.querySelector('#search-until').value,
      document.querySelector('.search-periods [aria-pressed=true]')?.dataset.days || '']"""
    result = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("eval", "clock", "Date.now = () => Date.parse('2026-03-09T02:30:00Z')"), *go("search"),
        ("wait", "document.querySelector('#search-q')"), set_value("#search-q", '"белый кот"'),
        ("wait", search_ready(2)), ("click", ".search-periods [data-days='1']"), ("wait", search_ready(1)),
        ("eval", "today", dates), ("click", ".search-periods [data-days='7']"), ("wait", search_ready(2)),
        ("eval", "week", dates), ("click", ".search-periods [data-days='30']"), ("wait", search_ready(2)),
        ("eval", "month", dates), ("click", "#search-reset"), ("wait", search_ready(2)), ("eval", "reset", dates)],
        width=390, mobile=True)
    assert not result.get("failed"), result
    assert result["values"]["today"] == ["2026-03-08", "2026-03-08", "1"]
    assert result["values"]["week"] == ["2026-03-02", "2026-03-08", "7"]
    assert result["values"]["month"] == ["2026-02-07", "2026-03-08", "30"]
    assert result["values"]["reset"] == ["", "", ""]
    assert not problems(result), problems(result)
