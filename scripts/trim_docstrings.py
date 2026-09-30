"""把冗长的模块 docstring 替换成精简版，详细讲解迁移到 docs/LEARN.md。

背景：这些 docstring 其实是"写在源码里的教材"（有的长达 50 行），
对学习者友好，但不符合一个正常工程项目的代码风格。精简后：
- 源码里只留一句话职责说明 + 必要的踩坑警告；
- 完整的分层讲解、设计取舍、面试话术全部写进 docs/LEARN.md。

只处理**文件开头第一个 docstring**，函数/类的 docstring 一概不动。

用法：
    python scripts/trim_docstrings.py --dry-run
    python scripts/trim_docstrings.py
"""

from __future__ import annotations

import argparse
import io
import sys
import tokenize
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SRC = Path(__file__).resolve().parents[1] / "src"

# 文件 → 精简后的模块 docstring 内容（不含三引号）
SHORT_DOCSTRINGS: dict[str, str] = {
    "app/main.py": """FastAPI 应用装配入口：中间件、异常处理器、路由与运维探针。

注意中间件顺序：Starlette 的 `add_middleware` 是 `insert(0, ...)`，
**最后 add 的在最外层**。因此 CORS 必须最后 add——它要给限流短路出来的 429
也补上跨域头，否则浏览器只会报 "CORS error"，前端读不到 Retry-After。
（详见 docs/LEARN.md「中间件洋葱模型」）""",
    "app/core/config.py": """全局配置：全部来自环境变量 / .env，启动期一次性校验。

默认值刻意取「本地开箱即跑」（SQLite + dev 密钥），生产环境由
`_guard_production` 拦住弱密钥与 DEBUG，属于 fail-fast 设计。""",
    "app/core/security.py": """安全组件：bcrypt 密码哈希 + JWT 双令牌（access / refresh）。

bcrypt 是**故意慢**的哈希（cost 可调 + 自带随机盐），用于抵消 GPU 暴力破解；
JWT 校验必须给 algorithms 白名单，否则存在 alg 混淆攻击面。""",
    "app/core/exceptions.py": """分层异常体系 + 统一错误响应。

业务层只抛 AppError 子类（不依赖 HTTP），由全局处理器统一转成
`{code, message, detail?, request_id?}`；5xx 只回 request_id，不回堆栈。""",
    "app/core/logging.py": """结构化日志：本地彩色可读，生产 JSON 便于采集。""",
    "app/db/session.py": """数据库引擎与会话管理（SQLAlchemy 2.0 异步）。

每请求一个会话（依赖注入），`expire_on_commit=False` 是异步场景的关键设置：
否则 commit 后访问属性会触发隐式查询并抛 MissingGreenlet。""",
    "app/models/base.py": """ORM 基类：统一命名约定、公共时间戳。

命名约定（naming_convention）让 Alembic 生成稳定可回滚的约束名。""",
    "app/models/user.py": """用户模型。密码只存 bcrypt 哈希，邮箱统一小写存储。""",
    "app/models/category.py": """分类模型。

唯一约束是 (user_id, name, kind)：同一用户下同类型不允许重名，
但「报销」这类名字可以同时存在于收入与支出。""",
    "app/models/transaction.py": """账单模型（核心业务表）。

两个关键设计：金额用 Numeric(12,2) 而非 Float（避免浮点累计误差）；
账期 occurred_on 与写入时间 created_at 分离（支持补录历史账单）。""",
    "app/schemas/common.py": """Schema 公共组件：枚举、分页、通用响应。""",
    "app/schemas/user.py": """用户与认证相关 Schema。密码字段的约束与敏感字段过滤都在这里。""",
    "app/schemas/category.py": """分类 Schema。""",
    "app/schemas/transaction.py": """账单 Schema：创建、更新、查询过滤、分页响应。""",
    "app/schemas/stats.py": """统计 Schema：月度概览、分类占比、趋势。金额统一序列化为字符串。""",
    "app/schemas/csv_io.py": """CSV 导入导出 Schema。""",
    "app/repositories/base.py": """数据访问层（Repository）基类。

Repository 只 `flush` 不 `commit`：事务边界由 Service 统一控制，
这样「一次用例的多表写入」天然在同一个事务里。""",
    "app/repositories/user_repo.py": """用户仓储。""",
    "app/repositories/category_repo.py": """分类仓储。""",
    "app/repositories/transaction_repo.py": """账单仓储：过滤、分页、批量导入与聚合入口。

两个必须知道的点：
- 列表查询统一用 `selectinload(Transaction.category)` 消除 N+1（查询数固定为 2）；
- 过滤条件全部走链式 `.where()` + 绑定参数，绝不拼字符串 SQL（免疫注入）。""",
    "app/services/auth_service.py": """认证与用户业务。

事务边界（重点）：Repository 只 flush，Service 决定何时 commit，
因此「插用户 + 批量插默认分类」要么全成要么全回滚。""",
    "app/services/category_service.py": """分类业务。""",
    "app/services/transaction_service.py": """账单业务。""",
    "app/services/stats_service.py": """统计业务：月度概览、分类占比、趋势、日历。

核心原则：**聚合下沉到数据库**。1 万条账单若在 Python 里循环累加，
耗时会随数据量线性劣化；交给 SQL 的 SUM/GROUP BY 后耗时基本与数据量无关。""",
    "app/services/csv_service.py": """CSV 导入导出业务。

三个工程细节：导出加 UTF-8 BOM（否则 Excel 中文乱码）、用生成器流式产出
（内存与数据量无关）、导入先全量校验再批量落库（错误定位到行号 + 指纹去重）。""",
    "app/middleware/context.py": """请求上下文中间件：分配 request_id、记录耗时、输出访问日志。

request_id 通过 structlog contextvars 绑定，此后该请求的所有日志自动携带，
可用一个 id 串起「访问日志 → Service 日志 → 异常堆栈」。""",
    "app/middleware/rate_limit.py": """限流中间件：进程内令牌桶 + Redis 固定窗口（多实例共享）。

令牌桶允许突发流量，是网关（Nginx / Sentinel）的主流选择。
Redis 故障时**降级放行并告警**，避免限流组件变成全站单点。""",
    "app/api/deps.py": """FastAPI 依赖注入工厂：数据库会话、当前用户、分页参数。

依赖可被 `app.dependency_overrides` 整体替换，测试里换成内存库非常轻量。""",
    "app/api/v1/auth.py": """认证路由。""",
    "app/api/v1/categories.py": """分类路由。""",
    "app/api/v1/transactions.py": """账单路由：列表（多条件过滤 + 分页）、详情、增删改。""",
    "app/api/v1/stats.py": """统计路由。""",
    "app/api/v1/csv_io.py": """CSV 导入导出路由。""",
    "app/db/init_db.py": """数据库初始化脚本：建表 / 重置本地数据。

生产环境请用 Alembic 迁移（保留版本可追溯），本脚本只用于本地开发。""",
}


