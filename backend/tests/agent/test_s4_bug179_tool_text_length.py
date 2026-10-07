"""S4 · 回归修复 判据载体：_tool_text_length 记账必须 >= 真实正文长度（保守方向）。

## 规格（交接件 §2 · 179）

- 记账值 **不得小于**真实正文长度 —— 欠记账会让 cap_tool_context 少扣 remaining，
  工具正文总量可以远超 TOOL_CONTEXT_TOTAL_LIMIT（这是硬上限）；
- 标记 / 占位 **仍不占**预算（既有口径：TOOL_CONTEXT_TRUNCATION_MARK /
  TOOL_CONTEXT_OMITTED_MARK / 落盘变体都不计）；
- 单条 <= TOOL_CONTEXT_ITEM_LIMIT(4000) 不得破。

## 判别构造（本件核心）

正文里**内嵌**一个标记形态的子串，后面**还有正文**，且整条以「）」收尾
（结尾是判断「有没有尾部标记」的第一道门）：整条都是工具正文，而旧解析

    head, sep, _tail = text.rpartition(TOOL_CONTEXT_TRUNCATION_PREFIX)

取的是**最后一次**前缀出现的位置 ⇒ 返回 head 长度（= 内嵌标记之前的长度，实测 2 / 1），
真实长度却是 5000+。⇒ 本条要求 >= len(text)：任何靠尾部标记反推的解析都必须先证明
标记真的在**尾部**，否则只能整条计入。

## 隔离说明

本件 monkeypatch runner.tool_artifacts.save -> None：只判「长度记账」，不判落盘
（落盘腿归 artifacts 自己的载体），同时避免测试往仓库 data/ 里写产物。

## 判别力标注（HEAD 实测）

RED：单元腿记 2 < 6016（内嵌截断标记）、1 < 15074（内嵌落盘标记）；
集成腿 6 条合计 36096 远超 12360（欠记账 ⇒ 预算闸失效）。
反向腿全绿（尾部标记不占预算、单条上限不破）。
"""

from __future__ import annotations

import pytest

from app.agent import runner
from app.agent.types import ToolContextItem

INNER_MARK = "（已截断，原 5 字符）"


def _inner_marker_text(repeat: int = 5000) -> str:
    return "xx" + INNER_MARK + "z" * repeat + "（完）"


# ⚠️ 结尾必须是「）」：_tool_text_length 的第一道门是 text.endswith("）")，
# 结尾不是「）」时它会整条计入长度（也就测不到「靠尾部标记反推」这条路径）。
INNER_ARTIFACT_TEXT = (
    "头"
    + runner.TOOL_CONTEXT_ARTIFACT_MARK.format(chars=5, id="0123456789ab")
    + "尾" * 5010
    + "（完）"
)

OMITTED_FORMS = [
    runner.TOOL_CONTEXT_OMITTED_MARK,
    runner.TOOL_CONTEXT_OMITTED_ARTIFACT_MARK.format(id="0123456789ab"),
]


@pytest.fixture(autouse=True)
def _no_artifact_side_effects(monkeypatch):
    monkeypatch.setattr(runner.tool_artifacts, "save", lambda text: None)


def _ctx_item(index: int, text: str) -> ToolContextItem:
    return ToolContextItem(
        tool="knowledge_search",
        tool_call_id="s4-179-" + str(index),
        ok=True,
        text=text,
        collected_at="2026-01-01 00:00:00",
    )


# ── 正向 discriminator ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text",
    [_inner_marker_text(), INNER_ARTIFACT_TEXT],
    ids=["truncation-mark-inside-body", "artifact-mark-inside-body"],
)
def test_bug179_inner_marker_body_is_counted_in_full(text):
    counted = runner._tool_text_length(text)
    assert counted >= len(text), (
        "记账 " + str(counted) + " < 真实 " + str(len(text)) + "：正文内嵌标记形态子串时欠记账"
    )


