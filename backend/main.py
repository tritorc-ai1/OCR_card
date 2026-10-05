from __future__ import annotations

import asyncio
import hashlib
from datetime import date
from io import BytesIO
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend.config import (
    DEFAULT_EVENT_DATE,
    DEFAULT_EVENT_ID,
    DEFAULT_EVENT_NAME,
    GEMINI_DAILY_REQUEST_LIMIT_PER_PROJECT,
    GEMINI_DAILY_TOKEN_LIMIT_PER_PROJECT,
    GEMINI_MINUTE_REQUEST_LIMIT_PER_PROJECT,
    GEMINI_PROJECT_COUNT,
    GEMINI_USE_IMAGE,
    MAX_UPLOAD_BYTES,
)
from backend.extraction.candidate_extractors import extract_candidates
from backend.imaging.preprocess import compress_for_storage, preprocess_image
from backend.llm.gemini_client import (
    gemini_key_labels,
    is_gemini_configured,
    structure_card_image,
    structure_card_text,
    structure_card_text_deterministic,
)
from backend.llm.usage_monitor import provider_usage_snapshot, usage_snapshot
from backend.models import BusinessCardRecord, EventIn, EventOut, ProcessingResult, UpdateRecordIn
from backend.storage import mongo, mongo_usage
from backend.ocr.google_vision import extract_text_combined as google_vision_extract_combined
from backend.ocr.google_vision import is_google_vision_configured
from backend.auth import require_admin, require_user
from backend.auth_routes import router as auth_router
from backend.storage.users import seed_admin, seed_test_user
from backend.storage.db import new_id, utc_now
from backend.storage.excel_writer import build_workbook_bytes
from backend.storage.repositories import (
    create_card,
    delete_event as delete_event_data,
    ensure_event,
    ensure_indexes,
    find_duplicate_flag,
    get_event,
    insert_field_candidates,
    insert_ocr_result,
    insert_card_side,
    list_events,
    list_records,
    log_scan,
    reset_event_data,
    save_audit,
    update_record,
    upsert_card_record,
)

IMAGES_BUCKET_FIELD = "images"

app = FastAPI(title="LLM Business Card Scanner")
app.include_router(auth_router)

STATIC_DIR = Path("frontend")
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


_NO_HEURISTIC_CACHE_PATHS = {"/", "/manifest.webmanifest"}


@app.middleware("http")
async def no_heuristic_static_caching(request, call_next):
    """FileResponse/StaticFiles send Last-Modified/ETag but no Cache-Control,
    so browsers fall back to heuristic freshness (commonly ~10% of the
    Last-Modified age) — for a file last touched days ago that can mean
    hours (or, for something untouched even longer, much more) of a stale
    JS/CSS/HTML file being served with ZERO request ever reaching this
    server, invisible to any server-side fix (no log line, no cache-busting
    query string helps, nothing). Force revalidation on every request to the
    app shell instead: still cheap (a 304 when unchanged), but guarantees a
    real edit is never missed on the next load."""
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path in _NO_HEURISTIC_CACHE_PATHS:
        response.headers["Cache-Control"] = "no-cache"
    return response

# Cached in-process; recomputed only if any (path, size, mtime) actually
# differs from last time. Avoids re-hashing the whole static/ tree on every
# single /sw.js request (which fires on every page load) while still picking
# up any real edit immediately.
_static_version_cache: dict[str, str] = {"fingerprint": "", "version": ""}


def static_assets_version() -> str:
    """Hash every file under static/ (path + size + mtime) into one short
    version string. Used to bust the service worker's precache automatically
    whenever any app-shell asset changes, instead of relying on a developer
    to remember to hand-bump a version constant in sw.js (see docs/README —
    that manual step was silently skipped often enough that real users kept
    running stale, sometimes-broken cached JS after a deploy)."""
    if not STATIC_DIR.exists():
        return "0"
    entries = sorted(
        f"{path.relative_to(STATIC_DIR)}:{path.stat().st_size}:{path.stat().st_mtime_ns}"
        for path in STATIC_DIR.rglob("*")
        if path.is_file()
    )
    fingerprint = "\n".join(entries)
    if fingerprint != _static_version_cache["fingerprint"]:
        _static_version_cache["fingerprint"] = fingerprint
        _static_version_cache["version"] = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12]
    return _static_version_cache["version"]


