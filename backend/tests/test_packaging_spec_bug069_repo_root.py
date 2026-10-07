"""`回归修复` 回归钉：`zhilan.spec` 的 ``REPO_ROOT`` 必须定义于**首次使用**之前。

## 路径常量必须先定义后使用

`tools/packaging/zhilan.spec` 的 `_RUNTIME_HOOK_PATH`（0a 节）在 ``os.path.join`` 的右操作数里
写了 ``REPO_ROOT / "build"``，而 ``REPO_ROOT`` 原本在更靠后的「0. 路径基准」节才赋值：

- **已设** ``ZL_WORKPATH`` ⇒ ``or`` 短路、右操作数不求值 ⇒ 靠侥幸不炸（正式构建走 build.ps1 不受影响）；
- **未设** ``ZL_WORKPATH`` 时直跑 ``pyinstaller zhilan.spec`` ⇒ 文件头部即
  ``NameError: name 'REPO_ROOT' is not defined``（行号位移造因 = 提交 ``22a9d65``）。

修法（2026-09-27）：把「0. 路径基准」节（``SPEC_DIR`` / ``REPO_ROOT`` / ``BACKEND_DIR``）
整体**前移到 imports 之后、0a 节之前**。

## 本载体的三腿（行为腿优先，不靠文本 grep 单腿定案）

1. **行为腿（主腿）**：真实 ``exec`` spec 头部（到 0a 节写完 runtime hook 为止）——
   ``SPECPATH`` 与 ``PyInstaller`` 按真实执行形态注入，**不设** ``ZL_WORKPATH`` ⇒
   必须无异常，且 hook 文件真的落在 ``<REPO_ROOT>/build/`` 下
   （证明 ``REPO_ROOT`` 那个右操作数**被求值**了，而不是被短路跳过）；
2. **行为腿（短路语义保持）**：**设** ``ZL_WORKPATH`` ⇒ hook 落到自定义目录
   （原「用户覆盖优先」的语义逐字不变）；
3. **结构腿**：定义行在首次使用行之前（文本序断言，防将来有人把定义又挪回去——
   行为腿护「今天」，结构腿护「重构时一眼红灯」）。

⚠️ 限度（如实登记）：本文件只执行 spec **头部**（0/0a 两节），**不**跑 PyInstaller 的
Analysis/exe 段（那需要真实依赖树与构建环境，属构建机的活）。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_FILE = REPO_ROOT / "tools" / "packaging" / "zhilan.spec"

#: 头部切割点：0a 节（runtime hook 落盘）之后、资源路径常量之前 ——
#: 头部恰好覆盖「会用到 REPO_ROOT 的最早位置」，且不含任何 PyInstaller 构建调用。
_CUT_MARKER = 'FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"'

_HOOK_NAME = "_zl_runtime_hook_utf8.py"


def _spec_head() -> str:
    """取 spec 头部源码（0. 路径基准 + 0a. runtime hook 两节）。"""
    source = SPEC_FILE.read_text(encoding="utf-8")
    index = source.index(_CUT_MARKER)
    return source[:index]


def _exec_spec_head(spec_dir: Path, monkeypatch: pytest.MonkeyPatch, workpath: str | None) -> None:
    """按 PyInstaller 执行 spec 的形态跑头部：注入 ``SPECPATH`` + 桩 ``PyInstaller`` 模块。"""
    monkeypatch.delenv("ZL_WORKPATH", raising=False)
    if workpath is not None:
        monkeypatch.setenv("ZL_WORKPATH", workpath)
    pyinstaller = types.ModuleType("PyInstaller")
    utils = types.ModuleType("PyInstaller.utils")
    hooks = types.ModuleType("PyInstaller.utils.hooks")
    hooks.collect_data_files = lambda **kwargs: []  # noqa: ARG005 —— 头部只 import 不调用
    pyinstaller.utils = utils
    utils.hooks = hooks
    monkeypatch.setitem(sys.modules, "PyInstaller", pyinstaller)
    monkeypatch.setitem(sys.modules, "PyInstaller.utils", utils)
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    namespace: dict[str, object] = {"SPECPATH": str(spec_dir), "__name__": "zhilan_spec_head"}
    exec(compile(_spec_head(), str(SPEC_FILE), "exec"), namespace)  # noqa: S102 —— 载体本体


def test_repo_root_resolves_without_zl_workpath(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """腿 ①（主腿）：未设 ``ZL_WORKPATH`` ⇒ 头部无 ``NameError``，hook 落 ``<REPO_ROOT>/build``。"""
    spec_dir = tmp_path / "tools" / "packaging"
    spec_dir.mkdir(parents=True)
    _exec_spec_head(spec_dir, monkeypatch, workpath=None)
    hook = tmp_path / "build" / _HOOK_NAME
    assert hook.is_file(), (
        "未设 ZL_WORKPATH 时头部执行失败或 hook 没落在 <REPO_ROOT>/build ⇒ "
        "`REPO_ROOT` 仍是『定义晚于使用』或落盘锚点被改 ⇒ 069 回归"
    )
    assert "utf-8" in hook.read_text(encoding="utf-8")


def test_zl_workpath_still_short_circuits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """腿 ②：设了 ``ZL_WORKPATH`` ⇒ hook 落自定义目录（短路语义逐字保持）。"""
    spec_dir = tmp_path / "tools" / "packaging"
    spec_dir.mkdir(parents=True)
    custom = tmp_path / "custom-work"
    _exec_spec_head(spec_dir, monkeypatch, workpath=str(custom))
    assert (custom / _HOOK_NAME).is_file()
    assert not (tmp_path / "build" / _HOOK_NAME).exists(), "ZL_WORKPATH 已设时不应再落 <REPO_ROOT>/build"


def test_definition_precedes_first_use():
    """腿 ③（结构腿）：``REPO_ROOT`` 的赋值行在首次使用行之前（文本序）。"""
    source = SPEC_FILE.read_text(encoding="utf-8")
    definition = source.index("REPO_ROOT = SPEC_DIR.parents[1]")
    first_use = source.index("_RUNTIME_HOOK_PATH = os.path.join(")
    assert definition < first_use, (
        f"REPO_ROOT 赋值（偏移 {definition}）晚于首次使用（偏移 {first_use}）⇒ 069 回归"
    )
