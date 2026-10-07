"""分路结果 → 契约字段的映射（阶段 1 · 条目 1.3.1）——**纯函数**，无 IO。

本模块是统筹 §3.8 那张表的**唯一代码形态**：

| 内部 source_type            | 契约 kind | answer_source | refs   |
|-----------------------------|-----------|---------------|--------|
| internal_db（命中）          | `normal`  | `kb`          | 有引用 |
| internal_db 未命中且允许直答 | `direct`  | `general`     | 恒空   |
| 范围空 / 拒答                | `refuse`  | `empty_scope` | 恒空   |
| 澄清                         | `clarify` | `clarify`     | 恒空   |

三条纪律（统筹 §3.8 + 阶段 1 设计说明 条目 1.3）：

1. **映射收口在此**：``generation`` / ``api`` 适配层调这里，禁止前端用
   「refs 有没有」反推 kind；
2. **取值唯一且穷尽**：``answer_source`` 只由 ``kind`` 派生——不是模型说、
   也不是前端猜；
3. **停止轮不适用**：``kind="stopped"``（用户手动停止）不是一次完整回答，
   ``answer_source`` 保持 ``None``（不写该列）。

外部（库外直答）路径也在这里定：``external`` → ``direct``。它**不受**
``CHAT_DIRECT_ANSWER_ENABLE`` 影响——那条开关管的是「库内未命中怎么办」
（阶段 1 条目 1.6 第 2 条），不是「库外问题答不答」。
"""

from __future__ import annotations

from typing import Literal

# 契约 kind（02 §7.1：normal / clarify / refuse / direct，外加停止轮 stopped）
Kind = Literal["normal", "clarify", "refuse", "direct", "stopped"]

# 契约 answer_source（02 §7.1：kb / general / empty_scope / clarify / web）
# 🔴 2026-09-27 v1.0.11（回归修复，拍板①取 B）：新增第五值 ``web`` —— 纯网页轮 /
# 混合轮（材料含任一网页材料）的角标不再错标「依据知识库」。它**不进** :data:`ANSWER_SOURCE_BY_KIND`
#（那张表仍只由 kind 派生），而是经 :func:`answer_source_for_materials` 在材料落地时覆写。
AnswerSource = Literal["kb", "general", "empty_scope", "clarify", "web"]

# 内部 source_type（统筹 §3.8，与 messages.source_type 同词汇表）
SourceType = Literal["internal_db", "external"]

KINDS: tuple[str, ...] = ("normal", "clarify", "refuse", "direct", "stopped")

# kind → answer_source（**唯一控制点**，见模块 docstring）
ANSWER_SOURCE_BY_KIND: dict[str, str] = {
    "normal": "kb",
    "direct": "general",
    "refuse": "empty_scope",
    "clarify": "clarify",
}

# 恒空的 refs：这三个 kind 不承载引用（direct/refuse/clarify 的引用一律为空）
KINDS_WITH_EMPTY_REFS: frozenset[str] = frozenset({"direct", "refuse", "clarify"})

# 库外直答对应的 kind（external 恒定走 direct）
DIRECT_KIND: Kind = "direct"
REFUSE_KIND: Kind = "refuse"
NORMAL_KIND: Kind = "normal"


def answer_source_for(kind: str | None) -> str | None:
    """``kind`` → ``answer_source``；未知取值与停止轮返回 ``None``（不写该列）。"""
    return ANSWER_SOURCE_BY_KIND.get(kind or "")


