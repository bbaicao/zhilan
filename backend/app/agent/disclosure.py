"""渐进式工具披露的**唯一实现**（专项 · 工具升级-2 · 阶段四 · 条目 `R2-08` 接线）。

把「本轮模型可见的工具清单」折成：**一段目录（进 system prompt）+ 两个内置工具**
（``load_tools`` / ``deferred_tool_call``，由 ``cubeloop`` 的 ``DeferredToolsMiddleware`` 提供）
**+ 留在原生态的少数工具**。折叠掉的工具不再逐个发给 provider（省掉它们的 schema 字节），
但仍留在 agent 的工具表里（``expose_to_model=False``）⇒ 引擎照旧能按名字解析并执行它们，
既有链路（含测试里那些直接点名调工具的假模型）行为不变。

## 为什么披露逻辑**不落进** ``ToolRegistry.for_model()``

``for_model()`` 是**条目 4.1 的硬契约面**（``dangerous`` 零暴露 + ``needs_web`` 门控），
它被 **3 个测试文件的 9 处断言**钉住（``tests/test_agent_registry.py`` 的
``== ["safe"]`` / ``== ["safe", "web_search"]`` 两处相等断言 + 一处成员断言；
``tests/agent/test_web_tools.py`` 的 ``== []`` / ``== ["fetch_page", "web_search"]``；
``tests/agent/test_agent_safety.py`` 四处）—— 把披露塞进去会**同时打红三个独立测试文件**。
本模块因此只在**装配之后**做映射：**读** ``for_model()`` 的结果，**不改**它。

## 五条硬边界（R2-08 通过分支）

1. ``for_model()`` 的过滤语义**一字不改**；
2. **零帧键 / 零事件类型 / 零端点 / 零新依赖**（deferred 件是 ``cubeloop`` 的**存量**）；
3. 策略**写死** ``dispatch``（官方语义：tools 数组与 system prompt **整轮字节稳定** ⇒ 零 prompt-cache 失效）；
4. 工具**一条不少、顺序不变**（只翻 ``expose_to_model``）——「隐藏但仍在表里」是有意的：
   它把「模型改走 ``deferred_tool_call`` 之后，既有直接点名调用照旧能执行」这条回归面压住；
5. **不动**收口 / 审计 / 配额面。

## 目录与折叠组的元数据

``GROUP_ID`` / ``GROUP_NAME`` / ``GROUP_DESCRIPTION`` 与离线评测件
（``tests/eval/test_tool_disclosure_r2.py``）里的同名常量**必须一致** ——
那三个值直接决定**模型读到的目录文本**，两处漂移会让「评测量的」与「线上跑的」不是一个东西
（接线判据件里有一条**同等性断言**钉住它）。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

#: 披露策略：**写死** ``dispatch``（``inject`` 会在每次扩展时重读 prompt cache，违背本条的立条理由）。
DISCLOSURE_STRATEGY = "dispatch"

#: **留在原生态**的工具名（其余全部进折叠组）。
#:
#: 为什么是 ``current_time``：它是**零参数、无副作用**的元信息工具（schema 仅约 305 B），
#: 且「``react`` 轮决策请求体 ``tools[]`` 含 ``current_time``」是**已验收批次**
#: （第 2 批 · ``tests/api/test_current_time_t2.py::test_a1_current_time_enters_react_checklist``）
#: 钉住的判据面 —— 把它折进组里 = **改既有判据**（越界）。
NATIVE_TOOL_NAMES: frozenset[str] = frozenset({"current_time"})

#: 折叠组的元数据（模型看到的就是这三项 + 工具名清单 + 数量）。
GROUP_ID = "sources"
GROUP_NAME = "资料工具"
# 专项工具升级-3 · 阶段 3 · `回归检查`（`D-回归检查`）：**补文件能力**。
# 本阶段新增三个**只读文件工具**（`list_files` / `read_file` / `grep_files`）并进折叠组
# ⇒ 组描述必须让模型知道「这组里还有浏览与读取工作区文件的能力」，否则模型只看到
# 原名而不知道有这条路（披露面的**能力可见性**）。
# ⚠️ `GROUP_NAME` **取「不改」**（`D-回归检查` ②）：「资料工具」已涵盖文件。
#
# 专项工具升级-3 · 阶段 5 · `回归检查`（`D-回归检查`）：**补写能力**（本笔的**唯一交付面**）。
# `D-回归检查` 定档 = **(甲)**：写工具`workspace_write`**留在折叠组**（**不进** `NATIVE_TOOL_NAMES`）
# ⇒ 模型对写工具**看不到完整参数 schema**，只能从**组描述 + 工具名**推断
# ⇒ 可发现性**全部**压在这一句文案上（这正是本笔要改它的理由）。
# ⚠️ 保留阶段 3 的「浏览 / 读取」措辞（**不得**为加写能力而删掉既有能力声明）；
# ⚠️ `NATIVE_TOOL_NAMES` / `GROUP_ID` / `GROUP_NAME` / `DISCLOSURE_STRATEGY` **逐字不变**。
# ⚠️ 改本行**必须同批改** `tests/eval/test_tool_disclosure_r2.py:110` 的逐字镜像
# （红线 7；两处对称断言 `eval:410` 与 `wiring_r2:132` 会同时红）。
GROUP_DESCRIPTION = "检索知识库、阅读文档、联网取资料与浏览、读取、写入工作区文件"


def build_disclosure(tools: list[Any]) -> tuple[list[Any], list[Any]]:
    """把一轮工具清单折成 ``(交给 Agent 的 tools, 折叠组列表)``。

    返回值一 = **原清单**（**顺序不变、一条不少**），只是折叠掉的那些逐个
    ``expose_to_model=False``（引擎仍可解析/执行，provider 不再收到它们的 schema）。

    返回值二 = 折叠组（``[]`` ⇒ 没有可折的，调用方照旧不接披露；``loader`` 每个 group
    每次 run 只调一次，由中间件缓存）。

    **不抛**：任何输入都能构造出结果；``tools`` 为空时返回 ``([], [])``。
    """
    from cubeloop.deferred.types import DeferredToolGroup

    deferred = [tool for tool in tools if tool.name not in NATIVE_TOOL_NAMES]
    revealed = [
        tool if tool.name in NATIVE_TOOL_NAMES else replace(tool, expose_to_model=False)
        for tool in tools
    ]
    if not deferred:
        return revealed, []

    async def _loader() -> list[Any]:
        """折叠组的加载器：只做「把本轮那份工具表交出去」，不碰任何状态。"""
        return list(deferred)

    group = DeferredToolGroup(
        group_id=GROUP_ID,
        display_name=GROUP_NAME,
        description=GROUP_DESCRIPTION,
        tool_names=[tool.name for tool in deferred],
        loader=_loader,
    )
    return revealed, [group]


__all__ = [
    "DISCLOSURE_STRATEGY",
    "GROUP_DESCRIPTION",
    "GROUP_ID",
    "GROUP_NAME",
    "NATIVE_TOOL_NAMES",
    "build_disclosure",
]
