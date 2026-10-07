"""执行侧安全闸门（阶段 4 · 条目 4.3.1）：**执行前**拒绝危险工具（I6 危险工具零暴露）。

## 为什么还需要这一道（前面已经有「两道」了）

``dangerous`` 在 4.3 之前有**两个**过滤点、**零个**执行点拦截：

| 闸 | 位置 | 管什么 |
|---|---|---|
| ① MCP 装配 | ``app/agent/tools/mcp.py::build_specs`` | dangerous 的工具**连 ``ToolSpec`` 都不生成** |
| ② 注册表 | ``app/agent/registry.py::for_model`` | 模型清单的唯一出口（``if not spec.dangerous``） |
| ③ **执行点** | **本模块** | 已经拿到 spec 对象的执行路径 |

前两道是**同一个假设**——「危险工具根本没进注册表」。而 ``build_specs`` 是**唯一的**生产者
假设：``ToolRegistry.register()`` 接受任何 ``dangerous=True`` 的 spec（它只做重名检查）。
今天没有别的调用方；**4.5.3 绑真 MCP transport 时就会有**（那时工具是**运行时**从 server
握手拿到的，不是启动时静态配置）。执行点无闸 = 一个「注册进表」的动作就能绕过 I6。

第二层理由：I6 的字面是「**零暴露**」。4.1 的用例断言的是「回到 ``build_agent_tools`` 的
清单」（= 闸②），**执行侧零覆盖**——执行点是它唯一还能被破的地方。

## 闸门位置：一处实现、两处调用

ReAct 的 ``runner._as_agent_tool.execute`` 闭包与 P&E 的 ``runner._run_plan_step``
各调一次 :func:`blocked_observation`。**两条路径各写一份判定 = 迟早漂移**（4.2 的
``_write_tool_events`` 共用是同一个教训）。

## 判据可证伪

「注册表里真的有一条 ``dangerous=True`` 的 spec ⇒ handler 调用计数 == 0」——
用例必须先把这个前提构造出来，否则会与 4.1 的清单用例重复、且**测不到执行点**
（见 ``tests/agent/test_agent_safety.py``）。

## 拦截的可见形状（三条，缺一不可）

1. **不抛异常**（I5）——返回一条 ``ok=False`` 的 :class:`~app.agent.types.ToolObservation`；
2. **留痕**——被拦截的那一步仍产 ``tool`` + ``observation`` 两帧、仍走
   ``_write_tool_events`` 写 ``tool/call`` ↔ ``tool/result`` 一对（I4 不破）；
   「拦了」必须看得见，静默跳过是 4.2 明令禁止的模式；
3. **文案冻结**——:data:`DANGEROUS_BLOCKED_TEMPLATE` 是**用户可见**的观察文本
   （会进 ``observation.detail`` 与 ``tool/result.content`` ⇒ 会落库、会被 4.4 的投影读到），
   措辞漂移会让「同一次拦截」在库里出现两种说法（`回归修复`/`027` 同型的
   「同一文案多处出现」坑），所以它是一个常量、不是就地拼的 f-string。

依赖方向：本模块只依赖 ``app.agent.types``（同层），**不 import ``app.api``**。
"""

from __future__ import annotations

from typing import Any

from app.agent.types import ToolObservation

#: 拦截文案模板（**冻结**，写进用例）。占位符是工具**契约名**。
DANGEROUS_BLOCKED_TEMPLATE = "危险工具 {tool} 已被安全闸门拦截，本轮不会执行。"


