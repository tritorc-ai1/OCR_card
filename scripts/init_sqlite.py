from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.storage.db import database_summary, initialize_event_database


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize an event-local SQLite database.")
    parser.add_argument("--events-root", default="events", help="Folder where event data is stored.")
    parser.add_argument("--event-id", default="test_uploads", help="Event identifier.")
    parser.add_argument("--name", default="Test Uploads", help="Event display name.")
    parser.add_argument("--date", default=date.today().isoformat(), help="Event date in YYYY-MM-DD format.")
    parser.add_argument("--location", default="Local", help="Event location.")
    parser.add_argument("--booth", default=None, help="Optional booth number/name.")
    parser.add_argument("--notes", default="SQLite setup event", help="Optional event notes.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = initialize_event_database(
        args.events_root,
        event_id=args.event_id,
        name=args.name,
        date=args.date,
        location=args.location,
        booth=args.booth,
        notes=args.notes,
    )
    print(json.dumps(database_summary(db_path), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

