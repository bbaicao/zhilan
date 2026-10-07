"""Rerank reasons remain per-call facts; prefixes describe the final answer."""

from __future__ import annotations

from dataclasses import asdict

import pytest

from app import retrieval
from app.api import chat_routes
from app.api.chat_schemas import ChatRequest
from app.core.config import Settings, settings
from app.core.errors import AUTH_HINT, TIMEOUT_HINT
from app.retrieval import rerank
from app.retrieval.types import Material
from app.session import events, repository, service
from app.session.attempt import delivered_fingerprint
from tests.api.test_chat_contract import _StubClassifier, client, faux_model, parse_sse  # noqa: F401
from tests.api.test_chat_stream_events import _StreamModel, _messages
from tests.api.test_bug153_stream_error_semantics import _FakeStream, _FailingModel as StreamFailure
from tests.api.test_bug154_sync_error_semantics import _FailingModel as SyncFailure, _error_result
from tests.api.test_turn_prefix_040 import PREFIX, QUESTION, prepare
from tests.retrieval.test_rerank import _bind_rerank_service
from tests.session.test_compaction import FakeCompactionModel

NOTICE = "本次检索未选出通过当前筛选的资料。以下按一般知识提供参考，不代表已核实你的资料。"


def candidates(count=12):
    return [
        Material(chunk_id=i + 1, document_id=1, content=f"片段{i}", filename="source.md")
        for i in range(count)
    ]


def install_rerank(monkeypatch, scores, *, threshold=0.2):
    _bind_rerank_service(monkeypatch)
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", True)
    monkeypatch.setattr(settings, "RAG_MIN_RERANK_SCORE", threshold)

    async def call(config, question, documents):
        if isinstance(scores, Exception):
            raise scores
        return list(scores)

    monkeypatch.setattr(rerank, "_call_rerank", call)


def install_recall(monkeypatch, count=12):
    async def keywords(session, question, filters=None):
        return candidates(count)

    async def vectors(session, question, filters=None):
        return [], None

    monkeypatch.setattr(retrieval.keyword, "search_keywords", keywords)
    monkeypatch.setattr(retrieval.vector, "search_vectors", vectors)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,count,threshold,scores,status,kept,removed,reason",
    [
        ("no_candidates", 0, 0.2, [], "no_candidates", None, None, "no_recall"),
        ("disabled", 12, 0.2, [], "disabled", None, None, None),
        ("unbound", 12, 0.2, [], "unbound", None, None, None),
        ("failed", 12, 0.2, RuntimeError("fake failure"), "failed", None, None, None),
        ("shape_mismatch", 12, 0.2, [0.8], "shape_mismatch", None, None, None),
        ("all_removed", 12, 0.99, [0.1] * 12, "applied", 0, 12, "below_threshold"),
        ("two_removed", 12, 0.2, [0.8] * 10 + [0.16, 0.19], "applied", 10, 2, None),
        ("none_removed", 12, 0.2, [0.8] * 12, "applied", 12, 0, None),
        # 保留历史 nodeid，便于与 OPT0 的 52 项逐例对账。
        pytest.param(
            "zero", 12, 0.0, [0.1] * 12, "applied", 12, 0, None,
            id="zero-12-0.0-scores8-applied-None-None-None",
        ),
        pytest.param(
            "negative", 12, -1.0, [0.1] * 12, "applied", 12, 0, None,
            id="negative-12--1.0-scores9-applied-None-None-None",
        ),
    ],
)
async def test_six_exits_record_stats_at_source(
    monkeypatch, case, count, threshold, scores, status, kept, removed, reason
):
    install_rerank(monkeypatch, scores, threshold=threshold)
    if case in {"no_candidates", "disabled"}:
        monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", False)
    if case == "unbound":
        monkeypatch.setattr(rerank, "resolve_rerank_config", lambda: None)
    stats = rerank.RerankStats()
    result = await rerank.apply_rerank(QUESTION, candidates(count), stats=stats)
    assert stats.status == status
    assert stats.candidates == count
    assert stats.kept == kept and stats.removed == removed
    assert stats.empty_reason == reason
    assert stats.threshold == (threshold if status == "applied" else None)
    assert stats.threshold_applied is (status == "applied" and threshold > 0)
    assert len(result) == (kept if kept is not None else count)
    if case == "two_removed":
        assert stats.removed_score_min == 0.16
        assert stats.removed_score_max == 0.19
    if threshold <= 0:
        assert [item.chunk_id for item in result] == list(range(1, count + 1))
        assert stats.removed_score_min is None and stats.removed_score_max is None
        assert Settings(_env_file=None, RAG_MIN_RERANK_SCORE=threshold).RAG_MIN_RERANK_SCORE == threshold


