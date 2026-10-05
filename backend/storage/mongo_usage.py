"""Persistent, restart-surviving API usage counters backed by MongoDB.

Render's filesystem is ephemeral, so the per-event SQLite ``llm_usage`` tables
reset on every restart and cannot enforce a real monthly/daily budget. This
module keeps the authoritative counters in MongoDB instead:

* Google Vision -> one document per calendar month (period "2026-07")
* Gemini        -> one document per day            (period "2026-07-09")

Each document is a bucket with ``provider``, ``period``, ``count`` and optional
``tokens`` fields. Counts are bumped with an atomic ``$inc`` upsert, so
concurrent workers do not lose increments. A TTL index on ``expire_at`` lets
MongoDB auto-delete old buckets after they are no longer needed.
"""

from __future__ import annotations

import logging
import queue as _queue
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from backend.config import (
    MONGO_GEMINI_DAILY_LIMIT,
    MONGO_USAGE_ENABLED,
    MONGO_USAGE_FAIL_CLOSED,
    MONGO_USAGE_TTL_DAYS,
    MONGO_VISION_MONTHLY_LIMIT,
    MONGODB_DB_NAME,
    MONGODB_URI,
)

logger = logging.getLogger(__name__)

COLLECTION_NAME = "usage_counters"
UNAVAILABLE_MESSAGE = "MongoDB usage tracker is unavailable"
FALLBACK_MESSAGE = (
    "MongoDB is configured but unreachable. Scans continue with local counters, "
    "but the persistent 24/7 tracker is not updating."
)

# provider -> ("month" | "day", limit, display label, unit label)
_PROVIDER_PERIODS = {
    "google_vision": ("month", MONGO_VISION_MONTHLY_LIMIT, "Google Vision", "OCR units"),
    "gemini": ("day", MONGO_GEMINI_DAILY_LIMIT, "Gemini", "requests"),
}

_client = None
_index_ready = False
_last_error: str | None = None

# Reuse the client across calls; only tear it down after repeated hard failures
# (PyMongo reconnects on its own for transient blips). Closing before discard
# releases the old connection pool/monitor threads instead of leaking them.
_consecutive_failures = 0
_MAX_FAILURES_BEFORE_RESET = 3

# Circuit breaker: after a connection failure, don't retry (and eat another
# multi-second timeout) until this cooldown passes. Keeps scans fast during a
# Mongo outage instead of stalling every request on server selection.
_RETRY_COOLDOWN_SECONDS = 30.0
_next_retry_at = 0.0

# Short-lived in-memory cache of per-period counts, keyed "provider:period".
# Value is (monotonic_ts, used, tokens). Keeps reads off the request path and
# lets the live meter reflect writes immediately via optimistic bumps.
_read_cache: dict[str, tuple[float, int, int]] = {}
_CACHE_TTL_SECONDS = 30.0

# Mongo writes run on a single daemon thread so a scan never blocks on the
# round-trip. increment() enqueues and returns immediately.
_write_queue: "_queue.Queue[tuple]" = _queue.Queue()
_writer_thread: threading.Thread | None = None
_writer_lock = threading.Lock()


@dataclass(frozen=True)
class MongoUsage:
    """Snapshot of one provider's current-period usage from MongoDB."""

    provider: str
    period_kind: str  # "month" or "day"
    period: str  # e.g. "2026-07" or "2026-07-09"
    used: int
    limit: int
    tokens: int = 0

    @property
    def hit_limit(self) -> bool:
        return self.limit > 0 and self.used >= self.limit

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


def is_enabled() -> bool:
    return bool(MONGO_USAGE_ENABLED and MONGODB_URI)


def is_unavailable_reason(reason: str | None) -> bool:
    return bool(reason and reason.startswith(UNAVAILABLE_MESSAGE))


