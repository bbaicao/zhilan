"""`回归检查` 判据载体：三个只读文件工具（专项工具升级-3 · 阶段 3）。

覆盖 **A15**（`materials == []`）· **A18 的零写腿**（两态清单 + 差集恰三名）· **A17**（import 面）·
**A19 / A13f 的前置腿**（前缀常量存在且三工具观察文本真出现它）· **A5b 第 5 腿的真模块版**
（`after - before == THREE_NAMES`）。

⚠️ **本文件自带夹具**：`tests/agent/` **无 `conftest.py`**（实测 `Test-Path` = False）
⇒ `tests/api/conftest.py` 的 autouse 钉子**对本文件不生效**。先例：`tests/agent/test_tool_paths_t3.py`。

⚠️ **红线 12**：测试的工作区根**一律 `tmp_path`**，**绝不**用仓库内任何目录（`D-回归检查` / `K-回归检查`）。

## 与 `test_workspace_root_t3_3.py` 的**两笔拆法**（`回归检查` → `回归检查`）

| 层 | 文件 | 观察量 | 靶子 |
|---|---|---|---|
| **桩层**（task-1） | `test_workspace_root_t3_3.py` | 往 `app.agent.tools` 内存命名空间注入三具桩 ⇒ `after >= THREE_NAMES` | 「**门开了就会注册**」（证明门与模块是否存在是**两条正交交付面**） |
| **真模块层**（本笔） | 本文件 | 不注入任何桩，用**真模块** ⇒ `after - before == THREE_NAMES` | 「**差集恰为三名**」（堵死软注册的静默降级：模块名写错 / `__init__` 漏登记 / import 失败） |

⚠️ **两层不是重复**：`>=` 单独存在会**放行空集**；`==` 单独存在则**无法区分**「门坏」与「模块没接上」。
🔴 **真模块层不得依赖桩** —— 否则真模块缺失时桩把它顶上 ⇒ **假绿**。本文件的 `_real_module_or_fail()`
**主动断言**「拿到的是真模块（有 `__file__`）而不是桩」，把这条封死（**肯定式证据**，比「不注入」这种否定式可靠）。

🔴 **总警告（本文件现 30 个 `test_*`，后续仍会被 task-4 改）—— 不配根的用例会静默走到「未配置」分支**

本文件的工具腿**依赖已配的工作区根**；而 `settings.WORKSPACE_ROOT` 在测试进程里**默认为空**。
⇒ 一条腿若**忘了挂** `in_workspace` / `workspace` 夹具，`resolve_workspace_root()` 会返 `None`，
工具会在 `list_files.py:228-229` 形态的「根未配置」分支**提前返回**，而 `_paths.classify` 则在
`:473-474` 的 root 分支提前返 `PATH_NO_ROOT_TEMPLATE`（「未配置工作区根…」）。

**两种后果都是「观察量错位」而不是「真红」**：

- 断 `ok=False` 的腿 ⇒ **假绿**（未配置也会 `False`，拿不到真想验的分支）；
- 断 `Reject` 文案/层号的腿、或断 `ok=True` 的腿 ⇒ **假红**（正文案是「未配置」）。

🔴 **写新腿时的自查句：「这条腿要求的分支，需不需要一个已配的根？」**
需要 ⇒ 必须挂 `in_workspace` 或 `workspace`；`in_workspace` 内部已自带 `assert P.resolve_workspace_root() is not None`，
它**就是为了把这个陷阱变成可见失败**。
（本笔实测踩过：初版有 2 条腿忘挂夹具 ⇒ 红在「未配置」分支上，已修。）
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from app.agent.bootstrap import build_registry
from app.agent.tools import _paths as P
from app.agent.tools import _source
from app.core.config import settings

#: 本条目新增的三个只读文件工具（`A18` 的零写腿用**差集**钉住恰为这三名）。
THREE_NAMES = {"list_files", "read_file", "grep_files"}

#: 工具模块所在目录（用于 A17 的 AST 扫描）。
TOOLS_DIR = Path(P.__file__).resolve().parent


# ── 夹具（自带；`tests/agent/` 无 conftest）──────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """一个**真**工作区根（红线 12：**一律 `tmp_path`**，绝不用仓库内目录）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("alpha\nbeta\n gamma \n", encoding="utf-8")
    (ws / "sub").mkdir()
    (ws / "sub" / "b.txt").write_text("needle here\n", encoding="utf-8")
    (ws / "bin.dat").write_bytes(b"head\x00tail")
    return ws


@pytest.fixture
def in_workspace(monkeypatch, workspace: Path) -> Path:
    """把 `settings.WORKSPACE_ROOT` 指向 `tmp_path` 下的真目录（例级撤销）。"""
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(workspace))
    assert P.resolve_workspace_root() is not None
    return workspace


def _real_module_or_fail(name: str):
    """取**真模块**；若拿到的是桩（或不存在）⇒ **直接失败**（防真模块层的假绿）。

    🔴 这是 task-1 桩层与 task-2 真模块层的**分界线** —— 本文件所有腿都**不得**在桩在场时求值。
    """
    module = importlib.import_module(f"app.agent.tools.{name}")
    assert getattr(module, "__file__", None), f"{name} 不是真模块（无 __file__）⇒ 真模块层无效"
    return module


def _spec_names(web_enabled: bool = False) -> set[str]:
    """`build_registry()` 装配后模型可见的工具名集合。"""
    return {spec.name for spec in build_registry().for_model(web_enabled=web_enabled)}


def _call(module, **kwargs):
    """同步跑一次工具 handler（`asyncio.run` 包一层，避免给每条腿加 `pytest.mark.asyncio`）。"""
    import asyncio

    spec = module.build_spec()
    args = module.Args(**kwargs)
    return asyncio.run(spec.handler(args))


# ── 前置判据：三模块**真可 import**（audit-facts B-⑰）────────────────────────


def test_all_tool_modules_are_really_importable() -> None:
    """**存在性前置**：包可 import + 三模块逐个 import + `TOOL_NAME` 自洽 + `__all__` 反向腿。

    🔴 **为什么需要它（audit-facts 的正式发现 B-⑰）**：`A17` 管的是 **import 的层次归属**
    （标准库 + `app.agent.tools` / `app.core`），**不管被 import 的模块是否真存在** ⇒
    下列三种**必然炸穿全轮装配**的情形 `A17` **统统照过**：
    ① 漏 commit 新文件；② `__init__` 登记了不存在 / 拼错的模块名；
    ③ 新模块 import 同包内**尚未落地**的兄弟模块（**本笔在途复现过**：三工具先于 `_source.py` 落盘）。
    ⚠️ **定性**：`A17` **没有缺陷**（它的设计目标本就是层次面）；缺的是**存在性**这一条判据。

    🔴 **反向腿（③）必须有**：单纯 `import app.agent.tools` 不够 —— 若 `__init__` 写成惰性 /
    `importlib` 登记，漏件可能**悄悄通过**；`getattr` 反向腿把它封死。
    """
    import app.agent.tools as tools_pkg

    for name in sorted(THREE_NAMES):
        module = _real_module_or_fail(name)
        assert module.TOOL_NAME == name, f"{name} 的 TOOL_NAME 与模块名不一致：{module.TOOL_NAME}"

    # 反向腿：`__all__` 里每一个名字都必须真能取到对象
    missing = [name for name in tools_pkg.__all__ if getattr(tools_pkg, name, None) is None]
    assert missing == [], f"__all__ 登记了取不到的名字（漏件 / 拼错）：{missing}"


def test_source_prefix_constant_exists_and_is_the_only_one() -> None:
    """**`A19` 的前置腿**：前缀常量存在（**非存在性断言**的后半腿在下面各工具腿里）。"""
    assert _source.DISK_SOURCE_PREFIX == "【磁盘】", _source.DISK_SOURCE_PREFIX


# ── A18 零写腿：两态清单 + 差集**恰为三名** ────────────────────────────────


def test_a18_two_state_diff_is_exactly_the_three_new_tools(monkeypatch, workspace: Path) -> None:
    """**A18 的零写腿（真模块层）**：两态由 `monkeypatch` **显式制造**，差集**恰为**三名。

    🔴 **这是 task-1 桩层 `>=` 的加强版**：桩层只证明「门开了会注册」；本腿用**真模块**证明
    「差集**恰为**三名」⇒ 堵死软注册（`getattr(..., None)`）的三种静默降级。
    ⚠️ **本腿不注入任何桩**，且 `_real_module_or_fail()` 会主动断言「拿到的是真模块」。
    """
    for name in sorted(THREE_NAMES):
        _real_module_or_fail(name)

    # 档 0：未配置根
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", "")
    offline = _spec_names()
    assert THREE_NAMES & offline == set(), f"未配置根时三工具不得进清单：{sorted(THREE_NAMES & offline)}"

    # 档 1：配真目录
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(workspace))
    online = _spec_names()
    # 🔴 **阶段 5（`回归检查`）起差集不是三名，而是「文件工具族」全体**：
    # `workspace_write` 与三件只读工具**同受** `bootstrap.build_registry()` 的工作区根
    # 注册门支配（`bootstrap.py:99`）⇒ 开根时**一起**进清单。
    # ⚠️ 本腿的**原判别力一字未减**：仍是**恰等**（无 `<=` / 无 `set()` 分支），
    # 仍能堵死软注册（`getattr(..., None)`）的三种静默降级；只是期望集随阶段 5 扩容。
    # 🔴 期望值**写死字面量、不从被测实现派生**（防自我印证）。
    FILE_FAMILY = THREE_NAMES | {"workspace_write"}
    assert online - offline == FILE_FAMILY, (
        f"两态差集必须**恰为**文件工具族全体（软注册漏件的唯一探针）：{sorted(online - offline)}"
    )
    # 反向腿：**只读三件**仍必须恰在其中（写工具的加入不得顶掉任何一件）
    assert THREE_NAMES <= (online - offline), (
        f"三件只读文件工具仍必须全部进清单：{sorted(THREE_NAMES - (online - offline))}"
    )
    assert offline - online == set(), f"跃迁不得让任何工具消失：{sorted(offline - online)}"


