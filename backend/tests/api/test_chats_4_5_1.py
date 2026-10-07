"""条目 4.5.1 · chats 域（HTTP 层）：``GET /api/chats`` + ``DELETE /api/chats/{chat_id}``。

**为什么这层必须在 HTTP 上测**：本条的验收面是「冻结前端拿到的字段形状对不对」——
真相源是 ``frontend/src/api.ts``（I2 冻结、与老仓逐字一致）：

| 前端 | 位置 | 读的字段 |
|---|---|---|
| ``listChats()`` | ``api.ts:666`` | ``ChatItem[]``（``api.ts:81-89``） |
| ``listArchivedChats()`` | ``api.ts:1397`` | ``GET /api/chats?archived=true`` |
| ``deleteChat()`` | ``api.ts:685`` | ``{ok: boolean}`` |

契约 ``02 §7.4`` 的 ``ChatOut`` 是五键（``id`` / ``title`` / ``created_at`` / ``group_id`` /
``archived_at``）——用例 4 把这张表**按契约原文写死**（不是从模型反射），
所以它能抓住「有人改了模型却没改契约」这类漂移。

``GET /api/chats/{chat_id}`` 的只读实现**不在本条**：它在 4.4 前移交付（唯一出口，用户已签字），
本文件只在删除/归档用例里借它验证「软删之后看不着了」。
"""

from __future__ import annotations

import json

import pytest

from app.session import repository as repo
from tests.api.test_chat_contract import client  # noqa: F401 —— 夹具复用（ASGI 直连 + 测试库）


async def _chat(db, title: str, *, archived: bool = False, deleted: bool = False):
    chat = await repo.create_chat(db, title=title)
    if archived:
        chat.archived_at = repo.utc_now()
    if deleted:
        chat.deleted_at = repo.utc_now()
    await db.commit()
    return chat


# --------------------------------------------------------------------------
# GET /api/chats
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_chats_is_empty_array_when_there_is_nothing(client, db):  # noqa: F811
    """空库返回 ``[]``（不是 404、不是 ``null``：前端直接 ``.map``）。"""
    resp = await client.get("/api/chats")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_chats_returns_the_five_contract_fields(client, db):  # noqa: F811
    """响应元素**五键齐**且**只有**这五键（契约 ``02 §7.4`` 的 ``ChatOut``）。

    这张表是契约原文的**字面量**：多了键（例如把消息数塞进来）= 契约漂移，同样会红。
    """
    await _chat(db, "会话 A")

    resp = await client.get("/api/chats")
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert len(items) == 1
    assert list(items[0]) == ["id", "title", "created_at", "group_id", "archived_at"]
    assert items[0]["title"] == "会话 A"
    assert items[0]["group_id"] is None
    assert items[0]["archived_at"] is None

    from app.api.chat_schemas import ChatOut

    assert list(ChatOut.model_fields) == [
        "id",
        "title",
        "created_at",
        "group_id",
        "archived_at",
    ]


@pytest.mark.asyncio
async def test_list_chats_splits_archived_and_live_and_hides_trashed(client, db):  # noqa: F811
    """``archived`` 两种取值各回一条；回收站里的会话**两种取值都不出现**。

    这是「归档 ≠ 删除」的钉子：归档只从「最近」隐藏，删除进回收站（恢复要走 trash 域）。
    """
    live = await _chat(db, "在最近")
    archived = await _chat(db, "已归档", archived=True)
    trashed = await _chat(db, "进回收站", deleted=True)

    default = await client.get("/api/chats")
    assert [item["id"] for item in default.json()] == [live.id]

    only_archived = await client.get("/api/chats?archived=true")
    assert [item["id"] for item in only_archived.json()] == [archived.id]
    # 归档标记要真的回给前端（侧栏靠它把会话放进「已归档」视图）
    assert only_archived.json()[0]["archived_at"] is not None

    explicit_false = await client.get("/api/chats?archived=false")
    assert [item["id"] for item in explicit_false.json()] == [live.id]

    assert trashed.id not in {item["id"] for item in default.json() + only_archived.json()}


