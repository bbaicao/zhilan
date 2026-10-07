"""会话与消息层（阶段 1 · 条目 1.1）。

- ``repository``：messages / chats 行级读写（不提交）；
- ``service``：轮次级业务入口（取回或新建会话、存一问一答、组装模型历史）。

分包边界：``api → routing/generation/retrieval → session → db/core``。
本包不 import 上层，也不 import fastapi（领域异常由 api 层映射成 HTTP）。
"""

from app.session import repository, service

__all__ = ["repository", "service"]
