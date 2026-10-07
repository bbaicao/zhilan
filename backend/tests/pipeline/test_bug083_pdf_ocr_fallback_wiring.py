"""`回归修复` 第二批判据载体：**PDF OCR 兜底链（S11–S13）的吞点接线**。

🔴 口径（逐字，防误读）

· 钉的是「**失败/降级出口存在且真的会动**」，**不是**「派生失败已被消除」；
· 可见面 = `GET /api/agent/audit` 的 **`counters.derived`**，键 = **`pdf_ocr_fallback`**
  （`COUNTER_KEYS` 只增不改义；零态键集钉在
  `tests/api/test_bug083_s10_breaker.py::EXPECTED_DERIVED_KEYS` 与
  `tests/api/test_bug083_counter_keys_ten.py::TEN_KEYS`；本批五键 → 七键 → 2026-09-28 批次三**十键**同步扩）；
· 该出口**前端零消费**（`grep -rn counters frontend/src` = 0 命中）⇒ 正确措辞 =
  「**机器可读 + 人工 curl 的可见面**」；⛔ 不得写「用户已能看见派生失败」。

## 覆盖的吞点（吞点地图 S11–S13）

| 吞点 | 落点（parsing.py） | 行为 |
|---|---|---|
| S11 | `_fill_scan_pages` 的 OCR 调用 except | 补齐失败 ⇒ 保留 pypdf 结果（缺页内容丢失） |
| 回归检查 | `_fill_scan_pages` 缺页数 > `_FILL_MAX_PAGES` | 按规模上限主动放弃 |
| 回归检查 | `_subset_pdf` 返回 None（抽页异常） | 放弃补齐 |
| 回归检查 | 子 PDF > `_FILL_MAX_SUBSET_BYTES` | 按字节上限主动放弃 |
| 回归检查 | OCR 返回形状不可用 | 放弃补齐（防页码错位） |
| S12 | `_parse_pdf_document` 整篇 MinerU 失败/异常 | 回落 pypdf 文本 |

⚠️ **不计数的两条**（有意，防计数器噪声）：`MINERU_ENABLE=False` 的「按配置未走 OCR」
分支（log.debug，属预期配置态，不是降级）与「传输接缝不存在」（见 web 载体）。
"""

from __future__ import annotations

import pytest

from app.core import derived_counters
from app.core.config import settings
from app.core.errors import DocumentParseError
from app.pipeline import parsing
from tests.pipeline.conftest import make_pdf, make_pdf_multi, make_empty_pdf

BODY = "This page carries real body text, far beyond the per page floor."


@pytest.fixture(autouse=True)
def _reset_process_counters():
    """进程全局计数**跨用例泄漏**是本仓踩过的坑 ⇒ 每例前后清零。"""
    derived_counters.reset_failure_counts()
    yield
    derived_counters.reset_failure_counts()


def _counts() -> int:
    return int(derived_counters.failure_counts().get("pdf_ocr_fallback", 0))


# --------------------------------------------------------------------------
# 零态
# --------------------------------------------------------------------------


def test_zero_state_has_all_seven_derived_keys():
    """零态：`ordered_counts()` 键集 = `COUNTER_KEYS` 全集（`回归修复` 后 = 八键；2026-09-28 批次三后 = **十键**）且全 0。"""
    counts = derived_counters.ordered_counts()
    assert set(counts) == set(derived_counters.COUNTER_KEYS)
    assert counts == dict.fromkeys(derived_counters.COUNTER_KEYS, 0)
    assert {"pdf_ocr_fallback", "web_fetch_degrade"} <= set(counts)


# --------------------------------------------------------------------------
# S11：缺页补齐失败
# --------------------------------------------------------------------------


def test_s11_fill_ocr_failure_bumps(monkeypatch):
    """S11：缺页 PDF + MinerU 可用但补齐 OCR 抛异常 ⇒ `pdf_ocr_fallback` +1，槽位保留。"""
    monkeypatch.setattr(settings, "MINERU_ENABLE", True, raising=False)
    monkeypatch.setattr(parsing.mineru, "available", lambda: (True, "stub 可用"))

    def _boom(_data):
        raise RuntimeError("模拟补齐 OCR 失败")

    monkeypatch.setattr(parsing.mineru, "parse_pdf", _boom)

    text, pages = parsing._parse_pdf_document(make_pdf_multi([BODY, ""]))

    assert _counts() >= 1, f"补齐失败被吞但计数没动 ⇒ 接线失效：{derived_counters.failure_counts()}"
    assert BODY in text and pages[1] == "", "缺页槽位必须保留（回归修复 回归腿）"


# --------------------------------------------------------------------------
# S13：四条「放弃补齐」退路
# --------------------------------------------------------------------------


