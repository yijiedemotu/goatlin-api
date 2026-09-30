"""账单 Schema：创建、更新、查询过滤、分页响应。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import Field, field_validator, model_validator

from app.schemas.category import CategoryOut
from app.schemas.common import ORMModel, TransactionKind

AmountField = Annotated[Decimal, Field(gt=0, le=Decimal("100000000"), decimal_places=2, max_digits=12)]


class TransactionCreate(ORMModel):
    """新增账单的请求体（POST /transactions）。

    category_id 可空表示「未归类」，此时不做分类方向校验；
    一旦传了分类，TransactionService 会校验它属于当前用户且 kind 与账单一致
    （防止把「工资」挂到支出上破坏统计口径）。
    """

    amount: AmountField
    kind: TransactionKind
    category_id: int | None = None
    occurred_on: date = Field(description="账期，格式 YYYY-MM-DD")
    note: str = Field(default="", max_length=500)

    @field_validator("note")
    @classmethod
    def _strip_note(cls, value: str) -> str:
        """去掉备注首尾空白后返回。

        为什么连备注都要规范化：CSV 导入用 (账期, 金额, 备注) 做去重指纹，
        空白差异会让同一条账单被当成两次导入。
        """
        # 备注两端空白没有信息量，统一清掉也便于导入去重指纹（账期, 金额, 备注）保持一致
        return value.strip()

    @field_validator("occurred_on")
    @classmethod
    def _not_future(cls, value: date) -> date:
        """校验账期不晚于今天，返回原日期。

        异常：账期在未来时抛 ValueError（→ 422），因为未来账单会让「本月支出」等口径失真。
        """
        # 账期是「已经发生的一天」，未来日期通常是用户手滑或客户端时区错误，入口直接拒绝
        if value > date.today():
            raise ValueError("账期不能晚于今天")
        return value


class TransactionUpdate(ORMModel):
    """更新账单的请求体（PATCH /transactions/{id}）：PATCH 语义，全部字段可选。

    用 model_fields_set 而不是「是否等于 None」判断用户到底传了什么：
    category_id=None 是合法意图（把账单改为未归类），必须与「没传这个字段」区分开。
    """

    amount: AmountField | None = None
    kind: TransactionKind | None = None
    category_id: int | None = None
    occurred_on: date | None = None
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> TransactionUpdate:
        """拒绝空 body 的更新请求，返回校验通过的自身。

        异常：model_fields_set 为空（即请求体是 {}）时抛 ValueError（→ 422）。
        """
        # 空 body（{}）会让更新变成无意义的空操作，明确报错而不是返回 200 误导调用方
        if not self.model_fields_set:
            raise ValueError("至少需要提供一个待更新字段")
        return self


class TransactionOut(ORMModel):
    """账单的对外表示：列表项、详情与创建/更新响应共用同一个结构。

    前端字段用法（goalfin-web）：amount 是字符串，用 toCents() 转「分」后再展示与求和；
    category 用于显示分类名与颜色（列表接口靠 selectinload 一次带出，避免 N+1）；
    category 为 null 时归入「未归类」；occurred_on 决定它落在哪个统计月份。
    """

    id: int
    amount: Decimal
    kind: TransactionKind
    category_id: int | None
    # 可空：分类被删除后外键被置为 NULL，历史账单仍要能正常序列化
    category: CategoryOut | None = None
    occurred_on: date
    note: str
    source: str


class TransactionQuery(ORMModel):
    """查询过滤器（作为 Query 参数注入，见 api/deps.py）。

    由路由层的 transaction_filters 依赖把散落的 Query 参数组装成这个对象，
    再交给 TransactionRepository.build_filters 逐条翻译成 WHERE 条件。
    好处：过滤维度增加时只改这一个模型与一处 build_filters，不用改 Service 签名。
    """

    start_date: date | None = None
    end_date: date | None = None
    kind: TransactionKind | None = None
    category_id: int | None = None
    min_amount: Decimal | None = Field(default=None, ge=0)
    max_amount: Decimal | None = Field(default=None, ge=0)
    keyword: str | None = Field(default=None, max_length=50, description="备注模糊搜索")

    @model_validator(mode="after")
    def _check_range(self) -> TransactionQuery:
        """校验区间类参数的先后关系，返回校验通过的自身。

        异常：start_date > end_date 或 min_amount > max_amount 时抛 InvalidDateRangeError
        （业务异常 → 400，比 422 更贴合「参数组合不合法」的语义）。
        """
        # 局部导入避免 schemas ↔ core.exceptions 在模块加载期形成循环依赖
        from app.core.exceptions import InvalidDateRangeError

        # 区间反了的查询条件在 SQL 里永远匹配不到行，必须当作参数错误报出来
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise InvalidDateRangeError()
        if self.min_amount is not None and self.max_amount is not None and self.min_amount > self.max_amount:
            raise InvalidDateRangeError("最小金额不能大于最大金额")
        return self
