"""S6 · 186 知识库唯一身份：canonical bytes / 迁移 0007 / 唯一约束 判据（2026-09-30）。

## 这个文件钉什么

1. **canonical bytes 的单一真源**：`app.core.kb_identity.kb_path_key` 与迁移 0007 的内联
   实现 `_key` 对同一批样本必须**逐字产出相同的 key**（迁移不能 import app 包，所以必然
   有两份实现；这份判据就是钉住它们不出分歧的那颗钉子）。
2. **空新库 → head**：0007 之后 `path_or_url_key` 列与唯一索引 `uk_knowledge_bases_path_or_url_key`
   **真实存在**（不是「健康检查 200」那种间接证据 —— `main.py` 的 lifespan 会吞迁移异常）。
3. **造出 0006 形态的重复存量 → 自动升级拒绝 → 具名维护 → 升 0007**：
   重复已合并、三张表的引用已迁、`MIN(id)` 保留、
   软删行（`deleted_at` 非空）也被迁、约束真的生效。
4. **`Base.metadata.create_all` 建出的库真的拒重复**（这是测试路径与迁移路径的另一个分叉点）。
5. **不兼容组 fail-closed**：组内 `kind` / `collection` 不一致 ⇒ 整个迁移中止、**零写入**
   （版本仍停在 0006、列都没加）。

## 安全前提（同 `test_migration_chain.py` 的口径）

所有迁移腿都把 `DB_BACKEND` 钉成 `sqlite` + `SQLITE_PATH` 钉到 `tmp_path`，并在夹具里
**断言**这两件事（`alembic/env.py` 的 URL 取自 `settings.database_dsn`，不钉就是拿真开发库
当靶子）。本文件不连 MySQL —— MySQL 方言腿在隔离测试库上单跑，见证据目录。
"""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.kb_identity import KB_PATH_KEY_LENGTH, kb_canonical_path, kb_path_key
from app.models import Base, KnowledgeBase
from tests.test_migration_chain import EXPECTED_HEAD

BACKEND_DIR = Path(__file__).resolve().parent.parent
MIGRATION_0007 = BACKEND_DIR / "alembic" / "versions" / "0007_knowledge_base_identity.py"
UNIQUE_INDEX = "uk_knowledge_bases_path_or_url_key"
#: 路径用「长共同前缀 + 末尾不同」的样本：前缀唯一索引会让这一对误撞号，全长摘要不会
LONG_PREFIX_HEAD = "知识库/共享目录/" * 60


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch) -> Path:
    """把「当前方言」钉到 tmp_path 上的临时 SQLite 文件，返回路径。

    ⚠️ 先钉 `DB_BACKEND` / `SQLITE_PATH` 再谈迁移：`alembic/env.py` 用
    `settings.database_dsn` 覆盖 ini 里的 URL，不钉就是**真开发库**。
    """
    from app.core.config import settings

    db_path = (tmp_path / "s6_186_kb.db").resolve()
    monkeypatch.setattr(settings, "DB_BACKEND", "sqlite")
    monkeypatch.setattr(settings, "SQLITE_PATH", str(db_path))
    monkeypatch.setenv("ZHILAN_KB_MERGE_DIR", str(tmp_path / "maintenance"))
    assert settings.is_sqlite, "夹具没把方言钉成 sqlite —— 拒绝在真库上跑迁移"
    assert settings.sqlite_file == db_path, "夹具没把库路径钉到 tmp_path —— 拒绝在真库上跑迁移"
    return db_path


def _cfg() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


def _upgrade(revision: str) -> None:
    command.upgrade(_cfg(), revision)


def _engine(db_path: Path):
    return create_engine("sqlite:///" + db_path.as_posix(), future=True)


def _rows(engine, sql: str) -> list[dict]:
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(text(sql)).mappings().all()]


def _scalar(engine, sql: str):
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar()


def _columns(db_path: Path, table: str) -> dict[str, str]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute("PRAGMA table_info(" + table + ")").fetchall()
    finally:
        connection.close()
    return {row[1]: (row[2] or "").upper() for row in rows}


def _indexes(db_path: Path, table: str) -> dict[str, dict]:
    connection = sqlite3.connect(db_path)
    try:
        out: dict[str, dict] = {}
        for row in connection.execute("PRAGMA index_list(" + table + ")").fetchall():
            name = row[1]
            cols = [r[2] for r in connection.execute("PRAGMA index_info(" + name + ")")]
            out[name] = {"unique": bool(row[2]), "columns": cols}
    finally:
        connection.close()
    return out


