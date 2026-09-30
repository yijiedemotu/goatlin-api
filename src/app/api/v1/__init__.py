"""v1 路由聚合：把各业务模块的 APIRouter 挂到同一棵树上。"""

from fastapi import APIRouter

from app.api.v1 import auth, categories, csv_io, stats, transactions

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(categories.router)
# CSV 路由必须早于 transactions 注册：/transactions/csv/... 需要先匹配静态前缀
# （否则 /transactions/{transaction_id} 会先吃掉请求，把 "csv" 当成 int 解析并 422）
api_router.include_router(csv_io.router)
api_router.include_router(transactions.router)
api_router.include_router(stats.router)

__all__ = ["api_router"]
