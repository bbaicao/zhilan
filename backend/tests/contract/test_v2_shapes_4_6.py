"""V2 counterparts to old-repository source assertions; old tests stay unchanged."""

from __future__ import annotations

import ast
import importlib
import json
from datetime import datetime

import pytest

from app.api.chat_schemas import ChatResponse, sse
from app.generation import citation, source_map
from app.retrieval.types import Material


MODELS = [
    ("chat_schemas", "ChatRequest", "message chat_id image_data images filters skill_ids agent_mode request_id"),
    ("chat_schemas", "ChatResponse", "chat_id message refs kind reasoning thinking skill_ids agent_trace answer_source"),
    ("chat_schemas", "ChatOut", "id title created_at group_id archived_at"),
    ("chat_schemas", "MessageOut", "id role content refs kind filters reasoning thinking skill_ids agent_trace images answer_source recovered created_at"),
    ("chat_schemas", "ChatDetailOut", "id title created_at group_id archived_at messages context_summary compressed_up_to"),
    ("chat_schemas", "Filters", "kb_ids source_types categories tags created_after created_before"),
    ("document_routes", "DocumentOut", "id filename file_type status chunk_count vectorized vector_error category tags created_at message kb_id"),
    ("document_routes", "FolderScanOut", "path files skipped truncated"),
    ("document_routes", "GeneratedNoteOut", "title content category tags source_document_id source_filename completeness"),
    ("document_routes", "ReindexOut", "documents notes vectors failed orphans_removed recleaned messages"),
    ("document_routes", "CleanPreviewOut", "filename file_type chunk_count content_hash raw_content clean_content report chunks removed_ratio frontmatter"),
    ("document_routes", "CleanReportOut", "id filename raw_content clean_content report chunk_count"),
    ("note_routes", "NoteOut", "id title content category created_at updated_at source_document_id tags generation_meta"),
    ("note_routes", "WikiLinkHit", "type id title snippet category direction"),
    ("note_routes", "GraphNode", "id type source_id title category tags kb_id degree"),
    ("note_routes", "GraphEdge", "source target kind"),
    ("note_routes", "GraphOut", "nodes edges stats"),
    ("note_routes", "KnowledgeBaseOut", "id name kind path_or_url collection read_only status last_sync_at"),
    ("search_schemas", "SearchHit", "id title snippet category document_id created_at tags"),
    ("search_schemas", "SearchOut", "notes chunks query hint"),
]


@pytest.mark.parametrize("module,name,fields", MODELS, ids=[row[1] for row in MODELS])
def test_v2_out_model_fields(v2_repo_backend, module, name, fields):
    loaded = importlib.import_module(f"app.api.{module}")
    assert str(v2_repo_backend.resolve()) in str(loaded.__file__)
    model = getattr(loaded, name)
    assert set(fields.split()) <= set(model.model_fields), f"{name} missing frozen fields"


@pytest.mark.parametrize("kind,expected", [
    ("normal", "kb"), ("direct", "general"), ("refuse", "empty_scope"), ("clarify", "clarify"),
])
def test_v2_result_mapping(kind, expected):
    assert source_map.answer_source_for(kind) == expected
    response = ChatResponse(chat_id=1, message="answer", kind=kind, answer_source=expected)
    assert response.agent_trace is None


@pytest.mark.parametrize("source", ["document", "note", "web"])
def test_v2_ref_contract_from_real_builder(source):
    material = Material(chunk_id=7, document_id=8, content="正文", filename="source", ordinal=1, block_ordinal=2)
    if source == "note":
        material.note_id = 9
        material.document_id = 0
    elif source == "web":
        material.url = "https://example.test/source"
        material.document_id = 0
    refs, cited = citation.build_refs("normal", "正文[1]", [material], "retrieval-test")
    assert cited == [material]
    ref = refs[0]
    assert set(ref) == {"type", "title", "snippet", "score", "kb_name", "source_id", "url",
                        "chunk_id", "retrieval_id", "ordinal", "citation_mode"}
    assert ref["type"] == source and ref["ordinal"] == 1 and ref["citation_mode"] == "cited"
    assert ref["retrieval_id"] == "retrieval-test" and isinstance(ref["score"], (float, int))
    if source == "document":
        assert ref["source_id"] == 8 and ref["chunk_id"] == "kb:1:document:8:2"
    if source == "note":
        assert ref["source_id"] == 9
    if source == "web":
        assert ref["url"] == material.url


def test_v2_historical_refs_remain_nullable():
    from app.api.chat_schemas import MessageOut
    # 回归检查 必要适配：created_at 已按契约 02:385 收紧为**必填** datetime，样本须给出确定时间；
    # 该字段不是本用例的被测面（refs/recovered 断言一字未改）。
    row = MessageOut(id=1, role="assistant", content="old", created_at=datetime(2025, 1, 1))
    assert row.refs is None and row.recovered is False
    assert citation.CitationMode.__args__ == ("cited", "fallback", "none")


def test_v2_sse_encoding():
    frame = sse({"type": "token", "text": "中文"})
    assert frame.decode("utf-8") == 'data: {"type": "token", "text": "中文"}\n\n'
    assert json.loads(frame.decode()[6:])["text"] == "中文"


@pytest.mark.parametrize("event,fields", [
    ("meta", {"chat_id", "skill_ids"}), ("token", {"text"}), ("reasoning", {"text"}),
    ("agent", {"step"}), ("error", {"detail"}), ("stopped", {"chat_id"}),
    ("done", {"chat_id", "refs", "kind", "reasoning", "thinking", "skill_ids", "agent_trace", "answer_source"}),
])
def test_v2_sse_source_event_fields(v2_repo_backend, event, fields):
    tree = ast.parse((v2_repo_backend / "app/api/chat_routes.py").read_text(encoding="utf-8"))
    matches = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            pairs = {k.value: v for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant)}
            if isinstance(pairs.get("type"), ast.Constant) and pairs["type"].value == event:
                matches.append(pairs)
    assert matches and all(fields <= set(item) for item in matches), f"v2 {event} fields"


def test_v2_note_stream_source_fields(v2_repo_backend):
    source = (v2_repo_backend / "app/api/document_routes.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    types = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "type" and isinstance(value, ast.Constant):
                    types.add(value.value)
    assert {"status", "token", "done", "error"} <= types
    assert "payload = _note_out_from(note).model_dump()" in source
    assert "text/event-stream" in source and "no-cache" in source and "X-Accel-Buffering" in source
