"""A16 · `chunk_document` / `chunk_text` 的可复跑门禁（工具升级-3 · 回归检查）。

**为什么另起一个文件**（照 `阶段2-离线索引/设计说明` §八 与包内 `D-回归检查`）：
原来保护 `chunk_document` 的「31/31 对拍」**一条都没打在 `chunk_document` 上**
（这些样本只覆盖 parse/clean/chunk_text 等其他入口），
且那个 fuzz 是**一次性证据件、今天没有常跑用例** ⇒ 本文件是那三条不变量的**常跑载体**。

三条不变量（出处：`阶段2-离线索引/设计说明` §八，**现读原文，不是并列三条**）：
1. **无标题文本 ≡ `chunk_text`**（`:132-133`）—— 硬约束；
2. **合并跨度 ≤ `size`**（`:170-171`）—— 不可直接观测，用**块数**作等价形态；
3. **不切短小节**（`:171`）—— 用块边界反推。

⚠️ **本轨的判别力分界（`K-回归检查`，如实登记）**：第 2 条的块数断言**只杀倍数级错法**，
**杀不住 ±1 级贴边错法**（本样本的跨度与 size 相差 ≥ 2）。**本文件不声称「任何错法都被杀」。**
⚠️ **不得引入 `hypothesis` / 随机**（先例 `test_scope_intersect_t0.py:10-11` + §0.3 禁新增依赖）。
⚠️ 本文件**只读** `chunk_document` / `chunk_text`，不改它们（改动面在 `chunk_code`，见另一份载体）。
"""

import inspect

import pytest

from app.core.config import settings
from app.pipeline.chunking import chunk_document, chunk_text, normalize


def _merge_sample() -> str:
    """已知合并结果的最小样本（包内 `D-回归检查` 第 2 条逐字给出，已跑通）。

    三节各 38 字 + 标题：A|B 跨度 ≤ 100 应合并、C 单独 ⇒ **2 块**。
    """
    nl = "\n"
    return "# A" + nl + "\u7532" * 38 + nl + nl + "# B" + nl + "\u4e59" * 38 + nl + nl + "# C" + nl + "\u4e19" * 38


def test_merge_span_invariant_block_count_is_2():
    """**M11 的唯一靶子**（包内 `D-回归检查` 第 2 条逐字）：基线必须恰 2 块。

    判别力（实测读数，2026-09-25 本机现跑）：
    - `span <= size * 2`（倍数级）⇒ **31 块** ⇒ 本断言必红 ✅ **本轨实测**；
    - `span <= size + 1`（贴边级）⇒ **仍 2 块** ⇒ **打不红**（`K-回归检查` 已登记为已知限度）；
    - `span <= 0`（不合并）⇒ **3 块** ⇒ 必红。
    ⚠️ 变异后的期望块数**不是 1** —— 合并块跨度 200 > 窗口 100，会被 `_iter_splits` 再切。
    """
    chunks = chunk_document(_merge_sample(), size=100)
    assert len(chunks) == 2, [c.text[:12] for c in chunks]


def test_merge_span_anchor_is_unique():
    """变异锚点必须先校验：`_merge_blocks` 里 `span <= size` **恰好一处**。

    锚点漂了（0 处或 2 处）⇒ 上面的块数断言即使红也说明不了「合并跨度错了」。
    """
    src = inspect.getsource(chunk_document.__globals__["_merge_blocks"])
    assert src.count("if span <= size:") == 1


def _meta_texts(chunks) -> list:
    return [c.text for c in chunks]


@pytest.mark.parametrize("size", [30, 100, 800])
def test_untitled_document_equals_chunk_text(size):
    """无标题 ⇒ 两者**逐块文本**必须相同（`chunk_document` 返回 `ChunkMeta`、
    `chunk_text` 返回 `list[str]` ⇒ **先归一化再比**，照字面写 `==` 会恒假）。"""
    text = "这是一段没有标题的正文。" * 40
    assert _meta_texts(chunk_document(text, size=size)) == chunk_text(text, size=size)