def _seed_duplicate_0006(db_path: Path) -> None:
    """在 0006 形态的库上造两组重复存量 + 三张表的引用（含软删行与空 path 行）。

    组 A（可合并）：id 4 / 7 / 9，`path_or_url` 分别是 `D:/kb-186`、`D:/kb-186`、
    「  D:/kb-186  」（strip 后同一身份）；kind 与 collection 全相等。
    组 B（可合并）：id 11 / 12，`path_or_url` = `http://kb-186/dup`。
    id 1 是 **0002 迁移建表时播种的本机库**（空 path，不进唯一域）：这里不插它，只断言
    它已存在——重复插会撞主键（首跑 4 条红的根因就是这一行）。
    """
    engine = _engine(db_path)
    with engine.connect() as conn:
        seeded = conn.execute(text("SELECT COUNT(*) FROM knowledge_bases WHERE id = 1")).scalar()
    assert seeded == 1, "0002 播种行 id=1 不存在：夹具假设失效"
    kb = [
        (4, "旧库A", "imported", "D:/kb-186", "zhilan_chunks", 1, "ready", None),
        (7, "旧库A-副本", "imported", "D:/kb-186", "zhilan_chunks", 1, "failed", "2026-01-02 03:04:05"),
        (9, "旧库A-带空格", "imported", "  D:/kb-186  ", "zhilan_chunks", 1, "ready", "2026-02-03 04:05:06"),
        (11, "旧库B", "external", "http://kb-186/dup", "zhilan_chunks", 1, "ready", None),
        (12, "旧库B-副本", "external", "http://kb-186/dup", "zhilan_chunks", 1, "ready", None),
    ]
    with engine.begin() as conn:
        for row in kb:
            conn.execute(
                text(
                    "INSERT INTO knowledge_bases (id, name, kind, path_or_url, collection,"
                    " read_only, status, last_sync_at) VALUES (:i, :n, :k, :p, :c, :r, :s, :t)"
                ),
                {"i": row[0], "n": row[1], "k": row[2], "p": row[3], "c": row[4],
                 "r": row[5], "s": row[6], "t": row[7]},
            )
        # notes：kb 4 两条（其一软删）、kb 7 一条、kb 9 一条；kb 1 一条（不许被碰）
        for note_id, kb_id, deleted in [(101, 4, None), (102, 4, "2026-03-01 00:00:00"),
                                        (103, 7, None), (104, 9, None), (105, 1, None)]:
            conn.execute(
                text(
                    "INSERT INTO notes (id, title, content, category, kb_id, deleted_at)"
                    " VALUES (:i, :t, :c, :g, :k, :d)"
                ),
                {"i": note_id, "t": "n" + str(note_id), "c": "正文", "g": "", "k": kb_id, "d": deleted},
            )
        # documents：kb 7 两条（其一软删）、kb 11 一条
        for doc_id, kb_id, deleted in [(201, 7, None), (202, 7, "2026-03-02 00:00:00"), (203, 11, None)]:
            conn.execute(
                text(
                    "INSERT INTO documents (id, filename, file_type, status, index_status,"
                    " chunk_count, vectorized, kb_id, category, deleted_at)"
                    " VALUES (:i, :f, :y, :s, :s, 0, 0, :k, :g, :d)"
                ),
                {"i": doc_id, "f": "d" + str(doc_id) + ".md", "y": "md", "s": "ready",
                 "k": kb_id, "g": "", "d": deleted},
            )
        # wiki_links：kb 4 一条、kb 12 一条（无软删列）
        for link_id, kb_id in [(301, 4), (302, 12)]:
            conn.execute(
                text(
                    "INSERT INTO wiki_links (id, source_type, source_id, kb_id, target_title,"
                    " resolved) VALUES (:i, :st, :si, :k, :tt, 0)"
                ),
                {"i": link_id, "st": "note", "si": 101, "k": kb_id, "tt": "目标" + str(link_id)},
            )
    engine.dispose()


