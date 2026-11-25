"""История DeepSeek Harness из файлов сессий: формат v4 (`.jsonl` и `.jsonl.zstd`), без запуска DSH."""
import json
import sys
from pathlib import Path

import pytest

from studio_agent import deepseek_history as history

FIXTURES = Path(__file__).parent / "fixtures" / "deepseek"
RAW = FIXTURES / "session.v4.jsonl"
PACKED = FIXTURES / "session.v4.jsonl.zstd"
SESSION = "5926d36e-7fde-492f-bcb8-1a27890b1cf8"


def compress(data: bytes) -> bytes:
    try:
        import compression.zstd as zstd
        return zstd.compress(data)
    except ImportError:
        import zstandard
        return zstandard.ZstdCompressor().compress(data)


def frames(data: bytes, size: int) -> bytes:
    """Как пишет DSH: независимые кадры — по пачке строк."""
    lines = data.splitlines(keepends=True)
    return b"".join(compress(b"".join(lines[i:i + size])) for i in range(0, len(lines), size))


def put(tmp_path, name, data, *, session=SESSION, key="--proj--"):
    folder = tmp_path / "home" / "sessions" / key / session
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return path


def shape(messages):
    """Без id: фикстуры записаны разными прогонами — id сообщений и вызовов у них свои."""
    return [(m["role"], m["text"], m.get("images"),
             [(s["tool"], s["input"], s["text"], s["error"]) for s in m["steps"]]) for m in messages]


def test_raw_and_compressed_fixture_read_the_same_messages(tmp_path):
    raw, packed = history.read(RAW), history.read(PACKED)
    assert len(raw) == len(packed) == 12
    assert shape(raw) == shape(packed)
    again = put(tmp_path, "session.v4.jsonl.zstd", frames(RAW.read_bytes(), 20))
    assert history.read(again) == raw                       # тот же файл, склейка кадров — те же сообщения


def test_user_text_assistant_text_thought_and_tool_step():
    messages = history.read(RAW)
    assert messages[0]["role"] == "user" and messages[0]["text"] == "Ответь одним словом: готово"
    assert messages[1]["role"] == "assistant" and "готово" in messages[1]["text"]
    assert [s["tool"] for s in messages[1]["steps"]] == ["Рассуждения"]
    assert messages[1]["steps"][0]["text"] == "Short answer."
    assert all(set(m) >= {"id", "role", "text", "steps", "time"} for m in messages)
    assert [m["role"] for m in messages] == ["user", "assistant"] * 6
    tool = messages[3]["steps"][1]                          # вызов MCP: аргументы разобраны, итог — текстом
    assert tool == {"id": "call_probe_12", "tool": "mcp__probe__echo", "input": {"text": "hi"},
                    "text": "echo:hi", "error": False}
    write = messages[5]["steps"][1]
    assert write["tool"] == "write" and write["input"]["file_path"] == "probe-written.txt"
    assert "Created file" in write["text"] and write["error"] is False
    assert [p["kind"] for p in messages[3]["timeline"]] == ["step", "step", "text"]


def test_failed_tool_result_is_an_error_step_and_interrupted_answer_stays():
    messages = history.read(RAW)
    denied = messages[7]["steps"][1]
    assert denied["error"] is True and "sandbox" in denied["text"]
    assert messages[9]["text"].startswith("медленно")       # ответ, оборванный отменой, остаётся в истории


def test_context_the_user_never_wrote_is_not_shown():
    text = json.dumps(history.read(RAW), ensure_ascii=False)
    for hidden in ("system-reminder", "available_skills", "model changed", "You are an AI agent"):
        assert hidden not in text
    assert not any(m["role"] == "user" and m["text"].startswith("[model") for m in history.read(RAW))


def test_overview_and_recorded_settings_without_resume():
    for path in (RAW, PACKED):
        assert history.overview(path)["title"] == "Ответь одним словом: г"     # последнее `session/title`
    assert history.overview(RAW)["created"] == 1795363689.603                 # `createdAt` заголовка, секунды
    assert history.recorded(RAW) == {"model": ("srvllm", "stub-tools"), "effort": "high", "used": 1212,
                                     "limit": 131072, "preset": "workspace-write"}
    assert history.recorded(PACKED)["model"] == ("srvllm", "stub-tools")


