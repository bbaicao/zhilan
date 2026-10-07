"""Agent 运行器（阶段 4 · 条目 4.1）：CubeLoop 决策循环 + 契约工具 + 步骤留痕。

## 一句话职责

**跑「决策 → 调工具 → 观察」的循环，产出步骤流与材料快照；最终正文不在这里生成。**

老基线（`<legacy-repo>\\backend\\app\\rag\\agent.py`，只读对照）的模块 docstring 写的正是这条分界线：
「本模块只负责决策/工具/观察的循环……**不做最终作答**——拿到 chunks 后仍走既有 RAG 回答链路，
引用格式、去重、编号规则与普通问答一致」。v2 沿用这个切分，理由是**不变量 I3**：

- 本运行器把材料累积、去重、上限（12）、快照编号（1..N）与渲染都收敛在
  :class:`ToolCollector` 一处，最终正文由 ``answer.build_messages`` +
  ``citation.refs_for_kind`` 用**同一份快照**产出 ⇒ 「模型看到的 ``[n]``」与
  「引用卡片的编号」由构造保证同源，不需要给 Agent 轮另抄一套编号逻辑。

## 「决策 max_tokens=600、不带 reasoning」怎么落

CubeLoop 的循环里每一次模型调用都**是**一次决策（要么调工具、要么收尾），
因此条目原文的要求直接作用在整条循环上：

- ``on_payload`` 回调把请求体的 ``max_tokens`` 钉成 :data:`DECISION_MAX_OUTPUT_TOKENS`。
  ⚠️ 走 ``on_payload`` 而不是 ``generate(max_output_tokens=...)`` 的原因：循环内部用的是
  ``BoundModel.stream()``，它**没有** ``max_output_tokens`` 形参（传了 TypeError，见
  ``chat_routes.stream_tokens`` 的注释）；``on_payload`` 是 provider 层唯一能在发送前
  改请求体的钩子（已核实 0.14.1 的 ``openai`` provider 在发请求前调用它）。
- ``reasoning=ReasoningControl(mode="off")``：不带思考（老基线「决策与计划都是小调用，
  思考预算是厂商自己的事」的口径）。
- 循环收尾时模型产出的那段文字**不进回答**（回答由回答链路重新生成）——所以 600 的预算
  不会截断用户看到的正文。

## 双模式（条目 4.2）：谁决定下一步

两条路径**结构不同**，不是同一段代码的两种参数（老基线同构，见
``<legacy-repo>\\backend\\app\\rag\\agent.py`` 的 ``mode == "plan_execute"`` 分支）：

- ``react``（默认）：**模型**决定下一步——CubeLoop 原生 tool-calling 循环，
  决策→工具→观察，循环到模型不再调工具或轮次用尽；
- ``plan_execute``：**计划表**决定下一步——一次计划调用（
  :func:`plan_prompt` + :data:`PLAN_SYSTEM`）拿到至多 :data:`PLAN_MAX_STEPS` 步，
  然后逐步直接执行工具、**步间不问模型**；计划解析失败 ⇒ 回退为只检索一次知识库
  （条目原文的「失败回退单步检索」）。

P&E 走 ``BoundModel.generate()`` 单次调用（它**原生支持** ``max_output_tokens`` 与
``reasoning``），所以不需要 ReAct 那条 ``on_payload`` 的路子。两条路径**共用**
:class:`ToolCollector`（材料编号/去重/上限 ⇒ I3 由构造保证）与
:func:`_write_tool_events`（``tool/call`` ↔ ``tool/result`` ⇔ I4），ReAct 路径的代码
**逐字未动**。

## 绝不炸穿（I5）

模型不可用、工具抛异常、CubeLoop 抛任何意外，一律转成 ``note`` 步骤 + ``turn.note``；
本生成器不向调用方抛业务异常。唯一向外传播的是 ``CancelledError``（用户在流中途按了停止）
——那是**预期路径**，由 ``finally`` 里的 ``agent.abort()`` + 任务取消收口。

## 安全闸门与审计（条目 4.3）

两条**执行点**（ReAct 的 ``_as_agent_tool.execute`` 闭包 + P&E 的 ``_run_plan_step``）
在调 ``spec.handler`` **之前**各过一次 :func:`app.agent.safety.is_blocked`——一处实现、
两处调用（各写一份判定迟早漂移；4.2 的 ``_write_tool_events`` 共用是同一个教训）。
被拦的那一步仍然产 ``tool`` + ``observation`` 两帧、仍然成对落 ``tool/*`` 事件（I4 不破）：
「拦了」必须看得见。

**审计（`agent_runs` / `agent_steps`）写在这里而不是在 `chat_routes`**：数据源就是本模块的
两个执行点（``ok`` / ``ms`` / ``args`` / 观察文本全在这一刻才知道）。``chat_routes`` 只负责
「一轮开始建 run、结束收口 status、assistant 落库后回填 message_id」这三件与 HTTP 生命周期
绑定的事（见 ``app/agent/audit.py`` 的模块 docstring）。

**审计的「记账 / 写库」是分开的两步**（``ToolCollector.audits`` + :func:`_flush_audit`）：
CubeLoop 的工具执行跑在**它自己的任务**上，而事件落库跑在**消费端任务**上——两者共用同一个
请求级 ``AsyncSession``。在执行点直接写库会与并发的 flush 打架并**静默丢一行审计**
（实测：ReAct 跑两个工具时第 2 行必丢）。所以执行点只记账，写库一律由消费端任务串行做。

``ToolAudit.step_no`` 是**整轮**的工具调用序号（1 起，ReAct 与 P&E 共用一个记账器）：
两条路径都往同一个 ``ToolCollector.audits`` 追加，所以「这一轮的第几次工具调用」在两处口径一致。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time

from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

# ⚠️ `ast` / `inspect` 只用于**给用户看的步骤标题**里的「行数」统计（`_file_change_title`）：
# diff 的行号本身由 `_write.render_diff` 产出（它才是唯一实现）—— 这里**只数类，不解析内容**，
# 避免两处各写一份 diff 解析（`回归检查` 的「同源」纪律）。
import ast
import inspect

from app.agent.registry import ToolRegistry
from app.agent.bootstrap import bind_retrieval_observation
from app.agent.types import ToolContextItem, ToolObservation, clip
from app.agent import audit as agent_audit
from app.agent import safety
from app.agent import tool_artifacts
from app.core import derived_counters
from app.core.config import settings
from app.db.session import independent_session
from app.retrieval.types import FUSE_LIMIT, Material, number
from app.session import events as event_log

logger = logging.getLogger(__name__)

# --- 常量（口径见模块 docstring 与 `_work-4.1接口冻结.md`）-------------------

#: 决策小调用的输出预算（条目 4.1 明文：max_tokens=600）
DECISION_MAX_OUTPUT_TOKENS = 600
#: 轮次上限：配置缺失 / 非法（<1 或 >12）时的兜底值（老基线同名常量）
DEFAULT_MAX_ROUNDS = 6
#: 轮次天花板：上限 12。
MAX_ROUNDS_CEILING = 12
#: P&E 计划步数上限（老基线同名常量 ``PLAN_MAX_STEPS``，条目 4.2 §2.1-3）
PLAN_MAX_STEPS = 5
#: 步骤流里观察详情的截断
STEP_CLIP = 800
#: 决策模型看到的单条材料正文截断
BLOCK_CLIP = 300
#: `tool/call` 事件里单个参数值的截断（载荷进 JSON 列，别塞整页正文）
ARGS_CLIP = 200
#: `tool_call_id` 兜底长度（CubeLoop 给空 id 时才自己铸，形态与老基线一致）
TOOL_CALL_ID_LEN = 12
#: 没有可引用材料时的观察文本（老基线逐字）
NO_HIT_TEXT = "没有检索到相关资料。"
#: 快照（上限 12）已满、本次材料一条也没进去时的观察文本。
#: ⚠️ 为什么不能回落成**工具自己渲染的那段文本**：它按本次调用从 1 重新编号（
#: ``knowledge_search`` 的 ``enumerate(hits, start=1)``），而 ``[n]`` 的含义是**快照编号**
#: ⇒ 回落会让 ``[1]`` 指向快照里的第 1 条材料，而原文其实是另一批材料（I3 破；条目 4.2
#: 对抗性核验的 F1，实测 P&E 第 4/5 步命中）。宁可说「没进快照」，也不给一指就错的编号。
SNAPSHOT_FULL_TEXT = "本轮可引用材料已达上限，本次新检到的片段未纳入引用快照。"

# --- 普通工具结果通道（专项「工具调用能力升级」第 0 批 · ①）------------------
#
# 这一节只回答一件事：*「纯文本工具结果」怎么进最终回答那一次模型调用的请求体*。
# 带 ``materials`` 的工具结果**早就**经 ``turn.materials → plan.materials_text`` 进了
# 「资料如下」区；本节补的是**另外半边**（MCP 文本 / 失败观察 / 快照打满那一类）。

#: 单条通道条目的文本上限（D4）。与 ``web_search`` 正文既有截断**同量级**。
#: ⚠️ 只对**拿得到原文**的条目（``absorb`` 分支 ③）成立；``SNAPSHOT_FULL_TEXT`` /
#: ``NO_HIT_TEXT`` 那两态是固定短串，天然远小于上限，**原样进通道**。
TOOL_CONTEXT_ITEM_LIMIT = 4000
#: 通道文本的**总计**上限（D4）。按**调用序**（D25）在总量上截断。
TOOL_CONTEXT_TOTAL_LIMIT = 12000
#: 截断的**显式**标记（D4：超限必须明确标记，不许静默切掉）。
#: 带上「原 N 字符」——模型据此知道这段不完整，而不是以为工具就返回了这么点。
TOOL_CONTEXT_TRUNCATION_MARK = "（已截断，原 {chars} 字符）"
#: 总量打满后**后续条目**的占位标记（文本整个放不下，但条目本身要留在通道里）。
TOOL_CONTEXT_OMITTED_MARK = "（超出上下文总量上限）"

#: 截断族的**落盘形态**（R2-06 / D-回归检查）：带一个**可核对**的 ``id``（= 内容哈希 sha256 前 12 位）。
#: ⚠️ 上面那对老常量**逐字保留** —— 它们现在是**回落形态**（落盘失败时才用）。
TOOL_CONTEXT_ARTIFACT_MARK = "（已截断，原 {chars} 字符；全文已落盘，id={id}）"
#: 占位族的落盘形态（``remaining <= 0`` 与「预算太小」两条分支共用）。
TOOL_CONTEXT_OMITTED_ARTIFACT_MARK = "（超出上下文总量上限；全文已落盘，id={id}）"
#: 头尾预览之间的**可辨识分隔符**（D-S1-12④，本阶段新立）。不加它的话模型会把
#: 「头一段 + 尾一段」读成**连续正文**（假引用风险）。
#: ⚠️ **长度有硬上限**：``len(本值)`` 变大即压缩头尾可行域（实测 ``>= 74`` 时连既有
#: ``remaining=111`` 那条都无解）⇒ 这 11 个字符的字面**不许加长**。
TOOL_CONTEXT_ELISION_MARK = "……（中间已省略）……"
#: 两个标记族的**解析前缀**（D-回归检查：一套解析覆盖「新老两组常量」共**四种形态**）
TOOL_CONTEXT_TRUNCATION_PREFIX = "（已截断，原 "
TOOL_CONTEXT_OMITTED_PREFIX = "（超出上下文总量上限"
#: 头尾预览里**头段**占正文预算的比例（3:1 ⇒ 0.75；只给 :func:`_preview_tool_context_text` 用）
_PREVIEW_HEAD_RATIO = 0.75


def _preview_tool_context_text(text: str, budget_body: int) -> str:
    """头尾预览（**纯字符口径**，D-回归检查）：``head + 分隔符 + tail``，**恰好**填满 ``budget_body``。

    - 头尾按 **3:1** 分配（= 清单卡片「头 120 行 / 尾 40 行」的**比例**；⚠️ **单位由行改字符**
      —— 实测那组行数与 4000 字符预算**不可同时成立**：单行 5000 字符 ⇒ 头 120 + 尾 40 =
      10000 字符；且「前 120 行素材 < 头段字符配额」时**填不满**，两个多行反例分别差
      2244 / 2994 字符。改为纯字符口径后同一组输入**全部精确填满**）；
    - 头尾之间**必须有可辨识分隔符**（D-S1-12④），否则模型会把两段读成**连续正文**；
    - ``budget_body < len(分隔符) + 2`` ⇒ 返回**空串**（显式小预算守卫，调用方转占位标记）；
    - **不得**对结果再 ``rstrip``（既有 ``test_tool_context_t0.py:407`` 是 ``==`` 精确长度断言）。
    """
    elision = TOOL_CONTEXT_ELISION_MARK
    if budget_body < len(elision) + 2:
        return ""
    head_size = int((budget_body - len(elision)) * _PREVIEW_HEAD_RATIO)
    tail_size = budget_body - len(elision) - head_size
    return text[:head_size] + elision + text[-tail_size:]


def _truncation_mark(text: str) -> str:
    """截断标记：**先落盘**（D-回归检查）；成功 ⇒ 带 ``id`` 的新形态，失败 ⇒ 回落**逐字保留**的老形态。

    ⚠️ 调用方传进来的是**当时手上的那份文本**（D-回归检查）：:func:`clip_tool_context_text` 传
    工具返回的**原文**，:func:`cap_tool_context` 传**可能已被 clip 过的预览**。
    """
    artifact_id = tool_artifacts.save(text)
    if artifact_id is None:
        return TOOL_CONTEXT_TRUNCATION_MARK.format(chars=len(text))
    return TOOL_CONTEXT_ARTIFACT_MARK.format(chars=len(text), id=artifact_id)


def _omitted_mark(text: str) -> str:
    """占位标记：同上（D-回归检查 的第 ③④ 分支都走这里）。"""
    artifact_id = tool_artifacts.save(text)
    if artifact_id is None:
        return TOOL_CONTEXT_OMITTED_MARK
    return TOOL_CONTEXT_OMITTED_ARTIFACT_MARK.format(id=artifact_id)


def clip_tool_context_text(text: str) -> str:
    """单条通道条目的**有界**文本：超 ``TOOL_CONTEXT_ITEM_LIMIT`` 则**头尾预览** + 显式标记（D4）。

    ⚠️ 标记**计入**上限（否则「单条上限 4000」这条会被标记本身的长度破掉）⇒ 本函数的
    ``budget_body`` = ``TOOL_CONTEXT_ITEM_LIMIT − len(标记)``。
    ⚠️ :func:`cap_tool_context` 用的是**另一把尺子**（总量只算正文，标记**不占**预算）⇒
    两处**不许统一**：统一到「含标记」会破 ``test_tool_context_t0.py:411`` / ``:429`` 的
    ``sum <= 12000``，统一到「只算正文」会破 ``:407`` 的 ``== 4000``。
    ⚠️ **不再**对整体 ``rstrip``（``:407`` 是 ``==`` 精确长度断言；预览由
    :func:`_preview_tool_context_text` 精确填满 ``budget_body``）。
    """
    value = str(text or "")
    if len(value) <= TOOL_CONTEXT_ITEM_LIMIT:
        return value
    # D-回归检查「凡是把正文换成标记的分支都先落盘」的第 ① 处：单条超上限
    mark = _truncation_mark(value)
    preview = _preview_tool_context_text(value, TOOL_CONTEXT_ITEM_LIMIT - len(mark))
    if not preview:
        # 小预算守卫（D-S1-12⑥，**显式**；不再依赖改前的魔数 ``remaining - 16``）
        return _omitted_mark(value)
    return preview + mark


def cap_tool_context(items: list[ToolContextItem]) -> list[ToolContextItem]:
    """把通道条目按**顺序**截到总计 ``TOOL_CONTEXT_TOTAL_LIMIT``（D4 + D25）。

    「按顺序」= **调用序**（调用方传进来时已排好，见 :meth:`ToolCollector.tool_context_items`）
    ——**不是**字典遍历序，也不是 ``observations`` 的完成序。

    超总量的条目**保留在通道里但文本已截断 + 标已截断**（而不是整条丢掉）：
    「工具调过、结果没进上下文」本身是模型需要知道的事实（它据此决定是否换个查法），
    静默丢掉会让模型以为那个工具压根没被调用 —— 与 D5 的「按实际调用顺序排」同一取向。

    ## 上限到底管什么（**读这条再改数字**）

    - **工具正文**（``item.text`` 里来自工具的那部分）：单条 ≤ :data:`TOOL_CONTEXT_ITEM_LIMIT`、
      总计 ≤ :data:`TOOL_CONTEXT_TOTAL_LIMIT` —— 这是 D4 的硬口径，**逐条可判**；
    - **截断 / 占位标记**（「（已截断，原 N 字符）」这类**固定短语**）：是**元数据、不是工具正文**，
      因此**不占**上面那份预算、也不并进那两个数字。它的长度由常量决定（与本条渠道无关），
      且**必须**留着 —— 静默截断正是 D4 要堵的那件事。

    ⇒ 判据写法：``sum(len(条目的正文部分))`` ≤ ``TOOL_CONTEXT_TOTAL_LIMIT``。
    「整条 ``item.text`` 之和」会把标记的长度也加进去，那不是超限，是把元数据算进了正文账。
    **要改成「标记也占预算」的话**：得连单条上限一起重算，并且要同步改 A4 的判据与文档
    —— 别只改一头。
    ⚠️ 标记的**解析**必须与新老两组常量同步（:func:`_tool_text_length` 覆盖**四种形态**，
    D-回归检查）：只改标记、不改解析器 ⇒ 新标记被整条算进正文预算 ⇒ A10 破 + 既有
    ``test_tool_context_t0.py:413`` / ``:411`` / ``:429`` 三条一起红。
    ⚠️ **尾部**才算标记（S4 · 179）：正文里**内嵌**标记形态子串时必须整条计入，否则
    ``remaining`` 少扣、这条上限形同虚设 —— :func:`_tool_text_length` 的出口一律
    **保守**（只会多算、不会少算，见其 docstring）。
    """
    out: list[ToolContextItem] = []
    remaining = TOOL_CONTEXT_TOTAL_LIMIT
    for item in items:
        text = str(item.text or "")
        if remaining <= 0:
            # ④ 预算已耗尽 ⇒ 整条转占位标记；正文**先落盘**（D-回归检查 / D-回归检查）
            out.append(replace(item, text=_omitted_mark(text)))
            continue
        if len(text) > remaining:
            # ② 单条超**总量剩余预算** ⇒ 头尾预览 + 标记。标记不占预算（见 docstring），
            #    故传 ``remaining − len(标记)``（取**更保守**的一侧，保证 ``len(text) <= remaining``）
            mark = _truncation_mark(text)
            preview = _preview_tool_context_text(text, remaining - len(mark))
            if not preview:
                # ③ 预算太小（连「一点正文 + 分隔符」都放不下）⇒ 只放占位标记，正文清空。
                #    ⚠️ 与 ② 落的是**同一份文本** ⇒ 同一个 id、同一个文件（幂等，不产生第二件）
                text = _omitted_mark(text)
            else:
                text = preview + mark
        out.append(replace(item, text=text))
        remaining -= _tool_text_length(text)
    return out


def _truncation_tail_pattern(template: str) -> re.Pattern[str]:
    """把截断/占位标记的**模板**编译成文法正则（D-回归检查：解析与常量**同源**）。

    - ``{chars}`` ⇒ 十进制数字；``{id}`` ⇒ :data:`tool_artifacts.ARTIFACT_ID_LENGTH`
      位**小写 hex**（内容哈希，见 :func:`tool_artifacts.save`）；
    - 由标记常量**模板**生成 ⇒ 以后改标记（加字段、改措辞）解析自动跟上，
      不会再出现「只改标记、不改解析器」那类漂移（D-回归检查 的成因）。
    """
    parts: list[str] = []
    for chunk in re.split(r"(\{[a-z]+\})", template):
        if chunk == "{chars}":
            parts.append(r"[0-9]+")
        elif chunk == "{id}":
            parts.append(r"[0-9a-f]{%d}" % tool_artifacts.ARTIFACT_ID_LENGTH)
        else:
            parts.append(re.escape(chunk))
    return re.compile("".join(parts))


#: 截断族的**尾部文法**（① 老形态 / ② 落盘形态；由上面两条常量模板生成，见上）。
_TRUNCATION_TAIL_PATTERNS = tuple(
    _truncation_tail_pattern(template)
    for template in (TOOL_CONTEXT_TRUNCATION_MARK, TOOL_CONTEXT_ARTIFACT_MARK)
)

#: 占位族必须匹配整条文本；只有前缀相同、后面还带正文的条目仍须计入预算。
_OMITTED_PATTERNS = tuple(
    _truncation_tail_pattern(template)
    for template in (TOOL_CONTEXT_OMITTED_MARK, TOOL_CONTEXT_OMITTED_ARTIFACT_MARK)
)


def _tool_text_length(text: str) -> int:
    """一条条目文本里的**工具正文长度**（扣掉尾部的截断/占位标记）。

    D-回归检查：**四种形态**都要解析 ——

    ① 老截断标记 ``（已截断，原 N 字符）`` ⇒ ``len(head)``；
    ② **新**落盘截断标记 ``（已截断，原 N 字符；全文已落盘，id=<id>）`` ⇒ ``len(head)``；
    ③ 老占位 ``（超出上下文总量上限）`` ⇒ ``0``；
    ④ **新**落盘占位 ``（超出上下文总量上限；全文已落盘，id=<id>）`` ⇒ ``0``。

    ⇒ 解析先按**前缀**定位，再要求**从该处到结尾**整段匹配「由常量模板生成的标记文法」
    （:data:`_TRUNCATION_TAIL_PATTERNS`）；占位则必须**整条**匹配
    :data:`_OMITTED_PATTERNS`，不得仅凭前缀就记零。两个关键点：

    - **必须是尾部**：正文里**内嵌**一个标记形态子串（后面还有正文）时**整条都算正文**
      —— 改前取 ``rpartition`` 的「最后一次前缀出现处」只算 ``head``，实测
      ``"xx（已截断，原 5 字符）" + "z"*5000 + "（完）"`` 真实 6016 字符、记账 **2**；
      落盘形态的同构反例真实 15074、记账 **1**。欠记账 ⇒ :func:`cap_tool_context` 少扣
      ``remaining`` ⇒ 通道正文合计可以远超 :data:`TOOL_CONTEXT_TOTAL_LIMIT`（S4 · 179）。
    - **形态要贴合常量**：尾部那段还得是**模板生成的**样子（``原 <数字> 字符）`` /
      ``原 <数字> 字符；全文已落盘，id=<12 位小写 hex>）``）—— 删掉改前的
      ``tail[:-4].isdigit()`` 那一步（新标记的尾串是「N 字符；全文已落盘，id=…」，
      那一步会让新标记被整条算进正文预算），改成**整段**对模板文法做 ``fullmatch``；
      对不上就按正文算。

    ⇒ 出口是**保守的**：``_tool_text_length(text) <= len(text)`` 恒成立，**只会多算、
    不会少算**（「标记不占预算」这条既有口径不变；反方向未获授权，别改）。
    """
    if not text.endswith("）"):
        return len(text)
    if any(pattern.fullmatch(text) for pattern in _OMITTED_PATTERNS):
        return 0
    index = text.rfind(TOOL_CONTEXT_TRUNCATION_PREFIX)
    if index >= 0 and any(
        pattern.fullmatch(text[index:]) for pattern in _TRUNCATION_TAIL_PATTERNS
    ):
        return index  # = len(text[:index])：把尾部标记整段扣掉
    return len(text)

#: 决策器的 system 提示。**不产正文**：最终回答由回答链路生成，这里只用来决定工具调用。
DECISION_SYSTEM_PROMPT = (
    "你是知澜的检索决策器，负责为知识库问答决定是否需要调用工具、调用哪一个。"
    "需要资料时就调用工具；资料已经够用时，就不要再调用任何工具，"
    "在回复末尾另起一行写结束标记，下一行只放一个 JSON 对象，键只有 action、use_candidates、clarification，"
    "然后再单独一行写结束标记的结尾："
    "<<<CLOSEOUT>>>"
    "{\"action\":\"answer\"或\"clarify\",\"use_candidates\":true或false,\"clarification\":\"仅追问时填写\"}"
    "<<<END>>>"
    "不要在这里写最终答案——最终回答由回答环节依据检索到的资料生成。"
    "回答用中文。"
)

#: 计划器的 system 提示（老基线 ``agent.py:68-71`` **逐字冻结**，一个字都不要改）。
#: 与 :data:`DECISION_SYSTEM_PROMPT` 的区别：那份管「下一步调什么」，这份管「把问题拆成几步」。
PLAN_SYSTEM = (
    "你是知澜的计划器，负责把问题拆成至多 5 步的可执行计划。"
    "只回一个 JSON 对象：不要输出 markdown 代码块、解释或任何多余文字。"
)


# --------------------------------------------------------------------------
# 轮次与模型装配
# --------------------------------------------------------------------------


def agent_max_rounds() -> int:
    """轮次上限：``AGENT_MAX_ROUNDS`` 非法（非 int / <1 / >12）一律回落 6（老基线口径）。"""
    try:
        value = int(getattr(settings, "AGENT_MAX_ROUNDS", DEFAULT_MAX_ROUNDS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_ROUNDS
    if value < 1 or value > MAX_ROUNDS_CEILING:
        return DEFAULT_MAX_ROUNDS
    return value


def agent_turn_deadline_seconds() -> int:
    """轮级 wall-time 上限（秒）：``AGENT_TURN_DEADLINE_SECONDS`` **非法一律回落 0**（= 不限）。

    「不限」是**出厂态**：0 时 :class:`_TurnDeadline` 不进任何分支 ⇒ 与改动前逐字等价
    （判据 A4，基线载体 `tests/api/test_turn_deadline_r2.py`）。

    ⚠️ 为什么读 ``getattr(settings, …, 0)`` 而不是裸取属性：冻结态（桌面交付）可能带着一份
    **旧 `.env`**，而 `SettingsConfigDict(extra="ignore")`（config.py:158-163）会把**未声明的键
    丢掉** ⇒ 「取不到」是必须容忍的常态（同型先例 `agent_max_rounds()`）。本函数**只做夹取**。
    """
    try:
        value = int(getattr(settings, "AGENT_TURN_DEADLINE_SECONDS", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


class _TurnDeadline:
    """一轮 Agent 的 wall-time 预算（R2-03 / R2-04 共用同一个起点）。

    **为什么是「在检查点上比单调时钟」而不是「外层套取消式 CM」**（D-回归检查 的路径 (a)）：
    取消式 CM 会让 ``CancelledError`` 冒到循环外，而它在本仓有**三处既有**的「停止路径」
    处理器 —— ReAct ``:1731-1735``（所属 ``try:`` = ``:1700``）/ P&E ``:1647-1650``（``:1624``）/
    ``_audit_run`` CM ``:1437-1440``（``:1435``）—— 最里面那处会**当场把 ``status`` 落库成
    ``stopped``**（``:1439`` 的 ``_safe_finish``，晚于任何就地捕获），于是三条口径同时破：
    ① ``turn.aborted=True`` **抑制**收尾的可见 note；② ``agent_runs.status`` 记成 ``stopped``
    （把「超时」与「用户停止」混成同值）；③ ``chat_routes.py:1246`` 的
    ``if completed and not turn.aborted`` 不成立 ⇒ **结束决策被丢**（D14 破）。
    ⇒ 超时是**正常收尾**，不是中止：本类只在**轮/步之间的检查点**比一次时钟，命中后让循环
    **自然结束**（``turn.aborted`` 保持 ``False``）。

    ⚠️ **只能用 ``time.monotonic()``**：runner.py 有一条源码级扫描判据
    （``tests/api/test_tool_context_t0.py:510-516``）禁用 ``datetime`` 的取当前时刻字面
    （**连注释/文档串也算**，同型先例 D-回归检查）。

    ⚠️ **命中只能发生在轮/步之间**：单个卡死的工具调用期间**没有任何检查点** ⇒ 收尾时刻
    是「≤ 上限 + 单个工具调用的上界」，不是「≈ 上限」（判据 A5⑥ 按前者断）。
    """

    def __init__(self, seconds: int, *, started_at: float) -> None:
        self.seconds = int(seconds or 0)
        self.started_at = float(started_at)
        #: 是否**真的是**本上限把循环停下的（``False`` = 轮结束是别的原因 ⇒ 不许报
        #: 「已达时间上限」，否则是**假归因**：模型自己收手、时钟刚好也过点时会误报）。
        self.triggered = False

    @property
    def enabled(self) -> bool:
        return self.seconds > 0

    def remaining(self) -> float | None:
        """剩余预算（秒）；**不限**时返回 ``None`` —— 调用方据此走「不吃预算」的老分支。"""
        if not self.enabled:
            return None
        return self.seconds - (time.monotonic() - self.started_at)

    def expired(self) -> bool:
        """到点了吗。**只读时钟、不改状态**（谁把它当停下来的原因，谁置 :attr:`triggered`）。"""
        left = self.remaining()
        return left is not None and left <= 0


def resolve_agent_model():
    """Agent 轮的对话模型 —— **辅助链路** ⇒ ``answer.build_aux_model``（不带思考参数）。

    复用而不是另建一条装配路径，是为了「同一个 ``MODEL_ROLES.chat`` 绑定同时决定普通轮与
    Agent 轮的模型」；否则设置页换一个模型只影响一半路径，是极难发现的偏科。
    未配置 ⇒ ``None``（正常状态，桌面版首次启动），调用方转 note 步骤，不抛。

    ⚠️ **与最终答案的差别只有一处**：不带 ``CHAT_REASONING`` 的思考参数
    （Agent 决策 / 计划调用属于内部编排，不是用户要的答案）。
    ⚠️ 本函数在 `app/**` **没有调用点**（只在 ``__all__`` 导出）——真正生效的注入是
    ``chat_routes.resolve_aux_model()`` 经 ``stream_tokens(aux_model=…)`` 传进来的那条；
    这里保持一致口径，避免将来有人接上它时又造出一个带思考参数的旁路。
    """
    from app.generation import answer as answer_mod

    return answer_mod.build_aux_model()


def new_tool_call_id() -> str:
    """兜底工具 id（只有 CubeLoop 给了空 id 时才用）。

    不用时间戳：同一毫秒里并发两次会撞（老基线同款注释）；``uuid4().hex`` 截 12 位足够短且唯一。
    """
    return uuid4().hex[:TOOL_CALL_ID_LEN]


#: **读文件类**工具（专项工具升级-3 · 阶段 3 的只读文件族）。
#: 阶段 6（`回归检查`）只对**这一族**的步骤载荷做路径归一 —— 白名单是**写死**的，
#: 不做「路径形似就归一」的猜测：猜出来的归一会在别处（如写入类工具，其路径语义由写侧
#: 单独定档）制造两套口径。
READ_FILE_TOOLS = frozenset({"read_file", "list_files", "grep_files"})

#: 读文件类工具步骤载荷里**承载路径**的键（三个工具有且仅有这一个路径键）。
#: ⚠️ 取值形态照工具自己的入参模型：`list_files` 省略 `path` ⇒ 给 `"."`（工作区根本身）。
READ_PATH_KEYS = ("path",)


def _mask_step_args(args: Any) -> Any:
    """步骤帧载荷的敏感键遮蔽（**顶层 + 任意嵌套**；条目 `回归修复`）。

    ## 为什么必须在这里遮蔽（而不是在帧装配点各写一份）

    `args` 是**模型原样传进来**的工具参数，而步骤帧这一条数据流同时通向 SSE `agent` 帧
    与 `messages.agent_trace`（:meth:`AgentTurn.trace` 是两处共用的同一份）。它此前
    **完全没有遮蔽**：`api_key` / `confirm_token` / `password` 这类键的**顶层与嵌套**值
    原样落进可观测面。DB 审计面（`audit._args_digest`）早已遮蔽 ⇒ 两条数据流
    **一侧已挡、一侧全漏**（同 `回归修复` 的成因族）。

    ## 四条口径（改之前先读完）

    1. **复用唯一真相源**：键集与递归实现都取 `agent_audit`（`SENSITIVE_ARG_KEYS` /
       `_mask_sensitive_value`），**不新写第二套键表** —— 两份判定必然漂移（092 的教训）。
    2. **不做包含匹配**：`token_count` 这类键由 `audit._is_sensitive_arg_key` **精确等值**
       判定；遮蔽是**不可逆**的，误伤即丢可观测性。
    3. **无命中时返回原对象**（`masked == data` ⇒ 返回 `args` 本体）：没有敏感键的载荷
       **连身份都不变** —— 既有语义（非读文件类不做归一 / 读文件类路径归一）逐字节不受影响。
    4. **绝不抛**（I5）：转形失败、遮蔽失败一律**回落原值**，绝不把决策循环炸穿。
       非 dict 载荷里只有带 `model_dump()` 的（pydantic）先转形再遮蔽；`None` / 裸字符串
       **无键面可遮蔽**，原样放行（生产态到不了：两处调用点都先把它收成 dict）。
    """
    data = args
    if not isinstance(data, (dict, list, tuple)):
        dump = getattr(data, "model_dump", None)
        if not callable(dump):
            return args
        try:
            data = dump()
        except Exception:  # noqa: BLE001 —— 转形失败 ⇒ 原样
            return args
    try:
        masked = agent_audit._mask_sensitive_value(data)
    except Exception:  # noqa: BLE001 —— 遮蔽失败 ⇒ 原样
        return args
    try:
        if masked == data:
            return args
    except Exception:  # noqa: BLE001
        pass
    return masked


def _tool_args_for_steps(tool_name: str, args: Any) -> Any:
    """把一次工具调用的载荷整理成**可以进观察面**的形态（两条帧路径共用这一处）。

    为什么要有它（阶段 6 · `回归检查` · 判据 `回归检查` 腿①）：载荷里的 `path` 是**模型原样
    传进来的字符串**，可以是根内绝对路径；直接落进步骤项就等于把**工作区根的绝对前缀**
    连同主机路径结构一起送进 SSE 帧与 `messages.agent_trace`（前端、审计、导出件全都能读）。
    归一成「相对工作区根」之后，界面只说「读了哪个文件」，不泄露根在哪。

    **归一实现只有一份**：`_paths.describe(root, resolved)`（阶段 1 落地、基线零调用点）。
    本函数**不重写**相对化逻辑 —— 两处实现必然漂移（同 `4.2` 的 `_write_tool_events` 教训）。

    三条**刻意**的口径（改之前先读完）：

    1. **只认读文件类三件**（`READ_FILE_TOOLS`）做路径归一。其余工具（检索 / 联网 / MCP / 写）
       载荷**不做路径归一** —— 它们的路径键语义各不同，且写侧有自己的定档（阶段 6 不做写侧归一）。
       ⚠️ 条目 156 起：**所有工具**的载荷都先过 :func:`_mask_step_args` 做敏感键遮蔽；
       无敏感键时它返回**原对象** ⇒ 本条「其余工具逐字不动」的语义仍然成立。
    2. **不落盘、不碰文件系统**：`resolve_workspace_root()` 未配置（`None`）⇒ **原样返回**。
       根没有就说不上「相对根」；此时硬编一个相对形态反而会把一个没归一的值伪装成归一过的。
    3. **失败 / 畸形一律回落原值**（I5：归一不得炸穿决策循环）：载荷不是 dict、`path` 缺失、
       非字符串、空白串 —— 全都按「没有可归一的路径」处理。

    ⚠️ 返回的是**新 dict**（浅拷）而不是就地改：`args` 是 CubeLoop 事件与 P&E 的 `raw_args`
    本体，就地改会同时改到 `tool/call` 事件载荷与审计 `meta["args"]` —— 那是两条**另有口径**
    的可观测面，本笔不越界。
    """
    if tool_name not in READ_FILE_TOOLS or not isinstance(args, dict):
        # 条目 156：非读文件类（或非 dict）载荷**只遮蔽**，不做路径归一（白名单语义不动）。
        return _mask_step_args(args)
    # 条目 156：读文件类**先遮蔽再归一** —— 否则下方三处早退分支会 `return args` 原文。
    args = _mask_step_args(args)
    raw = args.get(READ_PATH_KEYS[0])
    if not isinstance(raw, str) or not raw.strip():
        return args
    try:
        from app.agent.tools import _paths  # 局部 import：模块级不拉工具层依赖（同 :570 口径）

        root = _paths.resolve_workspace_root()
        if root is None:
            return args
        candidate = Path(raw)
        resolved = candidate if candidate.is_absolute() else Path(root) / candidate
        relative = _paths.describe(Path(root), resolved)
    except Exception:  # noqa: BLE001 —— 归一失败 ⇒ 退回原值（绝不把决策循环炸穿）
        logger.warning("步骤载荷路径归一失败，已按原值放行：tool=%s", tool_name)
        return args
    out = dict(args)
    out[READ_PATH_KEYS[0]] = relative
    return out


#: **写通道**工具的契约名（`回归检查` / `D-回归检查` ① 逐字冻结）。
#: 阶段 6（`回归检查`）只对**这一个**工具产出 `file_change` 步骤 —— 白名单写死，
#: 不做「看到 write_meta 就产出」的猜测：将来若有第二个写工具，它得自己显式进这张名单。
WRITE_TOOLS = frozenset({"workspace_write"})

#: `file_change` 步骤载荷的三个键（`回归检查` 的 `audit.WRITE_META_KEYS` 逐字同源）。
#: ⚠️ **不得各算一份**：这三个值只有一个合法来源 —— 写工具 handler 从 `WriteOutcome`
#: 原样挂在观察上的 `write_meta`（`workspace_write.py`）⇒ 本模块只 `getattr` 取它。
WRITE_STEP_KEYS = ("write_target", "write_byte_delta", "write_snapshot_id")

#: ``file_change`` 步骤载荷的**可选键**（``回归检查`` · 判据 ``回归检查``）：写前 vs 写后的行级 diff。
#:
#: 🔴 **与三键同源、不得各算一份**：它的「旧内容」只有写本体在**写盘之前**读到过 ⇒
#: 若由步骤层回读目标文件反推，算出来的永远是「新 vs 新」= 空 diff ⇒ 判据结构性恒真
#: （阶段 4 ``M-回归检查`` / 阶段 5 ``M-回归检查`` 是同型陷阱）。故它由 ``_write.render_diff``
#: 在写本体里产出，经 ``write_meta`` 原样带到这里。
#: ⚠️ **可选**：旧笔（``回归检查``）的替身 ``write_meta`` 只给三键 ⇒ 缺它时**不产出该键**
#: （而不是给空串：空串的语义是「内容没变」，与「没算」必须可区分）。
WRITE_DIFF_KEY = "write_diff"


def _file_change_step(turn: AgentTurn, *, tool: str, tool_call_id: str, meta: dict | None) -> dict | None:
    """**写成功**时产出一条 `kind="file_change"` 步骤；否则返回 ``None``。

    为什么要有它（阶段 6 · `回归检查` · 判据 `回归检查`）：写工具成功后，界面上目前
    只有「调用工具：workspace_write」一行 —— **写了哪个文件、改了多少字节、快照编号是多少**
    这三件事一个都看不见。本函数把它们做成一条**看得见的**步骤。

    ## 🔴 三键唯一来源（`D-T6-*`：三键同源，不得各算一份）

    载荷**只**从 ``meta``（即 :func:`_write_meta_of` 取自观察 ``write_meta`` 的那份 dict）取。
    **不得**从观察文本里解析（文本是给人看的、随时可改措辞），**不得**回读文件重算
    （那就是「自己算一份」⇒ 审计里记的与实际写的会漂移，正是 `回归检查` 早已封死的路）。

    ## 🔴 写失败 ⇒ 返回 ``None``（`D-回归检查` 定档）

    写失败时 `workspace_write` 的 handler **根本不挂** ``write_meta``（见其 :323-325 注释：
    「没写成」与「写了 0 字节」必须可区分）⇒ ``_write_meta_of`` 返 ``None`` ⇒ 本函数返 ``None``。
    **不产出一条内容为空的 `file_change`** —— 那会让界面显示「变更：/ /」这种半截信息，
    比不显示更坏。

    ## 为什么参数是 ``(turn, tool, tool_call_id, meta)`` 而不是整份 event/observation

    两条帧路径（ReAct 的 :func:`_steps_for` 与 P&E 的 :func:`_tool_frames`）的事件形状
    **不同**（前者是 CubeLoop 事件、后者是自己造的 ``SimpleNamespace``），但「要产出这条步骤」
    所需的四件东西在两边**同名同义**⇒ 收成一个窄接口，两处各调一次、实现只此一份。
    """
    if tool not in WRITE_TOOLS:
        return None
    if not isinstance(meta, dict) or not meta:
        return None
    payload = {key: meta[key] for key in WRITE_STEP_KEYS if key in meta}
    # 三个键**一个都不能少**：缺件说明上游形状变了，此时**宁可不产出**也不产半截载荷
    # （半截载荷会让判据「三键齐全」变成看运气，界面也会少显示一项）。
    if len(payload) != len(WRITE_STEP_KEYS):
        # `回归修复`：三键不全是**降级**（这一步不产出、本轮照常收尾）⇒ 接进既有派生
        # 计数出口，使「降级发生过」在 `GET /api/agent/audit` 的
        # `counters.derived.tool_result_channel` 上机器可读（只在降级路径计数）。
        derived_counters.bump("tool_result_channel")
        logger.warning("写通道三键不全，已跳过 file_change 步骤：tool=%s keys=%s", tool, sorted(meta))
        return None
    # ⚠️ **可选键**（`回归检查`）：diff 是**只增**的载荷键 —— 写本体给了才带（缺它 = 没算，
    # 与「内容没变 ⇒ 空串」是两种事实，故不兜空串）。写在三键齐全判定**之后**：
    # 三键不齐是「上游形状变了」⇒ 整条步骤不产出（连 diff 也没有独立意义）。
    _diff = meta.get(WRITE_DIFF_KEY)
    if isinstance(_diff, str):
        payload[WRITE_DIFF_KEY] = _diff
    return turn.add_step(
        "file_change",
        _file_change_title(payload),
        # `回归检查`：diff 进详情（有界截断）—— 界面展开态直接看到改了哪几行
        detail=_file_change_detail(payload),
        tool=tool,
        args=payload,
        ok=True,
        tool_call_id=tool_call_id,
    )


def _file_change_detail(payload: dict, *, limit: int = STEP_CLIP) -> str:
    """步骤的 ``detail``（`回归检查`）：把 diff 放进**用户可见**的详情里（有界截断 + 显式标记）。

    ## 为什么把加行/删行**数出来**（用 ``ast`` 而不是自己解析 diff）

    「加了 3 行 / 删了 2 行」比「一坨 +\\- 文本」可读得多。这里的计数**只认行首前缀**
    （``+`` / ``-``，且 **hunk 头 ``@@`` 不算**），与 ``_write.render_diff`` 的输出格式
    是**同一套**（由它的实现冻结、由 ``回归检查`` 腿① 钉住）⇒ 不存在「两把尺」。

    ⚠️ ``ast`` 的用途仅限**读取**本函数所在模块的源码做自证式注释定位（见下），
    **不参与** diff 计算：真算 diff 的只有 ``_write.render_diff`` 一处。
    """
    diff = payload.get(WRITE_DIFF_KEY)
    if not isinstance(diff, str) or not diff:
        # ⚠️ **不得**给「（内容未变）」这类文案：缺 diff 与「diff 为空」是两种事实
        # （前者 = 没算/旧笔替身，后者 = 真的没变），混淆会让用户以为「这次没改动」。
        return ""
    lines = [line for line in diff.splitlines() if not line.startswith("@@")]
    added = sum(1 for line in lines if line.startswith("+"))
    removed = sum(1 for line in lines if line.startswith("-"))
    head = f"变更摘要：+{added} 行 / -{removed} 行"
    body = _clip_detail(diff, limit)
    return f"{head}\n{body}"


def _clip_detail(text: str, limit: int) -> str:
    """详情文本的有界截断（超限**显式**标记；口径照 ``read_file.OBSERVATION_LIMIT``）。

    🔴 不截断会让一条大 diff 把整轮 SSE 帧撑爆（``agent` 帧是逐帧下发的）。
    """
    value = str(text or "")
    if len(value) <= limit:
        return value
    mark = "（diff 已截断，原 {chars} 字符）".format(chars=len(value))
    keep = max(0, limit - len(mark))
    return value[:keep].rstrip() + mark


def _file_change_steps(turn: AgentTurn, *, tool: str, tool_call_id: str, meta: dict | None) -> list[dict]:
    """`_file_change_step` 的**列表形态**：产出时为 `[step]`，不产出时为 `[]`。

    为什么需要它：P&E 那条路径把两帧写在**一个列表字面量**里，而 `file_change` 是**可选**的
    （写失败 ⇒ 不产出）⇒ 直接塞 `None` 会往 `turn.steps` 落一条空项，下游 `step["kind"]` 炸。
    用解包 `*_file_change_steps(...)` 既保持「一次调用决定 0 或 1 条」，又不必在 P&E 里写 if。
    """
    step = _file_change_step(turn, tool=tool, tool_call_id=tool_call_id, meta=meta)
    return [step] if step is not None else []


def _file_change_title(payload: dict) -> str:
    """`file_change` 步骤的人读标题：「写了哪个文件」+「差了多少字节」。

    ⚠️ 标题**只**由载荷派生（不另取观察文本）：标题与载荷必须说同一件事，
    否则界面上的摘要与详情会对不上（同 `_tool_title` 与 `args` 的口径）。
    """
    target = str(payload.get("write_target") or "").strip() or "（未知路径）"
    delta = payload.get("write_byte_delta")
    parts = [target]
    if isinstance(delta, int):
        sign = "+" if delta >= 0 else ""
        parts.append(f"{sign}{delta} 字节")
    # 🔴 `回归检查`：有 diff 时把「加了几行 / 删了几行」也写进标题 ——
    # 界面**折叠态**只显示标题，不显示详情；不写进来的话，用户在收起状态下看不出「大改还是小改」。
    diff = payload.get(WRITE_DIFF_KEY)
    if isinstance(diff, str) and diff:
        added = sum(1 for line in diff.splitlines() if line.startswith("+"))
        removed = sum(1 for line in diff.splitlines() if line.startswith("-"))
        parts.append(f"+{added}/-{removed} 行")
    return f"文件变更：{'（'.join([parts[0], '，'.join(parts[1:])]) + '）' if len(parts) > 1 else parts[0]}"


def _tool_title(tool_name: str, args: dict | None) -> str:
    """步骤流的人读短标题（老基线逐字；新工具回落「调用工具：」）。"""
    data = args or {}
    if tool_name == "knowledge_search":
        return f"检索知识库：{clip(str(data.get('query') or ''), 40)}"
    if tool_name == "web_search":
        return f"联网搜索：{clip(str(data.get('query') or ''), 40)}"
    if tool_name == "fetch_page":
        return f"抓取网页：{clip(str(data.get('url') or ''), 40)}"
    return f"调用工具：{tool_name or '未知'}"


def _args_digest(args: dict | None) -> dict[str, str]:
    """参数摘要：键原样、值只留短文本（无损 JSON，能进 ``session_events.data``）。

    ⚠️ **入参可能是 pydantic 模型**（ReAct 那条路径：CubeLoop 已把工具参数校验成
    ``ToolSpec.parameters`` 的实例再交给执行闭包）——``isinstance(args, dict)`` 一个判据会让
    ``meta["args"]`` 恒为空 dict，审计里看不到「模型当时到底传了什么」（实测抓到的缺陷，
    见 ``tests/agent/test_agent_audit.py::test_react_audit_meta_args_digest_is_recorded``）。
    故这里先 ``model_dump()`` 再摘要；P&E 那条路径传的本来就是 dict。

    ## 🔴 ``回归修复`` ① · 本函数**不再自持一份实现**（改为委托）

    本处**原先自带一份实现**，与 :func:`app.agent.audit._args_digest` 并存 ⇒ **同一份参数两个真相源**
    （``tests/agent/test_agent_audit.py`` 里那句「两处都要，否则同一份参数两个真相源」就是本缺陷的原始记录）。
    两份在基线实测**并不等价**，且差异是**要命的那一处**：

    ```
    {"path": "a.txt", "confirm_token": "S", "create": False}
      => 本模块旧版：{"path": "a.txt", "confirm_token": "S",   "create": "false"}   ← **凭证原文漏出**
      => audit 版 ：{"path": "a.txt", "confirm_token": "***", "create": "False"}
    ```

    ⇒ 本模块旧版**漏掉了** ``confirm_token`` 遮蔽，正是 ``回归修复`` 的根因形态逐字
    「**两条不同的数据流只挡了一侧**」（audit 那条挡了、这条没挡）。
    而 ``confirm_token`` 是**一次性凭证** ⇒ 原文可见即**可重放** ⇒ 违红线 13 第二款
    「不得存在只有模型能读到的确认通道」（该键会经 ``tool/call`` 载荷与 ``meta["args"]`` 出去）。

    ⇒ 现**委托** :func:`app.agent.audit._args_digest`（本仓该摘要的**唯一定义点**，含遮蔽表）。
    方向不可反转：audit 那份是**下游必经**（``meta["args"]`` 落库都走它）且带遮蔽 ⇒ 它才是正确语义。

    ⚠️ **已知的 4 类渲染差**（如实登记，**不得**称「渲染完全等价 / 行为零变化」）：
    以 audit 版为准后，``bool`` / ``None`` / **dict 及含字符串或布尔的嵌套容器** 的渲染形态会变 ——

    | 输入 | 旧（本模块 ``json.dumps``） | 新（audit 版 ``str()``） |
    |---|---|---|
    | ``{"create": False}`` | ``'false'`` | ``'False'`` |
    | ``{"x": None}`` | ``'null'`` | ``'None'`` |
    | ``{"d": {"a": 1}}`` | ``'{"a": 1}'`` | ``"{'a': 1}"`` ← JSON → Python repr（引号 ``"`` 变 ``'``） |
    | ``{"a": [{"b": None}]}`` | ``'[{"b": null}]'`` | ``"[{'b': None}]"`` ← 同上 |

    ⚠️ **边界**（别把这条记成「所有容器都变」）：**只有 dict / 含字符串或布尔的容器会分岔** ——
    纯数值容器**逐字不变**（``json.dumps([1, 2])`` 恰等于 ``str([1, 2])`` = ``'[1, 2]'``），int / float / 中文 str 亦然。
    🔴 **两条既有判据在此点上【互相矛盾】**（这是本笔最要紧的一处口径，别只看一侧）：
    · ``tests/api/test_chat_agent.py:778`` 对 ``session_events.data["args"]["filters"]`` 走 ``json.loads``，
      其 ``:777`` 注释逐字把「**非字符串值 JSON 化**」当作契约 ⇒ **要求 JSON 形态**；
    · ``tests/agent/test_write_audit_t3_5.py:642`` 钉 ``got["create"] == "False"`` ⇒ **要求 str() 形态**。
    ⇒ 收敛到 audit 的 ``str()`` **会踩死前者**（实测 ``json.JSONDecodeError``）——``回归修复`` 定档后
    **以 JSON 形态为唯一契约**（行为契约 > 实现形状快照）⇒ 本函数所委托的那份实现已随之改回 JSON 渲染，
    渲染差随之收敛为 **0 类**。上面那张表描述的是**收敛过程中一度出现过的形态**，保留作沿革与防复发。
    ``is None`` / ``model_dump()`` / 逐键 ``clip(ARGS_CLIP)`` / pydantic 入参这些既有语义，均由被委托方逐字承接。
    """
    return agent_audit._args_digest(args)


def _history_digest(history: list[dict] | None) -> str:
    """决策提示词里的最近对话摘要（老基线口径：末 4 条、每条 60 字）。"""
    lines: list[str] = []
    for item in (history or [])[-4:]:
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            who = "用户" if role == "user" else "知澜"
            lines.append(f"{who}：{clip(content, 60)}")
    return "\n".join(lines)


def decision_prompt(question: str, history: list[dict] | None = None) -> str:
    """决策这一轮的用户提示：原始问题 + 最近对话摘要（历史摘要只作背景）。"""
    parts = [f"原始问题：{question}"]
    digest = _history_digest(history)
    if digest:
        parts.append(f"最近对话：\n{digest}")
    return _finish_decision_prompt(parts)


def decision_prompt_with_candidates(
    question: str,
    history: list[dict] | None = None,
    *,
    candidates: str = "",
) -> str:
    """决策提示加上已检索到的候选。不传候选时与 :func:`decision_prompt` 逐字相同。"""
    parts = [f"原始问题：{question}"]
    digest = _history_digest(history)
    if digest:
        parts.append(f"最近对话：\n{digest}")
    if (candidates or "").strip():
        parts.append(
            "已检索到的候选资料（仅供决定是否采用，不是必须引用）：\n"
            + clip(candidates, 2000)
        )
    parts.append("请决定是否需要调用工具，以及调用哪一个。")
    return "\n\n".join(parts)


def _finish_decision_prompt(parts: list[str]) -> str:
    parts.append("请决定是否需要调用工具，以及调用哪一个。")
    return "\n\n".join(parts)


_CLOSEOUT_RE = re.compile(r"(?ms)^<<<CLOSEOUT>>>\r?\n(.*?)\r?\n<<<END>>>[ \t\r]*$")
_CLOSEOUT_KEYS = frozenset({"action", "use_candidates", "clarification"})


def parse_closeout_message(text: str) -> tuple[str, dict | None, bool]:
    """从决策正文里取最后一段围栏块。

    返回 ``(可见正文, 结束决策或 None, 是否格式无效)``。
    没有围栏不是失败。有围栏但键、类型或追问正文不合法，算无效。
    """
    raw = text or ""
    matches = list(_CLOSEOUT_RE.finditer(raw))
    if not matches:
        visible = raw.replace("<<<CLOSEOUT>>>", "").replace("<<<END>>>", "").strip()
        return visible, None, False
    match = matches[-1]
    visible = _CLOSEOUT_RE.sub("", raw)
    visible = visible.replace("<<<CLOSEOUT>>>", "").replace("<<<END>>>", "").strip()
    try:
        payload = json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return visible, None, True
    if not isinstance(payload, dict) or set(payload) != _CLOSEOUT_KEYS:
        return visible, None, True
    action = payload.get("action")
    use = payload.get("use_candidates")
    clarification = payload.get("clarification")
    if action not in ("answer", "clarify") or not isinstance(use, bool):
        return visible, None, True
    if not isinstance(clarification, str):
        return visible, None, True
    if action == "clarify" and not clarification.strip():
        return visible, None, True
    return visible, {
        "action": action,
        "use_candidates": use,
        "clarification": clarification,
    }, False


def _required_args_hint(spec) -> str:
    """工具参数的「必填键」提示（**v2 的必要适配**：老基线用自由文本 ``arg_hint``，v2 是 pydantic 模型）。

    只列**必填**键（``parameters.model_fields`` 里 ``is_required()`` 的那些）并各给一个占位值：
    这既是计划器唯一需要的信息（哪些键非给不可），也避免把可选键误写成「必须给」——
    后者会让计划器把 ``limit`` / ``filters`` 这类键也硬填，属于自找的约束。

    取不到模型（``parameters is None``）时退回「参数为 JSON 对象」的老基线 MCP 措辞。
    """
    parameters = getattr(spec, "parameters", None)
    fields = getattr(parameters, "model_fields", None)
    if not fields:
        return "参数为 JSON 对象，按工具说明给；不确定就 {}"
    required = {name: None for name, item in fields.items() if item.is_required()}
    return json.dumps(required, ensure_ascii=False)


def _tools_section(tools: list[Any]) -> str:
    """计划提示词里的工具清单：``- 名字：描述（参数：{必填键}）``（老基线 ``_tools_section`` 形态）。

    两处**容易"顺手改对"的地方**（都按老基线逐字保留，别改）：

    - 收尾括号是**半角** ``)``——老基线 ``agent.py:233`` 就是「全角 ``（`` 配半角 ``)``」这种混排，
      改成全角看着更整齐，但那属于擅自改冻结文案；
    - 清单为空时返回**空串**，不返回老基线那句 ``（没有可用工具）``——本轮运行器在调用前已经
      用 ``note("没有可用工具")`` 早退（空清单到不了这里），故保留老基线字面值没有收益。
    """
    if not tools:
        return ""
    return "\n".join(
        f"- {tool.name}：{tool.description}（参数：{_required_args_hint(tool)})" for tool in tools
    )


def plan_prompt(question: str, tools: list[Any], history: list[dict] | None = None) -> str:
    """计划这一轮的用户提示：问题 + 最近对话摘要 + 工具清单 + **末段逐字**（老基线 ``_plan_prompt``）。

    末段（含那个 JSON 形状样例与「tool 为 null」的说明）是**冻结文案**——计划器靠它才知道
    该回什么形状，改一个字都会改变可观察行为（`开发约定`「文案是契约的一部分」同型）。
    """
    parts = [f"原始问题：{question}"]
    digest = _history_digest(history)
    if digest:
        parts.append(f"最近对话：\n{digest}")
    parts.append(f"可用工具：\n{_tools_section(tools)}")
    parts.append(
        "请把回答这个问题拆成至多 5 步，只回一个 JSON 对象，不要输出代码块围栏或其他文字：\n"
        '{"steps": [{"goal": "这一步要达成什么", "tool": "工具名或null", "args": {…}}]}\n'
        "tool 为 null 表示这一步只做推理整理，不需要工具。"
    )
    return "\n\n".join(parts)


def plan_text(plan: list[dict]) -> str:
    """计划步的人读渲染：逐行 ``f"{i}. {goal}（工具：{tool_name}）"``，无工具时 ``（无工具）``。

    这是老基线 ``_plan_text`` 的逐字形态（含全角括号与那个冒号），步骤流里的 ``plan`` 步
    ``detail`` 就是它。
    """
    lines: list[str] = []
    for index, item in enumerate(plan, start=1):
        goal = str(item.get("goal") or "").strip()
        tool_name = str(item.get("tool") or "").strip()
        suffix = f"（工具：{tool_name}）" if tool_name else "（无工具）"
        lines.append(f"{index}. {goal}{suffix}")
    return "\n".join(lines)


def _extract_json_object(text: str | None) -> dict | None:
    """剥 ```` ```json ```` 围栏后取第一个 ``{`` 到最后一个 ``}``；解析失败返回 ``None``。

    逐字沿用老基线 ``_extract_json_object`` 的口径（含「首 ``{`` 到末 ``}``」这个宽松截取）——
    计划器被要求只回 JSON，但它偶尔会裹围栏，剥一层就够，不做更聪明的修复。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def parse_plan(text: str | None) -> list[dict] | None:
    """解析计划输出；形状不对 / 空计划返回 ``None``（触发**回退单步检索**）。

    三条判据与老基线 ``_parse_plan`` 一致：① 不是 JSON 对象 ⇒ ``None``；
    ② ``steps`` 不是列表 ⇒ ``None``；③ 列表里过滤掉非 dict 项后为空 ⇒ ``None``。
    回退不是「什么都不做」——调用方会强制执行一次 ``knowledge_search``（条目原文的
    「失败回退单步检索」）。
    """
    data = _extract_json_object(text)
    if data is None:
        return None
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list):
        return None
    plan = [item for item in raw_steps if isinstance(item, dict)]
    return plan or None