def test_a18_no_write_capability_and_no_confirm_field(monkeypatch, workspace: Path) -> None:
    """**A18 的另两条腿**：`dangerous` 全 `False` + **这三个只读工具**不声明写能力。

    ⚠️ **不得**用「搜关键字 `write`」当判据（存在性断言，改名即绕过 —— 阶段 2 的 `A-2` 同型教训）。
    ⇒ 本腿用**结构化**判据。

    🔴 **阶段 5（`回归检查`）口径变更（2026-09-26，如实登记，不改写历史）**：
    原判据是「`ToolSpec` 的**字段集**里没有 `needs_confirm`」—— 那条**只在阶段 3 成立**：
    当时写通道尚未开工，字段存在即越界。阶段 5 引入写通道后该字段**必须**存在，
    ⇒ 「字段不存在」不再是「零写」的判据；**真正的零写判据**变成：

      ① 三个只读文件工具**逐处显式**声明 `needs_confirm=False`（= 只读，不是写工具）；
      ② 它们的 `dangerous` 都是 `False`（`for_model()` 不过滤它们 ⇒ 不是危险工具）。

    ⇒ 原第① 条**升级**为「字段存在**且**三件都显式 `False`」—— 判据**没有被削弱**：
    若将来有人把这三个只读工具标成 `needs_confirm=True`，本腿照样红。
    """
    from dataclasses import fields

    from app.agent.registry import ToolSpec

    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(workspace))
    registry = build_registry()

    # ①（阶段 5 升级版）三个只读文件工具必须**逐处显式**是只读声明
    field_names = {item.name for item in fields(ToolSpec)}
    assert "needs_confirm" in field_names, (
        f"阶段 5 起 ToolSpec **必须**有 needs_confirm 字段（T3-5-1）：{sorted(field_names)}"
    )
    for name in sorted(THREE_NAMES):
        assert registry.get(name).needs_confirm is False, (
            f"{name} 是**只读**文件工具，必须显式 needs_confirm=False"
        )

    for name in sorted(THREE_NAMES):
        spec = registry.get(name)
        assert spec.dangerous is False, f"{name} 被打上 dangerous ⇒ 越界（本阶段零写）"
    visible = {spec.name for spec in registry.for_model(web_enabled=False)}
    assert THREE_NAMES <= visible, f"三件不得被 for_model 过滤掉：{sorted(visible)}"


def test_a18_mcp_dimension_uses_prefix_filter(monkeypatch, workspace: Path) -> None:
    """**A18 第 5 条**：清单含 `mcp:{slug}:{tool}` 维 ⇒ 判据用**前缀过滤**而非全等字面量。"""
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(workspace))
    names = _spec_names()
    non_mcp = {name for name in names if not name.startswith("mcp:")}
    assert THREE_NAMES <= non_mcp, f"三件必须出现在非 mcp 维里：{sorted(non_mcp)}"


# ── A15 零材料（红线 8）────────────────────────────────────────────────────


def test_a15_three_tools_return_zero_materials(in_workspace: Path) -> None:
    """**A15**：三工具返回的 `ToolObservation.materials == []`（**绝不**把文件内容当材料）。

    🔴 载体是 `materials`（`agent/types.py:41`），**不是**帧里的 `refs` 键。
    """
    list_obs = _call(module_of("list_files"))
    read_obs = _call(module_of("read_file"), path="a.txt")
    grep_obs = _call(module_of("grep_files"), pattern="beta")
    for label, obs in (("list_files", list_obs), ("read_file", read_obs), ("grep_files", grep_obs)):
        assert obs.ok is True, f"{label} 应成功：{obs.text!r}"
        assert obs.materials == [], f"{label} 返回了材料 ⇒ 违红线 8：{obs.materials!r}"


def test_a19_and_a13f_disk_prefix_really_appears_in_three_tools(in_workspace: Path) -> None:
    """**`A19`（非存在性断言）**：三工具观察文本**真出现** `DISK_SOURCE_PREFIX`。

    🔴 「常量存在」不够（那只是 `test_source_prefix_constant_exists_...` 的腿）——
    本腿断的是「**三处观察文本里真出现它**」，把 `M-回归检查`（把前缀内联进字符串、删常量）的靶子立起来。
    """
    for label, obs in (
        ("list_files", _call(module_of("list_files"))),
        ("read_file", _call(module_of("read_file"), path="a.txt")),
        ("grep_files", _call(module_of("grep_files"), pattern="beta")),
    ):
        assert _source.DISK_SOURCE_PREFIX in obs.text, f"{label} 观察文本未出现磁盘源前缀：{obs.text!r}"


def module_of(name: str):
    """取工具模块（真模块；桩会被 `_real_module_or_fail` 拒掉）。"""
    return _real_module_or_fail(name)


# ── A17 import 面（标准库放行 + 同层放行 + 显式禁上层）──────────────────────


