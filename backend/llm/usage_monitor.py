from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from backend.config import (
    GEMINI_DAILY_REQUEST_LIMIT,
    GEMINI_DAILY_TOKEN_LIMIT,
    GEMINI_MINUTE_REQUEST_LIMIT,
    GOOGLE_VISION_FREE_UNITS_MONTHLY,
    GOOGLE_VISION_MINUTE_REQUEST_LIMIT,
    GOOGLE_VISION_PRICE_PER_1000,
)
from backend.storage import mongo, mongo_usage
from backend.storage.db import new_id, utc_now

LLM_USAGE = "llm_usage"

# Short server-side cache so the /llm-usage poll (fired after every scan) and the
# pre-call budget check don't each run a fresh aggregate against Atlas. This is
# server memory, not browser cache.
_snapshot_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL_SECONDS = 8.0


@dataclass(frozen=True)
class UsageSnapshot:
    daily_requests: int
    minute_requests: int
    daily_tokens: int
    daily_request_limit: int
    minute_request_limit: int
    daily_token_limit: int

    @property
    def allowed(self) -> bool:
        return (
            self.daily_requests < self.daily_request_limit
            and self.minute_requests < self.minute_request_limit
            and self.daily_tokens < self.daily_token_limit
        )


@dataclass(frozen=True)
class ProviderUsageSnapshot:
    provider: str
    daily_requests: int
    minute_requests: int
    monthly_units: int
    daily_tokens: int = 0
    daily_request_limit: int | None = None
    minute_request_limit: int | None = None
    daily_token_limit: int | None = None
    free_units_monthly: int | None = None
    estimated_cost_usd: float = 0.0
    by_key: dict[str, int] | None = None


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _usage_collection():
    db = mongo.get_database()
    return db[LLM_USAGE] if db is not None else None


