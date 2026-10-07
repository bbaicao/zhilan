"""条目 2.1 / 2.2 回归用例：PDF 智能分流（pypdf 快提 → 密度判定 → MinerU OCR）。

纪律（统筹 §3.4）：全部 stub 化 MinerU——注入假 `mineru` / `mineru.parser` 模块或
monkeypatch `mineru.parse_pdf`，**不连真模型、不下载权重、不联网**。

🔴 `回归修复`（2026-09-27）口径补充：分流在密度腿之外新增**文本层覆盖腿**
（每页有效字符 ≥ `parsing._MIN_TEXT_PER_PAGE` ⇒ 原生、密度不作数）。密度阈值语义
（`>=` 边界 / 阈值 > 1 强制 OCR）只对**凑不出文本层**的 PDF 生效——相关用例已按
此适配（`test_threshold_above_one_forces_ocr_on_every_pdf` / `test_threshold_boundary_is_inclusive`），
090 自身的载体见 `test_bug090_density_misjudgement_probe.py` 与
`test_bug090_native_pdf_text_layer.py`。

覆盖点（对应阶段 2 文档条目 2.4 的 6 条）：
① `MINERU_ENABLE=False` 时 `available()` 未开启且不 import 重依赖；
② 原生 PDF（密度 ≥ 阈值）不调用 MinerU，直接用 pypdf 结果；
③ 扫描件 PDF（密度 < 阈值）调用 MinerU 并返回其结果；
③′ 混排 PDF（正文页 + 扫描页）：密度达标但缺页仍逐页补齐，不静默丢内容（回归修复）——
   含「OCR 返回空串时页槽位不漂移」「缺页过多不补齐」「抽页失败退回」「默认档零探测」
   「`_subset_pdf` 页序与非法字节」一共 8 条；
④ MinerU 抛 `DocumentParseError` 时回落 pypdf，不炸穿；
⑤ MinerU 抛裸异常（RuntimeError）时被转译并回落，不炸穿；
⑥ `OCR_SCAN_THRESHOLD=0` 边界：所有 PDF 都走 MinerU。
外加：`_text_density` 单测、密度阈值边界、真实 `mineru.parser.parse` 出口的 stub 全链路、
以及「MinerU 已装但版本 API 不匹配」的明确报错（不静默、不炸穿）。
"""

import logging
import sys
import types
from io import BytesIO

import pytest
from pypdf import PdfReader

from app.core.config import settings
from app.core.errors import DocumentParseError
from app.pipeline import chunking, mineru, parsing
from tests.pipeline.conftest import make_empty_pdf, make_pdf, make_pdf_multi


@pytest.fixture(autouse=True)
def _clean_heavy_modules():
    """每个用例前后清掉注入的假重依赖模块**与阶段 4 的后端探测缓存**，避免用例互相污染。

    阶段 4（条目 4.2）补充：`_select_backend()` / `_vlm_ready()` 的结果是**模块级缓存**
    （缓存是硬需求——`available()` 在扫描件 PDF 的每页热路径上，不缓存就等于每个扫描件跑一次
    nvidia-smi 子进程）。它按**配置指纹**失效，而用例 monkeypatch 掉探测函数时指纹不变，
    故必须显式重置（独立普查点名的污染面）。
    """

    def _clean():
        for name in ("mineru", "mineru.parser", "magic_pdf"):
            sys.modules.pop(name, None)
        mineru._reset_backend_cache()

    _clean()
    yield
    _clean()


def _enable_mineru(monkeypatch, *, threshold: float | None = None):
    """打开总开关（可按需改密度阈值），返回恢复用的 monkeypatch 上下文。"""
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    if threshold is not None:
        monkeypatch.setattr(settings, "OCR_SCAN_THRESHOLD", threshold)


def _stub_parse_pdf(monkeypatch, *, result=None, exc: Exception | None = None) -> list:
    """把 `mineru.parse_pdf` 换成桩（`available()` 也一并放行），记录调用入参。"""
    calls: list[bytes] = []
    monkeypatch.setattr(mineru, "available", lambda: (True, "MinerU 可用"))

    def fake_parse_pdf(data: bytes):
        calls.append(data)
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(mineru, "parse_pdf", fake_parse_pdf)
    return calls


def _stub_runtime_deps(monkeypatch, *, markdown: str, pages: list, seen: dict | None = None):
    """注入最小假 `mineru` / `mineru.parser` 包，让 `available()` 与主入口都认为 MinerU 装好了。

    `pages` 用「块列表」形态喂进去，以校验适配器是按 ``ParseResult.pages`` 取页边界，
    而不是靠 Markdown 里的分页标记。
    """
    if seen is None:
        seen = {}

    class _Block:
        def __init__(self, content):
            self.content = content

    class _Page:
        def __init__(self, page_idx: int, blocks):
            self.page_idx = page_idx
            self.blocks = blocks

    class _Result:
        def __init__(self):
            self.pages = [_Page(i, [_Block([_Span(chunk)])]) for i, chunk in enumerate(pages)]

        def markdown(self, *, add_markers: bool = False) -> str:
            if add_markers:
                seen["add_markers_calls"] = seen.get("add_markers_calls", 0) + 1
                return seen.get("marked_markdown") or markdown
            return markdown

    class _Span:
        def __init__(self, text: str):
            self.text = text

    def _parse(path, **kwargs):
        seen["path"] = path
        seen["suffix"] = path.suffix
        seen["bytes"] = path.read_bytes()
        seen["exists_during_call"] = path.exists()
        seen["tier"] = kwargs.get("tier")
        return _Result()

    pkg = types.ModuleType("mineru")
    pkg.__path__ = []
    sub = types.ModuleType("mineru.parser")
    sub.parse = _parse
    pkg.parser = sub
    monkeypatch.setitem(sys.modules, "mineru", pkg)
    monkeypatch.setitem(sys.modules, "mineru.parser", sub)
    return seen


# ---------------------------------------------------------------------------
# ① 总开关关：不 import 重依赖
# ---------------------------------------------------------------------------
def test_disabled_does_not_import_heavy_deps(monkeypatch):
    monkeypatch.setattr(settings, "MINERU_ENABLE", False)
    ok, reason = mineru.available()
    assert ok is False
    assert "未开启" in reason
    assert "mineru" not in sys.modules
    assert "magic_pdf" not in sys.modules


# ---------------------------------------------------------------------------
# ② 原生 PDF：密度达标，绝不调用 MinerU
# ---------------------------------------------------------------------------
def test_native_pdf_skips_mineru(pdf_with_text, monkeypatch):
    calls = _stub_parse_pdf(monkeypatch, result=("不该被用到", ["不该被用到"]))
    _enable_mineru(monkeypatch)

    text, pages = parsing.parse_document_pages("native.pdf", pdf_with_text)

    assert calls == [], "密度达标的原生 PDF 不应调用 MinerU（本条是阶段 2 的核心收益）"
    assert "Zhilan" in text
    assert pages == [text]


def test_native_pdf_text_identical_with_and_without_mineru(pdf_with_text, monkeypatch):
    """密度判定对原生 PDF 是「跳过 OCR」，产出与关着开关时逐字相同（不变量 5）。"""
    off_text, off_pages = parsing.parse_document_pages("native.pdf", pdf_with_text)
    _stub_parse_pdf(monkeypatch, result=("不该被用到", ["不该被用到"]))
    _enable_mineru(monkeypatch)
    on_text, on_pages = parsing.parse_document_pages("native.pdf", pdf_with_text)
    assert (on_text, on_pages) == (off_text, off_pages)


# ---------------------------------------------------------------------------
# ③ 扫描件：密度不足 → 调用 MinerU 并返回其结果
# ---------------------------------------------------------------------------
def test_scanned_pdf_uses_mineru_result(scanned_pdf, monkeypatch):
    calls = _stub_parse_pdf(monkeypatch, result=("OCR 出来的正文", ["OCR 出来的正文"]))
    _enable_mineru(monkeypatch)

    text, pages = parsing.parse_document_pages("scan.pdf", scanned_pdf)

    assert calls == [scanned_pdf], "扫描件应把原始字节交给 MinerU"
    assert text == "OCR 出来的正文"
    assert pages == ["OCR 出来的正文"]


def test_scanned_pdf_without_mineru_still_reports_hint(scanned_pdf, monkeypatch):
    """不变量 1：MINERU_ENABLE=False 时扫描件行为与升级前完全一致（报 SCANNED_PDF_HINT）。"""
    monkeypatch.setattr(settings, "MINERU_ENABLE", False)
    with pytest.raises(DocumentParseError) as excinfo:
        parsing.parse_document_pages("scan.pdf", scanned_pdf)
    assert str(excinfo.value) == parsing.SCANNED_PDF_HINT


def test_scanned_pdf_with_threshold_zero_still_reports_hint(scanned_pdf, monkeypatch):
    """阈值 0（最宽松档）也不能把「一个字都没提出来」当成原生成功。

    这条钉的是密度分支的 `pdf_text and ...` 短路：阈值 ≤ 0 时密度判定恒真，
    若去掉 `pdf_text and`，空文本就会被当成成功返回，扫描件**静默产出空文档**。
    （由变异测试 M3 暴露的覆盖盲区补上。）
    """
    monkeypatch.setattr(settings, "MINERU_ENABLE", False)
    monkeypatch.setattr(settings, "OCR_SCAN_THRESHOLD", 0.0)
    with pytest.raises(DocumentParseError) as excinfo:
        parsing.parse_document_pages("scan.pdf", scanned_pdf)
    assert str(excinfo.value) == parsing.SCANNED_PDF_HINT