def _imported_modules(path: Path) -> set[str]:
    """AST 收集一个文件里的 import 目标（顶层模块名）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
    return out


def test_a17_three_tools_import_surface_is_whitelisted() -> None:
    """**A17**：新模块 import 面 = **标准库放行** + `app.core` + `app.agent.tools`（**含包内子模块互引**）。

    ⚠️ **标准库必须放行**（审计 D 路：字面实现必红 —— `_paths.py` 实测 import 6 个标准库）。
    ⚠️ **同层互引必须放行**：`grep_files` 复用 `read_file` 的 `ENCODINGS` / `PROBE_BYTES`
    ⇒ 这是「**工具族侧只有一份探测实现**」的**结构性保证**，不是违规。
    """
    import sys

    stdlib = set(sys.stdlib_module_names)
    # ⚠️ **白名单必须包含工具族的既有标准依赖**（实测：`read_document.py:39-41` 就是
    # `app.agent.registry` + `app.agent.tools._errors` + `app.agent.types`）——
    # 写窄了会把**合法同族依赖**误判成越界（我第一版就是这样，本轮 7 条红里有 2 条由此而来）。
    # ⇒ 口径 = **`app.agent`**（工具族的宿主包：registry / types / tools 三个子面）+ **`app.core`**；
    # 仍**显式禁** `app.pipeline`（D-回归检查 取甲）/ `app.api` / `app.retrieval` / `app.models`。
    allowed_prefixes = ("app.agent.", "app.core")

    for name in sorted(THREE_NAMES):
        path = TOOLS_DIR / f"{name}.py"
        for target in _imported_modules(path):
            root = target.split(".")[0]
            if root in stdlib or root in {"__future__"}:
                continue
            if not target.startswith("app"):
                # ⚠️ **第三方依赖不属 A17 的管辖面**：A17 管的是「**本仓分层**是否被越界 import」。
                # 既有工具同样 import `pydantic` / `sqlalchemy`（实测 `read_document.py:36-37`）
                # ⇒ 把第三方算进白名单会**误伤**（我第一版就踩了）。
                # 🔴 **A17 的管辖面 = 「本仓分层」**，第三方依赖（`pydantic` / `sqlalchemy`）
                # **不在其面** ⇒「非 `app` 开头一律放行」是正确落法。
                # 「本阶段**零新增依赖**」由 **requirements 面另行保证，不在本腿** ——
                # 这句划清了**判据边界**，避免 A17 被当成「依赖守门人」。
                continue
            assert target.startswith(allowed_prefixes), (
                f"{name}.py 导入了白名单外的 app 层模块：{target}"
                "（只许 app.agent（含 registry/types/tools）/ app.core）"
            )


def test_a17_three_tools_never_import_app_pipeline() -> None:
    """**A17 的专门负腿**：三工具**不得** import `app.pipeline`（`D-回归检查` 取**甲**的约束）。

    ⚠️ 这条必须**单独**断，否则白名单写宽了会把「取甲」的约束**架空**
    （`detect_encoding()` 在 `app/pipeline/parsing.py:118`）。
    """
    for name in sorted(THREE_NAMES):
        targets = _imported_modules(TOOLS_DIR / f"{name}.py")
        offenders = {
            item
            for item in targets
            if item.split(".")[0] == "app"
            and not item.startswith("app.agent.")
            and not item.startswith("app.core")
        }
        assert offenders == set(), f"{name}.py 导入了禁用的 app.* 模块：{sorted(offenders)}"
        assert not any(item.startswith("app.pipeline") for item in targets), (
            f"{name}.py 导入了 app.pipeline（D-T3-8 取甲 ⇒ 必须自带探测）：{sorted(targets)}"
        )


# ── 三工具的边界与形状（D-回归检查）─────────────────────────────────────────────


def test_d_t3_7_boundaries_reject_out_of_workspace_paths(in_workspace: Path) -> None:
    """**越界 = `classify` 判 `Reject`**：三工具对越界路径一律 `ok=False` + 中文可读、**不夹紧**。

    🔴 红线 1/2：参数**不得突破**配置的根（范围只能收窄）。
    """
    for label, obs in (
        ("list_files", _call(module_of("list_files"), path="../../outside")),
        ("read_file", _call(module_of("read_file"), path="../../outside.txt")),
        ("grep_files", _call(module_of("grep_files"), pattern="x", path="../../outside")),
    ):
        assert obs.ok is False, f"{label} 对越界路径必须失败：{obs.text!r}"
        assert any("\u4e00" <= ch <= "\u9fff" for ch in obs.text), f"{label} 失败文案必须中文可读：{obs.text!r}"


def test_d_t3_7_bad_params_are_rejected_not_clamped(in_workspace: Path) -> None:
    """**坏参数一律 `ok=False`**（**不夹紧**成上限）—— 照 `read_document` 的 `arg_error` 先例。"""
    over_list = _call(module_of("list_files"), max_entries=10_000)
    over_read = _call(module_of("read_file"), path="a.txt", max_lines=10_000)
    over_grep = _call(module_of("grep_files"), pattern="a", max_hits=10_000)
    zero_read = _call(module_of("read_file"), path="a.txt", start_line=0)
    for label, obs in (
        ("list_files.max_entries", over_list),
        ("read_file.max_lines", over_read),
        ("grep_files.max_hits", over_grep),
        ("read_file.start_line", zero_read),
    ):
        assert obs.ok is False, f"{label} 越界/坏值必须失败（不夹紧）：{obs.text!r}"


def test_d_t3_7_relative_paths_are_relative_to_the_workspace_root(in_workspace: Path, tmp_path: Path, monkeypatch) -> None:
    """🔴 **红线 1**：相对路径**一律相对工作区根**，**绝不**取进程 CWD（`M-回归检查` 的靶子）。

    ⇒ 把进程 CWD 换到别处，行为**必须逐字不变**。
    """
    before = _call(module_of("read_file"), path="a.txt")
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    after = _call(module_of("read_file"), path="a.txt")
    assert before.ok is True, before.text
    assert after.ok is True, f"CWD 改变后相对路径解析断了 ⇒ 取了 CWD（违红线 1）：{after.text!r}"
    assert before.text == after.text, "CWD 改变后观察文本变了 ⇒ 相对路径基准不是工作区根"


def test_grep_files_skips_binary_files(in_workspace: Path) -> None:
    """**二进制跳过**：含 `\x00` 的文件不得产出命中（**不返回乱码**）。"""
    obs = _call(module_of("grep_files"), pattern="head")
    assert obs.ok is True, obs.text
    assert "bin.dat" not in obs.text, f"二进制文件不得进命中：{obs.text!r}"


def test_grep_files_is_literal_not_regex(in_workspace: Path) -> None:
    """🔴 **`K-回归检查`：固定字符串匹配（禁正则）** —— 正则元字符必须按字面处理。

    `pattern=\"a.*a\"` 在**正则**语义下会命中 `alpha`；在**字面**语义下**不该**命中。
    """
    obs = _call(module_of("grep_files"), pattern="a.*a")
    assert obs.ok is True, obs.text
    assert "a.txt" not in obs.text, f"\"a.*a\" 被当成正则了（K-T3-10 禁正则）：{obs.text!r}"


def test_read_file_reads_line_range_from_one(in_workspace: Path) -> None:
    """**行号 1 起** + 按区间读（`D-回归检查`）。"""
    obs = _call(module_of("read_file"), path="a.txt", start_line=2, max_lines=1)
    assert obs.ok is True, obs.text
    assert "beta" in obs.text, obs.text
    assert "alpha" not in obs.text, f"起始行 2 却读到了第 1 行：{obs.text!r}"


def test_read_file_decodes_utf8_chinese_before_falling_back(tmp_path: Path, monkeypatch) -> None:
    """🔴 **`M-回归检查` 的靶子**：工具族侧的编码探测**顺序**必须真的生效。

    ## 为什么必须有这条腿（第一版它是空靶）

    第一次跑变异时 `M-回归检查`（把 `ENCODINGS` 压成 `("latin-1",)` 一档）**SURVIVED** ——
    根因是**观察量选错**：它当时打在 `test_read_file_reads_line_range_from_one` 上，
    而那个用例读的是**纯 ASCII** 文件（`alpha` / `beta`）—— ASCII 在 utf-8 与 latin-1 下
    **解码结果逐字相同** ⇒ 改探测顺序**不改变任何可观测量** ⇒ 判据是空靶。

    ## 正确观察量

    用**含中文的 UTF-8 文件**：

    - 正确顺序（`utf-8` 优先）⇒ 中文**正确还原**；
    - 只剩 `latin-1` ⇒ 同一串字节被**逐字节映射**成拉丁字符（乱码）⇒ 观察文本**不同**。

    ⇒ 这一差异正是「探测顺序生效」的**唯一可观察效果**，把 `M-回归检查` 的靶子立起来。
    ⚠️ 本阶段**两份探测实现**（工具族侧 vs `app.pipeline.parsing`）的代价如实登记在
    `read_file.probe_encoding()` 的 docstring：**可独立失效、不互相顶替** ——
    本腿只证明**工具族侧**那一份真在工作。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "cn.txt").write_bytes("第一行：中文内容\n第二行\n".encode("utf-8"))
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(ws))

    obs = _call(module_of("read_file"), path="cn.txt")
    assert obs.ok is True, obs.text
    assert "第一行：中文内容" in obs.text, (
        f"UTF-8 中文未被正确解码 ⇒ 探测顺序没生效（M-T3-16）：{obs.text!r}"
    )


def test_list_files_excludes_directories(in_workspace: Path) -> None:
    """**目录不进清单**（`D-回归检查` 写死）。"""
    obs = _call(module_of("list_files"))
    assert obs.ok is True, obs.text
    assert "a.txt" in obs.text, obs.text
    assert "b.txt" in obs.text, obs.text
    # ⚠️ 断言必须精确匹配「整行就是那个目录」，**不得**用 `"\n- sub" not in text` ——
    # 那会把合法的 `sub/b.txt` 一起误判（我第一版就踩了这个坑：子串匹配 ≠ 条目匹配）。
    entries = [line[2:] for line in obs.text.splitlines() if line.startswith("- ")]
    assert "sub" not in entries, f"目录 sub 不得作为条目进清单：{entries}"
    assert "sub/b.txt" in entries, f"子目录里的文件仍应列出：{entries}"


def test_list_files_glob_star_does_not_cross_slash_and_double_star_does(in_workspace: Path) -> None:
    """**`glob` = `fnmatch` 风格**：`*` **不跨 `/`**、`**` **才跨**（`D-回归检查`；**禁正则**）。"""
    single = _call(module_of("list_files"), glob="*.txt")
    assert single.ok is True, single.text
    assert "a.txt" in single.text, f"单层 * 应命中 a.txt：{single.text!r}"
    assert "sub/b.txt" not in single.text, f"* 不得跨目录（应只命中根层）：{single.text!r}"

    deep = _call(module_of("list_files"), glob="**/*.txt")
    assert deep.ok is True, deep.text
    assert "sub/b.txt" in deep.text, f"** 应跨目录命中 sub/b.txt：{deep.text!r}"


def test_d_t3_7_glob_bounded_semantics_are_pinned(tmp_path: Path, monkeypatch) -> None:
    """**「有界 globstar」的口径钉子**（Lead 裁决：有界口径必须有判据看守）。

    🔴 本工具的 `glob` 是**手写两指针通配**（**零 `re`**，符合 `D-回归检查` 的「禁正则」精神），
    但**只**实现下列**有界**语义，**不是**完整 globstar：

    | 形态 | 语义 | 依据 |
    |---|---|---|
    | 无 `**` | 按 `/` 切段、**段数必须相等**，逐段匹配 ⇒ `*` **不跨** `/` | `D-回归检查` 写死「`*` 不跨 `/`」 |
    | 有 `**` | 前缀**逐段**匹配 + 后缀在**剩余段里找连续窗口** | 有界、可读、零回溯 |

    ⚠️ **未实现**（如实登记，**不得**写成「完整 globstar」）：`**` 的**多级可变深度**组合
    （如 `a/**/b/**/c` 的第二个 `**`）、字符类 `[abc]`、`?`。
    """
    ws = tmp_path / "ws"
    (ws / "a" / "b").mkdir(parents=True)
    (ws / "a" / "x.c").write_text("x", encoding="utf-8")
    (ws / "a" / "b" / "y.c").write_text("y", encoding="utf-8")
    (ws / "top.txt").write_text("t", encoding="utf-8")
    (ws / "a" / "nested.txt").write_text("n", encoding="utf-8")
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(ws))

    # ① `*` **不跨** `/`：`*.txt` 只命中根层，不得命中 `a/nested.txt`
    single = _call(module_of("list_files"), glob="*.txt")
    assert single.ok is True, single.text
    assert "top.txt" in single.text, f"*.txt 应命中根层 top.txt：{single.text!r}"
    assert "a/nested.txt" not in single.text, f"* 不得跨 / ：{single.text!r}"

    # ② `**` **才跨**：`**/*.txt` 必须命中 `a/nested.txt`
    deep = _call(module_of("list_files"), glob="**/*.txt")
    assert deep.ok is True, deep.text
    assert "a/nested.txt" in deep.text, f"** 应跨目录命中 a/nested.txt：{deep.text!r}"

    # ③ 带目录前缀的单层 `*`：命中 `a/x.c`，**不**命中 `a/b/y.c`
    scoped = _call(module_of("list_files"), glob="a/*.c")
    assert scoped.ok is True, scoped.text
    assert "a/x.c" in scoped.text, f"a/*.c 应命中 a/x.c：{scoped.text!r}"
    assert "a/b/y.c" not in scoped.text, "a/*.c 不得命中 a/b/y.c（* 不跨 /）：" + scoped.text

