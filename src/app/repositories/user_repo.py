"""用户仓储。"""

from __future__ import annotations

from sqlalchemy import func, select

from app.models.user import User
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    """users 表的查询入口。

    生命周期：无状态，"一次请求一个实例"，由 AuthService 或 api/deps.py 现场
    构造（`UserRepository(session)`），不复用、不缓存。

    为什么独立成类：用户表是唯一带**登录语义**的表，它的查询条件
    （邮箱大小写折叠、is_active 过滤）和通用 CRUD 差别很大；
    单独放一处，"哪些查询会检查封禁状态"才能一眼看全。
    """

    model = User  # 声明操作 users 表；基类的 get/add/delete/count 依赖它

    async def get_by_email(self, email: str) -> User | None:
        """邮箱统一小写后查询，配合唯一索引避免大小写绕过去重。

        参数:
            email: 用户输入的原始邮箱，大小写任意、允许首尾空格
                （`strip()` 让前端输入框的尾随空格不会导致"密码正确却登不上"）。
        返回:
            命中返回 User；未命中返回 None。注意 Service 会把"用户不存在"和
            "密码错误"合并成同一个错误，避免攻击者用它做账号枚举
            （见 services/auth_service.py::authenticate）。
        索引命中:
            `func.lower(email)` 是函数包装，**用不上 email 上的普通唯一索引**
            （B-tree 只对列本身有序，对 lower(列) 无序），数据量大时退化为全表扫描。
            彻底解决要建表达式索引，PG 与 SQLite 3.9+ 都支持：
            `CREATE UNIQUE INDEX ... ON users (lower(email))`。
            本项目注册量级小、且写入侧已保证小写，所以接受这个取舍——
            面试被追问"为什么 VARCHAR 上的唯一索引不生效"时正好答这个点。
        """
        stmt = select(User).where(func.lower(User.email) == email.strip().lower())
        return await self.session.scalar(stmt)

    async def email_exists(self, email: str) -> bool:
        """注册前的快速去重：只查主键 + LIMIT 1，不构造完整 ORM 对象。

        参数:
            email: 原始邮箱，同样先 strip + lower 再比对，与 `get_by_email`
                用完全相同的规范化规则（两处规则不一致会出现
                "提示已注册但查不到该用户"的诡异现象）。
        返回:
            已存在返回 True。
        注意这是 check-then-act：两个并发注册请求可能都拿到 False，
            真正的唯一性由 `users.email` 唯一索引保证——AuthService 捕获
            IntegrityError 后同样返回 409，所以用户看到的提示是一致的。
        实现选择: `select(User.id)` 而非 `select(User)`，避免把 bcrypt 哈希等
            无关字段从数据库搬到 Python；`.limit(1)` 让数据库找到一行就停，
            不必数出总数。
        """
        stmt = select(User.id).where(func.lower(User.email) == email.strip().lower()).limit(1)
        return await self.session.scalar(stmt) is not None

    async def get_active(self, user_id: int) -> User | None:
        """取"可用"用户：`is_active=True` 才返回，鉴权链路每个请求都会调用。

        参数:
            user_id: JWT `sub` 声明解析出的用户主键（见 api/deps.py），
                来源是服务端解签，可信。
        返回:
            命中返回 User；用户不存在**或已被封禁**都返回 None，
            调用方统一转成 401，不区分两种情况（同样是为了不泄漏账号状态）。
        为什么不用 `BaseRepository.get`: `get()` 只按主键取、不看 is_active，
            用它会让封禁形同虚设——用户拿着未过期的 token 照样访问。
            代价是每个请求多一次 SELECT（无法只靠 JWT 自校验），
            换来的是"封禁立刻生效"。
        `is_(True)` 是显式的布尔比较写法，生成 `is_active IS 1`（PG 上为
            `IS true`），比 `== True` 语义明确且能通过 ruff 的 E712。
        """
        stmt = select(User).where(User.id == user_id, User.is_active.is_(True))
        return await self.session.scalar(stmt)
