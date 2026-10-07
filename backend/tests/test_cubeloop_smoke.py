"""CubeLoop 0.14.1 结构级冒烟：不连真模型。

覆盖 pitfalls 复验点：
- Agent 无 async for event in stream；事件走 subscribe(listener)
- provider.stream 用 bound = provider.model(id)
- 构造 Agent 时 middleware=[]（不传 before_tool_call=，避免覆盖 middleware 链）
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import BaseModel

import cubeloop
from cubeloop import Agent, AgentTool, AgentToolResult, tool
from cubeloop.providers.faux import FauxProvider, faux_assistant_message, faux_tool_call


def test_cubeloop_version_and_exports():
    assert cubeloop.__version__ == "0.14.1"
    for name in (
        "Agent",
        "tool",
        "AgentTool",
        "AgentToolResult",
        "BaseProvider",
        "BoundModel",
        "UserMessage",
    ):
        assert hasattr(cubeloop, name), name


def test_agent_has_subscribe_not_async_stream():
    assert hasattr(Agent, "subscribe")
    # pitfall：不存在 async for event in stream 的 API
    assert not hasattr(Agent, "__aiter__")
    sig = inspect.signature(Agent.__init__)
    assert "middleware" in sig.parameters
    # 显式 before_tool_call 存在，但器官应统一写 middleware=[]
    assert "before_tool_call" in sig.parameters


def test_provider_model_bound_stream():
    provider = FauxProvider(provider_id="faux")
    bound = provider.model("faux-echo")
    assert hasattr(bound, "stream")
    assert hasattr(bound, "generate")
    assert hasattr(provider, "model")


@pytest.mark.asyncio
async def test_agent_loop_with_tool_via_faux_provider():
    """FauxProvider 走通：prompt → 模型 tool_call → 工具执行 → 最终答复。"""
    provider = FauxProvider(provider_id="faux")
    provider.set_responses(
        [
            faux_assistant_message(
                [
                    faux_tool_call("echo", {"text": "ping"}),
                ],
                stop_reason="tool_use",
            ),
            faux_assistant_message("pong"),
        ]
    )

    class EchoArgs(BaseModel):
        text: str

    async def execute_echo(tool_call_id: str, args: EchoArgs, **kwargs) -> AgentToolResult:
        from cubeloop.providers.base import TextContent

        return AgentToolResult(content=[TextContent(text=f"echo:{args.text}")])

    echo_tool = AgentTool(
        name="echo",
        description="echo text",
        parameters=EchoArgs,
        execute=execute_echo,
    )

    bound = provider.model("faux-echo")
    events: list[str] = []
    agent = Agent(
        model=bound,
        system_prompt="test",
        tools=[echo_tool],
        middleware=[],
    )
    agent.subscribe(lambda ev, _signal=None: events.append(getattr(ev, "type", type(ev).__name__)))
    run_id = await agent.prompt("hello")
    assert isinstance(run_id, str) and run_id
    last = agent.state.messages[-1]
    text = "".join(getattr(c, "text", "") for c in last.content)
    assert "pong" in text
    assert any("tool" in e for e in events), events


@pytest.mark.asyncio
async def test_tool_decorator_builds_agent_tool():
    @tool(name="add_nums", description="add two numbers")
    async def add_nums(a: int, b: int) -> str:
        return str(a + b)

    assert isinstance(add_nums, AgentTool)
    assert add_nums.name == "add_nums"
    assert add_nums.parameters.model_json_schema()["properties"].keys() >= {"a", "b"}
