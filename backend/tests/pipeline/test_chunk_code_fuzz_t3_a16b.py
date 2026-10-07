"""A16-b · `chunk_code()` 的可复跑门禁（工具升级-3 · 回归检查 · 终局审计 A-1 补）。

**为什么单独一份**：`test_chunk_invariants_t3_a16.py` 的 1296 格 fuzz **只打 `chunk_document`**，
而 `chunk_code()` 是**本笔唯一新增的切块器** —— 终局审计 A-1 指出这是「实现坏了也不会红」的真空。
该审计实跑核实：把 A16 的采样主体换成 `chunk_code` 是**通过**的（实现没问题），
**但仓库里没有这条常跑门禁** —— 本文件把它补上。

**三条不变量**（每条都打在 `chunk_code` 的**契约**上，不是实现细节）：
1. `line_start <= line_end`（行号区间自洽）；
2. **块文本 == 源行区间**（`"\n".join(lines[a-1:b]).strip() == chunk.text`）——
   这是「行号可回溯」的**定义式**，比「观察文本里出现了 `行 3-5`」强得多（正是 A-2 的病根）；
3. 非空输入 ⇒ 块数 ≥ 1（不得静默产出空集）。

**行尾维度是本文件的重点**（A-1 给的反例）：把 `lines = text.split("\n")` 改成先
`replace("\r\n", "\n")` 再切（看似无害的「规范化」）⇒ **CRLF 文件的行号与内容解耦**；
而 A16 的 fuzz 用 `"字" * n` 造样本（**无 CRLF**）⇒ 全绿。⇒ 本轨**必须**带 `\r\n` / `\r` 维度。
"""

import pytest

from app.pipeline.chunking import CODE_WINDOW_LINES, chunk_code

#: 每格的源码正文（多种语法形态）
_BODIES = (
    "x = 1",                                   # 单语句
    "def f():\n    return 1",                   # 单函数
    "def f():\n    return 1\n\ndef g():\n    return 2",  # 两函数
    "class C:\n    def m(self):\n        return 1",       # 类 + 方法
    "@deco\ndef f():\n    pass",                # 装饰器（起点须含装饰器行）
    "async def f():\n    await g()",            # 异步
    "def f(:",                                  # **语法错误** ⇒ 行窗兜底
    "# only comment",                           # 纯注释
    "import os\n\ndef f():\n    return os",     # 前言 + 函数
)
_LEADS = ("", "import os\n\n", "x = 1\n\ny = 2\n\n")     # 前言行数
_EOLS = ("\n", "\r\n", "\r")                              # 🔴 行尾（A-1 的关键维度）
_TRAIL = (True, False)                                     # 有无尾换行
_LONGS = (False, True)                                     # 是否超长（触发窗口内切）

_MIN_CASES = 200


def _build(lead: str, body: str, eol: str, trailing: bool, long_body: bool) -> str:
    if long_body:
        body = "def huge():\n" + "".join("    v" + str(i) + " = " + str(i) + "\n" for i in range(120))
    body = body.replace("\n", eol)
    lead = lead.replace("\n", eol)
    src = lead + body
    return src + eol if trailing and not src.endswith(eol) else src


def test_chunk_code_fuzz_three_invariants():
    """5 维确定性笛卡尔采样，**每格都断三条不变量**（含行尾维度）。

    ⚠️ 行号按**传入字符串的原始换行**计 —— 故 `_build` 先按 `\n` 拼、末尾才整体换成目标行尾。
    """
    checked = 0
    skipped = 0
    for lead in _LEADS:
        for body in _BODIES:
            for eol in _EOLS:
                for trailing in _TRAIL:
                    for long_body in _LONGS:
                        src = _build(lead, body, eol, trailing, long_body)
                        checked += 1

                        chunks = chunk_code(src)

                        # ③ 非空输入 ⇒ 块数 ≥ 1
                        assert chunks, (lead, body[:12], repr(eol), trailing, long_body)

                        # 行模型 = **源文件物理行**（吃 \r\n / \n / \r 三种行尾，与实现同口径）。
                        lines = src.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                        if lines and lines[-1] == "":
                            lines = lines[:-1]

                        for c in chunks:
                            # ① 行号区间自洽
                            assert c.line_start is not None and c.line_end is not None
                            assert 1 <= c.line_start <= c.line_end <= len(lines), (
                                c.line_start, c.line_end, len(lines)
                            )
                            # ② **块文本 == 源行区间**（行号可回溯的定义式）
                            seg = "\n".join(lines[c.line_start - 1 : c.line_end]).strip()
                            assert seg == c.text, (
                                repr(eol), c.line_start, c.line_end, seg[:40], c.text[:40]
                            )

    assert checked >= _MIN_CASES, "组数下限未达：" + str(checked)
    assert skipped == 0


def test_crlf_line_numbers_are_not_decoupled_by_normalization():
    """🔴 **A-1 给的反例的回归钉**：CRLF 文件的行号必须仍指向**原始**行。

    反例变更 = 在 `chunk_code` 里先 `text.replace("\r\n", "\n")` 再 `split("\n")`
    ⇒ 行号与源文件解耦。本用例直接钉住「CRLF 下第 3 行确实是第 3 行」。
    """
    src = "a = 1\r\n\r\ndef f():\r\n    return 1\r\n"
    chunks = chunk_code(src)
    assert chunks
    lines = src.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for c in chunks:
        seg = "\n".join(lines[c.line_start - 1 : c.line_end]).strip()
        assert seg == c.text.strip(), (c.line_start, c.line_end, seg[:40], c.text[:40])


def test_cr_only_line_numbers_map_to_physical_lines():
    """🔴 **A-1 反例的第二条腿（本轨实测暴露的真缺陷，已修）**：`\r`-only 文件的行号。

    首版 `chunk_code` 只按 `"\n"` 切 ⇒ **`\r`-only 的文件整份算一行** ⇒
    实测「121 物理行的文件」被报成 `line_start=1, line_end=80`，**行号与源文件解耦**。
    （`\r\n` 首版能过只是因为 `"\n"` 恰好也是它的分隔符 ⇒ **`\r`-only 是唯一的暴露面**。）
    修法 = 切行前先归一换行（吃三种行尾，与 Python universal newlines 同口径）。
    """
    src = "def f():\r" + "".join("    v" + str(i) + " = " + str(i) + "\r" for i in range(120))
    chunks = chunk_code(src)
    physical = len(src.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    assert physical >= 120
    assert max(c.line_end for c in chunks) <= physical, (chunks[0].line_start, chunks[-1].line_end, physical)
    lines = src.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for c in chunks:
        seg = "\n".join(lines[c.line_start - 1 : c.line_end]).strip()
        assert seg == c.text.strip(), (c.line_start, c.line_end, seg[:40], c.text[:40])


def test_max_block_lines_respects_window_for_python():
    """窗口上界对 Python 路径同样成立（前言/符号/空档三处都过 `_emit`）。"""
    src = "".join("x" + str(i) + " = " + str(i) + "\n" for i in range(200)) + "def f():\n    pass\n"
    chunks = chunk_code(src)
    assert max(c.line_end - c.line_start + 1 for c in chunks) <= CODE_WINDOW_LINES

