"""Вход в Cleaner через один заранее зарегистрированный OpenID Connect клиент."""
import base64
import hashlib
import hmac
import html
import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import auth
import clock
import email_accounts
import lock_policy
import security
import state
from config import CleanerOidc

router = APIRouter()
CLIENT_ID = "cleaner"
RESUME_PATH = "/oidc/resume"
LOGIN_NEXT = RESUME_PATH[1:]
CONTINUATION = "__Host-studio_oidc"
CONTINUATION_AGE = 600
CODE_AGE = 60
ID_TOKEN_AGE = 120
ACCESS_TOKEN_AGE = 12 * 3600
HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_OPAQUE = re.compile(r"[A-Za-z0-9_-]{16,128}")
_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")
_SOURCE = (
    "SELECT u.login, o.subject, s.expires FROM sessions s"
    " JOIN users u ON u.id = s.user_id JOIN cleaner_oidc_subjects o ON o.user_id = u.id"
    " WHERE s.digest = ? AND s.expires > ? AND u.disabled = 0 AND u.email != ''"
)


@dataclass(frozen=True)
class Flow:
    state: str
    nonce: str
    challenge: str


def _settings() -> CleanerOidc | None:
    return state.CFG.cleaner_oidc


def _error(error: str, status: int = 400, **headers: str) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status, headers={**HEADERS, **headers})


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _number(value: int) -> str:
    return _b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _kid(cfg: CleanerOidc) -> str:
    public = cfg.signing_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return _b64(hashlib.sha256(public).digest())


def _client_binding(cfg: CleanerOidc) -> str:
    return security.digest("\0".join((cfg.issuer, cfg.redirect_uri, cfg.secret)))


def _part(value: dict) -> str:
    return _b64(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _id_token(cfg: CleanerOidc, source, nonce: str, access: str, now: int) -> str:
    header = _part({"alg": "RS256", "kid": _kid(cfg), "typ": "JWT"})
    payload = _part({
        "iss": cfg.issuer, "aud": CLIENT_ID, "sub": source["subject"], "nonce": nonce,
        "iat": now, "exp": now + ID_TOKEN_AGE, "preferred_username": source["login"],
        "at_hash": _b64(hashlib.sha256(access.encode("ascii")).digest()[:16]),
    })
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = cfg.signing_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64(signature)}"


