from backend.storage import mongo_usage


def test_config_report_does_not_connect_to_mongo(monkeypatch) -> None:
    monkeypatch.setattr(mongo_usage, "MONGO_USAGE_ENABLED", True)
    monkeypatch.setattr(mongo_usage, "MONGODB_URI", "mongodb://example")
    monkeypatch.setattr(mongo_usage, "_last_error", None)

    def fail_if_connected():
        raise AssertionError("config_report must not open a Mongo connection")

    monkeypatch.setattr(mongo_usage, "_get_collection", fail_if_connected)

    report = mongo_usage.config_report()

    assert report["enabled"] is True
    assert report["configured"] is True
    assert report["available"] is False
    assert report["checked"] is False


def test_uri_option_detection_is_case_insensitive() -> None:
    assert mongo_usage._uri_has_option("mongodb+srv://host/db?retryWrites=true&TLS=false", {"tls"})
    assert mongo_usage._uri_has_option("mongodb://host/db?ssl_ca_certs=/tmp/ca.pem", {"ssl_ca_certs"})
    assert not mongo_usage._uri_has_option("mongodb+srv://host/db?retryWrites=true", {"tls", "ssl"})


def test_check_limits_allows_when_mongo_not_configured(monkeypatch) -> None:
    monkeypatch.setattr(mongo_usage, "MONGO_USAGE_ENABLED", True)
    monkeypatch.setattr(mongo_usage, "MONGODB_URI", "")

    allowed, reason = mongo_usage.check_limits({"gemini": 1})

    assert allowed is True
    assert reason is None


def test_check_limits_allows_when_configured_mongo_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(mongo_usage, "MONGO_USAGE_ENABLED", True)
    monkeypatch.setattr(mongo_usage, "MONGODB_URI", "mongodb://example")
    monkeypatch.setattr(mongo_usage, "MONGO_USAGE_FAIL_CLOSED", True)
    monkeypatch.setattr(mongo_usage, "_last_error", "connection failed")
    monkeypatch.setattr(mongo_usage, "_get_collection", lambda: None)

    allowed, reason = mongo_usage.check_limits({"gemini": 1})

    assert allowed is True
    assert reason is None


def test_check_limits_blocks_projected_overage(monkeypatch) -> None:
    monkeypatch.setattr(mongo_usage, "MONGO_USAGE_ENABLED", True)
    monkeypatch.setattr(mongo_usage, "MONGODB_URI", "mongodb://example")
    monkeypatch.setattr(mongo_usage, "MONGO_USAGE_FAIL_CLOSED", True)
    monkeypatch.setattr(mongo_usage, "_get_collection", lambda: object())

    def fake_get_usage(provider: str, now=None):
        if provider == "gemini":
            return mongo_usage.MongoUsage(provider="gemini", period_kind="day", period="2026-07-09", used=119, limit=120)
        return mongo_usage.MongoUsage(
            provider="google_vision",
            period_kind="month",
            period="2026-07",
            used=100,
            limit=1000,
        )

    monkeypatch.setattr(mongo_usage, "get_usage", fake_get_usage)

    allowed, reason = mongo_usage.check_limits({"gemini": 2, "google_vision": 1})

    assert allowed is False
    assert "Gemini daily limit reached" in reason
    assert "this scan needs 2 requests" in reason
