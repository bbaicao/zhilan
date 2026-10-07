"""文档解析：txt / md / pdf / docx / xlsx / xls / pptx / csv / 六种图片 / 代码 / 配置 / 前端资源。

改造点（算法优化方案 M3）：PDF 额外产出**按页文本**（``parse_document_pages``），
供清洗流水线的跨页重复行检测（页眉页脚）使用。没有页信息时该检测会自然跳过，
不会误删正常文档里的重复行。

PDF 分流与逐页补齐（条目 2.2；缺陷 ``回归修复`` / ``回归修复``）：
① pypdf 快提 → 文本密度达标即认定原生 PDF，**不整篇重跑 MinerU**（90% 的原生 PDF 就此打住）；
② 这一支里若**存在整页无文本**的页，只把这几页抽出来补一次 OCR（密度是**整篇**指标，混排 PDF 的
纯图像页原先会静默丢失）——规模有上限、页槽位不漂移，细则见 :func:`_fill_scan_pages`；
③ 密度不足才**整篇**交给 MinerU；④ MinerU 不可用或失败一律回落 pypdf（I5 绝不炸穿）。
整条分流只有 :func:`_parse_pdf_document` **一处实现**——原先 ``_parse_pdf`` 是同一逻辑的第二份
拷贝且不含密度分流，已删除。

上传格式扩展：新增 docx / xlsx / xls / pptx / csv 五种格式，统一走
「按文档结构转成带小节标题的文本」——docx 按正文顺序输出段落与 Markdown 表格，
表格类（xlsx / xls / csv）输出 ``## Sheet 名``（csv 无）+ Markdown 表格，
pptx 按页输出 ``## 幻灯片 i``。这些格式拿不到页边界，``pages`` 一律返回 []。

图片格式扩展（阶段 3 · 条目 3.1）：新增 jpg / jpeg / png / bmp / tiff / webp 六种图片，
走 MinerU OCR（拍照文档 / 截图 / 扫描图片从「不支持」变为「可检索」）。图片没有原生文本层，
故**必须**走 OCR：``MINERU_ENABLE`` 关或 MinerU 不可用时给中文可读报错，不做静默降级。
图片同样拿不到页边界（单张图片当一页），``pages`` 返回 []。
"""

import codecs
import csv
import logging
import re
from collections.abc import Callable
from io import BytesIO, StringIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from app.core import derived_counters
from app.core.config import settings
from app.core.errors import DocumentParseError
from app.pipeline import mineru

log = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = {
    ".txt", ".md", ".markdown", ".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".csv",
    # 阶段 3 · 条目 3.1：六种图片格式走 MinerU OCR
    ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp",
    # 工具升级-3 · 回归检查（单② 定档 (c)）：代码后缀（16）——走文本直读 + chunk_code
    ".py", ".js", ".ts", ".java", ".go", ".rs", ".c", ".h", ".cpp", ".cs", ".rb", ".php",
    ".sh", ".sql", ".kt", ".swift",
    # 配置后缀（7）
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml",
    # 前端资源后缀（6）
    ".jsx", ".tsx", ".vue", ".css", ".scss", ".html",
}

# 用户可见的「支持哪些格式」提示文案（条目 3.3）：parsing / document_routes / folder_scan
# 三处共用本常量，避免扩展格式时漏改某处而给出误导性提示。
# 顺序与 ``SUPPORTED_SUFFIXES`` 的书写顺序一致（文本 → Office → 表格 → 图片 → 代码 → 配置 → 前端资源）。
# ⚠️ 例外（工具升级-3 · 回归检查 保持既有口径）：`.markdown` 是 `.md` 的别名，历来不进本清单
# ⇒ token 数 = 后缀数 − 1（现为 43 = 44 − 1）。加新后缀时**不得**顺手把 `markdown` 补进来。
SUPPORTED_FORMATS_HINT = (
    "txt / md / pdf / docx / xlsx / xls / pptx / csv / jpg / jpeg / png / bmp / tiff / webp"
    " / py / js / ts / java / go / rs / c / h / cpp / cs / rb / php / sh / sql / kt / swift"
    " / json / yaml / yml / toml / ini / cfg / xml"
    " / jsx / tsx / vue / css / scss / html"
)

# 工具升级-3 · 回归检查：走「纯文本直读」的 29 个后缀（= 回归检查 新增的那 29 项，
# 即 ``SUPPORTED_SUFFIXES`` 减去原有 15 项文档后缀）。它们**不进** ``_PARSERS``、**不送** MinerU，
# 由 :func:`parse_document_pages` 在 PDF 与 ``_PARSERS`` 之后、通用 ``_decode`` 之前单独处理：
# 空字节按二进制闸拒绝 → :func:`detect_encoding` 解码 → 去掉至多一个 BOM → 返回 ``(text, [])``。
# ⚠️ 本常量是 ``SUPPORTED_SUFFIXES`` 的**真子集**（有判据钉住，见 tests/pipeline/test_code_suffixes_t3_2.py）。
CODE_SUFFIXES = frozenset({
    # 代码 16
    ".py", ".js", ".ts", ".java", ".go", ".rs", ".c", ".h", ".cpp", ".cs", ".rb", ".php",
    ".sh", ".sql", ".kt", ".swift",
    # 配置 7
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml",
    # 前端资源 6
    ".jsx", ".tsx", ".vue", ".css", ".scss", ".html",
})