def _ref_counts(db_path: Path) -> dict[str, dict[int, int]]:
    engine = _engine(db_path)
    try:
        out: dict[str, dict[int, int]] = {}
        for table in ("notes", "documents", "wiki_links"):
            rows = _rows(engine, "SELECT kb_id, COUNT(*) AS n FROM " + table + " GROUP BY kb_id")
            out[table] = {int(r["kb_id"]): int(r["n"]) for r in rows}
        return out
    finally:
        engine.dispose()


def _maintain_compatible_duplicates(db_path: Path) -> None:
    """具名维护后才允许自动升级；真本地 Qdrant 空点集也必须逐集合核验。"""
    import json
    from app.core.kb_identity import ensure_kb_maintenance_complete
    from app.vector.qdrant_store import QdrantVectorStore

    spec = importlib.util.spec_from_file_location("s6_186_maintenance_cli", BACKEND_DIR / "scripts" / "kb_merge_duplicates.py")
    tool = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(tool)
    engine = _engine(db_path)
    try:
        bundle = tool.plan_merge(engine)
        bundle["report"].update(tool._target_label(engine))
        files = tool._write_materials(db_path.parent / "materials", bundle)
        tool._begin_pending(engine, bundle, files)
        applied = tool.apply_merge(engine, bundle)
        tool._sql_receipt(applied, "applied")
    finally:
        engine.dispose()
    ledger = Path(applied["journal_path"])
    collections = json.loads(ledger.read_text(encoding="utf-8"))["required_collections"]
    vector_path = str(db_path.parent / "maintenance-vectors")
    for collection in collections:
        store = QdrantVectorStore(path=vector_path, collection=collection)
        try:
            store.ensure_collection(2)
        finally:
            store.close()
        repaired = tool.run_repair_vector(ledger, None, vector_path, collection)
        checked = tool.run_verify_synced(Path(repaired["snapshot"]), None, vector_path, collection)
        assert checked["ok"], checked
    ensure_kb_maintenance_complete("sqlite:///" + db_path.as_posix())


# ---------------------------------------------------------------------------
# 1. canonical bytes：单一真源 + 语义
# ---------------------------------------------------------------------------


def _sample_paths() -> list:
    return [
        None,
        "",
        "   ",
        "\t\n",
        "D:/kb",
        "  D:/kb  ",
        "\tD:/kb\n",
        "d:/kb",
        "D:/kb/",
        "http://kb-186/dup",
        "http://kb-186/dup ",
        LONG_PREFIX_HEAD + "尾A",
        LONG_PREFIX_HEAD + "尾B",
        "知" * 1024,
    ]


def test_kb_path_key_is_lower_hex_sha256_of_stripped_value():
    """身份 = strip 后的完整逐字值；不做任何归一（大小写 / 尾斜杠都算不同身份）。"""
    assert kb_path_key("  D:/kb  ") == hashlib.sha256("D:/kb".encode("utf-8")).hexdigest()
    assert len(kb_path_key("D:/kb") or "") == KB_PATH_KEY_LENGTH
    assert kb_path_key("D:/kb") == kb_path_key("D:/kb")
    assert kb_path_key("D:/kb") != kb_path_key("d:/kb"), "大小写不许被折叠"
    assert kb_path_key("D:/kb") != kb_path_key("D:/kb/"), "尾斜杠不许被去掉"


def test_kb_path_key_blank_is_out_of_the_unique_domain():
    """strip 后为空 / None ⇒ None（不进唯一域，保护本机种子 id=1 的空 path）。"""
    for sample in (None, "", "   ", "\t", "\n  \t"):
        assert kb_path_key(sample) is None, repr(sample)
        assert kb_canonical_path(sample) is None, repr(sample)
    assert kb_canonical_path(" D:/kb ") == "D:/kb"


def test_long_common_prefix_with_different_tail_are_different_keys():
    """长共同前缀、末尾不同 ⇒ 两个不同 key（前缀唯一索引会在这一对误撞号）。"""
    left = kb_path_key(LONG_PREFIX_HEAD + "尾A")
    right = kb_path_key(LONG_PREFIX_HEAD + "尾B")
    assert left is not None and right is not None and left != right


def test_1024_chinese_chars_fit_the_key_column():
    """String(1024) 满长度（中文 1024 字 = utf8mb4 下 4096 字节）也能算出 key。

    这条同时是「为什么不直接对 path_or_url 建唯一索引」的现场证据：4096 字节超过
    InnoDB 的 3072 字节键长上限，摘要列 64 字节则两方言都建得出来。
    """
    key = kb_path_key("知" * 1024)
    assert key is not None and len(key) == KB_PATH_KEY_LENGTH
    assert key != kb_path_key("知" * 1023)


