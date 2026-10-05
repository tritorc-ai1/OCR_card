"""MongoDB-backed user store for authentication.

Users are keyed by their lowercased email as the Mongo ``_id`` (mirroring how
``events`` uses ``event_id`` as ``_id``), so uniqueness is free. Password
hashes are never returned by the list/get helpers used to build API responses
— callers that need the hash (login) read the raw document via ``get_user``.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.auth import hash_password, normalize_email, validate_password
from backend.config import ADMIN_EMAIL, ADMIN_FORCE_PASSWORD_RESET, ADMIN_PASSWORD
from backend.storage import mongo
from backend.storage.db import utc_now

logger = logging.getLogger(__name__)

USERS = "users"

# Fields safe to expose in API responses (everything except password_hash).
_PUBLIC_FIELDS = ("email", "role", "active", "created_at", "created_by", "password_changed_at")


def _db():
    database = mongo.get_database()
    if database is None:
        raise RuntimeError("MongoDB is unavailable — cannot access users.")
    return database


def public_view(user: dict[str, Any] | None) -> dict[str, Any] | None:
    if not user:
        return None
    return {key: user.get(key) for key in _PUBLIC_FIELDS}


def get_user(email: str) -> dict[str, Any] | None:
    return _db()[USERS].find_one({"_id": normalize_email(email)})


def list_users() -> list[dict[str, Any]]:
    docs = _db()[USERS].find({}, {"password_hash": 0}).sort("created_at", 1)
    return [dict(doc) for doc in docs]


def create_user(email: str, password_hash: str, role: str = "user", created_by: str = "system") -> dict[str, Any]:
    """Insert a new user. Raises ValueError if one already exists for the email."""
    normalized = normalize_email(email)
    now = utc_now()
    doc = {
        "_id": normalized,
        "email": normalized,
        "password_hash": password_hash,
        "role": role,
        "active": True,
        "token_version": 0,
        "created_at": now,
        "created_by": created_by,
        "password_changed_at": now,
    }
    from pymongo.errors import DuplicateKeyError

    try:
        _db()[USERS].insert_one(doc)
    except DuplicateKeyError as exc:
        raise ValueError(f"A user with email {normalized} already exists.") from exc
    return doc


def set_user_active(email: str, active: bool) -> bool:
    """Activate/deactivate a user. Bumping token_version kills outstanding
    cookies so a deactivated user is logged out immediately."""
    result = _db()[USERS].update_one(
        {"_id": normalize_email(email)},
        {"$set": {"active": bool(active)}, "$inc": {"token_version": 1}},
    )
    return result.matched_count > 0


def set_user_password(email: str, password_hash: str) -> bool:
    """Replace a user's password hash. Bumps token_version so every existing
    session (on any device) is invalidated; the caller changing their own
    password is expected to be re-issued a fresh cookie in the same response."""
    result = _db()[USERS].update_one(
        {"_id": normalize_email(email)},
        {"$set": {"password_hash": password_hash, "password_changed_at": utc_now()}, "$inc": {"token_version": 1}},
    )
    return result.matched_count > 0


def seed_admin() -> None:
    """Create the bootstrap admin from ADMIN_EMAIL/ADMIN_PASSWORD if it doesn't
    already exist. Idempotent: an existing admin is left untouched unless
    ADMIN_FORCE_PASSWORD_RESET is set (recovery path for a forgotten password),
    which overwrites the hash and bumps token_version on the next boot."""
    if not ADMIN_EMAIL or not ADMIN_PASSWORD:
        logger.info("ADMIN_EMAIL/ADMIN_PASSWORD not set; skipping admin seed.")
        return
    existing = get_user(ADMIN_EMAIL)
    if existing is None:
        create_user(ADMIN_EMAIL, hash_password(ADMIN_PASSWORD), role="admin", created_by="seed")
        logger.info("Seeded admin user %s.", ADMIN_EMAIL)
        return
    if ADMIN_FORCE_PASSWORD_RESET:
        set_user_password(ADMIN_EMAIL, hash_password(ADMIN_PASSWORD))
        # Ensure the account is an active admin even if it was changed/disabled.
        _db()[USERS].update_one({"_id": normalize_email(ADMIN_EMAIL)}, {"$set": {"role": "admin", "active": True}})
        logger.warning("ADMIN_FORCE_PASSWORD_RESET applied: reset admin password for %s.", ADMIN_EMAIL)


def seed_test_user(email: str, password: str, role: str = "user", created_by: str = "seed") -> None:
    """Create a test user if it doesn't already exist. Useful for dev/test."""
    normalized = normalize_email(email)
    existing = get_user(normalized)
    if existing is None:
        try:
            validate_password(password)
            create_user(email, hash_password(password), role=role, created_by=created_by)
            logger.info("Seeded test user %s with role %s.", normalized, role)
        except ValueError as e:
            logger.warning("Failed to seed test user %s: %s", normalized, e)
