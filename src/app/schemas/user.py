"""用户与认证相关 Schema。密码字段的约束与敏感字段过滤都在这里。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.security import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH
from app.schemas.common import ORMModel


class UserRegister(BaseModel):
    """注册请求体（POST /auth/register）。

    nickname 允许为空：AuthService 会用邮箱 @ 前面那一段兜底，
    避免前端必须强制用户填写昵称才能完成注册。
    """

    email: EmailStr
    password: str = Field(
        min_length=MIN_PASSWORD_LENGTH,
        max_length=MAX_PASSWORD_LENGTH,
        description="至少 8 位，需同时包含字母与数字",
    )
    nickname: str = Field(default="", max_length=50)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        """把邮箱规范成「去空白 + 小写」后返回。

        规范化发生在校验阶段，因此 Service 拿到的 email 已经是唯一索引实际存储的形式。
        """
        # 入库前统一小写：唯一索引才有意义，登录查询也不必再套 lower()
        return value.strip().lower()

    @field_validator("password")
    @classmethod
    def _check_strength(cls, value: str) -> str:
        """注册时的密码强度校验：纯数字或纯字母都拒绝，返回原密码。

        异常：密码只由数字或只由字母组成时抛 ValueError（→ 422）。
        """
        # isdigit()/isalpha() 为真说明「纯数字」或「纯字母」，两种情况都拒绝；
        # 注意这里不校验是否含数字+字母之外的其他形态，强度下限由此函数定义
        if value.isdigit() or value.isalpha():
            raise ValueError("密码需同时包含字母和数字")
        return value


class UserLogin(BaseModel):
    """登录请求体（POST /auth/login）。

    只做长度约束不做强度约束：规则变化（例如历史上更宽松的密码）不该把老用户挡在门外。
    """

    email: EmailStr
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        """把登录邮箱规范成「去空白 + 小写」后返回，规则与注册完全一致。"""
        # 与注册保持一致，否则大写邮箱注册后无法登录
        return value.strip().lower()


class TokenPair(BaseModel):
    """双令牌响应体：access 用于日常请求，refresh 用于静默续期。

    前端 auth store 会保存两者，并在 access 过期时用 refresh 换新的一对；
    expires_in 让前端提前续期，而不必等到收到 401 才被动处理。
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth2 令牌类型标识，不是密码
    expires_in: int = Field(description="access_token 有效期（秒）")


class RefreshRequest(BaseModel):
    """刷新令牌请求体（POST /auth/refresh）：只带 refresh_token。"""

    refresh_token: str


class UserOut(ORMModel):
    """用户资料响应（注册/登录/资料接口共用）。

    刻意不含 password_hash：模型层有这一列，但契约层没有，属于「默认不泄露」的设计。
    前端只用到 nickname、email、is_active 与 created_at（展示注册时间）。
    """

    id: int
    email: EmailStr
    nickname: str
    is_active: bool
    created_at: datetime


class UserUpdate(BaseModel):
    """资料更新请求体（PATCH /auth/me）：目前只开放昵称。

    邮箱与密码不在这里修改：前者牵涉唯一性与重新验证，后者需要校验旧密码，
    都属于独立的敏感流程，混进通用 PATCH 会削弱可审计性。
    """

    nickname: str | None = Field(default=None, max_length=50)
