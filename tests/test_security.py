"""安全与工具单元测试：bcrypt 哈希、JWT 生命周期、令牌桶限流。"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import settings
from app.core.exceptions import InvalidTokenError, TokenExpiredError
from app.core.security import (
    _create_token,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.middleware.rate_limit import TokenBucket

# 本文件全是同步单元测试：不要加 asyncio 标记，否则 pytest-asyncio 会告警


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------
def test_hash_is_salted_and_verifiable() -> None:
    password = "Passw0rd123"
    h1, h2 = hash_password(password), hash_password(password)
    assert h1 != h2, "相同密码必须产生不同哈希（随机 salt）"
    assert h1.startswith("$2b$"), "使用 bcrypt 算法标识"
    assert password not in h1, "哈希中不得出现明文"


def test_verify_password_rejects_wrong_input() -> None:
    hashed = hash_password("Passw0rd123")
    assert verify_password("Passw0rd123", hashed) is True
    assert verify_password("passw0rd123", hashed) is False
    assert verify_password("", hashed) is False


def test_verify_password_survives_malformed_hash() -> None:
    assert verify_password("whatever", "not-a-bcrypt-hash") is False


def test_hash_rejects_overlong_password() -> None:
    with pytest.raises(ValueError):
        hash_password("x" * 100)


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
def test_access_token_roundtrip() -> None:
    token, expire = create_access_token(42)
    payload = decode_token(token, expected_type="access")
    assert payload["sub"] == "42"
    assert payload["type"] == "access"
    assert payload["iss"] == settings.jwt_issuer
    assert expire > datetime.now(UTC)


def test_token_type_mismatch_rejected() -> None:
    refresh, _jti, _exp = create_refresh_token(1)
    with pytest.raises(InvalidTokenError):
        decode_token(refresh, expected_type="access")
    assert decode_token(refresh, expected_type="refresh")["type"] == "refresh"


def test_tampered_token_rejected() -> None:
    token, _ = create_access_token(1)
    with pytest.raises(InvalidTokenError):
        decode_token(token[:-3] + "abc")


def test_wrong_signature_rejected() -> None:
    forged = jwt.encode(
        {"sub": "1", "type": "access", "exp": 9999999999, "iat": 1, "iss": settings.jwt_issuer},
        "a" * 40,
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        decode_token(forged)


def test_expired_token_raises_specific_error() -> None:
    token, _jti, _exp = _create_token("1", "access", timedelta(seconds=-10))
    with pytest.raises(TokenExpiredError):
        decode_token(token)


def test_wrong_issuer_rejected() -> None:
    forged = jwt.encode(
        {"sub": "1", "type": "access", "exp": 9999999999, "iat": 1, "iss": "evil"},
        settings.secret_key,
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        decode_token(forged)


# ---------------------------------------------------------------------------
# 限流
# ---------------------------------------------------------------------------
def test_token_bucket_allows_burst_then_blocks() -> None:
    bucket = TokenBucket(capacity=3, window=60.0)
    assert [bucket.consume() for _ in range(3)] == [True, True, True]
    assert bucket.consume() is False
    assert bucket.retry_after() >= 1


def test_token_bucket_refills_over_time() -> None:
    bucket = TokenBucket(capacity=10, window=1.0)  # 每 0.1s 补 1 个
    for _ in range(10):
        bucket.consume()
    assert bucket.consume() is False
    time.sleep(0.15)
    assert bucket.consume() is True
