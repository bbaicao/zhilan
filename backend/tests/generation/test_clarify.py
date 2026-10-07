"""条目 1.4.3 单测：clarify vs refuse vs direct 三路不混。

阶段 1 设计说明 条目 1.4 的三点规格：

1. 模糊/指代不清/过短 → ``kind=clarify``，输出追问原文（一次性下发）；
2. ``source_type`` 映射 → ``answer_source=clarify``、refs 空；
3. 追问上限与 ``ASSUME_PREFIX`` 语义对齐 02/老基线。

不变量：**I1 kind=clarify**；**I7 不把澄清误判成拒答**——本文件用「三路对照」
用例把 clarify / refuse / direct 的 kind、answer_source、文案三者一起钉死。

追问生成全部用 **CubeLoop FauxProvider** 驱动真链路（统筹 §3.4）。
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.generation import answer as answer_mod
from app.generation import clarify as clarify_mod
from app.generation import source_map


def faux_model(reply: str):
    """FauxProvider 驱动的真链路模型（只把上游换成假响应）。"""
    from cubeloop.providers.faux import FauxProvider, faux_assistant_message

    provider = FauxProvider(provider_id="faux")
    provider.set_responses([faux_assistant_message(reply)])
    return provider.model("faux-clarify")


@pytest.fixture
def clarify_on(monkeypatch):
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", True)
    monkeypatch.setattr(settings, "QUERY_CLARIFY_MAX_ROUNDS", 2)


# --------------------------------------------------------------------------
# 1.4.1 模糊判定（规则，纯函数）
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["嗯", "那个", "那个怎么样", "它呢", "这个怎么弄", "   ", "啊这", "他"],
)
def test_looks_vague_true(text):
    """过短 / 只有指代 / 剥掉虚词没剩东西 → 判模糊。"""
    assert clarify_mod.looks_vague(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "今天天气怎么样",
        "知识库里关于项目周报的内容",
        "帮我总结一下第三季度的销售数据",
        "Python 的 GIL 是什么",
        "把这段话翻译成英文：hello world",
    ],
)
def test_looks_vague_false(text):
    """有实质内容 → 不判模糊（**不许过度拦截**，否则伤害可用性）。"""
    assert clarify_mod.looks_vague(text) is False


def test_vagueness_reason_is_specific():
    """判定依据可区分（排障与阶段 5 调参用）。"""
    assert clarify_mod.vagueness_reason("嗯") == clarify_mod.REASON_TOO_SHORT
    assert clarify_mod.vagueness_reason("那个怎么样") in (
        clarify_mod.REASON_NO_SUBSTANCE,
        clarify_mod.REASON_DEMONSTRATIVE,
    )
    assert clarify_mod.vagueness_reason("今天天气怎么样") == clarify_mod.REASON_SPECIFIC


# --------------------------------------------------------------------------
# 1.4.1 + 1.4.2 追问生成（FauxProvider 真链路）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vague_question_gets_clarify(clarify_on):
    """模糊提问 → kind=clarify、answer_source=clarify、refs 空、正文是追问原文。"""
    verdict = await clarify_mod.judge_query(
        "那个怎么样", model=faux_model("你指的是哪份文档？")
    )

    assert verdict.clear is False
    assert verdict.is_clarify is True
    assert verdict.question == "你指的是哪份文档？"
    assert verdict.source == "llm"

    result = verdict.to_result()
    assert result.kind == "clarify"
    assert result.answer_source == "clarify"
    assert result.refs == []
    assert result.answer == "你指的是哪份文档？"


@pytest.mark.asyncio
async def test_clear_question_passes_through(clarify_on):
    """清晰提问 → 放行（clear=True），且**不调用模型**写追问。"""
    verdict = await clarify_mod.judge_query(
        "知识库里关于项目周报的内容", model=faux_model("不该被调用")
    )

    assert verdict.clear is True
    assert verdict.assume is False
    assert verdict.reason == clarify_mod.REASON_SPECIFIC
    assert verdict.question == ""


@pytest.mark.asyncio
async def test_clarify_question_is_cleaned(clarify_on):
    """模型爱加引号/前缀/换行 → 输出前剥干净，只留第一行。"""
    verdict = await clarify_mod.judge_query(
        "那个呢", model=faux_model('“追问：你指的是哪份文档？”\n第二行不该出现')
    )
    assert verdict.question == "你指的是哪份文档？"


# --------------------------------------------------------------------------
# 1.4.2 上限与 ASSUME_PREFIX
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_rounds_reached_assumes(clarify_on):
    """已达追问上限 → 放行且 assume=True（按现有信息作答）。"""
    verdict = await clarify_mod.judge_query(
        "那个怎么样", clarify_rounds=2, model=faux_model("不该被调用")
    )

    assert verdict.clear is True
    assert verdict.assume is True
    assert verdict.reason == clarify_mod.REASON_MAX_ROUNDS


@pytest.mark.asyncio
async def test_under_max_rounds_still_clarifies(clarify_on):
    """未达上限（1 < 2）→ 仍然追问。"""
    verdict = await clarify_mod.judge_query(
        "那个怎么样", clarify_rounds=1, model=faux_model("哪份文档？")
    )
    assert verdict.clear is False


def test_assume_prefix_matches_baseline():
    """ASSUME_PREFIX 文案与老基线逐字一致（阶段 1 条目 1.4 第 3 点）。"""
    assert clarify_mod.ASSUME_PREFIX == (
        "下面按目前已有信息作答（已达到追问上限），其中包含假设，请核实。"
    )
    assert clarify_mod.assume_prefix(False) == ""
    assert clarify_mod.assume_prefix(True) == f"{clarify_mod.ASSUME_PREFIX}\n\n"
    # 与 answer.py 里登记的是同一份常量（避免两处各写一份漂移）
    assert clarify_mod.ASSUME_PREFIX == answer_mod.ASSUME_PREFIX


def test_max_rounds_config_clamped():
    """上限配置：负值按 0 处理（不出现「负数轮」这种荒谬语义）。"""
    assert clarify_mod.max_rounds() >= 0


# --------------------------------------------------------------------------
# 降级：拿不到追问就放行（澄清是增益，不是关卡）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_off_passes_through(monkeypatch):
    """开关关闭 → 放行（不追问、不调用模型）。"""
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", False)

    verdict = await clarify_mod.judge_query("那个怎么样", model=faux_model("不该被调用"))
    assert verdict.clear is True
    assert verdict.reason == clarify_mod.REASON_DISABLED


@pytest.mark.asyncio
async def test_no_model_passes_through(clarify_on):
    """没配模型 → 放行（不把问答卡死）。"""
    from cubeloop.providers.faux import FauxProvider

    provider = FauxProvider(provider_id="faux")  # 无排队响应 → error 路径
    verdict = await clarify_mod.judge_query(
        "那个怎么样", model=provider.model("faux-clarify")
    )

    assert verdict.clear is True
    assert verdict.reason == clarify_mod.REASON_NO_QUESTION


@pytest.mark.asyncio
async def test_model_exception_passes_through(clarify_on):
    """模型抛异常 → 放行，**不炸穿**（I5 精神）。"""

    class Broken:
        async def generate(self, *args, **kwargs):
            raise RuntimeError("模型挂了")

    verdict = await clarify_mod.judge_query("那个怎么样", model=Broken())
    assert verdict.clear is True
    assert verdict.reason == clarify_mod.REASON_NO_QUESTION


@pytest.mark.asyncio
async def test_empty_question_passes_through(clarify_on):
    """模型回空串 → 放行（不拿空追问去问用户）。"""
    verdict = await clarify_mod.judge_query("那个怎么样", model=faux_model(""))
    assert verdict.clear is True
    assert verdict.reason == clarify_mod.REASON_NO_QUESTION


# --------------------------------------------------------------------------
# 1.4.3 核心：clarify vs refuse vs direct 三路不混
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_way_separation(clarify_on, monkeypatch):
    """同一套映射下，三路的 kind / answer_source / 文案三者全不同。

    这是 I7 的核心断言：澄清是「我再问你一句」，拒答是「资料里没有」，
    直答是「用我自己的知识答」——用户必须能一眼分清。
    """
    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", True)

    # ① 澄清：模糊提问
    clarify_verdict = await clarify_mod.judge_query(
        "那个怎么样", model=faux_model("你指的是哪份文档？")
    )
    clarify_result = clarify_verdict.to_result()

    # ② 拒答：库内未命中 + 开关关闭
    refuse_result = await answer_mod.answer_direct(
        "知识库里关于项目周报的内容",
        source_type="internal_db",
        has_material=False,
        direct_enabled=False,
    )

    # ③ 直答：库外问题
    direct_result = await answer_mod.answer_direct(
        "今天天气怎么样", model=faux_model("今天晴。"), source_type="external"
    )

    kinds = {clarify_result.kind, refuse_result.kind, direct_result.kind}
    sources = {
        clarify_result.answer_source,
        refuse_result.answer_source,
        direct_result.answer_source,
    }
    answers = {clarify_result.answer, refuse_result.answer, direct_result.answer}

    assert kinds == {"clarify", "refuse", "direct"}, "三路 kind 必须互不相同"
    assert sources == {"clarify", "empty_scope", "general"}, "三路 answer_source 必须互不相同"
    assert len(answers) == 3, "三路正文必须互不相同"
    assert all(r.refs == [] for r in (clarify_result, refuse_result, direct_result))


@pytest.mark.asyncio
async def test_clarify_never_uses_refuse_wording(clarify_on):
    """澄清正文不得是拒答文案（也不得含拒答措辞）。"""
    verdict = await clarify_mod.judge_query(
        "那个怎么样", model=faux_model("你指的是哪份文档？")
    )
    result = verdict.to_result()

    assert result.answer != answer_mod.REFUSE_TEXT
    assert answer_mod.REFUSE_TEXT not in result.answer
    assert result.kind != "refuse"
    assert result.answer_source != "empty_scope"


def test_clarify_mapping_via_source_map():
    """澄清三字段经 ``source_map`` 收口（§3.8 第 4 行唯一映射点）。"""
    kind, answer_source, refs = source_map.map_clarify_round()
    assert (kind, answer_source, refs) == ("clarify", "clarify", [])


def test_clarify_is_not_direct_or_normal():
    """澄清既不是 direct 也不是 normal（语义互斥）。"""
    kind, answer_source, _ = source_map.map_clarify_round()
    assert kind != "direct" and kind != "normal"
    assert answer_source != "general" and answer_source != "kb"
    assert source_map.refs_empty_for(kind) is True


# --------------------------------------------------------------------------
# 追问提示词与历史
# --------------------------------------------------------------------------


def test_clarify_system_prompt_asks_for_one_question():
    """追问提示词要求：只输出一句、不要套话、不要引号前缀。"""
    prompt = clarify_mod.CLARIFY_SYSTEM
    assert "追问一句" in prompt
    assert "只输出这一句中文追问" in prompt
    assert "不要解释" in prompt


def test_recent_history_formatting():
    """历史拼装只取 user/assistant 且有正文的最近若干条。"""
    history = [
        {"role": "user", "content": "问一"},
        {"role": "assistant", "content": "答一"},
        {"role": "system", "content": "系统不该出现"},
        {"role": "assistant", "content": ""},
    ]
    text = clarify_mod._recent_history(history)
    assert text == "用户：问一\n知澜：答一"
    assert "系统不该出现" not in text
