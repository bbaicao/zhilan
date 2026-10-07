"""S6 · 条目 186 取证：`POST /api/knowledge-bases` 重复身份的 **HTTP 语义**。

产品侧真源（本文件只读，不改 `backend/app/**` 一个字节）：
- `backend/app/api/note_routes.py:419-...` `create_knowledge_base`：kind 只允许
  `imported` / `external`（否则 400 `只能登记导入库或外部库`），落库时
  `path_or_url = payload.path_or_url.strip()`、
  `path_or_url_key = app.core.kb_identity.kb_path_key(path_or_url)`，然后裸 `await db.commit()`。
- 撞唯一索引 `uk_knowledge_bases_path_or_url_key` ⇒ `IntegrityError` ⇒ `await db.rollback()`
  ⇒ `_find_knowledge_base_by_key` 回查 ⇒ 回查行的 `path_or_url` 与本次请求**经同一
  `kb_canonical_path` 判定同值**（老库若存有带首尾空白的字面值行，同一地址照样 409 而不是 500）⇒
  `HTTPException(409, detail={"code": "duplicate_knowledge_base",
  "message": "该地址已登记为知识库，请勿重复创建。", "existing_id": <int>})`。
- 身份口径（S6 §2 业主确认「按建议」）：身份 = **strip 之后**的 `path_or_url` 完整逐字值，
  **不新增路径/URL 归一**；大小写不同 = **不同身份**；`kind` / `collection` 不参与身份。

与 `backend/tests/test_s6_186_kb_identity.py` 的分工：那边是迁移 / schema / ORM 层
（摘要算法、唯一索引真实存在、存量合并、fail-closed、downgrade）；本文件补开工包 §6.3
最小判据清单里的 **HTTP 顺序重复 / 并发（旁路插入模拟）重复 / 冲突 rollback 后仍可用**。

判别力：detail 多一键/少一键、message 文案漂移、`existing_id` 被转成 str、大小写被隐式归一
（第二次 POST 也回 409）、strip 语义丢失（带首尾空白被当成新身份）、IntegrityError 分支不
rollback（后续 POST 撞脏会话报 500）、或把非唯一冲突吞成 200 —— 本文件对应腿必红。
"""

from __future__ import annotations

import pytest

from app.core.kb_identity import kb_path_key
from app.models import KnowledgeBase

from tests.api.test_chat_contract import client  # noqa: F401  复用 ASGI 客户端夹具

pytestmark = pytest.mark.asyncio

KB_URL = "/api/knowledge-bases"
DUP_CODE = "duplicate_knowledge_base"
DUP_MESSAGE = "该地址已登记为知识库，请勿重复创建。"
DUP_KEYS = {"code", "message", "existing_id"}


def _body(name: str, path: str, *, kind: str = "imported", collection: str = "") -> dict:
    return {"name": name, "kind": kind, "path_or_url": path, "collection": collection}


async def _post(client, name: str, path: str, *, kind: str = "imported", collection: str = ""):
    return await client.post(KB_URL, json=_body(name, path, kind=kind, collection=collection))


async def _rows(client) -> list[dict]:
    response = await client.get(KB_URL)
    assert response.status_code == 200, response.text
    return response.json()


async def _assert_dup_detail(response, *, existing_id: int) -> None:
    """409 的 detail 精确三键 + 逐值逐类型（多一键/少一键都必须红）。"""
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, dict), f"409 的 detail 必须是对象，实得 {type(detail).__name__}"
    assert set(detail) == DUP_KEYS, (
        f"detail 键集必须精确等于 {sorted(DUP_KEYS)}；"
        f"多 {sorted(set(detail) - DUP_KEYS)} / 少 {sorted(DUP_KEYS - set(detail))}"
    )
    assert detail["code"] == DUP_CODE, detail["code"]
    assert detail["message"] == DUP_MESSAGE, (
        f"409 文案必须逐字为 {DUP_MESSAGE!r}，实得 {detail['message']!r}"
    )
    assert isinstance(detail["existing_id"], int) and not isinstance(detail["existing_id"], bool), (
        f"existing_id 必须是 int，实得 {type(detail['existing_id']).__name__}"
    )
    assert detail["existing_id"] == existing_id


async def test_second_post_same_path_is_409_with_exact_detail(client):  # noqa: F811
    """顺序重复：同一 path_or_url 第二次 POST ⇒ 409 精确三键，且**不新增行**。"""
    path = "D:/s6-186/route/sequential"
    first = await _post(client, "S6-186 顺序", path)
    assert first.status_code == 200, first.text
    first_id = first.json()["id"]

    second = await _post(client, "S6-186 顺序（重名）", path)
    await _assert_dup_detail(second, existing_id=first_id)

    rows = [r for r in await _rows(client) if r["path_or_url"] == path]
    assert len(rows) == 1, f"重复 POST 不许落第二行，实得 {len(rows)} 行"
    assert rows[0]["id"] == first_id