def test_untitled_equivalence_holds_with_pages():
    """带 pages 也不能破坏该等价（pages 只影响 `page`，不影响块文本）。"""
    text = "无标题正文。" * 60
    pages = ["无标题正文。" * 20, "无标题正文。" * 20, "无标题正文。" * 20]
    assert _meta_texts(chunk_document(text, pages=pages, size=120)) == chunk_text(text, size=120)


def test_short_sections_are_never_split():
    """块边界只落**小节边界**或**单个超长小节内部**。

    构造：5 个小节（每节正文 20 字，远小于 size=100）⇒ 每块都必须以某个标题行**开头**。
    """
    nl = "\n"
    doc = nl.join("# S" + str(i) + nl + "字" * 20 for i in range(5))
    chunks = chunk_document(doc, size=100)
    for c in chunks:
        assert c.text.startswith("# S"), c.text[:20]
    assert len(chunks) >= 2


def test_oversized_section_is_split_internally_only():
    """单个超长小节才会在**它自己内部**被再切；其余小节不受影响。"""
    nl = "\n"
    doc = "# small" + nl + "短" * 10 + nl + nl + "# huge" + nl + "长" * 600
    chunks = chunk_document(doc, size=100)
    huge = [c for c in chunks if c.heading_path == ("huge",)]
    assert len(huge) >= 2, "超长小节必须被切开"
    small = [c for c in chunks if c.heading_path == ("small",)]
    assert len(small) == 1, "短小节不得被切"


def test_chunk_text_size_zero_falls_back_to_default_and_does_not_raise():
    """`size=0` **回退默认值、不抛错**（老基线口径，`chunking.py` 的 `size or 默认值`）。

    🔴 **样本必须长于默认块长**（`RAG_CHUNK_SIZE` 现读 = 800）——
    包内 `D-回归检查` 原用 `"abc"`，那样「返回默认 size 下的结果」半句**恒真**、无判别力
    （独立复核 B-3 指出）。本轨改用 **> 800 字**的样本，同时断「不抛」与「块数 == 默认档块数」。
    """
    assert settings.RAG_CHUNK_SIZE == 800, "默认块长变了 ⇒ 本用例的样本长度前提要重核"
    text = "字" * (settings.RAG_CHUNK_SIZE * 3)
    got = chunk_text(text, size=0)
    assert len(got) > 1
    assert got == chunk_text(text)


def test_chunk_text_negative_size_still_raises():
    """反向腿：**负数仍抛** `ValueError`（改 `or` 成 `is None` 会同时打掉这两条）。"""
    with pytest.raises(ValueError):
        chunk_text("abc", size=-1)


# 🔴 **独立复核 B-3（本版就地修）**：首版 6 维里 `_EDGES` 的后三个取值是**伪维度** ——
# 它们直接赋常量文档、**不参与 `_build`** ⇒ 384 格里 288 格落在 3 个常量文档上、192 格只断
# `chunks == []` ⇒ 「不同文档」实际只有 83 个。`assert checked >= 300` 于是**靠重复计数达标**。
# ⇒ 本版：① 让每个维度都**真换输入**（`_EDGE` 参与 `_build`）；② 新增第 7 维 `_SIZE`
# （让 `size` 与正文长度**交叉**：同一样本在小 size 下是超长节、大 size 下是短节）；
# ③ 判据加 `assert len(set(docs)) >= 300` —— **堵死靠常量重复凑 checked**。
_HEADS = ("", "# T", "# T\n## U", "# T\n## U\n### V")   # ① 有/无标题 + 多级标题
_NSEC = (1, 2, 5)                                            # ② 单/多小节
_BODY_LEN = (12, 38, 60, 90)                                 # ③ 合并/不合并（M11 靶区）
_MIX = ("short", "long", "mixed")                            # ④ 超长与短小节混排
_TAILS = ("", "\n\n\n", "\n   \n")                        # ⑤ 尾随空行 / 纯空白尾
_EDGE = ("normal", "lead_ws", "no_trailing_nl")              # ⑥ 行首空白 / 无尾换行（参与 _build）
_SIZE = (40, 100, 800)                                       # ⑦ 窗口大小 × 正文长度交叉

#: 组数下限（路线件给「300 组」量级）
_MIN_CASES = 300


