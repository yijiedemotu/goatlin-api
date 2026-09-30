"""ORM 模型聚合导出：Alembic autogenerate 依赖这里的导入来发现全部表。

在链路中的位置：
    models 是链路最底层，向上依次是 repositories（怎么查）→ services（做什么）→ api（对外契约）。
    alembic/env.py 里 `from app.models import Base` 拿到 target_metadata；
    app/db/init_db.py 同样只 import 这一个模块就完成建表。
    本模块是「所有表都注册进同一个 Base.metadata」的唯一保证点。

设计取舍 / 面试可讲：
1. **为什么必须集中导入**：SQLAlchemy 的 Base.metadata 是全局注册表，只有被 import 过的模型类才会
   在映射完成时把 Table 注册进去。少导入一个模型，autogenerate 生成的迁移就会静默少一张表，
   直到线上查询报表才暴露，属于典型的「沉默故障」。
2. **import 即副作用**：下面的导入语句没有被任何函数直接调用，它们的作用是触发建模。
   因此禁止「清理未使用导入」式的重构，也禁止给它们加 noqa 之外的删除理由。
3. **为什么不在这里做 create_all**：建表要么交给 Alembic（生产，可追溯可回滚），
   要么交给 init_db 脚本（本地开发），模型包只负责声明结构，不负责执行 DDL。
4. **utcnow 一并导出**：给脚本、测试和服务层提供统一的「取当前时间」入口，
   避免各处再写 datetime.now(UTC)，也避免出现已废弃的 datetime.utcnow。
5. 业务代码请直接从子模块导入（from app.models.user import User），
   走本模块会让依赖关系变模糊，也会放大循环导入的风险。
"""

from app.models.base import Base, TimestampMixin, utcnow
from app.models.category import Category
from app.models.transaction import Transaction
from app.models.user import User

__all__ = ["Base", "Category", "TimestampMixin", "Transaction", "User", "utcnow"]