async def test_whitespace_strip_is_the_same_identity(client):  # noqa: F811
    """strip 之后同值即同身份：带首尾空白的第二次 POST 仍 409，落库值是 strip 后的值。"""
    path = "D:/s6-186/route/strip"
    first = await _post(client, "S6-186 strip", path)
    assert first.status_code == 200, first.text
    first_id = first.json()["id"]

    second = await _post(client, "S6-186 strip（空白）", f"   {path}  ")
    await _assert_dup_detail(second, existing_id=first_id)

    row = [r for r in await _rows(client) if r["id"] == first_id][0]
    assert row["path_or_url"] == path, (
        f"落库必须是 strip() 之后的值 {path!r}，实得 {row['path_or_url']!r}"
    )


async def test_case_difference_is_a_different_identity(client):  # noqa: F811
    """决策登记：**不新增归一** ⇒ 仅大小写不同的地址是两条独立知识库（都成功）。"""
    upper = "D:/s6-186/route/CaseSensitive"
    lower = "D:/s6-186/route/casesensitive"
    first = await _post(client, "S6-186 大写", upper)
    assert first.status_code == 200, first.text
    second = await _post(client, "S6-186 小写", lower)
    assert second.status_code == 200, (
        "身份 = strip 后的完整逐字值，大小写不同即不同身份；"
        f"若这里回 {second.status_code} 说明引入了未登记的归一；响应体={second.text}"
    )
    ids = {r["path_or_url"]: r["id"] for r in await _rows(client) if r["path_or_url"] in (upper, lower)}
    assert set(ids) == {upper, lower}, f"两条地址都必须各自存在，实得 {sorted(ids)}"
    assert ids[upper] != ids[lower]


async def test_integrity_error_branch_and_session_usable_after_conflict(client, db):  # noqa: F811
    """并发窗口模拟：旁路 ORM 先落一行（POST 端不做前置查重）⇒ 走 IntegrityError 分支 409，
    且 rollback 之后同一客户端**还能建新库**（会话没被失败事务粘死）。"""
    path = "D:/s6-186/route/race"
    bypass = KnowledgeBase(name="S6-186 旁路", kind="imported", path_or_url=path)
    db.add(bypass)
    await db.commit()
    await db.refresh(bypass)
    bypass_id = int(bypass.id)

    raced = await _post(client, "S6-186 并发", path)
    await _assert_dup_detail(raced, existing_id=bypass_id)

    fresh_path = "D:/s6-186/route/after-conflict"
    after = await _post(client, "S6-186 冲突之后", fresh_path)
    assert after.status_code == 200, (
        "唯一冲突分支必须 `await db.rollback()` 把会话放出失败事务；"
        f"这里失败（{after.status_code}）说明后续请求撞了脏会话；响应体={after.text}"
    )
    assert after.json()["path_or_url"] == fresh_path
    rows = [r for r in await _rows(client) if r["path_or_url"] == path]
    assert len(rows) == 1 and rows[0]["id"] == bypass_id


async def test_kind_and_collection_do_not_participate_in_identity(client):  # noqa: F811
    """身份只按 path_or_url：kind / collection 不同、地址相同 ⇒ 仍 409。"""
    path = "D:/s6-186/route/kind-blind"
    first = await _post(client, "S6-186 imported", path, kind="imported", collection="coll-a")
    assert first.status_code == 200, first.text
    first_id = first.json()["id"]

    second = await _post(client, "S6-186 external", path, kind="external", collection="coll-b")
    await _assert_dup_detail(second, existing_id=first_id)


async def test_padded_legacy_literal_row_still_conflicts_with_a_clean_address(client, db):  # noqa: F811
    """老库字面值带首尾空白（0007 只回填 key、**不改写** path_or_url 字面值）时，
    POST 干净地址必须 409 而不是 500 —— 409 判据走 canonical 而不是裸字节比较。

    判别力：把 :463 的判据退回裸 `existing.path_or_url == path_or_url` ⇒ 这里
    IntegrityError 直接冒泡成 500（该 500 就是本腿要打红的面）。
    """
    clean = "D:/s6-186/route/legacy-padded"
    legacy = KnowledgeBase(name="S6-186 老库空白字面值", kind="imported", path_or_url=f"  {clean}  ")
    db.add(legacy)
    await db.commit()
    await db.refresh(legacy)
    assert legacy.path_or_url == f"  {clean}  ", "前提：老库行字面值保留首尾空白"
    assert legacy.path_or_url_key == kb_path_key(clean), (
        "前提：该行的 key 是按 strip 后的值算的（0007 回填口径），所以干净地址会撞唯一索引"
    )

    response = await _post(client, "S6-186 干净地址", clean)
    await _assert_dup_detail(response, existing_id=int(legacy.id))


async def test_unknown_kind_is_400_not_409(client):  # noqa: F811
    """反向保护：kind 越界仍走既有 400（⛔ 不许被 186 的唯一身份逻辑吞成 409/200）。"""
    response = await _post(client, "S6-186 local", "D:/s6-186/route/kind-guard", kind="local")
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "只能登记导入库或外部库"
