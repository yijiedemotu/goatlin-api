"""全局配置：全部来自环境变量 / .env，启动期一次性校验。

默认值刻意取「本地开箱即跑」（SQLite + dev 密钥），生产环境由
`_guard_production` 拦住弱密钥与 DEBUG，属于 fail-fast 设计。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# 项目根目录（src/app/core/config.py → parents[3] = goalfin-api/）。
# 之所以要绝对定位，是因为进程的工作目录取决于启动方式（Makefile / uvicorn / pytest
# 各不相同），用相对路径找 .env 会出现「本地能跑、换个目录就找不到配置」的幽灵问题。
BASE_DIR = Path(__file__).resolve().parents[3]

# 弱密钥黑名单：既是「已知的不安全值」，也是给新同学的提示——
# 默认值本身就在名单里，因此 production 不显式配置就会启动失败。
_WEAK_SECRETS = {
    "",
    "dev-only-secret-key-please-change-me-in-production",
    "secret",
    "changeme",
}


class Settings(BaseSettings):
    """应用配置对象（进程内单例）。

    生命周期：进程启动时由 `get_settings()` 构造一次，之后只读，随进程退出而销毁；
    测试里靠 `monkeypatch.setattr(settings, "字段", 值)` 临时改（tests/test_rate_limit.py 就是
    这样把阈值收到 3 次/分钟，避免为限流单开一套配置）。

    为什么独立成类而不是模块级常量：① 每个字段都有类型与默认值，写错名字/类型立刻报错；
    ② 校验逻辑（弱密钥、日志级别、CORS 切分）能集中在一处并被测试单独覆盖；
    ③ 未来要给某个模块注入不同配置（如 webhook 用另一套），只需再 new 一个 Settings。
    """

    # extra="ignore"：宿主机上大量无关环境变量（PATH、HOME…）不应该让配置解析失败；
    # case_sensitive=False：`secret_key` 与 `SECRET_KEY` 视为同一个字段，少一类踩坑。
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------------- 应用 ----------------
    app_name: str = "goalfin-api"
    # 只允许三个取值：用 Literal 而不是 str，写错 APP_ENV 会在启动期报错，
    # 而不是让 `is_production` 悄悄变成 False（生产环境弱密钥就再也拦不住了）。
    app_env: Literal["local", "staging", "production"] = "local"
    app_debug: bool = True
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    api_prefix: str = "/api/v1"

    # ---------------- 数据库 ----------------
    # 前缀 `sqlite+aiosqlite` 与 `postgresql+asyncpg` 决定用哪个异步驱动，
    # 切换 PostgreSQL 只需要改这一个变量（见 db/session.py 的引擎构造）。
    database_url: str = "sqlite+aiosqlite:///./data/goalfin.db"
    db_echo: bool = False  # True 会把每条 SQL 打到日志，排查 N+1 时临时开，平时必须关
    db_pool_size: int = 10  # 常驻连接数；SQLite 分支不使用（见 db/session.py）
    db_max_overflow: int = 20  # 峰值允许的临时连接数，总上限 = pool_size + max_overflow

    # ---------------- 鉴权 ----------------
    secret_key: str = "dev-only-secret-key-please-change-me-in-production"
    jwt_algorithm: str = "HS256"  # 对称签名：单服务自签自验，够用且最快；多服务应换 RS256
    jwt_issuer: str = "goalfin-api"  # iss 声明，防止别的系统用同一密钥签的令牌混进来
    access_token_expire_minutes: int = 30  # 短有效期换取「泄露后窗口小」
    refresh_token_expire_days: int = 7  # 长有效期换取「用户不用反复登录」

    # ---------------- 限流 ----------------
    rate_limit_enabled: bool = True  # 测试里关掉，避免用例之间互相把对方限流掉
    rate_limit_auth_per_minute: int = 10  # 登录/注册/刷新：撞库的首要目标，阈值更严
    rate_limit_default_per_minute: int = 120
    rate_limit_window_seconds: int = 60  # 令牌桶的补满周期，也是 Redis 固定窗口的宽度
    redis_url: str | None = None  # 为空则用进程内令牌桶；配了就切 Redis（多实例共享配额）

    # ---------------- 可观测性 ----------------
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"  # 本地彩色可读，容器里换 json 给采集器
    # NoDecode 是必须的，不是装饰：pydantic-settings 对 list[str] 会先把环境变量当 JSON 解析，
    # 解析失败直接抛 SettingsError，根本走不到下面的 field_validator——
    # 于是最自然的 `CORS_ORIGINS=a,b` 会让服务启动即崩，且报错信息很难联想到 CORS。
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:5173"])

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """支持 `CORS_ORIGINS=a,b,c` 与 `CORS_ORIGINS=["a","b"]` 两种写法。

        因为字段上标了 `NoDecode`，环境变量会以**原始字符串**到达这里，所以两种写法都能处理：
        - 逗号分隔：直接 split（顺手 strip + 过滤空串，`a,,b,` 不会产生空来源）；
        - JSON 数组：交给 `json.loads`，兼容别人已经写成 JSON 的 `.env`。
        """
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                import json

                try:
                    parsed = json.loads(text)
                except ValueError as exc:  # 写了半截 JSON，给出明确原因而不是静默吞掉
                    raise ValueError(f"CORS_ORIGINS 不是合法 JSON 数组：{text[:60]}") from exc
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            return [item.strip() for item in text.split(",") if item.strip()]
        return value

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, value: str) -> str:
        """把日志级别规范化成大写并做白名单校验。

        `logging` 模块本身对未知级别名是「静默忽略」的，配错 LOG_LEVEL 只会让人觉得
        「日志怎么没了」，所以这里主动抛错；同时统一大写，避免 `info` 与 `INFO` 两种配置漂移。
        """
        level = value.upper()
        # 白名单校验：非法值直接让 Settings 构造失败（启动期暴露）
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"非法的日志级别: {value}")
        return level

    @field_validator("database_url")
    @classmethod
    def _anchor_sqlite_path(cls, value: str) -> str:
        """把「相对路径」的 SQLite URL 锚定到项目根目录（BASE_DIR）。

        为什么必须锚定：SQLite 的相对路径是按**进程工作目录**解析的，而工作目录随启动方式变化
        （终端在项目根、PyCharm 自动生成的运行配置把工作目录设成 `src/app`、pytest 又在别处）。
        不锚定的后果不是报错，而是**静默连到另一个空库**：`./data/goalfin.db` 会变成
        `src/app/data/goalfin.db`，表现为「表不存在」或「数据全没了」，极难排查。

        只动「`sqlite+aiosqlite:///` + 相对路径」这一种形态：`:memory:`（测试用）与
        绝对路径（POSIX 的 `/...`、Windows 的 `D:/...`）一律原样保留。
        """
        prefix = "sqlite+aiosqlite:///"
        if not value.startswith(prefix):
            return value
        raw = value[len(prefix) :]
        if not raw or raw.startswith(":memory:") or raw.startswith("/") or raw[1:2] == ":":
            return value
        return f"{prefix}{(BASE_DIR / raw).resolve().as_posix()}"

    @model_validator(mode="after")
    def _guard_production(self) -> Settings:
        """跨字段的最终防线：生产环境不允许弱密钥、不允许 DEBUG。

        为什么用 `mode="after"`：这两条约束依赖「多个字段已解析完成」——
        必须先知道 app_env 才能判断密钥是否弱。单字段校验器做不到这件事。

        为什么必须 fail-fast 而不是只打 WARNING：JWT 用的是对称密钥，密钥一旦可猜，
        攻击者可以在本地签出任意 `sub` 的合法令牌，服务端无法区分真伪——
        这是「静默的、无日志痕迹的全量越权」，只在启动期拦才有意义。
        """
        if self.app_env == "production":
            if self.secret_key in _WEAK_SECRETS:
                # 黑名单 + 空串都算弱密钥：空串常见于 `SECRET_KEY=` 忘了填值
                raise ValueError("生产环境必须设置强随机 SECRET_KEY")
            if self.app_debug:
                # debug 打开会把堆栈、SQL、内部路径暴露给调用方，等同于信息泄露漏洞
                raise ValueError("生产环境不允许开启 APP_DEBUG")
        return self

    # ------------------------------------------------------------------
    # 派生属性
    # ------------------------------------------------------------------
    @property
    def is_sqlite(self) -> bool:
        """当前是否使用 SQLite。db/session.py 用它决定 PRAGMA 调优与连接池分支。"""
        return self.database_url.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        """是否生产环境。给「只在线上生效」的行为（如关掉 /docs）留统一开关点。"""
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取配置单例（测试中可用 get_settings.cache_clear() 重置）。

    为什么用 lru_cache 而不是模块级 `settings = Settings()` 就完事：包一层函数后，
    测试可以先 `get_settings.cache_clear()`、改好环境变量再调用，拿到全新配置；
    直接写模块级常量就只能靠 reload 模块，副作用更大。
    """
    return Settings()


# 模块级常量：最省事的用法 `from app.core.config import settings`。
# 代价是它在 import 期就被求值，因此「先设环境变量、后 import」是唯一正确的顺序。
settings = get_settings()
