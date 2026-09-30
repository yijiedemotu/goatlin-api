"""请求上下文中间件：分配 request_id、记录耗时、输出访问日志。

request_id 通过 structlog contextvars 绑定，此后该请求的所有日志自动携带，
可用一个 id 串起「访问日志 → Service 日志 → 异常堆栈」。

顺序约束：必须排在 RateLimit **之外**（CORS → 本中间件 → RateLimit → 路由），
否则被限流短路的 429 拿不到 `X-Request-ID`、也不会有访问日志。"""

from __future__ import annotations

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.exceptions import REQUEST_ID_HEADER
from app.core.logging import get_logger

# logger 名固定为 "access"：访问日志与业务日志分开，便于在采集端做不同保留策略
logger = get_logger("access")

# 健康检查与文档这类高频噪音路径不打访问日志
# （探针可能每秒来一次，若全量记录，一天就能产生几十万条无用日志）
_SILENT_PATHS = {"/health", "/health/live", "/metrics", "/openapi.json"}


class RequestContextMiddleware(BaseHTTPMiddleware):
    """为每个请求建立可追踪上下文，并统计端到端耗时。

    生命周期：进程级单例（由 `add_middleware` 在应用构建时实例化一次），
    `dispatch` **每个请求执行一次**。

    为什么独立成中间件：request_id 与耗时统计要覆盖「路由 404」「参数 422」「限流 429」
    这些还没进入业务代码的路径，依赖（Depends）做不到这点——依赖只在路由匹配成功后运行。
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # 允许上游网关透传 request_id，实现跨服务串联（没有则本地生成）
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:16]
        # 先清空再绑定：防止当前任务上下文中残留上一个请求的字段
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        # request.state 是本次请求的「随身口袋」，异常处理器/限流中间件从这里取 request_id
        request.state.request_id = request_id

        # perf_counter 单调递增，测耗时不受系统校时影响
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # 异常路径也要有耗时与上下文：这里记录后重新抛出，
            # 让异常继续冒泡到全局处理器（那里负责生成统一 JSON 错误体），
            # 中间件不吞异常，否则全局错误处理与 5xx 不泄漏堆栈的约定都会失效。
            duration_ms = (time.perf_counter() - started) * 1000
            logger.exception("request_failed", duration_ms=round(duration_ms, 2))
            raise
        finally:
            # 无论成功/异常都解绑：contextvars 会被后续任务继承，必须及时清理
            structlog.contextvars.unbind_contextvars("request_id", "method", "path")

        duration_ms = (time.perf_counter() - started) * 1000
        # 回写响应头：前端（含错误提示里的 request_id）与网关据此关联日志。
        # 注意 CORS 必须在 main.py 里 expose_headers 声明这两个头，否则浏览器 JS 读不到。
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers["X-Process-Time-Ms"] = f"{duration_ms:.2f}"

        if request.url.path not in _SILENT_PATHS:
            # 4xx/5xx 用 warning 级别：便于在告警规则里按 level 过滤，而不必解析 status 字段
            log = logger.info if response.status_code < 400 else logger.warning
            log(
                "request",
                status=response.status_code,
                duration_ms=round(duration_ms, 2),
                # client 可能为 None（如 ASGI 测试传输），显式兜底避免日志里出现 "None"
                client=request.client.host if request.client else "-",
            )
        return response
