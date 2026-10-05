"""Round-trip tests for the MongoDB-backed data layer, using an in-memory Mongo."""

import mongomock
import pytest

from backend.models import BusinessCardRecord, FieldCandidate, OCRSideResult, OCRTextBlock


@pytest.fixture()
def repo(monkeypatch):
    from backend.storage import mongo as mongo_mod
    from backend.storage import repositories as repositories_mod

    client = mongomock.MongoClient()
    monkeypatch.setattr(mongo_mod, "get_database", lambda: client["cardscan"])
    repositories_mod.ensure_indexes()
    return repositories_mod


def _make_record(card_id: str) -> BusinessCardRecord:
    return BusinessCardRecord(
        record_id="rec_1", card_id=card_id, event_id="evt", date="2026-07-15", time="10:00:00",
        event_name="Expo", name="Jane Doe", company="ACME", email="jane@acme.com",
        phone_primary="+15551234", confidence_score="High", low_confidence_fields=[],
        front_image_filename=f"{card_id}_front.jpg",
    )


def test_event_and_card_roundtrip(repo):
    repo.ensure_event("evt", "Expo", "2026-07-15", "Hall A")
    assert repo.get_event("evt").name == "Expo"
    assert [e.event_id for e in repo.list_events()] == ["evt"]

    card_id = repo.create_card("evt", processing_mode="google_vision_ocr_gemini_text")
    repo.insert_card_side("evt", card_id=card_id, side="front", filename="f.jpg", content_type="image/jpeg")
    # Re-inserting the same side replaces rather than duplicates.
    repo.insert_card_side("evt", card_id=card_id, side="front", filename="f2.jpg", content_type="image/jpeg")
    from backend.storage import mongo as mongo_mod
    card = mongo_mod.get_database()["cards"].find_one({"_id": card_id})
    assert len(card["sides"]) == 1 and card["sides"][0]["filename"] == "f2.jpg"


def test_ocr_blocks_embedded(repo):
    repo.ensure_event("evt", "Expo", "2026-07-15")
    card_id = repo.create_card("evt")
    result = OCRSideResult(
        side="front", raw_text="ACME", average_confidence=0.9, engine="google_vision",
        blocks=[OCRTextBlock(text="ACME", confidence=0.9, side="front", line_index=0, size_tag="large")],
    )
    repo.insert_ocr_result("evt", card_id, result)
    repo.insert_field_candidates("evt", card_id, [FieldCandidate(field="name", value="Jane", confidence=0.8, source="regex")])
    from backend.storage import mongo as mongo_mod
    doc = mongo_mod.get_database()["ocr_results"].find_one({"card_id": card_id})
    assert doc["blocks"][0]["text"] == "ACME"
    assert mongo_mod.get_database()["field_candidates"].count_documents({"card_id": card_id}) == 1


def test_record_upsert_is_idempotent_and_dedup(repo):
    repo.ensure_event("evt", "Expo", "2026-07-15")
    card_id = repo.create_card("evt")
    record = _make_record(card_id)
    repo.upsert_card_record(record)
    record.company = "ACME Corp"
    repo.upsert_card_record(record)  # update path, must not duplicate

    records = repo.list_records("evt")
    assert len(records) == 1
    assert records[0].company == "ACME Corp"
    assert records[0].event_name == "Expo"
    assert repo.find_duplicate_flag("evt", email="jane@acme.com", phone=None, card_id="other") == "Exact"


def test_update_and_reset(repo):
    repo.ensure_event("evt", "Expo", "2026-07-15")
    card_id = repo.create_card("evt")
    repo.upsert_card_record(_make_record(card_id))

    updated = repo.update_record("evt", card_id, {"designation": "CEO", "not_allowed": "x"})
    assert updated.designation == "CEO"
    assert updated.reviewed_by_user is True

    counts = repo.reset_event_data("evt")
    assert counts["records"] == 1 and counts["cards"] == 1
    assert repo.list_records("evt") == []


def test_update_missing_card_raises(repo):
    repo.ensure_event("evt", "Expo", "2026-07-15")
    with pytest.raises(KeyError):
        repo.update_record("evt", "nope", {"name": "X"})
