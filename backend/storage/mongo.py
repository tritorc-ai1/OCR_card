"""Single shared MongoDB connection for the whole app.

Every durable store (events, cards, OCR results, field candidates, business-card
records, usage counters) and the card images (via GridFS) live in one MongoDB
database now — Render's local disk is ephemeral and space-constrained, so nothing
is persisted to disk anymore. This module owns the ONE ``MongoClient`` (and its
connection pool) that all of that shares, so we never open more pools than the
Atlas M0 free tier's connection cap allows.

The TLS-hardening patch here is the same fix proven in ``mongo_usage`` earlier:
Atlas's TLS terminator rejects OpenSSL-3 / TLS-1.3 handshakes from Render with
TLSV1_ALERT_INTERNAL_ERROR, so we force TLS 1.2 by patching pymongo's internal
SSL-context factory (there is no public MongoClient kwarg to inject a context).
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from backend.config import MONGODB_DB_NAME, MONGODB_URI

logger = logging.getLogger(__name__)

_client = None
_db = None
_gridfs_bucket = None

# Circuit breaker: after a failed connect, don't retry (and eat another
# multi-second server-selection timeout) until this cooldown passes.
_RETRY_COOLDOWN_SECONDS = 30.0
_next_retry_at = 0.0

_ssl_patch_applied = False


def is_configured() -> bool:
    return bool(MONGODB_URI)


def _uri_has_option(uri: str, names: set[str]) -> bool:
    try:
        query = urlsplit(uri).query
    except ValueError:
        return False
    return bool({key.lower() for key, _value in parse_qsl(query, keep_blank_values=True)} & names)


def _harden_ssl_context(ctx):
    """Force TLS 1.2 max + a wider cipher list (Atlas/Render OpenSSL-3 fix)."""
    import ssl

    try:
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    except AttributeError:
        pass
    try:
        ctx.set_ciphers("HIGH:!aNULL:!MD5:@SECLEVEL=1")
    except ssl.SSLError:
        pass
    return ctx


def _ensure_ssl_hardening_patch() -> None:
    global _ssl_patch_applied
    if _ssl_patch_applied:
        return
    try:
        import pymongo.client_options as _client_options

        _original = _client_options.get_ssl_context

        def _patched(*args, **kwargs):
            return _harden_ssl_context(_original(*args, **kwargs))

        _client_options.get_ssl_context = _patched
        _ssl_patch_applied = True
    except Exception as patch_err:  # noqa: BLE001
        logger.debug("Could not patch pymongo ssl context: %s", patch_err)


def get_client():
    """Return the shared MongoClient, building it once. None if unconfigured/unreachable."""
    global _client, _next_retry_at
    if not MONGODB_URI:
        return None
    if _client is not None:
        return _client
    if time.monotonic() < _next_retry_at:
        return None
    try:
        from pymongo import MongoClient
        from pymongo.server_api import ServerApi
        import certifi

        options: dict[str, Any] = {
            "server_api": ServerApi("1"),
            "serverSelectionTimeoutMS": 5000,
            "connectTimeoutMS": 10000,
            # Larger socket timeout than the usage counters used, since GridFS
            # image up/downloads move more bytes than a tiny counter doc.
            "socketTimeoutMS": 30000,
            "tls": True,
            "tlsCAFile": certifi.where(),
        }
        if _uri_has_option(MONGODB_URI, {"tls", "ssl"}):
            options.pop("tls", None)
            options.pop("tlsCAFile", None)
        _ensure_ssl_hardening_patch()
        _client = MongoClient(MONGODB_URI, **options)
        return _client
    except Exception as exc:  # noqa: BLE001
        _next_retry_at = time.monotonic() + _RETRY_COOLDOWN_SECONDS
        logger.warning("MongoDB connection unavailable: %s", exc)
        return None


def get_database():
    """Return the shared database handle, or None if Mongo is unavailable."""
    global _db
    client = get_client()
    if client is None:
        return None
    if _db is None:
        _db = client[MONGODB_DB_NAME]
    return _db


def get_gridfs():
    """Return the shared GridFS bucket for card images, or None if unavailable."""
    global _gridfs_bucket
    db = get_database()
    if db is None:
        return None
    if _gridfs_bucket is None:
        import gridfs

        _gridfs_bucket = gridfs.GridFSBucket(db)
    return _gridfs_bucket


def reset_client() -> None:
    """Drop the cached client/db/bucket so the next call rebuilds (used on hard errors)."""
    global _client, _db, _gridfs_bucket
    if _client is not None:
        try:
            _client.close()
        except Exception:  # noqa: BLE001
            pass
    _client = None
    _db = None
    _gridfs_bucket = None
