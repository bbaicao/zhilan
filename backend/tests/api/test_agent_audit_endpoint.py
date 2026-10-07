"""``GET /api/agent/audit`` 的**真库**行为用例（阶段 4 · 条目 4.3.2）。

这个端点在本条目之前是 501 占位（``app/api/agent_routes.py`` 只有 4 行），全仓**零行为断言**——
只有 ``tests/contract/test_endpoints_catalog.py`` 的一行端点存在性。本文件补上行为面：

- ``chat_id`` 过滤生效；
- ``limit`` 夹在 1..500（``0`` → 1 条、``9999`` → 500 条）；
- **倒序**（最新在前）；
- 响应形状 ``{"items": [...]}``，每项字段与 ``agent_steps`` 行 + 其 ``run`` 的关键字段对应；
- **只读**：不存在任何写/删入口。

数据是**直接造库**的（``AgentRun`` / ``AgentStep`` 行）——不经过 Agent 循环，
因为「读接口读得对不对」与「写入侧写得对不对」是两件事：前者用造行最干净，
后者在 ``tests/api/test_chat_agent_audit.py`` 用真链路钉。
"""

from __future__ import annotations

import pytest

from app.agent import audit as agent_audit
from app.models import AgentRun, AgentStep

# ``client`` 夹具（ASGITransport 打真 app + 覆盖 ``get_db``）定义在 ``test_chat_contract.py`` 里，
# 全目录共用那一份——**不另建一份**，否则「端点的 HTTP 形态」会有两个写法。
from tests.api.test_chat_contract import client  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _seed(db, *, count: int, mode: str = "react", ok: bool = True):  # noqa: ANN001
    """造一个**真会话** + 一个 run + ``count`` 条 step，返回 ``(chat_id, [step id 升序])``。

    ⚠️ 会话必须是真行：``agent_runs.chat_id`` 有外键（``ON DELETE SET NULL``），
    手搓一个不存在的 chat_id 会直接撞 FK。
    """
    from app.session import repository as repo

    chat = await repo.create_chat(db, title=f"审计会话-{count}-{mode}")
    await db.flush()
    run = AgentRun(chat_id=chat.id, mode=mode, status="done", meta={"retrieval_id": "rl-x"})
    db.add(run)
    await db.flush()
    ids: list[int] = []
    for index in range(1, count + 1):
        step = AgentStep(
            run_id=run.id,
            step_no=index,
            type="tool_call",
            tool_name="knowledge_search",
            tool_call_id=f"tc-{chat.id}-{index}",
            content=f"观察 {index}",
            meta={"ok": ok, "ms": 10 * index, "args": {"query": "蓝鲸"}},
        )
        db.add(step)
        await db.flush()
        ids.append(step.id)
    await db.commit()
    return chat.id, ids


