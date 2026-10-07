"""U3.5：消歧只绑定用户原文/选择标签，不调真实模型。"""

from dataclasses import replace

import pytest

from app.cache.entry import CacheKeyInputs, build_key, rebuild_stats
from app.retrieval import query as query_mod
from app.retrieval.rerank import RerankStats


def user(content):
    return {"role": "user", "content": content}


def assistant(content):
    return {"role": "assistant", "content": content}


def test_normal_question_does_not_read_history_or_change_scope():
    class UnreadableHistory:
        def __iter__(self):
            raise AssertionError("普通问题不能读取消歧历史")

    scope = {"kb_ids": [4], "tags": ["保修"]}
    result = query_mod.rewrite_query("索尼WH-1000XM5保修期限", UnreadableHistory(), filters=scope)
    assert (result.retrieval_query, result.status, result.trigger, result.calls) == (
        result.original_question, "unchanged", "none", 0
    )
    assert scope == {"kb_ids": [4], "tags": ["保修"]}
    assert result.elapsed_ms >= 0 and result.version == query_mod.QUERY_REWRITE_VERSION


@pytest.mark.parametrize("followup", ["它的保修期限呢？", "上一个的保修期限是什么？"])
def test_reference_preserves_user_constraints_but_excludes_assistant_guesses(followup):
    known = "查索尼WH-1000XM5，2026年10月之后的记录，预算不超过3000元，不要翻新机。"
    result = query_mod.rewrite_query(followup, [
        user(known), assistant("可能售价9999元，保修八年。"),
    ])
    assert result.status == "applied" and known in result.retrieval_query
    assert followup in result.retrieval_query and result.original_question == followup
    assert "9999" not in result.retrieval_query and "八年" not in result.retrieval_query
    assert result.calls == 0


@pytest.mark.parametrize("question", ["第二种的实施步骤？", "《第一种实践》的第二种实施步骤？"])
def test_user_numbered_selection_only_keeps_selected_item_and_global_constraints(question):
    history = [user("预算不超过3000元，2026年10月之后，不要联网。\n1. 甲方案：需6000元\n2. 乙方案：需2000元")]
    result = query_mod.rewrite_query(question, history)
    assert result.status == "applied"
    assert "预算不超过3000元" in result.retrieval_query and "不要联网" in result.retrieval_query
    assert "乙方案：需2000元" in result.retrieval_query and "6000" not in result.retrieval_query


def test_assistant_selection_label_is_not_factualized():
    known = "请给不超过3000元的离线方案，不要云服务，设备型号AX-02。"
    result = query_mod.rewrite_query("第二种支持AX-02吗？", [
        user(known),
        assistant("1. SQLite方案：可能免费\n2. Qdrant方案：预计7777元，免费试用十年"),
    ])
    assert result.status == "applied" and known in result.retrieval_query
    assert "Qdrant方案" in result.retrieval_query and "SQLite方案" not in result.retrieval_query
    assert all(value not in result.retrieval_query for value in ("7777", "十年", "预计"))


@pytest.mark.parametrize("history", [
    [user("比较甲方案和乙方案"), assistant("可能更推荐甲方案")],
    [assistant("它可能指甲方案，价格3000元")],
    [user("问题"), assistant("对象是甲方案")],
    [user("今天天气怎么样"), assistant("天气晴朗")],
])
def test_ambiguous_or_only_assistant_context_requires_existing_clarify(history):
    result = query_mod.rewrite_query("它的实施步骤是什么？", history)
    assert result.needs_clarify and result.retrieval_query == result.original_question
    assert result.calls == 0


def test_selected_option_remains_bound_across_followups():
    history = [
        user("预算不超过3000元，不要云服务，给我两个离线方案"),
        assistant("1. SQLite方案：免费\n2. Qdrant方案：预计4000元"),
        user("第二种的实施步骤？"),
        assistant("步骤可能包括付款4000元"),
    ]
    result = query_mod.rewrite_query("它支持AX-02吗？", history)
    assert result.status == "applied" and "Qdrant方案" in result.retrieval_query
    assert "不超过3000元" in result.retrieval_query and "不要云服务" in result.retrieval_query
    assert "4000" not in result.retrieval_query


def test_user_constraints_in_a_separate_turn_follow_the_explicit_object():
    result = query_mod.rewrite_query("它的保修期限呢？", [
        user("查看AX-02设备"),
        assistant("可能售价9999元"),
        user("预算不超过3000元，2026年10月之后，不要翻新机。"),
        assistant("预计都可以"),
    ])
    assert result.status == "applied" and "AX-02" in result.retrieval_query
    assert "不超过3000元" in result.retrieval_query and "不要翻新机" in result.retrieval_query
    assert "2026年10月之后" in result.retrieval_query and "9999" not in result.retrieval_query


def test_rule_failure_falls_back_to_original(monkeypatch):
    def fail(*args):
        raise RuntimeError("injected")

    monkeypatch.setattr(query_mod, "_bind", fail)
    result = query_mod.rewrite_query("它的型号呢？", [user("查AX-02设备")])
    assert result.fallback and result.retrieval_query == "它的型号呢？"
    assert not result.needs_clarify and result.calls == 0


@pytest.mark.parametrize("question", [
    "AX-02设备怎么样，它支持离线吗？", "查《它的故事》资料", "《它的故事》这份文档怎样？",
])
def test_same_question_supplies_object_without_history_rewrite(question):
    result = query_mod.rewrite_query(question)
    assert result.retrieval_query == question and result.trigger == "none"


def test_exact_cache_changes_with_strategy_retrieval_query_and_raw_history(monkeypatch):
    inputs = CacheKeyInputs(
        question="它的价格", filters={"kb_ids": [4]}, agent_mode="off", skill_ids=[],
        model_label="faux/chat", memory_text="", history=[user("相同压缩摘要")],
        has_images=False, retrieval_config={}, source_type="internal_db",
        retrieval_query="AX-02价格", rewrite_history=[user("只查AX-02，不要AX-03")],
    )
    first = build_key(inputs)
    assert build_key(inputs) == first
    assert build_key(replace(inputs, retrieval_query="AX-03价格")) != first
    assert build_key(replace(inputs, rewrite_history=[user("只查AX-03")])) != first
    monkeypatch.setattr(query_mod, "QUERY_REWRITE_VERSION", "u3-query-next")
    assert build_key(inputs) != first
    normal = replace(inputs, question="型号AX-02价格", retrieval_query=None, rewrite_history=None)
    next_key = build_key(normal)
    monkeypatch.setattr(query_mod, "QUERY_REWRITE_VERSION", "u3-query-v1")
    assert build_key(normal) != next_key


def test_cache_stats_restore_query_rewrite_and_links():
    stats = RerankStats(
        status="applied", candidates=3, kept=1, threshold_applied=True,
        query_rewrite={"status": "applied", "calls": 0, "strategy_version": "u3-query-v1"},
        links={"added": 1, "cap": 3},
    )
    restored = rebuild_stats(stats.payload())
    assert restored is not None and restored.payload() == stats.payload()
