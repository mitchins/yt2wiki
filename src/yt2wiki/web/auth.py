"""Minimal first-run operator authentication.

One shared operator password comes from the deployment environment
(``YT2WIKI_OPERATOR_PASSWORD``) — there is no default, no user database and
no SSO. A successful login mints a random session token (stored hashed with
an expiry in ``config/operator_sessions.json``) delivered as a Secure-flag-
free HttpOnly SameSite=Lax cookie — the publisher is loopback-only, so TLS
cannot be assumed — plus a per-session CSRF token that browser JavaScript
sends back as an ``X-CSRF-Token`` header (or a ``csrf_token`` form field for
plain HTML posts).

Fail-closed: with no password configured every mutation is refused with
setup instructions; nothing is ever granted on the basis of a localhost
peer address or a forwarded header (those are never consulted).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

PASSWORD_ENV = "YT2WIKI_OPERATOR_PASSWORD"
SESSION_COOKIE = "yt2wiki_op"
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf_token"
SESSION_TTL_S = 14 * 24 * 3600
SESSIONS_FILE = "operator_sessions.json"

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class AuthError(Exception):
    """Carries an HTTP status and a user-facing message."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def password_configured() -> bool:
    return bool(os.environ.get(PASSWORD_ENV, ""))


def verify_password(candidate: str) -> bool:
    expected = os.environ.get(PASSWORD_ENV, "")
    if not expected or not candidate:
        return False
    return hmac.compare_digest(candidate, expected)


def _sessions_path(paths) -> Path:
    return Path(paths.root) / "config" / SESSIONS_FILE


def _read_sessions(paths) -> dict[str, Any]:
    try:
        data = json.loads(_sessions_path(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_sessions(paths, data: dict[str, Any]) -> None:
    path = _sessions_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(paths, *, label: str = "") -> tuple[str, str]:
    """Mint a session; returns (token, csrf). Prunes expired entries."""
    now = time.time()
    sessions = {k: v for k, v in _read_sessions(paths).items()
                if isinstance(v, dict) and float(v.get("expires_at", 0)) > now}
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    sessions[_token_hash(token)] = {
        "csrf": csrf,
        "created_at": now,
        "expires_at": now + SESSION_TTL_S,
        "label": label[:80],
    }
    _write_sessions(paths, sessions)
    return token, csrf


def get_session(paths, token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    entry = _read_sessions(paths).get(_token_hash(token))
    if not isinstance(entry, dict):
        return None
    if float(entry.get("expires_at", 0)) <= time.time():
        return None
    return entry


def revoke_session(paths, token: str | None) -> None:
    if not token:
        return
    sessions = _read_sessions(paths)
    if _token_hash(token) in sessions:
        del sessions[_token_hash(token)]
        _write_sessions(paths, sessions)


def session_from_request(paths, request) -> dict[str, Any] | None:
    """Authenticate by session cookie only — never by peer address/headers."""
    return get_session(paths, request.cookies.get(SESSION_COOKIE))


def csrf_valid(session: dict[str, Any], request, form_field: str = "") -> bool:
    expected = session.get("csrf", "")
    if not expected:
        return False
    presented = request.headers.get(CSRF_HEADER, "") or form_field
    return bool(presented) and hmac.compare_digest(str(presented), str(expected))


def require_operator(paths, request, *, csrf: bool = True) -> dict[str, Any]:
    """Gate a mutating endpoint. Raises AuthError (→ 401/403) when denied."""
    if not password_configured():
        raise AuthError(403,
                        "Operator password is not configured. Set "
                        f"{PASSWORD_ENV} in the container environment and restart "
                        "yt2wiki, then log in on the Settings page.")
    session = session_from_request(paths, request)
    if session is None:
        raise AuthError(401, "Operator login required. Log in on the Settings page.")
    if csrf and request.method in MUTATING_METHODS and not csrf_valid(session, request):
        raise AuthError(403, "Invalid or missing CSRF token. Reload the page and try again.")
    return session
