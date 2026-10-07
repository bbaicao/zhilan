"""A12 / A13 / A19：推理参数的**装配面**与**隔离面**（开工包 §4 的观测纪律）。

## 这份文件为什么必须「真装配 + 真 OpenAIProvider」

开工包 §4 把观测手法钉死成两层：

1. **交给 SDK 的最终参数**（有没有思考键）—— 可以直接断**最终 HTTP 请求体**；
2. **最终 HTTP 请求体**（主张「验证了 HTTP 请求体」时必须）—— 要断**序列化后的 JSON**，
   且厂商字段在**顶层**（`body["thinking"]["type"]`），**不是**被包了一层 `extra_body`。

所以这里**不用 FauxProvider**（它证明不了 `extra_body` 的合并行为），
也**不用 `StreamOptions.on_payload`**（它在 `extra_body` 合并**之前**跑，
`cubeloop/providers/openai.py:129` → `:131-137`，看不见要验的键）。
本文件的做法是：**真走 `answer.build_chat_model()` 装配**（真 `OpenAIProvider`），
只在**发送边界**给 SDK 的 httpx 客户端换成 `httpx.MockTransport` —— 于是拦到的是
**序列化之后的 wire payload**，`extra_body` 的合并、字段落点、`max_tokens` 全都能验。

## 为什么本文件**不能**继承 `tests/api/conftest.py`

那个目录的 `pin_aux_model_delegates` 会把 `resolve_aux_model` 钉成跟随
`resolve_chat_model`（既有用例的兼容层）—— 而 A12–A15 要验的**正是真实装配**，
被它覆盖就什么都没验到。所以本文件**不放进** `tests/api/`，只从那里借
`client` 夹具（ASGI 直连 + 真测试库），自己带全部隔离。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from app.api import chat_routes
from app.core.config import settings
from app.generation import answer as answer_mod
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401 —— client 是夹具
from tests.retrieval.conftest import seed_document

pytestmark = pytest.mark.asyncio

BASE_URL = "https://wire.invalid/v1"
MODEL = "mimo-v2.5"
SERVICE = {
    "id": "svc_wire",
    "name": "wire",
    "provider": "mimo",
    "base_url": BASE_URL,
    "api_key": "sk-wire",
    "models": [MODEL],
    "model_roles": {MODEL: ["chat"]},
    "for_roles": ["chat"],
}
LEVEL_DISABLED = '{"param":"thinking.type","value":"disabled","model":"' + MODEL + '"}'
LEVEL_ENABLED = '{"param":"thinking.type","value":"enabled","model":"' + MODEL + '"}'

#: 出网工具：CubeLoop / openai-sdk / httpx 各自的出网口，一律记一笔（期望恒为空）
OUTBOUND_ATTEMPTS: list[str] = []


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _sse_chunk(delta: dict, *, finish: str | None = None) -> str:
    payload = {
        "id": "chatcmpl-wire",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _wire_sse(text: str) -> str:
    """OpenAI 兼容的**流式**响应体（`stream=true` 时厂商回的就是这个形状）。"""
    return (
        _sse_chunk({"role": "assistant", "content": text})
        + _sse_chunk({}, finish="stop")
        + "data: [DONE]\n\n"
    )


def _wire_sse_tool_call(name: str, args: dict) -> str:
    """流式响应里的一次**工具调用**（CubeLoop 的决策循环吃的是这个形状）。

    ⚠️ `arguments` 必须是**字符串**（OpenAI 的流式约定），且 `function.name` 与 `index`
    要齐——少了任何一样，CubeLoop 攒出来的工具调用都解析不出来，
    循环会**静默不做任何工具步骤**（本文件踩过一次：agent 帧一个都不出）。
    """
    call = {
        "index": 0,
        "id": "call_wire_1",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }
    return (
        _sse_chunk({"role": "assistant", "tool_calls": [call]})
        + _sse_chunk({}, finish="tool_calls")
        + "data: [DONE]\n\n"
    )


def _wire_json(content: str, tool: dict | None = None) -> dict:
    """OpenAI 兼容的**非流式**响应体（Agent 决策 / 计划调用走的就是这个形状）。"""
    message: dict = {"role": "assistant", "content": content}
    if tool is not None:
        message["tool_calls"] = [tool]
    return {
        "id": "chatcmpl-wire",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _tool_call(name: str, args: dict) -> dict:
    return {
        "id": "call_wire_1",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


class Wire:
    """假传输（`httpx.MockTransport`）：按响应队列回**真实厂商形状**的 HTTP 响应。

    每次请求都把**反序列化后的请求体**记下来 —— 这就是「序列化之后的 wire payload」。
    """

    def __init__(self, *responses: dict | str) -> None:
        self.queue = list(responses)
        self.bodies: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(BASE_URL), f"意外的出网目标：{request.url}"
        self.bodies.append(json.loads(request.content.decode("utf-8")))
        assert self.queue, "响应队列空了（说明被测代码多调了一次模型）"
        item = self.queue.pop(0)
        if isinstance(item, str):
            return httpx.Response(
                200, text=item, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=item, headers={"content-type": "application/json"})

    @property
    def streams(self) -> list[dict]:
        return [body for body in self.bodies if body.get("stream")]

    @property
    def nonstreams(self) -> list[dict]:
        return [body for body in self.bodies if not body.get("stream")]

    def request_body(self, index: int = 0) -> dict:
        assert self.bodies, "一次模型请求都没有发生"
        return self.bodies[index]


def _attach(wire: Wire, monkeypatch) -> SimpleNamespace:
    """真装配两个入口，并把假传输挂到**同一个** Provider 内部的 SDK 客户端上。

    ⚠️ **两件事必须同时做，缺一就会真出网**：

    1. **真装配**：`answer.build_chat_model()` / `build_aux_model()` 真跑一遍
       （真 `OpenAIProvider`、真 `extra_body` 合并）；换掉 Provider 就把被测行为换掉了；
    2. **把端点里的装配入口钉到同一个对象上**：`resolve_chat_model()` 若不被替换，
       它会在**请求里**重新装配一个新 Provider（没有假传输）⇒ 真出网。
       本文件（`tests/` 顶层）**不继承** `tests/api/conftest.py` 的
       `pin_aux_model_delegates`，端点里那两处必须在这里显式钉住。

    返回 :class:`SimpleNamespace`：``wire`` / ``chat`` / ``aux`` / ``provider``。
    """
    chat = answer_mod.build_chat_model()
    aux = answer_mod.build_aux_model()
    if chat is None:
        pytest.fail("真装配返回了 None —— 用例的配置没生效（A12–A15 需要真 Provider）")
    for bound in (chat, aux):
        bound.provider._client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(wire.handler)
        )
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: chat)
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: aux)
    return SimpleNamespace(wire=wire, chat=chat, aux=aux, provider=chat.provider)


@pytest.fixture(autouse=True)
def _reset_outbound(monkeypatch):
    """本文件自带的禁网钉子 + 三条隔离（重排 / 图通道 / 记忆）。"""
    from app.retrieval import graph as graph_mod
    from app.retrieval import vector as retrieval_vector
    from app.vector.embed import FakeEmbedding

    OUTBOUND_ATTEMPTS.clear()

    class _EmptyStore:
        def query(self, vector, limit: int = 5, filters=None):  # noqa: ANN001
            return []

    monkeypatch.setattr(
        retrieval_vector, "get_embedding_client", lambda *, fake=False: FakeEmbedding(dim=16)
    )
    monkeypatch.setattr(retrieval_vector, "get_vector_store", lambda **kwargs: _EmptyStore())
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", False)
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", False)
    monkeypatch.setattr(settings, "MEMORY_ENABLE", False)
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", False)
    monkeypatch.setattr(settings, "WEB_ENABLE", False)
    monkeypatch.setattr(settings, "AGENT_MODE", "off")
    graph_mod.reset_counters()
    yield
    assert OUTBOUND_ATTEMPTS == [], f"发生了出网：{OUTBOUND_ATTEMPTS}"
    graph_mod.reset_counters()


@pytest.fixture
def wire_env(monkeypatch):
    """真配置（mimo + 真 base_url）+ 路由分类器钉成 internal_db（走真检索）。"""
    monkeypatch.setattr(settings, "MODEL_SERVICES", _dump([SERVICE]))
    monkeypatch.setattr(
        settings, "MODEL_ROLES", _dump({"chat": {"service_id": "svc_wire", "model": MODEL}})
    )
    monkeypatch.setattr(settings, "CHAT_REASONING", "")
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db")
    )
    # 真装配两个入口（本文件不继承 tests/api 的 pin_aux_model_delegates，这里是显式口径）
    monkeypatch.setattr(chat_routes, "resolve_chat_model", answer_mod.build_chat_model)
    monkeypatch.setattr(chat_routes, "resolve_aux_model", answer_mod.build_aux_model)
    return settings


async def _seed(db) -> None:  # noqa: ANN001
    """造**与提问关键词命中**的真材料。

    ⚠️ 这一条不是凑数：`source_type=internal_db` 且**零命中**时，`source_map` 会按
    `CHAT_DIRECT_ANSWER_ENABLE` 判成 `direct` ⇒ **压根不调模型** ⇒ 本文件全部
    「请求体”断言会失去判据（`wire.bodies` 空，断言却还看得过去）。
    所以材料里必须带提问的关键词，让这一轮真的走到模型。
    """
    await seed_document(
        db,
        [
            "思考档位的真材料：这一轮必须命中才可能走到模型。",
            "推理参数隔离用的真材料：回答链路与辅助链路的请求体都要看。",
        ],
        filename="隔离测试.md",
    )


async def _ask(client, message: str, *, agent_mode: str | None = None) -> list[dict]:  # noqa: ANN001
    payload: dict = {"message": message}
    if agent_mode:
        payload["agent_mode"] = agent_mode
    resp = await client.post("/api/chat/stream", json=payload)
    assert resp.status_code == 200, resp.text
    events = parse_sse(resp.text)
    kinds = [event["type"] for event in events]
    assert "done" in kinds, f"没有 done 帧：{kinds}"
    return events


def _require_stream(wire: Wire, index: int = 0) -> dict:
    """取第 ``index`` 条**回答**请求体，且先断言真的发生了这么多条流式请求。"""
    assert len(wire.streams) > index, f"回答链路没发出流式请求：{[sorted(b) for b in wire.bodies]}"
    return wire.streams[index]


# --- A12：回答链路（最终答案按厂商原样带思考参数）-------------------------------------------


async def test_a12_answer_path_empty_level_sends_no_thinking_key(client, db, monkeypatch, wire_env):
    """空档位 ⇒ 最终答案的请求体里**一个思考键都没有**（与改动前逐字一致）。"""
    await _seed(db)
    wire = _attach(Wire(_wire_sse("直答正文。")), monkeypatch).wire

    await _ask(client, "思考档位的真材料")

    body = wire.request_body()
    assert body["stream"] is True
    assert "thinking" not in body, body.keys()
    assert "enable_thinking" not in body, body.keys()
    assert "reasoning_effort" not in body, body.keys()
    assert "extra_body" not in body, "厂商字段必须在**顶层**，不许被包一层 extra_body"
    assert wire.queue == []


async def test_a12_answer_path_disabled_level_lands_on_top_level(client, db, monkeypatch, wire_env):
    """`disabled @ mimo-v2.5` ⇒ wire payload 顶层真的带 `{"thinking":{"type":"disabled"}}`。"""
    await _seed(db)
    monkeypatch.setattr(settings, "CHAT_REASONING", LEVEL_DISABLED)
    wire = _attach(Wire(_wire_sse("直答正文。")), monkeypatch).wire

    events = await _ask(client, "思考档位的真材料")

    body = wire.request_body()
    assert body["thinking"] == {"type": "disabled"}, body
    assert body["model"] == MODEL
    assert "extra_body" not in body
    assert wire.queue == []
    assert [e["type"] for e in events][-1] == "done"


async def test_a12_answer_path_enabled_level_is_sent_verbatim(client, db, monkeypatch, wire_env):
    """显式 `enabled` ⇒ 原样发（厂商默认档被显式选中时也照发，用户的选择就是用户的选择）。"""
    await _seed(db)
    monkeypatch.setattr(settings, "CHAT_REASONING", LEVEL_ENABLED)
    wire = _attach(Wire(_wire_sse("直答正文。")), monkeypatch).wire

    await _ask(client, "思考档位的真材料")

    assert wire.request_body()["thinking"] == {"type": "enabled"}


async def test_a12_answer_path_level_written_for_another_model_is_not_sent(
    client, db, monkeypatch, wire_env
):
    """档位是写给**别的型号**的 ⇒ 不发（否则「给 MiMo 关思考」会被塞给不认识它的厂商）。"""
    await _seed(db)
    monkeypatch.setattr(
        settings,
        "CHAT_REASONING",
        '{"param":"thinking.type","value":"disabled","model":"mimo-v2.5-asr"}',
    )
    wire = _attach(Wire(_wire_sse("直答正文。")), monkeypatch).wire

    await _ask(client, "思考档位的真材料")

    assert "thinking" not in wire.request_body(), wire.request_body()


async def test_a12_bool_param_lands_flat_on_top_level(client, db, monkeypatch, wire_env):
    """千问系的 `enable_thinking` 是**扁平**顶层布尔（不是嵌套对象）。"""
    await _seed(db)
    monkeypatch.setattr(
        settings,
        "MODEL_SERVICES",
        _dump(
            [
                {
                    "id": "svc_q",
                    "name": "qwen",
                    "provider": "dashscope",
                    "base_url": BASE_URL,
                    "api_key": "sk-wire",
                    "models": ["qwen-plus"],
                    "model_roles": {"qwen-plus": ["chat"]},
                    "for_roles": ["chat"],
                }
            ]
        ),
    )
    monkeypatch.setattr(
        settings, "MODEL_ROLES", _dump({"chat": {"service_id": "svc_q", "model": "qwen-plus"}})
    )
    monkeypatch.setattr(
        settings, "CHAT_REASONING", '{"param":"enable_thinking","value":false,"model":"qwen-plus"}'
    )
    wire = _attach(Wire(_wire_sse("百炼直答。")), monkeypatch).wire

    await _ask(client, "思考档位的真材料")

    body = wire.request_body()
    assert body["enable_thinking"] is False, body
    assert "extra_body" not in body
    # 取值的**原样透传**：`PUT` 侧收到字符串 "true" 也会写成 JSON 的 `true`（不是 `"true"`）
    monkeypatch.setattr(
        settings, "CHAT_REASONING", '{"param":"enable_thinking","value":true,"model":"qwen-plus"}'
    )
    wire2 = _attach(Wire(_wire_sse("百炼直答。")), monkeypatch).wire
    await _ask(client, "思考档位的真材料")
    assert wire2.request_body()["enable_thinking"] is True, wire2.request_body()


# --- A13：Agent 决策 / 计划调用**不带**思考键（react 与 plan_execute 各跑一轮）-----------------


async def test_a13_react_decision_call_has_no_thinking_key_and_budget_600(
    client, db, monkeypatch, wire_env
):
    """`react`：① 决策调工具 ② 收尾 ③ 回答链路 —— **前两次不带思考键、max_tokens 钉 600**。"""
    from app.agent.tools import knowledge_search
    from tests.api.test_chat_agent import _seed_materials

    monkeypatch.setattr(settings, "CHAT_REASONING", LEVEL_DISABLED)
    materials = await _seed_materials(db, ["蓝鲸是哺乳动物。", "鲸类生活在海里。"], filename="蓝鲸.md")

    async def fake_retrieve(session, question, scope, *, stats=None):  # noqa: ANN001
        return list(materials), "", None

    monkeypatch.setattr(knowledge_search, "_default_retrieve", lambda: fake_retrieve)

    wire = _attach(
        Wire(
            # ⚠️ Agent 决策循环走的是**流式**（`BoundModel.stream`），工具调用必须以
            # `tool_calls` 增量下发；给非流式 JSON 会让循环静默不做任何工具步骤。
            _wire_sse_tool_call("knowledge_search", {"query": "蓝鲸"}),
            _wire_sse("可以作答了"),
            _wire_sse("蓝鲸是哺乳动物。[1]"),
        ),
        monkeypatch,
    ).wire

    events = await _ask(client, "蓝鲸是什么", agent_mode="react")
    kinds = [event["type"] for event in events]
    assert "agent" in kinds and kinds[0] == "meta"
    steps = [event["step"] for event in events if event["type"] == "agent"]
    assert [step["kind"] for step in steps] == ["tool", "observation", "thought"], steps

    assert len(wire.bodies) == 3, [sorted(body) for body in wire.bodies]
    # 前两次 = Agent 决策 / 收尾（**流式**，但预算与思考键都要看）
    for index, body in enumerate(wire.bodies[:2]):
        assert "thinking" not in body, f"第 {index + 1} 次（Agent 决策）带上了思考键：{body}"
        assert body.get("max_tokens") == 600, f"第 {index + 1} 次 max_tokens 不是 600：{body}"
    # 收尾那条**回答**仍按原样带思考参数（A13 只隔离 Agent，不隔离答案）
    answer_body = wire.bodies[2]
    assert answer_body["thinking"] == {"type": "disabled"}, answer_body
    assert answer_body.get("max_tokens") != 600


async def test_a13_plan_execute_plan_call_has_no_thinking_key_and_budget_600(
    client, db, monkeypatch, wire_env
):
    """`plan_execute`：计划调用是**非流式 generate**（只有它覆盖「计划调用」那一格）。"""
    from app.agent.tools import knowledge_search
    from tests.api.test_chat_agent import _plan_json, _seed_materials

    monkeypatch.setattr(settings, "CHAT_REASONING", LEVEL_DISABLED)
    materials = await _seed_materials(db, ["蓝鲸是哺乳动物。"], filename="蓝鲸.md")

    async def fake_retrieve(session, question, scope, *, stats=None):  # noqa: ANN001
        return list(materials), "", None

    monkeypatch.setattr(knowledge_search, "_default_retrieve", lambda: fake_retrieve)

    wire = _attach(
        Wire(
            # ⚠️ P&E 的计划调用虽然走 `BoundModel.generate()`，但 cubeloop 仍然把
            # `stream=True` 发出去（探针实测：请求体 `{'stream': True,
            # 'stream_options': {'include_usage': True}}`）⇒ **必须以流式形状回**，
            # 否则 accumulate 出来是空文本、`parse_plan` 必然回退（本文件踩过一次）。
            # 形状必须是 `{"steps":[...]}`（`runner.parse_plan` 只认这一个键）。
            _wire_sse(_plan_json({"goal": "检索蓝鲸", "tool": "knowledge_search", "args": {"query": "蓝鲸"}})),
            _wire_sse("蓝鲸是哺乳动物。[1]"),
        ),
        monkeypatch,
    ).wire

    events = await _ask(client, "蓝鲸是什么", agent_mode="plan_execute")
    steps = [event["step"] for event in events if event["type"] == "agent"]
    assert [step["kind"] for step in steps] == ["plan", "thought", "tool", "observation"], steps

    # 编排阶段**只该有一次**计划调用（`generate`），之后才轮到回答链路
    assert len(wire.bodies) == 2, [sorted(body) for body in wire.bodies]
    plan_call = wire.bodies[0]

    # ① 隔离面（本条的**主证据**）：计划调用不带**思考参数**。
    #    注意 `reasoning_effort` 这一条必须单独断：cubeloop 的 openai 档位表里
    #    `off` 的原生载荷就是 `{"reasoning_effort": "none"}`，而 `_cap_active` 为假时
    #    它不下发——所以这里断言的是「**没有**这个键」，不是「有这个键且值为 none」。
    for key in ("thinking", "enable_thinking", "reasoning_effort", "reasoning"):
        assert key not in plan_call, f"计划调用带上了思考键 {key}：{plan_call}"
    assert plan_call["stream_options"] == {"include_usage": True}

    # ② `max_tokens == 600`：**两条 Agent 路径都成立**（包 §5 A13 的字面要求）。
    #
    #    ⚠️ 这里曾经是「P&E 结构性到不了 wire、故按现状断言没有 max_tokens」——2026-09-20
    #    实测并**修掉**了：`generate(max_output_tokens=600)` / `reasoning=` 在本仓装配口径下
    #    **一个字都不下发**（cubeloop 把它们关在 `if self._cap_active or model.reasoning:`
    #    之后，而本仓不传 `capability`），所以 `runner._stream_plan_execute` 补上了
    #    `options=StreamOptions(on_payload=_cap_decision_payload)` —— 与 ReAct 那条路**同源**。
    #    断言随之翻成正向：计划调用必须真的带 600。
    assert plan_call.get("max_tokens") == 600, f"计划调用没有真的下发输出预算：{plan_call}"
    # 原生 reasoning 载荷同样不许出现（`_cap_decision_payload` 只钉 max_tokens，不动思考键）
    for key in ("thinking", "enable_thinking", "reasoning_effort", "reasoning"):
        assert key not in plan_call, f"计划调用带上了思考键 {key}：{plan_call}"
    assert plan_call["stream_options"] == {"include_usage": True}

    # ③ 回答链路仍按原样带思考参数（A13 只隔离 Agent，不隔离答案）
    assert _require_stream(wire, index=1)["thinking"] == {"type": "disabled"}


async def test_a13_agent_but_answer_still_carries_thinking(client, db, monkeypatch, wire_env):
    """对照：**同一个用例里** `off` 模式只有一条请求，且**带**思考键（证明档位真的生效）。"""
    await _seed(db)
    monkeypatch.setattr(settings, "CHAT_REASONING", LEVEL_DISABLED)
    wire = _attach(Wire(_wire_sse("普通轮正文。")), monkeypatch).wire

    await _ask(client, "推理参数隔离用的真材料", agent_mode="off")

    assert len(wire.bodies) == 1
    assert _require_stream(wire)["thinking"] == {"type": "disabled"}


# --- A19：公共夹具让受影响用例仍不调用真实模型 ------------------------------------------------


def test_a19_this_file_is_not_covered_by_the_api_fixture(wire_env):
    """**A19 的前半（反证）**：本文件**必须**真装配 —— 否则 A12–A15 什么都没验到。

    `tests/api/conftest.py` 的 `pin_aux_model_delegates` 会把 `resolve_aux_model` 钉成
    跟随 `resolve_chat_model`（既有用例的兼容层）。本文件在 `tests/` 顶层、**不受它影响**，
    这正是「A19 的夹具不许覆盖 A12–A15」这条纪律的**落地方式**：用具目录把它们分开。
    完整的 A19 断言（夹具生效 + 真装配函数调用计数为 0）在
    `tests/api/test_reasoning_aux_calls.py`。
    """
    assert answer_mod.build_chat_model() is not None, "本文件必须真装配（A12–A15 的前提）"
    assert answer_mod.build_aux_model() is not None