def _aggregate_usage(provider: str, event_id: str | None) -> dict:
    """Return rolling usage totals for a provider from the Mongo llm_usage collection.

    Cached for a few seconds so repeated polls/pre-call checks stay cheap.
    """
    cache_key = f"{provider}:{event_id or '*'}"
    cached = _snapshot_cache.get(cache_key)
    if cached is not None and (time.monotonic() - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    empty = {"daily_requests": 0, "minute_requests": 0, "daily_tokens": 0,
             "monthly_units": 0, "estimated_cost_usd": 0.0, "by_key": {}}
    collection = _usage_collection()
    if collection is None:
        return empty

    now = datetime.now(UTC)
    day_ago = now - timedelta(days=1)
    minute_ago = now - timedelta(minutes=1)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    base = {"provider": provider}
    if event_id:
        base = {**base, "event_id": event_id}

    try:
        daily = list(collection.aggregate([
            {"$match": {**base, "created_at": {"$gte": day_ago}}},
            {"$group": {"_id": None,
                        "requests": {"$sum": "$request_count"},
                        "tokens": {"$sum": "$total_tokens"},
                        "cost": {"$sum": "$cost_estimate_usd"}}},
        ]))
        minute = list(collection.aggregate([
            {"$match": {**base, "created_at": {"$gte": minute_ago}}},
            {"$group": {"_id": None, "requests": {"$sum": "$request_count"}}},
        ]))
        monthly = list(collection.aggregate([
            {"$match": {**base, "created_at": {"$gte": month_start}}},
            {"$group": {"_id": None, "units": {"$sum": "$unit_count"}}},
        ]))
        by_key_rows = list(collection.aggregate([
            {"$match": {**base, "created_at": {"$gte": day_ago}}},
            {"$group": {"_id": {"$ifNull": ["$key_label", "default"]},
                        "requests": {"$sum": "$request_count"}}},
        ]))
    except Exception:  # noqa: BLE001 — never let a metrics read break a scan
        return cached[1] if cached is not None else empty

    result = {
        "daily_requests": int(daily[0]["requests"]) if daily else 0,
        "minute_requests": int(minute[0]["requests"]) if minute else 0,
        "daily_tokens": int(daily[0]["tokens"]) if daily else 0,
        "monthly_units": int(monthly[0]["units"]) if monthly else 0,
        "estimated_cost_usd": float(daily[0]["cost"]) if daily else 0.0,
        "by_key": {row["_id"]: int(row["requests"]) for row in by_key_rows},
    }
    _snapshot_cache[cache_key] = (time.monotonic(), result)
    return result


def usage_snapshot(event_id: str | None = None) -> UsageSnapshot:
    totals = _aggregate_usage("gemini", event_id)
    return UsageSnapshot(
        daily_requests=totals["daily_requests"],
        minute_requests=totals["minute_requests"],
        daily_tokens=totals["daily_tokens"],
        daily_request_limit=GEMINI_DAILY_REQUEST_LIMIT,
        minute_request_limit=GEMINI_MINUTE_REQUEST_LIMIT,
        daily_token_limit=GEMINI_DAILY_TOKEN_LIMIT,
    )


def provider_usage_snapshot(provider: str, event_id: str | None = None) -> ProviderUsageSnapshot:
    totals = _aggregate_usage(provider, event_id)
    if provider == "gemini":
        return ProviderUsageSnapshot(
            provider=provider,
            daily_requests=totals["daily_requests"],
            minute_requests=totals["minute_requests"],
            daily_tokens=totals["daily_tokens"],
            monthly_units=totals["monthly_units"],
            estimated_cost_usd=totals["estimated_cost_usd"],
            daily_request_limit=GEMINI_DAILY_REQUEST_LIMIT,
            minute_request_limit=GEMINI_MINUTE_REQUEST_LIMIT,
            daily_token_limit=GEMINI_DAILY_TOKEN_LIMIT,
            by_key=totals["by_key"],
        )
    if provider == "google_vision":
        monthly_billable = max(0, totals["monthly_units"] - GOOGLE_VISION_FREE_UNITS_MONTHLY)
        return ProviderUsageSnapshot(
            provider=provider,
            daily_requests=totals["daily_requests"],
            minute_requests=totals["minute_requests"],
            daily_tokens=0,
            monthly_units=totals["monthly_units"],
            minute_request_limit=GOOGLE_VISION_MINUTE_REQUEST_LIMIT,
            free_units_monthly=GOOGLE_VISION_FREE_UNITS_MONTHLY,
            estimated_cost_usd=round((monthly_billable / 1000) * GOOGLE_VISION_PRICE_PER_1000, 4),
            by_key=totals["by_key"],
        )
    return ProviderUsageSnapshot(
        provider=provider,
        daily_requests=totals["daily_requests"],
        minute_requests=totals["minute_requests"],
        daily_tokens=totals["daily_tokens"],
        monthly_units=totals["monthly_units"],
        estimated_cost_usd=totals["estimated_cost_usd"],
        by_key=totals["by_key"],
    )


def record_usage(
    event_id: str,
    *,
    provider: str = "gemini",
    model: str,
    purpose: str,
    prompt_tokens: int,
    completion_tokens: int = 0,
    request_count: int = 1,
    unit_count: int = 0,
    cost_estimate_usd: float = 0.0,
    key_label: str | None = None,
    status: str = "ok",
    error_message: str | None = None,
) -> None:
    total_tokens = prompt_tokens + completion_tokens
    collection = _usage_collection()
    if collection is not None:
        try:
            collection.insert_one(
                {
                    "_id": new_id("usage"),
                    "event_id": event_id,
                    "provider": provider,
                    "model": model,
                    "purpose": purpose,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "request_count": request_count,
                    "unit_count": unit_count,
                    "cost_estimate_usd": cost_estimate_usd,
                    "status": status,
                    "error_message": error_message,
                    "key_label": key_label,
                    # Real datetime so the rolling-window aggregations can range-query it.
                    "created_at": datetime.now(UTC),
                    "created_at_iso": utc_now(),
                }
            )
        except Exception:  # noqa: BLE001 — usage logging must never break a scan
            pass

    # Keep the 24/7 MongoDB budget counters in sync with every real API attempt.
    # Local-limit/config skips do not touch external APIs, so they should not
    # consume the persistent budget.
    if status not in {"blocked_local_limit", "skipped_not_configured"}:
        if provider == "google_vision":
            mongo_usage.increment(provider, amount=unit_count or request_count)
        elif provider == "gemini":
            mongo_usage.increment(provider, amount=request_count, token_count=total_tokens)
