"""分类业务。"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import CategoryInUseError, CategoryNameConflictError, NotFoundError
from app.models.category import Category
from app.repositories.category_repo import CategoryRepository
from app.schemas.category import CategoryCreate, CategoryUpdate


class CategoryService:
    """分类用例编排：列表、新建、改名改色、删除保护。

    生命周期：每个请求由路由新建一次（`CategoryService(session)`），
    与请求共享同一个 AsyncSession，因此本类内所有 Repository 调用天然处于同一事务。

    为什么独立成类：分类的业务规则不在 SQL 里，而在「能不能删、能不能重名」这些判断上；
    把这些规则集中在 Service，Repository 保持纯查询、路由保持纯 HTTP，
    三层的测试可以各自独立（分类规则单测不需要起 FastAPI）。
    """

    def __init__(self, session: AsyncSession) -> None:
        # 注意：两个 Repository 复用同一个 session —— 事务边界是 session 级别的，
        # 用两个 session 的话「查重 + 插入」就不再是同一事务，唯一约束的兜底意义会打折。
        self.session = session
        self.categories = CategoryRepository(session)

    async def list_categories(
        self, user_id: int, *, kind: str | None = None, with_usage: bool = False
    ) -> Sequence[Category]:
        """列出当前用户的分类。

        参数：
            user_id: 只可能来自 JWT 解析出的当前用户，绝不接受请求体传入（否则就是越权入口）。
            kind: "income" / "expense" 过滤，None 表示全部。
            with_usage: True 时用 LEFT JOIN 聚合出每个分类的账单数，
                挂到瞬态属性 usage_count 上；前端 CategoriesView 用它显示「N 笔账单」。

        返回：Category 序列（with_usage=True 时额外带 usage_count 属性）。
        只读，不 commit。
        """
        return await self.categories.list_by_user(user_id, kind=kind, with_usage=with_usage)

    async def create(self, user_id: int, payload: CategoryCreate) -> Category:
        """新建用户自定义分类。

        抛出：CategoryNameConflictError（HTTP 409 / code=CATEGORY_NAME_CONFLICT），
        同一用户下 (name, kind) 已存在，或并发插入被唯一索引拦下。

        关键实现选择：
        1. 预检查 + 唯一索引双保险：预检查负责「99% 的情况给出友好错误」，
           数据库唯一索引负责「并发下也绝不出现两条同名同类型」。
        2. is_system 强制 False：用户永远无法通过接口造出「系统分类」来绕过删除保护。
        3. flush 而不是 commit 来拿自增 id，commit 留在本方法末尾 —— 事务只有一处出口。
        """
        if await self.categories.get_by_name(user_id, payload.name, payload.kind.value):
            raise CategoryNameConflictError()
        category = Category(
            user_id=user_id,
            name=payload.name,
            kind=payload.kind.value,
            color=payload.color,
            icon=payload.icon,
            is_system=False,
        )
        self.session.add(category)
        try:
            await self.session.flush()
        except IntegrityError as exc:  # 并发创建同名分类 → 唯一约束兜底
            # 必须 rollback：语句级失败后事务处于不可继续状态（PostgreSQL 会直接拒绝后续语句），
            # 且此时事务里也没有其它待提交的写入，整体回滚是安全的。
            await self.session.rollback()
            raise CategoryNameConflictError() from exc
        await self.session.commit()
        # refresh 重新从库里读一遍，拿到数据库侧生成的默认值与时间戳，保证响应字段完整
        await self.session.refresh(category)
        return category

    async def update(self, user_id: int, category_id: int, payload: CategoryUpdate) -> Category:
        """改名 / 改色 / 改图标（部分更新，未传的字段保持不变）。

        抛出：
            NotFoundError（404 / NOT_FOUND）：分类不存在或不属于当前用户。
            CategoryNameConflictError（409 / CATEGORY_NAME_CONFLICT）：新名字与同用户下
                同类型的另一个分类重名。

        关键实现选择：
        1. **只在确实改名时才查重**：`payload.name != category.name` 这个条件很关键 ——
           否则用户只改颜色时，会因为「查到自己」而误报重名。
        2. **不允许改 kind**：CategoryUpdate 里没有 kind 字段。方向变了会让该分类下
           已存在的账单与新方向矛盾（历史上「餐饮」是支出，改成收入后旧账单全部违规），
           正确做法是新建一个分类再迁移账单。
        3. **改名不做 flush 异常捕获**：改的是已存在行，唯一索引冲突在 commit 时才可能触发；
           这里选择依赖预检查（同用户并发改名的概率极低，且失败方会得到 500 而不是 409）。
           更严谨的做法是在 commit 外同样包一层 IntegrityError → 409。
        """
        category = await self.categories.get_owned(category_id, user_id)
        if category is None:
            raise NotFoundError("分类不存在")

        # 只在「确实改了名字」时才查重：payload.name == category.name 时跳过，
        # 否则用户只改颜色也会因为查到自己而误报 CATEGORY_NAME_CONFLICT
        if payload.name and payload.name != category.name:
            if await self.categories.get_by_name(user_id, payload.name, category.kind):
                raise CategoryNameConflictError()
            category.name = payload.name
        if payload.color:
            category.color = payload.color
        if payload.icon:
            category.icon = payload.icon

        await self.session.commit()
        await self.session.refresh(category)
        return category

    async def delete(self, user_id: int, category_id: int) -> None:
        """删除分类，任何一条保护命中都会拒绝并说明原因。

        抛出：
            NotFoundError（404 / NOT_FOUND）：分类不存在或不属于当前用户。
            CategoryInUseError（409 / CATEGORY_IN_USE）：系统预置分类（消息为固定文案），
                或该分类下仍有账单（消息里带上条数与「先迁移或删除」的建议）。

        关键实现选择：
        1. **先判 is_system 再数账单**：系统分类无论如何都不可删，
           先数一遍账单只会多一次无意义的查询，而且提示信息会误导用户去迁移账单。
        2. **不在业务层做级联**：models/category.py 里外键是 ON DELETE SET NULL、
           关系是 passive_deletes=True，但业务上不允许走到那一步 ——
           本方法在 DELETE 之前就把「有引用」拦成 409；数据库的 SET NULL
           只是「有人绕过业务层直接改库」时的最后兜底。
        3. 前端 CategoriesView 会把 409 的 message 原样透出（含条数与建议），
           所以这里是用户可见文案，改文案要同步检查前端提示是否还通顺。
        """
        category = await self.categories.get_owned(category_id, user_id)
        if category is None:
            raise NotFoundError("分类不存在")
        # 系统分类是「新用户首屏就有数据」的基础，宁可报错也不允许删掉；
        # 放在数账单之前，避免多一次无意义查询、也避免误导用户去迁移账单
        if category.is_system:
            raise CategoryInUseError("系统预置分类不可删除")

        # 数一下引用它的账单：非 0 就报 409 并带上条数（消息见下一行），
        # 让用户主动迁移，而不是由系统替他决定这些账单的去向
        used = await self.categories.count_transactions(category_id)
        if used:
            # 不静默级联：明确告诉用户先迁移账单，避免统计口径被悄悄改变
            raise CategoryInUseError(f"该分类下还有 {used} 笔账单，请先迁移或删除后再试")

        await self.categories.delete(category)
        await self.session.commit()
