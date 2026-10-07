"""knowledge_search：在知识库中检索相关片段（条目 4.1.2，契约工具名冻结）。

**契约工具名**：``knowledge_search``（不要改成 ``search_knowledge``）。
``direct_answer`` **不是** Agent 工具（它走 generation 直答），勿在此注册。

## 分工（一句话）

本模块只做「把参数变成一次检索、把结果做成 :class:`ToolObservation`」；
**观察文本的编号、材料去重与上限由运行器统一负责**（``app/agent/bootstrap.py`` 的适配层）
——这样「模型看到的 ``[n]``」与最终引用卡片的编号**同源**（I3），不会出现
「第二次检索的观察又从 1 开始编号」这种与引用对不上号的经典毛病。

## 依赖注入

``build_spec(db=..., filters=..., retrieve=...)`` 把这一轮要用的会话与范围闭合进 handler：

- ``db``：本轮 ``AsyncSession``（检索要读 ``chunks`` / ``documents``）；
- ``filters``：本轮元数据范围（阶段 3 §二「接线四条」之 1：范围必须传进检索）。
  工具参数里给了 ``filters`` 时**不能覆盖它**：两者由
  :func:`app.retrieval.types.intersect_scope_filters` 求交（专项 F6-A / ``回归修复``；
  第 0 批把入口从 ``intersect_tool_filters`` 泛化为这个名字，**两者是同一份实现**），
  模型只能在用户选定的范围里**继续收窄**；冲突或坏参数在**调用检索之前**拒绝；
- ``retrieve``：检索实现（默认 ``app.retrieval.retrieve``），单测注入假件用。
  签名为 ``async (session, question, filters, *, stats=None)``，统计经本次调用的出参回传；
  返回值仍为 ``(materials, materials_text, warning)`` 三元组。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

from app.agent.registry import ToolSpec
from app.agent.tools._errors import arg_error
from app.agent.types import ToolObservation
from app.retrieval.types import FUSE_LIMIT, intersect_scope_filters
from app.retrieval.rerank import RerankStats
from app.retrieval.query import rewrite_query

log = logging.getLogger(__name__)

TOOL_NAME = "knowledge_search"

DESCRIPTION = (
    "在本机知识库里做向量+关键词融合检索，返回最相关的资料片段；"
    "filters 只能在本轮范围内进一步收窄"
)

#: 单次检索最多取几条（上限与融合上限 FUSE_LIMIT 对齐，别在这里另立一个数字）
DEFAULT_LIMIT = 5

#: 检索实现签名；默认实现是 ``app.retrieval.retrieve``
RetrieveFn = Callable[..., Awaitable[tuple[list, str, str | None]]]


class KnowledgeSearchArgs(BaseModel):
    """工具入参（同时是给模型的 JSON Schema 来源）。"""

    query: str = Field(description="检索查询文本")
    limit: int = Field(
        default=DEFAULT_LIMIT,
        ge=1,
        le=FUSE_LIMIT,
        description="最大返回条数",
    )
    filters: dict[str, Any] | None = Field(
        default=None,
        description=(
            "可选过滤条件（JSON 对象）。认识的键只有这六个维度："
            "kb_ids（整数列表）/ kb_id（单个整数）、"
            "source_types（列表，取值只能是 document 或 note）/ source_type（单个）、"
            "categories（字符串列表）、tags（字符串列表，同一条资料需同时具备全部标签）、"
            "created_after / created_before（ISO 8601 日期或时间字符串）。"
            "只能在本轮范围里继续收窄，覆盖不了它；"
            "范围冲突、未知键或坏类型会返回失败观察，不会静默放宽"
        ),
    )


def _default_retrieve() -> RetrieveFn:
    """惰性取真实现（避免模块级 import 造成 retrieval → agent 的反向拉依赖错觉）。"""
    from app.retrieval import retrieve as real_retrieve

    return real_retrieve


def build_spec(
    *,
    db: Any = None,
    filters: dict[str, Any] | None = None,
    retrieve: RetrieveFn | None = None,
    query_history: list[dict] | None = None,
    original_question: str = "",
) -> ToolSpec:
    """装配 knowledge_search 工具声明。``db`` 为空时调用会返回可读的失败观察（不抛）。"""
    impl = retrieve

    async def handler(args: KnowledgeSearchArgs) -> ToolObservation:
        query = (args.query or "").strip()
        if not query:
            # 参数纠错走**唯一实现**（R2-01 模板 / R2-02 接线）：四件齐备（工具名 / 字段名 /
            # 约束 / 收到的值）+ 现算的 schema 摘要。
            # ⚠️ 「收到的值」回传 ``args.query`` **原文**（可能是空白串）——不要回传 strip 过的
            # 结果，否则模型看到的是「收到「」」，查不出自己写的其实是空白。
            return arg_error(
                tool=TOOL_NAME,
                field="query",
                constraint="非空字符串",
                received=args.query or "",
                parameters=KnowledgeSearchArgs,
            )
        if db is None:
            # 无会话上下文：直接说清楚，别让模型以为「库里没有资料」
            return ToolObservation(text="知识库检索缺少数据库会话，本次无法检索。", ok=False)

        # 工具参数**只能收窄**本轮宿主范围，不能覆盖它（专项 F6-A / 回归修复，
        # 讨论终稿 D1/D3）。⚠️ 这一行是**可能抛 ValueError 的调用点**，而下面的 try
        # 只包住检索调用本身 ⇒ 校验/捕获必须**就地**做，否则异常会冲出 handler，
        # 违反 I5「工具失败转观察，绝不炸穿」。
        #
        # 入口名取 ``intersect_scope_filters``（第 0 批泛化后的**唯一实现**）：第 2 批的
        # 本地资料工具（list_documents / read_document）会复用同一处；``intersect_tool_filters``
        # 是它的历史薄壳，**不要再新增调用点**。
        try:
            scope = intersect_scope_filters(filters, args.filters)
        except ValueError as exc:
            # 冲突或坏参数：**不检索**（retrieval_stats 保持 None，不伪造一次重排统计）
            return ToolObservation(text=f"知识库检索未执行：{exc}", ok=False)
        except Exception as exc:  # noqa: BLE001 —— 兜底：工具失败转观察，绝不炸穿（I5）
            # 非 ValueError 说明求交出了**没预料到**的问题（不该发生）⇒ 留一条日志便于定位，
            # 但给模型的仍是一句中文观察，不把内部异常细节当正文塞给它。
            log.warning("检索范围求交出现非预期错误：%s: %s", exc.__class__.__name__, exc)
            return ToolObservation(text=f"知识库检索未执行：{exc}", ok=False)

        fn = impl or _default_retrieve()
        stats = RerankStats()
        # 仅消息表的原始用户内容可补事实；当前用户问题也可作工具指代的明确上文。
        history = list(query_history or [])
        if original_question:
            history.append({"role": "user", "content": original_question})
        rewrite = rewrite_query(query, history, filters=scope)
        stats.query_rewrite = rewrite.payload()
        if rewrite.needs_clarify:
            return ToolObservation(
                text="检索对象仍不明确，请先向用户确认指的是哪个对象或选项。",
                ok=False,
                retrieval_stats=stats,
            )
        try:
            materials, _rendered, warning = await fn(
                db, rewrite.retrieval_query, scope, stats=stats
            )
        except Exception as exc:  # noqa: BLE001 —— 工具失败转观察，绝不炸穿（I5）
            # 回归修复：固定形状 + 原文只进日志
            log.warning("检索工具执行异常：%s", exc)
            return ToolObservation(
                text=f"知识库检索 执行失败（{exc.__class__.__name__}）。", ok=False
            )

        limit = int(args.limit or DEFAULT_LIMIT)
        hits = list(materials or [])[:limit]
        warnings = [warning] if warning else []
        if not hits:
            return ToolObservation(
                text="没有检索到相关资料。", warnings=warnings, retrieval_stats=stats
            )
        # text 是**兜底**渲染：运行器会用本轮快照编号重渲染（见模块 docstring）。
        text = "\n\n".join(
            f"[{index}] {item.filename}\n{_clip(item.content, 300)}"
            for index, item in enumerate(hits, start=1)
        )
        return ToolObservation(
            text=text, materials=hits, warnings=warnings, retrieval_stats=stats
        )

    return ToolSpec(
        name=TOOL_NAME,
        description=DESCRIPTION,
        parameters_schema=KnowledgeSearchArgs.model_json_schema(),
        handler=handler,
        dangerous=False,
        parameters=KnowledgeSearchArgs,
        needs_web=False,
        needs_confirm=False,
    )


def _clip(text: str, limit: int) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[:limit].rstrip() + "…"


#: 无会话的默认声明（运维可见的注册表 / 既有单测用；真轮次走 ``build_spec(db=...)``）
spec = build_spec()


__all__ = [
    "DEFAULT_LIMIT",
    "DESCRIPTION",
    "KnowledgeSearchArgs",
    "RetrieveFn",
    "TOOL_NAME",
    "build_spec",
    "spec",
]
