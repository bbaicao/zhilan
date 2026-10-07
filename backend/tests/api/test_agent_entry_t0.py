"""专项 · 工具调用能力第 0 批 · **② 两入口能力口径**的验收钉子（A12 / A12b）。

判据（开工包 §2 第 10–14 条 + 裁决 D9/D10/D11/D11′）：

- **判定点**在**模式解析之后**（请求显式值 → 设置页 ``AGENT_MODE`` → ``off``；带图强制 ``off``）
  ⇒ ``agent_mode=None`` + 全局 ``react`` 也**必须**拦；
- **错误** = **400** + 逐字冻结的中文文案；**不**返回 200 空回答、**不**静默降级成 off、
  **不**内部跑 Agent；
- 流式入口**照常**跑 Agent（只拦同步入口，不是能力回退）。

⚠️ **用例纪律（开工包 §5 第 22 条 / 评审 T5）**：``message`` **必须非空**。
``validate_request`` 的顺序是「未配模型 → 空输入 → 图片超限 → ``parse_agent_mode``」
⇒ 空 message + ``react`` 会**先** 400「请输入问题」，测到的是**另一条路径**（假绿）。
"""

from __future__ import annotations

import pytest

from app.api import chat_routes
from app.api.chat_routes import NON_STREAM_AGENT_MODE_DETAIL
from app.core.config import settings
from tests.api.t0_wire import (  # noqa: F401 —— client 是夹具
    Wire,
    attach_wire,
    client,
    wire_env,
    wire_sse_text,
)

#: 一个极小但**合法**的 PNG（1×1）。带图强制 off 那条要用它——
#: 直接给 `image_data` 一个 data URL 会走解码路径，假数据会在**更早**的地方 400。
_PNG_1X1 = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


# --------------------------------------------------------------------------
# A12 · 同步入口的四种态
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["react", "plan_execute"])
async def test_a12_explicit_agent_mode_is_refused_with_400_and_frozen_detail(
    client, wire_env, monkeypatch, mode
):
    """A12 态①：``agent_mode=react|plan_execute`` ⇒ 400 + **逐字**冻结文案。

    ⚠️ ``message`` 非空（否则先撞「请输入问题」，测到另一条路径）。
    """
    wire = Wire(wire_sse_text("不该被调用"))
    attach_wire(wire, monkeypatch)

    resp = await client.post("/api/chat", json={"message": "你好", "agent_mode": mode})

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == NON_STREAM_AGENT_MODE_DETAIL
    assert "非流式接口不支持 Agent 模式" in resp.json()["detail"]
    assert "请改用流式入口" in resp.json()["detail"]
    # **不**静默降级成 off：一次模型调用都不该发生
    assert wire.bodies == [], f"拒绝路径竟然调了模型：{wire.bodies}"


