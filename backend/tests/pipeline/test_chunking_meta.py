"""chunk_document 规格测试（条目 2.2.1）：元数据 / 标题层级 / 页码 / 偏移 / 不退化老基线。

对照条目 2.2 交付物 1) 的 6 条规格：
1) 保留标题层级；2) 无标题文本与 chunk_text 逐字相同；3) 元数据（ordinal / offset）；
4) page_number；5) size<=0 / 空 / overlap 夹紧；6) 超长无标题段落复用同一套切分。

**小节合并语义**（2026-09-17 补漏）：相邻小节的合并跨度仍 <= ``size`` 时合并成一块，
避免 PPT/表格/大纲这类「节多而短」的文档产出大量碎块。因此：
- **路径链计算**（同级替换、缺祖先成根、不累积）在 ``_split_blocks`` 层单测——它是路径
  语义的唯一真相源，且不受合并影响；
- **合并行为**在 ``chunk_document`` 层单测（小块合并 / 大节不合并 / 边界不切断小节）。
这样既守住原有回归（曾出现同级标题累积成父子链的 bug），又如实覆盖合并后的可观察行为。
"""

import pytest

from app.pipeline.chunking import (
    ChunkMeta,
    _split_blocks,
    chunk_document,
    chunk_text,
    normalize,
)


def _paths(md: str) -> list[tuple[str, ...]]:
    """取 ``_split_blocks`` 各小节的 heading_path（绕过合并，专测路径语义）。"""
    blocks, _lines, _starts = _split_blocks(normalize(md))
    return [path for _a, _b, path in blocks]


# ---------------------------------------------------------------------------
# 规格 1a：标题层级链计算（块不跨标题，标题行留在块内，heading_path 正确）
# ---------------------------------------------------------------------------


def test_three_level_heading_paths():
    md = "# 一级\n内容甲。\n\n## 二级\n内容乙。\n\n### 三级\n内容丙。"
    assert _paths(md) == [("一级",), ("一级", "二级"), ("一级", "二级", "三级")]
    # chunk_document 合并后仍以「首节路径」标注该块，被并入的小节标题留在块文本里
    chunks = chunk_document(md)
    assert chunks[0].heading_path == ("一级",)
    assert chunks[0].text.startswith("# 一级")
    assert "## 二级" in chunks[0].text and "### 三级" in chunks[0].text


def test_block_does_not_cross_heading():
    """大节（跨度 > size）之间不合并：任意块内不会出现「属于别的章节」的标题行。"""
    md = "# A\n长内容 " + "句。 " * 50 + "\n\n# B\n另一节内容 " + "句。 " * 50
    chunks = chunk_document(md, size=120, overlap=10)
    for ch in chunks:
        heading_lines = [ln for ln in ch.text.splitlines() if ln.startswith("#")]
        # 一个块最多含其自身小节的一个标题（块首），不会出现另一个不同层级的标题
        assert len(heading_lines) <= 1, ch.text


def test_office_heading_markers_recognized():
    """docx/xlsx/pptx 经 parsing 统一成 ``# 标题`` / ``## 幻灯片 i`` / ``## Sheet 名``，
    这套识别天然覆盖 office 文本。同级标题（如连续 ``## 幻灯片 i``）必须互相替换，
    不能累积成父子链——否则 100 页 PPT 的路径会膨胀到 100 项且父子关系全错。"""
    office = "## 幻灯片 1\n首页正文。\n\n## 幻灯片 2\n次页正文。"
    # 路径层：同为二级标题 → 幻灯片 2 替换幻灯片 1，而非累积
    assert _paths(office) == [("幻灯片 1",), ("幻灯片 2",)]


def test_consecutive_sibling_headings_do_not_accumulate():
    """缺祖先层的连续同级标题：每个小节路径仅含自身，绝不累积父子链。

    回归：旧实现把第二个同级标题当成第一个同级标题的子节点，100 页 PPT 最后一个
    小节的 heading_path 会膨胀到 100 项且父子关系全错。
    """
    md = "## 幻灯片 1\n正文一。\n\n## 幻灯片 2\n正文二。\n\n## 幻灯片 3\n正文三。"
    assert _paths(md) == [("幻灯片 1",), ("幻灯片 2",), ("幻灯片 3",)]


