"""条目 4.5.4 · 会话分组 + 归档七端点：``ChatGroup`` 字段表 + CRUD + ``chat_count`` 口径。

覆盖（契约 §1.2 的七行 + 冻结前端 ``ChatGroup`` / ``api.ts:1362-1397``）：

- ``ChatGroup`` = ``{id, name, sort, created_at, chat_count}`` **五键且只有这五键**；
- ``chat_count`` 只数**未归档的在库**会话（归档的、回收站里的都不计）；
- 新建空名 / 超长名走校验层（422），只剩空白走业务层（400 可读中文）——两条都不许 200/500；
- ``PUT`` 改名与排序可分开传（不传的字段不动），未知分组 404「分组不存在」；
- 删分组：组内会话回「最近」平铺（``group_id`` 置 NULL），**会话本身不删**；
- ``PUT /api/chats/{chat_id}/group``：``null`` = 移出分组；未知分组 / 未知会话 / 回收站里的会话 404；
- 归档 / 取消归档：与 ``GET /api/chats``（默认不含归档）、``GET /api/chats?archived=true``
  （只回归档）配合，幂等；回收站里的会话不能归档（404）。

夹具同 4.5.1：``client`` 从 ``tests.api.test_chat_contract`` 复用；本文件自带 ``clean_extra``
补清 ``session_groups``（公共夹具的清表清单没覆盖它）。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.session import repository as repo
from tests.api.test_chat_contract import client  # noqa: F401 —— 公共夹具（4.5.1）

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def clean_extra(db):
    """补清 ``session_groups``（其余表由 ``db`` 夹具负责）。"""
    await db.execute(text("DELETE FROM session_groups"))
    await db.commit()
    return db


async def _chat(db, title: str, *, archived: bool = False, deleted: bool = False):
    chat = await repo.create_chat(db, title=title)
    if archived:
        chat.archived_at = repo.utc_now()
    if deleted:
        chat.deleted_at = repo.utc_now()
    await db.commit()
    return chat


async def _group(client, name: str) -> dict:
    resp = await client.post("/api/chat-groups", json={"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- 字段表 + 列表 -----------------------------------------------------------


async def test_create_group_returns_the_five_contract_fields(client, clean_extra):  # noqa: F811
    """新建分组：响应的键集合 = 冻结前端 ``ChatGroup`` 的五个键（不多不少）。"""
    data = await _group(client, "工作")
    assert set(data) == {"id", "name", "sort", "created_at", "chat_count"}
    assert data["name"] == "工作"
    assert data["sort"] == 0
    assert data["chat_count"] == 0
    assert isinstance(data["created_at"], str)


async def test_list_groups_shape_and_chat_count(client, clean_extra):  # noqa: F811
    """列表：``chat_count`` 只数未归档的在库会话（归档 / 回收站里的都不计）。"""
    group = await _group(client, "甲组")
    live = await _chat(clean_extra, "在最近", )
    archived = await _chat(clean_extra, "已归档", archived=True)
    trashed = await _chat(clean_extra, "在回收站", deleted=True)
    for chat in (live, archived, trashed):
        chat.group_id = group["id"]
    await clean_extra.commit()

    resp = await client.get("/api/chat-groups")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["chat_count"] == 1, "只有未归档的在库会话计入角标"
    assert set(items[0]) == {"id", "name", "sort", "created_at", "chat_count"}


async def test_list_groups_orders_by_sort_then_id(client, clean_extra):  # noqa: F811
    """排序：``sort`` 升序、同序按创建先后（id 升序）。"""
    first = await _group(client, "第一")
    second = await _group(client, "第二")
    third = await _group(client, "第三")
    await client.put(f"/api/chat-groups/{third['id']}", json={"sort": -1})

    items = (await client.get("/api/chat-groups")).json()
    assert [item["id"] for item in items] == [third["id"], first["id"], second["id"]]


# --- 新建 / 改名 / 排序 ------------------------------------------------------


async def test_create_group_empty_name_is_400_or_422(client, clean_extra):  # noqa: F811
    """空名：422（校验层）；只剩空白：400（业务层）。**两条都不许 200/500**。"""
    empty = await client.post("/api/chat-groups", json={"name": ""})
    assert empty.status_code in (400, 422), empty.text
    blank = await client.post("/api/chat-groups", json={"name": "   "})
    assert blank.status_code == 400
    assert blank.json()["detail"] == "分组名不能为空"
    assert (await client.get("/api/chat-groups")).json() == []


async def test_create_group_too_long_name_is_422(client, clean_extra):  # noqa: F811
    """超长名（列宽 100）由校验层挡下，不写成半截。"""
    resp = await client.post("/api/chat-groups", json={"name": "长" * 101})
    assert resp.status_code == 422


async def test_update_group_partial_fields(client, clean_extra):  # noqa: F811
    """改名 / 排序不传就不动；空名 400；未知分组 404。"""
    group = await _group(client, "旧名")
    renamed = await client.put(f"/api/chat-groups/{group['id']}", json={"name": "新名"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "新名"
    assert renamed.json()["sort"] == 0, "只传 name 时 sort 不动"

    sorted_ = await client.put(f"/api/chat-groups/{group['id']}", json={"sort": 5})
    assert sorted_.json()["sort"] == 5
    assert sorted_.json()["name"] == "新名", "只传 sort 时 name 不动"

    blank = await client.put(f"/api/chat-groups/{group['id']}", json={"name": "  "})
    assert blank.status_code == 400
    assert blank.json()["detail"] == "分组名不能为空"

    missing = await client.put("/api/chat-groups/999999", json={"name": "无名"})
    assert missing.status_code == 404
    assert missing.json()["detail"] == "分组不存在"


# --- 删除分组 ---------------------------------------------------------------


async def test_delete_group_keeps_chats_and_rehomes_them(client, clean_extra):  # noqa: F811
    """删分组：组内会话回「最近」平铺（``group_id`` 置 NULL），会话本身一条不删。"""
    group = await _group(client, "临时组")
    chat = await _chat(clean_extra, "组内会话")
    await client.put(f"/api/chats/{chat.id}/group", json={"group_id": group["id"]})

    resp = await client.delete(f"/api/chat-groups/{group['id']}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "message": "分组已删除，组内会话回到「最近」"}

    assert (await client.get("/api/chat-groups")).json() == []
    listed = (await client.get("/api/chats")).json()
    assert [item["id"] for item in listed] == [chat.id]
    assert listed[0]["group_id"] is None, "会话回「最近」平铺，而不是跟着分组一起消失"

    missing = await client.delete("/api/chat-groups/999999")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "分组不存在"


# --- 会话归属 ---------------------------------------------------------------


async def test_set_chat_group_and_clear(client, clean_extra):  # noqa: F811
    """移入 / 移出分组：``group_id`` 传 null 表示移出；响应收敛成 ``{ok: true}``。"""
    group = await _group(client, "甲组")
    chat = await _chat(clean_extra, "会话")

    moved = await client.put(f"/api/chats/{chat.id}/group", json={"group_id": group["id"]})
    assert moved.status_code == 200
    assert moved.json() == {"ok": True}
    assert (await client.get("/api/chats")).json()[0]["group_id"] == group["id"]

    cleared = await client.put(f"/api/chats/{chat.id}/group", json={"group_id": None})
    assert cleared.status_code == 200
    assert (await client.get("/api/chats")).json()[0]["group_id"] is None


async def test_set_chat_group_404_cases(client, clean_extra):  # noqa: F811
    """未知分组 404「分组不存在」；未知会话 / 回收站里的会话 404「会话不存在」。"""
    group = await _group(client, "甲组")
    chat = await _chat(clean_extra, "在库会话")
    trashed = await _chat(clean_extra, "回收站会话", deleted=True)

    bad_group = await client.put(f"/api/chats/{chat.id}/group", json={"group_id": 999999})
    assert bad_group.status_code == 404
    assert bad_group.json()["detail"] == "分组不存在"

    missing_chat = await client.put("/api/chats/999999/group", json={"group_id": group["id"]})
    assert missing_chat.status_code == 404
    assert missing_chat.json()["detail"] == "会话不存在"

    trashed_chat = await client.put(f"/api/chats/{trashed.id}/group", json={"group_id": None})
    assert trashed_chat.status_code == 404
    assert trashed_chat.json()["detail"] == "会话不存在"


# --- 归档 / 取消归档 --------------------------------------------------------


async def test_archive_and_unarchive_cooperate_with_chats_list(client, clean_extra):  # noqa: F811
    """归档 ↔ ``GET /api/chats``（默认不含归档）/ ``?archived=true``（只回归档）。"""
    chat = await _chat(clean_extra, "要归档的会话")

    archived = await client.post(f"/api/chats/{chat.id}/archive")
    assert archived.status_code == 200
    assert archived.json() == {"ok": True}
    assert (await client.get("/api/chats")).json() == []
    only_archived = (await client.get("/api/chats?archived=true")).json()
    assert [item["id"] for item in only_archived] == [chat.id]
    assert only_archived[0]["archived_at"] is not None

    back = await client.post(f"/api/chats/{chat.id}/unarchive")
    assert back.status_code == 200
    assert back.json() == {"ok": True}
    assert (await client.get("/api/chats?archived=true")).json() == []
    assert [item["id"] for item in (await client.get("/api/chats")).json()] == [chat.id]


async def test_archive_is_idempotent_and_keeps_group(client, clean_extra):  # noqa: F811
    """重复归档幂等；归档保留分组归属（取消归档后回原分组）。"""
    group = await _group(client, "甲组")
    chat = await _chat(clean_extra, "会话")
    await client.put(f"/api/chats/{chat.id}/group", json={"group_id": group["id"]})

    first = await client.post(f"/api/chats/{chat.id}/archive")
    again = await client.post(f"/api/chats/{chat.id}/archive")
    assert first.status_code == again.status_code == 200
    await client.post(f"/api/chats/{chat.id}/unarchive")

    listed = (await client.get("/api/chats")).json()
    assert listed[0]["group_id"] == group["id"], "归档期间分组归属不该被清掉"
    assert listed[0]["archived_at"] is None


async def test_archive_rejects_trashed_or_missing_chat(client, clean_extra):  # noqa: F811
    """回收站里的会话按「不存在」处理（404），不会把它归档成「隐藏的垃圾」。"""
    trashed = await _chat(clean_extra, "回收站会话", deleted=True)
    resp = await client.post(f"/api/chats/{trashed.id}/archive")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "会话不存在"

    missing = await client.post("/api/chats/999999/unarchive")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "会话不存在"
