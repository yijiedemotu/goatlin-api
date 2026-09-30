"""统计 Schema：月度概览、分类占比、趋势。金额统一序列化为字符串。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from app.schemas.common import TransactionKind


class CategoryStat(BaseModel):
    """某分类下的聚合结果。

    数据来源：TransactionRepository.group_by_category 返回的
    (category_id, name, color, SUM(amount), COUNT) 五元组，由 StatsService 组装。
    """

    category_id: int | None = Field(description="分类ID，null 表示未归类")
    category_name: str
    color: str
    kind: TransactionKind
    total: Decimal
    count: int
    # 占「同类型」总额的百分比：income 与 expense 各自成一份 100%，不混算
    percentage: float = Field(description="占同类型总额的百分比，保留 2 位小数")


class MonthlySummary(BaseModel):
    """月度概览：账单页顶部的收支卡片数据。

    balance / saving_rate 都是派生值，前端直接用，不重新计算（口径以后端为准）。
    """

    month: str = Field(description="统计月份 YYYY-MM")
    income: Decimal
    expense: Decimal
    balance: Decimal = Field(description="结余 = 收入 - 支出")
    # 单位是「百分比数值」（60.0 表示 60%），不是 0-1 的小数比例
    saving_rate: float = Field(description="储蓄率 = 结余 / 收入，收入为 0 时为 0")
    transaction_count: int
    # 按「已过去的天数」摊平：当月未结束时用今天做分母，避免被未来空白天数稀释
    avg_daily_expense: Decimal = Field(description="日均支出，按账期天数摊平")


class TrendPoint(BaseModel):
    """趋势曲线上的一个点。"""

    month: str
    income: Decimal
    expense: Decimal
    balance: Decimal


class TrendSeries(BaseModel):
    """近 N 个月趋势（默认 6 个月）。

    months 与 points 一一对应且按时间升序，服务端已补齐无账单的空月份（值为 0），
    前端折线图可直接按下标对齐 x 轴；max/min_expense 供图表算 y 轴范围，
    省掉前端一次 O(n) 扫描（数据量大时这点开销也会被反复计算放大）。
    """

    months: list[str]
    points: list[TrendPoint]
    max_expense: Decimal
    min_expense: Decimal


class DailyPoint(BaseModel):
    """账期日历上的一天。"""

    day: date
    income: Decimal
    expense: Decimal


class CalendarSeries(BaseModel):
    """账期日历热力图数据。

    服务端按自然日补齐整月（包括没有账单的日子），前端只需要按 day 填色，
    不必自己判断「这个月有 28/29/30/31 天」。
    """

    month: str
    days: list[DailyPoint]


class CategoryStatList(BaseModel):
    """某月某方向（收/支）下的分类占比列表。

    items 已按金额倒序排列（排序在 SQL 里用 ORDER BY SUM(amount) DESC 完成），
    前端直接切片取 Top N 画饼图/环图。
    """

    month: str
    items: list[CategoryStat]
