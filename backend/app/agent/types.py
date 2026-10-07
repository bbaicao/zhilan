"""Agent 层公共类型（阶段 4 · 条目 4.1）。

两个类型：:class:`ToolObservation`（工具执行产物）与 :class:`ToolContextItem`
（**普通工具结果通道**的条目，专项工具调用第 0 批新增）。

为什么单独成模块：``app/agent`` 下的工具模块（``tools/*.py``）、装配层（``bootstrap.py``）
与运行器（``runner.py``）都要用它；放在任一侧都会造成「工具反向依赖 runner」或
「runner 依赖每个工具模块」的循环。放这里，依赖方向恒为
``tools / bootstrap / runner → types``。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.retrieval.types import Material
from app.retrieval.rerank import RerankStats


@dataclass
class ToolObservation:
    """一次工具执行的产物：给模型看的观察文本 + 可引用的材料 + 警告。

    三个字段分工（**别混用**）：

    - ``text``：观察文本，进「工具结果」消息给决策模型看；它是模型**唯一**能读到的
      工具产出（材料本体不给模型，材料的正文进最终回答的资料区）；
    - ``materials``：**可引用的材料快照行**。运行器按 ``chunk_key`` 去重累积、重新编号
      （``retrieval.types.number``）后交给既有回答链路 ⇒ 「模型看到的 ``[n]``」与
      「引用卡片的编号」同源（I3）；
    - ``warnings``：**可读中文**降级提示（如向量路降级、网页抓取失败）。⚠️ 实测登记
      （开工包 §5 第 21 条）：``ToolObservation.warnings`` 的这条 docstring 声称它们会
      「追加到最终回答正文末尾（老基线 ``_attach_warnings`` 口径）」，但**全仓只找到 log
      与注释、没有定位到真正执行该追加的代码** ⇒ 记为**未确认**，待后续条目核实。

    ``ok=False`` 表示这次执行没拿到有用结果（工具抛错 / 未配置 / 参数缺失）。
    与老基线一致：**失败不终止循环**，观察照常回给模型，让它下一轮自纠。
    """

    text: str = ""
    materials: list[Material] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ok: bool = True
    retrieval_stats: RerankStats | None = None
    tool_call_id: str | None = None

    def clipped_text(self, limit: int) -> str:
        """截断后的观察文本（步骤流与事件载荷共用同一份截断口径）。"""
        return clip(self.text, limit)


@dataclass
class ToolContextItem:
    """**普通工具结果**进最终回答请求体的结构化条目（恰好五键，专项第 0 批 · D1）。

    为什么要有它（开工包 §0.2 的「真实缺口」）：带 ``materials`` 的工具
    （``knowledge_search`` / ``web_search`` / ``fetch_page``）的结果**早就**经
    ``turn.materials → plan.materials_text``（``retrieval.render_materials``）进了回答请求体的
    「资料如下」区；**真正没有载体的是「纯文本工具结果」**——``mcp:{slug}:{tool}``（材料恒空）、
    失败 / 被安全闸拦下的观察、快照打满的 ``SNAPSHOT_FULL_TEXT`` 那一类。它们过了
    ``runner._finish``（只搬 ``collector.materials``）之后**永不进回答请求**。本类型就是给
    **这一半**结果补的载体。

    五键（**不得增删**，D1 逐字冻结）：

    - ``tool``：工具契约名（``knowledge_search`` / ``mcp:evil:probe``…）。⚠️ **工具名本身
      可以出现在最终回答请求体里**（安全闸的判据是「被拦工具的参数与观察文本不出现」，
      **不是**「名字不出现」——见 A19）；
    - ``tool_call_id``：I4 的**配对键**（唯一能跨事件表对账的字段；``ToolAudit.step_no``
      是记账序 = 完成序，**不能**用来对账，见 :class:`~app.agent.runner.ToolAudit` 的注释）；
    - ``ok``：这次执行成没成。``ok=False`` **可以**说明失败原因，但回答模型不得据此产生
      成功事实（D6）；
    - ``text``：**有界**的原始文本。⚠️ 取的是**观察原文**（``observation.text``），
      **不是** ``rendered``——``rendered`` 是按快照编号重渲染的决策侧文本，两者天然不同
      （A2b 的判据就钉这条）；
    - ``collected_at``：**库内时钟**（naive；D7）。取值走
      :func:`app.agent.audit.db_clock_now`，与 ``agent_runs`` / ``agent_steps`` 既有审计时钟
      **同源**。⚠️ **不得**用 ``datetime.now()``（本机钟）：两钟不同轴，桌面档（SQLite/UTC）
      会整体偏一个时区（``回归修复`` 的教训）。**字段与任何用户可见文案都必须标注
      「库内时钟」**。
    """

    tool: str
    tool_call_id: str
    ok: bool
    text: str
    collected_at: str


def clip(text: str, limit: int) -> str:
    """超长截断加省略号（老基线 ``agent._clip`` 逐字同形：``[:limit].rstrip() + "…"``）。"""
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "…"


__all__ = ["ToolContextItem", "ToolObservation", "clip"]
