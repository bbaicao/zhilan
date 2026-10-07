"""**零 IO 计数桩**（专项工具升级-3 · T3 阶段一 · 条目 `回归检查`）。

## 为什么需要它

T3 阶段一的判据 A1 有一条断件是「**越界判定不得产生任何 IO**」。可问题在于：
「没读文件」这件事在本仓**没有任何现成载体现**（全仓**零计数桩先例**，B6）。
⇒ 没有桩，A1 的那条断件**恒为真**（`classify()` 物理上不写文件 ⇒ 「未产生文件」自动成立）
⇒ **判据零判别力 = 假绿**。

## 用法（三件套之一）

```python
from tests._io_probe import io_probe

with io_probe() as probe:
    verdict = _paths.classify("../outside.txt", root)

assert probe.counts == {}          # 层 ①② 必须零原语
assert probe.total == 0
```

## 🔴 头号假绿风险：`Path.resolve()` 绕过一切 `Path.*` 桩

本会话两路独立实测（`回归检查-resolve读数.log` / `回归检查-链接可构造性.log`）：

- ``Path.resolve()`` 的实现是 ``os.path.realpath(self)``（``pathlib/_local.py:670``），
  而 ``os.path.realpath`` 即 ``ntpath.realpath``（实测 ``is`` 为真），
  其内部 ``from nt import _findfirstfile, _getfinalpathname`` ⇒ **走 C 实现**；
- **实测**：把 ``Path.stat`` / ``Path.open`` / ``Path.is_dir`` / ``Path.exists`` /
  ``Path.iterdir`` **全部** patch 成抛异常后，``Path('...').resolve()`` **照常成功、命中 0 次**。

⇒ 桩**必须**额外覆盖模块级 ``os.path.realpath`` / ``ntpath.realpath`` / ``nt._getfinalpathname``，
且 A2 必须含一条「``resolve()`` **被拦**」的正面样本 —— 否则「层 ③ 恰好奇数命中」也是假绿。

## 分两条腿（D-回归检查 第二条）

``os.path.exists`` / ``os.access`` 是 **C builtin**，patch ``Path.stat`` 对其**无效**（实测）
⇒ 枚举面分「模块级 ``os.*``」与「类属性 ``Path.*``」**两条腿分别 patch + 各自自证**。

## 本阶段的枚举面边界（**如实登记，不造假**）

``read_text`` / ``write_text`` / ``glob`` / ``shutil.*`` 在**阶段一**（只读地基）**无合法调用点**
⇒ **不列入本阶段自证面**，登记为「**阶段 5 适用**」。
⚠️ 桩**照样会拦**它们（拦了不亏），但**不要求**它们本轮 > 0 —— 造一个假调用点来「自证」本身就是假绿。

## 🔴 桩的**已知盲区**（审计 B 路实测 · 2026-09-24 · **必读**）

⚠️ **口径**：本桩的覆盖面是「**已枚举面**」，**不是**「**物理全 IO 面**」。
⇒ 「层 ①② 零 IO」这条断件的**准确含义**是：
「**未走下列任何一条通道**」，**不是**「物理上不可能发生任何 IO」。
**不得**把前者读成后者（那是把枚举面当物理面 —— 一个**尺度错误**）。

| 盲区 | 实测 | 后果 / 处置 |
|---|---|---|
| **`ctypes` 直连 Win32** | `GetFileAttributesW` / `CreateFileW` / **`GetCurrentDirectoryW`** 全部 `counts = {}` | 🔴 **最严重**：既能读文件属性，也能**读进程 CWD** ⇒ 「不得按 CWD 归因」（红线 1 同族）存在**完全绕过**的通道。**处置**：本阶段产品代码**不用 `ctypes`**；阶段 5（写侧）须**显式排除**或改白名单 |
| **`from os import stat` 形态** | 先绑定再调用 ⇒ `counts = {}`（**完全绕过**） | 桩 patch 的是**模块属性**，绑进本地命名空间后改不动。**现状**：`_paths.py` 用 `import os` + `os.xxx` 属性访问 ⇒ **不是易感形态**（AST 实测）。⚠️ 但这是**无护栏的隐性契约** ⇒ 建议阶段 2 加一条 AST 判据禁止 `from os import ...` |
| **`io.open`** | `io.open is builtins.open` 为真，但桩只 `setattr(builtins, "open", ...)` ⇒ `io.open` **不被计数** | 写 `import io; io.open(...)` 可绕过。**处置**：本阶段不用；阶段 5 登记 |
| **`subprocess.*`** | 子进程**独立进程空间** ⇒ 原理上盖不住 | 阶段 5 的写侧若调 subprocess，「零 IO」断言必须**显式排除** |
| **`os.environ` / `os.getenv`** | 全部 `counts = {}` | 严格说「读环境变量」不是文件 IO，**可接受**；但它是红线 1 的**旁路**（环境里可能有 `CD`/`PWD`）⇒ 已登记 |
| `shutil.disk_usage` / `get_terminal_size` / `os.get_exec_path` | `counts = {}` | 系统信息查询（非文件 IO），低危，**已登记** |

**✅ 已实测「盖章」（不是盲区）**：`Path.resolve()`（头号风险，见上节）· `os.scandir` ·
`glob.glob`/`iglob` · `Path.glob`/`rglob` · `os.stat`/`listdir`/`walk` · `shutil.copy`/`copyfile`/`rmtree`/`which` ·
`Path.read_text`/`write_text`/`stat`/`exists`/`is_dir`/`iterdir` · `ntpath.realpath` · `os.access`（本轮补漏后）。

**⚪ 不是缺陷的「绕过」**：`os.path.commonpath` 等**纯字符串**函数 `counts = {}` 是**期望行为**
（它们不产生系统调用）—— 桩**不应**把纯词法处理误报成 IO。
"""

