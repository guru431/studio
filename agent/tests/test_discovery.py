import pytest

from studio_agent import discovery


def test_file_suggestions_skip_private_files_generated_trees_and_links(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ёжик.py").write_text("ignored contents", encoding="utf-8")
    (tmp_path / ".env").write_text("must not appear")
    (tmp_path / ".env.local").write_text("must not appear")
    (tmp_path / "key.pem").write_text("must not appear")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "noise.py").write_text("ignored")
    result = discovery.suggest(tmp_path, "ежик", monotonic=lambda: 10)
    assert result == {"items": ["src/ёжик.py"], "complete": True, "more": False}
    assert discovery.suggest(tmp_path, "", monotonic=lambda: 10)["items"] == ["src/ёжик.py"]


def test_grep_finds_lines_with_the_same_exclusions_and_skips_binary_and_large_files(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\nЁЖИК = 2\n\tнайди  ежика\n", encoding="utf-8")
    (tmp_path / "src" / "bin.dat").write_bytes("\x00ежик".encode("utf-8"))
    (tmp_path / "src" / "cp1251.txt").write_bytes("ежик".encode("cp1251"))
    (tmp_path / ".env").write_text("ежик", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "noise.js").write_text("ежик", encoding="utf-8")
    monkeypatch.setattr(discovery.limits, "TEXT_MAX", 50)
    (tmp_path / "big.txt").write_text("ежик" + "x" * 60, encoding="utf-8")
    result = discovery.grep(tmp_path, "", "  Ежик ", monotonic=lambda: 10)
    assert result == {"items": [{"path": "src/a.py", "line": 2, "text": "ЁЖИК = 2"},
                                {"path": "src/a.py", "line": 3, "text": "найди ежика"}], "complete": True, "more": False}
    assert discovery.grep(tmp_path, "src", "x = ", monotonic=lambda: 10)["items"] == [
        {"path": "src/a.py", "line": 1, "text": "x = 1"}]
    assert discovery.grep(tmp_path, "", "   ", monotonic=lambda: 10) == {"items": [], "complete": True, "more": False}
    with pytest.raises(discovery.Refused, match="not_found"):
        discovery.grep(tmp_path, "src/a.py", "x", monotonic=lambda: 10)


def test_grep_bounds_hits_per_file_total_and_time(tmp_path, monkeypatch):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("hit\n" * 5, encoding="utf-8")
    monkeypatch.setattr(discovery, "GREP_PER_FILE", 2)
    result = discovery.grep(tmp_path, "", "hit", monotonic=lambda: 10)
    assert [(i["path"], i["line"]) for i in result["items"]] == [("a.txt", 1), ("a.txt", 2), ("b.txt", 1), ("b.txt", 2)] \
        or [(i["path"], i["line"]) for i in result["items"]] == [("b.txt", 1), ("b.txt", 2), ("a.txt", 1), ("a.txt", 2)]
    assert result["more"] is True and result["complete"] is True
    monkeypatch.setattr(discovery, "GREP_HITS", 3)
    result = discovery.grep(tmp_path, "", "hit", monotonic=lambda: 10)
    assert len(result["items"]) == 3 and result["more"] is True
    ticks = iter([0, 0, 0, 100, 100, 100, 100])
    assert discovery.grep(tmp_path, "", "hit", monotonic=lambda: next(ticks))["complete"] is False


def test_suggestions_report_a_bounded_scan(tmp_path, monkeypatch):
    (tmp_path / "one.py").write_text("")
    monkeypatch.setattr(discovery, "MAX_FILES", 0)
    assert discovery.suggest(tmp_path, "", monotonic=lambda: 10)["complete"] is False


async def test_search_pages_history_and_isolates_failed_backends():
    catalog = [{"chat": "one", "title": "first", "engine": "claude", "modified": 2},
               {"chat": "two", "title": "ёжик", "engine": "codex", "modified": 1}]
    calls = []

    async def history(row, before):
        calls.append((row["chat"], before))
        if row["chat"] == "two":
            raise OSError("not forwarded")
        if before is None:
            return {"items": [{"role": "assistant", "text": "recent"}], "start": 1}
        return {"items": [{"role": "user", "text": "старый ёжик", "id": "old"}], "start": 0}

    found = await discovery.search("ежик", catalog, history, monotonic=lambda: 10)
    assert calls == [("one", None), ("one", 1), ("two", None)]
    assert [row["chat"] for row in found["items"]] == ["one", "two"]
    assert found["items"][0]["snippets"][0]["id"] == "old"
    assert found["complete"] is False and found["scanned"] == 2


async def test_search_honours_global_message_budget(monkeypatch):
    monkeypatch.setattr(discovery, "MAX_MESSAGES", 1)

    async def history(*_):
        return {"items": [{"role": "assistant", "text": "match"}] * 4, "start": 0}

    found = await discovery.search("match", [{"chat": "one"}, {"chat": "two"}], history, monotonic=lambda: 10)
    assert found["scanned"] == 1 and not found["complete"] and len(found["items"]) == 1


async def test_search_cancel_propagates():
    import asyncio

    async def history(*_):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await discovery.search("x", [{"chat": "one"}], history, monotonic=lambda: 10)


async def test_file_index_shares_a_scan_and_expires_without_real_clock(tmp_path, monkeypatch):
    import asyncio

    clock, calls = [10], []

    def scan(*args, **kw):
        calls.append(args)
        return {"items": ["src/one.py", "src/two.py"], "complete": True}

    monkeypatch.setattr(discovery, "suggest", scan)
    index = discovery.FileIndex(now=lambda: clock[0])
    one, two = await asyncio.gather(index.read(tmp_path, "one"), index.read(tmp_path, "two"))
    assert one["items"] == ["src/one.py"] and two["items"] == ["src/two.py"]
    assert len(calls) == 1
    clock[0] += 31
    await index.read(tmp_path, "one")
    assert len(calls) == 2