# ═══════════════════════════════════════════════════════════════════════════
# 回归检查 · 敏感路径七清单（第 3 笔）
# ═══════════════════════════════════════════════════════════════════════════
#
# **产品面零新代码**：七张清单**早已**全部接进 `_paths.classify()`（阶段 1 交付），
# 本笔只**落判据**把它们钉住（`A6`），并接住阶段 2 移交的三条。
#
# ## `A6` = **七张清单各至少一个样本**（包 v1.2 明确：v1.0 只断 4 个 ⇒ 清空其余三张仍绿）
#
# | 清单 | 判定位置（`_paths.py`） | 本文件样本 |
# |---|---|---|
# | `SYSTEM_TOP_SEGMENTS` | `_lexical_layers` ST6c | `C:\Windows\x.txt` |
# | `DANGEROUS_SUFFIXES` | ST9 | `run.bat` |
# | `SENSITIVE_SEGMENTS` | ST10 | `.git/config` |
# | `SENSITIVE_PREFIXES` | ST10 | `id_rsa` |
# | `SENSITIVE_SUFFIXES` | ST10 | `k.pem` |
# | `SENSITIVE_FILENAMES` | ST10 | `sitecustomize.py` |
# | `STARTUP_HOOK_FILENAMES` | ST9 | `conftest.py` |
#
# ⚠️ **一处的文案归属（实测，勿误判为错位）**：`SYSTEM_TOP_SEGMENTS` 走 ST6c 分支，
# 其文案 = `PATH_OUTSIDE_TEMPLATE`（「不在工作区根内」），**不是** `PATH_SENSITIVE_TEMPLATE` ——
# 该分支只对「盘符绝对路径 + 首段是系统目录 + 候选不在根内」生效（相对路径的 `Users/a.txt`
# 指的是**根内**的 `Users`，**不得**按系统目录拒）。

#: `A6` 的七张清单样本：`(清单名, 会被该清单拒的样本, 是否落 PATH_SENSITIVE_TEMPLATE)`。
#: ⚠️ 每张清单都必须有样本 —— 清空任何一张，对应那一条腿**必红**（这就是 `M-回归检查` 的靶面）。
SEVEN_LIST_SAMPLES: tuple[tuple[str, str, bool], ...] = (
    ("SYSTEM_TOP_SEGMENTS", "C:\\\\Windows\\\\x.txt", False),
    ("DANGEROUS_SUFFIXES", "run.bat", True),
    ("SENSITIVE_SEGMENTS", ".git/config", True),
    ("SENSITIVE_PREFIXES", "id_rsa", True),
    ("SENSITIVE_SUFFIXES", "k.pem", True),
    ("SENSITIVE_FILENAMES", "sitecustomize.py", True),
    ("STARTUP_HOOK_FILENAMES", "conftest.py", True),
)


def test_a6_all_seven_lists_are_present_and_nonempty() -> None:
    """**`A6` 的前置腿**：七张清单**都在**且**非空**。

    ⚠️ 这是**必要不充分**：本腿只证明「清单非空」，下面几条才是「真被接进判定链」。
    两者并存的原因：只在下面断样本时，若有人**删掉整张清单常量**，会在别处以 `AttributeError`
    形态炸出噪音红；先断非空，能把「清单被清空」与「清单没接进判定链」**两种失败区分开**。
    """
    for label, _sample, _template in SEVEN_LIST_SAMPLES:
        value = getattr(P, label)
        assert value, label + " 不得为空"
        assert len(value) > 0, label + " 不得为空清单"


def test_a6_each_list_rejects_through_classify(in_workspace: Path) -> None:
    """**`A6` 主腿**：七张清单**各至少一个样本** ⇒ 经 `classify()` 全判 `Reject`。

    🔴 **观察量 = `classify()` 的判定结果**（不是「常量里有没有那个字符串」）——
    后者是**存在性断言**，删掉判定链里的分支它照样绿（阶段 2 的 `A-2` 同型教训）。
    """
    for label, sample_path, _template in SEVEN_LIST_SAMPLES:
        verdict = P.classify(sample_path, P.resolve_workspace_root())
        assert isinstance(verdict, P.Reject), (
            label + " 的样本 " + repr(sample_path) + " 未被拒 ⇒ 该清单没接进判定链：" + repr(verdict)
        )
        assert verdict.reason, label + " 的 Reject 必须带中文原因（I5 形态）"


def test_a6_three_tools_reject_each_of_the_seven_lists(in_workspace: Path) -> None:
    """**`A6` 的端到端腿**：七张清单的样本**走到三工具上**都 `ok=False` + **中文可读**。

    🔴 为什么必须有这条（而不止 `classify()` 那条）：`classify` 判 `Reject` **不等于**
    工具把它**转成可读失败观察** —— 中间还隔着「工具是否真调 `classify` 并消费 `Reject`」。
    三种工具**各自**跑一遍（工具是三条独立的产品路径，`grep_files` 的 `path` 还是**可选**的
    ⇒ 它走的是另一支）。
    """
    def _is_chinese(text: str) -> bool:
        return any("\u4e00" <= ch <= "\u9fff" for ch in text)

    for label, sample_path, _template in SEVEN_LIST_SAMPLES:
        obs_read = _call(module_of("read_file"), path=sample_path)
        assert obs_read.ok is False, "read_file 未拒 " + label + " 样本 " + repr(sample_path) + "：" + repr(obs_read.text)
        assert _is_chinese(obs_read.text), "read_file 的失败文案必须中文可读：" + repr(obs_read.text)

        obs_grep = _call(module_of("grep_files"), pattern="x", path=sample_path)
        assert obs_grep.ok is False, "grep_files 未拒 " + label + " 样本 " + repr(sample_path) + "：" + repr(obs_grep.text)
        assert _is_chinese(obs_grep.text), "grep_files 的失败文案必须中文可读：" + repr(obs_grep.text)

        obs_list = _call(module_of("list_files"), path=sample_path)
        assert obs_list.ok is False, "list_files 未拒 " + label + " 样本 " + repr(sample_path) + "：" + repr(obs_list.text)
        assert _is_chinese(obs_list.text), "list_files 的失败文案必须中文可读：" + repr(obs_list.text)


def test_a6_six_lists_use_the_sensitive_template_the_seventh_uses_outside(in_workspace: Path) -> None:
    """**文案归属的可判钉子**（防下一手把 ST6c 的文案误判成「写错了」）。

    六张走 `PATH_SENSITIVE_TEMPLATE`（「命中敏感路径清单」），
    `SYSTEM_TOP_SEGMENTS` 走 `PATH_OUTSIDE_TEMPLATE`（「不在工作区根内」）——
    这条差异是**现码事实**（ST6c 与 ST9/ST10 是两条不同分支），**不是缺陷**。

    ⚠️ **必须挂 `in_workspace`**（本笔实测踩过）：不配根时 `resolve_workspace_root()` 返 `None`，
    `classify` 会在 **`:473-474` 的 root 分支提前返回** `PATH_NO_ROOT_TEMPLATE`（「未配置工作区根…」）
    ⇒ 七张清单**一条都走不到**，本腿测的就变成了「未配置」那一条分支（**假红 + 观察量错位**）。
    """
    root = P.resolve_workspace_root()
    for label, sample_path, uses_sensitive_template in SEVEN_LIST_SAMPLES:
        verdict = P.classify(sample_path, root)
        assert isinstance(verdict, P.Reject), (label, sample_path)
        if uses_sensitive_template:
            assert verdict.reason == P.PATH_SENSITIVE_TEMPLATE.format(path=sample_path), (
                label + " 应走 PATH_SENSITIVE_TEMPLATE：" + repr(verdict.reason)
            )
        else:
            assert verdict.reason == P.PATH_OUTSIDE_TEMPLATE.format(path=sample_path), (
                label + " 应走 PATH_OUTSIDE_TEMPLATE（ST6c 分支）：" + repr(verdict.reason)
            )


def test_a6_deliberate_user_visible_rejections_are_documented(tmp_path: Path, monkeypatch) -> None:
    """**两条刻意的、用户可见的拒绝**（包 `D-回归检查` 要求写明，否则被当 bug）。

    ① `SENSITIVE_SEGMENTS` 含 `data` / `alembic` ⇒ **名为 `data` 或 `alembic` 的任何目录都被拒**；
    ② `STARTUP_HOOK_FILENAMES` 含 `conftest.py` / `.pth` / `usercustomize.py`
       ⇒ **普通项目的 `conftest.py` 读不了**。
    ⇒ 两类都**刻意的**（工作区不该成为解释器级自动加载面 / 数据根面），**不得放宽**。
    """
    ws = tmp_path / "ws"
    (ws / "data").mkdir(parents=True)
    (ws / "data" / "note.txt").write_text("payload", encoding="utf-8")
    (ws / "alembic").mkdir()
    (ws / "alembic" / "env.py").write_text("x", encoding="utf-8")
    (ws / "conftest.py").write_text("y", encoding="utf-8")
    (ws / "legit.txt").write_text("ok", encoding="utf-8")
    (ws / "harmless.sh").write_text("echo hi", encoding="utf-8")
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(ws))

    for seg in ("data/note.txt", "alembic/env.py"):
        obs = _call(module_of("read_file"), path=seg)
        assert obs.ok is False, seg + " 必须被拒（SENSITIVE_SEGMENTS 含 data/alembic，刻意）：" + repr(obs.text)

    obs_cfg = _call(module_of("read_file"), path="conftest.py")
    assert obs_cfg.ok is False, "conftest.py 必须被拒（STARTUP_HOOK_FILENAMES，刻意）：" + repr(obs_cfg.text)

    assert _call(module_of("read_file"), path="legit.txt").ok is True
    assert _call(module_of("read_file"), path="harmless.sh").ok is True, (
        ".sh 不在 DANGEROUS_SUFFIXES 里 ⇒ 不该被拒（实测口径）"
    )


