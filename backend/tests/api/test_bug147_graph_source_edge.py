"""`回归修复`（测试面 P3）：图谱 `kind="source"` 边的**正向行为**载体。

缺陷形态（判据面缺口，非产品缺陷）：`app/pipeline/knowledge_graph.py:162-164` 会把
`Note.source_document_id` 指向**在库文档**的笔记连成 `kind="source"` 边，但既有用例恰好只走
**丢弃**分支：

- `tests/api/test_notes_4_5_2.py:203` 只钉 `GraphEdge.kind` 的 `Literal`（**类型**面，不是行为面）；
- 同文件 `:843-881` 的三条 graph 行为用例**只造 wiki 边**（`rebuild_source_links`）；
- 同文件 `:307` 的 `source_document_id: 42` 指向**不存在的文档**，且那条用例根本不调 `/api/graph`。

⇒ 「真有条 `source` 边被画出来」这件事**在判据上不存在**：把 :162-164 整段删掉（source 边永不产生）
或反过来无条件产生（不再查 `in doc_ids`），既有用例**都不会红**。

本载体补四条行为腿（夹具用既有 seed 手段**真建 `Document` 行**，不用 `42` 这种悬空 id）：

- 腿①（正控）`test_a1`：笔记指向**真实存在**的文档 ⇒ `GET /api/graph` 的 `edges` **恰好**
  是那一条 `source` 边，且两端 id 正确（`note:<note_id>` → `document:<doc_id>`）、方向正确、
  两个端点 degree = 1、`stats` 闭合。
- 腿②（负控）`test_a2`：笔记指向**不存在**的文档 id ⇒ 不画边（丢弃分支）；
  同时确认「真文档仍在库」（不是「库里没文档」才没边），且丢边**不转幽灵**。
- 腿③（负控）`test_a3`：手工笔记（`source_document_id` 为 `None`）⇒ 不画 source 边
  （库里有文档也不行）。
- 腿④（正控）`test_a4`：两条笔记指向**同一篇**文档 ⇒ 各自一条边（文档端 degree = 2）。

⛔ 红线：本条**只补 `kind="source"` 边的行为面**；`wiki` 边与前端渲染面不在本条
⇒ **不得**写「图谱溯源已全覆盖」。

口径（如实登记）：**本条为覆盖面补齐** —— **零产品代码改动**、被测行为一字未改 ⇒
**新载体在改前代码上同样绿**（没有产品改动，也无回退击杀可言）；
本载体的价值 = 把「删掉 :162-164」与「无条件产生 source 边」两类变异变成**会被打红**。
"""

from __future__ import annotations

import pytest

from app.models import Document
from tests.api.test_chat_contract import client  # noqa: F401  —— 复用 ASGI 直连客户端（已覆盖 get_db）


async def _add_document(db, filename: str, *, kb_id: int = 1, category: str = "") -> Document:
    """**真建**一行 `Document`（graph 的 `doc_ids` 只认在库行）。

    ``documents`` 由根 conftest 的 `_CLEAN_ORDER` 每例清空，故这里的 id 从 1 起、完全确定。
    """
    doc = Document(
        filename=filename,
        file_type="md",
        status="ready",
        index_status="ready",
        kb_id=kb_id,
        category=category,
        chunk_count=0,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


async def _create_note(client, title: str, *, source_document_id: int | None = None) -> dict:
    """走端点建笔记（`source_document_id` 不传 = 手工笔记）⇒ 返回响应体。"""
    payload: dict = {"title": title, "content": "笔记正文"}
    if source_document_id is not None:
        payload["source_document_id"] = source_document_id
    resp = await client.post("/api/notes", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _graph(client) -> dict:
    resp = await client.get("/api/graph")
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_a1_note_pointing_at_a_real_document_draws_the_source_edge(client, db):
    """腿①（正控）：真实文档 ⇒ `edges` 恰好一条 `source` 边，两端 id / 方向 / degree 全对。"""
    doc = await _add_document(db, "溯源论文.md")
    note = await _create_note(client, "读后笔记", source_document_id=doc.id)
    note_id = note["id"]
    assert note["source_document_id"] == doc.id

    body = await _graph(client)

    assert body["edges"] == [
        {"source": f"note:{note_id}", "target": f"document:{doc.id}", "kind": "source"}
    ], "笔记 → 源文档的 source 边（方向：source = 笔记、target = 文档）"

    nodes = {node["id"]: node for node in body["nodes"]}
    assert set(nodes) == {f"note:{note_id}", f"document:{doc.id}"}
    assert nodes[f"note:{note_id}"]["degree"] == 1
    assert nodes[f"document:{doc.id}"]["degree"] == 1
    assert body["stats"] == {
        "notes": 1,
        "documents": 1,
        "ghosts": 0,
        "edges": 1,
        "isolated": 0,
    }


@pytest.mark.asyncio
async def test_a2_dangling_source_document_id_draws_no_edge(client, db):
    """腿②（负控）：指向不存在的文档 id ⇒ 丢边；但真文档仍在库、且丢边不转幽灵。"""
    doc = await _add_document(db, "唯一真文档.md")
    dangling = doc.id + 1000
    note = await _create_note(client, "悬空溯源", source_document_id=dangling)
    assert note["source_document_id"] == dangling, "端点原样存下悬空 id（不校验）"

    body = await _graph(client)

    assert body["edges"] == [], "指向不存在的文档 ⇒ 那条 source 边被丢弃"
    assert body["stats"]["documents"] == 1, "真文档仍在库（不是「库里没文档」才没边）"
    assert body["stats"]["edges"] == 0
    assert body["stats"]["ghosts"] == 0, "丢边不转幽灵"
    assert body["stats"]["isolated"] == 2
    assert {node["id"] for node in body["nodes"]} == {
        f"note:{note['id']}",
        f"document:{doc.id}",
    }


@pytest.mark.asyncio
async def test_a3_manual_note_without_source_document_draws_no_source_edge(client, db):
    """腿③（负控）：`source_document_id is None` 的手工笔记 ⇒ 库里有文档也不画 source 边。"""
    await _add_document(db, "无关文档.md")
    note = await _create_note(client, "手写笔记")
    assert note["source_document_id"] is None

    body = await _graph(client)

    assert body["edges"] == []
    assert body["stats"]["edges"] == 0
    assert body["stats"]["isolated"] == 2


@pytest.mark.asyncio
async def test_a4_each_note_pointing_at_the_same_document_gets_its_own_edge(client, db):
    """腿④（正控）：两条笔记指向同一篇文档 ⇒ 两条边（边按「笔记」逐条产生），文档端 degree = 2。"""
    doc = await _add_document(db, "共享来源.md")
    first = await _create_note(client, "甲笔记", source_document_id=doc.id)
    second = await _create_note(client, "乙笔记", source_document_id=doc.id)

    body = await _graph(client)

    assert {(edge["source"], edge["target"], edge["kind"]) for edge in body["edges"]} == {
        (f"note:{first['id']}", f"document:{doc.id}", "source"),
        (f"note:{second['id']}", f"document:{doc.id}", "source"),
    }
    nodes = {node["id"]: node for node in body["nodes"]}
    assert nodes[f"document:{doc.id}"]["degree"] == 2
    assert nodes[f"note:{first['id']}"]["degree"] == 1
    assert nodes[f"note:{second['id']}"]["degree"] == 1
    assert body["stats"]["edges"] == 2
