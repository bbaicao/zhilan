r"""knowledge_bases 唯一身份：只读预检 / 合并 / 恢复 / 向量修复（S6 · 186）。

**为什么不能只写一段裸 SQL 合并**：唯一身份去重会**不可逆地删行并改引用**
（``notes.kb_id`` / ``documents.kb_id`` / ``wiki_links.kb_id`` 三处普通 Integer 引用，
含回收站行）。裸 SQL 既拿不出「改前长什么样」，也说不清「谁被并到了谁」——
出了纠纷没有回头路。本器把这件事拆成四段，每段都能单独跑、单独验：

| 模式 | 写什么 | 输出 |
|---|---|---|
| ``--preflight``（默认） | SQL 数据只读，写预检材料 | ``preflight-report-<ts>.json`` / ``.md``：重复组、每组差异、old→kept 映射、三张表引用行数、KB 全字段 before-image |
| ``--apply`` | 一个事务里改引用 + 删重复行（+ 回填 key 列，若列在） | 独占批次目录、before-image、redirects、恢复 SQL 与 pending；SQL 成功后应用仍阻断 |
| ``--repair-vector`` | 向量侧按最初快照的点 id 改写 payload ``kb_id``（旧 id → 保留 id） | 不覆盖的 ``vector-points-*.json`` + hash 封条；重试复用首轮快照 |
| ``--verify-synced`` | 向量数据只读，写核验材料（可执行的「重新开放条件」） | 逐组读数：旧 id 的过滤召回必须为 0、快照里的点必须都在且 ``kb_id`` 已指到保留行；任一不满足 ⇒ 非零退出 |
| ``--restore-vector`` | 按 ``vector-points-*.json`` 把那些点的 ``kb_id`` 改回旧值（不删点、不新增点） | 控制台读数 + 逐点回查 + 恢复凭证 |
| ``--restore FILE`` | 一个事务里把引用改回、把被删行按原 id 插回 | 控制台读数 |

**为什么必须有向量修复这一段**：检索按 ``kb_id`` 下推过滤（``retrieval/vector.py``）。
SQL 侧把旧 kb 并进保留行、向量 payload 里却还写着旧 id ⇒ 那一整库**永远检索不到**
（静默漏召回，不报错）。所以 SQL 成功而向量未同步 = **整体失败**。

**向量硬门的 runbook（顺序不许换）**：

1. **停写 / 停检索**：合并窗口内不许有新文档写入，也不许把「旧 id 过滤召回为空」当成
   已经同步的证据（合并前它本来就是空的另一种原因）。
2. ``--apply``：改 SQL（before-image + redirects 账本先落盘、再开事务，见下）。
3. 对 redirects 中 ``required_collections`` **每个集合**分别执行 ``--repair-vector``：
   集合来自 KB 元数据与生产默认集合的并集，空集合也须留零点证据。**先把受影响点 id 快照落盘**，
   再原地改 payload。没有快照就说不清「原本哪些点属于哪个 kb」= 不可还原。
4. ``--verify-synced``：确认旧 id 的过滤召回为 0 且快照里的点都已指向保留行；
   **只有全部集合绿且 SQL / 向量凭证都归属于该目标、该批次，维护门才放行**。
5. 出问题就 ``--restore-vector``（向量）配合 ``--restore``（SQL）一起回退。
   SQL 与向量**不是同一个事务**，两步之间必须保持停写窗口。已完成批次开始恢复时
   另立恢复 pending，原来的完成章不能放行；若版本已是 0007，先 downgrade 0006。
   0007 本身只检查重复组并拒绝启动，不自动合并或产出重定向账本。

**中止即零写入**：预检里只要有一个组不兼容（``kind`` 或 ``collection`` 不一致），
``--apply`` 在**同一个事务**里先复核再动手，发现不兼容直接抛错——事务回滚，一行不改。
不兼容时也**不写任何材料**（报告只在预检模式落盘）。
材料落盘后若事务内发生 RuntimeError，只有明确回滚、最新全状态前后相同且从未进入
向量腿，才记 ``aborted`` 完成章并允许重新预检；数据库错误或提交状态不明仍保持阻断。

**先落材料、再动数据**：``--apply`` 的顺序是「只读复核 → 写 before-image/账本/恢复 SQL →
开事务改库」。材料成功而事务未成功 ⇒ 数据未合并，必须按确定回滚或不明状态登记；反过来（先改库后写材料）⇒
有数据没账本，**不可恢复**。所以顺序不许换。

用法::

    # ① 只读预检（独立临时 SQLite 或显式独立测试库）
    python scripts/kb_merge_duplicates.py --dsn "sqlite:///E:/tmp/kb-186.db" --preflight

    # ② 真合并（材料位于 <out>/<operation_id>/，开发/冻结态与应用维护门目录同源）
    python scripts/kb_merge_duplicates.py --dsn "sqlite:///E:/tmp/kb-186.db" --apply

    # ③ 向量同步（吃 ② 的 redirects 账本；先落 vector-points 快照再改）
    python scripts/kb_merge_duplicates.py --repair-vector --journal data/kb-merge/<operation_id>/redirects-<operation_id>.json --qdrant-url http://127.0.0.1:6333 --collection zhilan_chunks

    # ④ 重新开放前的校验（向量只读、写核验材料；rc != 0 就是还没同步干净）
    python scripts/kb_merge_duplicates.py --verify-synced --journal <repair输出的snapshot> --qdrant-url http://127.0.0.1:6333 --collection zhilan_chunks

    # ⑤ 向量回退（把 ③ 里那些点的 kb_id 改回旧值）
    python scripts/kb_merge_duplicates.py --restore-vector --journal <同目标的snapshot> --qdrant-path "E:/tmp/qdrant" --collection zhilan_chunks

    # ⑥ SQL 恢复（吃 ② 的 before-image）
    python scripts/kb_merge_duplicates.py --dsn "sqlite:///E:/tmp/kb-186.db" --restore data/kb-merge/<operation_id>/before-image-<operation_id>.json

材料绑定完整 SQL 路径/端点、批次、全部集合与显式向量目标，并且独占写，不许覆盖。
多个候选账本必须给 ``--journal``；旧 schema 材料只可留档；``--force-restore-target``
不能豁免跨库目标检查。副本必须从开始就在该副本具名维护。

**目标守卫**：``--dsn`` 指向 ``.env`` 里的开发库（MySQL 的 host/port/database 或
``SQLITE_PATH``）时**拒绝启动**（退出码 2）。要动安装库必须显式
``--i-know-this-is-the-install-db`` —— 那是业主授权之后的事，本阶段任何自动腿都不许带它。

退出码：``0`` 成功（预检通过 / 合并完成 / 修复完成 / 恢复完成）；``1`` 运行失败；
``2`` 前置不满足（有未决组 / 守卫拒绝 / 恢复前置不满足）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, make_url

SCRIPT_PATH = Path(__file__).resolve()
BACKEND_DIR = SCRIPT_PATH.parents[1]
REPO_DIR = SCRIPT_PATH.parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.kb_identity import (  # noqa: E402
    ensure_kb_maintenance_complete,
    kb_maintenance_dir,
    kb_path_key,
    kb_pending_path,
    kb_target_identity,
    kb_vector_target_identity,
)

KB_TABLE = "knowledge_bases"
KEY_COLUMN = "path_or_url_key"
UNIQUE_INDEX = "uk_knowledge_bases_path_or_url_key"
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
# 三处 kb_id 引用（都是普通 Integer，不是外键；含回收站行 —— 软删只是 deleted_at 非空）
REF_TABLES: tuple[str, ...] = ("notes", "documents", "wiki_links")
# 只有这两个字段全组一致才允许自动合并：kind 改语义，collection 换向量集合。
INCOMPATIBLE_FIELDS: tuple[str, ...] = ("kind", "collection")
# 报告里要逐组比对的字段（差异如实登记，但**不**因此拒绝合并）
DIFF_FIELDS: tuple[str, ...] = ("name", "read_only", "status", "last_sync_at")

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_PRECONDITION = 2

SQ = chr(39)  # SQL 字符串字面量的单引号（生成人读的 restore.sql 用）

# 旧迁移账本也进入歧义检测；旧 schema 只可留档，不能自动修复或恢复。
MIGRATION_LEDGER_PREFIX = "kb-merge-redirects-"
# 默认开发目录；冻结态与环境覆盖由 kb_maintenance_dir 统一解析。
DEFAULT_OUT_SUBDIR = ("data", "kb-merge")
VECTOR_SNAPSHOT_PREFIX = "vector-points-"
VECTOR_SNAPSHOT_SCHEMA = "kb-vector-points/v1"


def default_out_dir() -> Path:
    """与应用维护门及迁移预检同源的材料目录。"""
    return kb_maintenance_dir()


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _target_label(engine: Engine) -> dict[str, Any]:
    """Full endpoint identity; never includes credentials."""
    return kb_target_identity(engine.url)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json_once(path: Path, payload: dict[str, Any]) -> None:
    """Recovery evidence is immutable; an existing file is never replaced."""
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, default=str)


def _receipt_ref(path: Path) -> dict[str, str]:
    return {"path": str(path.resolve()), "sha256": _sha256(path)}


def _validate_managed_ledger(path: Path, *, sql_applied: bool = False) -> dict[str, Any]:
    ledger = json.loads(path.read_text(encoding="utf-8"))
    if ledger.get("schema") != "kb-merge-redirects/v2":
        raise SystemExit("REFUSED：旧账本或未知 schema 只可留档，须先建立当前具名维护批次。")
    pending_path = kb_pending_path(ledger["target"], ledger["operation_id"])
    if str(pending_path) != ledger.get("pending"):
        raise SystemExit("REFUSED：账本维护目标/操作不符。")
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    if (pending.get("schema") != "kb-maintenance/v1"
            or pending.get("target") != ledger["target"]
            or pending.get("operation_id") != ledger["operation_id"]
            or pending.get("required_collections") != ledger["required_collections"]
            or pending.get("operation_dir") != ledger["operation_dir"]):
        raise SystemExit("REFUSED：维护批次绑定不符。")
    artifacts = pending["artifacts_sha256"]
    if artifacts.get(str(path.resolve())) != _sha256(path):
        raise SystemExit("REFUSED：重定向账本不属于原批次材料。")
    for file, digest in artifacts.items():
        if _sha256(Path(file)) != digest:
            raise SystemExit("REFUSED：恢复材料已变化，不允许写数据。")
    if sql_applied:
        if kb_pending_path(ledger["target"], ledger["operation_id"] + "-restore").exists():
            raise SystemExit("REFUSED：本批已进入恢复，不许按旧账本再次修复向量。")
        receipt = json.loads((Path(ledger["operation_dir"]) / "sql-applied.json").read_text(encoding="utf-8"))
        if (receipt.get("schema") != "kb-sql-receipt/v1"
                or receipt.get("outcome") != "applied"
                or receipt.get("target") != ledger["target"]
                or receipt.get("operation_id") != ledger["operation_id"]
                or receipt.get("journal_sha256") != _sha256(path)):
            raise SystemExit("REFUSED：SQL 提交凭证与原批次不符。")
    return ledger


def guard_target(dsn: str, allow_install: bool) -> None:
    """目标不是开发库的**在脚本里断言**（业主红线：不许连开发库 / 安装库）。"""
    from app.core.config import settings

    url = make_url(dsn)
    dev_mysql = make_url(settings.mysql_dsn)
    dev_sqlite = Path(str(settings.sqlite_file)).resolve()
    if url.get_backend_name() == "sqlite":
        hit = Path(str(url.database or "")).resolve() == dev_sqlite
        label = Path(str(url.database or "")).name
    else:
        hit = (
            url.host == dev_mysql.host
            and (url.port or 3306) == (dev_mysql.port or 3306)
            and (url.database or "") == (dev_mysql.database or "")
        )
        label = str(url.database or "")
    if hit and not allow_install:
        raise SystemExit(
            "REFUSED：--dsn 指向 .env 里的开发库（" + label + "）。"
            "本器只许在独立临时 SQLite 文件或显式独立测试库上运行；"
            "真要动安装库必须由业主显式授权并传 --i-know-this-is-the-install-db。"
        )


def _columns(engine: Engine, table: str) -> dict[str, Any]:
    return {c["name"]: c for c in sa.inspect(engine).get_columns(table)}


def _indexes(engine: Engine, table: str) -> dict[str, dict[str, Any]]:
    return {ix["name"]: ix for ix in sa.inspect(engine).get_indexes(table)}


def _existing_ref_tables(engine: Engine) -> tuple[str, ...]:
    names = set(sa.inspect(engine).get_table_names())
    return tuple(t for t in REF_TABLES if t in names)


def _kb_rows(conn: sa.Connection) -> list[dict[str, Any]]:
    sql = "SELECT " + ", ".join(KB_FIELDS) + " FROM " + KB_TABLE + " ORDER BY id"
    return [dict(r) for r in conn.execute(sa.text(sql)).mappings().all()]


def _ref_ids(conn: sa.Connection, table: str, kb_ids: list[int]) -> dict[int, list[int]]:
    """每张引用表里 ``kb_id ∈ kb_ids`` 的行 id（**同一事务内**读，用于改向与恢复）。"""
    out: dict[int, list[int]] = {int(i): [] for i in kb_ids}
    if not kb_ids:
        return out
    stmt = sa.text(
        "SELECT kb_id, id FROM " + table + " WHERE kb_id IN :ids ORDER BY id"
    ).bindparams(sa.bindparam("ids", expanding=True))
    for row in conn.execute(stmt, {"ids": [int(i) for i in kb_ids]}).mappings():
        out.setdefault(int(row["kb_id"]), []).append(int(row["id"]))
    return out


def _differences(members: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """组内「同字段不同值」的登记：字段 → {行 id: 值}（只有不一致的才登记）。"""
    out: dict[str, dict[str, Any]] = {}
    for field in DIFF_FIELDS + INCOMPATIBLE_FIELDS:
        if len({_iso(m.get(field)) for m in members}) > 1:
            out[field] = {str(m["id"]): _iso(m.get(field)) for m in members}
    return out


def analyze(conn: sa.Connection, ref_tables: tuple[str, ...]) -> dict[str, Any]:
    """只读分析：重复组 / 兼容性 / 引用行数。**不写任何一行。**"""
    rows = _kb_rows(conn)
    by_key: dict[str, list[dict[str, Any]]] = {}
    null_rows: list[int] = []
    for row in rows:
        key = kb_path_key(row.get("path_or_url"))
        if key is None:
            null_rows.append(int(row["id"]))
        else:
            by_key.setdefault(key, []).append(row)
    groups: list[dict[str, Any]] = []
    for key in sorted(by_key):
        members = sorted(by_key[key], key=lambda r: int(r["id"]))
        if len(members) < 2:
            continue
        reasons: list[str] = []
        for field in INCOMPATIBLE_FIELDS:
            if len({str(m.get(field) or "") for m in members}) > 1:
                reasons.append(
                    field + " 不一致：" + "、".join(
                        str(m["id"]) + "=" + repr(m.get(field)) for m in members
                    )
                )
        groups.append(
            {
                "key": key,
                "ids": [int(m["id"]) for m in members],
                "kept_id": int(members[0]["id"]),
                "merged_ids": [int(m["id"]) for m in members[1:]],
                "compatible": not reasons,
                "reasons": reasons,
                "differences": _differences(members),
                "rows": [{f: _iso(m.get(f)) for f in KB_FIELDS} for m in members],
            }
        )
    probe_ids = sorted({i for g in groups for i in g["ids"]})
    ref_ids: dict[str, dict[str, list[int]]] = {}
    for table in ref_tables:
        per_kb = _ref_ids(conn, table, probe_ids)
        ref_ids[table] = {str(k): [int(x) for x in v] for k, v in per_kb.items()}
    for group in groups:
        group["ref_counts"] = {
            table: {str(i): len(ref_ids[table].get(str(i), [])) for i in group["ids"]}
            for table in ref_tables
        }
    return {
        "kb_rows": len(rows),
        "null_key_ids": null_rows,
        "groups": groups,
        "incompatible": [g for g in groups if not g["compatible"]],
        "merged_ids": sorted({i for g in groups for i in g["merged_ids"]}),
        "ref_tables": list(ref_tables),
        "ref_ids": ref_ids,
        "ref_totals": {t: sum(len(v) for v in ref_ids[t].values()) for t in ref_tables},
    }


def _metadata(engine: Engine) -> dict[str, Any]:
    cols = _columns(engine, KB_TABLE)
    unique_index = _indexes(engine, KB_TABLE).get(UNIQUE_INDEX)
    return {
        "key_column_present": KEY_COLUMN in cols,
        "unique_index_present": unique_index is not None,
        "unique_index_is_unique": bool(unique_index and unique_index.get("unique")),
    }


def run_preflight(engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        report = analyze(conn, _existing_ref_tables(engine))
    report.update(_metadata(engine))
    return report


def _write_keys(conn: sa.Connection, rows: list[dict[str, Any]]) -> int:
    params = [{"k": kb_path_key(r.get("path_or_url")), "i": int(r["id"])} for r in rows]
    if not params:
        return 0
    conn.execute(
        sa.text("UPDATE " + KB_TABLE + " SET " + KEY_COLUMN + " = :k WHERE id = :i"), params
    )
    return len(params)


def plan_merge(engine: Engine) -> dict[str, Any]:
    """**只读**复核并组装 bundle（不写一行、不写材料）：给 --apply 走「先落材料再改库」。"""
    ref_tables = _existing_ref_tables(engine)
    meta = _metadata(engine)
    with engine.connect() as conn:
        report = analyze(conn, ref_tables)
    if report["incompatible"]:
        raise RuntimeError(
            "S6-186 · 存在无法自动合并的重复组（" + str(len(report["incompatible"]))
            + " 组）：kind / collection 不一致的组不许自动合并。事务回滚，零写入。"
        )
    report.update(meta)
    redirects = [
        {
            "from": int(old),
            "to": int(group["kept_id"]),
            "collection": str(group["rows"][0].get("collection") or ""),
            "references": {
                table: [int(i) for i in report["ref_ids"][table].get(str(old), [])]
                for table in ref_tables
            },
        }
        for group in report["groups"]
        for old in group["merged_ids"]
    ]
    report["moved_refs"] = {t: 0 for t in ref_tables}
    report["keys_written"] = 0
    return {"report": report, "redirects": redirects,
            "stamp": _now_stamp() + "-" + uuid.uuid4().hex}


def _sql_state(conn: sa.Connection) -> dict[str, Any]:
    """Current transaction scope, including nonduplicate rows and the actual key/index."""
    inspector = sa.inspect(conn)
    tables = [KB_TABLE] + [t for t in REF_TABLES if t in inspector.get_table_names()]
    state = {
        "knowledge_bases": [dict(r) for r in conn.execute(
            sa.text("SELECT * FROM " + KB_TABLE + " ORDER BY id")).mappings()],
        "references": {table: [dict(r) for r in conn.execute(
            sa.text("SELECT id, kb_id FROM " + table + " ORDER BY id")).mappings()]
            for table in tables if table != KB_TABLE},
        "structure": {table: {
            "columns": [{"name": c["name"], "type": str(c["type"]), "nullable": c["nullable"]}
                        for c in inspector.get_columns(table)],
            "indexes": sorted([{"name": i["name"], "columns": i["column_names"], "unique": bool(i["unique"])}
                               for i in inspector.get_indexes(table)], key=lambda i: i["name"]),
        } for table in tables},
    }
    return json.loads(json.dumps(state, ensure_ascii=False, default=str))


def apply_merge(engine: Engine, bundle: dict[str, Any]) -> dict[str, Any]:
    """一个事务里：复核 → 改引用 → 删重复行 → 回填 key。不兼容 ⇒ 抛错回滚（零写入）。

    事务内完整 before-image / 引用集合必须与已经落盘的计划一致；任何漂移都先中止。
    """
    ref_tables = _existing_ref_tables(engine)
    meta = _metadata(engine)
    redirects: list[dict[str, Any]] = []
    moved: dict[str, int] = {t: 0 for t in ref_tables}
    with engine.begin() as conn:
        bundle["abort_before"] = _sql_state(conn)
        try:
            report = analyze(conn, ref_tables)
            if report["incompatible"]:
                raise RuntimeError(
                    "S6-186 · 存在无法自动合并的重复组（" + str(len(report["incompatible"]))
                    + " 组）：kind / collection 不一致的组不许自动合并。事务回滚，零写入。"
                )
            planned = bundle["report"]
            for field in ("kb_rows", "null_key_ids", "groups", "ref_tables", "ref_ids"):
                if report[field] != planned[field]:
                    raise RuntimeError("S6-186 · 计划材料发生漂移，请重新预检；事务中止，零写入。")
            if any(meta[key] != planned[key] for key in meta):
                raise RuntimeError("S6-186 · 表结构发生变化，请重新预检；事务中止，零写入。")
            for group in report["groups"]:
                kept = int(group["kept_id"])
                for old in group["merged_ids"]:
                    refs: dict[str, list[int]] = {}
                    for table in ref_tables:
                        ids = [int(i) for i in report["ref_ids"][table].get(str(old), [])]
                        refs[table] = ids
                        if not ids:
                            continue
                        result = conn.execute(
                            sa.text("UPDATE " + table + " SET kb_id = :kept WHERE kb_id = :old"),
                            {"kept": kept, "old": int(old)},
                        )
                        if int(result.rowcount or 0) != len(ids):
                            raise RuntimeError(
                                "S6-186 · " + table + " 引用改向行数不符：预期 " + str(len(ids))
                                + "，实际 " + str(result.rowcount)
                            )
                        moved[table] += len(ids)
                    conn.execute(
                        sa.text("DELETE FROM " + KB_TABLE + " WHERE id = :old"), {"old": int(old)}
                    )
                    redirects.append({"from": int(old), "to": kept,
                                      "collection": str(group["rows"][0].get("collection") or ""),
                                      "references": refs})
            keys_written = _write_keys(conn, _kb_rows(conn)) if meta["key_column_present"] else 0
        except RuntimeError:
            conn.rollback()
            bundle["rollback_confirmed"] = True
            raise
    report.update(meta)
    report["moved_refs"] = moved
    report["keys_written"] = keys_written
    out = dict(bundle)
    out.update({"report": report, "redirects": redirects})
    if "stamp" not in out:
        out["stamp"] = _now_stamp()
    return out


def _coerce(value: Any, column_type: Any) -> Any:
    if isinstance(value, str) and isinstance(column_type, sa.DateTime):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return value


def run_restore(
    engine: Engine, image_path: Path, drop_index: bool, *, force_target: bool = False
) -> dict[str, Any]:
    """按 before-image 插回被合并的行、把引用改回旧 id。

    先比对 image["target"] 与当前 --dsn；--force-restore-target 不能豁免完整维护门。
    缺少完整目标、未绑定原材料或属于另一个库均先拒绝。
    """
    image = json.loads(image_path.read_text(encoding="utf-8"))
    target = image.get("target") or {}
    current = _target_label(engine)
    if target != current:
        raise SystemExit(
            "REFUSED：before-image 记录的目标是 " + repr(target) + "，当前 --dsn 是 "
            + repr(current) + "。恢复会把行插回另一个库；请先在副本建立具名维护材料，"
            "--force-restore-target 不能绕过目标与恢复门。"
        )
    meta = _metadata(engine)
    if meta["unique_index_present"] and not drop_index:
        raise SystemExit(
            "REFUSED：唯一索引 " + UNIQUE_INDEX + " 还在——恢复会插回与保留行同 key 的行，"
            "必然撞唯一约束。要么先 alembic downgrade 到 0006，要么显式 --drop-index。"
        )
    if meta["unique_index_present"] and drop_index:
        with engine.connect() as conn:
            revision = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
        if str(revision) == "0007":
            raise SystemExit("REFUSED：当前版本为 0007，不能只 DROP 索引后恢复。"
                             "请保持停写，先 alembic downgrade 0006，再按原批次恢复两面。")
    cols = _columns(engine, KB_TABLE)
    ref_tables = _existing_ref_tables(engine)
    restored: list[int] = []
    reverted: dict[str, int] = {}
    ledger_files = list(image_path.parent.glob("redirects-*.json"))
    ledger_path = ledger_files[0] if len(ledger_files) == 1 else None
    if (image.get("schema") != "kb-merge-before-image/v1" or not ledger_path
            or image.get("operation_id") is None):
        raise SystemExit("REFUSED：旧恢复材料或未知 schema 只可留档，缺少完整具名维护批次。")
    ledger = _validate_managed_ledger(ledger_path)
    pending = json.loads(Path(ledger["pending"]).read_text(encoding="utf-8"))
    if pending["artifacts_sha256"].get(str(image_path.resolve())) != _sha256(image_path):
        raise SystemExit("REFUSED：before-image 不是原批次绑定的恢复材料。")
    if ledger["target"] != target or ledger["operation_id"] != image["operation_id"]:
        raise SystemExit("REFUSED：SQL before-image 与原批次不符。")
    if set(image["ref_tables"]) - set(ref_tables):
        raise RuntimeError("S6-186 · 原批次引用表缺失，恢复中止，零写入。")
    operation = _prepare_restore_operation(ledger)
    with engine.begin() as conn:
        for item in image["redirects"]:
            for table, ids in item["references"].items():
                if not ids:
                    continue
                stmt = sa.text("SELECT id, kb_id FROM " + table + " WHERE id IN :ids").bindparams(
                    sa.bindparam("ids", expanding=True))
                refs = list(conn.execute(stmt, {"ids": [int(i) for i in ids]}).mappings())
                if len(refs) != len(ids) or any(int(r["kb_id"] or -1) != int(item["to"]) for r in refs):
                    raise RuntimeError("S6-186 · 原引用行缺失或指向漂移，恢复中止，零写入。")
        if meta["unique_index_present"]:
            ddl = "DROP INDEX " + UNIQUE_INDEX
            if engine.dialect.name != "sqlite":
                ddl += " ON " + KB_TABLE
            conn.execute(sa.text(ddl))
        for item in image.get("redirects", []):
            old = int(item["from"])
            kept = int(item["to"])
            for table, ids in (item.get("references") or {}).items():
                if not ids:
                    continue
                stmt = sa.text(
                    "UPDATE " + table + " SET kb_id = :old WHERE kb_id = :kept AND id IN :ids"
                ).bindparams(sa.bindparam("ids", expanding=True))
                result = conn.execute(
                    stmt, {"old": old, "kept": kept, "ids": [int(i) for i in ids]}
                )
                if int(result.rowcount or 0) != len(ids):
                    raise RuntimeError("S6-186 · 引用恢复行数不符，事务回滚，不发恢复凭证。")
                reverted[table] = reverted.get(table, 0) + int(result.rowcount or 0)
        for group in image.get("groups", []):
            merged = {int(i) for i in group.get("merged_ids", [])}
            for row in group.get("rows", []):
                if int(row["id"]) not in merged:
                    continue
                payload: dict[str, Any] = {"id": int(row["id"])}
                for name, value in row.items():
                    if name == "id" or name not in cols:
                        continue
                    if name == KEY_COLUMN:
                        payload[name] = kb_path_key(row.get("path_or_url"))
                    else:
                        payload[name] = _coerce(value, cols[name]["type"])
                if KEY_COLUMN in cols and KEY_COLUMN not in payload:
                    payload[KEY_COLUMN] = kb_path_key(row.get("path_or_url"))
                names = list(payload)
                conn.execute(
                    sa.text(
                        "INSERT INTO " + KB_TABLE + " (" + ", ".join(names) + ") VALUES ("
                        + ", ".join(":" + n for n in names) + ")"
                    ),
                    payload,
                )
                restored.append(int(row["id"]))
        if meta["key_column_present"]:
            _write_keys(conn, _kb_rows(conn))
    result: dict[str, Any] = {"restored_ids": restored, "reverted_refs": reverted}
    if operation:
        receipt = {
            "schema": "kb-sql-receipt/v1", "target": target, "operation_id": operation,
            "outcome": "restored", "journal_sha256": _sha256(ledger_path),
            "before_image_sha256": _sha256(image_path),
        }
        receipt_path = image_path.parent / ("sql-restored-" + operation + ".json")
        _write_json_once(receipt_path, receipt)
        _complete_operation(ledger, operation, "restored")
    if meta["unique_index_present"] and drop_index:
        result["reopen_hint"] = (
            "唯一索引已移除；请保持停写，完成全部向量恢复。"
            "恢复重复行后 0007 仍会拒绝升级，须先重新完成去重与同步再 upgrade head。"
        )
    return result


def _vector_guard(flag: str, url: str | None, path: str | None) -> None:
    """向量侧三个模式共用的守卫：**必须显式指定目标**，不许落到应用单例。"""
    if bool(url) == bool(path):
        raise SystemExit(
            "REFUSED：" + flag + " 必须显式且只给 --qdrant-url 或 --qdrant-path 一个目标。"
            "不给就会落到应用单例（= 当前安装态的向量库），那正是本阶段不许自动碰的东西。"
        )


def _write_vector_snapshot(
    out_dir: Path, stamp: str, collection: str, entries: list[dict[str, Any]],
    *, binding: dict[str, Any] | None = None,
) -> Path:
    """把「会动哪些点」落盘成可还原账本（schema ``kb-vector-points/v1``）。

    这一份文件是向量侧**唯一的还原依据**：它记了每组重定向前后的 kb_id 与**逐点 id**。
    没有它，payload 一改就再也说不清哪些点原本属于旧 kb（SQL 的 before-image 只覆盖表行）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": VECTOR_SNAPSHOT_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "collection": collection,
        "entries": entries,
        "points_total": sum(int(e["points_total"]) for e in entries),
        **(binding or {}),
    }
    target = out_dir / (VECTOR_SNAPSHOT_PREFIX + stamp + ".json")
    _write_json_once(target, payload)
    _write_json_once(target.with_suffix(".seal.json"), {"sha256": _sha256(target)})
    return target


