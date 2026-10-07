"""S6 · 186：知识库唯一身份列；存量合并先经具名维护。

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-30

本支迁移给 `knowledge_bases` 定一个**两方言可执行的唯一身份**。
重复组必须先在停写窗口内经具名 CLI 合并、保留完整恢复材料并核验全部向量集合。
自动迁移遇重复或未完成维护即在任何 DDL/DML 前中止，不能自动删行后开放服务。
这是 S6 阶段唯一的新 head（`down_revision = "0006"`）。

## 身份口径（Lead 裁定，见 S6 · 186 设计决策 1/2/3）

唯一身份 = `path_or_url` 字面值 `strip()` 之后的**完整逐字值**（不归一化、不折叠
大小写、不去尾斜杠、不 normpath）。因为 `path_or_url` 是 `VARCHAR(1024)` + `utf8mb4`
= 4096 字节，超过 InnoDB 的 3072 字节索引键长上限（MySQL 8.0.46 实测），**不能**
直接对它建唯一索引；也不接受「缩小 API 长度」或「前缀唯一索引」（后者会让长共同
前缀、末尾不同的两个正常地址误撞号）。
⇒ 新增显式摘要列 `path_or_url_key`（`String(64)`，小写 hex sha256），两方言都对
**它**建同一个唯一索引 `uk_knowledge_bases_path_or_url_key`。

`strip()` 后为空串（或 NULL）⇒ key 为 `NULL`；NULL 在 MySQL 与 SQLite 下都允许
多行，所以本机种子行（`id=1`、`path_or_url=''`）天然不在唯一域内。

canonical bytes 的单一真源是 `app.core.kb_identity.kb_path_key`；本文件内联了一份
**逐字等价**的实现（历史迁移必须自包含，不能 import 会继续演进的 app 包）。
等价性由 `tests/test_kb_identity.py` 的一条判据钉住。

## 升级步骤与 fail-closed 口径

1. **维护材料检查 + 全量预检（纯 SELECT、零写入）**：读出所有重复组、每组元数据差异、三张引用表
   （notes / documents / wiki_links，**含软删行**）的引用数、KB 全字段 before-image。
   **任一重复组（包含兼容组）或未完成维护 ⇒ 直接 raise，一行都不写。**
   ⚠️ 预检必须排在 `add_column` **之前**：MySQL 的 DDL 是隐式提交，先加列就再也回不到
   「零写入」了。
2. `add_column`（可空、无 server_default ⇒ 既有行先留 NULL）。
3. 回填 key（逐行 UPDATE；空 path 留 NULL）。
4. 建唯一索引 `uk_knowledge_bases_path_or_url_key`；本迁移不合并、不删行。

## 向量侧的边界（S6 · 186 §1.3）——「SQL 成功而向量未同步 = 整体失败」

CLI 在任何 SQL 改动前写目标绑定、不可覆盖的 pending 与恢复材料；只有全部集合核验成功
（或 SQL/向量完整恢复）后才写独立 completion。迁移校验这两份材料与 SQL/向量回执哈希。
材料目录由 `ZHILAN_KB_MERGE_DIR` 指定；开发态默认 `<backend>/data/kb-merge`，
冻结态默认 `<可执行文件所在目录>/data/kb-merge`，不写临时解包资源目录。

## downgrade 的边界（⛔ 不许写成可完整回滚）

`downgrade()` 只撤「列 + 唯一索引」。它**不能**复活被合并掉的行，也**不能**把已经
改向的 `notes.kb_id` / `documents.kb_id` / `wiki_links.kb_id` 改回旧 id —— 那需要
被删行的主键与逐行改向记录，只有升级时写下的 before-image / 重定向日志里才有。
要真回滚必须先 `downgrade` 再按恢复材料重放
（`scripts/kb_merge_duplicates.py --restore`）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import make_url

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

KB_TABLE = "knowledge_bases"
KEY_COLUMN = "path_or_url_key"
UNIQUE_INDEX = "uk_knowledge_bases_path_or_url_key"
#: 引用 `knowledge_bases.id` 的三张表（普通 Integer 列、不是外键；软删只是标记）
REF_TABLES: tuple[str, ...] = ("notes", "documents", "wiki_links")
#: KB 的**全字段**（before-image 与差异比对都用它；顺序即 SELECT 列序）
KB_FIELDS: tuple[str, ...] = (
    "id",
    "name",
    "kind",
    "path_or_url",
    "collection",
    "read_only",
    "status",
    "last_sync_at",
)


def _key(path_or_url: Any) -> str | None:
    """canonical bytes 的**迁移内联实现**；与 `app.core.kb_identity.kb_path_key` 等价。

    历史迁移必须自包含：app 包会继续演进，而迁移要能在任意未来 checkout 上重跑。
    等价性由 `tests/test_kb_identity.py` 的判据钉住（对同一批样本逐字比 key）。
    """
    if path_or_url is None:
        return None
    canonical = str(path_or_url).strip()
    if canonical == "":
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _kb_rows(bind: sa.Connection) -> list[dict[str, Any]]:
    """全表 before-image（含 NULL 行），按 id 升序。"""
    columns = ", ".join(KB_FIELDS)
    result = bind.execute(sa.text(f"SELECT {columns} FROM {KB_TABLE} ORDER BY id"))
    return [dict(row) for row in result.mappings().all()]


def _ref_counts(bind: sa.Connection, ids: list[int]) -> dict[str, dict[int, int]]:
    """三张引用表对给定 KB id 集的引用行数（**含软删行**：软删只是打标记，行还在）。"""
    counts: dict[str, dict[int, int]] = {}
    for table in REF_TABLES:
        per_table = {int(i): 0 for i in ids}
        if ids:
            stmt = sa.text(
                f"SELECT kb_id, COUNT(*) AS n FROM {table} WHERE kb_id IN :ids GROUP BY kb_id"
            ).bindparams(sa.bindparam("ids", expanding=True))
            for row in bind.execute(stmt, {"ids": ids}).mappings().all():
                per_table[int(row["kb_id"])] = int(row["n"])
        counts[table] = per_table
    return counts


def _preflight(bind: sa.Connection) -> dict[str, Any]:
    """纯 SELECT 的全量预检：重复组、元数据差异、引用数、before-image、可合并性。

    ⛔ 本函数**一行都不写**——它是 fail-closed 的第一道闸。
    """
    rows = _kb_rows(bind)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        row_key = _key(row["path_or_url"])
        row["key"] = row_key
        if row_key is None:  # 空 path ⇒ 不进唯一域
            continue
        grouped.setdefault(row_key, []).append(row)

    groups: list[dict[str, Any]] = []
    for row_key, members in sorted(grouped.items(), key=lambda item: item[1][0]["id"]):
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda row: int(row["id"]))
        kinds = {str(row["kind"]) for row in members}
        collections = {str(row["collection"]) for row in members}
        reasons: list[str] = []
        if len(kinds) > 1:
            reasons.append(f"kind 不相等：{sorted(kinds)}")
        if len(collections) > 1:
            reasons.append(f"collection 不相等：{sorted(collections)}")
        differences: dict[str, dict[str, Any]] = {}
        for field in KB_FIELDS:
            values = {str(row["id"]): row[field] for row in members}
            if len({repr(value) for value in values.values()}) > 1:
                differences[field] = values
        ids = [int(row["id"]) for row in members]
        groups.append(
            {
                "key": row_key,
                "path_or_url": members[0]["path_or_url"],
                "kept_id": ids[0],
                "merged_ids": ids[1:],
                "ids": ids,
                "compatible": not reasons,
                "reasons": reasons,
                "kind": members[0]["kind"],
                "collection": members[0]["collection"],
                "differences": differences,
                "refs": _ref_counts(bind, ids),
                "before_image": [
                    {field: row[field] for field in KB_FIELDS} for row in members
                ],
            }
        )

    return {
        "total_rows": len(rows),
        "groups": groups,
        "incompatible": [group for group in groups if not group["compatible"]],
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _format_report(report: dict[str, Any]) -> str:
    """人可读的冲突清单（迁移中止时的异常消息 / 成功时的变更摘要都用它）。"""
    lines = [
        f"[S6-186] knowledge_bases 预检：全表 {report['total_rows']} 行，"
        f"重复组 {len(report['groups'])} 个（不兼容 {len(report['incompatible'])} 个）"
    ]
    for group in report["groups"]:
        lines.append(
            f"  - key={group['key'][:16]}… path_or_url={group['path_or_url']!r} "
            f"保留 id={group['kept_id']} 合并 {group['merged_ids']} "
            f"compatible={group['compatible']} kind={group['kind']!r} "
            f"collection={group['collection']!r}"
        )
        for reason in group["reasons"]:
            lines.append(f"      ⛔ 不兼容：{reason}")
        for field, values in group["differences"].items():
            lines.append(f"      差异 {field}: {values}")
        for table, per_table in group["refs"].items():
            lines.append(f"      引用 {table}: {per_table}")
        for row in group["before_image"]:
            lines.append(
                "      before-image "
                + json.dumps(
                    {k: _jsonable(v) for k, v in row.items()}, ensure_ascii=False, sort_keys=True
                )
            )
    return "\n".join(lines)


def _journal_dir() -> Path:
    override = os.environ.get("ZHILAN_KB_MERGE_DIR")
    if override:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data" / "kb-merge"
    # backend/alembic/versions/0007_*.py → parents[2] == backend
    return Path(__file__).resolve().parents[2] / "data" / "kb-merge"


class _MaintenanceRequired(RuntimeError):
    kb_maintenance_required = True


def _target_identity(dsn) -> dict:
    url = make_url(dsn)
    dialect = url.get_backend_name()
    if dialect == "sqlite":
        database = url.database or ":memory:"
        return {"dialect": dialect, "database": database if database == ":memory:" else
                os.path.normcase(str(Path(database).resolve()))}
    return {"dialect": dialect, "host": (url.host or "").lower(),
            "port": url.port or 3306, "database": url.database or ""}


def _target_token(target: dict) -> str:
    raw = json.dumps(target, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _pending_path(target: dict, operation_id: str) -> Path:
    if not operation_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in operation_id):
        raise ValueError("invalid KB maintenance operation id")
    return _journal_dir() / f"kb-maintenance-{_target_token(target)}-{operation_id}.pending.json"


def _check_maintenance(dsn) -> None:
    """Historical self-contained counterpart of core.ensure_kb_maintenance_complete."""
    target = _target_identity(dsn)
    directory = _journal_dir()

    def load(path, sha256=None):
        raw = Path(path).read_bytes()
        if sha256 is not None and hashlib.sha256(raw).hexdigest() != sha256:
            raise ValueError("maintenance material hash mismatch")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("maintenance material must be an object")
        return value, hashlib.sha256(raw).hexdigest()

    try:
        if directory.exists() and not directory.is_dir():
            raise ValueError("maintenance directory is not a directory")
        prefix = f"kb-maintenance-{_target_token(target)}-"
        paths = directory.iterdir() if directory.exists() else ()
        for pending_path in paths:
            if not pending_path.name.startswith(prefix) or not pending_path.name.endswith(".pending.json"):
                continue
            pending, pending_hash = load(pending_path)
            operation = pending["operation_id"]
            if pending.get("schema") != "kb-maintenance/v1" or pending.get("target") != target:
                raise ValueError("maintenance pending target/schema mismatch")
            if pending_path != _pending_path(target, operation):
                raise ValueError("maintenance operation filename mismatch")
            artifacts = pending["artifacts_sha256"]
            if not isinstance(artifacts, dict) or not artifacts:
                raise ValueError("maintenance has no recovery material")
            for path, digest in artifacts.items():
                if hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest:
                    raise ValueError("maintenance recovery material changed")
            complete_path = pending_path.with_name(pending_path.name.replace(".pending.json", ".complete.json"))
            complete, _ = load(complete_path)
            outcome = complete.get("outcome")
            collections = pending["required_collections"]
            if (not isinstance(collections, list) or not collections
                    or any(not isinstance(c, str) or not c.strip() for c in collections)
                    or len(collections) != len(set(collections))):
                raise ValueError("maintenance requires explicit unique collections")
            if (complete.get("schema") != "kb-maintenance-complete/v1"
                    or complete.get("target") != target or complete.get("operation_id") != operation
                    or complete.get("pending_sha256") != pending_hash
                    or complete.get("required_collections") != collections
                    or outcome not in ("verified", "restored", "aborted")):
                raise ValueError("maintenance completion does not match pending batch")
            sql_ref = complete["sql_receipt"]
            sql, _ = load(sql_ref["path"], sql_ref["sha256"])
            if (sql.get("schema") != "kb-sql-receipt/v1" or sql.get("target") != target
                    or sql.get("operation_id") != operation
                    or sql.get("outcome") != {"verified": "applied", "restored": "restored", "aborted": "aborted"}[outcome]
                    or sql.get("journal_sha256") not in artifacts.values()
                    or (outcome == "restored" and sql.get("before_image_sha256") not in artifacts.values())):
                raise ValueError("maintenance SQL receipt mismatch")
            receipts = complete["receipts"]
            if outcome == "aborted":
                operation_dir = Path(pending["operation_dir"]).resolve()
                if (receipts != {} or sql.get("rollback_confirmed") is not True
                        or sql.get("no_writes") is not True
                        or Path(complete["sql_receipt"]["path"]).resolve() != operation_dir / "sql-aborted.json"
                        or any(p.name == "sql-applied.json" or p.name.startswith(("sql-restored-", "vector-"))
                               for p in operation_dir.iterdir())):
                    raise ValueError("maintenance abort contains committed SQL/vector evidence")
                state_ref = sql["abort_state"]
                state, _ = load(state_ref["path"], state_ref["sha256"])
                if (Path(sql["abort_state"]["path"]).resolve() != operation_dir / "sql-abort-state.json"
                        or state.get("schema") != "kb-sql-abort-state/v1"
                        or state.get("target") != target or state.get("operation_id") != operation
                        or not isinstance(state.get("before"), dict) or not state["before"]
                        or state["before"] != state.get("after")):
                    raise ValueError("maintenance abort does not prove unchanged transaction state")
                continue
            if set(receipts) != set(collections):
                raise ValueError("maintenance is missing collection receipts")
            for collection in collections:
                receipt_ref = receipts[collection]
                receipt, _ = load(receipt_ref["path"], receipt_ref["sha256"])
                checks = receipt["checks"]
                if (receipt.get("schema") != "kb-vector-receipt/v1" or receipt.get("target") != target
                        or receipt.get("operation_id") != operation or receipt.get("collection") != collection
                        or receipt.get("outcome") != outcome or receipt.get("ok") is not True
                        or receipt.get("journal_sha256") != sql["journal_sha256"]
                        or checks.get("missing") != 0 or checks.get("wrong") != 0
                        or checks.get("checked_points") != checks.get("expected_points")
                        or type(checks.get("expected_points")) is not int or checks["expected_points"] < 0
                        or (outcome == "verified" and checks.get("leftover_old") != 0)):
                    raise ValueError("maintenance vector receipt mismatch")
                snapshot, _ = load(receipt["snapshot"], receipt["snapshot_sha256"])
                if (snapshot.get("schema") != "kb-vector-points/v1"
                        or snapshot.get("target") != target or snapshot.get("operation_id") != operation
                        or snapshot.get("collection") != collection
                        or snapshot.get("vector_target") != receipt.get("vector_target")
                        or snapshot.get("journal_sha256") != receipt["journal_sha256"]
                        or snapshot.get("points_total") != checks["expected_points"]):
                    raise ValueError("maintenance vector snapshot belongs to another batch")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise _MaintenanceRequired(
            "S6-186 维护尚未完成或恢复材料不可校验；请保持停写，完成原批次全部 SQL/向量核验后再启动。"
        ) from exc


def _backfill(bind: sa.Connection) -> int:
    """回填 `path_or_url_key`；返回写入了非 NULL key 的行数。"""
    rows = bind.execute(
        sa.text(f"SELECT id, path_or_url FROM {KB_TABLE} ORDER BY id")
    ).mappings().all()
    payload = [{"kid": _key(row["path_or_url"]), "rid": int(row["id"])} for row in rows]
    if payload:
        bind.execute(
            sa.text(f"UPDATE {KB_TABLE} SET {KEY_COLUMN} = :kid WHERE id = :rid"), payload
        )
    return sum(1 for item in payload if item["kid"] is not None)


def upgrade() -> None:
    bind = op.get_bind()
    _check_maintenance(bind.engine.url)
    report = _preflight(bind)
    if report["groups"]:
        raise _MaintenanceRequired(
            "S6-186 · knowledge_bases 唯一身份迁移已中止（零写入）：存在重复组，必须先停写执行具名维护。\n"
            + _format_report(report)
            + "\n请先以明确 --dsn 运行 scripts/kb_merge_duplicates.py --preflight/--apply，"
            "完成全部集合 --repair-vector/--verify-synced 后再升级；不兼容组须先裁决。"
        )

    try:
        op.add_column(KB_TABLE, sa.Column(KEY_COLUMN, sa.String(length=64), nullable=True))
        _backfill(bind)
        op.create_index(UNIQUE_INDEX, KB_TABLE, [KEY_COLUMN], unique=True)
    except Exception as exc:
        raise _MaintenanceRequired(
            "S6-186 唯一约束迁移未完成；请保持停写，核查并恢复部分表结构后再启动。"
        ) from exc


def downgrade() -> None:
    """⛔ 只撤列 + 唯一索引；**不能**复活被合并的行、也不能把引用改回旧 id。

    真正的回滚 = 先 `downgrade`（含本步骤）再按恢复材料重放被删行与改向记录，
    见本文件顶部「downgrade 的边界」。
    """
    op.drop_index(UNIQUE_INDEX, table_name=KB_TABLE)
    op.drop_column(KB_TABLE, KEY_COLUMN)
