"""用户模型。密码只存 bcrypt 哈希，邮箱统一小写存储。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    # 只在类型检查期导入，运行时避免 User ↔ Category ↔ Transaction 的循环导入；
    # `from __future__ import annotations` 让下面的 Mapped[list[Category]] 变成字符串注解，
    # 所以运行时并不需要 Category 这个名字（SQLAlchemy 靠字符串解析 relationship 目标）。
    from app.models.category import Category
    from app.models.transaction import Transaction


class User(Base, TimestampMixin):
    """用户表：账号、凭据与状态，是多租户隔离的锚点。

    生命周期：注册（AuthService.register）→ 登录/刷新令牌（authenticate / refresh）→
    可选封禁（is_active=False）。几乎不被物理删除；即便删除，
    数据库的 ON DELETE CASCADE 会清掉该用户的分类与账单。

    为什么独立成类：账号是唯一需要「凭据 + 唯一性 + 启用状态」三类约束的主体，
    与账目数据（Transaction）的关注点和生命周期完全不同，混在一起会让两张表都被迫放宽约束。
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, comment="用户ID")
    # unique=True + index=True：SQLAlchemy 合并成一个唯一索引（迁移里是 ix_users_email），
    # 既服务登录查询，也是「同一邮箱并发注册」的最后一道防线（IntegrityError 兜底）
    email: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False, comment="登录邮箱(小写)"
    )
    nickname: Mapped[str] = mapped_column(String(50), nullable=False, default="", comment="昵称")
    # 只存 bcrypt 哈希，永不存明文；该字段没有任何接口能返回，UserOut 里刻意不含它
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False, comment="bcrypt 哈希")
    # default 走 ORM 插入路径，server_default="1" 兜住绕过 ORM 的 Core/原生 SQL 插入；
    # SQLite 没有原生布尔类型，库里落地的就是 0/1，因此这里用 "1" 而不是 True
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1", comment="是否可用"
    )

    # lazy="raise"：禁止隐式懒加载。异步下访问未加载的关系会抛 MissingGreenlet（难以定位），
    # 所以这里把它升级成「一访问就报错」，强制调用方显式写 selectinload(...)，
    # 从而把 N+1 问题暴露在开发期而不是压测期。
    categories: Mapped[list[Category]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )
    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        """调试表示：只包含 id 与 email，绝不输出 password_hash。"""
        # 故意只输出 id 与 email：日志里绝不能出现 password_hash
        return f"<User id={self.id} email={self.email}>"
