"""专项 · 工具调用能力第 0 批 · **① 工具结果通道 + ② 两入口能力边界**的验收钉子。

## 本文件为什么长这样（两件必补的夹具，「装配点定选 (b) 方案」）

开工包 §0.2 的「请求体捕获能力」一节写明：**全仓没有任何「Agent 轮最终回答请求体」的夹具**
（`_RecorderModel` 只覆盖非 Agent 路径；`test_chat_agent.py` 的 `_install_model` 不记 messages）。
本文件补上它，并且**不用 FauxProvider** —— 理由是实测事实：

- `FauxProvider.stream()` 合成的 payload 只有 ``{"model", "messages", "system_prompt"}``
  （+ Agent 轮的 ``max_tokens``），**不含 ``tools``**（实测见开工包 §5 第 15 条）
  ⇒ 拿它做「哪一次是回答调用」的分派会写出**假绿**断言（决策调用与回答调用长得一样）；
- 而 Agent 轮的「回答调用」与「决策调用」的**唯一 wire 区别就是带不带 ``tools[]``**
  （``cubeloop/agent/loop.py::_stream_assistant_response``：``tools_defs = None`` 或
  ``[t.to_definition() for t in visible]``）⇒ 必须拿到**序列化之后的真 wire payload**。

所以本文件照抄仓内**进程内**先例 ``tests/test_reasoning_isolation.py`` 的做法：
**真装配**（``answer.build_chat_model`` / ``build_aux_model``，真 ``OpenAIProvider``），
只在**发送边界**把 SDK 的 httpx 客户端换成 ``httpx.MockTransport``。
零出网（只认 ``https://wire.invalid/v1``，任何别的目标当场断言失败）。

## 假工具值的载体（D20 / D20′）

**不用** ``knowledge_search`` / ``web_search`` / ``fetch_page`` 承载假值 —— 它们的结果**带
materials**，而带 materials 的结果**改前就已经**经 ``turn.materials → plan.materials_text``
进了「资料如下」区 ⇒ 拿它们当判据，**改前就绿**，断言失去鉴别力。

本文件用 **``build_registry(mcp_packs=…, call_tool=…)`` 注入的假 MCP 工具**：
「MCP 工具的材料恒空」是产品既有语义（``app/agent/tools/mcp.py`` 的注释与实现），
所以它的结果**天然落在第 ① 项要补的那半边**（纯文本、不进资料区），
且哨兵串是**模型事先不知道**的（不是 ``mcp.py`` 那条固定串「尚未绑定传输通道」）。

⚠️ 生产路径 ``chat_routes`` **不传** ``mcp_packs`` / ``call_tool``（全文零命中该两符号）
⇒ **只能在测试进程内 monkeypatch**；起跨进程 uvicorn 会把判据逼回那条模型可预知的固定串
（= 假绿）。本文件因此走**进程内 ASGI**（复用 ``test_chat_contract.py`` 的 ``client`` 夹具）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.api import chat_routes
from app.core.config import settings
from app.generation import answer as answer_mod
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401 —— client 是夹具

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
    # 回归修复 测试侧适配（2026-09-27）：带图轮的就绪校验改查**视觉角色** ⇒
    # wire 服务须声明 vision 可用，a12 态④「带图强制 off 仍 200」才继续测到原语义。
    "model_roles": {MODEL: ["chat", "vision"]},
    "for_roles": ["chat", "vision"],
}

#: 假 MCP 包的 slug 与包内工具名（契约名 = ``mcp:fauxkit:probe``）。
#: ⚠️ 名字与描述都要**避开** ``mcp.dangerous_by_default`` 的高危动词正则
#: （``delete|remove|drop|write|update|create|insert|exec|shell|command|run|rm|destroy|modify|send|post|put|patch|upload``）
#: —— 否则 ``build_specs`` 在工具级就把它丢掉了（I6），注入静默失效。
FAKE_PACK_SLUG = "fauxkit"
FAKE_TOOL_NAME = "probe"
FAKE_TOOL_CONTRACT = f"mcp:{FAKE_PACK_SLUG}:{FAKE_TOOL_NAME}"


# --------------------------------------------------------------------------
# 假传输（照抄 tests/test_reasoning_isolation.py::Wire 的做法）
# --------------------------------------------------------------------------


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


def wire_sse_text(text: str) -> str:
    """OpenAI 兼容的**流式**正文响应（``stream=true`` 时厂商回的就是这个形状）。"""
    return (
        _sse_chunk({"role": "assistant", "content": text})
        + _sse_chunk({}, finish="stop")
        + "data: [DONE]\n\n"
    )


def wire_sse_tool_call(name: str, args: dict, *, call_id: str = "call_wire_1") -> str:
    """流式响应里的一次**工具调用**（CubeLoop 决策循环吃这个形状）。

    ⚠️ ``arguments`` 必须是**字符串**，且 ``function.name`` 与 ``index`` 要齐
    —— 少任何一样，CubeLoop 攒出来的工具调用都解析不出来，循环会**静默不做任何工具步骤**
    （``tests/test_reasoning_isolation.py`` 的 docstring 记着这次踩坑）。
    """
    call = {
        "index": 0,
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }
    return (
        _sse_chunk({"role": "assistant", "tool_calls": [call]})
        + _sse_chunk({}, finish="tool_calls")
        + "data: [DONE]\n\n"
    )


def wire_json(content: str) -> dict:
    """OpenAI 兼容的**非流式**响应（Agent 决策 / 计划调用走这个形状）。"""
    return {
        "id": "chatcmpl-wire",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class Wire:
    """假传输：按**响应队列**回真厂商形状的 HTTP 响应，并逐次记下**序列化后的请求体**。

    ``is_stream`` 按请求体的判定：``stream=true`` ⇒ 回 ``text/event-stream`` 的 SSE 文本，
    否则回 JSON —— 与真厂商一致（Agent 决策走 ``generate`` ⇒ 非流式；回答走 ``stream()``）。
    """

    def __init__(self, *responses: str | dict) -> None:
        self.queue = list(responses)
        self.bodies: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(BASE_URL), f"意外的出网目标：{request.url}"
        body = json.loads(request.content.decode("utf-8"))
        self.bodies.append(body)
        assert self.queue, "响应队列空了（说明被测代码多调了一次模型）"
        item = self.queue.pop(0)
        wants_stream = bool(body.get("stream"))
        if wants_stream:
            assert isinstance(item, str), "流式请求必须配一段 SSE 文本响应"
            return httpx.Response(
                200, text=item, headers={"content-type": "text/event-stream"}
            )
        assert isinstance(item, dict), "非流式请求必须配一个 JSON 响应"
        return httpx.Response(200, json=item, headers={"content-type": "application/json"})

    # --- 请求体的读取与**分派**（决策 / 回答的判别式就在 tools[] 上）----------

    @property
    def request_bodies(self) -> list[dict]:
        return list(self.bodies)

    @property
    def decision_bodies(self) -> list[dict]:
        """**决策调用**：带 ``tools[]``（ReAct 的每一轮决策都带）。"""
        return [b for b in self.bodies if b.get("tools")]

    @property
    def answer_bodies(self) -> list[dict]:
        """**回答调用**：**不带** ``tools[]``（``_stream_assistant_response`` 的
        ``tools_defs = None`` 那一路）。

        ⚠️ 开工包 §5 第 7 条要求「分派不得只看有没有 ``tools[]``」——因为 **P&E 的
        **计划**调用也带 ``tools[]``**。本文件因此**同时**按 **工具名集合** 分派：
        :meth:`call_kinds` 给出每一次调用的类别，A3 就用它断言「回答调用不带通道段」。
        """
        return [b for b in self.bodies if not b.get("tools")]

    def tools_of(self, body: dict) -> list[str]:
        return [str((t or {}).get("function", {}).get("name", "")) for t in (body.get("tools") or [])]

    def call_kinds(self) -> list[str]:
        """逐次调用的类别：``answer``（无 tools）/ ``decision``（有 tools）。

        本批的用例只走 ReAct，所以「带 tools」= 决策；把判别式写成一个**方法**而不是
        散在断言里，是为了将来 P&E 加进来时只有一处要改（并已在 docstring 里点名）。
        """
        return ["answer" if not b.get("tools") else "decision" for b in self.bodies]

    def messages_of(self, body: dict) -> list[dict]:
        return list(body.get("messages") or [])

    def last_user_text(self, body: dict) -> str:
        """某次请求体里**最后一条 user 消息**的文本（回答请求的数据区就在这里）。"""
        for item in reversed(self.messages_of(body)):
            if item.get("role") == "user":
                return str(item.get("content") or "")
        return ""

    def system_prompt_of(self, body: dict) -> str:
        """该次请求体里的**系统提示段**正文。

        ⚠️ **不能读 ``body["system_prompt"]``**：真 ``OpenAIProvider`` 把它**折进
        ``messages[0]``（``role == "system"``）**了（wire payload 里没有独立键）；
        ``body["system_prompt"]`` 只有 Faux 的合成 payload 才有。实测踩过这条
        —— 断言会以「空字符串」的形式**假红**。
        """
        explicit = str(body.get("system_prompt") or "").strip()
        if explicit:
            return explicit
        for item in self.messages_of(body):
            if item.get("role") == "system":
                return str(item.get("content") or "")
        return ""


def attach_wire(wire: Wire, monkeypatch) -> SimpleNamespace:
    """真装配两个模型入口，并把假传输挂到**同一个** Provider 的 SDK 客户端上。

    ⚠️ 两件事必须同时做（``test_reasoning_isolation.py::_attach`` 的教训）：
    ① **真装配**（换掉 Provider 就把被测行为换掉了）；② **把端点里的装配入口钉住**
    —— 不钉的话请求里会重新装配一个**没有假传输**的 Provider ⇒ 真出网。

    ⚠️ **``resolve_aux_model`` 也钉成同一个模型**（2026-09-21 实测踩到）：Agent 轮的
    **决策 / 计划调用走 aux_model**（不带思考参数），而 ``answer.build_aux_model()``
    在**本机 ``.env``** 下会装配出**另一个真 Provider** ⇒ 决策调用**绕过假传输真出网**，
    wire 里只剩回答那一腿（``call_kinds() == ["answer"]``，看着像「Agent 没跑」）。
    ``tests/api/conftest.py`` 的 ``pin_aux_model_delegates`` 正是为此存在；本文件自带
    夹具，所以必须自己钉同一份。
    """
    chat = answer_mod.build_chat_model()
    aux = answer_mod.build_aux_model()
    if chat is None:
        pytest.fail("真装配返回了 None —— 用例的配置没生效（需要真 Provider 才能看 wire）")
    for bound in (chat, aux):
        bound.provider._client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(wire.handler)
        )
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: chat)
    # 决策链路与回答链路指向**同一个** wire 上的模型（见上面第二条 ⚠️）
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: chat)
    return SimpleNamespace(wire=wire, chat=chat, aux=aux, provider=chat.provider)


@pytest.fixture
def wire_env(monkeypatch):
    """真配置（mimo + 真 base_url）+ 本批特别依赖的几条显式隔离钉子。

    本文件在 ``tests/api/`` 下，``conftest.py`` 的九个 autouse 钉子已生效
    （含 ``pin_agent_mode_off`` / ``pin_memory_off`` / ``pin_web_off``），
    这里补的是**本批特别依赖**的那几条。
    """
    monkeypatch.setattr(settings, "MODEL_SERVICES", _dump([SERVICE]))
    monkeypatch.setattr(
        settings,
        "MODEL_ROLES",
        # 回归修复：带图轮就绪校验查视觉角色 ⇒ 角色表补 vision 绑定（同一 wire 服务）。
        _dump(
            {
                "chat": {"service_id": "svc_wire", "model": MODEL},
                "vision": {"service_id": "svc_wire", "model": MODEL},
            }
        ),
    )
    monkeypatch.setattr(settings, "CHAT_REASONING", "")
    monkeypatch.setattr(settings, "AGENT_MODE", "off")
    monkeypatch.setattr(settings, "WEB_ENABLE", False)
    # ⚠️ A1 的「逐字节」判据**必须**钉死记忆开关：记忆段经 ``system_prompt(memory_text=)``
    # 进请求体且是 fire-and-forget ⇒ 不钉它「逐字节」不可判（开工包 A1 行明文要求）。
    monkeypatch.setattr(settings, "MEMORY_ENABLE", False)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", False)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("external")
    )
    monkeypatch.setattr(chat_routes, "resolve_chat_model", answer_mod.build_chat_model)
    monkeypatch.setattr(chat_routes, "resolve_aux_model", answer_mod.build_aux_model)
    return settings


@pytest.fixture
def fake_mcp(monkeypatch):
    """注入一个**假 MCP pack + 返回哨兵串的 call_tool**（D20 的首选载体）。

    注入点 = ``app.agent.bootstrap.build_registry``（**唯一的工具注册表装配点**）：
    ``runner.build_agent_tools`` 在 ``registry is None`` 时调它 ⇒ 进程内的 monkeypatch
    能生效，而**产品注册表不留任何残留**（只在本用例的进程内、只在这几个关键字上）。

    返回的哨兵串形如 ``T0-FAKE-<uuid>``：``<uuid>`` 每次现铸，**模型不可能事先知道**。

    ⚠️ **返回文本可换**（``set_return_text``）：改 ``token`` **不会**改变工具返回值
    ——它是闭包在**测试进程**里拼好的字符串，不是模板（实测踩到：注入文本的用例
    改了 ``token`` 却断言失败，因为工具返回的还是旧串）。
    """
    from app.agent import bootstrap
    from app.agent.tools.mcp import McpPack, McpTool

    token = f"T0-FAKE-{uuid4().hex}"
    calls: list[tuple] = []
    seen: dict = {}
    text_holder = {"value": ""}

    def _return_text() -> str:
        return text_holder["value"] or f"{token}｜{FAKE_TOOL_NAME} 的返回文本"

    def fake_call_tool(transport, command, url, tool_name, args):
        calls.append((tool_name, dict(args or {})))
        return _return_text()

    real_build = bootstrap.build_registry

    def spy(*, mcp_packs=None, call_tool=None, **kwargs):
        seen["registry"] = real_build(
            mcp_packs=[
                McpPack(
                    slug=FAKE_PACK_SLUG,
                    name="假 MCP 包（用例）",
                    transport="stdio",
                    command=("noop",),
                    tools=(McpTool(name=FAKE_TOOL_NAME, description="返回一段纯文本"),),
                )
            ],
            call_tool=fake_call_tool,
            **kwargs,
        )
        return seen["registry"]

    monkeypatch.setattr(bootstrap, "build_registry", spy)

    def set_return_text(value: str) -> None:
        """换掉工具返回的**整段文本**（A5 的注入文本用例用）。"""
        text_holder["value"] = value

    return SimpleNamespace(
        token=token, calls=calls, seen=seen, set_return_text=set_return_text
    )


async def run_stream(client, *, message: str, agent_mode: str | None = "react", **extra):
    """打一次 ``/api/chat/stream`` 并返回解析后的 SSE 帧列表。"""
    payload: dict = {"message": message}
    if agent_mode is not None:
        payload["agent_mode"] = agent_mode
    payload.update(extra)
    resp = await client.post("/api/chat/stream", json=payload)
    assert resp.status_code == 200, resp.text
    return parse_sse(resp.text)
