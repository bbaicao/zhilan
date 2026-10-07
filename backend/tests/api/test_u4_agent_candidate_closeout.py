"""U4: no-tool Agent closeout keeps only accepted knowledge-base candidates."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agent.runner import AgentTurn
from app.api import chat_routes
from app.generation import answer, citation
from app.models import Message, MessageAttribution, SessionEvent
from app.retrieval.types import Material, number, render_materials
from tests.api.test_chat_agent import _install_retrieval, _seed_materials
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401


USE_CANDIDATES = {"action": "answer", "use_candidates": True, "clarification": ""}


def _plan(*, kind="normal", source_type="internal_db", has_material=True):
    materials = number([
        Material(chunk_id=1, document_id=1, content="甲公司付款未批准。",
                 filename="付款记录.md", block_ordinal=0)
    ]) if has_material else []
    return SimpleNamespace(
        materials=materials, materials_text=render_materials(materials) if materials else "",
        all_refs=citation.refs_from_materials(materials, "rl-u4-no-tool"), refs=[],
        kind=kind, answer_source="kb" if kind == "normal" else "general",
        source_type=source_type, retrieval_id="rl-u4-no-tool",
        preset_answer="", needs_model=True, attempt_id="a-u4", tool_context=[],
    )


@pytest.mark.parametrize(
    "closeout,invalid",
    [(None, False), (USE_CANDIDATES, False), (None, True)],
    ids=["absent", "explicit-use", "invalid"],
)
def test_no_tool_closeout_preserves_accepted_snapshot(closeout, invalid):
    plan = _plan()
    materials, text, refs = plan.materials, plan.materials_text, plan.all_refs
    turn = AgentTurn(mode="react", closeout=closeout, closeout_invalid=invalid)
    if invalid:
        turn.note_step("结束决策无效", "已改用已检索到的资料。")

    chat_routes._apply_agent_closeout(plan, turn)

    assert (plan.kind, plan.answer_source, plan.source_type) == ("normal", "kb", "internal_db")
    assert plan.materials is materials and plan.all_refs is refs
    assert plan.materials_text == text and plan.retrieval_id == "rl-u4-no-tool"
    assert chat_routes.answer_context(plan)["materials_text"] == text
    assert citation.refs_for_kind(
        plan.kind, "甲公司付款未批准。[1]", plan.all_refs
    )[0]["chunk_id"] == materials[0].chunk_key


@pytest.mark.parametrize(
    "kind,source_type,has_material",
    [("direct", "external", True), ("direct", "internal_db", True),
     ("normal", "external", True), ("normal", "internal_db", False)],
)
def test_no_tool_closeout_cannot_promote_unaccepted_candidates(kind, source_type, has_material):
    plan = _plan(kind=kind, source_type=source_type, has_material=has_material)
    chat_routes._apply_agent_closeout(plan, AgentTurn(mode="react", closeout=USE_CANDIDATES))
    assert (plan.kind, plan.answer_source, plan.source_type) == ("direct", "general", "external")
    assert plan.materials == [] and plan.all_refs == [] and plan.refs == []
    assert chat_routes.answer_context(plan)["materials_text"] == ""


@pytest.mark.parametrize("evidence", ["tool-step", "observation-step", "tool-context"])
def test_actual_empty_tool_result_keeps_bug076_clear_rule(evidence):
    plan = _plan()
    turn = AgentTurn(mode="react", closeout=USE_CANDIDATES)
    if evidence == "tool-context":
        turn.tool_context = [SimpleNamespace(tool="knowledge_search", text="没有检索到相关资料。")]
    else:
        turn.add_step("tool" if evidence == "tool-step" else "observation", "检索知识库",
                      tool="knowledge_search")
    chat_routes._apply_agent_closeout(plan, turn)
    assert (plan.kind, plan.answer_source) == ("direct", "general")
    assert plan.materials == [] and plan.all_refs == [] and plan.materials_text == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,decision,label",
    [
        ("react", "已有资料，可以回答。", "internal_db"),
        ("react", '<<<CLOSEOUT>>>\n{"action":"answer","use_candidates":true,"clarification":""}\n<<<END>>>',
         "internal_db"),
        ("react", "<<<CLOSEOUT>>>\n{bad\n<<<END>>>", "internal_db"),
        ("plan_execute", '{"steps":[{"goal":"根据已有资料作答","tool":null,"args":{}}]}', "internal_db"),
        ("react", '<<<CLOSEOUT>>>\n{"action":"answer","use_candidates":true,"clarification":""}\n<<<END>>>',
         "external"),
    ],
    ids=["no-fence", "explicit-use", "invalid", "plan-without-tool", "external"],
)
async def test_no_tool_agent_http_keeps_snapshot_refs_and_attribution(
    client, db, monkeypatch, mode, decision, label,
):
    from cubeloop.providers import FauxProvider, faux_assistant_message

    question = "甲公司付款是否批准？"
    materials = number(await _seed_materials(db, ["甲公司付款未批准。"], filename="付款记录.md"))

    async def retrieve(_db, query, filters, *, stats=None):
        assert query == question and filters["kb_ids"] == [1]
        return list(materials), render_materials(materials), None

    monkeypatch.setattr(chat_routes, "retrieve_materials", retrieve)
    tool_calls = _install_retrieval(monkeypatch, [])
    provider = FauxProvider(provider_id="faux")
    provider.set_responses([
        faux_assistant_message(decision), faux_assistant_message("甲公司付款未批准。[1]"),
    ])
    model = provider.model("faux-chat")
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: model)
    monkeypatch.setattr(chat_routes, "resolve_routing_classifier", lambda: _StubClassifier(label))
    prompts = []
    assemble = answer.assemble_answer_prompt

    def capture(message, history=None, **kwargs):
        result = assemble(message, history, **kwargs)
        prompts.append(str(result))
        return result

    monkeypatch.setattr(answer, "assemble_answer_prompt", capture)
    response = await client.post("/api/chat/stream", json={
        "message": question, "agent_mode": mode, "filters": {"kb_ids": [1]},
    })
    assert response.status_code == 200, response.text
    done = parse_sse(response.text)[-1]
    assert done["type"] == "done" and tool_calls == []
    assert not any(step["kind"] in ("tool", "observation") for step in done["agent_trace"]["steps"])
    await db.commit()
    message = (await db.execute(
        select(Message).where(Message.chat_id == done["chat_id"], Message.role == "assistant")
    )).scalar_one()
    attributions = (await db.execute(
        select(MessageAttribution).where(MessageAttribution.message_id == message.id)
    )).scalars().all()
    end = (await db.execute(
        select(SessionEvent).where(SessionEvent.chat_id == done["chat_id"], SessionEvent.type == "turn/end")
    )).scalar_one()
    if label == "internal_db":
        assert done["kind"] == "normal" and done["answer_source"] == "kb"
        assert len(done["refs"]) == len(attributions) == end.data["final_material_count"] == 1
        assert done["refs"][0]["chunk_id"] == attributions[0].meta["chunk_key"] == materials[0].chunk_key
        assert attributions[0].chunk_id == materials[0].chunk_id
        assert attributions[0].meta["retrieval_id"] == done["refs"][0]["retrieval_id"]
        assert prompts and "[1]（文档：付款记录.md）" in prompts[-1] and materials[0].content in prompts[-1]
        assert (message.kind, message.answer_source) == ("normal", "kb")
    else:
        assert (done["kind"], done["answer_source"]) == ("direct", "general")
        assert done["refs"] == [] and attributions == [] and end.data["final_material_count"] == 0
        assert prompts and "[1]（文档：付款记录.md）" not in prompts[-1]
