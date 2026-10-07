"""回归修复 定位探针：**写笔记的 PUT 路径**是否维护 `wiki_links`。

🔴 **口径（逐字，防误读）**

· 本文件是 **082 的定位探针**（钉「**哪条路径**下双链不落边」这个**现状**），**不是修复验证**；
· 其中若出现**现状断言**（断「边没落」），它**绿 = 现状被如实钉住**，⛔ **绿 ≠ 082 已修**；
· ⛔ 禁止在本文件的文件名 / 用例名 / 文案里出现「已修 | 修复 | fixed」等暗示已修的措辞。

背景（为什么单开一个文件）
--------------------------
`回归修复`（原登记 `F1`）的两条读数**互相矛盾**：

· **判据面是绿的** —— `test_notes_4_5_2.py::test_saving_a_note_writes_wiki_edges`（`:509-537`）断
  「`POST /api/notes` 正文里的 `[[标题]]` 真的落进 `wiki_links`」，现读实跑 44 passed；
· **真机手测判 FAIL** —— `阶段5-增强评估与切换/证据/S5-真机/手测档B.md:68` 与 `:466-470`：
  在笔记里写 `[[标题]]` 后 `wiki_links` **24 → 24，一条边都不生成**。

⇒ 二者并存的唯一解释只能是「**它们走的不是同一条路径**」。
既有载体覆盖的是 **POST（新建）**；真机那条路径（**PUT（覆盖保存）** / 前端流程）**此前无任何载体**。
本文件补的正是后者 —— 把「PUT 这条路径今天到底落不落边」钉死。

⚠️ 本文件是**判据载体 + 只读取证**：不改任何产品代码；若下面出现红，**红本身就是交付物**（它证明 082 可复现）。

夹具复用（与既有载体同口径）
----------------------------
`client` 从 `tests.api.test_chat_contract` 导入（本仓既有做法：不改公共 conftest）；
`tests/api/conftest.py` 的 autouse 隔离自动生效（向量路假件 / 重排关闭 / 联网关闭）。
⚠️ `wiki_links` / `notes` **不在** `tests/conftest.py::_CLEAN_ORDER` 里 ⇒ 本文件自带清理（照 `test_notes_4_5_2.py`）。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.models import Note, WikiLink
from tests.api.test_chat_contract import client  # noqa: F401

_DOMAIN_TABLES = ("wiki_links", "notes")


@pytest_asyncio.fixture(autouse=True)
async def clean_domain_tables(db):
    """用例前清空本域两张表（顺序：先边后端点）—— 与 `test_notes_4_5_2.py:41-45` 同口径。"""
    for table in _DOMAIN_TABLES:
        await db.execute(text(f"DELETE FROM {table}"))
    await db.commit()
    yield


async def _add_note(db, title: str, content: str = "正文") -> Note:
    note = Note(title=title, content=content, category="")
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return note


async def _edges_of(db, source_id: int) -> dict:
    """取该来源笔记的全部边（先换快照，才看得到 client 会话刚提交的写）。"""
    await db.rollback()
    rows = list(
        (
            await db.execute(
                select(WikiLink).where(
                    WikiLink.source_type == "note", WikiLink.source_id == source_id
                )
            )
        )
        .scalars()
        .all()
    )
    return {edge.target_title: edge for edge in rows}


# ===========================================================================
# 一、PUT（覆盖保存）—— 真机那条路径
# ===========================================================================


@pytest.mark.asyncio
async def test_put_overwrite_writes_wiki_edges(client, db):
    """**真机路径**：`PUT /api/notes/{id}` **写入内容**后，`[[标题]]` 是否落进 `wiki_links`。

    与 `test_saving_a_note_writes_wiki_edges` 的**唯一差别 = 走 PUT 而不是 POST** ——
    这正是 `回归修复` 里「绿用例」与「真机 FAIL」之间**唯一未取证的那一跳**。

    断言盯的还是**表里有没有行**（不是「函数被调用了」），与既有载体同口径。
    """
    target = await _add_note(db, "乙", "乙的正文")
    target_id = int(target.id)  # rollback 会让 ORM 属性过期，先取出成 int

    created = await client.post("/api/notes", json={"title": "甲", "content": "初始正文，无链接。"})
    assert created.status_code == 200, created.text
    source_id = int(created.json()["id"])

    # ↓↓↓ 真机路径：**覆盖保存**时把 [[乙]] 写进去 ↓↓↓
    updated = await client.put(
        f"/api/notes/{source_id}",
        json={"content": "改后正文：详见 [[乙]] 与 [[还没写]]。"},
    )
    assert updated.status_code == 200, updated.text

    by_title = await _edges_of(db, source_id)

    assert set(by_title) == {"乙", "还没写"}, (
        f"PUT 覆盖保存后 wiki_links 未按正文重建；实得 target_title = {sorted(by_title)}。"
        f"（POST 路径的同类断言在 test_notes_4_5_2.py:509）"
    )
    assert by_title["乙"].resolved is True
    assert by_title["乙"].target_note_id == target_id
    assert by_title["还没写"].resolved is False, "未创建的目标是「未创建」边，不是丢弃"


@pytest.mark.asyncio
async def test_put_overwrite_removes_stale_edges(client, db):
    """**真机路径（对偶面）**：PUT 把正文里的 `[[..]]` **删掉**后，旧边是否随之清掉。

    「重建」若不是真的重建，最常见的两种病就是：① 旧的没删；② 新的没加。
    上一条钉 ②，本条钉 ①。
    """
    created = await client.post("/api/notes", json={"title": "甲", "content": "详见 [[乙]]。"})
    assert created.status_code == 200, created.text
    source_id = int(created.json()["id"])

    updated = await client.put(f"/api/notes/{source_id}", json={"content": "改后正文：链接已删掉。"})
    assert updated.status_code == 200, updated.text

    by_title = await _edges_of(db, source_id)
    assert by_title == {}, (
        f"正文里的 [[..]] 已删除，但 wiki_links 仍留 {len(by_title)} 条陈旧边：{sorted(by_title)}"
    )

# ===========================================================================
# 二、剩余候选路径（真机 FAIL 的另两种可能形态）
# ===========================================================================


@pytest.mark.asyncio
async def test_late_target_creation_flips_edge_to_resolved(client, db):
    """**候选 ③**：引用者**先**建、目标**后**建 ⇒ 那条边是否从「未创建」转「已解析」。

    这是真机那条路径的另一种形态：用户**先**在笔记里写下 `[[SAD]]`，**后来**才建「SAD」。
    若这一跳不通，用户看到的就是「双链一直不生效」（与手测「24 → 24」同观感）。
    走的还是 **PUT**（覆盖保存），与真机手测的操作序一致。
    """
    created = await client.post("/api/notes", json={"title": "甲", "content": "初始正文。"})
    assert created.status_code == 200, created.text
    source_id = int(created.json()["id"])

    # 先在引用者里写下指向「尚未存在」的标题
    updated = await client.put(f"/api/notes/{source_id}", json={"content": "详见 [[稍后才建]]。"})
    assert updated.status_code == 200, updated.text
    before = await _edges_of(db, source_id)
    assert set(before) == {"稍后才建"}, f"边未落：{sorted(before)}"
    assert before["稍后才建"].resolved is False, "此时目标还不存在 ⇒ 应为「未创建」边"

    # 目标现在才出现（POST 新建）
    late = await client.post("/api/notes", json={"title": "稍后才建", "content": "我来了"})
    assert late.status_code == 200, late.text
    late_id = int(late.json()["id"])

    after = await _edges_of(db, source_id)
    assert after["稍后才建"].resolved is True, (
        "目标笔记已创建，但指向它的边仍停在「未创建」⇒ 反向解析那一跳没生效"
        "（`wikilink.refresh_resolved_for_title` 的调用点在 note_service.maintain_links）"
    )
    assert after["稍后才建"].target_note_id == late_id


@pytest.mark.asyncio
async def test_saving_note_with_preexisting_content_grows_edges(client, db):
    """**候选 ④（真机手测的字面口径）**：笔记**本来就有** `[[..]]`，保存后 `wiki_links` 是否**增长**。

    真机手测的原话是「`wiki_links` **24 → 24，一条边都不生成**」—— 断的是**全表行数有没有涨**，
    而不是「某条来源有没有边」。本条就照那个口径写：**比较保存前后的全表行数**。
    """
    from sqlalchemy import func

    await _add_note(db, "乙", "乙的正文")
    created = await client.post(
        "/api/notes",
        json={"title": "甲", "content": "这一段**一开始就**带 [[乙]]。"},
    )
    assert created.status_code == 200, created.text
    source_id = int(created.json()["id"])

    await db.rollback()
    total_after_create = int(await db.scalar(select(func.count()).select_from(WikiLink)) or 0)
    assert total_after_create >= 1, "新建时就带 [[乙]] ⇒ 全表至少 1 条边"

    # 再覆盖保存一次（正文里仍有 [[乙]]）—— 真机手测的动作就是这个
    updated = await client.put(
        f"/api/notes/{source_id}",
        json={"content": "这一段**一开始就**带 [[乙]]，外加 [[丙]]。"},
    )
    assert updated.status_code == 200, updated.text

    await db.rollback()
    total_after_update = int(await db.scalar(select(func.count()).select_from(WikiLink)) or 0)
    assert total_after_update == 2, (
        f"覆盖保存后全表边数应为 2（乙 + 丙），实得 {total_after_update}（保存前 {total_after_create}）"
    )

