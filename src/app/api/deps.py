"""FastAPI 依赖注入工厂：数据库会话、当前用户、分页参数。

依赖可被 `app.dependency_overrides` 整体替换，测试里换成内存库非常轻量。"""

from __future__ import annotations

from typing import Annotated

import jwt
from fastapi import Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InvalidTokenError, UnauthorizedError
from app.core.security import decode_token
from app.db.session import get_session
from app.models.user import User
from app.repositories.user_repo import UserRepository
from app.schemas.common import PageParams

# auto_error=False：自己抛领域异常，保证错误响应结构与全局约定一致
# （True 时 FastAPI 会直接返回 {"detail": "Not authenticated"}，结构与其他错误不一致，
#   前端 client.ts 的 toApiError 拿不到 code 字段，只能退化成 HTTP_401）
bearer_scheme = HTTPBearer(auto_error=False, description="Bearer <access_token>")

# 会话依赖：同一个请求内多次声明 SessionDep，FastAPI 默认会缓存依赖结果（同一请求只执行一次），
# 因此「每个请求一个会话」而不是「每个 Depends 一个会话」。
SessionDep = Annotated[AsyncSession, Depends(get_session)]
CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


async def get_current_user(
    session: SessionDep,
    credentials: CredentialsDep,
) -> User:
    """解析 Bearer Token → 校验签名/有效期/类型 → 加载用户。

    生命周期：**每次请求执行一次**（FastAPI 对同一请求内的依赖做结果缓存），
    所以一次请求里多个路由参数写 `CurrentUser` 也只会解码一次令牌、查一次库。

    为什么独立成一个依赖（而不是在路由里手动解 token）：
    - 所有需要登录的接口共用同一条校验链，安全策略只需改这一处；
    - 校验链的顺序有讲究：先解令牌（纯 CPU，无 I/O）再查库，非法令牌不产生数据库查询；
    - 校验的是「令牌有效」+「用户仍存在且未禁用」两件事。只验签名是不够的：
      用户被禁用后旧 access_token 在到期前依然签名有效，必须回库检查 is_active。

    失败语义：无 Authorization 头 → 401 UNAUTHORIZED；令牌非法/过期/类型不对 → 401 INVALID_TOKEN；
    用户不存在或已禁用 → 401 UNAUTHORIZED（统一对外，避免暴露账号状态）。
    """
    # 没有凭证：直接 401，而不是交给 HTTPBearer 抛框架异常（结构不统一）
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("缺少 Authorization 头")
    try:
        # expected_type="access"：refresh_token 不能当 access_token 用（反之亦然），
        # 否则短命 access 的设计就失效了——刷新令牌通常 7 天，被拿来直连等于长期凭证泄漏。
        payload = decode_token(credentials.credentials, expected_type="access")
        # sub 是 JWT 标准字段，值是字符串；转 int 失败说明令牌被伪造/篡改
        user_id = int(payload["sub"])
    except (InvalidTokenError, KeyError, TypeError, ValueError) as exc:
        # 把所有「解不开」的原因统一成 InvalidTokenError：不要向外区分
        # 「签名错 / 过期 / 缺 sub」，否则等于给攻击者做令牌探测。
        raise InvalidTokenError() from exc

    # 回库校验：确认用户仍存在且 is_active（get_active 已把 is_active 条件写进 WHERE）
    user = await UserRepository(session).get_active(user_id)
    if user is None:
        raise UnauthorizedError("用户不存在或已被禁用")
    return user


async def get_optional_user(session: SessionDep, credentials: CredentialsDep) -> User | None:
    """可选登录：用于既能匿名又能登录使用的接口（如公开演示数据）。

    与 get_current_user 的关键差别：**失败不抛异常，返回 None**。
    适用场景是「登录了就返回你自己的数据，没登录就返回公共数据」这类同一 URL 两种语义的接口；
    它把「是否需要登录」的判断权交给路由，而不是由依赖强制。
    """
    if credentials is None:
        return None
    try:
        return await get_current_user(session, credentials)
    except (UnauthorizedError, jwt.PyJWTError):
        # 令牌无效时静默降级为匿名，而不是 401：这类接口的契约是「永远可用」。
        # 捕获 UnauthorizedError 覆盖了「用户被禁用/不存在」，jwt.PyJWTError 兜住
        # decode_token 可能直接冒出的底层异常，防止把 500 泄漏成一个匿名接口。
        return None


# 路由签名里用这两个别名，而不是每次写 Depends(...)：可读性 + 类型完整
CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]


def pagination(
    page: Annotated[int, Query(ge=1, description="页码")] = 1,
    size: Annotated[int, Query(ge=1, le=100, description="每页条数")] = 20,
) -> PageParams:
    """把 page/size 两个 Query 参数收敛为强类型 `PageParams`。

    为什么要收敛：服务层需要的是「offset/limit」而不是「page/size」；
    让 `PageParams.offset` 这个派生属性去算 `(page-1)*size`，路由和 Service 都不碰算术，
    分页规则改动只改一个地方（单一职责）。

    边界即防线：`ge=1` 拦住 page=0/-1（否则 offset 变负数，SQLite 会返回全表），
    `le=100` 拦住 `?size=100000`——不设上限就是让人一条 URL 把库和内存拉满（DoS 面）。
    """
    return PageParams(page=page, size=size)


PaginationDep = Annotated[PageParams, Depends(pagination)]


def get_request_id(request: Request) -> str | None:
    """读取当前请求的 request_id（由 RequestContextMiddleware 写入 request.state）。

    为什么用 `getattr(..., None)` 而不是 `request.state.request_id`：
    `request.state` 是动态属性袋，中间件未执行（如单测直接调路由、或中间件顺序被改动）时
    直接取属性会抛 AttributeError，把一个「日志字段缺失」放大成 500。
    """
    return getattr(request.state, "request_id", None)
