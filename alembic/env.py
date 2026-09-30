"""Alembic 运行环境。

要点：
1. 数据库地址不写死在 alembic.ini，而是导入应用配置，保证「代码与迁移看到同一个库」；
2. `render_as_batch=True`：SQLite 不支持 ALTER COLUMN，Alembic 会用「建新表→拷数据→换名」
   的 batch 模式模拟，否则 SQLite 上的字段修改迁移会失败；
3. 打开 `compare_type` / `compare_server_default`，让 autogenerate 能识别类型与默认值变更。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool
# 让 alembic 能找到 src 下的 app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app.core.config import settings  # noqa: E402
from app.models import Base  # noqa: E402  # 必须导入聚合模块，否则 autogenerate 看不到表

config = context.config
sync_url = settings.database_url.replace("+aiosqlite", "").replace("+asyncpg", "+psycopg")
config.set_main_option("sqlalchemy.url", sync_url)

# SQLite 首次迁移时目录可能还不存在，直接连接会报 "unable to open database file"
if settings.is_sqlite:
    raw_path = sync_url.split(":///", 1)[-1]
    if raw_path and not raw_path.startswith(":memory:"):
        Path(raw_path).parent.mkdir(parents=True, exist_ok=True)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连接数据库（用于 DBA 审核）。"""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接数据库执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
