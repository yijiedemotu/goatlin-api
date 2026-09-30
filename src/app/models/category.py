"""分类模型。

唯一约束是 (user_id, name, kind)：同一用户下同类型不允许重名，
但「报销」这类名字可以同时存在于收入与支出。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.transaction import Transaction
    from app.models.user import User


class Category(Base, TimestampMixin):
    """用户自有的收支分类（支出分类与收入分类共用一张表，用 kind 区分）。

    生命周期：注册时批量预置（is_system=True）→ 用户可增删改 → 被账单引用后不可删除
    （CategoryService.delete 会先数 count_transactions，拒绝而不是偷偷级联）。

    为什么与 Transaction 分开建表：分类是低频写入、高频引用的枚举型数据，
    独立成表才能让「重命名分类」不触碰任何一条账单，也让聚合查询可以 LEFT JOIN 出展示信息。
    """

    __tablename__ = "categories"
    __table_args__ = (
        # (user_id, name, kind) 三列唯一：同一用户的同类型分类不允许重名
        UniqueConstraint("user_id", "name", "kind", name="uq_categories_user_name_kind"),
        # 覆盖列表接口最常见的过滤组合 WHERE user_id = ? AND kind = ?
        Index("ix_categories_user_kind", "user_id", "kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 单列外键不建索引时，user_id 上的查询与 CASCADE 删除都会全表扫描，所以显式 index=True
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(32), nullable=False, comment="分类名，如「餐饮」")
    kind: Mapped[str] = mapped_column(String(8), nullable=False, comment="income | expense")
    # 展示属性放在后端：前端直接拿到就可渲染，不需要维护一份「分类色卡」映射表
    color: Mapped[str] = mapped_column(String(9), nullable=False, default="#6366F1", comment="前端展示色")
    icon: Mapped[str] = mapped_column(String(32), nullable=False, default="wallet", comment="图标标识")
    is_system: Mapped[bool] = mapped_column(
        default=False, nullable=False, comment="注册时预置的系统分类不可删除"
    )

    user: Mapped[User] = relationship(back_populates="categories", lazy="raise")
    # passive_deletes=True：删除分类时不再逐条 UPDATE 关联账单，直接交给数据库的 SET NULL 完成，
    # 避免 ORM 先把所有关联账单加载进内存再改写（分类下账单很多时差别很明显）。
    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="category", lazy="raise", passive_deletes=True
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        """调试表示：输出 id、名称与方向，便于在日志里区分同类对象。"""
        return f"<Category id={self.id} name={self.name} kind={self.kind}>"
