"""U3.5：Agent工具复用同一消歧并维持宿主范围求交。"""

import pytest

from app.agent.bootstrap import build_registry
from app.agent.tools.knowledge_search import KnowledgeSearchArgs, build_spec
from app.retrieval import query as query_mod


@pytest.mark.asyncio
async def test_tool_binds_current_user_question_and_keeps_scope():
    calls = []

    async def retrieve(db, query, filters, *, stats):
        calls.append((query, filters, stats))
        return [], "", None

    registry = build_registry(
        db=object(), filters={"kb_ids": [4, 5], "tags": ["保修"]}, retrieve=retrieve,
        query_history=[{"role": "assistant", "content": "可能保修9999年"}],
        original_question="查AX-02设备，2026年10月记录，不超过3000元，不要翻新机",
    )
    spec = registry.get("knowledge_search")
    observation = await spec.handler(KnowledgeSearchArgs(query="它的保修呢", filters={"kb_ids": [5]}))
    assert len(calls) == 1
    query, scope, stats = calls[0]
    assert "AX-02" in query and "不超过3000元" in query and "不要翻新机" in query
    assert "9999" not in query and scope["kb_ids"] == [5] and scope["tags"] == ["保修"]
    assert observation.retrieval_stats is stats
    assert stats.query_rewrite["status"] == "applied" and stats.query_rewrite["calls"] == 0


@pytest.mark.asyncio
async def test_unresolved_tool_reference_requests_clarify_without_retrieval():
    async def retrieve(*args, **kwargs):
        raise AssertionError("不能猜对象并检索")

    spec = build_spec(
        db=object(), retrieve=retrieve,
        query_history=[{"role": "user", "content": "比较甲方案和乙方案"}],
    )
    observation = await spec.handler(KnowledgeSearchArgs(query="它的实施步骤呢"))
    assert not observation.ok and "确认" in observation.text
    assert observation.retrieval_stats.query_rewrite["status"] == "unresolved"


@pytest.mark.asyncio
async def test_rewrite_rule_failure_still_searches_original(monkeypatch):
    def fail(*args):
        raise RuntimeError("injected")

    calls = []

    async def retrieve(db, query, filters, *, stats):
        calls.append(query)
        return [], "", None

    monkeypatch.setattr(query_mod, "_bind", fail)
    spec = build_spec(db=object(), retrieve=retrieve, original_question="查AX-02设备")
    observation = await spec.handler(KnowledgeSearchArgs(query="它的保修呢"))
    assert calls == ["它的保修呢"]
    assert observation.retrieval_stats.query_rewrite["status"] == "failed"
