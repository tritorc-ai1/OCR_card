import pytest

from backend import auth


def test_hash_and_verify_password_roundtrip() -> None:
    hashed = auth.hash_password("correct horse battery")
    assert hashed != "correct horse battery"
    assert auth.verify_password("correct horse battery", hashed) is True
    assert auth.verify_password("wrong password", hashed) is False


def test_verify_password_is_safe_on_garbage_input() -> None:
    assert auth.verify_password("", "") is False
    assert auth.verify_password("x", "not-a-bcrypt-hash") is False


def test_validate_password_bounds() -> None:
    with pytest.raises(ValueError):
        auth.validate_password("short")  # < 8 chars
    with pytest.raises(ValueError):
        auth.validate_password("a" * 73)  # > 72 bytes
    auth.validate_password("just-long-enough")  # no raise


@pytest.mark.parametrize(
    "email,expected",
    [
        ("a.i@tritorc.com", True),
        ("A.I@TRITORC.COM", True),
        ("  spaced@tritorc.com  ", True),
        ("someone@gmail.com", False),
        ("evil@tritorc.com.attacker.io", False),
        ("evil@nottritorc.com", False),
        ("noatsign", False),
        ("@tritorc.com", False),
        ("two@@tritorc.com", False),
        ("", False),
    ],
)
def test_is_allowed_email(email: str, expected: bool) -> None:
    assert auth.is_allowed_email(email) is expected


def test_session_token_roundtrip() -> None:
    token = auth.create_session_token("A.I@tritorc.com", 3)
    payload = auth.parse_session_token(token)
    assert payload == {"e": "a.i@tritorc.com", "v": 3}


def test_parse_rejects_tampered_token() -> None:
    token = auth.create_session_token("a.i@tritorc.com", 0)
    assert auth.parse_session_token(token + "x") is None
    assert auth.parse_session_token("garbage.value.here") is None
    assert auth.parse_session_token("") is None


def test_parse_rejects_expired_token(monkeypatch) -> None:
    token = auth.create_session_token("a.i@tritorc.com", 0)
    # Negative TTL => negative max_age, so a just-minted token (age >= 0) is
    # always past it. (max_age=0 would be flaky when elapsed time rounds to 0.)
    monkeypatch.setattr(auth, "SESSION_TTL_DAYS", -1)
    assert auth.parse_session_token(token) is None