@pytest.mark.asyncio
@pytest.mark.parametrize("assume", [False, True])
async def test_e_sync_stream_and_history_are_identical(client, db, monkeypatch, assume):
    """三通道一致性（同步 / 流式 / 历史）——门槛删空场景（``ROUTING_MODE=off`` ⇒ external）。

    ⓘ 2026-09-27 注（`回归修复` 拍板④落地的边界说明）：本场景的决策源是
    ``ROUTING_MODE=off`` ⇒ ``source_type="external"``（路由层「不路由视为库外」的既有语义）
    ⇒ ``resolve_kind`` 首行直接落 direct，**不受** 084 的 refuse 口径影响（084 只拦
    **库内语义**轮：路由判 internal_db + 材料被门槛删空 ⇒ refuse）。本用例的期望值因此
    **保持原样**（direct + NOTICE 前缀）；internal_db 场景的 refuse 口径由
    `test_bug084_below_threshold_refuses.py` 承载。
    """
    raw = "一般可先确定主题，再规划镜头。"
    chat = await prepare(db, monkeypatch, faux_model(raw), assume=assume)
    install_recall(monkeypatch)
    install_rerank(monkeypatch, [0.1] * 12, threshold=0.99)
    response = await client.post("/api/chat", json={"chat_id": chat.id, "message": QUESTION})
    expected = NOTICE + "\n\n" + (PREFIX if assume else "") + raw
    assert response.json()["message"] == expected
    assert response.json()["kind"] == "direct"
    assert response.json()["answer_source"] == "general"
    assert response.json()["refs"] == []
    stream_chat = await prepare(db, monkeypatch, _StreamModel([raw]), assume=assume)
    streamed = await client.post(
        "/api/chat/stream", json={"chat_id": stream_chat.id, "message": QUESTION}
    )
    frames = parse_sse(streamed.text)
    assert [f["text"] for f in frames if f["type"] == "token"] == [expected]
    for cid in (chat.id, stream_chat.id):
        assert (await _messages(db, cid))[-1].content == expected
        assert (await client.get(f"/api/chats/{cid}")).json()["messages"][-1]["content"] == expected
        results = await events.list_events_by_type(db, cid, "retrieval/result")
        assert len(results) == 1
        data = results[0].data
        assert data["hits"] == 0 and data["degraded"] is False and data["warning"] is None
        assert data["empty_reason"] == "below_threshold"
        assert data["rerank"]["candidates"] == 12 and data["rerank"]["kept"] == 0
        end = (await events.list_events_by_type(db, cid, "turn/end"))[-1].data
        assert end["final_material_count"] == 0
        assert end["final_empty_reason"] == "below_threshold"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["react", "plan_execute"])
