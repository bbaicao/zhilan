"""推理等级登记表的**纯函数**面（A1–A7；开工包 §5）。

**为什么单列一个文件**：这一层（`app/core/reasoning/`）是纯函数——不读 `settings`、
不碰磁盘、不装配模型。把它与 HTTP 面（`tests/api/test_settings_reasoning.py`）和
装配面（`tests/test_reasoning_isolation.py`）分开，是为了让「表抄错了」与
「接线接错了」两种失败一眼可分。

**A1–A7 覆盖的正是「表」本身**：逐厂商、逐型号、含 `exclude` 与「未登记 ⇒ None」两类。
"""

from __future__ import annotations

import pytest

from app.core.reasoning import (
    SPECS,
    coerce_value,
    dump_stored,
    is_stale,
    manual_applies,
    nest,
    parse_stored,
    request_body,
    request_body_for,
    spec_for,
    stored_value,
)

MIMO = "mimo"
DASHSCOPE = "dashscope"
ZHIPU = "zhipu"
KIMI = "kimi"
DEEPSEEK = "deepseek"


# --- A1：MiMo（含 exclude）-----------------------------------------------------------------


def test_a1_mimo_thinking_type_and_excludes():
    for model in (
        "mimo-v2.5", "mimo-v2.5-pro", "mimo-v2.6-flash",
        "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed",
    ):
        spec = spec_for(MIMO, model)
        assert spec is not None, model
        assert spec.param == "thinking.type"
        assert spec.kind == "enum"
        assert spec.values == ("enabled", "disabled")
        assert spec.default == "enabled"
        for value in ("enabled", "disabled"):
            assert request_body_for(MIMO, model, dump_stored(spec.param, value, model)) == {
                "thinking": {"type": value}
            }
        assert request_body_for(MIMO, model, dump_stored(spec.param, "high", model)) is None
    # exclude：-asr / -tts 不登记（否则会给语音型号发它不认识的思考参数）
    assert spec_for(MIMO, "mimo-v2.5-asr") is None
    assert spec_for(MIMO, "mimo-v2.5-tts") is None
    # 厂商别名走同一条规格
    assert spec_for("xiaomi", "mimo-v2.5") is spec_for(MIMO, "mimo-v2.5")


# --- A2：千问两档（bool 型，默认值相反）-------------------------------------------------------


def test_a2_qwen_bool_param_two_defaults():
    for model in ("qwen-plus", "qwen-flash", "qwen3-max"):
        spec = spec_for(DASHSCOPE, model)
        assert spec is not None, model
        assert spec.param == "enable_thinking"
        assert spec.kind == "bool"
        assert spec.default is False, model
    series = spec_for(DASHSCOPE, "qwen3.7-max")
    assert series is not None
    assert series.param == "enable_thinking"
    assert series.default is True, "qwen3 系列默认开启思考（与上一条同参数、不同默认）"
    assert spec_for(DASHSCOPE, "qwen-turbo") is not None, "表里登记了这一款"


# --- A3：GLM（含 exclude glm-5.3）----------------------------------------------------------


def test_a3_glm_thinking_type_and_53_excluded():
    spec = spec_for(ZHIPU, "glm-4.6")
    assert spec is not None
    assert spec.param == "thinking.type"
    assert spec.values == ("enabled", "disabled")
    assert spec_for("glm", "glm-4.6") is spec
    assert spec_for(ZHIPU, "glm-5.3") is None, "GLM-5.3 起不再支持关闭 ⇒ 不登记"


# --- A4：Kimi 两档（参数名不同，互不接受）------------------------------------------------------


def test_a4_kimi_effort_and_thinking_are_two_specs():
    k3 = spec_for(KIMI, "kimi-k3")
    assert k3 is not None
    assert k3.param == "reasoning_effort"
    assert k3.values == ("low", "high", "max")
    assert k3.default == "max"
    k26 = spec_for(KIMI, "kimi-k2.6")
    assert k26 is not None
    assert k26.param == "thinking.type"
    assert k26.values == ("enabled", "disabled")
    assert spec_for("moonshot", "kimi-k3") is k3


# --- A5：DeepSeek -----------------------------------------------------------------------


def test_a5_deepseek_registered_two_models_only():
    for model in ("deepseek-flash", "deepseek-v4-pro"):
        spec = spec_for(DEEPSEEK, model)
        assert spec is not None, model
        assert spec.param == "thinking.type"
    assert spec_for(DEEPSEEK, "deepseek-chat") is None, "旧型号已下线 ⇒ 不登记"


# --- A6：OpenAI 未登记，但手写值仍原样透传 ------------------------------------------------------


def test_a6_openai_not_registered_but_manual_passthrough():
    assert spec_for("openai", "gpt-5-mini") is None, "本轮明确不登记 OpenAI"

    # 手写值：**原样透传**（嵌套展开用的是 param 字面，不去校验厂商是否接受）
    raw = dump_stored("reasoning_effort", "high")
    assert request_body_for("openai", "gpt-5-mini", raw) == {"reasoning_effort": "high"}
    assert manual_applies(raw, "gpt-5-mini") is True

    # 写给别的型号的手写值**不发**（否则「给 MiMo 关思考」会被塞给 OpenAI）
    other = dump_stored("thinking.type", "disabled", "mimo-v2.5")
    assert request_body_for("openai", "gpt-5-mini", other) is None
    assert manual_applies(other, "gpt-5-mini") is False
    # 但写给 OpenAI 自己的会发
    assert request_body_for("openai", "gpt-5-mini", dump_stored("thinking.type", "disabled", "gpt-5-mini")) == {
        "thinking": {"type": "disabled"}
    }