def test_scanned_pdf_with_negative_threshold_still_reports_hint(scanned_pdf, monkeypatch):
    """负阈值同样不得把空文本当成功（同一短路的另一个边界值）。"""
    monkeypatch.setattr(settings, "MINERU_ENABLE", False)
    monkeypatch.setattr(settings, "OCR_SCAN_THRESHOLD", -1.0)
    with pytest.raises(DocumentParseError) as excinfo:
        parsing.parse_document_pages("scan.pdf", scanned_pdf)
    assert str(excinfo.value) == parsing.SCANNED_PDF_HINT


# ---------------------------------------------------------------------------
# ③′ 混排 PDF（正文页 + 扫描页）：密度达标也要逐页补齐（缺陷 回归修复）
# ---------------------------------------------------------------------------
# 混排样本：第 1 页有文本层，第 2 / 3 页是纯图像页（无文本层）。第 1 页文字够长，
# 保证**整篇**密度远高于默认阈值——这正是缺陷的前提条件（`density=0.6483 ≥ 0.01`）。
MIXED_FIRST_PAGE = ("Native first page body. " * 12).strip()


def _mixed_pdf() -> bytes:
    return make_pdf_multi([MIXED_FIRST_PAGE, "", ""])


def test_mixed_pdf_fills_image_only_pages_with_ocr(monkeypatch):
    """密度达标 ≠ 每一页都有文本层：缺文本的页必须补 OCR，且按页序贴回原位。

    缺陷原文（回归修复）：3 页 PDF（1 页 2000 字 + 2 页无文本层）→
    ``density=0.6483 ≥ 0.01`` → ``MINERU_ENABLE=True`` 时 ``mineru.parse_pdf`` 调用次数 **0**
    → 两页内容**静默丢失、全程无日志**。本用例钉住「补齐 + 页序 + 不整篇重解析」三件事。
    """
    calls = _stub_parse_pdf(
        monkeypatch,
        # 故意带首尾空白：写回时必须 strip（对抗性复核 F4 —— 去掉 .strip() 会没有任何用例变红）
        result=("  OCR 第二页正文  \n\n   OCR 第三页正文 ", ["  OCR 第二页正文  ", "   OCR 第三页正文  "]),
    )
    _enable_mineru(monkeypatch)

    text, pages = parsing.parse_document_pages("mixed.pdf", _mixed_pdf())

    assert len(calls) == 1, "混排 PDF 应补一次 OCR（原先一次都不调 → 内容丢失）"
    assert pages == [MIXED_FIRST_PAGE, "OCR 第二页正文", "OCR 第三页正文"], "识别结果必须按页序贴回原位"
    assert text == "\n\n".join(pages)
    assert all(page in text for page in pages), "出口不变量：pages ⊂ text"
    # 只把缺文本的页抽给 OCR，原生的第 1 页不重解析（整篇重跑在 CPU 上可能数分钟）
    subset = calls[0]
    assert subset.startswith(b"%PDF")
    assert subset != _mixed_pdf(), "交给 OCR 的应是子 PDF，不是整篇"
    assert len(PdfReader(BytesIO(subset)).pages) == 2, "子 PDF 只含 2 个缺文本页"


def test_mixed_pdf_blank_ocr_page_keeps_later_page_numbers(monkeypatch):
    """某页 OCR 真的返回空串（空白页）时，**页槽位不许漂移**（对抗性复核 F1）。

    丢掉那一格会让其后所有页文本整体前移一位，而 ``chunking._assign_page`` 返回的是「第几个页文本
    命中探针」的**下标** ⇒ 第 3 页的正文会被标成第 2 页（静默错标、无日志）。这里钉住两件事：
    空白页照样占住第 2 格、第 3 页的正文仍然标 ``page=3``（修复前会标成 2）。
    """
    third = ("THIRDPAGE " + "epsilon zeta eta theta " * 8).strip()
    _stub_parse_pdf(monkeypatch, result=(f"\n\n{third}", ["", third]))
    _enable_mineru(monkeypatch)

    text, pages = parsing.parse_document_pages("mixed.pdf", _mixed_pdf())

    assert pages == [MIXED_FIRST_PAGE, "", third], "空白页必须占住第 2 格，否则第 3 页整体前移"
    assert text == "\n\n".join([MIXED_FIRST_PAGE, third]), "全文仍只由文本页拼回（空页不产生空段落）"
    chunks = chunking.chunk_document(text, pages=pages, size=40, overlap=0)
    assert any("THIRDPAGE" in c.text for c in chunks)
    assert 3 in [c.page for c in chunks], "第 3 页的块必须标 page=3"
    assert 2 not in [c.page for c in chunks], "第 2 页是空白页，任何块都不该标到它（修复前第 3 页会错标成 2）"


def test_mixed_pdf_skips_fill_when_too_many_pages_missing(monkeypatch, caplog):
    """缺页规模超过上限就不补齐（对抗性复核 F2）：请求路径里不许跑无界同步 OCR。"""
    calls = _stub_parse_pdf(monkeypatch, result=("x", ["不该被用到", "不该被用到"]))
    _enable_mineru(monkeypatch)
    monkeypatch.setattr(parsing, "_FILL_MAX_PAGES", 1)  # 样本有 2 个缺页

    with caplog.at_level(logging.WARNING, logger="app.pipeline.parsing"):
        text, pages = parsing.parse_document_pages("mixed.pdf", _mixed_pdf())

    assert calls == [], "超上限时连 OCR 都不该调用"
    # D1 起：退回 pypdf 文本，但**页槽位与原始页序对齐**（样本 3 页 ⇒ 2 个空位）。
    # 覆盖意图不变（护栏生效 + 用 pypdf 文本），只是「出口页列表」的口径按 D1 更新。
    assert (text, pages) == (MIXED_FIRST_PAGE, [MIXED_FIRST_PAGE, "", ""]), "退回 pypdf 文本，页槽位不漂移"
    assert "不补齐" in caplog.text


def test_mixed_pdf_skips_fill_when_subset_pdf_too_large(monkeypatch, caplog):
    """子 PDF 体积超上限同样不补齐（同一护栏的另一个维度：内存驻留）。"""
    calls = _stub_parse_pdf(monkeypatch, result=("x", ["不该被用到", "不该被用到"]))
    _enable_mineru(monkeypatch)
    monkeypatch.setattr(parsing, "_FILL_MAX_SUBSET_BYTES", 10)  # 任何真 PDF 都超它

    with caplog.at_level(logging.WARNING, logger="app.pipeline.parsing"):
        text, pages = parsing.parse_document_pages("mixed.pdf", _mixed_pdf())

    assert calls == []
    assert (text, pages) == (MIXED_FIRST_PAGE, [MIXED_FIRST_PAGE, "", ""])  # D1：页槽位不漂移
    assert "不补齐" in caplog.text


def test_mixed_pdf_ocr_page_count_mismatch_keeps_pypdf_result(monkeypatch, caplog):
    """OCR 返回的形状不可用（条数对不上 / 页文本不是字符串）时**放弃补齐**：宁可少补，也不给出会错位的页边界。

    ``chunking._assign_page`` 返回的是「第几个页文本命中探针」的下标，页错位是**静默错标**
    （不是 ``None``、也没有日志），比少补更危险；非字符串项还会让 ``strip()`` 在补齐路径上抛异常
    ——那是「回落分支自己炸穿 I5」的最坏形状。
    """
    # ① 条数对不上：2 个缺页只认出 1 页
    _stub_parse_pdf(monkeypatch, result=("只认出一页", ["只认出一页"]))
    _enable_mineru(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="app.pipeline.parsing"):
        text, pages = parsing.parse_document_pages("mixed.pdf", _mixed_pdf())

    assert pages == [MIXED_FIRST_PAGE, "", ""], "条数对不上 → 原样返回 pypdf 文本，页槽位不漂移（D1）"
    assert "只认出一页" not in text
    assert "放弃补齐" in caplog.text, "放弃补齐必须留下日志（缺陷原状是「无任何日志」）"

    # ② 条数对得上但页文本不是字符串：不得让补齐分支抛异常（I5 绝不炸穿）
    caplog.clear()
    _stub_parse_pdf(monkeypatch, result=("x", [None, object()]))
    _enable_mineru(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="app.pipeline.parsing"):
        text, pages = parsing.parse_document_pages("mixed.pdf", _mixed_pdf())
    assert pages == [MIXED_FIRST_PAGE, "", ""]
    assert "放弃补齐" in caplog.text


def test_mixed_pdf_ocr_failure_keeps_pypdf_result(monkeypatch):
    """补齐失败不炸穿（I5）：MinerU 抛错时原样返回 pypdf 结果，与修复前逐字相同。"""
    calls = _stub_parse_pdf(monkeypatch, exc=DocumentParseError("MinerU 解析 PDF 失败：模型没下"))
    _enable_mineru(monkeypatch)

    text, pages = parsing.parse_document_pages("mixed.pdf", _mixed_pdf())

    assert len(calls) == 1, "应尝试过补齐"
    assert (text, pages) == (MIXED_FIRST_PAGE, [MIXED_FIRST_PAGE, "", ""])  # D1：页槽位不漂移


