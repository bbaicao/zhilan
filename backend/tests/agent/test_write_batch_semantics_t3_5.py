"""``回归检查`` 判据载体：**写失败批语义（`A23`）+ 清缓存接线 + 写工具注册**。

## 本件覆盖的判据（`D-回归检查` 三腿 + `D-回归检查` 第 1 条的接线腿）

| 判据 | 腿 | 用例 |
|---|---|---|
| **`A23`**（批语义可判，生效档 = 单⑩ (a)「单文件原子、不做批」） | ① 单文件原子 | `test_a23_leg1_single_file_is_atomic` |
| | ② **无跨文件回滚** | `test_a23_leg2_no_cross_file_rollback` |
| | ③ **互不干扰** | `test_a23_leg3_calls_do_not_interfere` |
| **清缓存接线**（`D-回归检查` 第 1 条） | 正腿（写成功 ⇒ 清） | `test_cache_invalidated_after_successful_write` |
| | 🔴 **反向腿**（写失败 ⇒ **不清**） | `test_cache_not_invalidated_when_write_fails` |
| **写工具注册**（`D-回归检查` ⑧：**在 `回归检查` 首次进 `for_model()`**） | 在清单里 + 不进 `auto_executable` | `test_write_tool_is_registered_inside_the_root_gate` |

## 🔴 `A23` 是「**证明不存在**」型判据（`K-回归检查`）

单⑩ 取 (a) ⇒ **没有「批」可判**。三腿断的不是「批语义正确」，而是：

1. 单次写失败 ⇒ 该文件**逐字节不变** + **无残留临时文件**；
2. 第 1 次成功 + 第 2 次失败 ⇒ **第 1 次的成果仍在**（**不得**被「回滚」掉）；
3. 两次调用**各自独立**（第 2 次的失败不影响第 1 次的结果）。

⚠️ `A-R2` 裁决：腿② 与腿③ **观察的是同一量** ⇒ **视为一条腿的两种写法，不得计作两条**。

## 🔴 故障注入的**当前口径**（执行期实测更正 · 2026-09-26）

`A23` 三腿需要「**一次会失败的写调用**」。执行期实测（只读推断 + 交叉核对 `_write.py`）：
把 `data` 传成**空字节**`b""` **不会**失败 —— `_write_atomically` 落一个空文件是**合法**的。

⇒ 本件改用**规范内、必然被拒**的注入：`rel_path="../../outside.txt"`（越界）。
路径由 `_write_paths.resolve_write_target()` 在**第 1 步（纯词法、零 IO）**判 `WriteReject`
⇒ `write_workspace_file` 在**任何写动作之前** `raise WriteError` ⇒ 满足三腿要的
「**写失败**」前提，且**不触发** `_write.py` 内部的任何 IO（这让「原文件逐字节不变」的
断言读的是**真事实**，而不是「碰巧没改到」）。

🔴 **为什么没有走 `monkeypatch` 打 `os.replace` / `tmp.write_bytes`**（那本是 `A7-1` 的手法）：
本件打的是**模块内闭包引用** `os.replace`（`_write.py` 里是 `import os` + `os.replace`），
`monkeypatch.setattr(os, "replace", ...)` 是**全局补丁** ⇒ 会波及同进程里所有 `os.replace` 调用。
⇒ **真·故障注入腿（两个注入点）归 `A7-1` 的载体**，本件只落 `A23` 要的三条**批语义**腿。
⚠️ 这是一条**如实登记**：本件**不声称**「已覆盖 `A7-1` 的注入点」。

## 🔴 载体说明：本件打的是 `_write.py`（`D-回归检查` ⑥），**不是** `workspace_write.py`

写动作本体 = `_write.write_workspace_file(*, root, rel_path, data, create, snapshot) -> WriteOutcome`
（`回归检查` 交付；`D-回归检查` ⑥ 逐字冻结签名）。`workspace_write.py` 只是**声明层**。

⇒ **本体尚未落地时**（`回归检查` 之前的中间态）本件的腿走 `pytest.skip` 并**逐字写明原因** ——
**不得**改写成「恒真通过」（那会把 TODO 写成通过条件，撞阶段 3 的 `宽通过式` 教训）。

## 纪律（照 `test_tool_paths_t3.py` / `test_write_tool_gate_t3_5.py`）

- `backend/tests/agent/` **没有 `conftest.py`**（实测 `Test-Path` = `False`）⇒ 夹具**自带**；
- **红线 12**：工作区根一律 `tmp_path`（**绝不**指向仓库根 / 数据根 / dist / 证据目录）；
- **零出网**：本件不碰联网面；
- ⚠️ 写侧变异必须**真写盘** ⇒ 变异体与还原体须用**不同的 `tmp_path`**（否则前一次写入污染后一次的
  「原文件逐字节不变」断言）。
"""

