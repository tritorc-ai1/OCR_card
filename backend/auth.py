"""Authentication core: password hashing, session cookies, and FastAPI guards.

This module is deliberately split into two halves:

* Pure helpers (`hash_password`, `verify_password`, `validate_password`,
  `is_allowed_email`, `create_session_token`, `parse_session_token`) have no
  Mongo or FastAPI dependency, so they are fast to unit-test in isolation.
* FastAPI wiring (`set_session_cookie`, dependencies `require_user` /
  `require_admin`) reads the request cookie and looks the user up in Mongo on
  every call, so deactivating a user or resetting a password takes effect
  immediately (via the ``token_version`` claim) rather than only when the
  cookie eventually expires.
"""
from __future__ import annotations

import bcrypt
from fastapi import Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from backend.config import (
    ALLOWED_EMAIL_DOMAIN,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_SECURE,
    SESSION_SECRET,
    SESSION_TTL_DAYS,
)

# bcrypt truncates silently at 72 bytes; reject longer up front so a user
# never sets a password whose tail is ignored. 8 is a low but real floor.
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_BYTES = 72
_BCRYPT_ROUNDS = 12
_SESSION_SALT = "cardscan-session"

_serializer = URLSafeTimedSerializer(SESSION_SECRET, salt=_SESSION_SALT)


# --- Password helpers -------------------------------------------------------
def validate_password(password: str) -> None:
    """Raise ValueError if the password is too short or too long for bcrypt."""
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters.")
    if len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise ValueError(f"Password must be at most {PASSWORD_MAX_BYTES} bytes.")


def hash_password(password: str) -> str:
    validate_password(password)
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


# --- Email policy -----------------------------------------------------------
def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def is_allowed_email(email: str) -> bool:
    """True only if the email is a well-formed address at the allowed domain.

    Matches the domain exactly after the final ``@`` rather than a substring,
    so ``evil@tritorc.com.attacker.io`` is rejected.
    """
    normalized = normalize_email(email)
    if normalized.count("@") != 1:
        return False
    local, _, domain = normalized.partition("@")
    if not local:
        return False
    return domain == ALLOWED_EMAIL_DOMAIN


# --- Session tokens ---------------------------------------------------------
def create_session_token(email: str, token_version: int) -> str:
    return _serializer.dumps({"e": normalize_email(email), "v": int(token_version)})


def parse_session_token(token: str) -> dict | None:
    """Return the token payload, or None if it's tampered, malformed or expired."""
    if not token:
        return None
    try:
        payload = _serializer.loads(token, max_age=SESSION_TTL_DAYS * 86400)
    except (BadSignature, SignatureExpired):
        return None
    except Exception:  # noqa: BLE001 — any other decode failure = no session
        return None
    if not isinstance(payload, dict) or "e" not in payload or "v" not in payload:
        return None
    return payload


# --- Cookie wiring ----------------------------------------------------------
def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_TTL_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=SESSION_COOKIE_SECURE,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


# --- FastAPI dependencies ---------------------------------------------------
def _current_user_or_none(request: Request) -> dict | None:
    """Resolve the signed cookie to a live, active user document, or None.

    Looks the user up in Mongo every call so deactivation / password reset
    (which bumps ``token_version``) invalidates outstanding cookies at once.
    Raises HTTP 503 only when the user store itself is unreachable.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    payload = parse_session_token(token)
    if not payload:
        return None

    # Imported lazily so the pure helpers above stay Mongo-free for unit tests.
    from backend.storage.users import get_user

    try:
        user = get_user(payload["e"])
    except Exception as exc:  # noqa: BLE001 — Mongo down, TLS error, etc.
        raise HTTPException(status_code=503, detail="Sign-in storage is unavailable") from exc

    if not user or not user.get("active", False):
        return None
    if int(user.get("token_version", 0)) != int(payload["v"]):
        return None
    return {"email": user["email"], "role": user.get("role", "user")}


def require_user(request: Request) -> dict:
    user = _current_user_or_none(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_admin(user: dict = Depends(require_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