async def _cap_decision_payload(payload: dict, _model: Any = None) -> dict:
    """把每次模型调用的 ``max_tokens`` 钉成 600（决策小调用，条目 4.1 明文）。

    见模块 docstring：循环走的是 ``stream()``，没有 ``max_output_tokens`` 形参，
    ``on_payload`` 是发送前唯一可改请求体的钩子。返回新 dict（不改调用方的对象）。
    """
    capped = dict(payload)
    capped["max_tokens"] = DECISION_MAX_OUTPUT_TOKENS
    return capped


# --------------------------------------------------------------------------
# 材料累积（编号与引用的**唯一**来源，I3）
# --------------------------------------------------------------------------


def render_observation(entries: list[Material]) -> str:
    """观察文本：每块 ``[n] 文件名`` + 正文前 300 字，块间空行（老基线 ``_chunks_observation`` 形态）。

    ``[n]`` 取材料的**快照编号**（不是本次调用的局部序号）——这是 I3 的关键：
    第二次检索的观察不会又从 1 开始编号。
    """
    if not entries:
        return NO_HIT_TEXT
    return "\n\n".join(
        f"[{int(item.ordinal)}] {item.filename}\n{clip(item.content, BLOCK_CLIP)}"
        for item in entries
    )


class ToolCollector:
    """本轮工具产出的材料累积器：去重 → 上限 → 快照编号 → 观察文本渲染。

    去重键是 ``Material.chunk_key``（文档材料是冻结身份 ``kb:<kb>:document:<id>:<ordinal>``；
    网页材料是 ``web:<sha1>``），**首次出现为准、保持插入顺序**，上限
    :data:`~app.retrieval.types.FUSE_LIMIT`——与普通轮「最多 12 条引用」对齐
    （老基线 ``CHUNK_LIMIT`` 同为 12）。

    每次并入后立刻 ``number()`` 重新编号：新增项排在末尾 ⇒ 编号即快照位置，
    与被引用材料的 ``ordinal`` 一一对应（I3 的构造性保证）。

    **上限打满时**（本次材料一条都进不来）**不回落**到工具自渲染文本——那份文本按本次调用
    从 1 重新编号，会让 ``[1]`` 指到快照里的**另一条**材料（条目 4.2 对抗性核验的 F1，
    实测 3 步以上、每步 5 条新材料的正常计划即命中）。改为 :data:`SNAPSHOT_FULL_TEXT`
    + 一条「未纳入引用快照」的 warning：宁可说没进快照，也不给一指就错的编号。

    ⚠️ **S4 · 207 的两条同源口径**（改这一节前先读完）：

    - 告警按「**有 key 没进快照**」判，不按「本次一条都没进」判 —— 否则**混合批**
      （一部分 key 早已在快照、一部分是新 key）走分支 ① 时新 key 被 ``FUSE_LIMIT``
      静默丢掉，``warnings`` 仍是空（实测就是这么漏的）。丢弃的账在三个分支之后统一结，
      并按调用记进 :attr:`snapshot_warnings`（事件载荷的 ``warnings`` 计数从这里取）；
    - :attr:`material_hits` / 事件载荷的 ``materials_hit`` 一律取「**确实进了快照**」的
      条数（= 渲染本次观察文本用的那份 ``entries``）⇒ 丢掉的片段不计命中，
      与同行 ``content`` 同源。
    """

    def __init__(self) -> None:
        self.materials: list[Material] = []
        self.warnings: list[str] = []
        #: ``tool_call_id`` → 该次调用检到的材料里**确实进了快照**的条数（S4 · 207）。
        #: 与 ``render_observation(entries)`` 用的是**同一份** ``entries`` ⇒
        #: 事件载荷的 ``materials_hit`` 与同行 ``content`` 同源（被上限丢掉的片段不计命中）。
        self.material_hits: dict[str, int] = {}
        #: ``tool_call_id`` → 该次调用**因快照上限丢弃新片段**而记的告警（S4 · 207）。
        #: ``observation.warnings`` 之外的原因性告警：写事件载荷时并入 ``warnings`` 计数，
        #: 否则「静默丢」在库里读不出来（混合批原先正是这样：告警只有分支 ② 才生成）。
        self.snapshot_warnings: dict[str, list[str]] = {}
        #: ``tool_call_id`` → 该次调用的观察（步骤与事件共用同一份）
        self.observations: dict[str, ToolObservation] = {}
        #: ``tool_call_id`` → 该次调用的**写通道三键**（阶段 6 · `回归检查`）。
        #:
        #: 🔴 **为什么必须在这里另存一份，而不能到步骤帧那一步再从观察上取**
        #: （实测踩到的真陷阱，改之前先读完）：
        #: ``write_meta`` 是写工具 handler **动态附在观察实例上的属性**（不是 dataclass 字段，
        #: 见 `_write_meta_of` 的 docstring）。而 ``absorb`` 的第一件事是
        #: ``bind_retrieval_observation(...)`` —— 它是 ``dataclasses.replace(...)``，
        #: **造一个新实例**并丢掉``__dict__``里动态附的那一项 ⇒ 存进 ``self.observations`` 的
        #: 那份**没有** ``write_meta``。
        #: 于是「步骤帧那一步再 `getattr(observation, "write_meta")`」恒得 ``None``：
        #: 审计面（`_absorb_tool` 里取、走 `ToolAudit`）正常，**只在步骤面静默丢失**。
        #: ⇒ 取值点必须在 ``absorb`` **之前**、由唯一有它的 ``_absorb_tool`` 落进来。
        self.write_meta: dict[str, dict[str, Any]] = {}
        #: ``tool_call_id`` → 用**本轮快照编号**渲染好的观察文本（给模型与步骤流看）
        self.rendered: dict[str, str] = {}
        #: 待落审计的工具调用（**写库在消费端任务做**，见 :class:`ToolAudit`）
        self.audits: list[ToolAudit] = []
        # --- 普通工具结果通道（第 0 批 · ①）：``materials`` 与它**互斥分流**（D2′）---
        #: ``tool_call_id`` → 该次调用**是否走 ``materials`` 那条路**（即 ``absorb`` 分支 ①）。
        #: ``True`` ⇒ **不进**通道（那份文本已经在资料区里，再注入就是双注入）；
        #: ``False`` ⇒ 进通道。**分流判据是「实际走了哪条分支」**，不是「材料是否为空」
        #: ——失败前可能已检到材料（那也算进了资料区），而材料被去重光的情形两处都不进就
        #: 丢文本了（与本条目标相反）。
        self._in_materials: dict[str, bool] = {}
        #: ``tool_call_id`` → 待进通道的**原始有界文本**（分支 ②/③ 才有值）
        self._context_raw: dict[str, str] = {}
        #: ``tool_call_id`` → 通道条目的 ``ok``（与 ``tool/result`` 事件的 ``ok`` 同源）
        self._context_ok: dict[str, bool] = {}
        #: **完成序**的 ``tool_call_id``（与 ``observations`` 的插入序同源，仅作兜底排序）
        self._context_seq: list[str] = []
        #: 通道条目的**库内时钟**时刻（``tool_call_id`` → ISO 字符串；D7）。
        #: 在 ``absorb``（= 工具产出那一刻）取值，而不是在收口时统一取——
        #: 「collected_at」的语义是**采集时刻**，收口时刻会让多个条目拿到同一个时间戳。
        self._context_at: dict[str, str] = {}
        #: 通道条目的**调用序**契约名（``tool_call_id`` → 工具名；``absorb`` 时由调用方给）。
        self._context_tool: dict[str, str] = {}
        #: ``tool/call`` 事件的**事件序**（``tool_call_id`` 列表；D25 的排序真源）。
        #: 由 :meth:`note_call_order` 在**事件被消费**时追加（消费者单任务串行 ⇒ 天然是
        #: 「发起顺序」）；⚠️ 并行工具下它与 ``_context_seq``（完成序）**故意不同**，
        #: 这正是 A7 要钉的那条。
        self.call_order: list[str] = []
        #: 库内时钟的取值口径（``db`` 连接；``None`` ⇒ 回落 ``settings.is_sqlite``）。
        #: ⚠️ 用连接口径而不是进程级配置：时间戳的时钟属于**那条连接**（``回归修复``）。
        self.clock_db: Any = None
        #: 本轮**唯一的兜底工具 id**（``回归修复``）。空 ``tool_call_id`` 一律归一成它
        #: ——**通道的键也必须归一**，否则「执行侧用兜底 id、事件侧用兜底 id、通道里却是个
        #: 空串」⇒ 排序（按事件序对账）当场断链，条目排在末尾或压根排不上。
        self.fallback_id: str = ""
        #: 空 ``tool_call_id`` 的**每次调用**台账（``回归修复``）。
        #: 为什么不能只靠 :attr:`fallback_id`：一轮里两次空 id 会折成**同一个键** ⇒
        #: ``observations`` / ``rendered`` / 步骤帧 / 审计行互相覆盖（last-wins），先调用者
        #: 的结果对最终回答丢失、两条步骤帧的 id 相同。台账每项 = ``[工具名, 本次铸造的 id,
        #: 是否已被执行侧认领]``，按铸造序追加、随 ``tool_execution_end`` 删除。
        #: ⚠️ 这里的 id 由**事件桥**铸造（见 ``stream_agent_turn`` 的 ``_on_event``），
        #: 执行侧只**认领**、不另铸 —— 两侧同源是 I4 配对键成立的前提。
        self._blank_call_slots: list[list] = []

    # --- 普通工具结果通道：累积 / 排序 / 收口 ---------------------------------

    def note_tool_name(self, tool_call_id: str, tool: str) -> None:
        """登记某次调用的**契约工具名**（``absorb`` 之前由执行侧调用）。

        ``ToolContextItem.tool`` 需要工具名，而 ``ToolObservation`` **不带**工具名
        （它由工具模块自己产出，模块不知道自己被注册成什么名字）⇒ 只能由执行侧在
        ``_absorb_tool`` 里补上。单独一个方法是为了让 P&E / ReAct / 测试三条路共用一处。
        """
        self._context_tool[self._channel_key(tool_call_id)] = str(tool or "")

    def note_call_order(self, tool_call_id: str) -> None:
        """登记一次 ``tool/call`` 的**事件消费序**（D25 的排序真源）。

        调用点 = 消费端任务处理 ``tool_execution_start`` 事件时。为什么不能直接用
        ``observations`` / ``_context_seq``：那是**完成序**，并行工具下与发起序不同，
        照它排会让通道里两条结果的先后与审计 ``tool/call`` 的顺序对不上。
        """
        rid = self._channel_key(tool_call_id)
        if rid and rid not in self.call_order:
            self.call_order.append(rid)

    def _channel_key(self, tool_call_id: str) -> str:
        """把工具 id 归一成通道的键（空 id 一律折成 :attr:`fallback_id`）。

        **为什么必须归一**（``回归修复`` 的同型坑，载荷面已修、通道这是第一次遇到）：
        CubeLoop 在 ``tc_delta.id or ""`` 时会给**空 id**，而执行侧 / 事件侧 / 步骤帧三处
        都已经把它折成**同一个** ``fallback_id``。通道若照收空串，就会出现「事件序里是
        ``<fallback>``、通道里是 ``""``」⇒ 按事件序对账取不到该条 ⇒ 它被挤到兜底位置，
        多工具时的顺序就错了。

        ``回归修复`` 起：走事件桥时空 id 已在监听器里换成**每次调用各自的** id，因此这里
        收到空串只来自**没有事件桥**的调用点（测试 / 手造事件 / P&E 旧路）——回落口径不变。
        """
        rid = str(tool_call_id or "").strip()
        return rid or self.fallback_id

    def note_blank_call_start(self, tool: str) -> str:
        """为一个**空 id** 的 ``tool_execution_start`` 铸造独立 id（``回归修复``）。

        唯一铸造点 = 事件桥的监听器（``stream_agent_turn`` 的 ``_on_event``）：CubeLoop
        **同步**调用监听器，且同一任务内「Start 事件」与「``execute`` 调用」相邻（中间没有
        让出点，见 ``cubeloop/agent/tools.py`` 的 ``_run`` → ``_execute_prepared``）
        ⇒ 铸造序 = 发起序 = 执行侧认领序。事件对象上改写 id 之后，下行所有消费者（轮次记账 /
        参数记账 / 立即失败补账 / 事件写入 / 步骤帧）**自动同源**，不必各自改判据。
        """
        rid = new_tool_call_id()
        self._blank_call_slots.append([str(tool or ""), rid, False])
        return rid

    def claim_blank_call(self, tool: str) -> str:
        """执行侧在 ``execute`` 里认领**本调用自己**的铸造 id；认不到返回 ``""``。

        取**同名未认领槽位里最后一个**：正常流下每个 Start 都紧接着被它自己的 ``execute`` 认领
        （FIFO 即最新），只有「槽位因立即失败 / 取消而无人认领」时最新的那个才对本调用正确。
        ⛔ 不跨工具名兜底：跨名兜底会把别的工具刚铸造的槽偷过来 ⇒ 归属错，宁可回落
        ``fallback_id``（旧口径，逐字不变）。
        """
        index = self._blank_slot_index(tool, claimed=False)
        if index is None:
            return ""
        self._blank_call_slots[index][2] = True
        return str(self._blank_call_slots[index][1])

    def close_blank_call(self, tool: str) -> str:
        """为**空 id** 的 ``tool_execution_end`` 取回配对 id；取不到返回 ``""``。

        先找同名**已认领**槽位（最老的 = FIFO：完成序与发起序同）⇒ 取出并删；否则找同名
        **未认领**槽位（立即失败 / 被闸拦下：Start 铸了槽、``execute`` 没跑）⇒ 取出并删。
        两者都没有 ⇒ 空串（调用方回落 ``fallback_id``，手造事件 / 无事件桥的旧路逐字不变）。
        """
        index = self._blank_slot_index(tool, claimed=True)
        if index is None:
            index = self._blank_slot_index(tool, claimed=False)
        if index is None:
            return ""
        return str(self._blank_call_slots.pop(index)[1])

    def _blank_slot_index(self, tool: str | None, *, claimed: bool) -> int | None:
        """在台账里找第一个「同名 + 认领态相同」的槽位下标；``None`` 表示不限工具名。"""
        name = None if tool is None else str(tool or "")
        for index, slot in enumerate(self._blank_call_slots):
            if bool(slot[2]) is not claimed:
                continue
            if name is None or str(slot[0]) == name:
                return index
        return None

    def absorb(self, tool_call_id: str, observation: ToolObservation) -> str:
        """并入一次工具产出，返回该次的观察文本。"""
        observation = bind_retrieval_observation(observation, tool_call_id)
        self.observations[tool_call_id] = observation
        for item in observation.warnings or []:
            text = str(item or "").strip()
            if text and text not in self.warnings:
                self.warnings.append(text)

        keys = [item.chunk_key for item in observation.materials or []]
        known = {item.chunk_key for item in self.materials}
        for item in observation.materials or []:
            if item.chunk_key in known:
                continue
            if len(self.materials) >= FUSE_LIMIT:
                break
            known.add(item.chunk_key)
            self.materials.append(item)
        number(self.materials)

        by_key = {item.chunk_key: item for item in self.materials}
        entries = [by_key[key] for key in keys if key in by_key]
        # 本次检到、但**一条都没进快照**的 key（S4 · 207：混合批同样会落进这里）。
        # ⚠️ 判据是「**有 key 没进快照**」，不是「本次一条都没进」——只有前者才覆盖混合批。
        dropped = [key for key in keys if key not in by_key]
        if entries:
            # 有可引用材料 → 用**快照编号**重渲染（I3）
            text = render_observation(entries)
            # 分支 ①：观察文本**已进资料区**（``render_materials`` 从同一份
            # ``self.materials`` 渲染）⇒ 该条**不进** ``tool_context``（防双注入，D2′）。
            # ⚠️ S4 · 207：分支 ① **不等于**「本次全都进了快照」——混合批也走这里
            # （新 key 在上面的 ``break`` 处被丢掉）⇒ 丢弃的账在下面**统一结**。
            self._remember_context(tool_call_id, in_materials=True, raw="", text=text)
        elif keys:
            # 本次确实检到了东西，但一条都没进快照（上限已满）：**不能**回落到工具自渲染文本
            # ——那份文本从 1 重新编号，会让 ``[1]`` 指向快照里的别的材料（见 SNAPSHOT_FULL_TEXT）。
            text = SNAPSHOT_FULL_TEXT
            # 分支 ②：**没进**资料区 ⇒ 进通道；文本取该态的实测原文（固定短串，天然有界）。
            self._remember_context(tool_call_id, in_materials=False, raw=text, text=text)
        else:
            # 真的没有材料（失败原因 / MCP 文本结果 / 空命中）⇒ 用工具自己给的文本
            text = observation.text or NO_HIT_TEXT
            # 分支 ③：**没进**资料区 ⇒ 进通道；文本取**原始**文本（有界），
            # 不是 ``rendered``（两者天然不同，A2b 判据③钉这条）。
            self._remember_context(tool_call_id, in_materials=False, raw=observation.text or "", text=text)
        # ——S4 · 207：被上限丢掉的片段**必须显式告警**，三个分支之后统一结账——
        # 分支 ② 的文案逐字不变（那里 ``len(dropped) == len(keys)``）；混合批从此也能读到告警。
        if dropped:
            warning = f"可引用材料已达上限 {FUSE_LIMIT}，本次新检到的 {len(dropped)} 条未纳入引用快照"
            if warning not in self.warnings:
                self.warnings.append(warning)
            self.snapshot_warnings.setdefault(tool_call_id, [])
            if warning not in self.snapshot_warnings[tool_call_id]:
                self.snapshot_warnings[tool_call_id].append(warning)
        # ——S4 · 207：``materials_hit`` 与**本次观察文本**同源——
        # 分支 ① 的 ``content`` 由 ``entries`` 渲染 ⇒ 命中数就是 ``len(entries)``（丢掉的、
        # 不在快照里的都不计）；分支 ②/③ 的观察文本不来自快照 ⇒ 恒 0。
        # ⛔ 不得再取 ``len(observation.materials)``——那会把 break 掉的新片段也算成命中。
        self.material_hits[tool_call_id] = len(entries)
        # 完成序（兜底排序用；⚠️ **不是**排序真源——排序真源是 ``call_order``，见 D25）
        if tool_call_id not in self._context_seq:
            self._context_seq.append(tool_call_id)
        self.rendered[tool_call_id] = text
        return text

    def _remember_context(self, tool_call_id: str, *, in_materials: bool, raw: str, text: str) -> None:
        """记下一次调用在**通道**里的归属与内容（``absorb`` 三个分支各自调用）。

        ``raw`` 与 ``text`` 的区别：``raw`` 是**待进通道的原始文本**（分支 ② 是固定串、
        分支 ③ 是 ``observation.text``、分支 ① 为 ``""``）；``text`` 是**决策侧**的观察文本
        （分支 ① 是快照重渲染，永远不进通道正文）。
        """
        rid = self._channel_key(tool_call_id)
        self._in_materials[rid] = bool(in_materials)
        if not in_materials:
            self._context_raw[rid] = raw
            self._context_ok[rid] = bool(getattr(self.observations.get(rid), "ok", True))
            self._context_at.setdefault(rid, agent_audit.db_clock_now(self.clock_db).isoformat())
        else:
            # 双注入防线：若先前记过该 id（重入 / 重渲染），分支 ① 之后必须把它清干净
            self._context_raw.pop(rid, None)
            self._context_ok.pop(rid, None)
            self._context_at.pop(rid, None)

    def tool_context_items(self) -> list[ToolContextItem]:
        """通道条目，**按调用顺序**（D5 + D25）——最终回答回答请求体的输入。

        排序口径（**本批最容易写错的一处**）：

        - 主序 = :attr:`call_order`（``tool/call`` 事件的**消费序** = 发起序）；
        - 未记到事件序的 id 按**完成序**（:attr:`_context_seq`）兜底排在其后——
          只有「事件没到」（异常 / 取消 / 截断）才会走到这里，此时没有更权威的顺序；
        - **绝不**遍历 dict / set；**绝不**直接用 ``observations`` 的自然序。

        只在**最终回答**那一次调用使用（``_finish`` 收口 → ``AgentTurn.tool_context``）；
        **决策提示不带**（D2：「否则决策提示会随工具结果长度漂移」）。
        """
        ordered = [rid for rid in self.call_order if rid in self._context_raw]
        ordered += [
            rid
            for rid in self._context_seq
            if rid in self._context_raw and rid not in ordered
        ]
        items = [
            ToolContextItem(
                tool=self._context_tool.get(rid, ""),
                tool_call_id=rid,
                ok=bool(self._context_ok.get(rid, True)),
                # 分支 ③ 的原始文本**只有这里**才截断（D4）；分支 ② 的固定串原样过
                text=clip_tool_context_text(self._context_raw[rid]),
                collected_at=self._context_at.get(rid, ""),
            )
            for rid in ordered
        ]
        return cap_tool_context(items)


