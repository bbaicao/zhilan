"""回归检查 · 行号落库 + 读侧可回溯 + A8 前置腿（工具升级-3 · 阶段 2 · 第 3 笔）的判据载体。

三条判据（包 §3）：
- **A14**：只从 `read_document` 的**观察文本**读 —— 有行号的块，正文前有 `行 {a}-{b}`，且与源文件一致；
- **A8 前置腿**：入库后 `select(Document.source_encoding)` 读得到探测到的名字（**不是** `read_document`）；
- **K-回归检查 / K-回归检查 两条「静默丢列」面**：写侧（`_persist_chunks`）与读侧（`read_document` 的
  `select` + 元组解包）**都**必须同笔扩，任一处漏 = 不报错但读不出。

⚠️ **本文件不声称 A8 整条已过** —— A8 的完整断言（UTF-8 无 BOM 写回仍无 BOM / CRLF 仍 CRLF /
GBK 仍 GBK）要**写回**，写回在**阶段 5**。本文件只覆盖「编解码名可携带到文档行」。
⚠️ A14 的专门变异 = 把 `line_start` / `line_end` 从 `read_document` 的 `select` 里摘掉
⇒ 本文件的 `test_line_numbers_survive_the_read_path` 必须变红（包 §4 逐字要求，非可选）。
"""

import re

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.agent.tools.read_document import ReadDocumentArgs, build_spec
from app.models import Chunk, Document
from app.pipeline import ingest
from app.pipeline.chunking import CODE_WINDOW_OVERLAP, chunk_code
from app.vector.embed import FakeEmbedding


async def _read(db, document_id: int):
    """经**真实工具装配**读一次（不是直接调 SQL）—— A14 要求「经检索链路读得出」。"""
    spec = build_spec(db=db)
    args = ReadDocumentArgs(document_id=document_id, start_chunk=1, max_chunks=8)
    return await spec.handler(args)


# ⚠️ 本文件在 tests/pipeline/ 下 —— 而**隔离夹具只存在于 tests/api/conftest.py**。
# 不加这两个 autouse 夹具，本文件会去连真 embedding / 真 Qdrant，
# 且 documents/chunks 不清 ⇒ **单跑绿、合跑红**（本笔实测踩到）。
# 照抄 tests/pipeline/test_ingest.py:21-45 的写法。
@pytest_asyncio.fixture(autouse=True)
async def _clean_documents(db):
    """每个用例前清 documents/chunks（conftest 的 _CLEAN_ORDER 不含这两张表）。"""
    await db.execute(text("DELETE FROM chunks"))
    await db.execute(text("DELETE FROM documents"))
    await db.commit()
    yield


@pytest.fixture(autouse=True)
def _isolate_vector(monkeypatch):
    """隔离真 embedding / 真 Qdrant（index_chunks 会走真实路径）。"""

    class _NoEmbed(FakeEmbedding):
        async def embed_texts(self, texts):
            raise RuntimeError("测试隔离：不调用真实 embedding")

    class _NoStore:
        def ensure_collection(self, dim):
            raise RuntimeError("测试隔离：不连 Qdrant")

    monkeypatch.setattr(ingest, "get_embedding_client", lambda **kwargs: _NoEmbed(dim=4))
    monkeypatch.setattr(ingest, "get_vector_store", lambda **kwargs: _NoStore())

#: 带缩进、带注释、带两个符号的源码（行号可手算）
SRC = "import os\n\ndef alpha():\n    x = 1\n    return x\n\n\nclass Beta:\n    def m(self):\n        return 2\n\ntail = 3\n"


