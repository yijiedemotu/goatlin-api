"""分类接口测试：CRUD、唯一约束、越权与删除保护。"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

API = "/api/v1"


async def test_default_categories_grouped_by_kind(client: AsyncClient, auth_headers: dict) -> None:
    income = await client.get(f"{API}/categories?kind=income", headers=auth_headers)
    expense = await client.get(f"{API}/categories?kind=expense", headers=auth_headers)
    assert {c["name"] for c in income.json()} == {"工资", "奖金", "理财收益"}
    assert len(expense.json()) == 7


async def test_create_category(client: AsyncClient, auth_headers: dict) -> None:
    resp = await client.post(
        f"{API}/categories",
        headers=auth_headers,
        json={"name": "宠物", "kind": "expense", "color": "#22D3EE", "icon": "cat"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "宠物"
    assert body["is_system"] is False


async def test_create_duplicate_category_conflicts(client: AsyncClient, auth_headers: dict) -> None:
    payload = {"name": "宠物", "kind": "expense"}
    first = await client.post(f"{API}/categories", headers=auth_headers, json=payload)
    assert first.status_code == 201
    second = await client.post(f"{API}/categories", headers=auth_headers, json=payload)
    assert second.status_code == 409
    assert second.json()["code"] == "CATEGORY_NAME_CONFLICT"


async def test_same_name_allowed_across_kinds(client: AsyncClient, auth_headers: dict) -> None:
    """「报销」既可能是收入也可能是支出，唯一约束是 (user_id, name, kind)。"""
    a = await client.post(f"{API}/categories", headers=auth_headers, json={"name": "报销", "kind": "income"})
    b = await client.post(f"{API}/categories", headers=auth_headers, json={"name": "报销", "kind": "expense"})
    assert (a.status_code, b.status_code) == (201, 201)


async def test_invalid_color_rejected(client: AsyncClient, auth_headers: dict) -> None:
    resp = await client.post(
        f"{API}/categories",
        headers=auth_headers,
        json={"name": "颜色错", "kind": "expense", "color": "red"},
    )
    assert resp.status_code == 422


async def test_update_category(client: AsyncClient, auth_headers: dict) -> None:
    created = await client.post(
        f"{API}/categories", headers=auth_headers, json={"name": "宠物", "kind": "expense"}
    )
    cid = created.json()["id"]
    resp = await client.patch(
        f"{API}/categories/{cid}", headers=auth_headers, json={"name": "萌宠", "color": "#123456"}
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "萌宠"
    assert resp.json()["color"] == "#123456"


async def test_delete_empty_category(client: AsyncClient, auth_headers: dict) -> None:
    created = await client.post(
        f"{API}/categories", headers=auth_headers, json={"name": "临时", "kind": "expense"}
    )
    cid = created.json()["id"]
    assert (await client.delete(f"{API}/categories/{cid}", headers=auth_headers)).status_code == 204


async def test_delete_system_category_rejected(
    client: AsyncClient, auth_headers: dict, categories: dict
) -> None:
    cid = categories["餐饮"]["id"]
    resp = await client.delete(f"{API}/categories/{cid}", headers=auth_headers)
    assert resp.status_code == 409
    assert resp.json()["code"] == "CATEGORY_IN_USE"


async def test_delete_category_in_use_rejected(
    client: AsyncClient, auth_headers: dict, categories: dict
) -> None:
    created = await client.post(
        f"{API}/categories", headers=auth_headers, json={"name": "宠物", "kind": "expense"}
    )
    cid = created.json()["id"]
    await client.post(
        f"{API}/transactions",
        headers=auth_headers,
        json={
            "amount": "50.00",
            "kind": "expense",
            "category_id": cid,
            "occurred_on": "2025-03-01",
            "note": "猫粮",
        },
    )
    resp = await client.delete(f"{API}/categories/{cid}", headers=auth_headers)
    assert resp.status_code == 409
    assert "1 笔账单" in resp.json()["message"]


async def test_usage_count_returned(client: AsyncClient, auth_headers: dict, categories: dict) -> None:
    cid = categories["餐饮"]["id"]
    for amount in ("10.00", "20.00"):
        await client.post(
            f"{API}/transactions",
            headers=auth_headers,
            json={
                "amount": amount,
                "kind": "expense",
                "category_id": cid,
                "occurred_on": "2025-03-02",
            },
        )
    resp = await client.get(f"{API}/categories?with_usage=true", headers=auth_headers)
    usage = {c["name"]: c["usage_count"] for c in resp.json()}
    assert usage["餐饮"] == 2
    assert usage["交通"] == 0


async def test_cannot_touch_other_users_category(client: AsyncClient, auth_headers: dict) -> None:
    """水平越权防护：别人的分类一律 404，不泄露资源是否存在。"""
    other = await client.post(
        f"{API}/auth/register",
        json={"email": "other@example.com", "password": "Passw0rd123"},
    )
    assert other.status_code == 201
    other_login = await client.post(
        f"{API}/auth/login", json={"email": "other@example.com", "password": "Passw0rd123"}
    )
    other_headers = {"Authorization": f"Bearer {other_login.json()['access_token']}"}
    mine = await client.post(
        f"{API}/categories", headers=auth_headers, json={"name": "私有", "kind": "expense"}
    )
    cid = mine.json()["id"]

    assert (
        await client.patch(f"{API}/categories/{cid}", headers=other_headers, json={"name": "偷改"})
    ).status_code == 404
    assert (await client.delete(f"{API}/categories/{cid}", headers=other_headers)).status_code == 404


async def test_kinds_endpoint_not_shadowed_by_path_param(client: AsyncClient) -> None:
    """静态路由 /categories/kinds 不能被 /categories/{category_id} 抢占。"""
    resp = await client.get(f"{API}/categories/kinds")
    assert resp.status_code == 200
    assert {item["value"] for item in resp.json()} == {"income", "expense"}
