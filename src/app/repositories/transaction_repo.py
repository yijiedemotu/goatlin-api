"""账单仓储：过滤、分页、批量导入与聚合入口。

两个必须知道的点：
- 列表查询统一用 `selectinload(Transaction.category)` 消除 N+1（查询数固定为 2）；
- 过滤条件全部走链式 `.where()` + 绑定参数，绝不拼字符串 SQL（免疫注入）。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any

from sqlalchemy import Select, case, delete, func, or_, select
from sqlalchemy.orm import selectinload

from app.models.transaction import Transaction
from app.repositories.base import BaseRepository
from app.schemas.transaction import TransactionQuery


class TransactionRepository(BaseRepository[Transaction]):
    """transactions 表的数据访问入口，也是全项目最重的仓储。

    生命周期：请求级实例。TransactionService / StatsService / CsvService 各自在
    `__init__` 里构造它，并**共用同一个 AsyncSession**——这是"同请求多次写入
    落在同一事务"的前提。无状态、无缓存（CSV 导入的分类缓存是
    CsvService 自己的请求内字典）。

    为什么独立成类：账单是核心业务表，过滤 / 分页 / 聚合 / 批量写入的 SQL 全集中
    于此，Service 只管校验与事务。若把这些查询散落到 Service 里，
    任何一处漏写 `user_id` 条件就是水平越权；集中后"所有查询都带 user_id"
    成为可 review 的局部性质。
    """

    model = Transaction  # 声明操作 transactions 表；基类的 get/add/delete/count 依赖它

    # ------------------------------------------------------------------
    # 查询构建
    # ------------------------------------------------------------------
    @staticmethod
    def build_filters(user_id: int, query: TransactionQuery | None = None) -> Select[Any]:
        """把若干个可选过滤条件拼成一条 SELECT（只构造，不执行）。

        参数:
            user_id: 当前用户主键，**无条件作为第一个 WHERE 条件**——
                任何调用方都无法绕过，这是整个仓储的越权防线。
                它来自 token 解析（api/deps.py），不是客户端参数。
            query: 前端筛选栏收敛出的查询对象；None 表示"不加任何过滤"
                （导出全量等场景），此时只有 user_id 条件。
        返回:
            未执行的 `Select` 对象。返回语句而不是结果，是为了让列表查询与
            count 查询能复用同一套条件（见 `paginate`）——把"条件"做成可传递的
            值对象，是避免分页口径不一致的关键。
        为什么是 staticmethod: 它不碰 `self.session`，纯粹是语句构造，
            因此可以被单独调用去 `print(stmt.compile(...))` 看生成的 SQL，
            也方便做不依赖数据库的单测。

        条件语义（逐条对应 `schemas/transaction.py::TransactionQuery` 的字段，
        也就是前端筛选栏的每一个控件）:
            start_date / end_date → 账期闭区间，**两端都含**；
            kind → 收支方向，值为 "income" / "expense"（上层已把枚举拆成 value）；
            category_id → 分类过滤，判空用 `is not None`（详见行内注释）；
            min_amount / max_amount → 金额闭区间，Decimal 直接比较；
            keyword → 备注模糊搜索（`ilike`）。

        复杂度与索引: 构造只是 Python 对象操作，与数据量无关；执行阶段
            走 `ix_transactions_user_occurred` / `ix_transactions_user_kind_occurred`
            （见模块说明第 4 条）。
        """
        # 起步条件就是用户隔离：所有派生查询（列表、计数、导出）都从这里出发
        stmt = select(Transaction).where(Transaction.user_id == user_id)
        if query is None:
            # 无过滤：导出全量账单等场景
            return stmt
        if query.start_date:
            # 闭区间起点（含当天）；occurred_on 是 Date 列，与 Python date 直接比较
            stmt = stmt.where(Transaction.occurred_on >= query.start_date)
        if query.end_date:
            # 闭区间终点（含当天）：必须用 <=，写成 < 会漏掉"区间最后一天"的账单
            stmt = stmt.where(Transaction.occurred_on <= query.end_date)
        if query.kind:
            # 枚举 → 数据库里实际存储的字符串（Transaction.kind 是 String(8)）
            stmt = stmt.where(Transaction.kind == query.kind.value)
        if query.category_id is not None:
            # 显式 is not None 而不是真值判断：0 是假值，用 `if query.category_id`
            # 会让"id 为 0"这种边界值静默退化成"不过滤"，是经典埋雷写法
            stmt = stmt.where(Transaction.category_id == query.category_id)
        if query.min_amount is not None:
            # Decimal 之间比较，不转 float：金额边界不能引入二进制浮点误差
            stmt = stmt.where(Transaction.amount >= query.min_amount)
        if query.max_amount is not None:
            stmt = stmt.where(Transaction.amount <= query.max_amount)
        if query.keyword:
            pattern = f"%{query.keyword.strip()}%"
            # 单条件的 or_ 不会生成 OR 关键字，SQL 里仍然是 `note ILIKE ?`；
            # 保留 or_ 外壳是为了日后扩成"备注 OR 分类名"时不必改动结构
            stmt = stmt.where(or_(Transaction.note.ilike(pattern)))
        return stmt

    async def paginate(
        self, user_id: int, query: TransactionQuery, *, page: int, size: int
    ) -> tuple[Sequence[Transaction], int]:
        """返回 (当页数据, 总条数)。计数与列表复用同一套过滤条件。

        参数:
            page: 页码，从 1 开始（`api/deps.py::pagination` 已用 `Query(ge=1)` 校验，
                服务层不再重复校验）。
            size: 每页条数，上限 100（`Query(le=100)` 拦住"一次拉十万行"的请求，
                避免把内存打爆）。
        返回:
            `(rows, total)`。rows 是当页账单，**category 已预加载**，
            因此上层 `TransactionOut.model_validate(row)` 读 `row.category`
            不会再触发查询；total 是满足过滤条件的**总行数**（不是本页行数），
            前端分页组件用它算总页数（`Page.pages`）。
        为什么 count 必须单独查: `LIMIT/OFFSET` 只影响返回行数，数据库不会顺手
            告诉你一共多少行。替代方案是窗口函数 `count(*) OVER ()`，
            能省一次往返，但要扫完整个结果集、且旧版 SQLite 不支持；
            这里选"两条语句"换取可移植性与可读性。
        分页方式取舍: 这是 OFFSET 分页——实现简单、支持跳页，但有两个固有代价：
            (a) 深分页（OFFSET 100000）时数据库仍要扫描并丢弃前 10 万行；
            (b) 用户翻页期间若有新账单插入，会出现重复行或漏行。
            数据量再上一个量级应换 keyset（游标）分页：
            `WHERE (occurred_on, id) < (:last_occurred, :last_id)`，
            代价是不能跳页。
        索引命中: 过滤走 `ix_transactions_user_occurred`（或带 kind 的那条）；
            user_id 是等值条件，索引在该前缀内按 occurred_on 有序，
            所以 `occurred_on DESC` 可以反向扫索引直接拿到顺序，
            无需额外排序文件——只有同一天内的多条记录（id 不是索引列）
            需要一次很小的内存排序，这也是把 id 加进 ORDER BY 的成本所在。
        """
        # 先构造过滤条件；下面 count 与列表都用它，保证口径完全一致
        stmt = self.build_filters(user_id, query)
        # 计数：BaseRepository.count 会把它包成 SELECT count(*) FROM (...) 子查询
        total = await self.count(stmt)
        stmt = (
            # selectinload：额外发一条 WHERE category_id IN (...) 把关联一次装好，
            # 总查询数固定为 2（列表 1 + 关联 1），而不是 1 + N
            stmt.options(selectinload(Transaction.category))
            # 排序必须稳定：只按 occurred_on 排时，同一天的多条账单顺序由数据库
            # 自行决定，翻页会出现"同一条看两次/漏一条"，所以追加主键做 tie-breaker
            .order_by(Transaction.occurred_on.desc(), Transaction.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        # 取实体用 execute(...).scalars()：execute 得到 Result，scalars() 再把每行的
        # 第一个实体列剥出来（等价写法是 session.scalars(stmt)），
        # 这里保留 execute 是为了和上面 count 的写法对称、便于阅读
        rows = (await self.session.execute(stmt)).scalars().all()
        return rows, total

    async def get_owned(self, transaction_id: int, user_id: int) -> Transaction | None:
        """取单条账单（带归属校验 + 预加载分类），详情 / 更新 / 删除都先经过它。

        参数:
            transaction_id: 路径参数，客户端可控，**不可信**。
            user_id: 服务端解析出的当前用户主键，可信。
        返回:
            只有属于该用户才返回对象，否则 None（Service 转成 NotFoundError）。
            user_id 写进 WHERE 而不是查完再比，越权者无法从响应差异判断
            别人的账单 id 是否存在（水平越权 / IDOR 防护）。
        为什么只查一条也要 selectinload: Service 在 create / update 之后会重新调它
            把对象回传给路由，响应体里的 `category` 字段需要分类名与颜色；
            不预加载的话 `lazy="raise"` 会直接抛错（序列化阶段才发现就晚了）。
        索引命中: 主键定位后过滤 user_id，走主键索引一次回表，成本可忽略。
        """
        stmt = (
            select(Transaction)
            .options(selectinload(Transaction.category))
            .where(Transaction.id == transaction_id, Transaction.user_id == user_id)
        )
        return await self.session.scalar(stmt)

    async def list_for_export(
        self, user_id: int, start: date | None = None, end: date | None = None
    ) -> Sequence[Transaction]:
        """导出专用的全量查询：时间正序、区间可选。

        参数:
            start / end: 账期闭区间边界；None 表示不限制（导出全部历史账单）。
                两者相互独立，只传一个也能工作。
        返回:
            按 `occurred_on ASC, id ASC` 排序的账单列表。导出文件按时间正序
            才符合用 Excel 查看的习惯，与列表接口的倒序刚好相反。
        为什么要预加载 category: CsvService 逐行写 CSV 时要读 `tx.category.name`；
            模型上 `lazy="raise"` 决定了不预加载会直接抛错，
            若放宽成懒加载则每行一条 SQL（5000 行 = 5000 次往返）。
        已知瓶颈（值得主动讲）: 导出接口的"流式"只体现在**响应写出**上
            （yield 逐行），查询阶段仍是 `.all()` 全量物化到内存。
            账单量到百万级时这里会先成为内存瓶颈，届时应改成
            分批取数（`yield_per` / 服务端游标），或把导出改成异步任务。
        索引命中: `(user_id, occurred_on)` 前两列正好支撑"用户 + 区间 + 按账期排序"。
        """
        stmt = (
            select(Transaction)
            .options(selectinload(Transaction.category))
            .where(Transaction.user_id == user_id)
            # 正序：导出文件按时间正序，与列表接口的倒序不同
            .order_by(Transaction.occurred_on.asc(), Transaction.id.asc())
        )
        if start:
            # 注意这是在 order_by 之后继续追加 where：SQLAlchemy 编译时会自动
            # 把 WHERE 放到 ORDER BY 之前，链式顺序不影响生成 SQL 的正确性
            stmt = stmt.where(Transaction.occurred_on >= start)
        if end:
            stmt = stmt.where(Transaction.occurred_on <= end)
        return (await self.session.scalars(stmt)).all()

    async def existing_fingerprints(self, user_id: int) -> set[tuple[date, str, str]]:
        """导入去重指纹：同一用户下 (账期, 金额, 备注) 视为重复。

        参数:
            user_id: 只取该用户的账单，指纹集合因此天然按用户隔离，
                不同用户的相同账单不会被互相判重（csv_service 只跑自己那份）。
        返回:
            `{(账期, 金额字符串, 备注)}` 集合，供导入循环做 O(1) 成员判断。
            金额统一转字符串：数据库返回 Decimal，而 CSV 侧指纹用的是
            `f"{amount:.2f}"`，两边都按字符串比才能稳定命中
            （`Numeric(12,2)` 保证数据库侧固定两位小数）。
        实现取舍: 只 select 三个窄列而不是整行，1 万条账单也只回传这三列；
            但**仍然是全量拉取**，内存占用随历史账单条数增长。
            更大数据量应改成"把待导入指纹分批回查数据库"：
            `WHERE user_id = ? AND occurred_on IN (...)`，用往返次数换内存。
        索引命中: 只有 user_id 等值条件，走 user_id 上的普通索引；
            这条查询本身必须遍历该用户全部账单，属于导入这种低频重操作的合理成本。
        """
        stmt = select(Transaction.occurred_on, Transaction.amount, Transaction.note).where(
            Transaction.user_id == user_id
        )
        rows = await self.session.execute(stmt)
        # 用集合而不是列表：导入时每一行都要判断"是否已存在"，
        # 列表查找是 O(n)、集合是 O(1)；1 万行 × 1 万个已有指纹会差出几个数量级
        return {(row[0], str(row[1]), row[2]) for row in rows.all()}

    async def bulk_create(self, items: Iterable[Transaction]) -> int:
        """批量插入：单事务 + flush，CSV 导入 5000 行也从分钟级降到秒级。

        参数:
            items: 可迭代的 ORM 实例（CsvService 已逐行校验并构造好 user_id /
                category_id / amount 等字段），允许为空。
        返回:
            实际加入会话的行数；空输入直接返回 0，不会产生任何数据库往返。
        为什么快: `add_all` 把对象一次性交给 Unit of Work，flush 时 SQLAlchemy
            会合并成批量 INSERT（配合 2.0 的 insertmanyvalues，往返次数从 N 降到
            常数级），而不是"N 次 add + flush"。调用方（CsvService）在整个批次
            结束后只 commit 一次。
        为什么先 `list(items)`: 传进来的通常是生成器，`add_all` 和 `len()` 都需要
            具体长度；顺手把"空集合"判空提前，避免为 0 行开一次数据库往返。
        注意: 这里不做任何去重或校验——去重在 CsvService 用
            `existing_fingerprints` 完成。职责边界要保持干净。
        """
        payload = list(items)
        if not payload:
            # 空输入短路：什么都没导入时不要白白 flush 一次
            return 0
        self.session.add_all(payload)
        await self.session.flush()
        return len(payload)

    async def delete_by_user(self, user_id: int) -> int:
        """按用户批量删除全部账单（Core DELETE 语句，一条 SQL 搞定）。

        参数:
            user_id: 目标用户主键。
        返回:
            被删除的行数（`rowcount`），供调用方记录日志或回报给用户。
        实现取舍: 用 `delete()` 语句而不是 `for tx in rows: session.delete(tx)`，
            省掉"先 SELECT 全量、再逐条标记删除"的两步开销，
            是清库 / 注销这类批量场景的标准写法。代价是绕过 ORM 的级联与事件，
            会话里已加载的同批对象会变成脏状态，因此只适合
            "确定不再需要这些对象"的场景。
        现状说明: 本仓库该方法目前**没有 Service 调用方**（账号注销/重置功能尚未
            接入），属于预留能力；`models/transaction.py` 的
            `ON DELETE CASCADE` 才是删除用户时真正生效的那条路径。
            面试若被问"这段代码有人用吗"，如实回答比硬编一个调用点更好。
        """
        result = await self.session.execute(delete(Transaction).where(Transaction.user_id == user_id))
        # execute 的静态类型是 Result，DELETE 语句实际返回 CursorResult，才有 rowcount
        # getattr 兜底是为了 mypy strict：不强转类型也能取到属性
        return int(getattr(result, "rowcount", 0) or 0)

    async def sum_by_kind(self, user_id: int, start: date, end: date, kind: str) -> tuple[float, int]:
        """某方向（收入或支出）在区间内的合计金额与笔数。

        参数:
            start / end: 账期闭区间（`stats_service.parse_month` 保证是同月首末日）。
            kind: "income" / "expense"，由 `TransactionKind.value` 传入，
                不传枚举本身——仓储这一层只认数据库里存的字符串。
        返回:
            `(金额合计, 笔数)`；该区间没有任何账单时返回 `(0.0, 0)`。
            `coalesce(sum(...), 0)` 是关键：SQL 的 `SUM` 在零行时返回 **NULL**，
            不兜底就会在 `float(None)` 处抛 TypeError（统计接口的经典 500）。
        为什么不用 count(amount): `amount` 是 NOT NULL，`count(amount)` 与
            `count(*)` 等价；coalesce 只包 SUM 就够了，count 天然返回 0。
        索引命中: `(user_id, kind, occurred_on)` 复合索引最左前缀
            user_id、kind 均为等值，occurred_on 走范围扫描，
            只需读该区间对应的索引项。
        复杂度: O(区间内该用户该方向的账单数)，与全表规模无关，数据库只回传 1 行。
            换成"拉明细回 Python 累加"则要传输 N 行并构造 N 个 ORM 对象——
            这就是"聚合下沉"的意义。
        精度提醒: 这里把 Decimal 转成 float 返回，Service 再用
            `Decimal(str(value))` 量化回分（stats_service._money）。
            更严谨的做法是全程 Decimal（Numeric 取出来本就是 Decimal），
            少一次二进制浮点往返；当前实现依赖"金额不超过 2 位小数、
            远小于 2^53"这一前提才安全。
        """
        stmt = select(func.coalesce(func.sum(Transaction.amount), 0), func.count()).where(
            Transaction.user_id == user_id,
            Transaction.occurred_on >= start,
            Transaction.occurred_on <= end,
            Transaction.kind == kind,
        )
        # .one()：没有 GROUP BY 的聚合必定只返回 1 行。相比 first()（默默取第一行），
        # one() 把"有且仅有一行"写成断言：真出现 0 行或多行会立刻抛错，
        # 而不是拿着错行继续算出一个看起来正常的统计结果
        row = (await self.session.execute(stmt)).one()
        return float(row[0]), int(row[1])

    async def group_by_category(
        self, user_id: int, start: date, end: date, kind: str
    ) -> list[tuple[int | None, str, str, float, int]]:
        """按分类聚合：LEFT JOIN 分类表，未归类账单 category_id 为 NULL。

        参数:
            start / end: 账期闭区间。
            kind: 只统计收入或支出中的一种（环形图一次只画一个方向，
                混在一起算占比没有意义）。
        返回:
            列表，每项 `(category_id | None, 分类名, 颜色, 金额合计, 笔数)`，
            已按金额**从大到小**排序，前端环形图/排行榜可直接用这个顺序渲染，
            不必二次排序。
            分类名与颜色在 SQL 里就被兜底（「未归类」/ 中性灰），
            因此 Service 无需再判 None。
        为什么用 LEFT JOIN 而不是 INNER JOIN: `category_id` 可以为 NULL
            （分类被删除时走 `ON DELETE SET NULL`）。INNER JOIN 会把"未归类"
            的账单整条丢掉，导致各分类占比之和小于月度总支出——
            这种"数据对不上"的 bug 在财务产品里是致命的，
            对账时用户会直接失去信任。
        为什么 GROUP BY 要带上 Category.name / color: PG 等严格方言要求 SELECT 中的
            非聚合列必须出现在 GROUP BY 里（或被聚合、或有函数依赖）。
            按 `Transaction.category_id` 分组其实已隐含 name/color 唯一，
            但优化器不跨表推导这种依赖，所以显式列全，两种方言才能通吃。
        索引命中: 先用 `(user_id, kind, occurred_on)` 复合索引做过滤与范围扫描，
            再按 category_id 分组；JOIN 走 categories 主键。
        复杂度: 分组在数据库完成，回传行数 = 分类数（通常 < 50），与账单条数无关。
        """
        from app.models.category import Category

        stmt = (
            select(
                Transaction.category_id,
                # 未归类的展示兜底：写在这里，前端就不会拿到 null 或空字符串
                func.coalesce(Category.name, "未归类"),
                # 颜色兜底成中性灰，前端取色不用判空
                func.coalesce(Category.color, "#94A3B8"),
                func.sum(Transaction.amount),
                func.count(),
            )
            .outerjoin(Category, Category.id == Transaction.category_id)
            .where(
                Transaction.user_id == user_id,
                Transaction.occurred_on >= start,
                Transaction.occurred_on <= end,
                Transaction.kind == kind,
            )
            .group_by(Transaction.category_id, Category.name, Category.color)
            # 金额倒序：环形图/排行榜都从最大块开始，前端不再二次排序
            .order_by(func.sum(Transaction.amount).desc())
        )
        rows = await self.session.execute(stmt)
        # 逐行转成普通元组：Service 只做百分比计算，不需要 ORM 对象；
        # 而且跨表聚合结果也没有实体类可以装。float/int 转换顺便统一
        # SQLite 与 PG 的返回类型差异。
        return [(r[0], r[1], r[2], float(r[3]), int(r[4])) for r in rows.all()]

    async def group_by_month(self, user_id: int, start: date, end: date) -> list[tuple[str, float, float]]:
        """按月聚合收入与支出。

        可移植性取舍：`strftime` 只有 SQLite 有、`to_char` 只有 PG 有，
        因此月份表达式按方言分派；而 SUM(CASE WHEN ...) 是所有方言通用写法，
        统一使用 CASE，避免在聚合逻辑上再分叉。

        参数:
            start / end: 闭区间；Service 会算成"近 N 个月的 1 号 ~ 当月最后一天"
                （见 stats_service.trend）。
        返回:
            `[(YYYY-MM, 收入合计, 支出合计)]` 按月份升序。
            **只包含有账单的月份**——没有账单的月份不会出现在结果里，
            趋势图的 X 轴需要连续月份，所以 StatsService 会把结果转成字典后
            再补齐空月份（默认 0）。
        为什么用 SUM(CASE WHEN kind = 'income' ...) 而不是查两次: 一次表扫描同时
            算出两个方向，数据库往返从 2 降到 1；代价是 `kind` 无法作为分组
            前缀被索引利用，需要扫区间内全部账单。
            这是"少一次往返"换"多一点扫描"的取舍。
        索引命中: `(user_id, occurred_on)` 负责过滤；月份的表达式分组用不上索引排序，
            数据库需要在分组后额外排序——分组数只有几个月，成本可忽略。

        生成的 SQL 大致如下（PG 方言）::

            SELECT to_char(occurred_on, 'YYYY-MM') AS month,
                   coalesce(sum(CASE WHEN kind = 'income'  THEN amount ELSE 0 END), 0),
                   coalesce(sum(CASE WHEN kind = 'expense' THEN amount ELSE 0 END), 0)
            FROM transactions
            WHERE user_id = ? AND occurred_on >= ? AND occurred_on <= ?
            GROUP BY to_char(occurred_on, 'YYYY-MM')
            ORDER BY to_char(occurred_on, 'YYYY-MM')

        易踩的坑: CASE 的 `else_=0` 让"非目标方向"的行贡献 0；写成 `else_=None`
            结果其实相同（SUM 会忽略 NULL），但那样 coalesce 只在"整月零账单"时
            起作用，读代码的人得反复推敲"这列到底会不会是 NULL"。统一用 0 更直白，
            也让这个聚合根本不会产出 NULL。
        """
        dialect = self.session.get_bind().dialect.name
        # 方言分派：get_bind() 取当前会话绑定的引擎，判断 "sqlite" / "postgresql"。
        # 测试跑 SQLite、生产跑 PG，两者的日期格式化函数名不同，只能在此分叉；
        # 这是"同一份代码跨两种数据库"最典型的妥协点。
        month_expr = (
            func.strftime("%Y-%m", Transaction.occurred_on)
            if dialect == "sqlite"
            else func.to_char(Transaction.occurred_on, "YYYY-MM")
        )
        # 收入列：非收入行贡献 0，SUM 之后不会因 NULL 全被忽略而返回 NULL
        income_sum = func.coalesce(
            func.sum(case((Transaction.kind == "income", Transaction.amount), else_=0)), 0
        )
        # 支出列同理；两个 CASE 在同一次表扫描里一起算完
        expense_sum = func.coalesce(
            func.sum(case((Transaction.kind == "expense", Transaction.amount), else_=0)), 0
        )
        stmt = (
            select(month_expr.label("month"), income_sum, expense_sum)
            .where(
                Transaction.user_id == user_id,
                Transaction.occurred_on >= start,
                Transaction.occurred_on <= end,
            )
            .group_by(month_expr)
            .order_by(month_expr)
        )
        rows = await self.session.execute(stmt)
        return [(str(r[0]), float(r[1]), float(r[2])) for r in rows.all()]

    async def group_by_day(self, user_id: int, start: date, end: date) -> list[tuple[date, float, float]]:
        """按账期聚合每日收入与支出（账单日历 / 热力图用）。

        参数:
            start / end: 通常是一个自然月的首末日（`stats_service.parse_month`）。
        返回:
            `[(日期, 收入合计, 支出合计)]` 升序；同样**只含有账单的日期**，
            空白天数由 `stats_service.calendar` 补零，前端才能画固定格数的月历。
        与 `group_by_month` 的差别: 这里不需要方言分派——`occurred_on` 本身就是
            Date 列，直接 GROUP BY 即可跨库通用。这也印证一条经验：
            **能对原列分组就不要造表达式**，表达式分组通常意味着用不上索引顺序。
        索引命中: `(user_id, occurred_on)` 覆盖过滤与分组，索引在该前缀内已按
            occurred_on 有序，GROUP BY 可以直接顺着索引扫，省掉一次排序。
        """
        stmt = (
            select(
                Transaction.occurred_on,
                # 与月度聚合同构：每日的收入列 / 支出列各用一个 SUM(CASE WHEN)
                func.coalesce(func.sum(case((Transaction.kind == "income", Transaction.amount), else_=0)), 0),
                func.coalesce(
                    func.sum(case((Transaction.kind == "expense", Transaction.amount), else_=0)), 0
                ),
            )
            .where(
                Transaction.user_id == user_id,
                Transaction.occurred_on >= start,
                Transaction.occurred_on <= end,
            )
            .group_by(Transaction.occurred_on)
            .order_by(Transaction.occurred_on)
        )
        rows = await self.session.execute(stmt)
        # r[0] 保持 date 类型（SQLAlchemy 的 Date 类型会负责转换），
        # 与 Service 里 raw.get(cursor) 的查表 key 类型一致（两边都是 date）
        return [(r[0], float(r[1]), float(r[2])) for r in rows.all()]