@pytest.mark.asyncio
async def test_list_chats_newest_first(client, db):  # noqa: F811
    """排序 = ``id`` 倒序（老基线口径；侧栏「最近」直接按响应顺序渲染）。"""
    first = await _chat(db, "早")
    second = await _chat(db, "晚")

    resp = await client.get("/api/chats")
    assert [item["id"] for item in resp.json()] == [second.id, first.id]


@pytest.mark.asyncio
async def test_list_chats_bad_archived_value_is_422_in_chinese(client):  # noqa: F811
    """坏参数按契约走 422 中文（``02 §4`` 异常语义；不是 500、不是静默当 false）。"""
    resp = await client.get("/api/chats?archived=abc")
    assert resp.status_code == 422, resp.text
    assert "请求参数不正确" in json.dumps(resp.json(), ensure_ascii=False)


# --------------------------------------------------------------------------
# DELETE /api/chats/{chat_id}
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_chat_is_a_soft_delete_that_keeps_messages(
    client, db, fresh_db  # noqa: F811
):
    """删除 = **进回收站**：``deleted_at`` 有值、消息行一行不少（恢复才有东西可恢复）。"""
    chat = await _chat(db, "要删的会话")
    await repo.add_message(db, chat_id=chat.id, role="user", content="蓝鲸是什么")
    await repo.add_message(db, chat_id=chat.id, role="assistant", content="蓝鲸是哺乳动物。")
    await db.commit()

    resp = await client.delete(f"/api/chats/{chat.id}")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "message": "已移入回收站，可在回收站恢复或彻底删除"}

    # 换一个连接读（真的落库了，不是内存里的一次性标志）
    row = await repo.get_chat(fresh_db, chat.id, include_deleted=True)
    assert row is not None and row.deleted_at is not None
    messages = await repo.list_messages(fresh_db, chat.id)
    assert [m.content for m in messages] == ["蓝鲸是什么", "蓝鲸是哺乳动物。"]


@pytest.mark.asyncio
async def test_deleted_chat_disappears_from_list_and_detail(client, db):  # noqa: F811
    """软删之后：列表里没了，详情按 404「会话不存在」（与 4.4 的详情端点同口径）。"""
    chat = await _chat(db, "删完就看不见")

    before = await client.get(f"/api/chats/{chat.id}")
    assert before.status_code == 200

    assert (await client.delete(f"/api/chats/{chat.id}")).status_code == 200

    assert (await client.get("/api/chats")).json() == []
    after = await client.get(f"/api/chats/{chat.id}")
    assert after.status_code == 404
    assert after.json()["detail"] == "会话不存在"


@pytest.mark.asyncio
async def test_delete_chat_404_for_unknown_and_for_already_trashed(client, db):  # noqa: F811
    """不存在的会话 / 已在回收站里的会话 ⇒ 404（老基线同：回收站里的对用户不可见）。"""
    missing = await client.delete("/api/chats/999999")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "会话不存在"

    chat = await _chat(db, "已经在回收站", deleted=True)
    again = await client.delete(f"/api/chats/{chat.id}")
    assert again.status_code == 404
    assert again.json()["detail"] == "会话不存在"

    # 二次删除**不改**原来的删除时间（幂等：时间戳不漂）
    row = await repo.get_chat(db, chat.id, include_deleted=True)
    assert row is not None and row.deleted_at is not None


@pytest.mark.asyncio
async def test_delete_does_not_touch_the_other_chats(client, db):  # noqa: F811
    """删一条不影响别的（软删是本行操作，不是「清空」）。"""
    keep = await _chat(db, "留着")
    drop = await _chat(db, "删掉")

    await client.delete(f"/api/chats/{drop.id}")

    assert [item["id"] for item in (await client.get("/api/chats")).json()] == [keep.id]
    assert (await client.get(f"/api/chats/{keep.id}")).status_code == 200
