import hashlib
import threading

import pytest

from studio_agent import pin


def test_five_wrong_pins_lock_and_survive_a_restart(home):
    p = pin.PinStore(home); p.set_pin("2468")
    assert [pin.PinStore(home).check("1111") for _ in range(4)] == [("pin", 4), ("pin", 3), ("pin", 2), ("pin", 1)]
    assert pin.PinStore(home).check("1111") == ("pin_locked", 0)
    assert pin.PinStore(home).check("2468") == ("pin_locked", 0)       # и верный не принимается
    pin.PinStore(home).reset()
    assert pin.PinStore(home).check("2468") == ("ok", 0)


def test_the_attempt_is_counted_before_scrypt(home, monkeypatch):
    p = pin.PinStore(home); p.set_pin("2468")
    monkeypatch.setattr(pin.hashlib, "scrypt", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(RuntimeError):
        p.check("1111")
    assert pin.PinStore(home).fails == 1


def test_right_pin_resets_the_count(home):
    p = pin.PinStore(home); p.set_pin("2468"); p.check("0000")
    assert p.check("2468") == ("ok", 0) and pin.PinStore(home).fails == 0


@pytest.mark.parametrize("bad", ["123", "123456789", "12a4", "١٢٣٤", " 1234"])
def test_pin_form(home, bad):
    with pytest.raises(ValueError):
        pin.PinStore(home).set_pin(bad)


def test_pin_file_holds_no_plain_hash(home):
    pin.PinStore(home).set_pin("2468")
    assert b"scrypt" not in (home / "pin.bin").read_bytes()


def test_open_session_expires_after_idle():
    t = [0.0]; s = pin.OpenSessions(now=lambda: t[0]); s.open("a" * 32)
    t[0] = 1799; assert s.touch("a" * 32)
    t[0] = 1799 + 1801; assert not s.touch("a" * 32) and not s.touch("a" * 32)


def test_open_sessions_close_and_clear():
    s = pin.OpenSessions(now=lambda: 0.0)
    s.open("a" * 32); s.open("b" * 32); s.open("c" * 32)
    s.close("a" * 32)
    assert not s.touch("a" * 32) and s.touch("b" * 32)
    s.clear()
    assert not s.touch("b" * 32) and not s.touch("c" * 32)


def test_new_pin_and_no_pin_reset_the_count(home):
    p = pin.PinStore(home); p.set_pin("2468")
    for _ in range(5):
        p.check("1111")
    assert p.locked
    p.set_pin("13579")
    assert (p.has_pin, p.fails, p.locked) == (True, 0, False) and p.check("13579") == ("ok", 0)
    p.set_pin(None)
    assert (pin.PinStore(home).has_pin, pin.PinStore(home).fails) == (False, 0)


def test_parallel_attempts_cannot_skip_the_count(home, monkeypatch):
    pin.PinStore(home).set_pin("2468")
    monkeypatch.setattr(pin.hashlib, "scrypt", lambda pw, **k: hashlib.sha256(pw + k["salt"]).digest())
    results = []
    threads = [threading.Thread(target=lambda: results.append(pin.PinStore(home).check("1111"))) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [("pin", 1), ("pin", 2), ("pin", 3), ("pin", 4)] + [("pin_locked", 0)] * 6
    assert pin.PinStore(home).fails == 5
