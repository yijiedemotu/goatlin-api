"""账单路由：列表（多条件过滤 + 分页）、详情、增删改。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import CurrentUser, PaginationDep, SessionDep
from app.schemas.common import Page, TransactionKind
from app.schemas.transaction import (
    TransactionCreate,
    TransactionOut,
    TransactionQuery,
    TransactionUpdate,
)
from app.services.transaction_service import TransactionService

router = APIRouter(prefix="/transactions", tags=["账单"])


def transaction_filters(
    start_date: Annotated[str | None, Query(description="起始账期 YYYY-MM-DD")] = None,
    end_date: Annotated[str | None, Query(description="结束账期 YYYY-MM-DD")] = None,
    kind: Annotated[TransactionKind | None, Query(description="收入/支出")] = None,
    category_id: Annotated[int | None, Query(description="分类ID")] = None,
    min_amount: Annotated[float | None, Query(ge=0, description="最小金额")] = None,
    max_amount: Annotated[float | None, Query(ge=0, description="最大金额")] = None,
    keyword: Annotated[str | None, Query(max_length=50, description="备注关键词")] = None,
) -> TransactionQuery:
    """把散落的 Query 参数收敛成一个强类型查询对象（含合法区间校验）。

    生命周期：每次请求执行一次（FastAPI 对同一请求内的依赖结果做缓存）。
    为什么独立成依赖：列表接口签名从 7 个参数变成 1 个，且校验逻辑可被复用/单测。

    校验分层：
    - 单字段边界由 `Query(ge=...)` / `max_length` 在框架层拦截（→ 422）；
    - 跨字段规则（起始日期不能晚于结束日期、最小金额不能大于最大金额）写在
      `TransactionQuery._check_range` 里（→ 400 `INVALID_DATE_RANGE`），
      因为「谁先谁后」这种规则只有拿到全部字段才能判断（model_validator 的典型用途）。
    """
    from datetime import date

    def _parse(value: str | None, field: str) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            from app.core.exceptions import BadRequestError

            # detail 带上字段名与原始值：前端表单可精确标红出错的输入框
            raise BadRequestError(f"{field} 需为 YYYY-MM-DD 格式", detail={field: value}) from None

    return TransactionQuery(
        start_date=_parse(start_date, "start_date"),
        end_date=_parse(end_date, "end_date"),
        kind=kind,
        category_id=category_id,
        min_amount=min_amount,
        max_amount=max_amount,
        keyword=keyword,
    )


FiltersDep = Annotated[TransactionQuery, Depends(transaction_filters)]


@router.get("", response_model=Page[TransactionOut], summary="账单列表（多条件过滤 + 分页）")
async def list_transactions(
    current_user: CurrentUser,
    session: SessionDep,
    filters: FiltersDep,
    page: PaginationDep,
) -> Page[TransactionOut]:
    """账单列表（需要登录，只返回自己的账单）。

    过滤维度：账期区间、收支方向、分类、金额区间、备注关键词（全部可选，AND 组合）。
    分页：`page`（从 1 开始）+ `size`（1-100）；响应是 `Page` 信封
    `{items, total, page, size}`，`total` 与 `items` 复用同一套过滤条件。
    排序固定为 `occurred_on DESC, id DESC`（同一天按录入顺序倒序，保证分页稳定不跳行）。
    200 成功；筛选参数非法 400 / 422；令牌无效 401。
    前端 `TransactionsView.vue` 的列表与筛选栏（每次筛选变化都重新拉这一页）。
    """
    rows, total = await TransactionService(session).list(
        current_user.id, filters, page=page.page, size=page.size
    )
    return Page[TransactionOut](
        # 逐行 model_validate：ORM 对象 → 响应模型（含嵌套 category），
        # 显式构造而不是直接返回 ORM，避免把未声明的字段漏给前端
        items=[TransactionOut.model_validate(row) for row in rows],
        total=total,
        page=page.page,
        size=page.size,
    )


@router.post(
    "",
    response_model=TransactionOut,
    status_code=status.HTTP_201_CREATED,
    summary="新增账单",
    responses={400: {"description": "分类与收支类型不匹配"}},
)
async def create_transaction(
    payload: TransactionCreate, current_user: CurrentUser, session: SessionDep
) -> TransactionOut:
    """新增账单（需要登录，归属当前用户）。

    201 成功；`source` 由服务端固定写为 `"manual"`（区分手工录入与 CSV 导入）。
    400 `BAD_REQUEST`：分类的收支类型与账单方向不一致（例如把「工资」挂到支出）；
    404 `NOT_FOUND`：`category_id` 不属于当前用户（越权 → 当作不存在）；
    422：金额 <= 0 / 超过一亿 / 账期晚于今天（校验在 `TransactionCreate`）。
    前端 `TransactionFormModal.vue` 的「记一笔」表单。
    """
    tx = await TransactionService(session).create(current_user.id, payload)
    return TransactionOut.model_validate(tx)


@router.get("/{transaction_id}", response_model=TransactionOut, summary="账单详情")
async def get_transaction(
    transaction_id: int, current_user: CurrentUser, session: SessionDep
) -> TransactionOut:
    """账单详情（需要登录 + 归属校验）。

    不属于当前用户的 id 返回 404 `NOT_FOUND`（不返回 403，避免暴露资源是否存在）。
    响应结构与列表项完全一致（同一个 `TransactionOut`），前端可以复用同一个渲染组件。
    前端 `TransactionsView.vue` 点击某行查看/编辑时预取详情。
    """
    tx = await TransactionService(session).get(current_user.id, transaction_id)
    return TransactionOut.model_validate(tx)


@router.patch("/{transaction_id}", response_model=TransactionOut, summary="更新账单")
async def update_transaction(
    transaction_id: int,
    payload: TransactionUpdate,
    current_user: CurrentUser,
    session: SessionDep,
) -> TransactionOut:
    """部分更新账单（需要登录 + 归属校验）。

    PATCH 语义 = 只改传了的字段；`TransactionUpdate` 用 `model_validator` 强制
    「至少提供一个字段」，否则 422（防止空 PATCH 被静默当成成功）。
    只改 `kind` 而保留原分类时，Service 会重新校验分类方向是否仍然匹配（400）。
    404（不存在/越权）、422（字段非法）与创建接口一致。
    前端 `TransactionFormModal.vue` 的编辑提交。
    """
    tx = await TransactionService(session).update(current_user.id, transaction_id, payload)
    return TransactionOut.model_validate(tx)


@router.delete("/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除账单")
async def delete_transaction(transaction_id: int, current_user: CurrentUser, session: SessionDep) -> None:
    """删除账单（需要登录 + 归属校验）。

    204 无响应体；不存在或不属于当前用户 → 404 `NOT_FOUND`。
    删除是硬删除（不是软删除）——历史统计会随之变化，这是记账场景的预期行为；
    若需要「回收站」，应改为软删除并在所有查询里加 `deleted_at IS NULL`（面试可对比取舍）。
    前端 `TransactionTable.vue` 的删除按钮（带二次确认）。
    """
    await TransactionService(session).delete(current_user.id, transaction_id)
