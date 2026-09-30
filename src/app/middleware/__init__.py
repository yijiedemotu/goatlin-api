"""自定义中间件包：请求上下文 与 限流。

在链路中的位置：
    `app/main.py` 按顺序 `add_middleware`：CORS → RateLimit → RequestContext。
    Starlette 内部用 `insert(0, ...)` 保存，所以**最后 add 的最先执行（最外层）**，
    真实顺序是 RequestContext（最外层）→ RateLimit → CORS → 路由，返回时反向穿出。
    本包只做「聚合导出」，真正的实现在 `context.py` / `rate_limit.py`。

设计取舍 / 面试可讲：
1. **中间件 vs 依赖**：中间件能作用于「连路由都没匹配上」的请求（如 404、限流拦截），
   依赖只能在路由匹配成功后执行。所以「分配 request_id」和「限流」必须是中间件，
   而「当前用户」可以是依赖（它只需要作用于已匹配的业务路由，且要参与 OpenAPI 文档）。
2. **顺序决定语义**：RequestContext 在最外层，所以限流拒绝的 429、
   CORS 预检响应都能带上 `X-Request-ID`，全站可追踪。
   反例：本项目 CORS 是最先 `add_middleware` 的，因此落在最内层，
   被限流短路返回的 429 不经过它、拿不到 CORS 头（浏览器只会报 CORS 错误）——
   这是「顺序即语义」最容易踩的坑，详见 `rate_limit.py` 顶部说明。
3. **为什么独立成包**：中间件是典型的横切关注点，与业务零耦合；独立包让
   `app/main.py` 的装配代码只做「选择与排序」，而不是「实现」。
4. 本文件只导出两个中间件与一个内存限流器实现，`RedisRateLimiter` 不在此导出——
   它依赖可选包 `redis`，只在 `rate_limit.py` 内部按配置延迟构造。
"""

from app.middleware.context import RequestContextMiddleware
from app.middleware.rate_limit import InMemoryRateLimiter, RateLimitMiddleware

__all__ = ["InMemoryRateLimiter", "RateLimitMiddleware", "RequestContextMiddleware"]
