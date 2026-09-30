"""数据访问层。

在链路中的位置：
    `api/v1/*.py`（FastAPI 路由，只做参数校验与响应组装）
    → `services/*_service.py`（业务规则 + **事务边界**）
    → **`repositories/*_repo.py`（本包，全项目唯一直接写 SQL 的地方）**
    → `models/*.py`（ORM 表映射）→ PostgreSQL / SQLite。
    路由永远不直接 new 仓储；Service 在 `__init__` 里用注入进来的 AsyncSession
    构造它们。四个导出类的分工：BaseRepository 提供通用 CRUD，
    UserRepository 管登录/去重，CategoryRepository 管分类与引用计数，
    TransactionRepository 承担过滤、分页、聚合与批量写入（最重的一个）。

设计取舍 / 面试可讲：
1. **只做数据访问，不 commit（只 flush）**：flush 会把 INSERT/UPDATE 发给数据库
   并回填自增主键，但事务仍开着；提交/回滚由 Service 决定。因此
   「注册 = 插用户 + 批量插 10 条预置分类」这种多表写入天然落在同一个事务里，
   中途失败整体回滚，不会留下"有用户没分类"的脏数据。
   如果基类图省事自己 commit，Service 就没法把多个仓储调用绑成原子操作。
2. **只做数据访问，不抛业务异常**：查不到就返回 None，由 Service 判断后抛
   NotFoundError / CategoryNameConflictError 等领域异常。好处是同一套仓储
   能被记账、统计、CSV 导入导出、定时任务复用，它们不需要理解 HTTP 语义；
   坏处是"是否 404"这个判断在 Service 里重复出现，属于可接受的代价。
3. **集中加载策略，避免 N+1**：selectinload 只写在 transaction_repo.py 一处，
   Service 拿到对象时 category 已装好。模型里 relationship 设了 `lazy="raise"`
   （见 models/transaction.py），谁忘了预加载会立刻报错，而不是悄悄发出 N 条 SQL。
4. **显式 `__all__`**：本文件是包的对外契约，`from app.repositories import X`
   只认这四个名字；子模块名（base / user_repo / ...）属于实现细节，
   重命名时调用方不受影响，也让静态检查能发现"导出了不存在的名字"。
5. **代价**：多一层转发，最简单的 CRUD 看起来"绕了一圈"。换来的是业务规则
   与 SQL 各自可读、Service 单测可以注入假仓储不依赖数据库、
   以及所有越权风险点（user_id 条件）集中在少数几个文件里 review。

易混淆点：`app.models.base` 是 ORM 声明基类（Base / TimestampMixin），
`app.repositories.base` 是仓储基类（BaseRepository）——同名不同物，
搜索 `base` 时要看清是哪个包。
"""

# 绝对导入而非相对导入：`app` 包由 setuptools 的 src 布局安装进环境
# （测试侧另有 pyproject 的 pythonpath = ["src"]）。绝对导入让"谁依赖谁"
# 一眼可见，也避免模块被搬动时相对层级悄悄错位。
from app.repositories.base import BaseRepository
from app.repositories.category_repo import CategoryRepository
from app.repositories.transaction_repo import TransactionRepository
from app.repositories.user_repo import UserRepository

# 对外契约：只有这四个名字属于公开 API。`from app.repositories import *`
# 以及 ruff 的 F401「已导入但未使用」判断都以它为准（因此本文件里
# 只是转发的导入不会被告警）。
__all__ = ["BaseRepository", "CategoryRepository", "TransactionRepository", "UserRepository"]
