"""统计接口测试：验证 SQL 聚合口径是否正确。"""

from __future__ import annotations

import calendar
from datetime import date

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

API = "/api/v1"


def _this_month() -> str:
    today = date.today()
    return f"{today.year:04d}-{today.month:02d}"


def _day(day: int) -> str:
    today = date.today()
    last = calendar.monthrange(today.year, today.month)[1]
    return date(today.year, today.month, min(day, last)).isoformat()


async def _add(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    amount: str,
    kind: str,
    category_id: int | None,
    day: int,
) -> None:
    resp = await client.post(
        f"{API}/transactions",
        headers=headers,
        json={
            "amount": amount,
            "kind": kind,
            "category_id": category_id,
            "occurred_on": _day(day),
            "note": "seed",
        },
    )
    assert resp.status_code == 201, resp.text


async def test_summary_computes_income_expense_balance_and_saving_rate(
    client: AsyncClient, auth_headers: dict, categories: dict
) -> None:
    await _add(
        client, auth_headers, amount="10000.00", kind="income", category_id=categories["工资"]["id"], day=1
    )
    await _add(
        client, auth_headers, amount="3000.00", kind="expense", category_id=categories["居住"]["id"], day=2
    )
    await _add(
        client, auth_headers, amount="1000.00", kind="expense", category_id=categories["餐饮"]["id"], day=3
    )

    resp = await client.get(f"{API}/stats/summary?month={_this_month()}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["income"] == "10000.00"
    assert body["expense"] == "4000.00"
    assert body["balance"] == "6000.00"
    assert body["saving_rate"] == 60.0
    assert body["transaction_count"] == 3
    assert float(body["avg_daily_expense"]) > 0


async def test_summary_empty_month_all_zero(client: AsyncClient, auth_headers: dict) -> None:
    resp = await client.get(f"{API}/stats/summary?month=2000-01", headers=auth_headers)
    body = resp.json()
    assert body["income"] == "0.00"
    assert body["expense"] == "0.00"
    assert body["balance"] == "0.00"
    assert body["saving_rate"] == 0.0  # 收入为 0 时不允许除零崩溃


async def test_summary_invalid_month_format(client: AsyncClient, auth_headers: dict) -> None:
    resp = await client.get(f"{API}/stats/summary?month=2025-13", headers=auth_headers)
    assert resp.status_code == 422  # 由 Query(pattern) 拦截


async def test_summary_invalid_month_logic(
    client: AsyncClient, auth_headers: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """绕过路由正则直接测 Service 层解析逻辑，保证非法月份返回 400 而非 500。"""
    from app.core.exceptions import BadRequestError
    from app.services.stats_service import parse_month

    with pytest.raises(BadRequestError):
        parse_month("2025/01")


async def test_category_breakdown_percentage_sums_to_100(
    client: AsyncClient, auth_headers: dict, categories: dict
) -> None:
    await _add(
        client, auth_headers, amount="600.00", kind="expense", category_id=categories["餐饮"]["id"], day=1
    )
    await _add(
        client, auth_headers, amount="400.00", kind="expense", category_id=categories["交通"]["id"], day=2
    )

    resp = await client.get(
        f"{API}/stats/categories?kind=expense&month={_this_month()}", headers=auth_headers
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [i["category_name"] for i in items] == ["餐饮", "交通"]  # 按金额倒序
    assert items[0]["percentage"] == 60.0
    assert sum(i["percentage"] for i in items) == 100.0


async def test_category_breakdown_handles_uncategorized(client: AsyncClient, auth_headers: dict) -> None:
    await _add(client, auth_headers, amount="50.00", kind="expense", category_id=None, day=1)
    resp = await client.get(
        f"{API}/stats/categories?kind=expense&month={_this_month()}", headers=auth_headers
    )
    assert resp.json()["items"][0]["category_name"] == "未归类"
    assert resp.json()["items"][0]["category_id"] is None


async def test_trend_fills_empty_months(client: AsyncClient, auth_headers: dict, categories: dict) -> None:
    await _add(
        client, auth_headers, amount="100.00", kind="expense", category_id=categories["餐饮"]["id"], day=1
    )

    resp = await client.get(f"{API}/stats/trend?months=6", headers=auth_headers)
    assert resp.status_code == 200
    points = resp.json()["points"]
    assert len(points) == 6
    assert len(resp.json()["months"]) == 6
    # 没有账单的月份必须补 0，否则前端折线图会断点
    assert sum(1 for p in points if p["expense"] == "0.00") == 5
    assert points[-1]["expense"] == "100.00"


async def test_trend_rejects_out_of_range_months(client: AsyncClient, auth_headers: dict) -> None:
    assert (await client.get(f"{API}/stats/trend?months=0", headers=auth_headers)).status_code == 422
    assert (await client.get(f"{API}/stats/trend?months=99", headers=auth_headers)).status_code == 422


async def test_calendar_returns_every_day_of_month(
    client: AsyncClient, auth_headers: dict, categories: dict
) -> None:
    await _add(
        client, auth_headers, amount="88.00", kind="expense", category_id=categories["餐饮"]["id"], day=1
    )
    resp = await client.get(f"{API}/stats/calendar?month={_this_month()}", headers=auth_headers)
    body = resp.json()
    expected_days = calendar.monthrange(date.today().year, date.today().month)[1]
    assert len(body["days"]) == expected_days
    assert body["days"][0]["expense"] == "88.00"
    assert body["days"][1]["expense"] == "0.00"


async def test_stats_isolated_between_users(client: AsyncClient, auth_headers: dict) -> None:
    await client.post(f"{API}/auth/register", json={"email": "solo@example.com", "password": "Passw0rd123"})
    login = await client.post(
        f"{API}/auth/login", json={"email": "solo@example.com", "password": "Passw0rd123"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    resp = await client.get(f"{API}/stats/summary?month={_this_month()}", headers=headers)
    assert resp.json()["transaction_count"] == 0
