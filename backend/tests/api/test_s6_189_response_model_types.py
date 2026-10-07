"""S6 · 条目 189「响应模型类型对齐」自证载体（契约真源：``接口说明/02-API契约冻结清单.md``）。

被测面（02:379 DocumentOut / 02:382 ChatOut / 02:385 MessageOut）：

| 字段 | 契约 | 改前 | 改后 |
|---|---|---|---|
| ``ChatOut.created_at`` | ``datetime``（必填） | ``Any`` | ``datetime`` |
| ``ChatOut.archived_at`` | ``datetime | None`` | ``Any | None`` | ``datetime | None`` |
| ``MessageOut.created_at`` | ``datetime``（必填） | ``Any = None`` | ``datetime`` |
| ``MessageOut.refs`` | ``list | dict | None`` | ``Any | None`` | ``list | dict | None`` |
| ``MessageOut.filters`` | ``dict | list | None`` | ``Any | None`` | ``dict | list | None`` |
| ``DocumentOut.created_at`` | ``datetime``（必填） | ``object`` | ``datetime`` |

两条腿：

1. **OpenAPI 真 $ref 解析**：顺着 ``$ref`` 进 ``components.schemas`` 逐字段核对。
   只判「顶层有没有 $ref」抓不住目标里的漂移——``Any`` 的 schema 是 ``{}``，
   与 ``{"type":"string","format":"date-time"}`` 的区别只有在解析之后才看得见。
2. **HTTP 逐值序列化**：真起 app（复用 ``tests.api.test_chat_contract.client`` 夹具，
   照 ``tests/api/test_documents_contract.py`` 的先例）打 ``/api/chats``、
   ``/api/chats/{id}``、``/api/documents``、``/api/documents/upload``。

⚠️ 判别力如实登记：``created_at`` 走 ``Any`` 时 FastAPI **仍**把 datetime 序列化成 ISO 串，
所以腿 2 对「旧注解」的判别力有限——它防的是「收紧之后真响应反而打红」的回归；
旧/新注解的真正判别力在腿 1（schema ``{}`` vs date-time）+ 下面这条字段注解/必填性断言
（改前 ``created_at=None`` 被静默接受，改后 ValidationError；见报告里的负对照实测）。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import get_args

import pytest
from pydantic import ValidationError

from app.session import repository as repo
from tests.api.test_chat_contract import client  # noqa: F401 —— 夹具复用（ASGI 直连 + 测试库）

#: FastAPI 对 datetime 的序列化形态（含可选小数秒与偏移）
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)?$")


# ---------------------------------------------------------------------------
# 自钉夹具（照 tests/api/conftest.py:339-344 的告诫：Package 被重复 collect 时
# 本文件所在目录的 autouse 面可能整片丢掉 ⇒ 自证型载体就地自钉，不依赖外层）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _s6_upload_dir(monkeypatch, tmp_path):
    """上传落档目录重定向到 tmp_path（property 不能实例属性覆盖，改类属性）。"""
    from app.core.config import settings

    monkeypatch.setattr(type(settings), "upload_path", property(lambda self: tmp_path))
    yield


@pytest.fixture(autouse=True)
def _s6_no_real_vectors(monkeypatch):
    """隔离真 embedding / 真 Qdrant：本文件只断言字段形状，不碰外部服务。"""

    class _NoEmbed:
        dim = 4

        async def embed_texts(self, texts):
            raise RuntimeError("测试隔离：不调用真实 embedding")

    class _NoStore:
        def ensure_collection(self, dim):
            raise RuntimeError("测试隔离：不连 Qdrant")

        def upsert(self, points):
            raise RuntimeError("测试隔离：不连 Qdrant")

        def delete(self, ids):
            raise RuntimeError("测试隔离：不连 Qdrant")

    monkeypatch.setattr("app.pipeline.ingest.get_embedding_client", lambda **kwargs: _NoEmbed())
    monkeypatch.setattr("app.pipeline.ingest.get_vector_store", lambda **kwargs: _NoStore())


# ---------------------------------------------------------------------------
# OpenAPI 解析小工具（腿 1）
# ---------------------------------------------------------------------------


def _spec() -> dict:
    from main import app

    return app.openapi()


def _schema_by_name(spec: dict, name: str) -> dict:
    schemas = spec["components"]["schemas"]
    assert name in schemas, f"components.schemas 缺 {name}（实有 {sorted(schemas)[:24]}…）"
    return schemas[name]


def _ref_name(node) -> str:
    """从 ``{"$ref": "#/components/schemas/X"}`` 取出 X；不是真 $ref 就报错。

    ⛔ 这一步是本条的关键：``Any`` 字段在 FastAPI 里**不会**被 $ref 一层遮住——
    它就在 $ref 目标内部以 ``{}`` 出现，只有真解析进去才看得见。
    """
    ref = node.get("$ref") if isinstance(node, dict) else None
    assert isinstance(ref, str) and ref.startswith("#/components/schemas/"), (
        f"期望真 $ref，实得 {node!r}"
    )
    return ref.rsplit("/", 1)[-1]


def _resolve(spec: dict, node) -> tuple[str, dict]:
    name = _ref_name(node)
    return name, _schema_by_name(spec, name)


def _response_schema(spec: dict, path: str, method: str = "get", status: str = "200") -> dict:
    assert path in spec["paths"], f"OpenAPI 缺路径 {path}"
    op = spec["paths"][path][method]
    return op["responses"][status]["content"]["application/json"]["schema"]


def _branches(schema: dict) -> list[dict]:
    """把 anyOf/oneOf 拆成分支；单类型归一成一支。"""
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return list(schema[key])
    return [schema]


def _non_null_branches(schema: dict) -> list[dict]:
    return [b for b in _branches(schema) if b.get("type") != "null"]


def _has_null(schema: dict) -> bool:
    if any(b.get("type") == "null" for b in _branches(schema)):
        return True
    t = schema.get("type")
    return isinstance(t, list) and "null" in t


def _is_datetime_schema(schema: dict) -> bool:
    """``datetime`` 的 OpenAPI 形态（pydantic 另会挂 ``title``，故不整体比字典）。

    改前的 ``Any`` / ``object`` 在这里是 ``{}`` / ``{}`` ⇒ ``type``/``format`` 双缺，红。
    """
    return schema.get("type") == "string" and schema.get("format") == "date-time"


def _assert_required_datetime(model: dict, field: str) -> None:
    schema = model["properties"][field]
    assert _is_datetime_schema(schema), f"{field} 应为 string/date-time，实得 {schema!r}"
    assert field in model.get("required", []), f"{field} 必须是必填（契约写的是 datetime，不是 datetime|None）"


def _assert_nullable_datetime(schema: dict) -> None:
    branches = _non_null_branches(schema)
    assert len(branches) == 1 and _is_datetime_schema(branches[0]), (
        f"非 null 分支应恰为一个 string/date-time，实得 {branches!r}"
    )
    assert _has_null(schema), f"应含 null 分支，实得 {schema!r}"


def _assert_nullable_types(schema: dict, expected: set[str]) -> None:
    got = {b.get("type") for b in _non_null_branches(schema)}
    assert got == expected, f"非 null 分支类型应为 {expected}，实得 {got}（原 schema {schema!r}）"
    assert _has_null(schema), f"应含 null 分支，实得 {schema!r}"


def _assert_iso(value) -> None:
    assert isinstance(value, str), f"created_at 必须是 ISO **字符串**（不是 {type(value).__name__}）"
    assert ISO_RE.match(value), f"不是 ISO-8601 形态：{value!r}"
    datetime.fromisoformat(value.replace("Z", "+00:00"))  # 真的可解析


def _ann_names(ann) -> set[str]:
    args = get_args(ann)
    if not args:
        return {getattr(ann, "__name__", str(ann))}
    return {getattr(a, "__name__", str(a)) for a in args}


# ---------------------------------------------------------------------------
# 腿 1：OpenAPI 真 $ref 解析
# ---------------------------------------------------------------------------


def test_openapi_chats_items_ref_resolves_to_chatout_created_at():
    """``/api/chats`` → 数组 items 是真 $ref → ChatOut.created_at 是 date-time 且必填。"""
    spec = _spec()
    schema = _response_schema(spec, "/api/chats", "get")
    assert schema.get("type") == "array", f"/api/chats 应返回裸数组，实得 {schema!r}"
    name, model = _resolve(spec, schema["items"])
    assert name == "ChatOut"
    _assert_required_datetime(model, "created_at")
    _assert_nullable_datetime(model["properties"]["archived_at"])
    assert "archived_at" not in model.get("required", []), "archived_at 可空且可缺省（None 默认）"


def test_openapi_chat_detail_messages_ref_resolves_to_messageout_fields():
    """``/api/chats/{chat_id}`` → ChatDetailOut → messages.items 再一层 $ref → MessageOut。"""
    spec = _spec()
    name, model = _resolve(spec, _response_schema(spec, "/api/chats/{chat_id}", "get"))
    assert name == "ChatDetailOut"
    _assert_required_datetime(model, "created_at")  # 继承自 ChatOut 的字段也在同一 schema 里
    _assert_nullable_datetime(model["properties"]["archived_at"])

    messages = model["properties"]["messages"]
    assert messages.get("type") == "array", f"messages 应是数组，实得 {messages!r}"
    msg_name, msg = _resolve(spec, messages["items"])
    assert msg_name == "MessageOut"
    _assert_required_datetime(msg, "created_at")
    _assert_nullable_types(msg["properties"]["refs"], {"array", "object"})
    _assert_nullable_types(msg["properties"]["filters"], {"object", "array"})
    # skill_ids 外层是数组即可，**元素不加约束**（契约 :385 写的是 list | None）
    skill_ids = msg["properties"]["skill_ids"]
    assert {b.get("type") for b in _non_null_branches(skill_ids)} == {"array"}
    assert _has_null(skill_ids)
    array_branch = _non_null_branches(skill_ids)[0]
    assert array_branch.get("items", {}) == {}, f"skill_ids 元素不许收紧：{array_branch!r}"


@pytest.mark.parametrize(
    "path,method,is_array",
    [("/api/documents", "get", True), ("/api/documents/upload", "post", False)],
    ids=["list", "upload"],
)
def test_openapi_documentout_ref_resolves_to_datetime_created_at(path, method, is_array):
    """两个 documents 出口都经真 $ref 指到同一个 DocumentOut，created_at 是 date-time 且必填。"""
    spec = _spec()
    schema = _response_schema(spec, path, method)
    node = schema["items"] if is_array else schema
    name, model = _resolve(spec, node)
    assert name == "DocumentOut"
    _assert_required_datetime(model, "created_at")


def test_openapi_documentout_schema_is_shared_by_both_endpoints():
    """两个出口的 $ref **指向同一个** components.schemas 条目（不是各写一份）。"""
    spec = _spec()
    list_node = _response_schema(spec, "/api/documents", "get")["items"]
    upload_node = _response_schema(spec, "/api/documents/upload", "post")
    assert _ref_name(list_node) == _ref_name(upload_node) == "DocumentOut"


# ---------------------------------------------------------------------------
# 字段注解 / 必填性（腿 1 的模型面：改前 None 被静默接受，改后 ValidationError）
# ---------------------------------------------------------------------------


def test_model_field_annotations_match_contract():
    from app.api.chat_schemas import ChatOut, MessageOut
    from app.api.document_routes import DocumentOut

    assert _ann_names(ChatOut.model_fields["created_at"].annotation) == {"datetime"}
    assert _ann_names(ChatOut.model_fields["archived_at"].annotation) == {"datetime", "NoneType"}
    assert _ann_names(MessageOut.model_fields["created_at"].annotation) == {"datetime"}
    assert _ann_names(MessageOut.model_fields["refs"].annotation) == {"list", "dict", "NoneType"}
    assert _ann_names(MessageOut.model_fields["filters"].annotation) == {"dict", "list", "NoneType"}
    assert _ann_names(MessageOut.model_fields["skill_ids"].annotation) == {"list", "NoneType"}
    assert _ann_names(DocumentOut.model_fields["created_at"].annotation) == {"datetime"}
    # 三个 created_at 都**必填**（改前 MessageOut 有 = None 默认，ChatOut/DocumentOut 无默认但吃 None）
    for model, field in ((ChatOut, "created_at"), (MessageOut, "created_at"), (DocumentOut, "created_at")):
        assert model.model_fields[field].is_required(), f"{model.__name__}.{field} 必须必填"


def _document_kwargs() -> dict:
    return dict(
        id=1,
        filename="f.md",
        file_type="md",
        status="ready",
        chunk_count=0,
        vectorized=False,
        vector_error=None,
        category="c",
        tags=None,
        message=None,
        kb_id=1,
    )


def test_models_reject_none_and_bad_created_at():
    """负向判别力：``created_at=None`` / 非日期串**必须** ValidationError。

    改前这三处全被接受（``Any`` / ``Any = None`` / ``object``）——报告里的负对照
    实测给出了同一组调用的「旧注解下绿、新注解下红」逐字结果。
    """
    from app.api.chat_schemas import ChatOut, MessageOut
    from app.api.document_routes import DocumentOut

    good = datetime(2025, 1, 1, 12, 0, 0)
    ChatOut(id=1, title="t", created_at=good)
    MessageOut(id=1, role="assistant", content="x", created_at=good)
    DocumentOut(created_at=good, **_document_kwargs())

    with pytest.raises(ValidationError):
        ChatOut(id=1, title="t", created_at=None)
    with pytest.raises(ValidationError):
        ChatOut(id=1, title="t", created_at="not-a-date")
    with pytest.raises(ValidationError):
        MessageOut(id=1, role="assistant", content="x", created_at=None)
    with pytest.raises(ValidationError):
        MessageOut(id=1, role="assistant", content="x", created_at="not-a-date")
    with pytest.raises(ValidationError):
        DocumentOut(created_at=None, **_document_kwargs())
    with pytest.raises(ValidationError):
        ChatOut(id=1, title="t", **{"created_at": object()})


def test_historical_refs_and_filters_shapes_still_validate():
    """``refs`` / ``filters`` 的历史三形态都必须仍然通过（收紧成 dict 会打红）。"""
    from app.api.chat_schemas import MessageOut

    t = datetime(2025, 1, 1)
    assert MessageOut(id=1, role="assistant", content="x", created_at=t).refs is None
    assert MessageOut(id=1, role="assistant", content="x", created_at=t, refs=[{"type": "document"}]).refs == [
        {"type": "document"}
    ]
    assert MessageOut(id=1, role="assistant", content="x", created_at=t, refs={"legacy": 1}).refs == {"legacy": 1}
    assert MessageOut(id=1, role="assistant", content="x", created_at=t, filters={"kb_ids": [1]}).filters == {
        "kb_ids": [1]
    }
    assert MessageOut(id=1, role="assistant", content="x", created_at=t, filters=["legacy"]).filters == ["legacy"]


# ---------------------------------------------------------------------------
# 腿 2：HTTP 逐值序列化
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_chat_list_created_at_is_iso_and_archived_at_null(client, db):  # noqa: F811
    await repo.create_chat(db, title="S6-189 列表")
    await db.commit()

    resp = await client.get("/api/chats")
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert len(items) == 1
    body = items[0]
    _assert_iso(body["created_at"])
    assert body["archived_at"] is None

    from app.api.chat_schemas import ChatOut

    parsed = ChatOut(**body)  # 收紧后仍能反序列化真实响应
    assert isinstance(parsed.created_at, datetime)
    assert parsed.archived_at is None


@pytest.mark.asyncio
async def test_http_chat_list_archived_at_is_iso_when_set(client, db):  # noqa: F811
    chat = await repo.create_chat(db, title="S6-189 归档")
    chat.archived_at = repo.utc_now()
    await db.commit()

    resp = await client.get("/api/chats?archived=true")
    assert resp.status_code == 200, resp.text
    body = resp.json()[0]
    _assert_iso(body["created_at"])
    _assert_iso(body["archived_at"])

    from app.api.chat_schemas import ChatOut

    assert isinstance(ChatOut(**body).archived_at, datetime)


@pytest.mark.asyncio
async def test_http_chat_detail_message_created_at_and_refs_shapes(client, db):  # noqa: F811
    """会话详情：``created_at`` 是 ISO；``refs``/``filters`` 三形态原样回读。"""
    chat = await repo.create_chat(db, title="S6-189 详情")
    await repo.add_message(db, chat_id=chat.id, role="assistant", content="历史(空)")
    await repo.add_message(
        db,
        chat_id=chat.id,
        role="assistant",
        content="列表形态",
        refs=[{"type": "document", "title": "x"}],
        filters={"kb_ids": [1]},
    )
    await repo.add_message(
        db, chat_id=chat.id, role="assistant", content="字典形态", refs={"legacy": True}, filters=["legacy"]
    )
    await db.commit()

    resp = await client.get(f"/api/chats/{chat.id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _assert_iso(body["created_at"])
    assert body["archived_at"] is None
    msgs = body["messages"]
    assert [m["content"] for m in msgs] == ["历史(空)", "列表形态", "字典形态"]
    assert msgs[0]["refs"] is None and msgs[0]["filters"] is None
    assert msgs[1]["refs"] == [{"type": "document", "title": "x"}] and msgs[1]["filters"] == {"kb_ids": [1]}
    assert msgs[2]["refs"] == {"legacy": True} and msgs[2]["filters"] == ["legacy"]

    from app.api.chat_schemas import MessageOut

    for raw in msgs:
        _assert_iso(raw["created_at"])
        parsed = MessageOut(**raw)
        assert isinstance(parsed.created_at, datetime)
        assert parsed.refs == raw["refs"] and parsed.filters == raw["filters"]


@pytest.mark.asyncio
async def test_http_document_upload_and_list_created_at_is_iso(client):  # noqa: F811
    content = "# S6-189\n\n响应模型的 created_at 必须是 ISO 字符串。\n" * 3
    resp = await client.post(
        "/api/documents/upload",
        files={"file": ("s6-189.md", content.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    _assert_iso(body["created_at"])

    listed = await client.get("/api/documents")
    assert listed.status_code == 200, listed.text
    rows = [row for row in listed.json() if row["id"] == body["id"]]
    assert len(rows) == 1, f"列表里应恰好出现刚落档的文档，实得 {rows!r}"
    _assert_iso(rows[0]["created_at"])

    from app.api.document_routes import DocumentOut

    parsed = DocumentOut(**body)
    assert isinstance(parsed.created_at, datetime)