def test_mixed_pdf_with_mineru_disabled_keeps_pypdf_result(monkeypatch):
    """默认档（``MINERU_ENABLE=False``）：行为与修复前逐字相同，连 ``available()`` 都不问。

    默认档是「一律 pypdf、绝不 import 重依赖」，逐页补齐不得把它变成一次可选依赖探测。
    """
    monkeypatch.setattr(settings, "MINERU_ENABLE", False)

    def _boom():  # pragma: no cover —— 被调用即说明默认档多探测了一次
        raise AssertionError("默认档不应对 MinerU 做任何探测")

    monkeypatch.setattr(mineru, "available", _boom)
    assert parsing.parse_document_pages("mixed.pdf", _mixed_pdf()) == (
        MIXED_FIRST_PAGE,
        [MIXED_FIRST_PAGE, "", ""],  # D1：默认档同样保留页槽位
    )


def test_pdf_with_blank_page_keeps_later_page_numbers_on_native_route(monkeypatch):
    """**D1**：原生档下「无文本层页」必须占位——否则**其后页号整体前移一位**。

    缺陷原状（真机复现）：PDF 里只要有**一页没有文本层**（扫描页 / 纯图页 / 真空白页），
    ``pages`` 丢掉它 ⇒ ``chunking._assign_page`` 用 ``enumerate(pages, start=1)`` 取下标
    ⇒ 原文第 3 页的内文被标成第 2 页（**静默错标**：不是 ``None``、也没有日志）。
    页码直接进 ``chunk.page``，就是引用溯源显示的页号。

    样本刻意造成「第 1 页有文本 / 第 2 页无文本层 / 第 3 页有文本」：这样「前移」**可被检出**。
    （若缺页之后没有别的文本页，前移与不前移的观测结果相同 ⇒ 该样本测不出这条。）
    """
    monkeypatch.setattr(settings, "MINERU_ENABLE", False)
    third = ("THIRDPAGE " + "epsilon zeta eta theta " * 8).strip()
    sample = make_pdf_multi([MIXED_FIRST_PAGE, "", third])

    text, pages = parsing.parse_document_pages("mixed.pdf", sample)

    assert pages == [MIXED_FIRST_PAGE, "", third], "空页必须占住第 2 格，否则第 3 页整体前移"
    assert text == f"{MIXED_FIRST_PAGE}\n\n{third}", "全文仍只由文本页拼回（空页不产生空段落）"

    chunks = chunking.chunk_document(text, pages=pages, size=40, overlap=0)
    seen = {c.page for c in chunks if c.page is not None}
    assert 3 in seen, "第 3 页的块必须标 page=3（修复前会标成 2）"
    assert 2 not in seen, "第 2 页无文本层，任何块都不该标到它"


def test_no_missing_pages_never_probes_mineru(pdf_with_text, monkeypatch):
    """没有缺页的原生 PDF：连 ``available()`` 都不问——逐页补齐不许给主链加任何开销。"""
    _enable_mineru(monkeypatch)

    def _boom():  # pragma: no cover —— 被调用即说明白白探测了一次
        raise AssertionError("没有缺页时不应探测 MinerU")

    monkeypatch.setattr(mineru, "available", _boom)
    text, pages = parsing.parse_document_pages("native.pdf", pdf_with_text)
    assert "Zhilan" in text and pages == [text]


def test_subset_pdf_keeps_requested_pages_in_order():
    """子 PDF 只含指定页、且页序保持——逐页补齐的底：页序错了就必然错标页码。

    页序正确只是必要条件；真实扫描件里「页内容（图像 XObject）被一并带过去」由 pypdf 的
    `writer.add_page` 完成，本机没有真 MinerU 可端到端验证，故这里钉住可验证的那一半。
    """
    subset = parsing._subset_pdf(make_pdf_multi(["AAA", "BBB", "CCC"]), [0, 2])
    assert subset is not None and subset.startswith(b"%PDF")
    assert [page.extract_text() for page in PdfReader(BytesIO(subset)).pages] == ["AAA", "CCC"]


def test_subset_pdf_returns_none_on_unparsable_bytes():
    """抽页失败只返回 ``None``（调用方退回 pypdf 结果），不抛异常、不炸穿。"""
    assert parsing._subset_pdf(b"not a pdf at all", [0]) is None


def test_mixed_pdf_filled_page_gets_correct_page_number(monkeypatch):
    """端到端收益（缺陷的用户可见面）：补齐后第 3 页内容能切出块、且标到 ``page=3``。

    ``_assign_page`` 用「块首 40 字落在第几个页文本里」定位，故跨页块会诚实给 ``None``；
    这里让两页 OCR 正文各自长于块长，保证第 3 页内部能起点成块。
    """
    ocr_page2 = "SECONDPAGE " + "alpha beta gamma delta " * 8
    ocr_page3 = "THIRDPAGE " + "epsilon zeta eta theta " * 8
    _stub_parse_pdf(monkeypatch, result=(f"{ocr_page2}\n\n{ocr_page3}", [ocr_page2, ocr_page3]))
    _enable_mineru(monkeypatch)

    text, pages = parsing.parse_document_pages("mixed.pdf", _mixed_pdf())
    chunks = chunking.chunk_document(text, pages=pages, size=40, overlap=0)

    assert any("THIRDPAGE" in c.text for c in chunks), "第 3 页的 OCR 正文必须能切出块"
    assert 3 in [c.page for c in chunks], "第 3 页的块必须标 page=3（修复前该页内容根本不存在）"


# ---------------------------------------------------------------------------
# ④⑤ 失败回落：DocumentParseError / 裸异常都不炸穿
# ---------------------------------------------------------------------------
def test_mineru_document_parse_error_falls_back(scanned_pdf, monkeypatch):
    calls = _stub_parse_pdf(monkeypatch, exc=DocumentParseError("MinerU 解析 PDF 失败：模型没下"))
    _enable_mineru(monkeypatch)

    with pytest.raises(DocumentParseError) as excinfo:
        parsing.parse_document_pages("scan.pdf", scanned_pdf)

    assert len(calls) == 1, "应尝试过 MinerU"
    assert str(excinfo.value) == parsing.SCANNED_PDF_HINT, "回落 pypdf 后仍无文本 → 报扫描件提示"


def test_mineru_raw_exception_is_translated_and_falls_back(pdf_two_pages, monkeypatch):
    """裸异常被 `parse_pdf` 转译成中文 DocumentParseError，且回落 pypdf 拿到原文本。"""
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    monkeypatch.setattr(settings, "OCR_SCAN_THRESHOLD", 1.0)  # 强制判定为扫描件
    monkeypatch.setattr(mineru, "available", lambda: (True, "MinerU 可用"))

    def boom(_data):
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(mineru, "parse_pdf", boom)

    text, pages = parsing.parse_document_pages("two.pdf", pdf_two_pages)

    assert "HEADERUNIQUE" in text, "MinerU 炸了要回落 pypdf，不能炸穿调用链"
    assert len(pages) == 2


def test_unavailable_mineru_is_not_called_at_all(scanned_pdf, monkeypatch):
    """开关开着但 `available()` 为 False 时，**不得调用** `parse_pdf`，直接回落报提示。

    这条钉的是「调用前先探测可用性」这一步：去掉探测后，未安装 MinerU 的环境会去做一次
    注定失败的调用（多一次重试与一段误导日志）。（由变异测试 M15 暴露的覆盖盲区补上。）

    ⚠️ **回归修复（2026-09-22）**：这一格的**报错文案是有意改的** —— 开关开着而
    MinerU 不可用时，因果在模型包/解析器，不能再拿「请上传文字版」把用户引去换文件。
    旧断言 ``== SCANNED_PDF_HINT`` 随之作废；三条逐字回归钉见
    ``tests/pipeline/test_parsing_no_model_pack_056.py``。
    """
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    monkeypatch.setattr(settings, "OCR_SCAN_THRESHOLD", 1.0)
    # 刻意**不** stub available()：本机没装 mineru，它真实返回 False
    ok, reason = mineru.available()
    assert ok is False
    calls: list[bytes] = []
    monkeypatch.setattr(mineru, "parse_pdf", lambda d: calls.append(d) or ("不该被调用", ["不该被调用"]))

    with pytest.raises(DocumentParseError) as excinfo:
        parsing.parse_document_pages("scan.pdf", scanned_pdf)

    assert calls == [], "available() 为 False 时不该调用 parse_pdf"
    assert str(excinfo.value) == parsing.NO_MODEL_PACK_HINT.format(reason=reason)


def test_raw_exception_from_parse_pdf_is_translated(monkeypatch):
    """`parse_pdf` 自身把裸异常转成中文 DocumentParseError（不变量 3）。"""
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    _stub_runtime_deps(monkeypatch, markdown="x", pages=["x"])

    def boom(_path, **_kwargs):
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(sys.modules["mineru.parser"], "parse", boom)

    with pytest.raises(DocumentParseError) as excinfo:
        mineru.parse_pdf(b"%PDF-1.4 fake")
    assert "CUDA out of memory" in str(excinfo.value)
    assert "失败" in str(excinfo.value)


# ---------------------------------------------------------------------------
# ⑥ 阈值边界
# ---------------------------------------------------------------------------
def test_threshold_zero_keeps_everything_native(pdf_with_text, monkeypatch):
    """阈值 0 的边界语义：判定式是 `密度 >= 阈值`，故 0 是**最宽松**档——有字就不 OCR。

    这正是阶段 2 文档条目 2.4 用例⑥「阈值 0 时所有 PDF 都走 MinerU」的**反例**：
    该条与同文档条目 2.2 给的 `density >= OCR_SCAN_THRESHOLD` 判定式自相矛盾（密度恒 ≥ 0，
    故永远走原生）。本实现以判定式为准，并把这条边界用测试钉死，偏差见《S2-验收报告》。
    """
    calls = _stub_parse_pdf(monkeypatch, result=("不该被用到", ["不该被用到"]))
    _enable_mineru(monkeypatch, threshold=0.0)

    text, _pages = parsing.parse_document_pages("native.pdf", pdf_with_text)

    assert calls == [], "阈值 0（密度 >= 0 恒真）时原生 PDF 仍走 pypdf，不调 MinerU"
    assert "Zhilan" in text


