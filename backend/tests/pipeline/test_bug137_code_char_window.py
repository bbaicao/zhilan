"""``回归修复`` 判据载体：``chunk_code()`` 的**字符窗**（行数 + 字符双上限）。

**缺陷**：``chunk_code`` 的窗只有「行数」（:data:`CODE_WINDOW_LINES` = 80）⇒ 单行压缩型文件
（minified JS / 单行 JSON）**整份 = 1 行**，行窗完全失效 ⇒ 1 个块 = 全文。审计探针实测：
200,009 字符的单行 ``.js`` → **1 块 / 200,009 字**；该块入库成功、embed 侧 4xx、``vectorized=false``
（**优雅失败**、不报错，所以一直没被发现）。

**修法**：给每个**行窗内部**再加一级**字符窗**（:data:`CODE_WINDOW_CHARS`，同一实现见
``chunking._char_windows``）。两级顺序不可颠倒 —— 先行窗保住「行数 ≤ 80」与重叠口径，
后字符窗把**行窗拼接总字长**收到上限内。

🔴 **口径更正（全批审计 B 的 137-1 受理后落，2026-09-28）**：字符窗的判据是
**「该行窗拼接总字长 > 4000」**，**不是**「单行长度 > 4000」 ⇒ **普通多行代码的行窗同样会被切**
（实测两态：100 行 × 54 字的普通 Python 赋值、无任何超长行，80 行行窗拼起来 4399 字 ⇒
由旧实现 **2 块 / 最大 4399 字** 变成现实现 **3 块 / 最大 3959 字**）。原写的「只有病理级长行才触发 / 正常代码的切分与加字符窗之前逐字相同」
**两句已作废**；正确口径 = 「**只有窗口拼接总字长 ≤ 4000 的行窗**，切分才与加字符窗之前逐字相同」
（由本文件第 ③ 腿与第 ⑩ 腿分别钉住「≤ 上限不受影响」与「> 上限必被切」）。

**本文件钉的十二条腿**（每条都打在契约上；清单按 1..11 编号，另有第 ⓪ 条常量登记腿）：
1. ``test_200k_single_line_js_is_char_windowed`` —— 🔴 **审计探针复跑**（判决腿）；
2. ``test_100k_single_line_json_is_char_windowed`` —— 审计探针二；
3. ``test_windows_under_cap_are_untouched_by_the_char_window`` —— 🟢 **负控**（**窗口拼接总字长
   ≤ 4000** 的正常代码 + 短单行：切分与加字符窗之前**逐字不变**；回退击杀时本腿必须仍绿。
   ⚠️ 口径已收窄 —— 「正常代码一律不受影响」是**错的**，见上面口径更正段）；
4. ``test_char_window_boundary_is_the_constant`` —— 边界判别（恰好 = 上限 ⇒ 1 块；+1 字 ⇒ 2 块）
   ⇒ 证明不是「一切皆切」的恒真断言；
5. ``test_long_line_inside_symbol_block_keeps_symbol_path`` —— 符号路径在行内片上不丢；
6. ``test_fallback_path_is_char_windowed_too`` —— 语法错误/非 Python 的兜底路径同样覆盖；
7. ``test_mixed_long_line_keeps_line_numbers_real`` —— 行号仍指向**源文件物理行**；
8. ``test_intra_line_pieces_are_exact_and_seamless`` —— 行内片精确片数 + 无缝拼接 + 严格真子串；
9. ``test_ingest_stores_only_bounded_chunks`` —— 入库面（真 DB）：落库块全部 ≤ 上限。
10. ``test_multiline_window_char_boundary`` —— **多行行窗**的字符边界（拼接总字长恰好 4000 ⇒ 1 块；
    +1 字 ⇒ 2 块，断在行边界）——137-2 补的判据缺口；
11. ``test_window_level_trigger_splits_ordinary_multiline_code`` —— 🔴 **窗口级触发判别腿**（137-1）：
    **没有任何超长行**的普通多行代码，行窗拼起来 > 4000 ⇒ 必被切（旧实现下本腿必红）。

⚠️ **口径（如实登记，不得过度声称）**：
- 字符窗上界 = 4000 **大于** ``RAG_CHUNK_SIZE``（800）⇒ 本笔**不**恢复「主命中块 ≤ 800」的既有口径
  （该口径在加字符窗之前就已被 80 行行窗打破），只把上界**从无界收到 4000**；
- **行内切片**只对**单行 > 4000 字**生效（字符窗**本身**对「行窗拼接总字长」> 4000 生效，
  多行窗口同样会被切）⇒ **行内片不携带列号**（``ChunkMeta`` 无列字段），读侧只能回溯到「第 N 行」，
  不能回溯到「第 N 行第 M 列」；
- 本文件是**进程内机制判据** ⇒ **不**声称「embed 面已不会 4xx」（embed 侧由厂家限额决定，
  本笔只把进 embed 的块大小收进上界）。
"""

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.models import Chunk, Document
from app.pipeline import ingest
from app.pipeline import chunking as _chunking
from app.pipeline.chunking import (
    CODE_WINDOW_LINES,
    CODE_WINDOW_OVERLAP,
    _window_slices,
    chunk_code,
)
from app.vector.embed import FakeEmbedding

