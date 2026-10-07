"""专项 F6-B（``回归修复``）用例：**关联扩展**不得绕过本轮有效范围。

## 这条缺陷是什么

``retrieve()`` 把 ``filters`` 交给关键词路 / 向量路 / 图通道，**唯独没交给尾部的一跳扩展**
（``link.expand_materials``）。于是「主命中沿 ``wiki_links`` 多拉一跳」这条路会把**本轮范围
之外**的资料以 ``[n]`` 的形式喂给模型——用户设的范围在最后一步被静默绕过。

## 为什么本文件必须存在（E6 的实证）

全仓**没有**「非空 ``filters`` × 真有 wiki 边 × 走 ``retrieve()``」的用例：
现有带非空 ``filters`` 的用例都不造边，造边的用例都不传 ``filters``。
⇒「``filters`` 非空就整个不扩展」这种**退化解法**现有套件全绿也抓不到。
所以本文件每条越界断言旁边都有一条**正控**：「该扩的仍然扩」（E7 / B8）。

## 用例与断言的对应

| 用例 | 断言 |
|---|---|
| ``test_b1_note_*`` / ``test_b1_document_*`` | **B1 / B2**（kb / 分类 / 标签 / 日期四维，笔记与文档**两个域都覆盖**） |
| ``test_b2_dirty_cross_kb_edge_*`` | **B2**（坏边跨库不能靠 ``seed.kb_id`` 伪装；**真的造了一条脏跨库边**） |
| ``test_b2_source_types_*`` | **B2**（``source_types`` 限制不能被扩展绕过） |
| ``test_b3_no_scope_control_*`` | **B3 / B-a**（无范围正控：``filters=None`` 时逐条不变，含范围外目标照样扩） |
| ``test_b4_retrieve_*`` | **B4**（从真实 ``retrieve()`` 入口验证接线；**M1 调用点变异的检出器**） |
| ``test_b_edge_query_still_runs_*`` | **B5 / M2**（复核必须在回表之后：非空范围也照发边查询 ⇒ 既有降级路径仍被覆盖） |
| ``test_b_rejected_targets_are_skipped_silently`` | **§2.5**（被范围拒**不算失败**：不刷日志、不加事件） |
| ``test_b_switch_off_*`` | **E11**（``RAG_LINK_EXPAND`` 两态都显式钉） |

**本文件不连真服务也不依赖本机 ``.env``**：``tests/retrieval/`` 没有 conftest 级 autouse 钉子
⇒ 两把开关与出网点都在 :func:`_pin_defaults` 里显式钉死（E11 / E12）。
"""

from __future__ import annotations

import logging
from datetime import datetime

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import Chunk, Document, Note, WikiLink
from app.pipeline.wikilink import LINK_SCORE_FACTOR
from app.retrieval import graph
from app.retrieval import link as link_mod
from app.retrieval.types import Material, note_chunk_id

#: 宿主（用户）本轮的资料范围：库 1 / 库内来源 / 分类 allowed / 必须带 signed / 2026 年后
SCOPE = {
    "kb_ids": [1],
    "source_types": ["note", "document"],
    "categories": ["allowed"],
    "tags": ["signed"],
    "created_after": "2026-01-01",
}

QUESTION = "Allowed"
SEED_SCORE = 0.4

_NEW = datetime(2026, 9, 20)
_OLD = datetime(2020, 1, 1)

#: 四个维度各坏一个 —— 每条都必须让目标进不了材料
_BROKEN_DIMENSIONS = [
    pytest.param({"category": "outside"}, id="分类不符"),
    pytest.param({"tags": []}, id="标签不符"),
    pytest.param({"created_at": _OLD}, id="日期不符"),
    pytest.param({"kb_id": 2}, id="库不符（边自称种子库）"),
]


class _EmptyStore:
    """空结果假向量库（离线没有 embedding 配额，向量路显式不可用）。"""

    def query(self, vector, limit: int = 5, filters=None):  # noqa: ANN001
        return []