# ── 阶段 2 移交 ①（A-4b）：后缀不承担密钥语义 ────────────────────────────────


def test_a4b_suffixes_do_not_carry_secret_semantics(tmp_path: Path, monkeypatch) -> None:
    """**`A-4b`**（阶段 2 登记件 `:215`）：`.json` / `.ini` / `.cfg` **本身不因后缀被拒**。

    ## 成文口径（本判据的依据，写死）

    > **后缀不承担密钥语义；密钥判定走 `SENSITIVE_*` 三清单。**

    `.json` / `.ini` / `.cfg` 是**常见**密钥载体，但**不是**充分条件 —— 它们同样是
    **完全合法的配置文件**（且刚被阶段 2 纳入**入库白名单**：`pipeline/parsing.py` 的配置后缀 7 项含三者）。
    ⇒ **不得**把它们加进**任何后缀清单** —— 那会**误拒合法配置文件**，且与阶段 2 的入库白名单**直接冲突**。

    ## 两个方向都要断（缺一不可）

    ① **不被后缀拒**：三者判 `Normal`（**不能**因为「可能是密钥」就一刀切）；
    ② **同目录的 `.env` 必须被拒**：真正的密钥载体由 `SENSITIVE_SEGMENTS`（含 `.env`）承担
       ⇒ **「不因后缀拒」不等于「放行密钥」**，这一腿证明防线仍在（否则本条会被读成「放宽了」）。
    """
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    for name in ("config.json", "app.ini", "setup.cfg"):
        (ws / name).write_text("plain", encoding="utf-8")
        (ws / "sub" / name).write_text("plain", encoding="utf-8")
    (ws / ".env").write_text("KEY=1", encoding="utf-8")
    (ws / "sub" / ".env").write_text("KEY=1", encoding="utf-8")
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(ws))

    for name in ("config.json", "app.ini", "setup.cfg"):
        for rel in (name, "sub/" + name):
            verdict = P.classify(rel, P.resolve_workspace_root())
            assert isinstance(verdict, P.Normal), (
                rel + " 不该因后缀被拒（后缀不承担密钥语义）：" + repr(verdict)
            )
            obs = _call(module_of("read_file"), path=rel)
            assert obs.ok is True, rel + " 应可读：" + repr(obs.text)

    for rel in (".env", "sub/.env"):
        verdict = P.classify(rel, P.resolve_workspace_root())
        assert isinstance(verdict, P.Reject), rel + " 必须被拒（SENSITIVE_SEGMENTS 含 .env）"
        assert _call(module_of("read_file"), path=rel).ok is False


def test_a4b_secret_semantics_live_only_in_the_sensitive_lists() -> None:
    """**`A-4b` 的结构腿**：密钥语义**只**在 `SENSITIVE_*` 清单里，**不在**任何**后缀**清单里。

    🔴 **为什么必须单独断这一条**：上面那条断的是**行为**（`.json` 没被拒）；
    若有人把 `.json` 加进 `SENSITIVE_SUFFIXES` 而**同时**把 `SENSITIVE_SUFFIXES` 从判定链里摘掉，
    行为腿**仍绿**、防线却没了。本腿把「清单**本体**」钉住 ⇒ 两腿正交。

    `DANGEROUS_SUFFIXES` 也一并核：它 20 项**全为可执行 / 脚本**（实测），
    **不得**混入配置后缀或密钥后缀（那是两类完全不同的风险面）。
    """
    for suffix in (".json", ".ini", ".cfg"):
        assert suffix not in P.SENSITIVE_SUFFIXES, suffix + " 不得进 SENSITIVE_SUFFIXES（会误拒合法配置）"
        assert suffix not in P.DANGEROUS_SUFFIXES, suffix + " 不得进 DANGEROUS_SUFFIXES"

    assert P.SENSITIVE_SUFFIXES, "SENSITIVE_SUFFIXES 不得为空"
    assert P.SENSITIVE_PREFIXES, "SENSITIVE_PREFIXES 不得为空"
    assert P.SENSITIVE_FILENAMES or P.SENSITIVE_SEGMENTS, "敏感清单不得全空"


# ── 阶段 2 移交 ②（R-02）：两后缀清单互不派生 ──────────────────────────────


def test_r02_two_suffix_lists_are_independent() -> None:
    """**`R-02`**（阶段 2 登记件 `:42-43`）：`DANGEROUS_SUFFIXES` 与 `SUPPORTED_SUFFIXES`
    **互不派生、各自独立成文**。

    ## 为什么是「互不派生」而不是「对齐」

    两者服务**两个相反的目标**：

    - `DANGEROUS_SUFFIXES`（工具边界层）⇒ **拒绝**可执行 / 脚本落盘面（20 项）；
    - `SUPPORTED_SUFFIXES`（入库白名单）⇒ **允许**被解析入库的文档后缀（44 项）。

    企图「统一」会**同时打坏两个边界**：对齐到拒绝侧 ⇒ 大量合法文档进不了库；
    对齐到允许侧 ⇒ 工作区放行可执行载荷。

    ## 交集是**实测实算**（不是推导）

    `DANGEROUS_SUFFIXES`(20) ∩ `SUPPORTED_SUFFIXES`(44) = **`{".js"}`** —— **只有 `.js`**。
    ⚠️ `.sh` 在 `SUPPORTED_SUFFIXES` 里但**不在** `DANGEROUS_SUFFIXES` 里
    （包 v1.2 合稿时已更正草案的 `{".js", ".sh"}`）。
    ⚠️ **测试侧 import `app.pipeline.parsing` 不触 `A17`** —— `A17` 管的是**产品模块的 import 面**
    （`D-回归检查` 取甲的约束落在**工具模块**上）；本判据是**纯静态常量关系**的测试，读两侧清单即可，
    **不引入任何跨层边到产品代码里**。
    """
    from app.pipeline.parsing import SUPPORTED_SUFFIXES

    dangerous = P.DANGEROUS_SUFFIXES
    supported = SUPPORTED_SUFFIXES

    assert dangerous and supported, (len(dangerous), len(supported))
    assert not (dangerous <= supported), "DANGEROUS_SUFFIXES 派生成 SUPPORTED_SUFFIXES 的子集了"
    assert not (supported <= dangerous), "SUPPORTED_SUFFIXES 派生成 DANGEROUS_SUFFIXES 的子集了"
    assert dangerous & supported == {".js"}, sorted(dangerous & supported)
    assert ".sh" not in dangerous, ".sh 实测不在 DANGEROUS_SUFFIXES 里（包 v1.2 更正）"
    assert ".sh" in supported, ".sh 实测在 SUPPORTED_SUFFIXES 里"


# ── 阶段 2 移交 ③（A-4）：symbol_path 读侧展示 —— **显式登记移交** ────────────


def test_a4_symbol_path_read_side_is_explicitly_deferred() -> None:
    """**`A-4` 的显式登记**（阶段 2 登记件 `:214`）：`symbol_path` 读侧展示**本阶段未覆盖**。

    ## 为什么本阶段**不接**（写死理由，不是「忘了」）

    本阶段三工具（`list_files` / `read_file` / `grep_files`）**均按文件系统读、不读 `chunks` 表**
    ⇒ 「读侧展示 `symbol_path`」在产品面上**无落点**；强行接住会造出**无人消费的字段展示**。

    ## 处置去向（**不得**再以「归阶段 3」这一已到期却无落点的措辞挂着）

    ⇒ 「`symbol_path` 的读侧展示**未在本阶段覆盖**，处置去向 = `回归检查` 的来源标注面 / 阶段 4」。

    ## 本腿的可判形态

    断「**写侧齐备、读侧确无**」这一现况：
    ① `read_document.py` 的 select **不含** `Chunk.symbol_path`（读侧无展示）；
    ② 三工具模块**不 import 任何 `chunks` / 模型层**（语义层：它们读的是文件系统，不是库表）。
    ⚠️ 本腿**只登记现况**，**不**断言「将来不得接」 —— 移送阶段 4 的判定腿。
    """
    rd_src = (TOOLS_DIR / "read_document.py").read_text(encoding="utf-8")
    assert "symbol_path" not in rd_src, (
        "read_document 若已接 symbol_path 读侧展示 ⇒ 本登记件过期，须更新移交状态"
    )

    for name in sorted(THREE_NAMES):
        targets = _imported_modules(TOOLS_DIR / (name + ".py"))
        assert not any(item.startswith("app.models") for item in targets), (
            name + " 不得 import app.models（本阶段三工具按文件系统读）：" + repr(sorted(targets))
        )
        assert not any(item.startswith("app.db") for item in targets), (
            name + " 不得 import app.db：" + repr(sorted(targets))
        )


