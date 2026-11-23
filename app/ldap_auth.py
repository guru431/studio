"""Optional AD password verification and revocation for explicitly linked Studio accounts."""
import asyncio
import base64
import logging
import os
import sqlite3
import ssl
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from itertools import batched
from urllib.parse import urlsplit
from uuid import UUID

from ldap3 import NONE, SIMPLE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException, LDAPInvalidCredentialsResult
from ldap3.protocol.controls import build_control
from ldap3.utils.conv import escape_bytes, escape_filter_chars

import clock
import operations
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


def configured() -> bool:
    return bool(os.environ.get("STUDIO_LDAP_URL"))


def settings() -> Settings:
    try:
        url = urlsplit(os.environ.get("STUDIO_LDAP_URL", ""))
        port = url.port
        ca_pem = base64.b64decode(os.environ.get("STUDIO_LDAP_CA_PEM_B64", ""), validate=True).decode("ascii")
    except ValueError:
        raise Unavailable() from None
    if (url.scheme not in ("ldap", "ldaps") or not url.hostname or url.username is not None
            or url.path not in ("", "/") or url.query or url.fragment or port == 0):
        raise Unavailable()
    base, group, dn, password = (os.environ.get(f"STUDIO_LDAP_{key}", "") for key in
                                 ("BASE_DN", "GROUP_DN", "BIND_DN", "BIND_PASSWORD"))
    if not all((base.strip(), group.strip(), dn.strip(), password)):
        raise Unavailable()
    ldaps = url.scheme == "ldaps"
    return Settings(url.hostname, port or (636 if ldaps else 389), ldaps, base, group, dn, password,
                    ca_pem or None)


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


def find_user(login: str) -> str | None:
    """Resolve one eligible directory identity for an administrator's explicit binding."""
    cfg = settings()
    attribute = "userPrincipalName" if "@" in login else "sAMAccountName"
    with _service(cfg) as conn:
        found = _search(conn, cfg, f"({attribute}={escape_filter_chars(login)})", 2)
    return next(iter(found)) if len(found) == 1 else None


def verify(guid: str, password: str) -> bool:
    if not password:                       # empty SIMPLE bind can succeed anonymously
        return False
    cfg = settings()
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


def eligible(guids: list[str]) -> set[str]:
    cfg = settings()
    allowed = set()
    with _service(cfg) as conn:
        for batch in batched(guids, 100):
            selector = "(|" + "".join(map(_guid_filter, batch)) + ")"
            allowed.update(_search(conn, cfg, selector, len(batch)))
    return allowed


async def sync_access() -> None:
    from account import stop_disabled_user
    from email_accounts import revoke_invited_by

    rows = state.DB.execute("SELECT id, ldap_guid FROM users WHERE ldap_guid IS NOT NULL AND disabled=0").fetchall()
    if not rows:
        return
    allowed = await asyncio.to_thread(eligible, [row["ldap_guid"] for row in rows])
    for uid, guid in rows:
        if guid in allowed:
            continue
        with operations.barrier(uid):
            state.DB.execute("BEGIN")
            try:
                changed = state.DB.execute("UPDATE users SET disabled=1 WHERE id=? AND ldap_guid=? AND disabled=0",
                                           (uid, guid)).rowcount
                if changed:
                    state.DB.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
                    state.DB.execute("DELETE FROM email_tokens WHERE user_id=?", (uid,))
                    revoke_invited_by(uid)
                    security_log.record(uid, "ldap_revoke")
                state.DB.execute("COMMIT")
            except BaseException:
                state.DB.execute("ROLLBACK")
                raise
            if changed:
                log.info("пользователь %d: вход через LDAP отозван", uid)
                try:
                    await stop_disabled_user(uid)
                except Exception as error:
                    log.warning("пользователь %d: завершение работы после отзыва LDAP — %s", uid, type(error).__name__)


async def watch() -> None:
    while True:
        try:
            await sync_access()
        except Unavailable:
            log.warning("LDAP: проверка доступа временно недоступна")
        except sqlite3.Error as error:
            log.warning("LDAP: не удалось сохранить результат проверки — %s", type(error).__name__)
        await clock.SLEEP(CHECK_SEC)