def test_migration_inline_key_matches_app_helper_verbatim():
    """迁移 0007 的内联 `_key` 与 app 助手对同一批样本产出**逐字相同**的 key。

    迁移必须自包含（不能 import 会继续演进的 app 包），所以两份实现同时存在——
    这条判据就是钉住它们不分叉的那颗钉子。
    """
    spec = importlib.util.spec_from_file_location("s6_186_migration_0007", MIGRATION_0007)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for sample in _sample_paths():
        assert module._key(sample) == kb_path_key(sample), repr(sample)
    assert module.UNIQUE_INDEX == UNIQUE_INDEX
    assert module.down_revision == "0006"
    assert module.revision == "0007"


# ---------------------------------------------------------------------------
# 2. 空新库 → head：列与唯一索引真的落地
# ---------------------------------------------------------------------------


def test_empty_database_upgrades_to_0007_with_real_constraint(sqlite_db: Path):
    """空新库一路升到 head：列在、唯一索引在（不是靠健康检查 200 间接证明）。

    ⚠️ `main.py` 的 lifespan 会**吞掉**迁移异常，所以「服务起来了」什么都不证明；
    这条直接读 alembic 版本号与 PRAGMA，是唯一可靠的证据形态。
    """
    _upgrade("0007")
    engine = _engine(sqlite_db)
    try:
        assert _scalar(engine, "SELECT version_num FROM alembic_version") == "0007"
    finally:
        engine.dispose()
    columns = _columns(sqlite_db, "knowledge_bases")
    assert "path_or_url_key" in columns, sorted(columns)
    assert columns["path_or_url_key"].startswith("VARCHAR")
    indexes = _indexes(sqlite_db, "knowledge_bases")
    assert UNIQUE_INDEX in indexes, sorted(indexes)
    assert indexes[UNIQUE_INDEX]["unique"] is True
    assert indexes[UNIQUE_INDEX]["columns"] == ["path_or_url_key"]


# ---------------------------------------------------------------------------
# 3. 0006 形态的重复存量 → 升 0007：合并 / 迁引用 / MIN(id) / 约束
# ---------------------------------------------------------------------------


def test_0006_shaped_duplicates_are_merged_and_refs_migrated(sqlite_db: Path):
    """造出 0006 重复存量，先拒绝自动删行，再具名维护并升级后逐条验收。

    这里同时是「向量侧必须同步」的 SQL 侧前提：三张表（含**软删行**）的 kb_id 都要改向，
    否则检索按 kb 过滤会漏召回（向量腿在副本上单跑，见证据目录）。
    """
    _upgrade("0006")
    _seed_duplicate_0006(sqlite_db)
    before = _ref_counts(sqlite_db)
    assert before["notes"] == {1: 1, 4: 2, 7: 1, 9: 1}, before
    assert before["documents"] == {7: 2, 11: 1}, before
    assert before["wiki_links"] == {4: 1, 12: 1}, before

    with pytest.raises(RuntimeError, match="具名维护"):
        _upgrade("head")
    _maintain_compatible_duplicates(sqlite_db)
    _upgrade("head")

    engine = _engine(sqlite_db)
    try:
        assert _scalar(engine, "SELECT version_num FROM alembic_version") == EXPECTED_HEAD
        ids = [int(r["id"]) for r in _rows(engine, "SELECT id FROM knowledge_bases ORDER BY id")]
        assert ids == [1, 4, 11], ids  # MIN(id) 保留：组 A 留 4、组 B 留 11
        keeper = _rows(
            engine, "SELECT id, name, path_or_url, path_or_url_key FROM knowledge_bases WHERE id = 4"
        )[0]
        assert keeper["path_or_url"] == "D:/kb-186"
        assert keeper["path_or_url_key"] == kb_path_key("D:/kb-186")
        assert keeper["name"] == "旧库A", "保留行必须是 MIN(id) 那一条的原值（不许被覆写）"
        local = _rows(engine, "SELECT path_or_url_key FROM knowledge_bases WHERE id = 1")[0]
        assert local["path_or_url_key"] is None, "空 path 行必须留在唯一域之外"
        second = _rows(
            engine, "SELECT path_or_url_key FROM knowledge_bases WHERE id = 11"
        )[0]
        assert second["path_or_url_key"] == kb_path_key("http://kb-186/dup")
    finally:
        engine.dispose()

    after = _ref_counts(sqlite_db)
    assert after["notes"] == {1: 1, 4: 4}, after  # 7 与 9 的引用都改向 4（含软删行）
    assert after["documents"] == {4: 2, 11: 1}, after
    assert after["wiki_links"] == {4: 1, 11: 1}, after
    engine = _engine(sqlite_db)
    try:
        soft_deleted = _rows(
            engine,
            "SELECT id, kb_id FROM notes WHERE deleted_at IS NOT NULL ORDER BY id",
        )
        assert soft_deleted == [{"id": 102, "kb_id": 4}], soft_deleted
        soft_docs = _rows(
            engine,
            "SELECT id, kb_id FROM documents WHERE deleted_at IS NOT NULL ORDER BY id",
        )
        assert soft_docs == [{"id": 202, "kb_id": 4}], soft_docs
    finally:
        engine.dispose()


