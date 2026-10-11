"""跨会话记忆（阶段 5 · 条目 5.1）：**抽取 → 落库 → 注入**三段闭环。

## 一句话职责

``MEMORY_ENABLE`` 打开时，把每一轮**完整成功**的问答交给模型抽出 ≤3 条稳定事实、
按范围、事实槽位及版本落进 ``memories`` 表；后续轮次把相关且有效的事实渲染成一段
附加提示词挂进**系统提示**（不进 ``history``）。**默认关 = 逐字零影响**。

## 三条接口约束

1. **对外只有 3 个函数**：:func:`remember` / :func:`recall` / :func:`render_block`；
   其余一律 ``_`` 前缀。上游（``api`` / ``generation``）**不得摸表**
   ——换检索方式（将来走向量召回）只改这一个文件。
2. **注入进系统提示的附加段，**不**进 ``history``**。三条实测理由（见证据）：
   ``history`` 形状冻结（只有 ``role``/``content`` 两键）；塞进去会顶偏轮次号
   ``turn = len([user]) + 1``（进而污染事件载荷与分桶）；还会伪装成「最近的对话」
   喂给澄清判定与 Agent 的 ``_history_digest``，并挪动压缩保留窗。
3. **关态零行为**（§2.1 三条判据的可操作版）：三个入口**各自首行**调
   :func:`_enabled` 早退——关态下不查库、不调模型、渲染恒空串。
   早退点收敛在**一处**（``compaction.py::compact_history`` 的开关判断在函数体中段，
   那是它当时的形状，本模块不照抄位置）。

## 依赖方向与模型注入

``app/session/*`` 与 ``generation`` 之间有分层禁令（``session`` 层零 import
``vector``/``generation``/``retrieval``），故本模块**不 import 任何模型装配**
——LLM 走**参数注入**（``model`` 形参），装配点只有 ``api`` 侧一处
（口径与 ``compaction_model`` 的「单一注入参数 + 单一装配点」同型）。

## 抽取侧口径（写路径）

- 只抽**完整收口**的轮（以落库 assistant 消息的 ``kind`` 为准，``stopped``/``error``
  一律不抽——回答不完整，抽出来是半句）；
- **fire-and-forget**：自己的独立会话、吞异常、失败只 warning + 计数，
  本轮回答**绝不受影响**（I5）；
- **每轮最多 1 次**非流式调用、**不重试**（真机配额有限）；``reasoning`` 关掉，
  输出预算按「正文预算 + 思考余量」给足（``compaction`` 的实测教训）；
- 失败**分级计数**：① 调用失败 ② 调用成功但 JSON 解析失败 ③ 解析成功但 ``facts`` 为空
  （③ 是**正常**：这一轮没什么可记的）；三者都不影响本轮回答；
- **任务之间串行化**（模块内 asyncio 锁）：避免同一轮并发写同一条、也让测试有确定的等待点。
  :func:`drain_memory_tasks` 是**唯一等待点**（``usage.recorder.drain_pending`` 只管
  usage 任务、**不含**记忆任务，别拿它等）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.lexical import tokenize
from app.db.session import SessionLocal, new_independent_session
from app.models import Chat, Memory, Message

logger = logging.getLogger(__name__)

# --- 常量（模块内，**不进 config**）--------------------------------------
#
# 为什么这四条留在模块里而不是 .env 键：它们是**实现细节**（一次抽几条、单条多长），
# 不是用户该调的旋钮。暴露成键只会提高心智负担——注入端的三个键
# （``MEMORY_ENABLE`` / ``MEMORY_INJECT_MAX_CHARS`` / ``MEMORY_TOP_K``）才是。
# 口径裁决见开工指令包 §0.0 第 2 行。

#: 单轮抽取条数上限
EXTRACT_MAX_FACTS = 3
#: 单条记忆正文长度上限（超长直接丢弃，不截断——半句话的记忆没有价值）
EXTRACT_MAX_CHARS = 200
#: 抽取输入（提问 + 回答正文）总长上限，超出**从尾部截断**
EXTRACT_INPUT_MAX_CHARS = 4000
#: 抽取调用的输出预算（正文 800 token，另加思考余量）
EXTRACT_MAX_TOKENS = 800
#: 抽取输出的**思考余量**（compaction 的实测教训：推理型模型会把预算全花在思考上、
#: 正文为空 ⇒ 抽取永远失败。这条不要省）
EXTRACT_REASONING_HEADROOM = 1024
#: 抽取调用的采样温度（抽取要稳定，低温）
EXTRACT_TEMPERATURE = 0.0

#: 全局偏好范围；知识按实际库、完整筛选或会话隔离。
SCOPE_GLOBAL = "global"
#: ``kind`` 二值（提示词契约与落库校验共用同一份）
KINDS: tuple[str, ...] = ("knowledge", "interaction")

#: 注入段抬头（固定中文措辞）。正文段与它之间用空行分隔，全文预算含它。
MEMORY_HEADER = "【跨会话记忆（系统自动记录的长期事实，仅供补充参考）】"
#: 单条前缀（定长，计入预算）
MEMORY_BULLET = "- "
_CANDIDATE_LIMIT = 256
_CORRECTION = re.compile(r"纠正|更正|改为|改成|不再|其实|而不是|\b(?:instead|correction)\b", re.I)
_HARD_CONSTRAINT = re.compile(r"只(?:能|要|用)|必须|不要|不(?:使用|用)|禁止|仅限|\b(?:only|must|never|not|don't)\b", re.I)
_NUMBER_UNIT = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(元|年)")
# ponytail: bounded label aliases, not translation; broader paraphrases need a
# separately evaluated semantic retriever, not unfiltered memory injection.
_BILINGUAL_LABELS = (
    ("project", "项目"), ("code", "codename", "代号"),
    ("name", "called", "称呼", "名称"), ("preference", "preferred", "prefer", "偏好"),
    ("language", "语言"), ("response", "reply", "respond", "回复"),
    ("style", "风格"), ("length", "长度"),
)


def _knowledge_scope(filters: dict | None, chat_id: int | None) -> str:
    values = {key: value for key, value in (filters or {}).items() if value is not None and value != []}
    ids = values.get("kb_ids") or ([values["kb_id"]] if values.get("kb_id") is not None else [])
    if ids and set(values) <= {"kb_id", "kb_ids"} and len(set(ids)) == 1:
        return f"kb:{int(ids[0])}"
    if values:
        raw = json.dumps(values, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return "scope:" + hashlib.sha256(raw.encode()).hexdigest()[:24]
    return f"chat:{chat_id}" if chat_id is not None else SCOPE_GLOBAL


def _utc(value: datetime | None) -> datetime | None:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value and value.tzinfo else value

#: 每轮最多 1 次抽取调用、不重试；同一时刻只允许一个抽取任务在跑（串行化）
_EXTRACT_LOCK = asyncio.Lock()
#: 在途抽取任务的强引用（asyncio 只留弱引用，不攥着可能中途被 GC）
_PENDING: set[asyncio.Task] = set()

# --- 失败分级计数（进程内；面板化出口 = ``GET /api/agent/audit`` 的 ``counters.memory``）----
#
# 为什么不进 ``/api/health``：那是**冻结契约面**，且 4.4 已定「取消/失败类计数不进 health」。
# 这里只做进程内计数 + 日志；失败面板已由条目 5.5 收口（键名与 :func:`failure_counts` 逐字一致）。
_COUNTS: dict[str, int] = {
    "call_failed": 0,
    "parse_failed": 0,
    "empty": 0,
    "stored": 0,
    # 回归修复（#12，2026-09-27 拍板⑩）：记忆**落库**失败原先与「零写入」在计数上不可区分
    #（except 里只 warning + return 0）⇒ 补第五键。5.5 的 A9 键集断言已同步升为「既有键逐字 + 只增超集」。
    "store_failed": 0,
}


def failure_counts() -> dict[str, int]:
    """读失败分级与写入条数的**进程内计数**（诊断用；**不是**对外接口）。

    键：``call_failed``（调用失败）/ ``parse_failed``（JSON 解析失败）/
    ``empty``（解析成功但没有可记的事实——**正常**）/ ``stored``（真的写进去的条数）/
    ``store_failed``（落库失败，#12 接线）。
    """
    return dict(_COUNTS)


def reset_failure_counts() -> None:
    """清零全部计数（测试与诊断用）。"""
    for key in _COUNTS:
        _COUNTS[key] = 0


def _enabled() -> bool:
    """记忆总开关的**唯一读取点**（三个入口各自首行早退）。

    读 ``settings`` 的键不存在时按 **False**（未登记的键 = 没开）。
    """
    return bool(getattr(settings, "MEMORY_ENABLE", False))


def _int_setting(name: str, default: int) -> int:
    """读一个 int 配置键，**不吞 falsy 值**（0 是合法取值，不是「没配」）。

    与 ``compaction._int_setting`` 同一口径（那边踩过 ``0 or 8000`` 的坑），
    本模块不为一个三行的辅助去反向 import 上层/同层私有函数。
    """
    raw = getattr(settings, name, default)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _normalize(text: str) -> str:
    """去重用的规范化：**折叠所有空白序列为一个空格 + 去首尾**。

    为什么要折叠而不是直接用原文：模型对同一件事的复述常在换行/空格上抖动，
    「同一句话连续两轮」也会因消息拼接带上不同空白 ⇒ 不折叠就去不掉重。
    """
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _text_hash(text: str) -> str:
    """规范化文本的 sha256（64 位十六进制）。"""
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()


def _relevance_terms(tokens: list[str] | set[str]) -> set[str]:
    """Expand only the fixed bilingual labels used by memory questions."""
    terms = set(tokens)
    for labels in _BILINGUAL_LABELS:
        if terms.intersection(labels):
            terms.update(labels)
    return terms


# ==========================================================================
# 公开接口 1/3：写（抽取任务的落库端 + 直接调用端共用）
# ==========================================================================


async def remember(
    db: AsyncSession,
    *,
    text: str,
    kind: str = "knowledge",
    source_chat_id: int | None = None,
    source_message_id: int | None = None,
    scope: str = SCOPE_GLOBAL,
    fact_key: str | None = None,
    supersedes_id: int | None = None,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    source_text: str | None = None,
) -> int | None:
    """落一条记忆（**已规范化 + 去重**）。返回新行 id；重复 / 空串 / 坏 kind ⇒ ``None``。

    - 关态：首行早退，**不查不写**（A11 的「关态零 SELECT / 零 INSERT」就断在这里）；
    - 去重：先按 ``(scope, text_hash)`` 查；查不到再插，``IntegrityError`` 也**静默跳过**
      （并发兜底）——两条路径都不报警、不计失败（「重复」不是失败）；
    - ``db`` 是调用方给的会话（抽取任务给的是**它自己的独立会话**）；
      **提交归调用方**（本函数只 ``flush``），与 ``repository`` 的行级读写同一口径。
    """
    if not _enabled():
        return None
    body = _normalize(text)
    key = _normalize(fact_key) or None
    valid_from, valid_until = _utc(valid_from), _utc(valid_until)
    if (
        not body or kind not in KINDS or not scope or len(scope) > 32
        or (key and len(key) > 200)
        or (valid_from and valid_until and valid_until <= valid_from)
        or (supersedes_id is not None and not key)
    ):
        return None
    digest = _text_hash(body)
    try:
        existing = (
            await db.execute(
                select(Memory.id).where(
                    Memory.scope == scope, Memory.text_hash == digest,
                    Memory.status.in_(("active", "conflict")),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None and supersedes_id is None:
            return None
        previous = []
        if key:
            previous = list((await db.execute(
                select(Memory).where(Memory.scope == scope, Memory.fact_key == key)
                .order_by(Memory.version.desc()).with_for_update()
            )).scalars().all())
        latest = previous[0] if previous else None
        if supersedes_id is not None and (
            latest is None or latest.id != supersedes_id
            or latest.status not in ("active", "conflict")
        ):
            return None
        source = await db.get(Message, source_message_id) if source_message_id is not None else None
        if source_message_id is not None:
            source_chat = await db.get(Chat, source_chat_id) if source_chat_id is not None else None
            if (
                source is None or source.role != "user" or source_chat_id != source.chat_id
                or source_chat is None or source_chat.deleted_at is not None
                or (source_text is not None and _text_hash(source_text) != _text_hash(source.content))
            ):
                return None
        version = latest.version + 1 if latest else 1
        peers = [item for item in previous if item.status in ("active", "conflict")]
        row = Memory(
            scope=scope,
            kind=kind,
            text=body,
            text_hash=digest,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            fact_key=key,
            version=version,
            status="conflict" if peers and supersedes_id is None else "active",
            valid_from=valid_from,
            valid_until=valid_until,
            supersedes_id=supersedes_id,
            source_hash=_text_hash(source.content) if source is not None else None,
        )
        # SAVEPOINT 隔一下：唯一键冲突只回滚这一条，不污染调用方的事务
        async with db.begin_nested():
            for peer in peers:
                peer.status = "superseded" if supersedes_id is not None else "conflict"
                if supersedes_id is not None:
                    peer.valid_until = valid_from or datetime.now(timezone.utc).replace(tzinfo=None)
            db.add(row)
            await db.flush()
    except IntegrityError:
        # 并发下另一个任务刚插了同一条 —— 「重复」不是失败，静默跳过
        return None
    return int(row.id)


# ==========================================================================
# 公开接口 2/3：读（召回）
# ==========================================================================


async def recall(
    db: AsyncSession,
    *,
    limit: int | None = None,
    scope: str = SCOPE_GLOBAL,
    question: str | None = None,
    chat_id: int | None = None,
    filters: dict | None = None,
) -> list[Memory]:
    """先排冲突、过期和失效来源，再按词项相关度取 Top K；关闭态零 SQL。

    不传 question 的内部管理/兼容调用仍按时间倒序；回答入口必须传原问与范围。
    """
    if not _enabled():
        return []
    top_k = _int_setting("MEMORY_TOP_K", 5) if limit is None else int(limit)
    if top_k <= 0:
        return []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stmt = select(Memory).where(
        Memory.status == "active",
        or_(Memory.valid_from.is_(None), Memory.valid_from <= now),
        or_(Memory.valid_until.is_(None), Memory.valid_until > now),
    )
    if question is None:
        return list((await db.execute(
            stmt.where(Memory.scope == scope)
            .order_by(Memory.created_at.desc(), Memory.id.desc()).limit(top_k)
        )).scalars().all())
    if _HARD_CONSTRAINT.search(question) or _CORRECTION.search(question):
        return []
    terms = _relevance_terms([term for term in tokenize(question) if len(term) >= 2])
    if not terms:
        return []
    scoped = _knowledge_scope(filters, chat_id)
    stmt = stmt.where(or_(
        (Memory.scope == SCOPE_GLOBAL) & (Memory.kind == "interaction"),
        Memory.scope == scoped if scoped != SCOPE_GLOBAL else False,
    ))
    # ponytail: 最多 256 条相关候选，SQL 先相关排序；同词大量重复超过上限时升级到既有词项倒排。
    score = sum(case((func.lower(Memory.text).contains(term, autoescape=True), 1), else_=0) for term in sorted(terms))
    stmt = stmt.where(score > 0).order_by(score.desc(), Memory.created_at.desc(), Memory.id.desc())
    rows = list((await db.execute(stmt.limit(_CANDIDATE_LIMIT))).scalars().all())
    ids = {row.source_message_id for row in rows if row.source_message_id is not None}
    sources = {}
    if ids:
        sources = {
            message.id: (message, chat) for message, chat in
            (await db.execute(
                select(Message, Chat).join(Chat, Chat.id == Message.chat_id)
                .where(Message.id.in_(ids), Chat.deleted_at.is_(None))
            )).all()
        }
    ranked = []
    # ponytail: 只比较已复现的金额/年份单位；其他事实槽冲突仍需明确槽位，不能泛化为数字全禁。
    current_values = {}
    for value, unit in _NUMBER_UNIT.findall(question):
        current_values.setdefault(unit, set()).add(Decimal(value))
    for row in rows:
        if row.source_message_id is not None:
            source = sources.get(row.source_message_id)
            if (
                source is None or source[0].role != "user" or row.source_chat_id != source[0].chat_id
                or not row.source_hash or row.source_hash != _text_hash(source[0].content)
            ):
                continue
        elif row.source_chat_id is not None:
            continue
        if any(
            unit in current_values and Decimal(value) not in current_values[unit]
            for value, unit in _NUMBER_UNIT.findall(row.text)
        ):
            continue
        overlap = terms & _relevance_terms(tokenize(row.text))
        if overlap:
            ranked.append((len(overlap), row.id, row))
    return [row for _, _, row in sorted(ranked, key=lambda item: item[:2], reverse=True)[:top_k]]


# ==========================================================================
# 公开接口 3/3：渲染
# ==========================================================================


def render_block(memories: list[Memory] | None) -> str:
    """把记忆渲染成系统提示的**附加段**（固定中文措辞 + 固定分隔）。返回 ``""`` 表示不注入。

    - 关态 / 空列表 ⇒ ``""``（调用方据此完全不拼提示词——「关 = 零变化」的判据之一）；
    - **预算规则（唯一口径）**：按 ``created_at`` 倒序逐条累加，**下一条会超预算就停**
      ——不截断单条、不摘要、不变换排序。预算 = ``MEMORY_INJECT_MAX_CHARS``（默认 800）；
    - **条数上限 = ``MEMORY_TOP_K``（默认 5），由本函数自己夹**：``recall`` 已经夹过一次，
      但这里**再夹一次**是有意的——800 字预算能装下 7 条 100 字的短事实，
      只靠预算就装不出「≤5 条」这条保证（实测：**不夹就是 7 条**）。
      条数上限是**本条的要求**，就不能只依赖调用方传对了 limit。
    - 措辞固定是为了「注入内容对模型可解释、对读日志的人可辨认」：模型知道这是本机
      长期记下的事实、且只是**补充**参考。
    """
    if not _enabled():
        return ""
    budget = _int_setting("MEMORY_INJECT_MAX_CHARS", 800)
    top_k = _int_setting("MEMORY_TOP_K", 5)
    if budget <= 0 or top_k <= 0:
        return ""
    lines: list[str] = []
    used = len(MEMORY_HEADER)
    for row in memories or []:
        if len(lines) >= top_k:
            break
        body = _normalize(getattr(row, "text", "") or "")
        if not body:
            continue
        line = f"{MEMORY_BULLET}{body}"
        cost = len(line) + 1  # 包含抬头后的首个换行
        if used + cost > budget:
            break
        lines.append(line)
        used += cost
    if not lines:
        return ""
    return MEMORY_HEADER + "\n" + "\n".join(lines)


# ==========================================================================
# 抽取（写路径）—— :func:`remember_turn` / :func:`schedule_extract` 是
# 「fire-and-forget 调度端」的内部入口，由 ``chat_routes.persist_turn`` 调；
# 不算入上面那三个对外接口（上游业务代码不该自己排抽取）。
# ==========================================================================

EXTRACT_SYSTEM = (
    "你是知澜的记忆整理助手。阅读一轮用户提问与助手回答，判断其中有没有**值得长期记住**的"
    "稳定事实。只输出一个 JSON 对象，不要输出任何其它文字。"
)

EXTRACT_INSTRUCTION = (
    "请从下面这一轮对话里，抽取值得长期记住的稳定事实。\n"
    "\n"
    "值得记的：用户明确陈述的偏好、身份、目标、长期设定；对话中确立的、"
    "以后还会用到的事实与结论。\n"
    "不要记的：一次性的琐碎细节、时效性内容（今天天气、此刻时间）、"
    "助手的一般性知识讲解、重复的寒暄、与用户无关的内容。只记用户明确陈述的事实，"
    "助手回答不能作为事实来源。\n"
    "\n"
    "只输出一个 JSON 对象，格式严格如下（不要输出别的文字、不要加代码围栏）：\n"
    '{"facts": [{"text": "一句话陈述的事实（中文，小于 200 字）", "kind": "knowledge",'
    ' "key": "稳定事实槽位，例如 用户/城市", "supersedes_id": null}]}\n'
    "同一对象同一属性复用已登记 key；只有用户本轮明确纠正时，supersedes_id 填"
    "已登记该槽位的最新 id。没有明确纠正时保持 null，不擅自决定谁对谁错。"
    "可以提供 ISO 时间 valid_from/valid_until；不确定就省略。\n"
    "\n"
    "kind 只能取两个值：knowledge = 关于世界的稳定事实（项目、技术、领域结论等）；"
    "interaction = 关于用户本人的事实（偏好、身份、习惯、目标等）。\n"
    "\n"
    f"最多输出 {EXTRACT_MAX_FACTS} 条；没有任何值得记的内容时输出 "
    '{"facts": []}。\n'
    "\n"
    "本轮对话：\n{conversation}"
)


def extraction_input(question: str, answer: str) -> str:
    """抽取的输入材料：**只喂本轮的 user 提问 + assistant 正文**。

    **不喂** refs / 材料原文 / agent_trace：那些是「这一轮检索到了什么」，
    不是「用户是谁、在意什么」——混进去只会让抽取跑偏，并且把检索内容抄成「记忆」
    （下次它就会以「长期事实」的名义出现，比没有记忆更糟）。
    总长夹 ``EXTRACT_INPUT_MAX_CHARS``（超出**从尾部截断**——提问在头部，保住它）。
    """
    body = f"用户：{question}\n\n助手：{answer}"
    limit = EXTRACT_INPUT_MAX_CHARS
    return body if len(body) <= limit else body[:limit]


def _strip_fence(text: str) -> str:
    """去掉 ```json ... ``` 围栏（模型经常多此一举）。"""
    raw = str(text or "").strip()
    if not raw.startswith("```"):
        return raw
    lines = raw.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _locate_object(text: str) -> str:
    """括号定位：取**第一个** ``{`` 到**匹配它的** ``}`` 之间的子串（三级解析的第三级）。"""
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


def parse_facts(raw: str) -> list[dict]:
    """三级解析兜底：去围栏 → ``json.loads(strict=False)`` → 括号定位。**解析不了返回空列表**。

    空列表在调用方那里**不算失败**（那可能是「这一轮本来就没东西可记」）——
    区分靠 :func:`_extract_facts` 的两次调用：第一次真失败才计数、第二次空才计空。
    """
    text = _strip_fence(raw)
    if not text:
        return []
    for candidate in (text, _locate_object(text)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate, strict=False)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        facts = data.get("facts")
        if not isinstance(facts, list):
            return []
        out: list[dict] = []
        for item in facts:
            if not isinstance(item, dict):
                continue
            body = _normalize(item.get("text"))
            kind = str(item.get("kind") or "").strip()
            if not body or kind not in KINDS or len(body) > EXTRACT_MAX_CHARS:
                continue
            fact = {"text": body, "kind": kind}
            key = item.get("key")
            if key is not None:
                if not isinstance(key, str) or not _normalize(key) or len(_normalize(key)) > 200:
                    continue
                fact["key"] = _normalize(key)
            replacement = item.get("supersedes_id")
            if replacement is not None:
                if not isinstance(replacement, int) or isinstance(replacement, bool) or replacement <= 0 or "key" not in fact:
                    continue
                fact["supersedes_id"] = replacement
            try:
                for name in ("valid_from", "valid_until"):
                    if item.get(name) is not None:
                        fact[name] = _utc(datetime.fromisoformat(item[name]))
                if fact.get("valid_from") and fact.get("valid_until") and fact["valid_until"] <= fact["valid_from"]:
                    continue
            except (TypeError, ValueError):
                continue
            out.append(fact)
            if len(out) >= EXTRACT_MAX_FACTS:
                break
        return out
    return []


async def _call_model(model: Any, material: str) -> str:
    """一次**非流式**抽取调用，返回正文（失败抛异常，由调用方归类计数）。"""
    from cubeloop.providers.base import ReasoningControl, TextContent, UserMessage

    from app.usage.instrumentation import usage_scope
    with usage_scope(role="memory_extract"):
        result = await model.generate(
            [
                UserMessage(
                    content=[
                        TextContent(text=EXTRACT_INSTRUCTION.replace("{conversation}", material))
                    ]
                )
            ],
            system_prompt=EXTRACT_SYSTEM,
            max_output_tokens=EXTRACT_MAX_TOKENS + EXTRACT_REASONING_HEADROOM,
            temperature=EXTRACT_TEMPERATURE,
            reasoning=ReasoningControl(mode="off"),
        )
    return "".join(
        str(getattr(block, "text", "") or "")
        for block in (getattr(result, "content", None) or [])
        if getattr(block, "type", "") == "text"
    )


async def _extract_facts(model: Any, question: str, answer: str, *, previous: str = "") -> list[dict]:
    """调一次模型并解析；**失败分级计数**后返回空列表（绝不抛）。"""
    if not _enabled() or model is None:
        return []
    try:
        material = extraction_input(question, answer)
        if previous:
            material = (material + "\n已登记事实（只用于槽位匹配，不是本轮新事实）：\n" + previous)[:EXTRACT_INPUT_MAX_CHARS]
        raw = await _call_model(model, material)
    except Exception as exc:  # noqa: BLE001 —— 抽取绝不拖垮问答本身
        _COUNTS["call_failed"] += 1
        logger.warning("记忆抽取：模型调用失败（已隔离）：%s: %s", exc.__class__.__name__, exc)
        return []
    facts = parse_facts(raw)
    if facts:
        return facts
    # ② 与 ③ 的分界：正文压根解析不出对象 ⇒ 解析失败；解析得出但 facts 为空 ⇒ **正常**
    parsed = _locate_object(_strip_fence(raw))
    if not parsed:
        _COUNTS["parse_failed"] += 1
        logger.warning("记忆抽取：输出解析失败（已隔离，本轮无记忆）：%r", str(raw)[:200])
        return []
    _COUNTS["empty"] += 1
    return []


async def _store_facts(
    facts: list[dict],
    *,
    chat_id: int | None,
    message_id: int | None,
    bound_to: AsyncSession | None,
    filters: dict | None = None,
    question: str = "",
) -> int:
    """把事实落库：**用自己的独立会话**（fire-and-forget 的铁律，见模块 docstring）。

    ``bound_to`` 是**调用方会话**，只用来解析「该连哪个引擎的库」（生产态 = 请求同一个库，
    单测里 = 测试库而不是开发库；口径同 ``usage.recorder._open_session``）。
    """
    session = new_independent_session(bound_to) if bound_to is not None else None
    if session is None:
        session = SessionLocal()
    stored = 0
    try:
        async with session:
            for item in facts:
                # ``remember`` 返回 None = 重复 / 被过滤 ⇒ 不算「新写入」
                new_id = await remember(
                    session,
                    text=item["text"],
                    kind=item["kind"],
                    source_chat_id=chat_id,
                    source_message_id=message_id,
                    scope=SCOPE_GLOBAL if item["kind"] == "interaction" else _knowledge_scope(filters, chat_id),
                    fact_key=item.get("key"),
                    supersedes_id=item.get("supersedes_id") if _CORRECTION.search(question) else None,
                    valid_from=item.get("valid_from"),
                    valid_until=item.get("valid_until"),
                    source_text=question,
                )
                if new_id is not None:
                    stored += 1
            await session.commit()
    except Exception as exc:  # noqa: BLE001 —— 落库失败只 warning，绝不影响本轮回答
        logger.warning("记忆落库失败（已隔离）：%s: %s", exc.__class__.__name__, exc)
        # 回归修复（#12）：落库失败与「零写入」在计数上必须可区分（2026-09-27 拍板⑩ 接线）。
        _COUNTS["store_failed"] += 1
        return 0
    # Provider sends are recorded independently of extracted/stored fact counts.
    return stored


async def remember_turn(
    question: str,
    answer: str,
    *,
    chat_id: int | None = None,
    message_id: int | None = None,
    model: Any = None,
    db: AsyncSession | None = None,
    filters: dict | None = None,
) -> int:
    """**一轮完整成功回答之后**的抽取任务体（fire-and-forget 调它）：返回落库条数。

    串行化：整个任务体排在 :data:`_EXTRACT_LOCK` 里——同一时刻只有一个抽取在做，
    既避免同一轮并发写同一条，也让 :func:`drain_memory_tasks` 有确定的等待点。
    """
    if not _enabled():
        return 0
    async with _EXTRACT_LOCK:
        previous = ""
        if db is not None and model is not None:
            try:
                session = new_independent_session(db)
                if session is None:
                    session = SessionLocal()
                async with session:
                    rows = (await session.execute(
                        select(Memory).where(
                            Memory.fact_key.is_not(None), Memory.status.in_(("active", "conflict")),
                            Memory.scope.in_((SCOPE_GLOBAL, _knowledge_scope(filters, chat_id))),
                        ).order_by(Memory.id.desc()).limit(24)
                    )).scalars().all()
                    previous = json.dumps(
                        [{"id": row.id, "key": row.fact_key, "text": row.text, "status": row.status} for row in rows],
                        ensure_ascii=False,
                    )
            except Exception as exc:
                logger.warning("记忆修订上下文读取失败：%s", exc.__class__.__name__)
                return 0
        from app.usage.instrumentation import usage_scope
        with usage_scope(db=db):
            facts = await _extract_facts(model, question, answer, previous=previous)
        if not facts:
            return 0
        stored = await _store_facts(
            facts, chat_id=chat_id, message_id=message_id, bound_to=db, filters=filters, question=question,
        )
        _COUNTS["stored"] += int(stored)
        return int(stored)


def schedule_extract(
    *,
    question: str,
    answer: str,
    chat_id: int | None,
    message_id: int | None,
    model: Any,
    db: AsyncSession | None = None,
    filters: dict | None = None,
) -> None:
    """把 :func:`remember_turn` 排进后台（**fire-and-forget，不等待、不抛**）。

    调用点是两条回答链路共用的 ``chat_routes.persist_turn``（成功收口之后）——
    所以同步 ``/chat`` 与流式 ``/chat/stream`` 的行为不可能漂移。
    关态 / 没模型 / 没有回答正文 ⇒ 直接返回（零开销、零任务）。
    """
    if not _enabled() or model is None or not str(answer or "").strip():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 没有事件循环（纯同步上下文）：不抽取，记一行说明
        logger.warning("记忆抽取未执行（无可用事件循环）：chat_id=%s", chat_id)
        return
    task = loop.create_task(
        remember_turn(
            question,
            answer,
            chat_id=chat_id,
            message_id=message_id,
            model=model,
            db=db,
            filters=dict(filters) if filters is not None else None,
        )
    )
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


async def drain_memory_tasks() -> None:
    """等所有在途抽取任务跑完（**唯一等待点**；测试与关停收尾用，生产路径不调用）。

    生产路径不能等：它是 fire-and-forget，等一等就把「不阻塞主流程」那条口径丢了。
    ⚠️ ``usage.recorder.drain_pending()`` **只管 usage 任务、不含记忆任务**——
    记忆用例必须等这一条（照它的形状写的）。
    """
    for _ in range(100):
        if not _PENDING:
            return
        await asyncio.gather(*list(_PENDING), return_exceptions=True)
        await asyncio.sleep(0)


__all__ = [
    "EXTRACT_INPUT_MAX_CHARS",
    "EXTRACT_MAX_CHARS",
    "EXTRACT_MAX_FACTS",
    "KINDS",
    "MEMORY_HEADER",
    "SCOPE_GLOBAL",
    "drain_memory_tasks",
    "extraction_input",
    "failure_counts",
    "parse_facts",
    "recall",
    "remember",
    "remember_turn",
    "render_block",
    "reset_failure_counts",
    "schedule_extract",
]
