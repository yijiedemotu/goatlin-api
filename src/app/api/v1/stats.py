"""统计路由。"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, SessionDep
from app.schemas.common import TransactionKind
from app.schemas.stats import CalendarSeries, CategoryStatList, MonthlySummary, TrendSeries
from app.services.stats_service import StatsService

router = APIRouter(prefix="/stats", tags=["统计分析"])


def _current_month() -> str:
    """当前月份字符串 `YYYY-MM`。

    为什么在路由层算而不是让前端传：前端时钟可能不准（或时区不同），
    默认值由服务端决定才能保证「打开首页默认看到本月」这个语义一致。
    注意这里用服务器本地日期，多时区部署时应改为按用户时区计算（面试可提）。
    """
    today = date.today()
    return f"{today.year:04d}-{today.month:02d}"


# 正则限制到 4 位年 + 01-12 月：非法格式在参数层 422，Service 无需处理脏输入
MonthQuery = Annotated[
    str | None,
    Query(
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        description="月份 YYYY-MM，不传则默认当月",
    ),
]


def _resolve_month(month: str | None) -> str:
    """把可选的月份参数解析成必填值（不传 → 当月）。"""
    return month or _current_month()


@router.get("/summary", response_model=MonthlySummary, summary="月度概览（收入/支出/结余/储蓄率）")
async def monthly_summary(
    current_user: CurrentUser,
    session: SessionDep,
    month: MonthQuery = None,
) -> MonthlySummary:
    """月度概览（需要登录，仅统计当前用户）。

    返回：本月收入、支出、结余、储蓄率、账单笔数、日均支出。
    月份格式非法 → 422 `VALIDATION_ERROR`；令牌无效 → 401。
    注意「日均支出」按**已过去的天数**摊平（当月未结束时用今天，而不是整月 31 天），
    否则月初看到的日均会被未来空白天数稀释——口径细节写在 `StatsService` 里。
    实现上是 2 条聚合 SQL（收入一条、支出一条），不是按分类查 N 次。
    前端 `DashboardView.vue` 顶部 KPI 卡片区（`KpiCard.vue`）。
    """
    return await StatsService(session).monthly_summary(current_user.id, _resolve_month(month))


@router.get("/categories", response_model=CategoryStatList, summary="分类占比（用于环形图）")
async def category_breakdown(
    current_user: CurrentUser,
    session: SessionDep,
    kind: Annotated[TransactionKind, Query(description="统计收入还是支出")] = TransactionKind.EXPENSE,
    month: MonthQuery = None,
) -> CategoryStatList:
    """分类占比（需要登录，仅统计当前用户）。

    默认统计**支出**（`kind=expense`），因为环形图的常见默认视图是「钱花在哪了」。
    未归类账单在 SQL 里 LEFT JOIN 后以「未归类 / #94A3B8」呈现，保证占比之和为 100%。
    月份非法 422；`kind` 取值非法（不是 income/expense）422。
    前端 `DashboardView.vue` 的环形图（`DonutChart.vue`），点击扇区可下钻到账单列表。
    """
    return await StatsService(session).category_breakdown(current_user.id, _resolve_month(month), kind)


@router.get("/trend", response_model=TrendSeries, summary="近 N 个月收支趋势（用于折线图）")
async def trend(
    current_user: CurrentUser,
    session: SessionDep,
    months: Annotated[int, Query(ge=1, le=24, description="统计月份数")] = 6,
) -> TrendSeries:
    """近 N 个月收支趋势（需要登录，仅统计当前用户）。

    `months` 默认 6，范围 1-24（`ge/le` 在参数层拦截，防止构造超大响应）。
    一次 `GROUP BY 月份` 拿到全部数据，再由 Service 补齐没有账单的空月份为 0；
    响应同时带 `max_expense` / `min_expense`，供前端设置 Y 轴刻度，避免图表抖动。
    200 成功；`months` 越界 422；令牌无效 401。
    前端 `DashboardView.vue` 的趋势折线图（`TrendChart.vue`）。
    """
    return await StatsService(session).trend(current_user.id, months)


@router.get("/calendar", response_model=CalendarSeries, summary="当月每日收支（用于热力图）")
async def calendar(
    current_user: CurrentUser,
    session: SessionDep,
    month: MonthQuery = None,
) -> CalendarSeries:
    """当月每日收支（需要登录，仅统计当前用户）。

    返回当月**每一天**（含无账单的天，金额为 0），前端可直接按 1 号到月末渲染热力图，
    不需要在前端补空档。月份非法 422；令牌无效 401。
    与 `/trend` 的区别：trend 是月粒度、跨多个月；calendar 是日粒度、单个月。
    前端 `DashboardView.vue` 的日历热力图（`CalendarHeatmap.vue`）。
    """
    return await StatsService(session).calendar(current_user.id, _resolve_month(month))
