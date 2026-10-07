"""chunk id 前缀工具的无依赖落点（分包边界的最底层）。

**来由**：`prefix_chunk_id` 原先在 `app/pipeline/wikilink.py` 里从 `app.retrieval.ids` 导入。
两个问题：① 该模块在 v2 **从未创建**（旧仓库的真身是 `app/rag/retrievers/ids.py`，移植时
import 路径被改写成了一个不存在的模块）；② 即便存在，它也是 `pipeline/ → retrieval/` 的
**反向 import**，违反仓库根 设计说明「分包边界（禁止反向 import）」与《文档解析升级》
统筹 §4.2 不变量 6，已登记 `回归修复`。

本函数放在最低层 `app/core/ids.py`，以保持分包依赖方向：
`pipeline/` 依赖 `core/` 是合法方向，且**零新增依赖**。

实现逐字移植自老系统同名实现（只读基线快照 `app/rag/retrievers/ids.py::prefix_chunk_id`）。
"""

from __future__ import annotations


def prefix_chunk_id(kb_id: int, chunk_id: str) -> str:
    """把 chunk id 加上知识库前缀，使两个库可以共用同一 ``source_id``。

    已带 ``kb:`` 前缀的 id 原样返回（幂等），避免重复前缀。
    """
    text = str(chunk_id or "")
    if text.startswith("kb:"):
        return text
    return f"kb:{kb_id}:{text}"
