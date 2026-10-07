"""Agent 轮的端到端用例（阶段 4 · 条目 4.1）。

打的是**真链路**：``POST /api/chat/stream`` → ``build_turn_plan`` → ``stream_tokens`` →
``runner.stream_agent_turn``（真 CubeLoop Agent + 真注册表 + 真工具适配）→ 回答链路 →
落库。只把两样东西换成假的：

- **模型**：CubeLoop 的 FauxProvider（响应队列按调用顺序消费，零出网）；
- **检索实现**：``knowledge_search`` 的 ``retrieve`` 注入点（零 Qdrant / 零 embedding）。

覆盖条目 4.1 的三条明文要求，外加「``agent_mode`` 不再被静默忽略」这条收尾口径：

1. 工具契约名与 web 门控（``web_enabled=False`` 不进清单）；
2. SSE ``agent`` 帧**在任何 token 之前**、``done.agent_trace`` 只增字段；
3. ``tool/call`` ↔ ``tool/result`` 共用 ``tool_call_id``（I4）；
4. Agent 轮与普通轮**同一套引用编号**（I3）；
5. ``off`` 轮与旧行为逐字一致（``agent_trace`` 恒 ``None``）；
6. **条目 4.2 双模式**：``plan_execute`` 真跑「一次计划 → 逐步执行」，与 ReAct 共用
   材料快照与事件落库（I3/I4），失败回退单步检索（I5）。
"""

from __future__ import annotations

import json as _json

import pytest
from sqlalchemy import select

from app.api import chat_routes
from app.api.chat_routes import TurnPlan
from app.core.config import settings
from app.models import Chunk, Message, MessageAttribution, SessionEvent
from app.retrieval.types import Material
from app.session import events as event_log
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401
from tests.retrieval.conftest import seed_document

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# 助手
# --------------------------------------------------------------------------


def _provider_with_tool_call(query: str, *, finish: str = "可以作答了"):
    """响应队列：① 调 ``knowledge_search`` ② 收尾（不再调工具）③ 留给回答链路的正文。"""
    from cubeloop.providers import FauxProvider, faux_assistant_message, faux_tool_call

    provider = FauxProvider(provider_id="faux")
    provider.set_responses(
        [
            faux_assistant_message([faux_tool_call("knowledge_search", {"query": query})]),
            faux_assistant_message(finish),
        ]
    )
    return provider


def _plain_model(reply: str):
    from cubeloop.providers import FauxProvider, faux_assistant_message

    provider = FauxProvider(provider_id="faux")
    provider.set_responses([faux_assistant_message(reply)])
    return provider.model("faux-chat")


def _install_model(monkeypatch, provider, *, answer_text: str, label: str = "internal_db") -> None:
    """把端点里的模型装配换成 Faux，并把**回答链路那一格**也排进队列。

    Agent 循环与回答链路共用 ``resolve_chat_model``；Faux 的队列按调用顺序消费，
    「循环吃掉前两格、回答吃第三格」——所以这里补第三格。
    """
    from cubeloop.providers import faux_assistant_message

    provider.append_responses([faux_assistant_message(answer_text)])
    model = provider.model("faux-chat")
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    monkeypatch.setattr(chat_routes, "resolve_routing_classifier", lambda: _StubClassifier(label))


def _install_retrieval(monkeypatch, materials: list[Material]) -> list[tuple]:
    """把 ``knowledge_search`` 的检索实现换成假件（零向量库 / 零 embedding）。"""
    from app.agent.tools import knowledge_search

    calls: list[tuple] = []

    async def fake_retrieve(session, question, scope, *, stats=None):
        calls.append((question, scope))
        return list(materials), "", None

    monkeypatch.setattr(knowledge_search, "_default_retrieve", lambda: fake_retrieve)
    return calls


async def _seed_materials(db, texts: list[str], *, filename: str) -> list[Material]:  # noqa: ANN001
    """造**真实**的 documents/chunks 行，并返回与它们一一对应的材料。

    为什么不用手搓 ``Material``：``message_attributions.chunk_id`` 是指向 ``chunks`` 的
    外键——假 id 会撞 FK，归因整批写入回滚，用例就测不到「归因真的落了」。
    """
    document = await seed_document(db, texts, filename=filename)
    rows = await db.execute(
        select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.ordinal)
    )
    materials: list[Material] = []
    for chunk in rows.scalars().all():
        materials.append(
            Material(
                chunk_id=chunk.id,
                document_id=document.id,
                content=chunk.content,
                filename=filename,
                block_ordinal=chunk.ordinal,
            )
        )
    return materials


async def _events(client, payload: dict) -> list[dict]:  # noqa: ANN001
    resp = await client.post("/api/chat/stream", json=payload)
    assert resp.status_code == 200, resp.text
    return parse_sse(resp.text)


def _plan_json(*steps: dict) -> str:
    """计划器该回的 JSON 文本（``plan_execute`` 的唯一一次计划调用的返回值）。"""
    return _json.dumps({"steps": list(steps)}, ensure_ascii=False)


