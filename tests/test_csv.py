"""CSV 导入导出测试。"""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio

API = "/api/v1"


def _csv(*rows: str) -> bytes:
    header = "账期,金额,类型,分类,备注"
    return ("\n".join([header, *rows])).encode("utf-8")


async def test_export_contains_bom_and_rows(
    client: AsyncClient, auth_headers: dict, categories: dict
) -> None:
    await client.post(
        f"{API}/transactions",
        headers=auth_headers,
        json={
            "amount": "38.50",
            "kind": "expense",
            "category_id": categories["餐饮"]["id"],
            "occurred_on": date.today().isoformat(),
            "note": "午餐",
        },
    )
    resp = await client.get(f"{API}/transactions/csv/export", headers=auth_headers)
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    text = resp.content.decode("utf-8-sig")
    assert text.startswith("账期,金额,类型,分类,备注")
    assert "38.50" in text and "支出" in text and "餐饮" in text


async def test_template_download(client: AsyncClient, auth_headers: dict) -> None:
    resp = await client.get(f"{API}/transactions/csv/template", headers=auth_headers)
    assert resp.status_code == 200
    lines = resp.content.decode("utf-8-sig").strip().splitlines()
    assert lines[0].split(",") == ["账期", "金额", "类型", "分类", "备注"]
    assert len(lines) == 3  # 表头 + 2 行示例


async def test_import_success_creates_categories(client: AsyncClient, auth_headers: dict) -> None:
    payload = _csv(
        f"{date.today().isoformat()},38.50,支出,餐饮,午餐",
        f"{date.today().isoformat()},12000.00,收入,工资,月薪",
        f"{date.today().isoformat()},9.90,支出,新分类,自动建分类",
    )
    resp = await client.post(
        f"{API}/transactions/csv/import",
        headers=auth_headers,
        files={"file": ("data.csv", payload, "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert body["imported"] == 3
    assert body["failed"] == 0

    cats = await client.get(f"{API}/categories", headers=auth_headers)
    assert "新分类" in {c["name"] for c in cats.json()}


async def test_import_deduplicates_repeated_rows(client: AsyncClient, auth_headers: dict) -> None:
    payload = _csv(f"{date.today().isoformat()},38.50,支出,餐饮,午餐")
    first = await client.post(
        f"{API}/transactions/csv/import",
        headers=auth_headers,
        files={"file": ("data.csv", payload, "text/csv")},
    )
    second = await client.post(
        f"{API}/transactions/csv/import",
        headers=auth_headers,
        files={"file": ("data.csv", payload, "text/csv")},
    )
    assert first.json()["imported"] == 1
    assert second.json()["imported"] == 0
    assert second.json()["failed"] == 1
    assert "重复" in second.json()["errors"][0]["reason"]


async def test_import_reports_row_level_errors(client: AsyncClient, auth_headers: dict) -> None:
    payload = _csv(
        f"{date.today().isoformat()},38.50,支出,餐饮,正常",
        "2025-13-45,20.00,支出,餐饮,日期非法",
        f"{date.today().isoformat()},-5,支出,餐饮,金额非法",
        f"{date.today().isoformat()},10.00,未知,餐饮,类型非法",
    )
    resp = await client.post(
        f"{API}/transactions/csv/import",
        headers=auth_headers,
        files={"file": ("data.csv", payload, "text/csv")},
    )
    body = resp.json()
    assert body["imported"] == 1
    assert body["failed"] == 3
    assert [e["row"] for e in body["errors"]] == [3, 4, 5]  # 行号从表头下一行开始


async def test_import_accepts_gbk_encoding(client: AsyncClient, auth_headers: dict) -> None:
    payload = _csv(f"{date.today().isoformat()},12.00,支出,餐饮,GBK编码").decode("utf-8").encode("gbk")
    resp = await client.post(
        f"{API}/transactions/csv/import",
        headers=auth_headers,
        files={"file": ("data.csv", payload, "text/csv")},
    )
    assert resp.status_code == 200
    assert resp.json()["imported"] == 1


async def test_import_rejects_wrong_extension(client: AsyncClient, auth_headers: dict) -> None:
    resp = await client.post(
        f"{API}/transactions/csv/import",
        headers=auth_headers,
        files={"file": ("data.txt", b"whatever", "text/plain")},
    )
    assert resp.status_code == 400


async def test_import_rejects_empty_file(client: AsyncClient, auth_headers: dict) -> None:
    resp = await client.post(
        f"{API}/transactions/csv/import",
        headers=auth_headers,
        files={"file": ("data.csv", b"", "text/csv")},
    )
    assert resp.status_code == 400


async def test_import_rejects_bad_header(client: AsyncClient, auth_headers: dict) -> None:
    resp = await client.post(
        f"{API}/transactions/csv/import",
        headers=auth_headers,
        files={"file": ("data.csv", b"a,b\n1,2", "text/csv")},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "CSV_FORMAT_ERROR"


async def test_round_trip_export_then_import(
    client: AsyncClient, auth_headers: dict, categories: dict
) -> None:
    """导出 → 新账号导入，数据条数与金额必须一致（端到端回归）。"""
    for amount in ("11.11", "22.22", "33.33"):
        await client.post(
            f"{API}/transactions",
            headers=auth_headers,
            json={
                "amount": amount,
                "kind": "expense",
                "category_id": categories["餐饮"]["id"],
                "occurred_on": date.today().isoformat(),
                "note": f"备注{amount}",
            },
        )
    exported = await client.get(f"{API}/transactions/csv/export", headers=auth_headers)

    other = await client.post(
        f"{API}/auth/register", json={"email": "csv@example.com", "password": "Passw0rd123"}
    )
    assert other.status_code == 201
    login = await client.post(
        f"{API}/auth/login", json={"email": "csv@example.com", "password": "Passw0rd123"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    imported = await client.post(
        f"{API}/transactions/csv/import",
        headers=headers,
        files={"file": ("export.csv", exported.content, "text/csv")},
    )
    assert imported.json()["imported"] == 3

    listed = await client.get(f"{API}/transactions", headers=headers)
    assert listed.json()["total"] == 3
    assert sum(float(i["amount"]) for i in listed.json()["items"]) == pytest.approx(66.66)
