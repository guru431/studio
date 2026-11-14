"""Приглашения, почта и коды: реальные браузеры, клавиатура, касания и сохранение настроек."""
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

import account_mail
import db
from live_app import start_live_app

pw = pytest.importorskip("playwright.sync_api")
pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    root = tmp_path_factory.mktemp("email-live")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                   .serial_number(1).not_valid_before(datetime(2020, 1, 1, tzinfo=timezone.utc))
                   .not_valid_after(datetime(2040, 1, 1, tzinfo=timezone.utc))
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
                   .sign(key, hashes.SHA256()))
    certfile, keyfile = root / "localhost.crt", root / "localhost.key"
    certfile.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    app = start_live_app(root, tls=(certfile, keyfile))
    yield app
    app.stop()


@pytest.fixture
def mail(monkeypatch):
    sent = []

    async def send(email, subject, body):
        sent.append((email, subject, body))

    monkeypatch.setattr(account_mail, "send", send)
    return sent


@pytest.fixture(params=["chromium", "firefox", "webkit"])
def browser(request):
    with pw.sync_playwright() as driver:
        engine = getattr(driver, request.param)
        if not Path(engine.executable_path).is_file():
            pytest.skip(f"Нет установленного {request.param}")
        instance = engine.launch()
        yield instance, request.param
        instance.close()


def owner_headers(live):
    signed = httpx.post(live.url + "/login", data={"login": "owner", "password": "password-123"}, verify=False)
    assert signed.status_code == 303
    headers = {"Cookie": "; ".join(f"{key}={value}" for key, value in signed.cookies.items())}
    me = httpx.get(live.url + "/api/me", headers=headers, verify=False).json()
    return {**headers, "x-studio-csrf": me["csrf"]}


def last_link(mail):
    return re.search(r"https://localhost:\d+/account/\w+#[A-Za-z0-9_-]+", mail[-1][2])[0]


