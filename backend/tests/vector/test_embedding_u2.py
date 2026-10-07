"""U2.2：离线模拟上游，验证完整响应、重试边界与凭证脱敏。"""

import asyncio
import json
import time
import traceback
from types import SimpleNamespace

import httpx
import pytest

from app.vector import embed as embed_mod


def response(body=None, status=200, *, text=None):
    content = json.dumps(body) if text is None else text
    return httpx.Response(status, content=content.encode("utf-8"))


def row(index=0, vector=None):
    return {"index": index, "embedding": [1, 2, 3] if vector is None else vector}


@pytest.fixture
def wire(monkeypatch):
    cfg = SimpleNamespace(
        provider="offline",
        base_url="https://embedding.invalid/v1",
        api_key="test-secret-never-export",
        model="offline-v1",
    )
    monkeypatch.setattr(
        embed_mod,
        "settings",
        SimpleNamespace(EMBED_DIM=3, embed_config=cfg, has_service_registry=False),
    )
    usage = []
    from app.usage import instrumentation
    monkeypatch.setattr(instrumentation, "record_usage", lambda **data: usage.append(data))
    client_type = httpx.AsyncClient

    def install(responses, *, delay=0):
        calls = []
        pending = list(responses)

        async def route(request):
            calls.append(request)
            if delay:
                await asyncio.sleep(delay)
            outcome = pending.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(
            embed_mod.httpx,
            "AsyncClient",
            lambda **kwargs: client_type(transport=httpx.MockTransport(route), **kwargs),
        )
        return calls

    return install, cfg, usage


@pytest.mark.asyncio
async def test_shuffled_indices_are_restored_and_usage_is_once(wire):
    install, _, usage = wire
    calls = install([response({"data": [row(1, [4, 5, 6]), row(0)], "usage": {"total_tokens": 7}})])
    assert await embed_mod.OpenAICompatEmbedding().embed_texts(["a", "b"]) == [
        [1.0, 2.0, 3.0],
        [4.0, 5.0, 6.0],
    ]
    assert len(calls) == 1
    assert len(usage) == 1 and usage[0]["total_tokens"] == 7


@pytest.mark.parametrize(
    "body",
    [
        None,
        {"data": []},
        {"data": ["not-a-row"]},
        {"data": [{"embedding": [1, 2, 3]}]},
        {"data": [row(False)]},
        {"data": [row("0")]},
        {"data": [row(0.0)]},
        {"data": [row(-1)]},
        {"data": [row(1)]},
        {"data": [row(vector=[1, 2])]},
        {"data": [row(vector="123")]},
        {"data": [row(vector=[True, 2, 3])]},
        {"data": [row(vector=["1", 2, 3])]},
        {"data": [row(vector=[float("nan"), 2, 3])]},
        {"data": [row(vector=[float("inf"), 2, 3])]},
        {"data": [row(vector=[float("-inf"), 2, 3])]},
    ],
    ids=[
        "root",
        "count",
        "row",
        "missing-index",
        "bool-index",
        "str-index",
        "float-index",
        "negative-index",
        "overflow-index",
        "dim",
        "vector-type",
        "bool-value",
        "str-value",
        "nan",
        "inf",
        "negative-inf",
    ],
)
@pytest.mark.asyncio
async def test_invalid_response_is_rejected_without_retry(wire, body):
    install, _, usage = wire
    calls = install([response(body)])
    with pytest.raises(RuntimeError, match="embedding 调用失败"):
        await embed_mod.OpenAICompatEmbedding().embed_texts(["a"])
    assert len(calls) == 1
    assert len(usage) == 1 and usage[0]["total_tokens"] is None


@pytest.mark.asyncio
async def test_duplicate_index_and_invalid_json_are_rejected(wire):
    install, _, usage = wire
    calls = install([response({"data": [row(0), row(0)]}), response(text="{bad-json")])
    client = embed_mod.OpenAICompatEmbedding()
    with pytest.raises(RuntimeError, match="index"):
        await client.embed_texts(["a", "b"])
    with pytest.raises(RuntimeError, match="JSON"):
        await client.embed_texts(["a"])
    assert len(calls) == 2
    assert len(usage) == 2 and all(item["total_tokens"] is None for item in usage)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