def test_recorded_falls_back_to_request_context_and_answer_source(tmp_path):
    header = b'{"type":"session","version":4,"id":"x","createdAt":1,"cwd":"p"}\n'
    answer = (b'{"type":"assistant/message","seq":2,"time":3,"data":{"message":{"content":[],'
              b'"source":{"kind":"model","provider":"p1","model":"m1"}},"usage":{"totalTokens":40}}}\n')
    context = b'{"type":"request/context","seq":1,"time":2,"data":{"provider":"p2","model":"m2","contextWindow":100}}\n'
    path = put(tmp_path, "session.v4.jsonl", header + answer)
    assert history.recorded(path) == {"model": ("p1", "m1"), "effort": None, "used": 40, "limit": None,
                                      "preset": None}
    reply = (b'{"type":"assistant/message","seq":2,"time":3,"data":{"message":{"content":[{"type":"text",'
             b'"text":"ok"}],"source":{"kind":"model","provider":"deepseek","model":"deepseek-chat"}}}}\n')
    path.write_bytes(header + reply)
    assert [m.get("model") for m in history.read(path)] == ["deepseek-chat"]       # модель ответа — в историю
    path.write_bytes(header + answer + context)
    assert history.recorded(path)["model"] == ("p2", "m2") and history.recorded(path)["limit"] == 100
    assert history.overview(path) == {"title": "", "created": 0.001}
    path.write_bytes(b'{"type":"session","version":9}\n')
    for read in (history.recorded, history.overview):
        with pytest.raises(history.Unsupported):
            read(path)


def test_unknown_format_version_is_unsupported(tmp_path):
    only = put(tmp_path, "session.v9.jsonl", b'{"type":"session","version":9,"id":"x"}\n')
    with pytest.raises(history.Unsupported) as error:
        history.read(only)
    assert error.value.code == "format"
    renamed = put(tmp_path, "session.v4.jsonl", b'{"type":"session","version":5}\n', session=SESSION[::-1])
    with pytest.raises(history.Unsupported) as error:
        history.read(renamed)
    assert error.value.code == "format"
    assert str(error.value) == "format"                     # в тексте исключения нет ни пути, ни содержимого