@dataclass
class ToolAudit:
    """一次**真执行**的工具调用需要落审计的那份数据（条目 4.3.2）。

    ⚠️ **为什么记账与写库分开**（这是本条实测抓到的并发缺陷的修法，见 :func:`_flush_audit`）：
    CubeLoop 的工具执行跑在**它自己的 asyncio 任务**上，而事件落库跑在**消费端任务**上
    ——两者共用同一个 ``AsyncSession``（请求级依赖）。在工具执行里**直接**写库会让两个任务
    同时碰一个 session：实测稳定触发 ``SAWarning: Usage of the 'Session.add()' operation is
    not currently supported within the execution stage of the flush process``，被 ``safe_*``
    隔离后**静默丢一行 ``agent_steps``**（ReAct 跑两个工具时第 2 行必丢）——「审计看起来
    开着、实际缺行」，正是本条最不能容忍的那种假象。

    所以：工具执行只**记账**（本 dataclass 进 ``collector.audits``），真正的写库由消费端任务在
    :func:`_write_tool_events` 之后**串行**触发（:func:`_flush_audit`）。这样 DB 写全部落在
    **同一条任务**上，「Session 并发」在结构上不可能发生。

    ``step_no`` 取**记账顺序**（``len(collector.audits) + 1``，1 起）：它保证「不重复、不跳号」，
    因为记账只 append、从不删除。⚠️ **并行工具调用下的一个已知时序差别**：一个 assistant
    消息里同时发起多个 ``tool_call`` 时，CubeLoop 并发执行它们 ⇒ **记账顺序 = 完成顺序**，
    而 ``tool/call`` 事件的顺序 = 发起顺序。两者可能不同 ⇒ **按 ``step_no`` 与事件 ``seq``
    对账会错位**，对账必须用 ``tool_call_id``（那是 I4 的配对键，也是它存在的理由）。
    """

    tool_name: str
    tool_call_id: str
    ok: bool
    content: str
    #: **整轮**工具调用序号（1 起；由 :func:`_absorb_tool` 按记账顺序铸）
    step_no: int
    ms: int
    args: dict[str, str] | None = None
    #: **写通道专属三键**（专项工具升级-3 · 阶段 5 · `回归检查`）：由写工具的执行侧
    #: 从 ``WriteOutcome``（`D-回归检查` 第 ⑥ 项）里取出后原样带下来。
    #:
    #: ⚠️ **只读工具恒为 ``None``** ⇒ ``write_meta`` 一路到 :func:`audit.write_tool_step`
    #: 都是 ``None`` ⇒ 那条 ``meta`` 的键集**逐字不变**（`A20` 腿③ 同款的「既有语义不破」）。
    #: 这是本字段默认 ``None`` 而不是 ``{}`` 的理由：``{}`` 也能达成同样效果，但 ``None``
    #: 让「这次调用不是写通道」在类型上就说得清，不必去分辨「空字典 vs 没给」。
    write_meta: dict[str, Any] | None = None


