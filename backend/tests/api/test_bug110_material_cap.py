"""回归修复 载体：Agent 合并材料可超 12 条 ⇒ 资料区 ``[13]+`` 与引用分母脱节。

缺陷（修复前）：``_merge_material_snapshots`` 去重并集无上限——基线检索快照（≤
``FUSE_LIMIT``=12）与 Agent 自检材料（runner 侧也夹 12）合并可达 24 条；
``_install_agent_materials`` 照全部材料渲染出 ``[13]…`` 标记行进提示词，而
``citation.refs_from_materials`` 被 ``[:MAX_REFS]``（12）**静默切片** ⇒ 模型照资料区
写的 ``[13]`` 被判越界：引用卡缺失、归因行缺失、整轮可退化 fallback。

修复：合并集夹到 ``MAX_REFS``——材料编号、materials_text 渲染、refs 三者同源同长。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.api.chat_routes import TurnPlan, _apply_agent_closeout, _merge_material_snapshots
from app.generation import citation as citation_mod
from app.retrieval.types import Material


def _material(chunk_id: int, *, document_id: int = 1) -> Material:
    """真实形状的库内材料：``chunk_key`` 由 ``(kb_id, document_id, block_ordinal)`` 拼，
    同文档内块与块靠 ``block_ordinal`` 区分——桩数据必须照此取异，否则全被去重。"""
    return Material(
        chunk_id=chunk_id,
        document_id=document_id,
        content=f"内容{chunk_id}",
        filename=f"doc{document_id}.md",
        block_ordinal=chunk_id - 1,
        score=0.5,
    )


def _plan(materials: list) -> TurnPlan:
    return TurnPlan(
        chat=None,
        question="问",
        images=[],
        history=[],
        filters=None,
        skill_ids=[],
        source_type="internal_db",
        kind="normal",
        answer_source="kb",
        materials=list(materials),
        retrieval_id="rl-bug110",
        materials_finalized=True,
    )


def test_merged_agent_snapshot_is_clamped_to_max_refs():
    """基线 8 条 + Agent 8 条（chunk 互异）⇒ 收口后 ≤ 12 且三面一致（修复前 16 条）。"""
    baseline = [_material(i) for i in range(1, 9)]
    agent = [_material(i) for i in range(101, 109)]
    plan = _plan(baseline)
    turn = SimpleNamespace(
        closeout_invalid=False,
        closeout={"action": "final_answer", "use_candidates": True},
        materials=agent,
    )

    _apply_agent_closeout(plan, turn)

    assert len(plan.materials) == citation_mod.MAX_REFS
    assert len(plan.all_refs) == len(plan.materials)  # 引用分母与材料同长
    assert "[13]" not in plan.materials_text  # 资料区编号不得越过引用分母
    # 保序：基线在前、Agent 补位，按合并序截前 12
    assert [m.chunk_id for m in plan.materials] == list(range(1, 9)) + [101, 102, 103, 104]


def test_merge_dedups_before_clamping():
    """去重先于截断：重叠 chunk 只算一次，保序截前 MAX_REFS。"""
    baseline = [_material(i) for i in range(1, 11)]  # 10 条（块序 0..9）
    # 8..13 号块序与基线 8/9/10 重叠（同文档同块序 = 同一块）⇒ 去重后并集 13 条
    agent = [_material(i) for i in range(8, 14)]

    merged = _merge_material_snapshots(baseline, agent)

    assert len(merged) == citation_mod.MAX_REFS  # 并集 13 条 ⇒ 夹到 12
    keys = [m.chunk_key for m in merged]
    assert len(set(keys)) == len(keys)  # 去重仍生效
    assert keys == [m.chunk_key for m in baseline] + [
        _material(i).chunk_key for i in (11, 12)
    ]  # 先到先得、保序