@pytest.mark.asyncio
async def test_a12_off_mode_still_answers_normally(client, wire_env, monkeypatch):
    """A12 态②：``agent_mode=off`` ⇒ 200 正常（边界只挡 Agent 模式，不挡普通轮）。"""
    wire = Wire(wire_sse_text("正常回答"))
    attach_wire(wire, monkeypatch)

    resp = await client.post("/api/chat", json={"message": "用工具看一眼", "agent_mode": "off"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["message"]
    assert len(wire.bodies) == 1, wire.call_kinds()


@pytest.mark.asyncio
async def test_a12_global_default_react_is_refused_even_without_the_field(
    client, wire_env, monkeypatch
):
    """A12 态③（**默认迁移态**）：``agent_mode`` 不传 + 全局 ``react`` ⇒ **同样 400**。

    这条是「按**解析后的有效模式**判断」的核心判据（D10）：只看请求原文的实现会**放过**它。
    """
    monkeypatch.setattr(settings, "AGENT_MODE", "react")
    wire = Wire(wire_sse_text("不该被调用"))
    attach_wire(wire, monkeypatch)

    resp = await client.post("/api/chat", json={"message": "用工具看一眼"})

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == NON_STREAM_AGENT_MODE_DETAIL
    assert wire.bodies == []


@pytest.mark.asyncio
async def test_a12_image_forces_off_so_an_explicit_agent_mode_still_answers(
    client, wire_env, monkeypatch
):
    """A12 态④：**带图 + 显式 ``react`` ⇒ 仍 200**（有效模式被强制成 ``off``）。

    ⚠️ 这条**不是**本函数开的例外，而是「按解析后的有效模式判断」的**必然归属**：
    ``resolve_agent_mode`` 对带图**强制 ``off``**（视觉模型不走编排）。若实现改成
    按**请求原文**判断，本用例会红 —— 它正是防那种写法的钉子。
    """
    wire = Wire(wire_sse_text("看图回答"))
    attach_wire(wire, monkeypatch)

    resp = await client.post(
        "/api/chat", json={"message": "这是什么", "agent_mode": "react", "images": [_PNG_1X1]}
    )

    assert resp.status_code == 200, resp.text
    assert len(wire.bodies) == 1


@pytest.mark.asyncio
async def test_a12_empty_message_still_hits_the_input_validation_first(
    client, wire_env, monkeypatch
):
    """A12 附（**反面钉子**）：空 ``message`` + ``react`` 撞的是**输入校验**那条，不是本边界。

    这条把「用例纪律」本身钉住：它证明上面四条**必须**带非空 message，
    否则测到的就是这条路径（开工包 §5 第 22 条的假绿）。
    """
    wire = Wire(wire_sse_text("不该被调用"))
    attach_wire(wire, monkeypatch)

    resp = await client.post("/api/chat", json={"message": "", "agent_mode": "react"})

    assert resp.status_code == 400
    assert resp.json()["detail"] != NON_STREAM_AGENT_MODE_DETAIL, "语义撞车：说明顺序变了"
    assert "问题" in resp.json()["detail"] or "输入" in resp.json()["detail"]


# --------------------------------------------------------------------------
# A12b · **默认迁移态**的独立实例（真 HTTP 探针）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a12b_default_migration_state_only_blocks_the_sync_entry(
    client, wire_env, monkeypatch
):
    """A12b：全局 ``AGENT_MODE=react`` 的实例上 —— 同步 400、**流式照常跑 Agent**。

    证明「只拦同步入口」而不是「把 Agent 能力关掉」。
    本用例在**进程内**用 ``monkeypatch`` 模拟那个实例（等价于开工包 §六 第 6 段
    用 ``$env:AGENT_MODE='react'`` 起另一个实例；真起独立进程的那一遍见
    ``T0-验收报告.md`` 的 A12b 段与证据流水）。
    """
    monkeypatch.setattr(settings, "AGENT_MODE", "react")

    # ① 同步入口：不带 agent_mode 也 400
    sync_wire = Wire(wire_sse_text("不该被调用"))
    attach_wire(sync_wire, monkeypatch)
    resp = await client.post("/api/chat", json={"message": "用工具看一眼"})
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == NON_STREAM_AGENT_MODE_DETAIL
    assert sync_wire.bodies == []

    # ② 同一实例上流式：照常 200 + 真跑 Agent（决策 → 收尾 → 回答）
    # ⚠️ message 必须**长过** ``clarify.MIN_LENGTH = 4``：短句（如「你好」）会先被
    # 澄清判定短路（那是**第 1 批**的条目「Agent 模式不再因 MIN_LENGTH 硬短路」，
    # 第 0 批**不做**）⇒ 走到的是澄清轮、压根不进 Agent 循环（实测：``call_kinds``
    # 只剩 ``["answer"]``，看着像「Agent 没跑」）。
    stream_wire = Wire(
        wire_sse_text("可以作答了"),
        wire_sse_text("流式照常回答"),
    )
    attach_wire(stream_wire, monkeypatch)
    resp = await client.post("/api/chat/stream", json={"message": "用工具看一眼"})
    assert resp.status_code == 200, resp.text
    assert stream_wire.call_kinds() == ["decision", "answer"], stream_wire.call_kinds()
    assert [b for b in stream_wire.bodies if b.get("tools")], "流式没有走进 Agent 决策（工具清单为空）"


def test_a12b_guard_is_a_pure_function_over_the_resolved_mode():
    """A12b 附：守卫是**纯函数**（零 DB、零副作用、只读 payload 与设置页默认值）。

    三条独立判据：
    ① 它读的是 :func:`resolve_agent_mode` 的**解析结果**（不是请求原文）；
    ② 非法 ``agent_mode`` 值仍走**既有**那条 400（「未知的 Agent 模式…」），不被本守卫改写；
    ③ ``off`` / 空 一律放行。
    """
    from app.api.chat_schemas import ChatRequest

    assert chat_routes.guard_non_stream_agent_mode(ChatRequest(message="hi", agent_mode="off")) is None
    assert chat_routes.guard_non_stream_agent_mode(ChatRequest(message="hi")) is None

    for mode in ("react", "plan_execute"):
        with pytest.raises(chat_routes.HTTPException) as excinfo:
            chat_routes.guard_non_stream_agent_mode(ChatRequest(message="hi", agent_mode=mode))
        assert excinfo.value.status_code == 400
        assert excinfo.value.detail == NON_STREAM_AGENT_MODE_DETAIL

    # 非法值：既有校验先抛（文案不同 ⇒ 本守卫没有把两条路径合并掉）
    with pytest.raises(chat_routes.HTTPException) as excinfo:
        chat_routes.guard_non_stream_agent_mode(ChatRequest(message="hi", agent_mode="乱填的"))
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail != NON_STREAM_AGENT_MODE_DETAIL
    assert "未知的 Agent 模式" in excinfo.value.detail


def test_a12b_streaming_entry_does_not_call_the_guard():
    """A12b 附：守卫**只在同步入口**调用（AST 级判据，防止有人塞进 ``validate_request``）。

    ``validate_request`` 被流式入口**共用** ⇒ 把守卫放进去会把流式一起拒掉。
    这条用源码判据把「调用点在 ``chat()`` 里」钉死，而不是靠注释提醒。
    """
    import inspect

    source = inspect.getsource(chat_routes.chat)
    assert "guard_non_stream_agent_mode(payload)" in source

    validate_source = inspect.getsource(chat_routes.validate_request)
    assert "guard_non_stream_agent_mode" not in validate_source, (
        "守卫被塞进了 validate_request —— 流式入口会被一起拒掉"
    )
    stream_source = inspect.getsource(chat_routes.chat_stream)
    assert "guard_non_stream_agent_mode" not in stream_source