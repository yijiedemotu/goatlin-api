"""pytest 全局配置。

关键点 1：**必须在 import app 之前设置环境变量**。
因为 `app.core.config.settings` 是模块级单例，一旦导入就固定了，
所以这里用 os.environ 覆盖后再导入应用，保证测试与本地 .env 完全隔离。

关键点 2：**测试引擎必须是「每个用例新建」而不是模块级全局**。
SQLAlchemy 的异步连接对象绑定在创建它的事件循环上，而 pytest-asyncio 默认
每个用例一个事件循环；如果引擎在模块导入期创建（跨用例复用），就会出现
「表已创建却报 no such table」这类跨 loop 的诡异错误。
因此把 create_all/drop_all 与引擎创建放进同一个用例级 fixture。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

os.environ.update(
    {
        "APP_ENV": "local",
        "APP_DEBUG": "false",
        "SECRET_KEY": "test-secret-key-for-pytest-only-32bytes-minimum",
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "RATE_LIMIT_ENABLED": "false",
        "LOG_LEVEL": "WARNING",
        "LOG_FORMAT": "console",
    }
)

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.db.session import get_session
from app.main import app
from app.models.base import Base


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """内存 SQLite + StaticPool：同一池内共享同一个内存库，测试结束即销毁。

    StaticPool 让所有会话复用同一条 DBAPI 连接，这是「内存库在多个 session
    之间仍能看到彼此数据」的必要条件。
    """
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _override_db(engine: AsyncEngine) -> AsyncIterator[None]:
    """把应用的数据库依赖替换为测试引擎，用例结束后清理覆盖。"""

    async def _get_test_session() -> AsyncIterator[AsyncSession]:
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _get_test_session
    yield
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """异步 HTTP 客户端，直接打 ASGI app（不起真实端口，测试更快更稳）。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """直接操作数据库的会话，用于准备测试数据或断言落库结果。"""
    async with session_factory() as s:
        yield s


# ---------------------------------------------------------------------------
# 业务夹具
# ---------------------------------------------------------------------------
USER_PAYLOAD = {
    "email": "student@example.com",
    "password": "Passw0rd123",
    "nickname": "同学",
}


@pytest.fixture
async def registered_user(client: AsyncClient) -> dict:
    resp = await client.post("/api/v1/auth/register", json=USER_PAYLOAD)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
async def auth_headers(client: AsyncClient, registered_user: dict) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": USER_PAYLOAD["email"], "password": USER_PAYLOAD["password"]},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def categories(client: AsyncClient, auth_headers: dict[str, str]) -> dict[str, dict]:
    """返回 {分类名: 分类对象}，默认分类由注册流程预置。"""
    resp = await client.get("/api/v1/categories", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    return {item["name"]: item for item in resp.json()}
