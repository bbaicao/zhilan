"""条目 6.6 · 冻结态**解析链数据文件**回归钉（回归修复）。

## 这条钉子在守什么

`6.6` 的安装态验收抓到一条真缺陷：`zhilan.spec` 的 `datas` 只收了「资源根三件套」
（`frontend/dist` / `alembic/` / `alembic.ini`），而**交付默认档**（`MINERU_ENABLE=true` +
`MINERU_BACKEND=basic`）下 MinerU 的解析链要读三个包的**数据文件**：

| 包 | 数据 | 缺了会怎样（安装态实测原文） |
|---|---|---|
| `magika` | `models/**`、`config/content_types_kb.min.json` | 图片：`MinerU 图片 OCR 失败：model dir not found at …\\_internal\\magika\\models\\standard_v3_3` |
| `docvortex` | `resources/fonts/**`（`DroidSansFallbackFull.ttf` + `manifest.json`）、`assets/**` | 渲染 / 元数据降级 |
| `mineru` | `resources/**` | 解析链资源缺失 |

PyInstaller 的**模块图只收 `.py`** ⇒ 这些数据一个都不进产物 ⇒ 安装态下扫描件 PDF
**静默回落 pypdf**，报 400「该 PDF 未提取到文字，请上传文字版或 txt/md。」；
而开发态与打包 venv 里一切正常 ⇒ **只在安装态暴露**。

## 判据

1. 静态：spec 必须对这三个包调用 `collect_data_files`，且 `datas` 里真的出现它们的数据文件。
2. 点名：`magika/config/content_types_kb.min.json` 与
   `docvortex/resources/fonts/DroidSansFallbackFull.ttf` 两个**实测缺了就炸**的文件必须在 `datas` 里。

**变异自证（本轮实测）**：删掉 spec 里那段收集循环 ⇒ 本文件变红；还原 ⇒ 全绿。
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SPEC = REPO / "tools" / "packaging" / "zhilan.spec"
PKG_VENV = Path(r"E:\zhilan-pkg-venv\Lib\site-packages")

COLLECT_LINE = "_doc_parse_pkg in ("
#: 三个必须收数据的包（少任何一个，冻结态解析链都会缺资源）。
PACKAGES = ("magika", "docvortex", "mineru")
#: 点名文件（按 basename 匹配；两个都是实测"缺了就炸"的）。
NAMED_BASENAMES = ("content_types_kb.min.json", "DroidSansFallbackFull.ttf")


def _install_collect_shim() -> None:
    """把 `PyInstaller.utils.hooks.collect_data_files` 换成**同语义**的薄实现。

    为什么必须换：开发 venv 里**没有 PyInstaller**（它只在 `<build-environment>` 里），
    而 spec 顶层就 `from PyInstaller.utils.hooks import collect_data_files` ⇒ 直接 exec 会
    `ModuleNotFoundError`，测试就永远红/永远被 skip。薄实现与官方语义一致：
    **收包内的非 `.py` 数据文件**（`.pyc` / `__pycache__` / `.dist-info` 不收），返回
    `(源文件, 包目录)` 二元组列表 —— 正是 spec 里 `datas += collect_data_files(pkg)` 要的形状。
    """
    if "PyInstaller" in sys.modules:
        return
    pkg_root = PKG_VENV if PKG_VENV.is_dir() else Path(sys.prefix) / "Lib" / "site-packages"

    def collect_data_files(package: str, **_kw) -> list[tuple[str, str]]:
        pkg_dir = pkg_root / package
        if not pkg_dir.is_dir():
            return []
        out: list[tuple[str, str]] = []
        for f in pkg_dir.rglob("*"):
            if not f.is_file() or f.suffix in (".py", ".pyc", ".pyi"):
                continue
            if "__pycache__" in f.parts or ".dist-info" in str(f):
                continue
            out.append((str(f), package))
        return out

    module = types.ModuleType("PyInstaller.utils.hooks")
    module.collect_data_files = collect_data_files  # type: ignore[attr-defined]
    for name in ("PyInstaller", "PyInstaller.utils"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["PyInstaller.utils.hooks"] = module


def _spec_datas() -> list[tuple[str, str]]:
    """**不构建**、只跑到数据收集段为止，把 spec 算出来的 `datas` 取回来。

    spec 是给 PyInstaller 执行的脚本（顶层直接构造 `Analysis/EXE/COLLECT`），
    不能在测试进程里整份 exec；故截到收集循环结束就停，并补齐两处 PyInstaller 才会提供的东西：

    * `SPECPATH`：PyInstaller 执行 spec 时注入的「spec 所在目录」，spec 第 83 行用它做路径基准；
    * `ZL_WORKPATH`：spec 第 71 行 `os.environ.get("ZL_WORKPATH") or str(REPO_ROOT / "build")` ——
      ⚠️ 那一行的 `or` 右侧引用了**尚未定义**的 `REPO_ROOT`（它在第 83 行之后才赋值）⇒
      **只有在 `ZL_WORKPATH` 已设时才不炸**。`build.ps1` 会设它，故正式构建不受影响；
      这属**开发工具面的潜伏问题**（直接手跑 `pyinstaller zhilan.spec` 会 `NameError`），
      已在 6.6 证据里登记为发现，不在本条修复范围内。
    * `collect_data_files`：换成同语义薄实现（开发 venv 没有 PyInstaller，见下）。
    """
    _install_collect_shim()
    os.environ.setdefault("ZL_WORKPATH", tempfile.mkdtemp(prefix="s66_spec_workpath_"))
    source = SPEC.read_text(encoding="utf-8")
    assert COLLECT_LINE in source, (
        "spec 里找不到解析链数据收集段 —— 回归修复 的修复被删掉了？"
    )
    loop_at = source.index(COLLECT_LINE)
    add_at = source.index("datas += collect_data_files(_doc_parse_pkg)", loop_at)
    end = source.index("\n\n", add_at)
    namespace: dict = {"SPECPATH": str(SPEC.parent)}
    exec(compile(source[:end], str(SPEC), "exec"), namespace)  # noqa: S102 —— 跑的就是 spec 自己
    return list(namespace["datas"])


def _sources(datas: list[tuple[str, str]]) -> list[str]:
    return [str(item[0]).replace("\\", "/") for item in datas]


def test_spec_collects_all_three_packages() -> None:
    """三包的数据文件都要进 `datas`（防"补一半"）。"""
    sources = _sources(_spec_datas())
    missing = [
        pkg for pkg in PACKAGES
        if not any(f"/site-packages/{pkg}/" in src or src.endswith(f"/{pkg}/") or f"/{pkg}/" in src
                   for src in sources)
    ]
    assert not missing, (
        f"这些包的数据文件没进 datas：{missing} —— 装出来的 exe 会在解析时炸（回归修复）"
    )


def test_named_data_files_are_collected() -> None:
    """两个点名文件（实测缺了就炸）必须在 `datas` 里。"""
    sources = _sources(_spec_datas())
    for name in NAMED_BASENAMES:
        assert any(src.endswith("/" + name) for src in sources), (
            f"点名文件 `{name}` 不在 datas 里 ⇒ 冻结态解析链会缺资源（回归修复）"
        )


def test_collect_call_covers_the_three_packages_on_one_line() -> None:
    """收集循环的那一行必须把三个包都列出来（可读性 + 防被改成只收一个）。"""
    line = next((ln for ln in SPEC.read_text(encoding="utf-8").splitlines()
                 if COLLECT_LINE in ln), "")
    assert line, "spec 里找不到三包循环"
    for pkg in PACKAGES:
        assert f'"{pkg}"' in line, f"`{pkg}` 不在收集循环里"
