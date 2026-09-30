"""账单业务。"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BadRequestError, NotFoundError
from app.models.transaction import Transaction
from app.repositories.category_repo import CategoryRepository
from app.repositories.transaction_repo import TransactionRepository
from app.schemas.transaction import TransactionCreate, TransactionQuery, TransactionUpdate


class TransactionService:
    """账单用例编排：校验、增删改查、分页列表。

    生命周期：每请求一个实例，与请求共享同一个 AsyncSession（事务边界 = session 边界）。

    为什么独立成类：账单是「跨表写入 + 跨表校验」的典型用例 ——
    写 transactions 却要读 categories 做归属与方向校验；
    这类规则放在路由里会让路由膨胀，放在仓储里则会让数据访问层混入业务语义。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.transactions = TransactionRepository(session)
        # 与账单仓储共用同一 session：校验读取与后续写入处在同一事务/同一份快照，
        # 避免「校验时分类还在、写入前被删掉」的窗口（SQLite / 单库下由事务隔离保证）。
        self.categories = CategoryRepository(session)

    # ------------------------------------------------------------------
    async def _validate_category(self, user_id: int, category_id: int | None, kind: str) -> None:
        """校验分类归属与收支方向一致性，防止把「工资」挂到支出上。

        参数：
            user_id: 当前登录用户 id（来自 JWT，不接受客户端传入）。
            category_id: 待绑定的分类 id；None 表示「未归类」，直接放行。
            kind: 账单的收支方向（"income" / "expense"）。

        抛出：
            NotFoundError（404 / NOT_FOUND）：分类不存在，或存在但不属于当前用户。
                两种情形刻意合并 —— 这正是「水平越权返回 404 而不是 403」的落地点。
            BadRequestError（400 / BAD_REQUEST）：分类方向与账单方向不一致，
                detail 里带上 category_kind 与 transaction_kind，前端可直接提示原因。

        私有方法（下划线前缀）：只是本类的内部不变量，不给路由调用。
        """
        if category_id is None:
            return
        category = await self.categories.get_owned(category_id, user_id)
        if category is None:
            raise NotFoundError("分类不存在或不属于当前用户")
        if category.kind != kind:
            raise BadRequestError(
                "分类的收支类型与账单不一致",
                detail={"category_kind": category.kind, "transaction_kind": kind},
            )

    async def create(self, user_id: int, payload: TransactionCreate) -> Transaction:
        """新增一笔账单。

        参数：payload 已由 Pydantic 校验（金额 Decimal 且 >0、账期不晚于今天、
        分类可为空、备注已 strip）。

        返回：带 category 关联的 Transaction（供 TransactionOut 序列化）。

        抛出：NotFoundError（404）、BadRequestError（400），均由 _validate_category 抛出。

        关键实现选择：
        1. **校验在写库之前**：先确认分类归属与方向，再构造 Transaction，
           请求不合法时数据库里不留任何痕迹。
        2. **source 固定为 "manual"**：与 CSV 导入（"csv"）区分来源，
           将来做「只删导入数据」或数据溯源时有用。
        3. **flush + commit 后重新查询**：flush 只为拿自增 id（后续查询要用），
           重新 get_owned 是为了用 selectinload 把 category 一起装进响应，
           保证创建接口与列表接口的返回结构完全一致（前端同一套渲染逻辑）。
        """
        kind = payload.kind.value
        await self._validate_category(user_id, payload.category_id, kind)

        tx = Transaction(
            user_id=user_id,
            category_id=payload.category_id,
            amount=payload.amount,
            kind=kind,
            occurred_on=payload.occurred_on,
            note=payload.note,
            source="manual",
        )
        self.session.add(tx)
        await self.session.flush()
        await self.session.commit()

        # 重新查询以带上 category 关联，保证响应结构与列表接口一致
        created = await self.transactions.get_owned(tx.id, user_id)
        assert created is not None
        return created

    async def list(
        self, user_id: int, query: TransactionQuery, *, page: int, size: int
    ) -> tuple[Sequence[Transaction], int]:
        """分页查询账单列表。

        参数：
            query: 已由 Pydantic 校验的过滤器（日期区间、类型、分类、金额区间、备注关键词），
                区间合法性（起 <= 止、min <= max）也在 schema 层拦掉。
            page / size: 强制关键字参数，避免调用方把页码与每页条数位置写反；
                size 上限 100 由路由的 Query(le=100) 控制。

        返回：(当页 Transaction 列表, 满足条件的总条数)。总数与列表复用同一套过滤条件，
        否则前端分页器会出现「总数 50 但翻到第 3 页空白」的经典 bug。

        本方法不做归属校验之外的业务判断：user_id 由路由从 JWT 注入，
        仓储层的第一条 where 就是 `Transaction.user_id == user_id`，从 SQL 层面杜绝越权。
        """
        return await self.transactions.paginate(user_id, query, page=page, size=size)

    async def get(self, user_id: int, transaction_id: int) -> Transaction:
        """取单条账单（带 category 关联），不存在或不属于当前用户都抛 404。

        抛出：NotFoundError（404 / NOT_FOUND）。

        关键实现选择：get_owned 把 user_id 也作为 SQL 过滤条件，
        于是「不存在」与「不是你的」在这里合并成同一个 404 ——
        攻击者无法用状态码枚举出系统里存在哪些账单 id（403 等于确认资源存在）。
        update / delete 都复用本方法，越权语义因此只有一处定义。
        """
        tx = await self.transactions.get_owned(transaction_id, user_id)
        if tx is None:
            raise NotFoundError("账单不存在")
        return tx

    async def update(self, user_id: int, transaction_id: int, payload: TransactionUpdate) -> Transaction:
        """部分更新账单（PATCH 语义：字段为 None 表示不改，至少传一个由 schema 校验保证）。

        抛出：
            NotFoundError（404 / NOT_FOUND）：账单不存在、不属于当前用户，
                或新分类不存在/不属于当前用户（均由 _validate_category 抛出）。
            BadRequestError（400 / BAD_REQUEST）：新分类的收支方向与账单方向不一致。

        关键实现选择 —— 下面三个分支的顺序与条件是核心：
        1. `new_kind = payload.kind.value if payload.kind else tx.kind`：
           先算出「更新后的最终方向」，因为校验必须针对最终状态而不是当前状态。
        2. 传了 category_id：用 new_kind 校验新分类，通过后再赋值。
        3. **没传 category_id 但改了 kind**：拿**原有分类**与 new_kind 再校验一次。
           这是最容易漏掉的分支 —— 例如把一笔「餐饮(expense)/支出」改成收入，
           分类没变但已经矛盾；不拦下来，库里就会出现「收入账单挂在支出分类下」，
           之后所有分类占比、环形图统计都不可信。
        4. 校验与赋值都在同一事务内、commit 之前完成，校验失败时对象保持原状。
        5. 返回前重新 get_owned：用 selectinload 把 category 关联与最新值装进响应，
           与 create 的返回结构保持一致（前端同一套渲染逻辑）。
        """
        tx = await self.get(user_id, transaction_id)

        new_kind = payload.kind.value if payload.kind else tx.kind
        if payload.category_id is not None:
            await self._validate_category(user_id, payload.category_id, new_kind)
            tx.category_id = payload.category_id
        elif payload.kind and payload.kind.value != tx.kind:
            # 只改收支方向但保留原分类 → 可能出现分类与方向不匹配，提前拦截
            await self._validate_category(user_id, tx.category_id, new_kind)

        if payload.amount is not None:
            tx.amount = payload.amount
        if payload.kind is not None:
            tx.kind = new_kind
        if payload.occurred_on is not None:
            tx.occurred_on = payload.occurred_on
        if payload.note is not None:
            tx.note = payload.note

        await self.session.commit()
        refreshed = await self.transactions.get_owned(tx.id, user_id)
        assert refreshed is not None
        return refreshed

    async def delete(self, user_id: int, transaction_id: int) -> None:
        """删除一笔账单（物理删除，不是软删）。

        抛出：NotFoundError（404 / NOT_FOUND）：账单不存在或不属于当前用户。

        关键实现选择：
        1. **物理删除**：账单是用户私有数据，删了就是删了，没有审计或合规要求。
           软删（加 deleted_at 标记）会让每个查询、每个聚合都要额外过滤一次，
           成本高于收益；真要可恢复应该靠导出备份，而不是让业务表永久背负墓碑行。
        2. **复用 get() 拿到实体**：多带了一次 selectinload(category) 的关联查询，
           换来的是越权判断只有一处定义（404 语义不会在增删改之间跑偏），属于划算的取舍。
        3. commit 之前不显式 flush：ORM 的 delete 本来就在 flush 时发 SQL，
           这里一次性 commit 完成；异常由 db/session.py 的 get_session 兜底 rollback。
        """
        tx = await self.get(user_id, transaction_id)
        await self.transactions.delete(tx)
        await self.session.commit()
