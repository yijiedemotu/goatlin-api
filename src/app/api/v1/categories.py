"""分类路由。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status
from pydantic import BaseModel

from app.api.deps import CurrentUser, SessionDep
from app.schemas.category import CategoryCreate, CategoryOut, CategoryUpdate
from app.schemas.common import TransactionKind
from app.services.category_service import CategoryService

router = APIRouter(prefix="/categories", tags=["分类"])


class CategoryWithUsage(CategoryOut):
    """列表接口额外返回账单数，前端可提示「该分类下有 N 笔」。

    为什么定义在路由文件而不是 `schemas/`：它只服务这一个接口的展示需求，
    属于「接口视图模型」而非领域模型；放在使用点旁边，改动时不会误伤其他接口。
    `usage_count` 默认 0，是为了让 `CategoryOut.model_validate(...)` 的字段
    （不含 usage_count）也能填进来——调用方用 `getattr(category, "usage_count", 0)` 兜底。
    """

    usage_count: int = 0


class CategoryKindOption(BaseModel):
    """前端下拉选项模型：`{value, label}`。

    为什么要「枚举 → 选项」再包一层：前端不应硬编码中文标签与取值映射；
    由后端下发选项，将来加「转账」这类新类型时前端不改代码。
    """

    value: str
    label: str


# 注意：静态路径必须注册在 /{category_id} 之前，
# 否则 "kinds" 会被当作 category_id 去匹配并触发 422（FastAPI 按注册顺序匹配）。
# 这就是 tests/test_categories.py 里专门有一条用例守着它的原因：顺序改动无法被类型检查发现。
@router.get("/kinds", response_model=list[CategoryKindOption], summary="收支类型枚举（前端下拉用）")
async def list_kinds() -> list[CategoryKindOption]:
    """返回收支类型枚举（公开语义接口，实际仍需登录，因为挂在全局依赖之外但它自身不需要用户）。

    不查库、不鉴权、无参数：数据源是代码里的枚举 `TransactionKind`，
    因此这是「最便宜的接口」，也是验证「静态路由未被 `/{category_id}` 抢占」的探针。
    200 返回 `[{value:"income",label:"收入"},{value:"expense",label:"支出"}]`。
    前端 `api/categories.ts::kinds` 已封装本接口；实际渲染时
    `TransactionFilters.vue` / `TransactionFormModal.vue` 目前用本地常量做类型下拉，
    所以这是「接口先行、前端可按需切换」的预留能力。
    """
    return [
        CategoryKindOption(value=TransactionKind.INCOME.value, label="收入"),
        CategoryKindOption(value=TransactionKind.EXPENSE.value, label="支出"),
    ]


@router.get("", response_model=list[CategoryWithUsage], summary="列出当前用户的分类")
async def list_categories(
    current_user: CurrentUser,
    session: SessionDep,
    kind: Annotated[TransactionKind | None, Query(description="按收支类型过滤")] = None,
    with_usage: Annotated[bool, Query(description="是否统计每个分类下的账单数")] = False,
) -> list[CategoryWithUsage]:
    """列出当前用户的分类（需要登录，只返回自己的数据）。

    `kind` 可选：不传返回全部；`with_usage=true` 时额外统计每个分类下的账单数
    （用于删除前提示，走一次批量聚合而不是 N 次 COUNT，见 `CategoryRepository`）。
    200 成功；令牌无效 401。

    路由里的拼接逻辑只做「ORM → 视图模型」的字段搬运：把 `usage_count`
    从 Service 挂到 ORM 实例上的临时属性取出来合并进响应，不参与任何业务判断。
    前端 `CategoriesView.vue`（管理页）、`TransactionFormModal.vue`（记账时选分类）。
    """
    categories = await CategoryService(session).list_categories(
        current_user.id, kind=kind.value if kind else None, with_usage=with_usage
    )
    return [
        # Query 参数是枚举对象，落库比较的是字符串值，所以这里显式 .value
        CategoryWithUsage(
            **CategoryOut.model_validate(category).model_dump(),
            usage_count=getattr(category, "usage_count", 0),
        )
        for category in categories
    ]


@router.post(
    "",
    response_model=CategoryOut,
    status_code=status.HTTP_201_CREATED,
    summary="新建分类",
    responses={409: {"description": "同名分类已存在"}},
)
async def create_category(
    payload: CategoryCreate, current_user: CurrentUser, session: SessionDep
) -> CategoryOut:
    """新建分类（需要登录，归属当前用户）。

    201 成功；同名（同用户 + 同收支类型）已存在 → 409 `CATEGORY_NAME_CONFLICT`；
    入参非法（名称为空/超长、颜色格式错）→ 422 `VALIDATION_ERROR`。
    新分类 `is_system=False`，代表用户自建，因此可删除（系统预置分类不可删）。

    并发注意：Service 先查重再插入存在竞态，最终由 `(user_id, name, kind)` 唯一约束兜底，
    捕获 IntegrityError 后转成同一个 409，这条「先查后插 + 唯一索引收口」是标准写法。
    前端 `CategoriesView.vue` 的「新建分类」弹窗。
    """
    category = await CategoryService(session).create(current_user.id, payload)
    return CategoryOut.model_validate(category)


@router.patch("/{category_id}", response_model=CategoryOut, summary="更新分类")
async def update_category(
    category_id: int, payload: CategoryUpdate, current_user: CurrentUser, session: SessionDep
) -> CategoryOut:
    """更新分类（需要登录 + 归属校验）。

    `category_id` 不是当前用户的分类时返回 **404 `NOT_FOUND`** 而不是 403：
    对外不暴露「这个 id 存在但不属于你」，避免攻击者用状态码差异枚举他人资源 id。
    改名撞同名 → 409 `CATEGORY_NAME_CONFLICT`。
    只允许改 name/color/icon，`kind` 与 `is_system` 不可改（否则会破坏既有账单的方向一致性）。
    前端 `CategoriesView.vue` 编辑弹窗。
    """
    category = await CategoryService(session).update(current_user.id, category_id, payload)
    return CategoryOut.model_validate(category)


@router.delete(
    "/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除分类（系统分类或仍被账单引用时拒绝）",
)
async def delete_category(category_id: int, current_user: CurrentUser, session: SessionDep) -> None:
    """删除分类（需要登录 + 归属校验）。

    204 无响应体（删除成功不需要回传数据，前端 `client.ts` 对 204 直接返回 undefined）。
    两类拒绝都在 Service 里，均返回 409 `CATEGORY_IN_USE`：
      - 系统预置分类（`is_system=True`，注册时自动创建的那 10 个）不可删；
      - 分类下仍有账单时提示「还有 N 笔」，要求先迁移或删除。
    越权访问他人分类 → 404 `NOT_FOUND`（与更新接口一致）。
    前端 `CategoriesView.vue` 列表的删除按钮。
    """
    await CategoryService(session).delete(current_user.id, category_id)
