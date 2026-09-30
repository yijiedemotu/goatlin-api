"""FastAPI 应用装配入口：中间件、异常处理器、路由与运维探针。

注意中间件顺序：Starlette 的 `add_middleware` 是 `insert(0, ...)`，
**最后 add 的在最外层**。因此 CORS 必须最后 add——它要给限流短路出来的 429
也补上跨域头，否则浏览器只会报 "CORS error"，前端读不到 Retry-After。
（详见 docs/LEARN.md「中间件洋葱模型」）"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import api_router
from app.core.config import BASE_DIR, settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.db.session import check_database, dispose_engine
from app.middleware.context import RequestContextMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.schemas.common import HealthResponse

if sys.platform == "win32":
    # Windows 默认 ProactorEventLoop 在 ASGI 下慢得多（实测约 1/3 吞吐）；
    # 代价是不支持 asyncio.create_subprocess_*，本项目不使用子进程。
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logger = get_logger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动/关闭钩子：把「初始化」和「资源释放」显式化。

    参数：`app` 由 FastAPI 注入（本函数没用它，但签名要符合 lifespan 协议）。
    生命周期：`yield` 之前的代码在**开始接收请求之前**执行一次，
    `yield` 之后的代码在**进程关闭时**执行一次（uvicorn 收到 SIGTERM 后）。

    为什么用 `@asynccontextmanager` 而不是已废弃的 `@app.on_event("startup")`：
    启动与关闭写在同一个函数里，资源「谁申请、谁释放」一目了然，
    也不用把引擎、连接之类的对象挂到 app.state 上传递。
    代价是这个函数必须是 async（即使里面没有 await 的启动逻辑）。

    关键选择：数据库探活失败**只记日志，不抛异常**。
    抛异常会让进程起不来 → K8s 反复重启（CrashLoopBackOff），而依赖故障重启是治不好的；
    现在的行为是「进程活着、就绪探针返回 degraded → 摘流量」，等数据库恢复即可自动回来。
    """
    configure_logging()  # 必须早于任何一次日志调用（structlog 首次使用即固化配置）
    logger.info(
        "application_starting",
        version=__version__,
        env=settings.app_env,
        database="sqlite" if settings.is_sqlite else "postgres",
    )
    if not await check_database():
        # 只告警不中断：让 /health 用 degraded 回答，交给编排系统决定是否摘流量
        logger.error("database_check_failed")
    yield
    # yield 之后 = 优雅关闭：先停止接收新请求（uvicorn 负责），再释放连接池
    await dispose_engine()
    logger.info("application_stopped")