from __future__ import annotations

import builtins
import ntpath
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

#: 模块级 ``os.path.*`` 面（**C builtin 亦在此列** —— 实测 patch ``Path.stat`` 对其无效）
_OS_PATH_PRIMITIVES: Final[tuple[str, ...]] = (
    "exists",
    "lexists",
    "islink",
    "isfile",
    "isdir",
    "realpath",
    "getsize",
    "getmtime",
    "stat",
    "lstat",
    "access",
)
#: 枚举面里**已知在本版本 Python 不存在**的条目（如实登记，不是缺陷）。
#: 构造方式：**运行时实测**（`hasattr` 为假即入此表），**不靠人肉记忆** ——
#: 审计 P1 指出首版把 `os.path.access` 当有效条目（它只挂在 `os` 上），
#: 且漏登记了 `os.path.stat` / `os.path.lstat`。
#: ⚠️ 这三条留在清单里只为「对齐两条腿的清单形状」（`os.path.*` 与 `os.*` 同形），
#: **不指望它们生效**。
#: 🔴 **审计 B 路 P1 更正**：本行原文曾写「桩的腿 2 已覆盖真正的入口
#: （``os.stat`` / ``os.lstat`` / ``os.access``）」—— **该陈述是错的**：
#: ``access`` **当时并不在** ``_OS_PRIMITIVES`` 里 ⇒ ``os.access()`` 实测
#: ``counts = {}``（**静默绕过**），而这条注释却让人以为它被覆盖。
#: ⇒ 已把 ``"access"`` **补进** ``_OS_PRIMITIVES``（该条的真入口是 ``os.access``）；
#: 形如 ``os.access`` 的调用现在**会被计数**。
_KNOWN_INERT: Final[frozenset[str]] = frozenset(
    {"os.path.stat", "os.path.lstat", "os.path.access"}
)
#: 触**进程 CWD** 的原语（红线 1「不得取进程 CWD 当工作区根」同族）。
#: ⚠️ **本会话补登（对抗审计 P1）**：首版漏了这两条 ⇒ 若实现偷偷用 ``os.path.abspath``
#: （相对路径 → 按 CWD 归因）或读 ``os.getcwd()`` 来「找根」，**桩盖不住** ⇒ 静默通过。
#: ``abspath`` 只是 ``normpath(join(getcwd(), path))``，**不产生系统调用**，
#: 所以它既不是「IO 原语」也不是「纯词法」——它正是**CWD 归因**这个危险动作本身。
_OS_CWD_PRIMITIVES: Final[tuple[str, ...]] = (
    "getcwd",
    "abspath",
)
#: 🔴 **枚举面登记表（2026-09-24 · 对抗审计 A 路新发现 #3 的根治）**。
#:
#: 桩的每一条腿都来自**某一张枚举表**；而「桩接上了没有」这个自检
#: （:func:`unwired_primitive_names`）**曾经只扫其中三张** ⇒ `_OS_CWD_PRIMITIVES`
#: 与 `nt._getfinalpathname` 这两条腿**从来没被检查过**。
#:
#: 这里把「有哪些枚举表」变成**可枚举的数据**，让扫描自检按表全扫；
#: 并配一条断言（`test_a2_every_enumeration_table_is_scanned`）：
#: **任何新增的枚举表若没登记进本表，判据就红** —— 防止「将来又漏一条腿」重演。
#: 每项 = ``(表名, 载体模块, 是否用 ``hasattr`` 逐名扫)``；
#: ``nt._getfinalpathname`` 是**单点**（不是表），单独以 `_NT_FINAL_PATH` 表登记。
_NT_MODULE_NAME: Final[str] = "nt"
_NT_FINAL_PATH_ATTR: Final[str] = "_getfinalpathname"


