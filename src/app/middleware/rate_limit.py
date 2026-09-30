"""限流中间件：进程内令牌桶 + Redis 固定窗口（多实例共享）。

令牌桶允许突发流量，是网关（Nginx / Sentinel）的主流选择。
Redis 故障时**降级放行并告警**，避免限流组件变成全站单点。

顺序约束：本中间件必须排在 RequestContext 之内、CORS 之内（即 CORS → RequestContext →
本中间件 → 路由）。这样 429 经过外层两位修饰后才返回：带 `Access-Control-Allow-Origin`
（跨域前端读得到）与 `X-Request-ID`（能和访问日志对上）。详见 main.py 的 add_middleware 处。"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import settings
from app.core.exceptions import RateLimitedError
from app.core.logging import get_logger

logger = get_logger(__name__)

# 认证相关接口使用更严格的阈值（默认 10 次/分钟，见 core/config.py）
_AUTH_PATHS = ("/auth/login", "/auth/register", "/auth/refresh")
# 豁免路径：健康探针（K8s 依赖）、文档（开发用）、根路径（服务信息）
# ⚠️ 精确匹配：漏掉 /health/live 会让存活探针被限流成 429（K8s 会摘掉健康实例）
_EXEMPT_PATHS = ("/health", "/health/live", "/docs", "/redoc", "/openapi.json", "/")


class TokenBucket:
    """经典令牌桶：稳态速率 = capacity / window，允许一次性突发 capacity 个请求。

    算法细节（面试可讲）：
    - 桶里有 `capacity` 个令牌，按 `capacity / window` 的速率随时间补充；
    - 每个请求取走 1 个令牌，取不到就拒绝；
    - 桶的补充是**惰性计算**的：不跑定时器，而是在每次请求时用
      `elapsed * rate` 一次性补上，这样零后台任务、零锁开销。
    - 参数含义：`capacity` 同时是「桶容量」和「窗口内允许的请求数」，
      `window` 是补充满一桶所需秒数（本项目 60 秒）。因此 capacity=10、window=60
      的语义是「平均 10 次/分钟，但可以瞬间用掉 10 次」——突发被允许正是它和漏桶的区别。

    面试常问：令牌桶 vs 漏桶 vs 固定窗口
    - 固定窗口：实现最简单，但窗口边界处可能瞬时放行 2 倍流量；
    - 漏桶：恒定速率流出，适合流量整形，但不允许突发；
    - 令牌桶：既能限制平均速率又允许突发，是网关（Nginx limit_req / Sentinel）的主流选择。
    """

    __slots__ = ("capacity", "tokens", "updated_at", "window")

    def __init__(self, capacity: int, window: float) -> None:
        self.capacity = float(capacity)
        self.window = window
        self.tokens = float(capacity)  # 新桶从满开始：新用户不受历史影响
        # monotonic：测「经过时间」必须用单调时钟，否则系统校时会让补充量算错
        self.updated_at = time.monotonic()

    def consume(self, amount: float = 1.0) -> bool:
        """尝试取走 `amount` 个令牌；成功返回 True（**同步方法，不要 await**）。"""
        now = time.monotonic()
        elapsed = now - self.updated_at
        self.updated_at = now
        # 按时间流逝补充令牌，上限为桶容量（min 保证桶不会溢出积累）
        self.tokens = min(self.capacity, self.tokens + elapsed * (self.capacity / self.window))
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False

    def retry_after(self) -> int:
        """估算「还要等几秒才能通过」，用于响应头 `Retry-After`。

        计算：差一个令牌 / 补充速率，向上取整并 +1 秒（给客户端留出边界余量），
        下限为 1——`Retry-After: 0` 会让客户端立刻重试，反而加重压力。
        这是**建议值而非承诺**：客户端按它重试仍可能因并发而再次 429，
        所以客户端侧还应加退避与抖动（jitter）。
        """
        missing = max(1.0 - self.tokens, 0.0)
        return max(1, int(missing / (self.capacity / self.window)) + 1)


class InMemoryRateLimiter:
    """进程内限流器。key = 客户端标识 + 限流档位。

    生命周期：跟随后端实例存活（`self._buckets` 是进程内字典），
    因此它的计数**不跨实例、重启即清零**——这也是水平扩容后总配额翻倍的根本原因。

    注意 `check` 是 async：与 RedisRateLimiter 保持同一接口，
    中间件里统一 `await`，避免「两种实现一个是同步一个是异步」的隐式契约。
    """

    def __init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._last_gc = time.monotonic()

    async def check(self, key: str, limit: int) -> tuple[bool, int]:
        """返回 `(是否放行, 建议等待秒数)`，签名与 RedisRateLimiter 完全一致。

        **真实事故记录（务必理解这个教训）**：
        这个方法是 `async def`，但内部逻辑全是同步的（字典操作 + 令牌计算）。
        早期版本里方法签名是 `async def check`，而 `TokenBucket.consume`
        被误写成 `await bucket.consume()`——`await` 一个返回 `bool` 的**同步方法**，
        会在运行时抛 `TypeError: object bool can't be used in 'await' expression`。
        因为限流中间件包住了**每一个**请求，这个异常让全站请求无一例外地 500，
        等于服务整体挂掉；而当时测试套件默认关闭限流（`RATE_LIMIT_ENABLED=false`
        以免用例相互干扰），这条路径**完全没有覆盖**，所以 bug 一路溜到了运行时。

        教训三条：
        1. 中间件/拦截器里的任何异常都是「全站级故障」，必须当核心链路对待；
        2. 「测试里默认关闭的开关」= 未被测试的代码路径，至少要有专门用例打开它
           （现在 `tests/test_rate_limit.py` 用独立的 `create_app()` + 收紧阈值钉住这条路）；
        3. 用 mypy strict 能在编译期抓住大部分 `await` 误用，但动态鸭子类型
           （`limiter: object | None`）会绕过它——同一接口约定需要靠测试而非类型守护。
        """
        bucket = self._buckets.get(key)
        # 阈值热更新（配置变更/测试收紧阈值）时重建桶：
        # 否则旧的 capacity 会一直被沿用，改配置不生效。
        if bucket is None or bucket.capacity != float(limit):
            bucket = TokenBucket(limit, settings.rate_limit_window_seconds)
            self._buckets[key] = bucket
        self._gc()
        # 注意：consume 是同步方法，直接调用；写成 await 就是上面记录的事故
        allowed = bucket.consume()
        return allowed, bucket.retry_after()

    def _gc(self) -> None:
        """定期清理空闲桶，防止任意 IP 刷接口导致内存无限增长（内存泄漏）。

        为什么需要：字典键是「客户端 IP + 档位」，攻击者换个 IP 就多一个键；
        不清理的话，一个长期运行的服务会被几十万个只访问过一次的桶慢慢吃光内存。
        策略是「惰性 GC」：不另起线程/定时器，而是搭在每个请求上，
        每 300 秒最多扫一次，清掉 10 分钟没动过的桶（时间窗远大于限流窗口，
        不会误删仍在计数的活跃桶）。
        """
        now = time.monotonic()
        if now - self._last_gc < 300:
            return
        self._last_gc = now
        stale = [k for k, b in self._buckets.items() if now - b.updated_at > 600]
        for key in stale:
            self._buckets.pop(key, None)


class RedisRateLimiter:
    """Redis 固定窗口计数：INCR + 首次设置 EXPIRE，多实例共享配额。

    算法细节（面试可讲）：
    - `bucket = int(time.time()) // window` 把时间切成固定长度的窗口；
    - key 形如 `ratelimit:<客户端:档位>:<窗口编号>`，天然按窗口分片，
      新窗口自动是一条新 key，**不需要显式重置计数**；
    - `INCR` 返回自增后的计数，`<= limit` 即放行，否则拒绝；
    - `EXPIRE window+1` 兜底回收：让旧窗口的 key 自动过期，
      否则会被永久残留（内存泄漏）。+1 秒是为了跨窗口边界时 key 不至于先过期再被读到。
    - `pipeline(transaction=True)` 把 INCR 与 EXPIRE 合成一次往返（MULTI/EXEC），
      既省 RTT 又保证两条命令之间不会被其他客户端插队。
    - 缺点（固定窗口的固有缺陷）：窗口边界处可能出现瞬时 2 倍流量——
      第 59 秒打满，第 61 秒进入新窗口又能打满。要消除它就得用滑动窗口/令牌桶，
      代价是复杂度与内存（需要存时间戳列表或用 Redis 的 ZSET/Lua）。

    生命周期：进程级单例（在 RateLimitMiddleware.__init__ 里构造），
    内部异步连接池懒建立；`close()` 用于应用关闭时释放连接。
    """

    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis  # 延迟导入：未安装 redis 时不报错

        # decode_responses=True：让返回值是 str 而不是 bytes，省去每处 .decode()
        self._client = aioredis.from_url(url, encoding="utf-8", decode_responses=True)

    async def check(self, key: str, limit: int) -> tuple[bool, int]:
        """固定窗口计数并判定；Redis 异常时**降级放行**。"""
        window = settings.rate_limit_window_seconds
        bucket = int(time.time()) // window  # 当前窗口编号，换窗口 = 换 key = 计数归零
        redis_key = f"ratelimit:{key}:{bucket}"
        try:
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.incr(redis_key)
                pipe.expire(redis_key, window + 1)
                count, _ = await pipe.execute()
        except Exception as exc:  # Redis 故障时降级放行，避免限流组件成为单点
            logger.warning("rate_limit_backend_error", error=str(exc))
            return True, 0

        if int(count) <= limit:
            return True, 0
        # 等到本窗口结束：距离下一个窗口还有多少秒，至少 1 秒
        retry_after = window - int(time.time()) % window
        return False, max(retry_after, 1)

    async def close(self) -> None:
        """释放 Redis 连接池（由应用关闭钩子或测试调用）。"""
        await self._client.aclose()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """限流中间件的装配与判定层：选择后端、算限流键与阈值、生成 429 响应。

    设计取舍：把「用哪个后端」（`limiter`）与「怎么算键/阈值」（`_client_key` /
    `_limit_for`）分开，后者是纯静态方法、无状态，可以单独单测，不必构造 Redis。
    `limiter` 参数允许注入自定义实现（测试里可塞入一个永远拒绝的假限流器）。
    """

    def __init__(self, app: object, limiter: object | None = None) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        if limiter is not None:
            # 测试注入点：不必真的连 Redis 就能验证 429 分支
            self.limiter = limiter
        elif settings.redis_url:
            # 只要配了 REDIS_URL 就切到分布式实现——这就是「改配置即切换」
            self.limiter = RedisRateLimiter(settings.redis_url)
        else:
            # 开发/单机默认：零外部依赖，开箱即跑
            self.limiter = InMemoryRateLimiter()

    @staticmethod
    def _client_key(request: Request) -> str:
        """优先取 X-Forwarded-For 首段（经过网关时），否则取直连 IP。

        安全提醒：`X-Forwarded-For` 是客户端可伪造的头。若服务直接暴露在公网，
        攻击者只要每次换一个该头的值就能绕过限流。生产应确保它由可信网关覆写，
        或改用网关自身生成的不可伪造标识。
        """
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()  # 最左段是原始客户端
        return request.client.host if request.client else "unknown"

    @staticmethod
    def _limit_for(path: str) -> int:
        """按路径选档位：认证接口用严格阈值，其余用默认阈值。"""
        if any(path.endswith(p) for p in _AUTH_PATHS):
            return settings.rate_limit_auth_per_minute
        return settings.rate_limit_default_per_minute

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # 全局开关 + 探针豁免：健康探针被限流会导致 K8s 误判实例不可用
        if not settings.rate_limit_enabled or request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        # 限流键 = 客户端 + 档位（认证档与默认档各自独立计数）
        key = f"{self._client_key(request)}:{'auth' if self._is_auth(request) else 'default'}"
        allowed, retry_after = await self.limiter.check(key, self._limit_for(request.url.path))  # type: ignore[attr-defined]

        if not allowed:
            error = RateLimitedError(detail={"retry_after": retry_after})
            return JSONResponse(
                status_code=error.status_code,
                content=error.to_payload(getattr(request.state, "request_id", None)),
                # Retry-After 是 HTTP 标准头（单位秒）：告诉客户端「多久后可以重试」，
                # 良性的客户端/爬虫会遵守它，比返回裸 429 更友好，也更快恢复。
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)

    @staticmethod
    def _is_auth(request: Request) -> bool:
        """判断是否为认证类接口（决定用哪档阈值、也算进限流键）。"""
        return any(request.url.path.endswith(p) for p in _AUTH_PATHS)
