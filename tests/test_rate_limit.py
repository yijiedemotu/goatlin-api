"""限流与请求上下文中间件集成测试。

为什么单独建一个 app：限流是中间件级行为，且默认配置下测试环境关闭限流
（否则大量用例会互相干扰）。这里用 `create_app()` 新建实例并临时收紧阈值，
只对「被限流」这条路径做端到端验证——正是漏测它才让 `await 同步方法`
这类 bug 溜到运行时。

认证服务的数据库访问通过 monkeypatch 屏蔽，测试只关心中间件行为，
不依赖任何表结构（这样也避免为限流测试额外准备数据）。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.core.exceptions import InvalidCredentialsError


@pytest.fixture
async def limited_client(monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_auth_per_minute", 3)
    monkeypatch.setattr(settings, "rate_limit_default_per_minute", 5)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 60)

    async def _always_invalid(self: object, email: str, password: str) -> None:
        raise InvalidCredentialsError()

    monkeypatch.setattr("app.services.auth_service.AuthService.authenticate", _always_invalid)

    from app.main import create_app

    # 每个用例新建 app：限流桶随实例创建，天然互不干扰
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest.mark.parametrize("attempts", [6])
async def test_auth_login_is_rate_limited(limited_client: AsyncClient, attempts: int) -> None:
    statuses = []
    blocked = None
    for _ in range(attempts):
        resp = await limited_client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "Passw0rd123"},
        )
        statuses.append(resp.status_code)
        if resp.status_code == 429:
            blocked = resp

    # 阈值 3：前 3 次进入业务层（账号不存在 → 401），之后被中间件拦截
    assert statuses[:3] == [401, 401, 401], statuses
    assert 429 in statuses, statuses

    assert blocked is not None
    body = blocked.json()
    assert body["code"] == "RATE_LIMITED"
    assert body["detail"]["retry_after"] >= 1
    assert int(blocked.headers["Retry-After"]) >= 1
    # 被限流拒绝的响应同样要带链路 id（RequestContext 必须排在 RateLimit 之外）。
    # 这条断言就是钉住中间件顺序的：把两者调换，429 的 X-Request-ID 与 body.request_id 会消失。
    assert blocked.headers["X-Request-ID"]
    assert body["request_id"] == blocked.headers["X-Request-ID"]


async def test_health_endpoint_is_exempt(limited_client: AsyncClient) -> None:
    """健康探针不能被限流，否则 K8s 会误判实例不可用并把流量摘掉。"""
    for _ in range(10):
        resp = await limited_client.get("/health")
        assert resp.status_code == 200


async def test_liveness_probe_is_exempt(limited_client: AsyncClient) -> None:
    """存活探针同样要豁免。

    豁免表是**精确匹配**，早期只写了 `/health`、漏了 `/health/live`：
    默认阈值 120/min 下探针活得过，但一旦调低阈值（或同 IP 有其他流量），
    liveness 会返回 429 —— K8s 判定进程不健康并重启，正好是「探针自己被限流」
    这一类最典型的误伤。这里用收紧后的默认档（5/min）连打 10 次来钉住它。
    """
    for _ in range(10):
        resp = await limited_client.get("/health/live")
        assert resp.status_code == 200


async def test_response_carries_request_id_and_timing(limited_client: AsyncClient) -> None:
    resp = await limited_client.get("/health")
    assert resp.headers["X-Request-ID"]
    assert float(resp.headers["X-Process-Time-Ms"]) >= 0


async def test_client_supplied_request_id_is_propagated(limited_client: AsyncClient) -> None:
    """上游网关的 request_id 必须透传，实现跨服务链路串联。"""
    resp = await limited_client.get("/health", headers={"X-Request-ID": "trace-me-123"})
    assert resp.headers["X-Request-ID"] == "trace-me-123"