# ── 补 task-2 遗留的回归钉（**不计入 task-3 的原始交付计数**）─────────────────────────
#
# 背景（执行期发现 · 真缺陷，Lead 裁决归入本笔）：
# `list_files` **省略 `path`**（= 列工作区根目录，**最基础的合法调用**）**曾被自己的词法层误拒**
# （`classify("")` ⇒ `Reject 路径越界：路径为空，已拒绝。`）。修法 = `list_files.py:237` 的 `if rel != ROOT_LABEL:` 守卫。
# ⇒ 修后**零回归钉**，且既有两条 `list_files` 腿**都显式传了 `path`**。
#
# ⚠️ **为何越界类判据结构性抓不到它**：`A6` / `D-回归检查` 全在断「**非法路径被拒**」，
# 而本缺陷的形态是「**合法调用被误拒**」—— **方向相反**。
#
# ⚠️ **两条腿方向相反、不可互相代替**：
# - 只断「省略 path 成功」⇒ 抓不到**守卫被删**（那时省略 path 会变红……但若人改成「空串就跳过」又会绿，睡着的缺陷会回归）；
# - 只断「显式 `.` 走 classify」⇒ 抓不到「省略 path 被误拒」。


def _spy_classify(monkeypatch, module):
    """把 `list_files` **模块命名空间里**的 `classify` 换成记录档（返回调用参数列表）。

    ⚠️ **打的是「消费点的名字」，不是 `_paths` 里的原件**：`list_files.py` 用
    `from app.agent.tools._paths import ... classify ...` 把它引入了**自己的模块命名空间** ⇒ 必须 `setattr` 到
    `list_files` 模块上，改 `_paths.classify` **无效**。本函数就是为了把这个坑写在可执行代码里。
    """
    calls: list[tuple] = []
    real = module.classify

    def _spy(path, root):  # noqa: ANN001
        calls.append((path, root))
        return real(path, root)

    monkeypatch.setattr(module, "classify", _spy)
    return calls


def test_list_files_omitting_path_lists_the_root(tmp_path: Path, monkeypatch) -> None:
    """**回归钉 ①·正向腿**：省略 `path` ⇒ `ok=True` **且真列出根下条目**。

    🔴 **断内容，不只断 `ok`**：`ok=True` 单独成立会夹带「根被当成空目录」这种假绿。
    """
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "top.txt").write_text("t", encoding="utf-8")
    (ws / "sub" / "inner.txt").write_text("i", encoding="utf-8")
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(ws))

    obs = _call(module_of("list_files"))
    assert obs.ok is True, "省略 path（= 列根目录）不得被拒：" + repr(obs.text)
    assert "top.txt" in obs.text, "省略 path 应真列出根下 top.txt：" + repr(obs.text)
    assert "sub/inner.txt" in obs.text, "省略 path 应连子目录里的文件一并列出：" + repr(obs.text)
    assert obs.materials == [], "仍须零材料（A15）"


def test_list_files_root_path_guard_calls_and_skips_classify_correctly(in_workspace: Path, monkeypatch) -> None:
    """**回归钉 ②·反向腿**：守卫**不得**被过度放宽，且**跳过面要写清边界**。

    ## 🔴 一条**实测登记的覆盖面缺口**（定性：边界面收窄，**非安全漏洞**）

    显式 `path="."` **与省略 `path` 折叠进同一跳过分支** ——
    `list_files.py:232` 的 `rel = (args.path or "").strip() or ROOT_LABEL` 与
    `:64` `ROOT_LABEL = "."`、`:237` `if rel != ROOT_LABEL:` **三处同值** ⇒ `"."` 落到
    「与省略 path 完全同一分支」⇒ **不过边界层**。

    **定性依据（实测，不是推断）**：

    | 输入 | `classify` 判定 | 说明 |
    |---|---|---|
    | `"."` | **`Normal`** | 跳过它**放行不了任何非法输入** ⇒ 覆盖面缺口，**不是**边界被突破 |
    | `"./"` | **`Normal`** | 但它**走** classify（`rel` 为 `"./"` ≠ `"."`）⇒ 见下对照 |
    | `".."` / `"../x"` | **`Reject`** | 越界形态**仍被拒**（跳过面不含它们） |
    | `""` | **`Reject`** | 这才是当初「省略 path 被误拒」的根因 |

    **处置：如实登记 + 移交阶段 5**（写通道会给边界层加重负载，届时 `_paths` 的消费点重排，一并收口）。
    **本笔不修**：`回归检查` = 产品面**零新代码**；且改 `list_files.py` 属**跨笔改 task-2 已提交件**（违「一笔一条目」）。

    ## 本腿断四个方向（缺一不可）

    | 调用 | `classify` 调用次数 | 意义 |
    |---|---|---|
    | 省略 `path` | **0** | 守卫真生效（**这正是修 bug 的机制**） |
    | `path="."` | **0** | 🔴 **记录当前口径**（见上：与省略同支）—— **不是**「期望它永远这样」 |
    | `path="./"` | **1** | 守「**不是**凡点开头都跳过」（`"./"` 实测过 classify） |
    | `path="a.txt"` | **1** | 对照（真给路径照常判） |

    🔴 **`"."` 那条腿为什么必须留下**：若将来有人收紧守卫（让 `"."` 也走 classify），
    这条腿**会红** ⇒ 那**正是我们想要的「口径变更提醒」**。
    ⇒ 它的语义是「**记录当前口径**」，不是「断言它永久成立」——改动它的人应**同笔更新本条 docstring**。
    """
    import app.agent.tools.list_files as lf_mod

    ws_calls = _spy_classify(monkeypatch, lf_mod)

    # ① 省略 path ⇒ **不调 classify**（守卫生效；这正是「列根目录」被误拒的修法）
    obs_default = _call(module_of("list_files"))
    assert obs_default.ok is True, obs_default.text
    assert ws_calls == [], "省略 path 时不得调 classify（否则路径为空会把合法调用误拒）：" + repr(ws_calls)

    # ② 显式 path="." ⇒ **也是 0 次**：与省略 path 折叠进同一分支（**记录当前口径**，见 docstring）
    obs_dot = _call(module_of("list_files"), path=".")
    assert obs_dot.ok is True, obs_dot.text
    assert ws_calls == [], (
        "当前口径：path='.' 与省略 path 同支 ⇒ **不过** classify（边界面收窄，非安全漏洞）。"
        "若有人收紧了守卫使其走 classify ⇒ 本腿会红，属**预期的口径变更提醒**，"
        "改动者须同笔更新本用例 docstring："
        + repr(ws_calls)
    )

    # ③ **`"./"` ⇒ 恰 1 次**：守「不是凡点开头都跳过」（这是本腿的判别力所在）
    obs_dotslash = _call(module_of("list_files"), path="./")
    assert obs_dotslash.ok is True, obs_dotslash.text
    assert len(ws_calls) == 1, (
        "显式 path='./' 必须走 classify（恰 1 次）：守卫不得被写成「凡点开头都跳过」：" + repr(ws_calls)
    )
    assert ws_calls[0][0] == "./", ws_calls

    # ④ 对照：真给一个**目录**路径 ⇒ 照常调 classify
    # ⚠️ 必须用**目录**：`list_files` 的 `path` 语义是「列该目录」，给普通文件会走
    # `ROOT_UNUSABLE_MESSAGE`（"工作区根不可用（不是目录）"）——本笔实测踩过，故此处用 `sub`。
    ws_calls.clear()
    obs_dir = _call(module_of("list_files"), path="sub")
    assert obs_dir.ok is True, obs_dir.text
    assert len(ws_calls) == 1, ws_calls
    assert ws_calls[0][0] == "sub", ws_calls

# ═══════════════════════════════════════════════════════════════════════════
# 回归检查 · 两源来源标注（第 4 笔）
# ═══════════════════════════════════════════════════════════════════════════
#
# **产品面零新代码**：前缀常量与三工具的前缀已随 **task-2**（`2fa080d`）落地
# （Lead 裁决取甲：`_source.py` 提前到 `回归检查`）。本笔**只落判据载体**。
#
# ## 为什么是「互斥性」而不是「两源同框对比」
#
# ```
# 本阶段三个新工具全是磁盘工具；库内源是既有的 `read_document` —— 五笔交付无一要求改它
# ⇒ `A13f` 的「两源互斥」在本阶段构造不出来（需一次调用同时触达两源，属阶段 4 的端到端腿）
# ⇒ 本笔只断：**磁盘源标注存在** 且 **库内源不含该前缀** ⇒ 互斥性**可判**。
# ```
#
# ## 🔴 本笔四条腿**各自**承担前提（不靠某个 fixture 顺带配上）
#
# 来自 task-5 的实测教训（他踩过两次）：**腿① 与 腿② 都依赖「已配根」，但只把三态口径写进了腿② ⇒ 腿① 因未配根而观察量不敏感**（变异第一轮 SURVIVED）。
# ⇒ 本文件凡「真调三工具」的腿（**腿 B、腿 C**）**各自** `monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(tmp_path))`，
# 且**各自断言「三件真在工具表里」**（否则未配根时它们根本不在清单，走不到前缀。
#
# ## 🔴 引用任何基准前先「实测它到底是什么」（本笔实测读数附与此）
#
# ```
# _source.DISK_SOURCE_PREFIX = '【磁盘】'  (len=4, repr 实测)
# _source.__all__            = ['DISK_SOURCE_PREFIX']    ⇐ 唯一对外名
# read_document 是否有同名常量：hasattr(RD, 'DISK_SOURCE_PREFIX') = False
# 三工具配根后观察文本里该前缀的出现次数：各 1 次
# read_document.render_observation(...) 产出 = '块号区间（第 1–1 块 / 共 1 块）\nhello body'  ⇐ 不含前缀
# ```
#
# ⚠️ **断产品侧值一律带模块前缀**（`_source.DISK_SOURCE_PREFIX`）—— 不用字面量，
# 避免「本地镜像常量 vs 产品侧同名两对象」的同型坑（task-5 踩过）。


