"""解析器抽象（阶段 1 · 条目 1.2 / 1.3 / 1.4）：统一签名 + 注册表 + 公开兼容层。

钉死三件事：
1. 五个 Office/CSV 解析函数返回 ``tuple[str, list[str]]``，``pages`` 固定 ``[]``；
2. ``_PARSERS`` 注册表按后缀分发，PDF / txt / md **不进表**（PDF 要等阶段 2 分流）；
3. ``parse_document`` 仍是公开兼容层，返回纯文本（老调用方零改动）。

全部用例现场造 bytes，不连库、不连网、不碰 MinerU。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.errors import DocumentParseError
from app.pipeline import parsing
from app.pipeline.parsing import (
    _PARSERS,
    _parse_csv,
    _parse_docx,
    _parse_pptx,
    _parse_xlsx,
    parse_document,
    parse_document_pages,
)
from tests.pipeline.conftest import make_pdf, make_pdf_multi
from tests.pipeline.test_parsing_office import make_docx, make_empty_xlsx, make_pptx, make_xlsx

# 各后缀的造字节入口（parametrize 只传后缀名：直接把 bytes 当参数会在测试 id 里
# 塞进二进制，pytest 写环境变量时会炸「longer than 32767 characters」）。
_MAKERS = {
    "a.docx": make_docx,
    "a.xlsx": lambda: make_xlsx({"数据": [["名称"], ["投影仪"]]}),
    "a.pptx": make_pptx,
    "a.csv": lambda: "名称,金额\n投影仪,3".encode("utf-8"),
}

# `.xls` 不在 _MAKERS 里：本机没有能**生成** xls 的库（xlwt 未安装），
# 造不出行为级样本。它的 tuple 包装由「AST 语义等价证明」（证据/S1-语义等价证明.md）
# 与注册表键断言间接覆盖；happy path 留给有真实 .xls 样本的阶段补。
_XLS_GAP = "本机无 xlwt，无法现场生成 .xls 样本"


# ---------------------------------------------------------------------------
# 条目 1.2：签名统一为 (data) -> (text, pages)
# ---------------------------------------------------------------------------


def test_docx_parser_returns_tuple_with_empty_pages():
    """① docx 解析返回 tuple，且 pages 为空列表（该格式没有页边界）。"""
    result = _parse_docx(make_docx())
    assert isinstance(result, tuple) and len(result) == 2
    text, pages = result
    assert isinstance(text, str) and "# 项目说明" in text
    assert pages == []


def test_xlsx_parser_returns_tuple_with_empty_pages():
    """② xlsx 解析返回 tuple，且 pages 为空列表。"""
    result = _parse_xlsx(make_xlsx({"设备": [["名称", "金额"], ["投影仪", 3]]}))
    assert isinstance(result, tuple) and len(result) == 2
    text, pages = result
    assert isinstance(text, str) and "| 投影仪 | 3 |" in text
    assert pages == []


def test_csv_parser_returns_tuple_on_both_exit_paths():
    """csv 有两条出口（单行纯文本 / Markdown 表格），两条都必须包成 tuple。"""
    text, pages = _parse_csv("只有一行没有逗号".encode("utf-8"))
    assert text == "只有一行没有逗号" and pages == []
    text, pages = _parse_csv("名称,金额\n投影仪,3".encode("utf-8"))
    assert text == "| 名称 | 金额 |\n| --- | --- |\n| 投影仪 | 3 |" and pages == []


def test_pptx_parser_returns_tuple_with_empty_pages():
    text, pages = _parse_pptx(make_pptx())
    assert "## 幻灯片 1" in text and pages == []


def test_pdf_parser_returns_tuple_with_page_texts():
    """PDF 是唯一带页边界的格式：pages 非空，且全文 == 按页文本用空行拼回。

    2026-09-18 改动（缺陷 `回归修复`）：原先直接调 ``parsing._parse_pdf``，而它是 PDF
    分流逻辑的**第二份拷贝、生产代码零调用点**——即「这条用例覆盖的是一个没人调的函数」。
    该函数已删除，本用例改走唯一实现 ``parse_document_pages``（覆盖意图不变）。
    """
    data = make_pdf_multi(["First page body.", "Second page body."])
    text, pages = parse_document_pages("a.pdf", data)
    assert pages == ["First page body.", "Second page body."]
    assert text == "\n\n".join(pages)


def test_pdf_parser_keeps_scanned_hint():
    """签名改了，扫描件报错语义不变（改走唯一实现，理由见上一条）。"""
    with pytest.raises(DocumentParseError) as excinfo:
        parse_document_pages("a.pdf", make_pdf_multi(["", ""]))
    assert str(excinfo.value) == parsing.SCANNED_PDF_HINT


def test_pdf_parsing_has_single_implementation():
    """`回归修复` 的回归栅栏：PDF 分流只许有**一处**实现，且公开层不得漂移。

    缺陷原文：``_parse_pdf`` 是 ``parse_document_pages`` 内联逻辑的重复实现，且**不含密度分流**
    ——将来谁调它谁就绕过 MinerU。删掉它才是根治；本用例把这个「不许再长出第二份」钉住。
    """
    assert not hasattr(parsing, "_parse_pdf"), "PDF 分流出现了第二份实现（回归修复 复发）"
    data = make_pdf_multi(["only page body"])
    assert parse_document("a.pdf", data) == parse_document_pages("a.pdf", data)[0]



# ---------------------------------------------------------------------------
# 条目 1.3：注册表
# ---------------------------------------------------------------------------


def test_registry_covers_exactly_the_office_and_image_suffixes():
    """注册表 = 五种 Office + 六种图片（阶段 3 · 条目 3.1 追加图片）。

    2026-09-18 阶段 3 改动（见 S3-验收报告「登记偏差 P-登记-回归检查」）：原断言钉的是
    阶段 1 的「恰好五种 Office」。阶段 3 按规格要求把六种图片后缀注册到 `_parse_image`，
    故本断言随之扩展——**覆盖意图不变**（「注册表里该有什么、不该有什么」逐项钉死）。
    """
    assert set(_PARSERS) == {
        ".docx", ".xlsx", ".xls", ".pptx", ".csv",  # 阶段 1：五种 Office
        ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp",  # 阶段 3：六种图片（走 OCR）
    }
    # 六种图片都指向同一个 OCR 入口
    for suffix in (".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"):
        assert _PARSERS[suffix] is parsing._parse_image
    # PDF / txt / md 不进表：PDF 走分流器，txt/md 直接走 _decode
    for suffix in (".pdf", ".txt", ".md", ".markdown"):
        assert suffix not in _PARSERS


def test_xls_parser_is_registered_and_returns_tuple_on_bad_bytes():
    """`.xls` 的**行为级**覆盖只有「损坏字节 → 可读报错」这一条（见 _XLS_GAP）。

    这里额外确认：即便走异常路径，也不会漏出裸异常；成功路径的 tuple 包装由
    AST 语义等价证明兜底（`_parse_xls` 除 return 包装外逐语句未动）。
    """
    parser = _PARSERS[".xls"]
    with pytest.raises(DocumentParseError) as excinfo:
        parser(b"not a real xls")
    assert "Excel 97-2003 解析失败" in str(excinfo.value)
    assert _XLS_GAP  # 显式留痕：该格式的行为级 happy path 尚未覆盖


@pytest.mark.parametrize("filename", ["a.docx", "a.xlsx", "a.pptx", "a.csv"])
def test_parse_document_pages_dispatches_through_registry(filename):
    """分发路径必须经注册表，且出口恒为 (str, list)。"""
    data = _MAKERS[filename]()
    text, pages = parse_document_pages(filename, data)
    assert isinstance(text, str) and text.strip()
    assert pages == []


def test_registry_parser_receives_raw_bytes_and_returns_tuple():
    """注册表里的函数签名就是 (bytes) -> tuple；调用方不得再包一层。"""
    parser = _PARSERS[".csv"]
    result = parser("名称,金额\n投影仪,3".encode("utf-8"))
    assert isinstance(result, tuple) and len(result) == 2
    assert result[1] == []


def test_every_registry_parser_annotation_is_tuple():
    """类型标注也必须统一（回归修复 教训）：`_parse_xlsx` 曾漏改标注为 ``-> str``，
    运行时返回 tuple 却让静态检查/IDE 看到旧签名——标注与行为必须一致。

    两种合法形态都接受（对象形态 / ``from __future__ import annotations`` 下的字符串形态），
    但 ``-> str`` 这类旧签名两种形态都拦得住。
    """
    import inspect

    expected = (tuple[str, list[str]], "tuple[str, list[str]]")
    for suffix, parser in _PARSERS.items():
        annotation = inspect.signature(parser).return_annotation
        assert annotation in expected, f"{suffix} 的返回标注仍是 {annotation!r}"


def test_unsupported_suffix_still_rejected_before_registry_lookup():
    with pytest.raises(DocumentParseError):
        parse_document_pages("a.exe", b"whatever")


# ---------------------------------------------------------------------------
# 条目 1.4 ③：parse_document 仍是返回纯文本的公开兼容层
# ---------------------------------------------------------------------------


def test_parse_document_still_returns_plain_text():
    """``ingest`` 等老调用方拿到的必须是 str，不是 tuple。"""
    assert parse_document("a.txt", "第一行\n第二行".encode("utf-8")) == "第一行\n第二行"
    assert parse_document("a.csv", "名称,金额\n投影仪,3".encode("utf-8")).startswith("| 名称 |")
    pdf_text = parse_document("doc.pdf", make_pdf("Zhilan"))
    assert isinstance(pdf_text, str) and pdf_text.startswith("Zhilan")


def test_parse_document_equals_first_element_of_parse_document_pages():
    """兼容层与统一出口不得漂移：两处文本逐字相同。"""
    data = make_docx()
    assert parse_document("a.docx", data) == parse_document_pages("a.docx", data)[0]


# ---------------------------------------------------------------------------
# 空文件 / 空内容语义不变（签名改造不碰错误路径）
# ---------------------------------------------------------------------------


def test_empty_office_document_still_raises_empty_hint():
    with pytest.raises(DocumentParseError) as excinfo:
        _parse_xlsx(make_empty_xlsx())
    assert str(excinfo.value) == parsing.EMPTY_HINT


# ---------------------------------------------------------------------------
# 验收标准 4：MINERU_ENABLE=False 时 import parsing 不拉重依赖
# ---------------------------------------------------------------------------


def _run_isolated(code: str, extra_path: Path | None = None) -> str:
    """在干净子进程里跑一段代码，返回 stdout。

    ``PYTHONPATH`` 显式带上 backend 根（不依赖外部环境变量），可选再前置一个目录
    （用于注入假模块）。
    """
    backend_root = Path(__file__).resolve().parents[2]
    paths = [str(extra_path), str(backend_root)] if extra_path else [str(backend_root)]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(paths), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True, env=env
    )
    return proc.stdout


def test_import_parsing_does_not_pull_heavy_dependencies(tmp_path):
    """干净子进程里 import 解析模块，torch / mineru / magic_pdf / mineru_llama_cpp 一个都不许被拉起来。

    本机**没装** torch / mineru / magic_pdf（`find_spec` 全 None），所以「不在 sys.modules」
    是**必然为真**的弱断言——独立复核（F3）指出过这一点。这里先用**假模块**造出
    「重依赖在场」的世界：假模块一旦被 import 就会打印 `HEAVY-IMPORTED`。
    正对照确认假模块真的可被 import（否则本用例又会退化成空跑）。

    阶段 4 补充（验收标准 7）：清单里加 `mineru_llama_cpp`——它是 VLM 引擎的必需模块，
    「`MINERU_VLM_ENABLE=False` 时不 import 它」是本阶段的核心验收点之一，
    原先的三件套栅栏覆盖不到。
    """
    heavy_names = ("torch", "mineru", "magic_pdf", "mineru_llama_cpp")
    for name in heavy_names:
        (tmp_path / f"{name}.py").write_text(f"print('HEAVY-IMPORTED {name}')\n", encoding="utf-8")

    # 正对照：假模块必须真的可 import，否则下面的「没被 import」毫无意义
    control = _run_isolated(
        f"import {', '.join(heavy_names)}; print('CONTROL-OK')", extra_path=tmp_path
    )
    assert "CONTROL-OK" in control, f"假模块注入失败，用例会退化成空跑：{control!r}"
    assert control.count("HEAVY-IMPORTED") == len(heavy_names)

    # 主断言：import 解析模块不得拉起任何一个重依赖
    out = _run_isolated(
        "import sys; import app.pipeline.parsing; "
        f"heavy = [m for m in {heavy_names!r} if m in sys.modules]; "
        "print('HEAVY=' + ','.join(heavy))",
        extra_path=tmp_path,
    )
    assert "HEAVY=" in out, out
    assert out.strip().endswith("HEAVY="), f"重依赖被提前 import：{out.strip()!r}"
    assert "HEAVY-IMPORTED" not in out, f"重依赖真的被 import 了：{out.strip()!r}"


def test_mineru_module_import_is_lazy_even_when_enabled(tmp_path, monkeypatch):
    """``MINERU_ENABLE=True`` 也不许在 import 期拉 mineru：只有 `available()` 被调用才 import。

    这是 `mineru.py` 的延迟加载纪律（条目 2.2.4 设计），阶段 1 只负责**不破坏**它。
    """
    (tmp_path / "mineru.py").write_text("print('HEAVY-IMPORTED mineru')\n", encoding="utf-8")
    code = (
        "import sys; import app.pipeline.parsing; "
        "print('AFTER-IMPORT=' + str('mineru' in sys.modules)); "
        "from app.pipeline import mineru as m; "
        "m.available(); "
        "print('AFTER-AVAILABLE=' + str('mineru' in sys.modules))"
    )
    out = _run_isolated(code, extra_path=tmp_path)
    assert "AFTER-IMPORT=False" in out, f"import 期就拉了 mineru：{out!r}"
    # 开启后才允许 import；本机 MINERU_ENABLE 默认 False，故 available() 直接返回不可用
    assert "AFTER-AVAILABLE=" in out