@pytest.fixture(autouse=True)
def _pin_defaults(monkeypatch):
    """钉住两把开关 + 隔离出网点（**本文件所有用例都适用**；E11 / E12）。

    - ``RAG_LINK_EXPAND=True`` / ``RAG_LINK_MAX=4``：本机 ``.env`` **没有**这两个键的钉子，
      取值由默认值兜底 ⇒ 不显式钉住，换台机器这条缺陷就测不到了；
    - ``GRAPHRAG_ENABLE=False``：图通道由 ``test_graph_hop_scope_f6.py`` 单独覆盖，
      本文件只测尾部的一跳扩展（两条面分开，差集才分得清是谁引入的）；
    - ``RAG_ENABLE_RERANK=False`` + 向量路假件：本机 ``.env`` 有老仓库遗留的 ``true``，
      不钉住会真打上游 embedding 并连本机 Qdrant。
    """
    from app.retrieval import vector as vector_mod
    from app.vector.embed import FakeEmbedding

    monkeypatch.setattr(settings, "RAG_LINK_EXPAND", True)
    monkeypatch.setattr(settings, "RAG_LINK_MAX", 4)
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", False)
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", False)
    store = _EmptyStore()
    monkeypatch.setattr(vector_mod, "get_vector_store", lambda **kwargs: store)
    monkeypatch.setattr(
        vector_mod, "get_embedding_client", lambda *, fake=False: FakeEmbedding(dim=16)
    )
    graph.reset_counters()
    yield
    graph.reset_counters()


# --------------------------------------------------------------------------
# 造行助手（本文件自足；笔记支持 tags，文档**显式写 ordinal=0**）
# --------------------------------------------------------------------------


async def _seed_note(
    db, *, title: str, content: str, kb_id: int = 1, tags=None, category: str = "", created_at=None
) -> Note:
    note = Note(title=title, content=content, kb_id=int(kb_id), tags=tags, category=category)
    if created_at is not None:
        note.created_at = created_at
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return note


async def _seed_document(
    db,
    *,
    filename: str,
    content: str = "正文",
    kb_id: int = 1,
    tags=None,
    category: str = "",
    created_at=None,
    index_status: str = "ready",
) -> Document:
    """造一条可被链接扩展的文档。

    ⚠️ **必须写 ``ordinal=0``**：``_document_fragment`` 只认首块（``link.py`` 的硬门），
    而 ``tests/retrieval/conftest.py::seed_document`` 默认从 ``start_ordinal=1`` 起
    ⇒ 用它造的文档**永远扩不出片段**，用例会假绿（E13）。
    """
    document = Document(
        filename=filename,
        file_type="md",
        status=index_status,
        index_status=index_status,
        kb_id=int(kb_id),
        category=category,
        tags=tags,
        chunk_count=1,
    )
    if created_at is not None:
        document.created_at = created_at
    db.add(document)
    await db.commit()
    await db.refresh(document)
    db.add(Chunk(document_id=document.id, content=content, ordinal=0))
    await db.commit()
    return document


async def _seed_edge(
    db,
    *,
    source_kind: str,
    source_id: int,
    target_kind: str,
    target_id: int,
    target_title: str | None = None,
    kb_id: int = 1,
    resolved: bool = True,
) -> WikiLink:
    """造一条 ``wiki_links`` 边（``_edges`` 只认「源与目标同域」的边，故两边都显式给）。

    ``target_title`` 是 NOT NULL 列，且与 ``(source_type, source_id)`` 组成唯一键
    （``uk_wiki_links_source_target``）⇒ 同一个种子的两条边必须给不同标题；检索侧不读它。
    """
    edge = WikiLink(
        source_type=source_kind,
        source_id=int(source_id),
        kb_id=int(kb_id),
        target_title=str(target_title if target_title is not None else f"目标 {target_kind} {target_id}"),
        resolved=bool(resolved),
    )
    if target_kind == "note":
        edge.target_note_id = int(target_id)
    else:
        edge.target_document_id = int(target_id)
    db.add(edge)
    await db.commit()
    return edge


def _note_material(note: Note, *, score: float = SEED_SCORE) -> Material:
    return Material(
        chunk_id=note_chunk_id(int(note.id)),
        document_id=0,
        content=note.content or "",
        filename=note.title or "",
        category=note.category or "",
        kb_id=int(note.kb_id or 1),
        score=score,
        note_id=int(note.id),
    )


