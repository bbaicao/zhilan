"""Observe actual provider sends; absent receipts stay unknown, including retries."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

import httpx

from app.usage import record_usage

log = logging.getLogger(__name__)
_scope = ContextVar("provider_usage_scope", default={})
_call = ContextVar("provider_usage_call", default=None)


@contextmanager
def usage_scope(**values):
    token = _scope.set({**_scope.get(), **{k: v for k, v in values.items() if v is not None}})
    try:
        yield
    finally:
        _scope.reset(token)


def _tokens(usage, *names):
    if not isinstance(usage, dict):
        return None
    value = next((usage[name] for name in names if name in usage), None)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def record_provider_attempt(
    *, kind, provider, model, role, call_id, attempt=1, outcome, usage=None, db=None, **meta
):
    """One real send, even if validation or the later application operation fails."""
    context = _scope.get()
    record_usage(
        kind=kind, provider=provider, model=model,
        prompt_tokens=_tokens(usage, "prompt_tokens", "input_tokens"),
        completion_tokens=_tokens(usage, "completion_tokens", "output_tokens"),
        total_tokens=_tokens(usage, "total_tokens"),
        db=db if db is not None else context.get("db"),
        meta={
            "role": context.get("role", role),
            "call_id": call_id, "attempt": attempt, "retry": attempt > 1,
            "outcome": outcome, "usage_received": isinstance(usage, dict),
            "vendor_cost": None, "cost_status": "unknown",
            **{k: context[k] for k in ("assistant_attempt_id", "label", "chat_id", "turn") if k in context},
            **meta,
        },
    )


async def observed_post(client, url, *, config, kind, call_id=None, attempt=1, **kwargs):
    """Native HTTP provider calls use the same nullable receipt accounting."""
    response = None
    error = None
    try:
        response = await client.post(url, **kwargs)
        return response
    except BaseException as exc:
        error = exc
        raise
    finally:
        body = None
        if response is not None:
            try:
                body = response.json()
            except ValueError:
                pass
        outcome = (
            "stopped" if isinstance(error, asyncio.CancelledError) else
            "failed" if error is not None or (response is not None and response.status_code >= 400)
            else "completed"
        )
        with usage_scope(role=kind):
            record_provider_attempt(
                kind=kind, provider=str(getattr(config, "provider", "") or ""),
                model=str(getattr(config, "model", "") or ""), role=kind,
                call_id=call_id or uuid4().hex, attempt=attempt, outcome=outcome,
                usage=body.get("usage") if isinstance(body, dict) else None,
                http_status=response.status_code if response is not None else None,
                error_type=type(error).__name__ if error is not None else None,
            )


async def stop_provider_stream(stream):
    """The installed SDK exposes its producer task but no stream aclose method."""
    task = getattr(stream, "_producer_task", None)
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class _ReceiptStream(httpx.AsyncByteStream):
    """Pass bytes through; retain only usage, never prompt/answer text."""

    def __init__(self, stream, row):
        self.stream, self.row = stream, row
        self.pending = b""
        self.done = False

    async def __aiter__(self):
        async for chunk in self.stream:
            self._observe(chunk)
            yield chunk

    def _observe(self, chunk):
        if self.done:
            return
        self.pending += chunk
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                self.done = True
                self.pending = b""
                break
            try:
                body = json.loads(data)
            except (ValueError, UnicodeError):
                continue
            if isinstance(body, dict) and isinstance(body.get("usage"), dict):
                self.row["usage"] = body["usage"]
        # ponytail: unfinished lines >64 KiB leave usage unknown; use incremental JSON parsing for larger vendor receipts.
        if len(self.pending) > 65536:
            self.pending = b""

    async def aclose(self):
        await self.stream.aclose()


def observe_provider(provider, *, kind="chat"):
    """Use existing SDK listeners; observe this client's sends, including SDK retries."""
    if getattr(provider, "_zhilan_usage_observed", False):
        return provider
    provider._zhilan_usage_observed = True

    def request_started(body, spec):
        state = {
            "provider": provider, "model": spec.id, "kind": kind,
            "context": dict(_scope.get()), "id": uuid4().hex, "rows": [],
        }
        _call.set(state)
        client = provider._client._client
        if getattr(client, "_zhilan_usage_observed", False):
            return
        original_send = client.send

        async def send(request, *args, **kwargs):
            active = _call.get()
            if active is None or active["provider"] is not provider:
                return await original_send(request, *args, **kwargs)
            row = {"usage": None, "status": None, "error": None}
            active["rows"].append(row)
            try:
                response = await original_send(request, *args, **kwargs)
                row["status"] = response.status_code
                if "text/event-stream" in response.headers.get("content-type", ""):
                    receipt_stream = _ReceiptStream(response.stream, row)
                    # Preloaded HTTP responses bypass their stream on aiter_bytes.
                    # Parse only received wire bytes, never SDK-normalized Usage().
                    if response.is_closed:
                        try:
                            receipt_stream._observe(response.content)
                        except httpx.ResponseNotRead:
                            pass
                    response.stream = receipt_stream
                elif response.is_closed:
                    try:
                        result = response.json()
                        row["usage"] = result.get("usage") if isinstance(result, dict) else None
                    except ValueError:
                        pass
                return response
            except BaseException as exc:
                row["error"] = type(exc).__name__
                raise

        client.send = send
        client._zhilan_usage_observed = True

    def response_finished(body, spec, exc):
        state = _call.get()
        if state is None or state["provider"] is not provider:
            return
        try:
            rows = state["rows"]
            with usage_scope(**state["context"]):
                for index, row in enumerate(rows, 1):
                    if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) and index == len(rows):
                        outcome = "stopped"
                    elif row["error"] or (row["status"] or 0) >= 400:
                        outcome = "failed"
                    elif index == len(rows) and exc is not None:
                        outcome = "failed"
                    elif index == len(rows) and body is None:
                        outcome = "incomplete"
                    else:
                        outcome = "completed"
                    record_provider_attempt(
                        kind=kind,
                        provider=str(spec.provider_id or ""),
                        model=state["model"], role="main_answer",
                        call_id=state["id"], attempt=index, outcome=outcome,
                        usage=row["usage"],
                        db=state["context"].get("db"),
                        http_status=row["status"], error_type=row["error"],
                    )
        except Exception:
            log.exception("记录 provider 用量失败")
        finally:
            _call.set(None)

    provider.subscribe_request(request_started)
    provider.subscribe_response(response_finished)
    return provider
