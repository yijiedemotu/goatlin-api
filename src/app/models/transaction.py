"""账单模型（核心业务表）。

两个关键设计：金额用 Numeric(12,2) 而非 Float（避免浮点累计误差）；
账期 occurred_on 与写入时间 created_at 分离（支持补录历史账单）。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Date, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.category import Category
    from app.models.user import User


class Transaction(Base, TimestampMixin):
    """一条账单：谁、在哪天、花了/赚了多少钱、属于哪个分类。

    生命周期：由 TransactionService 创建/更新/删除（CSV 导入走 bulk_create 批量插入），
    一旦写入便只做「读多写少」的访问；删除是物理删除，没有软删除标记
    （分类被删的场景才用 category_id 置空来保住账目，见模块 docstring 第 7 条）。

    为什么独立成类并成为核心表：它是唯一持续增长的表，所有统计口径都以它为事实来源；
    与 Category 分开后，分类的增删改都不会让这张大表发生行级变更。
    """

    __tablename__ = "transactions"
    __table_args__ = (
        # 统计接口的主查询：WHERE user_id = ? AND occurred_on BETWEEN ? AND ?
        # 列顺序不能反：索引只能从最左列开始匹配，「日期 + 用户」的顺序用不上这个索引
        Index("ix_transactions_user_occurred", "user_id", "occurred_on"),
        # 覆盖「加一个 kind 过滤」的变体，kind 放在 occurred_on 之前便于等值列先定位
        Index("ix_transactions_user_kind_occurred", "user_id", "kind", "occurred_on"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # CASCADE：账号是账目的归属主体，注销即清空其数据
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # SET NULL + nullable：分类被删除时账单必须留存，历史金额不可丢，只丢归类信息。
    # 统计接口用 LEFT JOIN + coalesce 把 NULL 显示成「未归类」（见 transaction_repo）。
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 金额恒为正，方向由 kind 决定；Numeric 保证精确十进制，绝不退化为 float
    amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, comment="金额，恒为正数，方向由 kind 决定"
    )
    kind: Mapped[str] = mapped_column(String(8), nullable=False, comment="income | expense")
    # 账期 = 业务发生日（用户填写的「哪一天」），与 created_at（落库时间）严格分离：
    # 补录三个月前的账单时，统计口径按 occurred_on 归属到历史月份，而审计看 created_at
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False, comment="账期(业务日期)")
    # Text 而非 String(n)：备注长度上限由 schema 层限制（500），数据库不设人为上限
    note: Mapped[str] = mapped_column(Text, nullable=False, default="", comment="备注")
    # 记录数据来源，便于排查「这笔账是谁写的」：手工录入 / CSV 导入 / 开放接口
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="manual", comment="manual | csv | api"
    )

    # 两个关系都设 lazy="raise"：列表接口必须显式 selectinload(Transaction.category)，
    # 否则「列出账单 + 每条取分类名」会退化成 1 + N 次查询（详见 transaction_repo 的说明）。
    user: Mapped[User] = relationship(back_populates="transactions", lazy="raise")
    category: Mapped[Category | None] = relationship(back_populates="transactions", lazy="raise")

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        """调试表示：金额 + 方向 + 账期，足够判断这是哪一笔账，且不含用户隐私。"""
        return f"<Transaction id={self.id} {self.kind} {self.amount} on {self.occurred_on}>"
