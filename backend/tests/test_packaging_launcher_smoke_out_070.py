"""`回归修复` 判据载体：`--smoke` 的 **JSON 结果出口**（不依赖 stdout）。

## 无控制台构建的自检结果出口

冻结态应用层日志不进控制台；修复有**硬顺序**：① 先给 `--smoke` 一条**不依赖 stdout** 的出口
→ ② 才可能切 `console=False`（冻结态 `sys.stdout is None` ⇒ `print` 全部静默丢弃，
而 `build.ps1` 的 L2 门禁原先**全靠** stdout 正则拿「三项资源命中 / 失败数」读数）。
6.5 裁决 10「不改 console」的唯一理由正是「切了会打掉共用门禁」——本出口落地后该理由解除。

本笔交付 = ①（`zhilan_launcher.py` 新增 `--smoke-out <file>`）+ `build.ps1` L2 改吃 JSON
（`frozen=True` 同时纳入门禁，补上「采集了但没进门禁」的附带发现）。
⛔ **不是**完整修复：`console=False` 的切换是发布构建时的 `-Console $false` 选择，
应用层日志的文件出口属产品行为面、仍待拍板 ⇒ `070` 仍 `open`（前置已就绪）。

## 载体设计

真实加载 `tools/packaging/zhilan_launcher.py`（与 `zhilan.spec::_load_smoke_modules()` 同手法：
`module_from_spec + exec_module` 只跑顶层定义），桩掉 `_SMOKE_MODULES` / `main` 模块，
**不** import 任何重依赖。六腿：

1. **正控**：全绿 ⇒ 退出码 0 + JSON 六项判据齐全（`result/failures_count/dist_hit/ini_hit/dir_hit/frozen`）；
2. **模块失败腿**：假模块 import 失败 ⇒ 退出码 1 + JSON `failures[]` 带模块名与错误；
3. **资源缺失腿**：`index.html` 不在 ⇒ 退出码仍 0（launcher 不判）但 JSON `dist_hit=False`
   —— 正是门禁要消费、stdout 给不出来的那个布尔值；
4. **🔴 主腿（stdout=None）**：把 `sys.stdout` 钉成 `None`（模拟 `console=False` 冻结态实测形态）
   ⇒ `print` 全部静默丢弃，但 JSON **照常落盘**、退出码照常 0 —— 「不依赖 stdout」的行为面证明；
5. **fail-closed 腿**：出口路径写不出来 ⇒ 退出码 1 + `failures[]` 含 `--smoke-out`
   （门禁以本文件为判据主源，缺了不能当通过）；
6. **用法腿**：`--smoke-out` 不带值 ⇒ `main()` 返回 2（用法错误，不进 smoke）。

⚠️ 限度：本载体在**开发态**跑真 launcher 代码（`sys.frozen` 为假 ⇒ JSON 的 `frozen=false`）；
「真冻结 exe 上写 JSON」由下次构建的 L2 门禁实测（证据件 `*-smoke结果-*.json`）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LAUNCHER_FILE = REPO / "tools" / "packaging" / "zhilan_launcher.py"

#: 既有 6.5 载体钉死的模块数（`zhilan.spec` 动态读同一元组）。
MODULE_COUNT = 22


@pytest.fixture()
def launcher():
    """按 spec 同款手法加载 launcher（只跑顶层定义，不执行启动逻辑）。"""
    spec = importlib.util.spec_from_file_location("_zhilan_launcher_070", LAUNCHER_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_index_html: bool = True):
    """`main` 模块桩：`_roots()` / `frontend_dist()` 指向 tmp 布局。"""
    backend_root = tmp_path / "backend-root"
    backend_root.mkdir()
    (backend_root / "alembic.ini").write_text("x", encoding="utf-8")
    (backend_root / "alembic").mkdir()
    dist_dir = backend_root / "frontend" / "dist"
    dist_dir.mkdir(parents=True)
    if with_index_html:
        (dist_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    fake = types.ModuleType("main")
    fake._roots = lambda: (tmp_path / "res-root", backend_root)  # type: ignore[attr-defined]
    fake.frontend_dist = lambda: dist_dir  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "main", fake)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_smoke_pass_writes_full_json(launcher, tmp_path, monkeypatch):
    """腿 ①（正控）：全绿 ⇒ 退出码 0，JSON 六项判据齐全。"""
    _stub_main(tmp_path, monkeypatch, with_index_html=True)
    monkeypatch.setattr(launcher, "_SMOKE_MODULES", ("json",))
    out = tmp_path / "smoke.json"

    code = launcher._smoke(str(out))

    assert code == 0
    payload = _read_json(out)
    assert payload["result"] == "pass"
    assert payload["failures_count"] == 0
    assert payload["modules_total"] == 1
    assert payload["dist_hit"] is True
    assert payload["ini_hit"] is True
    assert payload["dir_hit"] is True
    assert payload["frozen"] is False, "开发态 frozen 必为假；真冻结态由构建门禁实测"
    assert set(payload["env"]) == set(launcher.FROZEN_ENV_KEYS), "五项环境读数键必须齐全（6.5 裁决 3 出口）"


def test_module_failure_lands_in_json(launcher, tmp_path, monkeypatch):
    """腿 ②：import 失败 ⇒ 退出码 1，JSON `failures[]` 带模块名与错误。"""
    _stub_main(tmp_path, monkeypatch)
    monkeypatch.setattr(launcher, "_SMOKE_MODULES", ("json", "zl_no_such_module_070"))
    out = tmp_path / "smoke.json"

    code = launcher._smoke(str(out))

    assert code == 1
    payload = _read_json(out)
    assert payload["result"] == "fail"
    assert payload["failures_count"] == 1
    assert payload["failures"][0]["module"] == "zl_no_such_module_070"
    assert "ModuleNotFoundError" in payload["failures"][0]["error"]


def test_resource_miss_carries_false_with_zero_exit(launcher, tmp_path, monkeypatch):
    """腿 ③：资源缺失 ⇒ 退出码仍 0（launcher 不判），但 JSON `dist_hit=False` 供门禁判死。"""
    _stub_main(tmp_path, monkeypatch, with_index_html=False)
    monkeypatch.setattr(launcher, "_SMOKE_MODULES", ("json",))
    out = tmp_path / "smoke.json"

    code = launcher._smoke(str(out))

    assert code == 0
    assert _read_json(out)["dist_hit"] is False


def test_json_written_even_when_stdout_is_none(launcher, tmp_path, monkeypatch):
    """腿 ④（🔴 主腿）：`sys.stdout=None`（模拟 `console=False` 冻结态）⇒ JSON 照常落盘。

    CPython 实测口径（6.5 轮核实件 A3）：`sys.stdout is None` 时 `print()` **静默丢弃**。
    本腿把 stdout 钉成 None 后跑 `_smoke`：判据文件与退出码都必须**照常工作**
    —— 这正是「不依赖 stdout 的出口」的定义面。
    """
    _stub_main(tmp_path, monkeypatch)
    monkeypatch.setattr(launcher, "_SMOKE_MODULES", ("json",))
    monkeypatch.setattr(sys, "stdout", None)
    out = tmp_path / "smoke.json"

    code = launcher._smoke(str(out))

    assert code == 0
    payload = _read_json(out)
    assert payload["result"] == "pass"
    assert payload["dist_hit"] is True and payload["ini_hit"] is True and payload["dir_hit"] is True


def test_unwritable_out_path_fails_closed(launcher, tmp_path, monkeypatch, capsys):
    """腿 ⑤：出口写不出来 ⇒ 退出码 1 + FAIL 项点名 `--smoke-out`（fail-closed）。"""
    _stub_main(tmp_path, monkeypatch)
    monkeypatch.setattr(launcher, "_SMOKE_MODULES", ("json",))
    blocker = tmp_path / "afile"
    blocker.write_text("我是文件，不是目录", encoding="utf-8")

    code = launcher._smoke(str(blocker / "smoke.json"))  # 父路径是文件 ⇒ mkdir 必炸

    assert code == 1, "门禁以本文件为判据主源：写不出来必须失败，不能静默当通过"
    assert "--smoke-out" in capsys.readouterr().out


def test_main_usage_error_when_flag_without_value(launcher, monkeypatch):
    """腿 ⑥：`--smoke-out` 不带值 ⇒ `main()` 返回 2（用法错误），且不进 smoke / 不 import client。"""
    monkeypatch.setattr(sys, "argv", ["zhilan.exe", "--smoke", "--smoke-out"])
    called = False

    def _boom(*_a):
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(launcher, "_smoke", _boom)

    assert launcher.main() == 2
    assert called is False