async def _document_material(db, document: Document, *, score: float = SEED_SCORE) -> Material:
    chunk_id = (
        await db.execute(
            select(Chunk.id).where(
                Chunk.document_id == int(document.id), Chunk.ordinal == 0
            )
        )
    ).scalar_one()
    return Material(
        chunk_id=int(chunk_id),
        document_id=int(document.id),
        content="(seed)",
        filename=document.filename or "",
        category=document.category or "",
        kb_id=int(document.kb_id or 1),
        block_ordinal=0,
        score=score,
    )


def _names(items) -> list[str]:
    return [item.filename for item in items]


# ==========================================================================
# B1 / B2 —— 四个维度：目标行任一不满足都不得进入材料（两个域各一遍）
# ==========================================================================


@pytest.mark.parametrize("broken", _BROKEN_DIMENSIONS)
@pytest.mark.asyncio
async def test_b1_note_target_must_pass_every_scope_dimension(db, broken):
    """**笔记域**：分类 / 标签 / 日期 / 库 任一不满足 ⇒ 目标不得进材料；**范围内的仍然进**（E7）。"""
    seed = await _seed_note(
        db,
        title=QUESTION,
        content=f"{QUESTION} 正文",
        category="allowed",
        tags=["signed"],
        created_at=_NEW,
    )
    inside = await _seed_note(
        db,
        title="Inside",
        content="Inside 正文",
        category="allowed",
        tags=["signed"],
        created_at=_NEW,
    )
    base = {
        "title": "Outside",
        "content": "Outside 正文",
        "category": "allowed",
        "tags": ["signed"],
        "created_at": _NEW,
    }
    base.update(broken)
    outside = await _seed_note(db, **base)

    await _seed_edge(
        db, source_kind="note", source_id=seed.id, target_kind="note", target_id=inside.id
    )
    await _seed_edge(
        db, source_kind="note", source_id=seed.id, target_kind="note", target_id=outside.id
    )

    expanded = await link_mod.expand_materials(
        db, [_note_material(seed)], filters=SCOPE, max_links=4
    )

    assert _names(expanded) == [QUESTION, "Inside"], "范围外目标不得进入材料"
    assert len(expanded) > 1, "正控（E7）：范围内的目标必须**仍然**扩进来，不许整体关掉"
    assert round(expanded[1].score, 6) == round(SEED_SCORE * LINK_SCORE_FACTOR, 6), "降权分不变"


@pytest.mark.parametrize("broken", _BROKEN_DIMENSIONS)
@pytest.mark.asyncio
async def test_b1_document_target_must_pass_every_scope_dimension(db, broken):
    """**文档域**：与笔记域同判（文档目标必须真的扩得出来，否则本用例假绿 ⇒ 首块写 ``ordinal=0``）。"""
    seed = await _seed_document(
        db,
        filename=f"{QUESTION}.md",
        content=f"{QUESTION} 正文",
        category="allowed",
        tags=["signed"],
        created_at=_NEW,
    )
    inside = await _seed_document(
        db,
        filename="Inside.md",
        content="Inside 正文",
        category="allowed",
        tags=["signed"],
        created_at=_NEW,
    )
    base = {
        "filename": "Outside.md",
        "content": "Outside 正文",
        "category": "allowed",
        "tags": ["signed"],
        "created_at": _NEW,
    }
    base.update(broken)
    outside = await _seed_document(db, **base)

    await _seed_edge(
        db,
        source_kind="document",
        source_id=seed.id,
        target_kind="document",
        target_id=inside.id,
    )
    await _seed_edge(
        db,
        source_kind="document",
        source_id=seed.id,
        target_kind="document",
        target_id=outside.id,
    )

    seed_material = await _document_material(db, seed)
    expanded = await link_mod.expand_materials(
        db, [seed_material], filters=SCOPE, max_links=4
    )

    assert _names(expanded) == [f"{QUESTION}.md", "Inside.md"]
    assert len(expanded) > 1, "正控（E7）：范围内文档目标必须仍然扩进来"


# ==========================================================================
# B2 —— 坏边跨库：材料身份取**目标行真实 kb**，不许拿种子库顶包
# ==========================================================================


