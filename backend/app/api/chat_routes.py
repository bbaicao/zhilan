"""chat 域端点（阶段 1 · 条目 1.5）：``POST /api/chat`` 与 ``POST /api/chat/stream``。

**这是把 1.1–1.4 真正接到 HTTP 的地方**——在此之前 routing / generation / clarify
都只是「已实现、已单测、无调用点」。本模块的装配顺序：

```
① 校验（未配模型 400 / 空输入 400 / 图片超限 413）
② 会话：新建或取回（回收站里按 404「会话不存在」）
③ 澄清判定（1.4，**在路由与检索之前**）
     └ 该追问 → kind=clarify，落库，结束（不检索、不直答）
④ 路由（1.2）→ internal_db / external，并留决策日志（I9）
⑤ 检索：**阶段 1 尚未实现**（属阶段 2/3），故本轮恒无资料
⑥ 分路定 kind（1.3 §3.8）：external → direct；internal 未命中 → direct 或 refuse
⑦ 生成正文（1.3 直答；clarify/refuse 不需要模型）
⑧ 落库（1.1）+ 事件（turn/start、user/message、assistant/message、turn/end）
```

## 契约对齐（02 §2 / §7.1 / §7.2）

- 空 text 且无图 → **400「请输入问题」**（流开始前）；
- 图片超限 → **413「文件超过限制，请压缩后重试」**；
- 未配置对话模型 → **400**（``NOT_CONFIGURED_HINT``，可读中文）；
- 会话不存在 → **404「会话不存在」**；
- SSE：``data: <JSON>\\n\\n`` + ``text/event-stream`` + ``Cache-Control: no-cache``
  + ``X-Accel-Buffering: no``；最小事件序 ``meta → token* → done``。

## 本阶段**有意未实装**的部分（不静默丢弃，逐条说明）

| 能力 | 现状 | 归属 |
|---|---|---|
| 检索 / 引用（``refs``） | **都已接真**（3.1 检索 + 3.2 引用：``cited`` / ``fallback`` 两种模式） | 阶段 3.1 / 3.2 |
| 图片问答（``images``） | **已实装**（``回归修复``，2026-09-27）：带图轮强制 ``off`` + 图片铸成多模态 ``image_url`` part 发给 ``vision_config`` 角色 + ``VISION_SYSTEM_PROMPT``；装配不到视觉模型 ⇒ 固定中文提示（``VISION_NOT_CONFIGURED_HINT``） | 业主真机反馈修复笔 |
| ``filters`` 下推 | **已接真**（3.1：按范围收窄两路） | 阶段 3.1 |
| ``skill_ids`` | 只落库，不注入提示词（**``builtin:web`` 例外**：它用来开联网工具门控，4.1） | 阶段 4.5.3 |
| ``agent_mode`` 编排 | **已接真**（4.1：流式路径跑 CubeLoop 决策循环，产 ``agent`` 帧 + ``done.agent_trace``） | 阶段 4.1 |
| ``reasoning`` / ``thinking`` | ``reasoning`` **已接真**（3.4：思考增量即时下发 + `done.reasoning` 为全文）；``thinking`` 元数据仍恒 None | 阶段 3.4 / 4 |
| ``/chat/stop`` 完整语义 | **已实装**（3.4：请求中断注册表 + 流内赛跑检查 + `stopped` 帧 + 部分留档） | 阶段 3.4 |

按统筹 §4.3，这些都不属阶段 1 的验收面；此处显式登记，避免"看起来做了其实没做"。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace

from anyio import CancelScope
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.exc import PendingRollbackError
from sqlalchemy.ext.asyncio import AsyncSession

from app import observe, retrieval
# 停止注册表：**必须起别名**——本模块的端点函数就叫 ``chat_stop``（与老基线同名），
# 直接 `from app.api import chat_stop` 会被函数定义遮蔽（调用点会 AttributeError）。
from app.api import chat_stop as stop_registry
from app.api.chat_schemas import ChatRequest, ChatResponse, ChatStopIn, Filters, sse

# 回归修复 切分 S2（低风险）：语义缓存簇（键构造 / 命中路径 / 写回）已整体迁至 app/api/chat_cache.py，
# 此处同名 re-export 维持既有外部面（chat_routes.build_cache_key / _cache_write_entry / drain_cache_tasks / _CACHE_TASKS 等均照旧可用）。
from app.api.chat_cache import (  # noqa: F401
    _CACHE_TASKS,
    _cache_write_entry,
    _cached_turn_plan,
    _log_cached_retrieval,
    build_cache_key,
    drain_cache_tasks,
    maybe_store_cache,
    schedule_cache_write,
)
# 回归修复 切分 S2（低风险）：TurnPlan 已整体迁至 app/api/turn_plan.py（逐字节搬运），此处同名 re-export 维持既有外部面：
# chat_routes.TurnPlan / from app.api.chat_routes import TurnPlan / monkeypatch.setattr(chat_routes, "TurnPlan", ...) 均照旧可用；chat_cache 从本模块导入 TurnPlan（而不是 chat_routes），避开循环。
from app.api.turn_plan import TurnPlan  # noqa: F401
from app.agent import audit as agent_audit
from app.agent import runner as agent_runner
from app.cache import entry as cache_entry
from app.cache import store as cache_store
from app.core import derived_counters
from app.core.config import settings
from app.core.errors import (
    AUTH_HINT,
    NOT_CONFIGURED_HINT,
    TIMEOUT_HINT,
    ProviderAuthError,
    ProviderTimeout,
)
from app.db.session import get_db
from app.generation import answer as answer_mod
from app.generation import citation as citation_mod
from app.generation import clarify as clarify_mod
from app.generation import source_map
from app.models import Chat, Message
from app.retrieval.types import filter_source_types, includes_supported_source
from app.retrieval.rerank import RerankStats
from app.retrieval import query as query_mod
from app.routing import decision as routing_decision
from app.routing import decisions as routing_decisions
from app.routing import router as routing_router
from app.session import attempt as attempt_mod
from app.session import events as event_log
from app.session import memory as memory_mod
from app.session import recover as recover_mod
from app.session import repository as repo
from app.session import service as session_service
# 回归修复 切分 S1（低风险）：轮号分配（`next_turn_no`）与事件归属键（`turn_key_of`）连同
# `回归修复` 的进程内预留台账已迁至 `app/session/turn_numbers.py`（按 `AsyncEngine` 分表的
# 模块级单例语义随迁，逐字不动）。此处**同名 re-export** 保持既有外部面逐字可用：
# `from app.api.chat_routes import turn_key_of`、`chat_routes.next_turn_no(...)` 属性访问，
# 以及 `monkeypatch.setattr(chat_routes, "next_turn_no", …)` 这类栅栏（本模块内调用点仍按
# **裸名**解析 ⇒ 栅栏照样命中）。
from app.session.turn_numbers import (  # noqa: F401
    _TURN_LEDGER,
    _TURN_LEDGER_MAX_CHATS,
    _reserve_turn,
    _turn_ledger_of,
    next_turn_no,
    turn_key_of,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# 图片上限（老基线口径：20MB，逐张校验，超限整单拒绝）
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# Agent 模式合法取值（本阶段只校验，不编排）
AGENT_MODES = ("react", "plan_execute")
VALID_AGENT_MODES = ("off", *AGENT_MODES)


# --------------------------------------------------------------------------
# 模型装配（可注入：测试 monkeypatch 本函数即可，无需改全局配置）
# --------------------------------------------------------------------------


def resolve_chat_model():
    """取本次问答用的对话模型（CubeLoop bound model）；没配好返回 ``None``。

    **这是「带思考参数」的那一个入口**：只有最终答案那一条调用该用它。
    辅助链路（Agent 决策 / 澄清 / 压缩 / 记忆抽取）用 :func:`resolve_aux_model`；
    **带图轮**用 :func:`resolve_vision_model`（回归修复）。
    """
    return answer_mod.build_chat_model()


def resolve_vision_model():
    """取**带图轮**用的视觉模型（``vision_config`` 角色）；没配好返回 ``None``。

    回归修复（2026-09-27）：带图轮的装配在**端点侧就分好**——``images`` 非空走这里，
    空走 :func:`resolve_chat_model`。两条回答链路（同步 / 流式）同一分叉。
    ⛔ 返回 ``None`` 时调用方按「视觉提示」收尾（``VISION_NOT_CONFIGURED_HINT``），
    **不得**回落 :func:`resolve_chat_model` 把纯文本发给模型猜——那正是 101 的缺陷本体。
    """
    return answer_mod.build_vision_model()


def resolve_aux_model():
    """取本次问答用的**辅助链路**模型（**不带**用户配置的思考参数）；没配好返回 ``None``。

    注入到三条辅助形参（`clarify_model` / `compaction_model` / `memory_model`）与
    `stream_tokens` 的 `aux_model`。分开的理由见
    `app/generation/answer.py::_build_bound_model` 的 docstring：
    「一个入口写漏就是一半带思考、一半不带的偏科」。
    """
    return answer_mod.build_aux_model()


def resolve_routing_classifier():
    """取意图路由的 LLM 兜底分类器；没配好返回 ``None``（路由按 fallback 降级）。

    单独抽一个可替换的装配点：**单测必须能把它换成假分类器**，否则「正则没命中」
    的用例会真连模型（统筹 §3.4 要求单测不连真模型；否则测试变慢、依赖网络与配额）。
    """
    from app.routing import llm as routing_llm

    return routing_llm.build_classifier()


def require_chat_ready() -> None:
    """未配置对话模型 → 400（契约口径，可读中文）。"""
    if resolve_chat_model() is None:
        raise HTTPException(status_code=400, detail=NOT_CONFIGURED_HINT)


# --------------------------------------------------------------------------
# 入参校验与归一
# --------------------------------------------------------------------------


def chat_images(payload: ChatRequest) -> list[str]:
    """图片归一成保序列表（老基线口径：``images`` 优先，回落旧单图字段）。"""
    images = [item for item in (payload.images or []) if item]
    if not images and payload.image_data:
        images = [payload.image_data]
    return images


def _image_decoded_bytes(image: str) -> int:
    """估算 ``image`` 的**解码后字节数**（不整串解码，避免再复制一份 20 MiB 级内存）。

    前端送的是 ``data:image/...;base64,<payload>`` 形态（``answer._image_content`` 的分支一），
    base64 相对原图膨胀 4/3：15 MiB 原图的 data URL 有 20 971 542 字符，旧口径拿**串长**判
    上限会把它误判 413（回归修复）。这里按规范 base64 的算术关系折算
    ``len(payload) * 3 // 4 - padding``——对规范 base64 该式**精确等于**解码字节数：
    ``n % 3 == 0`` 无 padding；``n % 3 == 1`` 补 ``==``；``n % 3 == 2`` 补 ``=``。

    无逗号的畸形头（``data:`` / ``data:image/png;base64``）用 ``partition`` 取**空载荷** ⇒
    0 字节，不抛（复核 must-fix：改前 ``split(",", 1)[1]`` 会 IndexError ⇒ 未处理 500）。
    """
    payload = image.partition(",")[2] if image.startswith("data:") else image
    padding = 2 if payload.endswith("==") else 1 if payload.endswith("=") else 0
    return max(0, len(payload) * 3 // 4 - padding)


def parse_agent_mode(payload: ChatRequest) -> str | None:
    """校验 agent_mode；非法值 400（流式/非流式同一套，流开始前定死）。"""
    mode = (payload.agent_mode or "").strip() or None
    if mode is not None and mode not in VALID_AGENT_MODES:
        raise HTTPException(
            status_code=400, detail="未知的 Agent 模式，可选：off / react / plan_execute"
        )
    return mode


def resolve_agent_mode(payload: ChatRequest, images: list[str] | None = None) -> str:
    """本轮**生效**的 Agent 模式（阶段 4 · 条目 4.1）。

    取值链与老基线逐条对齐：

    1. 请求显式给了就用它（非法值在 :func:`parse_agent_mode` 里已经 400）；
    2. 没给 → 跟随设置页默认值 ``settings.AGENT_MODE``；设置页里的坏值按 ``off`` 处理
       （与老基线同口径：**设置坏数据不该 400 掉用户这一轮**）；
    3. **带图强制 ``off``**（老基线 L1410 注释：视觉模型不走编排）。

    返回 ``off`` 表示走原来的单轮链路（行为与接 Agent 之前**逐字一致**）。
    """
    requested = parse_agent_mode(payload)
    mode = requested or (str(getattr(settings, "AGENT_MODE", "") or "").strip() or "off")
    if mode not in VALID_AGENT_MODES:
        mode = "off"
    # 带图强制 off：``images`` 没传时自己从请求里归一（``chat_images`` 的 ``image_data``
    # 回落口径），免得调用方漏传就悄悄走了编排。
    pictures = chat_images(payload) if images is None else images
    if pictures:
        return "off"
    return mode


def wants_web(plan: "TurnPlan") -> bool:
    """本轮是否启用联网工具（条目 4.1 的 ``web_enabled``）。

    老基线的取值链是 ``settings.WEB_ENABLE`` 且 ``builtin:web ∈ skill_ids``；v2 的 skills 域
    还没实装（4.5.3），但**冻结前端真的会发** ``builtin:web``（``App.tsx`` 对话页联网开关
    → ``skill_ids``），所以这里可以按同一口径直接判——不用等 skills 域，也不需要改前端。
    两个条件缺一不可：设置里没开联网就**永远不暴露**联网工具（条目 4.1 那条门控）。
    """
    if not bool(getattr(settings, "WEB_ENABLE", False)):
        return False
    return "builtin:web" in (plan.skill_ids or [])


# --------------------------------------------------------------------------
# 一次模型尝试（阶段 4 · 条目 4.4 · D6）
# --------------------------------------------------------------------------


def start_attempt(plan: TurnPlan) -> None:
    """为一轮「要用模型写答案」的提问铸一次**尝试 id**（两条回答链路共用这一个入口）。

    - 只在 ``plan.needs_model`` 时铸：clarify / refuse 走预置文案，**不算一次尝试**
      （判据只有一条——会不会落 ``assistant/message``）；
    - 已经有 id 就不重铸（同一轮只允许一次尝试，v2 没有回答侧重试）；
    - 铸点时点上，``/chat`` 与 ``/chat/stream`` **都**必须调用它：老基线只在流式那条铸造，
      只抄那一条会让同步端点的 ``assistant/attempt`` 永远缺席（核验代理 B1 打回的那点）。
    """
    if getattr(plan, "needs_model", False) and not plan_attempt_id(plan):
        plan.attempt_id = attempt_mod.new_attempt_id()


def attempt_provider_model(role: str = "chat") -> tuple[str, str]:
    """尽力取当前**对话 / 视觉**角色的厂商与型号（``assistant/attempt`` 载荷用）。

    ``role="vision"``（回归修复）：带图轮的用量行归属**视觉角色**——读
    ``vision_config``（自带 LLM_MODEL 回落）；读不到就是空串，与既有口径一致
    （这条日志的价值在 ``attempt_id`` 与终态上，**绝不**因为读不到型号而写失败）。
    ``chat_config`` 未配置时是 ``None``，所以整段都兜在 ``try`` 里。
    """
    try:
        config = settings.vision_config if role == "vision" else settings.chat_config
    except Exception:  # noqa: BLE001 —— 未配置 / 配置损坏都不影响日志写入
        return "", ""
    if config is None:
        return "", ""
    provider = str(getattr(config, "provider", "") or "")
    model = str(getattr(config, "model", None) or getattr(config, "name", "") or "")
    return provider, model


def plan_attempt_id(plan: Any) -> str | None:
    """取本轮尝试 id（``TurnPlan.attempt_id``）。

    用 ``getattr`` 而不是直接属性访问：既有用例用 ``SimpleNamespace`` 当 ``TurnPlan`` 的
    替身（只为驱动落库路径），没有这个字段时应当按「本轮没有模型尝试」处理——
    加个新字段就把老用例打成红是没必要的代价（改用例是**掩盖**，这里只是兼容替身）。
    """
    value = getattr(plan, "attempt_id", None)
    return value if isinstance(value, str) and value else None


async def log_attempt(
    db: AsyncSession, chat_id: int, plan: "TurnPlan", outcome: str
) -> None:
    """写一条 ``assistant/attempt``：这次尝试的**终态**（``committed``/``interrupted``/``aborted``）。

    一次尝试恰好一个终态、恰好落一条；失败隔离在 ``safe_append_event`` 里（绝不炸穿本轮）。
    ``outcome`` 非法时 ``attempt_payload`` 会抛 ``ValueError``——那是**写错的参数**，
    不该被静默吞掉（老基线同款：不默写未知值）。
    """
    attempt_id = plan_attempt_id(plan)
    if not attempt_id:
        return
    # 回归修复：带图轮的 ``assistant/attempt`` 记**视觉角色**的厂商与型号——与同轮
    # 用量行（``record_answer_usage`` 的 ``"vision" if images else "chat"`` 同一判据）
    # 同源，``attempt_id`` 串三表（attempt / usage / message）对账时 provider/model 才
    # 不会自相矛盾。``getattr`` 兼容 SimpleNamespace 替身（照 ``plan_attempt_id`` 口径）。
    provider, model = attempt_provider_model(
        "vision" if getattr(plan, "images", None) else "chat"
    )
    result_state = {
        "generation_status": getattr(plan, "generation_status", "completed"),
        "finish_reason": getattr(plan, "finish_reason", None),
    }
    await event_log.safe_append_event(
        db,
        chat_id,
        attempt_mod.ATTEMPT_EVENT,
        attempt_mod.attempt_payload(
            attempt_id, provider=provider, model=model, outcome=outcome
        ) | result_state,
        turn=plan.turn,
        turn_key=turn_key_of(chat_id, plan.turn),
        attempt_id=attempt_id,
    )


#: 未知 skill id 的错误文案（专项工具调用第 2 批 · 第 0 笔 `F4` · **逐字冻结**）。
#: 形状 = 前缀 + id 以「、」连接 + 后缀；全仓只允许本处定义（用例另抄字面量对拍）。
UNKNOWN_SKILL_ID_PREFIX = "未知的技能 id："
UNKNOWN_SKILL_ID_SUFFIX = "。可在设置 → 技能 / MCP 里查看可用技能。"


def validate_request(payload: ChatRequest, images: list[str] | None = None) -> tuple[str, list[str]]:
    """契约校验，返回 ``(正文, 图片列表)``。

    顺序与老基线一致：未配模型 → 空输入 → 图片超限。
    回归修复：就绪校验按**本轮实际装配的角色**查——带图轮装配
    ``resolve_vision_model()``（vision_config 角色，与对话角色互相独立），无图轮查
    ``resolve_chat_model()``（逐字不变）。vision-only 绑定（只给视觉角色配了服务）
    不再被 400「尚未配置对话模型」误拒；带图轮装配不到时 400 文案对齐 v1.0.13
    提示族（``VISION_NOT_CONFIGURED_HINT``），且发生在**流开始前**、零 DB 副作用。
    """
    images = chat_images(payload) if images is None else images
    if images:
        if resolve_vision_model() is None:
            raise HTTPException(status_code=400, detail=answer_mod.VISION_NOT_CONFIGURED_HINT)
    else:
        require_chat_ready()
    text = (payload.message or "").strip()
    if not text and not images:
        raise HTTPException(status_code=400, detail="请输入问题")
    for image in images:
        # 回归修复：按**解码后字节数**判上限——旧口径拿 data URL 串长判，
        # base64 膨胀 4/3 会把 15 MiB 原图误判 413（上限本身仍是 20 MiB 不变）。
        if _image_decoded_bytes(image) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="文件超过限制，请压缩后重试")
    parse_agent_mode(payload)
    if payload.skill_ids:
        from app.api.skills_spec import list_skills

        catalog = list_skills()
        known_ids = {item["id"] for item in catalog}
        # 保持请求顺序、去空白、去重（未知 id 的文案按用户给出的顺序列出）
        ordered_ids: list[str] = []
        seen_ids: set[str] = set()
        for item in payload.skill_ids:
            sid = (item or "").strip()
            if sid and sid not in seen_ids:
                ordered_ids.append(sid)
                seen_ids.add(sid)
        unknown = [sid for sid in ordered_ids if sid not in known_ids]
        if unknown:
            # F4：未知 id 必须在任何写库/调模型之前拒绝（原先静默忽略并真发模型）
            raise HTTPException(
                status_code=400,
                detail=UNKNOWN_SKILL_ID_PREFIX + "、".join(unknown) + UNKNOWN_SKILL_ID_SUFFIX,
            )
        unavailable = [
            f"「{item['name']}」不可用：{item.get('status_note') or '未启用'}"
            for item in catalog
            if item["id"] in seen_ids and (not item.get("available") or not item.get("enabled"))
        ]
        if unavailable:
            raise HTTPException(
                status_code=400,
                detail="；".join(unavailable) + "。可在设置 → 技能 / MCP 里启用或补全配置。",
            )
    return text, images


_IMAGE_REFERENCE = re.compile(
    r"(?:这|那|上|前|刚才|之前).{0,4}(?:图|照片|截图)|(?:图中|图里|图上|图片里|照片里)"
    r"|\b(?:this|that|previous|last)\s+(?:image|picture|photo|screenshot)\b", re.I,
)
_IMAGE_FOLLOWUP = re.compile(
    r"^(?:为什么|怎么回事|什么意思|原因呢|解释一下|详细说说|继续解释|why|how so)[？?！!。.\s]*$", re.I,
)
HISTORY_IMAGE_HINT = "当前没有可用的相关图片，或图片超出上下文限制，请重新附图后重试。"


async def prepare_request(db: AsyncSession, payload: ChatRequest):
    """Select one bounded, same-chat image round without changing stored history."""
    images = chat_images(payload)
    text = (payload.message or "").strip()
    explicit = bool(_IMAGE_REFERENCE.search(text))
    dependent = bool(_IMAGE_FOLLOWUP.fullmatch(text))
    source = None
    if not images and (explicit or dependent) and payload.chat_id is not None:
        chat = await repo.get_chat(db, payload.chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        rows = list((await db.execute(
            select(Message).where(
                Message.chat_id == chat.id, Message.role.in_(("user", "assistant")),
                Message.id > (chat.compressed_up_to or 0),
            ).order_by(Message.id.desc()).limit(query_mod.HISTORY_LIMIT)
        )).scalars())[::-1]
        users = [row for row in rows if row.role == "user"]
        # ponytail: conservative image grammar; ambiguous free-form follow-ups require reattachment.
        if explicit or (users and users[-1].images):
            candidates = [row for row in users if row.images]
            if not candidates:
                raise HTTPException(status_code=400, detail=HISTORY_IMAGE_HINT)
            row = candidates[-1]
            images = list(row.images)
            if len(images) > 4 or sum(_image_decoded_bytes(item) for item in images) > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=400, detail=HISTORY_IMAGE_HINT)
            try:
                if any(not base64.b64decode(item.split(",", 1)[-1], validate=True) for item in images):
                    raise ValueError("empty image")
            except (ValueError, binascii.Error):
                raise HTTPException(status_code=400, detail=HISTORY_IMAGE_HINT) from None
            source = (row.id, [
                {"role": item.role, "content": item.content}
                for item in rows[rows.index(row):]
            ])
    elif not images and explicit:
        raise HTTPException(status_code=400, detail=HISTORY_IMAGE_HINT)
    text, images = validate_request(payload, images)
    return text, images, source


def history_image_available(plan: TurnPlan, model) -> bool:
    source = getattr(plan, "image_history_source", None)
    if source is None:
        return True
    source_id, tail = source
    if source_id <= (plan.chat.compressed_up_to or 0) or plan.history[-len(tail):] != tail:
        return False
    context = answer_context(plan)
    try:
        _, _, budget = answer_mod.assemble_answer_prompt(
            plan.question, plan.history, model=model, images=plan.images,
            materials_text=context["materials_text"], memory_text=plan.memory_text,
            tool_context=context["tool_context"],
        )
    except answer_mod.ContextBudgetExceeded:
        return False
    return budget["history_kept"] >= len(tail)


def terminal_state(plan: TurnPlan, event: str) -> dict:
    status = "failed" if event == "error" else "stopped" if event == "stopped" else getattr(plan, "generation_status", "completed")
    reason = "aborted" if status == "stopped" else getattr(plan, "finish_reason", None)
    if status == "failed" and reason == "stop":
        reason = None
    return {"generation_status": status, "finish_reason": reason}


# --------------------------------------------------------------------------
# 检索（阶段 3 · 条目 3.1：双路召回 + RRF 融合 + 可选重排）
# --------------------------------------------------------------------------


async def retrieve_materials(
    db: AsyncSession, text: str, filters: dict | None = None,
    *, stats: RerankStats | None = None,
) -> tuple[list, str, str | None]:
    """检索库内资料，返回 ``(materials, materials_text, warning)``。

    实装委托给 ``app.retrieval.retrieve``；本函数保留为**唯一调用点与注入点**：
    单测可以 monkeypatch 它直接给材料，不必起 Qdrant、也不用打真 embedding。

    ``warning`` 非空 = 向量路降级（本次只有关键词路），条目 3.3 的 trace 器官据此记
    ``retrieval/result.degraded``（**3.2 时本函数还只有两个返回值**，3.3 为留痕把它加宽——
    调用点只有 ``build_turn_plan`` 一处）。

    ⚠️ **2026-09-18 更正**：本函数在阶段 1 的 docstring 曾写「阶段 3 只需替换本函数体，
    上下的分路/直答/落库一行都不用改」——**该说法不成立**。真正落地时一并改了四处：
    调用点原先只传问题文字（未传 ``filters``）、轮次数据没有材料字段、两条生成路径都把
    「无资料」写死。这四条即阶段 3 §二「接线与验证要求」：范围传递、检索正文进轮次数据、
    同步与流式两路消费资料、两条路径各有「有资料 / 无资料」用例。

    ``text`` 为空串时返回空（不查库）。
    """
    return await retrieval.retrieve(db, text, filters, stats=stats)


# --------------------------------------------------------------------------
# 一轮问答的装配（同步与流式共用）
# --------------------------------------------------------------------------


async def _resolve_chat(db: AsyncSession, payload: ChatRequest, text: str) -> Chat:
    """新建或取回会话；回收站/不存在 → 404「会话不存在」。"""
    try:
        return await session_service.resolve_chat(db, chat_id=payload.chat_id, message=text)
    except repo.ChatNotFound:
        raise HTTPException(status_code=404, detail="会话不存在") from None


async def build_turn_plan(
    db: AsyncSession,
    payload: ChatRequest,
    text: str,
    images: list[str],
    *,
    clarify_model=None,
    routing_classifier=None,
    compaction_model=None,
) -> TurnPlan:
    """定出这一轮的 kind / answer_source / 正文来源（**不生成正文**）。

    ``compaction_model``（阶段 4 · 条目 4.3.3）：长会话历史压缩要一次**非流式小调用**写摘要。
    不传时压缩**整段跳过**（回落到「全量历史」，与接压缩之前逐字一致）——单测与工具级调用
    大量走这条路，别在那里硬抛。**装配点只有本函数一处**（``history = ...``），
    所以同步 ``/chat`` 与流式 ``/chat/stream`` 两路的压缩行为不可能漂移。
    """
    # 首次数据库读取前固定世代，避免准备期间失效后仍把旧事务快照当成新资料。
    cache_generation = cache_store.current_generation()
    chat = await _resolve_chat(db, payload, text)
    # 阶段 4 · 条目 4.4：追问之前先把**上次中断留下的工具配对缺口**补平
    # （只追加 ``recovery/synthesized`` + 配对的表层 ``tool/result``，**不改任何 messages 行**）。
    # 与老基线同位置同口径（`/chat` 与 `/chat/stream` 都在定历史之前做），
    # 放在这里 ⇒ 两条链路不可能漂移；``ensure_balanced`` 对空 id 安全、内部全程失败隔离。
    synthesized = await recover_mod.ensure_balanced(db, chat.id)
    if synthesized:
        logger.info(
            "本轮开始前补平了 %s 条工具配对缺口（chat_id=%s）", len(synthesized), chat.id
        )
    history = await session_service.build_model_history(
        db,
        chat.id,
        chat=chat,
        model=compaction_model,
    )
    filters = payload.filters.to_stored() if isinstance(payload.filters, Filters) else None
    skill_ids = list(payload.skill_ids or [])
    # 轮次号先算一次（三个出口共用同一个值；事件留痕也要用它）。
    # ⚠️ **从 messages 表现算**，不要数 `history` 里的 user 条数：压缩会把早期 user 行换成
    # 「摘要 user+assistant 一对」⇒ 数出来会**回跳**（B7）。见 :func:`next_turn_no`。
    turn = await next_turn_no(db, chat.id)
    # 本轮的 Agent 模式在**流开始前**定死（老基线同口径：不吐半截 SSE 再报错）
    agent_mode = resolve_agent_mode(payload, images)
    query_history: list[dict] = []
    rewrite = query_mod.rewrite_query(text, filters=filters)
    if rewrite.trigger != "none" or agent_mode in AGENT_MODES:
        try:
            rows = await session_service.model_history_rows(db, chat.id)
            query_history = [
                {"role": row.role, "content": row.content}
                for row in rows[-query_mod.HISTORY_LIMIT:]
            ]
            rewrite = query_mod.rewrite_query(text, query_history, filters=filters)
        except Exception as exc:  # 读取原历史失败：保留原问题，不把摘要当用户事实。
            logger.warning("检索消歧历史读取失败：%s", exc.__class__.__name__)
            rewrite = replace(rewrite, status="failed", retrieval_query=text)
    if rewrite.trigger != "none":
        await event_log.safe_append_event(
            db, chat.id, "rewrite/applied", rewrite.payload(), turn=turn
        )

    # --- ③.0 跨会话记忆的「召回 + 渲染」（阶段 5 · 条目 5.1，**5.2 起上提**）---------
    # ⚠️ **为什么从下面（原 :574 的正常出口）提到这里**（条目 5.2 的 §0.0 第 4 行裁决，
    # 本条目**唯一**一处需要改动 5.1 已落地代码的地方）：
    #
    # 缓存的字面位置是「路由后、检索前」，而 `memory_text` 原先在检索**之后**（正常出口）
    # 才算出来 ⇒ 缓存点**早于**它。若键不含 `memory_text`，那么「第一轮无记忆时算出的答案」
    # 会在记忆写入后被复用 ⇒ **5.1 的注入被 5.2 静默吞掉**（两个增强互斥）。
    # 上提到这里三件事同时成立：
    #   ① 键含 `memory_text` ⇒ 5.1 的注入不会被吞；
    #   ② 缓存点前不产生额外 DB 读（召回只查 `memories` 表，与检索无关）；
    #   ③ 对外的「路由后、检索前」字面位置**不变**（缓存点仍排在路由与检索之间）。
    #
    # 放在 `agent_mode` 之后、澄清判定之前：澄清/拒答两条出口压根不调模型，记忆段对它们
    # 毫无意义——这一提**没有**让它们多查一次库（`memory_text` 只为正常出口与缓存键服务）。
    # 关态下 ``recall`` 首行早退返回空、``render_block`` 再早退一次 ⇒ 这一段零 SELECT。
    memory_text = memory_mod.render_block(await memory_mod.recall(
        db, question=text, chat_id=chat.id, filters=filters,
    ))

    # --- ③ 澄清判定：在路由与检索之前 ---------------------------------
    rounds = int(getattr(chat, "clarify_rounds", 0) or 0)
    if images or (agent_mode in AGENT_MODES and not rewrite.needs_clarify):
        # Current images supply context that the text-only clarification cannot inspect.
        # 第 1 批 ①：有效 Agent 模式跳过短句硬短路（不改 clarify 纯函数）。
        # 上限保护仍读同一计数；不在这里另写 routing/decided（A6 甲，并进 route() 那一条）。
        assume = rounds >= clarify_mod.max_rounds()
    else:
        verdict = await clarify_mod.judge_query(
            text if rewrite.needs_clarify else rewrite.retrieval_query,
            query_history if rewrite.trigger != "none" else history,
            clarify_rounds=rounds,
            model=clarify_model,
            force=rewrite.needs_clarify,
        )
        assume = bool(verdict.assume)
        if verdict.is_clarify:
            kind, answer_source, refs = source_map.map_clarify_round()
            # I9：澄清轮**也要**留决策（它短路了路由，「因为要澄清所以没路由」本身
            # 就是这一轮的路由决策）。不记的话事件流里分不清「澄清短路」与「事件写失败」
            # ——回归修复。
            await routing_decisions.record_decision(
                db,
                chat.id,
                routing_decision.clarify_decision(f"澄清轮短路（依据：{verdict.reason}）"),
                request_id=payload.request_id,
            )
            return TurnPlan(
                chat=chat,
                question=text,
                images=images,
                history=history,
                filters=filters,
                skill_ids=skill_ids,
                # 澄清不检索：source_type 记 external（它既非库内命中，也非库内未命中）
                source_type="external",
                kind=kind,
                answer_source=answer_source,
                refs=refs,
                needs_model=False,
                preset_answer=verdict.question,
                turn=turn,
                agent_mode=agent_mode,
                query_rewrite=rewrite,
                query_history=query_history,
            )

    # --- ④ 路由（并留 I9 决策日志）------------------------------------
    classifier = (
        routing_classifier if routing_classifier is not None else resolve_routing_classifier()
    )
    decision = await routing_router.route(rewrite.retrieval_query, classifier=classifier)
    if agent_mode in AGENT_MODES:
        decision = replace(
            decision,
            reason=f"{decision.reason}；Agent 模式：规则短路已降级",
        )
    await routing_decisions.record_decision(db, chat.id, decision, request_id=payload.request_id)

    # --- ④.5 语义缓存查表（阶段 5 · 条目 5.2；**路由后、检索前**的字面位置）--------
    # 位置口径（§0.0 第 3 行）：缓存点**上界** = 路由决策（上面那两行），**下界** = 检索调用
    # （下面的 `retrieve_materials`）；`retrieval_id` 的铸造排在缓存点**之后**——命中轮不复用
    # 缓存里的 id，改由命中路径**现铸**（契约 `Ref.retrieval_id` 是「本轮检索快照 id」，
    # 复用会让两轮共享一个 id，3.3 的 trace 配对语义随之坏掉）。
    #
    # 命中 ⇒ **检索与模型都省**（连答案一起返回，见 §0.0 第 14 行的综合方案）：
    # 既省延迟又省钱；代价是命中前必须先过**存活校验**（`chunk_id` 都在 **且** `content`
    # 指纹一致，见 `app/cache/entry.py::fingerprints_live`）——任一不符就当未命中、两者都重跑。
    # ⚠️ 刻意**不做**「省检索但每次重调模型」那档中间态：要么全用缓存，要么全重跑。
    #
    # `memory_text` 由上面「③.0」**上提后**的局部变量提供 ⇒ 键里的第 ⑥ 个成分拿得到
    # （这正是上提的唯一理由：5.1 的注入不能被 5.2 静默吞掉）。
    #
    # ⚠️ **键为 `None` = 这一轮不进缓存**（Agent 轮 / 带图轮，`build_cache_key` 自己判）——
    # `lookup` 拿到 `None` 直接返回未命中，于是这几类轮**零特殊逻辑**：照旧走完整链路。
    cache_key = build_cache_key(
        question=text,
        filters=filters,
        agent_mode=agent_mode,
        skill_ids=skill_ids,
        images=images,
        history=history,
        memory_text=memory_text,
        # ⑩ 同轮路由结论**必须进键**（回归修复）：上面 :531 刚判完、:537 刚落 `routing/decided`。
        # 不带它，同一句话「先判库内、后判库外」两轮会算出**逐字相同**的键 ⇒ 第二轮把
        # 第一轮 internal_db 的答案 + 引用行原样回放，而同轮事件写的是 external 的自相矛盾。
        source_type=decision.source_type,
        retrieval_query=rewrite.retrieval_query,
        query_history=query_history,
    )
    hit = await cache_store.lookup(db, cache_key)
    if hit is not None and (
        cache_generation != cache_store.current_generation()
        or cache_key != build_cache_key(
            question=text, filters=filters, agent_mode=agent_mode,
            skill_ids=skill_ids, images=images, history=history,
            memory_text=memory_text, source_type=decision.source_type,
            retrieval_query=rewrite.retrieval_query, query_history=query_history,
        )
    ):
        hit = None
    if hit is not None:
        plan = _cached_turn_plan(
            chat=chat,
            text=text,
            images=images,
            history=history,
            filters=filters,
            skill_ids=skill_ids,
            turn=turn,
            agent_mode=agent_mode,
            assume=assume,
            memory_text=memory_text,
            # 命中轮的 ``source_type`` / ``kind`` / ``answer_source`` 一律取自**同轮 decision**
            # （回归修复）：键里已含 source_type ⇒ 能命中的条目必然出自同 source_type 的轮，
            # 因此「本条决策」与「缓存条目」在这里指向同一个来源（⛔ 不再写死 internal_db）。
            source_type=decision.source_type,
            hit=hit,
        )
        plan.query_rewrite = rewrite
        plan.query_history = query_history
        if plan.retrieval_stats is not None:
            plan.retrieval_stats.query_rewrite = rewrite.payload()
        # ⚠️ **必须在这里调**（而**不是**在 `_cached_turn_plan` 里）：后者是**同步**函数，
        # 而事件写入是异步的。漏掉这一行的后果是实测抓到的：命中轮**只**留下
        # `routing/decided` 一条事件，`retrieval/start` + `retrieval/result` 双双缺席
        # ⇒ 4.4 的「本轮事件完整性」按 (chat_id, turn) 分桶会把这一轮记成**缺两行**
        # （而它其实什么都没出错）。
        await _log_cached_retrieval(db, plan)
        return plan

    # --- ⑤⑥ 检索（3.1 已接真：双路召回 + RRF 融合）→ 定 kind -----------
    # 3.3 trace 器官：检索前后各一条事件，用同一个 retrieval_id 配对（为 agent_steps 打样）
    retrieval_id = retrieval.new_retrieval_id()
    await observe.log_retrieval_start(
        db,
        chat.id,
        question=text,
        filters=filters,
        retrieval_id=retrieval_id,
        turn=turn,
    )
    stats = RerankStats()
    stats.query_rewrite = rewrite.payload()
    materials, materials_text, retrieval_warning = await retrieve_materials(
        db, rewrite.retrieval_query, filters, stats=stats
    )
    await observe.log_retrieval_result(
        db,
        chat.id,
        materials=materials,
        retrieval_id=retrieval_id,
        warning=retrieval_warning,
        turn=turn,
        stats=stats,
    )
    # 本轮**全部**资料的 Ref 列表（引用判定的分母，3.2）
    all_refs = citation_mod.refs_from_materials(materials, retrieval_id)
    has_materials = answer_mod.has_materials(materials=materials or None)
    # 回归修复：v2 能提供的来源类型是有限的（文档分块 + **笔记**）⇒ 范围只含库内不存在的
    # 来源（如 ``source_types=['web']``）时本轮材料**必然为空**。这属 §3.8 的「范围空」行
    # （拒答 + `empty_scope`），不是「未命中」⇒ 不再静默落库外直答，并把原因写进拒答正文
    # （复用 3.3 留的 `answer.EMPTY_SCOPE_MARK` 机制，老基线同款口径）。
    # ⚠️ 判据是「**v2 有没有这种来源**」，不是「这条范围里有没有命中」——4.5.2 落地 B-lite
    # （笔记进关键词路）后 ``source_types=['note']`` 已是**可满足的范围**：真没命中该走 §3.8 的
    # 「库内未命中」行（direct / refuse 看 CHAT_DIRECT_ANSWER_ENABLE），不该在这里拒答。
    scope_unsupported = not includes_supported_source(filters)
    if scope_unsupported:
        logger.warning(
            "本轮范围在 v2 里必然为空（只请求了库内不存在的来源：%s）——按范围空拒答（回归修复）",
            "、".join(filter_source_types(filters)),
        )
    kind, answer_source, refs = source_map.map_round(
        decision.source_type,
        has_materials=has_materials,
        # 范围空时**强制**走拒答行：直答会把「v2 没有这种来源」答成「模型自己的知识」
        direct_enabled=(bool(images) or bool(getattr(answer_mod.settings, "CHAT_DIRECT_ANSWER_ENABLE", True)))
        and not scope_unsupported
        # 回归修复（2026-09-27 拍板④「refuse」）：重排门槛把材料**删空**（检索明明命中、
        # 候选全被 RAG_MIN_RERANK_SCORE 过滤）⇒ 同样不落「库外直答」——留痕早已带
        # empty_reason="below_threshold"，缺的正是这条口径。两条边界：真·零命中（no_recall）
        # 仍按 direct_enabled 走库外直答不变；路由判成 external（用户问的就是库外知识，
        # 库内弱匹配被删空不该反过来拒答）也不拦。⚠️ 只作用**非 Agent 轮**：Agent 轮的
        # attempts 在 trace 里可见（不属「静默」），且此处 refuse 会经 needs_model=False
        # 短路掉整个 Agent（联网检索机会一并被杀）——Agent 轮的空手收口归 076 的拍板③管。
        and not (
            agent_mode == "off"
            and not images
            and decision.source_type != "external"
            and stats.empty_reason == "below_threshold"
        ),
    )

    if kind == source_map.REFUSE_KIND:
        return TurnPlan(
            chat=chat,
            question=text,
            images=images,
            history=history,
            filters=filters,
            skill_ids=skill_ids,
            source_type=decision.source_type,
            kind=kind,
            answer_source=answer_source,
            refs=refs,
            materials=materials,
            materials_text=materials_text,
            all_refs=all_refs,
            retrieval_id=retrieval_id,
            needs_model=False,
            preset_answer=answer_mod.refuse_text(
                [
                    f"{answer_mod.EMPTY_SCOPE_MARK}：本机知识库只提供「文档」与「笔记」两类来源，"
                    f"本轮范围里的「{'、'.join(filter_source_types(filters))}」不在其中。"
                ]
                if scope_unsupported
                else None
            ),
            assume=assume,
            turn=turn,
            agent_mode=agent_mode,
            retrieval_stats=stats,
            retrieval_warning=retrieval_warning,
            materials_finalized=True,
            query_rewrite=rewrite,
            query_history=query_history,
        )

    cache_fingerprints = (
        await cache_entry.capture_fingerprints(db, materials) if cache_key is not None else None
    )
    return TurnPlan(
        chat=chat,
        question=text,
        images=images,
        history=history,
        filters=filters,
        skill_ids=skill_ids,
        source_type=decision.source_type,
        kind=kind,
        answer_source=answer_source,
        refs=refs,
        materials=materials,
        materials_text=materials_text,
        all_refs=all_refs,
        retrieval_id=retrieval_id,
        needs_model=True,
        assume=assume,
        turn=turn,
        agent_mode=agent_mode,
        retrieval_stats=stats,
        retrieval_warning=retrieval_warning,
        query_rewrite=rewrite,
        query_history=query_history,
        materials_finalized=True,
        # 跨会话记忆（条目 5.1）：段落在上面「③.0」**上提后**已经算好（条目 5.2 的 §0.0
        # 第 4 行裁决）——缓存键需要它，而缓存点排在本出口**之前**，故不能留在这里现算。
        # 关态下 ``recall`` 首行早退返回空、``render_block`` 再早退一次 ⇒ 这一段零 SELECT。
        memory_text=memory_text,
        cache_generation=cache_generation,
        cache_key=cache_key,
        cache_fingerprints=cache_fingerprints,
    )


def answer_context(plan: "TurnPlan") -> dict:
    """**最终回答请求体的上下文**（两入口、两链路共用的**唯一**组装点 —— D14）。

    「计划」在流开始前定好，但材料与工具结果可能被 Agent 轮**替换/补齐**；所以这里
    在**要发请求的那一刻**现算，而不是把结果缓存进 ``TurnPlan``：

    - ``has_material``：``answer.has_materials(materials=plan.materials or None)``
      ——「检索有没有命中」的分路语义（§3.8），与下游 ``kind`` 一致；
    - ``materials_text``：``plan.materials_text``（Agent 轮收口时会重渲染，见
      :func:`run_agent_turn`）；
    - ``prompt_material``：保留的历史兼容字段，当前生产调用者不读取它。
      实际系统提示由 answer.py 按渲染材料、工具结果和路由状态构造；
      不能把本字段当成已经传给系统提示的判据。

    **为什么必须是一个函数**：同步入口（``_generate`` → ``answer_direct``）与流式入口
    （``stream_tokens`` 自拼）此前各算一份，本批的 D24 一改就是**两处都要改**；
    把它收成一处之后，「两个入口说同一句话」在结构上成立，而不是靠两处注释互相提醒。
    """
    citable = not source_map.refs_empty_for(plan.kind)
    materials_text = (plan.materials_text or "") if citable else ""
    tool_context = list(plan.tool_context or [])
    return {
        "has_material": citable and answer_mod.has_materials(materials=plan.materials or None),
        "materials_text": materials_text,
        # 历史兼容值；当前系统提示不从该字段取值。
        "prompt_material": answer_mod.prompt_has_material(materials_text) or bool(tool_context),
        "tool_context": tool_context,
    }


async def _generate(plan: TurnPlan, model) -> str:
    """产出正文：clarify/refuse 用预置文案，其余调模型（本轮资料区一并进提示词）。

    ``has_material`` 传给 ``answer_direct`` 的口径与 §3.8 的分路一致（**同一个判据**：
    材料快照非空）；``answer_direct`` 内部还会按**渲染正文**再自判一次，二者同源
    ——快照为空时渲染正文必为空，不会出现「说没资料又塞了资料」。

    ``memory_text``（条目 5.1）：由 ``build_turn_plan`` 渲染好存在 plan 上，这里透传——
    真正的拼装点在 ``answer.system_prompt``（本函数不自己拼提示词）。

    缓存命中（阶段 5 · 条目 5.2）：**直接返回缓存里的原始正文**，检索与模型都不跑
    （同一轮的成本只剩「读表 + 一次存活校验 SELECT + 落库」，见 §0.0 第 14 行）。
    调用点不需要分支：命中与未命中的正文在这之后**走完全相同的后续流程**
    （``get_turn_prefix`` → ``finalize_citations`` → ``persist_turn``）。
    """
    if not plan.needs_model:
        plan.generation_status = "completed"
        return plan.preset_answer
    if plan.cached:
        plan.generation_status = answer_mod.generation_status(plan.cached_answer)
        return plan.cached_answer
    web_error = await _prepare_direct_web(plan)
    if web_error:
        return web_error
    # 回归修复：``getattr`` 兼容替身（既有用例用 SimpleNamespace 当 TurnPlan，
    # 没有 ``images`` 字段时按无图处理——照 ``plan_attempt_id`` 的同款口径，
    # 新字段不得把替身驱动的老用例打红）。
    plan_images = getattr(plan, "images", None)
    ctx = answer_context(plan)
    if not history_image_available(plan, model):
        plan.generation_status = "failed"
        plan.error_hint = HISTORY_IMAGE_HINT
        return HISTORY_IMAGE_HINT
    result = await answer_mod.answer_direct(
        plan.question,
        history=plan.history,
        model=model,
        source_type=plan.source_type,
        has_material=ctx["has_material"],
        materials_text=ctx["materials_text"],
        # 普通工具结果通道（第 0 批 · ①）：**只在这条最终回答链路上**透传
        tool_context=ctx["tool_context"],
        memory_text=plan.memory_text,
        direct_enabled=True,
        # 回归修复：带图轮把图片透传给装配与消息构造（无图轮缺省 None，逐字不变）
        images=plan_images,
    )
    plan.generation_status = result.status
    plan.finish_reason = result.finish_reason
    plan.error_hint = result.error_hint
    return result.answer


async def _prepare_direct_web(plan: TurnPlan) -> str | None:
    """普通对话复用联网工具；搜索失败不继续生成无来源的时效回答。"""
    if (
        not plan.needs_model or plan.cached
        or getattr(plan, "agent_mode", "off") in AGENT_MODES
        or not wants_web(plan)
        or any((plan.filters or {}).values())
    ):
        return None
    from app.agent.tools.web_search import WebSearchArgs, build_spec

    observation = await build_spec().handler(WebSearchArgs(query=plan.retrieval_query))
    if not observation.ok or not observation.materials:
        hint = observation.text if not observation.ok else "联网搜索没有找到可引用的资料，请换个关键词重试。"
        plan.generation_status = "failed"
        plan.error_hint = hint
        return hint
    _install_agent_materials(
        plan, _merge_material_snapshots(observation.materials, plan.materials)
    )
    return None


def _install_agent_materials(plan: TurnPlan, materials: list) -> None:
    """用一份已经编号的材料快照替换本轮资料，并同步正文与引用分母。"""
    numbered = retrieval.number(list(materials or []))
    plan.materials = numbered
    plan.materials_text = retrieval.render_materials(numbered) if numbered else ""
    plan.all_refs = (
        citation_mod.refs_from_materials(numbered, plan.retrieval_id) if numbered else []
    )
    if numbered:
        plan.kind = source_map.NORMAL_KIND
        # 回归修复（2026-09-27 拍板①取 B）：normal 轮材料含网页 ⇒ answer_source="web"
        #（基值仍由 kind 派生；覆写唯一控制点在 source_map.answer_source_for_materials）。
        plan.answer_source = source_map.answer_source_for_materials(plan.kind, numbered)
        plan.source_type = "internal_db"
    else:
        plan.refs = []


def _merge_material_snapshots(candidates: list, agent_items: list) -> list:
    """候选与助手材料合成一份快照，同一 chunk 只留一次，编号只排一遍。

    回归修复：合并集必须**夹到 MAX_REFS**。基线检索快照 ≤ ``FUSE_LIMIT``、Agent 自检
    材料在 runner 侧也夹 12，但两者**去重并集**可达 24 条——``refs_from_materials`` 会把
    引用分母静默切成 12，而 ``materials_text`` 照全部材料渲染出 ``[13]…`` 标记行：资料区
    编号与引用判定分母脱节，模型照写 ``[13]`` 必然越界失效。夹齐后编号、渲染正文、refs
    三者同源同长。
    """
    seen: set = set()
    merged: list = []
    for item in list(candidates or []) + list(agent_items or []):
        key = getattr(item, "chunk_key", None) or id(item)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged[: citation_mod.MAX_REFS]


def _clear_plan_materials(plan: TurnPlan) -> None:
    """没有可采用资料时同步清空（保留 回归修复 的实际工具空结果口径）。

    与「use_candidates:False 且 Agent 无材料」的既有路径（收口五态里的 C 态）**同形**：
    kind=direct / answer_source=general / source_type=external，materials·materials_text·
    all_refs·refs 全清。修复的形态：实际工具空结果时 `build_turn_plan` 铸的基线候选原样留到
    `finalize_citations`，正文没写任何 ``[n]`` ⇒ fallback 把它们**全量**挂成
    「模型未标注来源编号，以下为本次检索到的全部资料」——与正文「没找到」自相矛盾（076 的 12 条）。
    """
    plan.materials = []
    plan.materials_text = ""
    plan.all_refs = []
    plan.refs = []
    plan.kind = source_map.DIRECT_KIND
    plan.answer_source = source_map.answer_source_for(plan.kind)
    plan.source_type = "external"


def _apply_agent_closeout(plan: TurnPlan, turn) -> None:
    """按结束决策收口。停止轮不走这里。"""
    invalid = bool(getattr(turn, "closeout_invalid", False))
    closeout = getattr(turn, "closeout", None)
    # 工具快照为空不等于本轮无资料：未调用工具时，已准入的库内候选仍是同轮证据。
    # 现有 trace.steps / tool_context 可判别实际工具尝试；直答路径的私有候选不得提升。
    keep_candidates = (
        plan.kind == source_map.NORMAL_KIND
        and plan.source_type == "internal_db"
        and answer_mod.has_materials(materials=plan.materials or None)
        and not getattr(turn, "tool_context", None)
        and not any(
            step.get("kind") in ("tool", "observation")
            for step in getattr(turn, "steps", ())
        )
    )
    if invalid or not isinstance(closeout, dict):
        # 无协议块（D-3）⇒ 用 Agent 快照；坏协议块（D-10）⇒ 回退经典路径**采候选**。
        # ⚠️ 但经典路径没有候选可采时仍须接住 Agent 已检到的材料 —— 否则整轮材料归零：
        #    回答请求体里一段资料都没有、系统提示还会说「没有资料」（回归修复）。
        if turn.materials and (not invalid or not plan.materials):
            _install_agent_materials(plan, turn.materials)
        elif not turn.materials and not keep_candidates:
            # 实际工具空结果或没有已准入候选：沿用 076 清空规则。
            _clear_plan_materials(plan)
        return
    action = closeout.get("action")
    if action == "clarify":
        plan.kind = "clarify"
        plan.answer_source = source_map.answer_source_for("clarify")
        plan.refs = []
        plan.preset_answer = str(closeout.get("clarification") or "")
        plan.needs_model = False
        plan.attempt_id = None
        plan.materials = []
        plan.materials_text = ""
        plan.all_refs = []
        return
    if closeout.get("use_candidates") is False:
        _install_agent_materials(plan, list(turn.materials or []))
        if not plan.materials:
            plan.kind = source_map.DIRECT_KIND
            plan.answer_source = source_map.answer_source_for(plan.kind)
            plan.refs = []
            plan.source_type = "external"
        return
    if not turn.materials:
        if not keep_candidates:
            _clear_plan_materials(plan)
        return
    _install_agent_materials(
        plan, _merge_material_snapshots(plan.materials, turn.materials)
    )


async def run_agent_turn(
    plan: TurnPlan, model, db: AsyncSession, *, aux_model=None
) -> AsyncIterator[tuple[str, dict]]:
    """跑一轮 Agent：产出 ``("agent", step)`` 帧，并把结果写回 ``plan``。

    阶段 4 · 条目 4.1。三件事，顺序固定：

    1. 装配本轮工具（``web_enabled`` 见 :func:`wants_web`）并跑 CubeLoop 决策循环；
    2. 循环产出的每一步**立刻** yield（前端要实时看到步骤，不能等跑完再一次性给）；
    3. 循环结束后：

       - ``plan.agent_trace`` = 轨迹（含 ``steps`` / ``events_incomplete``）；
       - **若 Agent 拿到了材料**，用它的快照**替换**本轮材料（``materials`` /
         ``materials_text`` / ``all_refs``），并把分路归一到 ``normal`` / ``kb``
         ——老基线 Agent 轮恒 ``kind="normal"``、``answer_source="kb"``；
       - ``retrieval_id`` **保持不变**（仍是 ``build_turn_plan`` 铸的那一个）：
         一次提问一条溯源链，引用、归因与 ``tool/result`` 事件都挂在它上面。

    未调用工具且已有准入库内候选时保留基线快照；明确拒用或澄清、实际工具空结果
    仍按结束决策清空。仅库外直答路径的候选不进入最终回答。

    ``plan.audit_run_id``（条目 4.3.2）：``agent_runs`` 的生命周期**在运行器内部**收口
    （``runner._audit_run``——四条退出路径都在那里），这里只把 id 抄到 plan 上，
    供 :func:`persist_turn` 回填 ``message_id``。

    ``model`` / ``aux_model``（推理等级回流）：**Agent 决策与计划走 ``aux_model``**
    （不带用户配置的思考参数），**收尾回答仍用 ``model``**（带）。两者分开是因为
    决策/计划调用是「内部编排」而不是「用户要的答案」——给它们塞厂商思考参数会
    既拖慢工具循环又让 max_tokens 预算被吃掉。``aux_model`` 为 ``None`` 时**回落 ``model``**
    （保证既有工具级调用与直调 ``run_agent_turn`` 的用例零改动）。
    """
    turn = agent_runner.AgentTurn(mode=plan.agent_mode)
    plan.materials_finalized = False
    completed = False
    # ⚠️ 用**显式句柄 + 显式 aclose**，不要写裸的 `async for ... in stream_agent_turn(...)`
    # （条目 4.3 停止收口，实测根因）：
    # 裸 `async for` 只有一个隐藏的「异步生成器迭代器」句柄，**关闭本生成器不会关闭它**
    # ——于是内层 `stream_agent_turn` 的 `finally`（`agent.abort()` / `prompt_task.cancel()` /
    # 审计收口）都**不会跑**，CubeLoop 的任务和它在途的 DB 读**一起活到测试 teardown**，
    # 与 `db` 夹具的 `session.rollback()` 抢同一个 MySQL 连接：
    # `RuntimeError: readexactly() called while another coroutine is already waiting for
    # incoming data`。显式 `aclose()` 让内层 `finally` **同步**走完（实测 4 passed / 1 error → 全绿）。
    source = agent_runner.stream_agent_turn(
        turn,
        question=plan.question,
        history=plan.history,
        # ⚠️ **Agent 决策 / 计划调用走 aux_model**（不带用户配置的思考参数）——
        # 用户配的「推理等级」是给**最终答案**的，不是给内部编排的。
        model=aux_model if aux_model is not None else model,
        db=db,
        filters=plan.filters,
        web_enabled=wants_web(plan),
        chat_id=plan.chat.id,
        turn_no=plan.turn,
        trace_id=plan.retrieval_id,
        candidates=plan.materials_text or "",
        query_history=plan.query_history,
        original_question=plan.question,
    )
    try:
        async for kind, payload in source:
            yield kind, payload
        completed = True
    finally:
        # 先把子生成器关掉：这一步保证 `stream_agent_turn` 的 `finally`（`_finish` +
        # `_audit_run` 收口 + `agent.abort()`）**在此刻**跑完，而不是等 GC。
        # `aclose()` 对**已经跑完**的生成器是无操作；对「被 `break` 掉、悬挂在 yield 上」的
        # 生成器会注入 `GeneratorExit` 并同步跑完它的 finally（正是我们要的）。
        # 它自己抛错也绝不能盖掉本生成器的关闭流程（I5：绝不炸穿）。
        try:
            await source.aclose()
        except Exception as exc:  # noqa: BLE001 —— 关不掉也要继续做收口
            logger.warning("Agent 轮子生成器关闭失败（已隔离）：%s: %s", exc.__class__.__name__, exc)
        # ⚠️ 以下是 ``回归修复`` 的回写（条目 4.3）：取消/停止时 `stream_agent_turn`
        # 已在它的 `finally` 里走完 `_finish`（材料 / warnings / `events_incomplete` 都算好了），
        # 若本生成器直接跳出就会把这一整份结果丢掉——停止轮的 trace 便只剩 `_produce` 手里那点
        # `steps`、`events_incomplete` 缺失。所以无论正常结束还是被关闭，都要写回 `plan`。
        plan.audit_run_id = turn.run_id
        plan.agent_trace = turn.trace()
        plan.materials_finalized = completed
        # 普通工具结果通道（第 0 批 · ①）：在**同一个收口点**（与下面 materials 同型）写回。
        # ⚠️ 这一行**不依赖** ``turn.materials`` 是否为空：纯文本工具结果（MCP / 失败 /
        # 快照打满）恰恰是「没有材料」的那种，它与材料**互斥分流**（D2′），不是并列双写。
        # 存的是**结构化条目**（渲染交给 ``answer.render_tool_context``，一处渲染）。
        plan.tool_context = list(turn.tool_context)
        if completed and not turn.aborted:
            _apply_agent_closeout(plan, turn)
        elif turn.materials:
            plan.materials = list(turn.materials)
            plan.materials_text = retrieval.render_materials(plan.materials)
            plan.all_refs = citation_mod.refs_from_materials(plan.materials, plan.retrieval_id)
            plan.kind = source_map.NORMAL_KIND
            # 回归修复：中断/未完成轮的同型收口也按材料来源覆写（与 _install_agent_materials 同口径）。
            plan.answer_source = source_map.answer_source_for_materials(
                plan.kind, plan.materials
            )
            # §3.8 的映射是**内部 source_type → kind** 的表：Agent 用工具检到了库内资料，
            # 这一轮就等价于「库内命中」⇒ source_type 一并归到 internal_db，别留下
            # 「external 却带引用」这种表里不存在的组合（落库的 messages.source_type 会自相矛盾）。
            plan.source_type = "internal_db"
        for warning in turn.warnings:
            logger.warning("Agent 轮降级提示：%s", warning)
        if turn.note:
            logger.info("Agent 轮收尾备注：%s", turn.note)


async def stream_tokens(
    plan: TurnPlan, model, db: AsyncSession | None = None, *, aux_model=None
) -> AsyncIterator[tuple[str, str | dict]]:
    """逐段产出正文（**到达即 yield，不攒批**——坑清单的硬口径）。

    产出 ``(kind, payload)`` 二元组（条目 3.4 起，为 SSE 全量事件让路）：

    - ``("agent", 步骤)``：Agent 编排的步骤（**全部在任何 token/reasoning 之前**，
      老基线同序：先跑完编排，再进回答链路）；
    - ``("token", 正文增量)``：转成 ``token`` 帧；
    - ``("reasoning", 思考增量)``：转成 ``reasoning`` 帧（**到达即发不攒批**，契约 §2）；
      厂商不给思考内容时这条自然不出现；
    - clarify / refuse：无需模型，一次性产出预置文案（``token``）。

    ``agent_mode`` 为 ``react`` / ``plan_execute`` 且本轮需要模型时，先跑 Agent
    （条目 4.1）；跑完后的材料若更全，会替换``plan`` 上的快照，**正文生成仍走下面这段
    同一条路径** ⇒ Agent 轮与普通轮的引用编号同源（I3）。

    ``aux_model``（推理等级回流）：只喂给 ``run_agent_turn``（Agent 决策 / 计划调用不带
    用户配置的思考参数）；**下面的收尾回答仍用 ``model``**（带）。缺省 ``None`` 时回落
    ``model`` —— 保证既有工具级调用与直调 ``stream_tokens`` 的用例**零改动**。
    """
    if not plan.needs_model:
        if plan.preset_answer:
            yield "token", plan.preset_answer
        return

    # 缓存命中（阶段 5 · 条目 5.2）：**不跑检索、不跑模型**，把缓存下来的整段正文一次发完。
    # 语义与未命中轮**逐字相同**的收敛点：
    #   - 增量划分不同**是有意的**（缓存里存的是一段完整正文，不是一个增量序列）——
    #     契约 §2 只要求 `meta → token* → done`，拼起来必须等于落库正文（既有用例断的就是这个）；
    #   - 展示前缀由统一流式出口按本轮 `final_empty_reason` 现算；
    #   - 思考走 `reasoning` 帧（「到达即发」在命中轮退化为「整段一次发完」，同样是有意的）；
    #     排在 token 之前，与未命中轮的顺序（先 reasoning 后 token）一致。
    if plan.cached:
        if plan.cached_reasoning:
            yield "reasoning", plan.cached_reasoning
        if plan.cached_answer:
            yield "token", plan.cached_answer
        return

    if plan.agent_mode in AGENT_MODES and db is not None:
        # ⚠️ 显式句柄 + 显式 ``aclose()``，**不要**写裸的 ``async for``（条目 4.6 实测，
        # `回归修复` 的第二面）：裸 ``async for`` 只有一个隐藏的迭代器句柄，关闭本生成器
        # **不会**把 ``GeneratorExit`` 注进 ``run_agent_turn`` —— 那一轮的收尾（`agent.abort()` /
        # 审计收口 / 事件落库，全都要碰请求会话）会被推迟到 **GC 终结器新建的任务**里执行，
        # 与消费端任务随后跑的 `persist_stopped_turn` **并发**用同一个 ``AsyncSession``。
        # 这一条与本仓 `run_agent_turn` 自己的那条修法同型（见 `run_agent_turn` 的 finally 注释：
        # 「不要写裸的 async for」——当年实测 4 passed/1 error）。
        source = run_agent_turn(plan, model, db, aux_model=aux_model if aux_model is not None else model)
        try:
            async for item in source:
                yield item
        finally:
            try:
                await source.aclose()
            except Exception as exc:  # noqa: BLE001 —— 关不掉也不许盖住本轮流程
                logger.warning(
                    "Agent 轮子生成器关闭失败（已隔离）：%s: %s", exc.__class__.__name__, exc
                )

    if not plan.needs_model:
        if plan.preset_answer:
            yield "token", plan.preset_answer
        return

    # 回归修复：``getattr`` 兼容替身（同 ``_generate``——替身 plan 可能没有 ``images``）。
    plan_images = getattr(plan, "images", None)
    # 回归修复：带图轮且视觉模型装配不到 ⇒ 固定中文提示收尾（走 ``hint_for_error``
    # 的错误族：用户消息留档、``error`` 帧送达、不记用量、不抽记忆）。
    # ⛔ 绝不回落对话模型把纯文本发给模型猜——那正是 101 要根治的「模型只能答图片没传上来」。
    # 无图轮不可能进这支（``plan.images`` 恒空），行为逐字不变。
    if model is None:
        plan.generation_status = "failed"
        plan.error_hint = answer_mod.VISION_NOT_CONFIGURED_HINT if plan_images else NOT_CONFIGURED_HINT
        yield "error", plan.error_hint
        return

    from cubeloop.providers.base import TextContent, UserMessage

    web_error = await _prepare_direct_web(plan)
    if web_error:
        yield "error", web_error
        return

    # ⚠️ **Agent 分支跑完之后才取**（上面那段 ``run_agent_turn`` 会写回 ``plan.tool_context``）
    # ——本批的第一性质（D8）就落在这里：``tool_context`` 为空时下面两次调用与改前逐字相同。
    ctx = answer_context(plan)
    if not history_image_available(plan, model):
        plan.generation_status = "failed"
        plan.error_hint = HISTORY_IMAGE_HINT
        yield "error", plan.error_hint
        return
    try:
        system_text, built, _budget = answer_mod.assemble_answer_prompt(
            plan.question, plan.history, materials_text=ctx["materials_text"],
            memory_text=plan.memory_text, tool_context=ctx["tool_context"],
            model=model, images=plan_images,
        )
    except answer_mod.ContextBudgetExceeded:
        plan.generation_status = "failed"
        plan.error_hint = answer_mod.CONTEXT_BUDGET_HINT
        yield "error", plan.error_hint
        return

    typed = answer_mod._to_typed_messages(
        built, TextContent, UserMessage, images=plan_images
    )
    answer_mod.note_prompt_size("stream", system_text, built)
    stream = await answer_mod.stream_answer(
        model, typed,
        # 系统提示按**渲染正文**选（不得写死 True）：answer.py 的一致性不变量要求
        # 「提示说有没有资料」与「消息里有没有资料」永远同真同假，否则等于请模型编造引用
        # （I7 的反面）——这是 3.0 就点名的坑。
        # ``memory_text``（条目 5.1）：**Agent 轮的收尾回答也走这一行**（Agent 分支
        # ``:697–715`` 跑完不 return），所以 Agent 轮的收尾回答与普通轮**同源**带记忆；
        # **不带**的只有 CubeLoop 的决策/计划提示（``agent/runner.py`` 内部另拼提示词）
        # ——这是本条唯一的**有意差异**，已登记进证据。
        #
        # system_text 已由实际材料、工具结果和路由状态生成；
        # answer_context 的历史 prompt_material 字段不驱动本次提示。
        system_text=system_text,
    )
    # 条目 153（P0）：本轮**上游失败**的读出口（事件面 + 收尾结果面各一条，见下两处）。
    # ``final`` 提前置 None：收尾读数整块在 try 里，读不到时这个名字也必须存在。
    failure: str | None = None
    final = None
    answer_parts: list[str] = []
    try:
        async for event in stream:
            kind = getattr(event, "type", "")
            delta = getattr(event, "delta", "") or ""
            if kind == "error":
                failure = failure or _stream_failure_detail(event=event)
                continue
            if not delta:
                continue
            if kind in ("text_delta", "text"):
                answer_parts.append(delta)
                yield "token", delta
            elif kind in ("thinking_delta", "reasoning_delta"):
                yield "reasoning", delta
    except (GeneratorExit, asyncio.CancelledError):
        from app.usage.instrumentation import stop_provider_stream
        await stop_provider_stream(stream)
        raise

    # 真实用量捕获（专项-上下文 · CX-01′）：**厂商回执里的真 token**，只读读数。
    #
    # 为什么必须在这里、为什么是这个写法（都是实测事实，别改成别的形态）：
    # - ``include_usage=True`` 的那个 trailing usage-only chunk **不进事件流**：适配器把它
    #   写进 ``partial.usage``（``cubeloop/providers/openai.py:245-265``），而
    #   ``StreamEvent(type="done")`` **不带** ``partial``（同文件 ``:520``）；
    #   ⇒ 上面那个消费循环只取 ``delta``，真读数在这里**必被丢掉**。
    # - 正解 = 循环结束后 ``await stream.result()``（``cubeloop/providers/base.py:364-390``
    #   返回最终的 ``AssistantMessage``，其 ``.usage`` 是 ``providers.base.Usage``）。
    #   ⚠️ ``result()`` 是**幂等**的（只 await 一个既有 future）⇒ 与 ``generate()`` 走
    #   同一条 provider 路径时读的还是同一份结果，不会多打一次上游。
    # - **绝不打断本轮**：读不到 / 抛错 / 被取消 ⇒ 一律只记 DEBUG。生成器语义照旧
    #   （停止轮与断开路径根本走不到这里；``aclose()`` 命中的是上面的 ``yield`` 悬挂点）。
    #   ⚠️ 这里**刻意不加** ``asyncio.shield``：正常走完这一轮时 producer 已经发出
    #   ``done``，``result()`` 只 await 一个既有 future（幂等、无二次上游调用）；
    #   真被取消时保险位不跟着挂住，才是「不打断本轮」的原意。
    try:
        usage = None
        result = getattr(stream, "result", None)
        if callable(result):
            try:
                final = await result()
            except Exception as exc:  # noqa: BLE001 —— 完成状态未知不能当完整成功
                failure = f"{STREAM_FAILURE_HINT}（{exc.__class__.__name__}）"
                logger.warning("流式收尾结果不可读：%s", exc.__class__.__name__)
            else:
                usage = getattr(final, "usage", None)
        if usage is None:
            logger.debug("本轮上游未回 usage（无真实用量可读）")
        else:
            answer_mod.note_usage("stream", usage)
    except Exception as exc:  # noqa: BLE001 —— 双重兜底：哨兵绝不炸穿本轮
        logger.debug("流式真实用量读数失败（已忽略）：%s: %s", exc.__class__.__name__, exc)

    # 条目 153（P0）· 收尾结果面的失败读出口：``stop_reason == "error"`` 是「本轮失败」的
    # 权威标记（适配器失败时造 ``content=[]`` + ``stop_reason="error"`` + ``error_message``，
    # 同文件 ``:523-555``）。刻意放在用量读数**之后**：真读数该记还得记。⛔ 正文为空 /
    # 模型轮最终空白正文同样失败；clarify / refuse 等预置轮已在模型调用前返回。
    if failure is None:
        failure = _stream_failure_detail(final=final)
    plan.finish_reason = getattr(final, "stop_reason", None) or getattr(final, "finish_reason", None)
    plan.generation_status = answer_mod.generation_status("".join(answer_parts), plan.finish_reason)
    if failure is None and plan.generation_status == "failed":
        failure = answer_mod.EMPTY_ANSWER_HINT
    if failure is not None:
        plan.generation_status = "failed"
        plan.error_hint = failure
        logger.warning("本轮上游失败，以 error 收尾：%s", failure)
        yield "error", failure
        return


RERANK_EMPTY_NOTICE = (
    "本次检索未选出通过当前筛选的资料。以下按一般知识提供参考，不代表已核实你的资料。"
)

#: current_time 成功且被采用时的正文前缀（D-b/D7/D8 · **逐字冻结**）。
#: 触发：``plan.kind ∈ {normal, direct}`` 且 ``tool_context`` 有 ``tool=="current_time"`` 且 ``ok is True``。
TIME_PREFIX = "时间依据：本机系统时钟（含时区偏移）。"


def _has_ok_current_time(plan: TurnPlan) -> bool:
    """本轮是否有一条成功的 ``current_time`` 工具结果（D-b 的唯一判定点）。"""
    for item in getattr(plan, "tool_context", None) or []:
        if getattr(item, "tool", None) == "current_time" and getattr(item, "ok", False) is True:
            return True
    return False


def final_material_state(plan: TurnPlan) -> dict:
    """字段缺席表示未定案；已定案的 null 表示没有空结果原因。"""
    if not getattr(plan, "materials_finalized", False):
        return {}
    stats = plan.retrieval_stats
    from app.retrieval.types import material_identities
    return {
        "final_material_count": len(plan.materials),
        "final_materials": material_identities(plan.materials),
        "semantic_support": None,
        "support_review": "unmeasured",
        "final_empty_reason": (
            stats.empty_reason if not plan.materials and stats is not None else None
        ),
    }


def get_turn_prefix(plan: TurnPlan, raw_text: str) -> str:
    """本轮正文前缀的唯一决策点；调用方只在正文非空时装配。

    顺序（D-b）：``时间前缀`` → ``RERANK_EMPTY_NOTICE`` → ``clarify.assume_prefix``。
    """
    if not (raw_text or "").strip():
        return ""
    time_note = (
        TIME_PREFIX + "\n\n"
        if plan.kind in ("normal", "direct") and _has_ok_current_time(plan)
        else ""
    )
    notice = (
        RERANK_EMPTY_NOTICE + "\n\n"
        if plan.kind == "direct"
        and hint_for_error(raw_text, plan) is None
        and final_material_state(plan).get("final_empty_reason") == "below_threshold"
        else ""
    )
    return time_note + notice + clarify_mod.assume_prefix(plan.assume)


def finalize_citations(plan: TurnPlan, answer: str) -> list:
    """正文产出后定出**最终引用**，返回被引用到的材料行（归因落库的来源）。

    用未装饰的模型正文判定，展示前缀不得参与引用编号解析：

    - ``kind`` 恒空的轮（``direct`` / ``refuse`` / ``clarify``）一律零引用（黑名单在
      :func:`source_map.refs_empty_for`，不在这里另立一份）；
    - 正文里有有效 ``[n]`` → 只保留被点到的来源（``citation_mode="cited"``，按快照顺序）；
    - **一个有效编号都没有**（忘写 / 只写越界）→ 原样带出**全部**来源并标
      ``citation_mode="fallback"``：保留候选快照，不能据此认定模型使用或事实受支持。
    """
    plan.refs = (
        [] if getattr(plan, "generation_status", "completed") in ("failed", "stopped")
        else citation_mod.refs_for_kind(plan.kind, answer, plan.all_refs)
    )
    return citation_mod.cited_materials(plan.materials, plan.refs)


async def persist_turn(
    db: AsyncSession,
    plan: TurnPlan,
    answer: str,
    *,
    images: list[str] | None = None,
    citations: list | None = None,
    reasoning: str | None = None,
    agent_trace: dict | None = None,
    memory_model=None,
    raw_answer: str | None = None,
    from_cache: bool = False,
) -> tuple[Message, Message]:
    """落库一轮问答 + 写会话事件 + 写**引用归因**（3.2）。返回 ``(user_msg, assistant_msg)``。

    ``citations`` 是 :func:`finalize_citations` 给出的编号对应材料或 fallback 候选行；为空则不写归因行
    （direct / refuse / clarify 轮与「零命中」轮都是这种情形）。
    ``reasoning`` 是这一轮的思考全文（3.4 起真的填；厂商不给则为 ``None``）。
    ``agent_trace`` 是 Agent 轮的轨迹（条目 4.1）；``None`` 表示本轮没跑 Agent，
    落库时该列保持 ``NULL``——**旧消息与旧前端因此零影响**（02 §7.4 的
    ``MessageOut.agent_trace`` 本来就允许空）。

    ``memory_model`` / ``raw_answer``（阶段 5 · 条目 5.1）：记忆抽取要一次额外模型调用，
    模型由**调用方注入**（装配点只有端点那一处，与 ``compaction_model`` 同型）；
    ``raw_answer`` 是**未加展示前缀**的模型正文（抽取只该看正文，不该把那句
    「本次检索未选出通过当前筛选的资料…」的前缀当成事实喂进去）。
    两者都不传 ⇒ **整段跳过抽取**（工具级调用与既有用例大量走这条路，行为与接记忆之前逐字一致）。

    ``from_cache``（阶段 5 · 条目 5.2）：本轮正文**来自缓存**（没调模型）。
    ⇒ **跳过记忆抽取**（理由见函数末尾那段注释）；调用方**必须**在命中路径传 ``True``
    ——``answer_succeeded`` 拦不住命中轮（它只看正文字面）。
    用量记录的跳过**不在本函数里**：它由调用点（:func:`record_answer_usage`）自己做，
    与既有的「排在既有 commit 之前」口径一致。
    """
    chat = plan.chat
    turn_key = turn_key_of(chat.id, plan.turn)
    user_msg = await repo.add_message(
        db,
        chat_id=chat.id,
        role="user",
        content=plan.question,
        source_type=plan.source_type,
        filters=plan.filters,
        skill_ids=plan.skill_ids or None,
        images=images or None,
    )
    await db.commit()

    # 事件在落库 commit 之后写（绝不插在 add 与 commit 之间）
    await event_log.safe_append_event(
        db, chat.id, "turn/start", {"turn": plan.turn}, turn=plan.turn, turn_key=turn_key
    )
    await event_log.safe_append_event(
        db,
        chat.id,
        "user/message",
        {"content": plan.question, "role": "user"},
        surface_op="append",
        turn=plan.turn,
        turn_key=turn_key,
    )

    assistant_msg = await repo.add_message(
        db,
        chat_id=chat.id,
        role="assistant",
        content=answer,
        source_type=plan.source_type,
        kind=plan.kind,
        refs=plan.refs,
        filters=plan.filters,
        skill_ids=plan.skill_ids or None,
        reasoning=reasoning or None,
        thinking=terminal_state(plan, "done"),
        agent_trace=agent_trace,
        answer_source=plan.answer_source,
    )
    await db.commit()

    # 归因（3.2）：消息 commit 之后单独写，**与事件日志同款隔离**——归因写失败只记警告，
    # 已经生成好的回答照常返回（绝不炸穿，I5）。不做同一事务是为了不让「溯源缺一行」
    # 反过来毁掉整轮回答（chunk 在检索后被硬删这种罕见情形）。
    if citations:
        try:
            await repo.add_attributions(
                db,
                citation_mod.attribution_rows(
                    assistant_msg.id, plan.materials, plan.refs, plan.retrieval_id
                ),
            )
            await db.commit()
        except Exception as exc:  # noqa: BLE001 —— 归因失败不炸穿本轮
            derived_counters.bump("trace_persist")
            # `回归修复`：rollback 自身也会抛（会话已毁 / 连接断）——它同样不能冒穿本轮，
            # 否则「归因失败不炸穿」的承诺在复位这一行上失效（I5）；失败同样只算一次。
            try:
                await db.rollback()
                # `回归修复`：失败 flush 之后的 rollback 会**过期**本会话里的 ORM 对象
                # （SQLAlchemy 既有语义；本次实测：过期对象的下一次属性访问撞 MissingGreenlet）
                # ⇒ 紧随其后的事件 / 尝试 / 追问计数都要读 chat.id、审计回填要读
                # assistant_msg.id，返回值里还有 user_msg，所以复位后必须把本轮这几个实例
                # 全部重取回来；重取失败同样只警告（「归因失败不炸穿本轮」的承诺不打折）。
                await db.refresh(chat)
                await db.refresh(user_msg)
                await db.refresh(assistant_msg)
            except Exception as rollback_exc:  # noqa: BLE001 —— 复位/重取失败也不炸穿本轮
                logger.warning(
                    "归因回滚或重取失败（事务可能停在失败态）：%s: %s",
                    rollback_exc.__class__.__name__,
                    rollback_exc,
                )
            logger.warning(
                "归因落库失败（回答已生成，仍以正常结果返回）：%s: %s",
                exc.__class__.__name__,
                exc,
            )

    await event_log.safe_append_event(
        db,
        chat.id,
        "assistant/message",
        {"content": answer, "role": "assistant", "kind": plan.kind, "refs": len(plan.refs)},
        surface_op="append",
        turn=plan.turn,
        turn_key=turn_key,
        # 条目 4.4：``assistant/attempt`` / ``stream/interrupted`` / 本条**三处同值**，
        # 于是「这一条回答是哪一次尝试产出的」在日志里可以逐条对账。
        attempt_id=plan_attempt_id(plan),
    )
    # 正文已落库 ⇒ 这次尝试以 ``committed`` 收尾（唯一产消息的终态）。写在 assistant/message
    # **之后**，与老基线同序（事件读方先看到消息、再看到这次尝试的终态记录）。
    await log_attempt(
        db, chat.id, plan,
        attempt_mod.ATTEMPT_INTERRUPTED
        if getattr(plan, "generation_status", "completed") == "incomplete"
        else attempt_mod.ATTEMPT_COMMITTED,
    )
    await event_log.safe_append_event(
        db, chat.id, "turn/end", {
            "kind": plan.kind, **final_material_state(plan),
            "generation_status": getattr(plan, "generation_status", "completed"),
            "finish_reason": getattr(plan, "finish_reason", None),
        },
        turn=plan.turn, turn_key=turn_key
    )

    # 澄清轮计数：本轮是澄清则 +1，否则清零（老基线口径）
    rounds = int(getattr(chat, "clarify_rounds", 0) or 0)
    chat.clarify_rounds = rounds + 1 if plan.kind == "clarify" else 0
    await db.commit()

    # 审计回填（条目 4.3.2）：assistant 消息落库**之后**才知道它的 id ⇒ 与 `add_attributions`
    # 同一位置、同一模式（commit 之后单独写、失败只警告）。写在最后，避免它影响本轮回答。
    if plan.audit_run_id is not None:
        try:
            await agent_audit.bind_message(db, plan.audit_run_id, assistant_msg.id)
            await db.commit()
        except Exception as exc:  # noqa: BLE001 —— 回填失败不炸穿本轮
            derived_counters.bump("trace_persist")
            # `回归修复`：commit 失败后事务停在失败态，不复位的话，紧随其后的记忆抽取
            # 或调用方再碰这个会话就会撞 PendingRollbackError（I5 的延迟型违反）⇒ 补复位，
            # 且复位自身抛错也不冒穿本轮。
            try:
                await db.rollback()
                # `回归修复`：同上 —— 失败 flush 之后的 rollback 会过期 ORM 对象，
                # 而紧随其后的记忆抽取要读 chat.id / assistant_msg.id（调用方还要读
                # plan.chat.id、返回值里还有 user_msg）⇒ 复位后逐个重取；重取失败只警告，
                # 本轮仍以正常结果返回。
                await db.refresh(chat)
                await db.refresh(user_msg)
                await db.refresh(assistant_msg)
            except Exception as rollback_exc:  # noqa: BLE001 —— 复位/重取失败也不炸穿本轮
                logger.warning(
                    "审计回填失败后的回滚或重取失败（事务可能停在失败态）：%s: %s",
                    rollback_exc.__class__.__name__,
                    rollback_exc,
                )
            logger.warning(
                "审计 message_id 回填失败（回答已生成，仍以正常结果返回）：%s: %s",
                exc.__class__.__name__,
                exc,
            )

    # 跨会话记忆抽取（阶段 5 · 条目 5.1）：**成功收口之后**才排（本函数末尾 = 消息、事件、
    # 归因、审计**全部落库之后**）。放在这里而不是两条端点各写一遍的理由：``persist_turn``
    # 是同步 ``/chat`` 与流式 ``/chat/stream`` **共用**的收口点 ⇒ 行为不可能漂移。
    #
    # **只抽完整轮**：``answer_succeeded`` 是「模型真的答出来了」的既有唯一判据
    # （clarify/refuse 走预置文案 ⇒ False；超时/鉴权/未配置/生成失败 ⇒ False）。
    # 停止轮与错误轮压根不走本函数（``persist_stopped_turn`` / ``persist_user_turn``
    # 是另外两个出口）⇒「``stopped`` / ``error`` 一律不抽」由**调用图**保证，不是靠这里判 kind。
    #
    # fire-and-forget：进去就返回（自己的会话、吞异常、失败只 warning+计数），
    # 本轮回答与响应**不受任何影响**（I5）。
    #
    # ⚠️ **缓存命中轮跳过**（阶段 5 · 条目 5.2 的 §0.0 第 5 行）：``answer_succeeded`` 只认
    # 正文字面，命中轮（正文是缓存里的完整回答）会被它判成**成功** ⇒ 必须由调用点用
    # ``from_cache=True`` 显式守卫。跳过的理由：命中轮**压根没调模型**（检索与模型一起省），
    # 重复抽取也不会产生新的记忆行（``memories`` 的 ``(scope, text_hash)`` 唯一索引会拦掉）
    # ——抽了就是白付一次模型调用。**不要**去改 ``answer_succeeded`` 的判据
    # （那是 4.5.4 的冻结口径：「用量记的是**真实发生**的模型调用」）。
    if answer_succeeded(plan, raw_answer) and not from_cache:
        from app.usage.instrumentation import usage_scope
        with usage_scope(db=db, assistant_attempt_id=plan_attempt_id(plan), chat_id=chat.id, turn=plan.turn):
            memory_mod.schedule_extract(
                question=plan.question,
                answer=raw_answer,
                chat_id=chat.id,
                message_id=user_msg.id,
                model=memory_model,
                db=db,
                filters=plan.filters,
            )
    return user_msg, assistant_msg


STREAM_FAILURE_HINT = "生成失败，请稍后重试。"


def _leading_error_class(raw: str | None) -> str | None:
    """从上游 ``error_message``（``"类名: 说明"`` 形态）里**只取类名**，取不到返回 None。

    条目 153（P0）：error 帧的 ``detail`` 必须与契约 §2 同族的**固定中文**提示同形，
    **不夹带厂商原文**——厂商原文可能带 base_url / key 前缀 / 上游 JSON 片段。
    宁可只发固定中文也不透传（所以这里只认 ASCII 标识符形态的开头）。
    """
    text = (raw or "").strip()
    if not text:
        return None
    head = text.split(":", 1)[0].strip()
    if not head or len(head) > 64 or not head.isascii() or not head[0].isalpha():
        return None
    return head if head.replace("_", "").isalnum() else None


def _stream_failure_detail(*, event=None, final=None) -> str | None:
    """本轮是否**上游失败**：失败给出固定中文 ``detail``，否则 None（条目 153 · P0）。

    两个读出口缺一不可（都是适配器实测形态，``cubeloop/providers/openai.py:523-555``）：

    - ``event``：先发的 ``StreamEvent(type="error")``（``delta`` 为 None，会被只认 delta 的
      消费循环整条吃掉）；
    - ``final``：收尾 ``AssistantMessage`` 的 ``stop_reason == "error"``。

    ⛔ 只有 ``"error"`` 算失败：``"length"``（长度截断）等照旧走正常收尾。
    """
    failed = getattr(event, "type", "") == "error" or getattr(final, "stop_reason", "") == "error"
    if not failed:
        return None
    name = getattr(event, "error_type", None) or _leading_error_class(
        getattr(event, "error_message", None)
    )
    if not name:
        name = getattr(final, "error_type", None) or _leading_error_class(
            getattr(final, "error_message", None)
        )
    return f"{STREAM_FAILURE_HINT}（{name}）" if name else STREAM_FAILURE_HINT


def hint_for_error(answer: str, plan: TurnPlan) -> str | None:
    """结构化失败或模型空白正文给出提示；正常回答中的错误词没有特殊含义。"""
    if getattr(plan, "generation_status", "completed") == "failed" or (
        plan.needs_model
        and getattr(plan, "generation_status", "completed") != "stopped"
        and not (answer or "").strip()
    ):
        return getattr(plan, "error_hint", None) or answer_mod.EMPTY_ANSWER_HINT
    return None


def answer_succeeded(plan, answer: str) -> bool:
    """需要模型、状态完整且有正文才触发成功副产物；预置、失败、停止、截断均跳过。"""
    return (
        bool(getattr(plan, "needs_model", False))
        and getattr(plan, "generation_status", "completed") == "completed"
        and bool((answer or "").strip())
    )


async def record_answer_usage(db: AsyncSession, plan, answer: str) -> None:
    """Compatibility call site; provider sends already account for this operation."""
    return


async def ensure_turn_session(db: AsyncSession, plan: TurnPlan) -> None:
    """把**被取消/回滚打断过**的本轮会话对象重新读一遍（条目 4.6 · `回归修复`）。

    为什么需要这一步（真机实测，`stop-before` 场景）：

    一轮的 DB 工作**攒在同一个事务里**，而「停止」是一条取消链——取消可能正好命中请求会话
    在途的那条语句（实测命中 `tool/result` 的 INSERT）⇒ SQLAlchemy 把事务标成
    pending-rollback ⇒ 恢复只能靠 `runner._safe_finish` 里那次 ``db.rollback()``。而
    ``rollback()`` 会把**会话里的对象全部置为 ``expired``**，偏偏 `persist_*` 一路都是
    **同步**属性访问（`plan.chat.id` / `turn_key_of(plan.chat.id, …)`）⇒ 懒加载落在没有
    greenlet 的上下文里 ⇒ ``MissingGreenlet``（实测：`SELECT chats … WHERE pk = 54`）。

    会话行本身在**建的时候就已经提交**（见 :func:`app.session.service.resolve_chat`），
    所以这里 ``refresh`` 一定读得到；回滚丢掉的只是本轮那些还没提交的留痕（事件 / 审计行），
    那由 `events_incomplete` 如实记着——**提问与会话行不许丢**。
    """
    if not sa_inspect(plan.chat).expired:
        return
    try:
        await db.refresh(plan.chat)
    except PendingRollbackError:
        # 「expired 且事务还不可用」= 回滚之前对象就已被作废：先把状态清干净再读一次
        await db.rollback()
        await db.refresh(plan.chat)


async def persist_user_turn(
    db: AsyncSession,
    plan: TurnPlan,
    *,
    images: list[str] | None = None,
    note: str | None = None,
) -> Message:
    """**只落用户消息**（+ ``turn/start`` / ``user/message`` 事件）。返回该消息行。

    两条提前中止的路径（用户停止 / 模型侧异常）共用它：v2 的用户消息是在一轮结束时由
    :func:`persist_turn` 一次写下的，提前中止就必须自己把提问落下来——否则用户的问题
    会从历史里**消失**（老基线是在生成之前就落了用户消息，行为上等价）。
    """
    await ensure_turn_session(db, plan)
    user_msg = await repo.add_message(
        db,
        chat_id=plan.chat.id,
        role="user",
        content=plan.question,
        source_type=plan.source_type,
        filters=plan.filters,
        skill_ids=plan.skill_ids or None,
        images=images or None,
    )
    await db.commit()
    turn_key = turn_key_of(plan.chat.id, plan.turn)
    await event_log.safe_append_event(
        db, plan.chat.id, "turn/start", {"turn": plan.turn}, turn=plan.turn, turn_key=turn_key
    )
    await event_log.safe_append_event(
        db,
        plan.chat.id,
        "user/message",
        {"content": plan.question, "role": "user"},
        surface_op="append",
        turn=plan.turn,
        turn_key=turn_key,
    )
    if note:
        # 这一轮以「模型侧异常」收尾：只补 turn/end，不落 assistant 消息
        await event_log.safe_append_event(
            db,
            plan.chat.id,
            "turn/end",
            {"kind": "error", "detail": note, **final_material_state(plan)},
            turn=plan.turn,
            turn_key=turn_key,
        )
    await db.commit()
    return user_msg


async def persist_stopped_turn(
    db: AsyncSession,
    plan: TurnPlan,
    answer: str,
    *,
    reasoning: str | None = None,
    images: list[str] | None = None,
    agent_trace: dict | None = None,
) -> Message:
    """停止轮落库（契约 §2 + 条目 3.4）。

    - **用户消息一定落**（提问不能因停止而丢）；
    - **助手消息只在真有内容时落**：``kind="stopped"``、``refs=[]``（被停的回答不挂引用）、
      ``answer_source`` 保持 ``NULL``（停止不是一次完整回答）；
    - **极早期停止（一个增量都没收到）⇒ 零字不落消息**：只留用户消息与事件；
    - ``clarify_rounds`` **保持不动**（老基线口径：停止不改变追问计数）；
    - 额外留痕 ``stream/interrupted``（载荷含已交付字符数）+ ``turn/end``（``kind=stopped``）；
    - ``agent_trace``（条目 4.1）：Agent 轮被停时，带上**已经下发给前端的那几步**
      （``{"mode": ..., "steps": [...]}``）。
      没有 agent 步骤则为 ``None``。
    - ``events_incomplete``（条目 4.3 · ``回归修复``）：**补上**。原先停止轮的 trace 刻意
      **整个丢掉**这个字段（注释写「停止不是异常收尾」），后果是：**取消造成的缺行既不计入
      也不出字段**——``CancelledError`` 不被 ``safe_append_event`` 的 ``except Exception`` 捕获，
      于是「本轮日志缺了几行」在停止轮**没有任何出口**。现在按「与该轮投影语义一起定」的口径：
      trace 补上该字段（``turn_key`` 归属到本轮），并且**取消也计数**（见 ``runner._finish``）。
      保留的部分：停止轮**仍然**不是 ``failed``——字段是「日志完整性」，不是「成败」。
    """
    user_msg = await persist_user_turn(db, plan, images=images)
    turn_key = turn_key_of(plan.chat.id, plan.turn)
    attempt_id = plan_attempt_id(plan)
    partial = (answer or "").strip()
    if partial or (reasoning or "").strip():
        assistant_msg = await repo.add_message(
            db,
            chat_id=plan.chat.id,
            role="assistant",
            content=answer,
            source_type=plan.source_type,
            kind="stopped",
            refs=[],
            filters=plan.filters,
            skill_ids=plan.skill_ids or None,
            reasoning=reasoning or None,
            thinking=terminal_state(plan, "stopped"),
            agent_trace=agent_trace,
        )
        await db.commit()
        if plan.audit_run_id is not None:
            try:
                await agent_audit.bind_message(db, plan.audit_run_id, assistant_msg.id)
                await db.commit()
            except Exception as exc:  # noqa: BLE001 —— 回填失败不改停止语义
                derived_counters.bump("trace_persist")
                # `回归修复`：同 #6 —— commit 失败后事务停在失败态 ⇒ 先把事务复位，
                # 再把停止轮交出去；复位自身抛错也不冒穿（停止语义优先）。
                try:
                    await db.rollback()
                    # `回归修复`：同上 —— 失败 flush 之后的 rollback 会过期 ORM 对象，
                    # 而紧随其后的停止轮事件与尝试记录都要读 plan.chat.id ⇒ 复位后重取；
                    # 重取失败只警告，停止语义不变。（同机制的既有先例：`ensure_turn_session`，
                    # 条目 4.6 · `回归修复`；注意本函数内没有局部名 chat，必须用 plan.chat。）
                    await db.refresh(plan.chat)
                    await db.refresh(user_msg)
                except Exception as rollback_exc:  # noqa: BLE001 —— 复位/重取失败也不炸穿本轮
                    logger.warning(
                        "停止轮回滚或重取失败（事务可能停在失败态）：%s: %s",
                        rollback_exc.__class__.__name__,
                        rollback_exc,
                    )
                logger.warning("停止轮审计回填失败：%s: %s", exc.__class__.__name__, exc)
        await event_log.safe_append_event(
            db,
            plan.chat.id,
            "assistant/message",
            {"content": answer, "role": "assistant", "kind": "stopped", "refs": 0},
            surface_op="append",
            turn=plan.turn,
            turn_key=turn_key,
            attempt_id=attempt_id,
        )
    await event_log.safe_append_event(
        db,
        plan.chat.id,
        attempt_mod.INTERRUPTED_EVENT,
        {
            "reason": "stopped",
            "chars": len(answer or ""),
            # 条目 4.4 · D6：载荷**只增键**——把「这次尝试的已交付前缀」固定成可对账的凭证
            # （``attempt_id`` / ``delivered_chars`` / ``delivered_text_hash``）。
            # 旧读方忽略未知键 ⇒ 老口径（``reason`` + ``chars``）逐字仍在。
            **(
                attempt_mod.interrupted_payload(attempt_id, answer, reason="stopped")
                if attempt_id
                else {}
            ),
        },
        turn=plan.turn,
        turn_key=turn_key,
        attempt_id=attempt_id,
    )
    # 停止是这次尝试的终态之一（interrupted）：与上面那条 ``stream/interrupted`` 同值
    await log_attempt(db, plan.chat.id, plan, attempt_mod.ATTEMPT_INTERRUPTED)
    await event_log.safe_append_event(
        db,
        plan.chat.id,
        "turn/end",
        {
            "kind": "stopped", "reason": "stopped", **final_material_state(plan),
            "generation_status": "stopped", "finish_reason": "aborted",
        },
        turn=plan.turn,
        turn_key=turn_key,
    )
    await db.commit()
    return user_msg


# --------------------------------------------------------------------------
# 端点
# --------------------------------------------------------------------------


#: 非流式 ``/api/chat`` 遇到**解析后的有效 Agent 模式**时的错误文案（D11 · **逐字冻结**）。
#: 四个要素缺一不可：① 说清「这个入口不支持」；② 指出**只有流式入口**支持；
#: ③ 给出**可操作的**出路（改用流式 / 把 ``agent_mode`` 设为 ``off``）；④ 中文（02 §4）。
NON_STREAM_AGENT_MODE_DETAIL = (
    "非流式接口不支持 Agent 模式（当前仅流式 /api/chat/stream 支持），"
    "请改用流式入口或把 agent_mode 设为 off。"
)


def guard_non_stream_agent_mode(payload: ChatRequest, images: list[str] | None = None) -> None:
    """非流式 ``/api/chat`` 的能力边界：**解析后的有效模式**是 Agent ⇒ 400（D9/D10/D11）。

    判定点（D10）：**模式解析之后**（``resolve_agent_mode``）、** ``build_turn_plan`` 之前**。

    - **必须**用 ``resolve_agent_mode``（请求显式值 → 设置页 ``AGENT_MODE`` → ``off``，
      带图强制 ``off``）——拍板 1 的原话是「按**解析后的有效模式**判断」，所以
      ``agent_mode=None`` + 全局 ``react`` 也要拦（迁移态，见下）；
    - **不得**放进 ``validate_request``：那个函数被流式入口**共用**，放进去会把流式一起拒掉
      （流式**必须**照常跑 Agent —— 这是本条的边界，不是能力回退）；
    - 纯函数：零 DB、零副作用、只读 ``payload`` 与设置页默认值。

    ⚠️ **一条必须明示的取舍（D11′）**：按本口径，全局 ``AGENT_MODE=react|plan_execute``
    的实例上**所有**非流式请求（**含不带 ``agent_mode`` 的**）都会 400。这是拍板 1
    「不得静默忽略」的直接推论，本批**采纳**；已登记进 ``T0-验收报告.md`` 的已知限制段。
    （本机 ``.env`` **未设** ``AGENT_MODE`` ⇒ 配置默认 ``off`` ⇒ 现有基线不受影响。）

    ⚠️ **带图 + 显式 ``react`` ⇒ 不拦**：``resolve_agent_mode`` 对带图**强制 ``off``**
    ⇒ 有效模式 = ``off`` ⇒ 走正常回答。这是「按解析后的有效模式判断」的**必然归属**，
    **不是**本函数开的例外（A12 的第四态就钉这条，防止有人按请求原文判）。
    """
    if resolve_agent_mode(payload, images) in AGENT_MODES:
        raise HTTPException(status_code=400, detail=NON_STREAM_AGENT_MODE_DETAIL)


@router.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest, db: AsyncSession = Depends(get_db)):
    """同步问答（契约 §7.1）。"""
    text, model_images, image_source = await prepare_request(db, payload)
    images = chat_images(payload)
    # 能力边界（第 0 批 · ②）：**解析后的有效模式**是 Agent ⇒ 可读的 400。
    # 位置有意紧贴 ``validate_request`` 之后：校验类的 400/404 优先（返回更贴题的提示），
    # 而它早于 ``build_turn_plan``（那里会写库 / 走缓存），所以拒绝是**零副作用**的。
    guard_non_stream_agent_mode(payload, model_images)
    # 回归修复：带图轮装配**视觉角色**（vision_config）；无图轮逐字不变。
    model = resolve_vision_model() if model_images else resolve_chat_model()
    # ❗ 辅助链路模型（推理等级回流）：**必须显式注入三处形参**，否则它们会自己去
    # `answer.build_chat_model()` 真装配 ⇒ 把用户配的思考参数带进内部编排
    #（A14 的「三条辅助链路请求体不含思考键」就是这么被踩破的）。
    # 三处 = clarify_model / compaction_model（本端点直接传）+ memory_model（persist_turn）。
    aux = resolve_aux_model()
    # clarify_model 必须一起传：否则澄清的追问生成会自己去真装配模型，
    # 绕过本端点的注入点（单测会真连上游）。
    # compaction_model 同理（条目 4.3.3）：长会话压缩的摘要调用也走这个注入点——
    # **不传就是整段跳过压缩**（回落全量历史，与接压缩之前逐字一致）。
    from app.usage.instrumentation import usage_scope
    with usage_scope(db=db):
        plan = await build_turn_plan(
            db, payload, text, model_images, clarify_model=aux, compaction_model=aux
        )
    plan.image_history_source = image_source
    if not history_image_available(plan, model):
        raise HTTPException(status_code=400, detail=HISTORY_IMAGE_HINT)
    # 条目 4.4：**同步链路也要铸**一次尝试 id（老基线只在流式那条铸 ⇒ 只抄那一条会让
    # 本端点的 ``assistant/attempt`` 永远缺席）。两条链路共用同一个入口函数。
    start_attempt(plan)

    generation_error = None
    try:
        with usage_scope(db=db, assistant_attempt_id=plan_attempt_id(plan)):
            raw_text = await _generate(plan, model)
    except (ProviderAuthError, ProviderTimeout) as exc:
        # 复用提示轮留档，再上抛原异常维持 401/504；不能跳过本轮提问与失败终态。
        generation_error = exc
        raw_text = AUTH_HINT if isinstance(exc, ProviderAuthError) else TIMEOUT_HINT
        plan.generation_status = "failed"
        plan.error_hint = raw_text
    display_text = f"{get_turn_prefix(plan, raw_text)}{raw_text}" if raw_text else raw_text

    # 回归修复（拍板单 B · 同步向流式看齐）：固定中文提示族（超时 / 鉴权 / 未配置 /
    # 视觉未配置）**不是一次完整回答**——落库语义必须与流式（``persist_user_turn(note=hint)``）
    # 同构：只留用户消息 + ``turn/end(kind=error)``，**不落** assistant 消息。否则下一轮
    # ``build_model_history`` 会把提示当助手历史**回灌给模型**，且两端点对同一请求的落库
    # 语义相反。与流式同序：先 ``log_attempt(aborted)`` 再落用户消息。
    hint = hint_for_error(raw_text, plan)
    if hint is not None:
        logger.warning("模型侧异常以固定提示收尾（同步链路）：%s", hint)
        await log_attempt(db, plan.chat.id, plan, attempt_mod.ATTEMPT_ABORTED)
        try:
            await persist_user_turn(db, plan, images=images, note=hint)
        except Exception as exc:  # noqa: BLE001 —— 与流式同款隔离：落库失败不改收尾语义
            logger.warning("异常轮的用户消息落库失败：%s: %s", exc.__class__.__name__, exc)
        # 事件（attempt / turn/*）写在 persist_user_turn 的提交之后，属于新事务——显式提交
        await db.commit()
        if generation_error is not None:
            raise generation_error
        return ChatResponse(
            chat_id=plan.chat.id,
            message=hint,
            refs=[],
            kind=plan.kind,
            reasoning="",
            thinking=None,
            skill_ids=plan.skill_ids,
            agent_trace=None,
            answer_source=plan.answer_source,
            generation_status="failed", finish_reason=plan.finish_reason,
        )

    if plan.generation_status == "stopped":
        await persist_stopped_turn(db, plan, display_text, images=images)
        return ChatResponse(
            chat_id=plan.chat.id, message=display_text, refs=[], kind="stopped",
            reasoning="", thinking=None, skill_ids=plan.skill_ids,
            agent_trace=None, answer_source=None,
            generation_status="stopped", finish_reason=plan.finish_reason,
        )
    citations = finalize_citations(plan, raw_text)
    await persist_turn(
        db,
        plan,
        display_text,
        images=images,
        citations=citations,
        # ⚠️ 记忆抽取是**辅助链路** ⇒ 用 aux（不带用户配置的思考参数）
        memory_model=aux,
        raw_answer=raw_text,
        from_cache=plan.cached,
    )
    # 兼容旧收口入口；真实用量由 provider send 独立记录，不随答案成功重复计数。
    if not plan.cached:
        await record_answer_usage(db, plan, raw_text)
    # 3.3 citation 器官：把「模型写了哪些 [n]」留痕（落库成功之后写，失败只留警告）
    await observe.log_citation_events(
        db,
        plan.chat.id,
        plan.kind,
        raw_text,
        plan.all_refs,
        turn=plan.turn,
        trace=plan.retrieval_id,
    )
    # 事件写在 persist_turn 的提交**之后**，属于新事务——这里显式提交（``get_db`` 不自动提交）
    await db.commit()

    # 阶段 5 · 条目 5.2：未命中且成功收口的这一轮**写回缓存**（fire-and-forget，
    # 失败只 warning+计数，绝不影响本轮）。命中轮不会走到这里（`maybe_store_cache` 首行即退）。
    maybe_store_cache(plan, raw_text, None)

    return ChatResponse(
        chat_id=plan.chat.id,
        message=display_text,
        refs=plan.refs,
        kind=plan.kind,
        # 阶段 1 不做思考捕获（阶段 4）：恒空 / None，与契约默认值一致
        reasoning="",
        thinking=None,
        skill_ids=plan.skill_ids,
        agent_trace=None,
        answer_source=plan.answer_source,
        generation_status=plan.generation_status, finish_reason=plan.finish_reason,
    )


class _ChatStreamingResponse(StreamingResponse):
    async def stream_response(self, send) -> None:
        try:
            await super().stream_response(send)
        finally:
            with CancelScope(shield=True):
                await self.body_iterator.aclose()


@router.post("/chat/stream")
async def chat_stream(payload: ChatRequest, db: AsyncSession = Depends(get_db)):
    """流式问答（SSE 最小形态：``meta → token* → done``，契约 §2）。"""
    text, model_images, image_source = await prepare_request(db, payload)
    images = chat_images(payload)
    # 回归修复：带图轮装配**视觉角色**（vision_config）；无图轮逐字不变。
    model = resolve_vision_model() if model_images else resolve_chat_model()
    # ❗ 辅助链路模型（推理等级回流）：与 `/chat` 逐字同口径（见那边的注释）——
    # 澄清 / 压缩 / Agent 决策 / 记忆抽取四处一律用 `aux`，用户配的思考参数只喂最终答案。
    aux = resolve_aux_model()
    # 停止登记必须在**返回响应之前**完成，而且必须早于**任何耗时步骤**
    # （登记在流结束的 finally 里注销；request_id 为空则不登记，这一轮停不了，其余照常）。
    #
    # ⚠️ **为什么排在 build_turn_plan 之前（阶段 4 · 条目 4.5.4 实测）**：`build_turn_plan`
    # 会写库（路由决策 / 压缩 / 消息落库），是本函数里最重的一段。原先登记排在它**之后**，
    # 于是一个「连上 SSE 就立刻点停止」的客户端（`tests/api/test_chat_stop.py` 的既有用例，
    # 流开始后 50ms 发 stop）会在登记完成前把 stop POST 出去 ⇒ `request_stop` 找不到登记、
    # 返回 False、信号**被静默丢弃** ⇒ 该停止的一轮跑成 `done`。这条竞态一直存在，只是
    # 「回答轮多一次真 DB 写」（4.5.4 的用量写入）把它从偶发放大成必现。
    stop_event = stop_registry.register(payload.request_id)
    try:
        # 计划在**流开始前**定好：这样 404/400 都是普通 HTTP 错误，
        # 不会先吐半截 SSE 再报错（契约明文要求）
        #
        # ❗ 辅助链路模型（推理等级回流）：澄清 / 压缩两处形参传 `aux`，并在下面把 `aux` 继续
        # 传给 `stream_tokens`（Agent 决策）与 `persist_turn`（记忆抽取）——
        # **四处装配点全在流开始之前定好**，流中途不会再去真装配一次。
        from app.usage.instrumentation import usage_scope
        with usage_scope(db=db):
            plan = await build_turn_plan(
                db, payload, text, model_images, clarify_model=aux, compaction_model=aux
            )
        plan.image_history_source = image_source
        if not history_image_available(plan, model):
            raise HTTPException(status_code=400, detail=HISTORY_IMAGE_HINT)
        if plan.needs_model and plan.agent_mode in AGENT_MODES:
            plan.materials_finalized = False
        # 条目 4.4：流式链路的尝试 id 在这里铸（与同步链路同一个入口）。铸在流开始**之前**，
        # 于是「停止了但一个增量都没收到」也能留下 ``stream/interrupted`` + ``assistant/attempt``。
        start_attempt(plan)
    except BaseException:
        # 计划阶段抛错（400/404/422/引擎异常）不进入流 ⇒ 登记必须就地撤销，
        # 否则这张表会留下一个永远没人消费的 ``request_id``（`/chat/stop` 会一直回 True）。
        stop_registry.unregister(payload.request_id, stop_event)
        raise

    display_chunks: list[str] = []
    reasoning_parts: list[str] = []
    agent_steps: list[dict] = []
    stopped_task: asyncio.Task | None = None
    terminal_started = False

    async def _save_stopped() -> None:
        plan.generation_status = "stopped"
        plan.finish_reason = "aborted"
        trace = plan.agent_trace if plan.agent_trace is not None else (
            {"mode": plan.agent_mode, "steps": list(agent_steps)} if agent_steps else None
        )
        try:
            await persist_stopped_turn(
                db, plan, "".join(display_chunks), reasoning="".join(reasoning_parts),
                images=images, agent_trace=trace,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("停止轮落库失败（仍以 stopped 收尾）：%s: %s", exc.__class__.__name__, exc)

    def _stopped_save() -> asyncio.Task:
        nonlocal stopped_task
        if stopped_task is None:
            stopped_task = asyncio.create_task(_save_stopped())
        return stopped_task

    async def _wait_closeout(task: asyncio.Task) -> None:
        cancelled = False
        with CancelScope(shield=True):
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _persist_user_turn_closeout(
        *,
        images: list[str] | None = None,
        note: str | None = None,
    ) -> None:
        task = asyncio.create_task(persist_user_turn(db, plan, images=images, note=note))
        await _wait_closeout(task)

    async def _events() -> AsyncIterator[bytes]:
        # 3.3 guard 器官：收尾（done / error / stopped）**恰好一次**——登记在 guard 上，帧由这里发
        guard = observe.StreamGuard()
        producer = _produce(guard)

        async def closeout() -> None:
            await producer.aclose()
            if stopped_task is not None:
                await stopped_task
            elif not guard.finished and not terminal_started and plan.generation_status != "failed":
                await _stopped_save()

        try:
            # meta：流开始
            # 回归修复：meta 帧的 yield 必须在 try 之内——客户端在 meta 帧后、下一帧前
            # 断开（「等首字时关页」的常见行为）时，Starlette 1.6 的 StreamingResponse 断开
            # **从不 aclose()** body_iterator，异步生成器悬挂在 meta yield 上等 GC；GC 终结器
            # 注入的 ``GeneratorExit`` 若落在 try 之外，下面 finally 的注销就不会执行
            # ⇒ 停止登记条目永久泄漏（active_count 只增不减，同一 request_id 的
            # /chat/stop 从此永远回 {"stopped": true}）。
            yield sse({"type": "meta", "chat_id": plan.chat.id, "skill_ids": plan.skill_ids})

            try:
                async for frame in producer:
                    yield frame
            except Exception as exc:  # noqa: BLE001 —— I5：内层没接住的意外，这里兜底收尾
                logger.warning("流式流程出现未捕获异常：%s: %s", exc.__class__.__name__, exc)
                if guard.error():
                    yield sse(
                        {
                            "type": "error",
                            "detail": f"生成失败，请稍后重试。（{exc.__class__.__name__}）",
                            **terminal_state(plan, "error"),
                        }
                    )

            if not guard.finished:
                # 任何分支忘了收尾都在这里补上——**绝不静默结束**（用户界面会一直转圈）
                guard.error()
                logger.warning("流式流程结束时没有收尾事件，已由 guard 补一条 error")
                yield sse({"type": "error", "detail": "回答流程异常结束，请重试。", **terminal_state(plan, "error")})
        finally:
            # ponytail: 请求退出前等同一份收口完成；取消继续上抛，避免 session 先关或重复留档。
            finalizer = asyncio.create_task(closeout())
            try:
                await _wait_closeout(finalizer)
            finally:
                stop_registry.unregister(payload.request_id, stop_event)

    async def _produce(guard) -> AsyncIterator[bytes]:
        """产 ``token`` / ``reasoning`` / ``agent`` / ``stopped`` / ``done`` / ``error`` 帧。

        每个收尾点都**先向 guard 登记**再发帧；停止信号的检查方式见下面的赛跑注释。
        """
        nonlocal terminal_started
        chunks: list[str] = []
        prefix = ""
        prefix_sent = False
        stopped = False
        source = None
        getter: asyncio.Task | None = None
        stop_wait: asyncio.Task | None = None
        try:
            source = stream_tokens(plan, model, db, aux_model=aux).__aiter__()
            with usage_scope(db=db, assistant_attempt_id=plan_attempt_id(plan)):
                getter = asyncio.ensure_future(source.__anext__())
            # 条目 3.4：**取下一个增量**与**停止信号**赛跑——厂商静默期 / 检索期也能即时停，
            # 而不是等下一个增量到达（契约 §2 行为细节）。
            stop_wait = (
                asyncio.ensure_future(stop_event.wait()) if stop_event is not None else None
            )
            exhausted = False
            while not exhausted:
                pending = {getter} if stop_wait is None else {getter, stop_wait}
                done_set, _pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                # 先收增量再判停止：done 与停止同时就绪时按**正常完成**处理，
                # 不把已经完整的回答误标成「已停止」。
                if getter in done_set:
                    try:
                        kind, payload = getter.result()
                    except StopAsyncIteration:
                        exhausted = True
                    except Exception as exc:  # noqa: BLE001 —— 生成中途出错也要收尾
                        plan.generation_status = "failed"
                        plan.error_hint = f"生成失败，请稍后重试。（{exc.__class__.__name__}）"
                        logger.warning(
                            "流式生成失败（将以 error 收尾）：%s: %s", exc.__class__.__name__, exc
                        )
                        # 条目 4.4：生成中途出错 ⇒ 这次尝试以 ``aborted`` 收尾
                        # （不产 assistant 消息事件的终态，与老基线同判据）。
                        await log_attempt(
                            db, plan.chat.id, plan, attempt_mod.ATTEMPT_ABORTED
                        )
                        await _persist_user_turn_closeout(images=images)
                        if guard.error():
                            yield sse(
                                {
                                    "type": "error",
                                    "detail": f"生成失败，请稍后重试。（{exc.__class__.__name__}）",
                                    **terminal_state(plan, "error"),
                                }
                            )
                        return
                    else:
                        if kind == "reasoning":
                            reasoning_parts.append(payload)
                            yield sse({"type": "reasoning", "text": payload})
                        elif kind == "agent":
                            # 条目 4.1：Agent 步骤即时下发（前端边跑边显示），并攒一份给
                            # 「停止轮」的 agent_trace（老基线同款：停止轮用已下发的部分步骤）。
                            agent_steps.append(payload)
                            yield sse({"type": "agent", "step": payload})
                        elif kind == "error":
                            plan.generation_status = "failed"
                            plan.error_hint = str(payload)
                            # 条目 153（P0）：上游失败（错误事件 / stop_reason="error"）
                            # ⇒ 与下面的 hint 收尾同形：固定中文 detail、审计记 aborted、
                            # 只落用户轮（**不落 assistant 消息**：空回答与半截回答都不当
                            # 成功留档）、以 error 帧收尾。⛔ 绝不下发 done。
                            logger.warning("本轮上游失败以 error 收尾：%s", payload)
                            await log_attempt(
                                db, plan.chat.id, plan, attempt_mod.ATTEMPT_ABORTED
                            )
                            try:
                                await _persist_user_turn_closeout(images=images, note=str(payload))
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(
                                    "失败轮的用户消息落库失败：%s: %s",
                                    exc.__class__.__name__,
                                    exc,
                                )
                            if guard.error():
                                yield sse({"type": "error", "detail": str(payload), **terminal_state(plan, "error")})
                            return
                        else:
                            chunks.append(payload)
                            if payload.strip() and not prefix_sent:
                                prefix = get_turn_prefix(plan, payload)
                                prefix_sent = True
                                payload = f"{prefix}{payload}"
                            display_chunks.append(payload)
                            yield sse({"type": "token", "text": payload})
                    if not exhausted:
                        with usage_scope(db=db, assistant_attempt_id=plan_attempt_id(plan)):
                            getter = asyncio.ensure_future(source.__anext__())
                if stop_wait is not None and stop_wait in done_set:
                    if exhausted:
                        # 回归修复：停止信号与流耗尽**同批**就绪（用户点停止的时刻落在
                        # 「最后一个增量已被消费」与「wait 观察到 StopAsyncIteration」之间）
                        # ⇒ 流已自然到头，按**正常完成**收尾——兑现上面注释的承诺。
                        # 不这样判，完整正文会以 kind="stopped"、refs=[]、answer_source=NULL
                        # 落库，且用量 / 记忆 / 缓存全跳过、clarify_rounds 不清零。
                        # 未耗尽（流还挂着增量未取）⇒ 维持既有 stopped 语义（中途停止）。
                        break
                    stopped = True
                    break
        finally:
            async def cleanup() -> None:
                # 四种出口都回收在途任务，再关闭厂商流与 Agent 审计。
                for task in (getter, stop_wait):
                    if task is not None and not task.done():
                        task.cancel()
                        try:
                            await task
                        except BaseException:  # noqa: BLE001 —— 取消是预期路径
                            pass
                # 条目 4.3 / 回归修复：在 session 关闭前显式 aclose，不能等 GC。
                # 尚未启动的 getter 被取消时，不会自行关闭内层 Agent 审计生成器。
                if source is not None:
                    # 兼容只实现 __anext__ 的迭代器。
                    close = getattr(source, "aclose", None)
                    if close is not None:
                        try:
                            await close()
                        except BaseException:  # noqa: BLE001 —— 关闭失败不影响收尾
                            pass
            await _wait_closeout(asyncio.create_task(cleanup()))

        reasoning_text = "".join(reasoning_parts)
        raw_text = "".join(chunks)
        display_text = "".join(display_chunks)

        if stopped or plan.generation_status == "stopped":
            # 停止轮：已生成的部分**留档**（kind=stopped、不挂引用）；
            # **一个增量都没收到**就只落用户消息——「极早期停止零字不落消息」（契约 §2）。
            # Agent 轮的停止同理：轨迹只带**已经下发给前端的那几步**（老基线同款）。
            # ⚠️ `events_incomplete`（条目 4.3 · `回归修复`）：优先用 **runner 收口好的那份**
            # （`plan.agent_trace` 带 mode/steps/events_incomplete，按本轮归属算的），
            # 只有在 runner 没跑过（`plan.agent_trace is None`）时才回落到这里的 steps。
            # 停止轮**仍然不是 failed**——这个字段说的是「本轮日志完不完整」，不是成败。
            await asyncio.shield(_stopped_save())
            if guard.stopped():
                yield sse({"type": "stopped", "chat_id": plan.chat.id, **terminal_state(plan, "stopped")})
            return

        hint = hint_for_error(raw_text, plan)
        if hint is not None:
            terminal_started = True
            # 契约 §2：超时 / 鉴权 / 未配置的 detail 是**固定中文提示**（不把厂商原文抛给用户）；
            # 这一轮不当完整回答落库，但**用户消息要留下**（否则提问会从历史里消失）。
            logger.warning("模型侧异常以 error 收尾：%s", hint)
            # 条目 4.4：超时 / 鉴权 / 未配置三类都属「这次尝试出错了」⇒ ``aborted``
            await log_attempt(db, plan.chat.id, plan, attempt_mod.ATTEMPT_ABORTED)
            try:
                await _persist_user_turn_closeout(images=images, note=hint)
            except Exception as exc:  # noqa: BLE001
                logger.warning("异常轮的用户消息落库失败：%s: %s", exc.__class__.__name__, exc)
            if guard.error():
                yield sse({"type": "error", "detail": hint, **terminal_state(plan, "error")})
            return

        terminal_started = True
        try:
            citations = finalize_citations(plan, raw_text)
            await persist_turn(
                db,
                plan,
                display_text,
                images=images,
                citations=citations,
                reasoning=reasoning_text or None,
                agent_trace=plan.agent_trace,
                # ⚠️ 记忆抽取是**辅助链路** ⇒ 用 aux（与同步 `/chat` 同口径）
                memory_model=aux,
                raw_answer=raw_text,
                from_cache=plan.cached,
            )
        except Exception as exc:  # noqa: BLE001 —— 落库失败也要收尾，不让前端挂死
            logger.warning("落库失败（以 error 收尾）：%s: %s", exc.__class__.__name__, exc)
            # 条目 4.4：回答已生成但没落库 ⇒ 这次尝试 ``aborted``（没有持久 assistant 消息）
            await log_attempt(db, plan.chat.id, plan, attempt_mod.ATTEMPT_ABORTED)
            if guard.error():
                yield sse({"type": "error", "detail": "回答已生成但保存失败，请重试。", **terminal_state(plan, "error")})
            return

        # 阶段 4 · 条目 4.5.4：回答成功收口记一条用量（只记成功——异常轮 / 停止轮在上面就
        # return 了，压根到不了这一行）。同样排在下面那次 commit 之前，搭既有提交落库。
        # ⚠️ 缓存命中轮**跳过**（阶段 5 · 条目 5.2 的 §0.0 第 5 行）：命中轮没调模型，
        # 记了就是把没发生的调用算成钱。
        if not plan.cached:
            await record_answer_usage(db, plan, raw_text)

        # 3.3 citation 器官：与同步路径同一口径（落库成功之后写）
        await observe.log_citation_events(
            db,
            plan.chat.id,
            plan.kind,
            raw_text,
            plan.all_refs,
            turn=plan.turn,
            trace=plan.retrieval_id,
        )
        await db.commit()  # 同上：事件属于 persist_turn 之后的新事务

        # 阶段 5 · 条目 5.2：未命中且成功收口的这一轮**写回缓存**（fire-and-forget，
        # 失败只 warning+计数，绝不影响本轮）。放在 commit 之后 ⇒ 缓存里存的一定是
        # **真的落了库**的那一轮；命中轮不会走到这里（`maybe_store_cache` 首行即退）。
        maybe_store_cache(plan, raw_text, reasoning_text or None)

        # done：契约十字段（type + 九个业务字段；`recovered` 自 02 v1.0.12 / `回归修复`
        # 拍板⑨「补」起加入）；reasoning 为**思考全文**（3.4 起真的填）；
        # agent_trace 是阶段 4 条目 4.1 接上的（此前恒 None；非 Agent 轮**仍恒 None**——
        # 02 §7.1 明文：非流式 /chat 的 agent_trace 恒为 None，只有流式接口才落它）。
        if guard.done():
            # `recovered` = 该会话**是否存在恢复痕迹**（现算，与 `MessageOut.recovered` 同源
            # `recover.recovered_message_ids`）。前端解冻（2026-09-24）后由 App.tsx 消费：
            # true ⇒ 拉取会话消息，让「上次崩溃的恢复标记」免刷新可见。⚠️ 只读现算，
            # **不在这里跑 ensure_balanced**（补平仍归 GET /chats/{id} 的读路径，4.4 口径不变）。
            try:
                chat_recovered = bool(await recover_mod.recovered_message_ids(db, plan.chat.id))
            except Exception as exc:  # noqa: BLE001 —— 观测项不炸收尾（I5 同型）
                # 回归修复：这里**不许静默** —— 读数失败与「真的没有恢复痕迹」逐字同形
                # （都是 recovered=False），一条 warning 是二者唯一可分之处。I5 不变：帧照发、
                # 值照旧；字段仍是 bool（02 v1.0.12 的 done 十字段口径与类型语义都不动）。
                logger.warning(
                    "恢复痕迹读数失败（按无痕迹收尾，recovered=False）：%s: %s",
                    exc.__class__.__name__,
                    exc,
                )
                chat_recovered = False
            yield sse(
                {
                    "type": "done",
                    "chat_id": plan.chat.id,
                    "refs": plan.refs,
                    "kind": plan.kind,
                    "reasoning": reasoning_text,
                    "thinking": None,
                    "skill_ids": plan.skill_ids,
                    "agent_trace": plan.agent_trace,
                    "answer_source": plan.answer_source,
                    "recovered": chat_recovered,
                    **terminal_state(plan, "done"),
                }
            )

    return _ChatStreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/chat/stop")
async def chat_stop(payload: ChatStopIn):
    """请求停止一次进行中的流式生成（契约 §1.1 + 条目 3.4 的停止机制）。

    只负责**置位**停止信号；真正的收尾由流内检查点完成（``stopped`` 帧 + 已生成部分留档）。
    ``request_id`` 不在册（流已结束 / 从未存在）→ ``{"stopped": false}``，**不报错**：
    用户点停止的瞬间回答恰好收尾是正常竞态（幂等友好）。
    """
    return {"stopped": stop_registry.request_stop(payload.request_id)}


__all__ = [
    "AGENT_MODES",
    "MAX_UPLOAD_BYTES",
    "NON_STREAM_AGENT_MODE_DETAIL",
    "TIME_PREFIX",
    "UNKNOWN_SKILL_ID_PREFIX",
    "UNKNOWN_SKILL_ID_SUFFIX",
    "VALID_AGENT_MODES",
    "answer_context",
    "attempt_provider_model",
    "build_turn_plan",
    "chat",
    "chat_images",
    "chat_stop",
    "chat_stream",
    "finalize_citations",
    "guard_non_stream_agent_mode",
    "hint_for_error",
    "log_attempt",
    "parse_agent_mode",
    "persist_stopped_turn",
    "persist_turn",
    "persist_user_turn",
    "plan_attempt_id",
    "require_chat_ready",
    "resolve_agent_mode",
    "resolve_chat_model",
    "resolve_vision_model",
    "retrieve_materials",
    "router",
    "run_agent_turn",
    "start_attempt",
    "stream_tokens",
    "turn_key_of",
    "validate_request",
    "wants_web",
]
