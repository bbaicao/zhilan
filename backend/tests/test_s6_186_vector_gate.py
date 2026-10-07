"""S6 · 186 向量硬门判据（交付项 E 的第二层）：快照 / 同步校验 / 回滚 + 先落材料。

为什么必须有这组证据：检索按 payload 的 kb_id 下推过滤（app/retrieval/vector.py），
SQL 侧并了库而向量 payload 还写旧 id ⇒ 那一整库**静默漏召回**。所以：

1. VectorStore 协议上的只读 list_ids（分页、过滤口径与 query / set_payload 一致）；
2. kb_merge_duplicates.py 的 --repair-vector / --verify-synced / --restore-vector 闭环：
   快照先落盘 → 改完能校验（旧 id 过滤召回为 0、快照点都在且指向保留 id）→ 能按快照
   把 kb_id 改回去且**其它 payload 键一字不动**；
3. 「先落材料、再动数据」：apply 还没动库就失败时材料已在盘上，且 before-image 的
   target 不是 None（否则 --restore 拦不住「恢复到另一个库」）。

假 store 覆盖协议反例与调用序；真实临时本地 Qdrant 副本覆盖 CLI 双集合闭环，零真实库。
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import types
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.vector.base import VectorStore
from app.vector.qdrant_store import QdrantVectorStore

BACKEND_DIR = Path(__file__).resolve().parent.parent
VERSIONS_DIR = BACKEND_DIR / "alembic" / "versions"


@pytest.fixture(autouse=True)
def isolated_maintenance(tmp_path, monkeypatch):
    monkeypatch.setenv("ZHILAN_KB_MERGE_DIR", str(tmp_path / "maintenance-gates"))


def _load(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_merger():
    return _load("s6_186_vector_gate_merger", BACKEND_DIR / "scripts" / "kb_merge_duplicates.py")


def _load_cli_harness():
    """复用既有 CLI 判据的 0006 造库 / 插重复行夹具（同一条真 alembic 路径）。"""
    return _load("s6_186_cli_harness", BACKEND_DIR / "tests" / "test_s6_186_merge_cli.py")


class _Hit:
    __slots__ = ("id", "payload")

    def __init__(self, pid: str, payload: dict) -> None:
        self.id = pid
        self.payload = dict(payload)


class _RichStore:
    """带 list_ids / get_by_ids 的假 store：能真过滤、能真回查 payload。"""

    def __init__(self, points: dict[str, dict]) -> None:
        self.points = {k: dict(v) for k, v in points.items()}
        self.ops: list[tuple] = []

    def count(self) -> int:
        return len(self.points)

    def list_ids(self, *, filters: dict | None = None, limit: int | None = None) -> list[str]:
        self.ops.append(("list_ids", dict(filters or {})))
        out = [
            pid
            for pid, pl in self.points.items()
            if not filters or all(pl.get(k) == v for k, v in filters.items())
        ]
        return out if limit is None else out[:limit]

    def set_payload(self, payload, *, ids=None, filters=None) -> int:
        assert (ids is None) != (filters is None), "ids 与 filters 必须二选一"
        self.ops.append(("set_payload", dict(payload), None if ids is None else list(ids),
                         dict(filters or {})))
        hit = 0
        for pid, pl in self.points.items():
            match = pid in ids if ids is not None else all(
                pl.get(k) == v for k, v in (filters or {}).items()
            )
            if match:
                pl.update(payload)
                hit += 1
        return hit

    def get_by_ids(self, ids: list[str]) -> list[_Hit]:
        return [_Hit(pid, self.points[pid]) for pid in ids if pid in self.points]


POINTS = {
    "d11-0": {"document_id": 11, "ordinal": 0, "kb_id": 4, "filename": "a.md", "category": "doc"},
    "d11-1": {"document_id": 11, "ordinal": 1, "kb_id": 4, "filename": "a.md", "category": "doc"},
    "d12-0": {"document_id": 12, "ordinal": 0, "kb_id": 4, "filename": "b.md", "category": "doc"},
    "d21-0": {"document_id": 21, "ordinal": 0, "kb_id": 1, "filename": "k.md", "category": "doc"},
}
REDIRECTS = [{"from": 4, "to": 1}, {"from": 9, "to": 1}]


def _store(monkeypatch) -> _RichStore:
    fake = _RichStore(POINTS)
    monkeypatch.setattr("app.vector.get_vector_store", lambda **kw: fake)
    return fake


def _journal(tmp_path: Path) -> Path:
    from app.core.kb_identity import kb_pending_path, kb_target_identity

    path = tmp_path / "kb-merge-redirects-20261002T000000Z.json"
    target = kb_target_identity("sqlite:///" + (tmp_path / "fixture.sqlite").as_posix())
    pending = kb_pending_path(target, "vector-fixture")
    ledger = {"schema": "kb-merge-redirects/v2", "operation_id": "vector-fixture",
              "operation_dir": str(tmp_path), "target": target, "pending": str(pending),
              "required_collections": ["zhilan_chunks"], "redirects": REDIRECTS}
    path.write_text(json.dumps(ledger), encoding="utf-8")
    if not pending.exists():
        pending.parent.mkdir(parents=True, exist_ok=True)
        _load_merger()._write_json_once(pending, {
            "schema": "kb-maintenance/v1", "operation_id": "vector-fixture",
            "operation_dir": str(tmp_path), "target": target,
            "required_collections": ["zhilan_chunks"],
            "artifacts_sha256": {str(path.resolve()): _load_merger()._sha256(path)},
        })
        _load_merger()._write_json_once(tmp_path / "sql-applied.json", {
            "schema": "kb-sql-receipt/v1", "target": target, "operation_id": "vector-fixture",
            "outcome": "applied", "journal_sha256": _load_merger()._sha256(path),
        })
    return path


def _repair(merger, tmp_path: Path) -> dict:
    return merger.run_repair_vector(
        _journal(tmp_path),
        url="http://127.0.0.1:16386",
        path=None,
        collection="zhilan_chunks",
        out_dir=tmp_path,
    )


def test_list_ids_is_on_the_protocol_and_implemented():
    assert "list_ids" in VectorStore.__abstractmethods__, "list_ids 必须在协议上（186 的账本/校验要用）"
    assert getattr(VectorStore.list_ids, "__isabstractmethod__", False) is True
    assert QdrantVectorStore.list_ids is not VectorStore.list_ids, "实类必须给出实现"
    assert getattr(QdrantVectorStore.list_ids, "__isabstractmethod__", False) is False
    params = inspect.signature(QdrantVectorStore.list_ids).parameters
    assert params["filters"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["limit"].kind is inspect.Parameter.KEYWORD_ONLY


class _FakeScrollClient:
    """只实现 scroll 的假客户端；兼容旧版 tuple 与新版 ScrollResult 两种返回形态。"""

    def __init__(self, pages: list[list[str]], *, new_style: bool = False) -> None:
        self.pages = pages
        self.new_style = new_style
        self.calls: list[dict] = []

    def scroll(self, **kw):
        self.calls.append(kw)
        offset = kw.get("offset") or 0
        page = list(self.pages[offset])
        cap = kw.get("limit")  # 真 Qdrant 会按 limit 截断这一页；假客户端必须同语义
        if cap is not None:
            page = page[:cap]
        records = [types.SimpleNamespace(id=pid) for pid in page]
        nxt = offset + 1 if offset + 1 < len(self.pages) else None
        if self.new_style:
            return types.SimpleNamespace(points=records, next_page_offset=nxt)
        return (records, nxt)


def _store_with_client(client: _FakeScrollClient, monkeypatch) -> QdrantVectorStore:
    monkeypatch.setattr("app.vector.qdrant_store.QdrantClient", lambda **kwargs: client)
    return QdrantVectorStore(path=":memory:")  # 真构造保留所有权字段，客户端替身不触网、不建目录


def test_qdrant_list_ids_pages_and_honours_limit_and_filters(monkeypatch):
    for new_style in (False, True):
        client = _FakeScrollClient([["a", "b"], ["c"]], new_style=new_style)
        store = _store_with_client(client, monkeypatch)
        assert store.list_ids() == ["a", "b", "c"], "分页必须跨页累积（新版/旧版返回都要认）"
        assert [c["offset"] for c in client.calls] == [None, 1], "第二页必须带 offset"
        assert all(c["with_payload"] is False and c["with_vectors"] is False for c in client.calls), (
            "只取 id：不许把 payload / 向量拉回本地"
        )
        assert all(c["limit"] == 256 for c in client.calls), "内部页大小"
        assert client.calls[0]["scroll_filter"] is None, "无过滤 = 不过滤（不是空过滤）"

    client = _FakeScrollClient([["a", "b"], ["c"]])
    store = _store_with_client(client, monkeypatch)
    assert store.list_ids(limit=1) == ["a"], "limit 截断在全部命中之上（不是「每页各 1 条」）"
    assert client.calls[0]["limit"] == 1, "剩余额度必须下推给 scroll（不是自己截断列表）"
    assert len(client.calls) == 1, "limit 已满足 ⇒ 不再翻页"

    client = _FakeScrollClient([["a", "b"]])
    store = _store_with_client(client, monkeypatch)
    assert store.list_ids(filters={"kb_id": 4}) == ["a", "b"]
    selector = client.calls[0]["scroll_filter"]
    assert selector is not None and selector.must[0].key == "kb_id"
    assert selector.must[0].match.value == 4


def test_repair_vector_snapshot_then_verify_synced_is_green(tmp_path, monkeypatch):
    merger = _load_merger()
    fake = _store(monkeypatch)
    report = _repair(merger, tmp_path)

    assert report["all_synced"] is True
    assert [op[0] for op in fake.ops[:3]] == ["list_ids", "list_ids", "set_payload"], (
        "顺序硬门：两次列点（落快照）必须都在任何 set_payload 之前"
    )

    checked = merger.run_verify_synced(
        Path(report["snapshot"]), url="http://127.0.0.1:16386", path=None, collection="zhilan_chunks"
    )
    assert checked["ok"] is True, checked["problems"]
    assert checked["problems"] == []
    assert checked["entries"][0] == {
        "from": 4, "to": 1, "expected_points": 3, "leftover_old": 0,
        "checked": 3, "missing": 0, "wrong": 0,
    }
    assert checked["entries"][1]["expected_points"] == 0


def test_verify_synced_turns_red_on_leftover_missing_and_wrong(tmp_path, monkeypatch):
    merger = _load_merger()
    fake = _store(monkeypatch)
    report = _repair(merger, tmp_path)
    snapshot = Path(report["snapshot"])

    # ① 漏改一个点（模拟改 payload 中途失败）⇒ 旧 id 过滤召回非空 + 该点 kb_id 未指向保留行
    fake.points["d11-0"]["kb_id"] = 4
    bad = merger.run_verify_synced(snapshot, url="http://127.0.0.1:16386", path=None,
                                   collection="zhilan_chunks")
    assert bad["ok"] is False
    assert any("漏召回面" in p for p in bad["problems"]), bad["problems"]
    assert any("未指到保留行" in p for p in bad["problems"]), bad["problems"]
    assert bad["entries"][0]["leftover_old"] == 1 and bad["entries"][0]["wrong"] == 1

    # ② 快照里的点没了（点被删）⇒ 丢失面必须点名
    fake.points["d11-0"]["kb_id"] = 1
    del fake.points["d12-0"]
    lost = merger.run_verify_synced(snapshot, url="http://127.0.0.1:16386", path=None,
                                    collection="zhilan_chunks")
    assert lost["ok"] is False
    assert any("快照点丢失" in p for p in lost["problems"]), lost["problems"]
    assert lost["entries"][0]["missing"] == 1


def test_restore_vector_reverts_only_kb_id_and_keeps_other_keys(tmp_path, monkeypatch):
    merger = _load_merger()
    fake = _store(monkeypatch)
    before = {pid: dict(pl) for pid, pl in fake.points.items()}
    report = _repair(merger, tmp_path)
    assert fake.points["d11-0"]["kb_id"] == 1

    back = merger.run_restore_vector(
        Path(report["snapshot"]), url="http://127.0.0.1:16386", path=None, collection="zhilan_chunks"
    )
    assert back["ok"] is True, back["problems"]
    assert back["restored_points"] == 3
    assert fake.points == before, "回滚必须逐键还原：只动 kb_id，其它 payload 键一字不改"
    assert fake.list_ids(filters={"kb_id": 4}) == ["d11-0", "d11-1", "d12-0"], (
        "回滚后旧 kb_id 的过滤召回必须恢复原样"
    )


def test_latest_ledger_requires_unique_candidate_and_keeps_old_prefix_compatible(tmp_path):
    merger = _load_merger()
    (tmp_path / "redirects-20260101T000000Z.json").write_text("{}", encoding="utf-8")
    preferred = tmp_path / "kb-merge-redirects-20261002T000000Z.json"
    preferred.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="journal"):
        merger._latest_ledger(tmp_path)
    (tmp_path / "redirects-20260101T000000Z.json").unlink()
    assert merger._latest_ledger(tmp_path) == preferred

    only_old = tmp_path / "old"
    only_old.mkdir()
    legacy = only_old / "redirects-20260101T000000Z.json"
    legacy.write_text("{}", encoding="utf-8")
    assert merger._latest_ledger(only_old) == legacy, "老账本名必须回退兼容"

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SystemExit) as ei:
        merger._latest_ledger(empty)
    assert "journal" in str(ei.value)


def test_migration_hint_and_script_default_dir_share_one_source(monkeypatch):
    monkeypatch.delenv("ZHILAN_KB_MERGE_DIR", raising=False)
    merger = _load_merger()
    migration = _load("s6_186_gate_migration_0007", VERSIONS_DIR / "0007_knowledge_base_identity.py")

    assert merger.VECTOR_SNAPSHOT_SCHEMA == "kb-vector-points/v1"
    assert merger.DEFAULT_OUT_SUBDIR == ("data", "kb-merge")
    assert Path(migration._journal_dir()) == Path(merger.default_out_dir()), (
        "迁移的重定向日志目录与脚本的材料目录必须同源（否则 --repair-vector 找不到账本）"
    )

    text = (VERSIONS_DIR / "0007_knowledge_base_identity.py").read_text(encoding="utf-8")
    assert all(token in text for token in (
        "--preflight", "--apply", "--repair-vector", "--verify-synced",
        "kb_maintenance_required",
    )), (
        "重复存量迁移必须先拒绝并指向完整维护流程"
    )
    assert "--verify-synced" in text, "迁移输出必须写清重新开放条件（向量同步校验）"
    doc = merger.__doc__ or ""
    assert "--verify-synced" in doc and "重新开放" in doc, "运维 runbook 必须落在模块 docstring 里"


def test_apply_writes_materials_before_touching_the_database(tmp_path, monkeypatch, capsys):
    merger = _load_merger()
    cli = _load_cli_harness()
    db = tmp_path / "apply-order.sqlite"
    cli._alembic_0006(db)
    cli._seed_compatible_duplicates(db)
    before_ids = cli._kb_ids(db)
    before_refs = cli._ref_counts(db)
    out = tmp_path / "materials"
    dsn = cli._dsn(db)

    engine = sa.create_engine(dsn, future=True)
    merger.plan_merge(engine)
    assert cli._kb_ids(db) == before_ids, "plan_merge 必须零写入"
    assert not out.exists(), "plan_merge 不许写材料"

    def _boom(engine, bundle):  # noqa: ANN001
        raise RuntimeError("S6-186 判据：模拟合并中途失败")

    monkeypatch.setattr(merger, "apply_merge", _boom)
    capsys.readouterr()
    rc = merger.main(["--dsn", dsn, "--apply", "--out", str(out), "--json"])

    assert rc == merger.EXIT_PRECONDITION, "不兼容/失败必须是前置条件退出码，不是 0"
    assert cli._kb_ids(db) == before_ids and cli._ref_counts(db) == before_refs, (
        "失败必须零写入（库一个字节都不能变）"
    )
    images = sorted(out.rglob("before-image-*.json"))
    ledgers = sorted(out.rglob("redirects-*.json"))
    assert len(images) == 1 and len(ledgers) == 1, "材料必须先于数据变更落盘"
    assert len(sorted(out.rglob("restore-*.sql"))) == 1
    image = json.loads(images[0].read_text(encoding="utf-8"))
    assert image["target"]["dialect"] == "sqlite", (
        "写材料之前必须带上目标标识（否则 --restore 拦不住「恢复到另一个库」）"
    )


def test_real_apply_merges_and_reports_three_materials(tmp_path, capsys):
    merger = _load_merger()
    cli = _load_cli_harness()
    db = tmp_path / "apply-real.sqlite"
    cli._alembic_0006(db)
    cli._seed_compatible_duplicates(db)
    out = tmp_path / "real-materials"

    capsys.readouterr()
    rc = merger.main(["--dsn", cli._dsn(db), "--apply", "--out", str(out), "--json"])
    assert rc == merger.EXIT_OK
    report = cli._json_of(capsys.readouterr().out)
    assert report["dialect"] == "sqlite" and Path(report["database"]) == db.resolve()
    assert len(report["materials"]) == 3 and all(Path(p).exists() for p in report["materials"])
    assert cli._kb_ids(db) == [1, 4, 11], "保留 MIN(id)：4 与 11 存活、本机种子行 1 不受影响"
    assert cli._ref_counts(db) == {"notes": {4: 3, 11: 1}, "documents": {4: 2, 11: 1},
                                  "wiki_links": {4: 1, 11: 1}}


def test_explicit_url_suppresses_configured_local_path(tmp_path, monkeypatch):
    import app.vector as vector

    merger = _load_merger()
    fake = _RichStore(POINTS)
    captured = {}

    def constructor(**kw):
        captured.update(kw)
        return fake

    monkeypatch.setattr(vector, "settings", types.SimpleNamespace(
        qdrant_path=str(tmp_path / "installed"),
        QDRANT_URL="http://127.0.0.1:6333", QDRANT_COLLECTION="zhilan_chunks",
    ))
    monkeypatch.setattr(vector, "QdrantVectorStore", constructor)
    _repair(merger, tmp_path)
    assert not captured["path"], "显式副本 URL 不许继承安装态本地路径"
    assert captured["url"] == "http://127.0.0.1:16386"


def test_same_second_retry_preserves_original_vector_image(tmp_path, monkeypatch):
    merger = _load_merger()
    fake = _store(monkeypatch)
    monkeypatch.setattr(merger, "_now_stamp", lambda: "20261002T000000Z")
    first = _repair(merger, tmp_path)
    original = Path(first["snapshot"]).read_bytes()
    second = _repair(merger, tmp_path)
    assert Path(first["snapshot"]).read_bytes() == original
    restored = merger.run_restore_vector(
        Path(second["snapshot"]), url="http://127.0.0.1:16386", path=None,
        collection="zhilan_chunks",
    )
    assert restored["ok"] and fake.points == POINTS


@pytest.mark.parametrize("mode", ["verify", "restore"])
def test_vector_snapshot_refuses_wrong_collection_before_opening_store(tmp_path, monkeypatch, mode):
    merger = _load_merger()
    _store(monkeypatch)
    report = _repair(merger, tmp_path)

    def forbidden(**kw):
        pytest.fail("快照目标不符时不许打开向量库")

    monkeypatch.setattr("app.vector.get_vector_store", forbidden)
    run = merger.run_verify_synced if mode == "verify" else merger.run_restore_vector
    with pytest.raises(SystemExit, match="collection|集合"):
        run(Path(report["snapshot"]), url="http://127.0.0.1:16386", path=None,
            collection="unrelated")


def test_partial_vector_retry_retains_first_operation_point_ids(tmp_path, monkeypatch):
    merger = _load_merger()

    class FailOnce(_RichStore):
        failed = False

        def set_payload(self, payload, *, ids=None, filters=None):
            if not self.failed:
                self.failed = True
                self.points["d11-0"].update(payload)
                raise RuntimeError("模拟写完一个点后失败")
            return super().set_payload(payload, ids=ids, filters=filters)

    fake = FailOnce(POINTS)
    monkeypatch.setattr("app.vector.get_vector_store", lambda **kw: fake)
    with pytest.raises(RuntimeError, match="一个点"):
        _repair(merger, tmp_path)
    retry = _repair(merger, tmp_path)
    restored = merger.run_restore_vector(
        Path(retry["snapshot"]), url="http://127.0.0.1:16386", path=None,
        collection="zhilan_chunks",
    )
    assert restored["ok"] and fake.points == POINTS


def test_default_ledger_refuses_multiple_operations_instead_of_guessing(tmp_path):
    merger = _load_merger()
    (tmp_path / "kb-merge-redirects-20261001T000000Z.json").write_text("{}", encoding="utf-8")
    (tmp_path / "redirects-20261002T000000Z.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="journal|账本"):
        merger._latest_ledger(tmp_path)


def test_real_local_cli_all_collections_and_restore_reblock_startup(tmp_path, capsys):
    """Actual Qdrant local copy: CLI / immutable receipts / restore reopening gate."""
    from app.core.kb_identity import (
        KnowledgeBaseMaintenanceRequired, ensure_kb_maintenance_complete,
    )
    from app.vector.base import VectorPoint

    merger, cli = _load_merger(), _load_cli_harness()
    db = tmp_path / "all-collections.sqlite"
    cli._alembic_0006(db)
    cli._seed_compatible_duplicates(db)
    out = tmp_path / "materials"
    path = str(tmp_path / "qdrant-copy")
    capsys.readouterr()
    assert merger.main(["--dsn", cli._dsn(db), "--apply", "--out", str(out), "--json"]) == 0
    report = cli._json_of(capsys.readouterr().out)
    ledger = next(Path(p) for p in report["materials"] if Path(p).name.startswith("redirects-"))
    doc = json.loads(ledger.read_text(encoding="utf-8"))
    assert set(doc["required_collections"]) == {"c1", "zhilan_chunks"}
    with pytest.raises(KnowledgeBaseMaintenanceRequired):
        ensure_kb_maintenance_complete(cli._dsn(db))

    snapshots = {}
    for index, collection in enumerate(doc["required_collections"]):
        store = QdrantVectorStore(path=path, collection=collection)
        store.ensure_collection(2)
        store.upsert([VectorPoint(id="11", vector=[1.0, 0.0],
                                  payload={"kb_id": 7, "document_id": 111, "untouched": collection})])
        store.close()
        capsys.readouterr()
        assert merger.main(["--repair-vector", "--journal", str(ledger), "--qdrant-path", path,
                            "--collection", collection, "--json"]) == 0
        repaired = cli._json_of(capsys.readouterr().out)
        snapshots[collection] = Path(repaired["snapshot"])
        assert merger.main(["--verify-synced", "--journal", str(snapshots[collection]),
                            "--qdrant-path", path, "--collection", collection, "--json"]) == 0
        capsys.readouterr()
        if index == 0:
            with pytest.raises(KnowledgeBaseMaintenanceRequired):
                ensure_kb_maintenance_complete(cli._dsn(db))
    ensure_kb_maintenance_complete(cli._dsn(db))

    collection = doc["required_collections"][0]
    assert merger.main(["--restore-vector", "--journal", str(snapshots[collection]),
                        "--qdrant-path", path, "--collection", collection, "--json"]) == 0
    capsys.readouterr()
    with pytest.raises(KnowledgeBaseMaintenanceRequired):
        ensure_kb_maintenance_complete(cli._dsn(db))
    image = next(Path(p) for p in report["materials"] if Path(p).name.startswith("before-image-"))
    assert merger.main(["--restore", str(image), "--dsn", cli._dsn(db), "--json"]) == 0
    capsys.readouterr()
    with pytest.raises(KnowledgeBaseMaintenanceRequired):
        ensure_kb_maintenance_complete(cli._dsn(db))
    for collection in doc["required_collections"][1:]:
        assert merger.main(["--restore-vector", "--journal", str(snapshots[collection]),
                            "--qdrant-path", path, "--collection", collection, "--json"]) == 0
        capsys.readouterr()
    ensure_kb_maintenance_complete(cli._dsn(db))
    assert cli._kb_ids(db) == [1, 4, 7, 9, 11, 12]
    for collection in doc["required_collections"]:
        store = QdrantVectorStore(path=path, collection=collection)
        assert store.get_by_ids(["11"])[0].payload == {
            "kb_id": 7, "document_id": 111, "untouched": collection,
        }
        store.close()


def test_snapshot_refuses_changed_target_or_schema_before_writes(tmp_path, monkeypatch):
    merger = _load_merger()
    fake = _store(monkeypatch)
    repaired = _repair(merger, tmp_path)
    writes = [op for op in fake.ops if op[0] == "set_payload"]
    with pytest.raises(SystemExit, match="目标"):
        merger.run_restore_vector(Path(repaired["snapshot"]), url="http://127.0.0.1:9999",
                                  path=None, collection="zhilan_chunks")
    snapshot = Path(repaired["snapshot"])
    document = json.loads(snapshot.read_text(encoding="utf-8"))
    document["schema"] = "untrusted/v99"
    snapshot.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SystemExit, match="schema"):
        merger.run_restore_vector(snapshot, url="http://127.0.0.1:16386",
                                  path=None, collection="zhilan_chunks")
    assert [op for op in fake.ops if op[0] == "set_payload"] == writes