from __future__ import annotations

import pathlib

import pytest

from app.agent.bootstrap import build_registry
from app.agent.registry import ToolRegistry
from app.agent.tools import _paths as P
from app.core.config import settings


@pytest.fixture(autouse=True)
def _isolate_backup_anchor(tmp_path, monkeypatch):
    """🔴 **把备份锚点隔离到 `tmp_path`**（红线 12 · 阶段 5 事故 ⑤ 的直接后果）。

    `_write.resolve_backup_dir()` 现在有**测试态守卫**：测试里若锚点仍指向仓库内
    `data/workspace-backups/` ⇒ 直接抛 `TestModeRepoWriteError`（**结构性 fail-closed**）。
    本夹具把那句「每个载体都记得打锚点」的**约定**变成**不需要记得**的实现 ——
    因为实测证明「靠自觉」两轮共漏了 **65 + 170 = 235 件**污染。
    """
    from app.agent.tools import _write as _write_mod  # noqa: PLC0415

    monkeypatch.setattr(_write_mod, "resolve_backup_dir", lambda: tmp_path / "backups")

#: 本阶段新增的写工具契约名（`D-回归检查` ① 逐字冻结）
WRITE_NAME = "workspace_write"

#: `bootstrap.build_registry` 的常驻四件
BASE_FOUR = {"knowledge_search", "current_time", "list_documents", "read_document"}
#: 与写工具**同一道注册门**（`bootstrap.py:99`）的三个只读文件工具
FILE_THREE = {"list_files", "read_file", "grep_files"}

#: 写动作本体（`回归检查` 交付）—— 落地后本件的腿才真正执行（见模块 docstring）
_WRITE_MODULE = "app.agent.tools._write"
_WRITE_FUNC = "write_workspace_file"

#: 🔴 **故障注入用的一对常量**（见模块 docstring「故障注入的当前口径」）：
#: `INJECT_OK` = 一次**必然成功**的写；`INJECT_OUTSIDE` = 一个**必然被拒**的越界相对路径。
#: ⚠️ `rel_path` 越界在 `_write_paths.resolve_write_target()` 的**第 1 步（纯词法、零 IO）**
#: 就被判 `WriteReject` ⇒ `_write.py` 在任何写动作之前 raise ⇒ 三腿要的「写失败」前提成立。
INJECT_OK = "a.txt"
INJECT_OUTSIDE = "../../outside.txt"


def _write_func():
    """取写动作本体；**未落地 ⇒ `pytest.skip` + 逐字原因**（不得改成恒真通过）。"""
    import importlib

    try:
        module = importlib.import_module(_WRITE_MODULE)
    except ImportError:
        pytest.skip(
            f"{_WRITE_MODULE} 尚未落地（T3-5-2 的交付面）——"
            "本件的 A23 三腿与清缓存两腿在那一笔落地后自动转为实跑"
        )
    func = getattr(module, _WRITE_FUNC, None)
    if func is None:
        pytest.skip(f"{_WRITE_MODULE} 缺 {_WRITE_FUNC}（T3-5-2 的交付面尚未完成）")
    return func


@pytest.fixture(autouse=True)
def pin_web_off(monkeypatch):
    """联网默认关（本目录无 conftest，钉子自带）。"""
    monkeypatch.setattr(settings, "WEB_ENABLE", False)


