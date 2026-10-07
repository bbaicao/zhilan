"""U4 shared answer input: retain original constraints/evidence or fail before calling."""
from types import SimpleNamespace

import pytest

from app.generation import answer


def model(window=8192):
    return SimpleNamespace(spec=SimpleNamespace(context_window=window))


def test_shared_budget_keeps_original_question_and_full_hit_window():
    question = "只答甲公司2026年金额；不得换成乙，不要省略否定。"
    evidence = "[2] 命中窗口：甲公司未批准支付20万元。"
    history = [{"role": role, "content": "旧历史" * 1000}
               for role in ("user", "assistant") for _ in range(6)]
    tool = SimpleNamespace(tool="large", ok=True, text="工具" * 10000)
    prompt, messages, report = answer.assemble_answer_prompt(
        question, history, materials_text=evidence, memory_text="记忆" * 10000,
        tool_context=[tool], model=model(),
    )
    assert question in messages[-1]["content"]
    assert evidence in messages[-1]["content"]
    assert report["input_units"] + report["output_tokens"] + report["reserve"] <= 8192
    assert report["history_removed"] > 0
    assert report["memory_removed"] and report["tools_removed"] == 1
    assert "未纳入" in messages[-1]["content"]
    assert "记忆" not in prompt


def test_small_cited_input_retains_original_assembly_and_adds_counted_constraints():
    history = [{"role": "user", "content": "早先"}, {"role": "assistant", "content": "答"}]
    kwargs = dict(materials_text="[1] 原文未批准。", memory_text="相关记忆")
    prompt, messages, report = answer.assemble_answer_prompt("原问题", history, **kwargs)
    assert messages == answer.build_messages("原问题", history, materials_text=kwargs["materials_text"])
    assert prompt == (
        answer.system_prompt(True, memory_text=kwargs["memory_text"])
        + "\n\n" + answer.CITED_ANSWER_CONSTRAINTS
    )
    assert report["input_units"] == (
        answer.context_units(prompt)
        + sum(answer.context_units(item["content"]) + 16 for item in messages) + 16
    )
    assert report["history_removed"] == 0


@pytest.mark.parametrize("materials_text,images", [("", None), ("[1] 图片背景。", ["data:image/png;base64,AA=="])])
def test_citation_constraints_only_apply_to_text_answers_with_materials(materials_text, images):
    prompt, _, _ = answer.assemble_answer_prompt(
        "原问题", materials_text=materials_text, images=images, model=model(65536),
    )
    assert answer.CITED_ANSWER_CONSTRAINTS not in prompt
    assert prompt == answer.system_prompt(bool(materials_text), has_images=bool(images))


def test_essential_question_and_evidence_overflow_is_not_silently_cut():
    with pytest.raises(answer.ContextBudgetExceeded):
        answer.assemble_answer_prompt(
            "不能删我的硬约束" * 500, materials_text="[1] 未批准" * 500, model=model(4096)
        )


def test_utf8_measure_is_conservative_and_message_count_based():
    assert answer.context_units("中") >= 3
    history = [{"role": "user" if n % 2 == 0 else "assistant", "content": str(n)}
               for n in range(6)]
    _, messages, report = answer.assemble_answer_prompt("原问", history, model=model(8192))
    assert len(messages[:-1]) == 6
    assert report["history_kept"] == 6


@pytest.mark.asyncio
async def test_external_answer_does_not_send_accidental_kb_materials():
    class Fake:
        async def generate(self, messages, **kwargs):
            self.messages, self.kwargs = messages, kwargs
            return SimpleNamespace(content=[], stop_reason="stop", usage=None)
    fake = Fake()
    result = await answer.answer_direct(
        "解释一般知识", source_type="external", model=fake,
        has_material=True, materials_text="[1] 不应进入的私有资料",
    )
    assert result.kind == "direct" and result.refs == []
    assert "不应进入" not in str(fake.messages)


@pytest.mark.asyncio
async def test_output_payload_cap_supports_provider_token_names():
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        payload = {key: 99999, "other": 1}
        capped = await answer.cap_answer_payload(payload)
        assert capped[key] == answer.DIRECT_MAX_TOKENS
        assert payload[key] == 99999


@pytest.mark.asyncio
async def test_sync_bound_model_passes_cap_hook_to_provider():
    from cubeloop.providers.base import AssistantMessage, BaseProvider

    class CaptureProvider(BaseProvider):
        async def generate(self, model, messages, *, options=None, **kwargs):
            self.payload = await options.on_payload({"model": model.id}, model) if options else {}
            return AssistantMessage(content=[], stop_reason="stop")

    provider = CaptureProvider()
    await answer.answer_direct("原问题", model=provider.model("local-check"))
    assert provider.payload["max_tokens"] == answer.DIRECT_MAX_TOKENS


@pytest.mark.parametrize(("provider", "model_id", "base_url", "window"), [
    ("mimo", "mimo-v2.6-flash", "https://api.xiaomimimo.com/v1", 1_048_576),
    ("xiaomi", "mimo-v2.6-flash", "https://api.xiaomimimo.com/v1/", 1_048_576),
    ("mimo", "mimo-v2.6-flash-preview", "https://api.xiaomimimo.com/v1", 8192),
    ("mimo", "other-model", "https://api.xiaomimimo.com/v1", 8192),
    ("other", "mimo-v2.6-flash", "https://api.xiaomimimo.com/v1", 8192),
    ("mimo", "mimo-v2.6-flash", "https://proxy.example/v1", 8192),
    ("mimo", "mimo-v2.6-flash", "https://api.xiaomimimo.com.example/v1", 8192),
    ("mimo", "mimo-v2.6-flash", "https://api.xiaomimimo.com/v1/other", 8192),
    ("mimo", "mimo-v2.6-flash", "", 8192),
])
def test_model_window_uses_exact_documented_identity(provider, model_id, base_url, window):
    bound = answer._build_bound_model(
        include_reasoning=False,
        config=SimpleNamespace(
            provider=provider, model=model_id, base_url=base_url, api_key="offline-test-key",
        ),
    )
    assert bound is not None
    assert bound.spec.context_window == window
    assert bound.spec.max_tokens == answer.DIRECT_MAX_TOKENS


def test_documented_mimo_window_keeps_evidence_rejected_by_unknown_model_budget():
    bound = answer._build_bound_model(
        include_reasoning=False,
        config=SimpleNamespace(
            provider="mimo", model="mimo-v2.6-flash",
            base_url="https://api.xiaomimimo.com/v1", api_key="offline-test-key",
        ),
    )
    question = "只依据原文回答，完整保留甲公司未批准付款的否定条件。"
    evidence = "[1] 甲公司未批准付款。\n" + "完整资料，不能截掉。\n" * 500
    with pytest.raises(answer.ContextBudgetExceeded):
        answer.assemble_answer_prompt(question, materials_text=evidence, model=model())
    _, messages, report = answer.assemble_answer_prompt(
        question, materials_text=evidence, model=bound,
    )
    assert question in messages[-1]["content"]
    assert evidence in messages[-1]["content"]
    assert report["window"] == 1_048_576
    assert report["input_units"] > answer.DEFAULT_CONTEXT_WINDOW
    assert report["input_units"] + report["output_tokens"] + report["reserve"] <= report["window"]
