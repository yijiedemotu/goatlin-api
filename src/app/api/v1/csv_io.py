"""CSV 导入导出路由。"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, File, Query, UploadFile
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUser, SessionDep
from app.core.exceptions import BadRequestError
from app.schemas.csv_io import ImportResult
from app.services.csv_service import CsvService

# 路由前缀含 /transactions：必须早于 transactions 路由注册（见 api/v1/__init__.py 的顺序说明），
# 否则 /transactions/{transaction_id} 会把 "csv" 当成 int 解析并返回 422
router = APIRouter(prefix="/transactions/csv", tags=["CSV 导入导出"])

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5MB，防止超大文件打满内存


def _parse_optional_date(value: str | None, field: str) -> date | None:
    """把可选的 `YYYY-MM-DD` 字符串解析成 `date`，非法则 400。

    与 `api/v1/transactions.py` 里的 `transaction_filters._parse` 是同类逻辑，
    但两者作用域不同（这里是导出范围、那里是列表筛选），各自保留一份避免跨模块耦合。
    用 `from None` 掐断异常链：对外只想让用户看到「格式应为 YYYY-MM-DD」，
    不需要露出 `ValueError` 的原始堆栈语义。
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise BadRequestError(f"{field} 需为 YYYY-MM-DD 格式", detail={field: value}) from None


@router.get("/template", summary="下载导入模板（含示例行）")
async def download_template(current_user: CurrentUser) -> StreamingResponse:
    """下载导入模板 CSV（需要登录，仅为统一鉴权，模板内容与用户无关）。

    模板 = 表头 + 两行示例，由 `CsvService.build_template()`（同步生成器）产出，
    不查库、不落盘。带 UTF-8 BOM，Excel 双击打开中文列名不乱码。
    200 返回 `text/csv` + `Content-Disposition: attachment; filename="goalfin_import_template.csv"`；
    令牌无效 401。前端 `TransactionsView.vue` 的「下载模板」链接。
    """
    return StreamingResponse(
        # 生成器作为 body：Starlette 逐块迭代并通过 ASGI 发送，不拼接完整字符串
        CsvService.build_template(),
        media_type="text/csv; charset=utf-8",
        # Content-Disposition 决定浏览器是「下载」还是「内联展示」；文件名必须显式给出
        headers={"Content-Disposition": 'attachment; filename="goalfin_import_template.csv"'},
    )


@router.get("/export", summary="导出账单为 CSV（流式，带 UTF-8 BOM 防 Excel 乱码）")
async def export_csv(
    current_user: CurrentUser,
    session: SessionDep,
    start_date: Annotated[str | None, Query(description="起始账期 YYYY-MM-DD")] = None,
    end_date: Annotated[str | None, Query(description="结束账期 YYYY-MM-DD")] = None,
) -> StreamingResponse:
    """导出账单为 CSV（需要登录，只导出当前用户的数据，按账期升序）。

    账期区间可选（不传即全部）；日期格式非法 → 400 `BAD_REQUEST`；令牌无效 → 401。
    响应是流式 `text/csv`，文件名带导出日期（`goalfin_YYYY-MM-DD.csv`），
    首行含 UTF-8 BOM，因此 Excel 打开中文列名/备注不会乱码——这是真实用户投诉最多的小坑。
    导出内容与导入模板列一致（账期, 金额, 类型, 分类, 备注），可以「导出 → 改 → 导回」闭环。

    为什么用 `StreamingResponse` 而不是 `Response(content=...)`：
    `Response` 需要先把全部内容拼成一个 `str/bytes` 常驻内存（几十万行会吃几百 MB），
    而流式响应让「生成一行 → 发送一行 → 丢弃」成为可能，内存占用由**单行大小**决定，
    而不是由结果集行数决定。
    前端 `TransactionsView.vue` 的「导出」按钮（拿到 Blob 后触发下载）。
    """
    service = CsvService(session)
    # 文件名由服务端生成：带日期便于用户区分多次导出；纯 ASCII，避免文件名编码问题
    filename = f"goalfin_{date.today().isoformat()}.csv"
    return StreamingResponse(
        service.export_stream(
            current_user.id,
            _parse_optional_date(start_date, "start_date"),
            _parse_optional_date(end_date, "end_date"),
        ),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/import",
    response_model=ImportResult,
    summary="导入账单 CSV（逐行校验、自动建分类、自动去重）",
)
async def import_csv(
    current_user: CurrentUser,
    session: SessionDep,
    file: Annotated[UploadFile, File(description="UTF-8 或 GBK 编码的 CSV 文件")],
) -> ImportResult:
    """导入账单 CSV（需要登录，导入到当前用户名下）。

    multipart/form-data 单文件字段名必须是 `file`（前端 `csvApi.import` 用 `form.append('file', ...)`）。
    200 部分成功：返回 `{total, imported, failed, errors[]}`，错误行带行号与原因；
    400：非 .csv 扩展名 / 文件超过 5MB / 文件为空 / 表头列数不足 / 行数超过 1 万；
    401：令牌无效。注意「行内数据非法」不会整体失败，而是记进 `errors` 继续处理——
    这是导入类接口的通用设计（用户不想因为第 300 行日期写错而丢掉前 299 行）。

    上传文件为什么不用 `await file.read()` 一次读完：`UploadFile` 底层是
    `SpooledTemporaryFile`（小文件在内存、超过阈值自动溢写到临时磁盘），
    一次 read 会把整个文件拉进内存；这里按 64KB 分块累加，**先超限先拒绝**。
    前端 `TransactionsView.vue` 的导入弹窗（选择文件 → 提交 → 展示错误明细表格）。
    """
    # 扩展名白名单：挡住误传 .xlsx/.txt。真正的格式校验在 CsvService（看表头与逐行解析）
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise BadRequestError("仅支持 .csv 文件")

    # 分块读取并累计大小，避免 read() 一次性把超大文件读进内存
    chunks: list[bytes] = []
    size = 0
    # 海象运算符：赋值并立即判断，读到空 bytes 即 EOF，循环自然结束
    while chunk := await file.read(64 * 1024):
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            # 超限立刻中断并返回 400：此时未读的部分不会被拉进内存
            raise BadRequestError(f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限")
        chunks.append(chunk)

    if size == 0:
        raise BadRequestError("上传文件为空")

    # 拼回 bytes 交给 Service：由它负责编码探测（utf-8-sig → utf-8 → gbk）与逐行校验。
    # 也可以把 chunks 交给 Service 边拼边解析以进一步省内存，此处为可读性做了取舍。
    return await CsvService(session).import_csv(current_user.id, b"".join(chunks))
