"""业务逻辑层（Service）：编排用例、决定事务边界；只依赖 Repository 与领域异常，不感知 HTTP。

在链路中的位置：
    api/v1/*（路由：解析 HTTP 入参、依赖注入、鉴权）
        → services/*（本层：业务规则 + 用例编排 + 唯一的 commit 出口）
        → repositories/*（数据访问：只 flush 不 commit）
        → models/*（SQLAlchemy ORM 映射）→ 数据库

设计取舍 / 面试可讲：
1. **事务边界放在 Service 而不是 Repository**：Repository 只 flush（发出 SQL、回填自增主键），
   commit 由用例决定。收益是一个用例里的多表写入天然同事务（如注册的「用户 + 10 条分类」）；
   代价是每个写用例都必须自己收尾 commit，漏写就会出现「接口返回成功但数据没落库」。
2. **本层不 import fastapi**：错误一律抛 app.core.exceptions 的 AppError 子类，
   由全局异常处理器翻译成 HTTP 状态码与 code。收益是同一套业务逻辑能被脚本、定时任务复用；
   代价是多一层异常映射，需要保证异常子类与路由文档里的 responses 对得上。
3. Service 是「每请求一个实例」的短生命周期对象（路由里直接 `AuthService(session)`），
   不做单例、不存缓存：实例状态只有 session 与 Repository，避免跨请求共享会话导致数据串号。
4. **按用例而不是按表切分**（认证/分类/账单/统计/CSV 各一个类）：
   收益是每个文件能独立读懂、独立单测；代价是出现跨领域编排时要 Service 调 Service。
5. **口径收敛在本层**：金额用 Decimal 量化、账期解析成 [start, end] 闭区间、
   「越权返回 404 而不是 403」这类规则都写在这里，避免同一规则在路由与仓储里各写一份。
6. 这里只做 re-export，不含任何逻辑：`from app.services import XxxService` 是调用方唯一入口，
   import 图保持单向（services 依赖 repositories，反过来不成立）。"""

from app.services.auth_service import AuthService
from app.services.category_service import CategoryService
from app.services.csv_service import CsvService
from app.services.stats_service import StatsService
from app.services.transaction_service import TransactionService

__all__ = [
    "AuthService",
    "CategoryService",
    "CsvService",
    "StatsService",
    "TransactionService",
]
