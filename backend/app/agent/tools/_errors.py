"""工具**参数纠错**观察文本的唯一实现（专项工具升级-2 · R2-01 · 阶段一）。

## 这个模块解决什么

「参数错了，回给模型的那句中文」过去散在**各个工具**的 handler 里，每个分支一句
（``缺少 query 参数：{...}`` / ``读取文档未执行：max_chunks 至少为 1（收到 0）。`` …）。
模型拿到这种句子只能**猜**——不知道约束是什么、也不知道这个字段在 schema 里长什么样，
于是往往**重试同一个坏参数**。本模块把它变成**结构化、可自纠**的一行：

    参数错误（工具 <工具名>）：字段 <字段名> 需要满足「<约束>」，收到 <值>。<schama 摘要>

## 口径（R2-01 卡片 + 开工包 D-回归检查 / D-回归检查）

- **四件齐备**：工具名 / 字段名 / 约束 / 收到的值；
- **schema 摘要现算**：从 ``parameters.model_json_schema()`` 算，**不硬编码**
  （``ToolSpec.parameters`` 是**模型类**，``registry.py:52``；另有 ``parameters_schema``
  是**预算好的 dict**，``registry.py:48`` —— 两者别混）；
- 中文、断句稳、**无换行污染**（便于日志 grep，也便于模型逐行读）；
- **只覆盖 handler 内可达的坏参数**（D-回归检查 的如实限定）：ReAct 链上的坏参数
  （缺必填 / 类型错 / 多余键 / 越界）**在 cubeloop 层就被拦**（``cubeloop/agent/tools.py:73`` 定义 / ``:207-208`` 拦点
  → 英文 ``_format_validation_error()``，**该文件不在本仓内**，是 venv 依赖，
  ``requirements.txt:19`` ``cubeloop[mysql,mcp]==0.14.1``），P&E 另有第二处英文直出
  （``runner.py:2248-2257`` 经 ``_write_tool_events_locked``（定义 ``runner.py:2014``）在 ``:2066-2071`` 落进 ``tool/result``）。
  ⇒ **本模块的「唯一实现」只在 handler 层成立**，**不得**据此写「参数纠错已统一」。
"""

from __future__ import annotations

from typing import Any

from app.agent.types import ToolObservation

#: 模板本体（**唯一一份**）。四个占位符 = 四件齐备；末尾 ``{schema}`` 是现算的摘要。
#: ⚠️ 里面**不许**出现 ``\n`` —— 无换行污染是本模块的口径之一（判据 A1）。
ARG_ERROR_TEMPLATE = (
    "参数错误（工具 {tool}）：字段 {field} 需要满足「{constraint}」，收到 {received}。{schema}"
)

#: schema 摘要的前缀（判据用它把摘要从整句里切出来）
SCHEMA_SUMMARY_PREFIX = "可用参数："

#: 必填字段在摘要里的后缀标记
REQUIRED_SUFFIX = "*"


def _describe_received(value: Any) -> str:
    """把「收到的值」渲染成**能一眼看出坏在哪**的形态（空串要显式，别让模型以为是忘了写）。"""
    if isinstance(value, str):
        return "「」（空字符串）" if not value else f"「{value}」"
    return f"「{value}」"


def _type_name(spec: dict) -> str:
    """从一个属性的 JSON Schema 片段里取出类型名（``anyOf`` 取非 null 侧）。"""
    kind = spec.get("type")
    if isinstance(kind, str):
        return kind
    kinds = [item.get("type") for item in (spec.get("anyOf") or ()) if item.get("type")]
    return "|".join(kinds) if kinds else "?"


def render_schema_summary(parameters: Any) -> str:
    """把 ``parameters`` 的 JSON Schema **现算**成一行摘要（``必填`` 用 ``*`` 标出）。

    ⚠️ **不许**把这行换成硬编码字符串 —— 它是 A1 的第二条判据（「换一个模型就换一份摘要」）。
    """
    schema = parameters.model_json_schema()
    required = set(schema.get("required") or ())
    parts = [
        f"{name}{REQUIRED_SUFFIX if name in required else ''}:{_type_name(spec)}"
        for name, spec in (schema.get("properties") or {}).items()
    ]
    return SCHEMA_SUMMARY_PREFIX + "、".join(parts)


def render_arg_error(
    *,
    tool: str,
    field: str,
    constraint: str,
    received: Any,
    parameters: Any = None,
) -> str:
    """渲染一句**可自纠**的参数错误（四件齐备 + 可选的 schema 摘要）。

    ``parameters`` = ``ToolSpec.parameters``（**模型类**）。给了就现算摘要；没给则留空，
    模板仍然完整（四件齐备不依赖它）。
    """
    summary = render_schema_summary(parameters) if parameters is not None else ""
    return ARG_ERROR_TEMPLATE.format(
        tool=tool,
        field=field,
        constraint=constraint,
        received=_describe_received(received),
        schema=summary,
    )


def arg_error(
    *,
    tool: str,
    field: str,
    constraint: str,
    received: Any,
    parameters: Any = None,
) -> ToolObservation:
    """``render_arg_error`` 的观察件包装：**统一 ``ok=False``**（坏参数一律失败观察，I5）。

    工具侧「渲染 + 失败标记」只有这一个出口 ⇒ 不会有哪个分支忘了带 ``ok=False``。
    """
    return ToolObservation(
        text=render_arg_error(
            tool=tool,
            field=field,
            constraint=constraint,
            received=received,
            parameters=parameters,
        ),
        ok=False,
    )


__all__ = [
    "ARG_ERROR_TEMPLATE",
    "REQUIRED_SUFFIX",
    "SCHEMA_SUMMARY_PREFIX",
    "arg_error",
    "render_arg_error",
    "render_schema_summary",
]
