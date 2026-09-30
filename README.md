# Goalfin · 个人记账与财务分析平台（后端 API）

> 一个可直接写进简历、也能在面试里讲 40 分钟的后端项目：
> **FastAPI + SQLAlchemy 2.0 异步 + JWT 双令牌 + Redis 限流 + Alembic 迁移 + 85 个自动化测试**。
> 零依赖即可跑通（默认 SQLite），一条命令切换 PostgreSQL。

<p>
<img alt="python" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white">
<img alt="fastapi" src="https://img.shields.io/badge/FastAPI-0.141-009688?logo=fastapi&logoColor=white">
<img alt="sqlalchemy" src="https://img.shields.io/badge/SQLAlchemy-2.1-D71F00">
<img alt="tests" src="https://img.shields.io/badge/tests-85%20passed-brightgreen">
<img alt="coverage" src="https://img.shields.io/badge/coverage-80%25-green">
<img alt="mypy" src="https://img.shields.io/badge/mypy-strict-blue">
<img alt="ruff" src="https://img.shields.io/badge/lint-ruff-261230">
</p>

---

## 一、这是什么

多用户记账后端服务：用户注册登录后可维护自己的收支分类与账单，按条件检索账单，
查看月度概览 / 分类占比 / 收支趋势 / 日历热力图，并把账单导出为 CSV 或从 CSV 批量导入。

它解决的是「一个真实后端该有的完整闭环」：

| 维度 | 本项目的落地方式 |
| --- | --- |
| 分层架构 | `api（路由）→ service（业务）→ repository（数据访问）→ models（ORM）`，依赖单向 |
| 鉴权 | bcrypt 密码哈希 + JWT access/refresh 双令牌 + 归属校验防水平越权 |
| 数据一致性 | Service 统一管理事务边界、唯一约束兜底并发、金额全程 `Decimal` |
| 性能 | SQL 聚合替代内存循环、`selectinload` 消除 N+1、复合索引、批量插入 |
| 稳定性 | 令牌桶限流、分层异常 + 统一错误结构、request_id 全链路日志 |
| 工程化 | Alembic 迁移、Docker 多阶段构建、GitHub Actions 三阶段流水线、Makefile 一键命令 |
| 质量 | 85 个测试、80% 覆盖率、ruff 全绿、mypy strict 零报错 |

---

## 二、30 秒跑起来

```bash
# 1) 准备环境
python -m venv .venv
.venv\Scripts\activate          # Windows；macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"

# 2) 配置 + 建库
copy .env.example .env           # macOS/Linux: cp .env.example .env
alembic upgrade head             # 迁移建表（推荐）
# 或（开发快速重置）: python -m app.db.init_db --drop

# 3) 启动
uvicorn app.main:app --reload --port 8000
```

打开 <http://127.0.0.1:8000/docs> 即可看到交互式 API 文档（20 个路由）。

```bash
# 4) 端到端自测（另一个终端）
python scripts/smoke_test.py --base-url http://127.0.0.1:8000
```

**质量校验三连**（CI 里跑的就是这三条）：

```bash
ruff check src tests scripts     # All checks passed!
mypy                             # Success: no issues found in 44 source files
pytest --cov=app                 # 85 passed, 80% coverage
```

> 有 `make` 的环境可以直接：`make install && make migrate && make dev && make test`

---

## 三、技术栈与版本

| 组件 | 版本 | 选型理由 |
| --- | --- | --- |
| Python | 3.11+（开发用 3.13） | 需要 `StrEnum`、`X \| Y` 类型语法、PEP 695 泛型 |
| FastAPI | 0.141 | 原生 async、Pydantic v2 校验、自动 OpenAPI |
| SQLAlchemy | 2.1（async） | `Mapped[]` 类型化 ORM、异步会话、`selectinload` 精准控查询 |
| Alembic | 1.20 | 迁移可回溯，生产不用 `create_all` |
| Pydantic / pydantic-settings | 2.13 / 2.15 | 请求校验与配置校验共用一套模型 |
| PyJWT + bcrypt | 2.15 / 5.0 | 轻量 JWT；bcrypt 自带 cost 与随机 salt |
| structlog | 26.1 | 本地彩色、生产 JSON，字段化日志便于采集 |
| SQLite / PostgreSQL | aiosqlite / asyncpg | 开发零依赖，生产换 URL 即可（`docker compose` 已带 PG17） |
| Redis（可选） | 7.x | 多实例共享限流配额；未配置时降级进程内令牌桶 |
| pytest + httpx | 9.1 / 0.28 | `ASGITransport` 直打 ASGI，不起端口、测试快 |