def test_s13a_page_limit_gives_up_and_bumps(monkeypatch):
    """回归检查：缺页数 > `_FILL_MAX_PAGES`（桩成 0）⇒ 主动放弃 + 计数（配置关着也计：这是降级不是配置态）。"""
    monkeypatch.setattr(settings, "MINERU_ENABLE", False, raising=False)
    monkeypatch.setattr(parsing, "_FILL_MAX_PAGES", 0)

    text, pages = parsing._parse_pdf_document(make_pdf_multi([BODY, ""]))

    assert _counts() >= 1, "规模上限退路被吞但计数没动 ⇒ 接线失效"
    assert BODY in text and pages[1] == ""


def test_s13c_subset_build_failure_bumps(monkeypatch):
    """回归检查：`_subset_pdf` 返回 None（抽页异常桩）⇒ 放弃补齐 + 计数。"""
    monkeypatch.setattr(settings, "MINERU_ENABLE", True, raising=False)
    monkeypatch.setattr(parsing.mineru, "available", lambda: (True, "stub 可用"))
    monkeypatch.setattr(parsing, "_subset_pdf", lambda _data, _missing: None)

    text, pages = parsing._parse_pdf_document(make_pdf_multi([BODY, ""]))

    assert _counts() >= 1, "子 PDF 构造失败退路被吞但计数没动 ⇒ 接线失效"
    assert BODY in text


def test_s13d_subset_bytes_limit_gives_up_and_bumps(monkeypatch):
    """回归检查：子 PDF > `_FILL_MAX_SUBSET_BYTES`（桩成 1 字节）⇒ 主动放弃 + 计数。"""
    monkeypatch.setattr(settings, "MINERU_ENABLE", True, raising=False)
    monkeypatch.setattr(parsing.mineru, "available", lambda: (True, "stub 可用"))
    monkeypatch.setattr(parsing, "_FILL_MAX_SUBSET_BYTES", 1)

    text, _pages = parsing._parse_pdf_document(make_pdf_multi([BODY, ""]))

    assert _counts() >= 1, "字节上限退路被吞但计数没动 ⇒ 接线失效"
    assert BODY in text


def test_s13e_bad_ocr_shape_gives_up_and_bumps(monkeypatch):
    """回归检查：OCR 返回条数对不上 ⇒ 放弃补齐（防页码错位）+ 计数。"""
    monkeypatch.setattr(settings, "MINERU_ENABLE", True, raising=False)
    monkeypatch.setattr(parsing.mineru, "available", lambda: (True, "stub 可用"))
    monkeypatch.setattr(parsing.mineru, "parse_pdf", lambda _data: ("OCR", ["一", "二"]))

    text, pages = parsing._parse_pdf_document(make_pdf_multi([BODY, ""]))

    assert _counts() >= 1, "形状不可用退路被吞但计数没动 ⇒ 接线失效"
    assert BODY in text and pages[1] == ""


# --------------------------------------------------------------------------
# S12：整篇 MinerU 失败回落 pypdf
# --------------------------------------------------------------------------


def test_s12_whole_doc_mineru_failure_bumps_and_falls_back(monkeypatch):
    """S12：真·扫描件（零文本）+ MinerU 解析失败 ⇒ 回落 pypdf + 计数 + 按因果报错。"""
    monkeypatch.setattr(settings, "MINERU_ENABLE", True, raising=False)
    monkeypatch.setattr(parsing.mineru, "available", lambda: (True, "stub 可用"))

    def _boom(_data):
        raise DocumentParseError("模拟 MinerU 整篇解析失败")

    monkeypatch.setattr(parsing.mineru, "parse_pdf", _boom)

    with pytest.raises(DocumentParseError):
        parsing._parse_pdf_document(make_empty_pdf())

    assert _counts() >= 1, "整篇 MinerU 回落被吞但计数没动 ⇒ 接线失效"


# --------------------------------------------------------------------------
# 成功路径负腿
# --------------------------------------------------------------------------


def test_success_paths_do_not_bump(monkeypatch):
    """负腿：原生快路（无缺页）与「配置关着不走 OCR」都**不**计数（防计数器噪声）。"""
    monkeypatch.setattr(settings, "MINERU_ENABLE", False, raising=False)

    text, pages = parsing._parse_pdf_document(make_pdf(BODY))
    assert BODY in text and pages == [BODY]
    assert _counts() == 0, "成功路径不该计数"

    text2, pages2 = parsing._parse_pdf_document(make_pdf_multi([BODY, ""]))
    assert BODY in text2 and pages2[1] == ""
    assert _counts() == 0, "配置关闭态（预期行为，非降级）不该计数"
