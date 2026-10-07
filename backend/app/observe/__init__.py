"""观察层（阶段 3 · 条目 3.3）：**器官**——citation / trace / guard。

器官的语义（统筹 §3.6-3 与阶段 3 §二）：

- **引用同源**：引用事件里的 ``chunk_id`` / ``ordinal`` / ``retrieval_id`` 全部取自本轮
  **同一份材料快照**，不重算、不另起一套编号；
- **绝不炸穿**：器官自己写失败只留警告（``session.events.safe_append_event``），
  永不把主流程带崩——guard 更直接保证「异常路径也必有一条收尾」；
- **事件配对**：写出的每一类事件都在 ``session.events.KNOWN_EVENT_TYPES`` 词汇表内。

依赖方向：``observe`` 与 ``generation`` 同层，可依赖 ``generation`` / ``session`` / ``core``；
**不** import ``app.api``。本层**不做** HTTP 帧化——SSE 组装留在 ``api``（器官只管语义与留痕）。
"""

from __future__ import annotations

from app.observe.citation import (
    classify_citations,
    classify_direct_citations,
    log_citation_events,
)
from app.observe.guard import StreamGuard
from app.observe.trace import log_retrieval_result, log_retrieval_start

__all__ = [
    "StreamGuard",
    "classify_citations",
    "classify_direct_citations",
    "log_citation_events",
    "log_retrieval_result",
    "log_retrieval_start",
]
