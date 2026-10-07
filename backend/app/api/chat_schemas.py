"""chat 域的请求/响应模型（阶段 1 · 条目 1.5）。

字段集**逐字对齐 02 契约**（§7.1 ChatResponse / §7.2 ChatRequest），
对照只读基线（老系统源码快照）的 `app/api/__init__.py` L165-201：

- ``ChatRequest``：message / chat_id / image_data / images / filters / skill_ids /
  agent_mode / request_id 八字段，默认值与契约一致；
- ``ChatResponse``：chat_id / message / refs / kind / reasoning / thinking /
  skill_ids / agent_trace / answer_source 九字段。

本阶段（1.5）**只实装最小可用子集**：文本问答 + 路由/澄清/直答分路 + 落库；
图片、filters、skills、Agent 编排的**完整语义**属后续条目（1.5 只保留字段位置，
不静默丢弃——见 ``chat_routes.py`` 的显式说明）。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: 时间串里的**显式偏移**形态（Z / ±HH:MM / ±HHMM 结尾）。命中的字符串**原样**交回 pydantic
#: （保住 offset 形态）；未命中的走 datetime.fromisoformat 自己解析 —— "20260101" 这类
#: **基本格式**若交回 pydantic 会被当 **Unix 秒**（⇒ 1970），而 fromisoformat 认它
#: （= 改前 retrieval/types.py::_parse_dt 的行为，审计 REAL-144-1）。
_EXPLICIT_OFFSET_RE = re.compile(r"(?:[Zz]|[+-]\d{2}:?\d{2})$")

#: **pydantic 认得的**扩展格式 + 偏移（``YYYY-MM-DD`` + ``T``/空格 + 时刻 + ``Z``/``±HH:MM``）。
#: 命中它 ⇒ 字符串**原样**交回 pydantic，保住 offset 形态（``to_stored()`` 逐字不变）；
#: 未命中而 ``_EXPLICIT_OFFSET_RE`` 命中（基本格式 / date-only / 偏移前有空格等**小众形态**，
#: 见 ``回归修复`` 加固 · 审计 REAL-144-2）⇒ 自己按同源配方解析（pydantic 会 422）。
_PYDANTIC_EXTENDED_OFFSET_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[Zz]|[+-]\d{2}:?\d{2})$"
)


def _iso_naive(text: str) -> datetime | None:
    """``Z``/``z`` 折成 ``+00:00`` 后按 ``fromisoformat`` 解析，aware 折 naive UTC；失败返回 ``None``。

    与 :func:`app.retrieval.types._parse_dt` 是**同一套配方**（同一库函数 + 同一折算口径）：
    改前该函数就是唯一的时间解析入口，这里只把「解析失败」从「静默放宽」改成「422」
    （失败判定交回调用方）。
    """
    probe = f"{text[:-1]}+00:00" if text[-1:] in ("Z", "z") else text
    try:
        parsed = datetime.fromisoformat(probe)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


class Filters(BaseModel):
    """元数据过滤范围（02 §7.2 子模型）。

    本阶段只做**透传与落库**（存进 ``messages.filters``），检索下推属阶段 3。
    空字段 = 该维度不限制，多维度 AND。
    """

    model_config = ConfigDict(extra="ignore")

    kb_ids: list[int] | None = None
    # 兼容单值写法（老前端可能传 kb_id）
    kb_id: int | None = None
    source_types: list[str] | None = None
    source_type: str | None = None
    categories: list[str] | None = None
    tags: list[str] | None = None
    # 契约（02 §7.2）是 ``datetime|None``：坏值 **422**，不是「解析失败就当作没给」
    # （``回归修复`` —— 旧实现声明成 ``str|None``，坏日期会被
    # ``retrieval/types.py::_parse_dt`` 记 warning 后**静默放宽**成「该维度不限」，
    # 与省略该字段**同效**，把用户的范围限制悄悄吃掉）。
    created_after: datetime | None = None
    created_before: datetime | None = None

    @field_validator("created_after", "created_before", mode="before")
    @classmethod
    def _coerce_time_input(cls, value: Any, info) -> Any:  # noqa: ANN001 —— pydantic 校验签名
        """把时间维度的输入统一成「pydantic 认得、且不会静默错义」的形态。

        四条分支（回归修复 加固 · 审计 REAL-144-1）：

        - ``None`` / ``datetime`` ⇒ 原样放行（进程内直调方）；
        - 非字符串（JSON 数字 / 布尔 / 容器）⇒ **422**：pydantic 的 datetime 会把 JSON
          数字当 **Unix 时间戳**收下（``{"created_before": 20260919}`` ⇒ 1970-08-23 ⇒ 整库
          几乎全被排除），而改前这类输入同样是 422 ⇒ 放行它等于换一种静默错义；
        - 字符串带**显式偏移**（``Z`` / ``±HH:MM`` / ``±HHMM`` 结尾）⇒ **再分两路**：
          ① **扩展格式**（``YYYY-MM-DD`` + 时刻 + 偏移，见 ``_PYDANTIC_EXTENDED_OFFSET_RE``）
          原样交回 pydantic（保住 offset 形态 ⇒ ``to_stored()`` 照旧落 ``…+08:00`` / ``…Z``）；
          ② **小众偏移形态**（基本格式 + 偏移 / date-only + ``Z`` / 偏移前有空格）pydantic 认不了
          ⇒ 走下面的同源配方自己解析（审计 REAL-144-2：这些形态改前 ``_parse_dt`` 能正确解析，
          短路交回 pydantic 会把它们打成 **422** = 可见失败）；解析不了才交回它（⇒ 422）；
        - 其余字符串（无显式偏移：``YYYYMMDD`` 基本格式、``2026-W01-1`` 周日期、两侧空白）⇒ 走与
          :func:`app.retrieval.types._parse_dt` **同源配方**（``datetime.fromisoformat`` +
          aware 折 naive UTC）自己解析。**为什么不能直接交回 pydantic**：pydantic 把
          ``"20260101"`` 当 **Unix 秒** ⇒ 1970-08-23T11:48:21Z（``created_before`` 几乎恒空集），
          而改前 ``_parse_dt``（``fromisoformat`` 认基本格式）得 **2026-01-01** = 正确收窄
          —— 审计 REAL-144-1 抓的正是这条镜像。
        空串与解析失败都是 **422**（不是「当作没给/该维度不限」）。
        """
        if value is None or isinstance(value, datetime):
            return value
        if not isinstance(value, str):
            raise ValueError(
                f"{info.field_name} 只接受 ISO 8601 日期时间字符串，收到 {type(value).__name__}"
            )
        text = value.strip()
        if not text:
            raise ValueError(f"{info.field_name} 不能是空字符串（要放开这一维就整项省略）")
        if _EXPLICIT_OFFSET_RE.search(text):
            if _PYDANTIC_EXTENDED_OFFSET_RE.search(text):
                # 扩展格式 + 偏移：pydantic 认 ⇒ 原样交回（形态逐字保留）
                return text
            # 小众偏移形态（基本格式 / date-only+Z / 偏移前有空格）：pydantic 认不了，
            # 但改前 _parse_dt 能正确解析 ⇒ 按同源配方自己解析；解析不了才交回它出 422。
            parsed = _iso_naive(text)
            return text if parsed is None else parsed

        parsed = _iso_naive(text)
        if parsed is None:
            raise ValueError(f"{info.field_name} 不是可解析的 ISO 8601 日期时间：{value!r}")
        return parsed

    def to_stored(self) -> dict | None:
        """转成落库形态；全空 → ``None``（不写 JSON 'null'，保持 SQL NULL）。

        ⚠️ **必须 ``model_dump(mode="json")``**（不是默认的 python 模式）：两个时间字段是
        ``datetime``，python 模式会把它**原样**塞进这个 dict，而两处消费者都只认 JSON 值 ——
        ``messages.filters`` 是 SQLAlchemy ``JSON`` 列（默认 ``json.dumps``，
        ``models/__init__.py:240``）、语义缓存键走 ``cache/entry.py::filters_token`` 的
        ``json.dumps`` ⇒ ``TypeError: Object of type datetime is not JSON serializable``
        （前者是**插库 500**，后者是缓存静默降级）。JSON 模式把它们序列化成 ISO 8601 字符串，
        保持与改前一致的**落库形态**（字符串），``_parse_dt`` 照旧可解析。
        """
        data = {
            key: value
            for key, value in self.model_dump(mode="json").items()
            if value not in (None, [], "")
        }
        return data or None


class ChatRequest(BaseModel):
    """``POST /api/chat`` 与 ``/api/chat/stream`` 的请求体（02 §7.2）。"""

    model_config = ConfigDict(extra="ignore")

    message: str = ""
    chat_id: int | None = None
    image_data: str | None = None
    images: list[str] | None = None
    filters: Filters | None = None
    skill_ids: list[str] | None = None
    agent_mode: str | None = None
    # 与 `session_events.request_id` 列（String(64)）对齐：超长不拦会在 MySQL 严格模式下抛 DataError，
    # 而它非 IntegrityError ⇒ 不重试、被 safe_append_event 静默吞掉；SQLite 不强制长度 ⇒ 同输入两方言两结果
    # （回归修复）。前端 newRequestId() 产出的是 UUID(36) 或 `req-<ts>-<base36>`，远小于 64。
    request_id: str | None = Field(default=None, max_length=64)


class ChatResponse(BaseModel):
    """``POST /api/chat`` 的同步响应体（02 §7.1）。"""

    chat_id: int
    message: str
    refs: list[dict] = Field(default_factory=list)
    kind: str = "normal"
    reasoning: str = ""
    thinking: dict | None = None
    skill_ids: list[str] = Field(default_factory=list)
    # 非流式 /chat 恒为 None（契约明文：流式接口才落 agent_trace）
    agent_trace: dict | None = None
    answer_source: str | None = None


class ChatStopIn(BaseModel):
    """``POST /api/chat/stop`` 请求体（契约 §1.1，实装在 3.4）。"""

    # 长度上限 **64**（阶段 4 · 条目 4.4 · N3）：契约 §7.2 的 ``ChatRequest.request_id``
    # 与 ``session_events.request_id`` 列（String(64)）都是 64，同一个 id 在两种端点上有两种
    # 长度语义就是漂移（128 的那版还会让超长 id 落库时在 MySQL 严格模式下抛 DataError）。
    request_id: str = Field(min_length=1, max_length=64)


# --------------------------------------------------------------------------
# 会话回读模型（阶段 4 · 条目 4.4 · D2/D3；契约 02 §7.4 冻结）
# --------------------------------------------------------------------------


class ChatOut(BaseModel):
    """``GET /api/chats`` 与会话详情的公共部分（契约 02 §7.4 的 ChatOut，逐字对齐）。"""

    model_config = ConfigDict(extra="ignore")

    id: int
    title: str
    #: 契约 02:382 为必填 ``datetime``：``chats.created_at`` 是 nullable=False + server_default=now()，
    #: 且 repository 写入即 refresh，所有构造点都能给出确定时间（回归检查 收紧）。
    created_at: datetime
    group_id: int | None = None
    archived_at: datetime | None = None


class MessageOut(BaseModel):
    """会话详情里的一条消息（契约 02 §7.4 的 MessageOut，**逐字对齐 14 个字段**）。

    ⚠️ 字段清单是**契约**（``接口说明/02-API契约冻结清单.md`` 的 ChatDetailOut 段），
    冻结前端 ``api.ts`` 的 ``ChatMessage`` 按它读回消息。字段名与顺序都不许漂：
    交付时的核对口径见 ``tests/api/test_chat_detail_4_4.py``（表驱动断言，按契约原文写死）。

    ``recovered`` 是**投影值**（阶段 4 · 条目 4.4 · D1/D6）：
    由 ``recovery/synthesized`` 事件**现算**，不是 ``messages`` 表上的列——
    界面上的「上次工具结果不明」徽章要「刷新后仍在」，而列会陈旧
    （见 :func:`app.session.recover.recovered_message_ids`）。
    历史消息与未恢复的轮次**缺省 false**。
    """

    model_config = ConfigDict(extra="ignore")

    id: int
    role: str
    content: str
    refs: list | dict | None = None
    kind: str = "normal"
    filters: dict | list | None = None
    reasoning: str | None = None
    thinking: dict | None = None
    skill_ids: list | None = None
    agent_trace: dict | None = None
    images: list | None = None
    answer_source: str | None = None
    #: 该条消息是不是「恢复流程标过」的挂载点（**现算**，见类 docstring）
    recovered: bool = False
    #: 契约 02:385 为必填 ``datetime``（同 ChatOut：messages.created_at nullable=False，
    #: 由 repository.add_message 刷新回填）。
    created_at: datetime


class ChatDetailOut(ChatOut):
    """``GET /api/chats/{chat_id}`` 的响应体（契约 02 §7.4 的 ChatDetailOut）。

    只增三个字段（``ChatOut`` 全字段 + 这三个），与老基线同形。
    """

    messages: list[MessageOut] = Field(default_factory=list)
    context_summary: str | None = None
    compressed_up_to: int | None = None


def sse(payload: dict[str, Any]) -> bytes:
    """SSE 帧：``data: <JSON>\\n\\n``（UTF-8，不转义中文）。

    与老基线 ``_sse`` 逐字同形（``ensure_ascii=False`` 是契约的一部分：
    前端直接读文本，转义会让中文变成 ``\\uXXXX``）。
    """
    import json

    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


__all__ = [
    "ChatDetailOut",
    "ChatOut",
    "ChatRequest",
    "ChatResponse",
    "ChatStopIn",
    "Filters",
    "MessageOut",
    "sse",
]
