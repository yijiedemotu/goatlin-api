"""数据访问层（Repository）基类。

Repository 只 `flush` 不 `commit`：事务边界由 Service 统一控制，
这样「一次用例的多表写入」天然在同一个事务里。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Base


class BaseRepository[ModelT: Base]:
    """通用 CRUD 仓储，子类只需声明 model。

    职责：提供与具体表无关的取 / 增 / 删 / 计数能力；子类声明 `model` 后
    再补充自己的查询方法（例如 `UserRepository.get_by_email`）。

    生命周期：**一次请求一个实例**，由 Service 的 `__init__` 现场创建
    （不是单例、不是全局），随请求作用域一起被回收。它自己不持有连接，
    连接归注入进来的 AsyncSession 管，所以实例之间没有共享状态，
    天然并发安全，也不需要加锁。

    为什么独立成类：三个子仓储的 CRUD 逻辑完全重复；抽出来后
    `count()` 里"包 subquery"这类容易写错的细节只维护一份，
    也让"仓储层不 commit"这条纪律有唯一落点。
    """

    model: type[ModelT]  # 子类必须覆写：声明本仓储操作哪张表（如 User）

    def __init__(self, session: AsyncSession) -> None:
        """绑定请求级数据库会话。

        参数:
            session: 由 `app/db/session.py::get_session` 创建、经 FastAPI 依赖注入
                传下来的 AsyncSession。仓储既不新建也不关闭它。
                关键约束：**同一请求里的所有仓储必须共用这一个会话**，
                否则它们会落在不同事务里，Service 的一次 commit 只能提交其中一个，
                另一半静默丢失（这类 bug 极难复现，务必记住）。
        """
        self.session = session

    async def get(self, obj_id: int) -> ModelT | None:
        """按主键读取单个对象（优先命中身份映射，不一定发 SQL）。

        参数:
            obj_id: 主键值，正整数；不存在或已删除都走"返回 None"的分支。
        返回:
            命中返回 ORM 对象，未命中返回 None——**不抛异常**（见模块说明第 5 条），
            由 Service 决定要不要转成 NotFoundError。
        实现选择: 用 `session.get()` 而不是 `select(...).where(id == ...)`。
            get() 会先查 identity map（身份映射）：同一会话内已经加载过的对象
            直接返回，不再往返数据库；对象若已过期则顺手刷新。这在
            "更新后要拿最新值"的场景能省下一条 SELECT。
        现状说明: 目前三个 Service 都走带归属校验的 `get_owned` / `get_active`，
            本方法暂无调用方，属于基类给"新表 + 内部主键访问"预留的通用能力。
            面试若被追问"这个方法有人用吗"，如实说明即可；更要强调的是
            **不要用它读用户数据**——它只按主键取，绕开了 user_id 过滤。
        """
        return await self.session.get(self.model, obj_id)

    async def add(self, obj: ModelT) -> ModelT:
        """新增：flush 拿到自增主键，事务边界交给上层 Service 控制。

        参数:
            obj: 已构造好的 ORM 实例。字段值由 Service 负责填（含 user_id），
                仓储不做任何字段校验——校验属于业务规则，不属于数据访问。
        返回:
            同一个对象实例（不是新对象）；flush 之后 `obj.id` 已被数据库回填，
            后续逻辑（如用 user.id 去插默认分类、或返回给客户端）就能用到。
        为什么是 flush 而不是 commit: flush 只是把待执行的 INSERT 发给数据库
            并取回自增主键，事务仍然打开；提交由 Service 统一决定。
            若这里 commit，Service 就失去了把多个仓储调用合成一个原子操作的能力。
        现状说明: 当前 Service 多写成 `self.session.add(obj)` 再手工 `await flush()`
            （见 auth_service.register、transaction_service.create），
            与调用本方法完全等价——区别只是"谁显式写出了 flush 的时机"。
            保留本方法是为了让"新增并立刻拿到主键"这个意图有一处统一表达。
        """
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def delete(self, obj: ModelT) -> None:
        """删除：同样只 flush，真正的提交由 Service 完成。

        参数:
            obj: 必须是与当前会话关联的**持久态**对象（先 get 再删），
                传游离态实例会报错。
        副作用:
            ORM 先把对象标记为 deleted，flush 时才发 DELETE。跨表级联不在这里
            处理，而由模型上的 `ondelete="CASCADE"` / `passive_deletes`
            决定（见 models/category.py、models/user.py）。
        """
        await self.session.delete(obj)
        await self.session.flush()

    async def count(self, stmt: Select[Any]) -> int:
        """对任意过滤条件计数：包一层 subquery 保证 count 结果与列表条件一致。

        参数:
            stmt: 任意 `Select`，通常是列表查询用的那条语句（不带 limit/offset）。
        返回:
            行数，永不为 None：数据库返回 NULL 时兜底成 0，
            因此调用方不需要写 `or 0`。

        生成的 SQL 形如::

            SELECT count(*) FROM (SELECT ... WHERE ...) AS anon_1

        为什么用 subquery 而不是把 where 条件搬到 count 上: 后者需要把过滤条件
        在两个地方各写一遍，filter 一多必然出现"列表 30 条、总数 28 条"的错乱，
        分页页码随之全错。复用同一条语句是唯一的单一事实来源。
        代价：数据库要多做一层子查询包装（开销可忽略），但计数的语义是
        "扫描整个过滤结果集"，所以 offset 分页每翻一页都要重新付这份钱
        （见 transaction_repo.paginate 的取舍说明）。
        """
        total = await self.session.scalar(select(func.count()).select_from(stmt.subquery()))
        # scalar() 对无 GROUP BY 的 count 只会返回一行一列；仍加 `or 0` 兜底，
        # 防御空表/异常方言返回 NULL 的情况
        return int(total or 0)
