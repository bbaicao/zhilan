"""迁移链与「迁移态 schema == models 声明」的机械守卫（2026-09-19 随条目 `5.3` 开工前置新增）。

## 为什么需要它（开工前侦察的实测结论）

本仓**没有任何测试碰 alembic**（全仓 grep `down_revision` / `0005` / `versions` 在
`backend/tests/` 下 **0 命中**）⇒ 两条风险**完全无人看守**：

1. **迁移链断链是静默的**：`down_revision` 写错 / 两条迁移指向同一个父 / 冒出第二个 head，
   测试套件照样全绿（测试建表走 `Base.metadata.create_all`，根本不跑迁移），
   而真启动时 `main.py` 的 `command.upgrade(config, "head")` 直接抛 `MultipleHeads` / 走错顺序。
2. **迁移态与 models 声明可以静默漂移**：`tests/conftest.py` 用 `drop_all + create_all` 建库，
   真实库走 `alembic upgrade head` —— 两条路各建一套 schema。`0005` 的 docstring 自己
   写着「列/索引要与 `Memory` 逐字对齐，否则两条路会分叉」，但**没有任何断言把它钉住**。

## 这个文件钉什么

- **链的形状**：恰好一个 head，且 `0005`（当前 head）能一路走回 `None`（不断链、无环）；
- **迁移态 == models**：在**临时 SQLite 文件**上跑 `alembic upgrade head`，把建出来的表/列
  与 `Base.metadata` 逐表比对。表集不全等 ⇒ 红；同名表的列集不全等 ⇒ 红。

## 有意不做的两件事

- **不比索引/约束/key 长度**：SQLite 与 MySQL 的 DDL 渲染差异大（`utf8mb4` 参数、
  `with_variant`、自动索引名），比它们只会做出一个天天假红的看门狗。
  列**类型**只比家族名（`VARCHAR(32)` → `VARCHAR`），比的是「改错类型」这一档。
- **不动真库**：全程在 `tmp_path` 的 SQLite 文件上跑，用完即删；不碰 `zhilan_v2`。

## ⚠️ 两条**必须**遵守的安全口径（实测踩过）

1. **`alembic/env.py` 的 URL 取自 `settings.database_dsn`，不看 `alembic.ini` 的
   `sqlalchemy.url`**（`env.py:48-49` 用 `_sync_url()` 覆盖它）。开发态 `DB_BACKEND` 默认
   `mysql` ⇒ 直接 `command.upgrade()` 会**把整个迁移跑在真 MySQL 上**（并把 alembic 版本号
   推到 head）。所以下面所有用例都用 :func:`_sqlite_settings` 夹具把
   `DB_BACKEND` 钉成 `sqlite` + `SQLITE_PATH` 钉到 `tmp_path`——**这是安全前提，不是可选设置**。
2. 建表与读列都用**同步** sqlite3 / 同步引擎：临时文件库与异步引擎无关，
   用异步反而要把事件循环带进来（无谓复杂度）。

## 与 `tests/test_models_schema.py` 的分工

那个文件管 **models 侧**（表清单 + 关键列），不连库；本文件管 **迁移侧**（链 + 迁移产物）。
两边一起才盖住「声明」与「落地」。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from app.models import Base

BACKEND_DIR = Path(__file__).resolve().parent.parent
#: 当前 head（改迁移时**这一行要跟着改**——它是断言的一部分，故意写死成第二个锚点）
# 工具升级-3 · 回归检查 = 0006（chunks 行号/符号三列 + documents.source_encoding）；
# S6 · 186 = 0007（knowledge_bases.path_or_url_key + 唯一索引 uk_knowledge_bases_path_or_url_key，
# 存量重复合并见 backend/alembic/versions/0007_knowledge_base_identity.py）。
# ⚠️ 每次新增迁移都必须同笔推进本常量 —— 这是全仓**唯一**看守迁移链的地方（见 :92 的断言文案）。
# 算法升级 U2：构建身份、正文版本与按构建保留分块。
# 算法升级 U3：SQL 词项倒排及笔记内部片段。
# 算法升级 U6：记忆修订链、有效时间与来源摘要。
# 专项 · 图谱自动连线 = 0011（document_similarity_edges：文档代表向量余弦的相似边；
# 默认关态下新表不读不写，见 backend/app/pipeline/graphlink.py）。
EXPECTED_HEAD = "0011"


@pytest.fixture
def migrated_db(tmp_path, monkeypatch) -> Path:
    """把迁移跑在**临时 SQLite 文件**上、返回库路径。

    ⚠️ 先钉 `DB_BACKEND=sqlite` + `SQLITE_PATH=<tmp>` **再** 跑 upgrade——
    不钉就是拿真开发库（MySQL）当靶子（见模块 docstring 第 1 条）。
    """
    from app.core.config import settings

    db_path = tmp_path / f"migration_guard_{tmp_path.name}.db"
    monkeypatch.setattr(settings, "DB_BACKEND", "sqlite")
    monkeypatch.setattr(settings, "SQLITE_PATH", str(db_path))
    assert settings.is_sqlite, "夹具没把方言钉成 sqlite —— 拒绝在真库上跑迁移"
    assert settings.sqlite_file == db_path, "夹具没把库路径钉到 tmp_path —— 拒绝在真库上跑迁移"

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")
    assert db_path.exists(), "迁移没有产出预期的 SQLite 文件"
    return db_path


def _config() -> Config:
    """只给 ScriptDirectory 读链用（不连库、不跑迁移）。"""
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


def test_migration_chain_has_a_single_head():
    """恰好一个 head，且它就是 :data:`EXPECTED_HEAD`（多个 head / 断链都在这条红）。"""
    heads = ScriptDirectory.from_config(_config()).get_heads()
    assert heads == [EXPECTED_HEAD], (
        f"迁移链的 head 不是唯一/不是预期的 {EXPECTED_HEAD}：{heads}。"
        "多半是 down_revision 写错或新迁移没接上——本仓没有任何别的地方看守这条链。"
    )


def test_migration_chain_walks_back_to_root():
    """从 head 一路走回起点、**不断链**（`down_revision` 指向不存在的 revision 会在这里红）。"""
    revisions = [item.revision for item in ScriptDirectory.from_config(_config()).walk_revisions()]
    assert revisions[-1] == "0001", f"链尾不是首条迁移：{revisions[-1]}"
    assert len(revisions) == len(set(revisions)), f"链里有重复 revision：{revisions}"


def _columns(db_path: Path, table: str) -> dict[str, str]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        connection.close()
    return {row[1]: (row[2] or "").split("(")[0].upper() for row in rows}


def test_migrated_schema_matches_models(migrated_db: Path):
    """**迁移态 == models 声明**：逐表比表集与列集。

    这是「`create_all` 建库的测试路径」与「`alembic upgrade head` 的真实路径」之间
    **唯一的机械对齐点**（`0005` 的 docstring 说的就是这件事，此前只有文字、没有断言）。
    """
    engine = create_engine(f"sqlite:///{migrated_db.as_posix()}")
    try:
        migrated = {name for name in inspect(engine).get_table_names() if name != "alembic_version"}
    finally:
        engine.dispose()
    declared = set(Base.metadata.tables)
    assert migrated == declared, (
        f"迁移建出来的表与 models 声明不一致：迁移多 {sorted(migrated - declared)} / "
        f"声明多 {sorted(declared - migrated)}"
    )

    for name in sorted(declared):
        migrated_cols = set(_columns(migrated_db, name))
        declared_cols = {column.name for column in Base.metadata.tables[name].columns}
        assert migrated_cols == declared_cols, (
            f"表 {name} 的列不一致：迁移多 {sorted(migrated_cols - declared_cols)} / "
            f"声明多 {sorted(declared_cols - migrated_cols)}"
        )


def test_migrated_column_types_match_family(migrated_db: Path):
    """列**类型家族**一致（`VARCHAR(32)` 与 `VARCHAR` 同族 ⇒ 不比长度，只比家族）。

    刻意不比索引/约束/key 长度：SQLite 与 MySQL 的 DDL 渲染差异会让那种断言天天假红。
    """
    for name, table in Base.metadata.tables.items():
        migrated = _columns(migrated_db, name)
        for column in table.columns:
            family = _family(column.type.__class__.__name__.upper())
            allowed = _FAMILIES.get(family, {family})
            assert migrated[column.name] in allowed, (
                f"{name}.{column.name} 的类型家族不符：迁移 {migrated[column.name]!r} / "
                f"声明 {family!r}（允许 {sorted(allowed)}）"
            )


#: SQLAlchemy 的抽象类型 → 该家族在两方言下**可能**渲染出的名字。
#: 只按家族比：`VARCHAR(32)` 与 `VARCHAR` 同族（长度差异不在本断言的范围里）。
_FAMILIES: dict[str, set[str]] = {
    "STRING": {"VARCHAR", "TEXT", "LONGTEXT", "CHAR"},
    "INTEGER": {"INTEGER", "BIGINT", "SMALLINT"},
    "DATETIME": {"DATETIME", "TIMESTAMP"},
    "JSON": {"JSON", "TEXT"},
    "BOOLEAN": {"BOOLEAN", "TINYINT"},
    "FLOAT": {"FLOAT", "REAL", "DOUBLE", "DECIMAL", "NUMERIC"},
    "LARGEBINARY": {"BLOB", "BINARY", "VARBINARY"},
}


def _family(name: str) -> str:
    """把 SQLAlchemy 侧的类型名归一到家族名（`String` → `STRING`，`Text` → `STRING` …）。"""
    return {
        "STRING": "STRING",
        "VARCHAR": "STRING",
        "TEXT": "STRING",
        "INTEGER": "INTEGER",
        "BIGINTEGER": "INTEGER",
        "SMALLINTEGER": "INTEGER",
        "DATETIME": "DATETIME",
        "JSON": "JSON",
        "BOOLEAN": "BOOLEAN",
        "FLOAT": "FLOAT",
        "NUMERIC": "FLOAT",
    }.get(name, name)


if __name__ == "__main__":  # pragma: no cover —— 手工跑：打印当前 head 便于改迁移时自查
    print(f"head = {ScriptDirectory.from_config(_config()).get_heads()} 期望 = ['{EXPECTED_HEAD}']")
    pytest.main([__file__, "-q"])
