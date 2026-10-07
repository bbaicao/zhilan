"""回答生成与契约映射（阶段 1 · 条目 1.3 / 1.4）。

- ``source_map``：分路结果 → 契约字段（kind / answer_source / refs）的**唯一映射点**
  （统筹 §3.8），纯函数；
- ``answer``：无资料直答分支（冻结提示词 + 拒答文案与直答严格分开）；
- ``clarify``：澄清轮（模糊判定 + 追问 + 上限），在路由与检索**之前**；
- ``note_gen``：文档 → 笔记草稿（条目 2.4，不落库）。

分包边界：``generation`` 只依赖 ``core``（阶段 3 的 ``retrieval`` 届时按需引入）；
不 import ``api``，也不 import ``fastapi``。
"""

from app.generation import answer, clarify, note_gen, source_map

__all__ = ["answer", "clarify", "note_gen", "source_map"]
