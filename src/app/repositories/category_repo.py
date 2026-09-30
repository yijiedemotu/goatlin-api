"""分类仓储。"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select

from app.models.category import Category
from app.repositories.base import BaseRepository


class CategoryRepository(BaseRepository[Category]):
    """categories 表的查询入口。

    生命周期：请求级实例，由 CategoryService / TransactionService / CsvService
    现场构造，共享同一个 AsyncSession（因此同请求内的多次写入处于同一事务）。
    无状态、无缓存——CsvService 那层"分类缓存"是它自己在请求内建的局部字典。

    为什么独立成类：分类是账单的维度表，既有"列表 + 用量"这类展示查询，
    又承担越权校验和引用计数，两者都不属于通用 CRUD，集中在这里才能
    保证所有查询都带 `user_id`。
    """

    model = Category  # 声明操作 categories 表；基类的 get/add/delete/count 依赖它

    async def list_by_user(
        self, user_id: int, *, kind: str | None = None, with_usage: bool = False
    ) -> Sequence[Category]:
        """按用户列出分类；with_usage=1 时统计每个分类下的账单数（一次 JOIN 聚合，避免 N+1）。

        参数:
            user_id: 当前登录用户主键（来自 `CurrentUser.id`），永远作为 WHERE 的
                第一个条件——列表接口漏掉用户过滤就是全站数据泄漏。
            kind: "income" / "expense"，None 表示不过滤（前端「全部」选项）。
                这里收的是裸字符串而非枚举，因为上层已经用 `kind.value` 拆好了。
            with_usage: 是否附带 `usage_count`。前端分类管理页传 True；
                记账页的分类选择器传 False（不需要计数，省掉一次聚合）。
        返回:
            分类列表，固定按 `(kind, id)` 排序——不写 order_by 时数据库返回顺序
            不保证，分页和展示都会抖动。
            当 with_usage=True 时每个对象额外挂了**瞬态属性** `usage_count`：
            它不是 mapped_column，不会被写库、也不出现在 SQL 里，
            只是给序列化层读的临时字段（api/v1/categories.py 用
            `getattr(category, "usage_count", 0)` 取值——所以 with_usage=False
            时也不会 AttributeError）。
        复杂度 / 索引:
            with_usage=False → 1 条 SQL，过滤条件 (user_id[, kind]) 命中复合索引
                `ix_categories_user_kind`；
            with_usage=True → 仍是 1 条 SQL，但内含一个按 category_id 分组的
                子查询，要扫一遍该用户的全部账单——这就是它默认关闭的原因。
        """
        if not with_usage:
            # 轻量路径：完全不碰 transactions 表，只查分类本身
            stmt = select(Category).where(Category.user_id == user_id)
            if kind:
                # 只有 kind 有值才追加过滤；kind=None 表示"收入 + 支出都要"
                # （前端「全部」选项就是不加这个条件）
                stmt = stmt.where(Category.kind == kind)
            return (await self.session.scalars(stmt.order_by(Category.kind, Category.id))).all()

        # 延迟导入：只有走到"统计用量"这条分支才需要 func 与 transactions 表
        from sqlalchemy import func

        from app.models.transaction import Transaction

        usage = (
            select(Transaction.category_id, func.count().label("cnt"))
            .where(Transaction.user_id == user_id)
            .group_by(Transaction.category_id)
            .subquery()
        )
        # 外层用 LEFT JOIN（不是 INNER JOIN）：没有账单的分类也必须出现在列表里，
        # 此时 cnt 为 NULL，由 coalesce 兜成 0
        stmt2 = (
            select(Category, func.coalesce(usage.c.cnt, 0).label("usage_count"))
            .outerjoin(usage, usage.c.category_id == Category.id)
            .where(Category.user_id == user_id)
        )
        if kind:
            stmt2 = stmt2.where(Category.kind == kind)
        # 用 execute 而不是 scalars：这里每行要取 (Category, usage_count) 两个值
        result = await self.session.execute(stmt2.order_by(Category.kind, Category.id))
        categories: list[Category] = []
        for category, usage_count in result.all():
            # 用瞬态属性挂载统计值，避免为展示再造一个模型
            # 瞬态属性不会进 ORM 的变更追踪，赋值只写在 Python 对象上，不会产生 UPDATE
            category.usage_count = int(usage_count)
            categories.append(category)
        return categories

    async def get_owned(self, category_id: int, user_id: int) -> Category | None:
        """带归属校验的查询：防止越权访问他人分类（水平越权）。

        参数:
            category_id: 路径/请求体里的分类主键，**客户端可控、不可信**。
            user_id: 服务端从 token 解析出的当前用户，可信。
        返回:
            只有"存在且属于该用户"才返回对象，否则统一返回 None。
        为什么把 user_id 写进 WHERE 而不是查完再比: 两种失败情况对外表现
            完全一致，攻击者无法通过响应差异枚举出别人的 category_id 是否有效。
        索引命中: 主键定位 + user_id 过滤，走主键索引回表后顺带判断，
            成本可忽略。
        """
        stmt = select(Category).where(Category.id == category_id, Category.user_id == user_id)
        return await self.session.scalar(stmt)

    async def get_by_name(self, user_id: int, name: str, kind: str) -> Category | None:
        """同名检查：(user_id, name, kind) 三个字段一起比，与数据库唯一约束完全对齐。

        参数:
            name: 分类名，区分大小写与空格（落库前由 Schema 层 strip）。
            kind: 必须传——「餐饮」可以同时存在为支出分类和收入分类，
                只比 name 会误报冲突。
        返回:
            命中返回已存在的 Category（Service 据此抛 409），否则 None。
        索引命中: `uq_categories_user_name_kind` 唯一索引正好覆盖这三个字段，
            等值查询做唯一索引扫描，命中即停。
        局限: 仍是 check-then-act，挡不住并发；真正的兜底是同一个唯一约束
            抛出的 IntegrityError（见 category_service.create 的 except 分支）。
        """
        stmt = select(Category).where(
            Category.user_id == user_id, Category.name == name, Category.kind == kind
        )
        return await self.session.scalar(stmt)

    async def count_transactions(self, category_id: int) -> int:
        """统计该分类被多少笔账单引用（删除分类前的安全检查）。

        参数:
            category_id: 目标分类主键。这里**不再带 user_id**：调用方
                （CategoryService.delete）已先用 `get_owned` 确认了归属，
                重复条件只会多扫一个索引列。
        返回:
            引用条数；0 表示可以安全删除。
        索引命中: transactions.category_id 上有普通索引（见 models/transaction.py），
            这类 `count(*)` 只需要索引就能算完（covering index），不必回表读整行；
            是否真的走索引由优化器决定，但至少索引是存在的。
        安全提醒: 这是本仓储里**唯一不带 user_id 条件**的方法。新增调用方时
            必须自己先用 `get_owned` 做归属校验，否则会变成"能数别人分类下有多少账单"
            的信息泄漏（金额统计同理，`group_by_category` 是带 user_id 的）。
        设计意图: 返回值 > 0 时 Service **拒绝删除**并要求先迁移账单，
            而不是靠 `ON DELETE SET NULL` 静默把账单变成"未归类"——
            那会悄悄改变用户的统计口径。数据库约束仍留作最后防线。
        """
        # 延迟导入：只有"删除前校验"这条路径才需要 func 与 transactions 表
        from sqlalchemy import func

        from app.models.transaction import Transaction

        total = await self.session.scalar(
            select(func.count()).select_from(Transaction).where(Transaction.category_id == category_id)
        )
        # count 在零行时返回 0 而非 NULL，这里的 or 0 只是防御性兜底
        return int(total or 0)