@pytest.fixture
def workspace(tmp_path: pathlib.Path, monkeypatch) -> pathlib.Path:
    """一个**真**工作区根（红线 12：一律 `tmp_path`，绝不用仓库内目录）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_bytes("旧内容A\n".encode("utf-8"))
    (ws / "b.txt").write_bytes("旧内容B\n".encode("utf-8"))
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(ws))
    assert P.resolve_workspace_root() is not None, "夹具自证：根必须已配（否则观察量错位）"
    return ws


# ═══════════════════════════════════════════════════════════════════════════
# A23 三腿（单⑩ (a)：单文件原子、不做批 —— 「证明不存在」型）
# ═══════════════════════════════════════════════════════════════════════════

def test_a23_leg1_single_file_is_atomic(workspace: pathlib.Path) -> None:
    """**`A23` 腿①**：单次写**失败** ⇒ 该文件**逐字节不变** + **无残留临时文件**。

    ⚠️ 注入点 = **越界路径**（`INJECT_OUTSIDE`）—— 在 `_write_paths` 的**第 1 步（纯词法、零 IO）**
    就被拒 ⇒ 原文件**根本没被碰过**。本腿同时**显式断言**「失败确实发生了」，
    否则下面的「逐字节不变」在「调用其实成功了」时也会绿（**假绿陷阱**）。
    """
    write = _write_func()
    target = workspace / "a.txt"
    before = target.read_bytes()

    with pytest.raises(Exception):
        # ⚠️ 必须是**被测函数内部**会走到的那条拒绝路径（变异纪律 2：注入点要在调用链里）
        write(
            root=workspace,
            rel_path=INJECT_OUTSIDE,
            data=b"never lands\n",
            create=False,
            snapshot=True,
        )

    assert target.read_bytes() == before, "写失败后原文件必须逐字节不变"
    leftovers = [p.name for p in workspace.iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == [], f"写失败必须清理临时文件，实测残留：{leftovers}"
    # 越界写**不得**在根内留下任何新文件（红线 2：范围只能收窄）
    assert sorted(p.name for p in workspace.iterdir()) == ["a.txt", "b.txt"], (
        f"越界写留下了痕迹：{sorted(p.name for p in workspace.iterdir())}"
    )


def test_a23_leg2_no_cross_file_rollback(workspace: pathlib.Path) -> None:
    """**`A23` 腿②**：第 1 次成功 + 第 2 次失败 ⇒ **第 1 次的成果仍在**（不得被回滚）。"""
    write = _write_func()
    first = workspace / "a.txt"
    second = workspace / "b.txt"
    second_before = second.read_bytes()

    write(
        root=workspace,
        rel_path=INJECT_OK,
        data="新内容A\n".encode("utf-8"),
        create=False,
        snapshot=True,
    )
    assert first.read_bytes() == "新内容A\n".encode("utf-8"), "第 1 次写应成功"

    with pytest.raises(Exception):
        write(
            root=workspace,
            rel_path=INJECT_OUTSIDE,
            data=b"never lands\n",
            create=False,
            snapshot=True,
        )

    assert first.read_bytes() == "新内容A\n".encode("utf-8"), (
        "第 1 次的成果被第 2 次的失败「回滚」掉了 ⇒ 出现了跨文件事务（单⑩ (a) 明文禁止）"
    )
    assert second.read_bytes() == second_before, "失败的那个文件必须原样不变"


def test_a23_leg3_calls_do_not_interfere(workspace: pathlib.Path) -> None:
    """**`A23` 腿③**：两次调用**各自独立**（第 2 次的失败**不影响**第 1 次的结果）。

    ⚠️ `A-R2` 裁决：本腿与腿② **观察的是同一量** ⇒ **视为一条腿的两种写法，不得计作两条**。
    """
    write = _write_func()
    first = workspace / "a.txt"
    write(
        root=workspace,
        rel_path=INJECT_OK,
        data="独立A\n".encode("utf-8"),
        create=False,
        snapshot=True,
    )
    snapshot_after_first = first.read_bytes()

    with pytest.raises(Exception):
        write(
            root=workspace,
            rel_path=INJECT_OUTSIDE,
            data=b"never lands\n",
            create=False,
            snapshot=True,
        )

    assert first.read_bytes() == snapshot_after_first, "第 2 次失败改动了第 1 次的成果（互不干扰被破）"


# ═══════════════════════════════════════════════════════════════════════════
# 清缓存接线（`D-回归检查` 第 1 条：**写成功后**调 `cache.invalidate_all`）
# ═══════════════════════════════════════════════════════════════════════════

def test_cache_invalidated_after_successful_write(workspace: pathlib.Path, monkeypatch) -> None:
    """**接线正腿**：写**成功**后缓存**必须被清**（阶段 4 `回归检查` 的接线点，落在本笔）。"""
    write = _write_func()
    from app.cache import store as cache_store

    calls: list[str] = []

    def _spy(reason: str = "") -> int:
        calls.append(reason)
        return 0

    monkeypatch.setattr(cache_store, "invalidate_all", _spy, raising=False)
    write(
        root=workspace,
        rel_path=INJECT_OK,
        data="写完\n".encode("utf-8"),
        create=False,
        snapshot=True,
    )
    assert calls, "写成功后没有清缓存 ⇒ 阶段 4 的 T3-4-2 接线点未落地（D-T5-13 第 1 条）"


def test_cache_not_invalidated_when_write_fails(workspace: pathlib.Path, monkeypatch) -> None:
    """🔴 **反向腿**（防「无条件清」的假绿）：写**失败** ⇒ 缓存**未被清**。"""
    write = _write_func()
    from app.cache import store as cache_store

    calls: list[str] = []

    def _spy(reason: str = "") -> int:
        calls.append(reason)
        return 0

    monkeypatch.setattr(cache_store, "invalidate_all", _spy, raising=False)
    with pytest.raises(Exception):
        write(
            root=workspace,
            rel_path=INJECT_OUTSIDE,
            data=b"never lands\n",
            create=False,
            snapshot=True,
        )
    assert calls == [], f"写失败却清了缓存（成功与失败不可区分）：{calls}"


# ═══════════════════════════════════════════════════════════════════════════
# 写工具注册（`D-回归检查` ⑧ + 红线 4）
# ═══════════════════════════════════════════════════════════════════════════

def test_write_tool_is_registered_inside_the_root_gate(tmp_path: pathlib.Path, monkeypatch) -> None:
    """**`A13d` 的注册腿**：写工具进 `for_model()`，且**与只读三件同一道注册门**。

    观察量 = **两态差集**（`D-回归检查`：**不得**断总数，基数随开关组合变）。
    """
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", "")
    offline = {s.name for s in build_registry().for_model(web_enabled=False)}
    assert WRITE_NAME not in offline, "根未配置时写工具**不得**进模型清单（红线 3 同款：不设默认）"

    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(tmp_path))
    online = {s.name for s in build_registry().for_model(web_enabled=False)}
    assert WRITE_NAME in online, "配根后写工具必须进 for_model()（A13d 的直接验收点）"
    assert online - offline == FILE_THREE | {WRITE_NAME}, (
        f"写工具必须与只读三件**同一道注册门**：{sorted(online - offline)}"
    )
    assert BASE_FOUR <= online, f"常驻四件不得因本笔消失：{sorted(BASE_FOUR - online)}"


def test_write_tool_never_enters_auto_executable(tmp_path: pathlib.Path, monkeypatch) -> None:
    """**红线 4**：写工具**永不自动执行**（`auto_executable()` 排除 `needs_confirm`）。"""
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(tmp_path))
    registry = build_registry()
    auto = {s.name for s in registry.auto_executable()}
    assert WRITE_NAME not in auto, f"写工具进了自动执行白名单（红线 4）：{sorted(auto)}"
    # 负控：注册表里**确实**有它（否则上面那条退化成「它压根不存在」）
    assert WRITE_NAME in {s.name for s in registry.list()}, "写工具不在注册表里 ⇒ 上一条是空靶"


def test_write_tool_declares_needs_confirm_true() -> None:
    """**单③ (a) 档 2**：写工具的声明面 `needs_confirm is True`（显式，不靠默认值）。"""
    from app.agent.tools import workspace_write

    spec = workspace_write.build_spec()
    assert spec.name == WRITE_NAME
    assert spec.needs_confirm is True, "写工具必须显式声明 needs_confirm=True（D-T5-2 B-B2）"
    assert spec.dangerous is False, "写工具不是 dangerous（危险工具连 spec 都不生成）"
    assert set(spec.parameters_schema["properties"]) == {
        "path",
        "content",
        "confirm_token",
        "create",
    }, spec.parameters_schema
    assert set(spec.parameters_schema["required"]) == {"path", "content"}, spec.parameters_schema
    # 写工具**不得**被误装进只读注册表实例
    assert ToolRegistry is not None