#: 字符窗上限的**期望值**。
#:
#: ⚠️ 这里刻意用 :func:`getattr` + 默认值取，而**不**直接 ``from ... import CODE_WINDOW_CHARS``：
#: 回退击杀实验（``git apply -R``）会把该常量整个摘掉 ⇒ 直接 import 会让本文件**败在收集期**
#: （ImportError），**逐腿读数全丢**。改成防御式取之后，回退时本文件正常收集，
#: 由 ``test_char_window_constant_is_registered`` **第一条红**（钉 4000），其余各腿各自给红。
CODE_WINDOW_CHARS = getattr(_chunking, "CODE_WINDOW_CHARS", 4000)

#: 审计探针一的形状：**单行 / 无空白**的 200,009 字 ``.js``。无空白是为了「逐片拼接 == 原文」可逐字核对。
JS_200K = "var a=;" + "x;" * 100001
#: 审计探针二的形状：单行 100,009 字 ``.json``。
JSON_100K = '{"k":"' + "a" * 100001 + '"}'

#: 上限内的「长但合法」单行 Python 文件（用作符号块内的长行）
_LONG_PY = "import os\n\n\ndef big():\n    payload = \"" + "z" * 12000 + "\"\n    return payload\n"


def _physical_lines(src: str) -> list[str]:
    """按 ``chunk_code`` 同口径取**源文件物理行**（吃 ``\\r\\n`` / ``\\n`` / ``\\r`` 三种行尾）。"""
    lines = src.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return lines


# ── 0 · 常量登记腿（回退击杀时**第一条红**） ─────────────────────────────────


def test_char_window_constant_is_registered():
    """字符窗常量必须**存在**且取 **4000**（取值依据见 ``chunking.CODE_WINDOW_CHARS`` 的注释）。

    回退击杀口径：摘掉字符窗 ⇒ ``hasattr`` 先红（这条红是「常量不存在」，其余腿红是「行为错」）。
    另断「上限 > ``RAG_CHUNK_SIZE``（800）」这一**如实登记**的事实（本笔不恢复 800 口径）。
    """
    assert hasattr(_chunking, "CODE_WINDOW_CHARS"), "字符窗常量不存在 ⇒ 行窗仍是无界的"
    assert _chunking.CODE_WINDOW_CHARS == 4000
    from app.core.config import settings

    assert _chunking.CODE_WINDOW_CHARS > settings.RAG_CHUNK_SIZE  # 4000 > 800（登记项，不是缺陷）


# ── 1/2 · 审计探针复跑 ────────────────────────────────────────────────────────


def test_200k_single_line_js_is_char_windowed():
    """🔴 **回归修复 的判决腿**：200,009 字单行 ``.js`` 不得再产出 1 块 / 200,009 字。

    回退击杀口径：把 ``chunking.py`` 的字符窗摘掉（``git apply -R``）⇒ 本腿**必须红**
    （实际产出 1 块 / 200,009 字）。
    """
    assert len(JS_200K) == 200_009, len(JS_200K)
    assert "\n" not in JS_200K, "探针必须是**单行**（否则测的不是行窗失效那条路径）"
    chunks = chunk_code(JS_200K)

    # ① 块数 > 1（旧代码 = 1）
    assert len(chunks) > 1
    # ② 每块 ≤ 上限（旧代码 = 200,009）
    assert max(len(c.text) for c in chunks) <= CODE_WINDOW_CHARS
    assert not [c for c in chunks if len(c.text) > CODE_WINDOW_CHARS]
    # ③ 行号仍指向**真实行**：整份只有 1 行 ⇒ 每片都是 (1, 1)（行内片语义，见 R-4）
    assert all((c.line_start, c.line_end) == (1, 1) for c in chunks), [
        (c.line_start, c.line_end) for c in chunks
    ]
    # ④ ``ordinal`` 仍从 0 起连续
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    # ⑤ 单行超上限时才走行内切分 ⇒ 该行内片**不重叠、不丢字**：逐片拼接 == 原文
    assert "".join(c.text for c in chunks) == JS_200K


