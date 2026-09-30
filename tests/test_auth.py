"""认证与用户接口测试。"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from tests.conftest import USER_PAYLOAD

pytestmark = pytest.mark.asyncio

API = "/api/v1"


async def test_health_check(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "up"


async def test_root_info(client: AsyncClient) -> None:
    resp = await client.get("/")
    assert resp.status_code == 200
    assert resp.json()["api_prefix"] == API


async def test_register_creates_user_and_default_categories(
    client: AsyncClient, registered_user: dict
) -> None:
    assert registered_user["email"] == USER_PAYLOAD["email"]
    assert "password_hash" not in registered_user  # 哈希绝不外泄

    login = await client.post(
        f"{API}/auth/login",
        json={"email": USER_PAYLOAD["email"], "password": USER_PAYLOAD["password"]},
    )
    token = login.json()["access_token"]
    categories = await client.get(f"{API}/categories", headers={"Authorization": f"Bearer {token}"})
    assert categories.status_code == 200
    assert len(categories.json()) == 10  # 7 个支出分类 + 3 个收入分类
    assert all(item["is_system"] for item in categories.json())


async def test_register_duplicate_email_conflicts(client: AsyncClient, registered_user: dict) -> None:
    resp = await client.post(f"{API}/auth/register", json=USER_PAYLOAD)
    assert resp.status_code == 409
    assert resp.json()["code"] == "EMAIL_ALREADY_EXISTS"


async def test_register_email_is_case_insensitive(client: AsyncClient, registered_user: dict) -> None:
    resp = await client.post(
        f"{API}/auth/register",
        json={**USER_PAYLOAD, "email": USER_PAYLOAD["email"].upper()},
    )
    assert resp.status_code == 409


@pytest.mark.parametrize(
    "password",
    ["short1", "12345678", "abcdefgh", "a1"],
)
async def test_register_weak_password_rejected(client: AsyncClient, password: str) -> None:
    resp = await client.post(
        f"{API}/auth/register",
        json={"email": "weak@example.com", "password": password},
    )
    assert resp.status_code == 422


async def test_login_wrong_password(client: AsyncClient, registered_user: dict) -> None:
    resp = await client.post(
        f"{API}/auth/login",
        json={"email": USER_PAYLOAD["email"], "password": "WrongPass123"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "INVALID_CREDENTIALS"


async def test_login_unknown_email_same_error_code(client: AsyncClient) -> None:
    """账号不存在与密码错误返回同一错误码，防止账号枚举。"""
    resp = await client.post(
        f"{API}/auth/login", json={"email": "ghost@example.com", "password": "Passw0rd123"}
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "INVALID_CREDENTIALS"


async def test_me_requires_token(client: AsyncClient) -> None:
    resp = await client.get(f"{API}/auth/me")
    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHORIZED"


async def test_me_with_invalid_token(client: AsyncClient) -> None:
    resp = await client.get(f"{API}/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "INVALID_TOKEN"


async def test_me_returns_profile(client: AsyncClient, auth_headers: dict) -> None:
    resp = await client.get(f"{API}/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["nickname"] == USER_PAYLOAD["nickname"]


async def test_refresh_token_flow(client: AsyncClient, registered_user: dict) -> None:
    login = await client.post(
        f"{API}/auth/login",
        json={"email": USER_PAYLOAD["email"], "password": USER_PAYLOAD["password"]},
    )
    tokens = login.json()

    refreshed = await client.post(f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"] != tokens["access_token"]

    # 用 access_token 冒充 refresh_token 必须被拒绝（令牌类型校验）
    misuse = await client.post(f"{API}/auth/refresh", json={"refresh_token": tokens["access_token"]})
    assert misuse.status_code == 401
    assert misuse.json()["code"] == "INVALID_TOKEN"


async def test_update_profile(client: AsyncClient, auth_headers: dict) -> None:
    resp = await client.patch(f"{API}/auth/me", headers=auth_headers, json={"nickname": "新昵称"})
    assert resp.status_code == 200
    assert resp.json()["nickname"] == "新昵称"


async def test_logout_returns_message(client: AsyncClient, auth_headers: dict) -> None:
    resp = await client.post(f"{API}/auth/logout", headers=auth_headers)
    assert resp.status_code == 200
    assert "已登出" in resp.json()["message"]
