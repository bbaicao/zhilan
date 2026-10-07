"""推理等级（厂商思考参数）登记表与请求体出口（阶段 5 后 · 推理等级回流）。

三维职责分离，各自一个文件：

- :mod:`app.core.reasoning.specs` —— **登记表**（7 条国内规格）+ 查表 + 取值校验；
- :mod:`app.core.reasoning.store` —— `CHAT_REASONING` 的**解析 / 组装**与对外唯一出口
  :func:`~app.core.reasoning.store.request_body_for`；
- :mod:`app.core.reasoning.payload` —— `GET /api/settings` 的 ``reasoning`` 段（15 键）。

## 作用范围（**本条目的核心约束**）

思考参数**只作用于最终答案那一条调用**：``answer.build_chat_model()``。
其余四条辅助链路（Agent 决策 / 澄清 / 压缩 / 记忆抽取）与笔记生成**一律不带**——
它们走 ``answer.build_aux_model()``，两函数由同一个私有实现
``_build_bound_model(include_reasoning=...)`` 分叉，**只有一处**读 ``CHAT_REASONING``。

这条隔离有 8 个钉（A12–A16、A19）在 `tests/test_reasoning_isolation.py` 与
`tests/api/test_reasoning_aux_calls.py` 上，理由见开工包 §3.2：
「漏一处就是一半带思考、一半不带的偏科」。
"""

from __future__ import annotations

from app.core.reasoning.payload import NO_MODEL_NOTE, NO_SPEC_NOTE, payload
from app.core.reasoning.specs import (
    SPECS,
    VALUE_PLACEHOLDER,
    ReasoningSpec,
    coerce_value,
    display_value,
    option_values,
    spec_for,
)
from app.core.reasoning.store import (
    dump_stored,
    is_stale,
    manual_applies,
    nest,
    parse_stored,
    request_body,
    request_body_for,
    stored_value,
)

__all__ = [
    "NO_MODEL_NOTE",
    "NO_SPEC_NOTE",
    "SPECS",
    "VALUE_PLACEHOLDER",
    "ReasoningSpec",
    "coerce_value",
    "display_value",
    "dump_stored",
    "is_stale",
    "manual_applies",
    "nest",
    "option_values",
    "parse_stored",
    "payload",
    "request_body",
    "request_body_for",
    "spec_for",
    "stored_value",
]