SCANNED_PDF_HINT = "该 PDF 未提取到文字，请上传文字版或 txt/md。"
# 回归修复（2026-09-22 复核修复）：MinerU 开着但**模型包 / 解析器没准备好**时，
# 因果在模型包，不在这份 PDF 上 —— 不能拿 SCANNED_PDF_HINT 让用户去换文件。
NO_MODEL_PACK_HINT = "未检测到离线模型包，请先在设置中导入模型包后重试（MinerU 不可用：{reason}）。"
EMPTY_HINT = "文件内容为空，无法入库。"
# 图片 OCR 的两条独立开关提示（条目 3.4）：总开关未开 / 图片开关被显式关闭。
IMAGE_OCR_DISABLED_HINT = "图片 OCR 未启用，请开启 MINERU_ENABLE 或安装 MinerU 后重试。"
IMAGE_OCR_OFF_HINT = "图片 OCR 已关闭（OCR_IMAGE_ENABLE=False），请开启后重试。"
IMAGE_EMPTY_HINT = "图片未识别到文字内容。"

# 单个 sheet 输出超过这么多行就截断（防止几千行的表把上下文/分块撑爆）
MAX_SHEET_ROWS = 5000

# 逐页补齐（`_fill_scan_pages`）的规模上限——**对抗性复核 F2 补的护栏**：
# 密度闸门要求「可提取文本 ≥ 文件字节的 1%」，纯扫描件够不着（实测 200KB/页的样本密度仅 0.0005），
# 但**文字页与整页插图混排**的 PDF（工具生成的图文报告）可以既达标又大面积缺文本。
# 那时「只跑缺页」的假设就失效了：一次同步 OCR 几百页在 CPU 上可能数十分钟，子 PDF 还要整份驻留内存。
# 超限一律**不补齐**（退回升级前行为）并记 warning——宁可少补，也不在请求路径里跑无界任务。
_FILL_MAX_PAGES = 50
_FILL_MAX_SUBSET_BYTES = 64 * 1024 * 1024

# 「文本层覆盖」判据的单页有效字符下限（`回归修复`）：
# 每页 ``len(page.strip())`` 达到该值才算「这一页有文本层」。**全部页都有文本层**的原生 PDF
# 直接走原生路（见 :func:`_parse_pdf_document` 的分流式），密度只作参考——密度分母是 PDF 字节数，
# 会被图片体积稀释（实测样本 A：有文本层、密度 0.0077 < 0.01，被误判扫描件走 OCR）。
# 设下限是防「盖章/页码戳扫描件」漏网：这类 PDF 每页也可能有零星 OCR 文本，但一页凑不出 20 个有效字符。
_MIN_TEXT_PER_PAGE = 20

_HEADING_STYLE_RE = re.compile(r"^(?:Heading|标题)\s*(\d+)$", re.IGNORECASE)


def _decode(data: bytes) -> str:
    return data.decode(detect_encoding(data))


#: 解码后的真正 NUL 拒绝；BOM 宽字符文本里的编码零字节不代表二进制。
BINARY_HINT = "该文件含有空字节，不能按文本入库。"