def _nt_module() -> Any | None:
    """取 ``nt`` 模块；非 Windows 返回 ``None``（如实降级，不伪造）。"""
    try:
        import nt  # type: ignore[import-not-found]

        return nt
    except ImportError:  # 非 Windows
        return None


#: 枚举面登记表：``(表名, 逐名扫描函数)``。新增枚举表**必须**在此登记。
_ENUMERATION_TABLE: Final[tuple[tuple[str, str], ...]] = (
    ("_OS_PATH_PRIMITIVES", "os.path"),
    ("_OS_PRIMITIVES", "os"),
    ("_OS_CWD_PRIMITIVES", "<mixed>"),  # 🔴 混载体：见 _CWD_LEG_CARRIERS
    ("_PATH_METHODS", "Path"),
    ("_NT_FINAL_PATH", "nt"),
)
#: 🔴 **`_OS_CWD_PRIMITIVES` 是「混载体」表**（2026-09-24 实测）：
#: ``getcwd`` 挂在 ``os`` 上，而 ``abspath`` **只**挂在 ``os.path`` 上
#: （``hasattr(os, "abspath")`` 实测为 **False**）。
#: ⚠️ **这里踩过一次**：给 A 路新发现 #3 补扫描时，我按「整表挂在 `os`」写成
#: ``hasattr(os, name)`` ⇒ 立刻报出 **假阳性** `['os.abspath']`
#: （把「找错了模块」误报成「桩接不上」）。⇒ 逐名登记载体，杜绝再次误判。
_CWD_LEG_CARRIERS: Final[tuple[tuple[str, str], ...]] = (
    ("getcwd", "os"),
    ("abspath", "os.path"),
)
#: ``nt._getfinalpathname`` 的**单点**登记（不是名字元组，故单列）。
_NT_FINAL_PATH: Final[tuple[str, ...]] = (_NT_FINAL_PATH_ATTR,)
#: ⚠️ **标准库自身会触发**的 CWD 读取（**不是产品代码的 IO 面**，如实登记）。
#: 实测（本会话）：``ntpath.realpath`` 内部调 ``os.getcwd()``
#: ⇒ 合法路径腿的 `os.getcwd: 1` 全部来自 Python 3.13 的 `ntpath.realpath`，
#: 与 `_paths.classify` 无关。**不得**因此把 `getcwd` 从桩里删掉（删了就盖不住产品代码的
#: CWD 归因），也**不得**让它污染「未登记 IO 面」的断言 —— 用「**归因**」而不是「存在」。
_STDLIB_INTERNAL_CWD_HITS: Final[frozenset[str]] = frozenset({"os.getcwd"})
#: 模块级 ``os.*`` 面
#: ⚠️ ``open`` / ``utime`` / ``rename`` / ``remove`` / ``mkdir`` / ``rmdir`` / ``symlink``
#: / ``link`` / ``truncate`` **必须**在内 —— 本会话实测（A1b 的判别力自证）：
#: ``Path.touch()`` **不**经 ``builtins.open``（命中 0 次），而是走 **``os.open``**
#: （命中 2 次，含临时名与目标名）⇒ 漏了 ``os.open`` 的话，「层 ①② 零 IO」这条断件的
#: **判别力自证**（插一次 ``touch()`` 必须让断言变红）会**失败**——桩盖不住就是假绿。
#: 🔴 **``access`` 必须在内（审计 B 路 P1 补漏）**：``os.access`` 是 **C builtin**、
#: 且**真实存在**（`hasattr(os, "access")` 为真）。首版把它**漏在本表之外**
#: （只在 `_OS_PATH_PRIMITIVES` 里留了个不存在的 `os.path.access` 死条目），
#: 却同时**在注释里声称已覆盖** ⇒ 实测 ``os.access(...)`` 的 `counts = {}`
#: （**静默绕过**）。⇒ 补进本表，形如 ``os.access`` 的调用现在会被计数。
_OS_PRIMITIVES: Final[tuple[str, ...]] = (
    "stat",
    "lstat",
    "readlink",
    "scandir",
    "listdir",
    "walk",
    "open",
    "utime",
    "rename",
    "replace",
    "remove",
    "unlink",
    "mkdir",
    "rmdir",
    "truncate",
    "symlink",
    "link",
    "access",
)
#: 类属性 ``Path.*`` 面
_PATH_METHODS: Final[tuple[str, ...]] = (
    "open",
    "read_text",
    "read_bytes",
    "write_text",
    "write_bytes",
    "iterdir",
    "glob",
    "rglob",
    "stat",
    "lstat",
    "exists",
    "is_file",
    "is_dir",
    "is_symlink",
    "is_junction",
    "resolve",
    "absolute",
    "samefile",
    "owner",
    "readlink",
)
#: ``Path`` 的类属性里需要**原样保留**（非 IO）的成员
_PATH_KEEP: Final[tuple[str, ...]] = ()