def _plan_execute_provider(plan_text: str, answer_text: str):
    """P&E 的响应队列：**① 计划调用**（``generate``，唯一的模型调用在编排阶段）→ ``answer`` 留给回答链路。

    与 ReAct 的三格不同：P&E 步间不问模型，所以编排阶段只吃掉一格（条目 4.2 §2.1-3 的
    「N 步计划 ⇒ 模型调用 1 次 + 回答链路 1 次」）。
    """
    from cubeloop.providers import FauxProvider, faux_assistant_message

    provider = FauxProvider(provider_id="faux")
    provider.set_responses(
        [
            faux_assistant_message(plan_text),
            faux_assistant_message(answer_text),
        ]
    )
    return provider


async def _refresh(db) -> None:  # noqa: ANN001
    """结束 ``db`` 会话当前读事务，让后面的断言能看见 HTTP 请求**另一个会话**刚提交的行。

    ⚠️ 为什么必须显式做：``db`` 夹具在造语料时已经读过一次（取了 ``chunks.id``），
    MySQL 默认 REPEATABLE READ 下那次读会**钉住快照**；请求用的另一个会话随后提交的行
    在旧快照里看不见 —— 不刷新的话断言会看到「一行都没有」，与真假无关。
    """
    await db.commit()


async def _assistant_messages(db, chat_id: int) -> list[Message]:  # noqa: ANN001
    rows = await db.execute(
        select(Message)
        .where(Message.chat_id == chat_id, Message.role == "assistant")
        .order_by(Message.id)
    )
    return list(rows.scalars().all())


async def _any_chat_id(db) -> int:  # noqa: ANN001
    rows = await db.execute(select(Message.chat_id).order_by(Message.id))
    return int(rows.scalars().first())


def _plan_with_skills(skill_ids: list[str]) -> TurnPlan:
    """只构造 ``wants_web`` 需要的那几个字段（它只看 ``skill_ids``）。"""
    return TurnPlan(
        chat=None,  # type: ignore[arg-type]
        question="q",
        images=[],
        history=[],
        filters=None,
        skill_ids=list(skill_ids),
        source_type="external",
        kind="direct",
        answer_source="general",
    )


# --------------------------------------------------------------------------
# 1 · agent_mode 不再被静默忽略
# --------------------------------------------------------------------------


async def test_off_mode_keeps_legacy_behaviour(client, monkeypatch):  # noqa: ANN001
    """``agent_mode`` 不传 / ``off``：**逐字**走旧链路，``agent_trace`` 恒 ``None``。"""
    monkeypatch.setattr(settings, "AGENT_MODE", "off")
    model = _plain_model("库外直答。")
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("external")
    )

    events = await _events(client, {"message": "你好"})
    assert [e["type"] for e in events] == ["meta", "token", "done"]
    assert events[-1]["agent_trace"] is None


