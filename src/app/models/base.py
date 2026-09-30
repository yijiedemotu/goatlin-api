"""ORM 基类：统一命名约定、公共时间戳。

命名约定（naming_convention）让 Alembic 生成稳定可回滚的约束名。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 约束类型 → 模板字符串，占位符由 SQLAlchemy 在编译 DDL 时替换：
# ix=普通索引 uq=唯一约束 ck=CHECK fk=外键 pk=主键
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """所有 ORM 模型的基类：注册表 + metadata 容器。

    职责：继承它即完成「Python 类 ↔ 数据库表」的映射声明；每个子类在类体结束时
    自动把自己注册进 declarative registry（因此同名表重复定义会在导入期直接报错，
    属于我们想要的快速失败）。生命周期与进程一致，不做实例化。

    为什么不把命名约定散在各个模型里：约定必须对所有表生效，写在这里只需一处，
    且 SQLAlchemy 2.x 的 MetaData(naming_convention=...) 只在 metadata 构造时读取一次。
    """

    # 每个模型文件各自的约束都会被这套规则命名；Alembic 的 op.f() 也读取同一份约定
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    """时区感知的当前 UTC 时间（禁止使用已废弃的 datetime.utcnow）。

    返回：带 tzinfo=UTC 的 datetime，可直接与数据库里 DateTime(timezone=True) 的值比较。
    为什么不用 datetime.utcnow()：它返回 naive datetime，写入 PG 的 timestamptz 时会被按
    服务器时区解释，跨时区部署必然偏移；该函数已发出 DeprecationWarning 并计划移除。
    取舍：统一用 UTC 存储、展示时再按用户时区换算，避免「同一笔账单在不同服务器时区属于不同月份」。
    """
    return datetime.now(UTC)


class TimestampMixin:
    """created_at / updated_at 公共时间戳 Mixin：数据库与服务端双重保证。

    为什么做成 Mixin 而不是放进 Base：有些表只关心创建时间、有些表根本不需要时间戳，
    塞进 Base 会让所有表都被迫多两列（还会让 Base 承担映射之外的责任）。
    Mixin 不参与 MRO 的实例化，只是把两列「复制」进每个使用它的模型，所以每个模型
    仍然只有单表结构，没有额外继承层级。

    双重保证的具体分工（面试可讲）：
    - `default=utcnow`：ORM 在 INSERT/UPDATE 前用 Python 时间填充，值由应用决定，
      便于测试自由控制时间，也不依赖数据库时钟；
    - `server_default=func.now()`：绕过 ORM 的直接 SQL / 批量 Core INSERT（CSV 导入、
      预置分类用的就是 Core insert）也不会留下 NULL；
    - `onupdate=utcnow`：只在 ORM 发 UPDATE 时触发；原生 SQL 更新不会自动刷新，
      因此「最后修改时间」更准确的来源是业务层主动赋值。
    - `DateTime(timezone=True)`：存带时区语义的值，避免 naive/aware 混用导致的比较错误。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,  # ORM 侧默认值：应用时区统一由这里决定
        server_default=func.now(),  # DB 侧兜底：绕过 ORM 的写入也不会为 NULL
        nullable=False,
        comment="创建时间(UTC)",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,  # ORM UPDATE 时自动刷新；Core/原生 SQL 更新不会触发
        server_default=func.now(),
        nullable=False,
        comment="更新时间(UTC)",
    )
