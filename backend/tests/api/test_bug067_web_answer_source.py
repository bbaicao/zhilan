"""`回归修复` 行为判据载体：**联网材料轮 ⇒ `answer_source="web"`**（覆写唯一控制点）。

## 腿表

| 腿 | 场景 | 期望 |
|---|---|---|
| ① | normal + 纯网页材料 | `web`（主腿） |
| ② | normal + 混合（web + 库内） | `web`（混合轮一票即走，口径见 source_map docstring） |
| ③ | normal + 纯库内材料 | `kb`（不受波及） |
| ④ | direct + 网页材料（构造性场景） | `general`（非 normal 轮不覆写） |
| ⑤ | normal + 零材料 | `kb` |
| ⑥ | `_install_agent_materials` 装配含网页的快照 | `plan.answer_source == "web"`（收口装配点） |
| ⑦ | `_install_agent_materials` 装配纯库内快照 | `plan.answer_source == "kb"`（防过修） |

⚠️ **边界（如实登记）**：本载体不跑真模型、不起真 Agent 轮、不起浏览器 ⇒
前端渲染腿（`MessageItem.tsx` 的 `web` 行）为**静态改动**，本仓无前端测试框架
（口径同 回归检查），由该文件的映射行 + 02 v1.0.11 承载。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.api import chat_routes
from app.generation import source_map
from app.retrieval.types import Material


def _web_material(seq: int = 1) -> Material:
    """网页材料：`url` 非空 ⇒ `is_web` 真假立判（库内材料 url 恒 None）。"""
    return Material(
        chunk_id=-seq,
        document_id=None,
        content=f"网页正文{seq}",
        filename="网页标题",
        block_ordinal=seq,
        url=f"https://example.com/page-{seq}",
    )


def _kb_material(seq: int = 1) -> Material:
    return Material(
        chunk_id=seq,
        document_id=seq,
        content=f"库内正文{seq}",
        filename="库内文档.md",
        block_ordinal=seq,
    )


# --------------------------------------------------------------------------
# 覆写函数（唯一控制点）
# --------------------------------------------------------------------------


def test_pure_web_round_maps_to_web():
    """腿①（主腿）：normal + 纯网页材料 ⇒ `web`。"""
    assert source_map.answer_source_for_materials("normal", [_web_material()]) == "web"


def test_mixed_round_maps_to_web():
    """腿②：混合轮（web + 库内）⇒ `web`（一票即走）。"""
    assert (
        source_map.answer_source_for_materials("normal", [_kb_material(), _web_material()])
        == "web"
    )


def test_kb_only_round_stays_kb():
    """腿③：纯库内材料 ⇒ `kb`（067 不得波及库内轮）。"""
    assert source_map.answer_source_for_materials("normal", [_kb_material()]) == "kb"


def test_non_normal_kinds_are_never_overwritten():
    """腿④：`direct` / `refuse` / `clarify` 即便（构造性地）带网页材料也不覆写。"""
    web = [_web_material()]
    assert source_map.answer_source_for_materials("direct", web) == "general"
    assert source_map.answer_source_for_materials("refuse", web) == "empty_scope"
    assert source_map.answer_source_for_materials("clarify", web) == "clarify"


def test_empty_materials_stay_kb():
    """腿⑤：normal + 零材料 ⇒ `kb`（空手轮收口归 076 的 direct，不经这里覆写）。"""
    assert source_map.answer_source_for_materials("normal", []) == "kb"
    assert source_map.answer_source_for_materials("normal", None) == "kb"


# --------------------------------------------------------------------------
# 收口装配点（Agent 收口的真实落笔处）
# --------------------------------------------------------------------------


def _plan() -> SimpleNamespace:
    return SimpleNamespace(
        materials=[],
        materials_text="",
        all_refs=[],
        refs=[],
        kind="normal",
        answer_source="kb",
        source_type="internal_db",
        retrieval_id="rl-067",
    )


def test_install_agent_materials_with_web_snapshot_sets_web():
    """腿⑥：装配含网页材料的快照 ⇒ `plan.answer_source == "web"`（067 的收口装配点）。"""
    plan = _plan()
    chat_routes._install_agent_materials(plan, [_kb_material(1), _web_material(2)])

    assert plan.kind == "normal"
    assert plan.answer_source == "web", f"收口装配点未覆写：{plan.answer_source}"
    assert plan.source_type == "internal_db"
    assert len(plan.all_refs) == 2


def test_install_agent_materials_kb_only_stays_kb():
    """腿⑦（防过修）：纯库内快照 ⇒ `kb` 逐字不变。"""
    plan = _plan()
    chat_routes._install_agent_materials(plan, [_kb_material(1), _kb_material(2)])

    assert plan.kind == "normal"
    assert plan.answer_source == "kb"
