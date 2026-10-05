"""Data-access layer, backed entirely by MongoDB.

Every store that used to be a per-event SQLite file now lives in one shared
MongoDB database (see ``backend/storage/mongo.py``). Function signatures are kept
identical to the old SQLite implementation so callers (``backend/main.py``) are
unchanged; only the storage underneath moved. Documents map directly onto the
Pydantic models in ``backend/models.py`` — nested lists (OCR blocks, card sides)
become embedded arrays instead of separate tables.
"""

from __future__ import annotations

from typing import Any

from backend.models import BusinessCardRecord, EventOut, FieldCandidate, OCRSideResult
from backend.storage import mongo
from backend.storage.db import new_id, utc_now, validate_event_id

# Collection names (one Mongo database, these collections replace the SQLite tables).
EVENTS = "events"
CARDS = "cards"
OCR_RESULTS = "ocr_results"
FIELD_CANDIDATES = "field_candidates"
CARD_RECORDS = "card_records"
DUPLICATE_LINKS = "duplicate_links"
AUDITS = "audits"
# Insert-only audit of every scan attempt, keyed by user. Deliberately NOT
# cascaded by reset_event_data/delete_event so admin stats survive an event
# being wiped — that's the whole reason it's separate from card_records.
SCAN_LOG = "scan_log"

_RECORD_FIELDS = getattr(BusinessCardRecord, "model_fields", None) or getattr(BusinessCardRecord, "__fields__", {})


def _db():
    database = mongo.get_database()
    if database is None:
        raise RuntimeError(
            "MongoDB is unavailable — the app requires a reachable MONGODB_URI to store and read data."
        )
    return database


def ensure_indexes() -> None:
    """Create the indexes that mirror the old SQLite ones. Safe to call repeatedly."""
    db = mongo.get_database()
    if db is None:
        return
    db[CARDS].create_index("event_id")
    db[CARD_RECORDS].create_index("event_id")
    db[CARD_RECORDS].create_index("email")
    db[CARD_RECORDS].create_index("phone_primary")
    db[CARD_RECORDS].create_index("company")
    db[CARD_RECORDS].create_index("card_id", unique=True)
    db[DUPLICATE_LINKS].create_index("event_id")
    db[OCR_RESULTS].create_index("card_id")
    db[FIELD_CANDIDATES].create_index("card_id")
    db[CARD_RECORDS].create_index("scanned_by")
    db[SCAN_LOG].create_index([("scanned_by", 1), ("created_at", -1)])
    db[SCAN_LOG].create_index("event_id")


# --- Events -----------------------------------------------------------------