def detect_encoding(data: bytes) -> str:
    """严格解码后拒绝明确非文本；BOM 的字节序优先，调用方至多剥一个 BOM。"""
    if data.startswith((b"\x89PNG\r\n\x1a\n", b"PK\x03\x04", b"%PDF-",
                        b"GIF87a", b"GIF89a", b"\xff\xd8\xff", b"\x1f\x8b", b"\x7fELF")):
        raise DocumentParseError("该文件是二进制格式，不能按文本入库。")
    # UTF-32 LE 与 UTF-16 LE 共享前两个字节，必须先检查四字节 BOM。
    encodings = ("utf-8", "gb18030", "latin-1")
    for bom, encoding in (
        (codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),
        (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"),
    ):
        if data.startswith(bom):
            encodings = (encoding,)
            break
    for encoding in encodings:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" in text:
            raise DocumentParseError(BINARY_HINT)
        if re.search(r"[\x01-\x08\x0b\x0e-\x1f\x7f-\x9f]", text):
            raise DocumentParseError("该文件含有非文本控制字符，不能按文本入库。")
        # ponytail: 只拒绝可确定的二进制证据；全可打印的未知编码不做概率猜测。
        return encoding
    raise DocumentParseError("文件编码无法识别，请转成 UTF-8 后重新上传。")


def _parse_code(data: bytes) -> tuple[str, list[str]]:
    """代码 / 配置 / 前端资源后缀的**纯文本直读**（工具升级-3 · 回归检查 · D-回归检查 / D-回归检查）。

    与 txt / md 共用严格文本检查；合法 BOM UTF-16/32 的编码空字节不判作二进制。

    返回 ``(text, [])``：代码文件没有页边界信息，与 txt / md 同口径。
    ⚠️ **不在这里做「去掉 BOM」之外的内容加工** —— 缩进必须原样保留（清洗会剥缩进，
    那是 ``回归检查`` 改道 ``clean(enable=False)`` 要解决的事，不在本笔）。
    """
    text = _decode(data)
    # 只剥**一个** BOM：`utf-8-sig` 命中时 BOM 已被吃掉，走 `utf-8` 分支时它会以 `\ufeff` 留下。
    # 不剥 ⇒ 阶段 2 的 `ast.parse` 会解析失败、静默退化成行窗切块（`D-回归检查` 第 2 条）。
    if text.startswith("\ufeff"):
        text = text[1:]
    if not text.strip():
        raise DocumentParseError(EMPTY_HINT)
    return text, []


def _md_cell(value) -> str:
    """单元格值转 Markdown 表格单元格文本：竖线转义、换行拍平成空格。"""
    text = "" if value is None else str(value)
    text = text.replace("|", "\\|").replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return text.strip()


def _md_table(rows: list[list[str]]) -> str:
    """行集转 Markdown 表格：首行作表头，短行补空单元格。rows 非空。"""
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(padded[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in padded[1:]]
    return "\n".join(lines)


def _sheet_block(title: str, rows: list[list[str]]) -> str | None:
    """一个 sheet 的行集转「## 标题 + 表格」块；超行数截断并注释；空行集返回 None。"""
    if not rows:
        return None
    lines = [f"## {title}", _md_table(rows[:MAX_SHEET_ROWS])]
    if len(rows) > MAX_SHEET_ROWS:
        lines.append(f"> （Sheet「{title}」超过 {MAX_SHEET_ROWS} 行，已截断）")
    return "\n".join(lines)


def _drop_trailing_empty_rows(rows: list[list[str]]) -> list[list[str]]:
    """去掉尾部整行空白（编辑器/导出工具常多留几行空行）。"""
    while rows and not any(cell for cell in rows[-1]):
        rows.pop()
    return rows


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def _pdf_raw_pages(data: bytes) -> list[str]:
    """按页返回 PDF 文本（已 strip），**保留空页**——空页的下标就是原始页号。

    逐页分流（:func:`_fill_scan_pages`）要靠「第几页没有文本层」这个位置信息，
    故这一层不能丢空页；需要「只要文本页」的调用方用 :func:`_pdf_pages`。
    """
    try:
        reader = PdfReader(BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except DocumentParseError:
        raise
    except Exception as exc:
        raise DocumentParseError(f"PDF 解析失败：{exc}") from exc
    return [page.strip() for page in pages]


def _pdf_pages(data: bytes) -> list[str]:
    """按页返回 PDF 文本，空页丢弃（**丢弃后的下标是「文本页序号」，不是原始页号**）。"""
    return [page for page in _pdf_raw_pages(data) if page]


class SourcePages(list[str]):
    """保持现有列表出口，同时携带解析器在拼接时已知的页号和字符范围。"""

    def __init__(self, pages: list[str], source_spans: list[dict]):
        super().__init__(pages)
        self.source_spans = source_spans


def _page_result(raw_pages: list[str], *, keep_slots: bool = True) -> tuple[str, list[str]]:
    """按页文本 → 出口 ``(全文, 按页文本)``：全文恒由**文本页**用空行拼回（空页不产生空段落）。

    ``keep_slots`` 只控制「按页列表」这一半（**默认 ``True`` = 页槽位不漂移**，D1 起为唯一生产口径）：

    - ``True``（默认）：**空页占位保留**——``pages`` 与原始页序**等长**，无文本层的页占一个空串。
      某页没有文本层（扫描页 / 纯图页 / 真空白页）时若把它丢掉，其后所有页文本会**整体前移一位**，
      位置传递以原始页槽位为准，不能把「文本页序号」写成原始页号。
      与 ``mineru._markdown_pages`` 的「空页照样占一格」是同一条纪律。
    - ``False``：**丢空页**——``回归修复`` 修复**之前**的旧口径，原生 PDF 曾与它逐字一致。
      **已无生产调用点**；仅为「显式需要旧行为」的场合保留，新代码不要用。

    出口不变量：``pages`` 的每一项都是 ``text`` 的字面子串（空串是任何字符串的子串，占位不破坏它）。
    """
    text = _pdf_text([page for page in raw_pages if page])
    pages = list(raw_pages) if keep_slots else [page for page in raw_pages if page]
    spans = []
    position = 0
    for page_number, value in enumerate(raw_pages, 1):
        if value:
            spans.append({"page": page_number, "raw_start": position, "raw_end": position + len(value)})
            position += len(value) + 2
    return text, SourcePages(pages, spans)


def _mineru_page_result(text: str, pages: list[str], page_count: int) -> tuple[str, list[str]]:
    """已有整页拼接 / 显式分页标记才能传位置；丢页或退化成整篇时不猜页号。"""
    from app.pipeline.cleaning import page_source_spans

    spans = page_source_spans(text, pages) if len(pages) == page_count else []
    return text, SourcePages(pages, spans)


def _pdf_text(pages: list[str]) -> str:
    """按页文本拼全文：页边界当段落边界（清洗的「重复段落到重」以空行切块，
    页与页不该并成一段）。"""
    return "\n\n".join(pages)


def _text_density(pages: list[str], data: bytes) -> float:
    """PDF 文本密度 = 提取文本字符数 ÷ PDF 字节数。

    原生 PDF 通常 > 0.05，纯扫描件接近 0（没有文本层）。分母取 PDF 字节数而不是
    页数，是为了让「页大图多」的扫描件密度自然趋近 0；``max(..., 1)`` 防除零。
    """
    return len(_pdf_text(pages)) / max(len(data), 1)


def _subset_pdf(data: bytes, indexes: list[int]) -> bytes | None:
    """把指定页抽成一份子 PDF（页序保持），供 OCR 只跑这几页；失败返回 ``None``。

    用 pypdf 自带的 ``PdfWriter``：解析侧本来就用 pypdf，不新增依赖、也不改 MinerU 的调用形态。
    """
    try:
        reader = PdfReader(BytesIO(data))
        writer = PdfWriter()
        for index in indexes:
            writer.add_page(reader.pages[index])
        buffer = BytesIO()
        writer.write(buffer)
    except Exception as exc:  # noqa: BLE001 —— 抽页失败只是「这几页补不上」，不影响主链
        log.warning("PDF 抽页失败（%s），跳过无文本页补齐", exc)
        return None
    return buffer.getvalue()


def _fill_scan_pages(data: bytes, raw_pages: list[str]) -> tuple[str, list[str]]:
    """密度达标但存在「整页无文本」的页时，只补这几页的 OCR（缺陷 ``回归修复``）。

    **为什么需要它**：文本密度是**整篇**指标。一份「正文页 + 扫描页」混排的 PDF 只要正文占比够，
    整篇就判为原生 PDF，纯图像页便**永不进 OCR、内容静默丢失、全程无日志**。

    **做法**：把缺文本的页抽成子 PDF 单独 OCR，再按页序贴回原位。五条纪律：
    - **只跑缺页**（代价与缺页数成正比，不是整篇重解析——整篇重跑在 CPU 上可能数分钟）；
    - **规模有上限**：缺页数 / 子 PDF 字节超 ``_FILL_MAX_PAGES`` / ``_FILL_MAX_SUBSET_BYTES`` 就不补
      （对抗性复核 F2：图文混排 PDF 也能「密度达标且大面积缺文本」，请求路径里不许跑无界同步 OCR）；
    - **形状必须可用**：条数 ≠ 缺页数，或页文本不是字符串，一律整页保留原样（前者会让 ``zip`` 丢页/错位，
      后者会让下面的 ``strip()`` 在**回落分支里**抛异常）；
    - **贴回后页槽位不漂移**：出口固定用 ``keep_slots=True``——某页 OCR 真的返回空串（空白页）时，
      丢掉它会让其后所有页**整体前移一位**（``chunking._assign_page`` 返回的是下标 ⇒ 页码静默错标；
      对抗性复核 F1）。**这也是 ``_page_result`` 自 D1 起的默认口径**：
      下面几条「放弃补齐」的退路同样保留槽位，否则退路本身就会造成同一个前移。
    - **尽力而为**：任何异常都只是「没补上」，原样返回 pypdf 结果（I5 绝不炸穿）。

    ⚠️ **外部假设（本机无真 MinerU，无法验证；已在证据文件单列）**：``mineru.parse_pdf`` 的按页文本
    顺序 = 子 PDF 的页序（即 ``missing`` 的升序）。上面的条数校验挡得住「多页 / 少页」，**挡不住
    「顺序被反」**——真机上若顺序反了，症状是页码互换且**没有任何日志**。只缺 1 页时天然免疫。
    """
    missing = [index for index, page in enumerate(raw_pages) if not page]
    if not missing:  # 常态：没有缺页，零额外开销、连 available() 都不问
        return _page_result(raw_pages)
    if len(missing) > _FILL_MAX_PAGES:
        log.warning(
            "PDF 缺文本页 %d 页 > 上限 %d，不补齐（避免在请求路径里跑无界 OCR，退回升级前行为）",
            len(missing), _FILL_MAX_PAGES,
        )
        # 回归修复（S13 退路A）：按规模上限主动放弃补齐 ⇒ 这几页内容仍是 pypdf 的空文本。
        derived_counters.bump("pdf_ocr_fallback")
        return _page_result(raw_pages)
    if not (settings.MINERU_ENABLE and mineru.available()[0]):
        # 默认档（MINERU_ENABLE=False）语义不变：一律 pypdf，不 import 重依赖。
        log.debug("PDF 有 %d 页没有文本层，按当前配置未走 OCR", len(missing))
        return _page_result(raw_pages)
    subset = _subset_pdf(data, missing)
    if subset is None:
        # 回归修复（S13 退路C）：子 PDF 构造失败（抽页异常）⇒ 补齐没跑成。
        derived_counters.bump("pdf_ocr_fallback")
        return _page_result(raw_pages)
    if len(subset) > _FILL_MAX_SUBSET_BYTES:
        log.warning(
            "PDF 缺页子 PDF %.1fMB > 上限 %.1fMB，不补齐",
            len(subset) / 1048576, _FILL_MAX_SUBSET_BYTES / 1048576,
        )
        # 回归修复（S13 退路D）：按字节上限主动放弃补齐。
        derived_counters.bump("pdf_ocr_fallback")
        return _page_result(raw_pages)
    try:
        _text, ocr_pages = mineru.parse_pdf(subset)
    except Exception as exc:  # noqa: BLE001 —— 补齐是尽力而为
        log.warning("PDF 无文本页补齐失败（%s），保留 pypdf 结果", exc)
        # 回归修复（S11）：OCR 补齐失败 ⇒ 缺页内容丢失（保留 pypdf 结果）。
        derived_counters.bump("pdf_ocr_fallback")
        return _page_result(raw_pages)
    if len(ocr_pages) != len(missing) or not all(isinstance(page, str) for page in ocr_pages):
        # 条数对不上会让 zip 丢页 / 错位；非字符串项会让下面的 strip() 抛异常（补齐路径越界炸穿 I5）。
        # 两种形状都一律放弃补齐——本函数对 MinerU 是**外部边界**，这里做形状校验不算防御式冗余。
        log.warning(
            "PDF 无文本页补齐：OCR 返回不可用（%d 页 / 缺页 %d），放弃补齐以免页码错位",
            len(ocr_pages), len(missing),
        )
        # 回归修复（S13 退路E）：OCR 返回形状不可用 ⇒ 放弃补齐（防页码错位）。
        derived_counters.bump("pdf_ocr_fallback")
        return _page_result(raw_pages)
    filled = list(raw_pages)
    for index, ocr_page in zip(missing, ocr_pages):
        filled[index] = ocr_page.strip()
    log.info("PDF 无文本页补齐：%d 页已走 OCR 并贴回原页序", len(missing))
    # keep_slots=True：OCR 返回空串（真空白页）时也占住这一格，其后的页号才不会前移（F1）。
    return _page_result(filled, keep_slots=True)


def _parse_pdf_document(data: bytes) -> tuple[str, list[str]]:
    """PDF 主分流（条目 2.2 + 缺陷 ``回归修复``/``007``/``090``）：**唯一**的 PDF 解析实现。

    顺序不能反：先 pypdf 快提（毫秒级）再决定要不要上 OCR；反过来（先 MinerU）
    会让 90% 的原生 PDF 白跑重模型。判「原生」有两条**或**关系的腿：

    1. **文本层覆盖腿**（``090``）：每一页（按原始页序，含空页一并数）都提出了
       ≥ :data:`_MIN_TEXT_PER_PAGE` 个有效字符 ⇒ 整篇有文本层 ⇒ 原生。扫描件的定义是
       「没有文本层」，不是「密度低」——密度分母是 PDF 字节数，图多的原生 PDF 会被
       图片字节稀释（样本 A：0.0077 < 0.01），故**每页都有文本层时密度不作数**；
    2. **密度腿**（既有）：``_text_density >= settings.OCR_SCAN_THRESHOLD``，兜「大多数页有文本、
       少数页整页无文本」的图文混排形态。

    任一腿命中 ⇒ :func:`_fill_scan_pages`：常态下零开销直接返回，有整页无文本才逐页补齐
    （``回归修复`` 那一支，行为不变）。
    """
    raw_pages = _pdf_raw_pages(data)
    pages = [page for page in raw_pages if page]
    text = _pdf_text(pages)
    covered = sum(1 for page in raw_pages if len(page.strip()) >= _MIN_TEXT_PER_PAGE)
    text_layer_everywhere = bool(raw_pages) and covered == len(raw_pages)
    if text and (text_layer_everywhere or _text_density(pages, data) >= settings.OCR_SCAN_THRESHOLD):
        return _fill_scan_pages(data, raw_pages)
    # 密度不足或一个字都没提出来 → 判定扫描件，试 MinerU（可插拔）。
    # 开启且可用才试；任何失败回落 pypdf（I5 绝不炸穿）。
    mineru_on = bool(settings.MINERU_ENABLE)
    mineru_ok, mineru_reason = mineru.available() if mineru_on else (False, "")
    if mineru_on and not mineru_ok:
        # 回归修复：这里**不能**静默落到下面的扫描件提示 —— 用户看到「请上传文字版」
        # 会去换 PDF，而真因是模型包没导入（6.5/6.6 之后桌面默认 MINERU_ENABLE=True，
        # 「导入了模型包却没有任何效果」正是这条缺陷的用户可见形态）。
        log.warning("MinerU 不可用（%s），回落 pypdf 文本", mineru_reason)
    elif mineru_on:
        try:
            ocr_text, ocr_pages = mineru.parse_pdf(data)
            return _mineru_page_result(ocr_text, ocr_pages, len(raw_pages))
        except DocumentParseError as exc:
            log.warning("MinerU 解析失败，回落 pypdf：%s", exc)
            # 回归修复（S12）：整篇 MinerU 解析失败 ⇒ 回落 pypdf 文本层（密度/覆盖腿的兜底失效）。
            derived_counters.bump("pdf_ocr_fallback")
        except Exception as exc:  # noqa: BLE001
            log.warning("MinerU 解析异常，回落 pypdf：%s", exc)
            # 回归修复（S12）：同上，异常形态的整篇回落。
            derived_counters.bump("pdf_ocr_fallback")
    # MinerU 不可用或失败 → 回落 pypdf 文本；啥也没提出来才报错（按因果分流）
    if not text:
        if mineru_on and not mineru_ok:
            raise DocumentParseError(NO_MODEL_PACK_HINT.format(reason=mineru_reason))
        raise DocumentParseError(SCANNED_PDF_HINT)
    # D1：这里**不能**直接返回上面的 ``pages``——那是丢掉空页的视图，会让「有页无文本层」的 PDF
    # 其后页号**整体前移一位**（真机复现：3 页样本里原文第 3 页的内文被标成第 2 页）。
    # 与 ``_fill_scan_pages`` 的出口同口径：页槽位与原始页序对齐。
    return _page_result(raw_pages)


# ---------------------------------------------------------------------------
# Word（docx）
# ---------------------------------------------------------------------------


def _docx_heading_prefix(paragraph) -> str:
    """Heading N / 标题 N 样式映射成 #*N 前缀，其余样式返回空串。"""
    try:
        name = (paragraph.style.name or "").strip()
    except Exception:
        return ""
    match = _HEADING_STYLE_RE.match(name)
    return f"{'#' * int(match.group(1))} " if match else ""


def _parse_docx(data: bytes) -> tuple[str, list[str]]:
    try:
        import docx
    except ImportError as exc:
        raise DocumentParseError("缺少依赖 python-docx，请先 pip install 后重试。") from exc

    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        document = docx.Document(BytesIO(data))
        blocks: list[str] = []
        # 按 body 子节点顺序迭代 w:p / w:tbl，保证段落与表格的原始交错顺序
        for child in document.element.body.iterchildren():
            if isinstance(child, CT_P):
                paragraph = Paragraph(child, document)
                text = paragraph.text.strip()
                if not text:
                    continue
                blocks.append(_docx_heading_prefix(paragraph) + text)
            elif isinstance(child, CT_Tbl):
                table = Table(child, document)
                rows = [[_md_cell(cell.text) for cell in row.cells] for row in table.rows]
                if rows:
                    blocks.append(_md_table(rows))
    except DocumentParseError:
        raise
    except Exception as exc:
        raise DocumentParseError(f"Word 文档解析失败：{exc}") from exc
    if not blocks:
        raise DocumentParseError(EMPTY_HINT)
    return "\n\n".join(blocks), []


# ---------------------------------------------------------------------------
# Excel（xlsx / xls）
# ---------------------------------------------------------------------------


def _xlsx_sheet_rows(sheet) -> list[list[str]]:
    """读一个 sheet 的缓存值（data_only=True），多读到第 MAX_SHEET_ROWS+1 行以判断截断。"""
    rows: list[list[str]] = []
    for row in sheet.iter_rows(values_only=True):
        if len(rows) >= MAX_SHEET_ROWS + 1:
            break
        rows.append([_md_cell(value) for value in row])
    return _drop_trailing_empty_rows(rows)


def _parse_xlsx(data: bytes) -> tuple[str, list[str]]:
    try:
        import openpyxl
    except ImportError as exc:
        raise DocumentParseError("缺少依赖 openpyxl，请先 pip install 后重试。") from exc
    try:
        workbook = openpyxl.load_workbook(BytesIO(data), data_only=True)
        blocks = [block for sheet in workbook.worksheets if (block := _sheet_block(sheet.title, _xlsx_sheet_rows(sheet)))]
    except DocumentParseError:
        raise
    except Exception as exc:
        raise DocumentParseError(f"Excel 解析失败：{exc}") from exc
    if not blocks:
        raise DocumentParseError(EMPTY_HINT)
    return "\n\n".join(blocks), []


def _xls_sheet_rows(sheet, book) -> list[list[str]]:
    import xlrd

    rows: list[list[str]] = []
    for r in range(min(sheet.nrows, MAX_SHEET_ROWS + 1)):
        cells: list[str] = []
        for c in range(sheet.ncols):
            cell = sheet.cell(r, c)
            value = cell.value
            if cell.ctype == xlrd.XL_CELL_DATE:
                try:
                    value = xlrd.xldate_as_datetime(value, book.datemode)
                except Exception:
                    value = cell.value  # 转不动的日期保持原值
            cells.append(_md_cell(value))
        rows.append(cells)
    return _drop_trailing_empty_rows(rows)


def _parse_xls(data: bytes) -> tuple[str, list[str]]:
    try:
        import xlrd
    except ImportError as exc:
        raise DocumentParseError("缺少依赖 xlrd，请先 pip install 后重试。") from exc
    try:
        book = xlrd.open_workbook(file_contents=data)
        blocks = [block for sheet in book.sheets() if (block := _sheet_block(sheet.name, _xls_sheet_rows(sheet, book)))]
    except DocumentParseError:
        raise
    except Exception as exc:
        raise DocumentParseError(f"Excel 97-2003 解析失败：{exc}") from exc
    if not blocks:
        raise DocumentParseError(EMPTY_HINT)
    return "\n\n".join(blocks), []


# ---------------------------------------------------------------------------
# PowerPoint（pptx）
# ---------------------------------------------------------------------------


def _parse_pptx(data: bytes) -> tuple[str, list[str]]:
    try:
        from pptx import Presentation
    except ImportError as exc:
        raise DocumentParseError("缺少依赖 python-pptx，请先 pip install 后重试。") from exc
    try:
        presentation = Presentation(BytesIO(data))
        blocks: list[str] = []
        for index, slide in enumerate(presentation.slides, start=1):
            parts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    text = shape.text_frame.text.strip()
                    if text:
                        parts.append(text)
            notes = ""
            if slide.has_notes_slide:
                notes = (slide.notes_slide.notes_text_frame.text or "").strip()
            if not parts and not notes:
                continue
            lines = [f"## 幻灯片 {index}", *parts]
            if notes:
                lines.append(f"**备注：**{notes}")
            blocks.append("\n\n".join(lines))
    except DocumentParseError:
        raise
    except Exception as exc:
        raise DocumentParseError(f"PPT 解析失败：{exc}") from exc
    if not blocks:
        raise DocumentParseError(EMPTY_HINT)
    return "\n\n".join(blocks), []


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def _parse_csv(data: bytes) -> tuple[str, list[str]]:
    try:
        text = _decode(data)
        if not text.strip():
            raise DocumentParseError(EMPTY_HINT)
        # newline='' 是 csv 模块官方口径：不转译换行，让 csv 自己处理 \r\n 与引号内换行
        rows = [
            [_md_cell(field) for field in row]
            for row in csv.reader(StringIO(text, newline=""))
            if any(field.strip() for field in row)
        ]
    except DocumentParseError:
        raise
    except csv.Error as exc:
        raise DocumentParseError(f"CSV 解析失败：{exc}") from exc
    except Exception as exc:
        raise DocumentParseError(f"CSV 解析失败：{exc}") from exc
    if not rows:
        raise DocumentParseError(EMPTY_HINT)
    if len(rows) == 1 and len(rows[0]) <= 1:
        return (rows[0][0] if rows[0] else ""), []
    return _md_table(rows), []


# ---------------------------------------------------------------------------
# 图片（jpg / jpeg / png / bmp / tiff / webp）· 阶段 3 · 条目 3.1
# ---------------------------------------------------------------------------


def _parse_image(data: bytes) -> tuple[str, list[str]]:
    """图片走 MinerU OCR；图片没有原生文本层，故不做任何静默降级。

    开关语义（条目 3.1 + 3.4，**顺序即优先级，两个开关都关时报「未启用」**）：
    - ``MINERU_ENABLE`` 关 → :data:`IMAGE_OCR_DISABLED_HINT`（总开关没开，装没装 MinerU 都一样）；
    - ``MINERU_ENABLE`` 开、``OCR_IMAGE_ENABLE`` 关 → :data:`IMAGE_OCR_OFF_HINT`（只关图片这一路）；
    - MinerU 不可用（未安装 / 版本 API 不匹配）→ 同样报「未启用」，附 ``available()`` 给的原因。

    出口与其它格式一致：``(全文, 按页文本)``。OCR 文本没有页边界可依，``pages`` 返回 []，
    与 txt / md 及五种 Office 格式的口径相同；识别不出文字时抛 :data:`IMAGE_EMPTY_HINT`，
    不让空文本静默流进切块（否则会产出「入库成功但零 chunk」的假象）。
    """
    if not settings.MINERU_ENABLE:
        raise DocumentParseError(IMAGE_OCR_DISABLED_HINT)
    if not settings.OCR_IMAGE_ENABLE:
        raise DocumentParseError(IMAGE_OCR_OFF_HINT)
    ok, reason = mineru.available()
    if not ok:
        raise DocumentParseError(f"{IMAGE_OCR_DISABLED_HINT}（{reason}）")
    text, _pages = mineru.parse_image(data)
    if not text.strip():
        raise DocumentParseError(IMAGE_EMPTY_HINT)
    return text, []


# 解析函数统一签名：(data: bytes) -> (全文, 按页文本)。
# 按后缀分发的注册表：txt / md 走纯文本解码（直接 _decode，不进表），
# PDF 需要阶段 2 的智能分流，暂不进表、由 parse_document_pages 单独处理。
ParserFn = Callable[[bytes], tuple[str, list[str]]]

_PARSERS: dict[str, ParserFn] = {
    ".docx": _parse_docx,
    ".xlsx": _parse_xlsx,
    ".xls": _parse_xls,
    ".pptx": _parse_pptx,
    ".csv": _parse_csv,
    # 阶段 3 · 条目 3.1：六种图片后缀注册到 _parse_image（走 OCR，非原生解析）
    ".jpg": _parse_image,
    ".jpeg": _parse_image,
    ".png": _parse_image,
    ".bmp": _parse_image,
    ".tiff": _parse_image,
    ".webp": _parse_image,
}


def parse_document(filename: str, data: bytes) -> str:
    """按文件名后缀解析二进制内容，返回纯文本。"""
    text, _pages = parse_document_pages(filename, data)
    return text


def parse_document_pages(filename: str, data: bytes) -> tuple[str, list[str]]:
    """解析文档，返回 (全文, 按页文本)。

    ``pages`` 只有 PDF 才有内容；txt / md、五种 Office 格式（docx / xlsx / xls /
    pptx / csv）、六种图片（阶段 3）与代码 / 配置 / 前端资源三类后缀（工具升级-3 · 回归检查）
    都返回空列表，表示「没有页边界信息」。
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise DocumentParseError(f"暂不支持 {suffix or '该'} 格式，请上传 {SUPPORTED_FORMATS_HINT}。")
    if not data:
        raise DocumentParseError(EMPTY_HINT)
    if suffix == ".pdf":
        # 条目 2.2 的 PDF 智能分流整体收在 `_parse_pdf_document` 一处（缺陷 回归修复：
        # 原先这里内联一份、`_parse_pdf` 又是另一份，两份有漂移风险）。
        return _parse_pdf_document(data)
    parser = _PARSERS.get(suffix)
    if parser is not None:
        # 图片与 Office 同路径（条目 3.1 规格 4）：按后缀查注册表即可。
        # OCR 开关判定在 `_parse_image` 内部完成（关时抛中文可读错，不静默、不裸异常），
        # 故这里不需要再为图片单开分支。
        return parser(data)
    if suffix in CODE_SUFFIXES:
        # 工具升级-3 · 回归检查：29 个代码 / 配置 / 前端资源后缀走纯文本直读。
        # ⚠️ 位置是**写死**的（`D-回归检查` 第 2 条）：在 pdf 与 `_PARSERS` 之后、**通用 `_decode` 之前**。
        # 放在 `_decode` 之后会被它先截胡（那就不叫「代码分支」了）；放进 `_PARSERS` 会把
        # 图片/Office 的注册表语义搅浑；送 MinerU 更不行（那是 PDF / 图片的路）。
        return _parse_code(data)
    text = _decode(data)
    # 回归修复：`_decode` 的尝试顺序是 utf-8 → utf-8-sig → … ⇒ 带 BOM 的文件由 utf-8 先命中，
    # BOM 以 `\ufeff` 留在文本里 ⇒ `parse_frontmatter` 首行变成 `\ufeff---`、`_OPEN_RE` 失配 ⇒
    # **静默回退**（present=False / warnings=[]）。在此**文本腿局部剥至多一个前导 BOM**
    # （口径与 `_parse_code` 的「只剥一个 · 仅前导」一致）。
    # ⛔ 不改 `_decode` 本体：那会同时改 `_parse_code`（它已自剥一次 ⇒ 双剥）与 `clean_status.py`
    # 的「至多剥一个」口径（`test_bug108_bom_content_stale.py` 四腿含**两个 BOM** 边界）。
    if text.startswith("\ufeff"):
        text = text[1:]
    if not text.strip():
        raise DocumentParseError(EMPTY_HINT)
    return text, []


def parse_path(path: str | Path) -> str:
    """按路径解析本地文件。"""
    p = Path(path)
    if not p.exists():
        raise DocumentParseError(f"文件不存在：{p}")
    return parse_document(p.name, p.read_bytes())
