"""数据库引擎与会话管理（SQLAlchemy 2.0 异步）。

每请求一个会话（依赖注入），`expire_on_commit=False` 是异步场景的关键设置：
否则 commit 后访问属性会触发隐式查询并抛 MissingGreenlet。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def _build_engine() -> AsyncEngine:
    """按 DATABASE_URL 构造异步引擎（SQLite 与 PostgreSQL 两条分支）。

    返回：`AsyncEngine`，内部包着一个 sync engine（`engine.sync_engine`），
    `run_sync()` / connect 事件都作用在那一层。

    为什么按驱动分支而不是一套参数走天下：
    - SQLite 是**单文件、单写者**模型，没有服务端连接数限制，瓶颈是文件锁而不是连接池，
      因此这一支不传 `pool_size/max_overflow`（传了也是自欺欺人），
      真正要调的是下面的 PRAGMA；
    - PostgreSQL 是 C/S 模型，连接是昂贵资源，必须显式控制池大小与回收时间。
    代价是两个分支的调优项不同，改一处参数要记得同步另一处（本项目用配置项统一暴露）。
    """
    url = settings.database_url

    if settings.is_sqlite:
        # SQLite 需要确保文件所在目录存在，否则首次启动会直接报 unable to open database file
        if ":///" in url:
            # URL 形如 sqlite+aiosqlite:///./data/goalfin.db，切出 ":///" 之后即是文件路径
            raw_path = url.split(":///", 1)[1]
            if raw_path and not raw_path.startswith(":memory:"):
                # 建父目录：data/ 被 .gitignore 忽略，全新 clone 时并不存在
                Path(raw_path).parent.mkdir(parents=True, exist_ok=True)
        return create_async_engine(
            url,
            echo=settings.db_echo,
            future=True,  # 显式声明使用 2.0 风格 API（为将来移除该开关做准备）
            pool_pre_ping=True,
            # aiosqlite 会在自己的工作线程里使用 sqlite3 连接，而 sqlite3 默认禁止
            # 跨线程使用连接对象 → 不关掉这个检查会直接抛 ProgrammingError。
            connect_args={"check_same_thread": False},
        )

    return create_async_engine(
        url,
        echo=settings.db_echo,
        future=True,
        pool_pre_ping=True,  # 取连接前 ping 一次，避免拿到被中间件掐断的死连接
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=1800,  # 小于 MySQL 默认 wait_timeout，防止空闲连接被服务端回收
    )


# 模块级引擎：整个进程共用一个连接池（引擎是线程/协程安全的，会话才不是）。
# 导入即创建，因此配置错误（URL 写错、目录不可写）会在进程启动第一步就暴露。
engine: AsyncEngine = _build_engine()

# expire_on_commit=False：commit 后对象属性仍可直接读取，
# 否则异步场景下访问属性会触发隐式 lazy load 并抛 MissingGreenlet。
SessionFactory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


if settings.is_sqlite:
    # 每个 DBAPI 连接建立后都会触发 "connect" 事件——PRAGMA 是**连接级**设置，
    # 必须逐连接执行；只设置一次（例如建库时）会导致池里新开的连接退回默认值。
    # 注意 listen 的对象是 sync_engine：异步引擎的 connect 事件挂在底层同步引擎上。
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection: object, _record: object) -> None:
        """SQLite 调优：WAL 提升读写并发，foreign_keys 打开外键约束。

        三条 PRAGMA 的取舍：
        - `journal_mode=WAL`（Write-Ahead Logging）：读事务不再被写事务阻塞，读写可并发。
          它是**写进库文件的持久设置**，一次生效后续连接沿用，但这里仍然逐连接执行，
          因为新建库、`--drop` 重建这些路径都会回到默认的 delete 模式。
        - `foreign_keys=ON`：SQLite **默认关闭**外键约束，不开的话 `ON DELETE CASCADE`
          这类声明只是注释；打开后数据库层才能兜住孤儿数据（应用层还有一道显式检查，
          见 services 里的删除分类保护）。
        - `synchronous=NORMAL`：默认 FULL 每次 commit 都 fsync，写入吞吐差一个量级；
          WAL 下 NORMAL 只在 checkpoint 时 fsync，**进程崩溃不丢数据**，
          只有「操作系统/断电」这类灾难才可能丢最后几个事务——记账场景可接受。
        """
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()  # 显式关闭：connect 事件里泄漏的游标会一直挂在连接上


async def get_session() -> AsyncGenerator[AsyncSession]:
    """FastAPI 依赖：每请求一个会话。

    生命周期：请求进入时创建，响应结束后由 FastAPI 调用生成器的收尾逻辑关闭
    （`async with` 保证连接归还池中，即使路由抛异常也不会泄漏连接）。

    为什么异常时显式 `rollback()` 再 raise：会话是从池里借来的，池归还连接时会做
    rollback，但**显式回滚**能保证「未提交的脏事务」在离开本函数前就结束，
    也避免了「异常被吞掉后同一个会话被继续复用」的隐式状态。
    为什么不让这个依赖 commit：事务边界属于业务（注册要一次写用户 + 10 条分类），
    放在这里会变成「一个请求多个事务」，中途失败就产生脏数据。
    """
    async with SessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def check_database() -> bool:
    """健康检查用：探测数据库连通性。

    实现：从连接池借一条连接执行 `SELECT 1`。之所以不用 `engine.connect()` 之外的
    ORM 查询：这条语句不需要表，能区分「库连不上」和「表没建」两种故障。

    返回：True 表示可用；False 表示探测失败（异常已记日志 `database_unreachable`）。
    注意返回 bool 而不是抛异常：`/health` 要能**明确回答**「依赖坏了」，
    抛异常会变成 500，K8s 反而只看到「进程崩了」，无法区分降级与不可用。
    导入 `text` 放在函数内是为了让模块导入期不依赖 SQLAlchemy 的这层 API（纯属就近可读）。
    """
    from sqlalchemy import text

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # pragma: no cover - 仅在故障时触发
        # 不打堆栈的 error：探针可能被高频调用，堆栈会把日志刷爆，异常类型+消息足够定位
        logger.error("database_unreachable", error=str(exc))
        return False


async def dispose_engine() -> None:
    """应用关闭时释放连接池。

    由 `app/main.py::lifespan` 的 yield 之后调用——不释放的话，
    进程退出时池里的连接会以「未正常关闭」的形式消失，
    PostgreSQL 侧要等 TCP 超时才能回收（表现为重启期间连接数缓慢回落、甚至打满 max_connections）。
    """
    await engine.dispose()
