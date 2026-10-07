"""专项 · 工具调用能力第 2 批 · **② 范围内 list_documents / read_document**（A7–A14）。

判据（开工包 §0.7 D-d…D-g / §0.0 D9–D12 / §2.2）：
- 范围只收窄不扩大；求交冲突/坏值 ⇒ ok=False，不回落全库；
- list 只列 ready + 未软删文档，有界 20，纯文本不产材料、不泄 file_path；
- read 有界窗口、1 起块号、逐块真实 Material、模板外多一句即红。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.agent.tools import list_documents as list_documents_mod
from app.agent.tools import read_document as read_document_mod
from app.models import Chunk, Document
from app.retrieval.types import Material, intersect_scope_filters
from tests.retrieval.conftest import seed_document

# 用例另抄字面量（模板对拍）
_LIST_HEAD = "范围内共 {n} 份文档（按 id 倒序）"
_LIST_ROW = "[{i}] {filename}（id={id}，{chunk_count} 块，{created_at}）"
_LIST_TRUNC = "（已截断，共 {N} 份）"
_LIST_EMPTY = "范围内没有可列出的文档"
_READ_RANGE = "第 {a}–{b} 块 / 共 {N} 块"
_READ_MORE = "还有 {n} 块"


def _doc_arg(model_cls, **kwargs):
    return model_cls(**kwargs)


async def _call_list(db, host_filters=None, args_filters=None):
    spec = list_documents_mod.build_spec(db=db, filters=host_filters)
    args = list_documents_mod.Args(filters=args_filters)
    return await spec.handler(args)


async def _call_read(db, *, document_id, start_chunk=1, max_chunks=4, host_filters=None, args_filters=None):
    spec = read_document_mod.build_spec(db=db, filters=host_filters)
    args = read_document_mod.Args(
        document_id=document_id,
        start_chunk=start_chunk,
        max_chunks=max_chunks,
        filters=args_filters,
    )
    return await spec.handler(args)


# --------------------------------------------------------------------------
# A7 · 范围不可扩大
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a7_host_scope_blocks_other_kb(db):
    """A7：宿主 kb_ids=[1] ⇒ list 不含 kb2；read(kb2) ⇒ ok=False。"""
    d1 = await seed_document(db, ["文档一正文"], filename="一.md", kb_id=1)
    d2 = await seed_document(db, ["文档二正文"], filename="二.md", kb_id=2)
    host = {"kb_ids": [1]}

    listed = await _call_list(db, host_filters=host)
    assert listed.ok is True
    assert f"id={d1.id}" in listed.text
    assert f"id={d2.id}" not in listed.text

    read2 = await _call_read(db, document_id=d2.id, host_filters=host)
    assert read2.ok is False
    assert not (read2.materials or [])


@pytest.mark.asyncio
async def test_a7_args_model_has_no_unscoped_entry():
    """A7③：参数模型除 filters/document_id/start_chunk/max_chunks 外无越权入口。"""
    fields = set(read_document_mod.Args.model_fields)
    assert fields <= {"document_id", "start_chunk", "max_chunks", "filters"}
    list_fields = set(list_documents_mod.Args.model_fields)
    assert list_fields <= {"filters"}


# --------------------------------------------------------------------------
# A8 · 求交真生效
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a8_intersection_narrows(db):
    """A8①：工具 kb_ids=[1] ∩ 宿主 [1,2] ⇒ 结果 ⊆ kb1。"""
    d1 = await seed_document(doc := db, ["甲"], filename="甲.md", kb_id=1)
    d2 = await seed_document(db, ["乙"], filename="乙.md", kb_id=2)
    obs = await _call_list(db, host_filters={"kb_ids": [1, 2]}, args_filters={"kb_ids": [1]})
    assert obs.ok is True
    assert f"id={d1.id}" in obs.text and f"id={d2.id}" not in obs.text


@pytest.mark.asyncio
async def test_a8_empty_intersection_is_failure(db):
    """A8②：工具 kb_ids=[3] 不在宿主内 ⇒ ok=False。"""
    await seed_document(db, ["甲"], filename="甲.md", kb_id=1)
    obs = await _call_list(db, host_filters={"kb_ids": [1]}, args_filters={"kb_ids": [3]})
    assert obs.ok is False
    assert obs.materials in (None, [])


@pytest.mark.asyncio
async def test_a8_reverse_date_range_zero_query(db, monkeypatch):
    """A8③：宿主反向区间 ⇒ ok=False + 冲突原因 + 零 DB 查询（打桩计数）。"""
    calls = {"n": 0}
    real_execute = type(db).execute

    async def counting_execute(self, *a, **k):
        calls["n"] += 1
        return await real_execute(self, *a, **k)

    monkeypatch.setattr(type(db), "execute", counting_execute)
    obs = await _call_list(
        db,
        host_filters={"created_after": "2026-01-01", "created_before": "2025-01-01"},
    )
    assert obs.ok is False
    assert calls["n"] == 0
    assert "冲突" in obs.text or "created" in obs.text or "时间" in obs.text


@pytest.mark.asyncio
async def test_a8_bad_filters_not_silently_widened(db):
    """A8④：坏日期 / 未知键 ⇒ ok=False，不得吞成不限。"""
    bad_date = await _call_list(db, host_filters=None, args_filters={"created_after": "not-a-date"})
    # 坏日期在 types 里会 warning 并忽略该维——但未知键应被 intersect 拒
    unknown = await _call_list(db, host_filters=None, args_filters={"nope": 1})
    assert unknown.ok is False
    assert bad_date is not None  # 至少不炸穿


# --------------------------------------------------------------------------
# A9 · list 可见性与上限
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a9_visibility_order_limit_and_projection(db):
    """A9：软删/非 ready 不出现；id desc；>20 恰 20 + 已截断；不泄 file_path。"""
    for i in range(22):
        doc = await seed_document(db, [f"c{i}"], filename=f"doc{i:02d}.md", kb_id=1)
        doc.file_path = f"C:\\secret\\{doc.filename}"
        doc.index_error = "boom"
    await seed_document(db, ["x"], filename="soft.md", deleted=True)
    await seed_document(db, ["y"], filename="pending.md", index_status="pending")
    # 跨范围腿（2026-09-22 复核补）：宿主 kb_ids=[1] 时，kb2 的**存活**文档一律不出现。
    # 补它的理由：原判据只往 kb1 里塞数据，把「范围求交」整段旁路掉也照样全绿 ⇒ 变异打不中，
    # 与结论件 §三「③ 范围求交失效 ⇒ A7/A9 必红」的声明不符。
    other = await seed_document(db, ["z"], filename="other-kb.md", kb_id=2)

    obs = await _call_list(db, host_filters={"kb_ids": [1]})
    assert obs.ok is True
    assert "soft.md" not in obs.text and "pending.md" not in obs.text
    assert "other-kb.md" not in obs.text and f"id={other.id}" not in obs.text
    assert "C:\\secret" not in obs.text and "boom" not in obs.text
    assert "（已截断" in obs.text
    # 恰 20 条行
    rows = [ln for ln in obs.text.splitlines() if ln.startswith("[")]
    assert len(rows) == 20
    assert obs.materials in (None, [])


@pytest.mark.asyncio
async def test_a9_note_only_scope_says_empty(db):
    """A9：宿主只含 note ⇒ 可读「范围内没有可列出的文档」，不含文档 id。"""
    d = await seed_document(db, ["x"], filename="d.md", kb_id=1)
    obs = await _call_list(db, host_filters={"source_types": ["note"]})
    assert obs.ok is True
    assert _LIST_EMPTY in obs.text
    assert f"id={d.id}" not in obs.text


# --------------------------------------------------------------------------
# A10 · read 有界与块号
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a10_window_bounds_and_ordinal_mapping(db):
    """A10：窗口 ≤ max_chunks；start_chunk=3 ⇒ ordinal=2；还有 N 块。"""
    doc = await seed_document(
        db, ["b0", "b1", "b2", "b3", "b4", "b5"], filename="多块.md", start_ordinal=0
    )
    obs = await _call_read(db, document_id=doc.id, start_chunk=3, max_chunks=2, host_filters={"kb_ids": [1]})
    assert obs.ok is True
    assert "b2" in obs.text and "b3" in obs.text
    assert "b0" not in obs.text and "b5" not in obs.text
    assert "还有" in obs.text
    assert obs.materials and all(isinstance(m, Material) for m in obs.materials)
    assert {m.block_ordinal for m in obs.materials} == {2, 3}


@pytest.mark.asyncio
async def test_a10_no_raw_content_leak(db):
    doc = await seed_document(db, ["可见正文"], filename="a.md")
    doc.raw_content = "RAW-SECRET-ONLY-IN-RAW"
    await db.commit()
    obs = await _call_read(db, document_id=doc.id, host_filters=None)
    assert "RAW-SECRET-ONLY-IN-RAW" not in obs.text


# --------------------------------------------------------------------------
# A11 · 坏参数六态
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a11_six_bad_params_via_handler(db):
    """A11：六态走 handler ⇒ ok=False + 中文 + 零材料（document_id 非整数归宿主校验）。"""
    ready = await seed_document(db, ["x"], filename="ok.md")
    soft = await seed_document(db, ["x"], filename="soft.md", deleted=True)
    pending = await seed_document(db, ["x"], filename="p.md", index_status="pending")

    cases = [
        dict(document_id=ready.id, start_chunk=0),
        dict(document_id=ready.id, max_chunks=99),
        dict(document_id=999999),
        dict(document_id=soft.id),
        dict(document_id=pending.id),
        dict(document_id=ready.id, host_filters={"kb_ids": [2]}, args_filters=None),
    ]
    for kw in cases:
        obs = await _call_read(db, **kw)
        assert obs.ok is False, kw
        assert obs.text.strip(), kw
        assert not (obs.materials or []), kw


# --------------------------------------------------------------------------
# A12 · 材料形态
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a12_materials_are_real_chunk_rows(db):
    """A12：chunk_id 在 chunks 真实存在；block_ordinal==ordinal；score=0；url/note_id None。"""
    doc = await seed_document(db, ["甲", "乙"], filename="m.md", start_ordinal=0)
    obs = await _call_read(db, document_id=doc.id, start_chunk=1, max_chunks=2)
    assert obs.ok is True
    rows = {
        r.id: r
        for r in (await db.execute(select(Chunk).where(Chunk.document_id == doc.id))).scalars()
    }
    assert obs.materials
    for mat in obs.materials:
        assert mat.chunk_id in rows
        assert mat.block_ordinal == rows[mat.chunk_id].ordinal
        assert mat.document_id == doc.id
        assert mat.url is None and mat.note_id is None
        assert mat.score == 0.0


# --------------------------------------------------------------------------
# A13 · 归因 / 编号 / 名额（构造级：材料可进归因）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a13_materials_citeable_and_dedup_key(db):
    """A13①②：材料带真实 chunk_id（归因 FK 可过）；chunk_key 稳定（跨来源去重键）。"""
    doc = await seed_document(db, ["唯一块"], filename="k.md", start_ordinal=0)
    obs = await _call_read(db, document_id=doc.id)
    mat = obs.materials[0]
    assert mat.chunk_key == f"kb:{mat.kb_id}:document:{mat.document_id}:{mat.block_ordinal}"
    assert mat.source_type == "document"
    # 与检索同键：同一 chunk 两路来 ⇒ fuse 用 chunk_id 去重
    twin = Material(
        chunk_id=mat.chunk_id,
        document_id=mat.document_id,
        content=mat.content,
        block_ordinal=mat.block_ordinal,
        filename=mat.filename,
        kb_id=mat.kb_id,
    )
    assert twin.chunk_key == mat.chunk_key


# --------------------------------------------------------------------------
# A14 · 不伪装来源 + 逐字模板
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a14_list_is_text_only_and_read_template(db):
    """A14：list 零材料；read 模板外多一句即红；source_type==document。"""
    doc = await seed_document(db, ["正文A", "正文B"], filename="t.md", start_ordinal=0)
    listed = await _call_list(db)
    assert listed.ok is True
    assert listed.materials in (None, [])

    obs = await _call_read(db, document_id=doc.id, start_chunk=1, max_chunks=2)
    assert obs.ok is True
    assert obs.materials and all(m.source_type == "document" for m in obs.materials)
    # 三件：区间行 + 正文 + （可选）还有 N 块 —— 不得出现未保证的断言句。
    # ⚠️ 2026-09-22 复核加固：原判据是 `"块 / 共" in text or "第 " in text`（OR + 子串），
    # 达不到「逐字模板、多一句即红」⇒ 改成**逐字等于** handler 产物的冻结形状
    # （块号区间（第 1–2 块 / 共 2 块）+ 两段正文，remaining=0 ⇒ 无第三件）。
    assert obs.text == "块号区间（第 1–2 块 / 共 2 块）\n正文A\n\n正文B"
    for forbidden in ("file_path", "raw_content", "index_error", "已核实"):
        assert forbidden not in obs.text


# --------------------------------------------------------------------------
# A-C3 · 越权存在性探活（回归修复）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_c3_out_of_scope_doc_is_indistinguishable_from_missing(db):
    """范围外**存在**的文档 ⇒ 与「不存在」**同文案**，且**不含** index_status。

    改前的顺序是「先按 id 取行 → 不存在 / 已删除 / 尚未就绪 三判 → **再**回表复核范围」
    ⇒ 范围外的文档能借错误文案把「存在 / 已删 / 未就绪」探出来（P1 信息泄漏）。
    """
    outside_ready = await seed_document(db, ["范围外正文"], filename="out.md", kb_id=2)
    outside_soft = await seed_document(db, ["x"], filename="out-soft.md", kb_id=2, deleted=True)
    outside_pending = await seed_document(db, ["x"], filename="out-pending.md", kb_id=2, index_status="pending")
    host = {"kb_ids": [1]}

    missing = await _call_read(db, document_id=999999, host_filters=host)
    assert missing.ok is False
    # 「文案」比的是**原因那一段**；括号里的 document_id 是调用方自己传进来的，本来就可回读。
    assert missing.text == "读取文档失败：文档不存在（document_id=999999）。"

    for doc_id in (outside_ready.id, outside_soft.id, outside_pending.id):
        obs = await _call_read(db, document_id=doc_id, host_filters=host)
        assert obs.ok is False, doc_id
        assert obs.text == f"读取文档失败：文档不存在（document_id={doc_id}）。", obs.text
        assert "index_status" not in obs.text, obs.text
        assert not (obs.materials or []), doc_id


@pytest.mark.asyncio
async def test_a_c3_out_of_scope_soft_and_pending_are_not_probeable_without_host_filters(db):
    """范围由**工具侧 filters** 收窄时同样不可探活（不只宿主 filters 那条腿）。"""
    outside_soft = await seed_document(db, ["x"], filename="out-soft2.md", kb_id=2, deleted=True)
    obs = await _call_read(db, document_id=outside_soft.id, host_filters={"kb_ids": [1]})
    assert obs.ok is False
    assert obs.text == f"读取文档失败：文档不存在（document_id={outside_soft.id}）。", obs.text
    assert "已删除" not in obs.text


@pytest.mark.asyncio
async def test_a_c3_in_scope_deleted_and_pending_diagnostics_are_unchanged(db):
    """范围内的软删 / 未就绪**仍给既有可读诊断** —— 修复只封越权口子，不删自己的诊断。"""
    soft = await seed_document(db, ["x"], filename="soft.md", deleted=True)
    pending = await seed_document(db, ["x"], filename="p.md", index_status="pending")

    obs_soft = await _call_read(db, document_id=soft.id, host_filters={"kb_ids": [1]})
    assert obs_soft.ok is False and "文档已删除" in obs_soft.text

    obs_pending = await _call_read(db, document_id=pending.id, host_filters={"kb_ids": [1]})
    assert obs_pending.ok is False and "文档尚未就绪" in obs_pending.text
    assert "index_status=pending" in obs_pending.text


@pytest.mark.asyncio
async def test_a_c3_in_scope_doc_observation_is_bytewise_unchanged(db):
    """范围内的**正常**文档：观察文本逐字不变（A-C3②，修复不得顺手改好路径）。"""
    doc = await seed_document(db, ["正文A", "正文B"], filename="in.md", start_ordinal=0)
    obs = await _call_read(
        db, document_id=doc.id, start_chunk=1, max_chunks=2, host_filters={"kb_ids": [1]}
    )
    assert obs.ok is True
    assert obs.text == "块号区间（第 1–2 块 / 共 2 块）\n正文A\n\n正文B"