def test_bug179_total_budget_is_not_bypassed_by_inner_markers():
    texts = ["xx" + INNER_MARK + "z" * 6000 + "（完）" for _ in range(6)]
    items = [_ctx_item(i, text) for i, text in enumerate(texts)]

    capped = runner.cap_tool_context(items)

    assert len(capped) == len(items), "被截的条目必须留在通道里（转占位），不得凭空消失"
    total = sum(len(item.text) for item in capped)
    slack = 60 * len(items)
    assert total <= runner.TOOL_CONTEXT_TOTAL_LIMIT + slack, (
        "通道正文合计 " + str(total) + " 字符，超出总量上限 "
        + str(runner.TOOL_CONTEXT_TOTAL_LIMIT) + "（允差 +" + str(slack) + "）："
        "欠记账让预算闸失效"
    )


@pytest.mark.parametrize("mark", OMITTED_FORMS, ids=["omitted", "omitted-artifact"])
def test_bug179_omitted_prefix_with_body_is_counted_in_full(mark):
    text = mark + "z" * 3900 + "（完）"
    assert runner.clip_tool_context_text(text) == text
    assert runner._tool_text_length(text) == len(text)


@pytest.mark.parametrize("mark", OMITTED_FORMS, ids=["omitted", "omitted-artifact"])
def test_bug179_total_budget_is_not_bypassed_by_omitted_prefix(mark):
    text = mark + "z" * 3900 + "（完）"
    items = [
        _ctx_item(i, runner.clip_tool_context_text(text))
        for i in range(6)
    ]

    capped = runner.cap_tool_context(items)

    assert len(capped) == len(items)
    # 不借被测记账函数验预算：直接量通道文本，只放宽实际可生成的固定短标记。
    marker_slack = max(
        len(runner.TOOL_CONTEXT_OMITTED_MARK),
        len(runner.TOOL_CONTEXT_TRUNCATION_MARK.format(chars=len(text))),
    ) * len(items)
    assert sum(len(item.text) for item in capped) <= (
        runner.TOOL_CONTEXT_TOTAL_LIMIT + marker_slack
    )


@pytest.mark.parametrize(
    "text",
    [
        runner.TOOL_CONTEXT_OMITTED_PREFIX + "，附带正文）",
        runner.TOOL_CONTEXT_OMITTED_ARTIFACT_MARK.format(id="0123456789a"),
        runner.TOOL_CONTEXT_OMITTED_ARTIFACT_MARK.format(id="0123456789abc"),
        runner.TOOL_CONTEXT_OMITTED_ARTIFACT_MARK.format(id="0123456789AG"),
        "正文" + runner.TOOL_CONTEXT_OMITTED_MARK,
    ],
)
def test_bug179_malformed_omitted_marker_is_counted_in_full(text):
    assert runner._tool_text_length(text) == len(text)


# ── 反向保护腿：既有口径不得破 ───────────────────────────────────────────────

def test_bug179_reverse_tail_marker_forms_still_do_not_take_budget():
    body = "z" * 100
    assert runner._tool_text_length(body) == 100
    assert runner._tool_text_length(
        body + runner.TOOL_CONTEXT_TRUNCATION_MARK.format(chars=5000)
    ) == 100, "尾部截断标记不得占预算（既有口径）"
    assert runner._tool_text_length(
        body + runner.TOOL_CONTEXT_ARTIFACT_MARK.format(chars=5000, id="0123456789ab")
    ) == 100, "尾部落盘标记不得占预算（既有口径）"
    assert runner._tool_text_length(runner.TOOL_CONTEXT_OMITTED_MARK) == 0
    assert runner._tool_text_length(
        runner.TOOL_CONTEXT_OMITTED_ARTIFACT_MARK.format(id="0123456789ab")
    ) == 0


def test_bug179_reverse_single_item_limit_is_not_exceeded():
    clipped = runner.clip_tool_context_text("z" * 9000)
    assert runner.TOOL_CONTEXT_TRUNCATION_PREFIX in clipped, "单条超限必须带截断标记"
    assert 0 < len(clipped) <= runner.TOOL_CONTEXT_ITEM_LIMIT
    counted = runner._tool_text_length(clipped)
    assert counted <= runner.TOOL_CONTEXT_ITEM_LIMIT, "记账值本身也不得超单条上限"
    # 反向口径：占预算的是**正文预览**（截断标记本身不计），所以记账值必须与
    # len(clipped) 同量级（差值只允许标记那一小段），不得因标记形态被少记成 0。
    assert counted >= len(clipped) - 100, (
        "记账 " + str(counted) + " 明显小于整条长度 " + str(len(clipped)) + "：真截断的正文被少记"
    )
