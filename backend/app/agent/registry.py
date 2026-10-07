"""极简 Tool Registry（OpenCode 形态：一工具一模块 + 描述与 handler 同置）。

约定：
- 工具名用契约冻结名（如 ``knowledge_search``、``mcp:{slug}:{tool}``），不要改义。
- ``dangerous=True`` 的工具默认仍可 ``list/get``（运维可见），
  但 ``for_model()`` / ``auto_executable()`` 会排除，供不变量 I6（危险工具零暴露）用。
- ``needs_web=True`` 的工具是联网工具：``for_model(web_enabled=False)`` 时不进模型清单
  （条目 4.1 明文要求「web_enabled=False 不进清单」——决策模型看不见就不会选它）。
- ``needs_confirm``（专项工具升级-3 · 阶段 5 · `回归检查`）是**写侧声明位**：
  ``True`` = 该工具会写盘、**必须**带宿主侧凭证才允许执行；``False`` = 既有只读工具。
  🔴 **默认 ``None` = 「未声明」哨兵，不是 ``False``** —— 见 :class:`ToolSpec` 的字段注释。
- handler 可为同步或异步 callable；``invoke`` 走同步，``ainvoke`` 统一 await。
  阶段 4 起工具一律写成 ``async def handler(args: BaseModel) -> ToolObservation``，
  由 :func:`app.agent.bootstrap.build_agent_tools` 适配成 CubeLoop 的 ``AgentTool``。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型标注用；运行时不 import pydantic（保持本模块零依赖）
    from pydantic import BaseModel

HandlerResult = Any
Handler = Callable[..., HandlerResult | Awaitable[HandlerResult]]


class ToolNotFoundError(LookupError):
    """请求了未注册的工具名。"""


class ToolAlreadyRegisteredError(ValueError):
    """同名工具重复注册。"""


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的完整声明：名字、描述、参数 schema、执行体、危险标记。

    ``parameters``（阶段 4 · 4.1 新增）是参数模型的**类本身**，用于转成 CubeLoop 的
    ``AgentTool(parameters=...)``；``parameters_schema`` 是它的 ``model_json_schema()``，
    给运维/契约侧看。两者同源（同一个 pydantic 模型），不会漂移。
    老构造点不传它 ⇒ 默认 ``None``，行为与新增前逐字一致。
    """

    name: str
    description: str
    parameters_schema: dict[str, Any]
    handler: Handler
    dangerous: bool = False
    #: 参数模型类（转 CubeLoop AgentTool 用）；``None`` 表示本工具不支持模型直接调用
    parameters: type["BaseModel"] | None = None
    #: 是否联网工具：``web_enabled=False`` 时不得进模型清单
    needs_web: bool = False
    #: **写侧声明位**（专项工具升级-3 · 阶段 5 · `回归检查`）：``True`` = 会写盘、**必须**带
    #: 宿主侧凭证（`confirm_token`）才允许执行；``False`` = 只读工具（既有 11 个产品工具）。
    #:
    #: 🔴🔴 **默认值是 ``None``（哨兵 = 「未声明」），不是 ``False``** —— 这是 A20 的命门：
    #:   · ``None`` ⇒ :func:`app.agent.safety.is_blocked` **一律拦下**（fail-closed）；
    #:   · 若默认写成 ``False`` ⇒ 「漏标字段」这条路径在真实构造面上**不可达**，
    #:     新加的写工具只要忘了写这个字段就会**静默拿到 False ⇒ 被放行** ⇒ 正是 B5 要防的
    #:     「漏标 ⇒ 静默变自动写」。
    #:
    #: ⇒ 🔴 **既有工具必须【逐处显式】写 ``needs_confirm=False``**（11 处产品构造点：
    #:   9 个工具 + ``tools/mcp.py`` + ``mcp_transport.py``），**不得依赖默认值** ——
    #:   否则「既有工具行为逐字不变」这条**无载体**（A20 腿②/腿③ 失去意义）。
    needs_confirm: bool | None = None