def test_threshold_above_one_forces_ocr_on_every_pdf(pdf_with_text, monkeypatch):
    """调高阈值超过 1 才是「所有 PDF 都走 OCR」的开关（密度上界约 1 字符/字节）。

    🔴 `回归修复` 语义适配（2026-09-27）：分流新增**文本层覆盖腿**——每页有效字符
    ≥ ``parsing._MIN_TEXT_PER_PAGE`` 的 PDF 走原生、密度不作数 ⇒ 「强制 OCR」只对
    **凑不出文本层**的 PDF（盖章页/空页）成立。故本用例改用盖章页样本验证强制语义，
    并补一条「真文本层 PDF 在阈值 > 1 下仍原生」的 090 例外断言（原样本 `pdf_with_text`
    在新语义下就该走原生——那正是 090 修的形态）。
    """
    calls = _stub_parse_pdf(monkeypatch, result=("强制 OCR", ["强制 OCR"]))
    _enable_mineru(monkeypatch, threshold=1.01)

    stamped = make_pdf_multi(["1", "2", "3"])  # 每页 1 字符，凑不出文本层
    text, _pages = parsing.parse_document_pages("stamped.pdf", stamped)

    assert len(calls) == 1, "阈值 > 1 且无文本层时仍应交给 MinerU"
    assert text == "强制 OCR"

    calls.clear()
    text, _pages = parsing.parse_document_pages("native.pdf", pdf_with_text)

    assert calls == [], "阈值 > 1 但每页都有真实文本层 ⇒ 090 起仍走原生（密度不作数）"
    assert "Zhilan" in text


def test_threshold_boundary_is_inclusive(pdf_with_text, monkeypatch):
    """密度 == 阈值 时判为原生（`>=` 语义），差一点点则判为扫描件。

    🔴 `回归修复` 语义适配（2026-09-27）：边界语义属**密度腿**，而 `pdf_with_text`
    每页都有真文本层、在新语义下不再由密度裁决 ⇒ 本用例改用**盖章页样本**（凑不出
    文本层、只能靠密度腿裁决的形态）验证 `>=` 边界——被钉的语义一字未变。
    """
    stamped = make_pdf_multi(["1", "2", "3"])
    pages = parsing._pdf_pages(stamped)
    density = parsing._text_density(pages, stamped)

    calls = _stub_parse_pdf(monkeypatch, result=("OCR", ["OCR"]))
    _enable_mineru(monkeypatch, threshold=density)
    parsing.parse_document_pages("stamped.pdf", stamped)
    assert calls == []

    _enable_mineru(monkeypatch, threshold=density * 1.01)
    parsing.parse_document_pages("stamped.pdf", stamped)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 密度函数单测（条目 2.4 规格 3）
# ---------------------------------------------------------------------------
def test_text_density_math():
    assert parsing._text_density([], b"x" * 100) == 0.0
    assert parsing._text_density(["abcd"], b"x" * 100) == pytest.approx(0.04)
    # 多页按空行拼接后再计长度：4 + 2 + 4 = 10
    assert parsing._text_density(["abcd", "efgh"], b"x" * 100) == pytest.approx(0.10)


def test_text_density_never_divides_by_zero():
    assert parsing._text_density(["abc"], b"") == 3.0


def test_text_density_of_real_fixtures(pdf_with_text, scanned_pdf):
    native = parsing._text_density(parsing._pdf_pages(pdf_with_text), pdf_with_text)
    scanned = parsing._text_density(parsing._pdf_pages(scanned_pdf), scanned_pdf)
    assert native > settings.OCR_SCAN_THRESHOLD, "原生样本密度必须高于默认阈值"
    assert scanned == 0.0, "无文本层样本密度必须是 0"


# ---------------------------------------------------------------------------
# 真实出口 stub：mineru.parser.parse（MinerU 4.0 唯一高层入口，只收路径）
# ---------------------------------------------------------------------------
def test_parser_facade_is_used_and_receives_temp_pdf(monkeypatch):
    _enable_mineru(monkeypatch)
    seen = _stub_runtime_deps(monkeypatch, markdown="全文", pages=["块页一", "块页二"])
    seen["marked_markdown"] = "标记页一\n\n---\n\n标记页二"

    text, pages = mineru.parse_pdf(b"%PDF-1.4 payload")

    assert seen["suffix"] == ".pdf", "高层入口只收路径，适配器必须落一个 .pdf 临时文件"
    assert seen["bytes"] == b"%PDF-1.4 payload", "临时文件内容必须是原始字节"
    assert seen["exists_during_call"] is True
    assert seen["add_markers_calls"] >= 1, "应请求带页边界标记的渲染（F2：pages 需为 text 的字面切片）"
    assert text == "标记页一\n\n---\n\n标记页二", "带标记的渲染结果是权威全文"
    assert pages == ["标记页一", "标记页二"], "页边界从带标记的全文切出"
    assert all(page in text for page in pages), "每页必须是全文的字面子串"
    assert not seen["path"].exists(), "临时文件应在调用后清理"


def test_pages_fall_back_to_markdown_markers_when_result_has_no_pages(monkeypatch):
    """旧版 / 变体返回不带 pages 时，才回落到按分页标记切（2.x 与 FULL 渲染模式）。"""
    _enable_mineru(monkeypatch)
    _stub_runtime_deps(
        monkeypatch,
        markdown="P1 正文\n\n---\n\nP2 正文",
        pages=[],
    )

    text, pages = mineru.parse_pdf(b"%PDF-1.4 payload")

    assert pages == ["P1 正文", "P2 正文"]
    assert text == "P1 正文\n\n---\n\nP2 正文"


def test_pages_from_markdown_pagebreak_marker():
    assert mineru._pages_from_markdown("A\n\n<!-- PageBreak -->\n\nB") == ["A", "B"]
    assert mineru._pages_from_markdown("整篇没有标记") == ["整篇没有标记"]
    assert mineru._pages_from_markdown("") == []


def test_backend_to_tier_mapping(monkeypatch):
    """`MINERU_BACKEND` → MinerU tier 的映射契约（两套命名不是一对一，故必须钉死）。

    **2026-09-18 阶段 4 改口径（登记偏差 `P-登记-回归检查`）**：原契约是「本仓库三档一律映射
    ``standard``」。阶段 4 实测推翻了它的前提——MinerU 4.0 里 ``standard`` 映射到 effort ``high``，
    而 ``effort ∈ {high, xhigh}`` 才会**构造 VLM 预测器**（``mineru/backend/analysis/pdf/pipeline.py``），
    即 ``standard`` 对 VLM/GGUF 是**无条件必需**（T1 证据：主树 `S6-回归检查-only判定.md`）；
    ``basic``（medium）则完全不碰 GGUF。沿用「一律 standard」会让阶段 4 §0.3 定案的交付形态
    （只带 818MB ONNX 件、VLM 由用户自备）**连扫描件 OCR 都跑不动**。新契约：

    - 本仓库三档（``auto`` / ``pipeline`` / ``vlm``，含空串与未知值）→ 按 `_select_backend()`
      的**实际路由**分档：``vlm`` 路由 → ``standard``；否则 → ``basic``；
    - 用户直接填 MinerU 的 tier 名（``flash`` / ``basic`` / ``standard`` / ``advanced``）→
      **原样透传**，尊重显式选择（该行为与阶段 2 一致，未改）。

    注意最后几行：**大小写归一的期望值必须与回落值不同**，否则该行是「假覆盖」——
    没有 `.lower()` 时未知值照样回落 `basic`，`("STANDARD","standard")` 这种写法会误报通过。
    """
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", False)  # 默认档 = 未自备 VLM 件
    for value, expected in (
        ("auto", "basic"),  # VLM 关闭 → pipeline 路由 → ONNX-only 档（不依赖 GGUF）
        ("pipeline", "basic"),
        ("vlm", "basic"),  # 开关没开 → 按 pipeline 解析（`_resolve_backend` 会给 warning）
        ("", "basic"),
        ("mInErU-NoT-a-TiEr", "basic"),  # 未知值不炸穿 → 按 pipeline 路由
        ("FLASH", "flash"),  # ← 期望值 ≠ 回落值，才真的在测大小写归一
        ("Advanced", "advanced"),
        ("flash", "flash"),  # 用户直接填 MinerU tier 名 → 原样透传
        ("basic", "basic"),
        ("standard", "standard"),
        ("advanced", "advanced"),
    ):
        monkeypatch.setattr(settings, "MINERU_BACKEND", value)
        assert mineru._mineru_tier() == expected, f"{value!r} 应映射为 {expected!r}"

    # VLM 路由（开关开 + 就绪三件套成立）→ standard。本机是**未自备**场景，故 stub 掉就绪判据；
    # 换掉 `_vlm_ready` 不改配置指纹，故必须显式重置缓存（否则读到 loop 里缓存的旧路由）。
    monkeypatch.setattr(settings, "MINERU_BACKEND", "auto")
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", True)
    monkeypatch.setattr(mineru, "_vlm_ready", lambda: (True, "stub：就绪三件套成立"))
    mineru._reset_backend_cache()
    assert mineru._select_backend() == "vlm"
    assert mineru._mineru_tier() == "standard"


