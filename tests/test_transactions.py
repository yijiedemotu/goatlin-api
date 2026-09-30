"""账单接口测试：校验、过滤、分页、越权、分类方向一致性。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

API = "/api/v1"


def _today(offset: int = 0) -> str:
    return (date.today() + timedelta(days=offset)).isoformat()


async def _create(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    amount: str,
    kind: str = "expense",
    category_id: int | None = None,
    occurred_on: str | None = None,
    note: str = "",
) -> dict:
    resp = await client.post(
        f"{API}/transactions",
        headers=headers,
        json={
            "amount": amount,
            "kind": kind,
            "category_id": category_id,
            "occurred_on": occurred_on or _today(),
            "note": note,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_create_transaction_returns_category(
    client: AsyncClient, auth_headers: dict, categories: dict
) -> None:
    tx = await _create(
        client, auth_headers, amount="38.50", category_id=categories["餐饮"]["id"], note="午餐"
    )
    assert tx["amount"] == "38.50"
    assert tx["category"]["name"] == "餐饮"  # 一次请求即带出关联，前端无需二次查询
    assert tx["source"] == "manual"


async def test_amount_precision_kept_as_decimal(client: AsyncClient, auth_headers: dict) -> None:
    """0.1 + 0.2 类浮点误差在记账场景不可接受，金额全程 Decimal。"""
    tx = await _create(client, auth_headers, amount="0.10")
    assert tx["amount"] == "0.10"


@pytest.mark.parametrize("amount", ["0", "-5", "0.001", "1000000000"])
async def test_invalid_amount_rejected(client: AsyncClient, auth_headers: dict, amount: str) -> None:
    resp = await client.post(
        f"{API}/transactions",
        headers=auth_headers,
        json={"amount": amount, "kind": "expense", "occurred_on": _today()},
    )
    assert resp.status_code == 422


async def test_future_date_rejected(client: AsyncClient, auth_headers: dict) -> None:
    resp = await client.post(
        f"{API}/transactions",
        headers=auth_headers,
        json={"amount": "10.00", "kind": "expense", "occurred_on": _today(1)},
    )
    assert resp.status_code == 422


async def test_category_kind_mismatch_rejected(
    client: AsyncClient, auth_headers: dict, categories: dict
) -> None:
    """把收入分类挂到支出账单上必须拦住，否则统计口径会被污染。"""
    resp = await client.post(
        f"{API}/transactions",
        headers=auth_headers,
        json={
            "amount": "100.00",
            "kind": "expense",
            "category_id": categories["工资"]["id"],
            "occurred_on": _today(),
        },
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "BAD_REQUEST"


async def test_unknown_category_rejected(client: AsyncClient, auth_headers: dict) -> None:
    resp = await client.post(
        f"{API}/transactions",
        headers=auth_headers,
        json={"amount": "10.00", "kind": "expense", "category_id": 999999, "occurred_on": _today()},
    )
    assert resp.status_code == 404


async def test_list_pagination_and_total(client: AsyncClient, auth_headers: dict) -> None:
    for i in range(25):
        await _create(client, auth_headers, amount=f"{i + 1}.00", note=f"第{i}笔")

    page1 = await client.get(f"{API}/transactions?page=1&size=10", headers=auth_headers)
    page3 = await client.get(f"{API}/transactions?page=3&size=10", headers=auth_headers)

    assert page1.json()["total"] == 25
    assert len(page1.json()["items"]) == 10
    assert len(page3.json()["items"]) == 5


async def test_list_filters(client: AsyncClient, auth_headers: dict, categories: dict) -> None:
    await _create(
        client,
        auth_headers,
        amount="30.00",
        note="午餐",
        occurred_on=_today(-5),
        category_id=categories["餐饮"]["id"],
    )
    await _create(
        client,
        auth_headers,
        amount="200.00",
        note="超市",
        occurred_on=_today(-2),
        category_id=categories["购物"]["id"],
    )
    await _create(
        client,
        auth_headers,
        amount="5000.00",
        kind="income",
        note="工资",
        occurred_on=_today(-1),
        category_id=categories["工资"]["id"],
    )

    by_kind = await client.get(f"{API}/transactions?kind=income", headers=auth_headers)
    assert by_kind.json()["total"] == 1

    by_range = await client.get(
        f"{API}/transactions?start_date={_today(-3)}&end_date={_today()}", headers=auth_headers
    )
    assert by_range.json()["total"] == 2

    by_amount = await client.get(f"{API}/transactions?min_amount=100", headers=auth_headers)
    assert by_amount.json()["total"] == 2

    by_keyword = await client.get(f"{API}/transactions?keyword=午餐", headers=auth_headers)
    assert by_keyword.json()["total"] == 1
    assert by_keyword.json()["items"][0]["note"] == "午餐"

    by_category = await client.get(
        f"{API}/transactions?category_id={categories['餐饮']['id']}", headers=auth_headers
    )
    assert by_category.json()["total"] == 1


async def test_list_sorted_by_date_desc(client: AsyncClient, auth_headers: dict) -> None:
    await _create(client, auth_headers, amount="1.00", occurred_on=_today(-10))
    await _create(client, auth_headers, amount="2.00", occurred_on=_today(-1))
    await _create(client, auth_headers, amount="3.00", occurred_on=_today(-5))
    resp = await client.get(f"{API}/transactions", headers=auth_headers)
    dates = [item["occurred_on"] for item in resp.json()["items"]]
    assert dates == sorted(dates, reverse=True)


async def test_invalid_date_range_returns_400(client: AsyncClient, auth_headers: dict) -> None:
    resp = await client.get(
        f"{API}/transactions?start_date=2025-05-01&end_date=2025-01-01", headers=auth_headers
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_DATE_RANGE"


async def test_update_transaction(client: AsyncClient, auth_headers: dict) -> None:
    tx = await _create(client, auth_headers, amount="10.00", note="原备注")
    resp = await client.patch(
        f"{API}/transactions/{tx['id']}", headers=auth_headers, json={"amount": "88.88", "note": "改过"}
    )
    assert resp.status_code == 200
    assert resp.json()["amount"] == "88.88"
    assert resp.json()["note"] == "改过"


async def test_update_with_empty_body_rejected(client: AsyncClient, auth_headers: dict) -> None:
    tx = await _create(client, auth_headers, amount="10.00")
    resp = await client.patch(f"{API}/transactions/{tx['id']}", headers=auth_headers, json={})
    assert resp.status_code == 422


async def test_delete_transaction(client: AsyncClient, auth_headers: dict) -> None:
    tx = await _create(client, auth_headers, amount="10.00")
    assert (await client.delete(f"{API}/transactions/{tx['id']}", headers=auth_headers)).status_code == 204
    assert (await client.get(f"{API}/transactions/{tx['id']}", headers=auth_headers)).status_code == 404


async def test_transactions_isolated_between_users(client: AsyncClient, auth_headers: dict) -> None:
    await _create(client, auth_headers, amount="66.00")

    await client.post(f"{API}/auth/register", json={"email": "other@example.com", "password": "Passw0rd123"})
    other = await client.post(
        f"{API}/auth/login", json={"email": "other@example.com", "password": "Passw0rd123"}
    )
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}

    resp = await client.get(f"{API}/transactions", headers=other_headers)
    assert resp.json()["total"] == 0  # 数据隔离：新用户看不到别人的账单


async def test_unauthorized_access_blocked(client: AsyncClient) -> None:
    assert (await client.get(f"{API}/transactions")).status_code == 401
