"""条目 1.2.4 单测：规则命中 / 未命中 / LLM 失败降级。

覆盖阶段 1 设计说明 条目 1.2 的四点规格 + 1.2.4 的三条路径：
- 1.2.1 正则优先：「查一下/知识库/文档里有没有/库里」等 → internal_db；
- 1.2.2 LLM 兜底：无 Key 或失败 → 按 ROUTING_LLM_FALLBACK（默认 direct），**禁止 500**；
- 1.2.3 ROUTING_MODE=regex|llm|off（本阶段 off 视为库外）；
- 1.2.4 决策日志可查询（I9）：来源 regex/llm/fallback + source_type。

LLM 路径全部用 **CubeLoop FauxProvider** 驱动真链路（不连真模型，统筹 §3.4）。
"""

from __future__ import annotations

import pytest

from app.routing import llm as llm_mod
from app.routing import router as router_mod
from app.routing import rules
from app.routing.decision import RouteDecision


# --------------------------------------------------------------------------
# 测试替身：一个可编程的分类器（守约的实现）
# --------------------------------------------------------------------------


class StubClassifier:
    """按预设返回值/异常作答的分类器替身。"""

    def __init__(self, label: str | None = None, exc: Exception | None = None) -> None:
        self.label = label
        self.exc = exc
        self.calls: list[str] = []

    async def classify(self, text: str) -> str | None:
        self.calls.append(text)
        if self.exc is not None:
            raise self.exc
        return self.label


def faux_classifier(label: str):
    """用 CubeLoop FauxProvider 造一个**真链路**分类器（只把上游换成假响应）。"""
    from cubeloop.providers.faux import FauxProvider, faux_assistant_message

    provider = FauxProvider(provider_id="faux")
    provider.set_responses([faux_assistant_message(label)])
    return llm_mod.LlmClassifier(provider.model("faux-router"))


# --------------------------------------------------------------------------
# 1.2.1 正则命中 → internal_db
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected_rule",
    [
        ("知识库里有没有项目周报", "kb_named"),
        ("库里关于项目周报的内容", "kb_scope"),
        ("文档里有没有提到预算", "doc_scope"),
        ("帮我查一下项目周报", "retrieval_verb"),
        ("我上传的资料里怎么说", "my_library"),
        ("这个结论的出处是什么", "citation_ask"),
    ],
)
@pytest.mark.asyncio
async def test_regex_hit_goes_internal(text, expected_rule):
    """正则命中 → internal_db，且记录命中规则名（I9 可追溯）。"""
    decision = await router_mod.route(text, mode="regex", classifier=StubClassifier("external"))

    assert decision.source_type == "internal_db"
    assert decision.source == "regex"
    assert decision.matched_rule == expected_rule
    assert decision.is_internal is True


@pytest.mark.asyncio
async def test_regex_hit_does_not_call_llm():
    """正则优先：命中就不该再花一次 LLM 调用。"""
    clf = StubClassifier("external")
    await router_mod.route("知识库里有什么", mode="regex", classifier=clf)

    assert clf.calls == []


@pytest.mark.asyncio
async def test_database_not_confused_with_knowledge_base():
    """防呆：「数据库里」不该被 `库里` 规则吃掉。"""
    assert rules.match_rule("数据库里怎么加索引") is None


def test_rules_are_named_and_stable():
    """规则集自检：名字唯一、都有描述（决策日志与调参依赖它）。"""
    assert len(rules.RULE_NAMES) == len(set(rules.RULE_NAMES))
    assert all(rule.description for rule in rules.ALL_RULES)


# --------------------------------------------------------------------------
# 1.2.2 LLM 兜底：未命中时问模型
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regex_miss_falls_back_to_llm():
    """未命中 → 问 LLM；LLM 说库内就库内（source="llm"）。"""
    decision = await router_mod.route(
        "帮我写一首诗", mode="regex", classifier=StubClassifier("internal_db")
    )

    assert decision.source_type == "internal_db"
    assert decision.source == "llm"
    assert decision.matched_rule is None


