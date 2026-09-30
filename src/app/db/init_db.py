"""数据库初始化脚本：建表 / 重置本地数据。

生产环境请用 Alembic 迁移（保留版本可追溯），本脚本只用于本地开发。"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import inspect

from app.core.config import settings
from app.db.session import engine
from app.models import Base
from app.models.base import utcnow  # noqa: F401  # 确保 Base.metadata 已加载全部模型


def _ensure_sqlite_dir() -> None:
    """确保 SQLite 文件所在目录存在（非 SQLite 直接返回）。

    为什么不直接对 `settings.database_url` 做字符串处理了事：
    这里要兼容 `:memory:`（测试用，没有目录）、相对路径（`./data/...`）与绝对路径三种形态，
    所以先切出路径再判断。

    实际执行顺序上的一个细节：本模块 import `engine` 时，`db/session.py::_build_engine()`
    已经把目录建好了，因此这一步在「已有目录」的情况下是幂等的兜底，
    主要价值是把解析结果**打印出来**，让人确认自己连的到底是哪个库文件。
    """
    if not settings.is_sqlite:
        return
    # split(..., 1)[-1] 取 ":///" 之后的部分；没有 ":///" 时整体作为路径（容错）
    raw = settings.database_url.split(":///", 1)[-1]
    if raw and not raw.startswith(":memory:"):
        path = Path(raw)
        path.parent.mkdir(parents=True, exist_ok=True)  # exist_ok：重复执行不报错
        print(f"[init_db] sqlite 目录已就绪: {path.parent.resolve()}")


async def _run(drop: bool) -> None:
    """执行「建表」这件事本身。

    参数：`drop=True` 时先 drop_all 再 create_all（等价于重置本地库）。

    实现选择：
    - `engine.begin()` 而不是 `engine.connect()`：前者开一个事务，DDL 与事务一起提交/回滚；
    - `run_sync(...)`：metadata 的 DDL 与 inspect 都是**同步 API**，
      异步引擎必须用 run_sync 把它切换到同步层执行（否则会拿到 coroutine 或直接报错）；
    - 建表依赖 `Base.metadata` 里注册好的全部表，而这些表来自
      `from app.models import Base` 触发的模型导入（见 app/models/__init__.py 的说明）——
      少 import 一个模型，这里就会静默少建一张表；
    - `create_all` 是「表不存在才建」，**不会**给已存在的表加列/改列，
      已上线的库要改结构必须用 Alembic 迁移（这条最容易踩）。
    """
    _ensure_sqlite_dir()
    async with engine.begin() as conn:
        if drop:
            print("[init_db] 删除全部表 ...")
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

        # 回读数据库里的真实表名（而不是打印 metadata 里「期望」的表），
        # 这样才能发现「模型没被导入」这类问题。
        tables = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())

    # 脚本是一次性任务，用完必须释放连接池，否则进程可能因未关闭的连接而迟迟不退出
    await engine.dispose()
    print(f"[init_db] 数据库就绪，共 {len(tables)} 张表: {', '.join(sorted(tables))}")


def main() -> None:
    """CLI 入口：解析参数并驱动 `_run`。

    为什么用 argparse 而不是读环境变量：破坏性操作应该是「打出来的命令」，
    而不是某个环境变量被继承导致的意外行为；`--drop` 的 help 里也写明了会清空数据。
    `asyncio.run` 在脚本里负责建/关事件循环——这正是应用侧
    （main.py 里换 Windows 事件循环策略）要考虑的那个循环。
    """
    parser = argparse.ArgumentParser(description="初始化 Goalfin 数据库")
    parser.add_argument("--drop", action="store_true", help="先删除已有表再重建（会清空数据）")
    args = parser.parse_args()
    asyncio.run(_run(drop=args.drop))


if __name__ == "__main__":
    main()