def test_facade_passes_mapped_tier(monkeypatch):
    """高层门面必须把映射后的 tier 传下去（而不是丢掉配置）。"""
    _enable_mineru(monkeypatch)
    monkeypatch.setattr(settings, "MINERU_BACKEND", "advanced")
    seen = _stub_runtime_deps(monkeypatch, markdown="x", pages=["x"])

    mineru.parse_pdf(b"%PDF-1.4 payload")

    assert seen["tier"] == "advanced"


def test_block_pages_use_array_order_not_page_idx():
    """块数据态兜底的页序契约：按 ``ParseResult.pages`` 的**数组顺序**输出，不按 ``page_idx`` 重排。

    被测函数是 `_blocks_pages` —— **活代码**（`_export` 在没有渲染文本时的最后兜底）。
    改名前的 `test_pages_use_array_order_not_page_idx` 测的是已删除的死函数 `_pages_from_result`，
    属于「覆盖了一个没有调用点的函数」的假覆盖（见 `回归修复`）。

    MinerU 的 ``page_idx`` 是「原文档页号」（做页范围解析时可能跳号，如 `[10, 12, 13]`，
    见参考快照 `tests/unittest/test_doclib_api_page_remap.py`），而 pages 数组本身已是
    解析器排好的阅读顺序。若按 `page_idx` 排序，跳号场景下页序仍对，但一旦两者不一致就会
    与 MinerU 自身的阅读顺序打架；故钉死「数组顺序即权威」。
    """

    class _Block:
        def __init__(self, content):
            self.content = content

    class _Page:
        def __init__(self, page_idx, content):
            self.page_idx = page_idx
            self.blocks = [_Block([types.SimpleNamespace(text=content)])]

    class _Result:
        # 数组顺序故意与 page_idx 顺序相反：数组 = [idx 5 的内容, idx 1 的内容]
        pages = [_Page(5, "先读到的这一页"), _Page(1, "后读到的这一页")]

    assert mineru._blocks_pages(_Result()) == ["先读到的这一页", "后读到的这一页"]


# ---------------------------------------------------------------------------
# 下游契约：pages 必须是 text 的字面切片（独立复核 F2）
# ---------------------------------------------------------------------------
def _result_with_pages(
    markdown_text: str,
    add_markers_text: str | None,
    block_pages: list[str],
    *,
    page_count: int | None = None,
):
    """构造假 ParseResult：``markdown(add_markers=True)`` 与 ``markdown()`` 可给不同文本。

    ``page_count`` 是 ``ParseResult.pages`` 的结构页数（适配器用它做「切出的页数不得超过结构页数」
    的上限自检）。默认取块页数；不传块页数时按带标记文本的 ``---`` 分隔数推断，模拟真实返回值
    「结构页数与渲染页边界一致」。要复现「单页正文里含 ``---``」的误判场景，就显式传 ``page_count=1``。
    """

    class _Span:
        def __init__(self, text):
            self.text = text

    class _Block:
        def __init__(self, content):
            self.content = content

    class _Page:
        def __init__(self, content):
            self.page_idx = 0
            self.blocks = [_Block([_Span(content)])] if content else []

    if page_count is None:
        page_count = len(block_pages)
        if not page_count and add_markers_text:
            page_count = add_markers_text.count("\n\n---\n\n") + 1

    class _Result:
        # 结构页数必须真实：适配器用它做「切出页数 ≤ 结构页数」的上限自检。
        # 有块内容时按内容建页，不足 page_count 的补空页。
        pages = [_Page(p) for p in block_pages] + [_Page("") for _ in range(max(0, page_count - len(block_pages)))]

        def markdown(self, *, add_markers: bool = False) -> str:
            return (add_markers_text or markdown_text) if add_markers else markdown_text

    return _Result()


def test_export_prefers_marked_markdown_so_pages_are_literal_slices():
    """出口不变量：`pages` 每一项都必须是 `text` 的字面子串（下游 `_assign_page` 的前提）。"""
    marked = "第一页正文\n\n---\n\n第二页正文"
    result = _result_with_pages(
        markdown_text="第一页正文\n第二页正文",  # 渲染态（无标记）
        add_markers_text=marked,
        block_pages=["块数据态文本"],  # 刻意与 markdown 不是同一套表示
        page_count=2,
    )

    text, pages = mineru._export(result)

    assert text == marked, "应优先用带标记的渲染结果作为全文"
    assert pages == ["第一页正文", "第二页正文"], "页边界应从带标记的全文切出"
    assert all(page in text for page in pages), "每页必须是全文的字面子串"


def test_export_falls_back_to_block_pages_only_without_markers():
    """拿不到任何标记时会回落；若回落结果是「整篇一页」，则该页仍是全文字面子串。"""
    result = _result_with_pages(
        markdown_text="渲染态全文（没有页标记）",
        add_markers_text=None,
        block_pages=["块一", "块二"],
    )

    text, pages = mineru._export(result)

    assert text == "渲染态全文（没有页标记）"
    assert pages == ["渲染态全文（没有页标记）"], "无标记时整篇当一页（且满足切片不变量）"
    assert all(page in text for page in pages)


def test_block_pages_used_when_markdown_is_empty():
    """Markdown 整篇为空时才真正退回块数据态（此时没有「切片」可言，属已知降级）。"""
    result = _result_with_pages(markdown_text="", add_markers_text=None, block_pages=["块一", "块二"])

    text, pages = mineru._export(result)

    assert pages == ["块一", "块二"]
    assert text == "块一\n\n块二", "缺全文时用页文本拼出全文"


def test_separator_inside_single_page_is_not_treated_as_page_break():
    """单页文档正文里自带的 ``---`` 分隔线不得被当成页边界（否则页码会静默丢失）。"""
    body = "第一段正文内容，比较长一些。\n\n---\n\n第二段正文内容，同样比较长。"
    result = _result_with_pages(markdown_text=body, add_markers_text=body, block_pages=[], page_count=1)

    text, pages = mineru._export(result)

    assert pages == [body], "切出的 2 片 > 结构页数 1 → 判为误判，降级为整篇一页"
    assert all(page in text for page in pages)


def test_chunk_page_assignment_works_with_exported_pages():
    """端到端钉住 F2：用 `_export` 的产物切块，页码必须真被标上（这正是 `pages ⊂ text` 的用处）。

    实测（默认 `RAG_CHUNK_SIZE=800`）：两页各约 630 字 → 2 块，页码分别是 1 与 2。
    """
    from app.pipeline.chunking import chunk_document

    page1 = "第一页第一段正文。" * 70   # 约 630 字
    page2 = "第二页第一段正文。" * 70
    marked = f"{page1}\n\n---\n\n{page2}"
    result = _result_with_pages(markdown_text=marked, add_markers_text=marked, block_pages=[], page_count=2)

    text, pages = mineru._export(result)
    assert pages == [page1, page2], "两页应被正确切出"
    assert all(page in text for page in pages), "每页必须是全文字面子串"

    chunks = chunk_document(text, pages=pages)

    assert len(chunks) >= 2, f"应切出多块，实际={len(chunks)}"
    assigned = [c.page for c in chunks]
    assert assigned == [1, 2], f"页码应按页序被标上，实际={assigned}"


def test_export_joins_pages_when_markdown_empty():
    result = types.SimpleNamespace(pages=[types.SimpleNamespace(blocks=[types.SimpleNamespace(content="只有页文本")])])
    text, pages = mineru._export(result)
    assert (text, pages) == ("只有页文本", ["只有页文本"])


def test_available_widens_to_magic_pdf_only_env(monkeypatch):
    """只装 magic_pdf（2.x 口径）时也算可用——2026-09-17 用户拍板「放宽」。"""
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    monkeypatch.setitem(sys.modules, "magic_pdf", types.ModuleType("magic_pdf"))
    ok, reason = mineru.available()
    assert ok is True
    # 阶段 4 · P-登记-回归检查：措辞断言降级为前缀断言（覆盖意图「放宽到 magic_pdf 也算可用」不变）
    assert reason.startswith("MinerU 可用，backend="), reason


def test_api_mismatch_reports_clearly_without_raw_exception(monkeypatch):
    """装了 mineru 但没有任何已知入口 → 明确报「API 不匹配」，不静默、不抛裸异常。"""
    _enable_mineru(monkeypatch)
    monkeypatch.setitem(sys.modules, "mineru", types.ModuleType("mineru"))  # 无 parser / 无 parse

    with pytest.raises(DocumentParseError) as excinfo:
        mineru.parse_pdf(b"%PDF-1.4 payload")

    message = str(excinfo.value)
    assert "API 不匹配" in message
    # 断言必须落在「实际尝试过的入口清单」上：固定文案里本来就含 "magic_pdf" 这个词，
    # 只断言 `"magic_pdf" in message` 的话，删掉入口④ 也照样通过（变异 M25 暴露的假覆盖）。
    for label in ("mineru.parser.parse", "mineru.parser.MinerUParser", "mineru.parse", "magic_pdf UNIPipe"):
        assert label in message, f"报错清单应包含实际尝试过的入口 {label!r}"


def test_available_reports_not_installed(monkeypatch):
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    ok, reason = mineru.available()
    assert ok is False
    assert "未安装" in reason
    assert "mineru" not in sys.modules


