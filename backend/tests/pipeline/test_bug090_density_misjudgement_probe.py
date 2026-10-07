"""`回归修复` 判据载体（原定位探针 · 修复落地时按其自述授权**就地改写**）。

## 沿革（逐字口径，防误读）

· 本文件原为 `090` 的**定位探针**（钉现状：有文本层 + 密度低 ⇒ 整篇交给 MinerU）；
· `回归修复` 修复（2026-09-27：`_parse_pdf_document` 新增**文本层覆盖腿**——每页有效字符
  ≥ ``parsing._MIN_TEXT_PER_PAGE`` ⇒ 原生，密度不作数）落地后，按原探针 docstring 的自述
  「阈值/分流实现已改 ⇒ 须重核本载体」**就地改写为修复验证载体**，断言方向反转：
  **同一形态（有文本层 + 样本 A 密度 0.0077 < 0.01）⇒ 不再交给 MinerU、走原生路**。

## 现在钉三件

① **判据面前提**：样本 A 的密度值今天仍低于默认阈值（阈值未动 ⇒ 本条前提没变）；
② **分流面（修复主腿）**：每页都有 ≥ 20 有效字符的文本层 + 密度 0.0077 ⇒ **MinerU 不被调用**，
   返回 pypdf 原生结果；
③ **反向腿（盖章扫描件）**：每页只有零星字符（< 20，凑不出文本层）+ 密度低 ⇒ **仍整篇交 MinerU**
   —— 文本层覆盖腿的下限护栏，防「页码/水印戳扫描件」漏网。

⚠️ 限度（如实登记）：本文件**不**复刻样本 A 那份真 PDF（需嵌入真图片字节）；「原样本上密度
确实是 0.0077」由解析树的既有读数承载。桩只打 `_pdf_raw_pages` / `_text_density` /
`mineru.*` 三个判据点，分流式本体跑的是**真实产品代码**。
"""

from __future__ import annotations

from app.core.config import settings
from app.pipeline import parsing

#: 样本 A 实测密度（解析树证据件文末逐字）
SAMPLE_A_DENSITY = 0.0077


def test_sample_a_density_is_still_below_the_default_threshold():
    """① **判据面前提**：样本 A 的密度今天仍低于默认阈值（否则本条前提变了）。"""
    threshold = float(settings.OCR_SCAN_THRESHOLD)
    assert SAMPLE_A_DENSITY < threshold, (
        f"样本 A 密度 {SAMPLE_A_DENSITY} 已不低于阈值 {threshold} ⇒ `090` 的前提变了，须重核"
    )


def test_text_layer_on_every_page_with_low_density_stays_native(monkeypatch):
    """② **分流面（修复主腿）**：每页都有文本层 + 密度低 ⇒ **原生**，MinerU 零调用。

    页面桩给足 ≥ ``_MIN_TEXT_PER_PAGE``（20）个有效字符——原探针里「第 1 页有真实文字」
    （10 字符）这类**够不着下限**的页面在这里升级成整句正文。
    """
    calls: list[str] = []

    monkeypatch.setattr(
        parsing,
        "_pdf_raw_pages",
        lambda data: [
            "这是第一页的真实正文内容，长度必须超过单页下限。",
            "这是第二页的真实正文内容，同样超过单页下限。",
        ],
    )
    monkeypatch.setattr(parsing, "_text_density", lambda pages, data: SAMPLE_A_DENSITY)
    monkeypatch.setattr(settings, "MINERU_ENABLE", True, raising=False)
    monkeypatch.setattr(parsing.mineru, "available", lambda: (True, "stub 可用"))

    def _must_not_be_called(data):
        calls.append("mineru.parse_pdf")
        return "MinerU 整篇重解析的正文", ["MinerU 整篇重解析的正文"]

    monkeypatch.setattr(parsing.mineru, "parse_pdf", _must_not_be_called)

    text, pages = parsing._parse_pdf_document(b"%PDF-1.4 stub")

    assert calls == [], (
        "每页都有文本层却被整篇交给 MinerU ⇒ `090` 的修复回归了。calls=" + repr(calls)
    )
    assert "第一页" in text, f"应返回 pypdf 的原生结果，实得：{text!r}"
    assert pages == [
        "这是第一页的真实正文内容，长度必须超过单页下限。",
        "这是第二页的真实正文内容，同样超过单页下限。",
    ]


def test_stamped_scan_pages_below_threshold_still_go_to_mineru(monkeypatch):
    """③ **反向腿（盖章/页码戳扫描件）**：每页 < 20 有效字符 + 密度低 ⇒ 仍交 MinerU。

    这是文本层覆盖腿的**下限护栏**：没有它，「每页都有一行水印/页码」的扫描件会被新腿误放行。
    """
    calls: list[str] = []

    monkeypatch.setattr(parsing, "_pdf_raw_pages", lambda data: ["1", "2", "3"])
    monkeypatch.setattr(parsing, "_text_density", lambda pages, data: SAMPLE_A_DENSITY)
    monkeypatch.setattr(settings, "MINERU_ENABLE", True, raising=False)
    monkeypatch.setattr(parsing.mineru, "available", lambda: (True, "stub 可用"))

    def _fake_parse_pdf(data):
        calls.append("mineru.parse_pdf")
        return "MinerU 整篇重解析的正文", ["MinerU 整篇重解析的正文"]

    monkeypatch.setattr(parsing.mineru, "parse_pdf", _fake_parse_pdf)

    text, _pages = parsing._parse_pdf_document(b"%PDF-1.4 stub")

    assert calls == ["mineru.parse_pdf"], (
        "每页只有零星字符却没交给 MinerU ⇒ 文本层下限护栏失效（盖章扫描件漏网）。calls=" + repr(calls)
    )
    assert "MinerU" in text