def _noop_raise(name: str):
    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(f"_io_probe: 命中未预期的 IO 原语 {name}")

    return _boom


@dataclass
class IoProbe:
    """一次探针会话的读数。"""

    #: 按**原语全名**计数的命中次数（如 ``os.path.realpath`` / ``Path.resolve``）
    counts: dict[str, int] = field(default_factory=dict)
    #: 按**腿**归并的计数（``os`` / ``os.path`` / ``Path`` / ``builtins.open`` / ``nt``）
    legs: dict[str, int] = field(default_factory=dict)
    #: 是否处于「拦截」模式（True = patch 成抛异常；False = 只计数、放行）
    raising: bool = False
    _saved: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def hit(self, name: str) -> int:
        return self.counts.get(name, 0)

    def legs_for(self, prefix: str) -> int:
        return sum(v for k, v in self.counts.items() if k.startswith(prefix))

    def as_matrix(self) -> dict[str, int]:
        """层 ①② 用的形态：**原语全名 → 命中次数**（未命中不出现）。"""
        return dict(sorted(self.counts.items()))


class _Patcher:
    """负责 patch/unpatch；把「计数」与「是否抛」两件事解耦。"""

    def __init__(self, probe: IoProbe, *, raising: bool) -> None:
        self.probe = probe
        self.raising = raising

    def _wrap(self, full_name: str, original: Any) -> Any:
        probe = self.probe

        def _wrapper(*args: Any, **kwargs: Any) -> Any:
            probe.counts[full_name] = probe.counts.get(full_name, 0) + 1
            leg = full_name.split(".", 1)[0] if "." in full_name else full_name
            if full_name.startswith("os.path."):
                leg = "os.path"
            probe.legs[leg] = probe.legs.get(leg, 0) + 1
            if self.raising:
                raise AssertionError(f"_io_probe: 命中 IO 原语 {full_name}")
            return original(*args, **kwargs)

        return _wrapper

    def install(self) -> None:
        p = self.probe
        # ── 腿 1：模块级 os.path.* ──
        for name in _OS_PATH_PRIMITIVES:
            if not hasattr(os.path, name):
                continue
            full = f"os.path.{name}"
            p._saved[full] = getattr(os.path, name)
            setattr(os.path, name, self._wrap(full, getattr(os.path, name)))
        # ── 腿 1b：CWD 归因面（os.getcwd / os.path.abspath）──
        # ⚠️ 审计 P1：``os.path.access`` 是**死条目**（实测 ``hasattr(os.path,'access')`` 为
        # **False** —— 真正的入口是 ``os.access``，已在腿 2），**保留但记明无效**；
        # 而 ``abspath`` / ``getcwd`` 此前**完全没覆盖**。见 `_OS_CWD_PRIMITIVES`。
        for name in _OS_CWD_PRIMITIVES:
            if not hasattr(os.path, name) and not hasattr(os, name):
                continue
            if hasattr(os.path, name):
                full = f"os.path.{name}"
                p._saved[full] = getattr(os.path, name)
                setattr(os.path, name, self._wrap(full, getattr(os.path, name)))
            else:
                full = f"os.{name}"
                p._saved[full] = getattr(os, name)
                setattr(os, name, self._wrap(full, getattr(os, name)))
        # ── 腿 2：模块级 os.* ──
        for name in _OS_PRIMITIVES:
            if not hasattr(os, name):
                continue
            full = f"os.{name}"
            p._saved[full] = getattr(os, name)
            setattr(os, name, self._wrap(full, getattr(os, name)))
        # ── 腿 3：builtins.open ──
        full = "builtins.open"
        p._saved[full] = builtins.open
        setattr(builtins, "open", self._wrap(full, builtins.open))
        # ── 腿 4：Path.* 类属性 ──
        for name in _PATH_METHODS:
            if not hasattr(Path, name):
                continue
            full = f"Path.{name}"
            p._saved[full] = getattr(Path, name)
            setattr(Path, name, self._wrap(full, getattr(Path, name)))
        # ── 腿 5：nt._getfinalpathname（resolve 的**最终**宿主，CTypes 直连）──
        try:
            import nt  # type: ignore[import-not-found]

            if hasattr(nt, "_getfinalpathname"):
                full = "nt._getfinalpathname"
                p._saved[full] = getattr(nt, "_getfinalpathname")
                setattr(nt, "_getfinalpathname", self._wrap(full, getattr(nt, "_getfinalpathname")))
        except ImportError:  # 非 Windows
            pass

    def uninstall(self) -> None:
        p = self.probe
        for full, original in p._saved.items():
            if full.startswith("os.path."):
                setattr(os.path, full.split(".", 2)[2], original)
            elif full.startswith("Path."):
                setattr(Path, full.split(".", 1)[1], original)
            elif full == "builtins.open":
                setattr(builtins, "open", original)
            elif full.startswith("nt."):
                import nt  # type: ignore[import-not-found]

                setattr(nt, full.split(".", 1)[1], original)
            else:
                setattr(os, full.split(".", 1)[1], original)
        p._saved.clear()


