"""Agent 审计（阶段 4 · 条目 4.3.2）：**真执行的工具调用**落 ``agent_runs`` / ``agent_steps``。

老基线（``<legacy-repo>\\backend\\app\\rag\\agent_audit.py``，只读对照）按月写
``backend/agent_audit/agent-audit-YYYYMM.jsonl``；**v2 不照搬**，改用已有的两张表——
``agent_runs`` / ``agent_steps``（``0003`` 迁移已建、ORM 类已有、**此前零写入点**）。
四条理由都指向桌面交付：

1. **表就是为这件事建的**：``AgentRun`` 的列（``chat_id``/``message_id``/``mode``/``status``/
   ``started_at``/``finished_at``/``meta``）与 ``AgentStep`` 的列（``run_id``/``step_no``/``type``/
   ``tool_name``/``tool_call_id``/``content``/``meta``）**与审计要记的字段一一对应**，
   ``tool_call_id`` 上还已有索引 ``ix_agent_steps_tool_call``；
2. **不劈真相源**：冻结前端的 ``ChatDetail.messages[]`` 读的是 ``messages`` 表；审计若另存
   JSONL，「这次对话 Agent 干了什么」就有了两个真相源（4.4 的投影还要再读文件）；
3. **桌面交付与打包**：``6.2b``/``6.4``/``6.5`` 的纪律明写「C 盘零污染」「数据根 = exe 目录」。
   JSONL 只能落在 ``backend/agent_audit/``（**冻结态没有这个目录**——``backend/`` 在
   ``_MEIPASS`` 里**只读**）或新开一个路径锚点；两条都要动 ``6.2b`` 的成果。表在 SQLite 里，零新增路径问题；
4. **键名沿用老基线** ``AGENT_ENABLE_AUDIT``（默认 ``True``）：写死「永远记」会让
   ``CHAT_EVENT_LOG_ENABLE=False`` 的场景仍然落库，与「关掉日志就别留痕」的操作者预期相反。

## 生命周期（三条，缺一不可）

- **开**：一轮 Agent 开始、且 ``db``/``chat_id`` 都可用时建一行（``status="running"``）；
- **收**：**所有退出路径**（正常 / 无工具 / 无模型 / 模型不可用 / 引擎异常 / 取消）都落
  ``status`` 与 ``finished_at``——它是断路器与 4.4 恢复的输入；漏一条就会出现永远 ``running`` 的僵尸行；
- **``message_id`` 回填**：建行时还不知道 assistant 消息 id ⇒ 在 ``chat_routes`` 落库
  assistant 消息**之后**回填（与 3.2 的 ``add_attributions`` 同一位置、同一「commit 之后单独写」模式）。

## 绝不炸穿（I5）

一律 ``safe_*`` 形状（``try/except`` + ``logger.warning`` + **失败计数**），任何异常都不许逃出。
``db is None`` 或 ``chat_id is None`` ⇒ **整段跳过**（单测与工具级调用大量走这条路，别在那里硬抛）。
**顺序固定「先建 run、再写 steps」**：``agent_steps.run_id`` 有外键，run 行没建就写 step
直接 ``IntegrityError``；run 建失败 ⇒ step 整段跳过（不是「写一半」）。

## 只增的读取面（阶段 5 · 条目 `5.5`）

``GET /api/agent/audit`` 的查询侧在本条目被扩成「run 级信息 + 过滤 + 分页 + 进程内计数」，
**全部只增**：端点、路由注册面、既有 4 条律（外层 ``{"items": […]}`` / ``limit`` 1..500 夹取 /
``id`` 倒序 / 只读）与 ``items[]`` 的既有 11 键逐字不动。分工如下：

- :func:`read_audit`：过滤（``run_id`` / ``status`` / ``step_type`` / ``since`` / ``until``）+
  分页（``offset``）+ ``items[].run`` 的第 12 键投影；
- :func:`summarize_runs`：``items[].run`` 去重成顶层 ``runs[]``；
- :func:`safe_counters`：顶层 ``counters`` 键（``5.1``/``5.2``/``5.3`` 留下的**面板化债**，
  **唯一落点**——既不放 ``/api/health``，也不新端点）。

依赖方向：``app.agent`` 层，只依赖 ``models`` / ``core``，**不 import ``app.api``**；
``safe_counters`` 对 ``session`` / ``retrieval`` / ``cache`` 的读取一律**函数内延迟 import**
（模块不可导入时回落全 0，绝不炸穿——I5）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.types import clip
from app.core.app_logging import redact
from app.core.config import settings
from app.models import AgentRun, AgentStep, Message

logger = logging.getLogger(__name__)

#: ``agent_steps.content`` 的截断（与 `tool/result` 的 ``content`` 同源同口径：``runner.STEP_CLIP``）。
#: 这里另写一份常量而不 import runner：``runner`` → ``safety``/``audit`` 是上层依赖下层，
#: 反向 import 会成环。两处数值相等由 ``tests/agent/test_agent_audit.py`` 钉住。
CONTENT_CLIP = 800
#: ``meta.args`` 单个值的截断（与 `tool/call` 载荷的 ``args`` 同口径：``runner.ARGS_CLIP``）。
ARGS_CLIP = 200
#: ``meta.error`` 的截断
ERROR_CLIP = 200
#: ``GET /api/agent/audit`` 的 ``limit`` 天花板（老基线同名常量：``MAX_AUDIT_LIMIT = 500``）
MAX_AUDIT_LIMIT = 500

#: ``status`` 取值域（与 ``models.AgentRun`` 的列注释一致）。
#: ``error``（``回归修复``）：本轮**决策模型调用失败过**（CubeLoop 上游
#: ``stop_reason="error"``）——轮次照常收尾、回答照常作答，但 run 行不与 ``done`` 混同。
RUN_STATUSES: tuple[str, ...] = ("running", "done", "failed", "stopped", "error")

#: **写通道专属的 ``meta`` 键**（专项工具升级-3 · 阶段 5 · `回归检查`）。
#:
#: 为什么要一份**白名单**而不是「调用方给什么就落什么」：``meta`` 是 JSON 列、
#: 会被 ``GET /api/agent/audit`` 原样读出去 ⇒ 一个随手加的键就**永久进了对外可读面**
#: （``02`` 契约未登记）。白名单把「写通道的 meta 形状」钉成**封闭三键**，
#: 将来要加第四键必须**显式**改这里 —— 与 ``A12a`` 的「专属谓词」同源。
#:
#: ⚠️ ``D-回归检查`` 第 ⑥ 项：三键的**来源**必须是写函数返回的 ``WriteOutcome``
#: （``target`` / ``byte_delta`` / ``snapshot_id``），**不得**由调用侧另算
#: —— 否则「审计里记的」与「实际写的」会漂移。
WRITE_META_KEYS: frozenset[str] = frozenset(
    {"write_target", "write_byte_delta", "write_snapshot_id"}
)


def audit_enabled() -> bool:
    """读审计开关（``settings.AGENT_ENABLE_AUDIT``，缺省为开——与老基线的 ``getattr`` 兜底同口径）。"""
    return bool(getattr(settings, "AGENT_ENABLE_AUDIT", True))


# --- 失败计数（可观测兜底；与事件层分开）--------------------------------


_FAILURE_COUNT = 0


def audit_write_failures() -> int:
    """进程内累计的**审计写入**失败次数（事件层另有一个 ``event_write_failures``）。

    为什么与事件层分开计数：两者失败的原因与处置都不同（事件是「留痕缺行」，
    审计是「审计缺行」），混在一个数里无法归因——`回归修复` 记的正是「计数越粗，
    信号越不值钱」。
    """
    return _FAILURE_COUNT


def reset_audit_write_failures() -> None:
    """清零失败计数（测试用）。"""
    global _FAILURE_COUNT
    _FAILURE_COUNT = 0


def restore_audit_write_failures(value: int) -> None:
    """把失败计数**还原到某个值**（测试用）。

    与 :func:`reset_audit_write_failures`（清零）分开的理由同
    :func:`app.session.events.restore_event_write_failures`：故意制造失败用例的用例
    必须在收尾把全局计数还回去，否则后续断言 ``== 0`` 的用例会在整目录连跑时假红。
    """
    global _FAILURE_COUNT
    _FAILURE_COUNT = max(0, int(value))


def _warn(what: str, exc: BaseException) -> None:
    global _FAILURE_COUNT
    _FAILURE_COUNT += 1
    logger.warning(
        "Agent 审计写入失败（已隔离，主流程不受影响）：%s %s: %s（累计第 %s 次）",
        what,
        exc.__class__.__name__,
        redact(str(exc)),
        _FAILURE_COUNT,
    )


def _usable(db: Any, chat_id: int | None) -> bool:
    """本轮能不能写审计：开关 + ``db`` + ``chat_id`` 三件齐才行。"""
    return audit_enabled() and db is not None and chat_id is not None


# --- agent_runs ---------------------------------------------------------


async def start_run(
    db: AsyncSession,
    *,
    chat_id: int | None,
    mode: str,
    meta: dict | None = None,
) -> int | None:
    """一轮 Agent 开始：建 ``agent_runs`` 行，返回 ``run_id``（不可用 / 失败时 ``None``）。

    ``meta`` 建议带 ``{"retrieval_id": …, "turn": …}``——审计要能回答「这一轮是哪次提问、
    哪次检索」，这两个值在 Agent 开跑之前就已经定了（``chat_routes`` 铸的）。
    """
    if not _usable(db, chat_id):
        return None
    try:
        row = AgentRun(chat_id=chat_id, mode=str(mode or "off"), status="running", meta=meta or None)
        db.add(row)
        await db.flush()
        await db.refresh(row)
        return int(row.id)
    except Exception as exc:  # noqa: BLE001 —— 审计绝不炸穿（I5）
        _warn("agent_runs 建行", exc)
        return None


async def finish_run(
    db: AsyncSession,
    run_id: int | None,
    *,
    status: str,
    error: str | None = None,
) -> None:
    """收口一轮：落 ``status`` 与 ``finished_at``（**所有退出路径都要走到**）。

    ``status not in RUN_STATUSES`` 时按 ``failed`` 归一：写进库的值必须是可枚举的，
    否则 4.4 的投影与断路器都读不出结论。``run_id`` 为 ``None``（没建成行）⇒ 整段跳过。

    ⚠️ **两段式收口的第二段（独立 session）已停用**（2026-09-18 实测判定，理由见
    :func:`_apply_run_close_via_fresh_session`：它连的是**产品配置的库**，与测试/调用方的
    库可能不是同一个 ⇒ 「修复」去改另一个库的行，把「没修好」伪装成「修过了」）。
    现在失败就是失败：留 warning + 失败计数，由 ``runner._safe_finish`` 的 rollback-重试兜一层。
    """
    if not audit_enabled() or db is None or run_id is None:
        return
    final = status if status in RUN_STATUSES else "failed"
    if final == "running":
        # 收口不许留 ``running``——那正是「僵尸行」的定义。调用方给错了就按失败记。
        final = "failed"
    try:
        await _apply_run_close(db, int(run_id), final, error)
    except Exception as exc:  # noqa: BLE001 —— 不换连接；异常原样交给调用方重试
        # ⚠️ **只记 warning，不在这里 ``_warn`` 计数**：本函数把异常**原样抛出**，
        # 而调用方 ``runner._safe_finish`` 会 rollback 后重试一次。
        # 若这里也计一次数，一次真实失败会被记成两次（``audit_write_failures`` 是给
        # 「缺口有多少」用的信号，重复计数会把它贬值——`回归修复` 的口径）。
        # 失败的**计数**统一由 `runner._flush_audit` 那侧按「丢了一条记账」记一次。
        logger.warning(
            "agent_runs 收口失败（原 session 不可用；独立 session 通道已停用）：%s: %s",
            exc.__class__.__name__,
            redact(str(exc)),
        )
        raise


async def _apply_run_close(
    db: AsyncSession, run_id: int, status: str, error: str | None
) -> None:
    """在调用方的 session 上写收口（正常路径）。"""
    row = await db.get(AgentRun, run_id)
    if row is None:
        return
    row.status = status
    # ⚠️ **必须用库内时钟**（`回归修复`）：``started_at`` 是 ``server_default=func.now()``，
    # 这里若写 ``datetime.now()``，两个戳就分属两个时钟 ⇒ SQLite 档「跑了多久」偏一个时区。
    # 传 ``db`` 是让判据按**这条连接**的方言走（不是进程级配置）。
    row.finished_at = db_clock_now(db)
    if error:
        meta = dict(row.meta or {})
        meta["error"] = clip(redact(str(error)), ERROR_CLIP)
        row.meta = meta
    await db.flush()


async def _apply_run_close_via_fresh_session(
    run_id: int, status: str, error: str | None
) -> None:
    """独立 session 修复通道 —— **已停用（2026-09-18 实测判定：这条通道是错的）**。

    ⚠️ **为什么停用而不是保留**（实测证据，别把它当"兜底还在、只是没触发"）：

    1. **它连的是另一个库**。``SessionLocal`` / ``get_db`` 用的是**产品配置**的 DSN
       （``settings.database_dsn``，开发态 = ``zhilan_v2``），而测试夹具把 `engine` 换成了
       **独立测试库** ``zhilan_v2_test`` 并覆盖了 ``get_db``。于是这条「修复通道」在测试里
       会去改**另一个库**的同 id 行——要么改不到（``row is None`` 静默返回），要么改错行。
       让一条「修复」通道去写**不同的数据库**，比没有它更糟：它把「没修好」伪装成「修过了」。
    2. **它现在也不需要了**：真正的根因是**生成器关闭链断裂**（``chat_routes.run_agent_turn``
       的裸 ``async for`` 不关子生成器 ⇒ CubeLoop 的任务与它在途的 DB 读活到 teardown），
       已由显式 ``source.aclose()`` 根治。修掉之后，「请求 session 在收口时已不可用」这个
       前提本身不再发生——独立 session 只是给症状打补丁。
    3. **它还会**在失败路径上把 ``CancelledError`` 带出来（实测：收口链里被取消 ⇒
       该函数的 ``await`` 抛 ``CancelledError`` ⇒ 收口整体再失败一次），对收口没有帮助。

    故保留函数名与签名（调用点在 :func:`finish_run`，便于后人查到「这里曾经有一条被否掉的
    通道」与否定它的理由），但**函数体只留一条 warning**，不再碰任何数据库。
    真要根治「调用方 session 不可用」，正确做法是**让 Agent 一轮独占自己的 session**
    （结构性改造，见 runner 模块 docstring 的边界登记），而不是在这里偷偷换一条连接。
    """
    _warn(
        "agent_runs 收口（原 session 不可用；独立 session 通道已按实测停用，不换连接）",
        RuntimeError(
            f"session unavailable while closing run_id={run_id} status={status}"
            + (f" error={error}" if error else "")
        ),
    )


async def bind_message(db: AsyncSession, run_id: int | None, message_id: int | None) -> None:
    """回填 ``agent_runs.message_id``（assistant 消息落库**之后**调）。

    建行时还不知道 assistant 消息 id（那一轮还没生成完），所以这是**第二个**写入点；
    与 3.2 的 ``add_attributions`` 同一位置、同一模式：消息 commit 之后单独写、失败只警告。
    """
    if not audit_enabled() or db is None or run_id is None or message_id is None:
        return
    try:
        row = await db.get(AgentRun, int(run_id))
        if row is None:
            return
        row.message_id = int(message_id)
        await db.flush()
    except Exception as exc:  # noqa: BLE001 —— 审计绝不炸穿（I5）
        _warn("agent_runs 回填 message_id", exc)


# --- agent_steps --------------------------------------------------------


async def write_tool_step(
    db: AsyncSession,
    run_id: int | None,
    *,
    step_no: int,
    tool_name: str,
    tool_call_id: str,
    ok: bool,
    content: str = "",
    ms: int = 0,
    args: dict | None = None,
    error: str = "",
    write_meta: dict | None = None,
) -> bool:
    """记一次**真执行**的工具调用（条目原文「audit 成败都记」），返回**是否真的写进去了**。

    字段映射固定（见 ``4.3`` 开工指令包 §2.2-2）：
    ``type="tool_call"`` / ``tool_name`` / ``tool_call_id`` / ``step_no``（整轮步骤序号，1 起）/
    ``content`` = 观察文本（**按快照编号渲染的那份**，截 :data:`CONTENT_CLIP`）/
    ``meta`` = ``{"ok", "ms", "args", "error"}``。

    **成败都记**：``ok`` 在 ``meta`` 里，不是一个筛选条件——「只记成功的」会让失败的工具调用
    在审计里彻底消失，而失败恰好是最需要回放的那一类。

    ⚠️ **返回 ``bool`` 是本函数与「沉默的 safe_*」的唯一区别**，调用方（``runner._flush_audit``）
    靠它决定要不要推进写入游标。没有它就只能「无条件推进」——那等于**静默丢行**；
    而「失败就重试」写在 ``while`` 里会死循环。返回值是这两者的唯一出路。
    跳过（开关关 / ``db`` 为空 / ``run_id`` 为空）返回 ``False``：那是「没写」，不是「写成功」。
    """
    if not audit_enabled() or db is None or run_id is None:
        return False
    meta: dict[str, Any] = {"ok": bool(ok), "ms": int(ms or 0), "args": _args_digest(args)}
    if error:
        meta["error"] = clip(redact(str(error)), ERROR_CLIP)
    # 写通道专属三键（`回归检查`）：**只增**、且只在**真的给了**的时候出现。
    # ⚠️ 默认 `None` ⇒ 本段一行不动 ⇒ 既有 11 个只读工具的 `meta` 逐字不变
    #（`A20` 腿③ 同款的「既有语义不破」要求；也是本参数存在的全部理由）。
    meta.update(_write_meta_digest(write_meta))
    try:
        row = AgentStep(
            run_id=int(run_id),
            step_no=int(step_no),
            type="tool_call",
            tool_name=str(tool_name or "") or None,
            tool_call_id=str(tool_call_id or "") or None,
            content=clip(str(content or ""), CONTENT_CLIP) or None,
            meta=meta,
        )
        db.add(row)
        await db.flush()
        return True
    except Exception as exc:  # noqa: BLE001 —— 审计绝不炸穿（I5）
        _warn("agent_steps 写入", exc)
        return False


#: **敏感参数键的遮蔽表**（专项工具升级-3 · 阶段 5 · 红线 13 第二款 / `WP-F`）。
#:
#: 为什么需要它（这是**真缺陷**的修法，不是预防性加固）：
#: ``confirm_token`` **原文**原先经 :func:`_args_digest` 原样落进 ``agent_steps.meta["args"]``。
#: 而 ``meta`` 会被 ``GET /api/agent/audit`` 读出、``run_id`` 又就在 ``done.agent_trace`` 里
#: ⇒ **模型/用户拿得到 token 原文** ⇒ 一次性凭证的防伪性被破
#: ⇒ 直接违 **红线 13 第二款**逐字「**不得存在只有模型能读到的确认通道**」。
#:
#: ## 为什么与 ``WRITE_META_KEYS`` 是**两道**防线（缺一不可）
#:
#: - :data:`WRITE_META_KEYS`（白名单）管的是**新增的那三个键**——「只放行三键」；
#: - **本表（黑名单遮蔽）**管的是 ``args`` 这一侧——**既有通道**里出现敏感键名时遮蔽值。
#:   ⚠️ 它**不是**「三键」那一侧的补丁：``confirm_token`` 从来就不是 ``WRITE_META_KEYS`` 的成员，
#:   它走的是 ``meta["args"]``（由 ``args`` 摘要而来）。两处是**不同的**数据流 ⇒ 只挡一侧必然漏另一侧。
#:
#: ## 为什么是「黑名单遮蔽」而不是「只放行 path/content」
#:
#: 后者会**改变既有工具的 meta 值**（任何非 path/content 的参数都会消失），违「只增不改」。
#: 遮蔽的语义是「**键还在、值换掉**」⇒ 既有键集**逐字不变**、只有敏感键的值变化。
#:
#: ⚠️ **大小写与分隔符不敏感**：比对前统一 ``lower()`` 并去掉 ``-``/``_``
#: （``Confirm-Token`` / ``confirmToken`` / ``CONFIRM_TOKEN`` 都是同一件事）。
SENSITIVE_ARG_KEYS: frozenset[str] = frozenset(
    {
        "confirm_token",
        "token",
        "secret",
        "api_key",
        "password",
        "authorization",
    }
)
#: 敏感键被替换成的固定占位（**逐字冻结**，判据直接断言它）
SENSITIVE_ARG_MASK: str = "***"


def _write_meta_digest(write_meta: dict | None) -> dict[str, Any]:
    """把写工具的专属三键**按白名单**转成可安全进 ``meta``（JSON 列）的形状。

    ## 三条口径（`回归检查` · 缺一即不合格）

    1. **白名单过滤**：只放行 :data:`WRITE_META_KEYS` 里的键 ⇒ 调用方多塞的键
       （尤其是将来不小心塞进来的 token / 密钥派生值）**落不了库**。
       这与 `C-R1(乙)` 的「只落校验结果、不落 HMAC 原文」是**同一道防线**：
       白名单是**结构性**的，而「记得别传」不是。
    2. **``None`` 值不落**：``write_snapshot_id`` 在「新建文件」时本就是 ``None``
       （`D-回归检查`：新建无旧内容 ⇒ 快照为空/标记）⇒ 落一个键值为 ``None``
       会让「没做快照」与「快照标识恰好是 None」不可区分。
       ⇒ **有值才落**，判据侧用「键在不在」而不是「值是不是 None」。
    3. **类型收敛**：``write_byte_delta`` 强制 ``int``（负数是合法的 —— 写短了）；
       ``write_target`` 截到 :data:`ARGS_CLIP` 同口径。

    ⚠️ **本函数不抛**：``write_meta`` 是任意 dict（调用侧可能还没落地）⇒ 坏值一律
    静默丢弃。审计的唯一纪律是「绝不炸穿」（I5），为一条元数据把整轮打挂不划算。
    """
    out: dict[str, Any] = {}
    if not isinstance(write_meta, dict):
        return out
    for key in WRITE_META_KEYS:
        if key not in write_meta:
            continue
        value = write_meta[key]
        if value is None:
            continue
        try:
            if key == "write_byte_delta":
                out[key] = int(value)
            elif key == "write_snapshot_id":
                out[key] = clip(str(value), ARGS_CLIP)
            else:  # write_target
                out[key] = clip(str(value), ARGS_CLIP)
        except Exception:  # noqa: BLE001 —— 坏元数据丢弃，绝不炸穿（I5）
            continue
    return out

def _is_sensitive_arg_key(key: Any) -> bool:
    """该参数名是否命中 :data:`SENSITIVE_ARG_KEYS`（**规范化后**比对）。

    规范化 = ``lower()`` + 去掉 ``-``/``_``：让 ``Confirm-Token`` / ``confirmToken`` /
    ``CONFIRM_TOKEN`` 落到同一个键上。⚠️ **不做**「包含匹配」——``token_count``
    这种正常参数不该被误遮蔽（遮蔽是**不可逆**的，误伤即丢审计信息）。
    """
    name = str(key or "").strip().lower().replace("-", "").replace("_", "")
    if not name:
        return False
    return any(
        name == candidate.replace("-", "").replace("_", "")
        for candidate in SENSITIVE_ARG_KEYS
    )


def _mask_sensitive_value(value: Any) -> Any:
    """递归遮蔽**嵌套**容器里的敏感键值（`回归修复`；078 只挡了顶层键，本助手补另一半）。

    - ``dict`` ⇒ **新** dict：键命中 :func:`_is_sensitive_arg_key` ⇒ :data:`SENSITIVE_ARG_MASK`，
      否则值继续递归；
    - ``list`` / ``tuple`` ⇒ 逐项递归（元组照 JSON 数组形态序列化，形状不变）；
    - 其余（叶子）原样返回，交回调用方的 ``json.dumps(..., default=str)`` 兜底。

    ⚠️ **不改入参**（只构造新容器）；键名判定**必须复用** ``_is_sensitive_arg_key``
    （078 教训：另写一套判定必然漂移）；递归**绝不抛**——任何失败都折回
    :data:`SENSITIVE_ARG_MASK`（fail-closed：宁可多遮，不可漏遮；
    反正序列化失败的值 ``json.dumps`` 本也吐不出来）。
    """
    try:
        if isinstance(value, dict):
            return {
                k: (SENSITIVE_ARG_MASK if _is_sensitive_arg_key(k) else _mask_sensitive_value(v))
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [_mask_sensitive_value(item) for item in value]
    except Exception:  # noqa: BLE001 —— 遮蔽失败不得影响审计主路
        return SENSITIVE_ARG_MASK
    return value


def _args_digest(args: dict | None) -> dict[str, str]:
    """参数摘要：键原样、值只留短文本（无损 JSON，能进 ``meta`` 的 JSON 列）。

    ⚠️ 入参可能是 **pydantic 模型**（ReAct 路径：CubeLoop 已把参数校验成实例），
    也可能是**已经过 ``runner._args_digest`` 的 ``dict[str, str]``**（那一步的结果本来
    就是无损 JSON，再走一次是恒等操作，安全）。``isinstance(args, dict)`` 一个判据会让
    ReAct 路径的 ``meta["args"]`` 恒为空 dict——所以这里不做那个判据。

    ## 🔴 敏感键遮蔽（阶段 5 · `WP-F` · 红线 13 第二款）

    键名命中 :data:`SENSITIVE_ARG_KEYS` ⇒ 值一律替换成 :data:`SENSITIVE_ARG_MASK`，
    **原文绝不落库**。为什么必须在这里做而不是在写工具里「记得别传」：

    - ``meta`` 会被 ``GET /api/agent/audit`` 读出去，``run_id`` 就在 ``done.agent_trace`` 里
      ⇒ 模型/用户**拿得到** ``meta``；
    - 而 ``confirm_token`` 是**一次性凭证**（`D-回归检查`）—— 原文可见 ⇒ 可重放 ⇒
      等于「存在只有模型能读到的确认通道」（红线 13 第二款逐字禁止）；
    - 「记得别传」是**纪律**，本函数是**结构**：任何路径（含 MCP 动态工具、将来的新写工具）
      参数里出现敏感键名都会被遮蔽，不依赖调用方自觉。

    ⚠️ **只增不改**：非敏感键的**键集与值逐字不变**（既有 11 个只读工具的 ``meta["args"]``
    一个字节都不动）；敏感键**键仍在**（不是删掉）——删掉会让「传了凭证」这件事
    在审计里**不可见**，而「遮蔽」保留了「这次传了凭证」这个事实。

    ⚠️ **递归面（`回归修复`）**：值在序列化前先过 :func:`_mask_sensitive_value` ——
    **任意深度**的嵌套 ``dict`` / ``list`` 里命中敏感键名的值同样置 ``***``
    （MCP 工具入参 ``extra="allow"``，任意嵌套均可）。非敏感嵌套值形状逐字不变
    （照旧 ``json.dumps`` 进、``json.loads`` 解得回）。"""
    data: Any = args
    if not isinstance(data, dict):
        dump = getattr(data, "model_dump", None)
        data = dump() if callable(dump) else None
    out: dict[str, str] = {}
    for key, value in (data or {}).items():
        # 🔴 敏感键：**先判后取文本** —— 绝不把原文交给 clip()（万一将来 clip 落日志）
        if _is_sensitive_arg_key(key):
            out[str(key)] = SENSITIVE_ARG_MASK
            continue
        # ⚠️ **非字符串值一律 ``json.dumps``（不是 ``str()``）**—— 这是**行为契约**，不是样式选择：
        # ``tests/api/test_chat_agent.py:778`` 逐字要求 ``json.loads(args["filters"])`` 解得回
        # ⇒ 输出必须是 **JSON 形态**（``{"kb_ids": [9]}``）而不是 **Python repr 形态**（``{'kb_ids': [9]}``）。
        # （该契约由旧 ``runner._args_digest`` 承载；本函数在 ``回归修复`` 收敛时**承接**了它，
        #  故必须逐字保留 —— 曾误用 ``str()`` 而把这条契约打破，见 ``回归修复`` 的回归记录。）
        # ``default=str`` 兜住不可 JSON 化的值（与原 runner 版同口径）。
        # 🔴 ``回归修复``：值**先过递归遮蔽助手**再序列化（顶层敏感键在上面已 continue，
        # 这里拦的是嵌套面）；``json.dumps`` 表达式的形态与 flags 逐字不动（092 契约）。
        text = value if isinstance(value, str) else json.dumps(_mask_sensitive_value(value), ensure_ascii=False, default=str)
        out[str(key)] = clip(text, ARGS_CLIP)
    return out


# --- 读取（``GET /api/agent/audit`` 的查询侧）---------------------------
#
# 条目 `5.5`（可观测）在**不改端点**的前提下把这一侧扩成「可查询 + 可过滤 + 可分页」：
# 全部是**只增**——既有 4 条律（``{"items": […]}`` 外层 / ``limit`` 1..500 夹取 / ``id`` 倒序 /
# 只读）一条不动，``items[]`` 的既有 11 键逐字不变、只多一个第 12 键 ``run``。


def safe_limit(limit: Any) -> int:
    """``limit`` 夹在 ``1..MAX_AUDIT_LIMIT``（老基线 ``max(1, min(int(limit), MAX_AUDIT_LIMIT))``）。

    非法值（``None`` / 非数字）回落默认 200：读接口不该因一个坏查询参数 500。
    """
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return 200
    return max(1, min(value, MAX_AUDIT_LIMIT))


def safe_offset(offset: Any) -> int:
    """``offset`` 夹到 ``>= 0``（条目 `5.5` 的分页：**只加** ``offset``，不改 ``limit`` 的语义）。

    负值夹 0、坏值回落 0——与 :func:`safe_limit` 同口径（读接口不该因坏参数 500）。
    刻意**不引入游标**：两套分页口径是后人的坑。
    """
    try:
        return max(0, int(offset))
    except (TypeError, ValueError):
        return 0


def db_clock_is_utc(db: Any = None) -> bool:
    """库内时钟是不是 **UTC** —— 决定「带时区的时间输入」往哪个方向折（见 :func:`fold_to_db_clock`）。

    两个方言的 ``func.now()`` **不是同一个时钟**（本条新增的 ``since`` / ``until`` 过滤、
    ``items[].run.started_at``、以及写入侧的 ``finished_at``，都建立在这件事之上）：

    - **SQLite**：``func.now()`` 编译成 ``CURRENT_TIMESTAMP``，那是 **UTC**
      （实测：``SELECT CURRENT_TIMESTAMP`` 比 ``datetime.now()`` 慢整整一个时区）；
    - **MySQL**：``NOW()`` 是**服务器本地时间**（``docker-compose.yml`` 钉了 ``TZ: Asia/Shanghai``，
      实测与 ``datetime.now()`` 同值）。

    ⚠️ **开发/测试两态看不出差别**（本机就是 +08:00）——只有桌面交付档（``DB_BACKEND=sqlite``）
    才暴露：不折的话，「最近 8 小时写入的行」会被 ``?since=<本地时间>`` 整段排除。

    ⚠️ **给了 ``db``（会话 / 引擎）就按它实际连的方言判**（推荐、也是写入侧的用法）：
    时间戳的时钟属于**那条连接**，而不是进程级的 ``DB_BACKEND`` 配置。
    这不只是「更准确」——`回归修复` 的方言回归钉就是靠它，在**一个 MySQL 进程里**
    对一条**真 SQLite 连接**做出可判真假的断言（否则只能 monkeypatch 全局配置，
    测的就变成「配置分支」而不是「真方言」）。没给才回落 ``settings.is_sqlite``。
    """
    bind = getattr(db, "bind", None)
    dialect = getattr(bind if bind is not None else db, "dialect", None)
    name = getattr(dialect, "name", None)
    if name:
        return str(name) == "sqlite"
    return bool(getattr(settings, "is_sqlite", False))


def db_clock_now(db: Any = None) -> datetime:
    """**库内时钟**的当前 naive 值——与列的 ``server_default=func.now()`` **同一个时钟**。

    为什么写入侧也必须用它、不能裸写 ``datetime.now()``（`回归修复`）：
    ``agent_runs.started_at`` 是 ``server_default=func.now()``（**数据库时钟**），
    而 ``finished_at`` 收口时若用 Python 本机时钟，两个戳就**分属两个时钟**——
    MySQL 开发态恰好同值（compose 钉了 ``TZ: Asia/Shanghai``，本机也是 +08:00）**看不出来**，
    而 **SQLite 档**（阶段 6 桌面交付）``func.now()`` = ``CURRENT_TIMESTAMP`` = **UTC**
    ⇒ 「一轮跑了多久」整整偏一个时区（实测 8.0 小时，见
    ``tests/agent/test_audit_clock_consistency.py``）。

    它是 :func:`db_clock_is_utc` / :func:`fold_to_db_clock` 那条「**naive = 库内时钟**」口径
    在**写入侧**的对应实现——读侧与写侧从此用同一条规则。

    ⚠️ **MySQL 分支的前提**：``NOW()`` 是**服务器本地时间**，本函数用 Python 本地时间近似它
    （本仓 compose 与本机同时区，实测同值）。若将来 MySQL 服务器与应用不在同一时区，
    正确做法是改用服务端 ``func.now()``（本函数即那个升级点）。
    """
    if db_clock_is_utc(db):
        return datetime.now(timezone.utc).replace(tzinfo=None)
    return datetime.now()


def fold_to_db_clock(value: datetime) -> datetime:
    """把带时区的时间折成**库内时钟**的 naive 值；naive 输入原样返回（就按库内时钟解释）。

    「naive = 库内时钟」是本条对外口径（:func:`parse_audit_time` 的 docstring 与 ``02`` v1.0.4 都写它），
    所以这里只处理**带偏移**的输入：往**库内时钟所在时区**折算，而不是一律折到本机时区。
    """
    if value.tzinfo is None:
        return value
    if db_clock_is_utc():
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.astimezone().replace(tzinfo=None)


def parse_audit_time(value: Any, *, field: str) -> datetime:
    """解析 ISO 8601 时间（``since`` / ``until``）；**坏值抛中文 ``ValueError``**。

    路由把 ``ValueError`` 翻成 **422 + 可读中文**——「静默忽略一个过滤条件」会让使用者
    以为查全了，那正是条目 `5.5` 明令禁止的（§2.1 的坏值列）。

    口径（**naive 就是库内时钟**）：不带时区的串按库内时钟解释、原样比较；
    带 ``Z`` / ``+08:00`` 这类偏移的，先折到**库内时钟所在时区**再丢掉 tzinfo
    （见 :func:`fold_to_db_clock`——SQLite 是 UTC、MySQL 是服务器本地时间）。
    ⚠️ 空值（``None`` / 空串 / 纯空白）在**端点层**就按「没给过滤」处理掉了，
    本函数的空值分支只对**程序化误用**生效。
    """
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{field} 需要 ISO 8601 时间，收到空值")
        candidate = text[:-1] + "+00:00" if text[-1] in ("Z", "z") else text
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError(f"{field} 不是合法的 ISO 8601 时间：{text}") from exc
    return fold_to_db_clock(parsed)


def normalize_run_status(value: Any) -> str | None:
    """校验 ``status`` 的取值域（:data:`RUN_STATUSES`）；不在域内抛中文 ``ValueError``。

    空值（``None`` / 空串）⇒ ``None`` = 不过滤。**不许静默忽略未知取值**——那会把
    「拼错的状态名」伪装成「查过了、没有」。
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text not in RUN_STATUSES:
        raise ValueError(f"status 只接受 {' / '.join(RUN_STATUSES)}，收到 {text}")
    return text


