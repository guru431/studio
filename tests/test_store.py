import json
import logging

import pytest

from studio_agent import dpapi, errors, journal, store


def test_key_is_stored_under_dpapi_and_comes_back(home):
    store.save_key(home, "secret-device-key-123")
    raw = (home / "key.bin").read_bytes()
    assert b"secret-device-key-123" not in raw and "secret-device-key-123".encode("utf-16-le") not in raw
    assert store.load_key(home) == "secret-device-key-123"


def test_a_broken_key_blob_reads_as_no_key(home):
    (home / "key.bin").write_bytes(b"\x01\x02garbage")
    assert store.load_key(home) is None


def test_no_key_file_reads_as_no_key(home):
    assert store.load_key(home) is None


def test_unprotect_refuses_a_tampered_blob():
    blob = bytearray(dpapi.protect(b"abc"))
    blob[-1] ^= 1
    with pytest.raises(OSError):
        dpapi.unprotect(bytes(blob))


def test_config_round_trip_and_defaults(home):
    assert store.load_config(home) == store.Config()
    store.save_config(home, store.Config(agent_id=7, name="Рабочий ПК", projects_root="C:\\work\\projects"))
    assert store.load_config(home).name == "Рабочий ПК"


def test_home_comes_from_the_environment_and_is_created(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_AGENT_HOME", str(tmp_path / "a" / "b"))
    assert store.home() == tmp_path / "a" / "b" and (tmp_path / "a" / "b").is_dir()


def test_write_atomic_replaces_and_leaves_no_temp_files(tmp_path):
    p = tmp_path / "x.json"
    store.write_atomic(p, b"one")
    store.write_atomic(p, b"two")
    assert p.read_bytes() == b"two" and [f.name for f in tmp_path.iterdir()] == ["x.json"]


def test_choices_remember_500_newest_chats(home):
    c = store.Choices(home)
    assert c.get("a") == (None, None)
    c.put("a", "opus", None)
    assert c.get("a") == ("opus", None)
    old = json.loads((home / "choices.json").read_text("utf-8"))
    old |= {f"c{i}": {"model": None, "effort": "high"} for i in range(499)}  # 500 чатов
    (home / "choices.json").write_text(json.dumps(old), "utf-8")
    c.put("c499", None, "high")
    assert store.Choices(home).get("a") == (None, None)  # 501-й с конца — прочь
    assert len(json.loads((home / "choices.json").read_text("utf-8"))) == 500
    assert c.get("c0") == (None, "high")
    c.put("c0", "sonnet", "low")  # свежий — в конец
    c.put("d", "opus", "max")
    assert c.get("c0") == ("sonnet", "low") and c.get("c1") == (None, None) and c.get("d") == ("opus", "max")
    (home / "choices.json").write_text("{битый", "utf-8")
    assert c.get("d") == (None, None)
    c.put("e", "opus", None)
    assert c.get("e") == ("opus", None)


def test_refused_takes_its_words_from_the_table():
    assert errors.Refused("closed").message == "компьютер закрыт"


def test_refused_keeps_explicit_words_and_extra_fields():
    r = errors.Refused("pin", left=3)
    assert (r.code, r.message, r.extra) == ("pin", "неверный PIN", {"left": 3})
    assert errors.Refused("not_found", "нет такого чата").message == "нет такого чата"


def test_journal_writes_to_agent_log(home):
    log = journal.setup(home)
    try:
        log.info("агент %d: на связи", 7)
        for h in log.handlers:
            h.flush()
        assert "агент 7: на связи" in (home / "agent.log").read_text("utf-8")
    finally:
        for h in list(log.handlers):
            h.close()
            log.removeHandler(h)


def test_journal_setup_twice_keeps_one_file_handler(home):
    log = journal.setup(home)
    try:
        journal.setup(home)
        assert len(log.handlers) == 1 and log is logging.getLogger("studio_agent")
    finally:
        for h in list(log.handlers):
            h.close()
            log.removeHandler(h)


@pytest.mark.parametrize("text", ["{", "[1]", '{"projects_root": 5}', '{"agent_id": "7"}', '{"autostart": "да"}'])
def test_a_broken_config_reads_as_defaults_and_is_left_alone(home, caplog, text):
    (home / "config.json").write_text(text, encoding="utf-8")
    assert store.load_config(home) == store.Config()
    assert (home / "config.json").read_text(encoding="utf-8") == text
    assert "config.json не читается" in caplog.text