@pytest.mark.asyncio
async def test_llm_mode_skips_regex_entirely():
    """ROUTING_MODE=llm：不走正则，直接问模型。"""
    clf = StubClassifier("external")
    decision = await router_mod.route("知识库里有什么", mode="llm", classifier=clf)

    assert clf.calls == ["知识库里有什么"]
    assert decision.source == "llm"
    assert decision.source_type == "external"


@pytest.mark.asyncio
async def test_llm_says_external():
    """LLM 说库外 → external（默认 fallback=direct 时的一致结果）。"""
    decision = await router_mod.route(
        "今天天气怎么样", mode="regex", classifier=StubClassifier("external")
    )

    assert decision.source_type == "external"
    assert decision.source == "llm"


@pytest.mark.asyncio
async def test_llm_real_chain_via_faux_provider():
    """FauxProvider 驱动**真** LlmClassifier：上游假、链路真（1.2.2 验收）。"""
    decision = await router_mod.route(
        "知识库里有没有周报", mode="llm", classifier=faux_classifier("internal_db")
    )
    assert decision.source_type == "internal_db"
    assert decision.source == "llm"

    decision2 = await router_mod.route(
        "随便聊聊", mode="llm", classifier=faux_classifier("external")
    )
    assert decision2.source_type == "external"
    assert decision2.source == "llm"


# --------------------------------------------------------------------------
# 1.2.4 LLM 失败降级（禁止 500）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_direct_by_default():
    """LLM 抛异常 → 按 ROUTING_LLM_FALLBACK=direct 降级为 external，不抛。"""
    clf = StubClassifier(exc=RuntimeError("模型挂了"))
    decision = await router_mod.route("帮我写点东西", mode="regex", classifier=clf, fallback="direct")

    assert decision.source_type == "external"
    assert decision.source == "fallback"


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_search_when_configured():
    """ROUTING_LLM_FALLBACK=search → 降级为 internal_db（去检索）。"""
    clf = StubClassifier(exc=RuntimeError("模型挂了"))
    decision = await router_mod.route("帮我写点东西", mode="regex", classifier=clf, fallback="search")

    assert decision.source_type == "internal_db"
    assert decision.source == "fallback"


@pytest.mark.asyncio
async def test_no_classifier_degrades_without_raising():
    """无 Key / 未配置（装配返回 None）→ 降级，**不抛 500**。"""
    decision = await router_mod.route(
        "随便问问", mode="llm", classifier=NoClassifier(), fallback="direct"
    )

    assert decision.source_type == "external"
    assert decision.source == "fallback"


class NoClassifier:
    """模拟「未配置」：classify 返回 None（判不了）。"""

    async def classify(self, text: str) -> str | None:
        return None


@pytest.mark.asyncio
async def test_classifier_none_uses_build_classifier_and_degrades(monkeypatch):
    """``classifier`` 不传时走真装配器；没配模型 → 装配器返回 None → 降级不炸。"""
    monkeypatch.setattr(router_mod.llm_mod, "build_classifier", lambda: None)

    decision = await router_mod.route("随便问问", mode="llm", fallback="direct")

    assert decision.source_type == "external"
    assert decision.source == "fallback"


@pytest.mark.asyncio
async def test_unparseable_label_degrades():
    """模型回看不懂的内容 → 降级，而不是硬猜。"""
    decision = await router_mod.route(
        "嗯", mode="llm", classifier=StubClassifier("我觉得应该查一下吧"), fallback="direct"
    )

    assert decision.source == "fallback"
    assert decision.source_type == "external"


@pytest.mark.asyncio
async def test_build_classifier_returns_none_when_unconfigured(monkeypatch):
    """真装配器：没配模型 → 返回 None（调用方据此降级），**不抛异常**。"""
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod.settings, "MODEL_SERVICES", "")
    monkeypatch.setattr(config_mod.settings, "MODEL_ROLES", "")
    monkeypatch.setattr(config_mod.settings, "MODEL_PROVIDER", "")
    monkeypatch.setattr(config_mod.settings, "LLM_MODEL", "")
    monkeypatch.setattr(config_mod.settings, "LLM_BASE_URL", "")
    monkeypatch.setattr(config_mod.settings, "LLM_API_KEY", "")

    assert llm_mod.build_classifier() is None