def test_available_wires_env_before_importing(monkeypatch):
    """`available()` 必须**自己**完成 env 接线——它是「import 重依赖前」的唯一落点。

    变异 M02 暴露的盲区：三条 env 用例都是直接调 `_apply_mineru_env()`，钉的是函数体
    而不是**接线**；把 `available()` 里那行调用删掉，测试照样全绿，设备/模型目录当场变哑配置。
    """
    import os

    monkeypatch.delenv("MINERU_DEVICE_MODE", raising=False)
    monkeypatch.delenv("MINERU_HOME", raising=False)
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    monkeypatch.setattr(settings, "MINERU_DEVICE", "cuda")
    monkeypatch.setattr(settings, "MINERU_MODEL_DIR", "/models/mineru")
    monkeypatch.setitem(sys.modules, "mineru", types.ModuleType("mineru"))  # 让 import 能成功

    ok, reason = mineru.available()
    assert ok is True
    # 阶段 4 · P-登记-回归检查：措辞断言降级为前缀断言（本用例的覆盖意图在下面两行 env 接线）
    assert reason.startswith("MinerU 可用，backend=pipeline, device=cuda"), reason
    assert os.environ["MINERU_DEVICE_MODE"] == "cuda", "available() 必须完成设备接线"
    assert os.environ["MINERU_HOME"] == "/models/mineru", "available() 必须完成模型目录接线"


def test_default_settings_do_not_pollute_env(monkeypatch):
    """不 monkeypatch 配置键：钉的正是**默认值本身**（`auto` / 空串）。

    变异 C4/C5 暴露的盲区：只测「显式写成 auto/空」不够——默认值若被改成 `cpu` 或某路径，
    会在用户毫无察觉的情况下把 MinerU 钉死在 CPU 或把权重目录指向不存在的路径。
    """
    import os

    monkeypatch.delenv("MINERU_DEVICE_MODE", raising=False)
    monkeypatch.delenv("MINERU_HOME", raising=False)

    mineru._apply_mineru_env()  # 用 settings 的**默认值**，不做任何 monkeypatch

    assert settings.MINERU_DEVICE == "auto", "默认设备必须仍是 auto（= 让 MinerU 自己探测）"
    assert settings.MINERU_MODEL_DIR == "", "默认模型目录必须仍是空（= 用 MinerU 默认缓存）"
    assert "MINERU_DEVICE_MODE" not in os.environ
    assert "MINERU_HOME" not in os.environ


def test_low_density_pdf_uses_default_threshold(pdf_with_text, monkeypatch):
    """把**默认阈值 0.01 本身**钉死：密度低于它（但非零）的「半扫描件」必须走 OCR。

    变异 C1 暴露的盲区：原有用例只覆盖「原生（密度 ≈0.1）」与「纯扫描（密度 0）」两端，
    中间地带没样本，把默认阈值改成 0 也不会有用例失败。这里用一个页多字少的 PDF
    （密度明显低于默认阈值）来固定默认值的行为含义。
    """
    from tests.pipeline.conftest import make_pdf_multi

    # 夹具只能编码 latin-1；13 页里只有一页有 2 个字符 → 密度远低于默认阈值
    sparse = make_pdf_multi(["hi"] + [""] * 12)
    density = parsing._text_density(parsing._pdf_pages(sparse), sparse)
    assert 0 < density < settings.OCR_SCAN_THRESHOLD, f"样本密度应落在 (0, 默认阈值) 之间，实测={density}"

    calls = _stub_parse_pdf(monkeypatch, result=("OCR", ["OCR"]))
    _enable_mineru(monkeypatch)  # 不动阈值，用默认值

    text, _pages = parsing.parse_document_pages("sparse.pdf", sparse)

    assert len(calls) == 1, "密度低于默认阈值 → 必须交给 MinerU"
    assert text == "OCR"


# ---------------------------------------------------------------------------
# 独立复核 F1：只换「入口不存在」，运行期异常必须忠实冒泡
# ---------------------------------------------------------------------------
def _inject_parser(monkeypatch, parse_callable):
    pkg = types.ModuleType("mineru")
    pkg.__path__ = []
    sub = types.ModuleType("mineru.parser")
    sub.parse = parse_callable
    pkg.parser = sub
    monkeypatch.setitem(sys.modules, "mineru", pkg)
    monkeypatch.setitem(sys.modules, "mineru.parser", sub)


def test_runtime_attribute_error_is_not_disguised_as_version_mismatch(monkeypatch):
    """运行期 AttributeError（MinerU 内部 bug）必须报真因，不能报成「版本 API 不匹配」。"""
    _enable_mineru(monkeypatch)

    def boom(_path, **_kwargs):
        raise AttributeError("'NoneType' object has no attribute 'layout'")

    _inject_parser(monkeypatch, boom)

    with pytest.raises(DocumentParseError) as excinfo:
        mineru.parse_pdf(b"%PDF-1.4 payload")

    message = str(excinfo.value)
    assert "'NoneType' object has no attribute 'layout'" in message
    assert "API 不匹配" not in message, "运行期异常不该被当成入口不存在"


def test_runtime_missing_dependency_is_not_disguised_as_version_mismatch(monkeypatch):
    """运行期 ModuleNotFoundError（懒加载缺可选依赖）同样要报真因。"""
    _enable_mineru(monkeypatch)

    def boom(_path, **_kwargs):
        raise ModuleNotFoundError("No module named 'torch'")

    _inject_parser(monkeypatch, boom)

    with pytest.raises(DocumentParseError) as excinfo:
        mineru.parse_pdf(b"%PDF-1.4 payload")

    message = str(excinfo.value)
    assert "No module named 'torch'" in message
    assert "API 不匹配" not in message


def test_runtime_failure_does_not_cascade_to_other_entries(monkeypatch):
    """运行期异常只应冒泡一次，不得让同一份 PDF 被逐个入口重复解析（F1 的 CPU 浪费面）。"""
    _enable_mineru(monkeypatch)
    calls: list[str] = []

    def factory(label):
        def _fn(_path, **_kwargs):
            calls.append(label)
            raise AttributeError(f"{label} 内部炸了")

        return _fn

    pkg = types.ModuleType("mineru")
    pkg.__path__ = []
    sub = types.ModuleType("mineru.parser")
    sub.parse = factory("facade")
    sub.MinerUParser = lambda: types.SimpleNamespace(parse=factory("instance"))
    pkg.parser = sub
    pkg.parse = factory("high-level")
    monkeypatch.setitem(sys.modules, "mineru", pkg)
    monkeypatch.setitem(sys.modules, "mineru.parser", sub)

    with pytest.raises(DocumentParseError):
        mineru.parse_pdf(b"%PDF-1.4 payload")

    assert calls == ["facade"], f"只应尝试主入口一次，实际={calls}"


def test_import_time_missing_entry_still_cascades(monkeypatch):
    """导入期缺入口仍要逐级回落（保留「多入口退路」这一原始需求）。"""
    _enable_mineru(monkeypatch)
    calls: list[str] = []

    pkg = types.ModuleType("mineru")
    pkg.__path__ = []  # 没有 mineru.parser 子模块 → 前两个入口导入期失败

    def high_level(_data):
        calls.append("high-level")
        return "旧版入口产出"

    pkg.parse = high_level
    monkeypatch.setitem(sys.modules, "mineru", pkg)

    text, pages = mineru.parse_pdf(b"%PDF-1.4 payload")

    assert calls == ["high-level"], "应回落到旧版高层入口"
    assert text == "旧版入口产出"
    assert pages == ["旧版入口产出"]


def test_markdown_add_markers_typeerror_falls_back(monkeypatch):
    """旧版 ``markdown()`` 不认 ``add_markers`` 时不得整条路径失败。"""
    _enable_mineru(monkeypatch)

    class _Span:
        def __init__(self, text):
            self.text = text

    class _Block:
        def __init__(self, content):
            self.content = content

    class _Page:
        page_idx = 0
        blocks = [_Block([_Span("页文本")])]

    class _Result:
        pages = [_Page()]

        def markdown(self) -> str:  # 不接受 add_markers
            return "旧版渲染全文"

    _inject_parser(monkeypatch, lambda _path, **_kwargs: _Result())

    text, pages = mineru.parse_pdf(b"%PDF-1.4 payload")
    assert text == "旧版渲染全文"
    assert pages == ["旧版渲染全文"], "无标记 → 结构页数 1 → 整篇一页（满足切片不变量）"


# ---------------------------------------------------------------------------
# 独立复核 F5：MINERU_DEVICE / MINERU_MODEL_DIR 必须真接线（不是哑配置）
# ---------------------------------------------------------------------------
def test_device_and_model_dir_are_mapped_to_mineru_env(monkeypatch):
    import os

    monkeypatch.delenv("MINERU_DEVICE_MODE", raising=False)
    monkeypatch.delenv("MINERU_HOME", raising=False)
    monkeypatch.setattr(settings, "MINERU_DEVICE", "cuda")
    monkeypatch.setattr(settings, "MINERU_MODEL_DIR", r"D:\models\mineru")

    mineru._apply_mineru_env()

    assert os.environ["MINERU_DEVICE_MODE"] == "cuda"
    assert os.environ["MINERU_HOME"] == r"D:\models\mineru"


def test_auto_device_and_empty_model_dir_do_not_set_env(monkeypatch):
    """默认值（auto / 空）不得污染环境——保持「不改开箱行为」。"""
    import os

    monkeypatch.delenv("MINERU_DEVICE_MODE", raising=False)
    monkeypatch.delenv("MINERU_HOME", raising=False)
    monkeypatch.setattr(settings, "MINERU_DEVICE", "auto")
    monkeypatch.setattr(settings, "MINERU_MODEL_DIR", "")

    mineru._apply_mineru_env()

    assert "MINERU_DEVICE_MODE" not in os.environ
    assert "MINERU_HOME" not in os.environ