def _load_vector_snapshot(
    snapshot_path: Path, url: str | None, path: str | None, collection: str,
) -> dict[str, Any]:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if (snapshot.get("schema") != VECTOR_SNAPSHOT_SCHEMA
            or snapshot.get("collection") != collection
            or snapshot.get("vector_target") != kb_vector_target_identity(url, path)):
        raise SystemExit("REFUSED：向量快照 schema / collection / 目标不符。")
    entries = snapshot.get("entries") or []
    if not entries:
        raise SystemExit("REFUSED：向量快照里没有 entries 条目：" + str(snapshot_path))
    journal = Path(snapshot["journal"])
    if _sha256(journal) != snapshot["journal_sha256"]:
        raise SystemExit("REFUSED：原批次重定向账本已变化。")
    seal = json.loads(snapshot_path.with_suffix(".seal.json").read_text(encoding="utf-8"))
    if seal.get("sha256") != _sha256(snapshot_path):
        raise SystemExit("REFUSED：向量快照已变化。")
    ledger = _validate_managed_ledger(journal)
    if (snapshot.get("target") != ledger.get("target", {})
            or snapshot.get("operation_id") not in (
                ledger.get("operation_id", snapshot["journal_sha256"]),
                ledger.get("operation_id", "") + "-restore",
            )):
        raise SystemExit("REFUSED：向量快照不属于原 SQL 批次。")
    return snapshot


