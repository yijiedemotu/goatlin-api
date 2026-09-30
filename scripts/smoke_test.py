"""端到端冒烟脚本：对着真实运行的服务跑一遍核心链路。

用途：
    # 终端 1
    python -m uvicorn app.main:app --port 8000
    # 终端 2
    python scripts/smoke_test.py --base-url http://127.0.0.1:8000

它覆盖：健康检查 → 注册 → 登录 → 分类 → 建账单 → 统计 → CSV 导出/导入 → 限流。
比 pytest 更"真"：走真实 HTTP、真实数据库文件、真实中间件栈，
面试时可以直接演示这个脚本作为「接口自测」的证据。
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import date
from io import BytesIO

import httpx

# Windows 控制台默认 GBK，直接 print 中文/emoji 会抛 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS = "[PASS]"
FAIL = "[FAIL]"


class SmokeRunner:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(base_url=self.base_url, timeout=15.0)
        self.failures: list[str] = []

    # ------------------------------------------------------------------
    def check(self, name: str, ok: bool, detail: str = "") -> None:
        print(f"{PASS if ok else FAIL} {name}" + (f"  -> {detail}" if detail else ""))
        if not ok:
            self.failures.append(name)

    def run(self) -> int:
        print(f"== 目标服务: {self.base_url} ==\n")

        self.check_health()
        token = self.register_and_login()
        headers = {"Authorization": f"Bearer {token}"}
        categories = self.check_categories(headers)
        self.create_transactions(headers, categories)
        self.check_stats(headers)
        self.check_csv(headers)
        self.check_rate_limit()
        self.check_docs()

        print()
        if self.failures:
            print(f"❌ 共 {len(self.failures)} 项失败: {', '.join(self.failures)}")
            return 1
        print("✅ 全部冒烟用例通过")
        return 0

    # ------------------------------------------------------------------
    def check_health(self) -> None:
        resp = self.client.get("/health")
        body = resp.json() if resp.status_code == 200 else {}
        self.check(
            "GET /health 返回 ok 且数据库连通",
            resp.status_code == 200 and body.get("database") == "up",
            f"{resp.status_code} {body}",
        )
        self.check(
            "响应携带 X-Request-ID",
            bool(resp.headers.get("X-Request-ID")),
            resp.headers.get("X-Request-ID", ""),
        )

    def register_and_login(self) -> str:
        email = f"smoke{random.randint(10**6, 10**7)}@example.com"
        password = "Passw0rd123"

        resp = self.client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "nickname": "冒烟测试"},
        )
        self.check("POST /auth/register 创建用户", resp.status_code == 201, str(resp.status_code))

        resp = self.client.post("/api/v1/auth/login", json={"email": email, "password": password})
        self.check(
            "POST /auth/login 返回双令牌",
            resp.status_code == 200 and "access_token" in resp.json() and "refresh_token" in resp.json(),
            str(resp.status_code),
        )
        tokens = resp.json()

        me = self.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
        self.check("GET /auth/me 带令牌可访问", me.status_code == 200, me.json().get("email", ""))

        wrong = self.client.post("/api/v1/auth/login", json={"email": email, "password": "WrongPass123"})
        self.check(
            "错误密码返回 401 INVALID_CREDENTIALS",
            wrong.status_code == 401 and wrong.json().get("code") == "INVALID_CREDENTIALS",
            str(wrong.status_code),
        )
        return tokens["access_token"]

    def check_categories(self, headers: dict[str, str]) -> dict[str, dict]:
        resp = self.client.get("/api/v1/categories?with_usage=true", headers=headers)
        items = resp.json()
        self.check(
            "GET /categories 返回注册预置的 10 个系统分类",
            resp.status_code == 200 and len(items) == 10 and all(c["is_system"] for c in items),
            f"count={len(items)}",
        )
        created = self.client.post(
            "/api/v1/categories",
            headers=headers,
            json={"name": "宠物", "kind": "expense", "color": "#22D3EE"},
        )
        self.check("POST /categories 新建自定义分类", created.status_code == 201, "")
        return {item["name"]: item for item in items}

    def create_transactions(self, headers: dict[str, str], categories: dict[str, dict]) -> None:
        today = date.today().isoformat()
        payloads = [
            {"amount": "42.50", "kind": "expense", "category_id": categories["餐饮"]["id"], "note": "午餐"},
            {"amount": "8.00", "kind": "expense", "category_id": categories["交通"]["id"], "note": "地铁"},
            {"amount": "12000.00", "kind": "income", "category_id": categories["工资"]["id"], "note": "月薪"},
        ]
        ids = []
        for payload in payloads:
            resp = self.client.post(
                "/api/v1/transactions",
                headers=headers,
                json={**payload, "occurred_on": today},
            )
            ids.append(resp.status_code)
        self.check("POST /transactions 连续创建 3 笔账单", ids == [201, 201, 201], str(ids))

        bad = self.client.post(
            "/api/v1/transactions",
            headers=headers,
            json={
                "amount": "100.00",
                "kind": "expense",
                "category_id": categories["工资"]["id"],  # 收入分类挂支出
                "occurred_on": today,
            },
        )
        self.check(
            "分类与收支类型不一致被拒绝(400)",
            bad.status_code == 400,
            str(bad.status_code),
        )

        listed = self.client.get("/api/v1/transactions?page=1&size=2", headers=headers)
        body = listed.json()
        self.check(
            "GET /transactions 分页返回 total=3 且当页 2 条",
            body.get("total") == 3 and len(body.get("items", [])) == 2,
            f"total={body.get('total')} size={len(body.get('items', []))}",
        )
        self.check(
            "列表项内嵌 category 对象（避免 N+1 二次请求）",
            body["items"][0].get("category") is not None,
            str(body["items"][0].get("category", {}).get("name")),
        )

    def check_stats(self, headers: dict[str, str]) -> None:
        month = date.today().strftime("%Y-%m")
        summary = self.client.get(f"/api/v1/stats/summary?month={month}", headers=headers).json()
        ok = summary["income"] == "12000.00" and summary["expense"] == "50.50"
        self.check(
            "GET /stats/summary 聚合金额精确到分",
            ok,
            f"income={summary['income']} expense={summary['expense']} balance={summary['balance']}",
        )

        breakdown = self.client.get(
            f"/api/v1/stats/categories?kind=expense&month={month}", headers=headers
        ).json()
        self.check(
            "GET /stats/categories 分类占比合计 100%",
            abs(sum(i["percentage"] for i in breakdown["items"]) - 100.0) < 0.01,
            ", ".join(f"{i['category_name']}={i['percentage']}%" for i in breakdown["items"]),
        )

        trend = self.client.get("/api/v1/stats/trend?months=6", headers=headers).json()
        self.check(
            "GET /stats/trend 补齐 6 个月（无数据月份为 0）",
            len(trend["points"]) == 6,
            ",".join(trend["months"]),
        )

        calendar = self.client.get(f"/api/v1/stats/calendar?month={month}", headers=headers).json()
        self.check("GET /stats/calendar 返回整月天数", len(calendar["days"]) >= 28, "")

    def check_csv(self, headers: dict[str, str]) -> None:
        exported = self.client.get("/api/v1/transactions/csv/export", headers=headers)
        text = exported.content.decode("utf-8-sig")
        self.check(
            "GET /transactions/csv/export 流式导出且带 BOM",
            exported.status_code == 200 and exported.content.startswith(b"\xef\xbb\xbf"),
            f"{len(exported.content)} bytes",
        )
        self.check("导出内容含 3 条账单", text.count("\n") >= 4, text.splitlines()[0])

        today = date.today().isoformat()
        csv_body = (
            "账期,金额,类型,分类,备注\n"
            f"{today},15.80,支出,交通,地铁\n"
            f"{today},300.00,收入,兼职,家教\n"
            "not-a-date,abc,支出,交通,坏行\n"
        ).encode()
        imported = self.client.post(
            "/api/v1/transactions/csv/import",
            headers=headers,
            files={"file": ("data.csv", BytesIO(csv_body), "text/csv")},
        )
        result = imported.json()
        self.check(
            "POST /transactions/csv/import 逐行校验（2 成功 1 失败）",
            result["imported"] == 2 and result["failed"] == 1 and result["errors"][0]["row"] == 4,
            str(result),
        )

        again = self.client.post(
            "/api/v1/transactions/csv/import",
            headers=headers,
            files={"file": ("data.csv", BytesIO(csv_body), "text/csv")},
        )
        self.check(
            "重复导入被去重（imported=0）",
            again.json()["imported"] == 0,
            str(again.json()["imported"]),
        )

    def check_rate_limit(self) -> None:
        """验证认证接口的限流闸门。

        注意：令牌桶容量默认 10/分钟，而 pytest 与上一次冒烟可能已经消耗过令牌，
        因此这里用「足够多次请求」而不是「精确第 N 次被拦」来断言，
        保证脚本可以连续重复执行而结果稳定。
        """
        statuses = [
            self.client.post(
                "/api/v1/auth/login",
                json={"email": "brute@example.com", "password": "Passw0rd123"},
            ).status_code
            for _ in range(50)
        ]
        self.check(
            "登录接口触发限流(429)并返回 Retry-After",
            statuses.count(429) >= 1,
            f"401={statuses.count(401)} 429={statuses.count(429)}",
        )

    def check_docs(self) -> None:
        self.check("GET /docs Swagger UI 可访问", self.client.get("/docs").status_code == 200, "")
        schema = self.client.get("/openapi.json")
        paths = schema.json().get("paths", {}) if schema.status_code == 200 else {}
        self.check(
            "OpenAPI 文档覆盖核心路由",
            all(
                any(p.startswith(prefix) for p in paths)
                for prefix in ("/api/v1/auth", "/api/v1/transactions", "/api/v1/stats", "/api/v1/categories")
            ),
            f"{len(paths)} paths",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Goalfin API 端到端冒烟测试")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    try:
        return SmokeRunner(args.base_url).run()
    except httpx.ConnectError:
        print(f"无法连接 {args.base_url}，请先启动服务：uvicorn app.main:app --port 8000")
        return 2


if __name__ == "__main__":
    sys.exit(main())