def test_existing_env_wins_over_settings(monkeypatch):
    """用户 shell 里已设的环境变量优先于本仓库配置。"""
    import os

    monkeypatch.setenv("MINERU_DEVICE_MODE", "cpu")
    monkeypatch.setenv("MINERU_HOME", r"E:\already-set")
    monkeypatch.setattr(settings, "MINERU_DEVICE", "cuda")
    monkeypatch.setattr(settings, "MINERU_MODEL_DIR", r"D:\models\mineru")

    mineru._apply_mineru_env()

    assert os.environ["MINERU_DEVICE_MODE"] == "cpu"
    assert os.environ["MINERU_HOME"] == r"E:\already-set"


# ---------------------------------------------------------------------------
# 统筹 §4.3 矩阵里的「返回空文本」异常维度
# ---------------------------------------------------------------------------
def test_empty_mineru_result_is_treated_as_failure(monkeypatch):
    """MinerU 正常返回但没识别出文字 → 抛 DocumentParseError，不静默产出空文本。"""
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    _stub_runtime_deps(monkeypatch, markdown="", pages=[])

    with pytest.raises(DocumentParseError) as excinfo:
        mineru.parse_pdf(b"%PDF-1.4 payload")

    assert "空" in str(excinfo.value)
    assert "失败" in str(excinfo.value)


def test_empty_mineru_result_falls_back_to_hint_on_scanned_pdf(scanned_pdf, monkeypatch):
    """端到端：扫描件 + MinerU 返回空 → 回落 pypdf 无文本 → 报 SCANNED_PDF_HINT。"""
    _enable_mineru(monkeypatch)
    _stub_runtime_deps(monkeypatch, markdown="   ", pages=["", "  "])

    with pytest.raises(DocumentParseError) as excinfo:
        parsing.parse_document_pages("scan.pdf", scanned_pdf)

    assert str(excinfo.value) == parsing.SCANNED_PDF_HINT


def test_ingest_chain_sees_scanned_pdf_succeed_with_stub_mineru(scanned_pdf, monkeypatch):
    """组合口径：MINERU_ENABLE=True × 扫描件 × MinerU 正常 → 解析出文字（切块前置条件）。"""
    _stub_parse_pdf(monkeypatch, result=("扫描件 OCR 正文。", ["扫描件 OCR 正文。"]))
    _enable_mineru(monkeypatch)

    assert parsing.parse_document("scan.pdf", scanned_pdf) == "扫描件 OCR 正文。"


def test_empty_pdf_fixture_has_no_text_layer():
    """坐实夹具语义：`make_empty_pdf` 确实是无文本层的扫描件样本。"""
    assert parsing._pdf_pages(make_empty_pdf()) == []
    assert parsing._pdf_pages(make_pdf("text")) == ["text"]


# ---------------------------------------------------------------------------
# 变异测试补充（M20 / M27 / M28）：F1 的语义边界与诊断保真
# ---------------------------------------------------------------------------
def test_import_time_runtime_error_is_not_disguised_as_version_mismatch(monkeypatch):
    """装坏的 mineru（导入期抛 RuntimeError 而非 ImportError）必须报真因。

    变异 M20 暴露的盲区：F1 只钉住了「运行期异常」，没钉「导入期非 ImportError」——
    把 `_import_or_unavailable` 的 `except ImportError` 放宽成 `except Exception`，
    真因又会被归一成误导性的「版本 API 不匹配」。
    """
    _enable_mineru(monkeypatch)

    class _BrokenFinder:
        """模拟「装了但装坏了」的重依赖：导入期抛非 ImportError 的异常。"""

        def find_spec(self, fullname, path=None, target=None):
            if fullname == "mineru":
                raise RuntimeError("坏掉的 mineru 安装：导入期炸了")
            return None

    monkeypatch.setattr(sys, "meta_path", [_BrokenFinder(), *sys.meta_path])

    with pytest.raises(DocumentParseError) as excinfo:
        mineru.parse_pdf(b"%PDF-1.4 payload")

    message = str(excinfo.value)
    assert "坏掉的 mineru 安装" in message, "导入期真因必须出现在报错里"
    assert "API 不匹配" not in message, "导入期真因不该被归一成「版本 API 不匹配」"


def test_non_callable_facade_cascades_to_next_entry(monkeypatch):
    """入口①「存在但不可调用」= 本版本没这个入口 → 必须级联到能成功的退路。

    变异 M27 暴露的盲区：把「不可调用」改抛 RuntimeError 后不再级联，
    本可成功的 `mineru.parse` 被跳过，解析直接失败。
    """
    _enable_mineru(monkeypatch)
    pkg = types.ModuleType("mineru")
    pkg.__path__ = []
    sub = types.ModuleType("mineru.parser")
    sub.parse = "不是可调用对象"  # 入口①存在但不可调用
    pkg.parser = sub
    pkg.parse = lambda _data: "旧版入口产出"  # 本可成功的退路
    monkeypatch.setitem(sys.modules, "mineru", pkg)
    monkeypatch.setitem(sys.modules, "mineru.parser", sub)

    ok, reason = mineru.available()
    assert ok is True
    # 阶段 4 · P-登记-回归检查：措辞断言降级为前缀断言（本用例的覆盖意图在下面的级联断言）
    assert reason.startswith("MinerU 可用，backend="), reason
    text, pages = mineru.parse_pdf(b"%PDF-1.4 payload")

    assert text == "旧版入口产出", "不可调用应判为「入口不可用」并级联，而不是直接失败"
    assert pages == ["旧版入口产出"]


def test_document_parse_error_from_entry_is_not_double_wrapped(monkeypatch):
    """入口抛出的中文 `DocumentParseError` 必须原样放行（`parse_pdf` 不得二次包裹）。

    变异 M28 暴露的盲区：把 `except DocumentParseError: raise` 删掉后真因被套一层
    「MinerU 解析 PDF 失败：…」，排障时首句看到的不是真因。
    """
    _enable_mineru(monkeypatch)

    def boom(_path, **_kwargs):
        raise DocumentParseError("原始诊断：模型权重没下")

    _inject_parser(monkeypatch, boom)

    with pytest.raises(DocumentParseError) as excinfo:
        mineru.parse_pdf(b"%PDF-1.4 payload")

    assert str(excinfo.value) == "原始诊断：模型权重没下", "已可读的中文异常不该被再包一层"


def test_argument_error_from_entry_fails_fast_not_silently_cascaded(monkeypatch):
    """运行期 `TypeError`（如关键字不匹配）必须**报真因**，不得被静默当成「入口不存在」。

    这条把 F1 的设计取舍钉死：宁可对真机 API 差异快速失败并给出真因，
    也不静默换入口（静默换入口会让同一 PDF 在旧路径上被重复解析，且质量差异无人知晓）。
    """
    _enable_mineru(monkeypatch)

    def rejects_kwargs(_path, **_kwargs):
        raise TypeError("parse() got an unexpected keyword argument 'tier'")

    _inject_parser(monkeypatch, rejects_kwargs)

    with pytest.raises(DocumentParseError) as excinfo:
        mineru.parse_pdf(b"%PDF-1.4 payload")

    message = str(excinfo.value)
    assert "unexpected keyword argument" in message, "必须报出真实的签名不匹配"
    assert "API 不匹配" not in message, "运行期 TypeError 不该被归一成「版本 API 不匹配」"