def test_unique_constraint_really_rejects_duplicates_after_migration(sqlite_db: Path):
    """迁移建出来的唯一索引**真的拒重复**（不是「索引存在」就算数）。"""
    _upgrade("head")
    engine = _engine(sqlite_db)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO knowledge_bases (id, name, kind, path_or_url, path_or_url_key,"
                    " collection, read_only, status) VALUES (21, :n, :k, :p, :key, :c, 1, :s)"
                ),
                {"n": "第一库", "k": "imported", "p": "D:/kb-186",
                 "key": kb_path_key("D:/kb-186"), "c": "zhilan_chunks", "s": "ready"},
            )
        with pytest.raises(IntegrityError) as excinfo:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO knowledge_bases (id, name, kind, path_or_url, path_or_url_key,"
                        " collection, read_only, status) VALUES (22, :n, :k, :p, :key, :c, 1, :s)"
                    ),
                    {"n": "第二库", "k": "imported", "p": "D:/kb-186",
                     "key": kb_path_key("D:/kb-186"), "c": "zhilan_chunks", "s": "ready"},
                )
        assert "UNIQUE" in str(excinfo.value).upper(), str(excinfo.value)
        # NULL key 不受唯一约束（空 path 行天然多行合法）
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO knowledge_bases (id, name, kind, path_or_url, path_or_url_key,"
                    " collection, read_only, status) VALUES (23, :n, :k, :p, NULL, :c, 0, :s),"
                    " (24, :n, :k, :p, NULL, :c, 0, :s)"
                ),
                {"n": "空路径", "k": "local", "p": "", "c": "", "s": "ready"},
            )
        # 1 = 0002 播种行；23 / 24 = 上面两行空 path（NULL 不受唯一约束）
        blank_ids = _rows(
            engine,
            "SELECT id FROM knowledge_bases WHERE path_or_url_key IS NULL ORDER BY id",
        )
        assert [row["id"] for row in blank_ids] == [1, 23, 24], blank_ids
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 4. create_all 建出的库（测试路径）也必须真的拒重复
# ---------------------------------------------------------------------------


def test_create_all_schema_rejects_duplicate_identity(tmp_path, monkeypatch):
    """`Base.metadata.create_all` 建出来的表同样由唯一索引兜底。

    这是「测试路径（create_all）」与「真实路径（alembic upgrade）」的另一个分叉点：
    只钉迁移侧的话，models 侧漏了索引会在生产之外全绿。
    """
    db_path = (tmp_path / "s6_186_create_all.db").resolve()
    engine = create_engine("sqlite:///" + db_path.as_posix(), future=True)
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(KnowledgeBase(name="甲", kind="imported", path_or_url="D:/kb-186"));
            session.commit()
            assert session.query(KnowledgeBase).one().path_or_url_key == kb_path_key("D:/kb-186")
            session.add(KnowledgeBase(name="乙", kind="imported", path_or_url="  D:/kb-186  "));
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
        with Session(engine) as session:
            session.add(KnowledgeBase(name="本机", kind="local", path_or_url=""));
            session.add(KnowledgeBase(name="本机二", kind="local", path_or_url=""));
            session.add(KnowledgeBase(name="空二", kind="local", path_or_url="   "));
            session.commit()  # 空 path ⇒ key 为 NULL ⇒ 多行合法
            assert session.query(KnowledgeBase).count() == 4
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 5. 不兼容组：fail-closed，零写入
# ---------------------------------------------------------------------------