def run_repair_vector(
    journal_path: Path,
    url: str | None,
    path: str | None,
    collection: str,
    *,
    out_dir: Path | None = None,
    stamp: str | None = None,
) -> dict[str, Any]:
    """按 SQL 侧账本把向量 payload 的 ``kb_id`` 从旧 id 改到保留 id。

    **顺序不许换**：① 先按 ``filters={"kb_id": old}`` 列出会动的点 id 并落盘快照
    （``vector-points-<ts>.json``）；② 再 ``set_payload`` 改写。
    先改后记 = 事后说不清动了哪些点 = 不可还原。
    """
    from app.vector import get_vector_store

    _vector_guard("--repair-vector", url, path)
    ledger = _validate_managed_ledger(journal_path, sql_applied=True)
    redirects = ledger.get("redirects") or []
    if not redirects:
        raise SystemExit("REFUSED：账本里没有 redirects 条目：" + str(journal_path))
    required = ledger.get("required_collections")
    if required is not None and collection not in required:
        raise SystemExit("REFUSED：collection 不在本批必验集合中。")
    journal_hash = _sha256(journal_path)
    snapshot_dir = Path(ledger["operation_dir"]) if ledger.get("operation_dir") else (
        out_dir if out_dir is not None else journal_path.parent
    )
    snapshot_name = journal_hash + "-" + hashlib.sha256(collection.encode()).hexdigest()[:16]
    existing = snapshot_dir / (VECTOR_SNAPSHOT_PREFIX + snapshot_name + ".json")
    store = get_vector_store(path=path or "", url=url, collection=collection)
    total = store.count()
    try:
        if existing.exists():
            entries = _load_vector_snapshot(existing, url, path, collection)["entries"]
            snapshot_path = existing
        else:
            entries = []
            for item in redirects:
                old, kept = int(item["from"]), int(item["to"])
                point_ids = [str(p) for p in store.list_ids(filters={"kb_id": old})] if old != kept else []
                entries.append({"from": old, "to": kept, "point_ids": point_ids,
                                "points_total": len(point_ids)})
            snapshot_path = _write_vector_snapshot(
                snapshot_dir, snapshot_name, collection, entries,
                binding={"target": ledger.get("target", {}),
                         "operation_id": ledger.get("operation_id", journal_hash),
                         "vector_target": kb_vector_target_identity(url, path),
                         "journal": str(journal_path.resolve()), "journal_sha256": journal_hash},
            )
        results = []
        for entry in entries:
            old, kept = int(entry["from"]), int(entry["to"])
            if old == kept:
                continue
            hits = {h.id: h.payload for h in store.get_by_ids(entry["point_ids"])}
            ids = [pid for pid in entry["point_ids"] if pid in hits and hits[pid].get("kb_id") == old]
            affected = store.set_payload({"kb_id": kept}, ids=ids) if ids else 0
            pending = len(store.list_ids(filters={"kb_id": old}))
            results.append({"from": old, "to": kept, "affected": affected, "pending_after": pending})
        checked = _verify_entries(store, entries)
        return {"collection": collection, "points_total": total, "redirects": results,
                "all_synced": checked["ok"], "snapshot": str(snapshot_path), "records": entries}
    finally:
        if hasattr(store, "close"):
            store.close()