async def test_agent_two_retrievals_keep_distinct_stats_and_baseline(client, db, monkeypatch, mode):
    import json
    from cubeloop.providers import FauxProvider, faux_assistant_message, faux_tool_call
    from tests.api.test_chat_agent import _seed_materials
    from app.agent.tools import knowledge_search

    provider = FauxProvider(provider_id="faux")
    if mode == "react":
        replies = [
            faux_assistant_message([faux_tool_call("knowledge_search", {"query": "first"})]),
            faux_assistant_message([faux_tool_call("knowledge_search", {"query": "second"})]),
            faux_assistant_message("可以回答"),
        ]
    else:
        replies = [faux_assistant_message(json.dumps({"steps": [
            {"tool": "knowledge_search", "args": {"query": "first"}},
            {"tool": "knowledge_search", "args": {"query": "second"}},
        ]}))]
    provider.set_responses(replies + [faux_assistant_message("实际资料的内容 [1]。")])
    chat = await prepare(db, monkeypatch, provider.model("faux"), assume=False)
    materials = await _seed_materials(db, ["实际检索资料"], filename="actual.md")
    install_recall(monkeypatch)
    install_rerank(monkeypatch, [0.1] * 12, threshold=0.99)
    recorded = []

    async def search(session, question, filters=None, *, stats=None):
        stats.status = "applied"
        stats.candidates = 3 if question == "first" else 7
        stats.threshold = 0.2
        stats.threshold_applied = True
        stats.kept = 0 if question == "first" else 1
        stats.removed = stats.candidates - stats.kept
        recorded.append(asdict(stats))
        return ([] if question == "first" else materials), "", None

    monkeypatch.setattr(knowledge_search, "_default_retrieve", lambda: search)
    response = await client.post(
        "/api/chat/stream",
        json={"chat_id": chat.id, "message": QUESTION, "agent_mode": mode},
    )
    frames = parse_sse(response.text)
    assert frames[-1]["type"] == "done", frames
    assert frames[-1]["kind"] == "normal" and frames[-1]["refs"]
    assert NOTICE not in "".join(f["text"] for f in frames if f["type"] == "token")
    # Seeding queried chunks on this connection; end its pre-request MySQL snapshot.
    await db.commit()
    baseline = await events.list_events_by_type(db, chat.id, "retrieval/result")
    assert len(baseline) == 1
    assert baseline[0].data["rerank"]["kept"] == 0
    assert baseline[0].data["empty_reason"] == "below_threshold"
    calls = await events.list_events_by_type(db, chat.id, "tool/call")
    results = await events.list_events_by_type(db, chat.id, "tool/result")
    assert len(calls) == len(results) == len(recorded) == 2
    assert {r.tool_call_id for r in results} == {c.tool_call_id for c in calls}
    assert len({r.tool_call_id for r in results}) == 2
    # 重排字段留在 rerank 内；查询改写与扩链统计属于 retrieval 同级字段。
    # 从独立记录的 dataclass 拆字段，不能用被测 payload() 自证序列化正确。
    assert [r.data["retrieval"]["rerank"] for r in results] == [
        {key: value for key, value in item.items() if key not in {"query_rewrite", "links"}}
        for item in recorded
    ]
    assert [r.data["retrieval"]["query_rewrite"] for r in results] == [
        item["query_rewrite"] for item in recorded
    ]
    assert all("links" not in r.data["retrieval"] for r in results)
    assert [r.data["retrieval"]["empty_reason"] for r in results] == ["below_threshold", None]
    end = (await events.list_events_by_type(db, chat.id, "turn/end"))[-1].data
    assert end["final_material_count"] == 1 and end["final_empty_reason"] is None


