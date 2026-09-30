"""API 层（HTTP 边界）的包入口，只负责把 v1 路由树向上暴露。

在链路中的位置：
    `app/main.py::create_app()` → `from app.api import api_router` → `app.include_router(...)`
    → `app/api/v1/__init__.py` 把各业务路由聚合进 `api_router`。
    本文件自身不做任何装配（不带 prefix、不加中间件），真正的挂载动作在 main.py，
    这样 `create_app()` 才能对「前缀 /docs / 中间件顺序」有唯一控制点。

设计取舍 / 面试可讲：
1. **为什么分 `api/` 与 `api/v1/` 两层**：`api/` 是「层」的边界（未来可加 `api/v2`、
   内部管理 API），`api/v1/` 是「版本」的边界。URL 版本化让破坏性变更有共存空间：
   `/api/v1/transactions` 改字段时可并行发布 `/api/v2`，老客户端不被强杀。
2. **为什么用 `from app.api.v1 import api_router` 再转出，而不是本文件 new 一个 APIRouter**：
   让 main.py 只依赖一个稳定符号；路由增删只改 v1 包，不改装配代码（开闭原则）。
3. **`__all__` 的作用**：声明本包的公共导出面，`from app.api import *` 只带出
   `api_router`。它同时是给 IDE / mypy 的类型边界提示，不是运行时必需，但改动它
   属于接口变更，须同步调用方。
4. 本文件不导入任何 Service / Repository：反向依赖（api → services → repositories → models）
   是单向的，包入口保持「薄」，避免 import 环。
"""

from app.api.v1 import api_router

__all__ = ["api_router"]
