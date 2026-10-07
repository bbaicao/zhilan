"""S6 · 186 判据：向量侧与 SQL 合并同步（交付项 E）。

三件事：
1. §1.3 授权的「原地改 payload 指定键」能力在 VectorStore 协议上（set_payload），
   且 qdrant_client 的 import 只出现在 app/vector/ 之内（业务层不许散落 qdrant 调用）；
2. 参数校验（ids 与 filters 二选一、空 payload 拒绝）在触网**之前**抛错；
3. kb_merge_duplicates.py --repair-vector 的语义：受影响 chunk 的 payload.kb_id 由旧 id
   改成保留 id，且幂等（第二次调用 affected=0）；用假 store 记录调用，不连真 Qdrant。

为什么这件事是硬门：检索按 kb_id 下推过滤（app/retrieval/vector.py:132-140），
SQL 侧并了库而 payload 里还写旧 id ⇒ 那一整库永远检索不到（静默漏召回）。
"""

from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path

import pytest

from app.vector.base import VectorStore
from app.vector.qdrant_store import QdrantVectorStore

BACKEND_DIR = Path(__file__).resolve().parent.parent
SCRIPT_PATH = BACKEND_DIR / "scripts" / "kb_merge_duplicates.py"


def _load_merger():
    """按路径加载合并器（不依赖 scripts 是不是包；模块顶层无副作用）。"""
    spec = importlib.util.spec_from_file_location("s6_186_merger", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_journal(tmp_path, redirects):
    from app.core.kb_identity import kb_pending_path, kb_target_identity

    merger = _load_merger()
    target = kb_target_identity("sqlite:///" + (tmp_path / "fixture.sqlite").as_posix())
    pending = kb_pending_path(target, "repair-fixture")
    pending.parent.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "kb-merge-redirects-20261002T000000Z.json"
    merger._write_json_once(path, {
        "schema": "kb-merge-redirects/v2", "operation_id": "repair-fixture",
        "operation_dir": str(tmp_path), "target": target, "pending": str(pending),
        "required_collections": ["zhilan_chunks"], "redirects": redirects,
    })
    merger._write_json_once(pending, {
        "schema": "kb-maintenance/v1", "operation_id": "repair-fixture",
        "operation_dir": str(tmp_path), "target": target, "required_collections": ["zhilan_chunks"],
        "artifacts_sha256": {str(path.resolve()): merger._sha256(path)},
    })
    merger._write_json_once(tmp_path / "sql-applied.json", {
        "schema": "kb-sql-receipt/v1", "target": target, "operation_id": "repair-fixture",
        "outcome": "applied", "journal_sha256": merger._sha256(path),
    })
    return path


@pytest.fixture(autouse=True)
def isolated_maintenance(tmp_path, monkeypatch):
    monkeypatch.setenv("ZHILAN_KB_MERGE_DIR", str(tmp_path / "maintenance-gates"))


class _FakeStore:
    """会真按 filters 过滤的假 store（只实现合并器用到的三个方法）。"""

    def __init__(self, points: dict[str, dict]) -> None:
        self.points = points
        self.calls: list[dict] = []
        # 记账：list_ids 的调用序（用于断言「快照在任何 set_payload 之前取」）
        self.listed: list[dict] = []

    def count(self) -> int:
        return len(self.points)

    def get_by_ids(self, ids):
        return [types.SimpleNamespace(id=pid, payload=dict(self.points[pid]))
                for pid in ids if pid in self.points]

    def set_payload(self, payload, *, ids=None, filters=None) -> int:
        assert (ids is None) != (filters is None), "ids 与 filters 必须二选一"
        self.calls.append({"payload": dict(payload), "ids": ids, "filters": dict(filters or {})})
        hit = 0
        for pid, pl in self.points.items():
            if ids is not None:
                match = pid in ids
            else:
                match = all(pl.get(k) == v for k, v in (filters or {}).items())
            if match:
                pl.update(payload)
                hit += 1
        return hit

    def list_ids(self, *, filters: dict | None = None, limit: int | None = None) -> list[str]:
        """只读列点（186 的向量账本快照用）——与 query / set_payload 同一过滤口径。"""
        self.listed.append({"filters": dict(filters or {}), "at": len(self.calls)})
        out = [
            pid
            for pid, pl in self.points.items()
            if not filters or all(pl.get(k) == v for k, v in filters.items())
        ]
        return out if limit is None else out[:limit]

    def query(self, limit: int, filters: dict | None = None) -> list[str]:
        """只按 payload 过滤（本判据只关心过滤召回的口径，不关心向量相似度）。"""
        out = []
        for pid, pl in self.points.items():
            if filters and not all(pl.get(k) == v for k, v in filters.items()):
                continue
            out.append(pid)
        return out[:limit]


def test_set_payload_is_on_the_protocol_and_implemented():
    assert "set_payload" in VectorStore.__abstractmethods__
    impl = getattr(QdrantVectorStore, "set_payload")
    assert getattr(impl, "__isabstractmethod__", False) is False, "实类必须给出实现"
    assert QdrantVectorStore.set_payload is not VectorStore.set_payload


def test_qdrant_client_is_imported_only_inside_the_vector_package():
    offenders = []
    for py in (BACKEND_DIR / "app").rglob("*.py"):
        if "app/vector" in py.as_posix().replace("\\", "/"):
            continue
        for line in py.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("import qdrant_client") or stripped.startswith("from qdrant_client"):
                offenders.append(py.as_posix() + " :: " + stripped)
    assert offenders == [], "qdrant_client 只许出现在 app/vector/ 之内：" + repr(offenders)


def test_set_payload_rejects_empty_payload_and_ambiguous_selector():
    store = object.__new__(QdrantVectorStore)  # 只验参数校验，不建客户端（不触网/不建目录）
    with pytest.raises(ValueError):
        store.set_payload({}, ids=["x"])
    with pytest.raises(ValueError):
        store.set_payload({"kb_id": 1})
    with pytest.raises(ValueError):
        store.set_payload({"kb_id": 1}, ids=["x"], filters={"kb_id": 2})


def test_repair_vector_rewrites_kb_id_and_is_idempotent(tmp_path, monkeypatch):
    merger = _load_merger()
    fake = _FakeStore(
        {
            "d11-0": {"document_id": 11, "ordinal": 0, "kb_id": 4, "filename": "a.md", "category": "doc"},
            "d11-1": {"document_id": 11, "ordinal": 1, "kb_id": 4, "filename": "a.md", "category": "doc"},
            "d12-0": {"document_id": 12, "ordinal": 0, "kb_id": 4, "filename": "b.md", "category": "doc"},
            "d21-0": {"document_id": 21, "ordinal": 0, "kb_id": 1, "filename": "k.md", "category": "doc"},
        }
    )
    monkeypatch.setattr("app.vector.get_vector_store", lambda **kw: fake)

    journal = _write_journal(tmp_path, [{"from": 4, "to": 1}, {"from": 9, "to": 1}])

    before_old = set(fake.query(50, {"kb_id": 4}))
    before_kept = set(fake.query(50, {"kb_id": 1}))
    assert len(before_old) == 3 and len(before_kept) == 1

    report = merger.run_repair_vector(
        journal,
        url="http://127.0.0.1:16386",
        path=None,
        collection="zhilan_chunks",
        out_dir=tmp_path,
    )

    assert report["points_total"] == 4
    assert report["all_synced"] is True
    assert report["redirects"][0] == {"from": 4, "to": 1, "affected": 3, "pending_after": 0}
    assert report["redirects"][1] == {"from": 9, "to": 1, "affected": 0, "pending_after": 0}

    assert fake.query(50, {"kb_id": 4}) == [], "旧 id 的过滤召回必须为空（否则就是漏召回）"
    assert set(fake.query(50, {"kb_id": 1})) == before_kept | before_old, "并进来的点一个不丢"
    assert len(fake.points) == 4, "原地改 payload：不新增、不删除任何点"
    assert [c["payload"] for c in fake.calls] == [{"kb_id": 1}], "每次只改 kb_id 这一个键"
    assert fake.calls[0]["ids"] == ["d11-0", "d11-1", "d12-0"], "只改原快照点，不能扩写未入账点"

    # 🔴 顺序硬门（S6 · 186 向量可还原）：先列点落快照，再改 payload。
    # 先改后记 = 事后说不清动过哪些点 ⇒ 不可还原；这条断言就是那道门的判别力。
    assert [item["filters"] for item in fake.listed[:2]] == [{"kb_id": 4}, {"kb_id": 9}]
    assert all(item["at"] == 0 for item in fake.listed[:2]), "快照必须在任何 set_payload 之前取"
    snapshot = Path(report["snapshot"])
    assert snapshot.parent == tmp_path and snapshot.name.startswith("vector-points-")
    doc = json.loads(snapshot.read_text(encoding="utf-8"))
    assert doc["schema"] == "kb-vector-points/v1"
    assert doc["collection"] == "zhilan_chunks"
    assert doc["points_total"] == 3
    assert doc["entries"][0] == {
        "from": 4,
        "to": 1,
        "point_ids": ["d11-0", "d11-1", "d12-0"],
        "points_total": 3,
    }
    assert doc["entries"][1] == {"from": 9, "to": 1, "point_ids": [], "points_total": 0}
    assert report["records"] == doc["entries"], "返回的 records 必须与落盘快照逐字一致"


def test_repair_vector_refuses_without_explicit_target(tmp_path):
    merger = _load_merger()
    journal = tmp_path / "kb-merge-redirects-20261002T000000Z.json"
    journal.write_text(json.dumps({"redirects": [{"from": 4, "to": 1}]}), encoding="utf-8")

    with pytest.raises(SystemExit) as ei:
        merger.run_repair_vector(journal, url=None, path=None, collection="zhilan_chunks")
    text = str(ei.value)
    assert "--qdrant-url" in text and "--qdrant-path" in text
    assert "安装态" in text, "拒绝理由必须写清为什么不许落到应用单例"


def test_repair_vector_refuses_on_empty_ledger(tmp_path):
    merger = _load_merger()
    journal = _write_journal(tmp_path, [])

    with pytest.raises(SystemExit) as ei:
        merger.run_repair_vector(journal, url="http://127.0.0.1:16386", path=None, collection="c")
    assert "redirects" in str(ei.value)
