"""Agent 层装配：把契约工具装进注册表，再按本轮依赖闭合。

两个装配入口，别混用：

- :func:`build_default_registry` / :func:`get_default_registry`：**运维视图**——不绑任何
  会话，只有「静态可见」的工具声明（``knowledge_search`` 等），供注册表自检、审计与
  ``/api`` 之外的运维脚本看；它**不带 db**，真调用会返回可读的失败观察。
- :func:`build_registry`：**每轮一份**——把这一轮的 ``AsyncSession`` / ``filters`` /
  ``web_enabled`` 闭合进各工具的 handler，是 Agent 实际用的那份。

为什么要「每轮一份」而不是全局单例：工具执行体需要当轮会话（检索要读库）与当轮范围
（§3.8 的 filters 必须下推，见阶段 3 §二接线四条之 1）。把它们藏进全局单例会让
「同一进程里两个并发请求共用一份会话」——异步驱动明确不允许同一连接并发执行。
"""

from __future__ import annotations

import logging
from typing import Any
from dataclasses import replace

from app.agent.registry import ToolRegistry
from app.agent.types import ToolObservation
from app.agent.tools import current_time, knowledge_search, list_documents, read_document
from app.agent.tools import mcp as mcp_tools

logger = logging.getLogger(__name__)

_default_registry: ToolRegistry | None = None


def bind_retrieval_observation(observation: ToolObservation, tool_call_id: str) -> ToolObservation:
    """固定每次检索的统计快照与既有调用 id；不影响材料编号和观察文本。"""
    return replace(
        observation,
        tool_call_id=tool_call_id,
        retrieval_stats=(
            replace(observation.retrieval_stats)
            if observation.retrieval_stats is not None else None
        ),
    )


def _register_mcp_specs(registry: ToolRegistry, specs) -> None:
    """注册 MCP 契约：**按 ``spec.name`` 去重**，后到者跳过 + warning（``回归修复``）。

    为什么修在**调用方**而不是 ``ToolRegistry.register``：那条 ``ToolAlreadyRegisteredError``
    是注册表的契约面（I6，既有用例钉着），把它降级成静默等于改语义。而真正的问题在配置面 ——
    MCP 的契约名是 ``mcp:{slug}:{tool}``，两个包写同一个 ``slug``（或同一包被写两遍）就会撞名，
    而 ``register`` 一抛就让**整轮工具装配作废**（连 ``knowledge_search`` 这些非 MCP 工具也一起没了），
    与 I5「装配不炸穿」直接冲突。⇒ 这里按「先到者胜」保住整轮，并留一条可检索的 warning。
    """
    seen: set[str] = set()
    for spec in specs:
        if spec.name in seen:
            logger.warning("MCP 契约名重复，已跳过：%s", spec.name)
            continue
        seen.add(spec.name)
        registry.register(spec)


