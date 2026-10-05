from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

if load_dotenv:
    load_dotenv()

EVENTS_ROOT = Path(os.getenv("EVENTS_ROOT", "events"))
DEFAULT_EVENT_ID = os.getenv("DEFAULT_EVENT_ID", "test_uploads")
DEFAULT_EVENT_NAME = os.getenv("DEFAULT_EVENT_NAME", "Test Uploads")
DEFAULT_EVENT_DATE = os.getenv("DEFAULT_EVENT_DATE", "2026-07-01")
DEFAULT_COUNTRY = os.getenv("DEFAULT_COUNTRY", "IN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


def _gemini_api_keys_from_env() -> list[str]:
    numbered_keys = [
        value
        for _, value in sorted(
            (
                (int(name.rsplit("_", 1)[1]), value)
                for name, value in os.environ.items()
                if name.startswith("GEMINI_API_KEY_")
                and name.rsplit("_", 1)[1].isdigit()
                and value
            ),
            key=lambda item: item[0],
        )
    ]
    values = [os.getenv("GEMINI_API_KEYS", ""), GEMINI_API_KEY or "", *numbered_keys]
    keys: list[str] = []
    seen: set[str] = set()
    for value in values:
        for key in value.split(","):
            clean = key.strip()
            if clean and clean not in seen:
                keys.append(clean)
                seen.add(clean)
    return keys


GEMINI_API_KEYS = _gemini_api_keys_from_env()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
# When true, send the card image(s) to Gemini alongside the OCR transcript
# (multimodal) so brand/logo vs. person-name layout is judged from the actual
# picture instead of flattened OCR text. Still exactly one Gemini call per
# card either way, so it doesn't change free-tier request-quota usage.
GEMINI_USE_IMAGE = os.getenv("GEMINI_USE_IMAGE", "true").lower() in {"1", "true", "yes"}
GEMINI_PROJECT_COUNT = max(1, int(os.getenv("GEMINI_PROJECT_COUNT", str(len(GEMINI_API_KEYS) or 1))))
GEMINI_DAILY_REQUEST_LIMIT_PER_PROJECT = int(
    os.getenv("GEMINI_DAILY_REQUEST_LIMIT_PER_PROJECT", os.getenv("GEMINI_DAILY_REQUEST_LIMIT", "50"))
)
GEMINI_MINUTE_REQUEST_LIMIT_PER_PROJECT = int(
    os.getenv("GEMINI_MINUTE_REQUEST_LIMIT_PER_PROJECT", os.getenv("GEMINI_MINUTE_REQUEST_LIMIT", "10"))
)
GEMINI_DAILY_TOKEN_LIMIT_PER_PROJECT = int(
    os.getenv("GEMINI_DAILY_TOKEN_LIMIT_PER_PROJECT", os.getenv("GEMINI_DAILY_TOKEN_LIMIT", "100000"))
)
GEMINI_DAILY_REQUEST_LIMIT = GEMINI_DAILY_REQUEST_LIMIT_PER_PROJECT * GEMINI_PROJECT_COUNT
GEMINI_MINUTE_REQUEST_LIMIT = GEMINI_MINUTE_REQUEST_LIMIT_PER_PROJECT * GEMINI_PROJECT_COUNT
GEMINI_DAILY_TOKEN_LIMIT = GEMINI_DAILY_TOKEN_LIMIT_PER_PROJECT * GEMINI_PROJECT_COUNT
# Cloud-friendly credential source: the full service-account JSON as a string
# (raw JSON or base64-encoded). Preferred on hosts where you cannot place a file.
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
# Local-dev credential source: path to a service-account JSON file on disk.
# No default path — an unset value fails loudly instead of pointing at a dead path.
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
GOOGLE_VISION_MODEL = os.getenv("GOOGLE_VISION_MODEL", "builtin/stable")
GOOGLE_VISION_TIMEOUT_SECONDS = int(os.getenv("GOOGLE_VISION_TIMEOUT_SECONDS", "60"))
GOOGLE_VISION_MINUTE_REQUEST_LIMIT = int(os.getenv("GOOGLE_VISION_MINUTE_REQUEST_LIMIT", "1800"))
GOOGLE_VISION_FREE_UNITS_MONTHLY = int(os.getenv("GOOGLE_VISION_FREE_UNITS_MONTHLY", "1000"))
GOOGLE_VISION_PRICE_PER_1000 = float(os.getenv("GOOGLE_VISION_PRICE_PER_1000", "1.50"))
# Comma-separated BCP-47 language hints to improve Vision's recognition of
# non-English names/text commonly seen on cards (e.g. "en,id,ar,zh").
GOOGLE_VISION_LANGUAGE_HINTS = [
    hint.strip() for hint in os.getenv("GOOGLE_VISION_LANGUAGE_HINTS", "en").split(",") if hint.strip()
]

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".webp"}

# Card images are stored in MongoDB (GridFS), not on disk. To keep the database
# small we store a COMPRESSED copy of each upload, not the full-size original:
# downscale so the longest edge is at most IMAGE_STORE_MAX_EDGE px, re-encoded as
# JPEG at IMAGE_STORE_QUALITY. This only affects the stored/displayed image — OCR
# still runs on the higher-fidelity preprocessed bytes, so accuracy is unchanged.
IMAGE_STORE_MAX_EDGE = int(os.getenv("IMAGE_STORE_MAX_EDGE", "1600"))
IMAGE_STORE_QUALITY = int(os.getenv("IMAGE_STORE_QUALITY", "80"))

