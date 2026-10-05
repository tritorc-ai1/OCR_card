from backend.storage.db import database_summary, initialize_event_database


def test_initialize_event_database(tmp_path) -> None:
    db_path = initialize_event_database(
        tmp_path,
        event_id="expo_test",
        name="Expo Test",
        date="2026-07-01",
        location="Local",
    )
    summary = database_summary(db_path)
    assert "events" in summary["tables"]
    assert "card_records" in summary["tables"]
    assert summary["counts"]["events"] == 1

