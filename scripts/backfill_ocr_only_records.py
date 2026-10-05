from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.llm.gemini_client import clean_structured_fields, structure_card_text_deterministic
from backend.main import record_from_llm_fields
from backend.models import OCRSideResult, OCRTextBlock
from backend.extraction.candidate_extractors import extract_candidates
from backend.storage.db import event_db_path
from backend.storage.excel_writer import export_records
from backend.storage.repositories import get_event, list_records, upsert_card_record
from backend.config import EVENTS_ROOT


def _ocr_results(conn: sqlite3.Connection, card_id: str) -> list[OCRSideResult]:
    rows = conn.execute(
        """
        SELECT ocr_result_id, side, raw_text, average_confidence, engine, engine_version,
               variant, runtime_ms, status, error_message
        FROM ocr_results
        WHERE card_id = ?
        ORDER BY side
        """,
        (card_id,),
    ).fetchall()
    results = []
    for row in rows:
        block_rows = conn.execute(
            """
            SELECT line_index, text, confidence, engine, variant, normalized_text, bbox_json
            FROM ocr_blocks
            WHERE ocr_result_id = ?
            ORDER BY line_index
            """,
            (row["ocr_result_id"],),
        ).fetchall()
        blocks = [
            OCRTextBlock(
                text=block["text"],
                confidence=block["confidence"] or 0,
                side=row["side"],
                line_index=block["line_index"],
                engine=block["engine"],
                variant=block["variant"],
                normalized_text=block["normalized_text"],
            )
            for block in block_rows
        ]
        results.append(
            OCRSideResult(
                side=row["side"],
                raw_text=row["raw_text"] or "",
                average_confidence=row["average_confidence"] or 0,
                blocks=blocks,
                engine=row["engine"],
                engine_version=row["engine_version"],
                variant=row["variant"] or "document_text_detection",
                runtime_ms=row["runtime_ms"],
                status=row["status"] or "ok",
                error_message=row["error_message"],
            )
        )
    return results


def _candidates(conn: sqlite3.Connection, card_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT field_name AS field, value, confidence, source, evidence
        FROM field_candidates
        WHERE card_id = ?
        ORDER BY confidence DESC
        """,
        (card_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def backfill_event(event_id: str) -> int:
    event = get_event(event_id)
    if not event:
        raise SystemExit(f"Event not found: {event_id}")
    db_path = event_db_path(EVENTS_ROOT, event_id)
    repaired = 0
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT card_id, front_image_filename, back_image_filename
            FROM card_records
            WHERE event_id = ?
              AND (low_confidence_fields = 'all'
               OR low_confidence_fields LIKE '%llm_sorting_skipped_or_failed%'
               OR (
                name IS NULL AND business IS NULL AND email IS NULL AND contact1 IS NULL
              ))
            """,
            (event_id,),
        ).fetchall()
        for row in rows:
            ocr_results = _ocr_results(conn, row["card_id"])
            if not ocr_results:
                continue
            front_text = next((result.raw_text for result in ocr_results if result.side == "front"), "")
            back_text = next((result.raw_text for result in ocr_results if result.side == "back"), None)
            fresh_candidates = [
                candidate.model_dump() if hasattr(candidate, "model_dump") else candidate.dict()
                for candidate in extract_candidates(ocr_results)
            ]
            fields = structure_card_text_deterministic(
                front_text=front_text,
                back_text=back_text,
                candidate_hints=fresh_candidates or _candidates(conn, row["card_id"]),
            )
            record = record_from_llm_fields(
                event_id=event_id,
                event_name=event.name,
                card_id=row["card_id"],
                front_image_filename=row["front_image_filename"],
                back_image_filename=row["back_image_filename"],
                fields=fields,
            )
            record.confidence_score = "Medium" if record.email or record.contact1 else "Low"
            if "llm_sorting_skipped_or_failed" not in record.low_confidence_fields:
                record.low_confidence_fields.append("llm_sorting_skipped_or_failed")
            upsert_card_record(record)
            repaired += 1
        all_rows = conn.execute(
            """
            SELECT cr.*, e.name AS event_name
            FROM card_records cr
            JOIN events e ON e.event_id = cr.event_id
            WHERE cr.event_id = ?
            """,
            (event_id,),
        ).fetchall()
        for row in all_rows:
            ocr_results = _ocr_results(conn, row["card_id"])
            if not ocr_results:
                continue
            front_text = next((result.raw_text for result in ocr_results if result.side == "front"), "")
            back_text = next((result.raw_text for result in ocr_results if result.side == "back"), None)
            fields = {
                "front_text": front_text,
                "back_text": back_text,
                "all_visible_text": "\n".join(part for part in [front_text, back_text or ""] if part.strip()),
                "name": row["name"],
                "designation": row["designation"],
                "business": row["business"],
                "company": row["company"],
                "address": row["address"],
                "city": row["city"],
                "state": row["state"],
                "country": row["country"],
                "zip_code": row["zip_code"],
                "website": row["website"],
                "category": row["category"],
                "social_media": row["social_media"],
                "notes": row["notes"],
                "email1": row["email1"],
                "email2": row["email2"],
                "email": row["email"],
                "contact1": row["contact1"],
                "contact2": row["contact2"],
                "contact3": row["contact3"],
                "phone_primary": row["phone_primary"],
                "phone_number": row["phone_number"],
                "mobile_number": row["mobile_number"],
                "fax_number": row["fax_number"],
                "country_code": row["country_code"],
            }
            cleaned = clean_structured_fields(fields)
            if cleaned.get("website") != row["website"] or cleaned.get("country") != row["country"]:
                record = record_from_llm_fields(
                    event_id=event_id,
                    event_name=event.name,
                    card_id=row["card_id"],
                    front_image_filename=row["front_image_filename"],
                    back_image_filename=row["back_image_filename"],
                    fields=cleaned,
                )
                record.confidence_score = row["confidence_score"] or record.confidence_score
                record.low_confidence_fields = [
                    part.strip()
                    for part in (row["low_confidence_fields"] or "").split(",")
                    if part.strip()
                ]
                upsert_card_record(record)
    records = list_records(event_id)
    export_records(records, Path(EVENTS_ROOT) / event_id / "exports" / "contacts.xlsx")
    return repaired


if __name__ == "__main__":
    target_event = sys.argv[1] if len(sys.argv) > 1 else "ui_event_smoke_2026_07_01"
    print(f"repaired={backfill_event(target_event)} event={target_event}")
