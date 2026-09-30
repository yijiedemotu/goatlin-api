"""CSV 导入导出业务。

三个工程细节：导出加 UTF-8 BOM（否则 Excel 中文乱码）、用生成器流式产出
（内存与数据量无关）、导入先全量校验再批量落库（错误定位到行号 + 指纹去重）。"""

from __future__ import annotations

import csv
import io
from collections.abc import AsyncIterator, Iterator
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BadRequestError, CsvFormatError
from app.core.logging import get_logger
from app.models.category import Category
from app.models.transaction import Transaction
from app.repositories.category_repo import CategoryRepository
from app.repositories.transaction_repo import TransactionRepository
from app.schemas.csv_io import ImportResult, RowError

logger = get_logger(__name__)

HEADER = ["账期", "金额", "类型", "分类", "备注"]
CSV_BOM = "\ufeff"
MAX_IMPORT_ROWS = 10_000
KIND_ALIASES = {
    "收入": "income",
    "支出": "expense",
    "income": "income",
    "expense": "expense",
    "in": "income",
    "out": "expense",
}
DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d")


def _parse_date(raw: str) -> date:
    """按 DATE_FORMATS 里的格式逐个尝试解析日期，全部失败才报错。

    参数：raw —— 用户 Excel / 其他记账 App 导出的日期单元格文本。

    返回：date 对象。

    抛出：ValueError（由 _parse_row 捕获并转成带行号的 RowError，不是 HTTP 异常）。

    关键实现选择：**接受 4 种格式而不是只认 ISO**。用户手上的文件来自 Excel 中文版、
    微信/支付宝账单、其他记账 App，日期写法不统一（2025/01/05、2025.01.05、20250105）；
    只认一种格式会让导入失败率极高，而逐个尝试的成本只是几次字符串比较。
    注意 strptime 对 "%Y%m%d" 也会容忍 "2025015" 这类短串（按位数匹配），
    但后续业务校验（如账期不晚于今天）会兜住明显的错误值。
    """
    text = raw.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"无法识别的日期: {raw}")


