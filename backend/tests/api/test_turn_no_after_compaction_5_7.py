r"""**压缩后轮号回跳**的回归钉（阶段 5 · 条目 `5.7` 的 33 轮真机读数暴露；红→绿自证）。

## 缺陷本体

`app/api/chat_routes.py` 初版把轮号写成「数**发给模型的 history** 里的 user 条数 + 1」。
长会话压缩一旦发生，`compaction.summary_pair` 会把历史开头换成**摘要 user + assistant 一对**、
早期 user 行被**替换**掉 ⇒ 那个数**回跳**：33 轮只落出 13 个不同 `turn`（`5.7` 长会话探针实测）。

影响面不是「日志难看」：`(chat_id, turn)` 是 `4.4` 的 `events_incomplete` **归属桶**
（`app/session/events.py` 的 `_TURN_FAILURES`）、也是 `agent_runs.meta.turn` 的输入。

## 为什么这条钉子必须有「旧写法确实回跳」这半条

只断言 `next_turn_no(...) == 期望` 是**假保护**：把实现换回「数 history」也照样可能碰巧相等
（比如一个不触发压缩的会话）。所以这里**同时**算出两种写法的值，并断言
**旧写法 < 新写法**——那半条就是「缺陷还在」的探测器，任何一次回退都会让它红。

## 边界

- 用例只碰**数据库**（`db` 夹具），**不出网、不调模型**：压缩用 `FakeCompactionModel` 桩
  （本仓既有写法，照 `tests/session/test_compaction.py`）。
- 阈值用 `monkeypatch.setattr(settings, ...)` **临时压小**——测的是「压缩发生之后轮号还对不对」，
  不是「默认 8000 阈值合不合理」（那归 `tests/session/test_compaction.py`）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from cubeloop.providers import faux_assistant_message
from sqlalchemy import select

from app.api import chat_routes
from app.core.config import settings
from app.models import Chat, Message
from app.session import repository
from app.session import service

#: 3 轮对话 ⇒ 6 条消息；把阈值压到「随便就超」即可稳定触发压缩。
TURNS = 3
TIGHT_THRESHOLD = 60
TIGHT_KEEP = 2


class FakeCompactionModel:
    """压缩摘要用的假模型：只回一段固定文本，**零出网**。"""
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.spec = SimpleNamespace(id="faux-turn-pin")

    @property
    def count(self) -> int:
        return len(self.calls)

    async def generate(self, messages, *, system_prompt="", max_output_tokens=None,
                       reasoning=None, **_kwargs):
        self.calls.append({"messages": list(messages), "system_prompt": system_prompt})
        # 返回形状照 `tests/session/test_compaction.py::FakeCompactionModel`（本仓既有写法），
        # 不要自己造一个 SimpleNamespace —— 压缩侧要的是 CubeLoop 的助手消息对象。
        return faux_assistant_message("（摘要）用户在验收长会话压缩后的轮号一致性。")


class _StubClassifier:
    """固定返回某个标签的假分类器（守约实现，**不连网络**；照 `test_chat_contract.py:63`）。"""

    def __init__(self, label: str | None) -> None:
        self.label = label

    async def classify(self, text: str) -> str | None:
        return self.label


async def _seed_chat(db, turns: int) -> Chat:
    chat = Chat(title="轮号回归钉")
    db.add(chat)
    await db.commit()
    await db.refresh(chat)
    for index in range(turns):
        await repository.add_message(db, chat_id=chat.id, role="user",
                                     content=f"第 {index + 1} 轮提问：这是用于撑大历史的一段正文。" * 3)
        await repository.add_message(db, chat_id=chat.id, role="assistant",
                                     content=f"第 {index + 1} 轮回答：收到，这是对应的回答正文。" * 3)
    await db.commit()
    return chat


@pytest.mark.asyncio
async def test_turn_no_survives_compaction(db, monkeypatch):
    """压缩发生之后，轮号仍等于「库内 user 条数 + 1」，**且旧写法确实更小**。"""
    monkeypatch.setattr(settings, "CHAT_COMPRESS_THRESHOLD", TIGHT_THRESHOLD, raising=False)
    monkeypatch.setattr(settings, "CHAT_COMPRESS_KEEP", TIGHT_KEEP, raising=False)
    monkeypatch.setattr(settings, "CHAT_COMPRESS_ENABLE", True, raising=False)

    chat = await _seed_chat(db, TURNS)
    model = FakeCompactionModel()

    history = await service.build_model_history(db, chat.id, chat=chat, model=model)
    await db.commit()
    await db.refresh(chat)

    # 前置：这一轮**真的压缩了**，否则下一条断言测的是未压缩路径（假绿）
    assert model.count == 1, "阈值压到 60 仍未触发摘要调用 ⇒ 本用例的前提不成立"
    assert (chat.context_summary or "").strip(), "context_summary 没被写 ⇒ 压缩没真发生"
    assert chat.compressed_up_to is not None, "compressed_up_to 没被写 ⇒ 压缩没真发生"

    # ① 旧写法（数压缩后的 history）**确实回跳** —— 这半条是「缺陷还在」的探测器
    legacy_style = len([item for item in history if item.get("role") == "user"]) + 1
    # ② 新写法（数库内 user 行）
    current = await chat_routes.next_turn_no(db, chat.id)

    assert current == TURNS + 1, f"轮号应为 {TURNS + 1}，实际 {current}"
    assert legacy_style < current, (
        "旧写法没有回跳 ⇒ 要么压缩没生效、要么 build_model_history 的返回形状变了："
        f"legacy={legacy_style} current={current}"
    )


@pytest.mark.asyncio
async def test_turn_no_equals_message_count_when_no_compaction(db, monkeypatch):
    """**正控**：没压缩时新旧两种写法必须**同值**（防止修复把无压缩路径改坏）。"""
    monkeypatch.setattr(settings, "CHAT_COMPRESS_ENABLE", False, raising=False)

    chat = await _seed_chat(db, TURNS)
    history = await service.build_model_history(db, chat.id, chat=chat, model=None)
    legacy_style = len([item for item in history if item.get("role") == "user"]) + 1
    assert await chat_routes.next_turn_no(db, chat.id) == legacy_style == TURNS + 1


@pytest.mark.asyncio
async def test_turn_no_counts_only_user_messages(db):
    """只数 ``role='user'``：助手消息、system 消息都不进轮号。"""
    chat = await _seed_chat(db, 1)
    await repository.add_message(db, chat_id=chat.id, role="system", content="系统提示")
    await repository.add_message(db, chat_id=chat.id, role="assistant", content="再来一条助手消息")
    await db.commit()
    assert await chat_routes.next_turn_no(db, chat.id) == 2

    rows = (await db.execute(select(Message).where(Message.chat_id == chat.id))).scalars().all()
    assert sum(1 for r in rows if r.role == "user") == 1


@pytest.mark.asyncio
async def test_build_turn_plan_uses_db_truth_after_compaction(db, monkeypatch):
    """**接线上钉**：真压缩过一次之后，``build_turn_plan`` 给出的 ``plan.turn`` 仍是库内真值。

    ⚠️ 这一条不可省：只测 ``next_turn_no`` 本身的话，把**调用点**改回旧写法用例照样全绿
    ——本文件第一次写出来就是这样，回退实验当场抓到的（5.2 的教训：「必须在测**真的命中**了」）。
    ``tests/api/conftest.py`` 的七个 autouse 钉子已把向量路/重排/联网/记忆/缓存/图谱全部钉关，
    这里额外给路由分类器与压缩模型各一个假件 ⇒ **零出网**。
    """
    from app.api.chat_schemas import ChatRequest

    monkeypatch.setattr(settings, "CHAT_COMPRESS_THRESHOLD", TIGHT_THRESHOLD, raising=False)
    monkeypatch.setattr(settings, "CHAT_COMPRESS_KEEP", TIGHT_KEEP, raising=False)
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", False, raising=False)

    chat = await _seed_chat(db, TURNS)
    # 先真压一次（两列被写），再让下一轮的 plan 走「压缩后」这条路径
    await service.build_model_history(db, chat.id, chat=chat, model=FakeCompactionModel())
    await db.commit()
    await db.refresh(chat)
    assert (chat.context_summary or "").strip(), "前置不成立：压缩没真发生"
    assert chat.compressed_up_to is not None, "前置不成立：compressed_up_to 没写"

    plan = await chat_routes.build_turn_plan(
        db,
        ChatRequest(chat_id=chat.id, message="再来一轮：压缩之后轮号还对不对？"),
        "再来一轮：压缩之后轮号还对不对？",
        [],
        routing_classifier=_StubClassifier("internal_db"),
        compaction_model=FakeCompactionModel(),
    )

    assert plan.turn == TURNS + 1, (
        f"``build_turn_plan`` 的轮号应为 {TURNS + 1}（库内 user 条数 + 1），实际 {plan.turn}"
        " —— 小于它说明调用点又回去数压缩后的 history 了"
    )


@pytest.mark.asyncio
async def test_turn_no_without_chat_is_one(db):
    """新建会话（``chat_id`` 还是 None）时恒为第 1 轮。"""
    assert await chat_routes.next_turn_no(db, None) == 1
