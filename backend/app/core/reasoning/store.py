"""`CHAT_REASONING` 的**存储载体**与**请求体出口**（阶段 5 后 · 推理等级回流）。

`CHAT_REASONING` 是 `.env` 里的**单行 JSON**，形状与老基线逐字一致::

    {"param":"thinking.type","value":"enabled","model":"mimo-v2.5"}

三个键的分工（老基线同）：

- ``param``：厂商参数名。**同时是取值归属的校验键**——它与当前型号官方规格的
  ``spec.param`` 不一致就当没设过（否则「给 MiMo 关思考」会被塞给不认识它的厂商并报错）；
- ``value``：原样发给厂商的取值（**我们不发明档位**）；
- ``model``：**写给哪个型号的**。手改 `.env` 时**可以省略**，省略 = 「哪个型号都按这个发」。

坏数据（不是 JSON / 不是对象 / 没有 ``value`` / ``param`` 为空）一律当作「**没设过**」，
绝不抛——`.env` 是人手改得动的文件，读侧一切不抛是 I5 的明文要求。

## 一句话记住两条支路

:func:`request_body_for` 是**唯一**的对外出口，两条支路：

1. **型号已登记** ⇒ 只认该型号官方规格里的参数，用 ``spec.template`` 组装
   （`{"thinking": {"type": "$value"}}` → `{"thinking": {"type": "disabled"}}`）；
2. **型号未登记** ⇒ 把 `.env` 里手写的值**原样透传**（``nest(param, value)``），
   但**写给别的型号的值不发**（``manual_applies``）。

两条都拿不到 ⇒ 返回 ``None``，**调用方必须把它当作「与改动前逐字一致」**。
"""

from __future__ import annotations

import json
from typing import Any

from app.core.reasoning import specs as specs_mod
from app.core.reasoning.specs import VALUE_PLACEHOLDER, ReasoningSpec


def _fill(template: Any, value: Any) -> Any:
    """把模板里的 ``$value`` 占位换成真实取值（递归 dict / list）。"""
    if isinstance(template, str):
        return value if template == VALUE_PLACEHOLDER else template
    if isinstance(template, dict):
        return {key: _fill(item, value) for key, item in template.items()}
    if isinstance(template, list):
        return [_fill(item, value) for item in template]
    return template


def request_body(spec: ReasoningSpec, value: Any) -> dict:
    """按厂商文档给的形状组装请求体片段（走 OpenAI 兼容的 ``extra_body``）。"""
    filled = _fill(spec.template, value)
    return filled if isinstance(filled, dict) else {}


def nest(param: str, value: Any) -> dict:
    """把 ``a.b.c`` 形式的参数名展开成嵌套请求体：``{"a": {"b": {"c": value}}}``。"""
    keys = [item for item in str(param or "").split(".") if item]
    if not keys:
        return {}
    body: Any = value
    for key in reversed(keys):
        body = {key: body}
    return body


# ---------------------------------------------------------------------------
# 存值解析
# ---------------------------------------------------------------------------


def parse_stored(raw: str) -> tuple[str, Any, str] | None:
    """解析 `.env` 里的存值，返回 ``(参数名, 取值, 写给哪个型号)``；坏数据一律当「没设过」。"""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or "value" not in data:
        return None
    param = str(data.get("param") or "").strip()
    if not param:
        return None
    return param, data["value"], str(data.get("model") or "").strip().lower()


def dump_stored(param: str, value: Any, model: str = "") -> str:
    """组装 `.env` 里那一行（与老基线逐字同形：不带多余空格，中文不转义）。"""
    payload: dict[str, Any] = {"param": param, "value": value}
    if model:
        payload["model"] = model
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def stored_value(spec: ReasoningSpec, raw: str, model: str = "") -> Any | None:
    """存值是否属于当前型号的规格；不属于就当没设（**换型号后旧值不生效**）。"""
    parsed = parse_stored(raw)
    if parsed is None:
        return None
    param, value, written_for = parsed
    if param != spec.param:
        return None
    if written_for and model and written_for != model.strip().lower():
        return None
    return specs_mod.coerce_value(spec, value)


def is_stale(spec: ReasoningSpec | None, raw: str, model: str = "") -> bool:
    """存了值、但当前型号的官方规格用不上（换了型号或厂商改了参数）：界面要说明，不静默。

    未登记型号不走这里——那种情况的表达是 ``manual_active`` 与 ``note``（见 ``payload``）。
    """
    if spec is None:
        return False
    parsed = parse_stored(raw)
    if parsed is None:
        return False
    _param, _value, written_for = parsed
    if written_for and model and written_for != model.strip().lower():
        return True
    return stored_value(spec, raw, model) is None


def manual_applies(raw: str, model: str) -> bool:
    """未登记型号的手填值能不能用：写给别的型号的（界面保存时会带型号）就不发。

    手改 `.env` 时 ``model`` 可以省略，省略表示「哪个型号都按这个发」。
    """
    parsed = parse_stored(raw)
    if parsed is None:
        return False
    _param, _value, written_for = parsed
    return not written_for or written_for == (model or "").strip().lower()


# ---------------------------------------------------------------------------
# 对外唯一出口
# ---------------------------------------------------------------------------


def request_body_for(provider: str, model: str, raw: str) -> dict | None:
    """**回答链路用**：当前型号要带的请求体片段；没设置 / 不适用则 ``None``。

    ``None`` 的语义是精确的：**调用方据此保持与接思考参数之前逐字一致**
    （``extra_body`` 传 ``None`` ⇒ cubeloop 不加任何键）。

    两条路：

    - **型号已登记**：只认该型号官方规格里的参数（换了型号的旧值不生效，不硬塞）；
    - **型号未登记**：把 `.env` 里手写的 `CHAT_REASONING` 原样透传，但写给别的型号的值不发
      ——否则「给 MiMo 关思考」会被塞给不认识它的厂商并报错。
    """
    spec = specs_mod.spec_for(provider, model)
    if spec is not None:
        value = stored_value(spec, raw, model)
        return request_body(spec, value) if value is not None else None
    if not manual_applies(raw, model):
        return None
    parsed = parse_stored(raw)
    if parsed is None:  # pragma: no cover —— manual_applies 已保证有解析结果
        return None
    param, value, _model = parsed
    return nest(param, value)


__all__ = [
    "dump_stored",
    "is_stale",
    "manual_applies",
    "nest",
    "parse_stored",
    "request_body",
    "request_body_for",
    "stored_value",
]
