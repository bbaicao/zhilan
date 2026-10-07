"""56 端点存在性 + 方法校验：从 OpenAPI 提取 paths 与 methods，与契约表比对。

优先用 openapi.json 做存在性与方法校验，不发起会改数据的调用。
"""

import pytest

# 契约端点表：(method, path) — 与 02 文档 §1 对齐，共 56 个（v1.0.35 具名新增 3 个）
# 路径中的路径参数统一用 {param} 形式（OpenAPI 格式）
ENDPOINTS = [
    # --- 主路由 27 ---
    ("GET", "/api/health"),
    ("GET", "/api/notes"),
    ("POST", "/api/notes"),
    ("PUT", "/api/notes/{note_id}"),
    ("DELETE", "/api/notes/{note_id}"),
    ("GET", "/api/wiki/backlinks"),
    ("GET", "/api/wiki/related"),
    ("GET", "/api/graph"),
    ("GET", "/api/documents"),
    ("POST", "/api/documents/upload"),
    ("POST", "/api/documents/import-folder/scan"),
    ("POST", "/api/documents/import-file"),
    ("POST", "/api/documents/reindex"),
    ("POST", "/api/documents/clean-preview"),
    ("GET", "/api/documents/{document_id}/clean-report"),
    ("POST", "/api/documents/{document_id}/generate-note"),
    ("POST", "/api/documents/{document_id}/generate-note/stream"),
    ("DELETE", "/api/documents/{document_id}"),
    ("GET", "/api/search"),
    ("GET", "/api/knowledge-bases"),
    ("POST", "/api/knowledge-bases"),
    ("GET", "/api/chats"),
    ("GET", "/api/chats/{chat_id}"),
    ("DELETE", "/api/chats/{chat_id}"),
    ("POST", "/api/chat"),
    ("POST", "/api/chat/stream"),
    ("POST", "/api/chat/stop"),
    # --- 分组路由 12 ---
    ("GET", "/api/trash"),
    ("POST", "/api/trash/restore"),
    ("DELETE", "/api/trash/{kind}/{item_id}"),
    ("POST", "/api/trash/empty"),
    ("GET", "/api/chat-groups"),
    ("POST", "/api/chat-groups"),
    ("PUT", "/api/chat-groups/{group_id}"),
    ("DELETE", "/api/chat-groups/{group_id}"),
    ("PUT", "/api/chats/{chat_id}/group"),
    ("POST", "/api/chats/{chat_id}/archive"),
    ("POST", "/api/chats/{chat_id}/unarchive"),
    ("POST", "/api/translate"),
    # --- 独立挂载 17 ---
    ("GET", "/api/agent/audit"),
    ("GET", "/api/settings"),
    ("PUT", "/api/settings"),
    ("GET", "/api/settings/offline-models"),
    ("POST", "/api/settings/offline-models/import"),
    ("POST", "/api/models"),
    ("POST", "/api/web/llm/models"),
    ("GET", "/api/models"),
    ("GET", "/api/skills"),
    ("PUT", "/api/skills"),
    ("POST", "/api/skills/scan"),
    ("POST", "/api/skills/mcp/probe"),
    ("POST", "/api/system/pick-directory"),
    ("POST", "/api/system/pick-model-archive"),
    ("POST", "/api/export/markdown"),
    ("GET", "/api/usage"),
    ("GET", "/api/usage/export"),
]

# path 参数名在源码中可能与 OpenAPI 中不同（FastAPI 自动转换）
# 这里做一个归一化映射：源码写法 → OpenAPI 写法
_PATH_ALIASES = {
    "/api/notes/{note_id}": ["/api/notes/{note_id}"],
    "/api/documents/{id}/clean-report": ["/api/documents/{document_id}/clean-report", "/api/documents/{id}/clean-report"],
    "/api/documents/{id}/generate-note": ["/api/documents/{document_id}/generate-note", "/api/documents/{id}/generate-note"],
    "/api/documents/{id}/generate-note/stream": ["/api/documents/{document_id}/generate-note/stream", "/api/documents/{id}/generate-note/stream"],
    "/api/documents/{id}": ["/api/documents/{document_id}", "/api/documents/{id}"],
}


def _normalize_path(path: str) -> str:
    """把 OpenAPI 路径归一化，方便比对。"""
    return path


def _get_openapi_paths_methods(openapi_schema: dict) -> dict[str, set[str]]:
    """从 OpenAPI schema 提取 {path: {method, ...}}。"""
    result: dict[str, set[str]] = {}
    for path, ops in openapi_schema.get("paths", {}).items():
        methods = {m.upper() for m in ops.keys() if m in ("get", "post", "put", "delete", "patch", "head", "options")}
        result[path] = methods
    return result


def _path_exists(openapi_paths: dict[str, set[str]], contract_path: str, method: str) -> bool:
    """检查契约路径是否在 OpenAPI 中存在且支持该方法。

    处理路径参数名差异（如 {note_id} vs {id}）。
    """
    # 直接匹配
    if contract_path in openapi_paths and method in openapi_paths[contract_path]:
        return True
    # 尝试别名
    for alias in _PATH_ALIASES.get(contract_path, []):
        if alias in openapi_paths and method in openapi_paths[alias]:
            return True
    # 模糊匹配：把路径参数名统一成占位符后比较
    import re

    def _strip_params(p: str) -> str:
        return re.sub(r"\{[^}]+\}", "{}", p)

    target = _strip_params(contract_path)
    for op_path, methods in openapi_paths.items():
        if _strip_params(op_path) == target and method in methods:
            return True
    return False


class TestEndpointsCatalog:
    """56 端点表驱动：存在性 + 方法。"""

    def test_endpoint_count(self):
        """契约表应为原 53 个端点 + v1.0.35 批准的 3 个。"""
        assert len(ENDPOINTS) == 56, f"契约表应有 56 个端点，实际 {len(ENDPOINTS)}"

    def test_all_endpoints_exist(self, openapi_schema):
        """所有 56 个端点都应在 OpenAPI 中存在且方法匹配。"""
        openapi_pm = _get_openapi_paths_methods(openapi_schema)
        missing = []
        for method, path in ENDPOINTS:
            if not _path_exists(openapi_pm, path, method):
                missing.append(f"{method} {path}")
        assert not missing, f"以下端点在 OpenAPI 中不存在或方法不匹配：{missing}"

    def test_openapi_path_count(self, openapi_schema):
        """OpenAPI 中 /api/* 方法与路径组合应 >= 56。"""
        openapi_pm = _get_openapi_paths_methods(openapi_schema)
        api_paths = [p for p in openapi_pm if p.startswith("/api/")]
        # 至少 56 个 method+path 组合
        total_ops = sum(len(m) for p, m in openapi_pm.items() if p.startswith("/api/"))
        assert total_ops >= 56, f"OpenAPI /api/* 操作数应 >= 56，实际 {total_ops}"

    @pytest.mark.parametrize("method,path", ENDPOINTS, ids=[f"{m} {p}" for m, p in ENDPOINTS])
    def test_single_endpoint(self, openapi_schema, method, path):
        """单个端点存在性 + 方法（参数化，便于定位失败项）。"""
        openapi_pm = _get_openapi_paths_methods(openapi_schema)
        assert _path_exists(openapi_pm, path, method), f"{method} {path} 不在 OpenAPI 中"