def test_corrupt_file_is_unsupported_not_crash(tmp_path):
    packed = PACKED.read_bytes()
    cases = {
        "cut-zstd": ("session.v4.jsonl.zstd", packed[:len(packed) // 2]),
        "garbage-zstd": ("session.v4.jsonl.zstd", b"not zstd at all" * 40),
        "empty": ("session.v4.jsonl", b""),
        "no-header": ("session.v4.jsonl", b'{"type":"user/message","seq":1,"data":{}}\n'),
        "bad-line": ("session.v4.jsonl", RAW.read_bytes().replace(b'"seq":3,', b"not json\n{", 1)),
        "not-object": ("session.v4.jsonl", RAW.read_bytes().split(b"\n", 1)[0] + b"\n[1]\n"),
        "bad-utf8": ("session.v4.jsonl", RAW.read_bytes().split(b"\n", 1)[0] + b'\n{"type":"x\xff"}\n'),
    }
    for number, (name, data) in enumerate(cases.values()):
        path = put(tmp_path, name, data, session=f"{number:08d}-0000-0000-0000-000000000000")
        with pytest.raises(history.Unsupported) as error:
            history.read(path)
        assert error.value.code == "corrupt"


def test_torn_tail_is_tolerated(tmp_path):
    """DSH дописывает файл по пачкам; оборванная последняя строка или кадр — не повод терять прочитанное."""
    data = RAW.read_bytes()
    cut = put(tmp_path, "session.v4.jsonl", data[:data.rindex(b'"turn/end"') + 10])
    assert [m["role"] for m in history.read(cut)] == ["user", "assistant"] * 5 + ["user"] + ["assistant"]
    packed = frames(data, 20)
    partial = put(tmp_path, "session.v4.jsonl.zstd", packed[:len(packed) - 30], session=SESSION[::-1])
    got = history.read(partial)
    assert 0 < len(got) < 12 and got == history.read(RAW)[:len(got)]


def test_images_are_references_and_blocks_collected():
    blocks = {}
    messages = history.read(RAW, image_blocks=blocks)
    assert messages[0]["images"] == [{"index": 0, "name": "Изображение 1"}]
    assert "images" not in messages[1] and "images" not in messages[2]
    assert list(blocks) == [messages[0]["id"]]
    [block] = blocks[messages[0]["id"]]
    assert block["type"] == "image" and block["attachment"]["attachmentId"].startswith("sha256:")
    assert "data" not in block                              # байты не читаются: только ссылка на вложение DSH
    assert history.read(RAW) == messages                    # без `image_blocks` ссылки те же


def test_too_large_file_is_refused_while_streaming(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "READ_MAX", 4096)
    with pytest.raises(history.Unsupported) as error:       # сам файл больше предела
        history.read(RAW)
    assert error.value.code == "too_large"
    bomb = put(tmp_path, "session.v4.jsonl.zstd", compress(b'{"type":"session","version":4}\n' + b" " * 100_000))
    assert bomb.stat().st_size < 4096                       # сжатый мал — предел сторожит распакованное
    with pytest.raises(history.Unsupported) as error:
        history.read(bomb)
    assert error.value.code == "too_large"


def test_zstandard_package_reads_what_compression_zstd_reads(monkeypatch):
    pytest.importorskip("zstandard")
    expected = history.read(PACKED)
    monkeypatch.setitem(sys.modules, "compression.zstd", None)      # как на Python 3.11–3.13
    assert history.read(PACKED) == expected


def test_missing_zstd_decoder_is_unsupported_format(monkeypatch):
    monkeypatch.setitem(sys.modules, "compression.zstd", None)
    monkeypatch.setitem(sys.modules, "zstandard", None)
    with pytest.raises(history.Unsupported) as error:
        history.read(PACKED)
    assert error.value.code == "format"


def session_tree(tmp_path, project, *, cwd=None, name="session.v4.jsonl", session=SESSION):
    header, rest = RAW.read_text(encoding="utf-8").split("\n", 1)
    record = json.loads(header)
    record["cwd"] = str(project) if cwd is None else cwd
    data = (json.dumps(record) + "\n" + rest).encode("utf-8")
    return put(tmp_path, name, compress(data) if name.endswith(".zstd") else data, session=session)


def test_locate_finds_the_session_of_this_project_only(tmp_path):
    project = tmp_path / "work" / "project"
    path = session_tree(tmp_path, project, name="session.v4.jsonl.zstd")
    home = tmp_path / "home"
    assert history.locate(home, project, SESSION) == path
    assert history.locate(home, project / ".." / "project", SESSION) == path
    with pytest.raises(history.Unsupported) as error:                         # cwd не сводится к проекту
        history.locate(home, tmp_path / "other", SESSION)
    assert error.value.code == "cwd"                                          # пояснение, не «нет такого чата»
    link = tmp_path / "link"
    try:
        link.symlink_to(project.parent, target_is_directory=True)
    except (OSError, NotImplementedError):
        pass
    else:                                                                     # тот же каталог по ссылке
        project.mkdir(parents=True, exist_ok=True)
        assert history.locate(home, link / "project", SESSION) == path
    assert history.locate(home, project, "00000000-0000-4000-8000-000000000000") is None
    assert history.read(history.locate(home, project, SESSION))[0]["role"] == "user"


def test_locate_takes_the_newest_format_and_accepts_web_ids(tmp_path):
    project = tmp_path / "project"
    session = "session-3aa7bb24-1111-4222-8333-444455556666"
    session_tree(tmp_path, project, name="session.v3.jsonl", session=session)
    session_tree(tmp_path, project, name="session.v4.jsonl", session=session)
    newest = session_tree(tmp_path, project, name="session.v9.jsonl", session=session)
    assert history.locate(tmp_path / "home", project, session) == newest
    with pytest.raises(history.Unsupported) as error:       # незнакомая версия — пояснение, не «нет чата»
        history.read(newest)
    assert error.value.code == "format"
    both = session_tree(tmp_path, project, name="session.v4.jsonl.zstd", session=SESSION)
    session_tree(tmp_path, project, name="session.v4.jsonl", session=SESSION)
    assert history.locate(tmp_path / "home", project, SESSION) == both


def test_locate_rejects_paths_outside_sessions_root(tmp_path):
    project = tmp_path / "project"
    session_tree(tmp_path, project)
    outside = tmp_path / "home" / "outside"
    outside.mkdir()
    (outside / "session.v4.jsonl").write_bytes(RAW.read_bytes())
    home = tmp_path / "home"
    for bad in ("..", "../outside", "..\\outside", "a/b", "a\\b", "C:", "C:evil", "", ".", SESSION + "/..",
                SESSION.upper(), "session-" + SESSION[:-1], None, 7):
        assert history.locate(home, project, bad) is None
    assert history.locate(tmp_path / "missing", project, SESSION) is None


def test_locate_does_not_follow_a_link_out_of_sessions(tmp_path):
    project = tmp_path / "project"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    session_tree(tmp_path, project, session="aaaaaaaa-0000-4000-8000-000000000000")
    real = tmp_path / "home" / "sessions" / "--proj--" / "aaaaaaaa-0000-4000-8000-000000000000"
    target = elsewhere / "aaaaaaaa-0000-4000-8000-000000000000"
    real.rename(target)
    try:
        real.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("ссылки недоступны")
    assert history.locate(tmp_path / "home", project, "aaaaaaaa-0000-4000-8000-000000000000") is None


def test_revision_changes_with_the_file(tmp_path):
    path = put(tmp_path, "session.v4.jsonl", b"x\n")
    first = history.revision(path)
    assert isinstance(first, tuple) and len(first) == 2 and first[1] == 2
    path.write_bytes(b"xy\n")
    assert history.revision(path) != first
