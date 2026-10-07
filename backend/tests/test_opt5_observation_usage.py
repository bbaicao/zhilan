"""OPT5: actual sends, nullable receipts, attribution boundaries and safe unknown errors.

Every HTTP request below uses MockTransport; no model or network is used.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from cubeloop.providers.openai import OpenAIProvider

from app.usage import instrumentation as observed
from tests.api.t0_wire import wire_sse_text


def _sse(usage=None):
    body = wire_sse_text("控制回答")
    if usage is None:
        return body
    receipt = json.dumps({
        "id": "chatcmpl-wire", "object": "chat.completion.chunk", "created": 0,
        "model": "controlled", "choices": [], "usage": usage,
    })
    return body.replace("data: [DONE]", f"data: {receipt}\n\ndata: [DONE]")


def _provider(handler, *, kind="chat", instrument=True):
    provider = OpenAIProvider(
        api_key="synthetic-offline-key", base_url="https://controlled.invalid/v1",
        provider_id="controlled",
    )
    provider._client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider._client.max_retries = 1
    if instrument:
        observed.observe_provider(provider, kind=kind)
    return provider


@pytest.fixture
def rows(monkeypatch):
    records = []
    monkeypatch.setattr(observed, "record_usage", lambda **values: records.append(values))
    return records


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", [None, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                                    {"prompt_tokens": 120, "completion_tokens": 7, "total_tokens": 127}])
async def test_actual_receipt_unknown_zero_and_answer_no_duplicate(rows, receipt):
    from app.api.chat_routes import record_answer_usage
    provider = _provider(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=_sse(receipt),
    ))
    try:
        with observed.usage_scope(role="main_answer", assistant_attempt_id="answer-a"):
            result = await provider.model("controlled").generate("question")
        await record_answer_usage(None, None, result.content)
        assert len(rows) == 1
        assert rows[0]["meta"]["assistant_attempt_id"] == "answer-a"
        assert rows[0]["meta"]["usage_received"] is (receipt is not None)
        assert rows[0]["meta"]["vendor_cost"] is None
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            assert rows[0][field] == (receipt[field] if receipt is not None else None)
    finally:
        await provider._client.close()


@pytest.mark.asyncio
async def test_sdk_retry_counts_each_send_and_preserves_payload(rows):
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(503, json={"error": {"message": "controlled retry"}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=_sse({"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}))
    provider = _provider(handler)
    control_bodies = []
    control = _provider(lambda request: (
        control_bodies.append(json.loads(request.content)) or
        httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse())
    ), instrument=False)
    try:
        await provider.model("controlled").generate("question")
        await control.model("controlled").generate("question")
        assert bodies == [control_bodies[0], control_bodies[0]]
        assert len(rows) == 2
        assert [row["meta"]["outcome"] for row in rows] == ["failed", "completed"]
        assert [row["meta"]["attempt"] for row in rows] == [1, 2]
        assert [row["meta"]["retry"] for row in rows] == [False, True]
        assert rows[0]["meta"]["call_id"] == rows[1]["meta"]["call_id"]
        assert rows[0]["total_tokens"] is None and rows[1]["total_tokens"] == 4
    finally:
        await provider._client.close()
        await control._client.close()


@pytest.mark.asyncio
async def test_same_provider_concurrent_roles_db_and_attempts_do_not_cross(rows):
    provider = _provider(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=_sse(),
    ), kind="vision")
    first_db, second_db = object(), object()
    async def call(role, attempt, db):
        with observed.usage_scope(role=role, assistant_attempt_id=attempt, db=db):
            await provider.model("controlled").generate(role)
    try:
        await asyncio.gather(call("main_answer", "a", first_db), call("judge", "b", second_db))
        assert len(rows) == 2
        keyed = {row["meta"]["role"]: row for row in rows}
        assert keyed["main_answer"]["db"] is first_db
        assert keyed["main_answer"]["meta"]["assistant_attempt_id"] == "a"
        assert keyed["judge"]["db"] is second_db
        assert keyed["judge"]["meta"]["assistant_attempt_id"] == "b"
        assert all(row["kind"] == "vision" for row in rows)
        assert len({row["meta"]["call_id"] for row in rows}) == 2
    finally:
        await provider._client.close()


class _WaitingStream(httpx.AsyncByteStream):
    def __init__(self):
        self.receipt_seen = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        yield _sse({"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}).replace(
            "data: [DONE]\n\n", "",
        ).encode()
        self.receipt_seen.set()
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_mode", ["aclose", "cancel"])
async def test_stopped_stream_keeps_received_usage_and_send_identity(rows, stop_mode):
    wire = _WaitingStream()
    provider = _provider(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=wire,
    ))
    try:
        with observed.usage_scope(role="main_answer", assistant_attempt_id="stopped-a"):
            stream = await provider.model("controlled").stream("question")
        await asyncio.wait_for(wire.receipt_seen.wait(), 2)
        if stop_mode == "aclose":
            async def consumer():
                try:
                    async for event in stream:
                        yield event
                finally:
                    await observed.stop_provider_stream(stream)
            source = consumer()
            await source.__anext__()
            await source.aclose()
        else:
            # SDK producer cancellation is the same operation a canceled HTTP consumer triggers.
            stream._producer_task.cancel()
            await asyncio.gather(stream._producer_task, return_exceptions=True)
        assert wire.closed
        assert len(rows) == 1
        assert rows[0]["meta"]["outcome"] == "stopped"
        assert rows[0]["meta"]["assistant_attempt_id"] == "stopped-a"
        assert rows[0]["total_tokens"] == 6
    finally:
        await provider._client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["embed", "rerank"])
async def test_native_http_failure_and_receipt_each_count_once(rows, kind):
    attempts = 0
    def handler(request):
        nonlocal attempts
        attempts += 1
        return (httpx.Response(503, json={"error": "controlled"}) if attempts == 1 else
                httpx.Response(200, json={"usage": {"input_tokens": 4}}))
    config = SimpleNamespace(provider="controlled", model="native")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with observed.usage_scope(role="agent_decision", assistant_attempt_id="native-a"):
            for attempt in (1, 2):
                await observed.observed_post(client, "https://controlled.invalid", config=config,
                                             kind=kind, call_id="native-call", attempt=attempt, json={})
    assert len(rows) == 2
    assert [row["meta"]["outcome"] for row in rows] == ["failed", "completed"]
    assert all(row["meta"]["role"] == kind for row in rows)
    assert rows[0]["prompt_tokens"] is None and rows[1]["prompt_tokens"] == 4
    assert all(row["total_tokens"] is None and row["meta"]["vendor_cost"] is None for row in rows)
    assert all(row["meta"]["assistant_attempt_id"] == "native-a" for row in rows)


@pytest.mark.asyncio
async def test_no_send_and_failed_send_do_not_become_successful_answer_rows(rows):
    def handler(request):
        raise httpx.ConnectError("controlled transport failure", request=request)
    provider = _provider(handler)
    provider._client.max_retries = 0
    try:
        assert rows == []
        await provider.model("controlled").generate("question")
        assert len(rows) == 1
        assert rows[0]["meta"]["outcome"] == "failed"
        assert rows[0]["meta"]["http_status"] is None
        assert rows[0]["total_tokens"] is None
    finally:
        await provider._client.close()


@pytest.mark.asyncio
async def test_recall_rerank_selection_marker_and_chunk_are_independent_of_support(monkeypatch):
    from app import retrieval
    from app.api.chat_routes import final_material_state
    from app.generation.citation import attribution_rows, refs_from_materials, resolve_citations
    from app.observe.citation import classify_citations
    from app.retrieval.types import Material, material_identities
    from app.retrieval.rerank import RerankStats
    main = Material(chunk_id=41, document_id=10, content="项目批准金额为100元。", kb_id=2,
                    span_start=0, span_end=15, covered_ordinals=(0,))
    dropped = Material(chunk_id=42, document_id=11, content="无关材料", kb_id=2)
    async def keywords(*args, **kwargs):
        return [main, dropped]
    async def vectors(*args, **kwargs):
        return [main], None
    async def links(_session, materials, **kwargs):
        return materials
    async def window(_session, materials, *args):
        return [replace(item, content="批准金额为100元。", span_start=2) if item.chunk_id == 41 else item
                for item in materials]
    async def scores(*args):
        return [0.9, 0.1]
    monkeypatch.setattr(retrieval.keyword, "search_keywords", keywords)
    monkeypatch.setattr(retrieval.vector, "search_vectors", vectors)
    monkeypatch.setattr(retrieval.graph, "enabled", lambda: False)
    monkeypatch.setattr(retrieval.link, "expand_materials", links)
    monkeypatch.setattr(retrieval.windows, "expand_windows", window)
    monkeypatch.setattr(retrieval.rerank, "enabled", lambda: True)
    monkeypatch.setattr(retrieval.rerank, "resolve_rerank_config", lambda: SimpleNamespace(model="controlled"))
    monkeypatch.setattr(retrieval.rerank, "_call_rerank", scores)
    monkeypatch.setattr(retrieval.rerank.settings, "RAG_MIN_RERANK_SCORE", 0.5)
    stats = RerankStats()
    materials, rendered, _ = await retrieval.retrieve(None, "批准金额", stats=stats)
    assert len(stats.recall["keyword"]) == 2
    assert stats.recall["keyword"][0]["ordinal"] is None
    assert len(stats.rerank_input) == 2 and len(stats.rerank_output) == 2
    assert stats.recall["keyword"][0]["content_sha256"] != stats.rerank_input[0]["content_sha256"]
    assert stats.selected[0]["ordinal"] == 1
    assert len(stats.selected) == 1
    assert stats.selected == material_identities(materials)
    assert "100元" in rendered and "无关材料" not in rendered
    plan = SimpleNamespace(materials_finalized=True, retrieval_stats=stats, materials=materials)
    assert final_material_state(plan)["final_materials"] == stats.selected
    all_refs = refs_from_materials(materials, retrieval_id="retrieval-a")
    for answer in ("批准金额为100元[1]。", "批准金额为900元[1]。"):
        refs, mode = resolve_citations(answer, all_refs)
        assert mode == "cited"
        events = classify_citations(answer, all_refs)
        used = next(payload for kind, payload in events if kind == "citation/used")
        assert used["ordinal"] == 1 and used["semantic_support"] is None
        attributed = attribution_rows(7, materials, refs, retrieval_id="retrieval-a")
        assert attributed[0]["chunk_id"] == 41
        assert attributed[0]["meta"]["semantic_support"] is None
    assert classify_citations("金额为100元[99]。", all_refs)[0][0] == "citation/unresolved"


@pytest.mark.asyncio
async def test_unknown_500_safe_response_and_real_cause_masked(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from main import unknown_error_handler
    from tests.test_bug070_app_log_sink import _sink
    app = FastAPI()
    app.add_exception_handler(Exception, unknown_error_handler)
    mirror = io.StringIO()
    monkeypatch.setattr(sys, "stderr", mirror)
    @app.get("/controlled-error")
    async def controlled():
        try:
            raise ValueError("password=synthetic-cause")
        except ValueError as cause:
            raise RuntimeError("api_key=synthetic-outer") from cause
    with _sink(tmp_path):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                     base_url="http://offline") as client:
            response = await client.get("/controlled-error")
        assert response.status_code == 500
        assert response.json() == {"detail": "服务处理失败，请稍后重试；如持续出现，请查看应用日志。"}
        logfile = (tmp_path / "zhilan.log").read_text(encoding="utf-8")
        assert "ValueError" in logfile and "RuntimeError" in logfile
        assert "direct cause" in logfile and "controlled" in logfile
        assert "synthetic-cause" not in logfile and "synthetic-outer" not in logfile
        assert "synthetic" not in response.text
        assert "ValueError" in mirror.getvalue() and "RuntimeError" in mirror.getvalue()
        assert "direct cause" in mirror.getvalue()
        assert "synthetic-cause" not in mirror.getvalue() and "synthetic-outer" not in mirror.getvalue()
