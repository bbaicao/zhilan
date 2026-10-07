"""OPT2：真实生成入口的状态、编号提示与图文正反例，无模型或数据库调用。"""

from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.core.errors import ProviderAuthError, ProviderTimeout
from app.generation import answer


class ReplyModel:
    def __init__(self, text="正常回答", reason="stop", **metadata):
        self.reply = SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason=reason, usage=None, **metadata,
        )
        self.spec = SimpleNamespace(context_window=65536)
        self.calls = []

    async def generate(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.reply


@pytest.mark.asyncio
@pytest.mark.parametrize(("text", "reason", "status"), [
    ("", "stop", "failed"),
    (" \r\n\t", "stop", "failed"),
    ("只有思考没有正文", "length", "incomplete"),
    ("部分答案", "aborted", "stopped"),
    ("", "aborted", "stopped"),
    ("说明：尚未配置或调用失败时应检查设置。", "stop", "completed"),
])
async def test_actual_generation_status_is_independent_of_error_words(text, reason, status):
    result = await answer.answer_direct("问题", model=ReplyModel(text, reason))
    assert result.status == status
    assert result.finish_reason == reason
    assert result.answer == text
    assert result.error_hint == (answer.EMPTY_ANSWER_HINT if status == "failed" else None)


@pytest.mark.asyncio
async def test_finish_reason_field_is_preserved_when_stop_reason_is_absent():
    model = ReplyModel("已生成的部分正文")
    del model.reply.stop_reason
    model.reply.finish_reason = "length"
    result = await answer.answer_direct("问题", model=model)
    assert result.status == "incomplete" and result.finish_reason == "length"
    assert result.answer == "已生成的部分正文"


@pytest.mark.asyncio
async def test_unconfigured_budget_exception_and_provider_error_are_explicit_failures(monkeypatch):
    monkeypatch.setattr(answer, "build_chat_model", lambda: None)
    missing = await answer.answer_direct("问题")
    assert missing.status == "failed"
    assert missing.error_hint == missing.answer == answer.NOT_CONFIGURED_HINT

    too_small = ReplyModel()
    too_small.spec.context_window = 1
    budget = await answer.answer_direct("必要原问题", model=too_small)
    assert budget.status == "failed"
    assert budget.error_hint == answer.CONTEXT_BUDGET_HINT
    assert too_small.calls == []

    class ExplodingModel:
        async def generate(self, *args, **kwargs):
            raise RuntimeError("synthetic unavailable")

    raised = await answer.answer_direct("问题", model=ExplodingModel())
    assert raised.status == "failed" and raised.finish_reason == "error"
    assert raised.error_hint == raised.answer

    returned = await answer.answer_direct(
        "问题", model=ReplyModel("不能冒充成功", "error", error_message="synthetic unavailable"),
    )
    assert returned.status == "failed" and returned.finish_reason == "error"
    assert returned.error_hint == returned.answer
    assert returned.answer != "不能冒充成功"


@pytest.mark.asyncio
@pytest.mark.parametrize(("status_code", "exception"), [
    (401, ProviderAuthError), (504, ProviderTimeout),
])
async def test_provider_auth_and_timeout_keep_existing_contract_exceptions(status_code, exception):
    with pytest.raises(exception):
        await answer.answer_direct(
            "问题", model=ReplyModel("", "error", status_code=status_code),
        )


@pytest.mark.asyncio
async def test_refusal_without_model_is_completed():
    result = await answer.answer_direct(
        "库内问题", source_type="internal_db", has_material=False, direct_enabled=False,
    )
    assert result.is_refuse and result.status == "completed"
    assert result.answer == answer.REFUSE_TEXT and result.refs == []


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize(("numbered", "tools"), [
    (False, False), (False, True), (True, False), (True, True),
])
def test_prompt_distinguishes_numbered_evidence_and_tool_results(monkeypatch, enabled, numbered, tools):
    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", enabled)
    evidence = "[4]（笔记：固定来源）\n原文未批准付款。" if numbered else ""
    items = [SimpleNamespace(tool="probe", text="[1] 工具自带数字，不能当文档编号", ok=True)] if tools else []
    prompt, messages, _ = answer.assemble_answer_prompt(
        "是否批准？", materials_text=evidence, tool_context=items, model=ReplyModel(),
    )
    assert (answer.CITED_ANSWER_CONSTRAINTS in prompt) == numbered
    assert ("资料，都带编号" in prompt or "下面的资料都带编号" in prompt) == numbered
    if numbered:
        assert evidence in messages[-1]["content"]
    elif tools:
        assert prompt == answer.TOOL_RESULT_SYSTEM_PROMPT
        assert "问题：" in messages[-1]["content"]
    if tools:
        assert answer.TOOL_CONTEXT_HEADER in messages[-1]["content"]
        assert items[0].text not in prompt


def test_unnumbered_material_body_does_not_invent_a_citation(monkeypatch):
    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", True)
    prompt, messages, _ = answer.assemble_answer_prompt(
        "问题", materials_text="普通资料正文，引用标记只在句末[1]。", model=ReplyModel(),
    )
    assert prompt == answer.TOOL_RESULT_SYSTEM_PROMPT
    assert answer.CITED_ANSWER_CONSTRAINTS not in prompt
    assert "普通资料正文" in messages[-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("numbered", [True, False])
async def test_image_with_text_keeps_input_and_numbered_material_identity(monkeypatch, numbered):
    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", False)
    model = ReplyModel("图文内容一致。[4]" if numbered else "图片内容。")
    evidence = "[4]（网页：合法来源）\n图像说明。" if numbered else ""
    result = await answer.answer_direct(
        "结合图片回答", model=model, source_type="internal_db", has_material=numbered,
        materials_text=evidence, images=["data:image/png;base64,AA=="],
    )
    assert result.status == "completed"
    messages, kwargs = model.calls[0]
    assert [part.type for part in messages[-1].content] == ["text", "image"]
    assert "结合图片回答" in messages[-1].content[0].text
    assert messages[-1].content[1].source == "AA=="
    assert kwargs["system_prompt"] == (
        answer.VISION_SYSTEM_PROMPT if numbered else answer.VISION_UNNUMBERED_SYSTEM_PROMPT
    )
    if numbered:
        assert evidence in messages[-1].content[0].text