---

## 四、目录结构

```
goalfin-api/
├─ src/app/
│  ├─ main.py                  # 应用装配：中间件洋葱顺序、异常处理、探针、路由挂载
│  ├─ core/                    # 跨层基础设施
│  │  ├─ config.py             # pydantic-settings 强类型配置 + 生产环境弱密钥 fail-fast
│  │  ├─ security.py           # bcrypt 哈希 + JWT 双令牌签发/校验
│  │  ├─ exceptions.py         # 分层异常体系 + 全局统一错误响应
│  │  └─ logging.py            # structlog 结构化日志
│  ├─ db/
│  │  ├─ session.py            # 异步引擎、会话工厂、SQLite PRAGMA 调优、健康探测
│  │  └─ init_db.py            # 本地初始化脚本（--drop 可重置）
│  ├─ models/                  # ORM：User / Category / Transaction + 命名约定与时间戳 Mixin
│  ├─ schemas/                 # Pydantic：请求校验、响应序列化、Decimal 精度保证
│  ├─ repositories/            # 数据访问：查询构造、分页、聚合、批量写入
│  ├─ services/                # 业务规则：认证、分类、账单、统计、CSV
│  ├─ middleware/              # 请求上下文（request_id/耗时）、令牌桶 & Redis 限流
│  └─ api/
│     ├─ deps.py               # 依赖注入：会话、当前用户、分页、查询过滤器
│     └─ v1/                   # auth / categories / transactions / stats / csv_io
├─ tests/                      # 85 个用例：接口集成 + 单元 + 限流/CORS/E2E
├─ scripts/smoke_test.py       # 真实 HTTP 端到端冒烟（23 项断言）
├─ scripts/load_test.py        # 吞吐压测（含裸 socket 对照，见「Windows 事件循环选型」）
├─ scripts/ast_guard.py        # 用 AST 摘要证明「只改了注释、没动逻辑」
├─ alembic/                    # 迁移环境与版本脚本
├─ Dockerfile                  # 多阶段构建 + 非 root 用户 + HEALTHCHECK
├─ docker-compose.yml          # PostgreSQL 17 + Redis 7 + API 一键起
├─ Makefile                    # install / dev / test / lint / typecheck / migrate / docker
└─ .github/workflows/ci.yml    # 三阶段流水线：质量 → 迁移一致性 → 冒烟
```

**依赖方向**：`api → service → repository → models`，`core`/`db` 被各层使用但不反向依赖。
路由里没有 SQL，Service 里没有 `fastapi` 的 import，Repository 里没有业务判断。

---

## 五、数据模型

```
users ──1:N──> categories ──1:N──> transactions
  └────────────1:N──────────────────────┘
```

- `users(email UNIQUE, nickname, password_hash, is_active)`
- `categories(user_id, name, kind, color, icon, is_system)`，唯一约束 `(user_id, name, kind)`
- `transactions(user_id, category_id, amount, kind, occurred_on, note, source)`

关键设计说明：

1. **金额用 `Numeric(12,2)`**：浮点二进制无法精确表示 `0.1`，记账场景必须用 `Decimal` 避免累计误差。
2. **`occurred_on`（账期）与 `created_at`（写入时间）分离**：支持补录历史账单，统计口径只看账期。
3. **复合索引** `(user_id, occurred_on)`、`(user_id, kind, occurred_on)`：统计与列表走索引范围扫描。
4. **外键策略**：删用户级联删分类/账单；删分类时账单 `category_id` 置空（`SET NULL`），历史金额不丢。
5. **命名约定（naming_convention）**：让 Alembic 生成稳定可回滚的约束名，SQLite 上也能正确 downgrade。

---