def is_blocked(spec: Any) -> bool:
    """该工具声明是否被安全闸门拦下。

    ## 两个维度，方向**相反**（专项工具升级-3 · 阶段 5 · `回归检查` 起）

    | 维度 | 取值 | 语义 | 本函数 |
    |---|---|---|---|
    | ``dangerous`` | ``True`` | 危险工具（I6 零暴露） | **拦** |
    | ``dangerous`` | ``False`` / 取不到 | 非危险 | **不拦**（**逐字未变**，4.3 起既有语义） |
    | ``needs_confirm`` | ``False`` | **显式声明**只读 | 不拦 |
    | ``needs_confirm`` | ``True`` | **显式声明**要确认 | 🔴 **不拦**（交**凭证闸**，见下） |
    | ``needs_confirm`` | ``None`` / 取不到 | **未声明**（漏标） | 🔴 **拦**（反向 fail-closed） |

    ## 为什么 ``needs_confirm`` 必须反向（本条的立条理由）

    ``dangerous`` 用 ``getattr(..., False)`` 兜底 = 「取不到 ⇒ 放行」。这条口径对**只读**世界
    是安全的（猜出来的拦截会误伤正常工具）。但**写通道**下同一个方向会**静默放行**：

    - 新增一个写工具、**忘了**在 ``build_spec`` 里声明 ``needs_confirm=True``；
    - 旧写法「取不到 ⇒ 放行」⇒ 它**被当成只读工具放行** ⇒ **写操作绕过凭证闸**；
    - 而漏标是**静默**的（不报错、不告警）⇒ 正是 B5 要防的「漏标 ⇒ 静默变自动写」。

    ⇒ 写侧取**反方向**：**只有【已显式声明】的工具才放行**，``None``（未声明）与**字段整个取不到**
      **一律拦下**。「漏标」于是**变成可见的拦截**，而不是静默的放行。

    🔴 **但 ``True`` 与 ``None`` 必须分开**（2026-09-26 实测更正，别把两件事合并）：

    - ``None`` / 取不到 = 「**未声明**」⇒ **本函数拦**（`A20` 的靶心就是它）；
    - ``True`` = 「**已声明要确认**」⇒ **本函数放行**，由**凭证闸**
      （``_confirm.verify_write_confirm``）在 handler 内部决定「放行 / 要求确认」。

    ⚠️ **为什么 ``True`` 不能在这里拦**：本函数是**执行侧第三道闸**
    （``runner.py`` 的 ReAct / P&E 两个执行点），返回 True ⇒ handler **一次都不被调用**；
    而写工具**全部交付面**（``A7``/``A8``/``A9``/``A12a``/``A21``/``A22``/``A23``/``A28``）
    都发生在 **handler 内部** ⇒ 在这里拦掉 ``True`` 会让写工具**在任何执行路径上都不可达**
    ⇒ **8 条判据结构性不可达**（不是红，是「测不到」）。
    单⑫ 档 (a) 的裁决是「写工具**进清单**、**执行前不落盘**」——「执行」是前提，
    「不落盘」由 handler 内部的凭证闸实现（无凭证 ⇒ ``ok=False``，零 IO）。

    ## 与既有调用方的关系（如实登记，别当没发生）

    传进来的可能是 CubeLoop 的 ``AgentTool``（没有这两个字段）或测试替身对象 ⇒
    按上表：``dangerous`` 取不到 ⇒ 不拦（不变）；``needs_confirm`` 取不到 ⇒ **拦**。
    ⚠️ 生产路径不受影响：``runner._as_agent_tool`` 的闭包捕获的是 **spec 本体**，
    P&E 侧走 ``_spec_map`` 按名字反查的也是 ``ToolSpec``（``registry.get``）——
    两条路径拿到的都是真 ``ToolSpec``。**只有「注入裸 ``AgentTool`` 且不在注册表里」**这种
    （测试专用）形态会命中「取不到」分支。
    """
    # ① 危险工具：既有语义**逐字不动**（取不到 ⇒ 不拦）
    if bool(getattr(spec, "dangerous", False)):
        return True
    # ② 写侧：反向 fail-closed —— **只对「漏标」拦**（``None`` / 字段取不到）。
    #
    # 🔴 语义边界（本条的命门，别把两件事合并）：
    #   · ``needs_confirm is None`` / 取不到 ⇒ 「**未声明**」⇒ **拦**（A20：漏标 ⇒ 拒）；
    #   · ``needs_confirm is True``  ⇒ 「**已声明要确认**」⇒ **不在这里拦** ——
    #     该工具**本来就该进执行路径**，由**凭证闸**（``_confirm.verify_write_confirm``）
    #     在执行点决定「放行 / 要求确认」。
    #
    # ⚠️ **为什么 ``True`` 不能在这里拦**（2026-09-26 实测踩到，如实登记）：
    #   本函数是**执行侧第三道闸**（``runner.py:1140`` ReAct / ``:2388`` P&E），
    #   返回 True ⇒ handler **一次都不被调用**。而写工具的**全部交付面**
    #   （``A7``/``A8``/``A9``/``A12a``/``A21``/``A22``/``A23``/``A28``）都发生在
    #   **handler 内部** ⇒ 若在这里拦掉 ``True``，写工具在**任何执行路径上都不可达**
    #   ⇒ **8 条判据结构性全不可达**（不是红，是「测不到」）。
    #   单⑫ 档 (a) 的裁决是「写工具**进清单**、**执行前不落盘**」——「执行」是前提，
    #   「不落盘」由 handler 内部的凭证闸实现（无凭证 ⇒ ``ok=False``，零 IO）。
    return getattr(spec, "needs_confirm", None) is None


def blocked_observation(spec: Any) -> ToolObservation:
    """被拦截时那一轮的观察：``ok=False`` + 冻结中文文案 + 一条同文 warning。

    ``warning`` 与 ``text`` 同句是**有意**的：``ToolCollector.absorb`` 会把 warnings 收进
    ``turn.warnings``（最终以 blockquote 出现在回答尾部），观察文本则进 ``tool/result.content``
    ——两处都看得见，且读者不必对照两份措辞。
    """
    text = DANGEROUS_BLOCKED_TEMPLATE.format(tool=str(getattr(spec, "name", "") or "未知"))
    return ToolObservation(text=text, ok=False, warnings=[text])


__all__ = ["DANGEROUS_BLOCKED_TEMPLATE", "blocked_observation", "is_blocked"]
