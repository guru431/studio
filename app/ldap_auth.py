"""Optional AD password verification and revocation for explicitly linked Studio accounts."""
import asyncio
import base64
import json
import logging
import os
import sqlite3
import ssl
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from itertools import batched
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.fernet import InvalidToken
from ldap3 import BASE, NONE, NO_ATTRIBUTES, SIMPLE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException, LDAPInvalidCredentialsResult
from ldap3.protocol.controls import build_control
from ldap3.utils.conv import escape_bytes, escape_filter_chars
from pydantic import BaseModel, Field

import clock
import operations
import security
import security_log
import state

CHECK_SEC = 60
NETWORK_SEC = 5
DOMAIN_SCOPE = "1.2.840.113556.1.4.1339"
log = logging.getLogger("studio")


class Unavailable(Exception):
    """No authoritative LDAP answer. Never treat this as loss of membership."""


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    ldaps: bool
    base: str
    group: str
    bind_dn: str
    bind_password: str = field(repr=False)
    ca_pem: str | None = None


class SettingsIn(BaseModel):
    enabled: bool
    url: str = Field(max_length=512)
    base: str = Field(max_length=1024)
    group: str = Field(max_length=1024)
    bind_dn: str = Field(max_length=1024)
    bind_password: str | None = Field(default=None, max_length=8192, repr=False)
    ca_pem: str = Field(default="", max_length=65536)
    admin_password: str = Field(max_length=1024, repr=False)


def configuration() -> tuple[dict, bool]:
    row = state.DB.execute("SELECT value FROM settings WHERE key='ldap'").fetchone() if state.DB is not None else None
    if row is not None:
        try:
            config = json.loads(security.decrypt(row[0]))
        except (InvalidToken, ValueError):
            return {}, True
        return (config, False) if isinstance(config, dict) else ({}, True)
    config = {key: os.environ.get(f"STUDIO_LDAP_{env}", "") for key, env in
              {"url": "URL", "base": "BASE_DN", "group": "GROUP_DN", "bind_dn": "BIND_DN",
               "bind_password": "BIND_PASSWORD"}.items()}
    config["enabled"] = bool(config["url"])
    try:
        config["ca_pem"] = base64.b64decode(os.environ.get("STUDIO_LDAP_CA_PEM_B64", ""), validate=True).decode("ascii").strip()
    except ValueError:
        return config, True
    return config, False


def public_settings() -> dict:
    config, reenter = configuration()
    return {**{key: config.get(key, "") for key in ("url", "base", "group", "bind_dn", "ca_pem")},
            "enabled": bool(config.get("enabled")), "password_set": bool(config.get("bind_password")) and not reenter,
            "reenter": reenter}


def prepare(body: SettingsIn, previous: tuple[dict, bool]) -> dict:
    old, reenter = previous
    config = body.model_dump(exclude={"admin_password"})
    for key in ("url", "base", "group", "bind_dn", "ca_pem"):
        config[key] = config[key].strip()
    if body.bind_password is None:
        same_target = all(config[key] == old.get(key, "") for key in ("url", "bind_dn", "ca_pem"))
        config["bind_password"] = old.get("bind_password", "") if same_target and not reenter else ""
    return config


def _connection_settings(config: dict) -> Settings:
    address = config.get("url", "")
    url = urlsplit(address)
    port = url.port
    if (url.scheme not in ("ldap", "ldaps") or not url.hostname or url.username is not None or port == 0
            or url.path not in ("", "/") or url.query or url.fragment
            or any(c.isspace() or ord(c) < 32 or c in "\\%" for c in address)):
        raise ValueError()
    base, group, dn, password = (config.get(key, "") for key in ("base", "group", "bind_dn", "bind_password"))
    if not all((base.strip(), group.strip(), dn.strip(), password)) or any(
        ord(c) < 32 or ord(c) == 127 for value in (base, group, dn) for c in value
    ):
        raise ValueError()
    ldaps = url.scheme == "ldaps"
    return Settings(url.hostname, port or (636 if ldaps else 389), ldaps, base, group, dn, password,
                    config.get("ca_pem") or None)


def settings() -> Settings:
    config, reenter = configuration()
    if reenter or not config.get("enabled"):
        raise Unavailable()
    try:
        return _connection_settings(config)
    except ValueError:
        raise Unavailable() from None


def configured() -> bool:
    try:
        settings()
    except Unavailable:
        return False
    return True


async def query(action, *args):
    config = settings()
    result = await asyncio.to_thread(action, config, *args)
    # Do not apply an LDAP answer obtained before the administrator changed the configuration.
    if config != settings():
        raise Unavailable()
    return result


def check(config: dict) -> None:
    cfg = _connection_settings(config)
    with _service(cfg) as conn:
        for dn, selector in ((cfg.base, "(objectClass=*)"), (cfg.group, "(objectClass=group)")):
            conn.search(dn, selector, search_scope=BASE, attributes=[NO_ATTRIBUTES], size_limit=1,
                        time_limit=NETWORK_SEC, controls=[build_control(DOMAIN_SCOPE, True, None)])
            if conn.result["result"] != 0 or len(conn.response) != 1 or conn.response[0]["type"] != "searchResEntry":
                raise Unavailable()