def test_windowed_blocks_may_overlap_by_design():
    """🔴 **独立复核 R-1 正向钉**：行窗重叠 ⇒ 同一符号切出的两块**行号真重叠**，这是**刻意的**。

    为什么要把它钉成「正确行为」而不是留白：A14 若写成「块间行号互不相交」或「并集等于全文」，
    就会**对一个正确的实现假红**，然后有人会去「修」一个不是 bug 的东西（K-回归检查 同型）。
    本钉把口径写死：**每块自成连续闭区间**（这才是判据该断的）。
    """
    nl = "\n"
    src = "def big():" + nl + "".join("    v" + str(i) + " = " + str(i) + nl for i in range(120))
    chunks = chunk_code(src)
    with_lines = [c for c in chunks if c.line_start is not None]
    assert len(with_lines) >= 2
    # 每块自成连续闭区间
    for c in with_lines:
        assert c.line_start <= c.line_end
    # 相邻块**允许**重叠（本样本实测重叠 = CODE_WINDOW_OVERLAP 行）
    spans = [(c.line_start, c.line_end) for c in with_lines]
    overlap = spans[0][1] - spans[1][0] + 1
    assert overlap == CODE_WINDOW_OVERLAP, spans
    # 行号总数**大于**文件行数 = 重叠存在的直接证据（不是「并集 == 文件」）
    total_lines = len(src.rstrip(nl).split(nl))
    assert sum(b - a + 1 for a, b in spans) > total_lines


@pytest.mark.asyncio
async def test_code_ingest_records_line_numbers_and_symbol(db, fresh_db):
    """写侧（K-回归检查）：行号/符号**真落库**（换一个会话也读得到）。"""
    await ingest.ingest_document(db, "sample.py", SRC.encode("utf-8"))

    rows = (
        await fresh_db.execute(select(Chunk.line_start, Chunk.line_end, Chunk.symbol_path).order_by(Chunk.ordinal))
    ).all()
    assert rows, "必须落出分块"
    with_lines = [r for r in rows if r[0] is not None]
    assert with_lines, "代码块必须有行号（否则是 K-T2-4 的静默丢列）"
    # alpha 在源文件第 3-5 行（1 起闭区间）
    assert (3, 5, "alpha") in [tuple(r) for r in rows]
    # 文档块（非代码）的三个字段恒为 NULL
    assert all(r[2] is None for r in rows if r[0] is None)


@pytest.mark.asyncio
async def test_document_suffix_keeps_null_line_columns(db, fresh_db):
    """反向腿：**文档后缀**（.md）的块行号必须仍为 NULL（不得被顺手填上）。"""
    body = "# 标题\n\n正文内容。\n"
    await ingest.ingest_document(db, "doc.md", body.encode("utf-8"))
    rows = (await fresh_db.execute(select(Chunk.line_start, Chunk.line_end, Chunk.symbol_path))).all()
    assert rows
    assert all(r == (None, None, None) for r in rows), rows


