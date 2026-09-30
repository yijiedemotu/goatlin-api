"""并发压测脚本：用纯 Python + asyncio 给关键接口跑基准，输出 QPS 与延迟分位。

为什么自己写而不是上 Locust/wrk：
1. 只有 httpx 一个依赖，`pip install -e ".[dev]"` 后立刻能跑，不需要额外环境；
2. 能直接复用项目自身的鉴权与接口语义（注册 → 登录 → 带 token 打业务接口），
   而不是让使用者手写脚本填 token；
3. 报告里的分位数计算方式与线上 `/stats` 一致，数字能对得上。

用法：
    # 终端 1：用关闭限流的配置启动服务（否则会被 429 拦住，测不出真实吞吐）
    set RATE_LIMIT_ENABLED=false
    python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

    # 终端 2：
    python scripts/load_test.py --base-url http://127.0.0.1:8000 --seconds 8 --concurrency 32 --raw

阅读结果的三个前提（很重要，别把数字讲错）：
1. 压测客户端与服务端在同一台机器上争抢 CPU，httpx 的数据偏保守，
   脚本里的 `--raw`（裸 socket）那一组更接近服务端的真实上限；
2. 默认数据库是 SQLite（单写者模型），写入吞吐不代表 PostgreSQL 表现；
3. 结果取决于机器、Python 版本与事件循环，报告里必须写清环境，否则数字没有意义。
"""

from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import date

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


@dataclass
class BenchResult:
    name: str
    concurrency: int
    total_requests: int = 0
    errors: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    status_codes: dict[int, int] = field(default_factory=dict)
    elapsed_s: float = 0.0

    @property
    def qps(self) -> float:
        return self.total_requests / self.elapsed_s if self.elapsed_s else 0.0

    @property
    def error_rate(self) -> float:
        return self.errors / self.total_requests * 100 if self.total_requests else 0.0

    def percentile(self, percent: float) -> float:
        """线性插值分位数，算法与 app/observability 中的实现保持一致。"""
        if not self.latencies_ms:
            return 0.0
        values = sorted(self.latencies_ms)
        if len(values) == 1:
            return values[0]
        position = (len(values) - 1) * percent / 100
        lower = int(position)
        upper = min(lower + 1, len(values) - 1)
        weight = position - lower
        return values[lower] * (1 - weight) + values[upper] * weight

    def row(self) -> str:
        return (
            f"{self.name:<30}{self.concurrency:>5}{self.total_requests:>9}{self.qps:>9.1f}"
            f"{self.percentile(50):>10.2f}{self.percentile(95):>10.2f}{self.percentile(99):>10.2f}"
            f"{self.error_rate:>8.2f}%"
        )