def iso_time(value: datetime | None) -> str | None:
    """``datetime`` → ISO 字符串（``None`` 原样透传）。读侧时间的**唯一**格式化点。"""
    return value.isoformat() if value else None


def _run_projection(run: AgentRun | None) -> dict | None:
    """``agent_runs`` 行的 **8 键**投影——``items[].run`` 与 ``runs[]`` 的**唯一**构造点。

    这 8 键里有 4 个（``started_at`` / ``finished_at`` / ``message_id`` / ``meta``）在条目
    `5.5` 之前**零出口**：查「这一轮什么时候跑的、跑完没有、对应哪条回答、检索 id 是什么」
    只能直查库。

    ``join`` 是 ``isouter`` ⇒ ``run`` 可能是 ``None``（孤儿 step）。那种情况**如实给 ``None``**：
    编一组假值会让「run 级信息缺失」这件事从观测面上消失。
    """
    if run is None:
        return None
    return {
        "id": int(run.id),
        "chat_id": run.chat_id,
        "mode": run.mode,
        "status": run.status,
        "started_at": iso_time(run.started_at),
        "finished_at": iso_time(run.finished_at),
        "message_id": run.message_id,
        "meta": run.meta,
    }


def summarize_runs(items: list[dict]) -> list[dict]:
    """把 ``items[].run`` 去重成顶层 ``runs[]``（**按 ``started_at`` 倒序**）。

    数量恒 ``≤`` ``items`` 涉及的 run 数（不额外查库：同一份投影再用一次）。
    排序键补 ``id`` 做第二键 ⇒ 同秒开始的多个 run 也有**确定**顺序（用例要能断言）。
    """
    unique: dict[int, dict] = {}
    for item in items:
        run = item.get("run")
        if isinstance(run, dict) and isinstance(run.get("id"), int):
            unique.setdefault(int(run["id"]), run)
    return sorted(
        unique.values(),
        key=lambda run: (str(run.get("started_at") or ""), int(run["id"])),
        reverse=True,
    )


