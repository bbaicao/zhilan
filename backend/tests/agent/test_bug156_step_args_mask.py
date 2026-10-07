"""条目 156（P1）· 工具步骤帧载荷：**顶层与任意嵌套**敏感键都要遮蔽。

缺陷（修复前）：`backend/app/agent/runner.py:446 _tool_args_for_steps` 在
`if tool_name not in READ_FILE_TOOLS or not isinstance(args, dict): return args`（`:470-471`）
把**非读文件类工具**的载荷逐字放行；读文件类的三处早退分支（`:473-474` / `:479-480` /
`:484-486`）同样 `return args` 原文 ⇒ 工具参数里的 `api_key` / `confirm_token` / `password`
等原地落进 `AgentTurn.steps`，经 `trace()`（`:1262-1272`）**同时**进 SSE `agent` 帧
（`:2225-2232`）与 `messages.agent_trace`（`backend/app/api/chat_routes.py:959`）。
DB 审计面（`backend/app/agent/audit.py` 的 `_args_digest`）早已遮蔽 —— 本件钉的是
**步骤帧这一条数据流**（与 `回归修复`「两条数据流只挡了一侧」同型）。

判据（拍板 #5 = C · A 为主 + 白名单可下发）：
  ① 顶层与任意嵌套（dict 内 dict / list 内 dict）敏感键值 == `SENSITIVE_ARG_MASK`，且**键仍在**；
  ② 非敏感键的**键集与值逐字不变**（092 契约：`json.loads(args["filters"])` 仍解得回）；
  ③ 两条帧路径（ReAct `_steps_for` / P&E `_tool_frames`）**同口径**；
  ④ `READ_FILE_TOOLS` 的路径归一 identity 腿不动（无敏感键时**逐字节**保留既有语义）；
  ⑤ 不误遮：`token_count` 这类「包含但非精确等值」的键保持原值；
  ⑥ 非 dict 载荷：有 `model_dump()` 的先转形再遮蔽；`None` / 裸 `str` 原样放行（**残余面**，见末条）。

负对照：`_s2-scratch/runner/negctl_156.py`（sys.meta_path 源码重定向）把 `app.agent.runner`
换成**修复前字节** ⇒ ①③⑥ 腿必红，②④⑤ 腿必须仍绿。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.agent import audit as agent_audit
from app.agent.runner import (
    AgentTurn,
    ToolCollector,
    _steps_for,
    _tool_args_for_steps,
    _tool_frames,
)
from app.agent.types import ToolObservation
from app.core.config import settings

MASK = agent_audit.SENSITIVE_ARG_MASK


# ── 载荷与期望（键集必须逐字一致：只换值、不吞键）────────────────────────────


def _payload() -> dict:
    return {
        "query": "知澜是什么",
        "api_key": "SK-TOP-111",
        "Confirm-Token": "CT-DASH-222",
        "opts": {
            "api_key": "SK-NESTED-444",
            "confirm_token": "CT-NESTED-555",
            "limit": 5,
            "note": "非敏感值·逐字保留",
        },
        "items": [{"token": "TK-LIST-666", "keep": "逐字保留"}, {"secret": "SC-LIST-777"}],
        "token_count": 7,
        "filters": json.dumps({"doc": "d1", "limit": 3}, ensure_ascii=False),
    }


def _masked() -> dict:
    out = _payload()
    out["api_key"] = MASK
    out["Confirm-Token"] = MASK
    out["opts"]["api_key"] = MASK
    out["opts"]["confirm_token"] = MASK
    out["items"][0]["token"] = MASK
    out["items"][1]["secret"] = MASK
    return out


SECRET_VALUES = [
    "SK-TOP-111",
    "CT-DASH-222",
    "SK-NESTED-444",
    "CT-NESTED-555",
    "TK-LIST-666",
    "SC-LIST-777",
]


def _assert_no_secret_visible(blob: str, where: str) -> None:
    hits = [v for v in SECRET_VALUES if v in blob]
    assert not hits, f"{where} 仍能看到敏感键的原文：{hits}"


# ── ① 直调装配口：顶层 + 嵌套（dict 内 dict / list 内 dict）─────────────────


def test_direct_call_masks_top_level_and_nested_keys() -> None:
    out = _tool_args_for_steps("knowledge_search", _payload())
    assert out == _masked()
    # 「键仍在」：遮蔽只换值，不吞键（否则前端/审计对账键集会漂移）
    assert set(out) == set(_payload())
    assert set(out["opts"]) == set(_payload()["opts"])
    assert set(out["items"][0]) == set(_payload()["items"][0])
    _assert_no_secret_visible(repr(out), "直调载荷")


def test_case_and_separator_variants_are_masked() -> None:
    payload = {
        "confirmToken": "CT-CAMEL-1",
        "CONFIRM_TOKEN": "CT-UPPER-2",
        "Password": "PW-3",
        "AUTHORIZATION": "Bearer-4",
        "SECRET": "SC-5",
        "apiKey": "AK-6",
        "not_token": "非等值·不遮",
        "nested": {"Secret-Key": "no", "deep": {"Token": "TK-7"}},
    }
    out = _tool_args_for_steps("mcp_call", payload)
    assert out == {
        "confirmToken": MASK,
        "CONFIRM_TOKEN": MASK,
        "Password": MASK,
        "AUTHORIZATION": MASK,
        "SECRET": MASK,
        "apiKey": MASK,
        "not_token": "非等值·不遮",
        "nested": {"Secret-Key": "no", "deep": {"Token": MASK}},
    }
    # 反向自证：`Secret-Key`（secretkey）/`not_token`（nottoken）规范化后**不等于**任何键表项
    # ⇒ 必须原样保留。遮蔽是**不可逆**的，误伤即丢审计信息（audit.py:426-439 的既定口径）。
    assert out["nested"]["Secret-Key"] == "no"
    assert out["not_token"] == "非等值·不遮"


def test_token_count_is_not_masked() -> None:
    """遮蔽是**不可逆**的 ⇒ 键表只做精确等值，不做包含匹配（`audit.py:426-439` 的既定口径）。"""
    payload = {
        "token_count": 7,
        "tokens": "abc",
        "api_keys": "not-a-match",
        "confirm_tokens": 3,
        "password_hint": "非敏感",
    }
    assert _tool_args_for_steps("knowledge_search", payload) == payload


# ── ② 非敏感键集与值逐字不变（092 契约）────────────────────────────────────


def test_non_sensitive_keys_and_values_are_verbatim() -> None:
    payload = _payload()
    out = _tool_args_for_steps("knowledge_search", payload)
    assert out["query"] == payload["query"]
    assert out["opts"]["limit"] == 5 and isinstance(out["opts"]["limit"], int)
    assert out["opts"]["note"] == "非敏感值·逐字保留"
    assert out["items"][0]["keep"] == "逐字保留"
    assert out["token_count"] == 7
    # 092 契约：filters 是 **JSON 字符串**，遮蔽把字符串当叶子 ⇒ 逐字保留且仍解得回
    assert out["filters"] == payload["filters"]
    assert json.loads(out["filters"]) == {"doc": "d1", "limit": 3}
    # 原载荷**未被就地改动**（audit.py 口径：只造新容器）
    assert payload == _payload()


def test_no_sensitive_key_returns_the_same_object() -> None:
    """identity 腿：无敏感键时**连对象身份都不变**（既有语义零变化，不用新 dict 替身）。"""
    payload = {"query": "x", "token_count": 2}
    assert _tool_args_for_steps("knowledge_search", payload) is payload


# ── ③ 两条帧路径（ReAct / P&E）同口径 ──────────────────────────────────────


def _react_step(payload: dict) -> tuple[AgentTurn, list[dict]]:
    turn = AgentTurn(mode="react")
    event = SimpleNamespace(
        type="tool_execution_start",
        tool_call_id="c-react",
        tool_name="knowledge_search",
        args=payload,
    )
    return turn, _steps_for(event, turn=turn, collector=ToolCollector(), started_at={})


def _pe_step(payload: dict) -> tuple[AgentTurn, list[dict]]:
    turn = AgentTurn(mode="plan_execute")
    frames = _tool_frames(
        turn=turn,
        tool_call_id="c-pe",
        name="mcp_call",
        args=payload,
        collector=ToolCollector(),
        observation=ToolObservation(text="ok"),
        elapsed_ms=3,
    )
    return turn, frames


def test_react_step_frame_masks_args() -> None:
    turn, frames = _react_step(_payload())
    assert len(frames) == 1
    assert frames[0]["kind"] == "tool"
    assert frames[0]["args"] == _masked()
    # 落点就是 SSE `agent` 帧与 `messages.agent_trace` 的同一份对象
    trace = turn.trace()
    assert set(trace) == {"mode", "steps", "events_incomplete"}
    assert trace["steps"][0]["args"] == _masked()
    assert turn.steps[0]["args"] == _masked()
    _assert_no_secret_visible(repr(frames[0]["args"]), "ReAct 步骤帧")


def test_plan_execute_frames_mask_args() -> None:
    turn, frames = _pe_step(_payload())
    assert frames[0]["kind"] == "tool"
    assert frames[0]["args"] == _masked()
    assert turn.trace()["steps"][0]["args"] == _masked()
    _assert_no_secret_visible(repr(frames[0]["args"]), "P&E 步骤帧")


def test_both_frame_paths_agree() -> None:
    """两条路径**同一个口径**（改一条不改另一条 = 同一个问题在两种模式下外观不同）。"""
    _, react_frames = _react_step(_payload())
    _, pe_frames = _pe_step(_payload())
    assert react_frames[0]["args"] == pe_frames[0]["args"]


def test_step_frame_matches_db_audit_face() -> None:
    """与 DB 审计面同口径：`_args_digest` 遮蔽的键，步骤帧也必须遮蔽。"""
    digest = agent_audit._args_digest(_payload())
    _, frames = _react_step(_payload())
    args = frames[0]["args"]
    assert digest["api_key"] == MASK
    assert args["api_key"] == MASK
    assert "SK-NESTED-444" not in digest["opts"]
    assert MASK in digest["opts"]
    _assert_no_secret_visible(digest["opts"], "DB 审计面（对照）")


# ── ④ READ_FILE_TOOLS 路径归一 identity 腿 + 早退分支 ──────────────────────


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    """真工作区根（红线：一律 tmp_path），并在末尾把「根已接上」变成**可见的**失败。"""
    from app.agent.tools import _paths

    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "a.txt").write_text("alpha\n", encoding="utf-8")
    settings.WORKSPACE_ROOT = str(ws)
    assert _paths.resolve_workspace_root() is not None, "根没接上 ⇒ 本夹具的腿会落进「未配置」分支"
    try:
        yield ws
    finally:
        settings.WORKSPACE_ROOT = ""


def test_read_file_normalization_is_unchanged(workspace_root: Path) -> None:
    """identity 腿：**无敏感键**时读文件类载荷的归一结果逐字不变。"""
    ws = workspace_root
    out = _tool_args_for_steps("read_file", {"path": str(ws / "a.txt")})
    assert out == {"path": "a.txt"}
    assert str(ws) not in repr(out)


def test_read_file_normalization_and_masking_together(workspace_root: Path) -> None:
    """同一趟里两件事都要做：路径照旧归一（白名单语义不动）+ 敏感键遮蔽。"""
    ws = workspace_root
    out = _tool_args_for_steps("read_file", {"path": str(ws / "a.txt"), "api_key": "SK-TOP-111"})
    assert out == {"path": "a.txt", "api_key": MASK}
    assert str(ws) not in repr(out)


def test_read_file_early_exit_branches_still_mask(monkeypatch) -> None:
    """三处早退分支（`:473-474` / `:479-480` / `:484-486`）**不得**成为遮蔽的旁路。"""
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", "")
    assert _tool_args_for_steps("read_file", {"path": 123, "api_key": "SK-TOP-111"}) == {
        "path": 123,
        "api_key": MASK,
    }
    assert _tool_args_for_steps("read_file", {"path": "   ", "confirm_token": "CT-NESTED-555"}) == {
        "path": "   ",
        "confirm_token": MASK,
    }
    # 根未配置 ⇒ :479-480 早退，但遮蔽仍要发生
    assert _tool_args_for_steps("read_file", {"path": "docs/a.md", "token": "TK-LIST-666"}) == {
        "path": "docs/a.md",
        "token": MASK,
    }


def test_read_file_normalization_exception_branch_still_masks(monkeypatch, workspace_root) -> None:
    """归一抛异常（`:484-486`）时同样只剩遮蔽，且**不炸穿**。"""
    from app.agent.tools import _paths

    def _boom(*_args, **_kwargs):
        raise RuntimeError("归一失败（构造）")

    monkeypatch.setattr(_paths, "describe", _boom)
    out = _tool_args_for_steps("read_file", {"path": "a.txt", "password": "PW-3"})
    assert out == {"path": "a.txt", "password": MASK}


# ── ⑤ 非 dict 载荷（pydantic / None / str）──────────────────────────────────


class _ProbeArgs(BaseModel):
    query: str = "q"
    api_key: str = "SK-PYD-888"
    opts: dict = {"confirm_token": "CT-PYD-999"}
    token_count: int = 4


def test_pydantic_payload_is_model_dumped_then_masked() -> None:
    """形状变化**已登记**：修复前这里拿到的是模型对象本身，现在拿到 `model_dump()` 后的 dict。"""
    out = _tool_args_for_steps("knowledge_search", _ProbeArgs())
    assert isinstance(out, dict)
    assert out["query"] == "q"
    assert out["api_key"] == MASK
    assert out["opts"]["confirm_token"] == MASK
    assert out["token_count"] == 4
    _assert_no_secret_visible(repr(out), "pydantic 载荷")


def test_none_and_raw_string_payload_pass_through() -> None:
    """残余面（登记，不粉饰）：`None` / 裸 str 无键面可遮蔽 ⇒ 原样放行。

    生产态**到不了**这两个形态：两处调用点（`:2382` / `:2692-2694`）都已先收成 dict；
    裸 str 更早一步就会被 `dict("abc")` 抛 `ValueError`（既有行为，本笔不引入、不修改）。
    """
    assert _tool_args_for_steps("knowledge_search", None) is None
    raw = "raw-string-payload"
    assert _tool_args_for_steps("knowledge_search", raw) is raw
