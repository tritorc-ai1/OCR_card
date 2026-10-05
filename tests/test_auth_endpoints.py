"""End-to-end auth/admin endpoint tests using TestClient + in-memory Mongo."""
import mongomock
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    from backend.storage import mongo as mongo_mod

    mongo_client = mongomock.MongoClient()
    db = mongo_client["cardscan"]
    monkeypatch.setattr(mongo_mod, "get_database", lambda: db)

    # Seed one admin and one active regular user directly through the repo.
    from backend import auth
    from backend.storage import users

    users.create_user("admin@tritorc.com", auth.hash_password("adminpass1"), role="admin", created_by="seed")
    users.create_user("bob@tritorc.com", auth.hash_password("bobpass123"), role="user", created_by="admin@tritorc.com")

    import backend.main as main_mod

    return TestClient(main_mod.app)


def _login(client, email, password):
    return client.post("/auth/login", json={"email": email, "password": password})


def test_login_sets_httponly_cookie_and_returns_role(client):
    resp = _login(client, "admin@tritorc.com", "adminpass1")
    assert resp.status_code == 200
    assert resp.json() == {"email": "admin@tritorc.com", "role": "admin"}
    set_cookie = resp.headers.get("set-cookie", "")
    assert "cardscan_session=" in set_cookie
    assert "httponly" in set_cookie.lower()


def test_login_uniform_401_on_bad_credentials_and_unknown_user(client):
    wrong = _login(client, "bob@tritorc.com", "wrongpass")
    unknown = _login(client, "ghost@tritorc.com", "whatever12")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"] == "Invalid email or password"


def test_me_requires_auth(client):
    assert client.get("/auth/me").status_code == 401
    _login(client, "bob@tritorc.com", "bobpass123")
    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json() == {"email": "bob@tritorc.com", "role": "user"}


def test_admin_stats_forbidden_for_regular_user(client):
    _login(client, "bob@tritorc.com", "bobpass123")
    assert client.get("/admin/stats").status_code == 403


def test_admin_stats_ok_for_admin(client):
    _login(client, "admin@tritorc.com", "adminpass1")
    resp = client.get("/admin/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"totals", "by_event", "daily", "untracked_records", "window_days"}


def test_admin_create_user_domain_and_duplicate(client):
    _login(client, "admin@tritorc.com", "adminpass1")
    bad = client.post("/admin/users", json={"email": "x@gmail.com", "password": "password12"})
    assert bad.status_code == 400
    ok = client.post("/admin/users", json={"email": "carol@tritorc.com", "password": "password12"})
    assert ok.status_code == 200
    assert ok.json()["role"] == "user"
    dup = client.post("/admin/users", json={"email": "carol@tritorc.com", "password": "password12"})
    assert dup.status_code == 409


def test_create_user_forbidden_for_regular_user(client):
    _login(client, "bob@tritorc.com", "bobpass123")
    assert client.post("/admin/users", json={"email": "z@tritorc.com", "password": "password12"}).status_code == 403


def test_deactivated_user_cookie_is_rejected(client):
    _login(client, "bob@tritorc.com", "bobpass123")
    assert client.get("/auth/me").status_code == 200
    # Admin deactivates bob (use the same client's admin login via a second client cookie jar).
    from backend.storage import users

    users.set_user_active("bob@tritorc.com", False)
    assert client.get("/auth/me").status_code == 401


def test_password_reset_invalidates_old_cookie(client):
    _login(client, "bob@tritorc.com", "bobpass123")
    from backend.storage import users

    users.set_user_password("bob@tritorc.com", "irrelevanthash")
    # bob's existing cookie carries the old token_version -> now invalid.
    assert client.get("/auth/me").status_code == 401


def test_change_password_keeps_own_session(client):
    _login(client, "bob@tritorc.com", "bobpass123")
    resp = client.post(
        "/auth/change-password",
        json={"current_password": "bobpass123", "new_password": "newbobpass1"},
    )
    assert resp.status_code == 200
    # Cookie was re-issued with the bumped token_version, so the session survives.
    assert client.get("/auth/me").status_code == 200


def test_change_password_wrong_current_rejected(client):
    _login(client, "bob@tritorc.com", "bobpass123")
    resp = client.post(
        "/auth/change-password",
        json={"current_password": "nope", "new_password": "newbobpass1"},
    )
    assert resp.status_code == 401


def test_admin_cannot_self_deactivate(client):
    _login(client, "admin@tritorc.com", "adminpass1")
    resp = client.patch("/admin/users/admin@tritorc.com", json={"active": False})
    assert resp.status_code == 400


def test_health_and_index_stay_public(client):
    assert client.get("/health").status_code == 200
