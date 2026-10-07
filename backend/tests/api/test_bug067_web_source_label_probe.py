"""`回归修复` 判据载体（**修复验证** · 原定位探针就地改写）。

## 沿革（照 090/076 探针就地改写先例）

本文件原为 067 的**定位探针**（钉「`answer_source` 封闭四值、无 web 档」的现状）。
2026-09-27 拍板①（取 B：新增 `answer_source="web"`）落地后，实现已改，
按「实现已改须重核」授权**就地改写**为修复验证载体：

- `ANSWER_SOURCE_BY_KIND` 的取值域**仍是四值**（`kb` / `general` / `empty_scope` / `clarify`）
  ——kind→source 的基表**一字未变**；新值 `web` 不进基表；
- 覆写走**新的唯一控制点** `source_map.answer_source_for_materials(kind, materials)`：
  基值为 `kb` 且材料含任一网页材料 ⇒ `"web"`（**混合轮口径 = web 一票即走**，
  理由见该函数 docstring 与 02 §7.1 v1.0.11）；
- `answer_source_for(kind)` 的签名**保持 `["kind"]`**（普通路径零改动）——原探针的
  「入参只有 kind」腿保留，但语义从「结构上不可能分流」改为「基表派生不受影响」。

## 修复口径（两条，不得含糊）

1. **修复的形态** = 纯网页轮 / 混合轮的 `answer_source="web"`、落库白名单放行、
   前端 `MessageItem.tsx` 渲染「依据联网资料」；`direct` / `refuse` / `clarify` 轮不受影响。
2. **不变量豁免的唯一性**：「`answer_source` 只由 `kind` 派生」这条不变量自 v1.0.11 起
   带**一个**显式豁免点（`answer_source_for_materials`），调用方只限 Agent 收口装配；
   ⛔ 不得再在别处按来源改写 `answer_source`。
"""

from __future__ import annotations

from typing import get_args

from app.generation import source_map


def test_answer_source_kind_table_is_still_the_closed_four_value_base():
    """基表不变：`ANSWER_SOURCE_BY_KIND` 取值域**仍是四值**，`web` 不在基表里（覆写不进表）。"""
    values = set(source_map.ANSWER_SOURCE_BY_KIND.values())
    assert values == {"kb", "general", "empty_scope", "clarify"}, (
        f"基表取值域变了：{sorted(values)} ⇒ kind→source 的派生面被改，须重核本载体"
    )
    assert "web" not in values, "web 进了基表 ⇒ 覆写应走 answer_source_for_materials，不该改基表"
    assert "web" in get_args(source_map.AnswerSource), "契约 Literal 未扩第五值"


def test_answer_source_for_signature_unchanged_and_base_derivation_intact():
    """普通路径不变：`answer_source_for(kind)` 签名仍是 `["kind"]`，`normal → kb` 逐字保持。"""
    assert source_map.answer_source_for("normal") == "kb"
    import inspect

    params = list(inspect.signature(source_map.answer_source_for).parameters)
    assert params == ["kind"], (
        f"`answer_source_for` 的入参变了（{params}）⇒ 普通路径被波及，须重核"
    )


def test_repository_persistence_whitelist_admits_web():
    """落库白名单同步扩第五值（否则 web 轮落库即 500）。"""
    from app.session import repository

    assert "web" in repository.ANSWER_SOURCES
    assert repository.ANSWER_SOURCES == ("kb", "general", "empty_scope", "clarify", "web")
