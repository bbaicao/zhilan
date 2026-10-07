"""OPT4.4/4.5: offline stdio ownership, request hooks, error redaction.

Run only in the isolated OPT4 SQLite mirror via the repository lock runner.
No model or network traffic. Child server reuses the existing stdlib fixture.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.agent import audit, mcp_transport
from app.agent.web import client as web_client
from app.agent.web import ssrf
from app.api import agent_routes
from app.core import app_logging
from app.core.config import settings
from app.db.session import get_db
from app.models import AgentRun, Chat


FAKE_SERVER = Path(__file__).parents[1] / "api" / "fake_mcp_server.py"
SENTINEL = "OPT4_FAKE_SECRET_98D3E6B7"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["normal", "discovery_fail", "call_timeout", "cancel", "server_exit"]
)
async def test_stdio_owned_processes_reaped(mode, tmp_path, monkeypatch, record_property):
    """Every created server exits; an unrelated control process stays alive."""
    import mcp.client.stdio as sdk

    phase = tmp_path / "phase.txt"
    server = tmp_path / "controlled_mcp.py"
    server.write_text(
        """
import json, runpy, sys, time
from pathlib import Path
fixture = runpy.run_path(sys.argv[1])
mode, phase = sys.argv[2], Path(sys.argv[3])
for raw in sys.stdin.buffer:
    request = json.loads(raw.decode("utf-8"))
    method = request.get("method")
    with phase.open("a", encoding="utf-8") as stream:
        stream.write(str(method) + "\\n")
    if mode == "discovery_fail" and method == "tools/list":
        fixture["_reply"](request["id"], error={"code": -32603, "message": "OPT4 discovery failure"})
    elif mode == "server_exit" and method == "tools/call":
        raise SystemExit(0)
    elif not fixture["_handle_request"](request, "hang" if mode in ("call_timeout", "cancel") else "normal"):
        while True:
            time.sleep(0.05)
