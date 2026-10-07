"""``回归修复``：``GET /api/agent/audit`` 在**数据库真失败**时必须 503（现状 200 + 空列表）。

缺陷 = ``app/agent/audit.py`` 的读取侧把**一切**异常都吞成 ``warning + []``，于是
``backend/main.py:170-172`` 既有的 ``@app.exception_handler(OperationalError)``
（⇒ 503 + ``database_unavailable_hint()``）**永远触发不到**：库挂了被伪装成「没有记录」。

修法（最小面，只在 ``read_audit`` 内）= DB 面真失败（``OperationalError``）上抛，
其余读失败照旧降级成空列表。五条腿逐条对应验收口径：

| 腿 | 验收点 |
|---|---|
| 1 | 真驱动层（SQLite 打不开的库路径）⇒ 端点 503 |
| 2 | 会话层显式 ``OperationalError`` ⇒ 端点 503 |
| 3 | 单元：``read_audit`` 对 ``OperationalError`` 上抛 |
| 4 | 既有降级契约**不得**被一起打死：``read_audit(None) == []`` |
| 5 | 非 DB 面读失败仍走原降级：200 + ``items == []`` |

⛔ 本文件只覆盖 HTTP / 单元语义面；不涉及「正常 UI 是否可达」之类前端论断。
"""

from __future__ import annotations

import contextlib

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agent import audit as agent_audit
from app.db.session import get_db

pytestmark = pytest.mark.asyncio


class _BoomSession:
    """只实现 ``execute`` 的替身会话（``read_audit`` 只用这一个方法）。"""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def execute(self, _stmt):  # noqa: ANN001, ANN201 —— 与 AsyncSession 同形的最小替身
        raise self.exc


@contextlib.asynccontextmanager
async def _client_with_db(session):  # noqa: ANN001, ANN202
    """ASGI 直连客户端，并把 ``get_db`` 钉到**给定会话**（本条要的是坏会话，不是测试库会话）。"""
    from main import app

    async def _override_get_db():
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_leg1_real_driver_failure_returns_503(tmp_path):
    """腿 1：真驱动层 —— SQLite 库文件打不开（父目录不存在）⇒ 端点 503。"""
    from main import database_unavailable_hint

    db_file = tmp_path / "missing-dir" / "audit.db"
    assert not db_file.parent.exists(), "父目录必须不存在，否则这条腿不成立"

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with _client_with_db(factory()) as client:
            resp = await client.get("/api/agent/audit")
    finally:
        await engine.dispose()

    assert resp.status_code == 503, f"断库必须 503（现状被吞成 200 空列表）：{resp.status_code} {resp.text}"
    assert resp.json() == {"detail": database_unavailable_hint()}


async def test_leg2_session_level_operational_error_returns_503():
    """腿 2：会话层显式 ``OperationalError`` ⇒ 端点 503（不吞成空列表）。"""
    from main import database_unavailable_hint

    boom = _BoomSession(OperationalError("SELECT 1", {}, Exception("数据库不可用")))
    async with _client_with_db(boom) as client:
        resp = await client.get("/api/agent/audit")

    assert resp.status_code == 503
    assert resp.json() == {"detail": database_unavailable_hint()}


async def test_leg3_read_audit_reraises_operational_error():
    """腿 3：单元面 —— ``read_audit`` 对 ``OperationalError`` **上抛**（修法本体）。"""
    boom = _BoomSession(OperationalError("SELECT 1", {}, Exception("down")))
    with pytest.raises(OperationalError):
        await agent_audit.read_audit(boom)


async def test_leg4_none_db_still_degrades_to_empty_list():
    """腿 4：既有降级契约**不得**被一起打死 —— ``db is None`` 仍返回空列表。"""
    assert await agent_audit.read_audit(None) == []


async def test_leg5_non_db_failure_still_degrades_to_200_empty():
    """腿 5：非 DB 面读失败仍走原降级 ⇒ 200 + 空列表（收窄只动 DB 面）。"""
    boom = _BoomSession(RuntimeError("非 DB 面故障：语句构造阶段出错"))
    async with _client_with_db(boom) as client:
        resp = await client.get("/api/agent/audit")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"] == []
    assert body["query"]["count"] == 0
