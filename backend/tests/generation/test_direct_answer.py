"""条目 1.3.2 单测：无资料直答分支。

三条硬口径：

1. **文案冻结**：四段系统提示与老基线（只读参照）逐字一致——本文件
   **故意独立再抄一份**做对拍，抄错一个字就该红；
2. **直答与拒答不混用**：直答轮不出现「资料中没有提到」，拒答轮不出现模型调用；
3. **开关**：`CHAT_DIRECT_ANSWER_ENABLE=false` → 库内未命中走拒答（不直答），
   且提示词逐字回到旧文案。

LLM 调用全部用 **CubeLoop FauxProvider** 驱动真链路（统筹 §3.4）。
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.generation import answer as answer_mod
from app.generation import source_map

# --------------------------------------------------------------------------
# 冻结文案（故意独立抄一份：抄错一个字就该红）
# --------------------------------------------------------------------------

# 与老仓库 test_direct_answer_compat.py 的 FROZEN_* 逐字一致
FROZEN_KB = (
    "你是知澜，一个可以依据用户本地知识库、也可以用自己的知识回答问题的助手。"
    "下面是本地知识库检索到的资料，都带编号：回答中凡是来自这些资料的内容，要在句末用它的编号标注来源（如 [1] [2]）；"
    "编号只能取资料自带的编号。"
    "资料与问题无关或不足以回答时，就直接用你自己的知识回答，并在回答里说明这部分的依据来自你自身的知识，"
    "不要编造资料里没有的内容。"
    "无论有没有资料，都要正常回答用户的问题。回答用中文，条理清晰。"
)
FROZEN_NO_MATERIAL = (
    "本次没有检索到可用的本地资料。请直接用你自己的知识回答用户的问题；"
    "不要说「资料中没有提到」，也不要假装引用了资料。回答用中文，条理清晰。"
)
FROZEN_LEGACY = (
    "你是知澜，一个基于用户本地知识库回答问题的助手。"
    "只依据下面提供的资料回答；资料不足时明确说「资料中没有提到」，不要编造。"
    "下面的资料都带编号：回答中凡是来自资料的内容，要在句末用它的编号标注来源（如 [1] [2]）；"
    "编号只能取资料自带的编号，确实没用到资料时才不标注。"
    "回答用中文，条理清晰。"
)
FROZEN_VISION = (
    "你是知澜，一个能看图的助手。用户随消息上传了一张图片，请优先依据图片内容回答；"
    "如果下面还提供了知识库资料，可以结合资料补充说明，但资料与图片无关时以图片为准。"
    "提供的资料都带编号：回答中凡是来自资料的内容，要在句末用它的编号标注来源（如 [1] [2]）；"
    "编号只能取资料自带的编号，确实没用到资料时才不标注。"
    "回答用中文，条理清晰。"
)


def faux_model(reply: str):
    """FauxProvider 驱动的真链路模型（只把上游换成假响应）。"""
    from cubeloop.providers.faux import FauxProvider, faux_assistant_message

    provider = FauxProvider(provider_id="faux")
    provider.set_responses([faux_assistant_message(reply)])
    return provider.model("faux-chat")


@pytest.fixture
def direct_on(monkeypatch):
    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", True)


@pytest.fixture
def direct_off(monkeypatch):
    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", False)


# --------------------------------------------------------------------------
# 1. 文案冻结
# --------------------------------------------------------------------------


def test_prompts_frozen(direct_on):
    """四段提示与老基线逐字一致（视觉提示本期未动）。"""
    assert answer_mod.SYSTEM_PROMPT == FROZEN_KB
    assert answer_mod.NO_MATERIAL_SYSTEM_PROMPT == FROZEN_NO_MATERIAL
    assert answer_mod.LEGACY_SYSTEM_PROMPT == FROZEN_LEGACY
    assert answer_mod.VISION_SYSTEM_PROMPT == FROZEN_VISION
    # 编号标注指令逐字保留（阶段 3 的 [n] 引用依赖它）
    assert "要在句末用它的编号标注来源（如 [1] [2]）；编号只能取资料自带的编号。" in answer_mod.SYSTEM_PROMPT


def test_system_prompt_three_states(direct_on):
    """开关打开：有资料 → KB 提示；无资料 → NO_MATERIAL 提示。"""
    assert answer_mod.system_prompt(True) == FROZEN_KB + "\n\n" + answer_mod.FACTUAL_ANSWER_CONSTRAINTS
    assert answer_mod.system_prompt(False) == FROZEN_NO_MATERIAL + "\n\n" + answer_mod.FACTUAL_ANSWER_CONSTRAINTS


def test_switch_off_is_legacy_verbatim(direct_off):
    """S5-R1：关闭直答仍选择旧基础文案，共享事实约束同样送达。"""
    assert answer_mod.system_prompt(True) == FROZEN_LEGACY + "\n\n" + answer_mod.FACTUAL_ANSWER_CONSTRAINTS
    assert answer_mod.system_prompt(False) == FROZEN_LEGACY + "\n\n" + answer_mod.FACTUAL_ANSWER_CONSTRAINTS


def test_refuse_text_is_not_direct_text():
    """拒答文案与直答文案**严格分开**（条目 1.3 规格 + 坑清单）。"""
    assert answer_mod.REFUSE_TEXT == "资料中没有提到。"
    assert answer_mod.REFUSE_TEXT not in answer_mod.NO_MATERIAL_SYSTEM_PROMPT
    assert "不要说「资料中没有提到」" in answer_mod.NO_MATERIAL_SYSTEM_PROMPT


def test_assume_prefix_registered():
    """ASSUME_PREFIX 文案登记在此（1.4 复用，避免两处各写一份）。"""
    assert answer_mod.ASSUME_PREFIX.startswith("下面按目前已有信息作答")
    assert answer_mod.assume_prefix(False) == ""
    assert answer_mod.assume_prefix(True) == f"{answer_mod.ASSUME_PREFIX}\n\n"


# --------------------------------------------------------------------------
# 2. has_materials 分界
# --------------------------------------------------------------------------


def test_has_materials_prefers_snapshot():
    """快照优先：给了空快照就是「本次没有资料」，哪怕 chunks 里还有东西。"""
    assert answer_mod.has_materials([{"x": 1}], []) is False
    assert answer_mod.has_materials([], None) is False
    assert answer_mod.has_materials([{"x": 1}], None) is True
    assert answer_mod.has_materials([], [{"ordinal": 1}]) is True


# --------------------------------------------------------------------------
# 3. user 消息措辞（无资料分支）
# --------------------------------------------------------------------------


def test_build_messages_no_material_wording(direct_on):
    """开关打开：无资料时 user 消息不出现「资料」措辞（避免拉回拒答口径）。"""
    built = answer_mod.build_messages("Python 的 GIL 是什么？", [], has_material=False)
    assert built == [{"role": "user", "content": "问题：Python 的 GIL 是什么？"}]
    assert "资料" not in built[0]["content"]


def test_build_messages_legacy_wording(direct_off):
    """开关关闭：逐字回到旧写法（含「（知识库中没有检索到相关资料。）」）。"""
    built = answer_mod.build_messages("Python 的 GIL 是什么？", [], has_material=False)
    assert built == [
        {
            "role": "user",
            "content": "问题：Python 的 GIL 是什么？\n\n（知识库中没有检索到相关资料。）",
        }
    ]


def test_prompt_has_material_uses_rendered_text():
    """提示词侧「有没有资料」以**渲染出来的正文**为准（回归：自查发现的矛盾分支）。"""
    assert answer_mod.prompt_has_material("资料正文") is True
    assert answer_mod.prompt_has_material("") is False
    assert answer_mod.prompt_has_material("   \n  ") is False
    assert answer_mod.prompt_has_material(None) is False


def test_no_material_flag_without_text_does_not_invite_fake_citations(direct_on):
    """``has_material=True`` 但没给正文时，**不得**渲染出「资料如下」分支。

    这是自查时发现的真实缺陷：旧写法只看 ``has_material and materials_text``，
    若标志为真而正文为空，系统提示会说「下面是本地知识库检索到的资料」，而消息里
    根本没有资料——等于邀请模型编造引用（I7 的反面）。现在两侧都以渲染结果为准。
    """
    built = answer_mod.build_messages("问题", [], has_material=True, materials_text="")
    assert built == [{"role": "user", "content": "问题：问题"}]
    assert "资料如下" not in built[0]["content"]


@pytest.mark.asyncio
async def test_system_prompt_matches_rendered_materials(direct_on):
    """端到端一致：走真链路时，系统提示与消息里的资料区必须同真同假。"""
    captured = {}

    def factory(messages, model, system_prompt, tools):
        from cubeloop.providers.faux import faux_assistant_message

        captured["system"] = system_prompt
        captured["user"] = "".join(
            getattr(c, "text", "") for c in messages[-1].content
        )
        return faux_assistant_message("答")

    from cubeloop.providers.faux import FauxProvider

    provider = FauxProvider(provider_id="faux")
    provider.set_responses([factory])

    # 标志为真但正文为空 → 两侧都必须按「无资料」渲染
    await answer_mod.answer_direct(
        "问题",
        model=provider.model("faux-chat"),
        source_type="internal_db",
        has_material=True,
        materials_text="",
    )
    assert captured["system"] == FROZEN_NO_MATERIAL + "\n\n" + answer_mod.FACTUAL_ANSWER_CONSTRAINTS
    assert "资料如下" not in captured["user"]


def test_build_messages_keeps_history_roles_only():
    """历史只留 role/content（思考内容不回灌，坑清单）；本轮问题带「问题：」前缀。

    历史条**原样**带出（不加前缀），只有本轮提问套「问题：…」——与老基线一致。
    """
    built = answer_mod.build_messages(
        "追问",
        [
            {"role": "user", "content": "问题一"},
            {"role": "assistant", "content": "答案一", "reasoning": "思考不该进历史"},
            {"role": "system", "content": "系统提示不该进历史"},
            {"role": "assistant", "content": ""},
        ],
        has_material=False,
    )
    assert built == [
        {"role": "user", "content": "问题一"},
        {"role": "assistant", "content": "答案一"},
        {"role": "user", "content": "问题：追问"},
    ]
    assert all(set(item) == {"role", "content"} for item in built)


# --------------------------------------------------------------------------
# 4. 直答真链路（FauxProvider）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_question_gets_direct_answer(direct_on):
    """库外问题 → direct / general / refs 空，正文来自模型。"""
    result = await answer_mod.answer_direct(
        "今天天气怎么样",
        model=faux_model("今天晴，气温 20 度。"),
        source_type="external",
    )

    assert result.kind == "direct"
    assert result.answer_source == "general"
    assert result.refs == []
    assert result.answer == "今天晴，气温 20 度。"
    assert result.is_direct is True
    assert result.is_refuse is False


@pytest.mark.asyncio
async def test_direct_answer_never_uses_refuse_wording(direct_on):
    """直答轮正文不得是拒答文案（两者不混用）。"""
    result = await answer_mod.answer_direct(
        "今天天气怎么样", model=faux_model("晴。"), source_type="external"
    )
    assert result.answer != answer_mod.REFUSE_TEXT


@pytest.mark.asyncio
async def test_internal_miss_with_direct_enabled_answers(direct_on):
    """库内未命中 + 开关打开 → 直答（条目 1.6 第 2 条定死的一种策略）。"""
    result = await answer_mod.answer_direct(
        "知识库里关于项目周报的内容",
        model=faux_model("根据我自己的知识……"),
        source_type="internal_db",
        has_material=False,
    )
    assert result.kind == "direct"
    assert result.answer_source == "general"
    assert result.refs == []


@pytest.mark.asyncio
async def test_internal_miss_with_direct_disabled_refuses(direct_off):
    """库内未命中 + 开关关闭 → 拒答（**不调用模型**）。"""
    result = await answer_mod.answer_direct(
        "知识库里关于项目周报的内容",
        model=faux_model("这段不该被用到"),
        source_type="internal_db",
        has_material=False,
    )
    assert result.kind == "refuse"
    assert result.answer_source == "empty_scope"
    assert result.refs == []
    assert result.answer == answer_mod.REFUSE_TEXT
    assert result.is_refuse is True


@pytest.mark.asyncio
async def test_refuse_keeps_scope_hint_when_present(direct_off):
    """范围内为空时用更具体的那条提示（老基线 refuse_text 口径）。"""
    result = await answer_mod.answer_direct(
        "知识库里有什么",
        source_type="internal_db",
        has_material=False,
        direct_enabled=False,
    )
    assert result.answer == answer_mod.REFUSE_TEXT

    assert answer_mod.refuse_text([f"指定{answer_mod.EMPTY_SCOPE_MARK}"]) == (
        f"指定{answer_mod.EMPTY_SCOPE_MARK}"
    )


@pytest.mark.asyncio
async def test_model_failure_degrades_without_raising(direct_on):
    """模型报错 → 可读提示，**不抛异常**（I5 绝不炸穿），kind 仍按分路定。"""
    from cubeloop.providers.faux import FauxProvider

    provider = FauxProvider(provider_id="faux")  # 无排队响应 → error 路径
    result = await answer_mod.answer_direct(
        "今天天气怎么样", model=provider.model("faux-chat"), source_type="external"
    )

    assert result.kind == "direct"
    assert result.answer_source == "general"
    assert "失败" in result.answer


@pytest.mark.asyncio
async def test_unconfigured_model_returns_readable_hint(monkeypatch, direct_on):
    """未配置模型 → 可读提示（**不是**拒答文案，两者语义不同档）。"""
    monkeypatch.setattr(answer_mod, "build_chat_model", lambda: None)

    result = await answer_mod.answer_direct("今天天气怎么样", source_type="external")

    assert result.answer == answer_mod.NOT_CONFIGURED_HINT
    assert result.answer != answer_mod.REFUSE_TEXT
    assert result.kind == "direct", "未配置不是拒答：kind 仍由分路结果决定"


@pytest.mark.asyncio
async def test_direct_enabled_override(direct_off):
    """``direct_enabled`` 参数可覆盖开关（单测注入用）。"""
    result = await answer_mod.answer_direct(
        "问题",
        model=faux_model("答"),
        source_type="internal_db",
        has_material=False,
        direct_enabled=True,
    )
    assert result.kind == "direct"


@pytest.mark.asyncio
async def test_result_kind_matches_source_map(direct_on):
    """生成结果的三字段与 source_map 的判定一致（同一真相源）。"""
    result = await answer_mod.answer_direct(
        "问题", model=faux_model("答"), source_type="external"
    )
    expected = source_map.map_round("external", has_materials=False, direct_enabled=True)
    assert (result.kind, result.answer_source, result.refs) == expected
