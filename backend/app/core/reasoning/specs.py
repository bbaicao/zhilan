"""厂商思考参数**登记表**（阶段 5 后 · 推理等级回流；从老基线 `app/rag/reasoning.py` 抄表）。

原则（2026-09-12 用户口径，老基线 module docstring 逐字沿用）：

- **能调什么、取值叫什么，全部由该型号的厂商规定**，不发明档位、不做自己的分级；
- 表里**只登记常用常见的型号**；没登记的型号不给选项，改为「自己到 `.env` 写
  `CHAT_REASONING`，原样透传给厂商」的配置通道（见 `store.request_body_for`）；
- 每条登记都挂着厂商官方文档链接，界面直接展示，方便核对。

出问题时的排查顺序：先看 ``source`` 里的文档是否已更新（厂商改了取值），再看
``template`` 是否还和文档里的请求体示例一致。

## 本轮（i.e. 首次回流）的范围

- **只搬国内五家的 7 条**（MiMo / 千问两档 / GLM / Kimi 两档 / DeepSeek）；
- **不登记 OpenAI**（未核实逐型号档位 ⇒ 走 `manual` 手写通道，见 §A6）；
- **不登记 Claude / Gemini** ⇒ 查表 `None`，一样走手写通道。

「不登记」**不是**「不能调」：`store.request_body_for` 对未登记型号会把 `.env` 里手写的
`CHAT_REASONING` **原样透传**。本表只是「界面给不出选项」的那部分。

## 与老基线的两处裁剪（有意，已登记）

1. **丢掉 `deepen` 字段**（老基线条目 19「复杂问题时自动加深」的档位）——本轮**不做自动加深**
   （业主口径 ④），保留一个没有读者的字段只会腐烂；
2. **丢掉 Gemini（4 条）/ Claude（2 条）/ OpenAI（1 条）**，见上文。

`coerce_value` 的 ``kind="int"`` 分支**保留**：它是表的**数据形状**（老基线 Gemini / Claude
两族用到），删掉会让「将来补登记」时静默降级成「一律非法」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 请求体模板里的占位符（`_fill` 只替换与它**完全相等**的字符串）
VALUE_PLACEHOLDER = "$value"


@dataclass(frozen=True)
class ReasoningSpec:
    """一个型号（或一族型号）的官方思考参数。"""

    vendor: str  # 厂商显示名，如「小米 MiMo」
    providers: tuple[str, ...]  # config.PROVIDER_PRESETS 里的键
    models: tuple[str, ...]  # 型号匹配（子串命中）；空元组表示该厂商全部型号
    param: str  # 厂商参数名（界面展示，同时是取值归属的校验键）
    kind: str  # enum | bool | int
    values: tuple[Any, ...] = ()  # enum / bool 的原样取值
    labels: tuple[str, ...] = ()  # 与 values 一一对应的中文注解
    exclude: tuple[str, ...] = ()  # 命中这些子串的型号不算（如 -tts / -asr）
    minimum: int | None = None
    maximum: int | None = None
    default: Any = None  # 厂商没收到该参数时的默认
    default_label: str = ""
    template: dict = field(default_factory=dict)  # 请求体模板，$value 占位
    note: str = ""
    source: str = ""

    def label_of(self, value: Any) -> str:
        for index, item in enumerate(self.values):
            if item == value and index < len(self.labels):
                return self.labels[index]
        return ""

    def allows(self, value: Any) -> bool:
        return coerce_value(self, value) is not None


# ---------------------------------------------------------------------------
# 规格表：全部来自厂商官方文档（改动前请先核对 source）
# ---------------------------------------------------------------------------


SPECS: tuple[ReasoningSpec, ...] = (
    ReasoningSpec(
        vendor="小米 MiMo",
        providers=("mimo", "xiaomi"),
        models=("mimo-v2.5", "mimo-v2.6-flash", "mimo-v2.6-pro"),
        exclude=("-asr", "-tts"),
        param="thinking.type",
        kind="enum",
        values=("enabled", "disabled"),
        labels=("开启思考（厂商默认）", "关闭思考，直接作答"),
        default="enabled",
        default_label="enabled（MiMo v2.5 / v2.6 系列默认开启思考）",
        template={"thinking": {"type": VALUE_PLACEHOLDER}},
        note=(
            "思考模式下 MiMo v2.5 / v2.6 系列的 temperature、top_p 由模型强制取默认值"
            "（1.0 / 0.95）；思考内容在响应的 reasoning_content 字段里，不计入正文。"
            "若要用 low / medium / high 分档，那是 MiMo Responses API 的 reasoning.effort，"
            "与这里的 Chat Completions 不是同一个参数。"
        ),
        source="https://mimo.mi.com/static/docs/api/chat/openai-api.md",
    ),
    ReasoningSpec(
        vendor="阿里云百炼 · 通义千问",
        providers=("dashscope",),
        models=("qwen-plus", "qwen-flash", "qwen-turbo", "qwen3-max"),
        param="enable_thinking",
        kind="bool",
        values=(True, False),
        labels=("先思考再回复", "直接回复（关闭思考）"),
        default=False,
        default_label="false（几款混合思考模型默认不开启思考）",
        template={"enable_thinking": VALUE_PLACEHOLDER},
        note=(
            "enable_thinking 不是 OpenAI 标准字段，我们按厂商要求通过 extra_body 发到请求体顶层；"
            "思考内容在 reasoning_content 里，思考 token 计入输出计费。"
            "部分开源版型号只支持流式，非流式会报错。"
        ),
        source="https://www.alibabacloud.com/help/zh/model-studio/deep-thinking",
    ),
    ReasoningSpec(
        vendor="阿里云百炼 · 通义千问",
        providers=("dashscope",),
        models=("qwen3",),
        param="enable_thinking",
        kind="bool",
        values=(True, False),
        labels=("先思考再回复（本系列默认开启）", "直接回复（关闭思考）"),
        default=True,
        default_label="true（Qwen3.5 / 3.6 / 3.7 / 3.8 等混合思考模型默认开启）",
        template={"enable_thinking": VALUE_PLACEHOLDER},
        note=(
            "本系列默认开启思考，可显式关掉。enable_thinking 走 extra_body 发到请求体顶层；"
            "思考内容在 reasoning_content 里。"
        ),
        source="https://www.alibabacloud.com/help/zh/model-studio/deep-thinking",
    ),
    ReasoningSpec(
        vendor="智谱 GLM",
        providers=("zhipu", "glm"),
        models=("glm-4.5", "glm-4.6", "glm-4.7", "glm-5"),
        exclude=("glm-5.3",),
        param="thinking.type",
        kind="enum",
        values=("enabled", "disabled"),
        labels=("开启思维链（厂商默认）", "禁用思考，直接回答"),
        default="enabled",
        default_label="enabled（OpenAPI 默认值）",
        template={"thinking": {"type": VALUE_PLACEHOLDER}},
        note=(
            "GLM-5.3 / 5.3-FLASH 起不再支持关闭（传 disabled 会报错），所以这两款不在这里给选项；"
            "GLM-4.5 / 4.6 是「模型自动判断是否思考」，GLM-4.7 是强制思考。"
            "思考内容在 reasoning_content 里；工具调用时要保留并回传它。"
        ),
        source="https://docs.bigmodel.cn/cn/guide/capabilities/thinking",
    ),
    ReasoningSpec(
        vendor="月之暗面 Kimi",
        providers=("kimi", "moonshot"),
        models=("kimi-k3",),
        param="reasoning_effort",
        kind="enum",
        values=("low", "high", "max"),
        labels=("低推理强度", "高推理强度", "最高推理强度（厂商默认）"),
        default="max",
        default_label="max",
        template={"reasoning_effort": VALUE_PLACEHOLDER},
        note=(
            "kimi-k3 不接受 thinking 参数（传了会报错），只能用 reasoning_effort；"
            "切换档位会破坏前缀缓存命中，官方建议在会话开始前定档。"
        ),
        source="https://platform.kimi.com/docs/guide/use-reasoning-effort",
    ),
    ReasoningSpec(
        vendor="月之暗面 Kimi",
        providers=("kimi", "moonshot"),
        models=("kimi-k2.6",),
        param="thinking.type",
        kind="enum",
        values=("enabled", "disabled"),
        labels=("开启思考（厂商默认）", "关闭思考"),
        default="enabled",
        default_label="enabled",
        template={"thinking": {"type": VALUE_PLACEHOLDER}},
        note=(
            "kimi-k2.6 不接受 reasoning_effort（那是 kimi-k3 的参数，传了会报错）；"
            "思考模型的 temperature 不可修改，不要显式传。"
        ),
        source="https://platform.kimi.com/docs/guide/use-thinking-models",
    ),
    ReasoningSpec(
        vendor="DeepSeek",
        providers=("deepseek",),
        models=("deepseek-flash", "deepseek-v4-pro"),
        param="thinking.type",
        kind="enum",
        values=("enabled", "disabled"),
        labels=("开启思考（厂商默认）", "关闭思考，走非思考模式"),
        default="enabled",
        default_label="enabled（默认强度 effort=high）",
        template={"thinking": {"type": VALUE_PLACEHOLDER}},
        note=(
            "这两款是混合模式，可以显式关掉；思考模式下 temperature、presence_penalty、"
            "frequency_penalty 不生效，top_p 下限被抬到 0.95。"
            "旧的 deepseek-chat / deepseek-reasoner 已于 2026-07-24 下线，不再登记。"
        ),
        source="https://api-docs.deepseek.com/guides/thinking_mode",
    ),
)


# ---------------------------------------------------------------------------
# 查表
# ---------------------------------------------------------------------------


def spec_for(provider: str, model: str) -> ReasoningSpec | None:
    """按「厂商 + 型号」找官方规格；没登记就返回 ``None``（**不猜**）。"""
    provider = (provider or "").strip().lower()
    model = (model or "").strip().lower()
    if not provider or not model:
        return None
    for spec in SPECS:
        if provider not in spec.providers:
            continue
        if any(block and block in model for block in spec.exclude):
            continue
        if not spec.models or any(pattern in model for pattern in spec.models):
            return spec
    return None


# ---------------------------------------------------------------------------
# 取值校验
# ---------------------------------------------------------------------------


def coerce_value(spec: ReasoningSpec, value: Any) -> Any | None:
    """把外部传进来的值收成该规格允许的取值；不合法返回 ``None``（由调用方报错）。"""
    if spec.kind == "enum":
        for item in spec.values:
            if item == value:
                return item
        return None
    if spec.kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        return None
    if spec.kind == "int":
        if isinstance(value, bool):
            return None
        number = value
        if isinstance(value, str):
            try:
                number = int(value.strip())
            except ValueError:
                return None
        if not isinstance(number, int):
            return None
        if spec.minimum is not None and number < spec.minimum:
            return None
        if spec.maximum is not None and number > spec.maximum:
            return None
        return number
    return None


def display_value(value: Any) -> str:
    """界面上按厂商的写法显示：布尔就是 JSON 的 true / false，其余原样。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def option_values(spec: ReasoningSpec) -> tuple[Any, ...]:
    """选项清单的取值序列（bool 型且没显式列值时就是 ``(True, False)``）。"""
    if spec.kind == "bool" and not spec.values:
        return (True, False)
    return spec.values


__all__ = [
    "SPECS",
    "VALUE_PLACEHOLDER",
    "ReasoningSpec",
    "coerce_value",
    "display_value",
    "option_values",
    "spec_for",
]
