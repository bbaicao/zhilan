"""models 层结构单测（阶段 0 · 0.2；历史骨架继承自阶段 2 · 2.1.1）。

不连库：只断言 Base.metadata 的表清单与关键列存在，
保证后续迁移/对拍脚本有稳定锚点。
"""

from app.models import (
    AgentRun,
    AgentStep,
    Base,
    Chat,
    Chunk,
    Document,
    IndexBuild,
    KnowledgeBase,
    Memory,
    Message,
    MessageAttribution,
    Note,
    SessionEvent,
    SessionGroup,
    UsageEvent,
    WikiLink,
)

EXPECTED_TABLES = {
    "notes",
    "documents",
    "document_builds",
    "chunks",
    "document_similarity_edges",
    "chats",
    "messages",
    "message_attributions",
    "agent_runs",
    "agent_steps",
    "session_groups",
    "wiki_links",
    "knowledge_bases",
    "usage_events",
    "session_events",
    "memories",
    "lexical_materials",
    "lexical_terms",
}


def test_all_tables_registered():
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_note_columns():
    cols = {c.name for c in Note.__table__.columns}
    assert {
        "id",
        "title",
        "content",
        "category",
        "kb_id",
        "tags",
        "source_document_id",
        "generation_meta",
        "deleted_at",
        "created_at",
        "updated_at",
    } <= cols


def test_document_columns():
    cols = {c.name for c in Document.__table__.columns}
    assert {
        "id",
        "filename",
        "file_type",
        "status",
        "index_status",
        "chunk_count",
        "vectorized",
        "vector_error",
        "content_hash",
        "active_build_id",
        "content_build_id",
        "raw_content",
        "clean_report",
        "kb_id",
        "category",
        "tags",
        "deleted_at",
        "created_at",
    } <= cols


def test_chat_and_message_columns():
    chat_cols = {c.name for c in Chat.__table__.columns}
    assert {
        "id",
        "title",
        "created_at",
        "clarify_rounds",
        "deleted_at",
        "context_summary",
        "compressed_up_to",
        "group_id",
        "archived_at",
    } <= chat_cols
    msg_cols = {c.name for c in Message.__table__.columns}
    assert {
        "id",
        "chat_id",
        "role",
        "content",
        "refs",
        "kind",
        "filters",
        "reasoning",
        "thinking",
        "skill_ids",
        "agent_trace",
        "images",
        "answer_source",
        "source_type",
        "created_at",
    } <= msg_cols


def test_message_attribution_and_agent_tables():
    attr_cols = {c.name for c in MessageAttribution.__table__.columns}
    assert {"id", "message_id", "chunk_id", "ordinal", "title", "snippet", "score"} <= attr_cols
    run_cols = {c.name for c in AgentRun.__table__.columns}
    assert {"id", "chat_id", "message_id", "mode", "status", "started_at", "finished_at"} <= run_cols
    step_cols = {c.name for c in AgentStep.__table__.columns}
    assert {"id", "run_id", "step_no", "type", "tool_name", "tool_call_id"} <= step_cols
    assert "uk_message_attributions_message_ordinal" in {
        uc.name for uc in MessageAttribution.__table__.constraints
    }


def test_session_event_columns_and_unique():
    cols = {c.name for c in SessionEvent.__table__.columns}
    assert {
        "id",
        "chat_id",
        "seq",
        "turn",
        "step",
        "type",
        "data",
        "trace",
        "tool_call_id",
        "attempt_id",
        "request_id",
        "created_at",
    } <= cols
    names = {uc.name for uc in SessionEvent.__table__.constraints}
    assert "uk_session_events_chat_seq" in names


def test_chunk_unique_and_fk():
    indexes = {index.name: index for index in Chunk.__table__.indexes}
    unique = indexes["uk_chunks_document_build_ordinal"]
    assert unique.unique
    assert list(unique.columns.keys()) == ["document_id", "build_id", "ordinal"]
    fks = list(Chunk.__table__.foreign_keys)
    assert any(fk.column.table.name == "documents" for fk in fks)


def test_other_tables_exist():
    for model in (KnowledgeBase, SessionGroup, WikiLink, UsageEvent, IndexBuild):
        assert model.__tablename__ in Base.metadata.tables