def _build(head: str, nsec: int, body_len: int, mix: str, tail: str, edge: str) -> str:
    """按 6 个维度造一份**各不相同**的文档（`edge` 必须参与构造，不得是旁路常量）。"""
    nl = "\n"
    parts = []
    if head:
        parts.append(head + nl)
    for i in range(nsec):
        if mix == "long":
            ln = body_len * 12
        elif mix == "mixed":
            ln = body_len * 12 if i % 2 == 0 else body_len
        else:
            ln = body_len
        body = "字" * ln
        if edge == "lead_ws" and i == 0:
            body = "    " + body          # 行首空白：打 normalize() 的空白压缩
        parts.append("# S" + str(i) + nl + body)
        if i != nsec - 1:
            parts.append(nl)
    doc = "".join(parts) + tail
    if edge == "no_trailing_nl" and doc.endswith(nl):
        doc = doc[:-1]                     # 无尾换行：打末行 line_starts 的计算
    return doc


def test_chunk_document_fuzz_invariants_hold_on_every_cell():
    """确定性笛卡尔采样：**每格都断**，`checked` 用下限、`skipped` 精确为 0。

    ⚠️ **每一维都必须真换输入**（`_EDGE` 参与 `_build`）—— 否则 `checked` 会靠常量重复凑数
    而使「组数下限」名存实亡（独立复核 B-3 的原始缺陷）。故本用例**同时**断
    `checked >= 300` **与** `len(set(docs)) >= 300`。
    7 维积 = 4×3×4×3×3×3×3 = **1296** 格 ⇒ 两条下限都成立。
    """
    checked = 0
    skipped = 0
    docs = []
    for head in _HEADS:
        for nsec in _NSEC:
            for body_len in _BODY_LEN:
                for mix in _MIX:
                    for tail in _TAILS:
                        for edge in _EDGE:
                            for size in _SIZE:
                                doc = _build(head, nsec, body_len, mix, tail, edge)
                                docs.append(doc)
                                checked += 1
                                chunks = chunk_document(doc, size=size)
                                texts = [c.text for c in chunks]

                                assert all(t.strip() for t in texts), (head, nsec, body_len, mix, tail, edge, size)
                                assert [c.ordinal for c in chunks] == list(range(len(chunks)))
                                # 文档块不得带行号/符号（只有 chunk_code 填）
                                assert all(c.line_start is None and c.line_end is None for c in chunks)
                                assert all(c.symbol_path is None for c in chunks)
                                # U1：代码缩进属于正文；补上的结构上下文不占真实正文坐标。
                                from app.pipeline.cleaning import normalize_blank_lines
                                norm = normalize_blank_lines(doc)
                                assert all(
                                    norm[c.start_offset:c.end_offset].strip() in c.text for c in chunks
                                ), (texts, norm[:80])

    # 退化输入单独一组：**空 / 纯空白** ⇒ 两函数必须**同为 0 块**（不得只让一边 0）。
    # ⚠️ 「仅标题」**不属退化** —— 它有一行非空内容，正确行为是产出 **1 块**（本轨实测）。
    # 首版把它并进这一组是错的，已分离：退化组只收「归一化后为空」的输入。
    for degenerate in ("", "   \n\t\n  \n", "\n\n", "\t\n "):
        checked += 1
        docs.append(degenerate)
        assert normalize(degenerate) == "", repr(degenerate)
        assert chunk_document(degenerate, size=100) == [], repr(degenerate)
        assert chunk_text(degenerate, size=100) == [], repr(degenerate)

    # 「仅标题」：恰好 1 块，且文本就是那一行（打「空输入」与「单行输入」不得同码）
    heading_only = "# only heading"
    checked += 1
    docs.append(heading_only)
    only = chunk_document(heading_only, size=100)
    assert [c.text for c in only] == [heading_only]

    for body_len in _BODY_LEN:
        doc = "正文" * body_len * 6
        assert _meta_texts(chunk_document(doc, size=100)) == chunk_text(doc, size=100)

    distinct = len(set(docs))
    assert checked >= _MIN_CASES, "组数下限未达：" + str(checked) + " < " + str(_MIN_CASES)
    assert distinct >= _MIN_CASES, "不同文档数下限未达（防靠常量重复凑 checked）：" + str(distinct)
    assert skipped == 0, "本轨不跳过任何格（跳过 = 判据被悄悄放宽）"