def _verify_entries(store, entries: list[dict[str, Any]]) -> dict[str, Any]:
    details, problems = [], []
    for entry in entries:
        old, kept = int(entry["from"]), int(entry["to"])
        ids = [str(p) for p in entry["point_ids"]]
        leftover = store.list_ids(filters={"kb_id": old}) if old != kept else []
        hits = {h.id: dict(h.payload or {}) for h in store.get_by_ids(ids)} if ids else {}
        missing = [pid for pid in ids if pid not in hits]
        wrong = [pid for pid in ids if pid in hits and hits[pid].get("kb_id") != kept]
        if leftover:
            problems.append("kb_id=" + str(old) + " 的过滤召回仍有点（漏召回面）")
        if missing:
            problems.append("快照点丢失：" + repr(missing[:5]))
        if wrong:
            problems.append("快照点 kb_id 未指到保留行：" + repr(wrong[:5]))
        details.append({"from": old, "to": kept, "expected_points": len(ids),
                        "leftover_old": len(leftover), "checked": len(hits),
                        "missing": len(missing), "wrong": len(wrong)})
    return {"entries": details, "problems": problems, "ok": not problems}


def run_verify_synced(
    snapshot_path: Path,
    url: str | None,
    path: str | None,
    collection: str,
) -> dict[str, Any]:
    """向量数据只读并写核验材料：旧 id 过滤召回为 0，快照所有点指到保留行。"""
    from app.vector import get_vector_store

    _vector_guard("--verify-synced", url, path)
    snapshot = _load_vector_snapshot(snapshot_path, url, path, collection)
    store = get_vector_store(path=path or "", url=url, collection=collection)
    try:
        result = {"collection": collection, **_verify_entries(store, snapshot["entries"])}
        if result["ok"]:
            _save_vector_receipt(snapshot_path, snapshot, result, "verified")
        return result
    finally:
        if hasattr(store, "close"):
            store.close()