## 六、API 一览（`/api/v1`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/auth/register` | 注册，自动预置 10 个系统分类（1 条批量 INSERT） |
| POST | `/auth/login` | 登录，返回 access + refresh 双令牌（限流更严格） |
| POST | `/auth/refresh` | 用 refresh 换新令牌（校验令牌类型，access 冒用被拒） |
| POST | `/auth/logout` | 登出（无状态 JWT，由客户端丢弃令牌） |
| GET/PATCH | `/auth/me` | 当前用户信息 / 更新昵称 |
| GET | `/categories` | 分类列表，支持 `kind` 过滤、`with_usage=true` 带账单数 |
| GET | `/categories/kinds` | 收支枚举（静态路由先注册，避免被 `/{id}` 抢占） |
| POST/PATCH/DELETE | `/categories[/{id}]` | 分类增改删（系统分类、被引用分类拒绝删除） |
| GET | `/transactions` | 多条件过滤 + 分页（日期区间/类型/分类/金额区间/备注关键词） |
| POST/GET/PATCH/DELETE | `/transactions[/{id}]` | 账单增删改查（校验分类归属与收支方向一致） |
| GET | `/stats/summary` | 月度概览：收入/支出/结余/储蓄率/日均支出 |
| GET | `/stats/categories` | 分类占比（环形图数据，按金额倒序） |
| GET | `/stats/trend` | 近 N 月趋势（自动补齐无数据月份） |
| GET | `/stats/calendar` | 当月每日收支（热力图） |
| GET | `/transactions/csv/template` | 下载导入模板 |
| GET | `/transactions/csv/export` | 流式导出（UTF-8 BOM，Excel 不乱码） |
| POST | `/transactions/csv/import` | 逐行校验、自动建分类、指纹去重、错误定位到行号 |
| GET | `/health`、`/health/live` | 就绪（含 DB 探测）/ 存活探针 |

**统一错误结构**（所有 4xx/5xx 都长这样，前端只需写一份处理逻辑）：

```json
{
  "code": "CATEGORY_IN_USE",
  "message": "该分类下还有 1 笔账单，请先迁移或删除后再试",
  "detail": { "retry_after": 12 },
  "request_id": "4324d1ce3841437d"
}
```

---

## 七、工程决策与取舍（面试就讲这些）

### 1. 为什么异步 + SQLAlchemy 2.0
记账接口绝大部分时间在等 I/O（数据库、磁盘）。异步模型下等待期间事件循环可以服务其他请求，
单进程可承载的并发连接数远高于同步模型。代价是不能再写隐式 lazy load——
所以我给所有关系配置了 `lazy="raise"`，一旦忘记预加载会立刻报错而不是悄悄发 N+1 查询。

### 2. 消除 N+1
账单列表需要返回分类名。逐条访问 `tx.category.name` 会变成 `1 + N` 次查询。
统一用 `selectinload(Transaction.category)`：额外 1 条 `WHERE category_id IN (...)`，
总查询数固定为 2；`lazy="raise"` 保证这个约定不会被后续开发悄悄破坏。

### 3. 聚合下沉到数据库
月度概览若在 Python 里 `for` 循环累加，耗时会随数据量线性增长。
改为 `SUM(...) + GROUP BY`，配合 `(user_id, occurred_on)` 索引，
耗时基本与数据量无关；同时把「10 个分类各查一次」压成 1 条 SQL。

### 4. 限流：两种实现同一接口
- **进程内令牌桶**：零依赖、允许突发，但扩容后每个实例各算一份配额；
- **Redis 固定窗口（INCR + EXPIRE）**：多实例共享配额，代价是一次网络往返。

`RateLimitMiddleware` 对两者统一 `await`，配置 `REDIS_URL` 即切换；
Redis 故障时**降级放行**并打告警日志——限流组件不应该成为全站单点。
（这个 bug 真实发生过：内存实现曾是同步方法，被我漏测的 `await` 直接打挂全部请求，
补了 `tests/test_rate_limit.py` 之后才算闭环。）

### 5. 事务边界放在 Service
Repository 只 `flush` 不 `commit`。注册要写「用户 + 10 条分类」，
放在同一事务里要么全成要么全败，不会出现有用户没分类的脏数据。

### 6. CSV 导入的三个细节
- **先解析校验、后批量插入**：错误行定位到文件行号返回，正确行一次 `add_all + flush`；
- **指纹去重**：`(账期, 金额, 备注)` 相同的行跳过，重复导入同一份账单不会翻倍；
- **编码兼容**：依次尝试 `utf-8-sig / utf-8 / gbk`，导出加 BOM 让 Excel 正确识别中文。

### 7. 配置与安全
- 配置集中到 `Settings`，生产环境（`APP_ENV=production`）遇到弱 `SECRET_KEY` 或 `DEBUG=true` **直接启动失败**；
- 登录失败统一返回 `INVALID_CREDENTIALS`，不区分「账号不存在/密码错误」，避免账号枚举；
- 所有资源查询都带 `user_id` 条件，越权返回 404 而不是 403，不泄露资源是否存在。

### 8. Windows 事件循环选型（实测 3.3 倍吞吐差）