def replace_module_docstring(path: Path, new_body: str, *, dry_run: bool) -> bool:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))

    # 第一个有意义的 token 必须是字符串（模块 docstring）
    ignored = {tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.COMMENT}
    first = next((tok for tok in tokens if tok.type not in ignored), None)
    if first is None or first.type != tokenize.STRING or first.start[0] != 1:
        return False

    end_line = first.end[0]
    quote = '"""'
    new_doc = f"{quote}{new_body}{quote}\n"
    rebuilt = [new_doc, *lines[end_line:]]
    if not dry_run:
        path.write_text("".join(rebuilt), encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="精简模块 docstring")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    changed = 0
    for relative, body in sorted(SHORT_DOCSTRINGS.items()):
        path = SRC / relative
        if not path.exists():
            print(f"  跳过（不存在）: {relative}")
            continue
        before = len(path.read_text(encoding="utf-8").splitlines())
        if not replace_module_docstring(path, body, dry_run=args.dry_run):
            print(f"  跳过（首个 token 不是模块 docstring）: {relative}")
            continue
        changed += 1
        print(f"  {relative:<44} 原 {before} 行 → 已替换模块 docstring")

    verb = "将处理" if args.dry_run else "已处理"
    print(f"\n{verb} {changed} 个文件的模块 docstring")
    return 0


if __name__ == "__main__":
    sys.exit(main())
