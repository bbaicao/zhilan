"""专项 · 工具调用能力第 2 批 · **第 0 笔 F4**：未知 skill id ⇒ 400（A0-1）。

原先「未知 id 被静默忽略并真发模型」（`回归修复` 同源静默吞意图）；F4 在
`validate_request` 的 skill 预检处（既有 `unavailable` 分支紧邻、任何写库/调模型之前）
改为 **400 + 逐字冻结中文文案**。

判据（开工包 §4 A0-1）：
① 400；② `detail` 逐字等于冻的那句；③ 零模型调用（`call_kinds() == []`）；
④ `Chat` 表 0 行；⑤ 合法 id 与空 `skill_ids` 两条腿不受影响（成对）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.api import skills_spec
from app.api.chat_routes import UNKNOWN_SKILL_ID_PREFIX, UNKNOWN_SKILL_ID_SUFFIX
from app.models import Chat
from tests.api.t0_wire import (  # noqa: F401 —— client 是夹具
    Wire,
    attach_wire,
    client,
    wire_env,
    wire_sse_text,
)

#: 用例**另抄一份字面量**（D8 先例：`test_rerank_provenance_029.py`），
#: 不从实现 import 拼好的整句，避免「实现改了用例跟着绿」。
_FROZEN_PREFIX = "未知的技能 id："
_FROZEN_SUFFIX = "。可在设置 → 技能 / MCP 里查看可用技能。"


def _frozen_detail(*ids: str) -> str:
    return _FROZEN_PREFIX + "、".join(ids) + _FROZEN_SUFFIX


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
async def test_a0_1_unknown_skill_id_is_400_before_any_model_call(
    client, wire_env, monkeypatch, path, db
):
    """A0-1 ①②③④：未知 id ⇒ 400 + 逐字文案 + 零模型 + 零 Chat 行。"""
    wire = Wire(wire_sse_text("不该被调用"))
    attach_wire(wire, monkeypatch)
    monkeypatch.setattr(skills_spec, "list_skills", lambda: [{
        "id": "builtin:knowledge", "name": "知识库", "enabled": True, "available": True,
    }])
    response = await client.post(
        path, json={"message": "你好", "skill_ids": ["builtin:does_not_exist"]}
    )
    assert response.status_code == 400
    assert response.json()["detail"] == _frozen_detail("builtin:does_not_exist")
    assert wire.call_kinds() == [], wire.call_kinds()
    assert await db.scalar(select(func.count()).select_from(Chat)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
async def test_a0_1_multiple_unknown_ids_preserve_request_order(
    client, wire_env, monkeypatch, path
):
    """多个未知 id 按**请求顺序**以「、」列出（不是 set 迭代序）。"""
    wire = Wire(wire_sse_text("不该被调用"))
    attach_wire(wire, monkeypatch)
    monkeypatch.setattr(skills_spec, "list_skills", lambda: [{
        "id": "builtin:knowledge", "name": "知识库", "enabled": True, "available": True,
    }])
    response = await client.post(
        path,
        json={
            "message": "你好",
            "skill_ids": ["zzz:second", "builtin:knowledge", "aaa:first", "zzz:second"],
        },
    )
    assert response.status_code == 400
    # 去重后仍按首次出现顺序：zzz:second → aaa:first（builtin:knowledge 已知，不进文案）
    assert response.json()["detail"] == _frozen_detail("zzz:second", "aaa:first")
    assert wire.call_kinds() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
async def test_a0_1_legal_and_empty_skill_ids_still_pass(client, wire_env, monkeypatch, path, db):
    """A0-1 ⑤ 成对腿：合法 id / 空 `skill_ids` / 不传 ⇒ 200（不受 F4 影响）。"""
    wire = Wire(wire_sse_text("正常回答"))
    attach_wire(wire, monkeypatch)
    monkeypatch.setattr(skills_spec, "list_skills", lambda: [{
        "id": "builtin:knowledge", "name": "知识库", "enabled": True, "available": True,
    }])

    ok = await client.post(path, json={"message": "你好", "skill_ids": ["builtin:knowledge"]})
    assert ok.status_code == 200

    empty = await client.post(path, json={"message": "你好", "skill_ids": []})
    assert empty.status_code == 200

    omitted = await client.post(path, json={"message": "你好"})
    assert omitted.status_code == 200

    # 至少真的走到了模型（不是被预检拦掉的假绿）
    assert wire.call_kinds() != []


@pytest.mark.asyncio
async def test_a0_1_frozen_constants_match_copied_literals():
    """单一定义：实现里的前后缀与用例另抄的字面量逐字一致（D8 先例）。"""
    assert UNKNOWN_SKILL_ID_PREFIX == _FROZEN_PREFIX
    assert UNKNOWN_SKILL_ID_SUFFIX == _FROZEN_SUFFIX


@pytest.mark.asyncio
async def test_a0_1_unknown_message_has_single_definition_in_app():
    """`PLACEHOLDER_DETAIL` 先例：冻的前后缀在 `backend/app/` 下只允许一处定义。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "app"
    hits: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if _FROZEN_PREFIX in text:
            hits.append(str(path))
    assert hits == ["{}".format(root / "api" / "chat_routes.py")] or set(hits) == {
        str(root / "api" / "chat_routes.py")
    }, f"`{_FROZEN_PREFIX}` 应只在 chat_routes.py 定义一次，实际出现在：{hits}"