def fits(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_invite_accept_recovery_and_email_settings(browser, live, mail, width):
    instance, engine = browser
    login = f"{engine}-{width}"
    email = login + "@example.com"
    headers = owner_headers(live)
    assert httpx.post(live.url + "/api/admin/invitations", headers=headers,
                      json={"email": email, "comment": "Private comment"}, verify=False).status_code == 201
    mobile = width < 760
    with instance.new_context(viewport={"width": width, "height": 900}, has_touch=mobile, ignore_https_errors=True,
                              service_workers="block") as context:
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(last_link(mail))
        page.locator("#login").wait_for()
        assert page.evaluate("location.hash") == ""
        assert "Private comment" not in page.locator("body").inner_text()
        page.locator("#login").fill(login)
        page.locator("#password").fill("friend1")
        page.locator("#repeat").fill("friend1")
        page.locator("#repeat").press("Enter")
        assert page.locator("#password").evaluate("input => input.validity.tooShort")
        page.locator("#password").fill("friend12")
        page.locator("#repeat").fill("different-password")
        page.locator("#account-form button").click()
        pw.expect(page.locator("#account-status")).to_have_text("Пароли не совпадают")
        page.locator("#repeat").fill("friend12")
        page.locator("#repeat").press("Enter")
        pw.expect(page.locator("#account-status")).to_contain_text("Учётная запись создана")
        fits(page)
        page.locator("#account-back").click()
        pw.expect(page.locator("label[for=login]")).to_have_text("Логин или email")
        if mobile:
            assert page.evaluate("document.activeElement.tagName") != "INPUT"
        page.get_by_text("Забыли пароль?", exact=True).click()
        page.locator("#login").fill(email)
        page.locator("#login").press("Enter")
        pw.expect(page.locator("#account-status")).to_contain_text("отправлена ссылка")
        page.goto(last_link(mail))
        page.locator("#password").fill("new-pas")
        page.locator("#repeat").fill("new-pas")
        page.locator("#repeat").press("Enter")
        assert page.locator("#password").evaluate("input => input.validity.tooShort")
        page.locator("#password").fill("new-pass")
        page.locator("#repeat").fill("new-pass")
        page.locator("#repeat").press("Enter")
        pw.expect(page.locator("#account-status")).to_contain_text("Пароль сохранён")
        assert not errors

    signed = httpx.post(live.url + "/login", data={"login": email, "password": "new-pass"}, verify=False)
    assert signed.status_code == 303
    cookie = "; ".join(f"{key}={value}" for key, value in signed.cookies.items())
    with instance.new_context(viewport={"width": width, "height": 900}, has_touch=mobile, ignore_https_errors=True,
                              extra_http_headers={"Cookie": cookie}, service_workers="block") as context:
        page = context.new_page()
        page.goto(live.url + "/#/settings/security")
        pw.expect(page.locator("#account-email")).to_have_text(email)
        pw.expect(page.locator("#email-codes")).not_to_be_checked()
        page.locator("#email-codes").check()
        page.locator("#email-codes-password").fill("new-pass")
        page.locator("#email-codes-save").click()
        pw.expect(page.locator("#email-codes-status")).to_have_text("Коды входа включены")
        page.reload()
        pw.expect(page.locator("#email-codes")).to_be_checked()
        fits(page)
        page.emulate_media(color_scheme="dark")
        fits(page)
        page.screenshot(path=str(ROOT / ".tmp" / "email-accounts" / f"settings-{engine}-{width}.png"), full_page=True)
        page.goto(live.url + "/account/email")
        page.locator("#email").fill("changed-" + email)
        page.locator("#password").fill("new-pass")
        page.locator("#account-form button").click()
        pw.expect(page.locator("#account-status")).to_contain_text("Письмо отправлено")
        connection = db.connect(live.db_path)
        try:
            connection.execute("UPDATE users SET screen_lock=1 WHERE login=?", (login,))
            connection.execute("UPDATE sessions SET seen=NULL WHERE user_id=(SELECT id FROM users WHERE login=?)", (login,))
        finally:
            connection.close()
        page.goto(last_link(mail))
        page.locator("#account-unlock-password").fill("new-pass")
        page.locator("#account-unlock button[type=submit]").click()
        pw.expect(page.locator("#account-status")).to_have_text("Почта подтверждена.")


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_admin_invitation_list_and_comment_survive_reload(browser, live, mail, width):
    instance, engine = browser
    headers = owner_headers(live)
    email = f"invited-{engine}-{width}@example.com"
    with instance.new_context(viewport={"width": width, "height": 900}, has_touch=width < 760, ignore_https_errors=True,
                              extra_http_headers={"Cookie": headers["Cookie"]}, service_workers="block") as context:
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(live.url + "/#/admin")
        page.locator("#user-add").click()
        page.locator("#invite-email").fill(email)
        page.locator("#invite-comment").fill("Comment <script>text</script>")
        page.locator("#invite-save").click()
        row = page.locator(f'#invitations tr[data-email="{email}"]')
        pw.expect(row).to_contain_text("Ожидает регистрации")
        pw.expect(row).to_contain_text("Comment <script>text</script>")
        row.locator(".invite-edit").click()
        page.locator("#invite-comment").fill("Saved comment")
        page.locator("#invite-save").click()
        pw.expect(row).to_contain_text("Saved comment")
        page.reload()
        pw.expect(row).to_contain_text("Saved comment")
        fits(page)
        row.locator(".invite-revoke").click()
        pw.expect(row).to_contain_text("Отозвано")
        assert not errors


def test_email_code_form_finishes_login_only_after_correct_code(browser, live, mail):
    instance, engine = browser
    login = "code-" + engine
    uid = live.add_user(login)
    connection = db.connect(live.db_path)
    try:
        connection.execute("UPDATE users SET email_codes=1 WHERE id=?", (uid,))
        response = httpx.post(live.url + "/login", data={"login": login, "password": "password-123"}, verify=False)
        assert response.status_code == 303 and response.headers["location"] == "/account/code"
        correct = re.search(r"\b[0-9]{6}\b", mail[-1][2])[0]
        wrong = "000000" if correct != "000000" else "111111"
        with instance.new_context(viewport={"width": 390, "height": 900}, has_touch=True, ignore_https_errors=True,
                                  service_workers="block") as context:
            context.add_cookies([{"name": key, "value": value, "url": live.url, "secure": True,
                                  "httpOnly": True, "sameSite": "Lax"} for key, value in response.cookies.items()])
            page = context.new_page()
            page.goto(live.url + "/account/code")
            page.locator("#code").fill(wrong)
            page.locator("#code").press("Enter")
            pw.expect(page.locator("#account-status")).to_contain_text("недействительны")
            assert connection.execute("SELECT count(*) FROM sessions WHERE user_id=?", (uid,)).fetchone()[0] == 0
            page.locator("#code").fill(correct)
            page.locator("#code").press("Enter")
            page.locator("#rail").wait_for()
            assert connection.execute("SELECT count(*) FROM sessions WHERE user_id=?", (uid,)).fetchone()[0] == 1
    finally:
        connection.close()
