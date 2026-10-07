"""条目 1.5.3 契约子集测试：``/api/chat`` 与 ``/api/chat/stream``。

覆盖 02 契约里**属于阶段 1 面**的那些条款：

- §7.1 ChatResponse 九字段、§7.2 ChatRequest 八字段；
- §2 SSE：``data: <JSON>\\n\\n``、``text/event-stream``、``Cache-Control: no-cache``、
  ``X-Accel-Buffering: no``、最小事件序 ``meta → token* → done``；
- §2 行为细节：空 text 且无图 → 400「请输入问题」（**流开始前**）；
- §4 异常语义：会话不存在 → 404「会话不存在」；未配模型 → 400 可读中文。

用 ``httpx.ASGITransport`` 直接打 ASGI app（不起真端口，不与 pytest 抢端口）。
模型一律注入 FauxProvider 驱动（**不连真模型**，统筹 §3.4），DB 用独立测试库。
"""

from __future__ import annotations

import base64
import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api import chat_routes
from app.generation import answer as answer_mod


def faux_model(reply: str, extra_replies: list[str] | None = None):
    """FauxProvider 驱动的真链路模型。

    ``extra_replies`` 用于同一用例里走多轮（例如先澄清追问、再直答）——
    Faux 的响应队列是**按调用顺序消费**的，排少了第二轮会走 error 分支。
    """
    from cubeloop.providers.faux import FauxProvider, faux_assistant_message

    provider = FauxProvider(provider_id="faux")
    provider.set_responses(
        [faux_assistant_message(reply)] + [faux_assistant_message(r) for r in (extra_replies or [])]
    )
    return provider.model("faux-chat")


@pytest.fixture
def use_faux(monkeypatch):
    """把端点里的模型装配换成 Faux（测试不连真模型）。

    同时**把路由的 LLM 兜底分类器也换成假分类器**——否则「正则未命中」的问题
    （如「今天天气怎么样」）会真连模型做意图分类：测试会变慢（每例数秒）、
    依赖网络与配额，且违反统筹 §3.4「单测不连真模型」。
    """

    def _install(reply: str, extra_replies: list[str] | None = None):
        model = faux_model(reply, extra_replies)
        monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
        # 默认判「库外」：与 ROUTING_LLM_FALLBACK=direct 同结果，但零网络调用
        monkeypatch.setattr(
            chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("external")
        )
        return model

    return _install


class _StubClassifier:
    """固定返回某个标签的假分类器（守约实现，不连网络）。"""

    def __init__(self, label: str | None) -> None:
        self.label = label

    async def classify(self, text: str) -> str | None:
        return self.label


@pytest_asyncio.fixture
async def client(db, engine, monkeypatch) -> AsyncClient:
    """ASGI 直连客户端；确保路由/澄清开关为默认开。

    **必须覆盖 ``get_db``**：``app/db/session.py`` 里的 engine 是进程级单例，
    它的连接池绑定在「第一次用到它的那个事件循环」上；而 pytest-asyncio 给每个
    用例一个新循环 —— 直接用全局 engine 会跨环，报
    ``AttributeError: 'NoneType' object has no attribute 'send'``。
    这里改成把请求会话绑到 function 级的测试 engine 上（顺带保证用测试库）。
    """
    from app.core.config import settings
    from app.db.session import get_db
    from app.session import memory as memory_mod
    from app.usage import recorder
    from main import app
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", True)
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", True)
    monkeypatch.setattr(settings, "ROUTING_MODE", "regex")

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        try:
            await db.rollback()
            await memory_mod.drain_memory_tasks()
            await recorder.drain_pending()
            await chat_routes.drain_cache_tasks()
        finally:
            app.dependency_overrides.pop(get_db, None)


def parse_sse(raw: str) -> list[dict]:
    """把 SSE 正文解析成事件列表。"""
    events = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block.startswith("data: "):
            continue
        events.append(json.loads(block[len("data: ") :]))
    return events