@pytest.mark.asyncio
async def test_recoverable_status_retries_once_and_records_both_attempts(wire, monkeypatch, status):
    install, _, usage = wire
    monkeypatch.setattr(embed_mod.OpenAICompatEmbedding, "_RETRY_BACKOFF", 0)
    calls = install([response(status=status), response({"data": [row()]})])
    assert await embed_mod.OpenAICompatEmbedding().embed_texts(["a"]) == [[1.0, 2.0, 3.0]]
    assert len(calls) == 2
    assert len(usage) == 2
    assert [item["meta"]["outcome"] for item in usage] == ["failed", "completed"]
    assert usage[0]["meta"]["call_id"] == usage[1]["meta"]["call_id"]
    assert [item["meta"]["attempt"] for item in usage] == [1, 2]


@pytest.mark.parametrize("status", [400, 401, 403, 501])
@pytest.mark.asyncio
async def test_nonrecoverable_status_does_not_retry_and_hides_keys(wire, status):
    install, cfg, usage = wire
    calls = install(
        [response(status=status, text=f"bad request: {cfg.api_key}; api_key=another-secret")]
    )
    with pytest.raises(RuntimeError, match=f"HTTP {status}") as error:
        await embed_mod.OpenAICompatEmbedding().embed_texts(["a"])
    rendered = "".join(traceback.format_exception(error.value))
    assert cfg.api_key not in rendered and "another-secret" not in rendered
    assert len(calls) == 1
    assert len(usage) == 1 and usage[0]["meta"]["outcome"] == "failed"


@pytest.mark.asyncio
async def test_transport_and_persistent_rate_limit_are_bounded(wire, monkeypatch):
    install, cfg, usage = wire
    monkeypatch.setattr(embed_mod.OpenAICompatEmbedding, "_RETRY_BACKOFF", 0)
    calls = install(
        [
            httpx.ConnectError(f"credential {cfg.api_key}"),
            response({"data": [row()]}),
            response(status=429),
            response(status=429),
            httpx.ConnectError(f"credential {cfg.api_key}"),
            httpx.ConnectError(f"credential {cfg.api_key}"),
        ]
    )
    client = embed_mod.OpenAICompatEmbedding()
    assert await client.embed_texts(["a"]) == [[1.0, 2.0, 3.0]]
    with pytest.raises(RuntimeError, match="HTTP 429"):
        await client.embed_texts(["b"])
    with pytest.raises(RuntimeError, match="ConnectError") as error:
        await client.embed_texts(["c"])
    assert cfg.api_key not in "".join(traceback.format_exception(error.value))
    assert len(calls) == 6
    assert len(usage) == 6
    assert [item["meta"]["outcome"] for item in usage] == [
        "failed", "completed", "failed", "failed", "failed", "failed",
    ]


@pytest.mark.asyncio
async def test_total_deadline_bounds_slow_transport(wire, monkeypatch):
    install, _, usage = wire
    monkeypatch.setattr(embed_mod.OpenAICompatEmbedding, "_RETRY_BACKOFF", 0)
    calls = install([response({"data": [row()]})], delay=1)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="总时限"):
        await embed_mod.OpenAICompatEmbedding(timeout=0.01).embed_texts(["a"])
    assert time.monotonic() - started < 0.3
    assert len(calls) == 1
    assert len(usage) == 1 and usage[0]["meta"]["outcome"] == "stopped"


@pytest.mark.asyncio
async def test_second_batch_failure_returns_no_partial_result(wire):
    install, _, usage = wire
    calls = install([response({"data": [row(i) for i in range(16)]}), response({"data": []})])
    with pytest.raises(RuntimeError, match="条数"):
        await embed_mod.OpenAICompatEmbedding().embed_texts([str(i) for i in range(17)])
    assert len(calls) == 2
    assert len(usage) == 2 and all(item["total_tokens"] is None for item in usage)


@pytest.mark.asyncio
async def test_model_snapshot_and_public_identity_exclude_credentials(wire, monkeypatch):
    install, cfg, _ = wire
    cfg.base_url = f"https://user:{cfg.api_key}@embedding.invalid/v1?api_key={cfg.api_key}#token"
    embed_mod.settings.has_service_registry = True
    embed_mod.settings.roles = {"embed": {"service_id": "svc-original", "model": cfg.model}}
    client = embed_mod.OpenAICompatEmbedding()
    original = client.identity()
    cfg.model = "changed-model"
    embed_mod.settings.roles["embed"]["service_id"] = "svc-changed"
    calls = install([response({"data": [row()]})])
    await client.embed_texts(["a"])
    assert json.loads(calls[0].content)["model"] == "offline-v1"
    assert client.identity() == original == {
        "provider": "offline",
        "service_id": "svc-original",
        "base_url": "https://embedding.invalid/v1",
        "model": "offline-v1",
        "dim": 3,
    }
    assert "test-secret" not in json.dumps(original)
    assert embed_mod.FakeEmbedding(dim=3).identity()["model"] == "fake"