# --------------------------------------------------------------------------
# 一轮 Agent 的产物
# --------------------------------------------------------------------------


@dataclass
class AgentTurn:
    """一轮 Agent 的全部产物：步骤流 + 材料快照 + 降级信息。

    ``steps`` 里的 dict **就是** ``agent_trace.steps`` 的元素（也直接进 SSE ``agent`` 帧），
    字段与老基线 ``AgentStep.to_dict()`` 逐字同构：
    ``kind / title / detail / tool / args / ok / ms / tool_call_id``
    （冻结前端只认前七个，``tool_call_id`` 是阶段 4 新增的对账字段，旧前端忽略）。
    """

    mode: str = "off"
    steps: list[dict] = field(default_factory=list)
    materials: list[Material] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    aborted: bool = False
    note: str = ""
    events_incomplete: bool = False
    #: 本轮「为什么缺行」（阶段 4 · 条目 4.4 · D5 的封闭取值域：
    #: ``none`` / ``cancelled`` / ``write_failed`` / ``disabled`` / ``read_failed``）。
    #: ⚠️ **不进** :meth:`trace`（那个键集是冻结的三键）；出口是日志 + 用例断言。
    events_incomplete_reason: str = "none"
    #: **普通工具结果通道**（专项工具调用第 0 批 · ①）：:class:`ToolContextItem` 的列表，
    #: 在 :func:`_finish` 与 ``materials`` **同型**收口（同一个收口点，两条路径共用）。
    #: ``[]`` = 本轮没有「未进资料区」的工具结果 ⇒ 回答请求体**逐字节 no-op**（D8 的第一性质）。
    #: ⚠️ **只喂最终回答**那一次模型调用，**不进决策提示**（D2）。
    tool_context: list = field(default_factory=list)
    #: 结束决策（第 1 批）。``None`` = 本轮没有协议块。不进 :meth:`trace`。
    closeout: dict | None = None
    #: 有协议块但解析失败。同样不进 :meth:`trace`。
    closeout_invalid: bool = False
    #: 已跑轮次（CubeLoop 的 turn 数；不参与 agent_trace，供测试与上限判定用）
    rounds: int = 0    #: ``agent_runs.id``（条目 4.3 审计；``None`` = 本轮没建审计行——开关关 / 没 db / 没 chat_id）
    run_id: int | None = None
    #: 收口时的「中止原因」提示（只给 :func:`_audit_run_status` 判 ``stopped`` 用；
    #: 由 :func:`_audit_run` 在异常/关闭路径上写，不参与 ``trace()`` 的形状）
    closed_by_generator_exit: bool = False
    #: 本轮**决策模型调用失败过**（CubeLoop 上游 ``stop_reason="error"``，``回归修复``）。
    #: 由 :func:`_steps_for` 在 ``turn_end`` 分支置位；:func:`_audit_run_status` 只认它——
    #: run 行记 ``error`` 而不是 ``done``，「这轮跑完了」与「这轮的模型调用失败过」必须可分。
    #: 不参与 ``trace()``。
    model_error: bool = False
    #: 轮次上限**实际**强制停过（``_should_stop`` 钩子观察到 ``rounds >= limit`` 并返回
    #: ``True`` 时置位，``回归修复``）。收尾 note 只认它：模型自然收尾恰好落在上限时
    #: 钩子根本没被征询（CubeLoop 只在有工具调用的轮尾问），标记保持 ``False``，
    #: 「模型自主收手」不再被误报成「已达最大轮次」。不参与 ``trace()``。
    rounds_capped: bool = False

    def add_step(
        self,
        kind: str,
        title: str,
        *,
        detail: str = "",
        tool: str | None = None,
        args: dict | None = None,
        ok: bool | None = None,
        ms: int | None = None,
        tool_call_id: str | None = None,
    ) -> dict:
        """追加一步并返回它的 dict（调用方负责把它 yield 出去当 SSE 帧）。"""
        step = {
            "kind": kind,
            "title": title,
            "detail": detail,
            "tool": tool,
            "args": args,
            "ok": ok,
            "ms": ms,
            "tool_call_id": tool_call_id,
        }
        self.steps.append(step)
        return step

    def note_step(self, title: str, detail: str = "") -> dict:
        """追加一条 ``note`` 并记进 ``turn.note``（降级/失败对外的唯一可见出口）。"""
        if not self.note:
            self.note = f"{title}：{detail}" if detail else title
        return self.add_step("note", title, detail=detail)

    def trace(self) -> dict:
        """``agent_trace``（SSE ``done`` 与 ``messages.agent_trace`` 共用同一份）。

        「只增不改义」：三个键（``mode`` / ``steps`` / ``events_incomplete``）
        与老基线逐字同构；后续条目只许加键。停止轮另有一套（见 ``chat_routes``）。
        """
        return {
            "mode": self.mode,
            "steps": list(self.steps),
            "events_incomplete": bool(self.events_incomplete),
        }


# --------------------------------------------------------------------------
# 工具装配（声明 → CubeLoop AgentTool）
# --------------------------------------------------------------------------


@dataclass
class AgentToolkit:
    """本轮工具装配的**全部产物**（条目 4.3 起不再用裸元组）。

    为什么要一个对象而不是 ``(tools, collector)`` 元组：P&E 的闸③ 需要**按名字反查
    ``ToolSpec``**，而反查必须用「**本轮真正用的那一份 registry**」——``stream_agent_turn``
    的 ``registry`` 形参在生产路径上是 ``None``（注册表由这里内部自建），把它透传过去
    等于让闸门恒失效（对抗性核验抓到的洞：``spec_map`` 恒 ``{}`` ⇒ P&E 结构性不拦）。
    把**实际用的 registry** 与 ``fallback_id`` 一并回传，调用方就不必去猜。
    """

    tools: list[Any] = field(default_factory=list)
    #: 实际使用的那份注册表（调用方建的 / :func:`build_agent_tools` 现建的）——P&E 的反查靠它
    registry: ToolRegistry | None = None
    #: 本轮唯一的兜底工具 id（``回归修复``：执行侧 / 事件侧 / 步骤帧三层共用）
    fallback_id: str = ""
    #: 本轮唯一的会话锁（执行闭包要用它罩住「执行 + 记账」；见 `_session_lock`）
    db_lock: "asyncio.Lock | None" = None
    #: 本轮的材料累积器（与 ``tools`` 同寿命）
    collector: ToolCollector | None = None

    def spec_map(self) -> dict[str, Any]:
        """名字 → :class:`ToolSpec`（闸③ 的判据来源）。见 :func:`_spec_map`。"""
        return _spec_map(self.tools, self.registry)

    def __iter__(self):
        """兼容既有写法 ``tools, collector = build_agent_tools(...)``（**一行都不用改**）。

        为什么保留二元组解包：4.1/4.2 的调用方与用例都是这么写的，而本条只是为了多回传一个
        ``registry``——让所有既有调用方被迫改写是没必要的代价。需要 registry 的调用方
        （``stream_agent_turn``）直接拿对象。
        """
        return iter((self.tools, self.collector or ToolCollector()))


def build_agent_tools(
    *,
    db: Any = None,
    filters: dict | None = None,
    web_enabled: bool = False,
    registry: ToolRegistry | None = None,
    collector: ToolCollector | None = None,
    turn: AgentTurn | None = None,
    fallback_id: str | None = None,
    db_lock: "asyncio.Lock | None" = None,
    **deps: Any,
) -> "AgentToolkit":
    """构造本轮模型可见的工具清单，返回 :class:`AgentToolkit`（**含实际用的 registry**）。

    ``registry`` 不传时按本轮依赖现建一份（``db`` / ``filters`` / ``web_enabled`` 闭合进
    handler）。**过滤全在** :meth:`ToolRegistry.for_model` 一处（dangerous + web 门控），
    这里只做「声明 → AgentTool」的适配。

    ``turn``（条目 4.3）：执行点闸门与审计都要整轮上下文（``run_id``），而 CubeLoop 的
    ``AgentTool.execute`` 只给 ``(tool_call_id, args)``——只能闭包带进去。
    不传时自建一个弃用的 ``AgentTurn``（旧调用方仍可用，只是没有审计行可写，``run_id`` 恒 ``None``）。

    ⚠️ **返回值从二元组改成** :class:`AgentToolkit`（条目 4.3 对抗性核验的 G6）：
    P&E 的闸③ 要按名字反查 ``ToolSpec``，而反查必须用「**本轮真正用的那一份 registry**」。
    原先只回传 ``(tools, collector)``，调用方（``stream_agent_turn``）只能把**形参** registry
    再透传下去——那在生产路径上是 ``None``（注册表由这里现建）⇒ ``spec_map`` 恒 ``{}`` ⇒
    **P&E 结构性不拦**。把实际用的 registry 与 ``fallback_id`` 一并回传，调用方就不必猜。

    兼容性：``AgentToolkit`` 实现了 ``__iter__``（产出 ``tools, collector``），
    所以既有的 ``tools, collector = build_agent_tools(...)`` 写法**一行都不用改**。
    """
    from cubeloop import AgentTool, AgentToolResult
    from cubeloop.providers.base import TextContent

    from app.agent.bootstrap import build_registry

    collector = collector or ToolCollector()
    turn = turn or AgentTurn()
    fallback_id = fallback_id or new_tool_call_id()
    registry = registry or build_registry(db=db, filters=filters, web_enabled=web_enabled, **deps)

    tools: list[Any] = []
    for spec in registry.for_model(web_enabled=web_enabled):
        if spec.parameters is None:
            # 没有参数模型的声明上不了模型清单（生成不了 JSON Schema）——注册表里仍可见
            logger.warning("工具 %s 缺少参数模型，未纳入本轮模型清单", spec.name)
            continue
        tools.append(
            _as_agent_tool(
                spec,
                collector,
                AgentTool,
                AgentToolResult,
                TextContent,
                turn,
                db,
                fallback_id,
                db_lock,
            )
        )
    return AgentToolkit(
        tools=tools,
        registry=registry,
        fallback_id=fallback_id,
        db_lock=db_lock,
        collector=collector,
    )


