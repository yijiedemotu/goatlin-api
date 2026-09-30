"""结构化日志：本地彩色可读，生产 JSON 便于采集。"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from app.core.config import settings

# 进程级幂等开关：configure_logging() 只在第一次真正生效。
# 用模块级布尔而不是「判断 structlog 是否已配置」：structlog 没有官方的 is_configured API，
# 自己记一个标志最直白，代价是测试里要重置它得手动改这个私有变量。
_configured = False


def configure_logging() -> None:
    """进程启动时调用一次。

    调用点：`app/main.py::lifespan`（应用启动阶段，早于任何请求处理）。
    副作用：配置 structlog 全局管线 + 调整 stdlib 根 logger 与几个第三方 logger 的级别。
    幂等：重复调用直接返回（见 `_configured`），因此测试里反复 `create_app()` 不会重复渲染。
    """
    global _configured
    if _configured:
        return

    # 这一行只作用于 stdlib logging（uvicorn 自己的启动/错误日志）：让它的输出与
    # structlog 的事件行风格一致（只要 message，不要 logging 默认的 "LEVEL:name:msg" 前缀）。
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level, logging.INFO),
    )
    # 降低第三方库噪音（用 getattr 取级别常量，避免把字符串写死在代码里）
    for noisy in ("uvicorn.access", "sqlalchemy.engine.Engine", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # processor 是「事件字典 → 事件字典」的链式函数，按列表顺序执行，最后一个通常是 renderer。
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,  # 自动带上 request_id / user_id
        structlog.processors.add_log_level,  # 注入 level 字段（JSON 采集后可按级别过滤）
        structlog.processors.TimeStamper(fmt="iso", utc=True),  # ISO8601 + UTC，跨时区可排序
        structlog.processors.StackInfoRenderer(),  # stack_info=True 时把调用栈渲染成字符串
        structlog.processors.format_exc_info,  # exc_info 转文本，必须早于 renderer
    ]
    if settings.log_format == "json":
        # ensure_ascii=False：中文原样输出（默认会转义成 \uXXXX，日志里搜不到中文）
        processors.append(structlog.processors.JSONRenderer(ensure_ascii=False))
    else:
        # ConsoleRenderer 只用于本地：带颜色、对齐的键值对，人读友好但不适合机器解析
        processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=processors,
        # 过滤型 bound logger：低于阈值的日志调用直接丢弃，省掉组装与渲染开销
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level, logging.INFO)
        ),
        # PrintLogger 直接写 stdout（无缓冲、无 stdlib handler 依赖），
        # 因此容器里 `docker logs` 能立刻看到，也不会被其他库的 logging 配置改坏
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """获取绑定上下文的 logger。

    参数：`name` 惯例传 `__name__`，用于标识日志来源模块（同名 stdlib logger 的语义）。

    返回：structlog 的**惰性代理**——首次真正打日志时才按当前配置组装出具体 logger。
    这一点很关键：模块顶部的 `logger = get_logger(__name__)` 发生在 `configure_logging()`
    之前也不会有问题，因为代理此时还没绑定。

    类型标注说明：返回类型写成 `structlog.stdlib.BoundLogger` 只是取「BoundLogger 协议」的
    名义类型（便于调用方拿到 `.bind/.info` 的补全），实际 wrapper 是
    `make_filtering_bound_logger(...)` 产出的过滤型 logger、printer 是 PrintLogger，
    与 stdlib 无关，所以这里有 `type: ignore`。
    """
    return structlog.get_logger(name)  # type: ignore[no-any-return]
