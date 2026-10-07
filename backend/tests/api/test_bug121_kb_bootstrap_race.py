"""`回归修复`：`GET /api/knowledge-bases` 的**空表自举**在并发下撞主键 ⇒ 幂等化。

缺陷形态（改前）：两个请求都在对方提交前读到空表 ⇒ 都插 `id=1` ⇒ 后者撞主键抛
`IntegrityError` ⇒ 裸 500。修法 = 捕获 → `rollback` → **重查**：重查到 id=1 行就照常返回
（别人已经建好了），重查仍无 id=1 行则**原样抛出**（绝不吞成 200）。

腿构成与口径（**两条并发腿都是编排态**，交付里如实登记，不是自然并发复现）：

- 腿① `test_a1`：两个**真并发请求**（同一 `AsyncClient`，各自一个请求级会话），交错点由
  `AsyncSession.commit` 钩子编排（A 的提交先挂起 ⇒ 放 B 跑完整轮并提交 `id=1` ⇒ 再放 A 提交）；
  **异常来自真实 DB 主键约束**（MySQL 1062 / SQLite UNIQUE），不是注入的假异常。
- 腿② `test_a2`：方言无关的等价构造 —— 库里**已有** id=1 行（另一个请求已提交），编排让**首次
  `SELECT` 返回空结果集**（= 改前那个「看不到别人已提交行」的窗口），A 的插入于是撞真实主键。
- 腿③（负控）：表是空的，`commit` 注入一个 `IntegrityError` ⇒ 重查仍无 id=1 行 ⇒ 必须**原样抛出**
  （证明修法不是「一律 200」）。
- 腿④⑤（正控）：空表自举的既有行为逐字不变 / 已有行时不再自举。

改前读数（回退实验）：腿①②红（裸 `IntegrityError` 逃到客户端），腿③④⑤绿（对照腿）。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeBase
from tests.api.test_chat_contract import client  # noqa: F401  —— 复用 ASGI 直连客户端（已覆盖 get_db）


class _EmptyRows:
    """一个「空结果集」替身：只实现被测代码用到的两个方法（`.scalars().all()`）。"""

    def scalars(self) -> "_EmptyRows":
        return self

    def all(self) -> list:
        return []


def _local_row() -> KnowledgeBase:
    """本机库那一行（字段与端点自举时逐字相同）。"""
    return KnowledgeBase(
        id=1,
        name="本机知识库",
        kind="local",
        path_or_url="",
        collection="",
        read_only=False,
        status="ready",
    )


async def _count(db) -> int:
    """换快照后数行（`db` 夹具的会话可能停在旧快照上）。"""
    await db.rollback()
    return (await db.execute(select(func.count(KnowledgeBase.id)))).scalar_one()


@pytest.mark.asyncio
async def test_a1_interleaved_bootstrap_is_idempotent(client, db, monkeypatch):
    """① 真并发两请求 + 编排交错点：两个响应都必须 200，且库里只有一行 id=1。

    ⚠️ **编排态**：交错点由 `AsyncSession.commit` 钩子编排（A 的提交挂起 ⇒ B 跑完整轮 ⇒ 放行 A），
    不是自然并发复现；但 `IntegrityError` **真的**由 DB 主键约束抛出。改前：A 的提交把它抛穿成裸 500。
    """
    orig_commit = AsyncSession.commit
    state = {"armed": True}
    a_blocked = asyncio.Event()
    release_a = asyncio.Event()

    async def hooked_commit(self):  # noqa: ANN001
        if state["armed"]:
            state["armed"] = False
            a_blocked.set()
            await release_a.wait()
        return await orig_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", hooked_commit)

    task_a = asyncio.create_task(client.get("/api/knowledge-bases"))
    await asyncio.wait_for(a_blocked.wait(), timeout=10)  # A 已查过空表、正要提交
    resp_b = await client.get("/api/knowledge-bases")  # B 跑完整轮（自举并提交 id=1）
    release_a.set()
    resp_a = await asyncio.wait_for(task_a, timeout=10)

    assert resp_b.status_code == 200, resp_b.text
    assert resp_a.status_code == 200, resp_a.text
    for body in (resp_a.json(), resp_b.json()):
        assert [row["id"] for row in body] == [1]
        assert body[0]["name"] == "本机知识库"
    assert await _count(db) == 1, "并发自举不得落下两行"


@pytest.mark.asyncio
async def test_a2_stale_snapshot_plus_real_duplicate_key_returns_existing_row(client, db, monkeypatch):
    """② 方言无关的编排构造：库里已有 id=1 行，但首次 SELECT 被编排成「看不见」⇒
    端点插入撞**真实主键约束** ⇒ 修后必须吞掉冲突并返回既有行（200 + 一行），不是 500。

    ⚠️ **编排态**：`SELECT` 的「看不见」是编排出来的（等价于改前的并发窗口），`IntegrityError` 是真的。
    """
    db.add(_local_row())
    await db.commit()

    orig_execute = AsyncSession.execute
    state = {"armed": True}

    async def blind_first_execute(self, *args, **kwargs):  # noqa: ANN001
        if state["armed"]:
            state["armed"] = False
            return _EmptyRows()
        return await orig_execute(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", blind_first_execute)
    try:
        resp = await client.get("/api/knowledge-bases")
    finally:
        monkeypatch.setattr(AsyncSession, "execute", orig_execute)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [row["id"] for row in body] == [1]
    assert body[0]["name"] == "本机知识库"
    assert await _count(db) == 1, "幂等返回不得再插一行"


@pytest.mark.asyncio
async def test_a3_conflict_without_local_row_is_re_raised(client, db, monkeypatch):
    """③ 负控：表是空的 + `commit` 抛 `IntegrityError` ⇒ 重查仍无 id=1 行 ⇒ **原样抛出**。

    这条钉住「不得把别的 IntegrityError 一并吞成 200」。改前改后都绿（对照腿）。
    """
    orig_commit = AsyncSession.commit
    state = {"raised": False}

    async def failing_commit(self):  # noqa: ANN001
        if not state["raised"]:
            state["raised"] = True
            raise IntegrityError("INSERT INTO knowledge_bases ...", {}, Exception("模拟非自举冲突"))
        return await orig_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", failing_commit)
    with pytest.raises(IntegrityError):
        await client.get("/api/knowledge-bases")
    assert await _count(db) == 0, "被拒的冲突不得留下半截行"


@pytest.mark.asyncio
async def test_a4_normal_bootstrap_unchanged(client, db):
    """④ 正控：空表自举的既有行为逐字不变（200 / 一行 id=1 / 第二次仍一行）。"""
    resp = await client.get("/api/knowledge-bases")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [row["id"] for row in body] == [1]
    assert body[0]["name"] == "本机知识库" and body[0]["kind"] == "local"
    assert body[0]["read_only"] is False and body[0]["status"] == "ready"

    again = await client.get("/api/knowledge-bases")
    assert [row["id"] for row in again.json()] == [1]
    assert await _count(db) == 1


@pytest.mark.asyncio
async def test_a5_existing_row_skips_bootstrap(client, db):
    """⑤ 正控：已有 id=1 行时走原路径（不自举、不写库），响应体就是那一行。"""
    db.add(_local_row())
    await db.commit()

    resp = await client.get("/api/knowledge-bases")
    assert resp.status_code == 200, resp.text
    assert [row["id"] for row in resp.json()] == [1]
    assert await _count(db) == 1