def test_100k_single_line_json_is_char_windowed():
    """审计探针二：100,009 字单行 ``.json`` 同样被字符窗切开。"""
    assert len(JSON_100K) == 100_009, len(JSON_100K)
    chunks = chunk_code(JSON_100K)
    assert len(chunks) > 1
    assert max(len(c.text) for c in chunks) <= CODE_WINDOW_CHARS
    assert all((c.line_start, c.line_end) == (1, 1) for c in chunks)


# ── 3 · 🟢 负控：**未超上限**的窗口切分逐字不变 ───────────────────────────────


def test_windows_under_cap_are_untouched_by_the_char_window():
    """🟢 **负控腿（回退击杀时本腿必须仍绿）**：**窗口拼接总字长 ≤ 4000** 的切分不得被改写。

    ⚠️ **口径已收窄**（全批审计 B 的 137-1）：原写的「正常代码一律不受影响」是**错的** ——
    窗口拼起来超 4000 的普通代码**会被切**（判别腿见文件末 ``test_window_level_trigger_...``）。
    本腿只钉「未超上限的那一半」。

    两条一起钉：
    ① 200 行正常 Python（每个行窗 ~640 字 << 4000 字）⇒ 行号区间必须与**纯行窗**（``_window_slices``
       独立算出的期望值）**逐项相等**，且「块文本 == 源行区间」这条旧定义式**仍然成立**；
    ② 短单行文件（13 字）⇒ **恰好 1 块** ⇒ 证明字符窗不是「一切皆切」。
    """
    src = "".join(f"x{i} = {i}\n" for i in range(200)) + "def f():\n    pass\n"
    chunks = chunk_code(src)

    expected = [(a + 1, b) for a, b in _window_slices(200, CODE_WINDOW_LINES, CODE_WINDOW_OVERLAP)]
    preamble = [c for c in chunks if c.symbol_path is None]
    assert [(c.line_start, c.line_end) for c in preamble] == expected, [
        (c.line_start, c.line_end) for c in preamble
    ]
    # 符号块保持一行一段（201-202）
    assert [(c.line_start, c.line_end) for c in chunks if c.symbol_path == "f"] == [(201, 202)]

    # 行边界片仍满足旧定义式（块文本 == 源行区间）—— 字符窗**没有**动它
    lines = _physical_lines(src)
    for c in chunks:
        assert "\n".join(lines[c.line_start - 1 : c.line_end]).strip() == c.text

    # ② 短单行：不许切
    tiny = chunk_code("const a = 1;\n")
    assert len(tiny) == 1
    assert tiny[0].text == "const a = 1;"
    assert (tiny[0].line_start, tiny[0].line_end) == (1, 1)


# ── 4 · 边界判别 ──────────────────────────────────────────────────────────────


def test_char_window_boundary_is_the_constant():
    """上限是 ``CODE_WINDOW_CHARS`` **本身**，不是「随便切」：恰好 = 上限 ⇒ 1 块；+1 字 ⇒ 2 块。"""
    exact = "a" * CODE_WINDOW_CHARS
    assert len(chunk_code(exact)) == 1
    assert chunk_code(exact)[0].text == exact

    over = exact + "b"
    chunks = chunk_code(over)
    assert [len(c.text) for c in chunks] == [CODE_WINDOW_CHARS, 1]
    assert "".join(c.text for c in chunks) == over


# ── 5 · 符号路径 ──────────────────────────────────────────────────────────────


def test_long_line_inside_symbol_block_keeps_symbol_path():
    """行内片**不得**丢 ``symbol_path``（长行落在顶层函数体内 ⇒ 每一片都仍属该符号块）。"""
    chunks = chunk_code(_LONG_PY)
    long_line_no = 5  # ``payload = "..."`` 那一行
    assert len(_physical_lines(_LONG_PY)[long_line_no - 1]) > CODE_WINDOW_CHARS

    inside = [c for c in chunks if c.line_start == long_line_no]
    assert len(inside) > 1, "12,000 字的长行必须被切成多片"
    assert all(c.symbol_path == "big" for c in inside), [c.symbol_path for c in inside]
    assert all((c.line_start, c.line_end) == (long_line_no, long_line_no) for c in inside)
    assert max(len(c.text) for c in chunks) <= CODE_WINDOW_CHARS
    # 符号块外的块不受影响：前言行仍是行边界片
    assert all(c.line_start != long_line_no or c.line_end == long_line_no for c in chunks)


