"""Schema 公共组件：枚举、分页、通用响应。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class TransactionKind(StrEnum):
    """账单方向：收入 / 支出。用枚举而非裸字符串，编译期即可发现拼写错误。

    生命周期：请求体里的 kind 会被 Pydantic 解析成本枚举成员；
    仓储层比较时取 `.value`（数据库列是 VARCHAR(8)，存的就是这两个小写字面量）。
    之所以不做成数据库原生 ENUM：新增取值时 MySQL 要 ALTER 列定义，
    而 VARCHAR + 入口枚举收口已经能拦住绝大多数错误。
    """

    INCOME = "income"
    EXPENSE = "expense"


class ORMModel(BaseModel):
    """可从 ORM 对象直接构造的响应基类。

    关键就是 `from_attributes=True`：允许 `TransactionOut.model_validate(tx)` 这种
    「按属性名取值」的构造方式，从而免去手写 `TransactionOut(id=tx.id, ...)` 的搬运代码。
    为什么单独抽一个基类：一旦某天要统一改序列化行为（别名、自定义 encoder），
    只需要动这里一处，所有 *Out 模型一起生效。
    """

    model_config = ConfigDict(from_attributes=True)


class PageParams(BaseModel):
    """统一分页参数（offset 分页，适合后台与中等数据量）。

    由 api/deps.py 的 `pagination` 依赖从 Query 参数构造，路由拿到的是强类型对象，
    不需要自己写 (page - 1) * size 这种容易算错的偏移计算。
    """

    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    size: int = Field(default=20, ge=1, le=100, description="每页条数，上限 100 防大结果集拖垮内存")

    @property
    def offset(self) -> int:
        """SQL OFFSET 值：`(page - 1) * size`。"""
        # 数据库 OFFSET 从 0 开始，页码从 1 开始，这里集中做一次换算，避免各处重复写错
        return (self.page - 1) * self.size


class Page[T](BaseModel):
    """统一分页响应。

    前端只需要 items / total / page / size 四个字段就能渲染列表与分页器
    （见 goalfin-web 的 Page<T> 类型与列表页组件）；
    pages 是给 Python 调用方用的派生属性，不出现在 JSON 里。
    """

    items: list[T]
    total: int = Field(description="符合条件的总条数")
    page: int
    size: int

    @property
    def pages(self) -> int:
        """总页数（向上取整）；只在 Python 侧使用，不进入 JSON 响应。"""
        # 向上取整算总页数；size 为 0 时直接返回 0，避免除零
        return (self.total + self.size - 1) // self.size if self.size else 0


class MessageResponse(BaseModel):
    """简单操作结果：默认 message="ok"，用于不返回实体的操作类接口。"""

    message: str = "ok"


class HealthResponse(BaseModel):
    """健康检查响应：同时报告应用版本、运行环境与数据库连通状态。

    为什么把 database 也放进来：容器探针只看 "status: ok" 会漏掉「进程活着但连不上库」
    这一最常见的故障形态，把依赖状态暴露出来才能被监控直接利用。
    """

    status: str
    version: str
    environment: str
    database: str
