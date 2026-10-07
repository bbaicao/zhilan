"""MinerU 可插拔适配器测试（条目 2.2.4）。

纪律（统筹 §3.3 / 条目 2.2.4）：不装 MinerU、不连网。
- MINERU_ENABLE 关时 available() 直接返回未开启，且绝不在 sys.modules 留下 mineru；
- 开启但未装 → parsing 回落 pypdf，解析结果与关时逐字相同（回落等价性）；
- parse_pdf 任何失败 → 中文可读的 DocumentParseError；
- 装了 MinerU（注入假模块）→ 走 MinerU 出口。
"""

import sys
import types

import pytest

from app.core.config import settings
from app.core.errors import DocumentParseError
from app.pipeline import mineru, parsing


def test_available_off_reports_and_does_not_import():
    assert settings.MINERU_ENABLE is False  # 默认关
    ok, reason = mineru.available()
    assert ok is False
    assert "未开启" in reason
    # 关时绝不 import 重依赖
    assert "mineru" not in sys.modules


def test_available_on_but_not_installed():
    monkeypatch_mode = pytest.MonkeyPatch()
    monkeypatch_mode.setattr(settings, "MINERU_ENABLE", True)
    try:
        ok, reason = mineru.available()
        assert ok is False
        assert "未安装" in reason
        assert "mineru" not in sys.modules
    finally:
        monkeypatch_mode.undo()


def test_parse_pdf_raises_chinese_when_unavailable():
    monkeypatch_mode = pytest.MonkeyPatch()
    monkeypatch_mode.setattr(settings, "MINERU_ENABLE", True)
    try:
        with pytest.raises(DocumentParseError) as excinfo:
            mineru.parse_pdf(b"%PDF-1.4 not really a pdf")
        assert "失败" in str(excinfo.value)
    finally:
        monkeypatch_mode.undo()


def test_fallback_equivalence_when_enabled_but_absent(pdf_with_text):
    """MINERU_ENABLE=True 但未装时，PDF 分支回落 pypdf，与关闭时逐字相同。"""
    off_text, off_pages = parsing.parse_document_pages("x.pdf", pdf_with_text)
    monkeypatch_mode = pytest.MonkeyPatch()
    monkeypatch_mode.setattr(settings, "MINERU_ENABLE", True)
    try:
        assert mineru.available()[0] is False  # mineru 确实没装
        on_text, on_pages = parsing.parse_document_pages("x.pdf", pdf_with_text)
    finally:
        monkeypatch_mode.undo()
    assert on_text == off_text
    assert on_pages == off_pages


def test_installed_path_uses_mineru(scanned_pdf, monkeypatch):
    """注入一个假 mineru 模块，验证 available() 与 parsing 的成功出口。

    **2026-09-17 阶段 2 改动（见统筹 §三「登记偏差 P-登记-1」）**：本条原用
    ``b"anything"`` 当输入，钉的是「MINERU_ENABLE 开 → 先试 MinerU」这一**已被条目 2.2
    淘汰的旧行为**——新契约是「先 pypdf 密度判定，密度达标不走 MinerU」，且 pypdf 对
    ``b"anything"`` 直接抛 DocumentParseError（阶段 2 文档 §六：损坏 PDF 不该让 OCR 兜底）。
    故只把输入换成**低密度的真扫描件样本**（保留「装了 MinerU 的扫描件走 MinerU 出口」的
    覆盖意图），断言内容与其余用例逐字未动；同文件其他用例零改动。
    """
    fake = types.ModuleType("mineru")

    def fake_parse(data):  # 假主入口，返回 MinerU 的 Markdown 文本（含分页标记）
        return "FAKE_PAGE_1\n\n<!-- PageBreak -->\n\nFAKE_PAGE_2"

    fake.parse = fake_parse
    sys.modules["mineru"] = fake
    monkeypatch_mode = pytest.MonkeyPatch()
    monkeypatch_mode.setattr(settings, "MINERU_ENABLE", True)
    try:
        ok, reason = mineru.available()
        assert ok is True
        # 阶段 4 · 条目 4.2 规格 2：第二元素从固定文案「MinerU 可用」**充实**为带后端/设备的摘要
        # （登记偏差 P-登记-回归检查：原断言是 `available() == (True, "MinerU 可用")` 的全等比较）。
        # 覆盖意图不变（「注入假 mineru 模块 → available() 为真」），并顺带钉住新契约的前缀形态。
        assert reason.startswith("MinerU 可用，backend=pipeline"), reason
        text, pages = parsing.parse_document_pages("x.pdf", scanned_pdf)
        assert "FAKE_PAGE_1" in text and "FAKE_PAGE_2" in text
        assert pages == ["FAKE_PAGE_1", "FAKE_PAGE_2"]
        # parse_pdf 同样走假模块，并按分页标记切成按页文本
        assert mineru.parse_pdf(b"anything") == (
            "FAKE_PAGE_1\n\n<!-- PageBreak -->\n\nFAKE_PAGE_2",
            ["FAKE_PAGE_1", "FAKE_PAGE_2"],
        )
    finally:
        monkeypatch_mode.undo()
        del sys.modules["mineru"]
