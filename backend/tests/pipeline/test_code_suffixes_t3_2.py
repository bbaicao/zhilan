"""回归检查 · 后端白名单放开（工具升级-3 · 阶段 2 · 第 1 笔）的判据载体。

判据 A13a（本笔部分）：`.py` 不再因后缀被拒（400 / DocumentParseError）。
⚠️ **本笔不把 `index_status == "ready"` 当通过** —— 现读回落路径会把 `.py` 送进清洗与
`chunk_document`，那样的 ready 是假通过。端到端 ready 留到 `回归检查`（`D-回归检查` 改道之后）。

判据 A13b（后端侧一半）：`SUPPORTED_FORMATS_HINT` ↔ `SUPPORTED_SUFFIXES` 双向一致，
唯一允许的差异是 `.markdown`（`.md` 的别名，历来不进 hint，见包内 `D-回归检查`）。
前端三处字面量的那一半在 `回归检查` 的本轨载体里，**不在本文件**。

变异 M9 的靶子 = `test_code_like_suffixes_are_no_longer_rejected_by_suffix` 与
`test_suffix_count_is_44` 两条：
把 `SUPPORTED_SUFFIXES` 里刚加的任一后缀摘掉 ⇒ 至少一条必红（已实测：摘 `.py` 时
2 failed / 32 passed，还原后 34 passed）。
"""

from app.pipeline.parsing import (
    SUPPORTED_FORMATS_HINT,
    SUPPORTED_SUFFIXES,
    parse_document_pages,
)

#: 工具升级-3 · 回归检查（单② 定档 (c)）新增的 29 项：代码 16 / 配置 7 / 前端资源 6
CODE_SUFFIXES = (
    ".py", ".js", ".ts", ".java", ".go", ".rs", ".c", ".h", ".cpp", ".cs", ".rb", ".php",
    ".sh", ".sql", ".kt", ".swift",
)
CONFIG_SUFFIXES = (".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml")
FRONTEND_SUFFIXES = (".jsx", ".tsx", ".vue", ".css", ".scss", ".html")
NEW_SUFFIXES = CODE_SUFFIXES + CONFIG_SUFFIXES + FRONTEND_SUFFIXES


def test_suffix_count_is_44():
    """15（阶段 2/3 既有）+ 29（本笔）= 44。项数写死，防「加了一半」。"""
    assert len(NEW_SUFFIXES) == 29
    assert len(SUPPORTED_SUFFIXES) == 44


def test_every_new_suffix_is_whitelisted():
    """29 项逐项在册（不是只看总数——总数对而个别缺席也能凑出 44）。"""
    missing = sorted(s for s in NEW_SUFFIXES if s not in SUPPORTED_SUFFIXES)
    assert missing == [], missing


def test_code_like_suffixes_are_no_longer_rejected_by_suffix():
    """A13a 本笔部分：`.py` 不再因**后缀**被拒。

    ⚠️ 断言的是「不因后缀抛」，不是「解析结果正确」—— 本笔尚未改入库主路（`D-回归检查`），
    `.py` 仍会走 `_decode` 快路径。
    """
    src = "def f():\n    return 1\n"
    text, pages = parse_document_pages("a.py", src.encode("utf-8"))
    assert pages == []
    # 🔴 独立复核 F-01：原先只断 `"def f():" in text` ⇒ **不覆盖缩进** ——
    # 把解码结果逐行 strip() 后该断言仍绿，而 A13a 明写要求「缩进还在」。
    # 改为**逐字相等**（缩进 + 换行 + 末尾换行全在内）。
    assert text == src, f"缩进/换行必须逐字保留，实得 {text!r}"


def test_still_unsupported_suffix_still_rejected():
    """反向腿：白名单**没被放开成万能** —— `.exe` 仍必须 400 语义。

    没有这条，把 `if suffix not in SUPPORTED_SUFFIXES` 整段删掉也不会红。
    """
    try:
        parse_document_pages("a.exe", b"MZ")
    except Exception as exc:  # noqa: BLE001 —— 只断言「拒绝了」，具体异常类型由产品定
        assert "暂不支持" in str(exc)
        assert SUPPORTED_FORMATS_HINT in str(exc)
    else:
        raise AssertionError(".exe 必须仍被后缀闸拒绝")


def test_hint_tokens_match_suffixes_except_markdown():
    """A13b 后端侧：hint ↔ 白名单双向一致，唯一例外 = `.markdown`。

    这是既有的活常量双向钉子（`test_image_ocr.py::test_hint_text_only_mentions_whitelisted_formats`
    的同口径复算）；本笔扩容后它必须仍成立。
    """
    tokens = {f".{part.strip()}" for part in SUPPORTED_FORMATS_HINT.split("/") if part.strip()}
    assert len(tokens) == 43
    assert tokens <= SUPPORTED_SUFFIXES
    assert tokens == SUPPORTED_SUFFIXES - {".markdown"}
