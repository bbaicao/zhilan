"""Agent 工具包：一工具一模块，描述与 handler 同置（OpenCode 形态）。

工具名称是对外协议的一部分，必须与各模块的定义保持一致：

- :mod:`knowledge_search` —— ``knowledge_search``
- :mod:`current_time` —— ``current_time``（第 2 批 ①）
- :mod:`list_documents` —— ``list_documents``（第 2 批 ②）
- :mod:`read_document` —— ``read_document``（第 2 批 ②）
- :mod:`web_search` —— ``web_search``（联网门控）
- :mod:`fetch_page` —— ``fetch_page``（联网门控）
- :mod:`mcp` —— ``mcp:{slug}:{tool}``
- :mod:`list_files` —— ``list_files``（专项工具升级-3 · 阶段 3）
- :mod:`read_file` —— ``read_file``（专项工具升级-3 · 阶段 3）
- :mod:`grep_files` —— ``grep_files``（专项工具升级-3 · 阶段 3）
- :mod:`workspace_write` —— ``workspace_write``（专项工具升级-3 · 阶段 5 · `回归检查`；**写**通道，
  `needs_confirm=True`）

每个模块的对外形状统一（见 `证据/_work-4.1接口冻结.md` §五）：
``TOOL_NAME`` / ``DESCRIPTION`` / ``Args``（pydantic 参数模型）/ ``build_spec(**deps) -> ToolSpec``。
"""

from app.agent.tools import (
    current_time,
    fetch_page,
    grep_files,
    knowledge_search,
    list_documents,
    list_files,
    mcp,
    read_document,
    read_file,
    web_search,
    workspace_write,
)

__all__ = [
    "current_time",
    "fetch_page",
    "grep_files",
    "knowledge_search",
    "list_documents",
    "list_files",
    "mcp",
    "read_document",
    "read_file",
    "web_search",
    "workspace_write",
]