def test_sibling_replaces_within_ancestor():
    """``# A`` → ``## B1`` → ``### C`` → ``## B2``：B2 的路径是 (A, B2)。"""
    md = "# A\n甲。\n\n## B1\n乙。\n\n### C\n丙。\n\n## B2\n丁。"
    assert _paths(md) == [("A",), ("A", "B1"), ("A", "B1", "C"), ("A", "B2")]


def test_missing_ancestor_sibling_is_own_root():
    """``## X`` → ``### Y``（缺 1 级祖先）→ ``## Z``：Y 路径 (X, Y)，Z 路径 (Z,)。"""
    md = "## X\n甲。\n\n### Y\n乙。\n\n## Z\n丙。"
    assert _paths(md) == [("X",), ("X", "Y"), ("Z",)]


# ---------------------------------------------------------------------------
# 规格 1b：小节合并（小块合到一起；大节互不干扰；边界不切断小节）
# ---------------------------------------------------------------------------


def test_small_sections_merge_into_one_chunk():
    """节多而短（PPT/表格/大纲）→ 合并，不再一节一块地碎掉。"""
    ppt = "\n\n".join(f"## 幻灯片 {i}\n第 {i} 页要点说明。" for i in range(1, 41))
    chunks = chunk_document(ppt)
    assert len(chunks) < 10, f"40 页 PPT 合并后不应仍接近 40 块：{len(chunks)}"
    # 合并块以首节路径标注；被并入的小节标题仍留在块文本内（检索可见）
    joined = "\n".join(c.text for c in chunks)
    for i in range(1, 41):
        assert f"## 幻灯片 {i}" in joined
    assert all(c.heading_path for c in chunks)


def test_large_sections_are_not_merged():
    """每节都够大时行为与逐节切块一致：块数 = 小节数，路径逐节正确。"""
    md = "".join(
        f"## 第{i}章\n" + "这是该章的正文内容，用于填充到较长篇幅。" * 25 + "\n\n"
        for i in range(1, 6)
    )
    chunks = chunk_document(md)
    assert [c.heading_path for c in chunks] == [(f"第{i}章",) for i in range(1, 6)]


def test_merge_never_cuts_a_section_in_half():
    """合并只发生在小节边界：块边界要么落在大节内部（该节超长被再切），
    要么落在小节之间；一个短小节的内容不会被拆到两个块里。"""
    md = "\n\n".join(f"## 小节{i}\n第 {i} 节正文。" for i in range(1, 21))
    size = 200
    chunks = chunk_document(md, size=size, overlap=0)
    # 每个小节的标题行必须**整行**落在同一个块里（不用子串匹配：`## 小节1` 是
    # `## 小节10` 的子串，会误判成被拆）
    for i in range(1, 21):
        title = f"## 小节{i}"
        holder = [c for c in chunks if title in c.text.splitlines()]
        assert len(holder) == 1, f"小节 {i} 被拆到了多个块"
        assert f"第 {i} 节正文。" in holder[0].text
    # 且每块跨度不超过 size（未触发再切分）
    assert all(len(c.text) <= size for c in chunks)



# ---------------------------------------------------------------------------
# 规格 2：无标题文本与 chunk_text 逐字相同（≥6 参数组）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,size,overlap",
    [
        ("".join(f"第{i}句。" for i in range(50)), None, None),       # 默认参数
        ("短文本没有标题。", 800, 100),
        ("a" * 100 + "\n" + "b" * 100, 80, 10),                       # 含换行
        ("中文段落。" * 40 + "English sentence. " * 40, 200, 50),     # 中英混排
        ("无句读的长串xxxxxxx" * 30, 100, 0),                          # 无句读、无重叠
        ("句一。句二！句三？句四；句五。" * 25, 300, 120),            # 多标点混合
        ("".join(f"编号{i}：内容。" for i in range(20)), 50, 5),      # 小块高重叠
    ],
)
def test_no_heading_matches_chunk_text(text, size, overlap):
    kwargs = {}
    if size is not None:
        kwargs["size"] = size
    if overlap is not None:
        kwargs["overlap"] = overlap
    assert [c.text for c in chunk_document(text, **kwargs)] == chunk_text(text, **kwargs)