@pytest.mark.asyncio
async def test_route_never_raises_even_if_classifier_is_broken():
    """分类器实现不守约（抛任意 ``Exception``）也不炸穿。

    注意边界：这里吸收的是 ``Exception`` 及其子类。``BaseException`` 直系
    （``KeyboardInterrupt`` / ``asyncio.CancelledError``）**故意不吞**——
    取消与中断必须能正常传播。
    """

    class Broken:
        async def classify(self, text: str) -> str | None:
            raise ValueError("实现不守约")

    decision = await router_mod.route("测试", mode="llm", classifier=Broken(), fallback="search")
    assert decision.source == "fallback"
    assert decision.source_type == "internal_db"


# --------------------------------------------------------------------------
# 1.2.3 ROUTING_MODE 三态 + 配置坏值
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mode_off_treats_as_external():
    """ROUTING_MODE=off：本阶段视为库外，且不调用 LLM。"""
    clf = StubClassifier("internal_db")
    decision = await router_mod.route("知识库里有什么", mode="off", classifier=clf)

    assert decision.source_type == "external"
    assert decision.source == "off"
    assert clf.calls == []


@pytest.mark.parametrize("bad", ["", "  ", "REGEX", "bogus", None])
def test_mode_resolution(bad):
    """ROUTING_MODE 归一：合法值原样（含大小写），坏值回落默认 regex。"""
    result = router_mod.resolve_mode(bad)
    assert result in router_mod.VALID_MODES
    if bad and bad.strip().lower() in router_mod.VALID_MODES:
        assert result == bad.strip().lower()
    else:
        assert result == router_mod.DEFAULT_MODE


@pytest.mark.parametrize("bad", ["", "bogus", None, "DIRECT"])
def test_fallback_resolution(bad):
    """ROUTING_LLM_FALLBACK 归一：坏值回落 direct。"""
    result = router_mod.resolve_fallback(bad)
    assert result in router_mod.VALID_FALLBACKS


@pytest.mark.asyncio
async def test_bad_mode_value_does_not_break_routing():
    """配置里写了坏值 → 仍能正常分路（不炸），按默认 regex。"""
    decision = await router_mod.route(
        "知识库里有什么", mode="不是合法值", classifier=StubClassifier("external")
    )
    assert decision.source_type == "internal_db"
    assert decision.source == "regex"


@pytest.mark.asyncio
async def test_empty_input_returns_safe_decision():
    """空输入不抛异常（1.5 会先按 400 拦掉，这里只保证不炸）。"""
    decision = await router_mod.route("   ", mode="regex", classifier=StubClassifier("external"))
    assert isinstance(decision, RouteDecision)
    assert decision.source_type in ("internal_db", "external")


# --------------------------------------------------------------------------
# parse_label 宽容解析（模型爱加标点）
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("internal_db", "internal_db"),
        ("  internal_db  ", "internal_db"),
        ("internal_db。", "internal_db"),
        ("'external'", "external"),
        ("EXTERNAL", "external"),
        ("kb", "internal_db"),
        ("库内", "internal_db"),
        ("我觉得是 internal_db", "internal_db"),
        ("", None),
        (None, None),
        ("不知道", None),
    ],
)
def test_parse_label(raw, expected):
    assert llm_mod.parse_label(raw) == expected


def test_decision_event_payload_roundtrip():
    """决策载荷可无损往返（阶段 5 回放依赖它）。"""
    decision = RouteDecision(
        source_type="internal_db", source="regex", reason="命中", matched_rule="kb_named"
    )
    data = decision.to_event_data()
    assert data == {
        "source_type": "internal_db",
        "source": "regex",
        "reason": "命中",
        "matched_rule": "kb_named",
    }
    assert RouteDecision.from_event_data(data) == decision
