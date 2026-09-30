"""Pydantic Schema 层：请求校验与响应序列化的唯一入口。
Pydantic模型，请求/响应序列化（DTO）
"""

from app.schemas.category import CategoryCreate, CategoryOut, CategoryUpdate
from app.schemas.common import (
    HealthResponse,
    MessageResponse,
    ORMModel,
    Page,
    PageParams,
    TransactionKind,
)
from app.schemas.csv_io import ExportQuery, ImportResult
from app.schemas.stats import (
    CalendarSeries,
    CategoryStat,
    CategoryStatList,
    DailyPoint,
    MonthlySummary,
    TrendPoint,
    TrendSeries,
)
from app.schemas.transaction import (
    TransactionCreate,
    TransactionOut,
    TransactionQuery,
    TransactionUpdate,
)
from app.schemas.user import (
    RefreshRequest,
    TokenPair,
    UserLogin,
    UserOut,
    UserRegister,
    UserUpdate,
)

__all__ = [
    "CalendarSeries",
    "CategoryCreate",
    "CategoryOut",
    "CategoryStat",
    "CategoryStatList",
    "CategoryUpdate",
    "DailyPoint",
    "ExportQuery",
    "HealthResponse",
    "ImportResult",
    "MessageResponse",
    "MonthlySummary",
    "ORMModel",
    "Page",
    "PageParams",
    "RefreshRequest",
    "TokenPair",
    "TransactionCreate",
    "TransactionKind",
    "TransactionOut",
    "TransactionQuery",
    "TransactionUpdate",
    "TrendPoint",
    "TrendSeries",
    "UserLogin",
    "UserOut",
    "UserRegister",
    "UserUpdate",
]
