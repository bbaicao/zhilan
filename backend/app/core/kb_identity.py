"""知识库唯一身份（S6 · 186）：``path_or_url`` 的 canonical 摘要键。

**唯一身份 = 现有 ``path_or_url`` 字面值 `strip()` 之后的完整逐字值**，
不做任何路径/URL/case 归一（不 ``normpath``、不折叠大小写、不去尾斜杠、不解码百分号）——
两方言共用一个**显式摘要列**（``knowledge_bases.path_or_url_key``，``String(64)`` 小写 hex
sha256）来承载唯一约束。

为什么是摘要列、而不是直接对 ``path_or_url`` 建唯一索引：
MySQL 8.0 的 InnoDB 索引键长上限是 3072 字节，而 ``path_or_url`` 是 ``VARCHAR(1024)`` +
``utf8mb4`` = 4096 字节，**建不出来**。也不接受「缩小 API 长度」或「前缀唯一索引」
（后者会让长共同前缀、末尾不同的两个正常地址误撞号）。SQLite 侧同样用这一列，
避免两方言语义分叉。

**空值语义**：`strip()` 后为空串（或 ``None``）⇒ 返回 ``None``。
NULL 在 MySQL 与 SQLite 下都允许多行，所以空 path 的行**天然不在唯一域内**——
这是为了保护「本机知识库」（``id=1``、path_or_url 为空串）这条既有入口。

**唯一真源**：``app/models`` / 迁移 ``0007`` / ``scripts/kb_merge_duplicates.py`` /
``scripts/migrate_legacy_to_v2.py`` / ``api/note_routes.py`` 都只经本模块取键。
迁移 ``0007`` 内联了一份等价实现（迁移文件必须自包含，不能 import app 包），
``tests/test_kb_identity.py`` 有一条判据断言两者对同一批样本逐字产出相同的 key。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.engine import make_url

__all__ = [
    "KB_PATH_KEY_LENGTH", "kb_canonical_path", "kb_path_key",
    "KnowledgeBaseMaintenanceRequired", "kb_target_identity", "kb_target_token",
    "kb_maintenance_dir", "kb_pending_path", "kb_vector_target_identity",
    "ensure_kb_maintenance_complete",
]

#: 摘要列的声明长度；sha256 hexdigest 恒为 64 个 ASCII 字符。
KB_PATH_KEY_LENGTH = 64


def kb_canonical_path(path_or_url: str | None) -> str | None:
    """唯一身份的 canonical 形态：`strip()` 后的原值；空白 / ``None`` ⇒ ``None``。"""
    if path_or_url is None:
        return None
    canonical = path_or_url.strip()
    return canonical or None


def kb_path_key(path_or_url: str | None) -> str | None:
    """``path_or_url`` ⇒ 唯一身份摘要（小写 hex sha256）。

    返回 ``None`` 表示「该行不在唯一域内」（空 path / NULL path），不是错误。
    """
    canonical = kb_canonical_path(path_or_url)
    if canonical is None:
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class KnowledgeBaseMaintenanceRequired(RuntimeError):
    """Only this failure prevents startup; ordinary offline database behavior stays unchanged."""

    kb_maintenance_required = True


def kb_target_identity(dsn) -> dict:
    """Persist endpoint identity without usernames/passwords; SQLite uses the full file path."""
    url = make_url(dsn)
    dialect = url.get_backend_name()
    if dialect == "sqlite":
        database = url.database or ":memory:"
        return {"dialect": dialect, "database": database if database == ":memory:" else
                os.path.normcase(str(Path(database).resolve()))}
    return {"dialect": dialect, "host": (url.host or "").lower(),
            "port": url.port or 3306, "database": url.database or ""}


def kb_target_token(target: dict) -> str:
    raw = json.dumps(target, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def kb_maintenance_dir() -> Path:
    override = os.environ.get("ZHILAN_KB_MERGE_DIR")
    if override:
        return Path(override).resolve()
    from app.core.env_store import resolve_data_root
    root = resolve_data_root()
    if not getattr(sys, "frozen", False):
        root = root / "backend"
    return root / "data" / "kb-merge"


def kb_pending_path(target: dict, operation_id: str) -> Path:
    if not operation_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in operation_id):
        raise ValueError("invalid KB maintenance operation id")
    return kb_maintenance_dir() / f"kb-maintenance-{kb_target_token(target)}-{operation_id}.pending.json"


def kb_vector_target_identity(url: str | None = None, path: str | None = None) -> dict:
    if path:
        return {"mode": "path", "location": os.path.normcase(str(Path(path).resolve()))}
    parsed = urlsplit(url or "")
    if not parsed.scheme or not parsed.hostname:
        raise ValueError("explicit vector target required")
    host = parsed.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    location = urlunsplit((parsed.scheme.lower(), f"{host}:{port}", parsed.path.rstrip("/"), "", ""))
    return {"mode": "url", "location": location}


def ensure_kb_maintenance_complete(dsn, *, expected_vector_target: dict | None = None) -> None:
    """Read only this SQL target's immutable records; any unreadable/incomplete batch blocks."""
    target = kb_target_identity(dsn)
    directory = kb_maintenance_dir()

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
        prefix = f"kb-maintenance-{kb_target_token(target)}-"
        paths = directory.iterdir() if directory.exists() else ()
        for pending_path in paths:
            if not pending_path.name.startswith(prefix) or not pending_path.name.endswith(".pending.json"):
                continue
            pending, pending_hash = load(pending_path)
            operation = pending["operation_id"]
            if pending.get("schema") != "kb-maintenance/v1" or pending.get("target") != target:
                raise ValueError("maintenance pending target/schema mismatch")
            if pending_path != kb_pending_path(target, operation):
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
                        or (outcome == "verified" and checks.get("leftover_old") != 0)
                        or (expected_vector_target is not None and receipt.get("vector_target") != expected_vector_target)):
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
        raise KnowledgeBaseMaintenanceRequired(
            "S6-186 维护尚未完成或恢复材料不可校验；请保持停写，完成原批次全部 SQL/向量核验后再启动。"
        ) from exc