@contextmanager
def _connection(cfg: Settings, dn: str, password: str):
    tls = Tls(validate=ssl.CERT_REQUIRED, ca_certs_data=cfg.ca_pem, sni=cfg.host)
    server = Server(cfg.host, port=cfg.port, use_ssl=cfg.ldaps, tls=tls, get_info=NONE,
                    connect_timeout=NETWORK_SEC)
    conn = Connection(server, user=dn, password=password, authentication=SIMPLE, auto_referrals=False,
                      receive_timeout=NETWORK_SEC, raise_exceptions=True)
    try:
        conn.open(read_server_info=False)
        if not cfg.ldaps and not conn.start_tls(read_server_info=False):
            raise Unavailable()
        if not conn.bind(read_server_info=False):
            raise Unavailable()
        yield conn
    finally:
        with suppress(LDAPException, OSError):
            conn.unbind()


@contextmanager
def _service(cfg: Settings):
    try:
        with _connection(cfg, cfg.bind_dn, cfg.bind_password) as conn:
            yield conn
    except (LDAPException, OSError, ValueError):
        raise Unavailable() from None


def _search(conn: Connection, cfg: Settings, selector: str, limit: int) -> set[str]:
    group = escape_filter_chars(cfg.group)
    filters = ("(&(objectCategory=person)(objectClass=user)"
               "(!(userAccountControl:1.2.840.113556.1.4.803:=2))"
               f"(memberOf:1.2.840.113556.1.4.1941:={group}){selector})")
    conn.search(cfg.base, filters, attributes=["objectGUID"], size_limit=limit, time_limit=NETWORK_SEC,
                controls=[build_control(DOMAIN_SCOPE, True, None)])
    # ldap3 can return False even with result=success when the result has zero entries.
    if conn.result["result"] != 0:
        raise Unavailable()
    found = set()
    for entry in conn.response:
        values = entry.get("raw_attributes", {}).get("objectGUID", [])
        if entry["type"] != "searchResEntry" or len(values) != 1 or len(values[0]) != 16:
            raise Unavailable()
        found.add(str(UUID(bytes_le=values[0])))
    return found


def _guid_filter(guid: str) -> str:
    return f"(objectGUID={escape_bytes(UUID(guid).bytes_le)})"


def find_user(cfg: Settings, login: str) -> str | None:
    """Resolve one eligible directory identity for an administrator's explicit binding."""
    attribute = "userPrincipalName" if "@" in login else "sAMAccountName"
    with _service(cfg) as conn:
        found = _search(conn, cfg, f"({attribute}={escape_filter_chars(login)})", 2)
    return next(iter(found)) if len(found) == 1 else None


def verify(cfg: Settings, guid: str, password: str) -> bool:
    if not password:                       # empty SIMPLE bind can succeed anonymously
        return False
    with _service(cfg) as conn:
        found = _search(conn, cfg, _guid_filter(guid), 1)
    if guid not in found:
        return False
    try:
        # AD accepts a braced objectGUID in a SIMPLE bind. A DN can be reassigned after a rename.
        with _connection(cfg, "{" + guid + "}", password):
            return True
    except LDAPInvalidCredentialsResult:
        return False
    except (LDAPException, OSError, ValueError):
        raise Unavailable() from None


def eligible(cfg: Settings, guids: list[str]) -> set[str]:
    allowed = set()
    with _service(cfg) as conn:
        for batch in batched(guids, 100):
            selector = "(|" + "".join(map(_guid_filter, batch)) + ")"
            allowed.update(_search(conn, cfg, selector, len(batch)))
    return allowed


def linked_active():
    return state.DB.execute("SELECT id, ldap_guid FROM users WHERE ldap_guid IS NOT NULL AND disabled=0").fetchall()

async def _apply_revocation(rows, saved: bytes | None = None) -> None:
    from account import stop_disabled_user
    from email_accounts import revoke_invited_by

    with ExitStack() as barriers:
        revoked = []
        state.DB.execute("BEGIN")
        try:
            if saved is not None:
                state.DB.execute("INSERT INTO settings(key,value) VALUES('ldap',?)"
                                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (saved,))
            for uid, guid in rows:
                changed = state.DB.execute("UPDATE users SET disabled=1 WHERE id=? AND ldap_guid=? AND disabled=0",
                                           (uid, guid)).rowcount
                if changed:
                    barriers.enter_context(operations.barrier(uid))
                    state.DB.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
                    state.DB.execute("DELETE FROM email_tokens WHERE user_id=?", (uid,))
                    revoke_invited_by(uid)
                    security_log.record(uid, "ldap_revoke")
                    revoked.append(uid)
            state.DB.execute("COMMIT")
        except BaseException:
            state.DB.execute("ROLLBACK")
            raise
        for uid in revoked:
            log.info("пользователь %d: вход через LDAP отозван", uid)
            try:
                await stop_disabled_user(uid)
            except Exception as error:
                log.warning("пользователь %d: завершение работы после отзыва LDAP — %s", uid, type(error).__name__)


async def sync_access() -> None:
    rows = linked_active()
    if rows:
        allowed = await query(eligible, [row["ldap_guid"] for row in rows])
        revoked = [row for row in rows if row["ldap_guid"] not in allowed]
        if revoked:
            await _apply_revocation(revoked)


async def save(config: dict) -> dict:
    revoked = [] if config["enabled"] else linked_active()
    await _apply_revocation(revoked, security.encrypt(json.dumps(config)))
    return public_settings()


async def watch() -> None:
    while True:
        try:
            if configured():
                await sync_access()
        except Unavailable:
            log.warning("LDAP: проверка доступа временно недоступна")
        except sqlite3.Error as error:
            log.warning("LDAP: не удалось сохранить результат проверки — %s", type(error).__name__)
        await clock.SLEEP(CHECK_SEC)
