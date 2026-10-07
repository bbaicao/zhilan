"""回归修复 判据载体：索引更新不能让旧点占用当前召回或误删仍可读版本。

历史修复通过删除旧整数点避免孤儿；U2 改为 UUID 构建点、SQL 当前指针与保留历史版本：
成功重建须保留旧块和旧向量用于引用、回滚；失败只清本轮点，旧启用版本继续可读。
当前点在 TOP_K 之前过滤的判据在 ``tests/vector/test_u2_spaces.py``。

本件保留产品入口的版本保留、失败清理隔离、清理失败计数和硬删全部历史判据；
``_stale_point_ids`` / ``_orphan_point_ids`` 的整数旧点工具检查继续保留。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, select, text

from app.core import derived_counters
from app.core.config import settings
from app.models import Chunk, Document, IndexBuild
from app.pipeline import ingest
from app.pipeline.builds import current_chunks, document_snapshot
from app.vector.base import VectorHit

_CONTENT = "# 孤儿点\n\n就地重试应保留旧完整版本，只启用本轮完整向量点。\n" * 3

# 邻居文档正文（必须与 _CONTENT 不同内容，否则会撞内容级重复判定）
_CONTENT_NEIGHBOR = "# 邻居文档\n\n用来验证目标文档的失败清理不误删另一篇的向量。\n" * 3


# --------------------------------------------------------------------------
# 夹具（照 tests/pipeline/test_ingest.py:24-63 的既有范式）
# --------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _clean_documents(db):
    """每个用例前清 documents/chunks（保证本文件单独跑时也不受别的用例残留影响）。"""
    await db.execute(text("DELETE FROM chunks"))
    await db.execute(text("DELETE FROM documents"))
    await db.commit()
    yield


@pytest.fixture(autouse=True)
def _reset_counters():
    """进程内计数跨用例泄漏（本仓踩过）⇒ 每条用例前后各清零一次。"""
    derived_counters.reset_failure_counts()
    yield
    derived_counters.reset_failure_counts()


@pytest.fixture(autouse=True)
def _upload_dir(monkeypatch, tmp_path):
    """把上传留档目录重定向到 tmp_path（property 不能被实例属性覆盖，改类属性）。"""
    monkeypatch.setattr(type(settings), "upload_path", property(lambda self: tmp_path))
    return tmp_path


class _FakeEmbed:
    """给定维度的确定性 embed 桩（不连真 embedding、不出网）。"""

    dim = 4

    async def embed_texts(self, texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class _RecordingStore:
    """**记录型假 store**：记录 upsert / delete 收到的 id，并给「删点时文档是什么状态」留快照。

    - ``status_at_delete`` 用**另一条同步连接**现读（mark_ready 已 commit 才能读到 ready）
      ⇒ 「删点发生在提交之后」这件事是**机器可判**的，不靠注释；
    - ``deleted_batches`` 记录删除尝试；``fail_delete=True`` 时点保留并抛错（腿②）。
    """

    def __init__(self, sync_engine, *, fail_delete: bool = False):
        self.sync_engine = sync_engine
        self.fail_delete = fail_delete
        self.fail_upsert = False
        self.points = {}
        self.upsert_calls = 0
        self.upserted_ids: list[str] = []
        self.delete_calls = 0
        self.deleted_batches: list[list[str]] = []
        self.status_at_delete: list[str | None] = []
        self.deleted_at_at_delete: list[bool | None] = []

    # --- VectorStore 协议面 ---
    def for_space(self, space):
        # ponytail: 记录桩统一存 UUID 点；真实集合隔离由 test_u2_spaces 验证。
        return self

    def ensure_collection(self, dim: int | None = None) -> None:
        return None

    def upsert(self, points) -> None:
        self.upsert_calls += 1
        self.upserted_ids.extend(str(getattr(p, "id", p)) for p in points)
        self.points.update((str(point.id), point) for point in points)
        if self.fail_upsert:
            raise RuntimeError("测试注入：Qdrant 写点失败")

    def get_by_ids(self, ids):
        return [
            VectorHit(id=str(point_id), score=0.0, payload=self.points[str(point_id)].payload)
            for point_id in ids if str(point_id) in self.points
        ]

    def delete(self, ids) -> None:
        self.delete_calls += 1
        batch = [str(i) for i in ids]
        self.deleted_batches.append(batch)
        status, deleted = self._peek_latest_doc()
        self.status_at_delete.append(status)
        self.deleted_at_at_delete.append(deleted)
        if self.fail_delete:
            raise RuntimeError("测试注入：Qdrant 删点失败")
        for point_id in batch:
            self.points.pop(point_id, None)

    # --- 观测辅助 ---
    @property
    def deleted_id_union(self) -> set[str]:
        """delete 尝试删除的 id **并集**（不假定一次调用传全部）。"""
        return {i for batch in self.deleted_batches for i in batch}

    def _peek_latest_doc(self) -> tuple[str | None, bool | None]:
        """用同步连接现读最新文档行的 ``(index_status, 是否在回收站)``。

        为什么用同步连接：``delete``` 是在事件循环里被同步调用的，这里**不能**再
        ``asyncio.run`` 起一个新循环（会撞「cannot be called from a running event loop」）。
        """
        with self.sync_engine.connect() as conn:
            row = conn.execute(
                text("SELECT index_status, deleted_at FROM documents ORDER BY id DESC LIMIT 1")
            ).first()
        if row is None:
            return None, None
        return row[0], row[1] is not None


@pytest.fixture
def _sync_engine(test_sync_url):
    """与用例同一个测试库的**同步**引擎（只用于假 store 里的状态快照）。"""
    eng = create_engine(test_sync_url, pool_pre_ping=True)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def _install_store(monkeypatch):
    """装上假 embed + 假 store；返回工厂，便于按腿取不同配置。"""

    def _factory(sync_engine, *, fail_delete: bool = False) -> _RecordingStore:
        store = _RecordingStore(sync_engine, fail_delete=fail_delete)
        monkeypatch.setattr(ingest, "get_embedding_client", lambda *, fake=False: _FakeEmbed())
        monkeypatch.setattr(ingest, "get_vector_store", lambda **kw: store)
        return store

    return _factory


async def _rows(db) -> list[Document]:
    await db.rollback()
    return list((await db.execute(select(Document).order_by(Document.id))).scalars().all())


async def _chunk_ids(db, doc_id: int) -> list[int]:
    await db.rollback()
    return list(
        (await db.execute(select(Chunk.id).where(Chunk.document_id == doc_id).order_by(Chunk.id)))
        .scalars()
        .all()
    )


async def _manifest(db, doc_id: int) -> tuple[str, list[str]]:
    doc = await db.get(Document, doc_id, populate_existing=True)
    build = await db.get(IndexBuild, doc.active_build_id, populate_existing=True)
    assert build is not None and build.state == "published"
    return build.id, list(build.point_ids)


async def _create_neighbor(db, src_dir) -> Document:
    """建另一篇真实入库文档，验证失败清理只影响目标的本轮点。"""
    path = src_dir / "neighbor.md"
    path.write_text(_CONTENT_NEIGHBOR, encoding="utf-8")
    result = await ingest.ingest_document(
        db, path.name, path.read_bytes(), stored_path=str(path)
    )
    return await db.get(Document, result.document_id)


# --------------------------------------------------------------------------
# 腿① 主腿：重试发布新版本，旧完整版本保留
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_keeps_complete_previous_version_after_commit(
    db, tmp_path, _sync_engine, _install_store
):
    """两种 SQL 方言均保留旧行，启用指针只切到通过完整校验的新 UUID 构建。"""
    store = _install_store(_sync_engine)
    src = tmp_path / "orphan.md"
    src.write_text(_CONTENT, encoding="utf-8")

    first = await ingest.ingest_document(db, src.name, src.read_bytes(), stored_path=str(src))
    assert first.vectorized is True, "前提：假 store 应让向量写成功"
    old_ids = await _chunk_ids(db, first.document_id)
    assert old_ids, "前提：第一次入库必须写出分块"
    old_build, old_points = await _manifest(db, first.document_id)

    # 第二次入库同一行（reindex → retry_document 的重建主路）
    second = await ingest.retry_document(db, first.document_id)
    assert second.document_id == first.document_id
    new_ids = [row.id for row in await current_chunks(db, first.document_id)]
    assert new_ids, "第二次入库必须写出分块"
    new_build, new_points = await _manifest(db, first.document_id)
    assert second.vectorized is True and new_build != old_build
    assert set(new_ids).isdisjoint(old_ids), "旧块未删，两种方言均不应复用仍存活的 id"
    assert set(await _chunk_ids(db, first.document_id)) == set(old_ids) | set(new_ids)
    assert (await db.get(IndexBuild, old_build, populate_existing=True)).state == "retired"
    assert set(old_points).isdisjoint(new_points)
    assert {hit.id for hit in store.get_by_ids(old_points + new_points)} == set(old_points + new_points)
    assert store.delete_calls == 0, "成功重建不能删除用于回滚的旧完整版本"


@pytest.mark.asyncio
async def test_failed_candidate_cleanup_error_preserves_active_and_bumps_counter(
    db, tmp_path, _sync_engine, _install_store
):
    """腿②：失败候选的点清不掉仍有计数，旧启用点和邻居点不进入删除批。"""
    store = _install_store(_sync_engine, fail_delete=True)
    src = tmp_path / "orphan.md"
    src.write_text(_CONTENT, encoding="utf-8")

    first = await ingest.ingest_document(db, src.name, src.read_bytes(), stored_path=str(src))
    old_build, old_points = await _manifest(db, first.document_id)
    neighbor = await _create_neighbor(db, tmp_path)
    neighbor_id = neighbor.id
    _, neighbor_points = await _manifest(db, neighbor_id)
    written_before = len(store.upserted_ids)
    before = sum(derived_counters.failure_counts().values())
    store.fail_upsert = True
    target = await db.get(Document, first.document_id, populate_existing=True)
    ok, error = await ingest.index_chunks(db, target)
    assert ok is False and "Qdrant 写点失败" in error
    attempted = set(store.upserted_ids[written_before:])
    assert attempted, "失败前必须实际写出本轮点"
    assert store.delete_calls >= 1 and store.deleted_id_union == attempted
    assert attempted.isdisjoint(old_points + neighbor_points)
    assert await _manifest(db, first.document_id) == (old_build, old_points)
    target = await db.get(Document, first.document_id, populate_existing=True)
    assert target.index_status == "ready" and target.vectorized is True
    assert {hit.id for hit in store.get_by_ids(old_points + neighbor_points)} == set(old_points + neighbor_points)
    after = sum(derived_counters.failure_counts().values())
    # ① 形状断言（键名无关）：只要求「有且只有一个计数 +1」——键名将来再改也能过
    assert after == before + 1, (
        "删点失败被吞了但计数没动 ⇒ 该失败仍不可观测（回归修复 家族口径）；"
        f"调用前总和={before}，调用后总和={after}，明细={derived_counters.failure_counts()}"
    )
    # ② 键名锚（B 路 MINOR-3）：键名已定稿 ⇒ 这里硬编，钉「是哪一个键记的」
    #    与 ① 并存（不是替换）：① 管「有没有记」，② 管「记在哪个键上」。
    assert derived_counters.failure_counts().get("stale_vector_points", 0) >= 1, (
        "删点失败必须记在 derived_counters 的 stale_vector_points 键上"
        "（该键已定稿，见 app/core/derived_counters.py::COUNTER_KEYS）；"
        f"实际明细={derived_counters.failure_counts()}"
    )


@pytest.mark.asyncio
async def test_failed_ingest_never_deletes_active_points(
    db, tmp_path, monkeypatch, _sync_engine, _install_store
):
    """腿③：正文准备失败尚未写候选点，启用指针、正文、旧点均保持原样。"""
    store = _install_store(_sync_engine)
    src = tmp_path / "orphan.md"
    src.write_text(_CONTENT, encoding="utf-8")

    first = await ingest.ingest_document(db, src.name, src.read_bytes(), stored_path=str(src))
    assert store.delete_calls == 0, "前提：第一次入库（无旧块）不该有删点"
    old_build, old_points = await _manifest(db, first.document_id)
    old_ids = await _chunk_ids(db, first.document_id)
    snapshot = document_snapshot(await db.get(Document, first.document_id, populate_existing=True))

    # 照 test_ingest.py:106 的既有范式：让写 chunks 抛错 ⇒ _finish_ingest 走失败回滚路径
    async def _boom(session, doc, prepared):
        raise RuntimeError("模拟写 chunks 失败")

    monkeypatch.setattr(ingest, "_persist_chunks", _boom)
    with pytest.raises(RuntimeError):
        await ingest.retry_document(db, first.document_id)

    rows = await _rows(db)
    assert len(rows) == 1 and rows[0].index_status == "ready" and rows[0].vectorized is True
    assert document_snapshot(rows[0]) == snapshot
    assert await _manifest(db, first.document_id) == (old_build, old_points)
    assert store.delete_calls == 0, (
        f"失败轮不许删点（异常回滚路径）；实际 delete_calls={store.delete_calls}，"
        f"批 = {store.deleted_batches}"
    )
    # 旧块仍在（回滚保住数据）⇒ 点与行仍然一致
    assert await _chunk_ids(db, first.document_id) == old_ids
    assert {hit.id for hit in store.get_by_ids(old_points)} == set(old_points)


# --------------------------------------------------------------------------
# 腿④ 既有删点行为不回退：purge_document 仍删点（照 test_ingest.py:223 口径）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_still_deletes_points(db, tmp_path, _sync_engine, _install_store):
    """硬删路径的既有删点行为不得回退（本腿修前修后都绿，是**防回退**腿）。"""
    store = _install_store(_sync_engine)
    src = tmp_path / "purge.md"
    src.write_text(_CONTENT, encoding="utf-8")

    first = await ingest.ingest_document(db, src.name, src.read_bytes(), stored_path=str(src))
    _, old_points = await _manifest(db, first.document_id)
    await ingest.retry_document(db, first.document_id)
    _, new_points = await _manifest(db, first.document_id)
    expected = set(old_points + new_points)
    assert old_points and new_points and set(old_points).isdisjoint(new_points)

    await ingest.purge_document(db, first.document_id)

    assert store.delete_calls >= 1, "purge_document 的既有删点行为回退了"
    assert store.deleted_id_union == expected
    assert not store.get_by_ids(list(expected))
    rows = await _rows(db)
    assert rows == [], "purge 后 documents 行必须清掉"
    assert await _chunk_ids(db, first.document_id) == [], "purge 后 chunks 行必须清掉"
    assert not list((await db.execute(select(IndexBuild))).scalars().all())


# --------------------------------------------------------------------------
# 腿⑤ 纯函数腿（DB 无关）：_stale_point_ids 三态 + 去重保序
# --------------------------------------------------------------------------


def test_stale_point_ids_subtracts_reused_ids():
    """**SQLite 档（id 被复用）**：旧 ⊆ 新 ⇒ 返回空 ⇒ **不许删**（删了就是删自己的新点）。"""
    old = [6, 7, 8, 9, 10]
    new = {6, 7, 8, 9, 10}
    assert ingest._stale_point_ids(old, new) == [], (
        "id 被复用时（SQLite 桌面档）旧点已按同一 id 被新点覆盖 ⇒ 一个都不该删"
    )


def test_stale_point_ids_keeps_all_when_no_reuse():
    """**MySQL 档（id 不复用）**：旧 ∩ 新 = ∅ ⇒ 旧点全删（孤儿消除）。"""
    assert ingest._stale_point_ids([1, 2, 3], {7, 8}) == ["1", "2", "3"]


def test_stale_point_ids_mixed_and_dedup_keeps_order():
    """混合态：只删「旧 − 新」，且**去重保序**（同一 id 出现多次只删一次）。"""
    assert ingest._stale_point_ids([1, 2, 3], {3, 4}) == ["1", "2"]
    assert ingest._stale_point_ids([3, 1, 3, 2, 1], {2}) == ["3", "1"], "去重且保持首次出现顺序"
    assert ingest._stale_point_ids([], {1}) == []
    assert ingest._stale_point_ids([1, 2], set()) == ["1", "2"]
    # 返回类型必须是字符串（Qdrant 点 id 的口径），不是 int
    assert all(isinstance(i, str) for i in ingest._stale_point_ids([1], set()))

# --------------------------------------------------------------------------
# 腿⑥ 收敛腿（DB 无关 · 直调）：_orphan_point_ids —— 用「孤儿点的定义」兜底
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orphan_point_ids_drops_still_alive_ids(db, tmp_path, _install_store, _sync_engine):
    """候选里**还活着**的 id（``chunks`` 表里仍有行）⇒ 不许返回它。

    这一道是审计 MINOR-2 的处置：``_stale_point_ids`` 只做「旧 − 新」，它的前提
    「被减掉的 id 不会再属于别人」在 SQLite 档**只在「新块数 ≥ 旧块数」时成立**
    ⇒ 旧多新少时多出来的 id 会被 rowid 复用给**别的文档**，照旧删就是**跨文档误删**。
    """
    _install_store(_sync_engine)  # 只为装上假 embed/store（本腿不跑入库）
    src = tmp_path / "alive.md"
    src.write_text(_CONTENT, encoding="utf-8")
    first = await ingest.ingest_document(db, src.name, src.read_bytes(), stored_path=str(src))
    alive_ids = await _chunk_ids(db, first.document_id)
    assert alive_ids, "前提：必须有活着的 chunk 行"

    candidates = [str(alive_ids[0]), "999901", "999902"]
    out = await ingest._orphan_point_ids(db, candidates)

    assert str(alive_ids[0]) not in out, (
        f"该 id 在 chunks 表里仍有行（别的文档正在用）⇒ 绝不能删它的点；返回 {out}"
    )
    assert out == ["999901", "999902"], f"只该保留「确实已无对应行」的候选；实际 {out}"


@pytest.mark.asyncio
async def test_orphan_point_ids_returns_all_when_nothing_alive(db, _install_store, _sync_engine):
    """全部候选都已消失 ⇒ 全部返回（孤儿点照删，孤儿消除不被这道防线挡掉）。"""
    _install_store(_sync_engine)
    candidates = ["999901", "999902", "999903"]
    out = await ingest._orphan_point_ids(db, candidates)
    assert out == candidates, f"全部候选都无对应行 ⇒ 应全数返回；实际 {out}"


@pytest.mark.asyncio
async def test_orphan_point_ids_is_failsafe_on_query_error(db, monkeypatch):
    """``SELECT`` 打桩抛错 ⇒ 返回 ``[]``（**fail-safe：一个都不删**）。

    宁可留孤儿点（检索面降级、下次重建还会再判一次），也绝不误删别人正在用的点（真丢数据）。
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    orig_execute = AsyncSession.execute

    async def _boom(self, *args, **kwargs):
        raise RuntimeError("测试注入：孤儿点判定查询失败")

    monkeypatch.setattr(AsyncSession, "execute", _boom)
    try:
        out = await ingest._orphan_point_ids(db, ["1", "2"])
    finally:
        monkeypatch.setattr(AsyncSession, "execute", orig_execute)

    assert out == [], f"判定失败必须一个都不删（fail-safe）；实际 {out}"


# --------------------------------------------------------------------------
# 腿⑥b 非整数 id 直通腿（审计 MINOR-6）：_as_int_or_none 的判据必须与 int() 等价
# --------------------------------------------------------------------------


def test_as_int_or_none_matches_int_not_isdigit():
    """**审计 MINOR-6 的机器化**：``str.isdigit()`` 与 ``int()`` 的判据**不等价** ⇒
    ``_as_int_or_none`` 必须用 try/except 而不是「先判再转」。

    实测反例（本机 Python 3.13.14）：``'²'.isdigit() is True`` 而 ``int('²')`` 抛
    ``ValueError``；``'٣'``（阿拉伯-印度数字）两边都真、``'½'`` 两边都假。
    ⇒ 若照 ``isdigit()`` 判，``'²'`` 会被当成整数送进 ``int()`` 炸掉；
    而那条分支在 ``_finish_ingest` 里跑在 **mark_ready 提交之后** ⇒ 接口报 500 而文档其实已入库。
    """
    # ① 反例本身（把「两个谓词不等价」这条前提钉成判据，不是注释）
    assert "²".isdigit() is True
    with pytest.raises(ValueError):
        int("²")

    # ② _as_int_or_none 与 int() 的行为**逐项一致**（该转的转、该 None 的 None、绝不抛）
    for raw, expected in [
        ("123", 123),
        ("007", 7),
        ("-5", -5),
        (" 12 ", 12),
        ("abc", None),
        ("a1b2", None),
        ("", None),
        ("²", None),  # ← 反例：isdigit() 为真但必须 None（不许抛）
        ("½", None),
        ("Ⅲ", None),
        ("3.7", None),
        (None, None),
        (123, 123),
    ]:
        assert ingest._as_int_or_none(raw) == expected, (
            f"_as_int_or_none({raw!r}) 应为 {expected!r}，实际 {ingest._as_int_or_none(raw)!r}"
        )
    # ③ 绝不抛（含 None / 任意怪值）
    for raw in [None, object(), [], {}, b"12", "²", "٣", "٠"]:
        ingest._as_int_or_none(raw)  # 不抛即过


@pytest.mark.asyncio
async def test_orphan_point_ids_failsafe_on_non_integer_ids(db, _install_store, _sync_engine):
    """**非整数 id ⇒ fail-safe 不删**（与 SELECT 失败那支同一口径）。

    口径沿革（w-108 独立复核的登记项，本笔对齐）：早先写法是「非整数 id 原样保留进**删除批**」
    = fail-open **删除**，与本函数 docstring 声明的 fail-safe **方向相反** ——
    非整数 id 的存活证据本函数**判不出来**（存活判据 = 查 ``chunks`` 表的整数主键）
    ⇒ 按「判不出来就不删」处置。生产路径不可达（candidates 恒来自 ``select(Chunk.id)`` 的
    ``str(int)``）⇒ **非回归**，但语义必须自洽。

    同时它**不许炸**——非整数候选混进来时函数必须正常返回。

    这条钉的是 ``_orphan_point_ids` 的 ``passthrough`` 分支：非整数点 id 不参与
    「chunks 表里还有没有对应行」这条整数判据 ⇒ 按「未知」保守**保留**（留在删除批里）。
    同时它**不许炸**——非整数候选混进来时函数必须正常返回。
    """
    _install_store(_sync_engine)
    out = await ingest._orphan_point_ids(db, ["uuid-aaaa", "uuid-bbbb", "999901"])
    assert out == [], (
        f"候选里出现非整数 id ⇒ 判不出存活证据 ⇒ 本次一个都不删（fail-safe）；实际 {out}"
    )
    # 纯整数候选仍按孤儿判据走（999901 无对应行 ⇒ 真孤儿 ⇒ 保留）
    out2 = await ingest._orphan_point_ids(db, ["999901"])
    assert out2 == ["999901"], f"纯整数候选：无对应 chunk 行的仍是孤儿 ⇒ 应保留；实际 {out2}"


# --------------------------------------------------------------------------
# 腿⑦ 跨文档安全腿：失败只清本轮 UUID 点，旧版本与邻居保持完整
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_build_cleanup_only_deletes_own_uuid_points(
    db, tmp_path, _sync_engine, _install_store
):
    """失败点已写入后必须实际清理，目标旧版本和旁观文档的点、指针均不变。"""
    store = _install_store(_sync_engine)
    src = tmp_path / "orphan.md"
    src.write_text(_CONTENT, encoding="utf-8")

    first = await ingest.ingest_document(db, src.name, src.read_bytes(), stored_path=str(src))
    assert first.vectorized is True
    old_ids = await _chunk_ids(db, first.document_id)
    assert old_ids, "前提：第一次入库必须写出分块"
    old_build, old_points = await _manifest(db, first.document_id)
    neighbor = await _create_neighbor(db, tmp_path)
    neighbor_id = neighbor.id
    neighbor_build, neighbor_points = await _manifest(db, neighbor_id)
    written_before = len(store.upserted_ids)
    store.fail_upsert = True
    doc = await db.get(Document, first.document_id, populate_existing=True)
    ok, error = await ingest.index_chunks(db, doc)
    expected = set(store.upserted_ids[written_before:])
    assert ok is False and "Qdrant 写点失败" in error
    assert expected and store.delete_calls >= 1, "必须实际写入并清理失败候选点"
    assert store.deleted_id_union == expected
    assert not store.get_by_ids(list(expected)), "本轮写后失败的点必须全部清掉"
    assert expected.isdisjoint(old_points + neighbor_points)
    assert await _manifest(db, first.document_id) == (old_build, old_points)
    assert await _manifest(db, neighbor_id) == (neighbor_build, neighbor_points)
    assert {hit.id for hit in store.get_by_ids(old_points + neighbor_points)} == set(old_points + neighbor_points)
    assert await _chunk_ids(db, first.document_id) == old_ids
    failed = list((await db.execute(select(IndexBuild).where(
        IndexBuild.document_id == first.document_id, IndexBuild.state == "failed"
    ))).scalars().all())
    assert len(failed) == 1 and set(failed[0].point_ids) == expected


# --------------------------------------------------------------------------
# 腿⑧ 键表同源锚（A 路 MINOR-4）：audit 的 derived 回退键集 == 权威键集
# --------------------------------------------------------------------------


def test_audit_derived_fallback_matches_authoritative_keys():
    """``app/agent/audit.py`` 的 ``COUNTER_KEYS["derived"]`` 必须与
    ``derived_counters.COUNTER_KEYS`` **同集**（键少了/多了都红）。

    为什么需要这一条：``audit.py`` 里那份是**回退默认值**（``safe_counters`` 读不到模块时
    用它补 0）—— 两处一旦漂移，``GET /api/agent/audit`` 的 ``counters.derived`` 形状就与
    权威键表不一致（该键表已 **只增** 过三轮：memory 第五键 / derived 第八键 /
    2026-09-28 批次三 derived `trace_persist` + `tool_result_channel` = 第十键）。
    ⚠️ 本判据名被 ``audit.py:697`` 的注释**逐字引用** ⇒ 改名要同步改那边。
    """
    from app.agent import audit as agent_audit

    assert set(agent_audit.COUNTER_KEYS["derived"]) == set(derived_counters.COUNTER_KEYS), (
        "audit 的 derived 回退键集与 derived_counters 权威键表漂移："
        f"audit={sorted(agent_audit.COUNTER_KEYS['derived'])} "
        f"vs 权威={sorted(derived_counters.COUNTER_KEYS)}"
    )