@pytest.mark.asyncio
async def test_b2_dirty_cross_kb_edge_reports_the_real_target_kb(db):
    """脏跨库边（边自称 kb 1、目标行其实是 kb 2）。

    ① ``filters=None`` ⇒ 既有行为逐字保留（照样扩出来），但材料的 ``kb_id`` 必须是
       **目标行自己的** 2 —— 取种子库就是「把目标身份改名后冒充合规」；
    ② ``filters`` 非空且宿主只含 kb 1 ⇒ 这条边必须被拒。
    """
    seed = await _seed_note(
        db,
        title=QUESTION,
        content=f"{QUESTION} 正文",
        category="allowed",
        tags=["signed"],
        created_at=_NEW,
        kb_id=1,
    )
    dirty = await _seed_note(
        db,
        title="Dirty",
        content="Dirty 正文",
        category="allowed",
        tags=["signed"],
        created_at=_NEW,
        kb_id=2,
    )
    # 边行自称 kb 1（= 种子库）⇒ 它会通过 `_edges` 的同库钉死，只有回表复核才拦得住
    await _seed_edge(
        db,
        source_kind="note",
        source_id=seed.id,
        target_kind="note",
        target_id=dirty.id,
        kb_id=1,
    )

    plain = await link_mod.expand_materials(db, [_note_material(seed)], max_links=4)
    assert _names(plain) == [QUESTION, "Dirty"], "无范围时既有行为不变（B-a 硬边界）"
    assert plain[1].kb_id == 2, "材料 kb_id 必须取目标行真实 kb"
    assert plain[1].kb_id != int(seed.kb_id), "不许把目标贴上种子库的标签"

    scoped = await link_mod.expand_materials(
        db, [_note_material(seed)], filters={"kb_ids": [1]}, max_links=4
    )
    assert _names(scoped) == [QUESTION], "宿主只含 kb 1 ⇒ kb 2 的目标必须被拒"


@pytest.mark.asyncio
async def test_b2_source_types_scope_is_not_bypassed_by_expansion(db):
    """``source_types`` 限制不能被扩展绕过；同时给出「该扩的仍然扩」的反向正控。"""
    seed = await _seed_note(
        db,
        title=QUESTION,
        content=f"{QUESTION} 正文",
        category="allowed",
        tags=["signed"],
        created_at=_NEW,
    )
    inside = await _seed_note(
        db,
        title="Inside",
        content="Inside 正文",
        category="allowed",
        tags=["signed"],
        created_at=_NEW,
    )
    await _seed_edge(
        db, source_kind="note", source_id=seed.id, target_kind="note", target_id=inside.id
    )

    docs_only = await link_mod.expand_materials(
        db,
        [_note_material(seed)],
        filters={"kb_ids": [1], "source_types": ["document"]},
        max_links=4,
    )
    assert _names(docs_only) == [QUESTION], "只要文档时，笔记目标不得被扩进来"

    notes_only = await link_mod.expand_materials(
        db,
        [_note_material(seed)],
        filters={"kb_ids": [1], "source_types": ["note"]},
        max_links=4,
    )
    assert _names(notes_only) == [QUESTION, "Inside"], "只要笔记时同一目标必须扩得出来"


# ==========================================================================
# B3 —— 无范围正控：filters=None 时逐条不变
# ==========================================================================


@pytest.mark.asyncio
async def test_b3_no_scope_control_is_step_for_step_unchanged(db):
    """``filters=None`` ⇒ **范围外目标照样扩**（既有行为），数量 / 降权分 / 去重逐条不变。

    这条同时是「不许拿『非空范围就一律不扩』当修法」的反向对照：一旦把 ``None`` 也当成
    「范围外」，本用例立刻红（M4）。
    """
    seed = await _seed_note(
        db, title=QUESTION, content=f"{QUESTION} 正文", category="allowed", tags=["signed"]
    )
    inside = await _seed_note(db, title="Inside", content="Inside 正文")
    outside = await _seed_note(db, title="Outside", content="Outside 正文")
    await _seed_edge(
        db, source_kind="note", source_id=seed.id, target_kind="note", target_id=inside.id
    )
    await _seed_edge(
        db, source_kind="note", source_id=seed.id, target_kind="note", target_id=outside.id
    )

    plain = await link_mod.expand_materials(db, [_note_material(seed)], max_links=4)

    assert plain[0].filename == QUESTION, "主命中逐条不变（在首位）"
    assert sorted(_names(plain)) == sorted([QUESTION, "Inside", "Outside"])
    assert len(plain) == 3, "无范围时两条边各扩一条（逐字不变）"
    for item in plain[1:]:
        assert round(item.score, 6) == round(SEED_SCORE * LINK_SCORE_FACTOR, 6)
    assert len({item.chunk_id for item in plain}) == 3, "去重键仍是来源身份，不重复"