# --------------------------------------------------------------------------
# 契约 §7.2 ChatRequest 字段 + §7.1 ChatResponse 字段
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_request_accepts_all_contract_fields(client, use_faux):
    """ChatRequest 八字段全部接受（未实装的字段不报错、不静默丢结构）。"""
    use_faux("好的。")
    resp = await client.post(
        "/api/chat",
        json={
            "message": "今天天气怎么样",
            "chat_id": None,
            "image_data": None,
            "images": None,
            "filters": {"kb_ids": [1], "categories": ["项目"]},
            "skill_ids": ["builtin:knowledge"],
            "agent_mode": "off",
            "request_id": "req-1",
        },
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_chat_response_has_nine_contract_fields(client, use_faux):
    """ChatResponse 九字段齐备（02 §7.1）。"""
    use_faux("今天晴。")
    resp = await client.post("/api/chat", json={"message": "今天天气怎么样"})
    assert resp.status_code == 200, resp.text

    body = resp.json()
    expected = {
        "chat_id",
        "message",
        "refs",
        "kind",
        "reasoning",
        "thinking",
        "skill_ids",
        "agent_trace",
        "answer_source",
    }
    assert set(body) == expected, f"字段不匹配：多 {set(body) - expected} / 少 {expected - set(body)}"
    # 非流式 /chat 的 agent_trace 恒为 None（契约明文）
    assert body["agent_trace"] is None
    # 阶段 1 无思考捕获
    assert body["reasoning"] == ""
    assert body["thinking"] is None
    assert body["refs"] == []
    assert body["kind"] == "direct"
    assert body["answer_source"] == "general"


# --------------------------------------------------------------------------
# 契约 §2 / §4 行为细节
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_message_400_before_stream(client, use_faux):
    """空 text 且无图 → 400「请输入问题」（流开始前，不吐半截 SSE）。"""
    use_faux("不该被调用")

    for path in ("/api/chat", "/api/chat/stream"):
        resp = await client.post(path, json={"message": ""})
        assert resp.status_code == 400, f"{path} 应 400"
        assert "请输入问题" in resp.json()["detail"]
        # 流式：必须是普通 JSON 错误，而不是 SSE 帧
        assert "text/event-stream" not in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_whitespace_only_message_400(client, use_faux):
    """纯空白也算空输入（老基线 strip 口径）。"""
    use_faux("不该被调用")
    resp = await client.post("/api/chat", json={"message": "   \n  "})
    assert resp.status_code == 400
    assert "请输入问题" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_request_id_longer_than_the_column_is_rejected(client, use_faux):
    """`回归修复`：`request_id` 上限 = 64（与 `session_events.request_id` 列宽一致）。

    超长不拦的后果：MySQL 严格模式下抛 `DataError`（**非** `IntegrityError` ⇒ 不重试）被
    `safe_append_event` 静默吞掉；SQLite 不强制长度 ⇒ 同一输入两种方言两种结果。
    所以这里让它**在校验层就红**。
    """
    use_faux("你好")

    too_long = await client.post("/api/chat", json={"message": "你好", "request_id": "x" * 65})
    assert too_long.status_code == 422, too_long.text

    boundary = await client.post("/api/chat", json={"message": "你好", "request_id": "x" * 64})
    assert boundary.status_code == 200, boundary.text


@pytest.mark.asyncio
async def test_note_only_scope_no_longer_refuses_as_unsupported(client, use_faux):
    """`回归修复`：B-lite（4.5.2）落地后，**「只请求笔记」不再是不可满足的范围**。

    这条用例的前身是 `test_note_only_scope_refuses_with_a_clear_reason`（阶段 3 收尾的
    A+C 处置）：那时 v2 只有文档来源 ⇒ 切「仅笔记」必然零命中，于是按 §3.8「范围空」拒答，
    免得静默落库外直答。4.5.2 把笔记接进**关键词路**之后，`source_types=["note"]`
    已经是可以真去检索的范围——**空库下没命中属「库内未命中」**（默认档走 direct），
    不再走「范围空」拒答。「范围空」这条判定保留给库内根本没有的来源
    （如 `source_types=["web"]`），见 `tests/api/test_notes_4_5_2.py`。
    """
    use_faux("库外直答")

    resp = await client.post(
        "/api/chat", json={"message": "笔记里怎么说？", "filters": {"source_types": ["note"]}}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "direct", "范围可满足但没命中 ⇒ 库内未命中行（默认档 direct），不是拒答"
    assert body["answer_source"] == "general"
    assert body["refs"] == []


@pytest.mark.asyncio
async def test_scope_with_documents_is_unaffected(client, use_faux):
    """负向对照：范围里**含文档**时不得触发上一条的拒答分支（否则等于把正常路也关了）。"""
    use_faux("库外直答")

    resp = await client.post(
        "/api/chat",
        json={"message": "笔记里怎么说？", "filters": {"source_types": ["note", "document"]}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "direct", resp.text


@pytest.mark.asyncio
async def test_image_too_large_413(client, use_faux):
    """图片超限 → 413「文件超过限制，请压缩后重试」。

    S5 · 回归修复 的**等价强化**（登记项，非放宽）：旧样本 ``"x" * (MAX_UPLOAD_BYTES + 1)``
    是「字符数超限」的假样本——判据改为「解码后字节数」后它会变成合法输入，本用例的 413
    断言就会恒绿失效。这里换成**真超限**样本：解码后恰 ``MAX_UPLOAD_BYTES`` + 1 字节的
    base64 data URL。断言面（413 + 文案）与旧样本逐字不变。
    """
    use_faux("不该被调用")
    huge = "data:image/png;base64," + base64.b64encode(
        b"\x5a" * (chat_routes.MAX_UPLOAD_BYTES + 1)
    ).decode("ascii")
    resp = await client.post("/api/chat", json={"message": "看图", "images": [huge]})
    assert resp.status_code == 413
    assert "文件超过限制，请压缩后重试" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_missing_chat_404(client, use_faux):
    """会话不存在 → 404「会话不存在」。"""
    use_faux("不该被调用")
    resp = await client.post("/api/chat", json={"message": "在吗", "chat_id": 999999})
    assert resp.status_code == 404
    assert "会话不存在" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_bad_agent_mode_400_before_stream(client, use_faux):
    """非法 agent_mode → 400（流开始前定死，不吐半截 SSE）。"""
    use_faux("不该被调用")
    resp = await client.post(
        "/api/chat/stream", json={"message": "hi", "agent_mode": "bad_mode"}
    )
    assert resp.status_code == 400
    assert "Agent 模式" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_unconfigured_model_400(client, monkeypatch):
    """未配置对话模型 → 400（可读中文，不是 500）。"""
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: None)
    resp = await client.post("/api/chat", json={"message": "你好"})
    assert resp.status_code == 400
    assert "尚未配置大模型" in resp.json()["detail"]


# --------------------------------------------------------------------------
# SSE 最小协议：meta → token* → done
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_minimal_event_sequence(client, use_faux):
    """最小事件序：meta → token* → done（02 §2）。"""
    use_faux("知澜是一个本地知识库客户端。" * 6)
    resp = await client.post("/api/chat/stream", json={"message": "今天天气怎么样"})
    assert resp.status_code == 200, resp.text

    events = parse_sse(resp.text)
    types = [e["type"] for e in events]
    assert types[0] == "meta", types
    assert types[-1] == "done", types
    assert "token" in types
    # 中间只允许 token（阶段 1 无 reasoning/agent 事件）
    assert set(types) <= {"meta", "token", "done"}, types


@pytest.mark.asyncio
async def test_stream_meta_fields(client, use_faux):
    """meta 事件带 chat_id 与 skill_ids（契约 §2）。"""
    use_faux("好。")
    resp = await client.post("/api/chat/stream", json={"message": "今天天气怎么样"})
    meta = parse_sse(resp.text)[0]
    assert meta["type"] == "meta"
    assert isinstance(meta["chat_id"], int)
    assert meta["skill_ids"] == []


@pytest.mark.asyncio
async def test_stream_done_has_nine_business_fields(client, use_faux):
    """done 事件：type + 九个业务字段（契约 §2「十字段」）。

    🔴 2026-09-27：`回归修复` 拍板⑨「补」⇒ 新增第 9 个业务字段 `recovered`
    （该会话是否存在恢复痕迹，前端据此免刷新拉取；02 v1.0.12）。原「八业务字段」
    用例名随实现更新（原九字段口径见 02 变更记录）。
    """
    use_faux("好。")
    resp = await client.post("/api/chat/stream", json={"message": "今天天气怎么样"})
    done = parse_sse(resp.text)[-1]

    expected = {
        "type",
        "chat_id",
        "refs",
        "kind",
        "reasoning",
        "thinking",
        "skill_ids",
        "agent_trace",
        "answer_source",
        "recovered",
    }
    assert set(done) == expected, f"多 {set(done)-expected} / 少 {expected-set(done)}"
    assert done["type"] == "done"
    assert done["kind"] == "direct"
    assert done["answer_source"] == "general"
    assert done["recovered"] is False
    assert done["refs"] == []


@pytest.mark.asyncio
async def test_stream_tokens_reassemble_to_done_answer(client, use_faux):
    """token 拼接 == 落库正文（不攒批、不丢字）。"""
    text = "知澜是一个本地知识库客户端。" * 6
    use_faux(text)
    resp = await client.post("/api/chat/stream", json={"message": "今天天气怎么样"})

    events = parse_sse(resp.text)
    joined = "".join(e["text"] for e in events if e["type"] == "token")
    assert joined == text


@pytest.mark.asyncio
async def test_turn_is_persisted_with_source_type_and_kind(client, use_faux, db):
    """落库校验：一问一答都进 MySQL，且 source_type / kind / answer_source 正确。"""
    from sqlalchemy import select

    from app.models import Message

    use_faux("今天晴，气温 20 度。")
    resp = await client.post("/api/chat", json={"message": "今天天气怎么样"})
    chat_id = resp.json()["chat_id"]

    rows = list(
        (await db.execute(select(Message).where(Message.chat_id == chat_id).order_by(Message.id)))
        .scalars()
        .all()
    )
    assert [r.role for r in rows] == ["user", "assistant"]
    assert rows[0].content == "今天天气怎么样"
    assert rows[1].content == "今天晴，气温 20 度。"
    # §3.8：库外 → direct / general
    assert rows[1].kind == "direct"
    assert rows[1].answer_source == "general"
    assert rows[1].source_type == "external"
    assert rows[0].source_type == "external"
    assert rows[1].refs == []


@pytest.mark.asyncio
async def test_continue_same_chat_keeps_one_chat(client, use_faux, db):
    """续问复用同一会话（chat_id 相同、消息累加、标题不变）。"""
    from sqlalchemy import select

    from app.models import Chat, Message

    use_faux("第一答。", extra_replies=["第二答。"])
    first = await client.post("/api/chat", json={"message": "第一个问题"})
    chat_id = first.json()["chat_id"]

    second = await client.post("/api/chat", json={"message": "第二个问题", "chat_id": chat_id})
    assert second.json()["chat_id"] == chat_id

    rows = list(
        (await db.execute(select(Message).where(Message.chat_id == chat_id).order_by(Message.id)))
        .scalars()
        .all()
    )
    assert [(r.role, r.content) for r in rows] == [
        ("user", "第一个问题"),
        ("assistant", "第一答。"),
        ("user", "第二个问题"),
        ("assistant", "第二答。"),
    ]
    chat = await db.get(Chat, chat_id)
    assert chat.title == "第一个问题", "标题取首问且不随续问改写"


@pytest.mark.asyncio
async def test_clarify_rounds_counter_increments_and_resets(client, use_faux, db):
    """澄清轮计数：澄清则 +1，非澄清则清零（老基线口径）。

    注意：MySQL 默认 **REPEATABLE READ**，``db`` 会话的快照在第一次读时就定下了，
    端点在**另一个会话**里提交的新值它看不见。所以每次校验前先 ``rollback()``
    结束当前事务，下一次读才会拿新快照。
    """
    from app.models import Chat

    use_faux("你指的是哪份文档？", extra_replies=["好的。"])
    first = await client.post("/api/chat", json={"message": "那个怎么样"})
    chat_id = first.json()["chat_id"]
    assert first.json()["kind"] == "clarify"

    await db.rollback()
    chat = await db.get(Chat, chat_id)
    assert chat.clarify_rounds == 1, "澄清轮应 +1"

    await client.post("/api/chat", json={"message": "今天天气怎么样", "chat_id": chat_id})
    await db.rollback()
    chat = await db.get(Chat, chat_id)
    assert chat.clarify_rounds == 0, "非澄清轮应清零"


@pytest.mark.asyncio
async def test_stream_is_incremental_not_batched(client, use_faux):
    """**不攒批**：长回答必须切出多个 token 帧（坑清单硬口径）。"""
    use_faux("知澜是一个本地知识库客户端。" * 12)
    resp = await client.post("/api/chat/stream", json={"message": "今天天气怎么样"})

    tokens = [e for e in parse_sse(resp.text) if e["type"] == "token"]
    assert len(tokens) > 1, f"token 帧只有 {len(tokens)} 个 → 疑似攒批输出"


@pytest.mark.asyncio
async def test_stream_headers_and_media_type(client, use_faux):
    """SSE 响应头：text/event-stream + no-cache + X-Accel-Buffering（契约 §2）。"""
    use_faux("好。")
    resp = await client.post("/api/chat/stream", json={"message": "今天天气怎么样"})

    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers.get("cache-control") == "no-cache"
    assert resp.headers.get("x-accel-buffering") == "no"


@pytest.mark.asyncio
async def test_stream_frames_are_utf8_unscaped(client, use_faux):
    """SSE 帧的中文不转义（``ensure_ascii=False`` 是契约的一部分）。"""
    use_faux("今天晴。")
    resp = await client.post("/api/chat/stream", json={"message": "今天天气怎么样"})

    assert "今天晴" in resp.text, "中文被转义了"
    assert "\\u" not in resp.text


@pytest.mark.asyncio
async def test_stream_each_frame_is_data_prefixed(client, use_faux):
    """每帧形如 ``data: <JSON>\\n\\n``（契约 §2）。"""
    use_faux("好。")
    resp = await client.post("/api/chat/stream", json={"message": "今天天气怎么样"})

    for block in [b for b in resp.text.split("\n\n") if b.strip()]:
        assert block.startswith("data: "), block
        json.loads(block[len("data: ") :])


# --------------------------------------------------------------------------
# 分路接线（1.2 / 1.3 / 1.4 通过 HTTP 真的生效）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clarify_over_http(client, use_faux):
    """模糊提问 → kind=clarify（1.4 接线生效）。"""
    use_faux("你指的是哪份文档？")
    resp = await client.post("/api/chat", json={"message": "那个怎么样"})

    body = resp.json()
    assert body["kind"] == "clarify"
    assert body["answer_source"] == "clarify"
    assert body["refs"] == []


@pytest.mark.asyncio
async def test_internal_question_routes_to_direct_when_no_retrieval(client, use_faux):
    """库内指令 + 阶段 1 无检索 + 开关开 → direct/general（§3.8 第 2 行）。"""
    use_faux("根据我自己的知识……")
    resp = await client.post("/api/chat", json={"message": "知识库里关于项目周报的内容"})

    body = resp.json()
    assert body["kind"] == "direct"
    assert body["answer_source"] == "general"
    assert body["refs"] == []


@pytest.mark.asyncio
async def test_internal_question_refuses_when_direct_disabled(client, use_faux, monkeypatch):
    """库内指令 + 开关关 → refuse/empty_scope（不调模型）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", False)
    use_faux("不该被调用")

    resp = await client.post("/api/chat", json={"message": "知识库里关于项目周报的内容"})
    body = resp.json()
    assert body["kind"] == "refuse"
    assert body["answer_source"] == "empty_scope"
    assert body["message"] == "资料中没有提到。"
    assert body["refs"] == []


@pytest.mark.asyncio
async def test_off_mode_goes_external(client, use_faux, monkeypatch):
    """ROUTING_MODE=off → 视为库外 → direct（1.2 三态经 HTTP 生效）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "ROUTING_MODE", "off")
    use_faux("库外回答。")

    resp = await client.post("/api/chat", json={"message": "知识库里有什么"})
    body = resp.json()
    assert body["kind"] == "direct"
    assert body["answer_source"] == "general"


@pytest.mark.asyncio
async def test_routing_llm_path_is_reachable_over_http(client, monkeypatch):
    """正则未命中时经 HTTP 走 LLM 分类（1.2 兜底接线生效，用假分类器零网络）。"""
    from app.api import chat_routes as routes
    from app.core.config import settings

    monkeypatch.setattr(settings, "ROUTING_MODE", "regex")
    model = faux_model("库内回答。")
    monkeypatch.setattr(routes, "resolve_chat_model", lambda: model)
    # 假分类器判「库内」→ 阶段 1 无检索 → §3.8 第 2 行落 direct
    monkeypatch.setattr(
        routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db")
    )

    resp = await client.post("/api/chat", json={"message": "帮我梳理下周安排"})
    body = resp.json()
    assert body["kind"] == "direct", "库内未命中 + 允许直答 → direct"
    assert body["answer_source"] == "general"


@pytest.mark.asyncio
async def test_test_path_never_assembles_real_model(client, use_faux, monkeypatch):
    """守门用例：测试路径**不得**真装配模型/分类器（统筹 §3.4「单测不连真模型」）。

    做法：**先装上 Faux 注入**（use_faux），再把「真装配」的两个入口换成一旦被调用
    就炸的桩，然后走一遍完整问答（含澄清路径）。只要端点哪次改动绕过了注入点去真连
    上游（测试会变慢、依赖网络与配额），本用例立刻红——这是防回归的守门人。
    """
    # 三轮调用：① 直答 ② 澄清追问 ③ 流式直答
    use_faux("好的。", extra_replies=["你指的是哪份文档？", "好的。"])

    from app.generation import answer as gen_answer
    from app.routing import llm as routing_llm

    def _boom():  # pragma: no cover - 正常路径不会执行
        raise AssertionError("测试路径不得真装配模型（会真连上游）")

    monkeypatch.setattr(gen_answer, "build_chat_model", _boom)
    monkeypatch.setattr(routing_llm, "build_classifier", _boom)

    # ① 常规路径（正则未命中 → 假分类器）
    resp = await client.post("/api/chat", json={"message": "今天天气怎么样"})
    assert resp.status_code == 200
    assert resp.json()["kind"] == "direct"

    # ② 澄清路径（追问生成也必须走注入的模型）
    resp2 = await client.post("/api/chat", json={"message": "那个怎么样"})
    assert resp2.status_code == 200
    assert resp2.json()["kind"] == "clarify"

    # ③ 流式路径
    resp3 = await client.post("/api/chat/stream", json={"message": "今天天气怎么样"})
    assert resp3.status_code == 200
    assert parse_sse(resp3.text)[0]["type"] == "meta"


# --------------------------------------------------------------------------
# I9 路由可观测：**每一轮**都要有且只有一条决策日志
# （回归修复 回归：澄清轮原本不写决策）
# --------------------------------------------------------------------------


async def _decisions_for(db, chat_id: int):
    """读某会话的 I9 决策日志（先 rollback 拿新快照，避开 REPEATABLE READ）。"""
    from app.routing import decisions as routing_decisions

    await db.rollback()
    return await routing_decisions.load_decisions(db, chat_id)


@pytest.mark.asyncio
async def test_every_turn_has_exactly_one_routing_decision(client, use_faux, db):
    """三类轮次**都**留下决策，且每轮恰好一条（I9 硬要求）。

    这是 回归修复 的回归用例：澄清轮短路了路由，但「因为要澄清所以没路由」
    本身就是该轮的路由决策——不记的话事件流里分不清「澄清短路」与「事件写失败」。
    """
    use_faux(
        "第一答。",
        extra_replies=["你指的是哪份文档？", "第二答。"],
    )

    # ① 库外（正则未命中 → 假分类器判 external）
    r1 = await client.post("/api/chat", json={"message": "今天天气怎么样"})
    chat_id = r1.json()["chat_id"]
    assert r1.json()["kind"] == "direct"

    # ② 澄清（短路路由）
    r2 = await client.post("/api/chat", json={"message": "那个怎么样", "chat_id": chat_id})
    assert r2.json()["kind"] == "clarify"

    # ③ 库内指令（正则命中）
    r3 = await client.post(
        "/api/chat", json={"message": "知识库里关于项目周报的内容", "chat_id": chat_id}
    )
    assert r3.json()["kind"] == "direct"

    decisions = await _decisions_for(db, chat_id)
    assert len(decisions) == 3, f"三轮应有 3 条决策，实际 {len(decisions)}：{decisions}"
    assert [d.source for d in decisions] == ["llm", "clarify", "regex"], (
        f"决策来源不符：{[(d.source, d.source_type) for d in decisions]}"
    )


@pytest.mark.asyncio
async def test_clarify_decision_is_recorded_and_queryable(client, use_faux, db):
    """澄清轮的决策可查询，且 source_type 落在两值词汇表内。"""
    from app.routing import decision as routing_decision

    use_faux("你指的是哪份文档？")
    resp = await client.post("/api/chat", json={"message": "那个怎么样"})
    chat_id = resp.json()["chat_id"]

    decisions = await _decisions_for(db, chat_id)
    assert len(decisions) == 1
    d = decisions[0]
    assert d.source == "clarify", "澄清轮必须以 clarify 来源留痕"
    assert d.source_type in ("internal_db", "external"), "source_type 必须在契约词汇表内"
    assert d.source_type == "external", "澄清轮没查库 → external"
    # 纯函数口径自检
    assert routing_decision.clarify_decision().source == "clarify"


@pytest.mark.asyncio
async def test_decision_count_matches_turn_count(client, use_faux, db):
    """决策条数 == 轮数（不重不漏）：与 turn/start 事件数对齐。"""
    from sqlalchemy import select

    from app.models import SessionEvent
    from app.session import events as event_log

    use_faux("答一。", extra_replies=["答二。"])
    r1 = await client.post("/api/chat", json={"message": "今天天气怎么样"})
    chat_id = r1.json()["chat_id"]
    await client.post("/api/chat", json={"message": "今天冷吗", "chat_id": chat_id})

    await db.rollback()
    turns = list(
        (
            await db.execute(
                select(SessionEvent).where(
                    SessionEvent.chat_id == chat_id,
                    SessionEvent.type == "turn/start",
                )
            )
        )
        .scalars()
        .all()
    )
    decisions = await _decisions_for(db, chat_id)
    assert len(decisions) == len(turns) == 2
