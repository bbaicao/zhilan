"""documents 两列 + alembic_version 只读校验（阶段 2 · 条目 2.1 证据工具）。

背景：条目 2.1 的 alembic 证据原先引用了一个不存在的 `_check_cols.py`，
6 处列校验全部是 `No such file or directory` —— 证据链断裂。本脚本是该断点的
正式替代品：真实连库、只读查询、输出可直接贴进验收日志。

查两件事（均 `SELECT`，**零写入**）：
1. `documents` 表是否存在 `file_path` / `index_error` 两列（`information_schema.COLUMNS`）；
2. 当前 `alembic_version.version_num`。

连接信息取 `app.core.config.settings`（默认读仓库根 `.env`；DSN 照 `alembic/env.py`
换同步驱动 `+pymysql`）。

切 scratch 库：`Settings` 是 pydantic BaseSettings，**环境变量优先级高于 `.env`**，
故导出 `MYSQL_DATABASE=<scratch 库名>` 即可，不动生产库：

  cd backend
  # 生产库（只读核对）
  PYTHONPATH=. ./.venv/Scripts/python.exe scripts/check_documents_columns.py
  # scratch 库（迁移可逆性验证）
  MYSQL_DATABASE=zhilan_v2_migcheck PYTHONPATH=. \
      ./.venv/Scripts/python.exe scripts/check_documents_columns.py

可选参数：
  --expect present|absent   断言两列齐备性；不符时退出码 1（缺省只报告事实，退出码 0）
  --label TEXT              在首行打一个阶段标签，便于日志分段（如 `upgrade head 之后`）

退出码：0 = 查询成功（含 --expect 通过）／1 = --expect 不符／2 = 连库失败。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core.config import settings  # noqa: E402

COLUMNS = ("file_path", "index_error")

COLUMNS_SQL = sa.text(
    "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE "
    "FROM information_schema.COLUMNS "
    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'documents' "
    "AND COLUMN_NAME IN ('file_path', 'index_error')"
)

VERSION_EXISTS_SQL = sa.text(
    "SELECT COUNT(*) FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'alembic_version'"
)
VERSION_VALUE_SQL = sa.text("SELECT version_num FROM alembic_version")

TABLE_EXISTS_SQL = sa.text(
    "SELECT COUNT(*) FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'documents'"
)


def _dsn_for_print() -> str:
    """可贴进日志的 DSN（隐去密码）。"""
    url = make_url(settings.mysql_dsn.replace("+aiomysql", "+pymysql"))
    return url.render_as_string(hide_password=True)


def _database_source() -> str:
    import os

    if os.environ.get("MYSQL_DATABASE"):
        return f"环境变量 MYSQL_DATABASE={os.environ['MYSQL_DATABASE']}（覆盖 .env）"
    return f".env 的 MYSQL_DATABASE={settings.MYSQL_DATABASE}"


def main() -> int:
    parser = argparse.ArgumentParser(description="documents 两列 + alembic 版本只读校验")
    parser.add_argument("--expect", choices=("present", "absent"), default=None)
    parser.add_argument("--label", default="")
    args = parser.parse_args()

    print("=== documents 列校验（只读）===")
    if args.label:
        print(f"阶段：{args.label}")
    print(f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"库：{settings.MYSQL_DATABASE}")
    print(f"库名来源：{_database_source()}")
    print(f"DSN：{_dsn_for_print()}")

    engine = sa.create_engine(
        settings.mysql_dsn.replace("+aiomysql", "+pymysql"),
        poolclass=sa.pool.NullPool,
    )
    try:
        with engine.connect() as conn:
            table_exists = bool(conn.execute(TABLE_EXISTS_SQL).scalar())
            if table_exists:
                rows = {
                    name: (col_type, nullable)
                    for name, col_type, nullable in conn.execute(COLUMNS_SQL).all()
                }
            else:
                rows = {}

            has_version_table = bool(conn.execute(VERSION_EXISTS_SQL).scalar())
            if has_version_table:
                values = [r[0] for r in conn.execute(VERSION_VALUE_SQL).all()]
                version = ", ".join(values) if values else "(空表)"
            else:
                version = "(无 alembic_version 表)"
    except Exception as exc:  # noqa: BLE001 —— 连库失败要如实打出来，不吞
        print(f"连接/查询失败：{type(exc).__name__}: {exc}")
        return 2
    finally:
        engine.dispose()

    print(f"documents 表：{'存在' if table_exists else '不存在'}")
    print(f"alembic_version：{version}")
    for name in COLUMNS:
        if name in rows:
            col_type, nullable = rows[name]
            print(f"documents.{name}：存在  {col_type}  NULL={nullable}")
        else:
            print(f"documents.{name}：不存在")

    present = [c for c in COLUMNS if c in rows]
    missing = [c for c in COLUMNS if c not in rows]
    print(f"COLUMNS_PRESENT {present}")
    print(f"COLUMNS_MISSING {missing}")

    if not missing:
        verdict = "两列齐备"
    elif not present:
        verdict = "两列均不存在"
    else:
        verdict = f"列状态不一致（缺 {missing}）"
    print(f"判定：{verdict}")

    if args.expect is None:
        return 0

    ok = (not missing) if args.expect == "present" else (not present)
    print(f"断言（--expect {args.expect}）：{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