# --- A7：其余一律 None（不猜）-------------------------------------------------------------


@pytest.mark.parametrize(
    "provider,model",
    [
        ("gemini", "gemini-3.8-flash"),
        ("google", "gemini-3-pro"),
        ("claude", "claude-opus-4-6"),
        ("anthropic", "claude-sonnet-5"),
        ("ollama", "qwen2.5"),
        ("", "mimo-v2.5"),
        ("mimo", ""),
        ("mimo", "   "),
        ("MIMO", None),
    ],
)
def test_a7_unknown_provider_or_model_is_none(provider, model):
    """查表一律 ``None``（**不猜**）。

    ⚠️ 这里只断 `spec_for`——`request_body_for` 对**未登记厂商**还有「手写原样透传」那条支路
    （`A6` 的语义），所以「查表 None」不等于「请求体一定空」。两者是两件事，
    本条钉前者，后者由 `A12`（真实请求体）负责。
    """
    assert spec_for(provider, model) is None


def test_a7b_empty_provider_or_model_never_yields_a_spec():
    """空 provider / 空 model ⇒ 查表恒 ``None``（**包括**别名大小写混写的情形）。"""
    for provider in ("", "   ", "mimo", "MIMO", "xiaomi"):
        assert spec_for(provider, "") is None
    assert spec_for("MIMO", "mimo-v2.5") is not None, "大小写不敏感（老基线口径）"


# --- 表的自洽性（不是开工包断言，但抄表时最常错）--------------------------------------------------


def test_table_shape_and_no_duplicate_params_per_vendor():
    assert len(SPECS) == 7, "本轮只搬国内五家 7 条"
    vendors = [row.vendor for row in SPECS]
    assert len(set(vendors)) == 5, "五家显示名（千问与 Kimi 各两档共用厂商名）"
    assert len({row.providers[0] for row in SPECS}) == 5, "五个主 provider 键"
    for spec in SPECS:
        assert spec.kind in ("enum", "bool", "int")
        if spec.kind in ("enum", "bool"):
            assert spec.values, spec.param
            assert len(spec.labels) == len(spec.values), spec.param
        assert spec.template, spec.param
        assert spec.source.startswith("https://"), f"{spec.param} 少了官方文档链接"
        # 模板与参数名必须自洽：`nest(param, value)` 与模板填出来的形状应能对上
        if "." in spec.param:
            head, tail = spec.param.split(".", 1)
            assert head in spec.template, spec.param
            assert isinstance(spec.template[head], dict) and tail in spec.template[head], spec.param
        else:
            assert spec.param in spec.template, spec.param
    # 同一厂商下不允许两条同 param 的规格同时命中同一个型号（spec_for 取首条，静默）
    for spec in SPECS:
        for other in SPECS:
            if other is spec or other.providers[0] != spec.providers[0]:
                continue
            hit = spec_for(spec.providers[0], spec.models[0])
            assert hit is spec, f"{spec.models[0]} 命中了 {hit and hit.param}，不是预期的那条"


# --- 存值解析 / 组装（A8–A11 的纯函数底座）----------------------------------------------------


def test_stored_roundtrip_and_bad_data_is_absent():
    raw = dump_stored("thinking.type", "disabled", "mimo-v2.5")
    assert raw == '{"param":"thinking.type","value":"disabled","model":"mimo-v2.5"}'
    assert parse_stored(raw) == ("thinking.type", "disabled", "mimo-v2.5")
    # 坏数据一律当「没设过」，绝不抛（`.env` 是人手改得动的文件）
    for bad in ("", "   ", "{", "[]", '{"param":"x"}', '{"value":1}', '{"param":"","value":1}', "null"):
        assert parse_stored(bad) is None, bad
        assert request_body_for("mimo", "mimo-v2.5", bad) is None


def test_nest_expands_dotted_param():
    assert nest("thinking.type", "disabled") == {"thinking": {"type": "disabled"}}
    assert nest("a.b.c", 1) == {"a": {"b": {"c": 1}}}
    assert nest("", 1) == {}


def test_request_body_fills_template_without_mutating_it():
    spec = spec_for(MIMO, "mimo-v2.5")
    assert request_body(spec, "disabled") == {"thinking": {"type": "disabled"}}
    assert spec.template == {"thinking": {"type": "$value"}}, "模板必须保持原样（是冻结的共享对象）"


def test_bool_kind_accepts_string_and_rejects_junk():
    spec = spec_for(DASHSCOPE, "qwen-plus")
    assert coerce_value(spec, True) is True
    assert coerce_value(spec, False) is False
    assert coerce_value(spec, "true") is True
    assert coerce_value(spec, " FALSE ") is False
    assert coerce_value(spec, "yes") is None
    assert coerce_value(spec, 1) is None, "1 不是 bool ⇒ 拒绝（不许把 int 当 bool）"
    assert coerce_value(spec, None) is None


def test_stored_value_scopes_to_current_model():
    spec = spec_for(MIMO, "mimo-v2.5")
    raw = dump_stored("thinking.type", "disabled", "mimo-v2.5")
    assert stored_value(spec, raw, "mimo-v2.5") == "disabled"
    assert stored_value(spec, raw, "mimo-v2.5-pro") is None, "写给别的型号的不生效"
    assert is_stale(spec, raw, "mimo-v2.5-pro") is True, "换了型号 ⇒ 界面要说「已不生效」"
    assert is_stale(spec, raw, "mimo-v2.5") is False
    # param 不符同样按「没设过」
    assert stored_value(spec, dump_stored("enable_thinking", True), "mimo-v2.5") is None
