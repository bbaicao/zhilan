"""S6 · 186 判据：knowledge_bases 唯一身份在 HTTP 面的行为（重复 409 / 并发 / rollback 可用）。

覆盖交付项 C（backend/app/api/note_routes.py:419-474 create_knowledge_base）与 Lead 决策 7：
IntegrityError -> rollback -> 按摘要 key 回查 -> 只有完整 path_or_url 逐字相同才 409。

为什么不能吞成 200：唯一身份落在 DB 唯一索引上（0007），并发两个同地址请求必须只有一个成功；
把 IntegrityError 吞成 200 会让第二个请求拿到一个「其实不存在第二次登记」的库。

夹具范式照抄 tests/api/test_chat_contract.py:74-105（每个请求一个新 session）——
两个请求才是真两会话，并发重复才会真的走到 DB 唯一索引，而不是被应用内串行化挡掉。
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.models import KnowledgeBase

KB_URL = "/api/knowledge-bases"
#: Lead 定稿文案（逐字断言；改文案必须同笔改这里）
DUPLICATE_CODE = "duplicate_knowledge_base"
DUPLICATE_MESSAGE = "该地址已登记为知识库，请勿重复创建。"
#: 长共同前缀：前缀唯一索引 / 截断长度方案会在这里撞号，完整 sha256 不会
LONG_PREFIX = "知识库/共享目录/" * 60


@pytest_asyncio.fixture
async def client(db, engine) -> AsyncClient:
    """ASGI 直连客户端（不起真端口）。

    必须覆盖 get_db：app/db/session.py 的 engine 是进程级单例，跨事件循环会炸
    （详见 test_chat_contract.py 同名夹具的注释）。
    """
    from app.db.session import get_db
    from main import app
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


def _payload(name: str, path: str, *, kind: str = "imported", collection: str = "c") -> dict:
    return {"name": name, "kind": kind, "path_or_url": path, "collection": collection}


async def _kb_rows(db, path: str | None = None) -> list[KnowledgeBase]:
    """换快照后读 KB 行（client 用的是另一个 session，必须先 rollback）。"""
    await db.rollback()
    stmt = select(KnowledgeBase)
    if path is not None:
        stmt = stmt.where(KnowledgeBase.path_or_url == path)
    return list((await db.execute(stmt)).scalars().all())


@pytest.mark.asyncio
async def test_sequential_duplicate_post_returns_409_with_exact_detail(client, db):
    first = await client.post(KB_URL, json=_payload("一号", "D:/s6/kb"))
    assert first.status_code == 200, first.text
    existing_id = first.json()["id"]

    second = await client.post(KB_URL, json=_payload("二号", "D:/s6/kb"))
    assert second.status_code == 409, second.text
    assert second.json()["detail"] == {
        "code": DUPLICATE_CODE,
        "message": DUPLICATE_MESSAGE,
        "existing_id": existing_id,
    }, second.text

    rows = await _kb_rows(db, "D:/s6/kb")
    assert len(rows) == 1, "唯一冲突不许落第二行"
    assert rows[0].id == existing_id
    assert rows[0].name == "一号", "留下的是先登记的那一行"
    assert rows[0].path_or_url_key is not None, "登记行必须写入身份摘要列"
    assert len(rows[0].path_or_url_key) == 64


@pytest.mark.asyncio
async def test_strip_variant_is_the_same_identity(client):
    first = await client.post(KB_URL, json=_payload("先", "D:/s6/kb-strip"))
    assert first.status_code == 200, first.text
    assert first.json()["path_or_url"] == "D:/s6/kb-strip", "存的是 strip 之后的值"

    padded = await client.post(KB_URL, json=_payload("后", "   D:/s6/kb-strip   "))
    assert padded.status_code == 409, padded.text
    assert padded.json()["detail"]["existing_id"] == first.json()["id"]


@pytest.mark.asyncio
async def test_different_identities_are_both_allowed(client):
    a = await client.post(KB_URL, json=_payload("A", "D:/s6/a"))
    b = await client.post(KB_URL, json=_payload("B", "D:/s6/b"))
    assert (a.status_code, b.status_code) == (200, 200), (a.text, b.text)
    assert a.json()["id"] != b.json()["id"]


@pytest.mark.asyncio
async def test_long_common_prefix_with_different_tail_is_allowed(client, db):
    left = LONG_PREFIX + "左尾"
    right = LONG_PREFIX + "右尾"
    assert left != right and len(left) == len(right)

    a = await client.post(KB_URL, json=_payload("左", left))
    b = await client.post(KB_URL, json=_payload("右", right))
    assert (a.status_code, b.status_code) == (200, 200), (a.text, b.text)

    rows = await _kb_rows(db)
    keys = {r.path_or_url_key for r in rows if r.name in {"左", "右"}}
    assert len(keys) == 2, "两条长共同前缀地址的唯一身份必须不同"
    assert all(k is not None and len(k) == 64 for k in keys)


@pytest.mark.asyncio
async def test_full_length_chinese_path_is_allowed(client, db):
    path = "库" * 1024
    resp = await client.post(KB_URL, json=_payload("满长", path))
    assert resp.status_code == 200, resp.text
    assert resp.json()["path_or_url"] == path

    rows = await _kb_rows(db, path)
    assert len(rows) == 1
    assert len(rows[0].path_or_url) == 1024
    assert len(rows[0].path_or_url_key) == 64

    over = await client.post(KB_URL, json=_payload("越界", "库" * 1025))
    assert over.status_code == 422, over.text


@pytest.mark.asyncio
async def test_local_bootstrap_row_stays_out_of_the_unique_domain(client, db):
    listed = await client.get(KB_URL)
    assert listed.status_code == 200, listed.text
    assert [r["id"] for r in listed.json()] == [1]

    created = await client.post(KB_URL, json=_payload("之后", "D:/s6/after-bootstrap"))
    assert created.status_code == 200, created.text

    rows = await _kb_rows(db)
    by_id = {r.id: r for r in rows}
    assert set(by_id) == {1, created.json()["id"]}
    assert by_id[1].path_or_url == ""
    assert by_id[1].path_or_url_key is None, "空 path 不入唯一域（NULL 不参与唯一约束）"
    assert by_id[created.json()["id"]].path_or_url_key is not None


@pytest.mark.asyncio
async def test_session_survives_the_conflict_and_recovers(client):
    first = await client.post(KB_URL, json=_payload("原", "D:/s6/recover"))
    assert first.status_code == 200, first.text
    dup = await client.post(KB_URL, json=_payload("重", "D:/s6/recover"))
    assert dup.status_code == 409, dup.text

    listed = await client.get(KB_URL)
    assert listed.status_code == 200, "409 回滚之后连接必须仍可用"
    recovered = await client.post(KB_URL, json=_payload("新", "D:/s6/recover-2"))
    assert recovered.status_code == 200, recovered.text


@pytest.mark.asyncio
async def test_concurrent_duplicate_posts_let_exactly_one_win(client, db):
    payload = _payload("并发", "D:/s6/kb-race")
    results = await asyncio.gather(
        client.post(KB_URL, json=payload),
        client.post(KB_URL, json=payload),
    )
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], [r.text for r in results]

    winners = [r.json()["id"] for r in results if r.status_code == 200]
    losers = [r.json()["detail"]["existing_id"] for r in results if r.status_code == 409]
    assert len(winners) == 1
    assert losers == winners, "409 里的 existing_id 必须指向唯一成功的那一行"

    rows = await _kb_rows(db, "D:/s6/kb-race")
    assert len(rows) == 1, [r.id for r in rows]