@pytest.mark.asyncio
async def test_compaction_input_preserves_notice_verbatim(db, monkeypatch):
    chat = await repository.create_chat(db, title="029 compaction")
    original = NOTICE + "\n\n一般建议不代表目标视频已经核实。"
    for role, content in [
        ("user", "我的夜市视频怎么拍的？"),
        ("assistant", original),
        ("user", "晚点我会补充资料。"),
        ("assistant", "好的，以你提供的资料为准。"),
    ]:
        await repository.add_message(db, chat_id=chat.id, role=role, content=content)
    await db.commit()
    monkeypatch.setattr(settings, "CHAT_COMPRESS_ENABLE", True)
    monkeypatch.setattr(settings, "CHAT_COMPRESS_THRESHOLD", 1)
    monkeypatch.setattr(settings, "CHAT_COMPRESS_KEEP", 2)
    model = FakeCompactionModel("前次回答尚未核实，等待补充资料。")
    await service.build_model_history(db, chat.id, chat=chat, model=model)
    assert model.count == 1
    assert original in model.last_prompt
    assert model.last_prompt.count(NOTICE) == 1
    assert (await _messages(db, chat.id))[1].content == original


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["", TIMEOUT_HINT, AUTH_HINT, "尚未配置模型。", "回答生成失败。"])
@pytest.mark.parametrize("stream", [False, True])
async def test_e_does_not_decorate_empty_or_known_failure(client, db, monkeypatch, raw, stream):
    if raw:
        final = _error_result(error_type="ProviderUnavailable", error_message=raw)
        model = StreamFailure(_FakeStream([], final)) if stream else SyncFailure(final=final)
    else:
        model = _StreamModel([raw]) if stream else faux_model(raw)
    chat = await prepare(db, monkeypatch, model, assume=True)
    install_recall(monkeypatch)
    install_rerank(monkeypatch, [0.1] * 12)
    response = await client.post(
        "/api/chat/stream" if stream else "/api/chat",
        json={"chat_id": chat.id, "message": QUESTION},
    )
    assert NOTICE not in response.text
    assert response.status_code == (
        200 if stream or raw not in {TIMEOUT_HINT, AUTH_HINT}
        else 504 if raw == TIMEOUT_HINT else 401
    )
    if stream:
        frames = parse_sse(response.text)
        assert frames[-1]["type"] == "error"
        assert not [f for f in frames if f["type"] in {"token", "done"}]
    assert [row.role for row in await _messages(db, chat.id)] == ["user"]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [TIMEOUT_HINT, AUTH_HINT, "尚未配置模型。", "回答生成失败。"])
@pytest.mark.parametrize("stream", [False, True])
async def test_e_completed_error_text_keeps_prefix_and_assistant(client, db, monkeypatch, raw, stream):
    """与失败腿同文：只有成功状态改变，错误词不能吞掉正常回答及 E 前缀。"""
    model = _StreamModel([raw]) if stream else faux_model(raw)
    chat = await prepare(db, monkeypatch, model, assume=True)
    install_recall(monkeypatch)
    install_rerank(monkeypatch, [0.1] * 12)
    response = await client.post(
        "/api/chat/stream" if stream else "/api/chat",
        json={"chat_id": chat.id, "message": QUESTION},
    )
    assert response.status_code == 200
    expected = NOTICE + "\n\n" + PREFIX + raw
    if stream:
        frames = parse_sse(response.text)
        assert frames[-1]["type"] == "done"
        assert not [f for f in frames if f["type"] == "error"]
        assert "".join(f["text"] for f in frames if f["type"] == "token") == expected
    else:
        assert response.json()["message"] == expected
    rows = await _messages(db, chat.id)
    assert [row.role for row in rows] == ["user", "assistant"]
    assert rows[-1].content == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("after_body", [False, True])
async def test_e_stop_keeps_only_already_delivered_prefix(client, db, monkeypatch, after_body):
    chat = await prepare(
        db, monkeypatch,
        _StreamModel(["已交付", "未交付"], first_delay=0 if after_body else 10, delay=10),
    )
    install_recall(monkeypatch)
    install_rerank(monkeypatch, [0.1] * 12)
    request_id = "e-stop"
    response = await chat_routes.chat_stream(
        ChatRequest(chat_id=chat.id, message=QUESTION, request_id=request_id), db
    )
    frames = []
    async for data in response.body_iterator:
        frame = parse_sse(data.decode() if isinstance(data, bytes) else data)[0]
        frames.append(frame)
        if frame["type"] == ("token" if after_body else "meta"):
            assert (await client.post("/api/chat/stop", json={"request_id": request_id})).json()["stopped"]
    assert frames[-1]["type"] == "stopped"
    delivered = "".join(f["text"] for f in frames if f["type"] == "token")
    assert delivered == (NOTICE + "\n\n" + PREFIX + "已交付" if after_body else "")
    rows = await _messages(db, chat.id)
    if after_body:
        assert rows[-1].content == delivered and rows[-1].kind == "stopped"
    else:
        assert [r.role for r in rows] == ["user"]
    interrupted = (await events.list_events_by_type(db, chat.id, "stream/interrupted"))[0]
    assert all(interrupted.data[k] == v for k, v in delivered_fingerprint(delivered).items())