@router.get("/.well-known/openid-configuration")
async def discovery():
    cfg = _settings()
    if cfg is None:
        return _error("not_configured", 404)
    return JSONResponse({
        "issuer": cfg.issuer,
        "authorization_endpoint": cfg.issuer + "/oidc/authorize",
        "token_endpoint": cfg.issuer + "/oidc/token",
        "userinfo_endpoint": cfg.issuer + "/oidc/userinfo",
        "jwks_uri": cfg.issuer + "/oidc/jwks",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code"],
        "subject_types_supported": ["public"],
        "scopes_supported": ["openid"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic"],
        "code_challenge_methods_supported": ["S256"],
        "claims_supported": ["iss", "aud", "sub", "nonce", "iat", "exp", "preferred_username", "at_hash"],
    }, headers=HEADERS)


@router.get("/oidc/jwks")
async def jwks():
    cfg = _settings()
    if cfg is None:
        return _error("not_configured", 404)
    numbers = cfg.signing_key.public_key().public_numbers()
    return JSONResponse({"keys": [{
        "kty": "RSA", "use": "sig", "alg": "RS256", "kid": _kid(cfg), "n": _number(numbers.n), "e": _number(numbers.e),
    }]}, headers=HEADERS)


def pending(request: Request) -> Flow | None:
    cfg = _settings()
    token = request.cookies.get(CONTINUATION, "")
    if cfg is None or not token or len(token) > 2048:
        return None
    try:
        plaintext = Fernet(state.CFG.data_key).decrypt_at_time(
            token.encode("ascii"), ttl=CONTINUATION_AGE, current_time=int(clock.NOW()),
        )
        purpose, issuer, redirect, valid_state, nonce, challenge = json.loads(plaintext)
    except (InvalidToken, UnicodeError, ValueError, TypeError):
        return None
    if (purpose, issuer, redirect) != ("cleaner-oidc", cfg.issuer, cfg.redirect_uri):
        return None
    return Flow(valid_state, nonce, challenge)


def _start_login(cfg: CleanerOidc, flow: Flow) -> RedirectResponse:
    payload = json.dumps(("cleaner-oidc", cfg.issuer, cfg.redirect_uri, flow.state, flow.nonce, flow.challenge))
    value = Fernet(state.CFG.data_key).encrypt_at_time(payload.encode(), int(clock.NOW())).decode("ascii")
    response = RedirectResponse("/login?continue=cleaner", status_code=303, headers=HEADERS)
    response.set_cookie(
        CONTINUATION, value, max_age=CONTINUATION_AGE, path="/", secure=True, httponly=True, samesite="lax",
    )
    return response


def _return(cfg: CleanerOidc, valid_state: str, *, resumed: bool = False, **params: str) -> Response:
    location = cfg.redirect_uri + "?" + urlencode({**params, "state": valid_state})
    if resumed:
        # Завершаем переход от формы Studio отдельной страницей: form-action 'self' защищает и цепочку 303.
        target = html.escape(location, quote=True)
        response = HTMLResponse(
            '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Cleaner</title>'
            f'<meta http-equiv="refresh" content="0;url={target}">'
            f'<p><a href="{target}">Перейти в Cleaner</a></p></html>',
            headers=HEADERS,
        )
    else:
        response = RedirectResponse(location, status_code=303, headers=HEADERS)
    response.delete_cookie(CONTINUATION, path="/", secure=True, httponly=True, samesite="lax")
    return response


def _authorize(cfg: CleanerOidc, request: Request, flow: Flow, *, resumed: bool = False):
    cookie = request.cookies.get(auth.COOKIE)
    session = auth.session_user(cookie, renew=False)
    if session is None:
        return _return(cfg, flow.state, resumed=True, error="login_required") if resumed else _start_login(cfg, flow)
    user, _ = session
    session_digest = security.digest(cookie)
    if not email_accounts.has_email(user.id) or lock_policy.locked(session_digest, "security"):
        return _return(cfg, flow.state, resumed=resumed, error="interaction_required")
    now = clock.NOW()
    code = security.new_token()
    state.DB.execute("DELETE FROM cleaner_oidc_codes WHERE expires <= ?", (now,))
    state.DB.execute(
        "INSERT INTO cleaner_oidc_codes(digest, session_digest, client_binding, challenge, nonce, expires)"
        " VALUES(?, ?, ?, ?, ?, ?)",
        (security.digest(code), session_digest, _client_binding(cfg), flow.challenge, flow.nonce, now + CODE_AGE),
    )
    return _return(cfg, flow.state, resumed=resumed, code=code)


@router.get("/oidc/authorize")
async def authorize(request: Request):
    cfg = _settings()
    if cfg is None:
        return _error("not_configured", 404)
    if len(request.scope["query_string"]) > 2048:
        return _error("invalid_request")
    params = request.query_params
    if (params.getlist("client_id") != [CLIENT_ID] or params.getlist("redirect_uri") != [cfg.redirect_uri]
            or len(params.getlist("state")) != 1 or not _OPAQUE.fullmatch(params["state"])):
        return _error("invalid_request")
    valid_state = params["state"]
    if params.getlist("response_type") != ["code"] or params.getlist("response_mode") not in ([], ["query"]):
        return _return(cfg, valid_state, error="unsupported_response_type")
    if params.getlist("scope") != ["openid"]:
        return _return(cfg, valid_state, error="invalid_scope")
    nonce, challenge = params.get("nonce", ""), params.get("code_challenge", "")
    if (params.getlist("code_challenge_method") != ["S256"] or len(params.getlist("code_challenge")) != 1
            or len(params.getlist("nonce")) != 1 or not _OPAQUE.fullmatch(nonce) or not _TOKEN.fullmatch(challenge)):
        return _return(cfg, valid_state, error="invalid_request")
    if params.getlist("prompt") not in ([], ["none"]):
        return _return(cfg, valid_state, error="invalid_request")
    if params.get("prompt") == "none" and auth.session_user(request.cookies.get(auth.COOKIE), renew=False) is None:
        return _return(cfg, valid_state, error="login_required")
    return _authorize(cfg, request, Flow(valid_state, nonce, challenge))


@router.get(RESUME_PATH)
async def resume(request: Request):
    cfg, flow = _settings(), pending(request)
    if cfg is None or flow is None:
        return _error("invalid_request")
    return _authorize(cfg, request, flow, resumed=True)


def _authenticated(cfg: CleanerOidc, request: Request) -> bool:
    headers = request.headers.getlist("authorization")
    if len(headers) != 1 or len(headers[0]) > 384:
        return False
    scheme, _, raw = headers[0].partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        credentials = base64.b64decode(raw, validate=True)
    except ValueError:
        return False
    return hmac.compare_digest(credentials, f"{CLIENT_ID}:{cfg.secret}".encode("ascii"))


def _exchange(cfg: CleanerOidc, code: str, verifier: str) -> dict | None:
    now = clock.NOW()
    database = state.DB
    database.execute("BEGIN IMMEDIATE")
    try:
        digest = security.digest(code)
        binding = _client_binding(cfg)
        row = database.execute(
            "SELECT * FROM cleaner_oidc_codes WHERE digest = ? AND client_binding = ? AND expires > ?",
            (digest, binding, now),
        ).fetchone()
        challenge = _b64(hashlib.sha256(verifier.encode("ascii")).digest())
        if row is None or not hmac.compare_digest(challenge, row["challenge"]):
            database.execute("ROLLBACK")
            return None
        session_digest = row["session_digest"]
        source = database.execute(_SOURCE, (session_digest, now + 1)).fetchone()
        if source is None or lock_policy.locked(session_digest, "security"):
            database.execute("ROLLBACK")
            return None
        database.execute("DELETE FROM cleaner_oidc_codes WHERE digest = ?", (digest,))
        database.execute("DELETE FROM cleaner_oidc_tokens WHERE expires <= ?", (now,))
        access = security.new_token()
        age = min(ACCESS_TOKEN_AGE, int(source["expires"] - now))
        database.execute(
            "INSERT INTO cleaner_oidc_tokens(digest, session_digest, client_binding, expires) VALUES(?, ?, ?, ?)",
            (security.digest(access), session_digest, binding, now + age),
        )
        token = _id_token(cfg, source, row["nonce"], access, int(now))
        database.execute("COMMIT")
    except BaseException:
        database.execute("ROLLBACK")
        raise
    return {"token_type": "Bearer", "access_token": access, "expires_in": age, "id_token": token}


@router.post("/oidc/token")
async def token(request: Request):
    cfg = _settings()
    if cfg is None:
        return _error("not_configured", 404)
    if not _authenticated(cfg, request):
        return _error("invalid_client", 401, **{"WWW-Authenticate": 'Basic realm="Cleaner"'})
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/x-www-form-urlencoded":
        return _error("invalid_request")
    body = await auth.small_body(request, 2048)
    if body is None:
        return _error("invalid_request")
    try:
        form = parse_qs(body.decode("ascii"), strict_parsing=True, max_num_fields=8, errors="strict")
    except (UnicodeError, ValueError):
        return _error("invalid_request")
    if form.get("grant_type") != ["authorization_code"]:
        return _error("unsupported_grant_type")
    if form.get("redirect_uri") != [cfg.redirect_uri] or "client_secret" in form or (
        "client_id" in form and form["client_id"] != [CLIENT_ID]
    ):
        return _error("invalid_grant")
    if len(form.get("code", [])) != 1 or len(form.get("code_verifier", [])) != 1:
        return _error("invalid_request")
    code, verifier = form["code"][0], form["code_verifier"][0]
    if not _TOKEN.fullmatch(code) or not _VERIFIER.fullmatch(verifier):
        return _error("invalid_grant")
    result = _exchange(cfg, code, verifier)
    return JSONResponse(result, headers=HEADERS) if result else _error("invalid_grant")


@router.api_route("/oidc/userinfo", methods=["GET", "POST"])
async def userinfo(request: Request):
    cfg = _settings()
    if cfg is None:
        return _error("not_configured", 404)
    headers = request.headers.getlist("authorization")
    if len(headers) == 1:
        scheme, _, access = headers[0].partition(" ")
        if scheme.lower() == "bearer" and _TOKEN.fullmatch(access):
            now = clock.NOW()
            entry = state.DB.execute(
                "SELECT session_digest FROM cleaner_oidc_tokens WHERE digest = ? AND client_binding = ? AND expires > ?",
                (security.digest(access), _client_binding(cfg), now),
            ).fetchone()
            if entry is not None:
                source = state.DB.execute(_SOURCE, (entry["session_digest"], now)).fetchone()
                if source is not None:
                    return JSONResponse({"sub": source["subject"]}, headers=HEADERS)
    return _error("invalid_token", 401, **{"WWW-Authenticate": 'Bearer error="invalid_token"'})