@pytest.mark.asyncio
async def test_line_numbers_survive_the_read_path(db, fresh_db):
    """**A14 的靶子**（读侧 K-回归检查）：行号必须能**经 read_document 读出**。

    判别力（包 §4 逐字要求）：把 `Chunk.line_start` / `Chunk.line_end` 从 `read_document` 的
    `select(...)` 里摘掉 ⇒ 本用例**必须红**。
    """
    result = await ingest.ingest_document(db, "sample.py", SRC.encode("utf-8"))
    obs = await _read(db, result.document_id)
    assert obs.ok is True, obs.text
    found = re.findall(r"行 (\d+)-(\d+)", obs.text)
    # alpha 在源文件第 3-5 行（1 起闭区间）
    assert ("3", "5") in found, f"观察文本里没有 3-5：{found}；正文={obs.text[:300]}"

    # 🔴 **终局审计 A-2（本版就地修）**：上面那条**不建立「行号 ↔ 源文件」的绑定** ——
    # 它只证明「观察文本里出现了字符串 `行 3-5`」。反例：行号改由 ordinal 映射表产出，
    # 只要 alpha 块仍输出 3/5 就全绿，而**其它块可以整体错位**无人检出。
    # ⇒ 本轨改为**逐块核对**：对读出的每一个 `行 a-b`，其**源文件行区间**必须等于
    #    该块正文（截断块按前缀断 —— OBSERVATION_BODY_LIMIT 会截断尾部）。
    src_lines = SRC.split("\n")
    if src_lines and src_lines[-1] == "":
        src_lines = src_lines[:-1]
    body = obs.text.split("块号区间", 1)[-1]
    # 按「行 a-b」切段：每段的行号 + 紧随其后的正文（到下一个行号标记或截断标记为止）
    pieces = re.split(r"行 (\d+)-(\d+)\n", body)
    assert len(pieces) >= 4, body[:400]          # 至少 1 块（前缀 + 3 段）
    checked_blocks = 0
    for i in range(1, len(pieces) - 2, 3):
        a, b, text = int(pieces[i]), int(pieces[i + 1]), pieces[i + 2]
        assert 1 <= a <= b <= len(src_lines), (a, b, len(src_lines))
        seg = "\n".join(src_lines[a - 1 : b]).strip()
        got = text.strip()
        if got.endswith("）") and "（已截断" in got:      # 截断块按前缀断
            got = got.split("（已截断", 1)[0].strip()
            assert seg.startswith(got[:200]), (a, b, seg[:80], got[:80])
        else:
            assert seg == got, (a, b, seg[:120], got[:120])
        checked_blocks += 1
    assert checked_blocks >= 1, "必须至少逐块核对成功一次"
    assert checked_blocks == len(found), (checked_blocks, found)


@pytest.mark.asyncio
async def test_document_blocks_have_no_line_prefix_in_read_path(db, fresh_db):
    """反向腿：文档块两列为 NULL ⇒ 正文前**不得**出现「行 a-b」（既有观察文本零变化）。"""
    body = "# 标题\n\n正文内容。\n"
    result = await ingest.ingest_document(db, "doc.md", body.encode("utf-8"))
    obs = await _read(db, result.document_id)
    assert obs.ok is True, obs.text
    assert "行 " not in obs.text, obs.text[:200]


@pytest.mark.asyncio
async def test_source_encoding_is_persisted_for_code(db, fresh_db):
    """**A8 前置腿**：编解码名落 `documents.source_encoding`（直接 select 该列，不走 read_document）。"""
    await ingest.ingest_document(db, "enc.py", SRC.encode("utf-8"))
    row = (await fresh_db.execute(select(Document.source_encoding))).scalar_one()
    assert row == "utf-8"


@pytest.mark.asyncio
async def test_source_encoding_detects_gb18030(db, fresh_db):
    """GBK 腿：`gb18030` 探得到并落库（阶段 5 的写回要靠它）。"""
    body = "# 中文注释\nx = 1\n"
    await ingest.ingest_document(db, "gbk.py", body.encode("gb18030"))
    row = (await fresh_db.execute(select(Document.source_encoding))).scalar_one()
    assert row == "gb18030"
    # 中文必须真解出来（不是乱码）
    content = (await fresh_db.execute(select(Chunk.content))).scalars().all()
    assert any("中文注释" in c for c in content), content


@pytest.mark.asyncio
async def test_document_suffix_has_no_source_encoding(db, fresh_db):
    """反向腿：文档后缀不产生编码名口径 ⇒ `source_encoding` 必须仍为 NULL。"""
    await ingest.ingest_document(db, "doc.md", "# T\n\n正文。\n".encode("utf-8"))
    assert (await fresh_db.execute(select(Document.source_encoding))).scalar_one() is None


@pytest.mark.asyncio
async def test_code_ingest_keeps_indentation_in_db(db, fresh_db):
    """入库后的块文本必须保留缩进（不得被清洗剥掉）—— A13a 的落库腿。"""
    await ingest.ingest_document(db, "sample.py", SRC.encode("utf-8"))
    content = (await fresh_db.execute(select(Chunk.content))).scalars().all()
    assert any("    x = 1" in c for c in content), content
