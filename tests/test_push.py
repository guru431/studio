"""Web Push раздела «Claude Code» (app/push.py): ключ VAPID в базе шифром, шифрование RFC 8291 (aes128gcm),
подпись VAPID ES256, подписка устройства на сессию, поводы из `bridge.deliver` — только без наблюдателей чата."""
import asyncio
import base64
import dataclasses
import hashlib
import hmac
import json
import logging
import os

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import push
import security
import state
from conftest import T0, make_user, signin
from fake_bridge import FakeMachine, connect, emit, enroll, pid
from test_chat import events_of

A = "0f8e2a52-5d0e-4b8e-9a51-2f3c4d5e6f70"
ALPHA = pid("alpha")
FCM = "https://fcm.googleapis.com/fcm/send/abc-endpoint-secret"


def b64(text: str) -> bytes:
    """base64url из RFC (пробелы переноса строк убраны)."""
    text = "".join(text.split())
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def test_encrypt_matches_rfc8291_appendix_a():
    # RFC 8291, Appendix A (входы) и раздел 5 (итог, на который ссылается Appendix A) — дословно
    plaintext = b64("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24")
    as_private = b64("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw")
    ua_public = b64("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV- JvLexhqUzORcx aOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4")
    salt = b64("DGv6ra1nlYgDCS1FRnbzlw")
    auth_secret = b64("BTBZMqHH6r4Tts7J_aSIgg")
    result = ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
              "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
              "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")
    assert plaintext == b"When I grow up, I want to be a watermelon"
    key = ec.derive_private_key(int.from_bytes(as_private, "big"), ec.SECP256R1())
    out = push.encrypt(plaintext, ua_public, auth_secret, salt=salt, private=key)
    assert out == push.unb64u(result)
    assert len(out) == 144                       # Content-Length 145 в примере RFC — с переводом строки
    assert push.b64u(b"\xfb\xff") == "-_8" and push.unb64u("-_8=") == push.unb64u("-_8") == b"\xfb\xff"


def test_vapid_header_is_a_valid_es256_jwt(studio):
    header = push.vapid_header("https://fcm.googleapis.com/fcm/send/x")
    scheme, rest = header.split(" ", 1)
    parts = dict(p.split("=", 1) for p in rest.split(", "))
    assert scheme == "vapid" and set(parts) == {"t", "k"} and parts["k"] == push.public_key()
    head, claims, sig = parts["t"].split(".")
    assert json.loads(push.unb64u(head)) == {"typ": "JWT", "alg": "ES256"}
    assert json.loads(push.unb64u(claims)) == {"aud": "https://fcm.googleapis.com", "exp": int(T0) + 43200,
                                               "sub": "https://testserver"}
    raw = push.unb64u(sig)
    assert len(raw) == 64
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), push.unb64u(parts["k"]))
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    try:
        public.verify(der, f"{head}.{claims}x".encode(), ec.ECDSA(hashes.SHA256()))
        raise AssertionError("подпись сошлась с чужим текстом")
    except InvalidSignature:
        pass
    assert len(push.unb64u(push.public_key())) == 65 and push.unb64u(push.public_key())[0] == 4
    assert push.JWT_SEC == 12 * 3600 and push.TTL_SEC == 3600 and push.ENDPOINT_MAX == 1024


class Device:
    """Браузер с подпиской: своя пара P-256 и auth — тест расшифрует то, что прислала служба."""

    def __init__(self, endpoint: str = FCM):
        self.endpoint = endpoint
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public = self.key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        self.auth = os.urandom(16)

    def body(self) -> dict:
        return {"endpoint": self.endpoint, "keys": {"p256dh": push.b64u(self.public), "auth": push.b64u(self.auth)}}

    def decrypt(self, data: bytes) -> dict:
        """Обратный ход RFC 8291: ECDH своим закрытым ключом с ключом из заголовка записи."""
        salt, rs, idlen = data[:16], int.from_bytes(data[16:20], "big"), data[20]
        as_pub, cipher = data[21:21 + idlen], data[21 + idlen:]
        assert rs == 4096 and idlen == 65
        shared = self.key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
        mac = lambda k, m: hmac.new(k, m, hashlib.sha256).digest()           # noqa: E731
        ikm = mac(mac(self.auth, shared), b"WebPush: info\x00" + self.public + as_pub + b"\x01")
        prk = mac(salt, ikm)
        cek = mac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
        nonce = mac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
        plain = AESGCM(cek).decrypt(nonce, cipher, None)
        assert plain.endswith(b"\x02")
        return json.loads(plain[:-1])


def subs() -> list[dict]:
    return [dict(r) for r in state.DB.execute("SELECT endpoint, user_id, digest FROM push_subs ORDER BY created")]


def settle(s) -> None:
    """Дождаться фоновых отправок push (`push.TASKS`)."""
    async def wait():
        while push.TASKS:
            await asyncio.gather(*list(push.TASKS), return_exceptions=True)
    s.c.portal.call(wait)