def test_incompatible_group_aborts_migration_with_zero_writes(sqlite_db: Path):
    """组内 `kind` 不一致 ⇒ 整个迁移中止，且**一行都不写**（版本停 0006、列都没加）。

    这是「不许先合一部分再停」的机械证据：预检在 `add_column` **之前**跑，
    MySQL 的 DDL 隐式提交会让「先加列」变成不可回退，所以顺序本身就是判据的一部分。
    """
    _upgrade("0006")
    engine = _engine(sqlite_db)
    try:
        with engine.begin() as conn:
            for row_id, kind in ((31, "imported"), (32, "external")):
                conn.execute(
                    text(
                        "INSERT INTO knowledge_bases (id, name, kind, path_or_url, collection,"
                        " read_only, status) VALUES (:i, :n, :k, :p, :c, 1, :s)"
                    ),
                    {"i": row_id, "n": "冲突库" + str(row_id), "k": kind,
                     "p": "D:/kb-conflict", "c": "zhilan_chunks", "s": "ready"},
                )
            conn.execute(
                text(
                    "INSERT INTO notes (id, title, content, category, kb_id)"
                    " VALUES (501, :t, :c, :g, 31)"
                ),
                {"t": "n501", "c": "正文", "g": ""},
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError) as excinfo:
        _upgrade("head")
    message = str(excinfo.value)
    assert "S6-186" in message and "已中止" in message, message
    assert "kind 不相等" in message, message

    engine = _engine(sqlite_db)
    try:
        assert _scalar(engine, "SELECT version_num FROM alembic_version") == "0006"
        assert _scalar(engine, "SELECT COUNT(*) FROM knowledge_bases") == 3, "0002 播种行 id=1 + 31/32 共 3 行（不许写入）"
        assert _scalar(engine, "SELECT kb_id FROM notes WHERE id = 501") == 31
    finally:
        engine.dispose()
    assert "path_or_url_key" not in _columns(sqlite_db, "knowledge_bases")
    assert UNIQUE_INDEX not in _indexes(sqlite_db, "knowledge_bases")


def test_collection_mismatch_also_aborts(sqlite_db: Path):
    """`collection` 不一致同样是不兼容组（换集合 = 换向量空间，不许自动合并）。"""
    _upgrade("0006")
    engine = _engine(sqlite_db)
    try:
        with engine.begin() as conn:
            for row_id, collection in ((41, "zhilan_chunks"), (42, "zhilan_chunks_other")):
                conn.execute(
                    text(
                        "INSERT INTO knowledge_bases (id, name, kind, path_or_url, collection,"
                        " read_only, status) VALUES (:i, :n, :k, :p, :c, 1, :s)"
                    ),
                    {"i": row_id, "n": "冲突库" + str(row_id), "k": "imported",
                     "p": "D:/kb-collection", "c": collection, "s": "ready"},
                )
    finally:
        engine.dispose()
    with pytest.raises(RuntimeError) as excinfo:
        _upgrade("head")
    assert "collection 不相等" in str(excinfo.value), str(excinfo.value)
    engine = _engine(sqlite_db)
    try:
        assert _scalar(engine, "SELECT version_num FROM alembic_version") == "0006"
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 6. downgrade 的边界：只撤列 + 索引，不复活行
# ---------------------------------------------------------------------------


def test_downgrade_only_drops_column_and_index(sqlite_db: Path):
    """`downgrade` 撤列 + 索引；**不**复活被合并的行（文档里写死的边界，这里钉住它）。"""
    _upgrade("0006")
    _seed_duplicate_0006(sqlite_db)
    _maintain_compatible_duplicates(sqlite_db)
    _upgrade("head")
    command.downgrade(_cfg(), "0006")
    assert "path_or_url_key" not in _columns(sqlite_db, "knowledge_bases")
    assert UNIQUE_INDEX not in _indexes(sqlite_db, "knowledge_bases")
    after = _ref_counts(sqlite_db)
    assert after["notes"] == {1: 1, 4: 4}, "downgrade 不该把引用改回去（改不回去）"
