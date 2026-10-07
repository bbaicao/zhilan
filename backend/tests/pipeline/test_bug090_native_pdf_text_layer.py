"""`回归修复` 判据载体：**文本层覆盖腿**（真实 pypdf 提取 + 真实最小 PDF 字节）。

## 与探针文件的分工

· [`test_bug090_density_misjudgement_probe.py`](test_bug090_density_misjudgement_probe.py)
  = 判据面前提 + 桩面分流三腿（桩 `_pdf_raw_pages` / `_text_density`，分流式跑真代码）；
· 本文件 = **真实提取面**：`_pdf_raw_pages` 用**真 pypdf** 解析 conftest 手工拼装的最小 PDF，
  只在「密度」与「MinerU」两个外部依赖上打桩（本机无真 MinerU、构造不出真扫描件字节）。

## 腿表

| 腿 | 形态 | 断言 |
|---|---|---|
| ① | 单页真 PDF（正文 > 20 字符）+ 密度桩 0.0077 | 原生路，MinerU 零调用，返回真提取文本 |
| ② | 双页真 PDF（每页 > 20 字符）+ 零密度桩（真密度） | 原生路，两页槽位与文本齐全 |
| ③ | 真·空 PDF（扫描件形态）+ MinerU 桩 | **仍整篇交 MinerU**（零文本 ⇒ 覆盖腿不生效） |
| ④ | 真·盖章 PDF（每页 1 字符）+ MinerU 桩 | **仍整篇交 MinerU**（凑不出 20 字符下限） |
| ⑤ | 混排真 PDF（1 正文页 + 1 空页，MinerU 关） | 走既有密度腿进 `_fill_scan_pages`，空页槽位保留（`回归修复` 行为逐字不变） |

⚠️ 边界（不得越界声称）：本修复只覆盖「**有文本层但密度被图片字节稀释**」的误判形态；
`回归修复` / `回归修复` 那两支（mineru 出口净化 / 缺页补齐）**不在本覆盖面内**，
⛔ 不得写「扫描件误判已全面修好」。
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.pipeline import parsing
from tests.pipeline.conftest import make_pdf, make_pdf_multi

SAMPLE_A_DENSITY = 0.0077
BODY = "This page carries real body text, far beyond the per page floor."


def _enable_mineru_stub(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    """把 MinerU 打成「可用且会留痕」的桩（本机无真 MinerU，且不许真跑重模型）。"""
    monkeypatch.setattr(settings, "MINERU_ENABLE", True, raising=False)
    monkeypatch.setattr(parsing.mineru, "available", lambda: (True, "stub 可用"))

    def _fake_parse_pdf(data):
        calls.append("mineru.parse_pdf")
        return "MINERU-整篇重解析", ["MINERU-整篇重解析"]

    monkeypatch.setattr(parsing.mineru, "parse_pdf", _fake_parse_pdf)


def test_real_extracted_text_layer_with_sample_a_density_stays_native(monkeypatch):
    """腿 ①（主腿·真 pypdf 提取）：真提取的文本层 + 样本 A 密度 ⇒ 原生，MinerU 零调用。"""
    calls: list[str] = []
    _enable_mineru_stub(monkeypatch, calls)
    monkeypatch.setattr(parsing, "_text_density", lambda pages, data: SAMPLE_A_DENSITY)

    text, pages = parsing._parse_pdf_document(make_pdf(BODY))

    assert calls == [], "真提取的文本层被判去 MinerU ⇒ 090 修复回归"
    assert BODY in text
    assert pages == [BODY]


def test_real_two_page_native_pdf_stays_native_without_any_density_stub(monkeypatch):
    """腿 ②（零桩·真密度）：双页正文真 PDF ⇒ 原生，页槽位与文本齐全。"""
    calls: list[str] = []
    _enable_mineru_stub(monkeypatch, calls)

    text, pages = parsing._parse_pdf_document(make_pdf_multi(["ALPHA " * 10, "BETA " * 10]))

    assert calls == []
    assert "ALPHA" in text and "BETA" in text
    assert len(pages) == 2 and all(pages), f"两页槽位都应有正文：{pages!r}"


def test_true_scanned_pdf_still_goes_to_mineru(monkeypatch, scanned_pdf: bytes):
    """腿 ③（反向·真扫描件）：零文本真 PDF ⇒ 仍整篇交 MinerU（覆盖腿对零文本不生效）。"""
    calls: list[str] = []
    _enable_mineru_stub(monkeypatch, calls)

    text, _pages = parsing._parse_pdf_document(scanned_pdf)

    assert calls == ["mineru.parse_pdf"], "真·零文本扫描件没交给 MinerU ⇒ 扫描件主路被打断"
    assert text == "MINERU-整篇重解析"


def test_stamped_pages_of_real_pdf_still_go_to_mineru(monkeypatch):
    """腿 ④（反向·盖章页）：每页 1 个字符的真 PDF ⇒ 仍整篇交 MinerU（下限护栏）。"""
    calls: list[str] = []
    _enable_mineru_stub(monkeypatch, calls)

    text, _pages = parsing._parse_pdf_document(make_pdf_multi(["1", "2", "3"]))

    assert calls == ["mineru.parse_pdf"], "盖章/页码戳形态漏进原生路 ⇒ 下限护栏失效"
    assert text == "MINERU-整篇重解析"


def test_mixed_pdf_with_empty_page_keeps_dp006_behavior(monkeypatch):
    """腿 ⑤（混排回归腿）：1 正文页 + 1 空页 ⇒ 密度腿进 `_fill_scan_pages`，MinerU 关时空槽保留。"""
    calls: list[str] = []
    monkeypatch.setattr(settings, "MINERU_ENABLE", False, raising=False)  # 缺页补齐按配置跳过
    monkeypatch.setattr(parsing.mineru, "available", lambda: (False, "MINERU_ENABLE 未开启"))

    text, pages = parsing._parse_pdf_document(make_pdf_multi([BODY, ""]))

    assert calls == [], "MinerU 关着却跑了 OCR ⇒ 配置语义被破坏"
    assert BODY in text
    assert len(pages) == 2 and pages[0] == BODY and pages[1] == "", (
        f"空页槽位必须保留（回归修复/D1 口径），实得：{pages!r}"
    )
