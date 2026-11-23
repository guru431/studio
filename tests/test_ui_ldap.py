"""The administrator checks the directory, saves it and links a Studio user."""
import pytest

import ldap_auth
from live_app import start_live_app
from test_ui_browser import READY, go, is_open, problems, set_value, signin

pytestmark = pytest.mark.integration


@pytest.fixture
def live_ldap(tmp_path, monkeypatch):
    guid = "00112233-4455-6677-8899-aabbccddeeff"
    monkeypatch.setattr(ldap_auth, "check", lambda config: None)
    monkeypatch.setattr(ldap_auth, "find_user", lambda cfg, name: guid if name == "employee@example.com" else None)
    monkeypatch.setattr(ldap_auth, "eligible", lambda cfg, guids: {guid})
    app = start_live_app(tmp_path)
    try:
        app.add_user("ldap-ui")
        yield app
    finally:
        app.stop()


@pytest.mark.timeout(300)
def test_admin_configures_ldap_and_links_an_existing_account(browser, live_ldap):
    row = "#users tr[data-login=ldap-ui]"
    dialog = "#ldap-link-dialog"
    r = browser.drive(live_ldap.url + "/", [*signin(), ("wait", READY), *go("admin"),
        ("wait", "document.querySelector('#ldap-config-save')?.disabled === false"),
        ("click", "#ldap-settings summary"), ("click", "#ldap-config-enabled"),
        set_value("#ldap-config-url", "ldaps://dc.example.com"), set_value("#ldap-config-base", "DC=example,DC=com"),
        set_value("#ldap-config-group", "CN=Studio,DC=example,DC=com"),
        set_value("#ldap-config-bind-dn", "CN=Reader,DC=example,DC=com"),
        set_value("#ldap-config-bind-password", "fixture-reader-password"),
        set_value("#ldap-config-admin-password", "password-123"), ("click", "#ldap-config-check"),
        ("wait", "document.querySelector('#ldap-config-status').textContent === 'Соединение с LDAP установлено'"),
        set_value("#ldap-config-admin-password", "password-123"), ("click", "#ldap-config-save"),
        ("wait", "document.querySelector('#ldap-config-status').textContent === 'Настройки LDAP сохранены'"),
        ("eval", "secretCleared", "document.querySelector('#ldap-config-bind-password').value === ''"),
        ("wait", f"document.querySelector('{row} .user-ldap')"),
        ("click", f"{row} .user-ldap"), ("wait", is_open(dialog)),
        set_value("#ldap-login", "employee@example.com"), set_value("#ldap-admin-password", "password-123"),
        ("click", "#ldap-link-confirm"),
        ("wait", f"!{is_open(dialog)} && !document.querySelector('{row} .user-reset') && "
                 f"document.querySelector('{row} .actions').textContent.includes('LDAP')")])
    assert not r.get("failed"), r
    assert r["values"]["secretCleared"]
    assert not problems(r), problems(r)