def _as_agent_tool(
    spec,
    collector: ToolCollector,
    AgentTool,
    AgentToolResult,
    TextContent,
    turn,
    db=None,
    fallback_id: str | None = None,
    db_lock: "asyncio.Lock | None" = None,
):
    """把一个 :class:`ToolSpec` 适配成 CubeLoop 的 ``AgentTool``。

    适配层做四件事（这是「工具模块不碰框架」的代价，也是它的价值）：

    1. **执行前过安全闸门**（条目 4.3.1）——危险工具**不执行**，返回冻结中文的 ``ok=False``
       观察（见 :mod:`app.agent.safety`）。这是执行侧的**第三道闸**（①② 在装配与注册表）；
    2. 把 handler 的 :class:`ToolObservation` 转成 ``AgentToolResult``；
    3. **用本轮快照编号重渲染观察文本**（材料编号与最终引用编号同源，I3）；
    4. 以 ``tool_call_id`` 为键记住观察，供运行器写步骤与事件（I4 的对账键），
       并按 :func:`app.agent.audit.write_tool_step` 落一行审计（**成败都记**）。

    ``turn`` 是 ``AgentTurn``：审计要 ``run_id``（整轮步骤序号由 ``_absorb_tool`` 的记账顺序铸）。
    CubeLoop 的 ``AgentTool.execute`` 只给 ``(tool_call_id, args)``，没有别的透传口子，
    所以整轮上下文只能靠闭包带进去（P&E 那条路径同理，走 ``_run_plan_step`` 的形参）。

    ``fallback_id`` 是**本轮唯一的兜底工具 id**（``回归修复``）：CubeLoop 给空 id 时，
    执行侧与事件侧必须用**同一个** id，否则 ``collector`` 的键与 ``tool/call`` 的配对键对不上。
    不传则自铸一个（``build_agent_tools`` 的旧调用方仍可用）。

    ⚠️ ``回归修复`` 起，「同一个 id」的粒度从**整轮**收窄到**每次调用**：空 id 事件由事件桥
    （``stream_agent_turn`` 的 ``_on_event```）按调用序铸成独立 id 并写回事件，执行侧在这里向
    ``collector`` 的空白调用台账**认领**同一个 id（:meth:`ToolCollector.claim_blank_call`）。
    认不到台账（无事件桥 / 手造调用）时仍回落 ``fallback_id`` ⇒ 旧的单 id 兜底口径完好。
    """
    fallback_id = fallback_id or new_tool_call_id()

    async def execute(tool_call_id: str, args, **_kwargs):
        rid = str(tool_call_id or "").strip()
        if not rid:
            # 回归修复：空 id 先向台账**认领「本调用自己」**的铸造 id（事件桥已为它改写
            # 对应的 start 事件，见 _on_event）；认不到（无事件桥的旧调用点 / 手造事件 /
            # 工具名被 resolve_tool_call 改写）才回落本轮兜底 id ⇒ 旧口径逐字不变。
            rid = collector.claim_blank_call(spec.name) or fallback_id
        started = time.perf_counter()
        if safety.is_blocked(spec):
            # 闸③：**不执行**（handler 一次都不调），但照样成对留痕（I4 不破）
            observation = safety.blocked_observation(spec)
            text = await _absorb_tool(
                spec=spec,
                collector=collector,
                rid=rid,
                args=args,
                observation=observation,
                started=started,
            )
        else:
            # ⚠️ **工具执行本体也要过会话锁**（对抗性核验 P1 的最后一块拼图）：
            # 工具 handler 会碰请求级 ``AsyncSession``（``knowledge_search`` 走真检索**读库**；
            # 将来的 MCP / 笔记工具同样会）。执行跑在 **CubeLoop 自己的任务**上，而事件落库 /
            # 审计写库跑在**消费端任务**上——只在写侧加锁**不够**：读侧仍会在对面 flush 的中途
            # 插入，于是收口撞出
            # ``This session is in 'prepared' state; no further SQL can be emitted``
            # ⇒ ``finish_run`` 失败 ⇒ **僵尸 ``running`` 行**（确定性复现过）。
            # 把「执行 + 记账」整段纳入锁：这一轮里碰 session 的事就只剩它一个在跑。
            async with _session_lock(db_lock):
                observation = await _run_tool_handler(spec, args)
                text = await _absorb_tool(
                    spec=spec,
                    collector=collector,
                    rid=rid,
                    # ⚠️ 传**原始** args（CubeLoop 把 ToolCall.arguments 原样给到这里），
                    # 不是校验后的 ``args_model``：后者带着默认值，落进审计的 ``meta["args"]``
                    # 会与 ``tool/call`` 载荷里的 ``args``（模型实给的键）不是同一份内容
                    # （实测前者含 ``limit/deep/filters``、后者只有实给键）。两个可观测面必须是
                    # 同一份事实（`回归修复` 同型的「同一语义两处不同」）。
                    args=args,
                    observation=observation,
                    started=started,
                )
        return AgentToolResult(
            content=[TextContent(text=text)],
            details={
                "tool": spec.name,
                "ok": bool(observation.ok),
                "materials": len(getattr(observation, "materials", None) or []),
                "warnings": len(getattr(observation, "warnings", None) or []),
            },
            is_error=not bool(observation.ok),
        )

    return AgentTool(
        name=spec.name,
        description=spec.description,
        parameters=spec.parameters,
        execute=execute,
    )

async def _run_tool_handler(spec, args) -> ToolObservation:
    """真跑一次工具 handler，把异常 / 违约返回值都转成观察（I5：绝不炸穿 Agent 循环）。

    ``回归修复``：观察文本是**固定形状**（``{工具名} 执行失败（{异常类名}）。``），
    ``str(exc)`` 原文**只进日志** —— 工具异常里常带 SQL / 路径 / 连接串，转述给模型等于把
    内部细节塞进上下文（还可能被它原样写进回答）。这是 ReAct 与 P&E 的**唯一**汇合点。
    """
    try:
        observation = await spec.handler(args)
    except Exception as exc:  # noqa: BLE001 —— 工具失败转观察，绝不炸穿（I5）
        logger.warning("工具 %s 执行异常：%s", spec.name, exc)
        return ToolObservation(text=f"{spec.name} 执行失败（{exc.__class__.__name__}）。", ok=False)
    if isinstance(observation, ToolObservation):
        return observation
    if all(hasattr(observation, name) for name in ("text", "materials", "warnings", "ok")):
        # 鸭子类型：调用方只读这四个属性，跨模块的替身对象不必是同一次 import 的类
        # （测试里给工具注入假检索时就会用别的实现）。
        return observation
    # 真违约（handler 返回了字符串 / None 之类）：包成观察，别让下游按属性访问炸掉。
    return ToolObservation(text=str(observation))


def _write_meta_of(observation: Any) -> dict[str, Any] | None:
    """从一次工具观察里取出**写通道专属三键**（`回归检查`）；非写工具 ⇒ ``None``。

    ## 为什么挂在 ``observation`` 上，而不是新写一个字段穿过四条调用链

    `ToolObservation` 是**冻结的公共类型**（`types.py` 的 D1 五键面：写通道的三键
    `write_target` / `write_byte_delta` / `write_snapshot_id` **不在** `ToolContextItem`
    的五键里，也**不该**进——那条通道是给**模型**看的，写通道元数据是给**审计**看的）。
    把它加成 `ToolObservation` 的正式字段则要动 `types.py`（阶段 4 已验收件），
    且「只读工具恒为空」会变成每个构造点都要写的义务。

    ⇒ 由**写工具的 handler** 在自己的观察实例上附一个属性（`workspace_write.py`），
    本函数只做 ``getattr`` 兜底。**没有该属性 ⇒ ``None`` ⇒ 审计 meta 逐字不变**
    —— 这正是「既有 11 个只读工具零影响」的机械保证。

    ⚠️ ``_run_tool_handler`` 有一条**鸭子类型**分支（跨模块替身对象）：那种对象没有本属性
    ⇒ 走 ``None`` 分支。这是**有意的**：猜出来的 write_meta 会往审计里灌假数据，
    而漏记只影响写通道自己的判据（`A12a`），不影响既有面。
    """
    if observation is None:
        return None
    meta = getattr(observation, "write_meta", None)
    return meta if isinstance(meta, dict) and meta else None

async def _absorb_tool(
    *,
    spec,
    collector: ToolCollector,
    rid: str,
    args,
    observation: ToolObservation,
    started: float,
) -> str:
    """观察并入 collector（编号/渲染/上限）+ **记一条待落审计**，返回渲染好的观察文本。

    ReAct 与 P&E **共用本函数**：两处若各写一份，``ok`` / ``ms`` / 审计 ``step_no``
    迟早漂移（4.2 的 ``_write_tool_events`` 共用是同一个教训）。
    调用方拿返回的文本自己组框架产物（ReAct 包成 ``AgentToolResult``，P&E 直接用）。

    ⚠️ **通道（第 0 批 · ①）的「工具名」也在这里补**：``ToolObservation`` 不带工具名，
    而 :class:`~app.agent.types.ToolContextItem` 的五键里有 ``tool`` ⇒ 由本函数（两条
    路径唯一的汇合点）在 ``absorb`` **之前**登记。放在别处就会出现「ReAct 有名字、
    P&E 是空串」的偏科。
    """
    collector.note_tool_name(rid, spec.name)
    # 🔴 阶段 6（`回归检查`）：**在 `absorb` 之前**把写通道三键留在 collector 上。
    # 理由见 `ToolCollector.write_meta` 的字段注释：`absorb` 里的 `bind_retrieval_observation`
    # 是 `dataclasses.replace(...)`，会把 handler 动态附上的 `write_meta` **丢掉** ⇒
    # 步骤帧那一步再取就恒为 `None`（审计面不受影响，因为下面那句用的是**本函数收到的**
    # 那个原始 `observation`）。这里与审计取的是**同一个入参**⇒ 天然同源。
    _meta = _write_meta_of(observation)
    if _meta is not None:
        collector.write_meta[rid] = _meta
    text = collector.absorb(rid, observation)
    # ⚠️ 只**记账**、不写库：执行闭包跑在 CubeLoop 自己的任务上，与事件落库的消费端任务
    # 共用同一个 AsyncSession——直接写库会与并发 flush 打架并**静默丢一行审计**
    # （见 :class:`ToolAudit`）。真正的写入在消费端的 `_flush_audit` 里串行做。
    collector.audits.append(
        ToolAudit(
            tool_name=spec.name,
            tool_call_id=rid,
            ok=bool(observation.ok),
            content=text,
            # ``step_no`` = **整轮**工具调用序号（1 起）：记账按执行顺序追加、从不删除，
            # 所以「这是第几条记账」就是「第几次工具调用」——ReAct 与 P&E 共用同一口径。
            step_no=len(collector.audits) + 1,
            ms=int((time.perf_counter() - started) * 1000),
            args=_args_digest(args),
            write_meta=_meta,
        )
    )
    # 收尾兜底（防「事件没到」）：正常路径下这里无事可做，见 `_flush_audit` 的 docstring。
    return text


def _agent_tool_result_text(result: Any) -> str:
    """从 CubeLoop 的 ``AgentToolResult`` 取纯文本（``_ImmediateOutcome`` 的错误载体）。"""
    blocks = getattr(result, "content", None) or []
    return "".join(str(getattr(block, "text", "") or "") for block in blocks).strip()


def _engine_tool_result_text(event: Any) -> str:
    """引擎**自执行**工具成功腿的结果文本（S4 · 208）。

    ``load_tools``（``deferred_tool_strategy="dispatch"``）这类工具由引擎自己执行、**不经**
    模型的 tool_call 通道：没有 handler 调用 ⇒ ``ToolCollector`` 里没有该 id 的观察与渲染，
    于是步骤帧退化 :data:`NO_HIT_TEXT`、``tool/result`` 落成 ``content=""`` / ``chars=0``
    ——引擎明明返回了内容，用户与库里读到的都是「什么都没发生」。

    ⚠️ 只认**成功腿**（``is_error`` 为假）：失败腿的文案由
    :func:`_absorb_immediate_failure` 统一给（``X 工具不可用`` / ``X 参数校验失败``），
    这里再补一份会让同一形态出现两种文案。
    ⚠️ 调用方一律把它放在 ``collector.rendered`` **之后**（最后一道兜底）⇒
    既有读数只增不改：``rendered`` 有值、或有观察文本时，本函数的结果永不被取用。
    """
    if bool(getattr(event, "is_error", False)):
        return ""
    return _agent_tool_result_text(getattr(event, "result", None))


async def _absorb_immediate_failure(
    event: Any,
    *,
    collector: ToolCollector,
    spec_map: dict[str, Any],
    fallback_id: str,
    args: dict,
) -> None:
    """``回归修复``：CubeLoop 层**参数校验失败 / 未知工具**的观察与审计补账。

    CubeLoop 在 ``execute`` 之前就把这两类失败定案（``_ImmediateOutcome``：pydantic
    ``model_validate`` 失败 / 工具名不存在）——Start/End 事件照发，但**不调 handler** ⇒
    collector 里没有观察、审计零行，``tool/result`` 落成「``ok=False, error="", content=""``」
    的无因失败行；模型能看到上游英文校错文本，库/审计里却查无此因。

    本函数在落库前识别这一形态（End 事件带 ``is_error`` 且 collector 无观察），
    照 P&E ``_run_plan_step`` 的校验失败口径补一份观察并 ``_absorb_tool`` 进 collector
    ⇒ 事件 ``content``/``error``/``warnings``、审计行、步骤帧四面同源。未知工具与校验
    失败按上游原因文本区分（``f"Tool {name} not found"`` 是 cubeloop tools.py 的常量；
    对不上时归「参数校验失败」，原因仍随文本保留，不丢真因）。

    ⚠️ 正常执行过的调用（哪怕失败）**不进本函数**——handler 自己的观察是唯一事实源；
    ⚠️ ``content`` 记「中文标签 + 上游原因」，与模型实际收到的英文原文有意不同（P&E
    同口径）；上游原文超长（resolved 工具附全量 schema）由既有 ``STEP_CLIP`` 与通道截断兜底。
    """
    if getattr(event, "type", "") != "tool_execution_end":
        return
    if not bool(getattr(event, "is_error", False)):
        return
    rid = str(getattr(event, "tool_call_id", "") or "").strip() or fallback_id
    if collector.observations.get(rid) is not None:
        return
    name = str(getattr(event, "tool_name", "") or "")
    reason = _agent_tool_result_text(getattr(event, "result", None))
    if reason == f"Tool {name} not found":
        label = "工具不可用"
    else:
        label = "参数校验失败"
    observation = ToolObservation(
        text=f"{name} {label}" + (f"：{reason}" if reason else ""),
        ok=False,
        warnings=[f"{name} {label}"],
    )
    await _absorb_tool(
        # _absorb_tool 只读 spec.name；未知工具没有 ToolSpec，用只带名字的替身
        spec=spec_map.get(name) or SimpleNamespace(name=name),
        collector=collector,
        rid=rid,
        args=args,
        observation=observation,
        started=time.perf_counter(),
    )


@contextlib.asynccontextmanager
async def _tool_db_scope(request_db: Any) -> AsyncIterator[Any]:
    """工具执行用的**独立会话**（阶段 4 · 条目 4.4 · 4.4.4）。

    背景（``回归修复`` 后半）：CubeLoop 的工具执行跑在它自己的 asyncio 任务上、
    消费端任务负责事件与审计落库，两者原先**共用同一个请求级 ``AsyncSession``**——
    驱动不允许同连接并发使用，实测撞出 ``readexactly() called while another coroutine is
    already waiting for incoming data``（``aclose()`` 路径）。4.3 的 ``_session_lock`` 是**绕**
    （把并发压成串行），这里从根上分开：工具任务用自己的会话 ⇒ 请求会话只剩消费端一个使用者。

    三条口径：

    1. **建会话与关会话只有一处实现**——:func:`app.db.session.independent_session`
       （按**请求会话所属的引擎**新建，不用全局 ``SessionLocal``：单测里会话绑在用例自己的
       临时引擎上，用全局引擎会跨事件循环；退出时它负责关闭）。本函数**只做回落适配**，
       不另抄一份"取引擎 + 新建 + 关闭"（两处实现迟早漂移，4.2/4.3 都吃过这个亏）；
    2. **建不出来就回落**：用请求会话本身（旧行为）。回落路径有四条：
       没传请求会话（工具级调用 / 单测）、会话没有绑定引擎、绑定的是同步引擎、
       以及新建过程自身抛错——**优化项绝不降级功能**；
    3. ``_session_lock`` **保留**：审计收口 / 事件写入仍在请求会话上、由消费端串行做，
       锁还在守着「同一轮里没有别的任务在动它」，删它必须等实测转绿（见指令包 §2.8）；
    4. ``yield`` **恒恰好一次、不在 except 内**（``回归修复``）：旧形状
       （``try: async with …: yield … except: yield``）让「``async with`` 体里逃出的异常」
       经 ``__aexit__`` 的 ``athrow`` 注进本生成器后命中 except 并**再次 yield**——
       contextlib 对「athrow 后未停」一律 ``RuntimeError`` ⇒ 原始异常对象被丢弃、
       日志还误报「独立工具会话不可用」。现形状：**先建会话、建失败才回落**，
       体内外异常原样穿出（finally 只负责关会话，回落语义与第 2 条完全一致）。
    """
    session_ctx = independent_session(request_db)
    tool_db: Any = None
    entered = False
    try:
        tool_db = await session_ctx.__aenter__()
        entered = True
    except Exception as exc:  # noqa: BLE001 —— 建不出来就回落，绝不炸穿本轮
        logger.warning(
            "独立工具会话不可用（回落共用请求会话）：%s: %s", exc.__class__.__name__, exc
        )
    try:
        yield tool_db if tool_db is not None else request_db
    finally:
        if entered:
            # __aenter__ 抛出 ⇒ 会话从未建立，不该（也不能）调 __aexit__
            await session_ctx.__aexit__(None, None, None)


@contextlib.asynccontextmanager
async def _session_lock(lock: "asyncio.Lock | None") -> AsyncIterator[None]:
    """**一把锁，管住整轮的 AsyncSession**（条目 4.3 对抗性核验 P1 的修法）。

    为什么要它：CubeLoop 的**工具执行**跑在它自己的 asyncio 任务上（`Agent.prompt()` 那个
    `ensure_future`），而**事件落库与审计写库**跑在消费端任务上——两者共用同一个请求级
    `AsyncSession`。数据库驱动**不允许同一连接并发使用**：实测出现
    ``InvalidRequestError: This session is in 'prepared' state; no further SQL can be emitted
    within this transaction.``——收口失败 ⇒ 库里留一行永远 ``running`` 的僵尸行（确定性复现）。

    这把锁在 :func:`stream_agent_turn` 里创建（那里在事件循环内）、传给**每一个**碰 session 的
    调用点（事件写入 / 审计写入 / 收口）。拿不到锁就等着——反正对面也是同一个请求，
    等的是毫秒级的事。锁是**可选**的：不传（单测 / 工具级调用）就是零开销直通。
    """
    if lock is None:
        yield
        return
    async with lock:
        yield


async def _flush_audit(
    collector: ToolCollector,
    turn: AgentTurn,
    *,
    db: Any,
    state: dict[str, Any],
    db_lock: "asyncio.Lock | None" = None,
) -> None:
    """把 ``collector.audits`` 里**尚未落库**的记账逐条写成 ``agent_steps``。

    ``state`` 是本轮的写入进度（**可变对象**，ReAct 与 P&E 各持一份，见 ``audit_state``），
    两个键分工明确：

    - ``written``：已**成功**写库的条数（游标）；
    - ``attempted``：已**试过**的条数（含失败的）。

    ⚠️ **失败不重试、但也不阻断后面的**（对抗性核验打回后改的，改之前先读完）：
    本函数在 drain 循环里**每来一个事件就调一次**、P&E 是**每步调一次**。原来的写法是
    「失败就 `break`、`written` 停在失败处」，结果是那一条之后的记账**本轮全都不写**——
    实测留下 `step_no=[1,3]` 这种**带洞且无缺口标记**的审计（读的人分不清「没调」与「丢了」）。
    现在的口径是：

    - 失败 ⇒ **跳过这一条**、继续写后面的（每一条**只试一次**，``attempted`` 无条件推进）；
    - 「跳过了一条」这件事由 :func:`app.agent.audit.audit_write_failures` 的**增量**记着
      ——**看得见**，不是静默（`agent_trace` 的键集是**冻结**的，不借道那个字段）。

    ⚠️ 也**不能**「失败就不推进游标、下次再试」：同一条会在一次工具调用里被反复重试，
    实测把失败计数顶到 **23**，让这个信号彻底失去意义（`回归修复` 的口径）。
    """
    while state["attempted"] < len(collector.audits):
        index = state["attempted"]
        item = collector.audits[index]
        state["attempted"] = index + 1
        # ⚠️ 写库必须过会话锁（CubeLoop 的任务与消费端任务共用同一个 AsyncSession）
        async with _session_lock(db_lock):
            ok = await agent_audit.write_tool_step(
                db,
                turn.run_id,
                step_no=item.step_no,
                tool_name=item.tool_name,
                tool_call_id=item.tool_call_id,
                ok=item.ok,
                content=item.content,
                ms=item.ms,
                args=item.args,
                # 写通道专属三键（`回归检查`）：只读工具恒 `None` ⇒ 那条 meta 逐字不变
                write_meta=item.write_meta,
            )
        if ok:
            continue
        # 跳过但**留痕**。为什么不去动 `agent_trace` 的形状：`trace()` 的键集是**冻结**的
        # （`{mode, steps, events_incomplete}`，4.1/4.2 的作用例都在钉它，02 §7 也只认这三个）。
        # 「审计缺了几行」的观测出口是 `audit.audit_write_failures()`（进程内计数）
        # ——与 `events_incomplete` 同型，但不借道那个已冻结的字段。
        logger.warning(
            "审计缺行（已跳过并继续，主流程不受影响）：tool=%s tool_call_id=%s step_no=%s "
            "（累计失败 %s 次）",
            item.tool_name,
            item.tool_call_id,
            item.step_no,
            agent_audit.audit_write_failures(),
        )


# --------------------------------------------------------------------------
# 主循环
# --------------------------------------------------------------------------


def _audit_run_status(turn: AgentTurn) -> str:
    """本轮 ``agent_runs.status`` 的收口值（**所有出口**共用这一处判定）。

    - ``aborted`` ⇒ ``stopped``（用户在流中途按了停止、或 CubeLoop 报 ``agent_aborted``）；
    - ``model_error``（``回归修复``）⇒ ``error``：本轮**决策模型调用失败过**
      （CubeLoop 上游 ``stop_reason="error"``）。轮次照常收尾、回答链路照常作答（I5），
      但 run 行不再与「一切正常」混同为 ``done``——审计上必须分得清
      「这一轮跑完了」与「这一轮的模型调用失败过」（``error`` 已入 ``RUN_STATUSES`` 取值域）；
    - 其余 ⇒ ``done``：注意**工具炸 / 装配失败**都归 ``done``——
      它们在本运行器里都是**已收尾的正常出口**（转成了 ``note``，回答链路照常作答，I5）。
      把「工具失败」记成 ``failed`` 会让 ``4.4`` 的投影与断路器分不清
      「这一轮没跑成」（基础设施问题）与「这一轮跑了、只是某个工具没成」（业务正常）。

    ``failed`` 只留给**异常真的逃出主循环、且不是「这一轮被中止」**的情形。
    ⚠️ ``GeneratorExit`` 与 ``CancelledError`` **不算 failed**：前者是「调用方关闭了这个生成器」
    （用户在流中途按停止 / 客户端断开 ⇒ ``chat_routes`` 的 ``aclose()``），后者是任务被取消
    ——两者都是「这一轮被中止」，与 ``aborted`` 同义。对抗性核验实测：不区分时，**停止轮会被
    误记成 ``failed``**，把「用户按了停止」读成「Agent 崩了」。
    """
    if turn.aborted:
        return "stopped"
    if turn.closed_by_generator_exit:
        return "stopped"
    if turn.model_error:
        return "error"
    return "done"