def test_a13f_disk_source_prefix_is_mutually_exclusive_with_the_library_source(tmp_path: Path, monkeypatch) -> None:
    """**`A13f` 互斥性腿（双向）**：磁盘源**含**前缀，库内源**不含**前缀。

    ## 为什么库内侧用 `render_observation` 而**不是**调 handler

    🔴 `read_document` 的 handler **需要 DB 会话**（无 `db` 时在其 `:181-182` 返
    「读取文档缺少数据库会话」的**失败观察**）⇒ 走 handler 只能拿到失败文本，
    **而本条要断的是「成功路径的观察文本不含磁盘前缀」** ⇒ 观察量**错位**
    （那红与互斥性**无关**）。

    `render_observation`（`:128-143`）是**产品侧同一份**拼装实现，且是 handler **成功路径唯一的文本出口**
    （`:345-351`）⇒ 用它断「库内源不含磁盘前缀」是**打在产品实现上**的，不是自造字符串。

    ## 🔴 为什么这条腿**独立复跑**、不引用任何「由上笔已证」

    本腿自建夹具、自调工具，只依赖产品侧常量与两侧真实实现。
    """
    import app.agent.tools.read_document as rd_mod

    prefix = _source.DISK_SOURCE_PREFIX
    assert prefix, "前缀常量不得为空（空串会让下面的 in 恒真）"

    # ① **磁盘侧：真调三工具**（各自配根，不靠外部 fixture）
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(tmp_path))
    ws_names = _spec_names()
    assert THREE_NAMES <= ws_names, f"本腿前提：配根后三件必须在工具表里：{sorted(ws_names)}"
    (tmp_path / "a.txt").write_text("alpha beta\n", encoding="utf-8")
    for label, obs in (
        ("list_files", _call(module_of("list_files"))),
        ("read_file", _call(module_of("read_file"), path="a.txt")),
        ("grep_files", _call(module_of("grep_files"), pattern="alpha")),
    ):
        assert obs.ok is True, label + " 应成功：" + repr(obs.text)
        assert prefix in obs.text, label + " 的观察文本必须含磁盘源前缀：" + repr(obs.text)

    # ② **库内侧：真调产品的 render_observation**（纯函数，零 DB）
    library_text = rd_mod.render_observation(start=1, end=1, total=1, body="hello body", remaining=0)
    assert library_text, "render_observation 不得返空（否则下面的互斥断言退化成恒真）"
    assert prefix not in library_text, (
        "库内源（read_document）的观察文本不得出现磁盘源前缀（互斥性）：" + repr(library_text)
    )
    # ③ 互斥性的**实质性**：两侧都不是空串的退化形态
    assert library_text != obs.text, "两源观察文本不得逐字相同（那会让「來源可分辨」失效）"


def test_a19_disk_prefix_really_appears_in_each_tool(tmp_path: Path, monkeypatch) -> None:
    """**`A19` 非存在性腿**：**真调**三工具，断观察文本里**真出现**前缀。

    🔴 **与 task-2 那条前置腿的区别**：task-2 断的是「**常量存在**」
    （`_source.DISK_SOURCE_PREFIX == "【磁盘】"`）—— 那是**存在性断言**；本腿断的是
    「**工具产出的文本里真出现它**」⇒ 删掉三工具里的前缀拼接而保留常量，**本腿会红、前置腿仍绿**（`M-回归检查` 的靶面）。

    ## 🔴 本腿**自己**配根 + 自己断「三件在表里」

    三个新工具由 `bootstrap.build_registry()` 的**注册门**支配（`resolve_workspace_root() is None`
    ⇒ 不注册）⇒ **不配根就根本不在工具表里、走不到前缀**；
    而不配根时调 handler 会返「未配置工作区根」失败观察 ⇒ **观察量错位**（不是本腿要断的东西）。
    ⇒ **本腿自己 `monkeypatch` 配根**（红线 12：用 `tmp_path`），并**自己**断言三件在表里。
    """
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(tmp_path))
    assert THREE_NAMES <= _spec_names(), "本腿前提：配根后三件必须在工具表里"
    (tmp_path / "a.txt").write_text("alpha beta\n", encoding="utf-8")

    prefix = _source.DISK_SOURCE_PREFIX
    seen: dict[str, int] = {}
    for label, obs in (
        ("list_files", _call(module_of("list_files"))),
        ("read_file", _call(module_of("read_file"), path="a.txt")),
        ("grep_files", _call(module_of("grep_files"), pattern="alpha")),
    ):
        assert obs.ok is True, label + " 应成功：" + repr(obs.text)
        seen[label] = obs.text.count(prefix)
        assert seen[label] >= 1, label + " 的观察文本里必须**真出现**前缀：" + repr(obs.text)
    # 三条腿各自独立成立（不是「只要有一个对就行」的弱断言）
    assert set(seen) == {"list_files", "read_file", "grep_files"}, seen


def test_a19_prefix_is_substantively_present_not_a_vacuous_substring(tmp_path: Path, monkeypatch) -> None:
    """**`A19` 的负向腿**：前缀是**实质出现**，不是空串包含的退化形态。

    🔴 **为什么必须单独断这一条**：若前缀常量被改成**空串**，
    那么 `prefix in obs.text` 恒为 `True` ⇒ **上面那条能过，但标注已经没了**。
    本腿把这个退化形态挡住（也是 `M-回归检查` 第一向的靶面）。
    """
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "a.txt").write_text("alpha beta\n", encoding="utf-8")

    prefix = _source.DISK_SOURCE_PREFIX
    assert prefix.strip(), "前缀不得为空（空串会让 `in` 恒真）：" + repr(prefix)

    obs = _call(module_of("read_file"), path="a.txt")
    assert obs.ok is True, obs.text
    # 实质包含：去掉前缀后文本**必须变**（空串包含时不变）
    assert obs.text.replace(prefix, "") != obs.text, (
        "前缀在观察文本里必须是**实质出现**（去掉它文本要变）：" + repr(obs.text)
    )
    # 前缀应在**开头**（来源标注的语义：先告知来源，再给内容）
    assert obs.text.startswith(prefix), "磁盘源前缀应在观察文本开头：" + repr(obs.text[:40])


# ── 起总腿：前缀的**唯一来源** + 空结果分支────────────────────────────────
#
# 🔴 这两条腿是**变异驱动补上的**（实测，不是设计时就有）：
# 
# `M-回归检查 ②`（常量**保留**、把一处字符串**内联同一值**）**第一轮 SURVIVED**：
# 它打的是 `list_files` 的「**没有匹配的文件**」分支，而我当时的四条腿**一条都没走到那个分支**
# （它们用的是有命中的工作区）⇒ **观察量盖不到被测点**。
# ⇒ 按纪律（「不许登记『口径例外」，回去改观察量或补锚点」），补了下面**腿 E**（空结果分支也必须带前缀）。
# 另补**腿 F**：前缀的**唯一来源**面（三工具模块不得出现该中文字面量）—— 这是「内联」这一类改法的**直接靶子**。


def test_a19_prefix_appears_even_on_the_empty_result_branch(tmp_path: Path, monkeypatch) -> None:
    """**空结果分支也必须带前缀**（变异驱动补上的腿）。

    ## 为什么单独一条（实测教训）

    `list_files` 在**空结果**时走的是**另一条分支**：
    `DISK_SOURCE_PREFIX + "工作区文件（根：" + ... + "）：没有匹配的文件"`。
    原先四条腿用的都是**有命中**的工作区 ⇒ 该分支**零覆盖** ⇒ `M-回归检查 ②`（打在该分支）**SURVIVED**。
    补上本腿后它被杀。

    ⚠️ 这也是「**同一前提各自钉**」的同族：**同一属性（“观察文本带前缀”）在不同分支上是不同的代码路径**，
    只断一条分支 = 只盖一部分实现。
    """
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(tmp_path))
    assert THREE_NAMES <= _spec_names(), "本腿前提：配根后三件必须在工具表里"
    # 空根：一个文件都没有 ⇒ 必定走「没有匹配的文件」分支
    obs = _call(module_of("list_files"))
    assert obs.ok is True, obs.text
    assert "没有匹配" in obs.text, "本腿要求走到空结果分支：" + repr(obs.text)
    assert _source.DISK_SOURCE_PREFIX in obs.text, (
        "**空结果分支也必须带磁盘源前缀**（变异 M-T3-14 ② 打的就是这里）：" + repr(obs.text)
    )