def period_key(kind: str, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    if kind == "month":
        return now.strftime("%Y-%m")
    return now.strftime("%Y-%m-%d")


def _provider_label(provider: str) -> str:
    return _PROVIDER_PERIODS.get(provider, ("", 0, provider, ""))[2]


def _provider_unit(provider: str) -> str:
    return _PROVIDER_PERIODS.get(provider, ("", 0, "", "units"))[3]


def _unavailable_reason() -> str:
    detail = f": {_last_error}" if _last_error else ""
    return f"{UNAVAILABLE_MESSAGE}{detail}. Processing is paused so API limits stay accurate."


def _error_summary() -> str | None:
    if not _last_error:
        return None
    if "SSL handshake failed" in _last_error:
        return "Atlas TLS handshake failed from this server."
    if "timed out" in _last_error.lower() or "timeout" in _last_error.lower():
        return "MongoDB connection timed out from this server."
    return "MongoDB connection failed from this server."


def _uri_has_option(uri: str, names: set[str]) -> bool:
    try:
        query = urlsplit(uri).query
    except ValueError:
        return False
    return bool({key.lower() for key, _value in parse_qsl(query, keep_blank_values=True)} & names)


def _harden_ssl_context(ctx):
    """Force TLS 1.2 as the max version and widen the cipher list.

    Root cause of TLSV1_ALERT_INTERNAL_ERROR on Render:
    - Render uses Linux with OpenSSL 3.x.
    - OpenSSL 3.x changed default TLS 1.3 behaviour/extensions that trigger
      Atlas's TLS terminator to reject the handshake with an internal error.
    - Forcing TLS 1.2 as the maximum version avoids this incompatibility.
    - Using SECLEVEL=1 widens the accepted cipher list for older Atlas configs.
    """
    import ssl

    try:
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    except AttributeError:
        pass  # ssl.TLSVersion not available on very old Python builds
    try:
        ctx.set_ciphers("HIGH:!aNULL:!MD5:@SECLEVEL=1")
    except ssl.SSLError:
        pass
    return ctx


_ssl_patch_applied = False


def _ensure_ssl_hardening_patch() -> None:
    """Patch pymongo's internal SSL context builder to harden every context it creates.

    PyMongo's MongoClient has no public option for injecting a pre-built
    ssl.SSLContext — neither "ssl_context" nor "tlsSSLContext" is a
    recognized keyword (both raise ConfigurationError; PyMongo only exposes
    the tls*/ssl string/bool options listed in pymongo.common.VALIDATORS).
    Patching the context factory it calls internally is the only way to
    force TLS 1.2, short of vendoring pymongo's TLS setup.
    """
    global _ssl_patch_applied
    if _ssl_patch_applied:
        return
    try:
        import pymongo.client_options as _client_options

        _original_get_ssl_context = _client_options.get_ssl_context

        def _patched_get_ssl_context(*args, **kwargs):
            return _harden_ssl_context(_original_get_ssl_context(*args, **kwargs))

        _client_options.get_ssl_context = _patched_get_ssl_context
        _ssl_patch_applied = True
    except Exception as patch_err:
        logger.debug("Could not patch pymongo ssl context: %s", patch_err)


def _get_collection():
    """Return the usage collection, or None if Mongo is unavailable/disabled."""
    global _client, _index_ready, _last_error, _consecutive_failures, _next_retry_at
    if not is_enabled():
        _last_error = None
        return None
    # During a known outage, skip the (re)connect attempt until the cooldown
    # elapses so scans don't stall on a server-selection timeout every time.
    if _client is None and time.monotonic() < _next_retry_at:
        return None
    try:
        # Reuse the existing client — PyMongo reconnects on its own, so a
        # transient blip should not trigger a full rebuild (which leaks the old
        # connection pool and monitor threads and churns the M0 connection cap).
        if _client is None:
            _index_ready = False
            # Import here so the app still starts if pymongo is missing.
            from pymongo import MongoClient
            from pymongo.server_api import ServerApi

            import certifi

            client_options: dict[str, Any] = {
                "server_api": ServerApi("1"),
                "serverSelectionTimeoutMS": 5000,
                "connectTimeoutMS": 5000,
                "socketTimeoutMS": 5000,
                "tls": True,
                "tlsCAFile": certifi.where(),
            }
            # Remove tls option if URI already specifies it to avoid conflicts.
            if _uri_has_option(MONGODB_URI, {"tls", "ssl"}):
                client_options.pop("tls", None)
                client_options.pop("tlsCAFile", None)

            # Force TLS 1.2 to avoid OpenSSL 3.x / TLS 1.3 handshake failures
            # with Atlas on Render (see _harden_ssl_context docstring).
            _ensure_ssl_hardening_patch()

            _client = MongoClient(MONGODB_URI, **client_options)

        collection = _client[MONGODB_DB_NAME][COLLECTION_NAME]
        if not _index_ready:
            collection.create_index([("provider", 1), ("period", 1)], unique=True)
            collection.create_index("expire_at", expireAfterSeconds=0)
            _index_ready = True
        _last_error = None
        _consecutive_failures = 0
        return collection
    except Exception as exc:  # noqa: BLE001
        _last_error = str(exc)
        _consecutive_failures += 1
        _next_retry_at = time.monotonic() + _RETRY_COOLDOWN_SECONDS
        logger.warning("Mongo usage unavailable: %s", exc)
        # Only tear the client down after repeated failures, and close it first
        # so the old pool is released rather than leaked.
        if _consecutive_failures >= _MAX_FAILURES_BEFORE_RESET and _client is not None:
            try:
                _client.close()
            except Exception:  # noqa: BLE001
                pass
            _client = None
            _index_ready = False
            _consecutive_failures = 0
        return None


def initialize() -> bool:
    """Warm up the Mongo connection and indexes. Returns True when available."""
    return _get_collection() is not None


def config_report() -> dict:
    """Mongo tracker configuration for cheap health checks.

    This intentionally avoids opening a MongoDB connection. Render calls
    ``/health`` during deploys, and a slow Atlas connection should not keep the
    app from booting.
    """
    report: dict[str, Any] = {
        "enabled": is_enabled(),
        "configured": bool(MONGODB_URI),
        "database": MONGODB_DB_NAME,
        "collection": COLLECTION_NAME,
        "fail_closed": False,
        "available": False,
        "checked": False,
        "blocking_scans": False,
    }
    if _error_summary():
        report["error_summary"] = _error_summary()
    return report


def get_usage(provider: str, now: datetime | None = None) -> MongoUsage | None:
    """Current-period usage for a provider, or None if tracking is unavailable.

    Reads are served from a short-lived in-memory cache so repeated scans and
    UI polls don't each hit MongoDB. Only a cache miss (older than the TTL)
    triggers a real query. When Mongo is unreachable, a stale cached value is
    served rather than nothing.
    """
    spec = _PROVIDER_PERIODS.get(provider)
    if spec is None:
        return None
    kind, limit, _label, _unit = spec
    period = period_key(kind, now)
    cache_key = f"{provider}:{period}"

    cached = _read_cache.get(cache_key)
    if cached is not None and (time.monotonic() - cached[0]) < _CACHE_TTL_SECONDS:
        return MongoUsage(provider=provider, period_kind=kind, period=period, used=cached[1], limit=limit, tokens=cached[2])

    collection = _get_collection()
    if collection is None:
        if cached is not None:
            return MongoUsage(provider=provider, period_kind=kind, period=period, used=cached[1], limit=limit, tokens=cached[2])
        return None
    try:
        doc = collection.find_one({"provider": provider, "period": period})
    except Exception as exc:  # noqa: BLE001
        global _last_error
        _last_error = str(exc)
        logger.warning("Mongo get_usage failed: %s", exc)
        if cached is not None:
            return MongoUsage(provider=provider, period_kind=kind, period=period, used=cached[1], limit=limit, tokens=cached[2])
        return None
    used = int(doc.get("count", 0)) if doc else 0
    tokens = int(doc.get("tokens", 0)) if doc else 0
    _read_cache[cache_key] = (time.monotonic(), used, tokens)
    return MongoUsage(provider=provider, period_kind=kind, period=period, used=used, limit=limit, tokens=tokens)


def check_limits(required: dict[str, int] | None = None, now: datetime | None = None) -> tuple[bool, str | None]:
    """Return whether the requested API work may start.

    ``required`` contains the units a new action expects to consume, for example
    ``{"google_vision": 2, "gemini": 1}`` for a two-sided card.
    """
    if not is_enabled():
        return True, None

    required = required or {}
    for provider in _PROVIDER_PERIODS:
        usage = get_usage(provider, now)
        # None means Mongo is unavailable for this provider -> fail open so
        # scanning continues (local counters still track usage).
        if usage is None:
            continue
        amount = max(0, int(required.get(provider, 0) or 0))
        projected = usage.used + amount
        if usage.limit > 0 and projected > usage.limit:
            label = "monthly" if usage.period_kind == "month" else "daily"
            needed = f"; this scan needs {amount} {_provider_unit(provider)}" if amount else ""
            return False, (
                f"{_provider_label(provider)} {label} limit reached "
                f"({usage.used}/{usage.limit} used for {usage.period}{needed}). "
                "Processing is paused until the counter resets."
            )
    return True, None


def _ensure_writer() -> None:
    """Start the background Mongo-write daemon thread if it isn't running."""
    global _writer_thread
    if _writer_thread is not None and _writer_thread.is_alive():
        return
    with _writer_lock:
        if _writer_thread is not None and _writer_thread.is_alive():
            return
        thread = threading.Thread(target=_writer_loop, name="mongo-usage-writer", daemon=True)
        thread.start()
        _writer_thread = thread


def _writer_loop() -> None:
    while True:
        args = _write_queue.get()
        try:
            _apply_increment(*args)
        except Exception as exc:  # noqa: BLE001
            global _last_error
            _last_error = str(exc)
            logger.warning("Mongo background increment failed: %s", exc)
        finally:
            _write_queue.task_done()


def _apply_increment(
    provider: str,
    period: str,
    kind: str,
    amount: int,
    token_count: int,
    expire_at: datetime,
    now: datetime,
) -> None:
    """Perform the real ``$inc`` upsert. Runs only on the writer thread."""
    if amount <= 0 and token_count <= 0:
        return
    collection = _get_collection()
    if collection is None:
        return
    inc: dict[str, Any] = {}
    if amount > 0:
        inc["count"] = amount
    if token_count > 0:
        inc["tokens"] = token_count
    if not inc:
        return
    update: dict[str, Any] = {
        "$inc": inc,
        "$setOnInsert": {
            "provider": provider,
            "period": period,
            "period_kind": kind,
            "expire_at": expire_at,
            "created_at": now,
        },
        "$set": {"updated_at": now},
    }
    collection.update_one({"provider": provider, "period": period}, update, upsert=True)
    # Refresh the cache with the authoritative post-write count.
    try:
        doc = collection.find_one({"provider": provider, "period": period})
        if doc is not None:
            _read_cache[f"{provider}:{period}"] = (
                time.monotonic(),
                int(doc.get("count", 0)),
                int(doc.get("tokens", 0)),
            )
    except Exception:  # noqa: BLE001
        pass


def increment(provider: str, amount: int = 1, token_count: int = 0, now: datetime | None = None) -> None:
    """Queue a usage increment for a provider's current-period bucket.

    The actual Mongo write happens on a background thread so the caller (and
    therefore the scan request) never blocks on the round-trip. The in-memory
    cache is bumped optimistically so the live meter reflects the change at once.
    """
    if amount <= 0 and token_count <= 0:
        return
    spec = _PROVIDER_PERIODS.get(provider)
    if spec is None or not is_enabled():
        return
    kind = spec[0]
    now = now or datetime.now(UTC)
    period = period_key(kind, now)
    expire_at = now + timedelta(days=MONGO_USAGE_TTL_DAYS)

    amount = max(0, amount)
    token_count = max(0, token_count)

    # Optimistic local bump so repeated scans show live counts between reads.
    cache_key = f"{provider}:{period}"
    cached = _read_cache.get(cache_key)
    if cached is not None:
        _read_cache[cache_key] = (time.monotonic(), cached[1] + amount, cached[2] + token_count)

    _ensure_writer()
    _write_queue.put((provider, period, kind, amount, token_count, expire_at, now))


def usage_report(now: datetime | None = None) -> dict:
    """Serializable snapshot of all tracked providers for the /llm-usage payload."""
    report: dict[str, Any] = {
        "enabled": is_enabled(),
        "configured": bool(MONGODB_URI),
        "database": MONGODB_DB_NAME,
        "collection": COLLECTION_NAME,
        "fail_closed": False,
        "blocking_scans": False,
        "available": False,
    }
    if not is_enabled():
        return report

    any_available = False
    for provider in _PROVIDER_PERIODS:
        usage = get_usage(provider, now)
        if usage is None:
            continue
        any_available = True
        report[provider] = {
            "period_kind": usage.period_kind,
            "period": usage.period,
            "used": usage.used,
            "limit": usage.limit,
            "remaining": usage.remaining,
            "hit_limit": usage.hit_limit,
            "tokens_estimated": usage.tokens,
        }
    report["available"] = any_available
    if not any_available and _last_error:
        report["error"] = _last_error
        report["error_summary"] = _error_summary()
        report["fallback_message"] = FALLBACK_MESSAGE
    return report
