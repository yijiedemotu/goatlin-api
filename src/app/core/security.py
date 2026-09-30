"""安全组件：bcrypt 密码哈希 + JWT 双令牌（access / refresh）。

bcrypt 是**故意慢**的哈希（cost 可调 + 自带随机盐），用于抵消 GPU 暴力破解；
JWT 校验必须给 algorithms 白名单，否则存在 alg 混淆攻击面。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

import bcrypt
import jwt

from app.core.config import settings
from app.core.exceptions import InvalidTokenError, TokenExpiredError

# 令牌用途的静态类型：让 `_create_token(subject, "acess", ...)`（拼错）在 mypy 阶段就报错，
# 而不是等到线上出现「令牌类型不匹配」的 401。
TokenType = Literal["access", "refresh"]

BCRYPT_MAX_BYTES: Final = 72  # bcrypt 算法只吃前 72 字节，超出部分被**静默忽略**
MIN_PASSWORD_LENGTH: Final = 8
MAX_PASSWORD_LENGTH: Final = 64  # 多字节字符下也不会触发 bcrypt 72 字节截断

# 运行时用的字符串常量（Literal 只在类型层存在，运行时需要真实值来写进 payload）。
ACCESS_TOKEN_TYPE: Final = "access"
REFRESH_TOKEN_TYPE: Final = "refresh"


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------
def _prepare(password: str) -> bytes:
    """把明文密码编码成 bcrypt 需要的 bytes，并在超长时**显式报错**。

    为什么要主动抛错：bcrypt 对超过 72 字节的输入是「截断但不报错」，
    于是「72 字节相同、后面不同」的两个密码会被判定为同一个密码——
    静默的安全缺陷。这里宁可让调用方失败，也不接受截断语义。
    """
    encoded = password.encode("utf-8")  # 先编码再判长度：中文 1 字符 = 3 字节
    if len(encoded) > BCRYPT_MAX_BYTES:
        raise ValueError("密码长度超出安全上限")
    return encoded


def hash_password(password: str) -> str:
    """生成 bcrypt 哈希（自带随机 salt）。

    参数：明文密码（UTF-8）。
    返回：形如 `$2b$12$...` 的字符串，前段带算法版本与 cost，可直接落库（users.password_hash）。
    关键实现：`gensalt()` 每次调用都生成新的随机 salt 与默认 cost（12 ≈ 100ms），
    因此**同一个密码两次注册得到的哈希不同**，无法用哈希相等来查「谁用了相同密码」，
    也让彩虹表失效。代价是登录时每次校验都要付出 ~100ms CPU——这正是限流要保护的对象。
    """
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """常数时间校验密码；任何异常都视为失败，不向调用方泄露细节。"""
    try:
        # 比较在 bcrypt 底层完成（bcrypt 5.x 为 Rust 实现），不在这里做逐字符比较
        return bcrypt.checkpw(_prepare(plain_password), hashed_password.encode("utf-8"))
    except (ValueError, TypeError):
        # 覆盖两类情况：① 明文超过 72 字节被 _prepare 拒绝；② 库里的哈希串损坏/为空。
        # 统一返回 False 而不是抛异常，调用方（auth_service）才能一律回 INVALID_CREDENTIALS，
        # 避免出现「哈希损坏返回 500、密码错误返回 401」这种可被用来探测账号状态的差异。
        return False


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
def _encode(payload: dict[str, Any]) -> str:
    """统一签发入口（包含 json → base64url → 签名三步，由 PyJWT 完成）。"""
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def _create_token(subject: str, token_type: TokenType, expires_delta: timedelta) -> tuple[str, str, datetime]:
    """生成令牌，返回 (token, jti, 过期时间)。

    参数语义：
    - `subject`：令牌归属者，这里固定放 user_id 的字符串形式（JWT 规范要求 sub 是字符串）；
    - `token_type`：`access` / `refresh`，写进 payload 供校验时区分用途；
    - `expires_delta`：相对当前时刻的有效期，由调用方决定（access 用分钟、refresh 用天）。

    为什么时间戳都用 `int(now.timestamp())`：JWT 的 iat/nbf/exp 规范是 **NumericDate（秒）**，
    直接塞 datetime 会签出不合规令牌（PyJWT 也会报错）；统一用 UTC 避免时区错位。
    为什么同时放 iat 与 nbf：iat 记录签发时刻（审计/排障），nbf 表示「此时间前不可用」，
    两者相等即「立即生效」；这样给未来的时钟偏移留了扩展位。
    """
    now = datetime.now(UTC)
    expire = now + expires_delta
    jti = uuid.uuid4().hex  # 令牌唯一 id：幂等重放检测与「服务端吊销」的抓手
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "jti": jti,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "iss": settings.jwt_issuer,
    }
    return _encode(payload), jti, expire


def create_access_token(user_id: int | str) -> tuple[str, datetime]:
    """返回 (access_token, 过期时间)。

    只返回过期时间不返回 jti：access 的有效期短、且校验是无状态的，
    服务端不做 access 吊销（要强制下线请吊销 refresh，见 auth_service 的说明）。
    """
    token, _jti, expire = _create_token(
        str(user_id),  # 统一转字符串：int 与 str 混用会让 sub 的类型随调用方漂移
        ACCESS_TOKEN_TYPE,
        timedelta(minutes=settings.access_token_expire_minutes),
    )
    return token, expire


def create_refresh_token(user_id: int | str) -> tuple[str, str, datetime]:
    """返回 (refresh_token, jti, 过期时间)，jti 可用于服务端吊销。

    为什么 refresh 必须带出 jti：refresh 有效期长（7 天）且能换出一整套新令牌，
    是「强制下线 / 单设备登录」唯一可操作的抓手（把 jti 记入黑名单即可作废）。
    当前实现只签发不落库，所以登出仍然是「客户端丢弃令牌」——见 README roadmap。
    """
    return _create_token(
        str(user_id),
        REFRESH_TOKEN_TYPE,
        timedelta(days=settings.refresh_token_expire_days),
    )


def decode_token(token: str, expected_type: TokenType | None = None) -> dict[str, Any]:
    """解码并校验 JWT，失败时抛出领域异常。

    参数：`token` 为不带 `Bearer ` 前缀的裸令牌；`expected_type` 传了就要求 payload["type"]
    与之相等（access 接口传 "access"，刷新端点传 "refresh"）。

    返回：解码后的 payload（dict，含 sub/type/jti/exp/iss）。
    异常：过期 → `TokenExpiredError`（401 TOKEN_EXPIRED，前端据此触发静默续期）；
    签名错/格式错/缺少必需声明/签发者不符 → `InvalidTokenError`（401 INVALID_TOKEN）。
    两类异常分开，是为了让前端能区分「该续期」和「该重新登录」——都揉成 401 会让
    前端在令牌被篡改时也去刷新，白白多一次失败请求。

    关键实现选择：
    - `algorithms=[...]` **必须显式传列表**：不接受令牌头部自称的算法，
      否则经典 `alg: none` / HS/RS 混淆攻击可以伪造令牌；
    - `issuer=...` 校验签发者，防止「同一个密钥被别的系统复用」时令牌串用；
    - `options={"require": [...]}` 强制必需声明存在，缺 `exp` 的永不过期令牌直接被拒；
    - 没有配 leeway：容忍时钟偏移需要在分布式部署时再加（当前单机部署不需要）。
    """
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "type"]},
        )
    except jwt.ExpiredSignatureError as exc:
        # 先 catch 过期：它是 PyJWTError 的子类，顺序反了就会被下面的分支吞掉，
        # 前端再也拿不到 TOKEN_EXPIRED，静默续期直接失效。
        raise TokenExpiredError() from exc
    except jwt.PyJWTError as exc:
        raise InvalidTokenError() from exc

    # 用途校验放在签名校验之后：先确认令牌可信，再谈它能不能用于这个场景。
    if expected_type and payload.get("type") != expected_type:
        raise InvalidTokenError(f"令牌类型不匹配，期望 {expected_type}")
    return payload
