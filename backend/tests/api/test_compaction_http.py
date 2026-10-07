"""长会话压缩的**端到端**用例（条目 4.3.3；对抗性核验 G2 的缺口补齐）。

## 为什么必须有这一条

本条的 12 条压缩用例全在 `build_model_history` / `compact_history` 这一层。
**把 `chat_routes.build_turn_plan` 里的 `chat=chat` 或 `compaction_model=model` 两处传参删掉，
那 45 条用例会全绿**，而压缩对 prompt 完全不生效——这正是「接线级假绿」的典型形状
（4.2 的教训：断言要细到「事情真的发生了」）。

所以这一条**走 HTTP**（`POST /api/chat/stream`），钉三件事：

1. 超阈值的历史真的触发了压缩 ⇒ `chats.context_summary` 非空、`compressed_up_to` 有值；
2. **摘要模型真的被调了**（计数 == 1）——而不是「碰巧没压也算过」；
3. 压缩后的 history 真的进了回答链路（用记录 model 收到消息的替身验证）。

⚠️ 这里**不**依赖 `tests/api` 的 autouse 钉子之外的任何全局状态；阈值/KEEP 用 monkeypatch
在用例内收紧，别污染别的用例。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select, text

from app.api import chat_routes
from app.core.config import settings
from app.models import Chat, Message
from app.session import repository as repo
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401

pytestmark = pytest.mark.asyncio

#: 压缩阈值收紧到「几条消息就超」——默认 8000 字要造很长的会话，没必要
TIGHT_THRESHOLD = 120
TIGHT_KEEP = 2


class _RecordingChatModel:
    """回答链路的替身：**记录它收到的消息列表**，然后回一句固定正文。

    为什么需要它：光看「两列被写了」还不够——要证明**压缩后的历史真的进了 prompt**
    （而不是压缩写了库、prompt 仍用全量）。记录 `messages` 是唯一能证明这一点的地方。
    """

    def __init__(self, reply: str = "好的。") -> None:
        self.reply = reply
        self.calls: list[list] = []

    async def stream(self, messages, system_prompt=None):  # noqa: ANN001
        self.calls.append(list(messages))
        return self._iterate()

    async def _iterate(self):
        from types import SimpleNamespace

        yield SimpleNamespace(type="text_delta", delta=self.reply)


def _block_text(item: object) -> str:  # noqa: ANN001
    """从类型化消息里取正文（``UserMessage`` / ``AssistantMessage`` 的 content 是块列表）。"""
    parts: list[str] = []
    for block in getattr(item, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
    return "".join(parts)


async def _seed_long_chat(db, *, rounds: int = 5) -> Chat:  # noqa: ANN001
    """造一段**够长**的历史（user/assistant 交替，每条约 40 字）。"""
    chat = await repo.create_chat(db, title="长会话压缩")
    for index in range(rounds):
        await repo.add_message(
            db, chat_id=chat.id, role="user", content=f"第{index}问：" + "关于压缩的事实。" * 6
        )
        await repo.add_message(
            db,
            chat_id=chat.id,
            role="assistant",
            content=f"第{index}答：" + "只落 chats 两列。" * 6,
        )
    await db.commit()
    return chat


async def _install(monkeypatch, summarizer, answer_model) -> None:  # noqa: ANN001
    """装：摘要模型（`generate`）+ 回答模型（`stream`）+ 零出网的路由分类器。

    ⚠️ 两者**必须是同一个对象**吗？不需要——`build_turn_plan(compaction_model=...)` 与
    回答链路的 `resolve_chat_model()` 在本条里都接到同一个 `model`（端点里就是这么传的），
    所以这里用一个**双方法替身**：既实现 `generate`（摘要）又实现 `stream`（回答）。
    """

    class _Both:
        def __init__(self) -> None:
            self.summary_calls: list[dict] = []
            self.answer_calls: list[list] = []

        # 摘要调用（非流式小调用）
        async def generate(self, messages, **kwargs):  # noqa: ANN001, ANN003
            from cubeloop.providers.base import AssistantMessage, TextContent

            self.summary_calls.append({"messages": list(messages), "kwargs": kwargs})
            return AssistantMessage(content=[TextContent(text=summarizer)])

        # 回答链路（流式）
        async def stream(self, messages, system_prompt=None):  # noqa: ANN001
            self.answer_calls.append(list(messages))
            return answer_model._iterate()

        @property
        def spec(self):  # 真实 BoundModel 的模型名在 `.spec.id` 上
            from types import SimpleNamespace

            return SimpleNamespace(id="both-fake")

    both = _Both()
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: both)
    # 聚合收集可能缺少目录 autouse；摘要模型由本例显式装配。
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: both)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("external")
    )
    monkeypatch.setattr(settings, "CHAT_COMPRESS_THRESHOLD", TIGHT_THRESHOLD)
    monkeypatch.setattr(settings, "CHAT_COMPRESS_KEEP", TIGHT_KEEP)
    # 澄清判定会让「问题」这种短句多调一次模型 ⇒ 关掉它，保证 `summary_calls` 只数压缩
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", False)
    auxiliary = chat_routes.resolve_aux_model()
    assert auxiliary is both, (
        "HTTP 摘要必须绑定本例辅助模型",
        type(auxiliary).__name__,
        chat_routes.resolve_aux_model.__module__,
        chat_routes.resolve_aux_model.__qualname__,
    )
    return both


async def test_compaction_really_happens_over_http(client, db, monkeypatch):  # noqa: ANN001
    """**G2 回归钉**：超阈值会话经 `POST /api/chat/stream` ⇒ 摘要真调、两列真写、prompt 真用摘要。"""
    chat = await _seed_long_chat(db, rounds=5)
    both = await _install(monkeypatch, "【摘要】早期对话的要点。", _RecordingChatModel("库外直答。"))

    resp = await client.post(
        "/api/chat/stream", json={"message": "继续讲讲压缩", "chat_id": chat.id}
    )
    assert resp.status_code == 200, resp.text
    kinds = [event["type"] for event in parse_sse(resp.text)]
    assert "done" in kinds, kinds

    # ① 摘要模型真的被调了（==1，不是 0；也不是每次都压）
    assert len(both.summary_calls) == 1, [c["kwargs"] for c in both.summary_calls]
    # ② 思考余量：真实发送预算 == 配置 + 1024
    budget = both.summary_calls[0]["kwargs"].get("max_output_tokens")
    assert budget == settings.CHAT_COMPRESS_SUMMARY_MAX_TOKENS + 1024, budget
    # ③ 两列真的写了。
    # ⚠️ **必须用原生 SQL 读**（实测踩过）：HTTP 路径那一侧是**另一个 session** 提交的，
    # 而本用例的 `db` 会话在 `_seed_long_chat` 时已经读过一次 `chats`（REPEATABLE READ 快照）；
    # 走 ORM 的 `select(Chat)` 会命中**身份映射里的旧对象**（`expire_on_commit=False` + 旧快照）
    # ⇒ 明明写进去了却读成 `None`，把「接线生效」误判成「没压」。
    # 原生 SQL 绕开身份映射与 ORM 缓存，读的是库里的真相。
    raw = (
        await db.execute(
            text("SELECT context_summary, compressed_up_to FROM chats WHERE id = :i"),
            {"i": chat.id},
        )
    ).all()
    assert len(raw) == 1, raw
    summary_value, upto_value = raw[0]
    assert summary_value == "【摘要】早期对话的要点。", raw
    assert upto_value is not None, raw
    # ④ `messages` 行数不变（压缩**不是**删历史）
    rows = list(
        (await db.execute(select(Message).where(Message.chat_id == chat.id))).scalars().all()
    )
    assert len([r for r in rows if r.role in ("user", "assistant")]) == 5 * 2 + 2  # 历史 10 + 本轮一问一答

    # ⑤ **prompt 真的用了摘要**（这条才是「接线生效」的硬证据）
    assert both.answer_calls, "回答链路必须被调用"
    sent = [_block_text(item) for item in both.answer_calls[0]]
    joined = "\n".join(sent)
    assert "【会话早期内容摘要" in joined, joined[:400]
    assert "第0问" not in joined, "被压掉的那几轮不该再进 prompt"


async def test_compaction_is_skipped_when_under_threshold_over_http(client, db, monkeypatch):  # noqa: ANN001
    """反面：**很短**的会话经 HTTP 走一轮 ⇒ 摘要模型零调用、两列仍为空（零变化）。"""
    chat = await repo.create_chat(db, title="短会话")
    await repo.add_message(db, chat_id=chat.id, role="user", content="短问题")
    await repo.add_message(db, chat_id=chat.id, role="assistant", content="短回答")
    await db.commit()

    both = await _install(monkeypatch, "不该被用到", _RecordingChatModel("库外直答。"))

    resp = await client.post(
        "/api/chat/stream", json={"message": "再问一句", "chat_id": chat.id}
    )
    assert resp.status_code == 200, resp.text
    assert [event["type"] for event in parse_sse(resp.text)][-1] == "done"
    assert both.summary_calls == [], "未超阈值不得调摘要模型"

    await db.commit()
    fresh = (await db.execute(select(Chat).where(Chat.id == chat.id))).scalars().first()
    assert fresh is not None
    assert fresh.context_summary is None
    assert fresh.compressed_up_to is None