@dataclass
class ToolRegistry:
    """进程内工具注册表。阶段 0 极简实现，不依赖外部服务。"""

    _tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> None:
        if not spec.name:
            raise ValueError("工具名不能为空")
        if spec.name in self._tools:
            raise ToolAlreadyRegisteredError(f"工具已注册: {spec.name}")
        self._tools[spec.name] = spec

    def list(self, *, include_dangerous: bool = False) -> list[ToolSpec]:
        """列出工具。默认不含 dangerous；include_dangerous=True 时全量。"""
        specs = list(self._tools.values())
        if include_dangerous:
            return specs
        return [s for s in specs if not s.dangerous]

    def auto_executable(self) -> list[ToolSpec]:
        """自动执行白名单：排除 dangerous=True（I6）。

        🔴 **阶段 5（`回归检查`）起再排除 ``needs_confirm``** —— 判据 = ``s.needs_confirm is False``：
        ``needs_confirm=True``（写工具）/ ``None``（**漏标**）**都不进**自动执行白名单
        ⇒ **写工具永不自动执行**。

        这是**红线 4**（「不得让『自动写』成为默认」）的第二个落点：闸① ``for_model`` 管
        「模型看不看得见」，本条管「不经确认能不能自己跑」。**默认方向 = 排除**。

        ⚠️ **判据必须是 ``is False``，不能写成 ``not s.needs_confirm``** —— 两者对 ``None``
        **方向相反**：``not None == True`` ⇒ 漏标工具**反而进白名单**，
        与 :func:`app.agent.safety.is_blocked`（漏标 ⇒ 拦）**同源反向**，
        正是 B5 要防的「**漏标 ⇒ 静默变自动写**」。
        ⇒ ``tests/agent/test_write_tool_gate_t3_5.py::test_a13d_unlabeled_spec_never_enters_auto_executable``
        钉住本条（该用例是本判据的**唯一**判别力来源）。
        """
        return [
            s
            for s in self._tools.values()
            if not s.dangerous and s.needs_confirm is False
        ]

    def for_model(self, *, web_enabled: bool = False) -> list[ToolSpec]:
        """**模型可见**的工具清单（条目 4.1.3「注册过滤 dangerous」的唯一出口）。

        两条过滤（缺一不可，都是不变量级的要求）：

        - ``dangerous=True`` 永不出现（I6 危险工具零暴露）——老基线在 MCP 工具装载时
          就逐条跳过，v2 把它收敛到这一个出口，避免「某条新路径忘了过滤」；
        - ``needs_web=True`` 且 ``web_enabled=False`` 时不出现（条目 4.1 明文：
          「web_enabled=False 不进清单」）——不是运行时拦截，而是**根本不暴露**，
          模型没有这个选项就不可能选它。

        顺序与注册顺序一致（``dict`` 保序）⇒ 工具清单在提示词里稳定，便于对拍与缓存。

        ## 第三过滤（阶段 5 · `回归检查`）—— **方向写死 = 不排除写工具**

        单⑫ 生效档 **(a)**：``needs_confirm`` 的**唯一**过滤落点是
        :meth:`auto_executable`（排除）与 :func:`app.agent.safety.is_blocked`（拦未确认），
        **不是**这里。⇒ 写工具**照常进**模型清单（档 2 = 「确认后落盘」要求模型先看见它、
        并带 ``confirm_token`` 再调一次），**不得**在本方法里把它滤掉。

        ⚠️ 若有人把写工具从这里滤掉 ⇒ 档 2 的「确认后落盘」在**模型侧不可达**
        （``D-回归检查` ② 的反面），且 ``A13d`（「写工具真进 ``for_model()``」）**当场红**。
        本条**刻意写成显式判据**（而不是「不加任何代码」）—— 让方向在代码里**可见**：
        将来的第三过滤若想加条件，必须**显式**改这一行。
        """
        return [
            spec
            for spec in self._tools.values()
            if not spec.dangerous
            and (web_enabled or not spec.needs_web)
            # 第三过滤落点③（`回归检查`）：`needs_confirm` 在本方法里**不参与判定**
            # ⇒ 写工具（needs_confirm=True）**照常进清单**。这行是**显式的方向声明**，
            # 不是可省的空操作：`M-回归检查` 打的就是它（把它改成排除 ⇒ `A13d` 必红）。
            and spec.needs_confirm in (True, False, None)
        ]

    def get(self, name: str) -> ToolSpec:
        """取工具声明（含 dangerous，供审计/管理端使用）。"""
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(f"未知工具: {name}") from None

    def invoke(self, name: str, /, **kwargs: Any) -> HandlerResult:
        """同步调用。handler 若为协程函数则本方法会返回协程，请改用 ainvoke。"""
        spec = self.get(name)
        return spec.handler(**kwargs)

    async def ainvoke(self, name: str, /, **kwargs: Any) -> HandlerResult:
        """异步调用：同步 handler 结果原样返回，协程则 await。"""
        spec = self.get(name)
        result = spec.handler(**kwargs)
        if inspect.isawaitable(result):
            return await result
        return result
