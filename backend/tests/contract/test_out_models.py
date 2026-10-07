"""Out 模型字段清单静态解析：从 schemas.py 与 api/__init__.py 直读。

这是 02 文档 §7.4 补录依据的可执行化。
"""

import re
from pathlib import Path

import pytest


def _extract_class_fields(source: str, class_name: str) -> list[str]:
    """从源码中提取指定 Pydantic 类的字段名列表。"""
    pattern = rf"class {class_name}\(.*?\):(.*?)(?=\nclass |\n@router|\n# ---|\Z)"
    match = re.search(pattern, source, re.DOTALL)
    if not match:
        return []
    body = match.group(1)
    # 匹配字段定义：name: type
    fields = re.findall(r"^\s{4}(\w+)\s*:", body, re.MULTILINE)
    # 排除 model_config 等
    return [f for f in fields if f not in ("model_config",)]


class TestOutModels:
    """主要 Out 模型字段清单核对。"""

    @pytest.fixture(scope="class")
    def schemas_source(self, old_repo_backend: Path) -> str:
        return (old_repo_backend / "app" / "rag" / "schemas.py").read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    def api_source(self, old_repo_backend: Path) -> str:
        return (old_repo_backend / "app" / "api" / "__init__.py").read_text(encoding="utf-8")

    # --- schemas.py 中的模型 ---

    def test_ref_fields(self, schemas_source):
        fields = _extract_class_fields(schemas_source, "Ref")
        expected = ["type", "title", "snippet", "score", "kb_name", "source_id", "url", "chunk_id", "retrieval_id", "ordinal", "citation_mode"]
        for f in expected:
            assert f in fields, f"Ref 缺少字段 {f}，实际 {fields}"

    def test_rag_result_fields(self, schemas_source):
        fields = _extract_class_fields(schemas_source, "RagResult")
        expected = ["answer", "refs", "kind", "reasoning", "thinking", "agent_trace", "answer_source"]
        for f in expected:
            assert f in fields, f"RagResult 缺少字段 {f}，实际 {fields}"

    def test_filters_fields(self, schemas_source):
        fields = _extract_class_fields(schemas_source, "Filters")
        expected = ["kb_ids", "source_types", "categories", "tags", "created_after", "created_before"]
        for f in expected:
            assert f in fields, f"Filters 缺少字段 {f}，实际 {fields}"

    # --- api/__init__.py 中的模型 ---

    def test_note_out_fields(self, api_source):
        fields = _extract_class_fields(api_source, "NoteOut")
        expected = ["id", "title", "content", "category", "created_at", "updated_at", "source_document_id", "tags", "generation_meta"]
        for f in expected:
            assert f in fields, f"NoteOut 缺少字段 {f}，实际 {fields}"

    def test_document_out_fields(self, api_source):
        fields = _extract_class_fields(api_source, "DocumentOut")
        expected = ["id", "filename", "file_type", "status", "chunk_count", "vectorized", "vector_error", "category", "tags", "created_at", "message", "kb_id"]
        for f in expected:
            assert f in fields, f"DocumentOut 缺少字段 {f}，实际 {fields}"

    def test_chat_out_fields(self, api_source):
        fields = _extract_class_fields(api_source, "ChatOut")
        expected = ["id", "title", "created_at", "group_id", "archived_at"]
        for f in expected:
            assert f in fields, f"ChatOut 缺少字段 {f}，实际 {fields}"

    def test_message_out_fields(self, api_source):
        fields = _extract_class_fields(api_source, "MessageOut")
        expected = ["id", "role", "content", "refs", "kind", "filters", "reasoning", "thinking", "skill_ids", "agent_trace", "images", "answer_source", "recovered", "created_at"]
        for f in expected:
            assert f in fields, f"MessageOut 缺少字段 {f}，实际 {fields}"

    def test_chat_detail_out_fields(self, api_source):
        fields = _extract_class_fields(api_source, "ChatDetailOut")
        # ChatDetailOut 继承 ChatOut，自身只有 messages/context_summary/compressed_up_to
        expected = ["messages", "context_summary", "compressed_up_to"]
        for f in expected:
            assert f in fields, f"ChatDetailOut 缺少字段 {f}，实际 {fields}"

    def test_chat_request_fields(self, api_source):
        fields = _extract_class_fields(api_source, "ChatRequest")
        expected = ["message", "chat_id", "image_data", "images", "filters", "skill_ids", "agent_mode", "request_id"]
        for f in expected:
            assert f in fields, f"ChatRequest 缺少字段 {f}，实际 {fields}"

    def test_chat_response_fields(self, api_source):
        fields = _extract_class_fields(api_source, "ChatResponse")
        expected = ["chat_id", "message", "refs", "kind", "reasoning", "thinking", "skill_ids", "agent_trace", "answer_source"]
        for f in expected:
            assert f in fields, f"ChatResponse 缺少字段 {f}，实际 {fields}"

    def test_search_hit_fields(self, api_source):
        fields = _extract_class_fields(api_source, "SearchHit")
        expected = ["id", "title", "snippet", "category", "document_id", "created_at", "tags"]
        for f in expected:
            assert f in fields, f"SearchHit 缺少字段 {f}，实际 {fields}"

    def test_search_out_fields(self, api_source):
        fields = _extract_class_fields(api_source, "SearchOut")
        expected = ["notes", "chunks", "query", "hint"]
        for f in expected:
            assert f in fields, f"SearchOut 缺少字段 {f}，实际 {fields}"

    def test_graph_node_fields(self, api_source):
        fields = _extract_class_fields(api_source, "GraphNode")
        expected = ["id", "type", "source_id", "title", "category", "tags", "kb_id", "degree"]
        for f in expected:
            assert f in fields, f"GraphNode 缺少字段 {f}，实际 {fields}"

    def test_graph_edge_fields(self, api_source):
        fields = _extract_class_fields(api_source, "GraphEdge")
        expected = ["source", "target", "kind"]
        for f in expected:
            assert f in fields, f"GraphEdge 缺少字段 {f}，实际 {fields}"

    def test_graph_out_fields(self, api_source):
        fields = _extract_class_fields(api_source, "GraphOut")
        expected = ["nodes", "edges", "stats"]
        for f in expected:
            assert f in fields, f"GraphOut 缺少字段 {f}，实际 {fields}"

    def test_knowledge_base_out_fields(self, api_source):
        fields = _extract_class_fields(api_source, "KnowledgeBaseOut")
        expected = ["id", "name", "kind", "path_or_url", "collection", "read_only", "status", "last_sync_at"]
        for f in expected:
            assert f in fields, f"KnowledgeBaseOut 缺少字段 {f}，实际 {fields}"

    def test_folder_scan_out_fields(self, api_source):
        fields = _extract_class_fields(api_source, "FolderScanOut")
        expected = ["path", "files", "skipped", "truncated"]
        for f in expected:
            assert f in fields, f"FolderScanOut 缺少字段 {f}，实际 {fields}"

    def test_generated_note_out_fields(self, api_source):
        fields = _extract_class_fields(api_source, "GeneratedNoteOut")
        expected = ["title", "content", "category", "tags", "source_document_id", "source_filename", "completeness"]
        for f in expected:
            assert f in fields, f"GeneratedNoteOut 缺少字段 {f}，实际 {fields}"

    def test_wiki_link_hit_fields(self, api_source):
        fields = _extract_class_fields(api_source, "WikiLinkHit")
        expected = ["type", "id", "title", "snippet", "category", "direction"]
        for f in expected:
            assert f in fields, f"WikiLinkHit 缺少字段 {f}，实际 {fields}"

    def test_reindex_out_fields(self, api_source):
        fields = _extract_class_fields(api_source, "ReindexOut")
        expected = ["documents", "notes", "vectors", "failed", "orphans_removed", "recleaned", "messages"]
        for f in expected:
            assert f in fields, f"ReindexOut 缺少字段 {f}，实际 {fields}"

    def test_clean_preview_out_fields(self, api_source):
        fields = _extract_class_fields(api_source, "CleanPreviewOut")
        expected = ["filename", "file_type", "chunk_count", "content_hash", "raw_content", "clean_content", "report", "chunks", "removed_ratio", "frontmatter"]
        for f in expected:
            assert f in fields, f"CleanPreviewOut 缺少字段 {f}，实际 {fields}"

    def test_clean_report_out_fields(self, api_source):
        fields = _extract_class_fields(api_source, "CleanReportOut")
        expected = ["id", "filename", "raw_content", "clean_content", "report", "chunk_count"]
        for f in expected:
            assert f in fields, f"CleanReportOut 缺少字段 {f}，实际 {fields}"

    def test_answer_source_mapping(self, schemas_source):
        """ANSWER_SOURCE_BY_KIND 映射：normal→kb, direct→general, refuse→empty_scope, clarify→clarify。"""
        assert '"normal": "kb"' in schemas_source
        assert '"direct": "general"' in schemas_source
        assert '"refuse": "empty_scope"' in schemas_source
        assert '"clarify": "clarify"' in schemas_source
