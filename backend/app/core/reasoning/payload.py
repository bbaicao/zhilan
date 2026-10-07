"""`GET /api/settings` 的 ``reasoning`` 段（阶段 5 后 · 推理等级回流）。

字段形状的**唯一真相源**是冻结前端 `frontend/src/api.ts:989-1010` 的 ``ReasoningState``
——**15 个键，一个不多一个不少**（前端按对象读，缺键会让下拉显示空值而不是「未登记」）。

本模块是**纯函数**：不读 `settings`、不碰磁盘。取值来源（`chat_config` + `CHAT_REASONING`）
由调用方 `settings_schemas.reasoning_payload()` 取好传进来——这样这一层可以被单测直接钉住，
而读配置失败的口径与 `active_of` 一样收敛在那一层（读侧一切不抛，I5）。
"""

from __future__ import annotations

from typing import Any

from app.core.reasoning import specs as specs_mod
from app.core.reasoning import store as store_mod
from app.core.reasoning.specs import ReasoningSpec, display_value, option_values

#: 未登记型号的说明（同时告诉用户「手改 `.env` 这条通道还在」）
NO_SPEC_NOTE = (
    "该型号没有登记官方思考参数（我们只登记常用型号）。"
    "要调的话请在本机 .env 里手写 CHAT_REASONING，例如 "
    '{"param":"thinking.type","value":"disabled"}，会原样发给厂商。'
)

#: 连对话模型都没绑定时的那句话
NO_MODEL_NOTE = "还没有绑定对话模型。"


def payload(provider: str, model: str, raw: str) -> dict:
    """`GET /api/settings` 的 ``reasoning`` 段：界面据此渲染，**不自己编选项**。

    三种情形：

    1. **型号已登记** ⇒ 填全 ``vendor`` / ``param`` / ``kind`` / ``options`` / ``default`` /
       ``current`` / ``stale`` / ``note`` / ``source``；``current`` 只认**属于本型号**的存值
       （换型号后旧值不算数，`stale=True` 时在 ``note`` 里明说而不是静默）；
    2. **型号未登记** ⇒ ``registered=False``，但若 `.env` 里的手写值写给的就是本型号
       （或没写型号），把它放进 ``manual`` 并置 ``manual_active=True``——**界面要如实显示
       「这条正在生效」**，否则用户会以为自己的配置没被读到；
    3. **没绑定型号** ⇒ 全空 + 「还没有绑定对话模型。」。
    """
    spec = specs_mod.spec_for(provider, model)
    parsed = store_mod.parse_stored(raw)
    base: dict[str, Any] = {
        "registered": False,
        "vendor": "",
        "param": "",
        "kind": "",
        "options": [],
        "minimum": None,
        "maximum": None,
        "default": None,
        "default_label": "",
        "current": None,
        "stale": False,
        "manual": None,
        "manual_active": False,
        "note": NO_SPEC_NOTE if model else NO_MODEL_NOTE,
        "source": "",
    }
    if spec is None:
        # 未登记：只有「.env 手填」这一条路
        if parsed is not None and store_mod.manual_applies(raw, model):
            param, value, _written_for = parsed
            base["manual"] = {"param": param, "value": value}
            base["manual_active"] = True
        elif parsed is not None:
            base["note"] = (
                f"`.env` 里存的是写给别的型号的（{parsed[0]} @ {parsed[2]}），当前型号不发它。\n"
                + base["note"]
            )
        return base

    current = store_mod.stored_value(spec, raw, model)
    options = [
        {"value": item, "label": display_value(item), "hint": spec.label_of(item)}
        for item in option_values(spec)
    ]
    note = spec.note
    stale = store_mod.is_stale(spec, raw, model)
    if stale and parsed is not None:
        note = (
            f"`.env` 里的设置（{parsed[0]}）不属于该型号的官方参数，已不生效；"
            f"请在上面重新选择或清空。\n{note}"
        )
    base.update(
        {
            "registered": True,
            "vendor": spec.vendor,
            "param": spec.param,
            "kind": spec.kind,
            "options": options,
            "minimum": spec.minimum,
            "maximum": spec.maximum,
            "default": spec.default,
            "default_label": spec.default_label,
            "current": current,
            "stale": stale,
            "note": note,
            "source": spec.source,
        }
    )
    return base


__all__ = ["NO_MODEL_NOTE", "NO_SPEC_NOTE", "ReasoningSpec", "payload"]
