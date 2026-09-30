"""回归测试：限流短路返回的 429 必须带 CORS 头。

为什么单独守这一条（真实缺陷的回归防线）：
Starlette 的 `add_middleware` 是 `insert(0, ...)`，**最后 add 的在最外层**。
早期 `main.py` 把 CORS 放在最内层（最先 add），而限流中间件是**直接 return** 一个
自己构造的 JSONResponse 的——不经过异常处理器、也不经过内层中间件，于是那个 429
响应里**没有** `Access-Control-Allow-Origin`。跨域前端在浏览器里只会看到 "CORS error"，
既读不到 `RATE_LIMITED` 也读不到 `Retry-After`，等于把限流提示彻底废掉。

这类缺陷的隐蔽之处在于：**同源请求（curl、pytest 默认）完全看不出来**，
只有浏览器跨域时才暴露。所以必须用一条显式带 `Origin` 的用例把它钉住。

修复方式是把 CORS 移到最后一个 `add_middleware`（= 最外层），
让任何中间件短路出来的响应都经过它；本用例防止以后有人"顺手调换顺序"再退回去。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.exceptions import InvalidCredentialsError

ORIGIN = "http://localhost:5173"


@pytest.fixture
async def cors_limited_client(monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    """收紧限流阈值 + 放行一个明确的跨域来源，用于观察 429 的响应头。"""
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_auth_per_minute", 3)
    monkeypatch.setattr(settings, "rate_limit_default_per_minute", 5)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 60)
    # 显式把测试用的来源加进白名单：CORSMiddleware 只对白名单内的 Origin 回跨域头
    monkeypatch.setattr(settings, "cors_origins", [ORIGIN])

    async def _always_invalid(self: object, email: str, password: str) -> None:
        raise InvalidCredentialsError()

    monkeypatch.setattr("app.services.auth_service.AuthService.authenticate", _always_invalid)

    from app.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def test_rate_limited_response_carries_cors_headers(cors_limited_client: AsyncClient) -> None:
    """被限流拒绝时，响应仍要能被跨域前端读到（429 + Retry-After + CORS 头）。"""
    blocked = None
    for _ in range(6):
        resp = await cors_limited_client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "Passw0rd123"},
            headers={"Origin": ORIGIN},
        )
        if resp.status_code == 429:
            blocked = resp
            break

    assert blocked is not None, "阈值 3 的情况下第 4 次起应被限流"
    # 限流语义本身
    assert blocked.json()["code"] == "RATE_LIMITED"
    assert int(blocked.headers["Retry-After"]) >= 1
    # 关键断言：跨域头必须存在，否则浏览器不会把这个响应交给前端代码
    assert blocked.headers["Access-Control-Allow-Origin"] == ORIGIN
    assert "Access-Control-Allow-Credentials" in blocked.headers


async def test_normal_response_still_carries_cors_headers(cors_limited_client: AsyncClient) -> None:
    """正常响应与 401 也要带跨域头：限流以内/以外的路径都不能漏。"""
    ok = await cors_limited_client.get("/health", headers={"Origin": ORIGIN})
    assert ok.status_code == 200
    assert ok.headers["Access-Control-Allow-Origin"] == ORIGIN

    unauthorized = await cors_limited_client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "Passw0rd123"},
        headers={"Origin": ORIGIN},
    )
    assert unauthorized.status_code == 401
    assert unauthorized.headers["Access-Control-Allow-Origin"] == ORIGIN