def _seed_mongo_on_startup() -> None:
    # All durable data lives in MongoDB now (no local disk). Create indexes and
    # seed the default event; tolerate a slow/unreachable Mongo at boot so the
    # health check can still report status instead of crashing the process.
    try:
        ensure_indexes()
        ensure_event(DEFAULT_EVENT_ID, DEFAULT_EVENT_NAME, DEFAULT_EVENT_DATE, "Local")
        seed_admin()
        # Seed a test user for dev/demo with admin privileges — kept permanent
        # (see PROTECTED_ADMIN_EMAIL) so the team always has a working login.
        from backend.config import PROTECTED_ADMIN_EMAIL

        seed_test_user(PROTECTED_ADMIN_EMAIL, "Tritorc@123", role="admin", created_by="seed")
    except Exception:  # noqa: BLE001
        pass


@app.on_event("startup")
async def startup() -> None:
    # ensure_indexes() + ensure_event() make 10 sequential round trips over the
    # SYNCHRONOUS pymongo driver (create_index x9, one upsert). Awaited inline
    # here they would block the entire asyncio event loop — not just "be
    # slow" — delaying every other request, including the very health check
    # Render polls to decide the service has finished a cold start. Run them
    # in a background thread instead so the app can start accepting traffic
    # immediately; indexes/the default event exist within a second or two
    # either way, and every reader already tolerates Mongo being briefly
    # unavailable.
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _seed_mongo_on_startup)


@app.get("/")
async def index() -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="UI is not available")
    return FileResponse(index_path)