Python 3.8+ 在 Windows 上默认用 `ProactorEventLoop`（为支持子进程），它在「大量小请求」的 ASGI 场景下
比 `SelectorEventLoop` 慢得多。同机、单 worker、keep-alive 长连接、裸 socket 客户端实测：

| 事件循环 | 并发 16 | 并发 64 |
| --- | --- | --- |
| ProactorEventLoop（默认） | ≈ 1 100 QPS | ≈ 1 120 QPS |
| **SelectorEventLoop** | **≈ 3 690 QPS** | **≈ 2 296 QPS** |

因此 `app/main.py` 顶部在 Windows 上显式切换策略（代价是不支持 `asyncio.create_subprocess_*`；
本项目不使用子进程，CPU 密集任务走 `asyncio.to_thread`）。Linux 部署无需此设置（默认即 epoll）。

复现方式：

```bash
# 终端 1：关掉限流启动
set RATE_LIMIT_ENABLED=false
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 终端 2：先写 500 条基准数据，再压测（--raw 会额外跑一组裸 socket 对照）
python scripts/load_test.py --base-url http://127.0.0.1:8000 --seconds 8 --concurrency 32 --seed 500 --raw
```

> 读结果的三个前提（别把数字讲错）：① 压测客户端与服务端同机抢 CPU，httpx 的读数偏保守，
> `--raw`（裸 socket）那一组才接近服务端上限；② SQLite 是单写者模型，写入吞吐不代表 PostgreSQL；
> ③ 报告里必须写明机器与 Python 版本，否则数字没有意义。

### 9. 中间件洋葱顺序：一个只有跨域前端才看得见的坑

Starlette 的 `add_middleware` 内部是 `insert(0, ...)`，**最后 add 的在最外层**。
本项目的实际顺序（由外到内）：

```
ServerErrorMiddleware → CORS → RequestContext → RateLimit → ExceptionMiddleware → 路由
```

其中两条顺序是**用真实故障换来的**，不能随手调换：

- **CORS 必须在限流之外**：限流中间件是「直接 return 一个自己构造的 JSONResponse」，
  不经过异常处理器、也不经过内层中间件。早期把 CORS 放在最内层时，
  被限流的 **429 响应里没有 `Access-Control-Allow-Origin`** —— 跨域前端在浏览器里
  只能看到 `CORS error`，既读不到 `RATE_LIMITED` 也读不到 `Retry-After`，限流提示等于白做。
  这个缺陷在**同源请求（curl、pytest 默认）下完全看不出来**，只有浏览器跨域才暴露，
  所以补了 `tests/test_cors_headers.py`：显式带 `Origin` 断言 429 也带跨域头。
- **RequestContext 也要靠外**：它写的 `X-Request-ID` / `X-Process-Time-Ms` 要覆盖所有响应。

通用判据：越靠外越早看到请求、越晚看到响应；决定两者相对位置的关键是
**短路的响应需不需要被外层修饰** —— 需要，就把修饰者放在短路者之外。

### 10. `.env.example` 必须「拷贝即可跑」：`NoDecode` 关掉 JSON 预解码

`CORS_ORIGINS` 是 `list[str]`，而 pydantic-settings 对复杂类型会**先按 JSON 解析环境变量**，
解析失败直接抛 `SettingsError`，**根本走不到 `field_validator`**。后果很隐蔽：

```bash
copy .env.example .env      # 按 README 第 2 步照做
uvicorn app.main:app        # 启动即崩
# SettingsError: error parsing value for field "cors_origins" from source "DotEnvSettingsSource"
```

报错信息里只有 `cors_origins`，很难联想到是 CORS 那一行写成了 `a,b` 而不是 JSON 数组。

修法：字段上标 `Annotated[list[str], NoDecode]` 关掉预解码，把原始字符串交给校验器，
于是**逗号分隔与 JSON 数组两种写法都支持**，写了半截 JSON 会给出明确原因。
这类「配置模板与代码不一致 → 新同学第一次启动就失败」的问题，
本质上是**契约没有测试守着**，所以它和上一条一样，都值得落一条测试/断言。

---

## 八、代码里的教学向注释（小白可学、面试可讲）

两个后端的源码都补写了面向「刚学完 Python、正在准备面试」的读者注释，规模如下：

| 项目 | 模块数 | src 总行 | 注释 + docstring 行 | 占比 |
| --- | --- | --- | --- | --- |
| `goalfin-api` | 43 | 6508 | 3591 | **55.2%** |
| `knowflow` | 32 | 8273 | 4324 | **52.3%** |