async def test_items_are_returned_newest_first(client, db):  # noqa: ANN001
    """**倒序**：最新写入的那条排在最前（与老基线的「最新在前」同口径）。"""
    chat_id, ids = await _seed(db, count=3)
    resp = await client.get("/api/agent/audit", params={"chat_id": chat_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # ⚠️ 条目 5.5 把这条**升级**为「改前键逐字保留 + 只增」——**不是**删断言：
    # 改前恰好只有 ``items`` 一个键（`回归检查5改前形状.json`），5.5 只增了 3 个新键。
    assert {"items"} <= set(body), "改前的顶层键必须逐字保留"
    assert set(body) == {"items", "runs", "query", "counters"}, "改后恰好这 4 个顶层键"
    got = [item["id"] for item in body["items"]]
    assert got == list(reversed(ids))


async def test_item_shape_matches_step_plus_run_fields(client, db):  # noqa: ANN001
    """每项 = ``agent_steps`` 行 + 其 ``run`` 的关键字段（``mode`` / ``status``）。"""
    chat_id, _ = await _seed(db, count=1, mode="plan_execute")
    resp = await client.get("/api/agent/audit", params={"chat_id": chat_id})
    item = resp.json()["items"][0]
    # ⚠️ 同样**升级**（不是删）：条目 5.5 之前这 11 个键逐字钉死，改后只多第 12 键 ``run``
    # ——前 11 键在这里逐字列出，等于「改前键集必须原样保留」。
    assert set(item) == {
        "id",
        "run_id",
        "step_no",
        "type",
        "tool_name",
        "tool_call_id",
        "content",
        "meta",
        "created_at",
        "mode",
        "status",
        "run",  # 第 12 键（条目 5.5 只增），内容在 test_agent_audit_5_5.py 里逐字段对库
    }
    assert item["step_no"] == 1
    assert item["type"] == "tool_call"
    assert item["tool_name"] == "knowledge_search"
    assert item["tool_call_id"] == f"tc-{chat_id}-1"
    assert item["content"] == "观察 1"
    assert item["meta"]["ok"] is True
    assert item["meta"]["ms"] == 10
    assert item["mode"] == "plan_execute"
    assert item["status"] == "done"
    assert item["created_at"], "时间戳要能序列化出来"


async def test_chat_id_filter_is_effective(client, db):  # noqa: ANN001
    """``chat_id`` 过滤真的按会话收窄；不传则跨会话全给。"""
    first, _ = await _seed(db, count=2)
    second, _ = await _seed(db, count=3)

    scoped = await client.get("/api/agent/audit", params={"chat_id": first})
    assert len(scoped.json()["items"]) == 2
    assert {item["run_id"] for item in scoped.json()["items"]}

    everything = await client.get("/api/agent/audit")
    assert len(everything.json()["items"]) == 5
    assert {item["id"] for item in everything.json()["items"]} >= {
        item["id"] for item in scoped.json()["items"]
    }
    assert second  # 第二个会话确实造了数据


async def test_limit_is_clamped_to_one_through_five_hundred(client, db):  # noqa: ANN001
    """``limit`` 夹在 1..500：``0`` → 1 条、``9999`` → 500 条（老基线 ``max(1, min(..., 500))``）。"""
    chat_id, _ = await _seed(db, count=5)

    zero = await client.get("/api/agent/audit", params={"chat_id": chat_id, "limit": 0})
    assert zero.status_code == 200
    assert len(zero.json()["items"]) == 1

    small = await client.get("/api/agent/audit", params={"chat_id": chat_id, "limit": 3})
    assert len(small.json()["items"]) == 3

    huge = await client.get("/api/agent/audit", params={"chat_id": chat_id, "limit": 9999})
    assert len(huge.json()["items"]) == 5, "只有 5 行，夹到 500 也只给 5 行"


async def test_safe_limit_helper_is_pinned(client, db):  # noqa: ANN001
    """夹取规则的**单一实现**：``safe_limit`` 的取值域逐条钉住（端点与实现不各处写一份）。"""
    assert agent_audit.safe_limit(0) == 1
    assert agent_audit.safe_limit(-5) == 1
    assert agent_audit.safe_limit(1) == 1
    assert agent_audit.safe_limit(200) == 200
    assert agent_audit.safe_limit(500) == 500
    assert agent_audit.safe_limit(501) == 500
    assert agent_audit.safe_limit(9999) == 500
    assert agent_audit.safe_limit(None) == 200, "非法值回落默认 200（读接口不该因坏参数 500）"
    assert agent_audit.safe_limit("abc") == 200


async def test_empty_table_returns_empty_items(client, db):  # noqa: ANN001
    """没数据时给 ``{"items": []}``，不是 404/500（审计是排查用的，读不出来不该炸界面）。"""
    resp = await client.get("/api/agent/audit", params={"chat_id": 999})
    assert resp.status_code == 200
    body = resp.json()
    # ⚠️ 升级（条目 5.5）：改前是 ``== {"items": []}``，现在只断言 ``items`` 仍逐字为空数组，
    # 并**显式**把新增的 3 个键钉成空集——少一个都算「改前形状被改掉」。
    assert body["items"] == []
    assert body["runs"] == []
    assert body["query"]["count"] == 0
    assert set(body) == {"items", "runs", "query", "counters"}


async def test_failed_step_is_recorded_with_ok_false(client, db):  # noqa: ANN001
    """**成败都记**：``meta.ok=False`` 的行照样读得出来（失败恰好最需要回放）。"""
    chat_id, _ = await _seed(db, count=1, ok=False)
    item = (await client.get("/api/agent/audit", params={"chat_id": chat_id})).json()["items"][0]
    assert item["meta"]["ok"] is False


async def test_endpoint_is_read_only(client):  # noqa: ANN001
    """**只读**：只提供 GET，没有任何写/删入口（老基线原话，照做）。"""
    for method in ("post", "put", "patch"):
        resp = await getattr(client, method)("/api/agent/audit", json={})
        assert resp.status_code in (404, 405), f"{method.upper()} 不该被接受：{resp.status_code}"
    resp = await client.delete("/api/agent/audit")
    assert resp.status_code in (404, 405), f"DELETE 不该被接受：{resp.status_code}"