# ── 6 · 兜底路径 ──────────────────────────────────────────────────────────────


def test_fallback_path_is_char_windowed_too():
    """语法错误（Python 解析失败 ⇒ 走 ``_code_fallback_chunks``）的长行同样被字符窗切开。"""
    src = "def broken(:\n" + "q" * 9000 + "\n"
    chunks = chunk_code(src)
    assert max(len(c.text) for c in chunks) <= CODE_WINDOW_CHARS
    long_line = [c for c in chunks if c.line_start == 2]
    assert len(long_line) > 1
    assert all(c.symbol_path is None for c in chunks)
    assert all((c.line_start, c.line_end) == (2, 2) for c in long_line)


# ── 7 · 混合文件的行号 ────────────────────────────────────────────────────────


def test_mixed_long_line_keeps_line_numbers_real():
    """长行夹在正常行之间：所有行号必须落在**真实物理行**范围内，且行边界片仍是整行拼接。"""
    src = "a = 1\nb = 2\n" + "m" * 5000 + "\nc = 3\nd = 4"
    lines = _physical_lines(src)
    assert len(lines) == 5

    chunks = chunk_code(src)
    assert max(len(c.text) for c in chunks) <= CODE_WINDOW_CHARS
    for c in chunks:
        assert 1 <= c.line_start <= c.line_end <= len(lines), (c.line_start, c.line_end)
        if c.line_start != c.line_end or c.line_end != 3:
            # 行边界片：文本必须等于源行区间
            assert "\n".join(lines[c.line_start - 1 : c.line_end]).strip() == c.text
        else:
            # 行内片：文本必须是第 3 行的连续子串
            assert c.text in lines[2]
    pieces_of_3 = [c for c in chunks if (c.line_start, c.line_end) == (3, 3)]
    assert len(pieces_of_3) == -(-len(lines[2]) // CODE_WINDOW_CHARS)
    assert "".join(c.text for c in pieces_of_3) == lines[2]


# ── 8 · 行内片精确性 ──────────────────────────────────────────────────────────


def test_intra_line_pieces_are_exact_and_seamless():
    """行内片：**严格真子串**（证明真被切开）+ 无缝拼接 + 精确片数（向上取整）。"""
    chunks = chunk_code(JS_200K)
    sizes = [len(c.text) for c in chunks]
    expected_count = -(-len(JS_200K) // CODE_WINDOW_CHARS)
    assert sizes == [CODE_WINDOW_CHARS] * (expected_count - 1) + [
        len(JS_200K) - CODE_WINDOW_CHARS * (expected_count - 1)
    ]
    assert all(len(c.text) < len(JS_200K) for c in chunks), "任一片 == 原文 ⇒ 根本没切"
    assert all(c.text in JS_200K for c in chunks)


# ── 9 · 入库面（真 DB） ───────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def _clean_documents(db):
    """照 ``tests/pipeline/test_code_lines_t3_2_4.py:38-44`` 的写法（conftest 的清理序不含这两张表）。"""
    await db.execute(text("DELETE FROM chunks"))
    await db.execute(text("DELETE FROM documents"))
    await db.commit()
    yield


@pytest.fixture
def _isolate_vector(monkeypatch):
    """隔离真 embedding / 真 Qdrant：``embed`` 抛错 ⇒ ``vectorized=false``（缺陷报告里的那条优雅失败路径）。"""

    class _NoEmbed(FakeEmbedding):
        async def embed_texts(self, texts):
            raise RuntimeError("测试隔离：不调用真实 embedding")

    class _NoStore:
        def ensure_collection(self, dim):
            raise RuntimeError("测试隔离：不连 Qdrant")

    monkeypatch.setattr(ingest, "get_embedding_client", lambda **kwargs: _NoEmbed(dim=4))
    monkeypatch.setattr(ingest, "get_vector_store", lambda **kwargs: _NoStore())


@pytest.mark.asyncio
async def test_ingest_stores_only_bounded_chunks(db, fresh_db, _clean_documents, _isolate_vector):
    """入库面：200,009 字的单行 ``.js`` 落库后**每个块都 ≤ 上限**（这是缺陷的下游症状面）。"""
    result = await ingest.ingest_document(db, "minified.js", JS_200K.encode("utf-8"))

    rows = (
        await fresh_db.execute(select(Chunk.content, Chunk.line_start, Chunk.line_end).order_by(Chunk.ordinal))
    ).all()
    assert len(rows) > 1, "旧代码在库里是 1 行（= 200,009 字）"
    assert max(len(r[0]) for r in rows) <= CODE_WINDOW_CHARS
    assert all((r[1], r[2]) == (1, 1) for r in rows), [(r[1], r[2]) for r in rows]

    # 缺陷报告里的下游症状：embed 失败只记 vectorized=false，文档仍 ready（本腿钉住它没被本笔改义）
    doc = (await fresh_db.execute(select(Document).where(Document.id == result.document_id))).scalar_one()
    assert doc.status == "ready"
    assert doc.vectorized is False


# ── 10 · 多行行窗的字符边界（137-2 补的判据缺口） ─────────────────────────────


def test_multiline_window_char_boundary():
    """**多行行窗的字符边界**：拼接总字长恰好 = 上限 ⇒ 1 块；+1 字 ⇒ 2 块（断在行边界）。

    样本刻意用**两行普通文本**（**没有任何一行超上限**）⇒ 本腿判的正是「行窗拼接总字长」这条判据，
    与「单行超长」那条路径无关。样本走**兜底路径**（两行裸文本，``ast.parse`` 失败）。
    回退击杀口径：旧实现（只有行窗）在 4001 字样本上仍是 **1 块** ⇒ 本腿必红。
    """
    head = "x" * 2000
    exact = head + "\n" + "y" * 1999          # 2000 + 1 + 1999 = 4000
    over = head + "\n" + "y" * 2000           # 4001
    assert len(exact) == CODE_WINDOW_CHARS, len(exact)
    assert len(over) == CODE_WINDOW_CHARS + 1, len(over)

    one = chunk_code(exact)
    assert len(one) == 1, [len(c.text) for c in one]
    assert one[0].text == exact
    assert (one[0].line_start, one[0].line_end) == (1, 2)

    two = chunk_code(over)
    assert [len(c.text) for c in two] == [2000, 2000]
    assert [(c.line_start, c.line_end) for c in two] == [(1, 1), (2, 2)]


# ── 11 · 窗口级触发（137-1 的判别力所在） ─────────────────────────────────────


def test_window_level_trigger_splits_ordinary_multiline_code():
    """🔴 **窗口级触发的判别腿**：**没有任何超长行**的普通多行代码，只要某个行窗拼起来 > 4000 字，
    就必须被切开 —— 字符窗**不是**只对「病理级长行」生效。

    样本 = 100 行合法 Python 赋值（每行 **54** 字，**无一行超上限**；行窗 80 行拼起来 **4399** 字）；
    走 ``_emit`` 的「前言 / 无符号」支路（``ast.parse`` 成功但无顶层符号）。

    实测读数（``CODE_WINDOW_CHARS``=4000 / ``CODE_WINDOW_LINES``=80 / 重叠 10）：
    **旧实现**（回退击杀轮实测）= **2 块 / 最大 4399 字**（= 纯行窗产物，见击杀日志）；
    **现实现** = **3 块 / 最大 3959 字**。
    回退击杀口径：旧实现下「块数 > 行窗数」「最大块 ≤ 上限」「精确片长表」三条**必红**。
    """
    lines_src = [f'v{i:03d} = "' + "a" * 45 + '"' for i in range(100)]
    src = "\n".join(lines_src)
    assert all(len(line) == 54 for line in lines_src)
    assert all(len(line) <= CODE_WINDOW_CHARS for line in lines_src), "本腿的样本里不许有超长行"

    row_windows = _window_slices(100, CODE_WINDOW_LINES, CODE_WINDOW_OVERLAP)
    assert len(row_windows) == 2, row_windows
    # 行窗 1 拼起来 = 80*54 + 79 = 4399 > 4000 ⇒ 必然触发第二级
    assert 80 * 54 + 79 == 4399 > CODE_WINDOW_CHARS

    chunks = chunk_code(src)
    assert len(chunks) > len(row_windows), [len(c.text) for c in chunks]
    assert max(len(c.text) for c in chunks) <= CODE_WINDOW_CHARS
    assert [len(c.text) for c in chunks] == [3959, 439, 1649], [len(c.text) for c in chunks]
    # 被切出来的都是**行边界片** ⇒ 「块文本 == 源行区间」这条旧定义式仍然成立
    src_lines = src.split("\n")
    for c in chunks:
        assert "\n".join(src_lines[c.line_start - 1 : c.line_end]).strip() == c.text
        assert c.symbol_path is None