async def test_react_mode_emits_agent_frames_before_tokens(client, db, monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(settings, "AGENT_MODE", "off")
    materials = await _seed_materials(db, ["蓝鲸是哺乳动物。", "鲸类生活在海里。"], filename="蓝鲸.md")
    _install_retrieval(monkeypatch, materials)
    provider = _provider_with_tool_call("蓝鲸")
    _install_model(monkeypatch, provider, answer_text="蓝鲸是哺乳动物。[1]")

    events = await _events(client, {"message": "蓝鲸是什么", "agent_mode": "react"})
    kinds = [e["type"] for e in events]

    assert kinds[0] == "meta"
    assert "agent" in kinds and "done" in kinds
    # Agent 帧**全部在任何 token 之前**（老基线同序：先编排，再作答）
    assert kinds.index("token") > max(i for i, k in enumerate(kinds) if k == "agent")

    steps = [e["step"] for e in events if e["type"] == "agent"]
    assert [s["kind"] for s in steps] == ["tool", "observation", "thought"]
    assert steps[0]["tool"] == "knowledge_search"
    assert steps[0]["title"] == "检索知识库：蓝鲸"
    assert steps[0]["tool_call_id"]
    assert steps[1]["tool_call_id"] == steps[0]["tool_call_id"]
    assert "[1] 蓝鲸.md" in steps[1]["detail"]
    assert "[2] 蓝鲸.md" in steps[1]["detail"]

    done = events[-1]
    assert done["type"] == "done"
    trace = done["agent_trace"]
    assert trace["mode"] == "react"
    assert trace["events_incomplete"] is False
    assert [s["kind"] for s in trace["steps"]] == ["tool", "observation", "thought"]
    # 旧前端只认这五个 kind，新增字段不改义
    assert set(trace) == {"mode", "steps", "events_incomplete"}


async def test_unknown_agent_mode_is_400_before_stream(client):  # noqa: ANN001
    resp = await client.post("/api/chat/stream", json={"message": "你好", "agent_mode": "nope"})
    assert resp.status_code == 400
    assert "Agent" in resp.json()["detail"]
    assert "text/event-stream" not in resp.headers.get("content-type", "")


async def test_mode_resolution_order(client, monkeypatch):  # noqa: ANN001
    """请求值 → 设置页默认值（坏值按 off）→ 带图强制 off。"""
    from app.api.chat_schemas import ChatRequest

    monkeypatch.setattr(settings, "AGENT_MODE", "react")
    assert chat_routes.resolve_agent_mode(ChatRequest(message="x")) == "react"
    assert chat_routes.resolve_agent_mode(ChatRequest(message="x", agent_mode="off")) == "off"
    monkeypatch.setattr(settings, "AGENT_MODE", "乱填的")
    assert chat_routes.resolve_agent_mode(ChatRequest(message="x")) == "off"
    assert (
        chat_routes.resolve_agent_mode(
            ChatRequest(message="x", agent_mode="react", images=["data:image/png;base64,AAA"])
        )
        == "off"
    )
    # 旧单图字段同样强制 off（chat_images 的回落口径）
    assert (
        chat_routes.resolve_agent_mode(
            ChatRequest(message="x", agent_mode="react", image_data="data:image/png;base64,AAA")
        )
        == "off"
    )


# --------------------------------------------------------------------------
# 2 · I3：Agent 轮与普通轮同一套引用编号
# --------------------------------------------------------------------------


async def test_agent_turn_reuses_shared_citation_path(client, db, monkeypatch):  # noqa: ANN001
    """Agent 检到的材料替换本轮快照；引用走**同一套** refs（ordinal 与 [n] 同源）。"""
    materials = await _seed_materials(
        db, ["蓝鲸是哺乳动物。", "鲸类生活在海里。"], filename="蓝鲸.md"
    )
    _install_retrieval(monkeypatch, materials)
    provider = _provider_with_tool_call("蓝鲸")
    _install_model(monkeypatch, provider, answer_text="蓝鲸是哺乳动物。[2]")

    events = await _events(client, {"message": "蓝鲸是什么", "agent_mode": "react"})
    done = events[-1]
    assert done["kind"] == "normal"
    assert done["answer_source"] == "kb"
    refs = done["refs"]
    assert [r["ordinal"] for r in refs] == [2]
    assert refs[0]["title"] == "蓝鲸.md"
    assert refs[0]["citation_mode"] == "cited"
    assert refs[0]["chunk_id"].startswith("kb:")
    assert refs[0]["retrieval_id"]
    # 模型看到的编号（观察文本里的 [1]/[2]）与引用卡片一致（I3）
    steps = [e["step"] for e in events if e["type"] == "agent"]
    assert "[1] 蓝鲸.md" in steps[1]["detail"]
    assert "[2] 蓝鲸.md" in steps[1]["detail"]


async def test_agent_turn_persists_trace_and_attributions(client, db, monkeypatch):  # noqa: ANN001
    """轨迹、引用与归因都真的落库（不是只发了帧）。"""
    materials = await _seed_materials(db, ["蓝鲸是哺乳动物。"], filename="蓝鲸.md")
    _install_retrieval(monkeypatch, materials)
    provider = _provider_with_tool_call("蓝鲸")
    _install_model(monkeypatch, provider, answer_text="蓝鲸是哺乳动物。[1]")

    await _events(client, {"message": "蓝鲸是什么", "agent_mode": "react"})
    await _refresh(db)
    assistant = (await _assistant_messages(db, await _any_chat_id(db)))[-1]

    assert assistant.kind == "normal"
    assert assistant.source_type == "internal_db"
    assert assistant.agent_trace["mode"] == "react"
    assert len(assistant.agent_trace["steps"]) == 3
    assert assistant.refs and assistant.refs[0]["title"] == "蓝鲸.md"

    rows = await db.execute(
        select(MessageAttribution).where(MessageAttribution.message_id == assistant.id)
    )
    attributions = list(rows.scalars().all())
    assert [a.ordinal for a in attributions] == [1]
    assert attributions[0].chunk_id == materials[0].chunk_id
    assert attributions[0].meta["retrieval_id"] == assistant.refs[0]["retrieval_id"]


# --------------------------------------------------------------------------
# 3 · I4：tool/call ↔ tool/result 配对
# --------------------------------------------------------------------------


async def test_tool_events_are_paired_by_tool_call_id(client, db, monkeypatch):  # noqa: ANN001
    materials = await _seed_materials(db, ["蓝鲸是哺乳动物。"], filename="蓝鲸.md")
    _install_retrieval(monkeypatch, materials)
    provider = _provider_with_tool_call("蓝鲸")
    _install_model(monkeypatch, provider, answer_text="蓝鲸是哺乳动物。[1]")

    # ⚠️ ``event_write_failures()`` 是**进程全局**计数 ⇒ 断言必须量**增量**（见下方 `assert`）。
    # 本仓库出现过一次同型事故：阶段 4.3 的用例故意注入写失败，把全局计数顶上去，
    # 于是这里的 ``== 0`` 在**整目录连跑**时假红（单跑本文件却绿）——测试间的进程级状态泄漏。
    failures_before = event_log.event_write_failures()
    await _events(client, {"message": "蓝鲸是什么", "agent_mode": "react"})
    await _refresh(db)
    rows = await db.execute(
        select(SessionEvent)
        .where(SessionEvent.type.in_(("tool/call", "tool/result")))
        .order_by(SessionEvent.seq)
    )
    events = list(rows.scalars().all())
    assert [e.type for e in events] == ["tool/call", "tool/result"]
    assert events[0].tool_call_id and events[0].tool_call_id == events[1].tool_call_id
    # ``tool/call`` 是仅日志事件（禁止 surface_op）、``tool/result`` 是表层事件（必须有）——
    # 这一条由事件层 ``_check_type`` 强制：写错会被 ``SurfaceOpError`` 拒掉而不落库，
    # 所以「两条都真的在库里」本身就是这条口径的证据。库表里没有 surface_op 列，
    # 这里改为钉住两侧载荷的形状。
    assert events[0].data["tool"] == "knowledge_search"
    assert events[0].data["args"] == {"query": "蓝鲸"}
    assert events[0].data["round"] == 1
    assert events[1].data["ok"] is True
    assert events[1].data["materials_hit"] == 1
    assert events[1].data["content"]
    assert events[1].data["retrieval_id"]
    assert event_log.event_write_failures() == failures_before


# --------------------------------------------------------------------------
# 4 · 联网门控（条目 4.1 明文）
# --------------------------------------------------------------------------


async def test_web_tools_absent_when_web_disabled(client, db, monkeypatch):  # noqa: ANN001
    """``WEB_ENABLE=False`` 时即便前端发了 ``builtin:web``，联网工具也不进清单。"""
    from app.agent import runner
    from app.api import skills_spec

    monkeypatch.setattr(settings, "WEB_ENABLE", False)
    # 模拟预检后配置变更；工具闸门仍须独立阻断联网（041预检另有端到端用例）。
    monkeypatch.setattr(skills_spec, "list_skills", lambda: [
        {"id": "builtin:web", "name": "联网检索", "available": True, "enabled": True}
    ])
    seen: list[list[str]] = []
    real_build = runner.build_agent_tools

    def spy(**kwargs):  # noqa: ANN003
        tools, collector = real_build(**kwargs)
        seen.append([t.name for t in tools])
        return tools, collector

    monkeypatch.setattr(runner, "build_agent_tools", spy)
    materials = await _seed_materials(db, ["蓝鲸是哺乳动物。"], filename="蓝鲸.md")
    _install_retrieval(monkeypatch, materials)
    provider = _provider_with_tool_call("蓝鲸")
    _install_model(monkeypatch, provider, answer_text="蓝鲸是哺乳动物。[1]")

    await _events(
        client,
        {"message": "蓝鲸是什么", "agent_mode": "react", "skill_ids": ["builtin:web"]},
    )
    # D4：超集 + 点名（唯一允许的改判；新工具无条件注册）
    assert len(seen) == 1 and {
        "knowledge_search", "current_time", "list_documents", "read_document"
    } <= set(seen[0])


async def test_wants_web_requires_flag_and_skill(client, monkeypatch):  # noqa: ANN001
    """两个条件缺一不可：设置里开了 + 前端显式带了 ``builtin:web``。"""
    monkeypatch.setattr(settings, "WEB_ENABLE", False)
    plan = _plan_with_skills(["builtin:web"])
    assert chat_routes.wants_web(plan) is False
    monkeypatch.setattr(settings, "WEB_ENABLE", True)
    assert chat_routes.wants_web(plan) is True
    assert chat_routes.wants_web(_plan_with_skills([])) is False


# --------------------------------------------------------------------------
# 5 · 降级与安全网（I5）
# --------------------------------------------------------------------------


async def test_tool_failure_does_not_break_the_turn(client, db, monkeypatch):  # noqa: ANN001
    """工具炸了也照常作答：观察 ``ok=False``，正文正常返回，**不炸穿**。"""
    from app.agent.tools import knowledge_search

    async def boom(session, question, scope, *, stats=None):  # noqa: ANN001
        raise RuntimeError("向量库炸了")

    monkeypatch.setattr(knowledge_search, "_default_retrieve", lambda: boom)
    provider = _provider_with_tool_call("蓝鲸")
    _install_model(monkeypatch, provider, answer_text="没查到，先说个大概。")

    events = await _events(client, {"message": "蓝鲸是什么", "agent_mode": "react"})
    steps = [e["step"] for e in events if e["type"] == "agent"]
    observation = [s for s in steps if s["kind"] == "observation"][0]
    assert observation["ok"] is False
    # 回归修复：SSE 观察帧同样只带固定形状（异常原文不外泄）
    assert "向量库炸了" not in observation["detail"]
    assert "知识库检索 执行失败（RuntimeError）。" in observation["detail"]
    assert events[-1]["type"] == "done"
    assert events[-1]["agent_trace"]["steps"]  # 失败也留完整轨迹


async def test_agent_without_materials_goes_direct_without_baseline_refs(client, db, monkeypatch):  # noqa: ANN001
    """Agent 空手而归 ⇒ 引用清空、kind=direct（`回归修复` 拍板③ · 2026-09-27 就地适配）。

    ⚠️ 本用例原名 `test_agent_without_materials_keeps_baseline_retrieval`，断「空手时基线
    检索结果原样保留并挂成引用」——那正是 076 的漏点（12 条 fallback 与正文「没找到」
    自相矛盾）。拍板③（一致性优先）后：空手 ⇒ 清空。
    """
    from app.agent.tools import knowledge_search
    from app.retrieval.types import number, render_materials

    baseline = await _seed_materials(db, ["基线检索到的内容。"], filename="基线.md")

    async def empty_retrieve(session, question, scope, *, stats=None):  # noqa: ANN001
        return [], "", None

    async def fake_baseline(session, text, filters=None, *, stats=None):  # noqa: ANN001
        return number(baseline), render_materials(baseline), None

    monkeypatch.setattr(knowledge_search, "_default_retrieve", lambda: empty_retrieve)
    monkeypatch.setattr(chat_routes, "retrieve_materials", fake_baseline)

    provider = _provider_with_tool_call("蓝鲸")
    _install_model(monkeypatch, provider, answer_text="蓝鲸是哺乳动物。[1]")

    events = await _events(client, {"message": "蓝鲸是什么", "agent_mode": "react"})
    done = events[-1]
    assert done["kind"] == "direct"
    assert done["refs"] == [], f"空手收尾不得挂基线候选引用（076）：{done.get('refs')}"


async def test_plan_execute_runs_real_plan_and_keeps_refs_in_sync(client, db, monkeypatch):  # noqa: ANN001
    """条目 4.2 端到端：真 P&E（一次计划调用 → 逐步执行）且引用与观察编号同源（I3）。

    这条替换了 4.1 的 ``test_plan_execute_degrades_visibly``（它断言的是**降级**，语义已变）：
    轨迹里**不再**有「计划执行未实装」，首帧是 ``plan`` 步、``title="执行计划"``。
    """
    materials = await _seed_materials(
        db, ["蓝鲸是哺乳动物。", "鲸类生活在海里。"], filename="蓝鲸.md"
    )
    _install_retrieval(monkeypatch, materials)
    provider = _plan_execute_provider(
        _plan_json(
            {"goal": "检索蓝鲸的定义", "tool": "knowledge_search", "args": {"query": "蓝鲸"}}
        ),
        answer_text="蓝鲸是哺乳动物。[1]",
    )
    _install_model(monkeypatch, provider, answer_text="蓝鲸是哺乳动物。[1]")

    events = await _events(client, {"message": "蓝鲸是什么", "agent_mode": "plan_execute"})
    kinds = [e["type"] for e in events]
    assert kinds[0] == "meta" and kinds[-1] == "done"
    # 编排帧全部在任何 token 之前（与 ReAct 同序）
    assert kinds.index("token") > max(i for i, k in enumerate(kinds) if k == "agent")

    steps = [e["step"] for e in events if e["type"] == "agent"]
    assert [s["kind"] for s in steps] == ["plan", "thought", "tool", "observation"]
    assert steps[0]["title"] == "执行计划"
    assert steps[0]["detail"] == "1. 检索蓝鲸的定义（工具：knowledge_search）"
    assert steps[2]["title"] == "检索知识库：蓝鲸"
    assert "[1] 蓝鲸.md" in steps[3]["detail"]
    assert "[2] 蓝鲸.md" in steps[3]["detail"]

    done = events[-1]
    trace = done["agent_trace"]
    assert trace["mode"] == "plan_execute"  # 老基线同口径
    assert set(trace) == {"mode", "steps", "events_incomplete"}
    assert "计划执行未实装" not in [s["title"] for s in trace["steps"]]
    # I3：引用卡片的编号取自同一份材料快照（模型正文写 [1]，引用就是 ordinal 1；
    # 观察文本里的 [1]/[2] 与它同源——由同一个 ``ToolCollector`` 编号）
    assert done["kind"] == "normal" and done["answer_source"] == "kb"
    assert [r["ordinal"] for r in done["refs"]] == [1]
    assert done["refs"][0]["title"] == "蓝鲸.md"
    # ``messages.agent_trace`` 与 SSE 同一份（落库路径也走真 P&E）
    await _refresh(db)
    assistant = (await _assistant_messages(db, await _any_chat_id(db)))[-1]
    assert assistant.agent_trace["mode"] == "plan_execute"
    assert [s["kind"] for s in assistant.agent_trace["steps"]] == [
        "plan",
        "thought",
        "tool",
        "observation",
    ]


async def test_plan_events_are_paired_and_do_not_fake_completeness(client, db, monkeypatch):  # noqa: ANN001
    """I4：P&E 每个计划步的 ``tool/call`` ↔ ``tool/result`` 同 id 成对且无静默丢事件。

    ⚠️ 必须用**带 ``request_id`` 的请求**：一轮只允许一条事件带它（``uk(chat_id, request_id)``，
    第 2 条起会被静默吞掉——`回归修复`）。不带 request_id 的用例会**假绿**。
    """
    materials = await _seed_materials(
        db, ["蓝鲸是哺乳动物。", "鲸类生活在海里。"], filename="蓝鲸.md"
    )
    _install_retrieval(monkeypatch, materials)
    provider = _plan_execute_provider(
        _plan_json(
            {"goal": "第一次检索", "tool": "knowledge_search", "args": {"query": "蓝鲸"}},
            {"goal": "换个词再检", "tool": "knowledge_search", "args": {"query": "鲸类"}},
        ),
        answer_text="蓝鲸是哺乳动物。[1]",
    )
    _install_model(monkeypatch, provider, answer_text="蓝鲸是哺乳动物。[1]")

    failures_before = event_log.event_write_failures()  # 全局计数 ⇒ 只量增量（见上一用例的注释）
    await _events(
        client,
        {"message": "蓝鲸是什么", "agent_mode": "plan_execute", "request_id": "req-pe-pair"},
    )
    await _refresh(db)
    rows = await db.execute(
        select(SessionEvent)
        .where(SessionEvent.type.in_(("tool/call", "tool/result")))
        .order_by(SessionEvent.seq)
    )
    events = list(rows.scalars().all())
    assert [e.type for e in events] == ["tool/call", "tool/result"] * 2
    calls = [e for e in events if e.type == "tool/call"]
    results = [e for e in events if e.type == "tool/result"]
    # 每个计划步一个 ``tool_call_id``，两两配对（I4 的 {0,1} 差分在这里取 0）
    assert len({e.tool_call_id for e in calls}) == 2
    assert [e.tool_call_id for e in calls] == [e.tool_call_id for e in results]
    # ``round`` = **计划步序号**（1 起，条目 4.2 §2.1-5）
    assert [e.data["round"] for e in calls] == [1, 2]
    assert [e.data["args"] for e in calls] == [{"query": "蓝鲸"}, {"query": "鲸类"}]
    # 事件**真的两条都落了**（``tool/call`` 带 surface_op 会被 SurfaceOpError 拒掉）
    assert all(e.data["ok"] is True for e in results)
    # 载荷诚实性（对抗性核验 F3）：``chars`` 量的就是 ``content`` 那段文本
    assert all(e.data["chars"] == len(e.data["content"]) for e in results)
    assert all(e.data["chars"] > 0 for e in results)
    assert event_log.event_write_failures() == failures_before


async def test_plan_step_with_bad_args_records_a_real_failure(client, db, monkeypatch):  # noqa: ANN001
    """对抗性核验 F2：计划步 ``args`` 校验失败时，事件载荷必须**照实**记这次失败。

    原先落库是 ``chars=0 / warnings=0 / content=""``（因为那条观察没有并入 collector），
    只有 ``error`` 是对的——按 ``chars==0`` 查库会把「有文本有警告的失败」读成「空结果」。
    """
    materials = await _seed_materials(db, ["蓝鲸是哺乳动物。"], filename="蓝鲸.md")
    calls = _install_retrieval(monkeypatch, materials)
    provider = _plan_execute_provider(
        _plan_json(
            # ``limit`` 有 ``le=FUSE_LIMIT`` 约束：999 必然校验不过
            {"goal": "越界参数", "tool": "knowledge_search", "args": {"query": "蓝鲸", "limit": 999}}
        ),
        answer_text="蓝鲸是哺乳动物。",
    )
    _install_model(monkeypatch, provider, answer_text="蓝鲸是哺乳动物。")

    failures_before = event_log.event_write_failures()  # 全局计数 ⇒ 只量增量
    events = await _events(
        client,
        {"message": "蓝鲸是什么", "agent_mode": "plan_execute", "request_id": "req-pe-badargs"},
    )
    steps = [e["step"] for e in events if e["type"] == "agent"]
    assert [s["kind"] for s in steps] == ["plan", "thought", "tool", "observation"]
    assert steps[3]["ok"] is False and "参数校验失败" in steps[3]["detail"]
    assert calls == []  # 没有真去检索

    await _refresh(db)
    rows = await db.execute(
        select(SessionEvent)
        .where(SessionEvent.type.in_(("tool/call", "tool/result")))
        .order_by(SessionEvent.seq)
    )
    db_events = list(rows.scalars().all())
    assert [e.type for e in db_events] == ["tool/call", "tool/result"]  # 失败也照样成对（I4）
    payload = db_events[1].data
    assert payload["ok"] is False
    assert payload["chars"] > 0 and payload["content"]  # **不是**空结果
    assert payload["chars"] == len(payload["content"])
    assert "参数校验失败" in payload["content"]  # 内容里写明是校验失败
    assert "limit" in payload["error"]  # error 是原始校验错误（哪个键越界一眼可见）
    assert event_log.event_write_failures() == failures_before


async def test_plan_parse_failure_falls_back_to_a_single_search(client, db, monkeypatch):  # noqa: ANN001
    """「失败回退单步检索」（条目原文）：计划不可解析 ⇒ note **且真检索一次**。"""
    materials = await _seed_materials(db, ["蓝鲸是哺乳动物。"], filename="蓝鲸.md")
    calls = _install_retrieval(monkeypatch, materials)
    provider = _plan_execute_provider("这不是 JSON", answer_text="蓝鲸是哺乳动物。[1]")
    _install_model(monkeypatch, provider, answer_text="蓝鲸是哺乳动物。[1]")

    events = await _events(client, {"message": "蓝鲸是什么", "agent_mode": "plan_execute"})
    steps = [e["step"] for e in events if e["type"] == "agent"]
    assert [s["kind"] for s in steps] == ["note", "tool", "observation"]
    assert steps[0]["title"] == "计划回退"
    assert steps[1]["tool"] == "knowledge_search"
    assert calls and calls[0][0] == "蓝鲸是什么"  # 原始问题当检索词，**真的检索了**
    assert events[-1]["type"] == "done"


async def test_plan_model_failure_does_not_break_the_turn(client, db, monkeypatch):  # noqa: ANN001
    """I5：计划调用抛错 ⇒ 一句可读 note，**轨迹照旧留在收尾帧上**，流不炸穿。

    计划调用失败后**不放弃作答**（不 ``return``）——回答链路照常跑，所以用户既看到
    「模型不可用」的可见留痕，也看到回答链路自己的结果或它自己的错误收尾。
    本腿只让计划调用失败，回答流仍交付正文：恰好一次 done 且携带轨迹。
    回答自身失败由 OPT2/153 载体另验，冻结 error 帧不追加 agent_trace 键。
    """
    materials = await _seed_materials(db, ["蓝鲸是哺乳动物。"], filename="蓝鲸.md")
    _install_retrieval(monkeypatch, materials)

    class PlanBoomModel:
        """只有**计划调用**（``generate``）炸：回答链路（``stream``）照常返回一段正文。"""

        async def generate(self, *_args, **_kwargs):
            raise RuntimeError("上游 502")

        async def stream(self, *_args, **_kwargs):
            from types import SimpleNamespace

            class _Stream:
                emitted = False

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    if self.emitted:
                        raise StopAsyncIteration
                    self.emitted = True
                    return SimpleNamespace(type="text_delta", delta="先按已有资料作答。")

                async def result(self):
                    return SimpleNamespace(
                        content=[SimpleNamespace(type="text", text="先按已有资料作答。")],
                        stop_reason="stop",
                    )

            return _Stream()
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: PlanBoomModel())
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db")
    )

    events = await _events(client, {"message": "蓝鲸是什么", "agent_mode": "plan_execute"})
    steps = [e["step"] for e in events if e["type"] == "agent"]
    assert [s["title"] for s in steps] == ["模型不可用"]
    # 计划失败而回答成功：正文真实交付，done 恰好一次且携带轨迹。
    assert [e["type"] for e in events].count("done") == 1
    assert not [e for e in events if e["type"] == "error"]
    assert "先按已有资料作答。" in "".join(e["text"] for e in events if e["type"] == "token")
    assert events[-1]["agent_trace"]["steps"][0]["title"] == "模型不可用"


