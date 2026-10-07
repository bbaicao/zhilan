"""`回归修复` 判据载体 —— 知识库 `name` / `path_or_url` 纯空白必须 **422**，不得建出空名库。

**纯空白字段的校验风险**：
`KnowledgeBaseIn`（`backend/app/api/note_routes.py:167-173`）两个字段只有
`Field(min_length=1, max_length=255)` / `Field(min_length=1, max_length=1024)`，
**`min_length` 数的是字符数、不是 strip 后长度** ⇒ `"   "` 通过校验；
`create_knowledge_base`（`backend/app/api/note_routes.py:390-405`）到**落库前**才
`payload.name.strip()`（`:402`）/`payload.path_or_url.strip()`（`:404`）
⇒ **库里出现 `name == ""` 的知识库**（`name` 是前端列表显示名与引用面字段；
`path_or_url` 空 ⇒ 登记进来的库指向空路径）。修前实测：`POST` 返回 **200** 且落库 `name == ""`。

**修法（本载体钉住的最小改动）**：两字段各加一个 `field_validator`
（`strip()` 后为空 ⇒ `raise ValueError` ⇒ Pydantic `RequestValidationError` ⇒ **422**）；
`max_length`/`min_length` 与落库 `strip()` **一律保留**。
- **契约依据**（⛔ 本阶段不改契约文档，升版归 S6）：`接口说明/02-API契约冻结清单.md:282`
  全局登记 `RequestValidationError → 422 {"detail": "请求参数不正确，请检查后重试"}`；
  `:181` 的 `POST /api/knowledge-bases` 行**不枚举状态码** ⇒ 本笔属**全局语义的收紧**、非契约偏离。
- ⛔ **非法 `kind` 仍 400**（`note_routes.py:399-400`「只能登记导入库或外部库」）：
  `kind` 是**端点体内**的显式判定、不经 Pydantic ⇒ 本笔不得把它改写成 422。
- ⚠️ **两错并存**（空白 `name` **且** 非法 `kind`）⇒ Pydantic 校验先于端点体 ⇒ 应 **422 优先**；
  本载体按「字段合法 + `kind` 非法 ⇒ 400」构造，不把顺序问题混进判据。

**腿构成**：① `name` 纯空白族（空格/制表/换行/全角空格…）⇒ 422 且**零落库**；
② `path_or_url` 纯空白族 ⇒ 422 且零落库；③ `" kb " ⇒ 200` 且**库内** `name == "kb"`
（既有 strip 语义不得被取消）；④ 非法 `kind` ⇒ **仍 400**（⛔ 不得变 422）；
⑤ 反向保护：`name == ""` / 缺 `name` / 超 `max_length` ⇒ 仍 422，正常建库仍 200。

🔴 **未覆盖（如实登记）**：其它写 `knowledge_bases` 的路径（`local` 自举 / 导入）不走本端点、
不在本载体面内（见阶段 S5 §10）。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

from app.models import KnowledgeBase
from tests.api.test_chat_contract import client, faux_model  # noqa: F401

# `knowledge_bases` 不在 `tests/conftest.py::_CLEAN_ORDER` 里 ⇒ 夹具落在本文件（同 test_notes_4_5_2.py 口径）。
_TABLE = "knowledge_bases"

#: 纯空白族：Python `str.strip()` 认这些（含全角空格 `\u3000`），但 `min_length=1` 都放行。
_BLANKS = [" ", "   ", "\t", "\n", "\r\n", "\u3000", " \t \u3000 "]


@pytest_asyncio.fixture(autouse=True)
async def clean_kb_table(db):
    """用例前后清空 `knowledge_bases`（自举是惰性且幂等的，清掉不影响其它模块）。"""
    await db.execute(text(f"DELETE FROM {_TABLE}"))
    await db.commit()
    yield
    await db.execute(text(f"DELETE FROM {_TABLE}"))
    await db.commit()


async def _kb_rows(db) -> list[KnowledgeBase]:
    await db.rollback()  # 换快照才数得准 client 会话刚写进去的行（同 test_notes_4_5_2.py 口径）
    return list((await db.execute(select(KnowledgeBase))).scalars().all())


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", _BLANKS)
async def test_blank_name_is_rejected_and_not_persisted(client, db, blank):
    """腿①：`name` 纯空白 ⇒ 422，且**不得**落库成 `name == ""` 的库。"""
    resp = await client.post(
        "/api/knowledge-bases",
        json={"name": blank, "kind": "imported", "path_or_url": "D:/kb"},
    )
    assert resp.status_code == 422, f"name={blank!r} 应 422，实际 {resp.status_code}：{resp.text}"
    assert await _kb_rows(db) == [], "校验失败不得落库"


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", _BLANKS)
async def test_blank_path_or_url_is_rejected_and_not_persisted(client, db, blank):
    """腿②：`path_or_url` 纯空白 ⇒ 422，且零落库。"""
    resp = await client.post(
        "/api/knowledge-bases",
        json={"name": "团队库", "kind": "imported", "path_or_url": blank},
    )
    assert resp.status_code == 422, f"path_or_url={blank!r} 应 422，实际 {resp.status_code}：{resp.text}"
    assert await _kb_rows(db) == [], "校验失败不得落库"


@pytest.mark.asyncio
async def test_surrounding_whitespace_still_stripped_on_persist(client, db):
    """腿③：合法名两端的空白**仍须**被落库 `strip()` 清掉（不得为治空白名而取消 strip）。"""
    resp = await client.post(
        "/api/knowledge-bases",
        json={
            "name": " kb ",
            "kind": "imported",
            "path_or_url": "  D:/kb  ",
            "collection": "  team  ",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "kb"
    assert body["path_or_url"] == "D:/kb"
    assert body["collection"] == "team"

    rows = await _kb_rows(db)
    assert len(rows) == 1
    assert rows[0].name == "kb", "库内真身也必须是 strip 后的值"
    assert rows[0].path_or_url == "D:/kb"
    assert rows[0].collection == "team"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["local", "unknown", ""])
async def test_illegal_kind_still_400_not_422(client, db, kind):
    """腿④（反向保护）：非法 `kind` **仍 400**「只能登记导入库或外部库」——⛔ 不得变 422。"""
    resp = await client.post(
        "/api/knowledge-bases",
        json={"name": "x", "kind": kind, "path_or_url": "p"},
    )
    assert resp.status_code == 400, f"kind={kind!r} 应 400，实际 {resp.status_code}：{resp.text}"
    assert resp.json()["detail"] == "只能登记导入库或外部库"
    assert await _kb_rows(db) == [], "非法 kind 不得落库"


@pytest.mark.asyncio
async def test_existing_validation_semantics_unchanged(client, db):
    """腿⑤（反向保护）：`min_length=1` / `max_length` 既有语义不得退化，正常建库仍 200。"""
    # 空串 / 缺字段：修前修后都必须 422（min_length 与必填语义保留）
    assert (
        await client.post(
            "/api/knowledge-bases", json={"name": "", "kind": "imported", "path_or_url": "p"}
        )
    ).status_code == 422
    assert (
        await client.post("/api/knowledge-bases", json={"kind": "imported", "path_or_url": "p"})
    ).status_code == 422
    assert (
        await client.post(
            "/api/knowledge-bases", json={"name": "x", "kind": "imported", "path_or_url": ""}
        )
    ).status_code == 422
    # max_length=255 不得被本笔顺手删掉（256 字符 ⇒ 422）
    assert (
        await client.post(
            "/api/knowledge-bases",
            json={"name": "x" * 256, "kind": "imported", "path_or_url": "p"},
        )
    ).status_code == 422
    # path_or_url 的 max_length=1024 同理
    assert (
        await client.post(
            "/api/knowledge-bases",
            json={"name": "x", "kind": "imported", "path_or_url": "p" * 1025},
        )
    ).status_code == 422
    assert await _kb_rows(db) == [], "以上五笔均不得落库"

    # happy path 不受影响
    ok = await client.post(
        "/api/knowledge-bases", json={"name": "正常库", "kind": "external", "path_or_url": "http://x"}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["name"] == "正常库"
    assert len(await _kb_rows(db)) == 1


@pytest.mark.asyncio
async def test_blank_name_with_illegal_kind_is_422_first(client, db):
    """腿⑥（口径登记）：两错并存 ⇒ **422 优先**（Pydantic 校验先于端点体的 kind 判定）。

    ⚠️ 这是**顺序事实**，不是新增语义：本笔未改端点体顺序；登记在此以免后人误以为
    「空白名 + 非法 kind」应得 400。
    """
    resp = await client.post(
        "/api/knowledge-bases",
        json={"name": "   ", "kind": "local", "path_or_url": "p"},
    )
    assert resp.status_code == 422, f"实际 {resp.status_code}：{resp.text}"
    assert await _kb_rows(db) == []