每个模块的 docstring 都包含三块内容：

1. **一句话定位**：这个文件是什么；
2. **在链路中的位置**：上游谁调用它、下游调用了谁、数据从哪来到哪去；
3. **设计取舍 / 面试可讲**：3~6 条带编号的「做法 + 理由 + 代价」。

### 怎么证明「只加了注释、没动代码」

这个工作区**不是 git 仓库**，用不了 `git diff`，所以写了
`scripts/ast_guard.py` 做 AST 层面的等价性证明：

```bash
.venv\Scripts\python.exe scripts\ast_guard.py selftest          # 先证明工具本身有效
.venv\Scripts\python.exe scripts\ast_guard.py snapshot . base.json
.venv\Scripts\python.exe scripts\ast_guard.py check . base.json # 只看注释就通过
```

原理是把语法树里所有字符串字面量（含 docstring）归一化后比较 `ast.dump()`：
注释不进语法树、docstring 被一起归一化，所以「只加注释」判定为等价；
而运算符、控制流、签名、装饰器任何一处改动都会被判为**结构变化**。
`selftest` 用三个用例（加注释 / 改运算符 / 改字符串）证明这套判定不会空转。

完整的注释标准与验证流程见 `../../docs/注释规范与验证方法.md`。

---

## 九、测试策略

| 层次 | 文件 | 覆盖内容 |
| --- | --- | --- |
| 接口集成 | `tests/test_auth.py` | 注册/登录/刷新/资料/令牌校验/弱密码/账号枚举 |
| 接口集成 | `tests/test_categories.py` | CRUD、唯一约束、越权、删除保护、路由优先级 |
| 接口集成 | `tests/test_transactions.py` | 金额精度、未来日期、方向一致性、过滤、分页、数据隔离 |
| 接口集成 | `tests/test_stats.py` | 聚合口径、除零保护、空月份补齐、整月日历 |
| 接口集成 | `tests/test_csv.py` | BOM、模板、逐行错误、去重、GBK、导出→导入回归 |
| 中间件 | `tests/test_rate_limit.py` | 429 与 `Retry-After`、探针豁免、request_id 透传 |
| 中间件 | `tests/test_cors_headers.py` | 带 `Origin` 断言 **429/401/200 都带跨域头**（防「被限流的响应前端读不到」回归） |
| 单元 | `tests/test_security.py` | bcrypt 加盐、篡改/过期/错签发者/类型混用、令牌桶补充 |

隔离手段：内存 SQLite + `StaticPool`（同一池共享同一内存库），
**引擎与建表都放在用例级 fixture 里**——异步连接绑定事件循环，
模块级引擎跨用例复用会出现「表已建却报 no such table」这类诡异错误（踩过，已写进注释）。

```bash
pytest -q                                   # 85 passed
pytest --cov=app --cov-report=html          # 打开 htmlcov/index.html
pytest tests/test_csv.py -k round_trip -v    # 只跑导出→导入回归
```

---

## 十、部署

```bash
# 方式一：本地文件库（最小依赖）
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4 --proxy-headers

# 方式二：Docker 单镜像
docker build -t goalfin-api .
docker run -p 8000:8000 -e SECRET_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(48))") goalfin-api

# 方式三：完整生产形态（PostgreSQL 17 + Redis 7 + API）
docker compose up --build -d
docker compose logs -f api
```

生产清单：

- [ ] `SECRET_KEY` 用随机值（`python -c "import secrets;print(secrets.token_urlsafe(48))"`）
- [ ] `APP_ENV=production`、`APP_DEBUG=false`（配错会启动失败，属于有意设计）
- [ ] `DATABASE_URL` 指向 PostgreSQL，`REDIS_URL` 指向 Redis
- [ ] `LOG_FORMAT=json` 接入日志采集；`/health`（就绪）与 `/health/live`（存活）分别配置探针
- [ ] 容器以非 root 用户运行（Dockerfile 已内置 `appuser`）

---

## 十一、还可以继续做什么（下一步 roadmap）

- [ ] refresh token 服务端黑名单（Redis 存 jti），支持强制下线与「单设备登录」
- [ ] 账单预算与超支提醒（定时任务 + 站内通知）
- [ ] 多币种与汇率换算
- [ ] 用 Locust 压测聚合接口，补一份 QPS/P99 数据写进简历
- [ ] 前端（Vue3 + ECharts）把统计接口可视化

---

## License

MIT