def create_app() -> FastAPI:
    """装配并返回 FastAPI 应用（应用工厂）。

    返回：完整的 ASGI app——中间件、异常处理器、路由、探针都已就位。

    为什么用工厂函数而不是只写一个模块级 `app = FastAPI(...)`：
    ① 测试可以 `create_app()` 拿到**全新实例**，中间件内部状态（如限流的令牌桶）互不干扰，
       tests/test_rate_limit.py 正是靠这一点给每个用例一套干净的计数器；
    ② 未来要按环境组装不同中间件（如只在生产挂反向代理相关中间件）时，有天然的插入点。
    代价是模块底部仍然要 `app = create_app()` 供 uvicorn 引用，存在两个入口（工厂 + 单例），
    改装配逻辑时要知道线上跑的是哪一个。
    """

    app = FastAPI(
        title="Goalfin · 个人记账与财务分析平台",
        description=(
            "多用户记账后端 API：JWT 双令牌鉴权、分类与账单管理、"
            "SQL 聚合统计、CSV 导入导出、限流与结构化日志。\n\n"
            "**错误响应统一结构**：`{code, message, detail?, request_id?}`"
        ),
        version=__version__,  # 与 app/__init__.py 的 __version__ 同源，避免两处版本漂移
        lifespan=lifespan,
        # /docs（Swagger UI）与 /redoc 是前端联调入口，/openapi.json 供前端生成类型或做契约测试
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        contact={"name": "Goalfin API", "url": "https://github.com/your-name/goalfin-api"},
    )

    # 洋葱模型：**最后 add 的在最外层**（add_middleware 内部是 insert(0, ...)）。
    # 调用顺序读作「限流 → 请求上下文 → CORS」，实际执行顺序（由外到内）是
    # CORS → RequestContext → RateLimit → 路由。三者位置都是实测过的，别随手调换：
    # ① CORS 最外：限流短路出来的 429 也要带跨域头，否则浏览器只报 "CORS error"；
    # ② RequestContext 次外：429 同样要带 X-Request-ID 并留下访问日志（见 test_rate_limit.py）；
    # ③ RateLimit 在其内：在解析业务、碰数据库之前尽早短路。
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        # 必须逐个列出允许的来源：allow_credentials=True 与 allow_origins=["*"] 无法共存
        # （浏览器禁止带凭证的通配符跨域），列白名单是唯一正确的做法。
        allow_origins=settings.cors_origins,
        allow_credentials=True,  # 允许跨域携带凭证（本项目用 Authorization 头，属于凭证）
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],  # 白名单而非 "*"
        # 允许的请求头：Authorization（鉴权）+ Content-Type（JSON/表单）+ X-Request-ID（透传链路 id）
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        # 暴露给 JS 的头：默认跨域下 JS 读不到自定义响应头，不 expose 就拿不到这两个值
        expose_headers=["X-Request-ID", "X-Process-Time-Ms"],
        max_age=600,  # 预检请求缓存 10 分钟，减少 OPTIONS 往返
    )

    register_exception_handlers(app)
    # prefix 只在挂载点给一次：各路由文件里的路径都是相对前缀写的，
    # 将来出 /api/v2 只要再 include 一次另一个 router 树。
    app.include_router(api_router, prefix=settings.api_prefix)

    # ------------------------------------------------------------------
    # 运维探针：存活 / 就绪分离，K8s 用 liveness 重启、readiness 摘流量
    # ------------------------------------------------------------------
    @app.get("/health", response_model=HealthResponse, tags=["运维"], summary="健康检查（含数据库探测）")
    async def health() -> HealthResponse:
        """就绪探针：每次调用都真的探一次数据库。

        为什么每次都探而不是缓存结果：探针的意义就是反映**此刻**能不能服务；
        代价是探针会给数据库带来极轻的负载（一条 SELECT 1），
        所以它被排除在访问日志和限流之外。
        """
        db_ok = await check_database()
        # status=degraded 而不是直接 500：HTTP 200 表示「进程还能响应」，
        # 由 body 里的状态决定是否摘流量——K8s 也可改用 503 语义，二者都对但要与编排配置一致。
        return HealthResponse(
            status="ok" if db_ok else "degraded",
            version=__version__,
            environment=settings.app_env,
            database="up" if db_ok else "down",
        )

    @app.get("/health/live", tags=["运维"], summary="存活探针")
    async def liveness() -> dict[str, str]:
        """存活探针：只回答「进程还在跑」。

        刻意不碰数据库、不碰 Redis、不碰磁盘——任何依赖抖动都不该让 K8s 重启这个进程。
        分工是：依赖出问题走 readiness（摘流量），进程真死了（此接口无响应）才走 liveness。
        """
        return {"status": "alive"}

    @app.get("/", tags=["运维"], summary="服务信息")
    async def root() -> dict[str, str]:
        """根路径信息页：给运维/联调确认「连上的是哪个服务、哪个版本、文档在哪」。"""
        return {
            "name": settings.app_name,
            "version": __version__,
            "docs": "/docs",
            "api_prefix": settings.api_prefix,
        }

    return app


# uvicorn 的入口（`uvicorn app.main:app`）需要模块级对象；测试则用 create_app() 新建实例。
app = create_app()


def run() -> None:
    """命令行 / IDE 直跑入口：`python -m app.main` 或直接运行 `src/app/main.py`。

    为什么必须单独有这个函数：本模块是**模块**而不是脚本，直接执行时它只会定义
    `app = create_app()` 然后正常退出——不报错、也没有任何日志，进程退出码是 0。
    PyCharm 里对着 main.py 点绿三角（自动生成的运行配置就是 `python src/app/main.py`）
    或手敲 `python src/app/main.py` 都会命中这条路径，表现就是「后端起不来」。
    这里补上真正的入口，并复用配置里的 APP_HOST / APP_PORT / APP_DEBUG。

    两个细节：
    - 传 `"app.main:app"` 这个 **import string** 而不是 `app` 对象：uvicorn 的 reload
      模式必须靠字符串在子进程里重新导入应用（传对象会直接报错）；
    - `reload_dirs` 显式指向 `src/`：工作目录可能是 `src/app`（IDE 默认），
      不指定就只监视那一个目录，改了别的文件不会热重载。
    """
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_debug,
        reload_dirs=[str(BASE_DIR / "src")] if settings.app_debug else None,
    )


if __name__ == "__main__":
    run()