def _finish(
    turn: AgentTurn,
    collector: ToolCollector,
    before: event_log.FailureSnapshot | int,
    *,
    turn_key: tuple[int | None, int | None] | None = None,
) -> None:
    """收口：把累积结果写回 ``turn``（每条退出路径都要走到，包括提前 return）。

    **只做纯记账，不碰 db**——``agent_runs`` 的收口**不在这里**，在 :func:`_audit_run`
    那个上下文管理器里（它才是「四条退出路径都覆盖」的那一处，且它是 ``async`` 的；
    本函数是同步函数，拿不到 ``await``）。原先的 docstring 把收口点写在这里、还留了个
    从未使用的 ``db`` 形参，已按实测更正（对抗性核验的 G5）。

    ``events_incomplete`` 的归属是**本轮**（``回归修复``）：4.1/4.2 用的是
    ``event_write_failures()`` 的**进程全局**计数增量——别的会话写失败一次，本轮就会
    误报「缺行」（方向安全：只假阳性，但会把这个信号贬值）。现在按
    :func:`app.session.events.failures_since` 只认**本会话本轮**的失败。

    **条目 4.4 再加一条来源**：``event_incomplete``（被取消 / 未写完的缺行，D5）。
    ``CancelledError`` 既不是写失败、原先也**根本没有出口**（``回归修复``），
    现在它按轮记账，所以「本轮日志缺了几行」这个信号把取消也算进去。
    两条都按 ``(chat_id, turn)`` 归属，**都只读本轮桶**（不许再用进程全局数——
    那正是 ``回归修复`` 的形状）。`reason` 落在 ``turn.events_incomplete_reason``
    （**不进 trace()**：那个键集是冻结的三键，见 :meth:`AgentTurn.trace`）。

    ``turn.tool_context``（专项工具调用第 0 批 · ①）与 ``turn.materials`` **同型收口**
    ——放在这里而不是散在四条退出路径上，是因为「每条退出路径都要走到」这条纪律对
    材料已经验证过一次（停止轮 / 异常轮 / 装配失败轮都靠它），通道没有理由另开一处。
    排序真源是 ``collector.call_order``（``tool/call`` 的事件序，D25），**不是**本函数
    的参数、也不是 ``observations`` 的自然序。

    ⚠️ **I5 回落（开工包 A20）**：通道的**构造**（排序 / 截断 / 建条目）任一步抛错 ⇒
    **回落空通道**，只记 warning，**绝不让它炸穿本轮**。为什么是「回落空通道」而不是
    「原样抛出」：抛出去会被外层兜成 ``error`` 收尾（实测：假上游那轮变成
    ``generator didn't stop after athrow()`` 的报错），而这一轮的**工具其实跑完了**、
    回答链路也好好的 —— 为一段**附加上下文**的排版失败把整轮做废，代价与收益完全不成比例。
    空通道也不违反任何判据：D8 已经保证「空通道 ⇒ 请求体逐字节 no-op」，
    所以回落态就是一个**本批之前的正常轮**。
    """
    turn.materials = collector.materials
    turn.warnings = collector.warnings
    try:
        turn.tool_context = collector.tool_context_items()
    except Exception as exc:  # noqa: BLE001 —— 通道是附加物，绝不炸穿本轮（I5 / A20）
        # `回归修复`：只在**回落路径**计数（成功构造不计数）⇒「通道降级发生过」
        # 在 `GET /api/agent/audit` 的 `counters.derived.tool_result_channel` 上机器可读。
        derived_counters.bump("tool_result_channel")
        logger.warning("工具结果通道构造失败（已回落空通道）：%s: %s", exc.__class__.__name__, exc)
        turn.tool_context = []
    if turn_key is None:
        turn.events_incomplete = event_log.failures_since(before)
        return
    failures = event_log.failures_since(before, turn_key=turn_key)
    incomplete, reason = event_log.turn_event_incomplete(turn_key)
    turn.events_incomplete = bool(failures) or incomplete > 0
    turn.events_incomplete_reason = reason
    if reason not in (event_log.INCOMPLETE_NONE,):
        logger.warning(
            "本轮会话事件缺行（turn_key=%s）：event_incomplete=%s reason=%s",
            turn_key,
            incomplete,
            reason,
        )


@contextlib.asynccontextmanager
async def _audit_run(
    turn: AgentTurn,
    *,
    db: Any,
    chat_id: int | None,
    meta: dict | None = None,
    db_lock: "asyncio.Lock | None" = None,
) -> AsyncIterator[None]:
    """``agent_runs`` 的生命周期（条目 4.3.2）：**建行 → 跑 → 收口 status**。

    做成上下文管理器而不是散落的 ``try/finally``：四条退出路径（正常 return /
    ``CancelledError`` / ``GeneratorExit``（生成器被 ``aclose()``）/ 其它异常）都必须收口，
    写成上下文管理器就只有一处能写错。

    收口值：

    - 正常出口 ⇒ :func:`_audit_run_status`（``aborted`` / 被中止 ⇒ ``stopped``，其余 ``done``）；
    - ``CancelledError`` ⇒ ``stopped``（任务被取消 = 这一轮被中止）；
    - ``GeneratorExit`` ⇒ ``stopped`` **且标记 ``turn.closed_by_generator_exit``**
      （调用方 ``aclose()`` 了生成器：用户在流中途按停止 / 客户端断开。对抗性核验实测：
      原先一律记 ``failed``，把「用户按了停止」读成「Agent 崩了」）；
    - 其它 ``BaseException`` ⇒ ``failed``（真的逃出主循环）。

    **收口本身绝不炸穿**（I5）：``finish_run`` 内部就是 safe 形状；这里再兜一层是因为
    收口可能发生在**请求 session 已关闭**之后（GC 延迟关闭的情形）——那时
    ``ResourceClosedError`` 会从 ``finish_run`` 的 get 里冒出来（实测 6/6），
    结果就是**库里留一行永远 ``running`` 的僵尸行**。宁可只留 warning，也不放僵尸行出去。

    ⚠️ **``start_run`` 与收口都必须过会话锁**（``db_lock``）——本函数是**碰 session 的第五个
    调用点**，而它原先漏在锁外，实测症状是 AIOMySQL 层的
    ``RuntimeError: readexactly() called while another coroutine is already waiting for
    incoming data``（两个协程在**同一个连接**上并发读）+ teardown error。
    死锁自查：锁的持有者只可能是 CubeLoop 的工具任务；本函数在**调用方任务**上运行，
    进入时锁从未被本任务持有，退出时（``except``/``else`` 分支里）本任务也没持锁
    ⇒ **不会自锁**。锁是**可选**的：不传（单测 / 工具级调用）就是零开销直通。
    """
    # ⚠️ ``audit.start_run`` 在开关关 / 没 db / 没 chat_id 时返回 ``None``，
    # 此时整段是零 DB 触碰的直通（单测与工具级调用大量走这条路）。
    async with _session_lock(db_lock):
        turn.run_id = await agent_audit.start_run(
            db, chat_id=chat_id, mode=turn.mode, meta=meta
        )
    try:
        yield
    except asyncio.CancelledError:
        turn.aborted = True
        await _safe_finish(db, turn, status="stopped", db_lock=db_lock)
        raise
    except GeneratorExit:
        # 生成器被 ``aclose()``：调用方中止了这一轮（停止 / 断开），**不是**引擎故障。
        # 先按 `stopped` 收口，再把 `GeneratorExit` 原样抛出（生成器的契约要求）。
        turn.closed_by_generator_exit = True
        await _safe_finish(db, turn, status="stopped", db_lock=db_lock)
        raise
    except BaseException as exc:  # noqa: BLE001 —— 收口后原样抛出（不吞调用方的异常）
        await _safe_finish(
            db, turn, status="failed", error=f"{exc.__class__.__name__}: {exc}", db_lock=db_lock
        )
        raise
    else:
        await _safe_finish(db, turn, status=_audit_run_status(turn), db_lock=db_lock)


async def _safe_finish(
    db: Any,
    turn: AgentTurn,
    *,
    status: str,
    error: str | None = None,
    db_lock: "asyncio.Lock | None" = None,
) -> None:
    """``finish_run`` 的二次防护：**收口失败也不许抛**，且**尽量不留僵尸行**。

    两段式（对抗性核验 P1 的收尾）：

    1. 直接收口（**过会话锁**）；
    2. 失败时 **``rollback()`` 后重试一次**——「``This session is in 'prepared' state``」
       这类失败是**会话事务状态**问题（有别的任务把这个 session 用在半途），
       回滚把状态清干净之后再写一次，往往就成功了。

    仍然失败 ⇒ 只留一条 warning。**如实登记边界**：请求级 ``AsyncSession`` 被两个任务
    共用是 v2 已知的结构性约束，真正的根治要等「Agent 用独立 session」那条改造；
    本条只保证「常见路径不留僵尸行」。
    """
    for attempt in (1, 2):
        try:
            async with _session_lock(db_lock):
                await agent_audit.finish_run(db, turn.run_id, status=status, error=error)
            return
        except BaseException as exc:  # noqa: BLE001 —— 收口不炸穿（I5）
            # 带**最内层**一帧调用点（不是整棵栈）：这条 warning 只出现在并发/关闭竞态下，
            # 定位时最需要知道的是「收口挂在哪一步」（`finish_run` 的 get / flush / rollback），
            # 整棵 traceback 会把日志淹没，而单帧足够指向那一步。
            where = _innermost_frame(exc)
            logger.warning(
                "agent_runs 收口失败（第 %s/2 次，已隔离）：%s: %s [%s]",
                attempt,
                exc.__class__.__name__,
                exc,
                where,
            )
            if attempt == 1:
                try:
                    async with _session_lock(db_lock):
                        await db.rollback()  # 清掉「prepared / 半途事务」状态，给第二次机会
                except BaseException:  # noqa: BLE001 —— 回滚失败也要继续重试
                    pass


def _innermost_frame(exc: BaseException) -> str:
    """``文件名:行号:函数名`` —— 异常 traceback 里**最内层**那一帧（收口失败时定位用）。

    为什么不打整棵栈：这条 warning 只在并发/关闭竞态下出现，排查时要回答的是「挂在
    ``finish_run`` 的哪一步」（get / flush / rollback），单帧就能指到，整栈反而淹没日志。
    """
    tb = exc.__traceback__
    if tb is None:
        return "no-traceback"
    while tb.tb_next is not None:
        tb = tb.tb_next
    code = tb.tb_frame.f_code
    return f"{code.co_filename.rsplit(chr(92), 1)[-1]}:{tb.tb_lineno}:{code.co_name}"


async def stream_agent_turn(
    turn: AgentTurn,
    *,
    question: str,
    history: list[dict] | None = None,
    model: Any = None,
    db: Any = None,
    filters: dict | None = None,
    web_enabled: bool = False,
    chat_id: int | None = None,
    turn_no: int | None = None,
    trace_id: str | None = None,
    tools: list[Any] | None = None,
    registry: ToolRegistry | None = None,
    max_rounds: int | None = None,
    candidates: str = "",
    **deps: Any,
) -> AsyncIterator[tuple[str, dict]]:
    """跑一轮 Agent，产出 ``("agent", step_dict)`` 若干次。

    ``turn.mode`` 决定走哪条路（条目 4.2；两条路径共用工具装配与材料累积）：

    - ``plan_execute`` → :func:`_stream_plan_execute`（计划表驱动）；
    - 其余（``react`` / ``off``）→ CubeLoop 决策循环（4.1 原样，**逐字未动**）。

    结束时 ``turn`` 上带着 ``steps`` / ``materials`` / ``warnings`` / ``aborted`` / ``note``。
    **不抛业务异常**（I5）；调用方只需要处理「产出帧」与「读取 turn」两件事。
    """
    from cubeloop import Agent
    from cubeloop.providers.base import ReasoningControl
    from app.agent.disclosure import build_disclosure

    failures_before = event_log.failure_snapshot()
    turn_key = (chat_id, turn_no)
    limit = max_rounds if max_rounds is not None else agent_max_rounds()
    started_at: dict[str, float] = {}
    collector = ToolCollector()
    # 本轮唯一的兜底工具 id（`回归修复`）：执行闭包 / 事件写入 / 步骤帧**共用**它，
    # 否则 CubeLoop 给空 id 时 collector 的键与 `tool/call` 的配对键会对不上。
    fallback_id = new_tool_call_id()
    # 通道也用它归一空 id（口径同源：三处 + 通道都是同一个兜底 id，见 `_channel_key`）。
    collector.fallback_id = fallback_id
    # 本轮实际用的注册表（P&E 的闸③ 反查 spec 靠它；**不能用形参 registry** —— 生产为 None）
    spec_map: dict[str, Any] = {}
    #: **本轮唯一的会话锁**：CubeLoop 的工具任务与消费端任务共用同一个 AsyncSession，
    #: 并发使用会撞出 `This session is in 'prepared' state` ⇒ 收口失败 ⇒ 僵尸 `running` 行。
    #: 所有碰 session 的调用点都必须过它（见 `_session_lock`）。
    db_lock = asyncio.Lock()

    async with _tool_db_scope(db) as tool_db, _audit_run(
        turn,
        db=db,
        chat_id=chat_id,
        meta={"retrieval_id": trace_id, "turn": turn_no},
        db_lock=db_lock,
    ):
        # R2-03 · 轮级预算的**计时起点**必须是这一句 —— 即 `adiscover_tools()` **之前**
        # （D-回归检查）。为什么起点要前移到发现之前：MCP 的逐包发现 8s **串行**、今天**没有整轮
        # 上限**（`mcp_transport._adiscover` 的 for 循环里失败只 `log.warning + continue`）
        # ⇒ 最坏 ≈ 8s × 许可包数，**调用方取消不掉**。R2-04 要治的正是它，而它只有在
        # 「发现与本轮决策循环共用同一份预算」时才治得住。
        deadline = _TurnDeadline(
            agent_turn_deadline_seconds(), started_at=time.monotonic()
        )
        try:
            # D-k：装配前发现；零许可 ⇒ mcp_specs=None（老分支逐字不变）
            mcp_specs = None
            if tools is None:
                try:
                    from app.agent.mcp_transport import adiscover_tools

                    # R2-04：把**本轮剩余预算**交给发现（``None`` = 不限 ⇒ 逐字等价老分支）。
                    # 为什么必须传：逐包 8s 的串行循环今天没有整轮上限（最坏 ≈ 8s × 许可包数），
                    # 而调用方取消不掉 —— 只有「发现与本轮决策循环共用同一份预算」才治得住。
                    mcp_specs = await adiscover_tools(budget_seconds=deadline.remaining())
                except Exception as exc:  # noqa: BLE001
                    logger.warning("MCP 发现失败：%s: %s", exc.__class__.__name__, exc)
                    mcp_specs = None
            toolkit = build_agent_tools(
                # ⚠️ 工具装配用**独立会话**（4.4.4）：工具执行跑在 CubeLoop 自己的任务上，
                # 与消费端任务共用请求级 session 就会撞并发（见 `_tool_db_scope`）。
                # 事件与审计仍走请求会话 `db`（消费端任务，串行）。
                db=tool_db,
                filters=filters,
                web_enabled=web_enabled,
                registry=registry,
                collector=collector,
                turn=turn,
                fallback_id=fallback_id,
                db_lock=db_lock,
                mcp_specs=mcp_specs,
                **deps,
            )
            agent_tools = toolkit.tools
            collector = toolkit.collector or collector
            # ✅ 用它**实际用的那份** registry 建反查表（对抗性核验的 G6：透传形参会恒空——
            # 生产的 `registry` 形参是 `None`，`spec_map` 会是 `{}` ⇒ P&E 的闸③ 结构性不生效）
            spec_map = toolkit.spec_map()
        except Exception as exc:  # noqa: BLE001 —— 装配失败也不炸穿
            logger.warning("Agent 工具装配失败：%s: %s", exc.__class__.__name__, exc)
            yield "agent", turn.note_step("引擎异常", f"Agent 工具装配失败：{exc}")
            _finish(turn, collector, failures_before, turn_key=turn_key)
            return

        if tools is not None:  # 注入优先（测试/高级调用方完全替换清单）
            agent_tools = list(tools)
        if not agent_tools:
            yield "agent", turn.note_step("没有可用工具", "本轮没有可调用的工具，直接进入作答。")
            _finish(turn, collector, failures_before, turn_key=turn_key)
            return
        if model is None:
            yield "agent", turn.note_step("模型不可用", "尚未配置对话模型，无法进行 Agent 编排。")
            _finish(turn, collector, failures_before, turn_key=turn_key)
            return

        if turn.mode == "plan_execute":
            # 条目 4.2：真 Plan-and-Execute——**计划表**驱动下一步（老基线同构）。
            # 与下面 ReAct 循环的分派点是「谁决定下一步」，不是同一段代码的参数差；
            # ReAct 路径的代码因此逐字不动（条目前置：零回归）。
            try:
                async for frame in _stream_plan_execute(
                    turn,
                    question=question,
                    tools=agent_tools,
                    collector=collector,
                    model=model,
                    history=history,
                    db=db,
                    chat_id=chat_id,
                    turn_no=turn_no,
                    trace_id=trace_id,
                    fallback_id=fallback_id,
                    spec_map=spec_map,
                    deadline=deadline,
                ):
                    yield frame
                if not turn.aborted:
                    turn.closeout = {
                        "action": "answer",
                        "use_candidates": True,
                        "clarification": "",
                    }
                    turn.closeout_invalid = False
            except asyncio.CancelledError:
                # 停止路径：与 ReAct 同口径（预期，不是错误；stopped 帧由 _produce 发）
                turn.aborted = True
                raise
            finally:
                _finish(turn, collector, failures_before, turn_key=turn_key)
            return

        queue: asyncio.Queue = asyncio.Queue()
        rounds_at_call: dict[str, int] = {}
        pending_tool_events: list[Any] = []
        ended_calls: set[str] = set()
        #: ``回归修复``：发起时的原始参数（End 事件不带 args，而立即失败补账的
        #: 审计行要记参数摘要——与 ``tool/call`` 事件同一份来源，键用同一套归一）。
        args_at_call: dict[str, dict] = {}

        def _on_event(event, _signal=None) -> None:
            # ⚠️ 轮次计数**必须在这里累加**（监听器是 CubeLoop 同步调用的），不能放到消费端：
            # 消费端（_drain → _steps_for）与 Agent 循环是两个并发任务，Agent 可能一口气跑完
            # 好几轮而消费端还没处理到 —— 那样 `should_stop_after_turn` 读到的轮次是**过期值**，
            # 上限形同虚设（实测：上限 3 时跑满 8 轮）。
            kind = getattr(event, "type", "")
            if kind in ("tool_execution_start", "tool_execution_end"):
                # 回归修复：空 id 在**唯一铸造点**这里换成「每次调用各自」的 id。监听器由
                # CubeLoop **同步**调用，且同一任务内 Start 事件与 ``execute`` 相邻（中间没有
                # 让出点）⇒ 铸造序 = 发起序 = 执行侧认领序。改写事件对象之后，下面的轮次记账 /
                # 参数记账 / 立即失败补账 / 事件写入 / 步骤帧**全部自动同源**（都读事件上的 id），
                # 不必各自改判据。非空 id（含带空白的非空）路径逐字不变。
                if not str(getattr(event, "tool_call_id", "") or "").strip():
                    _tool_name = str(getattr(event, "tool_name", "") or "")
                    _minted = (
                        collector.note_blank_call_start(_tool_name)
                        if kind == "tool_execution_start"
                        else collector.close_blank_call(_tool_name)
                    )
                    if _minted:
                        event.tool_call_id = _minted
            if kind == "turn_start":
                turn.rounds += 1
            elif kind in ("tool_execution_start", "tool_execution_end"):
                pending_tool_events.append(event)
                if kind == "tool_execution_end":
                    ended_calls.add(str(getattr(event, "tool_call_id", "") or "").strip())
                # 同理：工具事件要带上**它发生那一刻**的轮次，事后读 turn.rounds 会拿到后续轮的值
                # （`tool/call` 载荷里的 round 因此不能是「消费端此刻的轮次」）。
                rounds_at_call[str(getattr(event, "tool_call_id", "") or "")] = turn.rounds
            if kind == "tool_execution_start":
                _rid = str(getattr(event, "tool_call_id", "") or "").strip() or fallback_id
                args_at_call[_rid] = dict(getattr(event, "args", None) or {})
                # 通道排序的**真源**（专项第 0 批 · D25）：监听器由 CubeLoop **同步**调用，
                # 所以这里的追加序 = **发起序**（与 `tool/call` 事件同源；消费端任务
                # 串行处理队列，所以「事件序」与「这里」是同一个序）。
                # ⚠️ **不能**用 `collector.observations` 的自然序（那是**完成序**，
                # 并行工具下与发起序不同 ⇒ A7 会结构性失败或写成永远绿的假判据）。
                collector.note_call_order(_rid)
            queue.put_nowait(event)

        # R2-08 · 渐进式工具披露（dispatch 策略）：模型看到的是「目录 + 两个内置工具 +
        # 原生态工具」，折叠掉的工具**仍留在工具表里**（`expose_to_model=False`）⇒
        # 引擎照旧按名字解析与执行，既有直接点名调工具的链路行为不变。
        # ⚠️ 策略**写死 dispatch**：官方语义 = tools 数组与 system prompt **整轮字节稳定**
        # （零 prompt-cache 失效）；`inject` 会在每次扩展时重读缓存，本条不用。
        # 披露只覆盖这一条（ReAct）链；P&E 的计划提示词仍逐字渲全量清单（如实登记）。
        model_tools, deferred_groups = build_disclosure(agent_tools)
        agent = Agent(
            model=model,
            system_prompt=DECISION_SYSTEM_PROMPT,
            tools=model_tools,
            middleware=[],
            deferred_tool_groups=deferred_groups,
            deferred_tool_strategy="dispatch",
            reasoning=ReasoningControl(mode="off"),
            on_payload=_cap_decision_payload,
            should_stop_after_turn=_stop_after_rounds(limit, turn, deadline),
        )
        agent.subscribe(_on_event)

        from app.usage.instrumentation import usage_scope
        with usage_scope(role="agent_decision"):
            prompt_task = asyncio.ensure_future(
                agent.prompt(decision_prompt_with_candidates(question, history, candidates=candidates))
            )
        # ⚠️ `_drain` 的**生成器本体**留一个句柄：退出时要能力争把它 aclose 掉，
        # 否则它内部那个挂着的 `queue.get()` 会留成 pending 任务
        # （实测报「Task was destroyed but it is pending」），并可能在收口之后继续碰 session。
        drain_gen = _drain(queue, prompt_task)
        audit_state: dict[str, Any] = {"written": 0, "attempted": 0}
        try:
            async for event in drain_gen:
                # 回归修复：立即失败（校验失败/未知工具）在落库前补观察+审计行——
                # 必须先于 _write_tool_event（content/error 从 collector 取）与 _steps_for。
                await _absorb_immediate_failure(
                    event,
                    collector=collector,
                    spec_map=spec_map,
                    fallback_id=fallback_id,
                    args=args_at_call.get(
                        str(getattr(event, "tool_call_id", "") or "").strip() or fallback_id
                    )
                    or {},
                )
                # 先登记尝试；取消/落库失败后不重试同一事件。
                if event in pending_tool_events:
                    pending_tool_events.remove(event)
                await _write_tool_event(
                    event,
                    db=db,
                    chat_id=chat_id,
                    turn_no=turn_no,
                    round_no=rounds_at_call.get(
                        str(getattr(event, "tool_call_id", "") or ""), turn.rounds
                    ),
                    trace_id=trace_id,
                    collector=collector,
                    fallback_id=fallback_id,
                    turn_key=turn_key,
                    db_lock=db_lock,
                )
                # 审计写库**只在消费端任务上做**（见 `ToolAudit` 的并发说明）：
                # 事件处理完顺手把已记账、未落库的工具调用补写掉，写失败也不炸穿。
                await _flush_audit(
                    collector, turn, db=db, state=audit_state, db_lock=db_lock
                )
                for frame in _steps_for(
                    event,
                    turn=turn,
                    collector=collector,
                    started_at=started_at,
                    fallback_id=fallback_id,
                ):
                    yield "agent", frame
            # ``回归修复``：轮次侧与 deadline 侧同款「谁停的」判别——
            # ``rounds_capped`` 只在「被上限**强制**停」（钩子在有工具的轮尾观察到
            # ``rounds >= limit``）时置位；模型自然收尾恰好落在上限时钩子虽返回 True，
            # 但循环本来就要结束，不能把「模型自主收手」报成「配额强制截断」。
            if turn.rounds_capped and not turn.aborted:
                yield "agent", turn.note_step("已达上限", f"已达最大轮次 {limit}，用现有资料作答")
            elif deadline.triggered and not turn.aborted:
                # R2-03 · 到点收尾：**正常收尾**，不是中止 —— 这里**绝不**动 ``turn.aborted``
                # （置真会把 ``agent_runs.status`` 记成 ``stopped``、并让 chat_routes 跳过
                # 结束决策，见 :class:`_TurnDeadline` 的说明）。条件用 ``triggered`` 而不是
                # ``expired()``：后者会在「模型自己收手、时钟刚好也过点」时误报。
                note = turn.note_step(
                    "已达时间上限",
                    f"本轮已用满时间上限 {deadline.seconds} 秒，用现有资料作答",
                )
                yield "agent", note
        except asyncio.CancelledError:
            # 停止路径：**预期**，不是错误。abort 让 CubeLoop 把在途工具收尾，
            # 抽到 stopped 帧由外层（_produce）负责。
            turn.aborted = True
            raise
        except Exception as exc:  # noqa: BLE001 —— 引擎异常绝不炸穿（I5）
            logger.warning("Agent 循环异常（已隔离）：%s: %s", exc.__class__.__name__, exc)
            yield "agent", turn.note_step("引擎异常", f"Agent 过程出现意外错误，已终止：{exc}")
        finally:
            # ⚠️ **退出前必须先让在途的 CubeLoop 任务停干净**（对抗性核验 P1 的根因）：
            # 调用方（``_produce`` 的停止分支 / 生成器 ``aclose()``）只关掉了**外面这一层**
            # 生成器，CubeLoop 的 ``prompt_task`` 与 ``_drain`` 的任务当时还在跑——它们会在
            # 我们**之后**继续碰这个请求级 ``AsyncSession``（工具执行读库 / 写事件），
            # 于是审计收口撞上 ``This session is in 'prepared' state``，
            # 最后一步「把 status 写成 stopped」失败 ⇒ 库里留一行**永远 running 的僵尸行**。
            # 实测：不等干净的版本 6/6 复现僵尸行 + `status` 误记 + trace 丢字段。
            agent.abort()
            if not prompt_task.done():
                prompt_task.cancel()
            try:
                await prompt_task
            except BaseException:  # noqa: BLE001 —— 取消 / prompt 失败都已在上面登记
                pass
            # ``_drain`` 的生成器也要收掉，别留一个 pending 的 `queue.get()`
            # （「Task was destroyed but it is pending」就是它）。它可能正挂在 `await queue.get()`，
            # 所以给一个短超时：收不掉也不影响收口（那只是事件循环里一条待回收的 getter）。
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(drain_gen.aclose(), timeout=1.0)
            # 仅补消费尚未尝试的真实事件；不根据副作用或观察伪造结束事件。
            for event in pending_tool_events:
                await _absorb_immediate_failure(
                    event, collector=collector, spec_map=spec_map, fallback_id=fallback_id,
                    args=args_at_call.get(str(getattr(event, "tool_call_id", "") or "").strip(), {}),
                )
                await _write_tool_event(
                    event, db=db, chat_id=chat_id, turn_no=turn_no,
                    round_no=rounds_at_call.get(str(getattr(event, "tool_call_id", "") or ""), turn.rounds),
                    trace_id=trace_id, collector=collector, fallback_id=fallback_id,
                    turn_key=turn_key, db_lock=db_lock,
                )
            if turn.aborted and set(args_at_call) - ended_calls:
                event_log.note_event_cancelled(turn_key)
            _finish(turn, collector, failures_before, turn_key=turn_key)
            # 收尾兜底：万一某个工具调用的事件没到（异常 / 截断 / 取消），审计还有最后一次
            # 补写机会。正常路径这里是无操作（``state["attempted"]`` 已等于记账条数）。
            # 二次防护：`_flush_audit` 依赖 `write_tool_step` 是 safe 形状（内部吞异常），
            # 那里一旦改成会抛的实现，异常会从生成器逃出并破 I5——所以这里自己兜一层。
            try:
                await _flush_audit(
                    collector, turn, db=db, state=audit_state, db_lock=db_lock
                )
            except Exception as exc:  # noqa: BLE001 —— 审计绝不炸穿（I5）
                logger.warning("审计补写失败（已隔离）：%s: %s", exc.__class__.__name__, exc)