def run_restore_vector(
    snapshot_path: Path,
    url: str | None,
    path: str | None,
    collection: str,
) -> dict[str, Any]:
    """按快照把那些点的 ``kb_id`` 改回旧值（只动 kb_id 这一个键，不删点不新增点）。"""
    from app.vector import get_vector_store

    _vector_guard("--restore-vector", url, path)
    snapshot = _load_vector_snapshot(snapshot_path, url, path, collection)
    snapshot_path, snapshot = _prepare_restore_snapshot(snapshot_path, snapshot)
    entries = snapshot["entries"]
    store = get_vector_store(path=path or "", url=url, collection=collection)
    details: list[dict[str, Any]] = []
    total = 0
    problems: list[str] = []
    try:
        for entry in entries:
            old = int(entry["from"])
            kept = int(entry["to"])
            ids = [str(p) for p in (entry.get("point_ids") or [])]
            if not ids or old == kept:
                details.append({"from": old, "to": kept, "restored": 0, "checked": 0, "reverted": 0})
                continue
            written = int(store.set_payload({"kb_id": old}, ids=ids))
            total += written
            hits = {h.id: dict(h.payload or {}) for h in store.get_by_ids(ids)}
            reverted = [pid for pid in ids if pid in hits and int(hits[pid].get("kb_id") or -1) == old]
            not_back = [pid for pid in ids if pid not in hits or pid not in reverted]
            if not_back:
                problems.append(
                    "点没回到 kb_id=" + str(old) + "：" + repr(not_back[:5])
                    + "（共 " + str(len(not_back)) + " 个）"
                )
            details.append(
                {"from": old, "to": kept, "restored": written, "checked": len(hits), "reverted": len(reverted)}
            )
    finally:
        if hasattr(store, "close"):
            store.close()
    result = {
        "collection": collection,
        "restored_points": total,
        "entries": details,
        "problems": problems,
        "ok": not problems,
    }
    if result["ok"]:
        _save_vector_receipt(snapshot_path, snapshot, result, "restored")
    return result


def _prepare_restore_operation(ledger: dict[str, Any]) -> str:
    original = ledger["operation_id"]
    pending = Path(ledger["pending"])
    complete = pending.with_name(pending.name.replace(".pending.json", ".complete.json"))
    if not complete.exists():
        return original
    if json.loads(complete.read_text(encoding="utf-8")).get("outcome") != "verified":
        raise SystemExit("REFUSED：本批已经恢复，不能再次重放恢复材料。")
    operation = original + "-restore"
    blocked = kb_pending_path(ledger["target"], operation)
    if not blocked.exists():
        original_pending = json.loads(pending.read_text(encoding="utf-8"))
        original_pending["operation_id"] = operation
        original_pending["parent_operation_id"] = original
        _write_json_once(blocked, original_pending)
    elif blocked.with_name(blocked.name.replace(".pending.json", ".complete.json")).exists():
        raise SystemExit("REFUSED：本批已经恢复，不能再次重放恢复材料。")
    return operation