class LoadRunner:
    def __init__(self, base_url: str, seconds: float, concurrency: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.seconds = seconds
        self.concurrency = concurrency
        self.token = ""
        self.category_id: int | None = None

    # ------------------------------------------------------------------
    async def prepare(self, client: httpx.AsyncClient) -> None:
        """注册一个压测专用账号并登录，拿到 token 与一个可用分类。"""
        email = f"bench{random.randint(10**6, 10**7)}@example.com"
        password = "Benchmark123"

        resp = await client.post("/api/v1/auth/register", json={"email": email, "password": password})
        if resp.status_code != 201:
            raise SystemExit(f"注册压测账号失败: {resp.status_code} {resp.text[:200]}")

        resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
        if resp.status_code != 200:
            raise SystemExit(f"登录失败: {resp.status_code} {resp.text[:200]}")
        self.token = resp.json()["access_token"]

        headers = {"Authorization": f"Bearer {self.token}"}
        categories = (await client.get("/api/v1/categories", headers=headers)).json()
        expense = next((c for c in categories if c["kind"] == "expense"), None)
        self.category_id = expense["id"] if expense else None

    async def seed_expenses(self, client: httpx.AsyncClient, count: int) -> None:
        """写入基准数据：统计接口要扫到真实数据量才有意义。"""
        headers = {"Authorization": f"Bearer {self.token}"}
        today = date.today().isoformat()
        payloads = [
            {
                "amount": f"{random.randint(1, 500)}.{random.randint(10, 99)}",
                "kind": "expense",
                "category_id": self.category_id,
                "occurred_on": today,
                "note": f"压测数据 {index}",
            }
            for index in range(count)
        ]
        for start in range(0, len(payloads), 50):
            batch = payloads[start : start + 50]
            await asyncio.gather(
                *(client.post("/api/v1/transactions", json=payload, headers=headers) for payload in batch)
            )
        print(f"[prepare] 已写入 {count} 条账单用于压测\n")

    # ------------------------------------------------------------------
    async def _worker(
        self,
        client: httpx.AsyncClient,
        result: BenchResult,
        request_factory: object,
        deadline: float,
    ) -> None:
        """一个 worker 循环发请求直到到达截止时间。"""
        assert callable(request_factory)
        while time.perf_counter() < deadline:
            started = time.perf_counter()
            try:
                response = await request_factory(client)
                status = response.status_code
                result.status_codes[status] = result.status_codes.get(status, 0) + 1
                if status >= 400:
                    result.errors += 1
            except Exception:
                result.errors += 1
            finally:
                result.total_requests += 1
                result.latencies_ms.append((time.perf_counter() - started) * 1000)

    async def run_case(
        self,
        name: str,
        request_factory: object,
        *,
        concurrency: int | None = None,
    ) -> BenchResult:
        workers = concurrency or self.concurrency
        result = BenchResult(name=name, concurrency=workers)
        limits = httpx.Limits(max_connections=workers * 2, max_keepalive_connections=workers * 2)
        timeout = httpx.Timeout(30.0)

        async with httpx.AsyncClient(base_url=self.base_url, limits=limits, timeout=timeout) as client:
            deadline = time.perf_counter() + self.seconds
            started = time.perf_counter()
            await asyncio.gather(
                *(self._worker(client, result, request_factory, deadline) for _ in range(workers))
            )
            result.elapsed_s = time.perf_counter() - started
        return result


class RawSocketRunner:
    """裸 socket + keep-alive 压测：用来测出「服务端真实上限」。

    为什么需要它：httpx 每个请求要构造 Request/URL/Headers 对象、跑一遍传输层，
    在「同机压测」时客户端自己就会吃掉大量 CPU，测出来更像客户端的上限。
    裸 socket 只做 write + 读响应，能更接近服务端的真实能力。

    局限：只能压无鉴权的简单 GET（要自己拼 HTTP 报文），所以只用它测 /health/live。
    """

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    async def run(self, path: str, *, seconds: float, concurrency: int) -> BenchResult:
        request = (f"GET {path} HTTP/1.1\r\nHost: {self.host}\r\nConnection: keep-alive\r\n\r\n").encode()
        result = BenchResult(name=f"GET  {path}(裸 socket)", concurrency=concurrency)

        async def worker(deadline: float) -> None:
            reader, writer = await asyncio.open_connection(self.host, self.port)
            try:
                while time.perf_counter() < deadline:
                    started = time.perf_counter()
                    try:
                        writer.write(request)
                        await writer.drain()
                        head = await reader.readuntil(b"\r\n\r\n")
                        length = 0
                        for line in head.split(b"\r\n"):
                            if line.lower().startswith(b"content-length:"):
                                length = int(line.split(b":", 1)[1])
                        if length:
                            await reader.readexactly(length)
                        result.status_codes[200] = result.status_codes.get(200, 0) + 1
                    except Exception:
                        result.errors += 1
                    finally:
                        result.total_requests += 1
                        result.latencies_ms.append((time.perf_counter() - started) * 1000)
            finally:
                writer.close()

        deadline = time.perf_counter() + seconds
        started = time.perf_counter()
        await asyncio.gather(*(worker(deadline) for _ in range(concurrency)))
        result.elapsed_s = time.perf_counter() - started
        return result


# ---------------------------------------------------------------------------
# 各接口的请求构造
# ---------------------------------------------------------------------------
def make_factories(runner: LoadRunner) -> dict[str, object]:
    headers = {"Authorization": f"Bearer {runner.token}"}
    today = date.today().isoformat()

    async def health(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get("/health/live")

    async def list_page(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get("/api/v1/transactions?page=1&size=20", headers=headers)

    async def list_filtered(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get(
            "/api/v1/transactions"
            f"?start_date={today}&end_date={today}&kind=expense&min_amount=10&page=1&size=20",
            headers=headers,
        )

    async def stats_summary(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get("/api/v1/stats/summary", headers=headers)

    async def stats_categories(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get("/api/v1/stats/categories?kind=expense", headers=headers)

    async def stats_trend(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get("/api/v1/stats/trend?months=6", headers=headers)

    async def create_tx(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            "/api/v1/transactions",
            headers=headers,
            json={
                "amount": f"{random.randint(1, 999)}.{random.randint(10, 99)}",
                "kind": "expense",
                "category_id": runner.category_id,
                "occurred_on": today,
                "note": "压测写入",
            },
        )

    async def login(client: httpx.AsyncClient) -> httpx.Response:
        # bcrypt 校验是 CPU 密集操作，这条用来量化"认证接口为什么必须限流"
        return await client.post(
            "/api/v1/auth/login",
            json={"email": "nonexistent-bench@example.com", "password": "Benchmark123"},
        )

    return {
        "GET  /health/live(无鉴权)": health,
        "GET  /transactions(分页)": list_page,
        "GET  /transactions(多条件过滤)": list_filtered,
        "GET  /stats/summary": stats_summary,
        "GET  /stats/categories": stats_categories,
        "GET  /stats/trend": stats_trend,
        "POST /transactions(写入)": create_tx,
        "POST /auth/login(bcrypt)": login,
    }


async def main_async(args: argparse.Namespace) -> int:
    runner = LoadRunner(args.base_url, args.seconds, args.concurrency)

    async with httpx.AsyncClient(base_url=runner.base_url, timeout=httpx.Timeout(60.0)) as client:
        try:
            health = await client.get("/health")
        except httpx.ConnectError:
            print(f"无法连接 {runner.base_url}，请先启动服务：uvicorn app.main:app --port 8000")
            return 2
        print(f"== 目标服务 {runner.base_url} ==  health={health.json()}\n")
        await runner.prepare(client)
        if args.seed:
            await runner.seed_expenses(client, args.seed)

    factories = make_factories(runner)
    if args.only:
        wanted = [k for k in factories if any(token in k for token in args.only)]
        factories = {k: factories[k] for k in wanted}
        if not factories:
            print(f"没有匹配的接口：{args.only}")
            return 2

    header = (
        f"{'接口':<30}{'并发':>5}{'请求数':>9}{'QPS':>9}"
        f"{'P50(ms)':>10}{'P95(ms)':>10}{'P99(ms)':>10}{'错误率':>9}"
    )
    print(header)
    print("-" * len(header))

    results: list[BenchResult] = []
    for name, factory in factories.items():
        # 写入类接口用较低并发：SQLite 是单写者模型，高并发下会大量锁等待，
        # 用一个更贴近真实写入压力的并发数，避免得出误导性的低 QPS。
        concurrency = max(4, args.concurrency // 4) if name.startswith("POST") else args.concurrency
        result = await runner.run_case(name, factory, concurrency=concurrency)
        results.append(result)
        print(result.row())

    print("-" * len(header))
    read_results = [r for r in results if not r.name.startswith("POST")]
    if read_results:
        best = max(read_results, key=lambda r: r.qps)
        print(f"\n读接口最高吞吐：{best.name} {best.qps:.1f} QPS（并发 {best.concurrency}）")
        all_p95 = [r.percentile(95) for r in read_results]
        print(f"读接口 P95 中位数：{statistics.median(all_p95):.2f} ms")

    # 补一组裸 socket 数据：httpx 在同机压测时客户端本身就是瓶颈，
    # 这一组才能反映服务端的真实上限（见 RawSocketRunner 的说明）。
    if args.raw:
        host = runner.base_url.split("//", 1)[-1]
        hostname, _, port_text = host.partition(":")
        raw_runner = RawSocketRunner(hostname, int(port_text or 80))
        print("\n== 裸 socket 对照（排除客户端开销，反映服务端上限）==")
        for concurrency in (16, 64):
            raw = await raw_runner.run("/health/live", seconds=args.seconds, concurrency=concurrency)
            print(raw.row())

    print("\n说明：")
    print("  1) 上面的 httpx 数据中，压测客户端与服务端争夺同一个 CPU，读数偏保守；")
    print("  2) SQLite 是单文件、单写者模型，写入吞吐不代表 PostgreSQL 表现；")
    print("     要压 PostgreSQL，请用 docker compose 起 PG 后把 DATABASE_URL 指过去再跑本脚本；")
    print("  3) 压测前请关闭限流（RATE_LIMIT_ENABLED=false），否则会被 429 拦住。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Goalfin API 并发压测")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--seconds", type=float, default=8.0, help="每个接口的压测时长")
    parser.add_argument("--concurrency", type=int, default=32, help="读接口并发数")
    parser.add_argument("--seed", type=int, default=500, help="压测前写入的账单条数")
    parser.add_argument(
        "--raw",
        action="store_true",
        help="额外跑一组裸 socket 对照（排除 httpx 客户端开销，反映服务端上限）",
    )
    parser.add_argument(
        "--only",
        nargs="*",
        default=None,
        help="只压匹配的接口，例如 --only stats summary",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
