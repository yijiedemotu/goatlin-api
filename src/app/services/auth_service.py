"""认证与用户业务。

事务边界（重点）：Repository 只 flush，Service 决定何时 commit，
因此「插用户 + 批量插默认分类」要么全成要么全回滚。"""

from __future__ import annotations

from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import EmailAlreadyExistsError, InvalidCredentialsError, UnauthorizedError
from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.category import Category
from app.models.user import User
from app.repositories.category_repo import CategoryRepository
from app.repositories.user_repo import UserRepository
from app.schemas.user import TokenPair, UserRegister, UserUpdate

logger = get_logger(__name__)

DEFAULT_CATEGORIES: tuple[tuple[str, str, str, str], ...] = (
    ("餐饮", "expense", "#F97316", "utensils"),
    ("交通", "expense", "#0EA5E9", "bus"),
    ("购物", "expense", "#EC4899", "shopping-bag"),
    ("居住", "expense", "#8B5CF6", "home"),
    ("娱乐", "expense", "#F59E0B", "gamepad"),
    ("医疗", "expense", "#EF4444", "heart-pulse"),
    ("学习", "expense", "#10B981", "book"),
    ("工资", "income", "#22C55E", "banknote"),
    ("奖金", "income", "#84CC16", "gift"),
    ("理财收益", "income", "#14B8A6", "trending-up"),
)