# ---------------------------------------------------------------------------
# 规格 3：元数据（ordinal 连续；offset 指向归一化全文且可还原 text）
# ---------------------------------------------------------------------------


def test_metadata_ordinal_and_offsets():
    text = "# 章\n" + "内容 " * 300
    chunks = chunk_document(text, size=150, overlap=20)
    normalized = normalize(text)
    ordinals = [c.ordinal for c in chunks]
    assert ordinals == list(range(len(chunks)))
    for ch in chunks:
        assert isinstance(ch, ChunkMeta)
        assert 0 <= ch.start_offset < ch.end_offset <= len(normalized)
        # 偏移可用的最低要求：normalized[start:end] 还原出该块文本
        assert normalized[ch.start_offset : ch.end_offset].strip() == ch.text


# ---------------------------------------------------------------------------
# 规格 4：page_number（仅 PDF 给；找不到诚实给 None；可跨页取起始页）
# ---------------------------------------------------------------------------


def test_pdf_pages_assigned(pdf_two_pages):
    from app.pipeline.parsing import parse_document, parse_document_pages

    text = parse_document("x.pdf", pdf_two_pages)
    _full, pages = parse_document_pages("x.pdf", pdf_two_pages)
    chunks = chunk_document(text, pages=pages, size=30, overlap=0)
    assert chunks[0].page == 1
    assert any(c.page == 2 for c in chunks)
    # 含次页唯一标记 SECONDPAGE 的块，其起始页应是 2
    second = next(c for c in chunks if "SECONDPAGE" in c.text)
    assert second.page == 2


def test_no_pages_means_none():
    text = "纯文本没有页边界。\n\n第二段。"
    chunks = chunk_document(text, pages=None)
    assert all(c.page is None for c in chunks)
    chunks2 = chunk_document(text, pages=[])
    assert all(c.page is None for c in chunks2)


# ---------------------------------------------------------------------------
# 规格 5：size<=0 / 空文本 / overlap 夹紧
# ---------------------------------------------------------------------------


def test_size_zero_raises():
    with pytest.raises(ValueError):
        chunk_document("任意文本", size=0)


def test_empty_text_returns_empty():
    assert chunk_document("   \n  ") == []
    assert chunk_document("") == []


def test_overlap_clamped_within_range():
    """overlap 夹在 [0, size-1]，不得抛错；复用同一内部切分。"""
    chunks = chunk_document("数据 " * 100, size=10, overlap=999)
    assert len(chunks) > 1
    assert all(0 <= (c.end_offset - c.start_offset) for c in chunks)


# ---------------------------------------------------------------------------
# 规格 6：超长无标题段落仍按 size + _BREAKS 切（复用 _iter_splits）
# ---------------------------------------------------------------------------


def test_long_paragraph_split_by_breaks():
    long = "超长段落没有标题但有很多句读。" * 120
    chunks = chunk_document(long, size=200, overlap=30)
    assert len(chunks) > 1
    # 与 chunk_text 逐字相同（无标题 → 必然等价）
    assert [c.text for c in chunks] == chunk_text(long, size=200, overlap=30)


def test_long_heading_section_split_keeps_same_path():
    """一个超长小节被 size 再切，所有子块 heading_path 相同，标题留在首块。"""
    md = "## 长小节\n" + ("这是一段很长的内容用于验证再切块。" * 200)
    chunks = chunk_document(md, size=200, overlap=20)
    assert len(chunks) > 1
    assert all(c.heading_path == ("长小节",) for c in chunks)
    assert chunks[0].text.startswith("## 长小节")
