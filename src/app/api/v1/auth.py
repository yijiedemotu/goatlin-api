"""认证路由。"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import CurrentUser, SessionDep
from app.schemas.common import MessageResponse
from app.schemas.user import RefreshRequest, TokenPair, UserLogin, UserOut, UserRegister, UserUpdate
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="注册新用户并预置默认分类",
    responses={409: {"description": "邮箱已被注册"}},
)
async def register(payload: UserRegister, session: SessionDep) -> UserOut:
    """注册账号（公开接口，无需登录）。

    入参 `UserRegister` 已在 schema 层校验邮箱格式与密码强度（Pydantic 校验失败统一 422）。
    成功 201 返回用户信息（**不含密码哈希**，`UserOut` 是白名单输出模型）；
    邮箱重复 409 `EMAIL_ALREADY_EXISTS`（含并发场景：唯一索引兜底后转成同一错误码）。

    路由里只调用一次 Service：因为「插用户 + 批量插 10 条默认分类」必须同事务提交，
    事务边界归 Service 管，路由不掺和。
    前端 `LoginView.vue` 的注册表单 / `stores/auth.ts::register`。
    """
    user = await AuthService(session).register(payload)
    # from_attributes：ORM 对象 → Pydantic 模型。显式 model_validate 保证返回类型与
    # response_model 声明一致（响应字段一旦在 UserOut 里被删掉，这里立刻类型报错，而不是线上多返回字段）
    return UserOut.model_validate(user)


@router.post(
    "/login",
    response_model=TokenPair,
    summary="邮箱密码登录，返回 access/refresh 双令牌",
    responses={401: {"description": "邮箱或密码错误"}, 429: {"description": "触发限流"}},
)
async def login(payload: UserLogin, session: SessionDep) -> TokenPair:
    """邮箱密码登录（公开接口，无需登录），返回双令牌。

    401 `INVALID_CREDENTIALS`：邮箱不存在或密码错误（刻意不区分，防账号枚举）；
    401 `UNAUTHORIZED`：账号被禁用；
    429 `RATE_LIMITED`：触发认证档限流，响应带 `Retry-After`（秒）。

    为什么拆成 `authenticate` + `issue_tokens` 两步：校验身份与签发令牌是两个独立职责，
    将来加「登录设备记录 / 二次验证」只需在两者之间插入，不必改 authenticate。
    前端 `LoginView.vue` 提交表单后由 `stores/auth.ts` 落盘令牌。
    """
    service = AuthService(session)
    user = await service.authenticate(payload.email, payload.password)
    return await service.issue_tokens(user)


@router.post("/refresh", response_model=TokenPair, summary="用 refresh_token 换取新的双令牌")
async def refresh(payload: RefreshRequest, session: SessionDep) -> TokenPair:
    """用 refresh_token 换新的双令牌（公开接口，但入参令牌本身即凭证）。

    refresh_token 校验 `expected_type="refresh"`：access 不能拿来换票，否则
    「短命 access」的安全收益直接归零。
    令牌非法/过期 → 401；用户被禁用 → 401 `UNAUTHORIZED`；触发限流 → 429。

    前端不是人手动调的：`api/client.ts` 收到 401 时做**单飞刷新**
    （并发 401 只发一次 refresh，其余请求等同一 Promise 后重放），
    否则 5 个并发请求会各自刷新，第一次换票成功后其余 refresh_token 全部失效，用户被踢下线。
    """
    return await AuthService(session).refresh(payload.refresh_token)


@router.post("/logout", response_model=MessageResponse, summary="登出（客户端丢弃令牌）")
async def logout(current_user: CurrentUser) -> MessageResponse:
    """登出（需要登录，仅用于校验令牌有效性）。

    无状态 JWT 的服务端登出需要令牌黑名单；
    本项目采用短 access + refresh 轮换策略，登出由客户端清除令牌完成，
    因此该接口只回一个 message，不做任何服务端状态变更。

    面试追问：这样的登出「安全吗」？答：在 access 到期前，已被「登出」的令牌仍然可用，
    这是有状态黑名单 vs 无状态可扩展性的取舍。要真正的即时失效，就得引入
    Redis 存 jti 黑名单（每请求一次 Redis 查询）或给用户表加 `token_version` 字段。
    前端 `AppShell.vue` 顶栏的退出按钮调用。
    """
    # 无状态 JWT 的服务端登出需要令牌黑名单；
    # 本项目采用短 access + refresh 轮换策略，登出由客户端清除令牌完成。
    return MessageResponse(message=f"用户 {current_user.id} 已登出")


@router.get("/me", response_model=UserOut, summary="获取当前登录用户信息")
async def me(current_user: CurrentUser) -> UserOut:
    """获取当前登录用户资料（需要登录；只能读自己，天然无越权可能）。

    这个接口是「令牌仍然有效」的探针：前端刷新页面后先调它恢复用户态，
    失败即视为登录失效并跳登录页（见前端 `router/index.ts` 的 `beforeEach`）。
    无效/过期令牌 → 401。
    """
    return UserOut.model_validate(current_user)


@router.patch("/me", response_model=UserOut, summary="更新个人资料")
async def update_me(payload: UserUpdate, current_user: CurrentUser, session: SessionDep) -> UserOut:
    """更新个人资料（需要登录，只能改自己）。

    这里把 `current_user` 直接交给 Service，而不是传 `user_id`：Service 拿到的是
    已加载的 ORM 实体，更新后 `commit + refresh` 即可，少一次按 id 查询。
    风险点是「实体已经 `expire_on_commit=False`」——`db/session.py` 里关掉了提交后过期，
    所以 commit 后读属性不会触发异步 lazy load（否则会抛 MissingGreenlet）。
    前端 `stores/auth.ts::updateProfile` 调它，由 `api/auth.ts::updateMe`（PATCH /auth/me）发请求；
    当前 Vue 侧尚无组件消费 `updateProfile`，属预留能力（后端接口先就位）。
    """
    user = await AuthService(session).update_profile(current_user, payload)
    return UserOut.model_validate(user)