def answer_source_for_materials(kind: str | None, materials) -> str | None:
    """``kind`` 派生 + **材料来源覆写**（`回归修复` 修复的唯一控制点，2026-09-27）。

    规则：基值仍由 :func:`answer_source_for` 从 ``kind`` 派生；当基值是 ``kb``
    （= ``normal`` 轮）且材料里**含任一网页材料**（``Material.is_web``）⇒ 覆写为
    ``"web"``。**混合轮口径 = web 一票即走**：角标的职责是告诉用户「这条答案
    用没用互联网」（网页内容未经本仓核实，用户有权一眼看出），逐条出处由引用
    卡片的 ``refs[].type`` 承载；``direct`` / ``refuse`` / ``clarify`` 轮不受影响
    （它们的基值本来就不是 ``kb``）。

    ⚠️ 这是「``answer_source`` 只由 ``kind`` 派生」不变量的**唯一**豁免点
    （02 §7.1 v1.0.11 登记）：调用方只限 Agent 收口装配（``_install_agent_materials``
    与 run_agent_turn 的中断收口），普通路径（基线检索不产网页材料）一律仍走
    :func:`answer_source_for`。
    """
    base = answer_source_for(kind)
    if base == "kb" and any(
        bool(getattr(item, "is_web", False)) for item in (materials or [])
    ):
        return "web"
    return base


def refs_empty_for(kind: str | None) -> bool:
    """该 ``kind`` 的 refs 是否**恒为空**（direct / refuse / clarify 为真）。"""
    return (kind or "") in KINDS_WITH_EMPTY_REFS


def resolve_kind(
    source_type: str,
    *,
    has_materials: bool,
    direct_enabled: bool,
) -> Kind:
    """按 §3.8 定出这一轮回答的契约 ``kind``。

    参数：

    - ``source_type``：路由结论（``internal_db`` 该去库里找 / ``external`` 库外）；
    - ``has_materials``：本轮**实际有没有可用的库内资料**（阶段 1 检索可空，
      阶段 3 接真检索后由它填）。``internal_db`` 且为假 = 「库内未命中」；
    - ``direct_enabled``：``CHAT_DIRECT_ANSWER_ENABLE``——**库内未命中时**
      是否允许库外直答；关掉即拒答（条目 1.6 第 2 条的「定死一种」）。

    判定顺序（clarify 不在这里判——那是 1.4 的入口，故本函数不产出 clarify）：

    - ``external`` → ``direct``（库外问题照答，不受开关影响）；
    - ``internal_db`` + 有资料 → ``normal``；
    - ``internal_db`` + 无资料 + 允许直答 → ``direct``；
    - ``internal_db`` + 无资料 + 不允许 → ``refuse``。
    """
    if source_type == "external":
        return DIRECT_KIND
    if has_materials:
        return NORMAL_KIND
    return DIRECT_KIND if direct_enabled else REFUSE_KIND


def map_round(
    source_type: str,
    *,
    has_materials: bool,
    direct_enabled: bool,
    refs: list | None = None,
) -> tuple[Kind, str | None, list]:
    """一次问答的三个契约字段一起定：``(kind, answer_source, refs)``。

    ``refs`` 只在 ``normal`` 时原样带出；``direct`` / ``refuse`` 恒空
    （即使调用方误传了引用也不会漏出去——这正是「无假引用」的兜底，I7）。
    """
    kind = resolve_kind(
        source_type, has_materials=has_materials, direct_enabled=direct_enabled
    )
    answer_source = answer_source_for(kind)
    final_refs = list(refs or []) if not refs_empty_for(kind) else []
    return kind, answer_source, final_refs


def map_clarify_round() -> tuple[Kind, str | None, list]:
    """澄清轮的三字段：``clarify`` / ``clarify`` / ``[]``（§3.8 第 4 行）。

    澄清不经过路由、也不看有没有资料（它在检索**之前**发生），所以单独给一个
    入口，避免调用方为了拿这三个常量去凑 ``map_round`` 的参数。
    """
    return "clarify", answer_source_for("clarify"), []


__all__ = [
    "ANSWER_SOURCE_BY_KIND",
    "DIRECT_KIND",
    "KINDS",
    "KINDS_WITH_EMPTY_REFS",
    "NORMAL_KIND",
    "REFUSE_KIND",
    "AnswerSource",
    "Kind",
    "SourceType",
    "answer_source_for",
    "answer_source_for_materials",
    "map_clarify_round",
    "map_round",
    "refs_empty_for",
    "resolve_kind",
]