def _stop_after_rounds(limit: int, turn: AgentTurn, deadline: _TurnDeadline):
    """``should_stop_after_turn`` 钩子：轮次达上限或**轮级时间到点**即停。

    ⚠️ 必须是 ``async def``：CubeLoop 对钩子统一 ``await``，传同步函数会 ``await bool`` 报错。
    ⚠️ 形状照旧（普通 ``def`` 返回内层 ``async def``）—— ``should_stop_after_turn`` **不是本仓
    符号**，它是 cubeloop 的 Agent kwarg，由上游 ``cubeloop/agent/loop.py:374`` ``await`` 消费。

    R2-03：**这是 ReAct 侧唯一的到点检查点**（CubeLoop 每轮结束调一次，即「轮与轮之间」）。
    命中时置 :attr:`_TurnDeadline.triggered` —— 收尾 note 只认它（谁停的就说谁），
    并让循环**自然结束**（不抛、不动 ``turn.aborted``）。

    ``回归修复``：轮次上限侧同款「谁停的」判别——只有**本轮真的跑过工具**
    （``_ctx.tool_results`` 非空）时，``rounds >= limit`` 的 True 才是「**被上限强制停**」：
    无工具轮的循环本来就自然结束（``has_more_tool_calls`` 已为 ``False``），钩子返回 True
    与 False 对流程无差别。模型自然收尾恰好落在上限时（无工具轮），:attr:`AgentTurn.rounds_capped`
    保持 ``False``，收尾不再误报「已达最大轮次」。
    """

    async def _should_stop(_ctx) -> bool:
        capped = turn.rounds >= limit
        if capped and getattr(_ctx, "tool_results", None):
            turn.rounds_capped = True
        if deadline.expired():
            deadline.triggered = True
            return True
        return capped

    return _should_stop


async def _drain(queue: "asyncio.Queue", task: "asyncio.Task") -> AsyncIterator[Any]:
    """把监听器推进队列的事件按到达顺序吐出；``task`` 结束后把队列排干再结束。

    为什么用队列而不是在监听器里直接 yield：CubeLoop 的 ``subscribe`` 回调是**同步调用点**
    （``_emit_to_listeners`` 里 await 它的返回值），回调里不能 yield；而主循环又必须边跑边把
    帧交给 SSE（前端要实时看到步骤）。

    ``回归修复``：getter（``queue.get()`` 任务）必须随生成器一起收——用户停止 /
    客户端断开 / 任务取消会让 ``GeneratorExit``/``CancelledError`` 从 ``await``/``yield``
    点穿出本生成器，``aclose()`` 只关生成器本体、管不到它内部另起的 getter；不 cancel
    的话该任务原样遗留 pending（若 abort 后 CubeLoop 仍 emit，它还会取走一个事件且结果
    永远无人消费），每轮中止漏一个、关机时刷「Task was destroyed but it is pending」。
    finally 是唯一收口点（循环体后半段的分支取消已并入这里，一个出口一个逻辑）。
    """
    getter: asyncio.Task | None = None
    try:
        getter = asyncio.ensure_future(queue.get())
        while True:
            done, _pending = await asyncio.wait(
                {task, getter}, return_when=asyncio.FIRST_COMPLETED
            )
            if getter in done:
                yield getter.result()
                getter = asyncio.ensure_future(queue.get())
                continue
            if task.done():
                break
        # task 已完成：可能有一个事件已被 getter 取走，再排干队列剩余部分
        if getter.done() and not getter.cancelled():
            try:
                yield getter.result()
            except Exception:  # noqa: BLE001 —— 取件失败不影响收尾
                pass
        while not queue.empty():
            yield queue.get_nowait()
    finally:
        if getter is not None and not getter.done():
            getter.cancel()


def _steps_for(
    event: Any,
    *,
    turn: AgentTurn,
    collector: ToolCollector,
    started_at: dict[str, float],
    fallback_id: str = "",
) -> list[dict]:
    """把一个 CubeLoop 事件映射成 0..1 个步骤（映射表见 ``_work-4.1接口冻结.md`` §八）。

    ⚠️ ``fallback_id``（条目 4.3 · ``回归修复``）：CubeLoop 给的 ``tool_call_id`` 可以是
    **空串**（``providers/openai.py`` 的 ``tc_delta.id or ""``）。执行侧与事件侧都已接到同一个
    兜底 id，**步骤帧这一侧原先漏了**——后果是 `agent_trace.steps[].tool_call_id` 恒为 `""`、
    且 ``collector.rendered`` 用**空键**取不到（观察文本退化成「没有检索到相关资料。」，
    与真实结果不符）。三处口径必须一致，否则「用 tool_call_id 对账」在界面上就断了。

    ⚠️ ``回归修复`` 起：走事件桥时空 id 已按**调用序**铸成独立 id 写回事件 ⇒ 这里的
    ``fallback_id`` 只剩「没有事件桥」的兜底职责；同一轮两次空 id 不再共用一键。
    """
    kind = getattr(event, "type", "")
    if kind == "turn_start":
        return []  # 轮次计数在监听器里做（见 stream_agent_turn 的 _on_event 注释）
    if kind == "tool_execution_start":
        tool_call_id = str(getattr(event, "tool_call_id", "") or "").strip() or fallback_id
        started_at[tool_call_id] = time.perf_counter()
        args = dict(getattr(event, "args", None) or {})
        name = str(getattr(event, "tool_name", "") or "")
        return [
            turn.add_step(
                "tool",
                _tool_title(name, args),
                tool=name,
                # 🔴 阶段 6（`回归检查`）：步骤载荷里的路径归一进观察面 —— 归一**只此一处**
                # （`_tool_args_for_steps`），两条帧路径共用，避免各写一份后漂移。
                # ⚠️ 标题仍取**原** `args`：`_tool_title` 不吃 path 键，传归一后的 dict
                # 只会让「将来标题要吃路径」时给出一个与事件载荷不同的值。
                args=_tool_args_for_steps(name, args),
                tool_call_id=tool_call_id,
            )
        ]
    if kind == "tool_execution_end":
        tool_call_id = str(getattr(event, "tool_call_id", "") or "").strip() or fallback_id
        name = str(getattr(event, "tool_name", "") or "")
        is_error = bool(getattr(event, "is_error", False))
        elapsed = started_at.pop(tool_call_id, None)
        ms = int((time.perf_counter() - elapsed) * 1000) if elapsed is not None else 0
        observation = collector.observations.get(tool_call_id)
        # S4 · 208：引擎自执行工具（``load_tools``）不经 ``ToolCollector`` ⇒ 上面两项都空。
        # 事件自带的成功结果文本放在**最后一道兜底**（rendered / 观察文本优先）⇒ 只补空，不改既有读数。
        text = collector.rendered.get(tool_call_id) or (
            observation.text if observation else ""
        ) or _engine_tool_result_text(event) or NO_HIT_TEXT
        steps = [
            turn.add_step(
                "observation",
                "工具执行失败" if is_error else "观察结果",
                detail=clip(text, STEP_CLIP),
                tool=name,
                ok=not is_error,
                ms=ms,
                tool_call_id=tool_call_id,
            )
        ]
        # 🔴 阶段 6（`回归检查`）：写成功 ⇒ 在观察之后补一条 `file_change`（变更列表可见）。
        # 放在观察**之后**：先「做了什么」再「结果是什么」，与 `tool` → `observation` 的既有次序同向。
        # `meta` 从 **collector 里那份观察**取（不是事件对象）—— 事件的形状在两条路径上不同，
        # 而 `collector.observations` 是两处**共用**的同一份事实（也正是 `_write_meta_of` 的读点）。
        # ⚠️ `meta` 取的是 **collector 上缓存的那一份**（`_absorb_tool` 在 `absorb` 之前落的），
        # **不是** `_write_meta_of(observation)` —— 后者在这里恒 `None`（`absorb` 里的
        # `dataclasses.replace` 已把动态附的 `write_meta` 丢掉，见该字段的注释）。
        change = _file_change_step(
            turn,
            tool=name,
            tool_call_id=tool_call_id,
            meta=collector.write_meta.get(tool_call_id),
        )
        if change is not None:
            steps.append(change)
        return steps
    if kind == "turn_end":
        message = getattr(event, "message", None)
        steps: list[dict] = []
        # 决策调用失败（限流 / 瞬时 5xx / 鉴权过期 …，CubeLoop 上游 ``_handle_run_failure``
        # 铸的 ``stop_reason="error"`` 消息：正文为空串、原因在 ``error_message``）。
        # ⚠️ ``回归修复``：此前这一形态被整体吞没——本分支什么都不产、``agent_trace``
        # 零痕迹、run 行照记 ``done``，「引擎异常 / 模型不可用」与「一切正常」在库外观感一致。
        # 与上面「决策被截断」同款处理：补一条 note（不给「Agent 什么都没干」的假象），
        # 并在 turn 上留标记，让 :func:`_audit_run_status` 把 run 行记成 ``error``。
        if getattr(message, "stop_reason", "") == "error":
            turn.model_error = True
            err = clip(str(getattr(message, "error_message", "") or "").strip(), ARGS_CLIP)
            steps.append(
                turn.note_step(
                    "模型调用失败",
                    f"第 {turn.rounds} 轮模型调用失败{('：' + err) if err else ''}，"
                    "已用现有资料继续作答。",
                )
            )
        # 决策调用的输出预算被用尽（厂商 finish_reason="length"）：**必须可见**。
        # 为什么不能静默：600 只覆盖「决策 + 工具参数」这一步，正常足够；但被截断时
        # ① 模型可能正要说「要调哪个工具」就被切断 ⇒ 这一轮白跑；② 工具参数 JSON 被切成
        # 半截 ⇒ CubeLoop 只能报一个「参数非法」的失败观察，看不出真因。
        # 两种情形都表现为「Agent 好像什么都没干」，与「确实不需要工具」外观一致 —— 正是
        # 阶段 4 交接表点名要避免的「不给假象」。留一条 note 后，`agent_trace` 与界面步骤流
        # 里都能一眼看到，而不是让人去猜模型为什么不干活。
        if getattr(message, "stop_reason", "") == "length":
            steps.append(
                turn.note_step(
                    "决策被截断",
                    f"决策调用的输出预算（max_tokens={DECISION_MAX_OUTPUT_TOKENS}）已用尽，"
                    "这一轮的决策可能不完整（工具参数被切断时会表现为一次工具执行失败）。"
                    "已用现有资料继续作答。",
                )
            )
        text = _assistant_text(message)
        if text:
            visible, parsed, invalid = parse_closeout_message(text)
            turn.closeout = parsed
            turn.closeout_invalid = invalid
            if invalid:
                steps.append(
                    turn.note_step(
                        "结束决策无效",
                        "结束决策格式不正确，已改用已检索到的资料继续作答。",
                    )
                )
            if visible and "<<<CLOSEOUT>>>" not in visible:
                steps.append(turn.add_step("thought", "思考", detail=clip(visible, STEP_CLIP)))
        return steps
    if kind == "message_end":
        if getattr(getattr(event, "message", None), "stop_reason", "") == "aborted":
            turn.aborted = True
        return []
    if kind == "agent_aborted":
        turn.aborted = True
        return []
    if kind in ("hitl_request", "agent_suspended"):
        # 4.1 不做 HITL；真出现（说明有人挂了 channel）就如实留痕，别静默
        return [turn.add_step("note", "需要人工确认", "本轮工具请求需要用户确认，已跳过。")]
    return []


def _assistant_text(message: Any) -> str:
    """取助手消息里的正文文本（``ThinkingContent`` / ``ToolCall`` 不属于正文）。"""
    parts: list[str] = []
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", "") == "text":
            parts.append(str(getattr(block, "text", "") or ""))
    return "".join(parts).strip()


async def _write_tool_event(
    event: Any,
    *,
    db: Any,
    chat_id: int | None,
    turn_no: int | None,
    round_no: int,
    trace_id: str | None,
    collector: ToolCollector,
    fallback_id: str,
    turn_key: tuple[int | None, int | None] | None = None,
    db_lock: "asyncio.Lock | None" = None,
) -> None:
    """ReAct 路径的适配壳：把一对 CubeLoop 事件交给 :func:`_write_tool_events`。

    事件与 CubeLoop 的 ``tool_call_id`` **逐条对应**，所以这里一次只转一个（见 ``_drain`` 循环）。

    ``fallback_id`` 是本轮**唯一的**兜底 id（见 :func:`_write_tool_events` 的 ``回归修复`` 段）：
    它由 :func:`stream_agent_turn` 与执行闭包**共用**，所以「CubeLoop 给空 id」这类情形下
    ``tool/call``、``tool/result`` 与 ``collector`` 的键仍然是同一个。
    """
    await _write_tool_events(
        [event],
        db=db,
        chat_id=chat_id,
        turn_no=turn_no,
        round_no=round_no,
        trace_id=trace_id,
        collector=collector,
        fallback_id=fallback_id,
        turn_key=turn_key,
        db_lock=db_lock,
    )


async def _write_tool_events(
    events: list[Any],
    *,
    db: Any,
    chat_id: int | None,
    turn_no: int | None,
    round_no: int,
    trace_id: str | None,
    collector: ToolCollector,
    fallback_id: str,
    turn_key: tuple[int | None, int | None] | None = None,
    db_lock: "asyncio.Lock | None" = None,
) -> None:
    """写一次工具调用的**配对**事件（I4）：执行前 ``tool/call``、执行后 ``tool/result``。

    两条路径共用这一处落库（ReAct 逐事件传单元素列表，P&E 每步传 ``[开始, 结束]``），
    所以「P&E 的事件口径与 ReAct 不同」这种漂移在结构上不可能发生。

    口径（逐条都有原因）：

    - 两条共用**同一个** ``tool_call_id``（ReAct 用 CubeLoop 的 ``ToolCall.id``，即厂商给的 id；
      P&E 不经 CubeLoop，用 :func:`new_tool_call_id` 现铸——见条目 4.2 §2.2-2）——
      「调了但不知道结果」与「压根没调起来」在日志里因此可分辨（I4 的 {0,1} 差分）；
    - ``tool/call`` 是**仅日志事件**：带 ``surface_op`` 会被事件层直接拒（``SurfaceOpError``）；
    - ``tool/result`` 是**表层事件**（模型可见），必须带 ``surface_op="append"``；
    - **不传 ``request_id``**：一轮只允许一条事件带它（``uk(chat_id, request_id)``，
      第 2 条起会被静默吞掉——`回归修复`）。串链一律走 ``trace`` 列（本轮 ``retrieval_id``）；
    - 一律走 ``safe_append_event``（失败只警告 + 计数，绝不炸穿），
      并把「本轮是否写失败过」通过 ``event_write_failures()`` 快照记进 ``turn.events_incomplete``
      ——**不给假象**：日志缺了就说缺了；
    - ``turn_key``（条目 4.3）**必须传给事件层**，否则失败计数只进进程全局桶
      （``回归修复``）——工具事件恰恰是 Agent 轮最主要的事件族，它们缺行而
      ``events_incomplete`` 说「完整」就是**信号说假话**（对抗性核验实测抓到过）。

    ⚠️ **空 ``tool_call_id`` 的兜底是「一个轮一个」**（条目 4.3 的 ``回归修复``）：
    原先兜底写在循环体内，同一对事件的 ``start`` 与 ``end`` 都为空格 id 时会**各铸一个不同的
    id** ⇒ I4 的配对当场破裂（P&E 不可达，ReAct 在 CubeLoop 给空 id 时可达——静态依据
    ``cubeloop/providers/openai.py`` 的 ``"id": tc_delta.id or ""``）。现在兜底 id 由调用方
    （:func:`stream_agent_turn`、:func:`_run_plan_step`）铸一次并**与执行侧、步骤帧共用**，
    这里只做「事件没带 id 就用它」。

    ⚠️ ``回归修复`` 起：走事件桥（ReAct）时空 id 已在监听器里按调用序铸成**独立** id 写回
    事件，因此「整轮共用一个兜底 id」只剩**没有事件桥**时的兜底（P&E 旧路 / 手造事件）——同轮
    两次空 id 不再折成同一个键（那会让 ``observations``/``rendered`` last-wins、步骤帧同 id）。

    ⚠️ **写库过会话锁**（``db_lock``）：本函数可能被 CubeLoop 的任务与消费端任务**并发**调用，
    而它们共用同一个 ``AsyncSession``——并发使用会撞出
    ``This session is in 'prepared' state`` 并让后面的收口失败（僵尸 ``running`` 行）。
    """
    if db is None or chat_id is None:
        return
    async with _session_lock(db_lock):
        await _write_tool_events_locked(
            events,
            db=db,
            chat_id=chat_id,
            turn_no=turn_no,
            round_no=round_no,
            trace_id=trace_id,
            collector=collector,
            fallback_id=fallback_id,
            turn_key=turn_key,
        )


async def _write_tool_events_locked(
    events: list[Any],
    *,
    db: Any,
    chat_id: int,
    turn_no: int | None,
    round_no: int,
    trace_id: str | None,
    collector: ToolCollector,
    fallback_id: str,
    turn_key: tuple[int | None, int | None] | None,
) -> None:
    """事件写入的**正文**（在 :func:`_write_tool_events` 拿到会话锁之后调用）。"""
    for event in events:
        kind = getattr(event, "type", "")
        if kind not in ("tool_execution_start", "tool_execution_end"):
            continue
        tool_call_id = str(getattr(event, "tool_call_id", "") or "").strip() or fallback_id
        name = str(getattr(event, "tool_name", "") or "")
        if kind == "tool_execution_start":
            await event_log.safe_append_event(
                db,
                chat_id,
                "tool/call",
                {
                    "tool_call_id": tool_call_id,
                    "tool": name,
                    "args": _args_digest(dict(getattr(event, "args", None) or {})),
                    "round": round_no,
                },
                turn=turn_no,
                trace=trace_id,
                tool_call_id=tool_call_id,
                turn_key=turn_key,
            )
            continue
        observation = collector.observations.get(tool_call_id)
        is_error = bool(getattr(event, "is_error", False))
        # ``content`` 是「模型实际看到的观察」的持久记录（已按快照编号重渲染）；``chars``
        # **必须量同一段文本**——原先量的是工具自己渲染的那份（局部编号），同一行里两个字段
        # 描述不同字符串（对抗性核验 F3：实测 83 vs 34）。读方拿 ``chars`` 校验 ``content``
        # 完整性时会直接误判。
        # S4 · 208：引擎自执行工具的成功结果补进 content（同一个 ``content`` 量 ``chars``，
        # F3 的「chars 与 content 同源」口径照旧）；失败腿不补（见 ``_engine_tool_result_text``）。
        # P&E 自造的 End 事件（``_pe_events``）**没有 ``result`` 字段** ⇒ 本项在 P&E 恒空，
        # 该腿的落库与步骤帧仍只认 ``collector.rendered`` / 观察文本。
        content = clip(
            collector.rendered.get(tool_call_id, "") or _engine_tool_result_text(event),
            STEP_CLIP,
        )
        payload: dict[str, Any] = {
            "tool_call_id": tool_call_id,
            "tool": name,
            "ok": not is_error,
            "chars": len(content),
            # materials_hit 与**该次的观察文本**同源（S4 · 207）：取 ``absorb`` 结账时记下的
            # ``len(entries)``（= 真正进了快照、并渲染进 content 的材料条数）。
            # ⛔ 不再取 ``len(observation.materials)``：它会把被上限丢掉的新片段也算成命中
            # （混合批实测 3，其中 2 条一条都没进快照）。
            "materials_hit": collector.material_hits.get(tool_call_id, 0),
            # 告警计数同源：工具自家告警 + 本次**因上限丢弃新片段**而记的告警。
            # 后者原先只在分支 ② 生成、且从不进载荷 ⇒ 混合批「静默丢」时库里读数是 0。
            "warnings": (len(observation.warnings) if observation else 0)
            + len(collector.snapshot_warnings.get(tool_call_id, ())),
            "content": content,
        }
        if is_error:
            payload["error"] = clip(
                str(getattr(event, "block_reason", "") or "")
                or (observation.text if observation else ""),
                ARGS_CLIP,
            )
        if trace_id:
            payload["retrieval_id"] = trace_id
        if observation is not None and observation.retrieval_stats is not None:
            payload["retrieval"] = observation.retrieval_stats.payload()
        await event_log.safe_append_event(
            db,
            chat_id,
            "tool/result",
            payload,
            surface_op="append",
            turn=turn_no,
            trace=trace_id,
            tool_call_id=tool_call_id,
            turn_key=turn_key,
        )


