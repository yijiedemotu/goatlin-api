"""CSV 导入导出 Schema。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RowError(BaseModel):
    """单行导入失败详情：定位到行号，方便用户回文件里修改。"""

    row: int
    reason: str
    raw: str = ""


class ImportResult(BaseModel):
    total: int = Field(description="解析到的数据行数")
    imported: int
    failed: int
    errors: list[RowError] = Field(default_factory=list, description="最多返回前 50 条错误")


class ExportQuery(BaseModel):
    start_date: str | None = None
    end_date: str | None = None
