from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

DATABASE_FILENAME = "app.db"
SCHEMA_VERSION = 1
EVENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    date TEXT NOT NULL,
    location TEXT,
    booth TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
    card_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    status TEXT NOT NULL,
    processing_mode TEXT NOT NULL,
    confidence_score TEXT,
    duplicate_flag TEXT NOT NULL DEFAULT 'No',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS card_sides (
    side_id TEXT PRIMARY KEY,
    card_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('front', 'back')),
    filename TEXT NOT NULL,
    content_type TEXT,
    width INTEGER,
    height INTEGER,
    quality_score TEXT,
    quality_warnings TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (card_id) REFERENCES cards(card_id) ON DELETE CASCADE,
    UNIQUE (card_id, side)
);

CREATE TABLE IF NOT EXISTS ocr_results (
    ocr_result_id TEXT PRIMARY KEY,
    card_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('front', 'back')),
    engine TEXT NOT NULL,
    engine_version TEXT,
    variant TEXT,
    runtime_ms INTEGER,
    status TEXT NOT NULL DEFAULT 'ok',
    error_message TEXT,
    raw_text TEXT,
    average_confidence REAL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (card_id) REFERENCES cards(card_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ocr_blocks (
    block_id TEXT PRIMARY KEY,
    ocr_result_id TEXT NOT NULL,
    line_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    confidence REAL,
    engine TEXT,
    variant TEXT,
    normalized_text TEXT,
    bbox_json TEXT,
    FOREIGN KEY (ocr_result_id) REFERENCES ocr_results(ocr_result_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS field_candidates (
    candidate_id TEXT PRIMARY KEY,
    card_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL,
    source TEXT NOT NULL,
    evidence TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (card_id) REFERENCES cards(card_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS card_records (
    record_id TEXT PRIMARY KEY,
    card_id TEXT NOT NULL UNIQUE,
    event_id TEXT NOT NULL,
    name TEXT,
    designation TEXT,
    company TEXT,
    business TEXT,
    phone_primary TEXT,
    phone_number TEXT,
    mobile_number TEXT,
    phone_extra TEXT,
    fax_number TEXT,
    country_code TEXT,
    email TEXT,
    website TEXT,
    address TEXT,
    city TEXT,
    state TEXT,
    country TEXT,
    zip_code TEXT,
    category TEXT,
    social_media TEXT,
    notes TEXT,
    email1 TEXT,
    email2 TEXT,
    contact1 TEXT,
    contact2 TEXT,
    contact3 TEXT,
    confidence_score TEXT,
    low_confidence_fields TEXT,
    duplicate_flag TEXT NOT NULL DEFAULT 'No',
    front_image_filename TEXT,
    back_image_filename TEXT,
    reviewed_by_user INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (card_id) REFERENCES cards(card_id) ON DELETE CASCADE,
    FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS duplicate_links (
    duplicate_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    card_id TEXT NOT NULL,
    matched_card_id TEXT NOT NULL,
    match_type TEXT NOT NULL,
    match_score REAL,
    reason TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE CASCADE,
    FOREIGN KEY (card_id) REFERENCES cards(card_id) ON DELETE CASCADE,
    FOREIGN KEY (matched_card_id) REFERENCES cards(card_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS exports (
    export_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    row_count INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS llm_usage (
    usage_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    request_count INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    error_message TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cards_event_id ON cards(event_id);
CREATE INDEX IF NOT EXISTS idx_card_records_event_id ON card_records(event_id);
CREATE INDEX IF NOT EXISTS idx_card_records_email ON card_records(email);
CREATE INDEX IF NOT EXISTS idx_card_records_phone_primary ON card_records(phone_primary);
CREATE INDEX IF NOT EXISTS idx_card_records_company ON card_records(company);
CREATE INDEX IF NOT EXISTS idx_duplicate_links_event_id ON duplicate_links(event_id);
CREATE INDEX IF NOT EXISTS idx_ocr_results_card_id ON ocr_results(card_id);
CREATE INDEX IF NOT EXISTS idx_field_candidates_card_id ON field_candidates(card_id);
CREATE INDEX IF NOT EXISTS idx_llm_usage_event_created ON llm_usage(event_id, created_at);
"""


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def validate_event_id(event_id: str) -> str:
    if not EVENT_ID_PATTERN.fullmatch(event_id):
        raise ValueError("event_id may contain only letters, numbers, underscores, and hyphens")
    return event_id


def event_dir(events_root: str | Path, event_id: str) -> Path:
    validate_event_id(event_id)
    return Path(events_root) / event_id


def event_db_path(events_root: str | Path, event_id: str) -> Path:
    return event_dir(events_root, event_id) / DATABASE_FILENAME


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connection(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    _ensure_column(conn, "card_records", "business", "TEXT")
    _ensure_column(conn, "card_records", "country_code", "TEXT")
    _ensure_column(conn, "card_records", "phone_number", "TEXT")
    _ensure_column(conn, "card_records", "mobile_number", "TEXT")
    _ensure_column(conn, "card_records", "fax_number", "TEXT")
    _ensure_column(conn, "card_records", "social_media", "TEXT")
    _ensure_column(conn, "card_records", "notes", "TEXT")
    _ensure_column(conn, "card_records", "email1", "TEXT")
    _ensure_column(conn, "card_records", "email2", "TEXT")
    _ensure_column(conn, "card_records", "contact1", "TEXT")
    _ensure_column(conn, "card_records", "contact2", "TEXT")
    _ensure_column(conn, "card_records", "contact3", "TEXT")
    _ensure_column(conn, "ocr_results", "variant", "TEXT")
    _ensure_column(conn, "ocr_results", "runtime_ms", "INTEGER")
    _ensure_column(conn, "ocr_results", "status", "TEXT NOT NULL DEFAULT 'ok'")
    _ensure_column(conn, "ocr_results", "error_message", "TEXT")
    _ensure_column(conn, "ocr_blocks", "engine", "TEXT")
    _ensure_column(conn, "ocr_blocks", "variant", "TEXT")
    _ensure_column(conn, "ocr_blocks", "normalized_text", "TEXT")
    _ensure_column(conn, "llm_usage", "key_label", "TEXT")
    _ensure_column(conn, "llm_usage", "unit_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "llm_usage", "cost_estimate_usd", "REAL NOT NULL DEFAULT 0")
    conn.execute(
        """
        INSERT OR IGNORE INTO schema_migrations (version, applied_at)
        VALUES (?, ?)
        """,
        (SCHEMA_VERSION, utc_now()),
    )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    columns = {row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
    if column not in columns:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {column_type}')


def upsert_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    name: str,
    date: str,
    location: str | None = None,
    booth: str | None = None,
    notes: str | None = None,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO events (event_id, name, date, location, booth, notes, created_at, updated_at)
        VALUES (:event_id, :name, :date, :location, :booth, :notes, :now, :now)
        ON CONFLICT(event_id) DO UPDATE SET
            name = excluded.name,
            date = excluded.date,
            location = excluded.location,
            booth = excluded.booth,
            notes = excluded.notes,
            updated_at = excluded.updated_at
        """,
        {
            "event_id": validate_event_id(event_id),
            "name": name,
            "date": date,
            "location": location,
            "booth": booth,
            "notes": notes,
            "now": now,
        },
    )


def write_event_metadata(
    events_root: str | Path,
    *,
    event_id: str,
    name: str,
    date: str,
    location: str | None = None,
    booth: str | None = None,
    notes: str | None = None,
) -> None:
    metadata_path = event_dir(events_root, event_id) / "metadata.json"
    metadata = {
        "event_id": event_id,
        "name": name,
        "date": date,
        "location": location,
        "booth": booth,
        "notes": notes,
        "updated_at": utc_now(),
        "database": DATABASE_FILENAME,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def initialize_event_database(
    events_root: str | Path,
    *,
    event_id: str,
    name: str,
    date: str,
    location: str | None = None,
    booth: str | None = None,
    notes: str | None = None,
) -> Path:
    root = event_dir(events_root, event_id)
    for child in ("images", "ocr", "exports"):
        (root / child).mkdir(parents=True, exist_ok=True)

    db_path = root / DATABASE_FILENAME
    with connection(db_path) as conn:
        initialize_schema(conn)
        upsert_event(
            conn,
            event_id=event_id,
            name=name,
            date=date,
            location=location,
            booth=booth,
            notes=notes,
        )

    write_event_metadata(
        events_root,
        event_id=event_id,
        name=name,
        date=date,
        location=location,
        booth=booth,
        notes=notes,
    )
    return db_path


def list_tables(db_path: str | Path) -> list[str]:
    with connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
    return [row["name"] for row in rows]


def database_summary(db_path: str | Path) -> dict[str, Any]:
    tables = list_tables(db_path)
    with connection(db_path) as conn:
        counts = {
            table: conn.execute(f'SELECT COUNT(*) AS count FROM "{table}"').fetchone()["count"]
            for table in tables
        }
    return {"path": str(db_path), "schema_version": SCHEMA_VERSION, "tables": tables, "counts": counts}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"