def build_registry(
    *,
    db: Any = None,
    filters: dict | None = None,
    web_enabled: bool = False,
    retrieve: Any = None,
    search: Any = None,
    fetch: Any = None,
    mcp_packs: list | None = None,
    call_tool: Any = None,
    mcp_specs: list | None = None,
    query_history: list[dict] | None = None,
    original_question: str = "",
) -> ToolRegistry:
    """构造**本轮**的工具注册表（依赖闭合进 handler）。

    「web_enabled=False 不进清单」的**注册侧**落法：联网工具只有在 ``web_enabled=True`` 时
    才``register``。做成「注册了但被 for_model 过滤掉」也可以（``needs_web`` 字段就是为它
    准备的），但**两道一起做**更稳：注册表本身（运维视图）也不该出现用不了的联网工具。
    ``ToolRegistry.for_model`` 那一层是给「运行期把 web_enabled 关了」的兜底。
    """
    registry = ToolRegistry()
    registry.register(
        knowledge_search.build_spec(
            db=db, filters=filters, retrieve=retrieve,
            query_history=query_history, original_question=original_question,
        )
    )
    # 无条件注册（D3/D-h）：与 knowledge_search 同列
    registry.register(current_time.build_spec())
    registry.register(list_documents.build_spec(db=db, filters=filters))
    registry.register(read_document.build_spec(db=db, filters=filters))

    # 只读文件工具族（专项工具升级-3 · 阶段 3 · 回归检查 的**注册门**）：
    # 工作区根**未配置** ⇒ **不注册**（单⑨ (c)「不设默认」⇒ 功能默认不可用，出厂档 0）。
    # ⚠️ **必须在函数体内现调** `resolve_workspace_root()`（`D-回归检查` 第 🔴 段）：
    # 它是真解析链（`_paths.py:589` 会做 `path.resolve()`），若在 **import 期**算一次
    # 写成模块级常量，**设置页改根后不热生效**（同一个进程里第二次装配拿到的仍是旧根）
    # ⇒ 判据 `A5b` 第 5 条腿「同进程跃迁」正是它的靶子（`M-回归检查`）。
    # ⚠️ 门只看 `is None`：`_paths.py` 的 `resolve_workspace_root()` **不判 `is_dir`**
    # ⇒ 根配成**不存在** / **普通文件** 时**照常注册**（`A5b` 态②③「仍在清单」= 出处语义），
    # 拒只在**调用**时由 `classify()` 给（态③ ⇒ `PATH_ROOT_UNUSABLE_TEMPLATE`）。
    from app.agent.tools import _paths as _tool_paths

    if _tool_paths.resolve_workspace_root() is not None:
        from app.agent import tools as _tools_pkg

        # 三件在 `回归检查` 落地（本条目只落**门**）。逐件 `getattr` 而不是整块
        # `from ... import a, b, c`：门与工具是**两条**交付面，门不得因为「工具还没接上」
        # 而把整轮装配炸穿（I5「装配不炸穿」同款口径，与上面 MCP 重复名只 warning 一致）。
        for _name in ("list_files", "read_file", "grep_files"):
            _module = getattr(_tools_pkg, _name, None)
            if _module is not None:
                registry.register(_module.build_spec())

        # 写工具（专项工具升级-3 · 阶段 5 · 回归检查 · D-回归检查 ⑧）
        # 🔴 **注册在本门【之内】**（与只读三件同列）—— 两条理由：
        #   ① 红线 3 同款：根未配 ⇒ 文件能力**整体不可用**（写工具也不例外）；
        #   ② 与既有断言一致：`tests/eval/test_tool_disclosure_r2.py` 的 `_assembly()`
        #      **不配根** ⇒ 根未配态清单仍为 4/6（写工具进来会把它打红）。
        # ⚠️ 逐件 `getattr` 同款：工具未落地时门不得把整轮装配炸穿（I5）。
        _writer = getattr(_tools_pkg, "workspace_write", None)
        if _writer is not None:
            registry.register(_writer.build_spec())

    if web_enabled:
        from app.agent.tools import fetch_page, web_search

        registry.register(web_search.build_spec(search=search))
        registry.register(fetch_page.build_spec(fetch=fetch))

    if mcp_specs is not None:
        # D-i′：以 mcp_specs 为准；同时给 mcp_packs 只 warning、不抛错（t0_wire spy 会透传两者）
        if mcp_packs is not None:
            logger.warning(
                "build_registry 同时收到 mcp_specs 与 mcp_packs，以 mcp_specs 为准（D-i′）"
            )
        _register_mcp_specs(registry, mcp_specs)
    else:
        packs = mcp_packs if mcp_packs is not None else mcp_tools.load_packs()
        _register_mcp_specs(registry, mcp_tools.build_specs(packs, call_tool=call_tool))
    return registry


def build_default_registry() -> ToolRegistry:
    """构造一份全新的默认 registry（静态可见工具；不绑会话）。

    只有 ``knowledge_search``：联网工具默认关（``WEB_ENABLE=False``），MCP 包清单默认为空
    ——两者都进不来，与「没配置就等于没有」的产品口径一致。
    """
    return build_registry()


def get_default_registry() -> ToolRegistry:
    """进程级默认 registry；首次访问时惰性构建。"""
    global _default_registry
    if _default_registry is None:
        _default_registry = build_default_registry()
    return _default_registry


def reset_default_registry() -> ToolRegistry:
    """重置为全新默认实例（测试/热更新用）。"""
    global _default_registry
    _default_registry = build_default_registry()
    return _default_registry


__all__ = [
    "build_default_registry",
    "build_registry",
    "get_default_registry",
    "reset_default_registry",
]