def test_a19_prefix_has_a_single_source_no_inlined_literal_in_tools() -> None:
    """**前缀的唯一来源**：三工具模块**不得出现该中文字面量**（变异驱动补上的腿）。

    ## 为什么必须单独一条

    `A19` 的主体腿断的是「**观察文本里真出现前缀**」—— 那是**行为面**；
    但 `M-回归检查 ②`（常量**保留**、字符串**内联同一值**）的行为**逐字不变** ⇒ 行为腿**照样绿**。
    ⇒ 只有断「**最初一份常量才是唯一来源**」才能杀它。

    本腿 = **结构化断言**：三个工具模块的源码里**不得出现该字面量**（它们必须经 `_source` 引入）。
    ⚠️ 用的是「**字面值不得出现**」这一结构化形态，**不是**「搜 `DISK_SOURCE_PREFIX` 关键字」的存在性断言
    （后者改名即绕过 —— 阶段 2 的 `A-2` 同型教训）。
    同时正向断：三个模块**都真 import 了** `_source` 的那个名字。
    """
    literal = _source.DISK_SOURCE_PREFIX
    assert literal, "前缀不得为空"
    for name in sorted(THREE_NAMES):
        src = (TOOLS_DIR / (name + ".py")).read_text(encoding="utf-8")
        assert literal not in src, (
            name + ".py 不得出现前缀的**字面值**（必须经 _source 引入，否则不再是唯一来源）：" + repr(literal)
        )
        assert "DISK_SOURCE_PREFIX" in src, name + ".py 应从 _source 引入 DISK_SOURCE_PREFIX"


# ── 回归修复：目录遍历 / 逐文件扫描丢线程 + grep 候选文件数上限 ────────────


def test_bug112_list_files_walk_runs_off_the_event_loop(in_workspace: Path, monkeypatch) -> None:
    """`_walk` 的无界同步目录遍历必须丢线程（大工作区一次 `list_files` 不再冻结整个后端）。

    判别法：桩 `_walk` 记录执行线程；`_call` 的 `asyncio.run` 让 handler 跑在主线程的
    事件循环上 ⇒ 桩的执行线程必须异于主线程。回退（handler 内直调 `_walk`）⇒ 回到主线程 ⇒ 红。
    """
    import threading

    lf = _real_module_or_fail("list_files")
    loop_tid = threading.get_ident()
    tids: list[int] = []

    def _stub_walk(ws: Path, base: Path) -> list[str]:  # noqa: ANN001
        tids.append(threading.get_ident())
        return ["a.txt"]

    monkeypatch.setattr(lf, "_walk", _stub_walk)
    obs = _call(lf)
    assert obs.ok is True, obs.text
    assert "- a.txt" in obs.text, f"桩返回必须真进观察文本（判据有效性）：{obs.text!r}"
    assert tids, "桩 _walk 没被调到 ⇒ 判据无效"
    assert all(t != loop_tid for t in tids), (
        "_walk 跑在事件循环线程上 ⇒ 遍历仍冻结循环（回归修复 未修）"
    )


def test_bug112_grep_files_iter_candidates_runs_off_the_event_loop(
    in_workspace: Path, monkeypatch
) -> None:
    """`_iter_candidate_files`（os.walk 全树收集，冷门 pattern 时零命中也扫完全盘）必须丢线程。"""
    import threading

    gf = _real_module_or_fail("grep_files")
    loop_tid = threading.get_ident()
    tids: list[int] = []

    def _stub_iter(ws: Path, base: Path) -> list[Path]:  # noqa: ANN001
        tids.append(threading.get_ident())
        return [ws / "a.txt"]

    monkeypatch.setattr(gf, "_iter_candidate_files", _stub_iter)
    obs = _call(gf, pattern="beta")
    assert obs.ok is True, obs.text
    assert tids, "桩 _iter_candidate_files 没被调到 ⇒ 判据无效"
    assert all(t != loop_tid for t in tids), (
        "候选收集跑在事件循环线程上 ⇒ 遍历仍冻结循环（回归修复 未修）"
    )


def test_bug112_grep_files_scan_runs_off_the_event_loop(in_workspace: Path, monkeypatch) -> None:
    """逐文件「读头 + 解码 + 逐行匹配」段（每文件最多读 1MB，文件多时比收集段更重）必须丢线程。"""
    import threading

    gf = _real_module_or_fail("grep_files")
    loop_tid = threading.get_ident()
    tids: list[int] = []

    def _stub_scan(files, ws, pattern, max_hits):  # noqa: ANN001
        tids.append(threading.get_ident())
        return ["a.txt:2: beta"], False

    monkeypatch.setattr(gf, "_scan_files", _stub_scan)
    obs = _call(gf, pattern="beta")
    assert obs.ok is True, obs.text
    assert "beta" in obs.text, f"桩返回必须真进观察文本（判据有效性）：{obs.text!r}"
    assert tids, "桩 _scan_files 没被调到 ⇒ 判据无效"
    assert all(t != loop_tid for t in tids), (
        "逐文件扫描跑在事件循环线程上 ⇒ 仍冻结循环（回归修复 未修）"
    )


def test_bug112_grep_files_file_cap_marks_truncation(in_workspace: Path, monkeypatch) -> None:
    """grep 补「候选文件数上限 + 截断标记」：配额在**文件数**上用完时，观察文本必须
    出现与命中数上限同一句的「（已按上限截断）」，且被裁掉的文件不得再产出命中
    （「不静默截断」纪律 —— 与 `folder_scan.MAX_FOLDER_FILES` 的「上限 + 显式标记」先例同形）。
    """
    gf = _real_module_or_fail("grep_files")
    monkeypatch.setattr(gf, "MAX_GREP_FILES", 1)  # workspace 夹具有 3 个文件
    obs = _call(gf, pattern="e")  # "e" 在 a.txt（beta）有命中；sub/b.txt（needle）应被裁掉
    assert obs.ok is True, obs.text
    assert "（已按上限截断）" in obs.text, f"文件数超限必须显式标记：{obs.text!r}"
    assert "a.txt" in obs.text, f"上限内的文件仍应正常产出命中：{obs.text!r}"
    assert "sub/b.txt" not in obs.text, f"被文件数上限裁掉的文件不得再产出命中：{obs.text!r}"


# ── 回归修复：命中配额在**最后一个扫描文件**内触顶 ⇒ 截断标记不得丢失 ────
# ⚠️ 载体必须用 `path=` 把候选文件隔离到只含目标文件的子目录：workspace 夹具自带的
# a.txt / sub/b.txt 排在候选序列更后，旧代码「配额满且还有下一个文件」的置标记分支
# 会照样触发，把真缺口掩盖掉（判据失效——首轮载体就栽在这上面）。


def test_bug109_last_file_hit_cap_still_marks_truncation(in_workspace: Path) -> None:
    """单文件 150 条命中、max_hits=100 ⇒ 观察文本既有「命中 100 条」又有「（已按上限截断）」。

    修前：配额在最后一个（此处即唯一）扫描文件内用完，旧置标记分支
    （「配额满且**还有下一个文件**」才进）不再进入 ⇒ 标记丢失，残缺结果被当全量。
    """
    gf = _real_module_or_fail("grep_files")
    solo = in_workspace / "solo"
    solo.mkdir()
    (solo / "big.txt").write_text("\n".join(["x"] * 150) + "\n", encoding="utf-8")
    obs = _call(gf, pattern="x", path="solo")
    assert obs.ok is True, obs.text
    assert "命中 100 条" in obs.text, f"命中数必须被裁到上限展示：{obs.text!r}"
    assert "（已按上限截断）" in obs.text, f"末文件触顶也必须显式标记（回归修复）：{obs.text!r}"


def test_bug109_exact_fill_is_not_marked(in_workspace: Path) -> None:
    """负控①：恰好 100 条（全量）⇒ **不得**误报「（已按上限截断）」（多探一条判别式的反向腿）。"""
    gf = _real_module_or_fail("grep_files")
    solo = in_workspace / "solo"
    solo.mkdir()
    (solo / "exact.txt").write_text("\n".join(["y"] * 100) + "\n", encoding="utf-8")
    obs = _call(gf, pattern="y", path="solo")
    assert obs.ok is True, obs.text
    assert "命中 100 条" in obs.text, f"命中数不符：{obs.text!r}"
    assert "（已按上限截断）" not in obs.text, f"恰好 100 条是全量结果，不得误报截断：{obs.text!r}"


def test_bug109_below_cap_is_not_marked(in_workspace: Path) -> None:
    """负控②：99 条（上限之下）⇒ 无标记（平凡腿，防修法把「无超额」也误标）。"""
    gf = _real_module_or_fail("grep_files")
    solo = in_workspace / "solo"
    solo.mkdir()
    (solo / "below.txt").write_text("\n".join(["w"] * 99) + "\n", encoding="utf-8")
    obs = _call(gf, pattern="w", path="solo")
    assert obs.ok is True, obs.text
    assert "命中 99 条" in obs.text, f"命中数不符：{obs.text!r}"
    assert "（已按上限截断）" not in obs.text, f"99 条未触顶，不得挂标记：{obs.text!r}"


def test_bug109_cross_file_cap_still_marks_truncation(in_workspace: Path) -> None:
    """跨文件腿：两文件各 60 条、max_hits=100 ⇒ 标记仍在（超额命中落在第二个文件内）。

    修前：第二个文件按剩余配额（40）截齐到恰好 100 条，循环自然结束 ⇒ 标记丢失。
    """
    gf = _real_module_or_fail("grep_files")
    duo = in_workspace / "duo"
    duo.mkdir()
    (duo / "c1.txt").write_text("\n".join(["z"] * 60) + "\n", encoding="utf-8")
    (duo / "c2.txt").write_text("\n".join(["z"] * 60) + "\n", encoding="utf-8")
    obs = _call(gf, pattern="z", path="duo")
    assert obs.ok is True, obs.text
    assert "命中 100 条" in obs.text, f"命中数必须被裁到上限展示：{obs.text!r}"
    assert "（已按上限截断）" in obs.text, f"跨文件触顶必须显式标记：{obs.text!r}"