# --- 计数可见性（条目 `5.5.2`：面板化债的**唯一**落点）--------------------
#
# 为什么不放 `/api/health`：那是**冻结契约面**（前端状态灯判据），且 `5.1`/`5.2`/`5.3`
# 三个开工包 + 《任务清单》**逐字禁止**动它；为什么不新端点：`02` 的 53 条是全集。
# 详见 `_交接-阶段5-5.5可观测-开工指令包.md` §0.0 第 4 条。

#: 各模块进程内计数的**稳定键序**（缺席即 0；实现里多出来的键照旧透传 ⇒ 只增不改义）。
#: 键名与各模块 ``failure_counts()`` 的文档逐字一致，**不自创同义名**（避免两套真相）。
COUNTER_KEYS: dict[str, tuple[str, ...]] = {
    "graph": (
        "calls",
        "seeds",
        "neighbors",
        "materials",
        "failures",
        "no_terms",
        "no_budget",
        "no_seeds",
    ),
    "cache": ("hit", "miss", "stale", "stored", "evicted", "expired", "error"),
    # 🔴 2026-09-27 第二批（`回归修复` #12，业主拍板⑩）：memory 家族键集 **只增** 第五键
    # `store_failed`（记忆落库失败，`session/memory.py` 的 `_store_facts` except 分支）；
    # 既有四键语义一字不变；`test_agent_audit_5_5.py` 的 A9 精确键集断言已同步升为
    # 「既有键逐字 + 只增超集」（A8 先例）。
    "memory": ("call_failed", "parse_failed", "empty", "stored", "store_failed"),
    # `回归修复`：派生数据失败的可见出口（键 = 各派生链，见 `app/core/derived_counters.py`）。
    # `回归修复`（2026-09-27 阶段二）：**只增**第三键 `stale_vector_points`
    # （旧向量点删除失败 ⇒ 孤儿点挤占检索；真实键表与顺序仍以 `derived_counters.COUNTER_KEYS`
    # 为准 —— `_read_derived_counters()` 回 `ordered_counts()`，本表只是「读不到时的补 0 底」。
    # ⚠️ 本表是**读失败时的补 0 底**，权威表 = `app/core/derived_counters.py::COUNTER_KEYS`
    # （键序/键义以它为准，`_read_derived_counters()` 回的就是 `ordered_counts()`）。
    # 审计 MINOR-2/6（2026-09-27）：兜底曾只列 3 键 ⇒ 读失败时 `counters.derived` 形状会从
    # 8 键**塌缩**成 3 键（同一接口两种形状）⇒ 这里补齐全部 8 键。
    # 判据 = `tests/pipeline/test_bug107_orphan_vector_points.py` 里的
    # `test_audit_derived_fallback_matches_authoritative_keys`（断言本表与
    # `derived_counters.COUNTER_KEYS` **同集**）—— 补键时那条会红，别只改这里。
    # 🔴 2026-09-28 批次三（`083` 收口）：权威表**八键 → 十键**（新增 `trace_persist` /
    # `tool_result_channel`）⇒ 本兜底表**同笔跟随**，否则读失败时形状会从 10 键塌缩回 8 键。
    # 顺序也照权威表尾部追加（`ordered_counts` 的键序 = 权威表键序）。
    "derived": (
        "note_links",
        "doc_links",
        "compaction_summary",
        "usage_events",
        "graph_view_edges",
        "pdf_ocr_fallback",
        "web_fetch_degrade",
        "stale_vector_points",
        "trace_persist",
        "tool_result_channel",
    ),
}