def test_vapid_key_is_stored_encrypted_and_replaced_when_unreadable(studio, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    make_user(studio, "owner")
    signin(studio, "owner")
    assert studio.c.post("/api/me/push", json=Device().body()).status_code == 204
    key = push.vapid_key()
    raw = key.private_numbers().private_value.to_bytes(32, "big")
    [stored] = state.DB.execute("SELECT value FROM settings WHERE key = 'vapid'").fetchone()
    assert push.b64u(raw) not in stored and raw.hex() not in stored
    assert security.decrypt(stored.encode()) == push.b64u(raw)
    push.KEY = None
    assert push.vapid_key().private_numbers() == key.private_numbers()        # из базы — тот же
    assert len(subs()) == 1
    monkeypatch.setattr(state, "CFG", dataclasses.replace(state.CFG, data_key=Fernet.generate_key().decode()))
    push.KEY = None
    fresh = push.vapid_key()
    assert fresh.private_numbers() != key.private_numbers()
    assert subs() == []
    assert "push: ключ VAPID заменён, подписки сброшены" in caplog.text
    [stored] = state.DB.execute("SELECT value FROM settings WHERE key = 'vapid'").fetchone()
    assert security.decrypt(stored.encode()) == push.b64u(fresh.private_numbers().private_value.to_bytes(32, "big"))
    assert studio.c.get("/api/me/push").json() == {"key": push.public_key(), "subscribed": False}


def test_only_known_push_hosts_are_accepted(studio):
    make_user(studio, "owner")
    signin(studio, "owner")
    for bad in ("http://fcm.googleapis.com/x", "https://fcm.googleapis.com.evil.io/x",
                "https://evil.io/fcm.googleapis.com", "https://u@fcm.googleapis.com/x",
                "https://fcm.googleapis.com:8443/x", "https://10.77.1.10/x", "https://push.apple.com/x",
                "https://fcm.googleapis.com/" + "x" * push.ENDPOINT_MAX, "https://fcm.googleapis.com/a b",
                "fcm.googleapis.com/x", ""):
        r = studio.c.post("/api/me/push", json=Device(bad).body())
        assert (r.status_code, r.json()["detail"]) == (
            422, {"code": "endpoint", "message": "адрес не известной службы push"}), bad
    for good in ("https://fcm.googleapis.com/fcm/send/a", "https://web.push.apple.com/b",
                 "https://updates.push.services.mozilla.com/wpush/v2/c", "https://db5p.notify.windows.com/w/?token=d"):
        r = studio.c.post("/api/me/push", json=Device(good).body())
        assert (r.status_code, r.content) == (204, b""), good
    d = Device("https://fcm.googleapis.com/fcm/send/keys")
    off_curve = b"\x04" + b"\x01" * 64
    for keys in ({"p256dh": push.b64u(d.public[1:]), "auth": push.b64u(d.auth)},
                 {"p256dh": push.b64u(b"\x02" + d.public[1:]), "auth": push.b64u(d.auth)},
                 {"p256dh": push.b64u(off_curve), "auth": push.b64u(d.auth)},
                 {"p256dh": push.b64u(d.public), "auth": push.b64u(d.auth[:15])},
                 {"p256dh": push.b64u(d.public), "auth": "не base64!"},
                 {"p256dh": "%%%", "auth": push.b64u(d.auth)}):
        r = studio.c.post("/api/me/push", json={"endpoint": d.endpoint, "keys": keys})
        assert (r.status_code, r.json()["detail"]) == (422, {"code": "keys", "message": "неверные ключи подписки"}), keys
    assert len(subs()) == 4


def test_subscription_lives_with_its_session(studio):
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    first, second = Device(), Device("https://web.push.apple.com/second")
    assert studio.c.get("/api/me/push").json() == {"key": push.public_key(), "subscribed": False}
    assert studio.c.post("/api/me/push", json=first.body()).status_code == 204
    assert studio.c.get("/api/me/push").json()["subscribed"] is True
    [row] = subs()
    old_digest = row["digest"]
    studio.c.cookies.clear()
    signin(studio, "owner")                          # другая сессия того же браузера-учётки; прежняя жива
    assert studio.c.get("/api/me/push").json()["subscribed"] is False
    assert studio.c.post("/api/me/push", json=second.body()).status_code == 204
    assert studio.c.delete("/api/me/push").status_code == 204
    assert [r["endpoint"] for r in subs()] == [first.endpoint]          # только свои подписки сессии
    assert studio.c.post("/api/me/push", json=first.body()).status_code == 204   # адрес переходит этой сессии
    [row] = subs()
    assert row["digest"] != old_digest and row["user_id"] == uid
    assert studio.c.get("/api/me/push").json()["subscribed"] is True
    assert studio.c.post("/logout", follow_redirects=False).status_code == 303
    assert subs() == []


def test_notify_sends_an_encrypted_payload_and_forgets_gone_subscriptions(studio):
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    d = Device()
    assert studio.c.post("/api/me/push", json=d.body()).status_code == 204
    studio.c.portal.call(push.notify, uid, "Рабочий ПК: вопрос", f"code/1/{ALPHA}/{A}", f"code-{A}")
    [req] = studio.gw.push_requests
    assert str(req.url) == d.endpoint and req.method == "POST"
    h = req.headers
    assert (h["ttl"], h["urgency"], h["content-encoding"], h["content-type"]) == (
        "3600", "high", "aes128gcm", "application/octet-stream")
    assert h["authorization"].startswith("vapid t=") and h["authorization"].endswith(f", k={push.public_key()}")
    assert d.decrypt(req.content) == {"title": "Код", "body": "Рабочий ПК: вопрос",
                                      "href": f"code/1/{ALPHA}/{A}", "tag": f"code-{A}"}
    assert studio.gw.requests == []                  # мимо шлюза
    studio.gw.push_status = 500
    studio.c.portal.call(push.notify, uid, "x", "code", "t")
    assert len(subs()) == 1                          # прочий отказ подписку не снимает
    for gone in (410, 404):
        assert studio.c.post("/api/me/push", json=d.body()).status_code == 204
        studio.gw.push_status = gone
        studio.c.portal.call(push.notify, uid, "x", "code", "t")
        assert subs() == []
    other = make_user(studio, "other")
    studio.c.portal.call(push.notify, other, "x", "code", "t")     # подписок нет — запросов нет
    assert len(studio.gw.push_requests) == 4


def test_cards_and_ends_push_only_without_watchers(studio):
    make_user(studio, "owner")
    aid, _ = enroll(studio)
    signin(studio, "owner")
    d = Device()
    assert studio.c.post("/api/me/push", json=d.body()).status_code == 204
    m = FakeMachine({"alpha": {"files": {}, "git": None, "chats": {
        A: {"title": "Чат", "created": 1, "modified": 1, "tag": "studio", "messages": []}}}})
    link = connect(studio, aid, m)
    assert studio.c.post(f"/api/code/agents/{aid}/open", json={"pin": None}).status_code == 204
    ask = {"k": "ask", "request_id": "r1", "kind": "permission", "tool": "Bash", "input": {"command": "ls"},
           "always": False}
    emit(studio, link, ALPHA, A, ask)
    settle(studio)
    [req] = studio.gw.push_requests
    assert d.decrypt(req.content) == {"title": "Код", "body": "Рабочий ПК: ждёт подтверждения",
                                      "href": f"code/{aid}/{ALPHA}/{A}", "tag": f"code-{A}"}
    # ход идёт и карточку смотрит открытый поток: push не нужен
    m.script = [{"k": "text", "text": "При"}, ask]
    chat = f"/api/code/agents/{aid}/projects/{ALPHA}/chats/{A}"
    with studio.c.stream("POST", chat + "/turn", json={"text": "Привет"}) as r:
        events = events_of(r.iter_lines())
        assert [next(events)[0] for _ in range(3)] == ["turn", "text", "ask"]
    with studio.c.stream("GET", chat + "/live") as r:
        events = events_of(r.iter_lines())
        assert next(events)[0] == "snapshot"
        emit(studio, link, ALPHA, A, ask, {"k": "done", "status": "done"})
        assert [e for e, _ in events] == ["ask", "done"]
    settle(studio)
    assert len(studio.gw.push_requests) == 1
    question = {"k": "ask", "request_id": "q1", "kind": "question", "questions": []}
    emit(studio, link, ALPHA, A, {"k": "text", "text": "x"}, question, {"k": "done", "status": "stopped"},
         {"k": "done", "status": "error", "message": "сбой"}, {"k": "done", "status": "done"})
    settle(studio)
    assert [d.decrypt(r.content)["body"] for r in studio.gw.push_requests[1:]] == [
        "Рабочий ПК: вопрос", "Рабочий ПК: ход прерван", "Рабочий ПК: ошибка", "Рабочий ПК: ход окончен"]


def test_push_log_holds_no_endpoint(studio, caplog):
    caplog.set_level(logging.DEBUG)
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    d = Device("https://fcm.googleapis.com/fcm/send/секрет-адреса-7319")
    assert studio.c.post("/api/me/push", json=d.body()).status_code == 204
    studio.gw.push_status = 500
    studio.c.portal.call(push.notify, uid, "Рабочий ПК: вопрос", "code/1", "code-1")
    studio.gw.push_raise = httpx.ConnectError
    studio.c.portal.call(push.notify, uid, "Рабочий ПК: вопрос", "code/1", "code-1")
    studio.gw.push_raise, studio.gw.push_status = None, 410
    studio.c.portal.call(push.notify, uid, "Рабочий ПК: вопрос", "code/1", "code-1")
    assert studio.c.post("/api/me/push", json=d.body()).status_code == 204
    assert studio.c.delete("/api/me/push").status_code == 204
    text = caplog.text
    assert "push: ответ 500" in text and "push: сбой сети" in text
    for secret in ("fcm.googleapis.com", "секрет-адреса", "7319", d.body()["keys"]["p256dh"],
                   d.body()["keys"]["auth"], "Рабочий ПК", "вопрос"):
        assert secret not in text, secret