def _prepare_restore_snapshot(
    path: Path, snapshot: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    ledger = json.loads(Path(snapshot["journal"]).read_text(encoding="utf-8"))
    if not ledger.get("pending"):
        return path, snapshot
    operation = _prepare_restore_operation(ledger)
    if operation == snapshot["operation_id"]:
        return path, snapshot
    restored = dict(snapshot, operation_id=operation)
    restore_path = path.with_name(path.stem + "-restore.json")
    if restore_path.exists():
        if json.loads(restore_path.read_text(encoding="utf-8")) != restored:
            raise SystemExit("REFUSED：既有恢复快照与原批次不符。")
    else:
        _write_json_once(restore_path, restored)
        _write_json_once(restore_path.with_suffix(".seal.json"), {"sha256": _sha256(restore_path)})
    return restore_path, restored


def _save_vector_receipt(
    snapshot_path: Path, snapshot: dict[str, Any], result: dict[str, Any], outcome: str,
) -> None:
    ledger = json.loads(Path(snapshot["journal"]).read_text(encoding="utf-8"))
    if not ledger.get("pending"):
        return
    operation = snapshot["operation_id"]
    sql_path = snapshot_path.parent / (
        "sql-applied.json" if outcome == "verified" else "sql-restored-" + operation + ".json"
    )
    if outcome == "verified":
        if not sql_path.exists():
            raise SystemExit("REFUSED：SQL 尚无已提交凭证，不可完成向量同步。")
        sql = json.loads(sql_path.read_text(encoding="utf-8"))
        if (sql.get("schema") != "kb-sql-receipt/v1" or sql.get("outcome") != "applied"
                or sql.get("target") != ledger["target"]
                or sql.get("operation_id") != operation
                or sql.get("journal_sha256") != snapshot["journal_sha256"]):
            raise SystemExit("REFUSED：SQL 提交凭证不属于当前批次。")
    entries = result["entries"]
    if outcome == "verified":
        checks = {
            "expected_points": sum(e["expected_points"] for e in entries),
            "checked_points": sum(e["checked"] for e in entries),
            "leftover_old": sum(e["leftover_old"] for e in entries),
            "missing": sum(e["missing"] for e in entries),
            "wrong": sum(e["wrong"] for e in entries),
        }
    else:
        checks = {
            "expected_points": snapshot["points_total"],
            "checked_points": sum(e["checked"] for e in entries),
            "missing": 0, "wrong": 0,
        }
    receipt = {
        "schema": "kb-vector-receipt/v1", "target": snapshot["target"],
        "operation_id": operation, "collection": snapshot["collection"],
        "vector_target": snapshot["vector_target"], "outcome": outcome, "ok": True,
        "snapshot": str(snapshot_path.resolve()), "snapshot_sha256": _sha256(snapshot_path),
        "journal_sha256": snapshot["journal_sha256"], "checks": checks,
    }
    suffix = hashlib.sha256(snapshot["collection"].encode()).hexdigest()[:16]
    receipt_path = snapshot_path.parent / ("vector-" + outcome + "-" + operation + "-" + suffix + ".json")
    if receipt_path.exists():
        if json.loads(receipt_path.read_text(encoding="utf-8")) != receipt:
            raise SystemExit("REFUSED：既有核验凭证不符，不许覆盖。")
    else:
        _write_json_once(receipt_path, receipt)
    _complete_operation(ledger, operation, outcome)


def _complete_operation(ledger: dict[str, Any], operation: str, outcome: str) -> None:
    pending_path = kb_pending_path(ledger["target"], operation)
    if not pending_path.exists():
        raise SystemExit("REFUSED：本批维护阻断凭证不存在。")
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    directory = Path(ledger["operation_dir"])
    sql_path = directory / ("sql-applied.json" if outcome == "verified"
                            else "sql-restored-" + operation + ".json")
    if not sql_path.exists():
        return
    sql = json.loads(sql_path.read_text(encoding="utf-8"))
    if (sql.get("schema") != "kb-sql-receipt/v1"
            or sql.get("target") != ledger["target"] or sql.get("operation_id") != operation
            or sql.get("outcome") != ("applied" if outcome == "verified" else "restored")
            or sql.get("journal_sha256") not in pending["artifacts_sha256"].values()):
        raise SystemExit("REFUSED：SQL 提交/恢复凭证不属于当前维护批次。")
    receipts = {}
    vector_target = None
    for collection in pending["required_collections"]:
        suffix = hashlib.sha256(collection.encode()).hexdigest()[:16]
        path = directory / ("vector-" + outcome + "-" + operation + "-" + suffix + ".json")
        if not path.exists():
            return
        receipt = json.loads(path.read_text(encoding="utf-8"))
        checks = receipt.get("checks", {})
        if (receipt.get("schema") != "kb-vector-receipt/v1" or receipt.get("ok") is not True
                or receipt.get("outcome") != outcome or receipt.get("target") != ledger["target"]
                or receipt.get("operation_id") != operation or receipt.get("collection") != collection
                or receipt.get("journal_sha256") != sql["journal_sha256"]
                or checks.get("missing") != 0 or checks.get("wrong") != 0
                or type(checks.get("expected_points")) is not int or checks["expected_points"] < 0
                or checks.get("checked_points") != checks["expected_points"]
                or (outcome == "verified" and checks.get("leftover_old") != 0)
                or _sha256(Path(receipt["snapshot"])) != receipt["snapshot_sha256"]):
            raise SystemExit("REFUSED：向量核验凭证不完整或不属于当前批次。")
        if vector_target is None:
            vector_target = receipt["vector_target"]
        elif receipt["vector_target"] != vector_target:
            raise SystemExit("REFUSED：各集合核验目标不同，不可完成本批。")
        receipts[collection] = _receipt_ref(path)
    complete_path = pending_path.with_name(pending_path.name.replace(".pending.json", ".complete.json"))
    complete = {
        "schema": "kb-maintenance-complete/v1", "target": ledger["target"],
        "operation_id": operation, "pending_sha256": _sha256(pending_path),
        "outcome": outcome, "required_collections": pending["required_collections"],
        "sql_receipt": _receipt_ref(sql_path), "receipts": receipts,
    }
    if complete_path.exists():
        if json.loads(complete_path.read_text(encoding="utf-8")) != complete:
            raise SystemExit("REFUSED：已完成凭证不符，不许覆盖。")
        return
    _write_json_once(complete_path, complete)


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    return SQ + text.replace(SQ, SQ + SQ) + SQ


def _restore_sql(image: dict[str, Any]) -> str:
    lines = [
        "-- S6-186 恢复材料（人读；真恢复请用 kb_merge_duplicates.py --restore <before-image>）",
        "-- 生成时间：" + str(image.get("created_at")),
        "-- 目标：" + repr(image.get("target")),
        "-- ⚠️ 先确保唯一索引不在（alembic downgrade 0006），否则插回同 key 行会撞约束。",
    ]
    for item in image.get("redirects", []):
        old = int(item["from"])
        kept = int(item["to"])
        lines.append("-- 引用从 " + str(kept) + " 改回 " + str(old))
        for table, ids in (item.get("references") or {}).items():
            if not ids:
                continue
            lines.append(
                "UPDATE " + table + " SET kb_id = " + str(old) + " WHERE kb_id = " + str(kept)
                + " AND id IN (" + ", ".join(str(int(i)) for i in ids) + ");"
            )
    for group in image.get("groups", []):
        merged = {int(i) for i in group.get("merged_ids", [])}
        for row in group.get("rows", []):
            if int(row["id"]) not in merged:
                continue
            cols = [f for f in KB_FIELDS if f in row and f != KEY_COLUMN]
            lines.append(
                "INSERT INTO " + KB_TABLE + " (" + ", ".join(cols) + ") VALUES ("
                + ", ".join(_sql_literal(row.get(c)) for c in cols)
                + ");"
            )
    return chr(10).join(lines) + chr(10)


def _report_md(report: dict[str, Any], mode: str) -> str:
    lines = ["# knowledge_bases 唯一身份 · " + mode + " 报告", ""]
    lines.append("| 读数 | 值 |")
    lines.append("|---|---|")
    for key in (
        "dialect",
        "database",
        "kb_rows",
        "key_column_present",
        "unique_index_present",
        "merged_ids",
        "ref_totals",
        "moved_refs",
        "keys_written",
    ):
        if key in report:
            lines.append("| " + key + " | " + repr(report[key]) + " |")
    lines.append("")
    lines.append("空 path 行（不在唯一域，天然多行合法）：" + repr(report.get("null_key_ids", [])))
    lines.append("")
    lines.append("## 重复组（" + str(len(report.get("groups", []))) + "）")
    for group in report.get("groups", []):
        lines.append("")
        flag = "可合并" if group["compatible"] else "不兼容"
        lines.append("### key " + group["key"][:16] + "… （" + flag + "）")
        lines.append(
            "- ids：" + repr(group["ids"]) + "；保留 id=" + str(group["kept_id"])
            + "；并入：" + repr(group["merged_ids"])
        )
        if group["reasons"]:
            lines.append("- 不兼容原因：" + "；".join(group["reasons"]))
        if group["differences"]:
            lines.append("- 组内字段差异：" + repr(group["differences"]))
        for row in group["rows"]:
            lines.append("  - 行 " + str(row["id"]) + "：" + repr(row))
        lines.append("- 引用行数（改前）：" + repr(group["ref_counts"]))
    return chr(10).join(lines) + chr(10)


def _latest(out_dir: Path, pattern: str) -> Path:
    found = sorted(out_dir.glob(pattern))
    if not found:
        raise SystemExit("找不到 " + pattern + "（目录 " + str(out_dir) + "）")
    return found[-1]


def _latest_ledger(out_dir: Path) -> Path:
    """One unambiguous operation may be selected; multiple ledgers require a name."""
    found = sorted(set(out_dir.rglob(MIGRATION_LEDGER_PREFIX + "*.json"))
                   | set(out_dir.rglob("redirects-*.json")))
    if len(found) != 1:
        raise SystemExit("REFUSED：重定向账本不唯一或不存在，请显式 --journal <原批次账本>："
                         + str(out_dir))
    return found[0]


def _write_materials(out_dir: Path, bundle: dict[str, Any]) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = bundle["stamp"]
    operation_dir = out_dir.resolve() / stamp
    operation_dir.mkdir(exist_ok=False)
    bundle["operation_dir"] = str(operation_dir)
    report = bundle["report"]
    ref_ids = report.get("ref_ids", {})
    ref_tables = report.get("ref_tables", [])
    image = {
        "schema": "kb-merge-before-image/v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target": {k: report[k] for k in ("dialect", "host", "port", "database") if k in report},
        "operation_id": stamp,
        "operation_dir": str(operation_dir),
        "kb_fields": list(KB_FIELDS),
        "ref_tables": ref_tables,
        "groups": [
            {
                "key": g["key"],
                "kept_id": g["kept_id"],
                "merged_ids": g["merged_ids"],
                "rows": g["rows"],
            }
            for g in report.get("groups", [])
        ],
        "redirects": bundle["redirects"],
        "ref_counts_before": {
            str(i): {t: len(ref_ids.get(t, {}).get(str(i), [])) for t in ref_tables}
            for i in report.get("merged_ids", [])
        },
    }
    image_path = operation_dir / ("before-image-" + stamp + ".json")
    _write_json_once(image_path, image)
    from app.core.config import settings

    global_collection = str(settings.QDRANT_COLLECTION or "").strip()
    if not global_collection:
        raise RuntimeError("S6-186 · 生产默认向量集合为空，无法确定完整维护面。")
    metadata_collections = sorted({
        str(row.get("collection") or "").strip()
        for group in report.get("groups", []) for row in group["rows"]
        if str(row.get("collection") or "").strip()
    })
    collections = sorted(set(metadata_collections) | {global_collection})
    ledger = {
        "schema": "kb-merge-redirects/v2",
        "source": "kb_merge_duplicates.py --apply " + stamp,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "operation_id": stamp,
        "operation_dir": str(operation_dir),
        "target": image["target"],
        "pending": str(kb_pending_path(image["target"], stamp)),
        "required_collections": collections,
        "collection_sources": {
            collection: [name for name, present in (
                ("metadata", collection in metadata_collections),
                ("global_default", collection == global_collection),
            ) if present]
            for collection in collections
        },
        "redirects": [
            {"from": r["from"], "to": r["to"], "collection": r.get("collection", "")}
            for r in bundle["redirects"]
        ],
    }
    ledger_path = operation_dir / ("redirects-" + stamp + ".json")
    _write_json_once(ledger_path, ledger)
    bundle["journal_path"] = str(ledger_path)
    bundle["image_path"] = str(image_path)
    sql_path = operation_dir / ("restore-" + stamp + ".sql")
    with sql_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(_restore_sql(image))
    return [image_path, ledger_path, sql_path]


def _begin_pending(engine: Engine, bundle: dict[str, Any], files: list[Path]) -> None:
    ensure_kb_maintenance_complete(engine.url)
    ledger = json.loads(Path(bundle["journal_path"]).read_text(encoding="utf-8"))
    pending = Path(ledger["pending"])
    pending.parent.mkdir(parents=True, exist_ok=True)
    _write_json_once(pending, {
        "schema": "kb-maintenance/v1",
        "target": ledger["target"],
        "operation_id": ledger["operation_id"],
        "operation_dir": ledger["operation_dir"],
        "required_collections": ledger["required_collections"],
        "artifacts_sha256": {str(p.resolve()): _sha256(p) for p in files},
    })


def _sql_receipt(bundle: dict[str, Any], outcome: str) -> Path:
    ledger_path = Path(bundle["journal_path"])
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    path = ledger_path.parent / ("sql-" + outcome + ".json")
    receipt = {
        "schema": "kb-sql-receipt/v1", "target": ledger["target"],
        "operation_id": ledger["operation_id"], "outcome": outcome,
        "journal_sha256": _sha256(ledger_path),
    }
    if outcome == "restored":
        receipt["before_image_sha256"] = _sha256(Path(bundle["image_path"]))
    _write_json_once(path, receipt)
    return path


def _abort_operation(engine: Engine, bundle: dict[str, Any]) -> bool:
    """Only a known body RuntimeError with a confirmed rollback can close as zero-write."""
    if bundle.get("rollback_confirmed") is not True:
        return False
    journal = Path(bundle["journal_path"])
    ledger = _validate_managed_ledger(journal)
    directory = Path(ledger["operation_dir"])
    if any(p.name == "sql-applied.json" or p.name.startswith(("sql-restored-", "vector-"))
           for p in directory.iterdir()):
        return False
    with engine.connect() as conn:
        after = _sql_state(conn)
    before = bundle.get("abort_before")
    if not before or before != after:
        return False
    state_path = directory / "sql-abort-state.json"
    _write_json_once(state_path, {
        "schema": "kb-sql-abort-state/v1", "target": ledger["target"],
        "operation_id": ledger["operation_id"], "before": before, "after": after,
    })
    sql_path = directory / "sql-aborted.json"
    _write_json_once(sql_path, {
        "schema": "kb-sql-receipt/v1", "target": ledger["target"],
        "operation_id": ledger["operation_id"], "outcome": "aborted",
        "journal_sha256": _sha256(journal), "rollback_confirmed": True, "no_writes": True,
        "abort_state": _receipt_ref(state_path),
    })
    pending_path = Path(ledger["pending"])
    _write_json_once(pending_path.with_name(pending_path.name.replace(".pending.json", ".complete.json")), {
        "schema": "kb-maintenance-complete/v1", "target": ledger["target"],
        "operation_id": ledger["operation_id"], "pending_sha256": _sha256(pending_path),
        "outcome": "aborted", "required_collections": ledger["required_collections"],
        "sql_receipt": _receipt_ref(sql_path), "receipts": {},
    })
    ensure_kb_maintenance_complete(engine.url)
    return True


def _write_report(out_dir: Path, report: dict[str, Any], mode: str, stamp: str) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / (mode + "-report-" + stamp + ".json")
    _write_json_once(json_path, report)
    md_path = out_dir / (mode + "-report-" + stamp + ".md")
    with md_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(_report_md(report, mode))
    return [json_path, md_path]


def _fmt_preflight(report: dict[str, Any]) -> str:
    head = (
        "PREFLIGHT " + str(report.get("dialect")) + "/" + str(report.get("database"))
        + " | kb_rows=" + str(report["kb_rows"])
        + " | 重复组=" + str(len(report["groups"]))
        + " | 不兼容=" + str(len(report["incompatible"]))
        + " | key 列=" + str(report["key_column_present"])
        + " | 唯一索引=" + str(report["unique_index_present"])
    )
    body = [
        "  · key=" + g["key"][:12] + " ids=" + repr(g["ids"]) + " 保留=" + str(g["kept_id"])
        + (" 兼容" if g["compatible"] else " 不兼容 " + "；".join(g["reasons"]))
        for g in report["groups"]
    ]
    tail = (
        "  引用行数（改前）=" + repr(report["ref_totals"])
        + "  空 path 行=" + repr(report["null_key_ids"])
    )
    verdict = "PREFLIGHT_OK" if not report["incompatible"] else "PREFLIGHT_BLOCKED"
    return chr(10).join([head] + body + [tail, verdict])


def _fmt_apply(report: dict[str, Any], files: list[Path]) -> str:
    return chr(10).join([
        "APPLY " + str(report.get("dialect")) + "/" + str(report.get("database"))
        + " | 合并组=" + str(len(report["groups"]))
        + " | 删除行=" + str(len(report["merged_ids"]))
        + " | 引用改向=" + repr(report.get("moved_refs"))
        + " | 回填 key=" + str(report.get("keys_written")),
        "  材料：" + "，".join(str(p) for p in files),
        "APPLY_OK" + (" · MAINTENANCE_PENDING：全部集合 repair/verify 完成后才允许启动"
                      if report.get("maintenance_pending") else ""),
    ])


def _fmt_restore(result: dict[str, Any]) -> str:
    lines = [
        "RESTORE 插回行=" + repr(result["restored_ids"]) + " 引用改回=" + repr(result["reverted_refs"]),
    ]
    if result.get("reopen_hint"):
        lines.append("  REOPEN: " + str(result["reopen_hint"]))
    lines.append("RESTORE_OK")
    return chr(10).join(lines)


def _fmt_vector(result: dict[str, Any]) -> str:
    body = [
        "  · " + str(r["from"]) + " -> " + str(r["to"]) + " 改写=" + str(r["affected"])
        + " 残留=" + str(r["pending_after"])
        for r in result["redirects"]
    ]
    return chr(10).join(
        ["REPAIR-VECTOR 集合=" + result["collection"] + " 总点数=" + str(result["points_total"])]
        + body
        + ["VECTOR_SYNCED" if result["all_synced"] else "VECTOR_PENDING"]
    )


def _fmt_verify(result: dict[str, Any]) -> str:
    body = [
        "  · " + str(d["from"]) + " -> " + str(d["to"]) + " 应有点=" + str(d["expected_points"])
        + " 旧id残留=" + str(d["leftover_old"]) + " 已核对=" + str(d["checked"])
        for d in result["entries"]
    ]
    return chr(10).join(
        ["VECTOR-VERIFY 集合=" + str(result["collection"])]
        + body
        + ["  PROBLEM: " + p for p in result["problems"]]
        + ["VECTOR_SYNCED" if result["ok"] else "VECTOR_PENDING"]
    )


def _fmt_restore_vector(result: dict[str, Any]) -> str:
    body = [
        "  · " + str(d["from"]) + " -> " + str(d["to"]) + " 改回=" + str(d["restored"])
        + " 已核对=" + str(d["checked"]) + " 回到旧id=" + str(d["reverted"])
        for d in result["entries"]
    ]
    return chr(10).join(
        ["RESTORE-VECTOR 集合=" + str(result["collection"])]
        + body
        + ["  PROBLEM: " + p for p in result["problems"]]
        + ["VECTOR_RESTORED" if result["ok"] else "VECTOR_RESTORE_INCOMPLETE"]
    )


def _apply_error(exc: Exception, as_json: bool) -> int:
    if as_json:
        print(
            json.dumps(
                {"mode": "apply", "error": "incompatible_groups", "message": str(exc)},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(str(exc))
    return EXIT_PRECONDITION


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="knowledge_bases 唯一身份：预检/合并/恢复/向量修复")
    parser.add_argument("--dsn", default="", help="目标库 SQLAlchemy 同步 DSN（向量三模式除外）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true", help="只读预检（默认）")
    mode.add_argument("--apply", action="store_true", help="真合并（不兼容则整体中止）")
    mode.add_argument("--restore", metavar="BEFORE_IMAGE", default="", help="按 before-image 恢复")
    mode.add_argument("--repair-vector", action="store_true", help="按账本修向量 payload.kb_id")
    mode.add_argument("--verify-synced", action="store_true", help="向量数据只读并写核验材料（维护放行条件）")
    mode.add_argument("--restore-vector", action="store_true", help="按向量快照把 kb_id 改回旧值")
    parser.add_argument(
        "--out", default=str(default_out_dir()), help="材料目录（默认与应用维护门同源）"
    )
    parser.add_argument("--journal", default="", help="账本路径（redirects / vector-points）")
    parser.add_argument("--qdrant-url", default="", help="向量库 URL（向量三模式）")
    parser.add_argument("--qdrant-path", default="", help="向量库本地目录（向量三模式）")
    parser.add_argument("--collection", default="zhilan_chunks", help="向量集合名")
    parser.add_argument("--drop-index", action="store_true", help="恢复前先 DROP 唯一索引")
    parser.add_argument(
        "--force-restore-target", action="store_true",
        help="兼容旧参数；不再豁免目标绑定，副本须从本目标具名维护开始"
    )
    parser.add_argument("--i-know-this-is-the-install-db", action="store_true", help="跳过开发库守卫")
    parser.add_argument("--json", action="store_true", help="只打印 JSON 结果")
    args = parser.parse_args(argv)
    out_dir = Path(args.out)

    if args.repair_vector:
        journal = Path(args.journal) if args.journal else _latest_ledger(out_dir)
        result = run_repair_vector(
            journal,
            args.qdrant_url or None,
            args.qdrant_path or None,
            args.collection,
            out_dir=out_dir,
        )
        payload = json.dumps(result, ensure_ascii=False, indent=2, default=str)
        print(payload if args.json else _fmt_vector(result))
        return EXIT_OK if result["all_synced"] else EXIT_FAIL

    if args.verify_synced or args.restore_vector:
        if not args.journal:
            raise SystemExit(
                "REFUSED：--verify-synced / --restore-vector 必须给 --journal <vector-points-*.json>"
                "（--repair-vector 落盘的那份快照）"
            )
        snapshot = Path(args.journal)
        if args.verify_synced:
            checked = run_verify_synced(
                snapshot, args.qdrant_url or None, args.qdrant_path or None, args.collection
            )
            print(
                json.dumps(checked, ensure_ascii=False, indent=2, default=str)
                if args.json
                else _fmt_verify(checked)
            )
            return EXIT_OK if checked["ok"] else EXIT_FAIL
        reverted = run_restore_vector(
            snapshot, args.qdrant_url or None, args.qdrant_path or None, args.collection
        )
        print(
            json.dumps(reverted, ensure_ascii=False, indent=2, default=str)
            if args.json
            else _fmt_restore_vector(reverted)
        )
        return EXIT_OK if reverted["ok"] else EXIT_FAIL

    if not args.dsn:
        raise SystemExit("REFUSED：--preflight / --apply / --restore 必须给 --dsn")
    guard_target(args.dsn, args.i_know_this_is_the_install_db)
    engine = sa.create_engine(args.dsn, future=True)

    if args.restore:
        result = run_restore(
            engine, Path(args.restore), args.drop_index, force_target=args.force_restore_target
        )
        payload = json.dumps(result, ensure_ascii=False, indent=2, default=str)
        print(payload if args.json else _fmt_restore(result))
        return EXIT_OK

    if args.apply:
        # 顺序：先落恢复材料，再动数据（材料写失败绝不会留下「已合并但无材料」的库）。
        ensure_kb_maintenance_complete(engine.url)
        try:
            bundle = plan_merge(engine)
        except RuntimeError as exc:
            return _apply_error(exc, args.json)
        bundle["report"]["mode"] = "apply"
        bundle["report"].update(_target_label(engine))
        if not bundle["redirects"]:
            report = bundle["report"]
            report["materials"] = []
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str)
                  if args.json else _fmt_apply(report, []))
            return EXIT_OK
        files = _write_materials(out_dir, bundle)
        _begin_pending(engine, bundle, files)
        try:
            bundle = apply_merge(engine, bundle)
        except RuntimeError as exc:
            _abort_operation(engine, bundle)
            return _apply_error(exc, args.json)
        bundle["report"]["mode"] = "apply"
        bundle["report"].update(_target_label(engine))
        _sql_receipt(bundle, "applied")
        report = bundle["report"]
        report["materials"] = [str(p) for p in files]
        report["maintenance_pending"] = True
        report["journal"] = bundle["journal_path"]
        report["required_collections"] = json.loads(Path(bundle["journal_path"]).read_text("utf-8"))["required_collections"]
        _write_report(Path(bundle["operation_dir"]), report, "apply", bundle["stamp"])
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        else:
            print(_fmt_apply(report, files))
        return EXIT_OK

    report = run_preflight(engine)
    report["mode"] = "preflight"
    report.update(_target_label(engine))
    stamp = _now_stamp() + "-" + uuid.uuid4().hex
    _write_report(out_dir, report, "preflight", stamp)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(_fmt_preflight(report))
    return EXIT_OK if not report["incompatible"] else EXIT_PRECONDITION


if __name__ == "__main__":
    sys.exit(main())
