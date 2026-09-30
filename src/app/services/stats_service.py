"""统计业务：月度概览、分类占比、趋势、日历。

核心原则：**聚合下沉到数据库**。1 万条账单若在 Python 里循环累加，
耗时会随数据量线性劣化；交给 SQL 的 SUM/GROUP BY 后耗时基本与数据量无关。"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BadRequestError
from app.repositories.transaction_repo import TransactionRepository
from app.schemas.common import TransactionKind
from app.schemas.stats import (
    CalendarSeries,
    CategoryStat,
    CategoryStatList,
    DailyPoint,
    MonthlySummary,
    TrendPoint,
    TrendSeries,
)

TWO_PLACES = Decimal("0.01")


def _money(value: float | Decimal | int) -> Decimal:
    """统一量化到分，避免二进制浮点误差渗透到接口层。

    参数：value —— 可能是数据库聚合出的 float（SQLite 的 SUM 走 REAL）、
    也可能是已经在业务层算好的 Decimal 或整数。

    返回：量化到两位小数、ROUND_HALF_UP 的 Decimal（schema 层会序列化成字符串给前端）。

    关键实现选择：**先 str() 再 Decimal()，绝不能写 Decimal(value)**。
    Decimal(0.1) 会把 float 的二进制真值原样变成 0.1000000000000000055511...，
    而 Decimal("0.1") 才是用户心里的 0.1；SQL 聚合结果经 float 回来后
    这一步是唯一能拦住误差扩散的位置。
    ROUND_HALF_UP 是业务上的「四舍五入」（银行家舍入会让 0.125 变成 0.12，
    对账时用户会认为算错了）。ROUND_HALF_UP 是显式传参，不依赖默认规则（默认是 HALF_EVEN）。
    """
    return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def parse_month(month: str) -> tuple[date, date]:
    """把 'YYYY-MM' 解析成 [起,止] 闭区间，非法格式直接 400。

    参数：month —— 形如 "2025-07" 的字符串（路由层用正则约束过格式，
    但本函数仍自行校验，因为它也被内部/测试直接调用）。

    返回：(当月 1 号, 当月最后一天) 两个 date。用 calendar.monthrange 取最后一天，
    因此闰年 2 月是 29 号 —— 这正是不能用「固定 28/30/31 天」硬编码的原因，
    也不能用 `month.replace(day=28) + timedelta(days=4)` 那种技巧（会跨月、可读性差）。

    抛出：BadRequestError（HTTP 400 / code=BAD_REQUEST），detail 回传原始 month 便于定位。

    关键实现选择：手写 split + int 而不是 strptime("%Y-%m")，
    因为 strptime 会接受 "2025-1" 这类非补零写法，同时把解析错误与业务错误混在同一个异常里；
    手写能保证「格式」与「月份范围」两类检查的语义都明确，且不引入额外依赖。
    """
    try:
        year_str, month_str = month.split("-")
        year, mon = int(year_str), int(month_str)
        if not 1 <= mon <= 12:
            raise ValueError
        last_day = calendar.monthrange(year, mon)[1]
        return date(year, mon, 1), date(year, mon, last_day)
    except (ValueError, AttributeError) as exc:
        # 带 from exc 保留原始异常链：日志里能看到到底是「格式不对」还是「月份越界」
        raise BadRequestError("月份格式应为 YYYY-MM", detail={"month": month}) from exc


class StatsService:
    """统计用例编排：四个只读接口，不做任何写入。

    生命周期：每请求一个实例，共享请求的 AsyncSession。
    实例上只挂了一个 TransactionRepository，因为统计永远只读 transactions 表
    （分类名与颜色由仓储层的 LEFT JOIN 一次带出来，不需要 CategoryRepository）。

    为什么独立成类：统计的口径（percent 的分母、储蓄率公式、日均的天数）是产品语义，
    既不是 SQL 也不是 HTTP；集中在这里才能保证「同一个数在所有接口里算法一致」。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.transactions = TransactionRepository(session)

    # ------------------------------------------------------------------
    async def monthly_summary(self, user_id: int, month: str) -> MonthlySummary:
        """月度概览：收入、支出、结余、储蓄率、笔数、日均支出。

        参数：month —— "YYYY-MM"；由 parse_month 转成 [start, end] 闭区间，
        所有聚合都以 occurred_on 落在该区间为准（不看 created_at）。

        返回：MonthlySummary，金额字段均为 Decimal（序列化成 JSON 字符串给前端 KPI 卡）。

        抛出：BadRequestError（400 / BAD_REQUEST），月份格式非法时由 parse_month 抛出。

        关键实现选择：
        1. **只有 2 条聚合 SQL**：收入一条、支出一条，而不是「把账单全查出来再在 Python 里
           分组累加」，也不是「按分类各查一次」。笔数随 SUM 一起返回，省掉一次 COUNT。
        2. **储蓄率 = 结余 / 收入 × 100**：分母取收入而不是总流水
           （用支出当分母会得到接近 1 或负数的无意义值）。收入为 0 时定义为 0，
           而不是 null 或 Infinity —— JSON 不允许 Infinity，null 会让前端显示占位符，
           两者都不如 0 直观。
        3. **日均支出按「已过去的天数」摊平**：历史月份用整月天数；查当月时只算到今天，
           否则月中的「日均」会被未来十几天稀释成一个假的小数字。
        """
        start, end = parse_month(month)
        income, income_count = await self.transactions.sum_by_kind(
            user_id, start, end, TransactionKind.INCOME.value
        )
        expense, expense_count = await self.transactions.sum_by_kind(
            user_id, start, end, TransactionKind.EXPENSE.value
        )

        income_d, expense_d = _money(income), _money(expense)
        balance = income_d - expense_d
        saving_rate = float((balance / income_d * 100).quantize(TWO_PLACES)) if income_d > 0 else 0.0

        # 日均支出按「已过去的天数」摊平：当月未结束时用今天，避免被未来空白天数稀释。
        # 三行分别处理：查询历史/未来月份（start > today，用整月）、查询当月（用今天）、
        # 以及 start == today 的边界（elapsed_days = 1，日均 = 当日支出）。
        today = date.today()
        effective_end = min(end, today) if start <= today else end
        elapsed_days = max((effective_end - start).days + 1, 1)
        avg_daily = _money(expense_d / elapsed_days)

        return MonthlySummary(
            month=month,
            income=income_d,
            expense=expense_d,
            balance=balance,
            saving_rate=saving_rate,
            transaction_count=income_count + expense_count,
            avg_daily_expense=avg_daily,
        )

    # ------------------------------------------------------------------
    async def category_breakdown(self, user_id: int, month: str, kind: TransactionKind) -> CategoryStatList:
        """分类占比：某方向（收入/支出）下每个分类的金额、笔数与百分比。

        参数：
            month: "YYYY-MM"。
            kind: 统计收入还是支出 —— 路由层默认给 EXPENSE（前端环形图默认看支出）。

        返回：CategoryStatList，items 已按金额降序（排序在 SQL 层完成），
        前端 DonutChart 直接按顺序渲染，不需要再排一次。

        抛出：BadRequestError（400 / BAD_REQUEST），月份非法时由 parse_month 抛出。

        关键实现选择：
        1. **百分比的分母是「同方向总额」**（`sum(row[3] for row in rows)`，
           即本接口返回的这批行的和），不是全月总流水。全部按支出统计时占比之和
           恰好 100%，用户拿计算器一加就能对上，这对账本类产品很重要。
        2. **分母为 0 时 percentage 取 0.0**：正常情况下列表为空，
           这条保护主要防「有行但金额全为 0」的极端脏数据，避免 ZeroDivisionError → 500。
        3. **未归类账单也进结果**：仓储层 LEFT JOIN + coalesce 把 category_id 为 NULL
           的归到「未归类」，否则各项占比之和不到 100%，用户会怀疑系统漏账。
        4. percentage 是 float（只用于展示宽度，不参与金额累计）；
           total 保持 Decimal 并量化到分，避免前端累加时出现 0.01 的漂移。
        """
        start, end = parse_month(month)
        rows = await self.transactions.group_by_category(user_id, start, end, kind.value)
        # 分母 = 本批行的金额之和（同方向总额），不是全月流水：
        # 这样占比之和恒等于 100%，用户用计算器加一遍就能对上
        total = sum(row[3] for row in rows)

        items = [
            CategoryStat(
                category_id=category_id,
                category_name=name,
                color=color,
                kind=kind,
                total=_money(amount),
                count=count,
                percentage=round(amount / total * 100, 2) if total > 0 else 0.0,
            )
            for category_id, name, color, amount, count in rows
        ]
        return CategoryStatList(month=month, items=items)

    # ------------------------------------------------------------------
    async def trend(self, user_id: int, months: int = 6) -> TrendSeries:
        """近 N 个月趋势：一次 GROUP BY 拿到全部月份，再补齐没有账单的空月份。

        参数：months —— 统计月份数，默认 6（前端折线图默认口径）。
        路由层已用 Query(ge=1, le=24) 限制过，本方法再校验一次：
        它也可能被脚本或测试直接调用，不能假设调用方一定经过路由。

        返回：TrendSeries(months=连续月份标签, points=每月收入/支出/结余,
        max_expense / min_expense 供前端画参考线)。

        抛出：BadRequestError（400 / BAD_REQUEST）：months 不在 1-24 区间。

        关键实现选择：
        1. **把「年月」压成一个整数索引再 divmod**：`year * 12 + (month - 1)` 之后，
           加减月份就是整数加减，最后 divmod 还原成年月。这样既不用引入
           dateutil.relativedelta 这类额外依赖，也不会踩「3 月 31 日减一个月」的溢出坑。
        2. **只查库一次，再在内存里补齐 N 个月**：GROUP BY 的自然结果只含有数据的月份，
           而折线图必须有连续 x 轴 —— 缺月会被前端连成一条斜线，视觉上等于撒谎。
           补齐的月份用 (0, 0) 兜底，再统一经 _money() 量化成 Decimal("0.00")。
        3. **max / min 的空列表保护**：months 最小为 1，points 理论上不会为空，
           `if expenses else` 这层保护让函数在 months 被改成 0 时也不会崩。
        4. 区间是「起点所在月 1 号 到 今天所在月最后一天」，因此当月只统计到今天为止的数据，
           查询区间略大于实际数据范围，不影响结果，但能让索引范围扫描的边界保持简单。
        """
        if not 1 <= months <= 24:
            raise BadRequestError("months 取值范围 1-24")

        today = date.today()
        # 计算起点：向前推 months-1 个月的 1 号
        total_month_index = today.year * 12 + (today.month - 1) - (months - 1)
        start_year, start_month = divmod(total_month_index, 12)
        start = date(start_year, start_month + 1, 1)
        end = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])

        monthly_rows = await self.transactions.group_by_month(user_id, start, end)
        raw = {month: (inc, exp) for month, inc, exp in monthly_rows}

        labels: list[str] = []
        points: list[TrendPoint] = []
        for offset in range(months):
            index = total_month_index + offset
            year, mon = divmod(index, 12)
            label = f"{year:04d}-{mon + 1:02d}"
            labels.append(label)
            # 库里没有这个月的账单时用 (0, 0) 补齐，保证 points 与 months 长度一致、
            # x 轴连续 —— 否则前端折线图会把 1 月直接连到 3 月，视觉上相当于造了一个假趋势
            income, expense = raw.get(label, (0.0, 0.0))
            income_d, expense_d = _money(income), _money(expense)
            points.append(
                TrendPoint(month=label, income=income_d, expense=expense_d, balance=income_d - expense_d)
            )

        expenses = [p.expense for p in points]
        return TrendSeries(
            months=labels,
            points=points,
            max_expense=max(expenses) if expenses else _money(0),
            min_expense=min(expenses) if expenses else _money(0),
        )

    # ------------------------------------------------------------------
    async def calendar(self, user_id: int, month: str) -> CalendarSeries:
        """账单日历：补齐当月每一天，前端可直接渲染热力图。

        参数：month —— "YYYY-MM"。

        返回：CalendarSeries，days 覆盖当月 1 号到月末，没有账单的日期金额为 0。

        抛出：BadRequestError（400 / BAD_REQUEST），月份非法时由 parse_month 抛出。

        关键实现选择：
        1. **查询结果先转成 dict，再按日历顺序重建**：SQL 的 GROUP BY 只返回有账单的日期，
           而热力图需要完整网格 —— 缺日期在前端会表现为空洞或格子错位。
        2. **用 date.fromordinal(toordinal() + 1) 逐日推进**：与 timedelta(days=1) 等价，
           这样写只是把「按日期序数加一天」的意图写得明确；循环上限 31 次，开销可忽略。
        3. **只读不 commit**：与其它统计接口一致，本方法返回的是纯展示数据，
           不产生任何写操作。
        """
        start, end = parse_month(month)
        raw = {day: (inc, exp) for day, inc, exp in await self.transactions.group_by_day(user_id, start, end)}

        days: list[DailyPoint] = []
        cursor = start
        while cursor <= end:
            # 没有账单的日期补 0：热力图需要「当月每一天都有一个格子」，
            # 缺格子会让前端按 index 上色时整体错位一天
            income, expense = raw.get(cursor, (0.0, 0.0))
            days.append(DailyPoint(day=cursor, income=_money(income), expense=_money(expense)))
            cursor = date.fromordinal(cursor.toordinal() + 1)

        return CalendarSeries(month=month, days=days)