# ---------------------------------------------------------------------------
# 阶段 5 · 条目 5.3 真机抽查抓到的缺陷 回归修复：MinerU 内联 base64 图片污染正文
# ---------------------------------------------------------------------------
def _giant_base64_line(size: int = 200_000) -> str:
    """造一行「内联 base64 图片」——真机上它长达 814,352 字（见 S5-真机抽查.md）。"""
    return "![](" + "data:image/jpeg;base64," + "QUJDREVGR0g" * (size // 12) + ")"


def test_export_strips_embedded_base64_images(monkeypatch):
    """`_export` 必须剥离内联 base64 图片，且**不得破坏** `pages ⊂ text`（下游页码定位前提）。

    实测依据：样本 A（16 页原生 PDF）的 MinerU markdown 是 3,637,885 字，其中 17 行占
    3,581,032 字（98.4%）、最长一行 814,352 字，全是 `![](data:image/...;base64,...)`。
    它原样入库会变成 5214 个垃圾块并让向量化 400 失败（`回归修复`）。
    """
    page1 = "第一页正文 " + "内容" * 60
    page2 = "第二页正文 " + "内容" * 60
    giant = _giant_base64_line()
    marked = f"{giant}\n\n{page1}\n\n---\n\n{page2}\n\n![图](data:image/png;base64,{'QQ==' * 50_000})\n"

    seen = {"marked_markdown": marked}
    _stub_runtime_deps(monkeypatch, markdown=marked, pages=[page1, page2], seen=seen)

    text, pages = mineru.parse_pdf(b"%PDF-1.4 payload")

    assert "base64" not in text, "内嵌 base64 必须被剥掉"
    assert "QUJDREVGR0g" not in text, "内嵌图片数据不得留在正文里"
    assert len(text) < 5000, f"剥离后应回到散文量级，实际 {len(text)} 字"
    assert page1 in text and page2 in text, "剥离不得吃掉正文"
    assert pages == [page1, page2], "页边界仍要切得出来"
    assert all(page in text for page in pages), "每页必须是全文的字面子串（页码定位前提）"


def test_export_falls_back_to_blocks_when_markdown_is_only_binary(monkeypatch):
    """正文被剥空时回落**块数据态**（与既有取文顺序一致），而不是返回空文本。"""
    marked = _giant_base64_line()
    seen = {"marked_markdown": marked}
    _stub_runtime_deps(monkeypatch, markdown=marked, pages=["块数据态的正文"], seen=seen)

    text, pages = mineru.parse_pdf(b"%PDF-1.4 payload")

    assert text == "块数据态的正文"
    assert pages == ["块数据态的正文"]


def test_export_falls_back_to_default_render_when_marked_render_is_only_binary(monkeypatch):
    """带标记渲染**只剩内嵌图**、默认渲染还有正文时，必须改用默认渲染取文（对抗性复核 F5a）。

    这是 `_export` 第二道取文（`_markdown_of(result)` 不带标记）的**唯一独立价值**：
    原先的用例桩里两个 markdown 取值相同、去掉这道防线结果逐字不变 ⇒ 变异存活、防线无人守
    （独立核验代理 2026-09-18 的隔离变异实测）。
    """
    prose = "默认渲染里的正文。"
    seen = {"marked_markdown": _giant_base64_line()}  # 带标记渲染：只剩内嵌图，没有散文
    _stub_runtime_deps(monkeypatch, markdown=prose, pages=["块数据态"], seen=seen)

    text, pages = mineru.parse_pdf(b"%PDF-1.4 payload")

    assert text == prose, "带标记渲染没正文时应改用默认渲染，而不是直接掉到块数据态"
    assert "块数据态" not in text
    assert pages == [prose]


def test_strip_embedded_binary_never_raises_and_keeps_plain_text():
    """净化是尽力而为：正常 Markdown 逐字不动，异常输入也不抛（不变量 3 不炸穿）。"""
    plain = "# 标题\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n公式：$E=mc^2$\n\n---\n\n第二页"
    assert mineru._strip_embedded_binary(plain) == plain.strip()
    assert mineru._strip_embedded_binary("") == ""
    # 短 base64 片段（<64 连续字符）不算内嵌图数据，不该被误删
    short = "见 data:image/png;base64,QUJD 该图"
    assert "QUJD" in mineru._strip_embedded_binary(short)


# ---------------------------------------------------------------------------
# 阶段 5 · 对抗性复核抓出的缺陷 回归修复（修复 回归修复 时引入）
# 每条都对应复核方给出的最小复现，逐条钉死
# ---------------------------------------------------------------------------
def test_strip_does_not_eat_body_when_data_uri_has_no_closing_paren():
    """**F1（数据丢失）**：内嵌图缺右括号时，不得把后续正文一并吃掉。

    旧正则 `[^)]*` 无界且跨行 ⇒ 会一路匹配到文档里**任意一个** `)`，实测 11,800 字 → 0 字。
    修法：负载类不得跨空白（`[^)\\s]*`）+ 长度上界。
    """
    broken = "![fig](data:image/jpeg;base64," + "QUJDREVGR0g" * 400 + "\n\n第一段正文。\n\n第二段正文（见下文）。"
    cleaned = mineru._strip_embedded_binary(broken)

    assert "第一段正文。" in cleaned, "正文段 1 被误删（F1 复发）"
    assert "第二段正文（见下文）。" in cleaned, "正文段 2 被误删（F1 复发）"
    assert "QUJDREVGR0gQUJDREVGR0g" not in cleaned, "内嵌数据未被剥离"


def test_strip_normalizes_real_mime_data_uri():
    """**F3（兜底网失效）**：带斜杠的真实 MIME 必须被兜底正则命中，且不得吞掉后续正文。

    旧 `[A-Za-z0-9.+-]+` 不含 `/` ⇒ `data:image/jpeg;base64,…` 等真实形态全不命中；
    而补上 `/` 若不同时去掉负载类里的 `\\s`，正则会开始吞掉后续英文正文（实测 311 字 → 21 字）。
    """
    cases = [
        "data:image/jpeg;base64," + "QUJDREVGR0g" * 20,
        "data:image/png;base64," + "iVBORw0KGgo" * 20,
        "data:application/octet-stream;base64," + "AAECAwQFBgc" * 20,
        "data:text/plain;charset=utf-8;base64," + "SGVsbG8gd29ybGQ" * 20,
    ]
    for payload in cases:
        text = f"前文。{payload} 后文继续 (This sentence must survive)."
        cleaned = mineru._strip_embedded_binary(text)
        assert "This sentence must survive" in cleaned, f"MIME 兜底网吞掉了正文：{payload[:32]}"
        assert "前文。" in cleaned and "后文继续" in cleaned
        assert "QUJDREVGR0gQUJD" not in cleaned and "iVBORw0KGgoiVBORw0KGgo" not in cleaned, "负载未被剥离"


def test_export_keeps_page_numbering_when_first_page_is_image_only(monkeypatch):
    """**F2（页码静默错标·回归）**：第 1 页只剩一张内嵌图被剥空时，页表必须**保持 2 页**。

    旧实现丢空页 + 整篇 `strip()` 吃掉页标记前导换行 ⇒ 页数塌缩成 1，而
    ``chunking._assign_page`` 返回的是「第几个页文本命中探针」的**下标**，
    于是第 2 页的正文被静默标成第 1 页（不是 None、也没有日志）；
    连带 ``cleaning`` 的跨页页眉检测因 pages<2 直接放弃。

    **钉的是「不位移 / 不误标」**：页表长度与结构页数一致、第 2 页文本落在 pages[1]、
    并且**任何块都不会被标成 page=1**。跨在被剥空页边界上的那一块允许 `page=None`
    ——那是「找不到就诚实给 None」的既有语义，比错标成 1 正确。
    """
    page1_markdown = "![](" + "data:image/jpeg;base64," + "QUJDREVGR0g" * 100 + ")"
    p2 = "第二页的正文段落，用于验证页码没有前移。" * 4
    p3 = "第三页的正文段落，用于验证后续页仍然可定位。" * 4
    marked = f"{page1_markdown}\n\n---\n\n{p2}\n\n---\n\n{p3}"

    seen = {"marked_markdown": marked}
    _stub_runtime_deps(monkeypatch, markdown=marked, pages=["块页一", "块页二", "块页三"], seen=seen)

    text, pages = mineru.parse_pdf(b"%PDF-1.4 payload")

    assert len(pages) == 3, f"页数塌缩（F2 复发）：pages={pages!r}"
    assert pages[0] == "", "被剥空的第 1 页要占位（空串），不能消失"
    assert pages[1] == p2.strip() and pages[2] == p3.strip(), "第 2/3 页文本丢失或错位"
    assert all(page in text for page in pages), "pages ⊂ text 仍须成立"

    # 页码解析用 `_assign_page` 直接钉（不依赖切块尺寸的偶然性）：
    #   · 第 2/3 页的正文仍解析到 **2/3**（没有整体前移）；
    #   · 跨在「被剥空页」边界上的那一块诚实给 None（既有语义「找不到就给 None」），
    #     而不是像旧实现那样被**错标成第 1 页**。
    assert chunking._assign_page(p2.strip()[:40], pages) == 2, "第 2 页被前移（F2 回归）"
    assert chunking._assign_page(p3.strip()[:40], pages) == 3, "第 3 页被前移（F2 回归）"
    assert chunking._assign_page("---\n\n" + p2[:30], pages) is None, "跨空页边界的块应诚实给 None"


def test_export_falls_back_when_markdown_left_with_only_page_markers(monkeypatch):
    """**F5**：多页文档「剥完只剩页标记」时，必须回落到块数据态，而不是把 `---` 当正文入库。"""
    image = "![](" + "data:image/jpeg;base64," + "QUJDREVGR0g" * 100 + ")"
    marked = f"{image}\n\n---\n\n{image}"
    seen = {"marked_markdown": marked}
    _stub_runtime_deps(monkeypatch, markdown=marked, pages=["块态第一页正文", "块态第二页正文"], seen=seen)

    text, pages = mineru.parse_pdf(b"%PDF-1.4 payload")

    assert text == "块态第一页正文\n\n块态第二页正文", f"未回落块数据态（F5 复发）：{text!r}"
    assert pages == ["块态第一页正文", "块态第二页正文"]


def test_is_data_line_keeps_long_prose_url_and_digit_runs():
    """**F4**：兜底网不得误删正常长行（长 URL / 纯数字串 / 带空格的散文）。

    旧判据「≥4096 字且 ≥90% 为 base64 字母表字符」会把这些全判成数据；新判据要求
    **整行无表外字符且大小写混合**，三者因此安全。
    """
    long_url = "https://example.com/" + "abcdefghij" * 500          # 含 . / : 等表外字符
    digits = "31415926535897932384626433832795" * 200               # 纯数字（无大小写混合）
    prose = ("This paragraph is intentionally very long so that it crosses the threshold. " * 90)
    real_b64 = ("QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVph" * 120)        # 真实 base64 形态（混合大小写）

    assert not mineru._is_data_line(long_url)
    assert not mineru._is_data_line(digits)
    assert not mineru._is_data_line(prose)
    assert mineru._is_data_line(real_b64), "真实长 base64 负载必须被兜底网接住"


def test_strip_is_linear_on_pathological_input():
    """**F7**：病态输入（`![` 无 `]`、`<img src=` 无闭合引号）不得退化成二次方扫描。

    旧的两条无界量词在 108KB 输入上实测 597ms；加长度上界后应当远低于此。判据给得很松
    （< 1.5s），只要能挡住「无界 × 无界」的退化即可。
    """
    import time

    nasty = "![unclosed " * 8000 + "\n" + '<img src="data:image/png;base64,' * 3000
    started = time.perf_counter()
    mineru._strip_embedded_binary(nasty)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.5, f"净化耗时 {elapsed:.3f}s，疑似二次方扫描（F7 复发）"