# ==========================================================================
# B4 —— 从真实 retrieve() 入口验证接线（M1 调用点变异的检出器）
# ==========================================================================


@pytest.mark.asyncio
async def test_b4_retrieve_hands_the_turn_scope_to_the_expansion(db, monkeypatch):
    """``retrieve()`` 必须把本轮 ``filters`` 交给一跳扩展；**最终材料里不得有范围外目标**。"""
    seed = await _seed_note(
        db,
        title=QUESTION,
        content=f"{QUESTION} 正文",
        category="allowed",
        tags=["signed"],
        created_at=_NEW,
    )
    inside = await _seed_note(
        db,
        title="Inside",
        content="Allowed Inside 正文",
        category="allowed",
        tags=["signed"],
        created_at=_NEW,
    )
    outside = await _seed_note(
        db, title="Outside", content="Outside 正文", category="outside", tags=[], created_at=_OLD
    )
    await _seed_edge(
        db, source_kind="note", source_id=seed.id, target_kind="note", target_id=inside.id
    )
    await _seed_edge(
        db, source_kind="note", source_id=seed.id, target_kind="note", target_id=outside.id
    )

    seen: dict = {}
    real = link_mod.expand_materials

    async def _spy(session, materials, **kwargs):  # noqa: ANN001
        seen.update(kwargs)
        return await real(session, materials, **kwargs)

    monkeypatch.setattr(link_mod, "expand_materials", _spy)

    from app.retrieval import retrieve

    materials, _text, warning = await retrieve(db, QUESTION, SCOPE)

    assert warning is None
    assert seen.get("filters") == SCOPE, "retrieve() 必须把本轮 filters 交给扩展（接线）"
    assert _names(materials) == [QUESTION, "Inside"], "范围外目标不得进最终材料"
    assert [item.ordinal for item in materials] == [1, 2], "编号仍连续 1..N（I3）"


# ==========================================================================
# 钉子 —— 时间维度参与判定时，`created_at` 必须**按列读**（实测踩到的坑）
# ==========================================================================


@pytest.mark.asyncio
async def test_b_time_bounded_scope_still_expands_in_scope_targets(db):
    """⚠️ 回归钉（本项实测踩到）：范围里带时间维度时，**不得直接读 ORM 行的 ``created_at``**。

    ``created_at`` 是 ``server_default=func.now()``，MySQL 方言不支持 ``RETURNING``
    ⇒ 同一会话里「刚写、没 refresh 过」的行上它是**过期**的；直接读属性会触发**属性级懒加载**，
    异步会话里抛 ``MissingGreenlet`` —— 而扩展是静默降级，症状只是「什么都没扩出来」，
    看起来像范围收得太紧了。

    ``tests/retrieval/conftest.py::seed_document`` 正是**不 refresh** 的写法
    （与 ``_add_document`` 的 ``db.refresh`` 相反）⇒ 拿它造目标行才能钉住这条路：
    **范围内的目标必须照样扩进来**。
    """
    from tests.retrieval.conftest import seed_document

    seed = await seed_document(
        db,
        [f"{QUESTION} 正文"],
        filename=f"{QUESTION}.md",
        category="allowed",
        tags=["signed"],
        start_ordinal=0,
    )
    inside = await seed_document(
        db,
        ["Inside 正文"],
        filename="Inside.md",
        category="allowed",
        tags=["signed"],
        start_ordinal=0,
    )
    outside = await seed_document(
        db, ["Outside 正文"], filename="Outside.md", category="outside", start_ordinal=0
    )
    await _seed_edge(
        db,
        source_kind="document",
        source_id=seed.id,
        target_kind="document",
        target_id=inside.id,
    )
    await _seed_edge(
        db,
        source_kind="document",
        source_id=seed.id,
        target_kind="document",
        target_id=outside.id,
    )

    expanded = await link_mod.expand_materials(
        db, [await _document_material(db, seed)], filters=SCOPE, max_links=4
    )

    assert _names(expanded) == [f"{QUESTION}.md", "Inside.md"], (
        "时间维度参与判定时，范围内目标仍须扩出来（读 created_at 炸了就会只剩种子）"
    )


