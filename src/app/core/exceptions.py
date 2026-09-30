"""分层异常体系 + 统一错误响应。

业务层只抛 AppError 子类（不依赖 HTTP），由全局处理器统一转成
`{code, message, detail?, request_id?}`；5xx 只回 request_id，不回堆栈。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


class AppError(Exception):
    """所有业务异常的基类。

    职责：把「业务上出了什么事」表达成 code + message + http 状态码，而不关心怎么变成 HTTP。
    生命周期：由 Service / Repository 层在发现业务规则被违反时创建（通常存活几毫秒），
    向上冒泡到 ExceptionMiddleware 被 `_app_error_handler` 消费后即销毁；不落库、不缓存。
    为什么独立成类（而不是到处 `raise HTTPException`）：
    ① 业务层不依赖 Web 框架，Service 代码可以脱离 FastAPI 单测；
    ② code 是固定枚举值，前端可以按 code 做逻辑分支（而不是去匹配中文文案）；
    ③ 状态码与文案集中定义，避免同一语义在各处写法不一。
    """

    # 类属性即默认值：子类只改需要改的那几项，其余继承父类。
    code: str = "INTERNAL_ERROR"
    message: str = "服务内部错误"
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    detail: dict[str, Any] | None = None

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: dict[str, Any] | None = None,
        code: str | None = None,
    ) -> None:
        """构造异常。

        参数：`message` 覆盖默认文案（不传就用类属性上的默认值）；
        `detail` 是给前端/排障用的结构化补充（如 `{"field": "amount"}`）；
        `code` 用于「同一类错误需要不同业务码」的场景（如复用 ConflictError 时换 code）。

        注意 `message or self.message`：传空字符串会回落到默认文案——
        这是有意的，避免前端拿到空 message 只能显示「请求失败」。
        """
        self.message = message or self.message
        self.detail = detail
        if code:
            # 只在显式传入时覆盖实例属性，避免把 None 写进去污染类属性语义
            self.code = code
        super().__init__(self.message)  # 让 Exception.args/str() 也是可读文案，便于日志与调试

    def to_payload(self, request_id: str | None = None) -> dict[str, Any]:
        """转成给前端的响应体：`{code, message, detail?, request_id?}`。

        可选键在为空时**不出现**（而不是给 null），前端 `record.code === undefined` 之类的
        判断才不会被 null 干扰；request_id 有值时才带上，便于用户复制到报障信息里。
        """
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail:
            # 空 dict 也不下发：`detail: {}` 对前端没有信息量，少一个键少一处歧义
            payload["detail"] = self.detail
        if request_id:
            payload["request_id"] = request_id
        return payload


# --------------------------- 4xx：客户端错误 ---------------------------
# 每个子类 = 一个稳定的 (code, message, status_code) 三元组，
# 前端按 code 分支、Swagger 上按 message 自解释，新增错误只要 3 行。
class BadRequestError(AppError):
    """400：请求本身合法但业务上不可执行（Pydantic 拦不住的规则）。"""

    code = "BAD_REQUEST"
    message = "请求参数不合法"
    status_code = status.HTTP_400_BAD_REQUEST


class UnauthorizedError(AppError):
    """401：没带凭证、凭证无效或已被禁用。前端收到它会清空本地会话。"""

    code = "UNAUTHORIZED"
    message = "未认证或凭证已失效"
    status_code = status.HTTP_401_UNAUTHORIZED


class ForbiddenError(AppError):
    """403：身份有效但权限不足（本项目越权统一返回 404，见 README 第 7 条）。"""

    code = "FORBIDDEN"
    message = "无权访问该资源"
    status_code = status.HTTP_403_FORBIDDEN


class NotFoundError(AppError):
    """404：资源不存在**或不属于当前用户**——两者故意合并，避免枚举他人 id。"""

    code = "NOT_FOUND"
    message = "资源不存在"
    status_code = status.HTTP_404_NOT_FOUND


class ConflictError(AppError):
    """409：与现有数据冲突（唯一约束、被引用而不可删除等）。"""

    code = "CONFLICT"
    message = "资源冲突"
    status_code = status.HTTP_409_CONFLICT


class RateLimitedError(AppError):
    """429：超过限流阈值。detail 里带 retry_after，前端可据此提示「请 N 秒后重试」。"""

    code = "RATE_LIMITED"
    message = "请求过于频繁，请稍后再试"
    status_code = status.HTTP_429_TOO_MANY_REQUESTS


# --------------------------- 领域专用错误 ---------------------------
# 领域子类只换 code/message，继承父类的状态码：语义更细，但 HTTP 语义与父类一致。
class EmailAlreadyExistsError(ConflictError):
    """409：注册邮箱重复。前端把它映射到邮箱输入框的错误提示。"""

    code = "EMAIL_ALREADY_EXISTS"
    message = "该邮箱已被注册"


class InvalidCredentialsError(UnauthorizedError):
    """401：「邮箱不存在」与「密码错误」共用同一个错误，防止账号枚举。"""

    code = "INVALID_CREDENTIALS"
    message = "邮箱或密码错误"


class TokenExpiredError(UnauthorizedError):
    """401：令牌过期。与 InvalidTokenError 分开，前端才知道「可以拿 refresh 续期」。"""

    code = "TOKEN_EXPIRED"
    message = "登录状态已过期，请重新登录"


class InvalidTokenError(UnauthorizedError):
    """401：签名/格式/用途不对（含 access 当 refresh 用）。前端应直接登出。"""

    code = "INVALID_TOKEN"
    message = "凭证无效"


class CategoryNameConflictError(ConflictError):
    """409：同一用户下分类重名（对应 uq 唯一索引的数据库兜底）。"""

    code = "CATEGORY_NAME_CONFLICT"
    message = "同名分类已存在"


class CategoryInUseError(ConflictError):
    """409：分类下还有账单，禁止删除（数据完整性保护，对应 FK 的 RESTRICT）。"""

    code = "CATEGORY_IN_USE"
    message = "该分类下仍有账单，无法删除"


class InvalidDateRangeError(BadRequestError):
    """400：统计/筛选的时间区间反了（end < start），无法构成有效窗口。"""

    code = "INVALID_DATE_RANGE"
    message = "开始日期不能晚于结束日期"


class CsvFormatError(BadRequestError):
    """400：上传的 CSV 表头/编码/行格式不合法，detail 里给出行号与原因。"""

    code = "CSV_FORMAT_ERROR"
    message = "CSV 文件格式不正确"


# ---------------------------------------------------------------------------
# 注册到 FastAPI
# ---------------------------------------------------------------------------
def _request_id(request: Request) -> str | None:
    """从 request.state 取 request_id（由 RequestContextMiddleware 写入）。

    用 getattr 而不是 `request.state.request_id`：中间件在极早期失败时 state 上可能还没有
    这个属性，直接取属性会再抛一个 AttributeError，把原始异常盖掉。
    """
    return getattr(request.state, "request_id", None)


def register_exception_handlers(app: FastAPI) -> None:
    """统一异常出口：任何异常最终都变成同构 JSON。

    在 main.py 的 `create_app()` 里调用一次（放在 include_router 之前，顺序无关，
    但集中在一处注册便于审阅「到底有哪些错误出口」）。

    设计选择：处理器用**闭包**定义在函数内部，而不是模块级函数。
    好处是它们只在注册时存在、不污染模块命名空间；代价是无法被直接 import 单测
    （要测就通过 ASGI 客户端打接口，tests 里正是这么做的）。
    """

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        """所有业务异常的出口：状态码与 code 全部取自异常对象自身。"""
        rid = _request_id(request)
        log = logger.bind(code=exc.code, path=request.url.path, request_id=rid)
        if exc.status_code >= 500:
            # 5xx 说明是「我们的错」，用 error 级别（监控告警看这一级）；
            # 4xx 是调用方的问题，用 info，避免把正常的表单校验刷进告警里。
            log.error("app_error", message=exc.message, detail=exc.detail)
        else:
            log.info("app_error", message=exc.message, detail=exc.detail)
        # 响应头也回传 request_id：前端/网关不解析 body 也能拿到链路 id
        headers = {REQUEST_ID_HEADER: rid} if rid else None
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload(rid), headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Pydantic 入参校验失败（422）：把 loc/msg/type 压平成前端可直接渲染的字段列表。"""
        # 把 Pydantic 的 loc/msg/type 压平、裁剪成前端真正需要的三个字段，
        # 顺带把原始结构里可能出现的类对象、异常对象挡在序列化之外。
        rid = _request_id(request)
        # 只保留可序列化的字段，避免 ctx 中出现异常对象导致序列化失败
        errors = [
            {
                # loc 形如 ("body", "password")：去掉 "body" 这层噪音，前端直接按字段名高亮输入框；
                # path/query 参数则保留（如 "page"），用于提示「第几页参数不对」。
                "field": ".".join(str(p) for p in err.get("loc", ()) if p != "body"),
                "message": err.get("msg", ""),
                "type": err.get("type", ""),  # 机器可判别的原因，如 value_error / missing
            }
            for err in exc.errors()
        ]
        logger.info("validation_error", path=request.url.path, errors=errors, request_id=rid)
        headers = {REQUEST_ID_HEADER: rid} if rid else None
        return JSONResponse(
            status_code=422,  # HTTP_422_UNPROCESSABLE_ENTITY 常量在新版 starlette 中已更名
            content={
                # 注意这里形状与 AppError 不同：detail 是 {"errors": [...]} 而不是业务 detail。
                # 前端 client.ts 先读 message、再读 detail.errors，因此键名改动属于契约变更。
                "code": "VALIDATION_ERROR",
                "message": "请求参数校验失败",
                "detail": {"errors": errors},
                **({"request_id": rid} if rid else {}),
            },
            headers=headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """框架自身抛出的 HTTP 异常（404/405/401 等）：补上 `HTTP_<状态码>` 形式的 code。"""
        # 框架自己抛的异常（无匹配路由 404、方法不允许 405、HTTPBearer 的 401 等）也要
        # 收敛成同构 JSON。fastapi.HTTPException 是它的子类，Starlette 按 MRO 查处理器，
        # 所以注册父类即可同时覆盖两者。
        rid = _request_id(request)
        headers = {REQUEST_ID_HEADER: rid} if rid else None
        if exc.status_code == status.HTTP_401_UNAUTHORIZED and headers:
            # RFC 6750：401 应带 WWW-Authenticate 告知客户端用哪种认证方案。
            # 这里的 `and headers` 等价于「有 request_id 才加」——正常请求都经过
            # RequestContextMiddleware，所以线上恒定成立；脱离中间件的调用则退化为不带该头。
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse(
            status_code=exc.status_code,
            content={
                # code 由状态码推导（HTTP_404 / HTTP_405），保证前端总能拿到非空 code，
                # 不会因为某条路径没被显式建模而出现 `undefined`。
                "code": f"HTTP_{exc.status_code}",
                "message": str(exc.detail),
                **({"request_id": rid} if rid else {}),
            },
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        """最后一道兜底：未知异常只回固定文案 + request_id，细节全部进日志。"""
        rid = _request_id(request)
        logger.exception("unhandled_error", path=request.url.path, request_id=rid, error=str(exc))
        headers = {REQUEST_ID_HEADER: rid} if rid else None
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "code": "INTERNAL_ERROR",
                "message": "服务内部错误",
                **({"request_id": rid} if rid else {}),
            },
            headers=headers,
        )
