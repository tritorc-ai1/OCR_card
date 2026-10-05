import mongomock
import pytest


@pytest.fixture()
def users(monkeypatch):
    from backend.storage import mongo as mongo_mod
    from backend.storage import users as users_mod

    client = mongomock.MongoClient()
    monkeypatch.setattr(mongo_mod, "get_database", lambda: client["cardscan"])
    client["cardscan"]["users"].create_index("_id")
    return users_mod


@pytest.fixture()
def repo(monkeypatch):
    from backend.storage import mongo as mongo_mod
    from backend.storage import repositories as repositories_mod

    client = mongomock.MongoClient()
    monkeypatch.setattr(mongo_mod, "get_database", lambda: client["cardscan"])
    repositories_mod.ensure_indexes()
    return repositories_mod


def test_create_get_and_list_users_never_leak_hash(users):
    users.create_user("A.I@tritorc.com", "hash1", role="admin", created_by="seed")
    users.create_user("bob@tritorc.com", "hash2")

    fetched = users.get_user("a.i@tritorc.com")
    assert fetched["role"] == "admin"
    assert fetched["email"] == "a.i@tritorc.com"

    listed = users.list_users()
    assert {u["email"] for u in listed} == {"a.i@tritorc.com", "bob@tritorc.com"}
    assert all("password_hash" not in u for u in listed)


def test_duplicate_user_rejected(users):
    users.create_user("bob@tritorc.com", "hash")
    with pytest.raises(ValueError):
        users.create_user("BOB@tritorc.com", "hash")  # same email, different case


def test_deactivate_and_reset_bump_token_version(users):
    users.create_user("bob@tritorc.com", "hash")
    assert users.get_user("bob@tritorc.com")["token_version"] == 0

    users.set_user_active("bob@tritorc.com", False)
    doc = users.get_user("bob@tritorc.com")
    assert doc["active"] is False
    assert doc["token_version"] == 1

    users.set_user_password("bob@tritorc.com", "newhash")
    doc = users.get_user("bob@tritorc.com")
    assert doc["password_hash"] == "newhash"
    assert doc["token_version"] == 2


def test_seed_admin_is_idempotent_and_force_resets(users, monkeypatch):
    monkeypatch.setattr(users, "ADMIN_EMAIL", "admin@tritorc.com")
    monkeypatch.setattr(users, "ADMIN_PASSWORD", "supersecret")
    monkeypatch.setattr(users, "ADMIN_FORCE_PASSWORD_RESET", False)

    users.seed_admin()
    first = users.get_user("admin@tritorc.com")
    assert first["role"] == "admin"
    original_hash = first["password_hash"]

    users.seed_admin()  # second call must not change the hash
    assert users.get_user("admin@tritorc.com")["password_hash"] == original_hash

    monkeypatch.setattr(users, "ADMIN_FORCE_PASSWORD_RESET", True)
    users.seed_admin()
    reset = users.get_user("admin@tritorc.com")
    assert reset["password_hash"] != original_hash
    assert reset["token_version"] == 1


def test_seed_admin_skips_when_unconfigured(users, monkeypatch):
    monkeypatch.setattr(users, "ADMIN_EMAIL", "")
    monkeypatch.setattr(users, "ADMIN_PASSWORD", "")
    users.seed_admin()
    assert users.list_users() == []


def test_log_scan_and_stats(repo):
    repo.log_scan("evt1", "Expo One", "card1", "a@tritorc.com", "processed")
    repo.log_scan("evt1", "Expo One", "card2", "a@tritorc.com", "error")
    repo.log_scan("evt2", "Expo Two", "card3", "b@tritorc.com", "processed")

    totals = {row["email"]: row for row in repo.scan_totals_by_user()}
    assert totals["a@tritorc.com"]["total"] == 2
    assert totals["a@tritorc.com"]["errors"] == 1
    assert totals["b@tritorc.com"]["total"] == 1
    assert totals["a@tritorc.com"]["last_scan_at"]

    by_event = repo.scan_counts_by_user_event()
    a_evt1 = next(r for r in by_event if r["email"] == "a@tritorc.com" and r["event_id"] == "evt1")
    assert a_evt1["count"] == 2
    assert a_evt1["event_name"] == "Expo One"

    daily = repo.scan_counts_by_user_day(30)
    assert sum(r["count"] for r in daily if r["email"] == "a@tritorc.com") == 2


def test_log_scan_ignores_missing_user(repo):
    repo.log_scan("evt1", "Expo", "card1", None, "processed")
    assert repo.scan_totals_by_user() == []


def test_count_untracked_records(repo):
    from backend.models import BusinessCardRecord

    tracked = BusinessCardRecord(
        record_id="r1", card_id="c1", event_id="evt", date="2026-07-16", time="10:00:00",
        event_name="Expo", name="Jane", confidence_score="High", scanned_by="a@tritorc.com",
    )
    untracked = BusinessCardRecord(
        record_id="r2", card_id="c2", event_id="evt", date="2026-07-16", time="10:00:00",
        event_name="Expo", name="Old", confidence_score="High",
    )
    repo.ensure_event("evt", "Expo", "2026-07-16")
    repo.upsert_card_record(tracked)
    repo.upsert_card_record(untracked)
    assert repo.count_untracked_records() == 1

    # scanned_by must round-trip through the read path.
    by_card = {r.card_id: r for r in repo.list_records("evt")}
    assert by_card["c1"].scanned_by == "a@tritorc.com"
    assert by_card["c2"].scanned_by is None