#: ``counters`` 的自我说明（**写进响应**，因为这是进程内量、会被误当成持久化统计）
COUNTERS_NOTE = "进程内自启动以来的累计，不是持久化统计；关态/未启用一律 0"


def _fill_counters(defaults: tuple[str, ...], raw: Any) -> dict[str, int]:
    """按 ``defaults`` 补 0，再把 ``raw`` 里多出来的键原样并入（值坏掉就跳过）。"""
    out: dict[str, int] = {key: 0 for key in defaults}
    if isinstance(raw, dict):
        for key, value in raw.items():
            try:
                out[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
    return out


def safe_counters() -> dict:
    """``counters`` 顶层键的**唯一构造点**（开工包 §2.2 记作 ``_safe_counters()``）。

    - 只调各模块**既有的只读计数 API**（``event_write_failures`` / ``event_incomplete_total`` /
      ``audit_write_failures`` / ``graph.failure_counts`` / ``cache.failure_counts`` /
      ``memory.failure_counts``）——**不改它们的签名或语义**；
    - **读失败 / 模块不可导入 ⇒ 该子对象回落全 0，绝不抛**（I5 同型：观测项不该让被观测的
      请求炸掉）。这也是这里用**函数内延迟 import** 而不是模块顶层 import 的原因；
    - ``events.write_failures`` 与 ``/api/health`` 的第 12 键**是同一个值**——**有意重复**：
      health 面向「就绪」、``counters`` 面向「面板」，两处语义不同，别当成重复口径去删。
    """
    counters: dict[str, Any] = {
        "_note": COUNTERS_NOTE,
        "events": {"write_failures": 0, "incomplete_total": 0},
        "audit": {"write_failures": 0},
        "graph": _fill_counters(COUNTER_KEYS["graph"], None),
        "cache": _fill_counters(COUNTER_KEYS["cache"], None),
        "memory": _fill_counters(COUNTER_KEYS["memory"], None),
        "derived": _fill_counters(COUNTER_KEYS["derived"], None),
    }
    for name, keys, reader in (
        ("graph", COUNTER_KEYS["graph"], _read_graph_counters),
        ("cache", COUNTER_KEYS["cache"], _read_cache_counters),
        ("memory", COUNTER_KEYS["memory"], _read_memory_counters),
        ("derived", COUNTER_KEYS["derived"], _read_derived_counters),
    ):
        try:
            counters[name] = _fill_counters(keys, reader())
        except Exception as exc:  # noqa: BLE001 —— 观测项回落，绝不炸穿
            logger.warning("计数读取失败（回落全 0）：%s %s: %s", name, exc.__class__.__name__, redact(str(exc)))
    try:
        from app.session import events as events_mod

        counters["events"] = {
            "write_failures": int(events_mod.event_write_failures()),
            "incomplete_total": int(events_mod.event_incomplete_total()),
        }
    except Exception as exc:  # noqa: BLE001 —— 同上
        logger.warning("计数读取失败（回落全 0）：events %s: %s", exc.__class__.__name__, redact(str(exc)))
    try:
        # 本模块自己的计数（同文件，不会 import 失败）
        counters["audit"] = {"write_failures": int(audit_write_failures())}
    except Exception as exc:  # noqa: BLE001 —— 同上
        logger.warning("计数读取失败（回落全 0）：audit %s: %s", exc.__class__.__name__, redact(str(exc)))
    return counters


def _read_graph_counters() -> dict:
    """图谱通道计数（延迟 import：模块不可导入时由调用方回落全 0）。"""
    from app.retrieval import graph as graph_mod

    return dict(graph_mod.failure_counts())


def _read_cache_counters() -> dict:
    """语义缓存计数（同上）。"""
    from app.cache import failure_counts as cache_failure_counts

    return dict(cache_failure_counts())


def _read_derived_counters() -> dict:
    """派生数据失败计数（`回归修复`；同上，延迟 import + 调用方回落全 0）。"""
    from app.core import derived_counters

    return derived_counters.ordered_counts()


def _read_memory_counters() -> dict:
    """跨会话记忆计数（同上）。"""
    from app.session.memory import failure_counts as memory_failure_counts

    return dict(memory_failure_counts())


async def read_audit(
    db: AsyncSession,
    *,
    chat_id: int | None = None,
    limit: int = 200,
    run_id: int | None = None,
    status: str | None = None,
    step_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    offset: int = 0,
) -> list[dict]:
    """读审计记录（**最新在前**），每项 = ``agent_steps`` 行的 11 键 + 第 12 键 ``run``。

    过滤与分页都是**只增的关键字参数**（既有调用点行为不变）：

    - ``chat_id`` 按会话过滤（走 ``agent_runs.chat_id``——``agent_steps`` 没有这个列，
      这正是「run 是主记录、step 是明细」的结构）；
    - ``run_id`` 按 run 过滤；``status`` 按 run 状态（取值域见 :data:`RUN_STATUSES`）；
      ``step_type`` 对应端点的查询参数 ``type``；``since`` / ``until`` 是
      ``agent_steps.created_at`` 上的**半开区间** ``[since, until)``；
    - ``offset`` 与 ``limit`` 组合分页。
    - ⚠️ **过滤一律下推 SQL**：先取全量再在 Python 里过滤会让 ``limit`` 的语义随过滤漂移。
    - ⚠️ ``step_no`` 是**完成序、不是发起序**（``runner`` 在工具返回后按序落行）：并行工具调用
      下按 ``step_no`` 排序 ``≠`` 按发起顺序；要对账请用 ``tool_call_id``。
    - **只读**：不提供任何修改 / 删除入口（老基线原话，照做）；
    - 读失败**降级不炸穿**——但只对非 DB 面：语句构造 / 投影阶段的异常照旧吞成空列表
      （审计是排查用的，读不出来不该把界面搞挂）；
    - **DB 面真失败上抛**：``OperationalError``（连不上库 / 执行时库不可用）**原样抛出**，
      交给 ``main.py`` 既有的 ``OperationalError`` 处理器 ⇒ 503 + ``database_unavailable_hint()``。
      否则断库会被伪装成 200 + 空列表（回归修复：库挂了却报「没有记录」）。
      读取侧**不**计入 ``audit_write_failures``（那是写入失败的专用信号）。
    """
    if db is None:
        return []
    stmt = (
        select(AgentStep, AgentRun)
        .join(AgentRun, AgentRun.id == AgentStep.run_id, isouter=True)
        .order_by(AgentStep.id.desc())
    )
    if chat_id is not None:
        stmt = stmt.where(AgentRun.chat_id == int(chat_id))
    if run_id is not None:
        stmt = stmt.where(AgentStep.run_id == int(run_id))
    if status:
        stmt = stmt.where(AgentRun.status == str(status))
    if step_type:
        stmt = stmt.where(AgentStep.type == str(step_type))
    if since is not None:
        stmt = stmt.where(AgentStep.created_at >= since)
    if until is not None:
        stmt = stmt.where(AgentStep.created_at < until)
    stmt = stmt.limit(safe_limit(limit)).offset(safe_offset(offset))
    try:
        rows = (await db.execute(stmt)).all()
    except OperationalError:
        # 回归修复：DB 面**真失败**（连不上 / 库不可用）不得吞——上抛给 main.py 的
        # OperationalError 处理器（⇒ 503 + database_unavailable_hint()）；否则断库被伪装成
        # 「没有记录」。非 DB 面的读失败仍走下面的降级。
        raise
    except Exception as exc:  # noqa: BLE001 —— 非 DB 面读失败不炸穿
        logger.warning("Agent 审计读取失败（返回空列表）：%s: %s", exc.__class__.__name__, redact(str(exc)))
        return []
    items: list[dict] = []
    for step, run in rows:
        items.append(
            {
                "id": int(step.id),
                "run_id": int(step.run_id),
                "step_no": int(step.step_no),
                "type": str(step.type),
                "tool_name": step.tool_name,
                "tool_call_id": step.tool_call_id,
                "content": step.content,
                "meta": step.meta,
                "created_at": iso_time(step.created_at),
                "mode": getattr(run, "mode", None),
                "status": getattr(run, "status", None),
                # 第 12 键（条目 5.5）：run 级信息一次性收在一处，**不再往 item 顶层散**
                "run": _run_projection(run),
            }
        )
    return items


# --- 消息行的定位辅助（`chat_routes` 回填用的轻量查询）-------------------


async def assistant_message_id(db: AsyncSession, chat_id: int) -> int | None:
    """取该会话**最新一条 assistant 消息**的 id（回填 ``message_id`` 用）。

    为什么不直接拿 ``repo.add_message`` 的返回值：``persist_turn`` 里有两条路径
    （正常 / 停止），各自 ``add_message`` 后都要回填；从库里按「最新一条 assistant」
    取一次，比把 assistant 消息对象一路透传出来更不容易漏（也覆盖「停止轮只落用户消息」
    的情形——那种情况下返回上一轮的 assistant，是**有意**的：``message_id`` 描述的是
    「这一轮的 run 最终对应哪条 assistant 消息」，没有就不该写一个错的）。
    """
    if db is None:
        return None
    stmt = (
        select(Message.id)
        .where(Message.chat_id == int(chat_id), Message.role == "assistant")
        .order_by(Message.id.desc())
        .limit(1)
    )
    try:
        return (await db.execute(stmt)).scalar_one_or_none()
    except Exception:  # noqa: BLE001 —— 回填失败不影响主流程
        return None


__all__ = [
    "ARGS_CLIP",
    "CONTENT_CLIP",
    "COUNTERS_NOTE",
    "COUNTER_KEYS",
    "ERROR_CLIP",
    "MAX_AUDIT_LIMIT",
    "RUN_STATUSES",
    "SENSITIVE_ARG_KEYS",
    "SENSITIVE_ARG_MASK",
    "WRITE_META_KEYS",
    "assistant_message_id",
    "audit_enabled",
    "audit_write_failures",
    "bind_message",
    "db_clock_is_utc",
    "db_clock_now",
    "finish_run",
    "fold_to_db_clock",
    "iso_time",
    "normalize_run_status",
    "parse_audit_time",
    "read_audit",
    "reset_audit_write_failures",
    "restore_audit_write_failures",
    "safe_counters",
    "safe_limit",
    "safe_offset",
    "start_run",
    "summarize_runs",
    "write_tool_step",
]