@app.get("/manifest.webmanifest")
async def manifest() -> FileResponse:
    manifest_path = STATIC_DIR / "manifest.webmanifest"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Manifest is not available")
    return FileResponse(manifest_path, media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker() -> Response:
    sw_path = STATIC_DIR / "sw.js"
    if not sw_path.exists():
        raise HTTPException(status_code=404, detail="Service worker is not available")
    content = sw_path.read_text(encoding="utf-8")
    content = content.replace("__CACHE_VERSION__", static_assets_version())
    # No-cache (not just no-store) so the browser always revalidates the SW
    # script itself with the server on every check — the one request that
    # must never be served stale, since it's what decides whether every other
    # cached asset gets refreshed at all.
    return Response(content=content, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "processing_mode": "google_vision_ocr_gemini_text",
        "gemini_configured": is_gemini_configured(),
        "gemini_key_count": len(gemini_key_labels()),
        "google_vision_configured": is_google_vision_configured(),
        "mongo_usage": mongo_usage.config_report(),
        "storage": "mongodb",
        "default_event_id": DEFAULT_EVENT_ID,
    }


@app.get("/events", response_model=list[EventOut])
async def api_list_events(user: dict = Depends(require_user)) -> list[EventOut]:
    return list_events()


def slugify(value: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_")
    return "_".join(part for part in slug.split("_") if part)[:48] or f"event_{date.today().isoformat()}"


@app.post("/events", response_model=EventOut)
async def api_create_event(payload: EventIn, admin: dict = Depends(require_admin)) -> EventOut:
    event_id = slugify(f"{payload.name}_{payload.date}")
    ensure_event(event_id, payload.name, payload.date, payload.location)
    event = get_event(event_id)
    if not event:
        raise HTTPException(status_code=500, detail="Event was not created")
    return event


@app.delete("/events/{event_id}")
async def api_delete_event(event_id: str, admin: dict = Depends(require_admin)) -> dict:
    if not get_event(event_id):
        raise HTTPException(status_code=404, detail="Event not found")
    database_counts = delete_event_data(event_id)
    images_removed = clear_event_images(event_id)
    return {
        "event_id": event_id,
        "status": "deleted",
        "deleted": {
            **database_counts,
            "images": images_removed,
        },
    }


async def read_upload(file: UploadFile | None) -> bytes | None:
    if file is None:
        return None
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"{file.filename} is larger than the upload limit")
    return data


def save_side_file(event_id: str, card_id: str, side: str, upload: UploadFile, image_bytes: bytes) -> str:
    """Compress the upload and store it in GridFS. Returns the logical filename.

    The stored copy is a downscaled/re-encoded JPEG (see compress_for_storage) so
    the database stays small; the original full-size bytes are never persisted.
    """
    filename = f"{card_id}_{side}.jpg"
    stored = compress_for_storage(image_bytes)
    bucket = mongo.get_gridfs()
    if bucket is None:
        raise HTTPException(status_code=503, detail="Image storage (MongoDB) is unavailable")
    # Drop any previous file with this name (e.g. a retake) so we don't accumulate.
    _delete_gridfs_by_name(filename)
    bucket.upload_from_stream(
        filename,
        BytesIO(stored),
        metadata={
            "event_id": event_id,
            "card_id": card_id,
            "side": side,
            "content_type": "image/jpeg",
            "original_bytes": len(image_bytes),
            "stored_bytes": len(stored),
        },
    )
    return filename


def _delete_gridfs_by_name(filename: str) -> None:
    db = mongo.get_database()
    bucket = mongo.get_gridfs()
    if db is None or bucket is None:
        return
    for existing in db["fs.files"].find({"filename": filename}, {"_id": 1}):
        try:
            bucket.delete(existing["_id"])
        except Exception:  # noqa: BLE001
            pass


def _read_gridfs_image(filename: str) -> tuple[bytes, str] | None:
    bucket = mongo.get_gridfs()
    if bucket is None:
        return None
    try:
        stream = bucket.open_download_stream_by_name(filename)
        data = stream.read()
        content_type = (stream.metadata or {}).get("content_type", "image/jpeg")
        return data, content_type
    except Exception:  # noqa: BLE001 — includes NoFile
        return None


def save_llm_audit(event_id: str, card_id: str, fields: dict) -> None:
    sections = [
        "FRONT TEXT", str(fields.get("front_text") or ""), "",
        "BACK TEXT", str(fields.get("back_text") or ""), "",
        "ALL VISIBLE TEXT", str(fields.get("all_visible_text") or ""), "",
        "FIELD EVIDENCE", json_dumps_pretty(fields.get("field_evidence")), "",
        "UNCERTAIN FIELDS", json_dumps_pretty(fields.get("uncertain_fields")),
    ]
    try:
        save_audit(event_id, card_id, "llm_transcript", "\n".join(sections))
    except Exception:  # noqa: BLE001 — audits are debug-only, never break a scan
        pass


def save_ocr_audit(event_id: str, card_id: str, ocr_results: list) -> None:
    payload = [
        result.model_dump() if hasattr(result, "model_dump") else result.dict()
        for result in ocr_results
    ]
    try:
        save_audit(event_id, card_id, "google_vision_ocr", json_dumps_pretty(payload))
    except Exception:  # noqa: BLE001
        pass


def json_dumps_pretty(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, indent=2) if value is not None else ""


def clear_event_images(event_id: str) -> int:
    """Delete all GridFS images belonging to an event. Returns the count removed."""
    db = mongo.get_database()
    bucket = mongo.get_gridfs()
    if db is None or bucket is None:
        return 0
    count = 0
    for existing in db["fs.files"].find({"metadata.event_id": event_id}, {"_id": 1}):
        try:
            bucket.delete(existing["_id"])
            count += 1
        except Exception:  # noqa: BLE001
            pass
    return count


def enforce_usage_limits(required: dict[str, int]) -> None:
    allowed, reason = mongo_usage.check_limits(required)
    if allowed:
        return
    status_code = 503 if mongo_usage.is_unavailable_reason(reason) else 429
    raise HTTPException(status_code=status_code, detail=reason)


def record_from_llm_fields(
    *,
    event_id: str,
    event_name: str,
    card_id: str,
    front_image_filename: str,
    back_image_filename: str | None,
    fields: dict,
) -> BusinessCardRecord:
    now = utc_now()
    low_confidence_fields = [
        field
        for field in ("name", "business", "phone_primary", "email")
        if not fields.get(field)
    ]
    confidence = "High" if not low_confidence_fields[:2] and (fields.get("email") or fields.get("phone_primary")) else "Medium"
    if len(low_confidence_fields) >= 3:
        confidence = "Low"
    return BusinessCardRecord(
        record_id=new_id("record"),
        card_id=card_id,
        event_id=event_id,
        date=now[:10],
        time=now[11:19],
        event_name=event_name,
        name=fields.get("name"),
        designation=fields.get("designation"),
        company=fields.get("company") or fields.get("business"),
        business=fields.get("business") or fields.get("company"),
        phone_primary=fields.get("phone_primary") or (f"+{fields.get('contact1')}" if fields.get("contact1") else None),
        phone_number=fields.get("phone_number") or fields.get("contact2"),
        mobile_number=fields.get("mobile_number") or fields.get("contact1"),
        phone_extra=fields.get("phone_extra"),
        fax_number=fields.get("fax_number") or fields.get("contact3"),
        country_code=fields.get("country_code"),
        email=fields.get("email") or fields.get("email1"),
        website=fields.get("website"),
        address=fields.get("address"),
        city=fields.get("city"),
        state=fields.get("state"),
        country=fields.get("country"),
        zip_code=fields.get("zip_code"),
        category=fields.get("category"),
        social_media=fields.get("social_media"),
        notes=fields.get("notes"),
        email1=fields.get("email1") or fields.get("email"),
        email2=fields.get("email2"),
        contact1=fields.get("contact1"),
        contact2=fields.get("contact2"),
        contact3=fields.get("contact3"),
        confidence_score=confidence,
        low_confidence_fields=low_confidence_fields,
        front_image_filename=front_image_filename,
        back_image_filename=back_image_filename,
    )


def process_card(event_id: str, front_upload: UploadFile, front_bytes: bytes, back_upload: UploadFile | None, back_bytes: bytes | None, scanned_by: str | None = None) -> ProcessingResult:
    event = get_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if not is_gemini_configured():
        raise HTTPException(status_code=503, detail="Gemini is not configured")
    if not is_google_vision_configured():
        raise HTTPException(status_code=503, detail="Google Vision OCR is not configured")

    card_id = create_card(event_id, processing_mode="google_vision_ocr_gemini_text")
    front_filename = save_side_file(event_id, card_id, "front", front_upload, front_bytes)
    back_filename = save_side_file(event_id, card_id, "back", back_upload, back_bytes) if back_upload and back_bytes else None

    try:
        sides = [("front", front_upload, front_bytes, front_filename)]
        if back_upload and back_bytes and back_filename:
            sides.append(("back", back_upload, back_bytes, back_filename))

        # Preprocess once per side and OCR the CLEANED bytes (EXIF-rotated,
        # contrast-enhanced, downscaled) rather than the raw upload — Vision
        # sees the same image a human would find easiest to read.
        ocr_bytes_by_side: dict[str, bytes] = {}
        for side, upload, raw_bytes, filename in sides:
            processed_bytes, width, height, quality, warnings = preprocess_image(raw_bytes)
            ocr_bytes_by_side[side] = processed_bytes
            insert_card_side(
                event_id,
                card_id=card_id,
                side=side,
                filename=filename,
                content_type=upload.content_type,
                width=width,
                height=height,
                quality_score=quality,
                quality_warnings=warnings,
            )

        # Stitch front+back into a single Vision call so a two-sided card costs
        # one OCR unit instead of two (front-only stays a single call too).
        ocr_results = google_vision_extract_combined(
            ocr_bytes_by_side["front"],
            ocr_bytes_by_side.get("back"),
            event_id=event_id,
        )
        for result in ocr_results:
            insert_ocr_result(event_id, card_id, result)
        save_ocr_audit(event_id, card_id, ocr_results)
        front_ocr = next((result.raw_text for result in ocr_results if result.side == "front"), "")
        back_ocr = next((result.raw_text for result in ocr_results if result.side == "back"), None)
        if not front_ocr.strip() and not (back_ocr or "").strip():
            errors = "; ".join(result.error_message or "No text detected" for result in ocr_results)
            raise RuntimeError(f"Google Vision OCR did not return text: {errors}")

        candidates = extract_candidates(ocr_results)
        insert_field_candidates(event_id, card_id, candidates)
        candidate_hints = [
            candidate.model_dump() if hasattr(candidate, "model_dump") else candidate.dict()
            for candidate in candidates
        ]
        deterministic_fields = structure_card_text_deterministic(
            front_text=front_ocr,
            back_text=back_ocr,
            candidate_hints=candidate_hints,
        )
        if GEMINI_USE_IMAGE:
            # preprocess_image always re-encodes to JPEG (backend/imaging/preprocess.py),
            # so the mime type for these bytes is always image/jpeg regardless of
            # what the client originally uploaded.
            fields = structure_card_image(
                event_id=event_id,
                front_image=ocr_bytes_by_side["front"],
                front_mime_type="image/jpeg",
                back_image=ocr_bytes_by_side.get("back"),
                back_mime_type="image/jpeg" if ocr_bytes_by_side.get("back") else None,
                front_text=front_ocr,
                back_text=back_ocr,
                candidate_hints=candidate_hints,
                ocr_results=ocr_results,
            )
        else:
            fields = structure_card_text(
                event_id=event_id,
                front_text=front_ocr,
                back_text=back_ocr,
                candidate_hints=candidate_hints,
                ocr_results=ocr_results,
            )
        used_fallback = not fields
        if used_fallback:
            fields = deterministic_fields
        save_llm_audit(event_id, card_id, fields)
        draft = record_from_llm_fields(
            event_id=event_id,
            event_name=event.name,
            card_id=card_id,
            front_image_filename=front_filename,
            back_image_filename=back_filename,
            fields=fields,
        )
        draft.duplicate_flag = find_duplicate_flag(event_id, email=draft.email, phone=draft.phone_primary, card_id=card_id)
        if used_fallback:
            # Gemini returned nothing usable (quota/error, or a burst of rapid
            # scans briefly exceeded the local per-minute budget) and this
            # record came from the regex-only deterministic extractor. Always
            # tag it so it's traceable, but only downgrade confidence one
            # notch rather than blanket-forcing "Low" — a deterministic
            # extraction that actually captured name/company/phone/email is
            # still useful and shouldn't flood every card in a busy scanning
            # session into "needs review".
            if "llm_unavailable" not in draft.low_confidence_fields:
                draft.low_confidence_fields = [*draft.low_confidence_fields, "llm_unavailable"]
            if draft.confidence_score == "High":
                draft.confidence_score = "Medium"
        draft.scanned_by = scanned_by
        status = "needs_review" if draft.confidence_score == "Low" else "processed"
        upsert_card_record(draft)
        log_scan(event_id, event.name, card_id, scanned_by, status)
        return ProcessingResult(card=draft, status=status)
    except Exception as exc:
        fallback = record_from_llm_fields(
            event_id=event_id,
            event_name=event.name,
            card_id=card_id,
            front_image_filename=front_filename,
            back_image_filename=back_filename,
            fields={},
        )
        fallback.low_confidence_fields = ["all"]
        fallback.confidence_score = "Low"
        fallback.scanned_by = scanned_by
        upsert_card_record(fallback)
        log_scan(event_id, event.name, card_id, scanned_by, "error")
        return ProcessingResult(card=fallback, status="error", error_message=str(exc))


@app.post("/events/{event_id}/cards", response_model=ProcessingResult)
async def upload_card(
    event_id: str,
    front: UploadFile = File(...),
    back: UploadFile | None = File(default=None),
    user: dict = Depends(require_user),
) -> ProcessingResult:
    front_bytes = await read_upload(front)
    back_bytes = await read_upload(back)
    if front_bytes is None:
        raise HTTPException(status_code=400, detail="Front image is required")
    # Front+back are OCR'd as one stitched image, so a card is always 1 Vision unit.
    enforce_usage_limits({"google_vision": 1, "gemini": 1})
    return process_card(event_id, front, front_bytes, back, back_bytes, scanned_by=user["email"])


@app.post("/events/{event_id}/vision-scan")
async def vision_scan(
    event_id: str,
    front: UploadFile = File(...),
    back: UploadFile | None = File(default=None),
    user: dict = Depends(require_user),
) -> dict:
    if not get_event(event_id):
        raise HTTPException(status_code=404, detail="Event not found")
    front_bytes = await read_upload(front)
    back_bytes = await read_upload(back)
    if front_bytes is None:
        raise HTTPException(status_code=400, detail="Front image is required")
    enforce_usage_limits({"gemini": 1})
    fields = structure_card_image(
        event_id=event_id,
        front_image=front_bytes,
        front_mime_type=front.content_type or "image/jpeg",
        back_image=back_bytes,
        back_mime_type=back.content_type if back else None,
    )
    if not fields:
        raise HTTPException(status_code=502, detail="Gemini Vision did not return structured fields")
    return {"event_id": event_id, "fields": fields}


@app.post("/events/{event_id}/ocr-scan")
async def ocr_scan(
    event_id: str,
    front: UploadFile = File(...),
    back: UploadFile | None = File(default=None),
    user: dict = Depends(require_user),
) -> dict:
    if not get_event(event_id):
        raise HTTPException(status_code=404, detail="Event not found")
    if not is_google_vision_configured():
        raise HTTPException(status_code=503, detail="Google Vision OCR is not configured")
    front_bytes = await read_upload(front)
    back_bytes = await read_upload(back)
    if front_bytes is None:
        raise HTTPException(status_code=400, detail="Front image is required")
    # Front+back are OCR'd as one stitched image, so a card is always 1 Vision unit.
    enforce_usage_limits({"google_vision": 1})
    results = google_vision_extract_combined(front_bytes, back_bytes, event_id=event_id)
    return {
        "event_id": event_id,
        "ocr_engine": "google_vision",
        "results": [
            result.model_dump() if hasattr(result, "model_dump") else result.dict()
            for result in results
        ],
    }


@app.get("/events/{event_id}/cards")
async def records(event_id: str, user: dict = Depends(require_user)) -> dict:
    return {"event_id": event_id, "records": list_records(event_id)}


@app.delete("/events/{event_id}/cards")
async def reset_cards(event_id: str, admin: dict = Depends(require_admin)) -> dict:
    if not get_event(event_id):
        raise HTTPException(status_code=404, detail="Event not found")
    database_counts = reset_event_data(event_id)
    images_removed = clear_event_images(event_id)
    return {
        "event_id": event_id,
        "status": "reset",
        "deleted": {
            **database_counts,
            "images": images_removed,
        },
    }


def usage_response() -> dict:
    snapshot = usage_snapshot()
    gemini = provider_usage_snapshot("gemini")
    vision = provider_usage_snapshot("google_vision")
    return {
        "scope": "global",
        "provider": "multi",
        "daily_requests": snapshot.daily_requests,
        "minute_requests": snapshot.minute_requests,
        "daily_tokens_estimated": snapshot.daily_tokens,
        "daily_request_limit": snapshot.daily_request_limit,
        "minute_request_limit": snapshot.minute_request_limit,
        "daily_token_limit": snapshot.daily_token_limit,
        "allowed": snapshot.allowed,
        "gemini": {
            "daily_requests": gemini.daily_requests,
            "minute_requests": gemini.minute_requests,
            "daily_tokens_estimated": gemini.daily_tokens,
            "daily_request_limit": gemini.daily_request_limit,
            "minute_request_limit": gemini.minute_request_limit,
            "daily_token_limit": gemini.daily_token_limit,
            "key_count": len(gemini_key_labels()),
            "project_count": GEMINI_PROJECT_COUNT,
            "daily_request_limit_per_project": GEMINI_DAILY_REQUEST_LIMIT_PER_PROJECT,
            "minute_request_limit_per_project": GEMINI_MINUTE_REQUEST_LIMIT_PER_PROJECT,
            "daily_token_limit_per_project": GEMINI_DAILY_TOKEN_LIMIT_PER_PROJECT,
            "by_key": gemini.by_key or {},
        },
        "google_vision": {
            "daily_requests": vision.daily_requests,
            "minute_requests": vision.minute_requests,
            "monthly_units": vision.monthly_units,
            "minute_request_limit": vision.minute_request_limit,
            "free_units_monthly": vision.free_units_monthly,
            "estimated_cost_usd": vision.estimated_cost_usd,
        },
        "mongo": mongo_usage.usage_report(),
        "note": "MongoDB usage counters are the app's persistent 24/7 budget tracker when configured. Provider consoles remain the billing source of truth.",
    }


@app.get("/llm-usage")
async def global_llm_usage(user: dict = Depends(require_user)) -> dict:
    return usage_response()


@app.get("/events/{event_id}/llm-usage")
async def llm_usage(event_id: str, user: dict = Depends(require_user)) -> dict:
    if not get_event(event_id):
        raise HTTPException(status_code=404, detail="Event not found")
    return {**usage_response(), "event_id": event_id}


@app.patch("/events/{event_id}/cards/{card_id}")
async def patch_record(event_id: str, card_id: str, payload: UpdateRecordIn, user: dict = Depends(require_user)) -> dict:
    values = payload.model_dump(exclude_unset=True) if hasattr(payload, "model_dump") else payload.dict(exclude_unset=True)
    try:
        return {"record": update_record(event_id, card_id, values)}
    except KeyError:
        raise HTTPException(status_code=404, detail="Card not found") from None


@app.get("/events/{event_id}/download")
async def download_excel(
    event_id: str,
    skip_duplicates: bool = Query(False),
    columns: str | None = Query(None, description="Comma-separated EXCEL_COLUMNS subset; omit for all columns"),
    category: str | None = Query(None, description="Comma-separated category values to include; omit for all"),
    search: str | None = Query(None, description="Case-insensitive substring match against name/business/designation"),
    user: dict = Depends(require_user),
) -> StreamingResponse:
    records_for_event = list_records(event_id)
    if skip_duplicates:
        records_for_event = [r for r in records_for_event if r.duplicate_flag == "No"]
    if category:
        wanted = {c.strip().lower() for c in category.split(",") if c.strip()}
        records_for_event = [r for r in records_for_event if (r.category or "").strip().lower() in wanted]
    if search:
        needle = search.strip().lower()
        records_for_event = [
            r for r in records_for_event
            if needle in " ".join(filter(None, [r.name, r.business, r.company, r.designation])).lower()
        ]
    selected_columns = [c.strip() for c in columns.split(",") if c.strip()] if columns else None
    # Images come from GridFS (no local files); the workbook is built in memory
    # and streamed straight back — nothing is written to disk.
    workbook_bytes = build_workbook_bytes(
        records_for_event,
        image_provider=lambda filename: (_read_gridfs_image(filename) or (None, None))[0],
        columns=selected_columns,
    )
    return StreamingResponse(
        BytesIO(workbook_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{event_id}_contacts.xlsx"'},
    )


@app.get("/events/{event_id}/images/{filename}")
async def get_image(event_id: str, filename: str, user: dict = Depends(require_user)) -> Response:
    image = _read_gridfs_image(filename)
    if image is None:
        raise HTTPException(status_code=404, detail="Image not found")
    data, content_type = image
    return Response(content=data, media_type=content_type)