class AuthService:
    """认证用例编排：注册、登录、令牌签发与刷新、个人资料。

    生命周期：每个请求由路由新建一次（`AuthService(session)`），随请求结束被丢弃；
    实例上只有 session 与两个 Repository，没有跨请求共享的可变状态。

    为什么独立成类：认证是「跨表 + 跨组件」的用例（写 users、写 categories、
    调 core/security 做哈希与签名），把这些编排放在路由里会让鉴权逻辑与 HTTP 细节纠缠；
    独立成类后，注册的「同一事务」语义有唯一归属地，测试也可以直接构造会话调用它。
    """

    def __init__(self, session: AsyncSession) -> None:
        # 两个 Repository 共用同一个 session：注册要同时写 users 与 categories，
        # 只有同一 session 才能保证它们落在同一个事务里（要么全成、要么全败）。
        # 如果这里各自开 session，「用户已建、分类写入失败」就会留下脏数据。

        self.session = session
        self.users = UserRepository(session)
        self.categories = CategoryRepository(session)

    # ------------------------------------------------------------------
    async def register(self, payload: UserRegister) -> User:
        """注册新用户，并在同一事务内预置 10 条系统分类。

        参数：payload —— 已由 Pydantic 校验（邮箱已 trim + 转小写、密码 8-64 位且字母数字混合、
        nickname 最长 50）。

        返回：已持久化的 User（供 UserOut 序列化）。注意注册接口**不签发令牌**：
        前端 stores/auth.ts 注册成功后要再走一次 login 才能拿到双令牌。

        抛出：EmailAlreadyExistsError（HTTP 409 / code=EMAIL_ALREADY_EXISTS）。
        预检查命中与并发下被唯一索引拦下走的是同一个异常、同一个响应，
        调用方（前端）无法区分，从而不泄露「这个邮箱刚刚被别人抢先注册」的时序信息。

        关键实现选择见方法内逐条注释。
        """
        if await self.users.email_exists(payload.email):
            raise EmailAlreadyExistsError()

        user = User(
            email=payload.email,
            nickname=payload.nickname or payload.email.split("@")[0],
            password_hash=hash_password(payload.password),
        )
        self.session.add(user)
        # flush（而不是 commit）只做两件事：把 INSERT 发给数据库、把自增主键回填到 user.id。
        # 事务此时仍然是打开的 —— 这正是「用户 + 分类同一事务」的实现方式：
        # 后续的分类 INSERT 会挂在同一个事务上，直到下面那次唯一的 commit 才一起提交。
        try:
            await self.session.flush()  # 拿到自增 id
        except IntegrityError as exc:  # 并发注册同一邮箱：唯一索引兜底
            # 走到这里说明两个请求都通过了 email_exists 预检查，数据库唯一索引拦下了后到者。
            # rollback 是必须的：语句失败后事务进入不可继续状态，不回滚后续语句会继续报错。
            # 转成与预检查相同的异常，让并发注册与普通重复注册对用户完全不可区分。
            await self.session.rollback()
            raise EmailAlreadyExistsError() from exc

        await self.session.execute(
            insert(Category).values(
                [
                    {
                        "user_id": user.id,
                        "name": name,
                        "kind": kind,
                        "color": color,
                        "icon": icon,
                        "is_system": True,
                    }
                    for name, kind, color, icon in DEFAULT_CATEGORIES
                ]
            )
        )
        # 整个注册用例唯一的 commit：用户与 10 条分类在这里一起落库。
        # 若前面任何一步抛异常，控制权会回到 db/session.py 的 get_session，
        # 由它统一 rollback —— 所以本方法内部不需要 try/except 包住全部逻辑。
        await self.session.commit()
        # refresh 重新从数据库读一遍：created_at / updated_at 由 TimestampMixin 的
        # server_default 生成，不 refresh 的话响应里的时间戳可能是 None 或旧值。
        # 注意会话是 expire_on_commit=False，属性能直接读而不会触发异步下的隐式 lazy load。
        await self.session.refresh(user)
        logger.info("user_registered", user_id=user.id, email=user.email)
        return user

    # ------------------------------------------------------------------
    async def authenticate(self, email: str, password: str) -> User:
        """校验邮箱 + 密码，成功返回可用于签发令牌的用户实体。

        参数：email —— 路由层已 trim + 转小写（UserLogin 的 field_validator）；
        password —— 明文密码，只在本方法内与哈希比对，绝不落库、绝不打日志。

        返回：通过认证的 User。

        抛出：
            InvalidCredentialsError（HTTP 401 / code=INVALID_CREDENTIALS）：
                邮箱不存在 **或** 密码错误。两者刻意合并成同一个错误 ——
                否则攻击者拿一份邮箱字典批量请求，就能靠「邮箱不存在」与「密码错误」
                的响应差异筛出哪些邮箱在本站注册过（账号枚举）。
            UnauthorizedError（401 / code=UNAUTHORIZED）：账号被禁用。

        关键实现选择：
        1. **先合并失败分支，再判禁用状态**：`user is None or not verify_password(...)`
           短路成同一个 401；禁用判断放在密码校验之后，这样「不知道密码的人」
           无法通过错误码差异确认某个邮箱存在。
        2. **verify_password 内部把异常都当失败**（见 core/security.py），
           所以这里不需要 try/except；迁移时留下的非 bcrypt 脏哈希也只会表现为登录失败。
        3. 日志只记 email 不记密码，用于配合限流中间件排查撞库；
           注意这条日志本身也包含「谁在被爆破」的信息，是运维侧的线索。
        """
        user = await self.users.get_by_email(email)
        # 无论用户不存在还是密码错误，都返回同一个错误，避免账号枚举
        if user is None or not verify_password(password, user.password_hash):
            logger.info("login_failed", email=email)
            raise InvalidCredentialsError()
        if not user.is_active:
            raise UnauthorizedError("账号已被禁用，请联系管理员")
        return user

    async def issue_tokens(self, user: User) -> TokenPair:
        """签发一对 access + refresh 令牌（登录与刷新共用这一条出口）。

        参数：user —— 必须是已通过 authenticate 或 refresh 校验、且 is_active 的实体。

        返回：TokenPair。其中 expires_in 是 access_token 的有效期秒数
        （配置的分钟数 × 60）；前端 stores/auth.ts 用它算出 expiresAt 并提前 30 秒续期，
        把「请求正好卡在过期瞬间」的 401 挡在用户无感的地方。

        关键实现选择：
        1. 签发不查库也不写库，纯 HMAC 签名计算（微秒级），所以 refresh 接口很便宜，
           可以放心让客户端「过期即刷」。
        2. **refresh 的 jti 在这里被丢弃**（`_jti` 用下划线前缀）：
           等于放弃服务端吊销能力，换取完全无状态的实现。要支持「踢下线 / 单设备登出」
           就得把 jti 落库并在 refresh 时校验是否被吊销，代价是一次存储查询
           加上一张需要清理过期的表。
        3. access 的绝对过期时间 `_expire` 同样被丢弃：客户端只需要相对秒数
           expires_in，用绝对时间反而要处理服务端与客户端的时钟偏差。
        """
        access_token, _expire = create_access_token(user.id)
        refresh_token, _jti, _rexp = create_refresh_token(user.id)
        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.access_token_expire_minutes * 60,
        )

    async def refresh(self, refresh_token: str) -> TokenPair:
        """用 refresh_token 换一对新令牌（静默续期，前端拿到 401 后自动调用）。

        参数：refresh_token —— 前端本地持久化的长效令牌。

        抛出：
            InvalidTokenError（401 / code=INVALID_TOKEN）：签名错误、已过期、issuer 不符，
                或**令牌类型不是 refresh** —— 这是防「access 冒充 refresh」的关键收口。
            UnauthorizedError（401 / code=UNAUTHORIZED）：令牌本身合法，
                但用户已被删除或被禁用。

        关键实现选择：
        1. **expected_type="refresh" 强制校验 payload["type"]**：
           access 与 refresh 用同一个 secret、同一个算法签名，如果这里不校验类型，
           一个 30 分钟的 access_token 就能当 7 天的 refresh_token 用，
           「短 access 缩小泄露窗口」的设计会直接失效。
           反方向（refresh 冒充 access 调业务接口）由 api/deps.py 的
           expected_type="access" 拦住 —— 两个方向的类型校验缺一不可。
        2. **必须重新查库**：JWT 是无状态的，用户被封禁后旧 refresh_token 在 7 天内
           依然能通过签名与有效期校验；get_active 额外过滤 is_active，
           这是「禁用账号立即生效」的唯一保证。
        3. **轮换但不吊销**：每次刷新都签发新的一对，但旧 refresh 在过期前仍可重放。
           严格的一次性 refresh 需要在库里记录已用过的 jti，
           与第 2 点同属「无状态 vs 可吊销」的取舍，本项目选了简单的一侧。
        """
        payload = decode_token(refresh_token, expected_type="refresh")
        user = await self.users.get_active(int(payload["sub"]))
        if user is None:
            raise UnauthorizedError("用户不存在或已被禁用")
        return await self.issue_tokens(user)

    # ------------------------------------------------------------------
    async def update_profile(self, user: User, payload: UserUpdate) -> User:
        """更新个人资料（目前只有 nickname）。

        参数：user —— 由 api/deps.py 的 CurrentUser 注入的**已鉴权实体**。
        直接改这个对象的属性即可，不需要再按 id 查一次库：
        它与本 Service 的 session 是同一个身份映射里的对象，改动会被 flush/commit 捕获。
        这正是「鉴权与取数在同一次查询里完成」带来的好处，也避免了「先查再改」的竞态。

        关键实现选择：`if payload.nickname is not None` 而不是 `if payload.nickname`——
        PATCH 语义下 None 表示「不改」，而空字符串是合法的「清空昵称」意图，
        用真值判断会把清空操作静默忽略掉。

        返回：更新后的 User（供 UserOut 序列化）。
        抛出：无业务异常 —— 非法昵称（超长）已在 schema 层被 422 拦下。
        """
        if payload.nickname is not None:
            user.nickname = payload.nickname
        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def get_profile(self, user_id: int) -> User:
        """按 id 取「可用」用户，用于 /auth/me 之类的读接口。

        抛出：UnauthorizedError（401 / code=UNAUTHORIZED）——
        用户不存在 **或** is_active 为假。两者合并成 401（而不是 404）：
        从「当前登录者」的视角看，这两种情况都等价于「你的登录态无效了，请重新登录」，
        前端 stores/auth.ts 收到 401 就会清会话并跳登录页，语义正好对上。

        关键实现选择：用 get_active 而不是 get —— 封禁用户在这里就被挡住，
        不需要每个调用方各自判断 is_active（漏一处就是一个越权口子）。
        """
        user = await self.users.get_active(user_id)
        if user is None:
            raise UnauthorizedError("用户不存在或已被禁用")
        return user