# ==========================================================================
# §2.5 / E9 —— 复核在**回表之后**：非空范围照发边查询，降级路径不被绕过
# ==========================================================================


@pytest.mark.asyncio
async def test_b_edge_query_still_runs_when_a_scope_is_present(db, monkeypatch):
    """筛选判定**不得前置到边加载之前**（E9 / M2 的检出器）。

    前置会让既有的 ``test_expand_materials_is_silent_on_failure`` 仍绿、却**不再覆盖**
    降级路径（静默丢覆盖）；这里用「边查询是否被真的调到」把它钉死。
    """
    seed = await _seed_note(db, title=QUESTION, content=f"{QUESTION} 正文")
    calls: list[tuple] = []

    async def _boom(session, seeds):  # noqa: ANN001
        calls.extend(seeds)
        raise RuntimeError("wiki_links 查询炸了")

    monkeypatch.setattr(link_mod, "load_wiki_adjacency", _boom)

    expanded = await link_mod.expand_materials(
        db, [_note_material(seed)], filters=SCOPE, max_links=4
    )

    assert calls == [(1, "note", int(seed.id))], "有范围时也必须走到边查询（复核在回表之后）"
    assert _names(expanded) == [QUESTION], "失败仍静默降级（I5）：主命中照常返回"


@pytest.mark.asyncio
async def test_b_rejected_targets_are_skipped_silently(db, caplog):
    """被范围拒 = 与「目标不存在」同待遇（**静默跳过**）：不刷日志、不加事件（§2.5）。"""
    seed = await _seed_note(db, title=QUESTION, content=f"{QUESTION} 正文")
    outside = await _seed_note(
        db,
        title="Outside",
        content="Outside 正文",
        category="allowed",
        tags=["signed"],
        created_at=_OLD,
    )
    await _seed_edge(
        db, source_kind="note", source_id=seed.id, target_kind="note", target_id=outside.id
    )

    with caplog.at_level(logging.WARNING, logger="app.retrieval.link"):
        expanded = await link_mod.expand_materials(
            db, [_note_material(seed)], filters=SCOPE, max_links=4
        )

    assert _names(expanded) == [QUESTION]
    assert [record for record in caplog.records if record.name == "app.retrieval.link"] == [], (
        "被范围拒不是失败：不许记 warning，也不许新增事件 / 计数器"
    )


@pytest.mark.asyncio
async def test_b_switch_off_expands_nothing_even_with_a_link(db, monkeypatch):
    """E11：``RAG_LINK_EXPAND=False`` 这另一态显式钉住（默认值来自配置，不许靠本机 ``.env``）。"""
    seed = await _seed_note(
        db,
        title=QUESTION,
        content=f"{QUESTION} 正文",
        category="allowed",
        tags=["signed"],
        created_at=_NEW,
    )
    inside = await _seed_note(
        db,
        title="Inside",
        content="Inside 正文",
        category="allowed",
        tags=["signed"],
        created_at=_NEW,
    )
    await _seed_edge(
        db, source_kind="note", source_id=seed.id, target_kind="note", target_id=inside.id
    )
    seed_material = _note_material(seed)

    monkeypatch.setattr(settings, "RAG_LINK_EXPAND", False)
    assert await link_mod.expand_materials(
        db, [seed_material], filters=SCOPE, max_links=4
    ) == [seed_material]

    monkeypatch.setattr(settings, "RAG_LINK_EXPAND", True)
    assert _names(
        await link_mod.expand_materials(db, [seed_material], filters=SCOPE, max_links=4)
    ) == [QUESTION, "Inside"]