# MongoDB — persistent, restart-surviving usage counters with per-period buckets.
# Unset MONGODB_URI (or MONGO_USAGE_ENABLED=false) disables the feature entirely;
# processing then falls back to the local SQLite-only behaviour.
MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "cardscan")
MONGO_USAGE_ENABLED = os.getenv("MONGO_USAGE_ENABLED", "true").lower() not in {"0", "false", "no"}
# Deprecated compatibility flag. The app now fails open when MongoDB is
# unreachable so trade-show scanning can continue with local counters.
MONGO_USAGE_FAIL_CLOSED = os.getenv("MONGO_USAGE_FAIL_CLOSED", "true").lower() not in {"0", "false", "no"}
# Google Vision free tier is 1000 OCR units per calendar month.
MONGO_VISION_MONTHLY_LIMIT = int(os.getenv("MONGO_VISION_MONTHLY_LIMIT", "1000"))
# Gemini: hard global cap on requests per day across all API keys.
MONGO_GEMINI_DAILY_LIMIT = int(os.getenv("MONGO_GEMINI_DAILY_LIMIT", "120"))
# How long a usage bucket document lives before Mongo auto-deletes it (TTL).
# Comfortably longer than a month so the current bucket is never expired early.
MONGO_USAGE_TTL_DAYS = int(os.getenv("MONGO_USAGE_TTL_DAYS", "40"))

# --- Authentication / admin portal -----------------------------------------
# Render always sets the RENDER env var, so we use it to distinguish a real
# deployment (where secrets MUST be provided) from local dev (where we can
# generate a throwaway secret to stay frictionless).
_IS_RENDER = bool(os.getenv("RENDER"))

# Secret used to sign stateless session cookies (itsdangerous). Rotating it
# logs everyone out. On Render it must be set (and persisted) so sessions
# survive restarts/spin-down; locally we generate an ephemeral one so dev
# doesn't need any setup — with the caveat that logins reset on each restart.
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
if not SESSION_SECRET:
    if _IS_RENDER:
        raise RuntimeError(
            "SESSION_SECRET must be set in production (Render). "
            "Set it as an environment variable so sessions survive restarts."
        )
    SESSION_SECRET = secrets.token_urlsafe(32)
    logger.warning(
        "SESSION_SECRET is not set; generated an ephemeral one. "
        "All sessions will be invalidated on restart. Set SESSION_SECRET for stable local logins."
    )

SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "30"))
# Secure cookies require HTTPS; on by default on Render (always HTTPS), off by
# default locally so cookies work over http://localhost during development.
SESSION_COOKIE_SECURE = os.getenv(
    "SESSION_COOKIE_SECURE", "true" if _IS_RENDER else "false"
).lower() in {"1", "true", "yes"}
SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "cardscan_session")

# Only emails at this domain may be created/log in. Stored without a leading @.
ALLOWED_EMAIL_DOMAIN = os.getenv("ALLOWED_EMAIL_DOMAIN", "tritorc.com").lstrip("@").lower()

# First admin, seeded on startup if it doesn't already exist. Set both in the
# environment (Render dashboard / .env). ADMIN_FORCE_PASSWORD_RESET overwrites
# the stored hash on next boot (recovery path for a forgotten admin password).
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_FORCE_PASSWORD_RESET = os.getenv("ADMIN_FORCE_PASSWORD_RESET", "false").lower() in {"1", "true", "yes"}

# The seeded dev/demo admin (see main.py's startup seed_test_user call) — kept
# permanently active so the team never gets locked out of the deployment.
# Enforced here (not just hidden client-side) so no admin can deactivate it
# via a direct API call either.
PROTECTED_ADMIN_EMAIL = os.getenv("PROTECTED_ADMIN_EMAIL", "a.i@tritorc.com").strip().lower()

EXCEL_COLUMNS = [
    "date",
    "name",
    "designation",
    "business",
    "address",
    "city",
    "state",
    "country",
    "zip_code",
    "website",
    "category",
    "social_media",
    "notes",
    "email1",
    "email2",
    "contact1",
    "contact2",
    "contact3",
    "card",
]

EXCEL_HEADERS = {
    "date": "Date",
    "name": "Name",
    "designation": "Designation",
    "business": "Business",
    "address": "Address",
    "city": "City",
    "state": "State",
    "country": "Country",
    "zip_code": "Zip Code",
    "website": "Website",
    "category": "Category",
    "social_media": "Social Media",
    "notes": "Notes",
    "email1": "Email1",
    "email2": "Email2",
    "contact1": "Contact1",
    "contact2": "Contact2",
    "contact3": "Contact3",
    "card": "Card",
}

BUSINESS_CATEGORIES = [
    "Engineering",
    "Engineering Services",
    "Industrial Services",
    "Industrial Equipment",
    "Certification",
    "Supply Chain Management",
    "Marine Contractors",
    "Marine Services",
    "Oil & Gas",
    "Oil and Gas",
    "Oilfield Services",
    "Manufacturing",
    "Construction",
    "Logistics",
    "Trading",
    "Energy",
    "Energy Services",
    "Renewable Energy",
    "Technology",
    "Sales",
    "Services",
    "Business Services",
    "Industrial",
    "Other",
]