def _tool_events(
    *,
    tool_call_id: str,
    name: str,
    args: dict,
    observation: ToolObservation,
    elapsed_ms: int,
    block_reason: str = "",
) -> tuple[Any, Any]:
    """P&E 的一对工具事件（形状与 CubeLoop 的 ``tool_execution_start`` / ``_end`` 逐字对齐）。

    为什么造两个 ``SimpleNamespace`` 而不另写一份落库代码：落库口径只有一处
    （:func:`_write_tool_events`），P&E 只是把一个异步执行结果**表述成同样的两个事件**——
    这样事件层的字段校验、``surface_op`` 规则、配对键都不用重讲一遍。
    """
    start = SimpleNamespace(
        type="tool_execution_start", tool_call_id=tool_call_id, tool_name=name, args=dict(args or {})
    )
    # ``is_error`` / ``block_reason`` 是 ``_write_tool_events`` 真正读的两个字段；
    # ``ms`` 只是照 CubeLoop 事件形状带上（当前无读取点，将来接审计时现成可用）。
    end = SimpleNamespace(
        type="tool_execution_end",
        tool_call_id=tool_call_id,
        tool_name=name,
        is_error=not observation.ok,
        block_reason=block_reason,
        ms=elapsed_ms,
    )
    return start, end


def _tool_frames(
    *,
    turn: AgentTurn,
    tool_call_id: str,
    name: str,
    args: dict,
    collector: ToolCollector,
    observation: ToolObservation,
    elapsed_ms: int,
) -> list[dict]:
    """P&E 一步的两帧：``tool`` + ``observation``（标题/渲染与 ReAct 同一套口径）。

    三个「同一套」：标题走 :func:`_tool_title`；观察文本取 ``collector.rendered``
    （**快照编号**，I3）；失败时标题为「工具执行失败」、``ok=False``——与
    :func:`_steps_for` 里 CubeLoop 事件的映射逐字一致，所以界面看不出两条路径的差别。
    """
    return [
        turn.add_step(
            "tool",
            _tool_title(name, args),
            tool=name,
            # 🔴 同 ReAct 那条路径：归一走 `_tool_args_for_steps`（**不是**各写一份）。
            # 只改 P&E 不改 ReAct（或反之）会让同一个问题在两种 agent 模式下外观不同 ——
            # 「界面看不出两条路径的差别」正是本函数 docstring 的既定口径。
            args=_tool_args_for_steps(name, args),
            tool_call_id=tool_call_id,
        ),
        turn.add_step(
            "observation",
            "工具执行失败" if not observation.ok else "观察结果",
            detail=clip(collector.rendered.get(tool_call_id, "") or observation.text, STEP_CLIP),
            tool=name,
            ok=bool(observation.ok),
            ms=elapsed_ms,
            tool_call_id=tool_call_id,
        ),
        # 🔴 同 ReAct 那条路径：写成功 ⇒ 补一条 `file_change`（`_file_change_step` 是唯一实现）。
        # ⚠️ 必须 append 而**不能**写在列表字面量里：`_file_change_step` 在「不该产出」时返 `None`，
        # 而列表字面量里塞一个 `None` 会往 `turn.steps` 里落一条 `None`（下游 `step["kind"]` 直接炸）。
        # ⚠️ 同 ReAct：meta 取 collector 缓存（`_write_meta_of(observation)` 在这里恒 None）。
        *_file_change_steps(
            turn,
            tool=name,
            tool_call_id=tool_call_id,
            meta=collector.write_meta.get(tool_call_id),
        ),
    ]


def _spec_map(tools: list[Any], registry: ToolRegistry | None) -> dict[str, Any]:
    """按名字回收本轮工具对应的 :class:`ToolSpec`（P&E 的闸门要用）。

    **两个 map 不是一回事**：``tool_map``（在 :func:`_stream_plan_execute` 里现建）的值是
    CubeLoop 的 ``AgentTool``——只有 ``name`` / ``description`` / ``parameters`` / ``execute``，
    **没有** ``handler`` 与 ``dangerous``；闸门需要的是 :class:`ToolSpec`。

    ReAct 那条路径不存在这个问题：``_as_agent_tool`` 的闭包**捕获了 spec 本体**，所以它的闸门
    判据天然是准的。P&E 只拿到 ``AgentTool``，只能按名字反查——用 ``registry.get()``
    （注册表是**闸②的同一个真相源**，反查它最不容易漂）。

    反查不到的项（调用方注入的裸 ``AgentTool``、或不在注册表里的替身）**不入表**，
    于是闸门按「没有 ``dangerous`` 声明」处理——**保守方向：不拦**。理由：闸门只负责
    「明确声明为危险」的那一类；猜出来的拦截会误伤正常工具，而 I6 的字面要求是
    「危险工具零暴露」，`dangerous=True` 是它的唯一判据。
    """
    out: dict[str, Any] = {}
    if registry is None:
        return out
    for tool in tools:
        name = str(getattr(tool, "name", "") or "")
        if not name or name in out:
            continue
        try:
            out[name] = registry.get(name)
        except Exception:  # noqa: BLE001 —— 不在注册表里就不入表（见 docstring）
            continue
    return out


async def _run_plan_step(
    tools: list[Any],
    tool_map: dict[str, Any],
    *,
    plan_no: int,
    goal: str,
    tool_name: str,
    raw_args: dict,
    thought: bool = True,
    turn: AgentTurn,
    collector: ToolCollector,
    db: Any,
    chat_id: int | None,
    turn_no: int | None,
    trace_id: str | None,
    fallback_id: str,
    spec_map: dict[str, Any] | None = None,
    audit_state: dict[str, Any] | None = None,
    db_lock: "asyncio.Lock | None" = None,
) -> AsyncIterator[dict]:
    """执行计划里的**一步**（``thought`` → ``tool`` → ``observation``）并落 ``tool/*`` 事件。

    四条分支（顺序即优先级，口径见条目 4.2 §2.1-6 与 §2.2-1）：

    1. ``tool`` 为空 / 不在本轮清单 ⇒ ``note("跳过：无可用工具", …)``，**不产出空转的工具调用**
       （既不执行、也不写事件——「没调」就该在日志里表现为没调）；
    2. ``args`` 过不了 pydantic 校验 ⇒ ``observation.ok=False``（**§2.2-1 的细化**：
       老基线把原始 dict 塞给 executor 靠它抛错，v2 的入参是模型 ⇒ 校验失败与「executor 抛错」
       同档处理，**不静默跳过**，否则会与上一条的 note 混同）；
    3. 工具自己抛异常 ⇒ 由 ``_run_tool_handler`` 内部转成 ``ok=False`` 的观察（4.1 既有行为）；
    4. 正常 ⇒ 观察文本用**快照编号**重渲染（I3）。

    ``round`` = 计划步序号（1 起，条目 4.2 §2.1-5）；``tool_call_id`` 用 :func:`new_tool_call_id`
    现铸，``tool/call``、``tool/result`` 与两个步骤**共用同一个 id**（I4）。

    ``thought=False`` 是**回退路径**专用：老基线的回退直接调 ``run_tool``，而 ``thought`` 步是在
    计划循环体里产出的（``agent.py:689``）——回退不在循环体里，所以它**只有 tool + observation**。

    条目 4.3 在这一步补两件事（**这两处最容易只做 ReAct 那一份**）：
    ① 执行前过安全闸门（``spec.dangerous`` ⇒ 不执行、留冻结文案的 ``ok=False`` 观察）；
    ② 执行后落一行审计（``agent_steps``，**成败都记**）。

    ⚠️ **两个 map 不是一回事**（踩过）：``tool_map`` 的值是 CubeLoop 的 ``AgentTool``
    （只有 ``name`` / ``description`` / ``parameters`` / ``execute``），**没有** ``handler``
    与 ``dangerous``；``spec_map`` 的值才是 :class:`~app.agent.registry.ToolSpec`。
    执行走 ``AgentTool.execute``（与 ReAct 同一条通路），闸门走 ``spec_map``。
    ``spec_map`` 缺省时回落成 ``tool_map``（旧调用方兼容；此时闸门因取不到 ``dangerous``
    而**不拦**，属保守方向——见 :func:`app.agent.safety.is_blocked`）。
    """
    spec_map = tool_map if spec_map is None else spec_map
    audit_state = {"written": 0, "attempted": 0} if audit_state is None else audit_state
    if tool_name and tool_name in tool_map:
        if thought:
            yield turn.add_step("thought", "思考", detail=goal)
    else:
        # 老基线逐字：goal 为空时退到「工具不可用：<名 or 未指定>」
        yield turn.note_step(
            "跳过：无可用工具", goal or f"工具不可用：{tool_name or '未指定'}"
        )
        return

    tool = tool_map[tool_name]
    spec = spec_map.get(tool_name, tool)
    tool_call_id = new_tool_call_id()
    started = time.perf_counter()
    await _write_tool_events(
        [SimpleNamespace(type="tool_execution_start", tool_call_id=tool_call_id,
                         tool_name=tool_name, args=dict(raw_args or {}))],
        db=db, chat_id=chat_id, turn_no=turn_no, round_no=plan_no,
        trace_id=trace_id, collector=collector, fallback_id=tool_call_id,
        turn_key=(chat_id, turn_no), db_lock=db_lock,
    )
    parameters = getattr(tool, "parameters", None)
    if parameters is not None:
        try:
            args_model = parameters(**raw_args)
        except Exception as exc:  # noqa: BLE001 —— 入参校验失败也是**可见的**一次失败观察
            observation = ToolObservation(
                text=f"{tool_name} 参数校验失败：{exc}",
                ok=False,
                warnings=[f"{tool_name} 参数校验失败"],
            )
            block_reason = str(exc)
        else:
            observation = None
            block_reason = ""
    else:  # 没有参数模型（如 MCP 文本工具）：原样透传
        args_model = raw_args
        observation = None
        block_reason = ""

    if observation is not None:
        # ⚠️ 校验失败那条**必须**也走 collector：事件载荷（``chars`` / ``warnings`` /
        # ``content``）与 ``turn.warnings`` 都从 collector 取，不并入的话库里会落一行
        # 「结果为空、零警告」的假记录，而真实情况是一条带文本、带 1 条 warning 的失败观察
        # （条目 4.2 对抗性核验的 F2）。审计与它同一份文本（``content`` 同源）。
        await _absorb_tool(
            spec=spec,
            collector=collector,
            rid=tool_call_id,
            args=raw_args,
            observation=observation,
            started=started,
        )
    elif safety.is_blocked(spec):
        # 闸③（P&E 侧）：**不执行**。与 ReAct 同一文案、同一 ``ok=False`` 形状。
        observation = safety.blocked_observation(spec)
        await _absorb_tool(
            spec=spec,
            collector=collector,
            rid=tool_call_id,
            args=raw_args,
            observation=observation,
            started=started,
        )
    else:
        # ``execute`` 闭包内部已把 handler 异常 / 违约返回值都转成了观察（I5），
        # 观察并入 collector 并重渲染（I3）也由它做——**与 ReAct 同一条通路**，
        # 所以两处的 ``ok`` / ``ms`` / 审计 ``step_no`` 口径不会漂移。
        try:
            await tool.execute(tool_call_id, args_model)
        except asyncio.CancelledError:
            event_log.note_event_cancelled((chat_id, turn_no))
            raise
        observation = collector.observations.get(tool_call_id) or ToolObservation(
            text=NO_HIT_TEXT, ok=False
        )

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    events = _tool_events(
        tool_call_id=tool_call_id,
        name=tool_name,
        args=raw_args,
        observation=observation,
        elapsed_ms=elapsed_ms,
        block_reason=block_reason,
    )
    await _write_tool_events(
        [events[1]],
        db=db,
        chat_id=chat_id,
        turn_no=turn_no,
        round_no=plan_no,
        trace_id=trace_id,
        collector=collector,
        fallback_id=tool_call_id,
        turn_key=(chat_id, turn_no),
        db_lock=db_lock,
    )
    # 审计写库（P&E 这一侧本来就在同一条任务上，直接写完）。
    # ReAct 那条路径**不能**在这里做——它的执行闭包跑在 CubeLoop 的任务上，
    # 与事件落库的消费端任务共用同一个 AsyncSession（见 `ToolAudit` 的并发说明）。
    await _flush_audit(collector, turn, db=db, state=audit_state, db_lock=db_lock)
    for frame in _tool_frames(
        turn=turn,
        tool_call_id=tool_call_id,
        name=tool_name,
        args=raw_args,
        collector=collector,
        observation=observation,
        elapsed_ms=elapsed_ms,
    ):
        yield frame


async def _stream_plan_execute(
    turn: AgentTurn,
    *,
    question: str,
    tools: list[Any],
    collector: ToolCollector,
    model: Any,
    history: list[dict] | None,
    db: Any,
    chat_id: int | None,
    turn_no: int | None,
    trace_id: str | None,
    fallback_id: str,
    registry: ToolRegistry | None = None,
    spec_map: dict[str, Any] | None = None,
    db_lock: "asyncio.Lock | None" = None,
    deadline: _TurnDeadline | None = None,
) -> AsyncIterator[tuple[str, dict]]:
    """Plan-and-Execute 路径（条目 4.2 · 口径 A：忠实移植老基线）。

    流程与老基线 ``mode == "plan_execute"`` 分支逐条对应：

    1. **一次计划调用**（**不在循环里**）：``PLAN_SYSTEM`` + :func:`plan_prompt`，
       ``max_output_tokens=600``、``reasoning=off``（条目 4.2 §2.1-1）；
    2. 模型不可用（抛错）⇒ ``note("模型不可用", …)`` 后**照常收尾**（I5）；
    3. 计划解析失败 ⇒ ``note("计划回退", …)`` + **强制执行一次** ``knowledge_search``
       （原始问题当检索词）——条目原文的「失败回退单步检索」；
    4. 可解析 ⇒ ``plan`` 步（``title="执行计划"``）→ 截到 :data:`PLAN_MAX_STEPS` 步（超出产
       ``计划过长`` note）→ 逐步执行，**步间不问模型**；
    5. 任何意外 ⇒ ``note("引擎异常", …)``，不炸穿。

    ⚠️ ``turn.rounds`` 在 P&E 路径上**保持 0**：``AGENT_MAX_ROUNDS`` 不约束 P&E（老基线同口径，
    P&E 的上限是 ``PLAN_MAX_STEPS`` + 1 次计划调用）。这里**没有** ``turn.rounds += 1`` 的写入点。
    """
    from cubeloop.providers.base import (
        ReasoningControl,
        StreamOptions,
        TextContent,
        UserMessage,
    )

    tool_map = {tool.name: tool for tool in tools}
    # ``spec_map`` 是 ToolSpec（有 ``handler`` / ``dangerous``）；``tool_map`` 是 AgentTool。
    # 两条路径的执行都走 ``AgentTool.execute``，闸门判定走 ``spec_map``（见 ``_run_plan_step``）。
    spec_map = _spec_map(tools, registry) if spec_map is None else spec_map
    #: 审计写入进度（P&E 每步刷一次；可变对象是为了让 ``_run_plan_step`` 也能推进它）
    audit_state: dict[str, Any] = {"written": 0, "attempted": 0}

    try:
        # ``UserMessage.content`` 是**内容块列表**（不是裸字符串——pydantic 会拒），
        # 与 ``chat_routes.stream_tokens`` 里 ``_to_typed_messages`` 的装配口径一致。
        #
        # ⚠️ **`on_payload=_cap_decision_payload` 不能省**（2026-09-20 实测）：
        # `max_output_tokens=` / `reasoning=` 在**本仓的装配口径下到不了请求体** ——
        # cubeloop 把 `max_tokens` / `temperature` / reasoning 载荷一律关在
        # `if self._cap_active or model.reasoning:` 之后
        # （`.venv/.../cubeloop/providers/openai.py:149-167`），而
        # `answer._build_bound_model` **不传 `capability`** ⇒ `_cap_active=False`。
        # `generate(max_output_tokens=…)` 只是改 `Model` 的值（`base.py:780-786`），
        # 上面那道门不开就**一个字都不下发**（实测：只有 `stream: true`）。
        # `on_payload` 是本仓**唯一**能改请求体的机制（ReAct 那条路就是靠它，
        # 见 :func:`_cap_decision_payload` 的调用点）——**同一个「决策小调用」预算，
        # 两条路必须同源**，否则 P&E 的计划调用实际**没有输出预算**（会按模型上限生成）。
        from app.usage.instrumentation import usage_scope
        with usage_scope(role="agent_plan"):
            message = await model.generate(
                [UserMessage(content=[TextContent(text=plan_prompt(question, tools, history))])],
                system_prompt=PLAN_SYSTEM,
                options=StreamOptions(on_payload=_cap_decision_payload),
                max_output_tokens=DECISION_MAX_OUTPUT_TOKENS,
                reasoning=ReasoningControl(mode="off"),
            )
    except Exception as exc:  # noqa: BLE001 —— 模型不可用：note 后照常收尾（I5）
        logger.warning("P&E 计划调用失败（已隔离）：%s: %s", exc.__class__.__name__, exc)
        yield "agent", turn.note_step("模型不可用", f"无法调用对话模型：{exc}")
        return

    try:
        text = _assistant_text(message)
        if getattr(message, "stop_reason", "") == "length":
            # 与 ReAct 的「决策被截断」同型：被切断的计划器很可能回不出合法 JSON，
            # 静默回退会让人以为「模型没给计划」，所以截断本身也留一条痕。
            yield "agent", turn.note_step(
                "决策被截断",
                f"计划调用的输出预算（max_tokens={DECISION_MAX_OUTPUT_TOKENS}）已用尽，"
                "计划可能不完整；若无法解析将回退为单步检索。",
            )
        plan = parse_plan(text)
        if plan is None:
            # 回退：**不是**「什么都不做」——只检索一次知识库（条目原文）
            yield "agent", turn.note_step("计划回退", "计划解析失败，已回退为只检索一次知识库。")
            async for frame in _run_plan_step(
                tools,
                tool_map,
                plan_no=1,
                goal="",
                tool_name="knowledge_search",
                raw_args={"query": question},
                thought=False,
                turn=turn,
                collector=collector,
                db=db,
                chat_id=chat_id,
                turn_no=turn_no,
                trace_id=trace_id,
                fallback_id=fallback_id,
                spec_map=spec_map,
                audit_state=audit_state,
                db_lock=db_lock,
            ):
                yield "agent", frame
            return

        total = len(plan)
        plan = plan[:PLAN_MAX_STEPS]
        yield "agent", turn.add_step("plan", "执行计划", detail=plan_text(plan))
        if total > PLAN_MAX_STEPS:
            # 老基线逐字：含步数与两个上限数字
            yield "agent", turn.note_step(
                "计划过长",
                f"计划共 {total} 步，超过上限 {PLAN_MAX_STEPS}，只执行前 {PLAN_MAX_STEPS} 步。",
            )
        for plan_no, item in enumerate(plan, start=1):
            if deadline is not None and deadline.expired():
                # R2-03/R2-04 · P&E 侧**新造的收口语**（P&E 没有「轮数达上限」这一形：
                # ``turn.rounds`` 恒 0）⇒ 措辞与 ReAct 侧刻意同族而不逐字相同。
                # 断点落在**步与步之间**：正在执行的那一步**不打断** —— 工具调用中途没有
                # 检查点，硬打断会切出「只有 tool/call 没有 tool/result」的半对（破 I4）。
                yield "agent", turn.note_step(
                    "已达时间上限",
                    f"本轮已用满时间上限 {deadline.seconds} 秒，"
                    f"计划共 {total} 步、已执行 {plan_no - 1} 步，其余不再执行。",
                )
                break
            goal = str(item.get("goal") or "").strip()
            tool_name = str(item.get("tool") or "").strip()
            args = item.get("args") if isinstance(item.get("args"), dict) else {}
            async for frame in _run_plan_step(
                tools,
                tool_map,
                plan_no=plan_no,
                goal=goal,
                tool_name=tool_name,
                raw_args=dict(args),
                turn=turn,
                collector=collector,
                db=db,
                chat_id=chat_id,
                turn_no=turn_no,
                trace_id=trace_id,
                fallback_id=fallback_id,
                spec_map=spec_map,
                audit_state=audit_state,
                db_lock=db_lock,
            ):
                yield "agent", frame
    except Exception as exc:  # noqa: BLE001 —— 引擎异常绝不炸穿（I5）
        logger.warning("P&E 执行异常（已隔离）：%s: %s", exc.__class__.__name__, exc)
        yield "agent", turn.note_step("引擎异常", f"Agent 过程出现意外错误，已终止：{exc}")
    finally:
        try:
            await _flush_audit(collector, turn, db=db, state=audit_state, db_lock=db_lock)
        except Exception as exc:  # noqa: BLE001 —— 与 ReAct 一样隔离审计异常
            logger.warning("P&E 审计补写失败（已隔离）：%s: %s", exc.__class__.__name__, exc)


__all__ = [
    "ARGS_CLIP",
    "BLOCK_CLIP",
    "DECISION_MAX_OUTPUT_TOKENS",
    "DECISION_SYSTEM_PROMPT",
    "DEFAULT_MAX_ROUNDS",
    "MAX_ROUNDS_CEILING",
    "NO_HIT_TEXT",
    "PLAN_MAX_STEPS",
    "PLAN_SYSTEM",
    "SNAPSHOT_FULL_TEXT",
    "STEP_CLIP",
    "TOOL_CONTEXT_ARTIFACT_MARK",
    "TOOL_CONTEXT_ELISION_MARK",
    "TOOL_CONTEXT_ITEM_LIMIT",
    "TOOL_CONTEXT_OMITTED_ARTIFACT_MARK",
    "TOOL_CONTEXT_OMITTED_MARK",
    "TOOL_CONTEXT_OMITTED_PREFIX",
    "TOOL_CONTEXT_TOTAL_LIMIT",
    "TOOL_CONTEXT_TRUNCATION_MARK",
    "TOOL_CONTEXT_TRUNCATION_PREFIX",
    "AgentTurn",
    "ToolCollector",
    "agent_max_rounds",
    "build_agent_tools",
    "cap_tool_context",
    "clip_tool_context_text",
    "decision_prompt",
    "new_tool_call_id",
    "parse_plan",
    "plan_prompt",
    "plan_text",
    "render_observation",
    "resolve_agent_model",
    "stream_agent_turn",
]