def _scope_batch(mode: str, steps: list[dict], answer: str) -> list:
    """一轮「按给定步骤调 knowledge_search」的完整响应批次（含回答链路那一格）。

    ReAct：每次工具调用一条助手消息，最后一条收尾；P&E：**一次**计划调用列出全部步骤。
    两种模式的批次长度不同，但都**恰好**覆盖该模式的模型调用次数（队列不乱序）。
    """
    from cubeloop.providers import faux_assistant_message, faux_tool_call

    if mode == "react":
        return [
            *[
                faux_assistant_message([faux_tool_call("knowledge_search", step)])
                for step in steps
            ],
            faux_assistant_message("可以作答了"),
            faux_assistant_message(answer),
        ]
    return [
        faux_assistant_message(
            _plan_json(
                *[
                    {"goal": step["query"], "tool": "knowledge_search", "args": step}
                    for step in steps
                ]
            )
        ),
        faux_assistant_message(answer),
    ]


async def test_user_scope_reaches_the_tool_and_conflict_is_refused(client, db, monkeypatch):  # noqa: ANN001
    """专项 F6-A（A13）· 真 HTTP：用户范围 → 工具 → 最终材料/引用/落库**逐项一致**。

    两轮打同一条 ``/api/chat/stream``：

    - **冲突轮**：宿主 ``kb_ids=[1,2]``，模型先要 ``[9]``（越界）再要 ``[2,3]``（合法）；
    - **对照轮**：两次都要合法范围。

    判据：① 越界那次**一次检索都没发**；② 冲突观察可见、与合法观察**同键集**（拒绝不改变
    SSE/步骤帧形状）；③ ``done`` 正常收尾且引用来自合法材料；④ ``tool/call`` ↔ ``tool/result``
    成对落库、拒绝那条照实 ``ok=False``；⑤ 与对照轮**帧类型序列逐字相同** ⇒ 既有键集不变。
    """
    materials = await _seed_materials(db, ["蓝鲸是哺乳动物。"], filename="蓝鲸.md")
    calls = _install_retrieval(monkeypatch, materials)
    bad = {"query": "越界", "filters": {"kb_ids": [9]}}
    good = {"query": "合法", "filters": {"kb_ids": [2, 3]}}
    good2 = {"query": "合法", "filters": {"kb_ids": [1, 2]}}
    answer = "蓝鲸是哺乳动物。[1]"
    from cubeloop.providers import FauxProvider

    provider = FauxProvider(provider_id="faux")
    provider.set_responses(
        _scope_batch("react", [bad, good], answer)
        + _scope_batch("react", [good2, good2], answer)
    )
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: provider.model("faux-chat"))
    monkeypatch.setattr(chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db"))

    events = await _events(
        client,
        {
            "message": "蓝鲸是什么",
            "agent_mode": "react",
            "filters": {"kb_ids": [1, 2]},
            "request_id": "req-f6a-react",
        },
    )
    conflict_calls = list(calls)

    # ① 越界那次根本没落到检索实现；合法那次收到的是**取交结果**
    assert [c[0] for c in conflict_calls] == ["合法"]
    assert conflict_calls[0][1] == {"kb_ids": [2]}

    # ② 冲突可见，且两条观察帧**键集相同**（拒绝不是另一套帧形状）
    steps = [e["step"] for e in events if e["type"] == "agent"]
    observations = [s for s in steps if s["kind"] == "observation"]
    assert len(observations) == 2
    assert observations[0]["ok"] is False
    assert "知识库检索未执行" in observations[0]["detail"]
    assert observations[1]["ok"] is True
    assert set(observations[0]) == set(observations[1])

    # ③ 收尾正常，引用来自合法材料
    done = events[-1]
    assert done["type"] == "done"
    assert done["kind"] == "normal" and done["answer_source"] == "kb"
    assert [r["title"] for r in done["refs"]] == ["蓝鲸.md"]
    assert done["agent_trace"]["mode"] == "react"

    # ④ 落库：成对 + 拒绝照实记
    await _refresh(db)
    rows = await db.execute(
        select(SessionEvent)
        .where(SessionEvent.type.in_(("tool/call", "tool/result")))
        .order_by(SessionEvent.seq)
    )
    logged = list(rows.scalars().all())
    assert [e.type for e in logged] == ["tool/call", "tool/result"] * 2
    call_rows = [e for e in logged if e.type == "tool/call"]
    result_rows = [e for e in logged if e.type == "tool/result"]
    assert [e.tool_call_id for e in call_rows] == [e.tool_call_id for e in result_rows]
    # 模型提的越界参数**照实留痕**（``_args_digest`` 把非字符串值 JSON 化 ⇒ 这里解回来比）
    assert _json.loads(call_rows[0].data["args"]["filters"]) == bad["filters"]
    assert [e.data["ok"] for e in result_rows] == [False, True]
    assert result_rows[0].data["materials_hit"] == 0

    # ⑤ 对照轮（两次都合法）：帧类型序列与 done 键集逐字相同
    control = await _events(
        client,
        {
            "message": "蓝鲸是什么",
            "agent_mode": "react",
            "filters": {"kb_ids": [1, 2]},
            "request_id": "req-f6a-control",
        },
    )
    assert [e["type"] for e in control] == [e["type"] for e in events]
    assert set(control[-1]) == set(done)
    assert len([e for e in control if e["type"] == "agent"]) == len(
        [e for e in events if e["type"] == "agent"]
    )


async def test_user_scope_reaches_the_tool_in_plan_execute(client, db, monkeypatch):  # noqa: ANN001
    """专项 F6-A（A13）· 同一条判据在 **P&E** 上复跑（两条 Agent 链路都要测，不靠「一处 handler」推断）。"""
    materials = await _seed_materials(db, ["蓝鲸是哺乳动物。"], filename="蓝鲸.md")
    calls = _install_retrieval(monkeypatch, materials)
    bad = {"query": "越界", "filters": {"kb_ids": [9]}}
    good = {"query": "合法", "filters": {"kb_ids": [2, 3]}}
    answer = "蓝鲸是哺乳动物。[1]"
    from cubeloop.providers import FauxProvider

    provider = FauxProvider(provider_id="faux")
    provider.set_responses(_scope_batch("plan_execute", [bad, good], answer))
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: provider.model("faux-chat"))
    monkeypatch.setattr(chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db"))

    events = await _events(
        client,
        {
            "message": "蓝鲸是什么",
            "agent_mode": "plan_execute",
            "filters": {"kb_ids": [1, 2]},
            "request_id": "req-f6a-pe",
        },
    )
    assert [c[0] for c in calls] == ["合法"]
    assert calls[0][1] == {"kb_ids": [2]}
    steps = [e["step"] for e in events if e["type"] == "agent"]
    observations = [s for s in steps if s["kind"] == "observation"]
    assert [s["ok"] for s in observations] == [False, True]
    assert "知识库检索未执行" in observations[0]["detail"]
    assert events[-1]["type"] == "done"
    assert events[-1]["agent_trace"]["mode"] == "plan_execute"
    assert [r["title"] for r in events[-1]["refs"]] == ["蓝鲸.md"]


async def test_plan_without_usable_tool_does_not_call_it(client, db, monkeypatch):  # noqa: ANN001
    """``tool`` 为 null 的步骤：只留 note，**不产生空转的工具调用**（也不落事件）。"""
    materials = await _seed_materials(db, ["蓝鲸是哺乳动物。"], filename="蓝鲸.md")
    calls = _install_retrieval(monkeypatch, materials)
    provider = _plan_execute_provider(
        _plan_json(
            {"goal": "这一步只做推理整理", "tool": None},
            {"goal": "查真正的资料", "tool": "knowledge_search", "args": {"query": "蓝鲸"}},
        ),
        answer_text="蓝鲸是哺乳动物。[1]",
    )
    _install_model(monkeypatch, provider, answer_text="蓝鲸是哺乳动物。[1]")

    events = await _events(
        client,
        {"message": "蓝鲸是什么", "agent_mode": "plan_execute", "request_id": "req-pe-skip"},
    )
    steps = [e["step"] for e in events if e["type"] == "agent"]
    assert [s["kind"] for s in steps] == [
        "plan",
        "note",
        "thought",
        "tool",
        "observation",
    ]
    assert steps[1]["title"] == "跳过：无可用工具"
    assert steps[1]["detail"] == "这一步只做推理整理"
    assert len(calls) == 1  # 只有第二个步骤真去检索

    await _refresh(db)
    rows = await db.execute(
        select(SessionEvent).where(SessionEvent.type == "tool/call").order_by(SessionEvent.seq)
    )
    events_rows = list(rows.scalars().all())
    assert len(events_rows) == 1  # 「没调」在日志里就是没调（不留空转记录）