class CsvService:
    """CSV 用例编排：模板、流式导出、批量导入。

    生命周期：每请求一个实例，共享请求的 AsyncSession —— 导入是唯一会写库的入口，
    因此 commit 也集中在这里（分类自动创建与账单批量插入共用一次 commit）。

    为什么独立成类：CSV 的业务全在「解析与校验」上（编码、日期格式、金额清洗、去重），
    这些逻辑既不属于 HTTP 层也不属于 SQL 层；独立成类后可以用纯字节输入做单测，
    不必起数据库（本类里 build_template / _decode / _parse_row 都是无状态静态方法）。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.transactions = TransactionRepository(session)
        self.categories = CategoryRepository(session)

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    async def export_stream(
        self, user_id: int, start: date | None = None, end: date | None = None
    ) -> AsyncIterator[str]:
        """逐行产出 CSV 文本。先 yield 表头，再逐条 yield 数据行。

        参数：
            user_id: 当前登录用户（导出始终限定在自己数据范围内，不接受全量导出）。
            start / end: 可选的账期区间（闭区间），None 表示不限 ——
                前端 TransactionsView 可以把当前筛选条件带进来。

        返回：AsyncIterator[str]，由路由包进 StreamingResponse 边生成边发送。

        关键实现选择：
        1. **BOM 与表头合成第一个 chunk**：BOM 必须是文件的第一个字节，
           否则 Excel 仍会按本地编码解析中文（乱码）。
        2. **复用一个 StringIO buffer**：每行 writerow 后立即 getvalue() 再 seek(0)+truncate(0)，
           避免为每一行新建缓冲区（GC 压力）或让 buffer 无限增长。
        3. **lineterminator 显式指定 "\\r\\n"**：csv 模块默认写 "\r\n"，
           但显式声明能防止有人改默认值后 Windows Excel 里行粘连。
        4. **诚实说明内存边界**：流式省掉的是「拼接整份文件」的峰值内存，
           查询侧 list_for_export 仍是一次性把结果集取到内存（带 selectinload 的分类）。
           真要支撑百万行导出，需要把查询改成 keyset 分页（按 id > last_id LIMIT N）
           在生成器里逐批 yield，代价是多几次查询与「导出期间数据可能变动」的口径问题。
        """
        yield CSV_BOM + ",".join(HEADER) + "\r\n"
        rows = await self.transactions.list_for_export(user_id, start, end)
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        for tx in rows:
            writer.writerow(
                [
                    # 金额用 ".2f" 固定两位：CSV 是给 Excel/人看的，不是给程序做精度的
                    tx.occurred_on.isoformat(),
                    f"{tx.amount:.2f}",
                    # 导出中文类型名，与导入时的 KIND_ALIASES 双向对应，导出文件可原样回传
                    "收入" if tx.kind == "income" else "支出",
                    tx.category.name if tx.category else "未归类",
                    tx.note,
                ]
            )
            # 取走一行后立刻清空缓冲区，保证 buffer 里始终只有一行内容
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    # ------------------------------------------------------------------
    # 导入
    # ------------------------------------------------------------------
    async def import_csv(self, user_id: int, content: bytes) -> ImportResult:
        """导入 CSV：逐行校验 → 收集错误行号 → 批量插入 → 返回导入报告。

        参数：
            user_id: 当前登录用户（新账单与新分类都挂到它名下）。
            content: 原始文件字节（路由已限制 5MB）。传 bytes 而不是 str，
                是为了把「编码嗅探」这一层的责任留在本模块内。

        返回：ImportResult(total=解析到的数据行数, imported=成功入库数,
        failed=失败/跳过的行数, errors=最多前 50 条 RowError)。

        抛出：
            CsvFormatError（HTTP 400 / code=CSV_FORMAT_ERROR）：文件为空、
                表头列数不足 5、或编码完全无法识别。
            BadRequestError（400 / BAD_REQUEST）：数据行数超过 MAX_IMPORT_ROWS 上限。

        关键实现选择：
        1. **先全量校验、后批量落库**：循环里只做解析与校验，把待插入对象攒进 pending，
           循环结束后 bulk_create 一次性 flush，最后 commit 一次。
           边解析边插是 N 次往返 + 可能 N 次事务，5000 行会从秒级退化到分钟级。
        2. **部分成功语义**：坏行记进 errors 并跳过，好行照常入库并 commit。
           不整批回滚，是因为「一行格式错误导致 999 行白导」对用户更糟；
           代价是调用方必须检查 failed 字段，不能只看 HTTP 200。
        3. **两套去重集合**：existing 是库里已有的指纹，seen_in_file 是本文件内已见过的指纹。
           只查库不去重文件内重复是不够的（同一份账单在文件里出现两次，第一次还没 commit）；
           只去重文件内不看库则重复导入会翻倍。
        4. **顺序依赖**：本方法内部有 flush（自动建分类）与 commit，
           异常时由 db/session.py 的 get_session 统一 rollback，本层不再包 try/except。
           注意「分类自动创建」的 flush 发生在 commit 之前的同一个事务里，
           所以「分类建了但账单插入失败」不会留下孤立分类。
        """
        text = self._decode(content)
        reader = csv.reader(io.StringIO(text))
        try:
            header = next(reader)
        except StopIteration as exc:
            raise CsvFormatError("文件为空") from exc

        # 表头少于 5 列直接拒绝：与其让每一行都在解析时报错（用户会看到 1 万条错误），
        # 不如在开头给出一个「列数不够」的明确结论。
        # 注意这里只判「少于」：多出来的列会被 _parse_row 切片丢弃，兼容用户自己加了备注列的情况。
        if len(header) < 5:
            raise CsvFormatError(
                "表头至少需要 5 列：账期, 金额, 类型, 分类, 备注",
                detail={"header": header},
            )

        existing = await self.transactions.existing_fingerprints(user_id)
        # 分类缓存：一次把该用户的分类拉进 dict，避免每行都查一次数据库（N+1）。
        # key 用 (name, kind) 而不是 name：同名分类可以同时存在收入版与支出版
        # （唯一约束就是 (user_id, name, kind)），只用 name 会把两个分类混成一个。
        category_cache = {(c.name, c.kind): c for c in await self.categories.list_by_user(user_id)}

        pending: list[Transaction] = []
        errors: list[RowError] = []
        seen_in_file: set[tuple[date, str, str]] = set()
        total = 0

        for line_no, row in enumerate(reader, start=2):  # 从 2 开始，对应文件真实行号
            if not row or all(not cell.strip() for cell in row):
                continue
            total += 1
            # 行数上限挡在解析之前：既是防「误传了几十万行的全量账单导致请求超时」，
            # 也是给前端一个明确的「请拆分文件」提示，而不是让请求卡死到网关超时。
            # detail 回传 processed 便于排查到底传了多少行（total - 1 是不含当前这行）。
            if total > MAX_IMPORT_ROWS:
                raise BadRequestError(
                    f"单次导入上限 {MAX_IMPORT_ROWS} 行，请拆分文件",
                    detail={"processed": total - 1},
                )

            try:
                payload = self._parse_row(row)
            except ValueError as exc:
                errors.append(RowError(row=line_no, reason=str(exc), raw=",".join(row)))
                continue

            occurred_on, amount, kind, category_name, note = payload
            fingerprint = (occurred_on, f"{amount:.2f}", note)
            if fingerprint in existing or fingerprint in seen_in_file:
                # 重复行也记进 errors 并计入 failed：让用户知道「文件里有 3 行没导进去」
                # 以及具体是哪几行，而不是默默少了几笔账
                errors.append(RowError(row=line_no, reason="重复账单，已跳过", raw=",".join(row)))
                continue
            seen_in_file.add(fingerprint)

            category_id: int | None = None
            if category_name:
                key = (category_name, kind)
                category = category_cache.get(key)
                if category is None:
                    category = Category(user_id=user_id, name=category_name, kind=kind, is_system=False)
                    self.session.add(category)
                    await self.session.flush()
                    category_cache[key] = category
                category_id = category.id

            pending.append(
                Transaction(
                    user_id=user_id,
                    category_id=category_id,
                    amount=amount,
                    kind=kind,
                    occurred_on=occurred_on,
                    note=note,
                    source="csv",
                )
            )

        imported = 0
        if pending:
            # 所有校验通过的行在这里一次性 add_all + flush（一条或极少数几条 INSERT），
            # 再 commit 一次。整个导入只开一个事务：要么这批好行全部入库，
            # 要么整体回滚（异常由 get_session 兜底），不会出现「导了一半」的状态。
            imported = await self.transactions.bulk_create(pending)
            await self.session.commit()

        # 结构化日志：total/imported/failed 三个数就能判断一次导入是否异常，
        # 排查用户报障时按 user_id 过滤即可定位到具体这一次请求。
        logger.info("csv_imported", user_id=user_id, total=total, imported=imported, failed=len(errors))
        return ImportResult(
            total=total,
            imported=imported,
            failed=len(errors),
            errors=errors[:50],  # 只回传前 50 条，避免超大响应体
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _decode(content: bytes) -> str:
        """按固定顺序嗅探编码并解码上传的文件内容。

        返回：解码后的整份文本。

        抛出：CsvFormatError（HTTP 400 / code=CSV_FORMAT_ERROR），三种编码都失败时。

        为什么顺序不能换（面试常问）：
        1. **utf-8-sig 必须排第一**：Excel 的「CSV UTF-8」导出会在文件头写 BOM（EF BB BF）。
           用普通 utf-8 解码时 BOM 会变成一个真实字符 "\\ufeff" 混进第一个字段，
           表头第一列从 "账期" 变成 "\\ufeff账期"，后续按列序/列名做的判断全部错位，
           而且现象是「表头莫名其妙不认识」，排查起来非常费时间。
        2. **utf-8 排在 gbk 之前**：两种编码的字节范围有重叠，GBK 对不少 UTF-8 字节序列
           也能「解码成功」但得到一堆乱码 —— 乱码不会抛异常，只会静默变成脏数据。
           先试约束更严的 UTF-8（多字节序列必须合法），失败才退到 GBK。
        3. **gbk 兜底**：Excel 中文版另存为「CSV（逗号分隔）」默认就是 GBK 系编码，
           不支持它会让一大批真实用户的文件直接导入失败。
        4. 全部失败时抛 400 并给出可操作建议（另存为 UTF-8 或 GBK），
           而不是让 UnicodeDecodeError 冒到全局处理器变成 500。
        """
        for encoding in ("utf-8-sig", "utf-8", "gbk"):
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise CsvFormatError("无法识别文件编码，请另存为 UTF-8 或 GBK")

    @staticmethod
    def _parse_row(row: list[str]) -> tuple[date, Decimal, str, str, str]:
        """解析并校验一行数据，返回可直接落库的字段元组。

        参数：row —— csv.reader 给出的一行字符串列表（列数可能不足 5，也可能更多）。

        返回：(occurred_on: date, amount: Decimal, kind: str, category_name: str, note: str)。

        抛出：ValueError（消息是中文原因），由 import_csv 捕获并转成 RowError(row=行号)。
        这里**刻意不抛 HTTP 异常**：单行失败不应该中断整份文件的导入。

        关键实现选择：
        1. **列数不足时先补齐再取前 5 列**：`[cell.strip() ...] + [""] * (5 - len(row))`，
           于是「只有 3 列」也能解析（备注为空），不用写一堆 if；
           少于 3 列直接拒绝，因为账期/金额/类型是必需信息。
           列数多于 5 时切片丢弃多余列，避免多出来的空列干扰。
        2. **金额清洗**：去掉千分位逗号与货币符号 "¥"，兼容从 Excel、银行 App
           复制出来的 "1,234.50" 文本；用 Decimal 而不是 float，
           与模型 Numeric(12,2) 的精度语义一致。
        3. **金额必须 > 0，方向由「类型」列决定**：库里永远不会出现负数金额，
           统计时就不必再判断符号（负金额 + income 的组合是最难排查的一类脏数据）。
        4. **类型别名归一化**：KIND_ALIASES 把 收入/支出/income/expense/in/out 收成两个内部值；
           `kind_text.lower() if kind_text.isascii() else kind_text` 让 ASCII 输入大小写不敏感，
           同时不动中文（中文没有大小写）。
        5. **拒绝未来账期**：与 schema 层 TransactionCreate 的校验保持一致，
           否则 CSV 就成了绕过该校验的后门。
        6. **note 截断到 500**：与 schema 的 max_length=500 对齐，
           避免「导出再导入」往返时超长备注一处被拒一处被截。
        """
        if len(row) < 3:
            raise ValueError("列数不足，至少需要 账期/金额/类型")

        cells = [cell.strip() for cell in row] + [""] * (5 - len(row))
        date_text, amount_text, kind_text, category_name, note = cells[:5]

        occurred_on = _parse_date(date_text)

        try:
            amount = Decimal(amount_text.replace(",", "").replace("¥", "")).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"金额非法: {amount_text}") from exc
        if amount <= 0:
            raise ValueError("金额必须大于 0，方向由「类型」列决定")

        kind = KIND_ALIASES.get(kind_text.lower() if kind_text.isascii() else kind_text)
        if kind is None:
            raise ValueError(f"类型应为 收入/支出，收到: {kind_text}")

        if occurred_on > date.today():
            raise ValueError("账期不能晚于今天")

        return occurred_on, amount, kind, category_name, note[:500]

    @staticmethod
    def build_template() -> Iterator[str]:
        """生成导入模板（表头 + 两行示例数据），同样是逐行 yield 的流式产出。

        返回：Iterator[str]，由 api/v1/csv_io.py 包进 StreamingResponse 供下载。

        关键实现选择：
        1. **带示例数据而不是空表头**：用户照着示例填能一次成功；
           只给空表头时，最常见的结果是「类型列填成『支出（餐饮）』」导致整列解析失败。
        2. **示例行的分类用系统预置分类名（餐饮 / 工资）**：导入时能直接命中已存在的分类，
           顺便演示「同名分类自动匹配、没有的分类自动创建」这条行为。
        3. **与 export_stream 相同的 BOM + \\r\\n 处理**：模板下载下来要能双击用 Excel 打开
           且中文不乱码 —— 模板乱码会直接导致用户把错误格式的文件传回来。
        4. 无状态静态方法：不依赖 session、不查库，所以不需要 async（同步生成器即可）。
        """
        yield CSV_BOM + ",".join(HEADER) + "\r\n"
        samples = [
            ["2025-01-05", "38.50", "支出", "餐饮", "午餐"],
            ["2025-01-10", "12000.00", "收入", "工资", "1月工资"],
        ]
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        for sample in samples:
            writer.writerow(sample)
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