@contextmanager
def io_probe(*, raising: bool = True) -> Iterator[IoProbe]:
    """拦截模式探针：**任何**被覆盖的 IO 原语一命中就抛 ``AssertionError``。

    默认 ``raising=True`` —— 「零 IO」判据（A1 第 4 断件）用这一档：只要碰了 IO 就**当场红**，
    比事后查计数更难糊弄。
    """
    probe = IoProbe(raising=raising)
    patcher = _Patcher(probe, raising=raising)
    patcher.install()
    try:
        yield probe
    finally:
        patcher.uninstall()


@contextmanager
def io_count() -> Iterator[IoProbe]:
    """**计数模式**探针：放行 IO、只记数（A2 的「逐原语自证 > 0」用这一档）。"""
    probe = IoProbe(raising=False)
    patcher = _Patcher(probe, raising=False)
    patcher.install()
    try:
        yield probe
    finally:
        patcher.uninstall()


def probe_resolve_is_interceptable() -> bool:
    """自检：``Path.resolve()`` 到底能不能被本桩拦住？（D-回归检查 的活性自证前提）

    返回 ``True`` ⇒ 桩**接得上**，A1 的「层 ③ 零命中」才有判别力。
    返回 ``False`` ⇒ **交付物本身不可判**，必须显式失败（不得静默降级为假绿）。
    """
    try:
        with io_probe():
            Path(ntpath.abspath(".")).resolve()
    except AssertionError:
        return True
    return False


