"""分类 Schema。"""

from __future__ import annotations

from pydantic import Field, field_validator

from app.schemas.common import ORMModel, TransactionKind


class CategoryCreate(ORMModel):
    """新建分类的请求体（POST /categories）。由 CategoryService 落库，is_system 恒为 False。

    输入契约：name 必填 1-32 字符；kind 必填且必须是 income/expense；
    color / icon 可选，不传时由下面的默认值补齐（与 Category 模型列的默认值一致）。
    """

    name: str = Field(min_length=1, max_length=32)
    kind: TransactionKind
    # 默认色与 Category 模型列的 default 保持一致，避免「不传颜色时前后端各存一套默认值」
    color: str = Field(default="#6366F1", pattern=r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
    icon: str = Field(default="wallet", max_length=32)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        """去掉首尾空白并拒绝空名字；返回 strip 后的值，让「用户看到的」与「库里存的」一致。

        异常：strip 后为空时抛 ValueError，被 FastAPI 统一转成 422 响应。
        """
        # 先 strip 再判空：拦掉 "   " 这种通过了 min_length 但实际无意义的分类名
        stripped = value.strip()
        if not stripped:
            raise ValueError("分类名不能为空")
        return stripped


class CategoryUpdate(ORMModel):
    """更新分类的请求体（PATCH /categories/{id}）：全部字段可选，只改传入的字段。

    注意这里没有 kind：变更收支方向会让已挂靠的账单方向不一致，业务上不开放。
    """

    name: str | None = Field(default=None, min_length=1, max_length=32)
    color: str | None = Field(default=None, pattern=r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
    icon: str | None = Field(default=None, max_length=32)


class CategoryOut(ORMModel):
    """分类的对外表示：列表、详情以及 TransactionOut.category 都复用它。

    前端用它渲染分类标签（name + color + icon），并根据 is_system 隐藏删除按钮。
    """

    id: int
    name: str
    kind: TransactionKind
    color: str
    icon: str
    is_system: bool
