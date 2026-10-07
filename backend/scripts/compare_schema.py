"""v2 vs 老仓库 models 结构对拍（阶段 2 · 2.1.1 验收）。

对比方式：
1. 元数据对比：动态加载老仓库的 `backend/app/models/__init__.py` 与 v2 models，
   逐表比对列名集合（要求 v2 ⊇ 老库）。
2. 可选：information_schema 对比两个 MySQL 库（zhilan / zhilan_v2），需两边都在。

老仓库在**仓库之外**，属可选依赖：按「环境变量 ``OLD_REPO_BACKEND`` → ``.env`` 的同名键
→ 空」解析（仓库里不写死本机绝对路径）；为空时本脚本打印中文提示并以退出码 2 收尾。

用法：
  cd backend
  PYTHONPATH=. ./.venv/Scripts/python.exe scripts/compare_schema.py
  输出写入 stdout；调用方重定向到证据日志。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.core.config import settings  # noqa: E402
from app.models import Base as V2Base  # noqa: E402

# 仓库外部的可选依赖：环境变量优先（跨机临时覆盖），其次 .env（本机常驻配置）。
OLD_REPO_BACKEND = (
    os.environ.get("OLD_REPO_BACKEND") or settings.OLD_REPO_BACKEND or ""
).strip()
OLD_MODELS_PATH = Path(OLD_REPO_BACKEND) / "app" / "models" / "__init__.py"


def load_old_base():
    spec = importlib.util.spec_from_file_location("old_zhilan_models", OLD_MODELS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载老仓库 models：{OLD_MODELS_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Base


def columns_of(base) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for table in base.metadata.tables.values():
        out[table.name] = {c.name for c in table.columns}
    return out


def main() -> int:
    if not OLD_REPO_BACKEND:
        print(
            "未配置老仓库路径（可选依赖）：请设环境变量 OLD_REPO_BACKEND=<老仓库 backend 根目录>，"
            "或在本机未入库的 .env 里配置同名键后重跑。",
            file=sys.stderr,
        )
        return 2

    old_base = load_old_base()
    old_cols = columns_of(old_base)
    v2_cols = columns_of(V2Base)

    old_tables = set(old_cols)
    v2_tables = set(v2_cols)
    missing_tables = sorted(old_tables - v2_tables)
    extra_tables = sorted(v2_tables - old_tables)

    print("=== 知澜 v2 · 结构对拍（models 元数据）===")
    print(f"老仓库表：{len(old_tables)}  v2 表：{len(v2_tables)}")
    print(f"老库有 v2 无：{missing_tables or '（无）'}")
    print(f"v2 多出：{extra_tables or '（无）'}")
    print()

    all_ok = not missing_tables
    for name in sorted(old_tables & v2_tables):
        old_c, v2_c = old_cols[name], v2_cols[name]
        missing = sorted(old_c - v2_c)
        extra = sorted(v2_c - old_c)
        status = "PASS" if not missing else "FAIL"
        if missing:
            all_ok = False
        print(f"[{status}] {name}")
        print(f"  老库列({len(old_c)}): {sorted(old_c)}")
        print(f"  v2 列({len(v2_c)}): {sorted(v2_c)}")
        if missing:
            print(f"  !! v2 缺列: {missing}")
        if extra:
            print(f"  +  v2 多列: {extra}")
        print()

    print("=== 总结 ===")
    print(f"表级：老库表全部在 v2 中 = {'PASS' if not missing_tables else 'FAIL'}")
    print(f"列级：每表 v2 ⊇ 老库 = {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok and not missing_tables else 1


if __name__ == "__main__":
    raise SystemExit(main())