@pytest.mark.asyncio
async def test_refuse_and_unfinalized_turn_never_claim_e(client, db, monkeypatch):
    chat = await prepare(db, monkeypatch, _StreamModel([]), assume=False)
    install_recall(monkeypatch)
    install_rerank(monkeypatch, [0.1] * 12)
    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", False)
    monkeypatch.setattr(settings, "ROUTING_MODE", "regex")
    monkeypatch.setattr(chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db"))
    response = await client.post("/api/chat", json={"chat_id": chat.id, "message": QUESTION})
    assert response.json()["kind"] == "refuse"
    assert NOTICE not in response.json()["message"]
    plan = await chat_routes.build_turn_plan(
        db, ChatRequest(message=QUESTION), QUESTION, [], clarify_model=faux_model("")
    )
    plan.materials_finalized = False
    assert chat_routes.final_material_state(plan) == {}
    assert NOTICE not in chat_routes.get_turn_prefix(plan, "正文")


@pytest.mark.asyncio
async def test_save_failure_does_not_fabricate_turn_end(client, db, monkeypatch):
    chat = await prepare(db, monkeypatch, _StreamModel(["正文"]), assume=False)
    install_recall(monkeypatch)
    install_rerank(monkeypatch, [0.1] * 12)

    async def fail(*args, **kwargs):
        raise RuntimeError("injected save failure")

    monkeypatch.setattr(chat_routes, "persist_turn", fail)
    response = await client.post("/api/chat/stream", json={"chat_id": chat.id, "message": QUESTION})
    frames = parse_sse(response.text)
    assert frames[-1]["type"] == "error"
    assert [f["text"] for f in frames if f["type"] == "token"] == [NOTICE + "\n\n正文"]
    assert not await events.list_events_by_type(db, chat.id, "turn/end")


@pytest.mark.asyncio
async def test_leading_whitespace_token_does_not_change_prefix(client, db, monkeypatch):
    chat = await prepare(db, monkeypatch, _StreamModel([" ", "正文"]), assume=False)
    install_recall(monkeypatch)
    install_rerank(monkeypatch, [0.1] * 12)
    response = await client.post("/api/chat/stream", json={"chat_id": chat.id, "message": QUESTION})
    tokens = [f["text"] for f in parse_sse(response.text) if f["type"] == "token"]
    assert tokens == [" ", NOTICE + "\n\n正文"]
    assert (await _messages(db, chat.id))[-1].content == "".join(tokens)


@pytest.mark.asyncio
async def test_partial_filter_event_keeps_hits_and_degraded_semantics(db, monkeypatch):
    from app import observe

    install_rerank(monkeypatch, [0.8] * 10 + [0.16, 0.19])
    stats = rerank.RerankStats()
    materials = await rerank.apply_rerank(QUESTION, candidates(), stats=stats)
    retrieval.number(materials)
    chat = await repository.create_chat(db, title="partial filter")
    await observe.log_retrieval_result(
        db, chat.id, materials=materials, retrieval_id="rl-partial", stats=stats
    )
    result = (await events.list_events_by_type(db, chat.id, "retrieval/result"))[0].data
    assert result["hits"] == 10
    assert result["degraded"] is False and result["empty_reason"] is None
    assert result["rerank"]["removed"] == 2
    # A reused out-parameter must not retain the prior filter's numbers.
    await rerank.apply_rerank(QUESTION, [], stats=stats)
    assert stats.status == "no_candidates"
    assert stats.kept is None and stats.removed is None and stats.threshold is None