""",
        encoding="utf-8",
    )
    owned = []
    real_spawn = sdk._create_platform_compatible_process

    async def capture_spawn(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        owned.append(process)
        return process

    monkeypatch.setattr(sdk, "_create_platform_compatible_process", capture_spawn)
    monkeypatch.setattr(sdk, "PROCESS_TERMINATION_TIMEOUT", 0.2)
    monkeypatch.setattr(sdk, "_KILL_REAP_TIMEOUT", 1.0)
    monkeypatch.setattr(mcp_transport, "MCP_DISCOVERY_TIMEOUT", 3.0)
    monkeypatch.setattr(mcp_transport, "MCP_CALL_TIMEOUT", 0.5 if mode == "call_timeout" else 4.0)
    pack = {
        "slug": "opt4",
        "name": "OPT4 local fixture",
        "transport": "stdio",
        "enabled": True,
        "command": [sys.executable, str(server), str(FAKE_SERVER), mode, str(phase)],
        "tools": [{"name": "fake_mcp_ping", "enabled": True, "dangerous": False}],
    }
    monkeypatch.setattr(settings, "MCP_SERVERS", json.dumps([pack]))
    monkeypatch.setattr(settings, "MCP_ALLOW_SERVERS", "opt4")
    monkeypatch.setattr(settings, "MCP_ALLOW_EGRESS", "")
    control = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    task = None
    try:
        outcome = await mcp_transport._adiscover()
        assert owned, "The test must actually create a stdio process"
        assert owned[0].returncode is not None, "Discovery process must exit before discovery returns"
        if mode == "discovery_fail":
            assert not outcome.specs
            assert any(item["reason"] == "discover_failed" for item in outcome.skipped)
        else:
            assert outcome.specs, "A call case must pass discovery; do not skip it"
            spec = outcome.specs[0]
            task = asyncio.create_task(spec.handler(spec.parameters(message="OPT4")))
            if mode == "cancel":
                async with asyncio.timeout(3.0):
                    while not phase.exists() or "tools/call" not in phase.read_text(encoding="utf-8"):
                        await asyncio.sleep(0.01)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                result = await task
                if mode == "normal":
                    assert result.ok and "FAKE_MCP_OK" in result.text
                else:
                    assert result.ok is False
                    if mode == "call_timeout":
                        assert "超时" in result.text
            assert len(owned) == 2, "Discovery and call must each create their own server"
        async with asyncio.timeout(2.0):
            while any(process.returncode is None for process in owned):
                await asyncio.sleep(0.01)
        record_property(
            "owned_processes",
            json.dumps([{"pid": process.pid, "returncode": process.returncode} for process in owned]),
        )
        assert all(process.returncode is not None for process in owned)
        assert control.poll() is None, "An unrelated control process must not be terminated"
    finally:
        # Cleanup only processes this test actually created. Checks above run first.
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for process in owned:
            if process.returncode is None:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=2.0)
        if control.stdin is not None:
            control.stdin.close()
        try:
            control.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            control.terminate()
            control.wait(timeout=2.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_request", [False, True], ids=["response-only", "request-response"])
async def test_custom_hooks_keep_guard_and_callbacks(with_request, monkeypatch):
    seen = []
    guard_calls = []
    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", False)

    def resolve(host):
        guard_calls.append(host)
        return [host]

    monkeypatch.setattr(ssrf, "resolve_host", resolve)

    async def request_hook(request):
        seen.append(("callback", request.url.host))

    async def response_hook(response):
        seen.append(("response", response.status_code))

    def transport(request):
        seen.append(("sent", request.url.host))
        return httpx.Response(200, text="OPT4 OK")

    hooks = {"response": [response_hook]}
    if with_request:
        hooks["request"] = [request_hook]
    original = {key: list(value) for key, value in hooks.items()}
    async with web_client._make_client(
        event_hooks=hooks, transport=httpx.MockTransport(transport), trust_env=False
    ) as client:
        assert (await client.get("http://93.184.216.34/")).text == "OPT4 OK"
        assert ("response", 200) in seen
        if with_request:
            assert ("callback", "93.184.216.34") in seen
        seen.clear()
        with pytest.raises(web_client.WebToolError):
            await client.get("http://127.0.0.1/")
        assert not seen, "The guard must run before both custom callbacks and transport"
    assert hooks == original, "Client construction must not mutate caller hooks"
    guard_calls.clear()
    duplicate_hooks = {"request": [web_client._guard_hook, request_hook]}
    duplicate_original = list(duplicate_hooks["request"])
    async with web_client._make_client(
        event_hooks=duplicate_hooks, transport=httpx.MockTransport(transport), trust_env=False
    ) as client:
        assert (await client.get("http://93.184.216.34/")).status_code == 200
    assert guard_calls == ["93.184.216.34"], "An explicitly supplied guard must execute only once"
    assert duplicate_hooks["request"] == duplicate_original


@pytest.mark.asyncio
async def test_custom_hook_cannot_bypass_redirect_guard(monkeypatch):
    seen = []
    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", False)
    monkeypatch.setattr(ssrf, "resolve_host", lambda host: [host])

    async def custom(request):
        seen.append(("callback", request.url.host))

    def transport(request):
        seen.append(("sent", request.url.host))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/blocked"})

    async with web_client._make_client(
        event_hooks={"request": [custom]},
        transport=httpx.MockTransport(transport),
        trust_env=False,
        follow_redirects=True,
    ) as client:
        with pytest.raises(web_client.WebToolError):
            await client.get("http://93.184.216.34/start")
    assert seen == [("callback", "93.184.216.34"), ("sent", "93.184.216.34")]


@pytest.mark.parametrize(
    "template",
    [
        'api_key={}',
        '{{"api_key":"{}"}}',
        '{{"Authorization":"Bearer {}"}}',
        '{{"Authorization":"{}"}}',
        "https://example.invalid/?token={}",
    ],
    ids=["assignment", "json-key", "json-authorization", "json-authorization-plain", "url-query"],
)
def test_redact_recognizes_sensitive_error_forms(template):
    text = "ordinary diagnostic; " + template.format(SENTINEL)
    sanitized = app_logging.redact(text)
    assert SENTINEL not in sanitized, "The synthetic secret must be masked"
    assert app_logging.MASK in sanitized
    assert "ordinary diagnostic" in sanitized
    assert app_logging.redact("ordinary error code=503") == "ordinary error code=503"


@pytest.mark.asyncio
async def test_error_redacted_before_clip_in_storage_and_api(db, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_ENABLE_AUDIT", True)
    chat = Chat(title="OPT4 redaction")
    db.add(chat)
    await db.flush()
    run_id = await audit.start_run(db, chat_id=chat.id, mode="react")
    assert run_id is not None
    # Clip-first would leave the start of the value in the persisted 200 chars.
    error = "diagnostic " + "x" * 166 + " api_key=" + SENTINEL + " end"
    assert await audit.write_tool_step(
        db,
        run_id=run_id,
        step_no=1,
        tool_name="opt4",
        tool_call_id="opt4-call",
        content="tool failure",
        ok=False,
        ms=1,
        error=error,
    )
    await audit.finish_run(db, run_id, status="failed", error=error)
    run = await db.get(AgentRun, run_id)
    # Read the same real query used by the endpoint, then drive the HTTP route.
    items = await audit.read_audit(db, run_id=run_id)
    assert items and run is not None
    for saved in (run.meta["error"], items[0]["meta"]["error"]):
        assert SENTINEL[:8] not in saved, "No partial synthetic credential may survive clipping"
        assert app_logging.MASK in saved, "Redact must happen before the shorter audit clip"
        assert saved.startswith("diagnostic")
        assert len(saved) <= audit.ERROR_CLIP + 1
    app = FastAPI()
    app.include_router(agent_routes.router, prefix="/api")

    async def isolated_db():
        yield db

    app.dependency_overrides[get_db] = isolated_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/agent/audit", params={"run_id": run_id})
    assert response.status_code == 200
    assert SENTINEL[:8] not in response.text, "API projection must not expose a partial synthetic credential"
    assert app_logging.MASK in response.text


def test_audit_warning_redacts_before_logger(caplog):
    previous = audit.audit_write_failures()
    try:
        with caplog.at_level(logging.WARNING, logger=audit.logger.name):
            audit._warn("OPT4 injected write failure", RuntimeError("api_key=" + SENTINEL))
        text = "\n".join(record.getMessage() for record in caplog.records if record.name == audit.logger.name)
        assert SENTINEL not in text, "A root/capture logger must receive sanitized error text"
        assert app_logging.MASK in text
        assert "RuntimeError" in text and "OPT4 injected write failure" in text
    finally:
        audit.restore_audit_write_failures(previous)