def probe_os_realpath_is_interceptable() -> bool:
    """自检：``os.path.realpath`` 能否被拦住（模块级腿）。"""
    try:
        with io_probe():
            os.path.realpath(".")
    except AssertionError:
        return True
    return False


def unwired_primitive_names() -> list[str]:
    """**桩覆盖自检（对抗审计 P1 的根治）**：列出「声明在枚举面里、但实际接不上」的条目。

    为什么必须有：桩的**漏项**是**静默**的 —— 漏了 `os.open` 时，
    A1b 的「插 IO 必须红」退化成 `DID NOT RAISE`，而 A1 的「层 ①② 零 IO」
    **照样是绿的**（因为它恒真）。本会话首版正是这样假绿了一轮。
    ⇒ 把「枚举了但 `hasattr` 为假」这件事**变成可读的断言**，而不是注释里的自我提醒。

    返回值里**不含**已知的无效条目（见 :data:`_KNOWN_INERT`）—— 那些是如实登记的边界，
    不是缺陷；但它们**必须显式列在** `_KNOWN_INERT` 里，**不得**靠「反正也没人查」蒙混。

    🔴 **覆盖面扩展（2026-09-24 · 对抗审计 A 路新发现 #3 的处置）**：
    本函数初版**只扫三张表**（`_OS_PATH_PRIMITIVES` / `_OS_PRIMITIVES` / `_PATH_METHODS`），
    而**漏扫**了另两条腿 —— ① :data:`_OS_CWD_PRIMITIVES`（`os.getcwd` / `os.path.abspath`）、
    ② :data:`_NT_FINAL_PATH`（`nt._getfinalpathname`，来自 `pathlib` 的 `Path.resolve()`）。
    漏扫的后果与漏项同族：**这两条腿哪天接不上，`unwired` 仍然是空的**
    ⇒ 「桩已全接上」这句结论里，**有两条腿从来没被检查过**。
    ⇒ 现按 :data:`_ENUMERATION_TABLE` 逐表全扫（新增的枚举表**必须**登记进去，
    `test_a2_*` 会断言「每张枚举表都在扫描名单里」，防止将来又漏）。
    """
    missing: list[str] = []
    for name in _OS_PATH_PRIMITIVES:
        if not hasattr(os.path, name):
            missing.append(f"os.path.{name}")
    for name in _OS_PRIMITIVES:
        if not hasattr(os, name):
            missing.append(f"os.{name}")
    for name in _PATH_METHODS:
        if not hasattr(Path, name):
            missing.append(f"Path.{name}")
    # 🔴 审计 A 路新发现 #3：这两条腿此前**从未被本函数扫过**。
    # ⚠️ 必须**逐名**按载体扫 —— 本表是「混载体」（`getcwd` 在 `os`、`abspath` 在 `os.path`），
    #    整表按 `os` 扫会报出假阳性 `os.abspath`（实测踩过，见 `_CWD_LEG_CARRIERS`）。
    for name, carrier in _CWD_LEG_CARRIERS:
        host = os.path if carrier == "os.path" else os
        if not hasattr(host, name):
            missing.append(f"{carrier}.{name}")
    _nt = _nt_module()
    if _nt is not None:
        for name in _NT_FINAL_PATH:
            if not hasattr(_nt, name):
                missing.append(f"{_NT_MODULE_NAME}.{name}")
    return sorted(set(missing) - _KNOWN_INERT)


def enumeration_tables() -> list[str]:
    """**枚举面登记表自检**：返回桩实际拥有的枚举表名（供判据比对）。

    用途：`test_a2_every_enumeration_table_is_scanned` 断言
    「**模块里存在的名字元组枚举表**」与「:data:`_ENUMERATION_TABLE` 里登记的」
    **完全一致** ⇒ 新增一条腿却忘了登记进扫描名单时，判据**当场红**。
    """
    return [name for name, _carrier in _ENUMERATION_TABLE]


__all__ = [
    "IoProbe",
    "enumeration_tables",
    "io_count",
    "io_probe",
    "probe_os_realpath_is_interceptable",
    "probe_resolve_is_interceptable",
    "unwired_primitive_names",
]
