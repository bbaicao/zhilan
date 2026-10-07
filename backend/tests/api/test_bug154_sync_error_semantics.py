"""回归修复 载体：同步 ``/api/chat`` 的上游失败必须走 02 §4 的契约状态码。

缺陷（修复前）：``answer_direct`` 把上游失败**渲染成回答正文**回 200 ——
``except Exception`` 与 ``stop_reason == "error"`` 两个出口都产出
「回答生成失败，请稍后重试。（…）」，于是 ``ProviderAuthError``/``ProviderTimeout``
这两条 02 §4（``接口说明/02-API契约冻结清单.md`` §4 异常语义表）已声明的语义
**全仓零生产者**（``backend/tests/contract/test_error_semantics.py`` 逐字登记了这点），
用户拿到 200 + 一条像是「回答」的文本，分不清该去检查 API Key 还是该重试。

修复：``answer_direct`` 把上游失败读数（异常类名 / ``error_type`` / ``status_code`` /
``error_code`` / ``error_message``）翻译成 ``ProviderAuthError``（→401）或
``ProviderTimeout``（→504）后**上抛**，由 ``backend/main.py`` 的
``@app.exception_handler(RagError)`` 映射；翻译不出的失败**逐字保持**原来的正文渲染
（其它异常的可读提示不许变）。

判据口径：只断言 HTTP 状态码、响应 detail（中文提示常量）与落库形状，
**不硬编码产品源码行号**（chat_routes.py 行号在 S1 期间已多次漂移）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api import chat_routes
from app.core.config import settings
from app.core.errors import AUTH_HINT, TIMEOUT_HINT, ProviderAuthError, ProviderTimeout, RagError
from app.models import Chat, Message, SessionEvent
from app.session import attempt, events
from tests.api.test_chat_contract import _StubClassifier, client  # noqa: F401

FAILURE_PREFIX = "回答生成失败，请稍后重试。"


class _FailingModel:
    """假 bound model：按预制形状失败（真 provider 的两条形态都覆盖）。

    形态 ①（抛异常）：适配器/替身直接抛 typed 错误 —— 走 ``answer_direct`` 的
    ``except Exception`` 出口；形态 ②（回错误消息）：真 ``cubeloop`` provider 把
    上游失败**规范化**成 ``content=[] + stop_reason="error" + error_*`` 的
    ``AssistantMessage`` 返回（不抛）—— 走 ``stop_reason == "error"`` 出口。
    """

    def __init__(self, *, exc: BaseException | None = None, final=None) -> None:
        self.exc = exc
        self.final = final
        self.calls = 0

    async def generate(self, messages, system_prompt=None, max_output_tokens=None):  # noqa: ANN001
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.final

    async def stream(self, messages, system_prompt=None):  # noqa: ANN001
        raise AssertionError("本载体只覆盖同步端点，不应走到流式生成")


def _install(monkeypatch, model):
    """装好模型与路由分类器（不连真模型；澄清关掉，避免它抢占这一轮）。"""
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("external")
    )
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", False)
    return model


def _error_result(*, error_type, error_message, status_code=None, error_code=None):
    """真 provider 的规范化失败形状（cubeloop ``AssistantMessage``：content 为空）。"""
    return SimpleNamespace(
        content=[],
        stop_reason="error",
        error_type=error_type,
        error_code=error_code,
        status_code=status_code,
        error_message=error_message,
    )


async def _rows(db, chat_id: int) -> list[Message]:
    # 先结束本会话可能持有的读快照（MySQL REPEATABLE READ：不 rollback 会读到旧 snapshot）
    await db.rollback()
    return list(
        (
            await db.execute(
                select(Message).where(Message.chat_id == chat_id).order_by(Message.id)
            )
        )
        .scalars()
        .all()
    )


async def _post(client):
    return await client.post("/api/chat", json={"message": "向量库用什么存储？"})


# --- ① 两条契约语义变得可达（修复前：全是 200 + 正文）------------------------


@pytest.mark.asyncio
async def test_auth_failure_raises_401_not_a_200_answer(client, db, monkeypatch):
    """上游认证失败（typed 异常形态）：401 + 中文 detail，且失败正文**不落库**。"""
    from cubeloop.errors import ProviderAuthFailed

    model = _install(
        monkeypatch,
        _FailingModel(
            exc=ProviderAuthFailed(
                "Error code: 401 - Incorrect API key provided: probe-key.",
                status_code=401,
                error_code="invalid_api_key",
            )
        ),
    )

    resp = await _post(client)

    assert resp.status_code == 401, resp.text
    assert resp.json() == {"detail": AUTH_HINT}
    assert model.calls == 1, "认证失败就该一轮定音，不许重试"
    # 修复前：这条失败被渲染成 assistant 正文落库 ⇒ 下一轮回灌给模型
    await db.rollback()
    leaked = list(
        (
            await db.execute(
                select(Message).where(Message.content.startswith(FAILURE_PREFIX))
            )
        )
        .scalars()
        .all()
    )
    assert leaked == [], "失败正文不许落库成回答"


@pytest.mark.asyncio
async def test_auth_failure_fields_raise_401(client, monkeypatch):
    """上游认证失败（规范化字段形态，status_code/error_code 齐全）：401。"""
    _install(
        monkeypatch,
        _FailingModel(
            final=_error_result(
                error_type="ProviderAuthFailed",
                error_message="ProviderAuthFailed: Error code: 401 - Incorrect API key provided.",
                status_code=401,
                error_code="invalid_api_key",
            )
        ),
    )

    resp = await _post(client)

    assert resp.status_code == 401, resp.text
    assert resp.json() == {"detail": AUTH_HINT}


@pytest.mark.asyncio
async def test_rate_limited_fields_raise_401(client, monkeypatch):
    """限流没有独立出口（``app/core/errors.py`` 里 ProviderAuthError 的文档串）：

    429/RateLimited 一并归到 ProviderAuthError ⇒ 401，detail 用同一条中文提示。
    """
    _install(
        monkeypatch,
        _FailingModel(
            final=_error_result(
                error_type="RateLimited",
                error_message="RateLimited: Error code: 429 - Rate limit reached.",
                status_code=429,
                error_code="rate_limit_exceeded",
            )
        ),
    )

    resp = await _post(client)

    assert resp.status_code == 401, resp.text
    assert resp.json() == {"detail": AUTH_HINT}


@pytest.mark.asyncio
async def test_timeout_by_text_raises_504(client, monkeypatch):
    """上游超时（cubeloop 把 httpx 超时归到 ``ProviderUnavailable`` + 文本）：504。

    cubeloop 没有独立超时类型（``cubeloop/errors.py`` 的分类器把
    ``TimeoutError``/``APITimeoutError`` 归到 ``ProviderUnavailable``）⇒ 超时只能靠
    ``error_message`` 文本识别，这正是本腿钉住的分支。
    """
    _install(
        monkeypatch,
        _FailingModel(
            final=_error_result(
                error_type="ProviderUnavailable",
                error_message=(
                    "ProviderUnavailable: Request timed out. "
                    "<- APITimeoutError: Request timed out."
                ),
            )
        ),
    )

    resp = await _post(client)

    assert resp.status_code == 504, resp.text
    assert resp.json() == {"detail": TIMEOUT_HINT}


@pytest.mark.asyncio
async def test_timeout_exception_raises_504(client, monkeypatch):
    """上游超时（抛 builtin ``TimeoutError`` 的适配器形态）：504。"""
    _install(monkeypatch, _FailingModel(exc=TimeoutError("matrix model timeout")))

    resp = await _post(client)

    assert resp.status_code == 504, resp.text
    assert resp.json() == {"detail": TIMEOUT_HINT}


@pytest.mark.asyncio
async def test_timeout_status_code_raises_504(client, monkeypatch):
    """错误读数只带 ``status_code=504``（无超时字样）：仍判超时 ⇒ 504。"""
    _install(
        monkeypatch,
        _FailingModel(
            final=_error_result(
                error_type="ProviderBadRequest",
                error_message="Upstream gateway error.",
                status_code=504,
            )
        ),
    )

    resp = await _post(client)

    assert resp.status_code == 504, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_chat", [False, True])
@pytest.mark.parametrize(
    ("failure_kind", "status", "hint"),
    [("auth", 401, AUTH_HINT), ("timeout", 504, TIMEOUT_HINT)],
)
async def test_http_failure_keeps_submitted_question(
    client, db, monkeypatch, existing_chat, failure_kind, status, hint
):
    """401/504 仍留提问与失败终态，新会话和已有会话都不能丢这一轮。"""
    from cubeloop.errors import ProviderAuthFailed

    exc = (
        ProviderAuthFailed("probe auth failure", status_code=401)
        if failure_kind == "auth"
        else TimeoutError("probe timeout")
    )
    _install(monkeypatch, _FailingModel(exc=exc))
    payload = {"message": "这条失败的提问必须保留"}
    if existing_chat:
        chat = Chat(title="已有会话")
        db.add(chat)
        await db.commit()
        await db.refresh(chat)
        payload["chat_id"] = chat.id

    resp = await client.post("/api/chat", json=payload)

    assert resp.status_code == status, resp.text
    assert resp.json() == {"detail": hint}
    await db.rollback()
    rows = list((await db.execute(select(Message).order_by(Message.id))).scalars())
    assert [(row.role, row.content) for row in rows] == [("user", payload["message"])]
    if existing_chat:
        assert rows[0].chat_id == payload["chat_id"]
    events = list(
        (await db.execute(select(SessionEvent).order_by(SessionEvent.id))).scalars()
    )
    assert {"turn/start", "user/message"}.issubset(event.type for event in events)
    attempts = [event for event in events if event.type == "assistant/attempt"]
    assert len(attempts) == 1 and attempts[0].data["outcome"] == "aborted"
    ends = [event for event in events if event.type == "turn/end"]
    assert len(ends) == 1 and ends[0].data["kind"] == "error"
    assert ends[0].data["detail"] == hint


# --- ② 其它失败的既有可读提示**逐字不变**（⛔ 回归护栏）----------------------


@pytest.mark.asyncio
async def test_generic_upstream_failure_keeps_text_render(client, db, monkeypatch):
    """连接类失败保留 HTTP 200 和固定文案，OPT2 失败只留用户并记 aborted/error。"""
    detail = "ProviderUnavailable: Connection error. <- APIConnectionError: Connection error."
    _install(
        monkeypatch,
        _FailingModel(
            final=_error_result(error_type="ProviderUnavailable", error_message=detail)
        ),
    )

    resp = await _post(client)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["message"] == f"{FAILURE_PREFIX}（{detail}）"
    rows = await _rows(db, body["chat_id"])
    assert [row.role for row in rows] == ["user"], "结构化失败不许当成功回答回灌历史"
    aborted = await events.list_events_by_type(db, body["chat_id"], attempt.ATTEMPT_EVENT)
    assert aborted[-1].data["outcome"] == attempt.ATTEMPT_ABORTED
    ended = await events.list_events_by_type(db, body["chat_id"], "turn/end")
    assert ended[-1].data["kind"] == "error"


@pytest.mark.asyncio
async def test_plain_exception_keeps_text_render(client, monkeypatch):
    """普通异常（非上游语义）仍走既有「回答生成失败…（类名）」正文，200。"""
    _install(monkeypatch, _FailingModel(exc=RuntimeError("boom")))

    resp = await _post(client)

    assert resp.status_code == 200, resp.text
    assert resp.json()["message"] == f"{FAILURE_PREFIX}（RuntimeError）"


# --- ②b ``RagError`` 透传分支（复核补腿：关掉判据盲区）------------------------
# s1-audit 复核指出：``answer_direct`` 的 ``except RagError: raise`` 透传分支原先**零覆盖**
# ⇒ 若删掉它，模型侧直接抛的 RagError 会退回「200 + 正文渲染」。下面两腿把该分支钉住，
# 断言 ``detail`` **逐字等于** ``str(exc)``（即原样透传，中间不许再加工）。


@pytest.mark.asyncio
async def test_rag_error_timeout_from_model_passes_through(client, monkeypatch):
    """模型侧直接抛本仓 ``ProviderTimeout``（已是契约语义）：透传 ⇒ 504 + 原 detail。"""
    exc = ProviderTimeout(TIMEOUT_HINT)
    _install(monkeypatch, _FailingModel(exc=exc))

    resp = await _post(client)

    assert resp.status_code == 504, resp.text
    assert resp.json() == {"detail": str(exc)}


@pytest.mark.asyncio
async def test_rag_error_auth_from_model_passes_through(client, monkeypatch):
    """同上认证侧：``ProviderAuthError`` 透传 ⇒ 401 + 原 detail。"""
    exc = ProviderAuthError(AUTH_HINT)
    _install(monkeypatch, _FailingModel(exc=exc))

    resp = await _post(client)

    assert resp.status_code == 401, resp.text
    assert resp.json() == {"detail": str(exc)}

# --- ②c ``RagError`` 透传分支的**判别力**补钉（Lead m00592 裁决）---------------
# s1-audit 窗口③ 预检发现：上面⑨⑩两腿对该分支**行为冗余** —— 删掉
# ``backend/app/generation/answer.py:728-732`` 的 ``except RagError: raise`` 整块后，
# 两条仍绿（⑨ 的 ``ProviderTimeout`` 经 ``_TIMEOUT_ERROR_TYPES`` 命中翻译表；
# ⑩ 传入的 ``AUTH_HINT`` 文本含「限流」∈ ``_AUTH_MARKERS``）⇒ 该分支此前零判别力覆盖。
# 本腿改用**翻译表认不出**的纯 ``RagError`` 钉住它：注入串刻意不含任何 marker
# （timeout / 超时 / authentication / unauthorized / limit / 限流 / invalid_api_key /
# permission denied）⇒ 删掉该分支后，本异常会落进 ``except Exception`` 的翻译表、
# 翻译不出 ⇒ 退化成 200 + 「回答生成失败…（类名）」正文渲染（本腿随即变红）。


@pytest.mark.asyncio
async def test_rag_error_from_model_passes_through_unchanged(client, monkeypatch):
    """模型侧直接抛本仓纯 ``RagError``（翻译表认不出）：必须原样透传 ⇒ 400 + 原 detail。"""
    exc = RagError("probe-detail-must-not-be-rewritten")
    _install(monkeypatch, _FailingModel(exc=exc))

    resp = await _post(client)

    assert resp.status_code == 400, resp.text
    assert resp.json() == {"detail": "probe-detail-must-not-be-rewritten"}
