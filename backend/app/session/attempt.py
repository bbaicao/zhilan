"""一次模型尝试的标识 + 「已交付前缀」的凭证（阶段 4 · 条目 4.4）。

**这是老基线 ``<legacy-repo>\\backend\\app\\rag\\attempt.py`` 的忠实移植**（与 4.2 的「口径 A」同性质）：
原文件首行 docstring 就写着「阶段 4 条目 4.4：一次模型尝试的标识 +『已交付前缀』的凭证」，
标识、三种终态与指纹保持兼容（``uuid4().hex[:12]`` / ``sha256:`` 前缀 + 16 位摘要 /
空文本也要给哈希 / 非法 ``outcome`` 直接抛）。OPT2 补入生成状态与完成原因，截断不记完整成功。

本模块只放**纯逻辑**（不碰 db、不碰 SSE、不碰 provider），理由沿用老基线：

1. 「已交付前缀可重建」必须可单测——字符数与哈希是这条结论的**唯一凭证**，算错了整条证据链
   就是假的；
2. 接线层（``app/api/chat_routes.py`` 的两条回答链路）只剩「取一次指纹 + 写两条事件」，
   可测逻辑集中在这里，共享文件上的改动面最小。

三种终态（写进 ``assistant/attempt`` 事件载荷的 ``outcome``）：

| 终态 | 什么时候 | 是否落了 assistant 消息行 |
|---|---|---|
| ``committed`` | 完整回答正常收尾 | 是（完整回答） |
| ``interrupted`` | 用户点停止 / 上游 aborted / length 截断 | 有正文时保留；停止为 ``kind="stopped"``，截断保留原 kind 并标 ``generation_status="incomplete"`` |
| ``aborted`` | 出错、客户端断开、进程内异常收尾 | 否（只有日志） |

**v2 的覆盖面（核验代理 B1 打回的一点）**：铸造点必须覆盖**两条回答链路**——
流式 ``/api/chat/stream`` 与同步 ``/api/chat``（老基线只在流式那条铸造；v2 两条都铸，
否则同步端点的 ``assistant/attempt`` 永远缺席）。两条链路共用 :func:`app.api.chat_routes.start_attempt`
这一个入口，别在别处再铸一个。

**不算 attempt 的调用**（一句话切清，别把「所有模型调用」都算进来）：
``note_gen`` 生成笔记、``compaction`` 压缩摘要（已有自己的 ``compaction/*`` 三事件）、
``runner`` 的 Agent 决策轮、``routing/llm`` 路由分类、``clarify`` 澄清判定。
判据只有一条：**会不会落 ``assistant/message``**——只有「产生持久 assistant 消息的回答链路」算一次尝试。
"""

from __future__ import annotations

import hashlib
import uuid

# 一次模型尝试的三种终态（写进 assistant/attempt 事件载荷的 outcome）
ATTEMPT_COMMITTED = "committed"
ATTEMPT_INTERRUPTED = "interrupted"
ATTEMPT_ABORTED = "aborted"

ATTEMPT_OUTCOMES: tuple[str, ...] = (ATTEMPT_COMMITTED, ATTEMPT_INTERRUPTED, ATTEMPT_ABORTED)

#: id 长度（老基线同值）：够短（能进 ``session_events.attempt_id`` 的 String(64) 列）且够唯一
ATTEMPT_ID_LEN = 12

# 哈希前缀：把算法写进值里，将来换算法时读日志的人不会误比
HASH_PREFIX = "sha256:"

#: 事件类型字面量只许出现一处，消费方 import 常量（与 ``events.ROUTING_DECIDED`` 同一纪律）
ATTEMPT_EVENT = "assistant/attempt"
INTERRUPTED_EVENT = "stream/interrupted"

__all__ = [
    "ATTEMPT_ABORTED",
    "ATTEMPT_COMMITTED",
    "ATTEMPT_EVENT",
    "ATTEMPT_ID_LEN",
    "ATTEMPT_INTERRUPTED",
    "ATTEMPT_OUTCOMES",
    "HASH_PREFIX",
    "INTERRUPTED_EVENT",
    "attempt_payload",
    "delivered_fingerprint",
    "interrupted_payload",
    "new_attempt_id",
]


def new_attempt_id() -> str:
    """一次模型尝试的稳定 id。

    **不用时间戳**：同毫秒并发会撞（老基线踩坑记录）；``uuid4().hex`` 截 12 位足够唯一且短。
    同一轮的 ``assistant/attempt``、``stream/interrupted``、``assistant/message`` 三处共用它，
    跨事件才能串成一条链（``session_events.attempt_id`` 列上有 ``ix_session_events_attempt``）。
    """
    return uuid.uuid4().hex[:ATTEMPT_ID_LEN]


def delivered_fingerprint(text: str | None) -> dict:
    """「已交付前缀」的凭证：字符数 + 内容哈希。

    - ``delivered_chars`` 与已交付正文**逐字符**相等（不做 strip：空格也是交付过的东西，
      去掉它会让「字符数对得上」变成一句模糊话）；
    - 空文本也要给哈希（``sha256:`` + 空串摘要），**不用 None**：``None`` 与「没记录」
      分不开，而对账需要的是「记录了什么」而不是「有没有值」；
    - 哈希是**内容**哈希而不是长度哈希：字符数能对上、内容被改写的情况必须能被判出来。
    """
    payload = "" if text is None else str(text)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return {"delivered_chars": len(payload), "delivered_text_hash": f"{HASH_PREFIX}{digest}"}


def interrupted_payload(attempt_id: str, text: str | None, *, reason: str = "stopped") -> dict:
    """``stream/interrupted`` 事件的载荷：把「这次尝试的已交付前缀」固定成可对账的四个键。

    阶段 3 只写了 ``{"reason": "stopped", "chars": <int>}``；本条目**只增字段**
    （旧读方忽略未知键），新增 ``attempt_id`` / ``delivered_chars`` / ``delivered_text_hash``。
    ``chars`` 仍由调用方按老口径带上，本函数只负责这四键。
    """
    data = {"attempt_id": attempt_id, "reason": reason or "stopped"}
    data.update(delivered_fingerprint(text))
    return data


def attempt_payload(attempt_id: str, *, provider: str = "", model: str = "", outcome: str) -> dict:
    """``assistant/attempt`` 事件的载荷：一次尝试的标识 + 终态。

    ``outcome`` 必须是 :data:`ATTEMPT_OUTCOMES` 之一（写错了日志就不再有判据意义，
    因此这里直接抛，而不是默默写个未知值进去）。
    """
    if outcome not in ATTEMPT_OUTCOMES:
        raise ValueError(f"未知的尝试终态：{outcome!r}（合法值：{ATTEMPT_OUTCOMES}）")
    return {
        "attempt_id": attempt_id,
        "provider": provider or "",
        "model": model or "",
        "outcome": outcome,
    }
