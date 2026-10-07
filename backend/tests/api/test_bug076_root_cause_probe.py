"""回归修复 五态判据载体（**修复验证 + 回归钉** · 原定位探针就地改写）。

## 沿革（照 090 探针就地改写先例）

本文件原为 076 的**定位探针**（2026-09-26）：用桩把收尾五态固化，其中 A/D/E 三态断的正是
「照样输出 12 条 fallback 引用」这个**缺陷现状**（绿 = 现状被钉住）。
2026-09-27 拍板③（**一致性优先**：正文断言没找到 ⇒ 引用清空）落地后，实现已改，
按该文件自述的授权语义（「实现已改须重核」·090 先例）**就地改写**为修复验证载体：

- A（无协议块）/ D（`use_candidates=true`）/ E（坏协议块）三态在 **Agent 空手**时
  一律落 `_clear_plan_materials`：kind=direct、材料/引用全清、`answer_source=general`；
- B（clarify）/ C（use_candidates=false）两态的既有正确行为**逐字不变**；
- 原「对照腿 1」（空手 ⇒ plan 一字未动）随实现翻转为「空手 ⇒ 基线候选被清空」，
  即本修复的**主腿**；原「对照腿 2」（Agent 有料 ⇒ 替换基线）**一字未动**。

## 修复口径（三条，不得含糊）

1. **一致性优先**：Agent 实际调用工具而零命中 ⇒ 本轮没有采用的资料 ⇒ 引用清空。
   修的形态 = 12 条基线候选以 `citation_mode="fallback"` 挂成「本次检索到的全部资料」，
   与正文「没找到」自相矛盾（账本 §一 msg 958 的 12 条）。
2. **D 路是有意覆盖**：`use_candidates=true` 是模型的显式选择，但 A/D 在落库读数上
   结构性不可判（`closeout`/`use_candidates` 均不落库、不进 `trace()`），拍板取统一清空；
   2026-10-04 U4 修正仅覆盖**未执行工具**且已有合法库内候选的轮；现有 trace.steps
   足以区分这一状态，本文件保留实际工具空结果的五态口径。
3. **C/B 两态不变**：收尾明确表态的两条路（clarify / use_candidates=false）行为逐字保持。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.api import chat_routes
from app.generation import citation as citation_mod
from app.retrieval.types import Material

#: 账本 §三 事实 1 逐字给出的 retrieval_id（12 条共享同一个）。
RETRIEVAL_ID = "rl-38f7fe8bd6be"

#: 基线候选条数 = 账本 §一 逐字观察到的「12 条引用」。
BASELINE_CANDIDATES = 12


@dataclass
class _FakeTurn:
    """AgentTurn 的最小替身：历史五态均有实际工具尝试。

    materials 空 = 「Agent 空手」（账本事实 5 的工具链命中三条空观察：
    load_tools / knowledge_search / list_documents 均未检到材料）。
    closeout / closeout_invalid 的取值见各参数化分支。
    """

    materials: list = field(default_factory=list)
    closeout: dict | None = None
    closeout_invalid: bool = False
    steps: list = field(default_factory=lambda: [
        {"kind": "tool", "tool": "knowledge_search", "title": "检索知识库"}
    ])
    tool_context: list = field(default_factory=list)


@dataclass
class _FakePlan:
    """TurnPlan 在引用装配链上被读写的全部字段。"""

    retrieval_id: str = RETRIEVAL_ID
    materials: list = field(default_factory=list)
    materials_text: str = ""
    all_refs: list = field(default_factory=list)
    refs: list = field(default_factory=list)
    kind: str = "normal"
    answer_source: str = "kb"
    source_type: str = "internal_db"


def _material(i: int) -> Material:
    """造第 i 条基线候选材料。

    document_id 各不相同 ⇒ Material.chunk_key（kb:<kb>:document:<doc>:<ord>）
    各不相同，**不会被 ToolCollector.absorb 的按-key 去重折叠掉**。
    """

    return Material(
        chunk_id=i,
        document_id=i,
        content=f"候选正文{i}",
        filename=f"候选文档{i}.md",
        block_ordinal=0,
    )


#: 模型正文：**不含任何有效 [n]**。修复后空手轮 kind=direct ⇒ refs 黑名单恒空，
#: fallback 不再有数据来源（all_refs 已清空）。
_NO_CITATION_ANSWER = "我没有办法帮你写入笔记哦，说明一下原因。"


def _baseline_plan() -> _FakePlan:
    """模拟 build_turn_plan 的基线检索结果：12 条候选已写进 plan（账本 §三「最可能的一跳」的前提）。"""

    plan = _FakePlan()
    plan.materials = [_material(i) for i in range(1, BASELINE_CANDIDATES + 1)]
    plan.materials_text = "（基线检索到的 12 条候选正文）"
    plan.all_refs = citation_mod.refs_from_materials(plan.materials, plan.retrieval_id)
    return plan


#: 五态参数化表（**修复后期望**）。
#:
#: - A 无协议块（closeout=None / invalid=False）：turn.materials 为空 ⇒ `_clear_plan_materials`
#:   ⇒ direct / 0 / 0 / 0（**修复主腿之一**；修复前 = normal / 12 / 12 / 12）。
#: - B clarify 分支：**九处**字段同步改写 ⇒ 零引用（既有正确行为，未动）。
#: - C use_candidates is False 分支：空快照 ⇒ 落 DIRECT_KIND ⇒ 零引用（既有正确行为，未动）。
#: - D use_candidates=True：Agent 空手 ⇒ 合并只剩基线候选 ⇒ 清空（**修复主腿之一**；
#:   修复前 = 12 条带出；对「模型显式要候选」的有意覆盖，见文件头口径 2）。
#: - E 坏协议块（invalid=True）：同 A ⇒ 清空（修复前 = 12 条带出）。
_CASES = [
    pytest.param(None, False, [], "direct", 0, 0, 0, id="A-no-fence-closeout-none"),
    pytest.param(
        {"action": "clarify", "use_candidates": False, "clarification": "你指的是哪一份资料？"},
        False,
        [],
        "clarify",
        0,
        0,
        0,
        id="B-clarify",
    ),
    pytest.param(
        {"action": "answer", "use_candidates": False, "clarification": ""},
        False,
        [],
        "direct",
        0,
        0,
        0,
        id="C-use-candidates-false",
    ),
    pytest.param(
        {"action": "answer", "use_candidates": True, "clarification": ""},
        False,
        [],
        "direct",
        0,
        0,
        0,
        id="D-use-candidates-true",
    ),
    pytest.param(None, True, [], "direct", 0, 0, 0, id="E-invalid-closeout"),
]

#: 空手清空态（A/D/E）的 answer_source / source_type 期望（与 C 态既有口径同形）。
_CLEARED_STATES = {"A-no-fence-closeout-none", "D-use-candidates-true", "E-invalid-closeout"}


@pytest.mark.parametrize(
    "closeout,invalid,agent_materials,expected_kind,expected_materials,"
    "expected_all_refs,expected_final_refs",
    _CASES,
)
def test_bug076_closeout_five_states_probe(
    closeout,
    invalid,
    agent_materials,
    expected_kind,
    expected_materials,
    expected_all_refs,
    expected_final_refs,
):
    """回归修复 五态判据（修复后）：逐态钉 kind/materials/all_refs/final_refs 四值。"""

    plan = _baseline_plan()
    turn = _FakeTurn(
        materials=list(agent_materials), closeout=closeout, closeout_invalid=invalid
    )

    chat_routes._apply_agent_closeout(plan, turn)

    # ① 分路：决定 refs_for_kind 是否走黑名单（direct / refuse / clarify 恒空）。
    assert plan.kind == expected_kind
    # ② 材料快照：修复后空手态一律清空。
    assert len(plan.materials) == expected_materials
    # ③ 引用分母：all_refs 是 fallback 的唯一数据源——清空后 fallback 无米下锅。
    assert len(plan.all_refs) == expected_all_refs

    # ③' 空手清空态的来源标记与 C 态同形（direct/general/external）。
    if expected_materials == 0 and plan.kind == "direct":
        assert plan.answer_source == "general"
        assert plan.source_type == "external"
        assert plan.refs == []
        assert plan.materials_text == ""

    # ④ 收口后的最终引用（与 finalize_citations 同源：refs_for_kind + 正文无有效编号）。
    final_refs = citation_mod.refs_for_kind(plan.kind, _NO_CITATION_ANSWER, plan.all_refs)
    assert len(final_refs) == expected_final_refs


def test_bug076_empty_handed_clears_baseline_when_closeout_missing():
    """**修复主腿**（原对照腿 1 就地翻转）：Agent 空手 + 无协议块 ⇒ 基线候选被清空。

    修复前这里断「plan 一字未动」（12 条候选原样保留 ⇒ fallback 挂出）——那正是 076 的漏点。
    现在断「清空 + direct」：正文断言没找到的轮不再挂引用。
    """

    plan = _baseline_plan()

    for closeout, invalid in ((None, False), (None, True)):
        chat_routes._apply_agent_closeout(
            plan, _FakeTurn(materials=[], closeout=closeout, closeout_invalid=invalid)
        )
        assert plan.kind == "direct"
        assert plan.materials == [] and plan.all_refs == [] and plan.refs == []
        assert plan.answer_source == "general"
        # 复位基线，让第二次循环同样从「12 条候选」出发。
        plan = _baseline_plan()

    final_refs = citation_mod.refs_for_kind("direct", _NO_CITATION_ANSWER, [])
    assert final_refs == []


def test_bug076_agent_materials_replace_baseline_when_no_fence():
    """对照腿（未动）：Agent **有料** + 无协议块 ⇒ 用 Agent 快照**替换**基线。"""

    plan = _baseline_plan()
    turn = _FakeTurn(materials=[_material(201), _material(202)], closeout=None)

    chat_routes._apply_agent_closeout(plan, turn)

    assert len(plan.materials) == 2
    assert len(plan.all_refs) == 2
    assert plan.kind == "normal"


def test_bug076_agent_materials_with_use_candidates_true_still_merged():
    """对照腿（新增，防过修）：Agent **有料** + use_candidates=true ⇒ 合并照旧（076 只管空手）。

    ⚠️ 回归修复 测试侧适配（2026-09-27）：合并集现被夹到 ``MAX_REFS``——基线 12 条
    （名额已满）+ Agent 1 条的并集 13 ⇒ 截前 12（先到先得、基线在前，Agent 新材料让位）。
    本腿的判别力不变：合并**照旧发生**、``kind=normal``、未被 076 清空（若 076 过修，
    这里会是清空态而不是 12 条）；「有名额时 Agent 材料参与合并」由
    ``test_bug110_material_cap.py`` 的保序腿覆盖。
    """

    plan = _baseline_plan()
    turn = _FakeTurn(
        materials=[_material(201)],
        closeout={"action": "answer", "use_candidates": True},
    )

    chat_routes._apply_agent_closeout(plan, turn)

    assert plan.kind == "normal"
    assert len(plan.materials) == citation_mod.MAX_REFS, "合并语义不得被 076 改动（110 起夹到上限）"