def ensure_event(event_id: str, name: str, date: str, location: str | None = None) -> str:
    validate_event_id(event_id)
    now = utc_now()
    _db()[EVENTS].update_one(
        {"_id": event_id},
        {
            "$set": {
                "event_id": event_id,
                "name": name,
                "date": date,
                "location": location,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    return event_id


def list_events() -> list[EventOut]:
    db = mongo.get_database()
    if db is None:
        return []
    docs = db[EVENTS].find(
        {}, {"event_id": 1, "name": 1, "date": 1, "location": 1, "booth": 1, "notes": 1}
    ).sort("created_at", 1)
    return [
        EventOut(
            event_id=doc["event_id"],
            name=doc.get("name", ""),
            date=doc.get("date", ""),
            location=doc.get("location"),
            booth=doc.get("booth"),
            notes=doc.get("notes"),
        )
        for doc in docs
    ]


def delete_event(event_id: str) -> dict[str, int]:
    """Delete the event itself and every document that belongs to it.

    Cascades across every collection that carries ``event_id`` (cards, card
    records, OCR results, field candidates, duplicate links, audits) and the
    event document itself. Images are NOT removed here — the caller (main.py)
    handles GridFS deletion via ``clear_event_images`` since that lives outside
    Mongo's collection API.
    """
    counts = reset_event_data(event_id)
    result = _db()[EVENTS].delete_one({"_id": event_id})
    counts["event"] = result.deleted_count
    return counts


def get_event(event_id: str) -> EventOut | None:
    doc = _db()[EVENTS].find_one({"_id": event_id})
    if not doc:
        return None
    return EventOut(
        event_id=doc["event_id"],
        name=doc.get("name", ""),
        date=doc.get("date", ""),
        location=doc.get("location"),
        booth=doc.get("booth"),
        notes=doc.get("notes"),
    )


# --- Cards ------------------------------------------------------------------

def create_card(event_id: str, processing_mode: str = "balanced") -> str:
    card_id = new_id("card")
    now = utc_now()
    _db()[CARDS].insert_one(
        {
            "_id": card_id,
            "card_id": card_id,
            "event_id": event_id,
            "status": "processing",
            "processing_mode": processing_mode,
            "confidence_score": None,
            "duplicate_flag": "No",
            "sides": [],
            "created_at": now,
            "updated_at": now,
        }
    )
    return card_id


def insert_card_side(
    event_id: str,
    *,
    card_id: str,
    side: str,
    filename: str,
    content_type: str | None,
    width: int | None = None,
    height: int | None = None,
    quality_score: str | None = None,
    quality_warnings: list[str] | None = None,
    file_id: Any = None,
) -> None:
    # Remove any existing entry for this side, then push the fresh one (mirrors
    # the old INSERT OR REPLACE on the UNIQUE(card_id, side) constraint).
    _db()[CARDS].update_one({"_id": card_id}, {"$pull": {"sides": {"side": side}}})
    _db()[CARDS].update_one(
        {"_id": card_id},
        {
            "$push": {
                "sides": {
                    "side": side,
                    "filename": filename,
                    "content_type": content_type,
                    "width": width,
                    "height": height,
                    "quality_score": quality_score,
                    "quality_warnings": quality_warnings or [],
                    "file_id": file_id,
                    "created_at": utc_now(),
                }
            },
            "$set": {"updated_at": utc_now()},
        },
    )


def insert_ocr_result(event_id: str, card_id: str, result: OCRSideResult) -> None:
    blocks = [
        {
            "line_index": block.line_index,
            "text": block.text,
            "confidence": block.confidence,
            "engine": block.engine or result.engine,
            "variant": block.variant or result.variant,
            "normalized_text": block.normalized_text,
            "bbox": block.bbox,
            "size_tag": block.size_tag,
            "position_band": block.position_band,
        }
        for block in result.blocks
    ]
    _db()[OCR_RESULTS].insert_one(
        {
            "_id": new_id("ocr"),
            "card_id": card_id,
            "event_id": event_id,
            "side": result.side,
            "engine": result.engine,
            "engine_version": result.engine_version,
            "variant": result.variant,
            "runtime_ms": result.runtime_ms,
            "status": result.status,
            "error_message": result.error_message,
            "raw_text": result.raw_text,
            "average_confidence": result.average_confidence,
            "blocks": blocks,
            "created_at": utc_now(),
        }
    )


def insert_field_candidates(event_id: str, card_id: str, candidates: list[FieldCandidate]) -> None:
    if not candidates:
        return
    now = utc_now()
    _db()[FIELD_CANDIDATES].insert_many(
        [
            {
                "_id": new_id("cand"),
                "card_id": card_id,
                "event_id": event_id,
                "field_name": candidate.field,
                "value": candidate.value,
                "confidence": candidate.confidence,
                "source": candidate.source,
                "evidence": candidate.evidence,
                "created_at": now,
            }
            for candidate in candidates
        ]
    )


def save_audit(event_id: str, card_id: str, kind: str, content: str) -> None:
    """Store an OCR/LLM debug transcript in Mongo instead of on disk."""
    _db()[AUDITS].update_one(
        {"card_id": card_id, "kind": kind},
        {
            "$set": {
                "card_id": card_id,
                "event_id": event_id,
                "kind": kind,
                "content": content,
                "created_at": utc_now(),
            }
        },
        upsert=True,
    )


# --- Card records -----------------------------------------------------------

def _record_to_doc(record: BusinessCardRecord, now: str) -> dict[str, Any]:
    data = record.model_dump() if hasattr(record, "model_dump") else record.dict()
    # date/time/event_name are derived on read, not stored on the document.
    for key in ("date", "time", "event_name"):
        data.pop(key, None)
    data["updated_at"] = now
    return data


def upsert_card_record(record: BusinessCardRecord) -> None:
    now = utc_now()
    doc = _record_to_doc(record, now)
    _db()[CARD_RECORDS].update_one(
        {"card_id": record.card_id},
        {"$set": doc, "$setOnInsert": {"_id": record.record_id, "created_at": now}},
        upsert=True,
    )
    _db()[CARDS].update_one(
        {"_id": record.card_id},
        {
            "$set": {
                "status": "needs_review" if record.confidence_score == "Low" else "processed",
                "confidence_score": record.confidence_score,
                "duplicate_flag": record.duplicate_flag,
                "updated_at": now,
            }
        },
    )


def _doc_to_record(doc: dict[str, Any], event_name: str) -> BusinessCardRecord:
    created_at = doc.get("created_at", "") or ""
    data = dict(doc)
    data["date"] = created_at[:10]
    data["time"] = created_at[11:19]
    data["event_name"] = event_name
    data["reviewed_by_user"] = bool(data.get("reviewed_by_user"))
    if not isinstance(data.get("low_confidence_fields"), list):
        data["low_confidence_fields"] = []
    return BusinessCardRecord(**{k: v for k, v in data.items() if k in _RECORD_FIELDS})


def list_records(event_id: str) -> list[BusinessCardRecord]:
    db = _db()
    event = db[EVENTS].find_one({"_id": event_id}, {"name": 1})
    event_name = event.get("name", "") if event else ""
    docs = db[CARD_RECORDS].find({"event_id": event_id}).sort("created_at", -1)
    return [_doc_to_record(doc, event_name) for doc in docs]


def _get_one_record(event_id: str, card_id: str) -> BusinessCardRecord:
    db = _db()
    doc = db[CARD_RECORDS].find_one({"card_id": card_id, "event_id": event_id})
    if not doc:
        raise KeyError(card_id)
    event = db[EVENTS].find_one({"_id": event_id}, {"name": 1})
    return _doc_to_record(doc, event.get("name", "") if event else "")


def update_record(event_id: str, card_id: str, values: dict[str, Any]) -> BusinessCardRecord:
    allowed = {
        "name", "designation", "company", "business", "phone_primary", "phone_number",
        "mobile_number", "phone_extra", "fax_number", "country_code", "email", "website",
        "address", "city", "state", "country", "zip_code", "category", "social_media",
        "notes", "email1", "email2", "contact1", "contact2", "contact3",
    }
    updates = {key: value for key, value in values.items() if key in allowed}
    if not updates:
        return _get_one_record(event_id, card_id)
    now = utc_now()
    updates["reviewed_by_user"] = True
    updates["updated_at"] = now
    result = _db()[CARD_RECORDS].update_one({"card_id": card_id, "event_id": event_id}, {"$set": updates})
    if result.matched_count == 0:
        raise KeyError(card_id)
    _db()[CARDS].update_one({"_id": card_id}, {"$set": {"status": "reviewed", "updated_at": now}})
    return _get_one_record(event_id, card_id)


def reset_event_data(event_id: str) -> dict[str, int]:
    db = _db()
    counts = {
        "cards": db[CARDS].count_documents({"event_id": event_id}),
        "records": db[CARD_RECORDS].count_documents({"event_id": event_id}),
    }
    db[DUPLICATE_LINKS].delete_many({"event_id": event_id})
    db[CARDS].delete_many({"event_id": event_id})
    db[CARD_RECORDS].delete_many({"event_id": event_id})
    db[OCR_RESULTS].delete_many({"event_id": event_id})
    db[FIELD_CANDIDATES].delete_many({"event_id": event_id})
    db[AUDITS].delete_many({"event_id": event_id})
    return counts


# --- Scan tracking (per-user, insert-only) ----------------------------------

def log_scan(event_id: str, event_name: str, card_id: str, scanned_by: str | None, status: str) -> None:
    """Record one scan attempt for admin stats. Best-effort: a logging failure
    must never fail the scan itself, so all errors are swallowed. ``day`` is
    precomputed from created_at so daily-grouping pipelines stay simple and
    work under mongomock in tests."""
    if not scanned_by:
        return
    try:
        now = utc_now()
        _db()[SCAN_LOG].insert_one(
            {
                "_id": new_id("scan"),
                "card_id": card_id,
                "event_id": event_id,
                "event_name": event_name,
                "scanned_by": scanned_by,
                "status": status,
                "created_at": now,
                "day": now[:10],
            }
        )
    except Exception:  # noqa: BLE001 — stats logging is non-critical
        pass


def scan_totals_by_user() -> list[dict[str, Any]]:
    """[{email, total, errors, last_scan_at}] across all events, busiest first."""
    pipeline = [
        {
            "$group": {
                "_id": "$scanned_by",
                "total": {"$sum": 1},
                "errors": {"$sum": {"$cond": [{"$eq": ["$status", "error"]}, 1, 0]}},
                "last_scan_at": {"$max": "$created_at"},
            }
        },
        {"$sort": {"total": -1}},
    ]
    return [
        {"email": row["_id"], "total": row["total"], "errors": row["errors"], "last_scan_at": row.get("last_scan_at")}
        for row in _db()[SCAN_LOG].aggregate(pipeline)
        if row.get("_id")
    ]


def scan_counts_by_user_event() -> list[dict[str, Any]]:
    """[{email, event_id, event_name, count}] — per-user breakdown by event."""
    pipeline = [
        {
            "$group": {
                "_id": {"u": "$scanned_by", "e": "$event_id"},
                "event_name": {"$first": "$event_name"},
                "count": {"$sum": 1},
            }
        },
        {"$sort": {"count": -1}},
    ]
    return [
        {
            "email": row["_id"]["u"],
            "event_id": row["_id"]["e"],
            "event_name": row.get("event_name") or "",
            "count": row["count"],
        }
        for row in _db()[SCAN_LOG].aggregate(pipeline)
        if row.get("_id", {}).get("u")
    ]


def scan_counts_by_user_day(days: int) -> list[dict[str, Any]]:
    """[{email, day, count}] for the last ``days`` days (ISO date string compare)."""
    from datetime import date, timedelta

    cutoff = (date.today() - timedelta(days=max(0, days - 1))).isoformat()
    pipeline = [
        {"$match": {"day": {"$gte": cutoff}}},
        {"$group": {"_id": {"u": "$scanned_by", "d": "$day"}, "count": {"$sum": 1}}},
        {"$sort": {"_id.d": 1}},
    ]
    return [
        {"email": row["_id"]["u"], "day": row["_id"]["d"], "count": row["count"]}
        for row in _db()[SCAN_LOG].aggregate(pipeline)
        if row.get("_id", {}).get("u")
    ]


def scan_stats_for_user(email: str, days: int) -> dict[str, Any]:
    """A single user's own totals + daily breakdown — the /me/stats scope, so a
    non-admin can see their own card-scanning activity without the admin-only
    /admin/stats endpoint (which returns every user's data)."""
    from datetime import date, timedelta

    cutoff = (date.today() - timedelta(days=max(0, days - 1))).isoformat()
    totals_pipeline = [
        {"$match": {"scanned_by": email}},
        {
            "$group": {
                "_id": "$scanned_by",
                "total": {"$sum": 1},
                "errors": {"$sum": {"$cond": [{"$eq": ["$status", "error"]}, 1, 0]}},
                "last_scan_at": {"$max": "$created_at"},
            }
        },
    ]
    totals = list(_db()[SCAN_LOG].aggregate(totals_pipeline))
    daily_pipeline = [
        {"$match": {"scanned_by": email, "day": {"$gte": cutoff}}},
        {"$group": {"_id": "$day", "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]
    daily = [{"day": row["_id"], "count": row["count"]} for row in _db()[SCAN_LOG].aggregate(daily_pipeline)]
    today = date.today().isoformat()
    return {
        "total": totals[0]["total"] if totals else 0,
        "errors": totals[0]["errors"] if totals else 0,
        "last_scan_at": totals[0].get("last_scan_at") if totals else None,
        "today": next((row["count"] for row in daily if row["day"] == today), 0),
        "daily": daily,
    }


def count_untracked_records() -> int:
    """card_records with no scanned_by — cards created before per-user tracking."""
    return _db()[CARD_RECORDS].count_documents(
        {"$or": [{"scanned_by": {"$exists": False}}, {"scanned_by": None}]}
    )


def find_duplicate_flag(event_id: str, *, email: str | None, phone: str | None, card_id: str) -> str:
    db = _db()
    if email and db[CARD_RECORDS].find_one(
        {"event_id": event_id, "email": email, "card_id": {"$ne": card_id}}, {"_id": 1}
    ):
        return "Exact"
    if phone and db[CARD_RECORDS].find_one(
        {"event_id": event_id, "phone_primary": phone, "card_id": {"$ne": card_id}}, {"_id": 1}
    ):
        return "Exact"
    return "No"
