"""`_paths` 路径边界层的定向用例（专项工具升级-3 · T3 阶段一 · `回归检查`…`回归检查`）。

## 覆盖（判据 A1 / A1b / A2 / A3a / A3b / A4 / A5a′ / A5b′ / A6a / A6b / A17）

| 组 | 判据 | 说明 |
|---|---|---|
| `test_a1_*` | A1 | **7 输入 × 4 断件的布尔矩阵**（含**分层桩计数**） |
| `test_a1b_*` | A1b | 第 4 断件的**判别力自证**（在词法层后插 `Path.touch()` ⇒ 层③计数必须红） |
| `test_a2_*` | A2 | 桩自证三件套（同一入口 / 分层自证 / 覆盖变异） |
| `test_a3a_*` / `test_a3b_*` | A3 | junction 腿 / 符号链接腿（带**能力探测 + 降级分支**） |
| `test_a4_*` | A4 | 大小写双向 + `proj`/`proj2` 相邻前缀陷阱 |
| `test_a5a_*` / `test_a5b_*` | A5a′/A5b′ | **解析层腿**（全量腿归阶段 3，见包 §3 的分期降级登记） |
| `test_a6a_*` / `test_a6b_*` | A6 | 敏感路径词法腿（零 IO）/ 归一腿（允许 IO） |
| `test_a6b_layer5_*` | **A6b-⑤** | **第 ⑤ 层「敏感复判」的独立判别**（`layer == "sensitive"`，**不容 ④ 兜住**） |
| `test_a3_*layer4*` | **A3-④** | **第 ④ 层「越界复判」的不建链判别**（故障注入，`layer == "recheck"`） |
| `test_a17_*` | A17 | AST 断 `_paths.py` 的 import 面 |

## 夹具（**不得**依赖 `tests/api/conftest.py`）

实测：`tests/agent/` **无 `conftest.py`** ⇒ 本文件自带夹具。
⚠️ 红线 12：**一律以 `tmp_path` 作根**（不写死盘符归属；越界样本用「同盘 / 异盘各一条」）。
⚠️ 本文件**不写任何仓库内路径**（工作区根恒为 `tmp_path` 的父级/自身），跑完
`git status --porcelain` 必须为空（由 `test_red_line_12_*` 断言）。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest

from app.agent.tools import _paths as P

from tests._io_probe import (
    enumeration_tables,
    io_count,
    io_probe,
    probe_os_realpath_is_interceptable,
    probe_resolve_is_interceptable,
    unwired_primitive_names,
)

# ── 夹具 ────────────────────────────────────────────────────────────────────


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """工作区根（**红线 12：一律以 `tmp_path` 作根**）。"""
    w = tmp_path / "ws"
    w.mkdir(parents=True, exist_ok=True)
    return w


@pytest.fixture
def other_drive_root(tmp_path: Path) -> Path:
    """异盘根：找一个与 `tmp_path` **不同盘符**的已有盘；找不到则跳过。"""
    tmp_drive = tmp_path.drive.lower()
    for letter in "CDEFGH":
        candidate = Path(f"{letter}:/")
        if candidate.exists() and candidate.drive.lower() != tmp_drive:
            return candidate
    pytest.skip("本机只有一个盘符，异盘样本不可构造")


# ── A1 · 7 输入 × 4 断件的布尔矩阵（含分层桩计数）─────────────────────────────


#: A1 的输入集（**阶段包 §3 A1 的 7 类**，逐类一个样本）。
#: ⚠️ **仅覆盖「词法可拦」的那些**（D-回归检查）—— 见 `A1_LEXICAL_CASES` 与 `A1_PREFIX_CASES` 的拆分。
A1_LEXICAL_CASES: list[tuple[str, str]] = [
    ("unc", r"\\server\share\x.py"),
    ("unc-forward", "//server/share/x.py"),
    ("namespace-unc", r"\\?\UNC\server\share\x.py"),
    ("extended-length", r"\\?\C:\Windows\x.py"),
    ("device-namespace", r"\\.\C:\Windows\x.py"),
    ("device-name", "CON"),
    ("device-name-with-ext", "NUL.log"),
    ("drive-relative", "C:foo"),
    ("ads", "notes.txt:stream"),
    ("trailing-space", "sub/foo "),
    ("trailing-dot", "sub/foo."),
    ("illegal-char", "sub/a<b.txt"),
    ("nul-byte", "sub/a\x00b.txt"),
    ("short-name-outside", r"C:\PROGRA~1\x.py"),
    ("other-drive", "D:\\other\\x.py"),
    ("system-dir", r"C:\Windows\System32\x.py"),
]

A1_PREFIX_CASES: list[tuple[str, str]] = [
    ("parent-escape", "../outside.txt"),
    ("parent-escape-deep", r"..\..\Windows\x.py"),
    ("parent-escape-mixed", "sub/../../outside.txt"),
]

A1_ALL_CASES = A1_LEXICAL_CASES + A1_PREFIX_CASES

#: 冻结文案表（A1 第 2 断件：中文 ⇒ 必须命中其中之一，**不是**「随便有句中文」）
#: ⚠️ 本表覆盖**全部 12 条**冻结常量（含 `PATH_NO_ROOT_TEMPLATE`）——
#: 首版漏了 `PATH_NO_ROOT_TEMPLATE` ⇒ `root=None` 那条被**误判**成「未命中冻结表」
#: （本会话实测：`未配置工作区根，路径 ok.txt 无法校验，已拒绝。` 判 False）。
FROZEN_TEMPLATES: tuple[str, ...] = (
    P.PATH_OUTSIDE_TEMPLATE,
    P.PATH_NO_ROOT_TEMPLATE,
    P.PATH_UNC_TEMPLATE,
    P.PATH_DEVICE_NAMESPACE_TEMPLATE,
    P.PATH_DEVICE_NAME_TEMPLATE,
    P.PATH_DRIVE_RELATIVE_TEMPLATE,
    P.PATH_SHORT_NAME_TEMPLATE,
    P.PATH_OTHER_DRIVE_TEMPLATE,
    P.PATH_TRAILING_TEMPLATE,
    P.PATH_ILLEGAL_CHAR_TEMPLATE,
    P.PATH_SENSITIVE_TEMPLATE,
    P.PATH_EMPTY_TEMPLATE,
    P.PATH_ROOT_UNUSABLE_TEMPLATE,
)


def _matches_frozen(reason: str) -> bool:
    """冻结文案命中判定：把模板的 `{...}` 占位符换成通配后做前缀比对。

    ⚠️ `str.format` 后模板里的 `{path}` 已被替换，长短不一 ⇒ 用「首尾夹逼」而非等值比对。
    """
    for tpl in FROZEN_TEMPLATES:
        head = tpl.split("{", 1)[0]
        tail = tpl.rsplit("}", 1)[1] if "}" in tpl else ""
        if reason.startswith(head) and reason.endswith(tail):
            return True
    return False


@pytest.mark.parametrize("case,path", A1_ALL_CASES, ids=[c[0] for c in A1_ALL_CASES])
def test_a1_lexical_and_prefix_rejects_matrix(root: Path, case: str, path: str) -> None:
    """**A1：每行断四件** ① `Reject` ② 冻结中文文案 ③ **层①②零原语** ④ **未产生任何文件**。

    ⚠️ 第 4 断件在本包**不是**「未产生文件」（只读判定层该断件零判别力），
    而是「**分层桩计数 == 0**」—— 由 `io_probe()` 的**拦截模式**实现（碰 IO 即当场红）。
    """
    with io_probe() as probe:
        verdict = P.classify(path, root)

    # 断件 ①：必须拒绝
    assert isinstance(verdict, P.Reject), f"[{case}] 未被拒绝：{verdict!r}"
    # 断件 ②：必须命中**冻结文案表**
    assert _matches_frozen(verdict.reason), f"[{case}] 文案未命中冻结表：{verdict.reason!r}"
    assert verdict.reason.endswith("已拒绝。"), f"[{case}] 缺少结尾：{verdict.reason!r}"
    # 断件 ③：层 ①② 零 IO（拦截模式下能跑到这里就说明没碰 IO）
    assert probe.total == 0, f"[{case}] 产生了 IO：{probe.as_matrix()}"
    # 断件 ④：层 ③ 计数 == 0（未进 realpath 归一）
    assert probe.legs_for("os.path.realpath") == 0, f"[{case}] 进了 realpath：{probe.as_matrix()}"
    # 断件 ⑤（域断件）：根下**未产生任何文件**
    assert list(root.rglob("*")) == [], f"[{case}] 根下产生了文件"


def test_a1_reject_layer_is_lexical_or_prefix(root: Path) -> None:
    """A1 的域断件：拒绝必须来自 ① 或 ②（**不得**是 ③④⑤ 层的失败）。"""
    for case, path in A1_ALL_CASES:
        verdict = P.classify(path, root)
        assert isinstance(verdict, P.Reject)
        assert verdict.layer in {"lexical", "prefix"}, f"[{case}] 层不对：{verdict.layer}"


# ── A1b · 第 4 断件的判别力自证（**M29 打在词法层之后插 `Path.touch()`**）──────


def test_a1b_zero_io_assertion_has_discriminating_power(root: Path, monkeypatch) -> None:
    """**A1b**：人为插一次 `Path.touch()` ⇒ 「层 ③ 计数 == 0」**必须红**。

    ⚠️ 没有这条，A1 的第 4 断件就是装饰（`classify()` 物理上不写文件 ⇒ 恒真）。
    实现方式 = 在**候选路径**的词法判定之后、`classify` 的 ② 之前插探测点。

    🔴 **本会话实测两个坑（都使本用例一度假绿）**：

    ① **桩漏了 `os.open`**：`Path.touch()` **不**经 `builtins.open`（命中 0 次），
       而是走 **`os.open`**（命中 2 次）。首版 `_io_probe.py` 的 `os.*` 腿只列了
       `stat/lstat/readlink/scandir/listdir/walk` ⇒ 本用例**首跑 `DID NOT RAISE`**。
       ⇒ 已把 `open`/`utime`/`rename`/`remove`/`mkdir` 等写侧原语补进 `_OS_PRIMITIVES`。

    ② **注入点选错了**：`classify` 现在先对**根**跑一次 `_lexical_layers(is_root=True)`
       （根本身形态闸）⇒ 若把 poison 挂在 `result[1] is None` 上，**根那次调用**就会先触发
       （根是合法的 `tmp_path`），注入点**前移**到「根判定之后、极早的位置」——
       而 `../outside.txt` 在**候选**词法层就返回了 ⇒ 后续 assert 仍会红（这条侥幸能过）。
       但更严重的是：**桩漏 `os.open` 时本用例整体假绿**。
       ⇒ 注入点改为**只在 `is_root=False` 且候选词法层放行**时触发（语义精确、与设计意图一致）。
    """
    original = P._lexical_layers

    def poisoned(raw: str, *, is_root: bool = False, root_prefix=None):
        result = original(raw, is_root=is_root, root_prefix=root_prefix)
        # 只在**候选路径**（非根）且候选已过词法层时注入 —— 模拟「② 之后、③ 之前」的位置
        if result[1] is None and not is_root:
            # ⚠️ 注入的 IO 操作要**同时**经过 `os.open` 与 `os.utime`（`touch` 两者都走），
            # 这样「桩漏掉任一条腿」都会被抓到。实测 `Path.touch()`：
            # 经 `os.open`（2 次）+ `os.utime`（1 次），**不**经 `builtins.open`（0 次）。
            Path(root / "poisoned.tmp").touch(mode=0o600)
        return result

    monkeypatch.setattr(P, "_lexical_layers", poisoned)
    # 用一条**必须被 ② 拦下**的输入（`../outside.txt`）—— 它在原实现下是「零 IO 拒绝」
    with pytest.raises(AssertionError, match="_io_probe"):
        with io_probe():
            P.classify("../outside.txt", root)
    # 二次自证：把注入点关掉后，同一条输入**必须**回到「零 IO 拒绝」
    #（否则「能红」可能只是因为环境脏，而不是注入点本身有效）
    monkeypatch.undo()
    with io_probe() as clean:
        verdict = P.classify("../outside.txt", root)
    assert isinstance(verdict, P.Reject) and clean.total == 0, (
        f"未注入 IO 时本该零 IO 拒绝，实测 {verdict!r} / {clean.as_matrix()}"
    )


# ── A2 · 桩自证三件套 ───────────────────────────────────────────────────────


def test_a2_probe_is_alive_for_resolve_and_realpath() -> None:
    """**A2 前提**：桩必须接得上 —— `resolve()` 与 `os.path.realpath` **都要**能被拦。

    ⚠️ 本仓头号假绿风险（D-回归检查）：`Path.resolve()` 走 C 实现，绕过一切 `Path.*` 桩。
    这两条自证若为 `False` ⇒ **交付物本身不可判**，必须显式失败。
    """
    assert probe_resolve_is_interceptable(), "Path.resolve() 未能被桩拦住 ⇒ A1 第 4 断件假绿"
    assert probe_os_realpath_is_interceptable(), "os.path.realpath 未能被桩拦住"


def test_a2_stub_declared_entries_are_all_wired() -> None:
    """**桩覆盖自检**（对抗审计 P1 的根治）：枚举面里**声明了却接不上**的条目必须为 0。

    ⚠️ 为什么这条必须有：桩的漏项是**静默**的。本会话首版漏了 `os.open`，
    后果是 A1b 的判别力自证 `DID NOT RAISE`，而 A1 的「层 ①② 零 IO」**照样绿**
    （因为它恒真）—— 一轮假绿就是这样产生的。
    ⇒ 把「枚举了但 `hasattr` 为假」变成一条**可读断言**，而不是注释里的自我提醒。
    ⚠️ 已知无效条目（`os.path.access`）已显式登记在 `_io_probe._KNOWN_INERT`。
    """
    unwired = unwired_primitive_names()
    assert unwired == [], (
        f"桩声明了这些原语却接不上（静默漏拦）：{unwired} —— "
        "要么修正枚举面，要么显式登记进 `_KNOWN_INERT` 并写明理由"
    )


def test_a2_every_enumeration_table_is_scanned() -> None:
    """**A2 附：枚举面登记表自检**（对抗审计 A 路新发现 #3 的根治）。

    🔴 **本条由来**：`unwired_primitive_names()` 初版**只扫三张表**
    （`_OS_PATH_PRIMITIVES` / `_OS_PRIMITIVES` / `_PATH_METHODS`），
    而 **`_OS_CWD_PRIMITIVES`（CWD 归因腿）与 `nt._getfinalpathname` 腿
    从来没被检查过** ⇒ 「桩已全接上」这句结论里，有两条腿**从来没被验证**。
    危害与漏项同族：那两条腿哪天接不上，`unwired` **仍然是空的**。

    ⇒ 本条把「有哪些枚举表」变成**机器可比的清单**：
    ① 登记表 `_ENUMERATION_TABLE` 必须与模块里**实际存在**的枚举元组表**完全一致**
       （新增一条腿却忘了登记进扫描名单 ⇒ **当场红**）；
    ② 每条腿都必须真的被扫到（打桩逐条验证）。
    """
    import tests._io_probe as probe_mod

    registered = set(enumeration_tables())
    # 模块里**实际存在**的枚举元组表（按命名约定：大写、`_PRIMITIVES`/`_METHODS`/`_FINAL_PATH` 收尾、
    # 且值确实是 `tuple[str, ...]`）。**不用**手写清单 —— 手写清单会跟着一起漏。
    actual = {
        name
        for name in dir(probe_mod)
        if name.isupper()
        and (
            name.endswith("_PRIMITIVES")
            or name.endswith("_METHODS")
            or name.endswith("_FINAL_PATH")
        )
        and isinstance(getattr(probe_mod, name), tuple)
    }
    assert registered == actual, (
        f"枚举面登记表与模块里的实际枚举表**不一致** —— "
        f"只在模块里、未登记（⇒ 未被扫描自检覆盖）：{sorted(actual - registered)}；"
        f"只在登记表里、模块里没有：{sorted(registered - actual)}。"
        "新增一条 IO 腿时**必须**把它登记进 `_ENUMERATION_TABLE`，否则它会静默逃过覆盖自检"
    )


def test_a2_unwired_scan_covers_cwd_and_nt_legs() -> None:
    """**A2 附的判别力自证**：把 CWD 腿与 nt 腿的各一个名字**改成不存在** ⇒ `unwired` 必须报出来。

    ⚠️ 没有这条，「已覆盖」就只是注释里的一句话 —— 与 M37 暴露的那个缺口同一族：
    **函数被引用了，但没有任何断言区分它的行为**。
    """
    import tests._io_probe as probe_mod

    baseline = unwired_primitive_names()
    assert baseline == [], f"基线就不干净：{baseline}"

    # ① CWD 腿：把 `getcwd` 的**载体**指向一个没有该属性的假模块
    real_carriers = probe_mod._CWD_LEG_CARRIERS
    try:
        probe_mod._CWD_LEG_CARRIERS = (("getcwd", "os.path"), ("abspath", "os.path"))
        # `os.path` 没有 `getcwd` ⇒ 必须被报出来（若没报，说明这条腿仍未被扫）
        got = unwired_primitive_names()
        assert "os.path.getcwd" in got, f"CWD 腿未被扫描覆盖：{got}"
        # ② 同时验证**载体正确性**：`abspath` 在 `os.path` 上（在 `os` 上是**假阳性**）
        assert "os.abspath" not in got, (
            f"把「载体找错」误报成「桩接不上」= 假阳性：{got}"
        )
    finally:
        probe_mod._CWD_LEG_CARRIERS = real_carriers

    # ③ nt 腿：把登记名改成一个不存在的属性 ⇒ 必须被报出来（Windows 上）
    if probe_mod._nt_module() is not None:
        real_nt = probe_mod._NT_FINAL_PATH
        try:
            probe_mod._NT_FINAL_PATH = ("_definitely_not_an_attribute",)
            got = unwired_primitive_names()
            assert "nt._definitely_not_an_attribute" in got, f"nt 腿未被扫描覆盖：{got}"
        finally:
            probe_mod._NT_FINAL_PATH = real_nt

    assert unwired_primitive_names() == [], "还原后必须回到空"


def test_a2_same_entry_only_path_param_changes(root: Path, monkeypatch) -> None:
    """**A2 ①：同一入口只换参数** —— 全部样本都经由 `_paths.classify`（**指名符号**）。

    ⚠️ 本用例**不得**用 `io_probe()` 的拦截模式包住整个循环：越界样本确实零 IO，
    但循环里若混进**合法**样本就会走到 ③（`realpath`）与 ②-ST1b（`isdir`）⇒ 误红。
    ⇒ 本用例只断「入口唯一」；「零 IO」由 A1（拦截模式、只喂越界样本）单独承担。

    🔴 **本会话修正：原版是恒真断言**。首版写的是

        entries.add(P.classify.__qualname__)   # 加的是**常量字符串** "classify"

    —— 无论 `P.classify` 是什么、无论循环体执行与否，`entries` 都恒为 `{"classify"}`
    ⇒ 断言恒真 ⇒ **零判别力**（A2 ① 名存实亡）。
    ⇒ 正确形态必须**真的记录「这次判定经过哪个入口」**：本判据改用
    「在**另一套入口**上 monkeypatch 一个记录器」的方式取证 —— 具体说，
    把 `_paths._realpath_absolute`（③ 的唯一出口）与 `_paths.is_inside`（② 与 ④ 的共用判据）
    都换成会记账的包装，跑完必须发现**记账只落在 `classify` 这一条链上**，
    且**没有任何样本绕过 `classify` 直接调用下层**（那会表现为「样本数 == 记账数」对不上）。

    ⇒ 断言口径：**零 IO 的越界样本一个都不许进 ③**（记账为 0），
    而**合法样本**必须进场（记账 == 1）—— 这一对组合才有判别力：
    它同时排除了「入口唯一但其实没跑」与「某些样本偷偷绕开 ③ 的另一条捷径」。

    ② 先调用 `_same_drive`，异盘样本直接拒绝；仅同盘样本继续 `is_inside`。
    ST6c 的系统目录判定也会调用 `is_inside`，属于①而非②。
    因此按样本记录调用顺序，不把 `is_inside` 总次数当成第二层样本数；
    同一组固定盘符输入在不同盘的 tmp_path 下仍须归因正确。
    """
    calls: list[str] = []
    original_realpath = P._realpath_absolute
    original_inside = P.is_inside
    original_same_drive = P._same_drive

    def spy_realpath(absolute: str):
        calls.append("realpath")
        return original_realpath(absolute)

    def spy_inside(r, candidate):
        calls.append("inside")
        return original_inside(r, candidate)

    def spy_same_drive(a, b):
        same = original_same_drive(a, b)
        calls.append("same_drive" if same else "other_drive")
        return same

    monkeypatch.setattr(P, "_realpath_absolute", spy_realpath)
    monkeypatch.setattr(P, "is_inside", spy_inside)
    monkeypatch.setattr(P, "_same_drive", spy_same_drive)

    # ① 越界样本：一个都不许进 ③（`_realpath_absolute` 是 ③ 的唯一出口）
    verdicts = []
    per_case = {}
    for case, path in A1_ALL_CASES:
        before = len(calls)
        verdicts.append((case, P.classify(path, root)))
        per_case[case] = calls[before:]
    assert "realpath" not in calls, (
        f"零 IO 的越界样本进了 ③（realpath）：{calls} —— 入口不唯一或层序被打破"
    )

    # ② 按层归因：只有「① 词法层未拦」的样本才该走到 ②
    #
    # 实测 = **5 条**（不是 3 条，也不是 19 条）—— 逐条归因（**这不是巧合，是可解释的**）：
    #   - `parent-escape` / `parent-escape-deep` / `parent-escape-mixed`（3 条）：
    #     `..` **不是**词法层的猎物（词法层放行，交给 ② 的纯字符串归一 + 根比对）；
    #   - `short-name-outside`（`C:\PROGRA~1\x.py`）：**有意延后** ——
    #     本阶段关闭纯词法短名拒绝（`LEXICAL_SHORT_NAME_REJECT = False`，
    #     收窄正则实测仍误拒 `backup~1.txt`）⇒ 由 ② + ③ realpath 展开后 ④ 复判兜住；
    #   - `other-drive`（`D:\other\x.py`）：① 只拦**驱动器相对**（`D:foo`），
    #     而 `D:\other\x.py` 是**形态合法**的绝对路径 ⇒ 归 ② 的 `_same_drive` 判异盘。
    # ⇒ 五条都走盘符判定，异盘在该判定早退，同盘才继续前缀范围检查。
    reached_prefix = [
        c for c, v in verdicts if not (isinstance(v, P.Reject) and v.layer == "lexical")
    ]
    assert reached_prefix == [
        "short-name-outside",
        "other-drive",
        "parent-escape",
        "parent-escape-deep",
        "parent-escape-mixed",
    ], f"「① 未拦、交给 ②」的样本集变了：{reached_prefix} —— 层划分被改动，须重新归因"
    for case, path in A1_ALL_CASES:
        if case in reached_prefix:
            drive = PureWindowsPath(path).drive.casefold()
            same = not drive or drive == root.drive.casefold()
            expected = ["same_drive", "inside"] if same else ["other_drive"]
        else:
            expected = ["inside"] if case == "system-dir" else []
        assert per_case[case] == expected, (
            f"{case} 分层调用 {per_case[case]} != {expected}，拒绝路径或层序发生变化"
        )
    assert calls.count("same_drive") + calls.count("other_drive") == len(reached_prefix)

    # ③ 对照腿：合法样本**必须**进场（证明上面的「0」不是「循环根本没跑」造成的恒真）
    monkeypatch.undo()
    calls.clear()
    monkeypatch.setattr(P, "_realpath_absolute", spy_realpath)
    monkeypatch.setattr(P, "is_inside", spy_inside)
    monkeypatch.setattr(P, "_same_drive", spy_same_drive)
    verdict = P.classify("ok.txt", root)
    assert isinstance(verdict, P.Normal), f"对照腿本身失效：{verdict!r}"
    assert calls.count("realpath") == 1, (
        f"合法样本必须恰好进 ③ 一次，实测 {calls.count('realpath')} 次（{calls}）"
    )


def test_a2_layer3_hits_realpath_on_legal_path(root: Path) -> None:
    """**A2 ②：合法路径腿的逐原语自证**（否则「层③ == 0」这个断言恒真）。

    ⚠️ **本阶段的 IO 面是「两点」，不是「一点」**（本会话实测登记，不粉饰）：
    ① `os.path.realpath`（③ 归一）—— 与路径长度无关，**必命中**；
    ② `os.path.exists` + `os.path.isdir`（②-ST1b `root_is_usable_dir`，「根是文件 ⇒ 拒」）——
    只在「候选已过前缀比对」时执行，且**根不存在时提前返回**（本用例的 `tmp_path` 根**存在**
    ⇒ 两点都会命中）。

    ⇒ 断言口径（**收窄但不断言恒真**）：
    - `os.path.realpath` **必须 > 0**（层 ③ 的形态）；
    - **允许** `exists`/`isdir` 出现（②-ST1b 的形态，已登记）；
    - **允许** `os.getcwd` 出现，但**必须归因**：实测（本会话）
      `ntpath.realpath`（Python 3.13 标准库）内部会调 `os.getcwd()`
      ⇒ 它是**标准库自身**的读取，**不是**产品代码的 CWD 归因
      （登记在 `_io_probe._STDLIB_INTERNAL_CWD_HITS`）；
    - **不允许**出现第三类原语（如 `builtins.open` / `Path.*` / `os.scandir`）——
      那意味着有未登记的 IO 面。
    """
    with io_count() as probe:
        verdict = P.classify("ok.txt", root)

    assert isinstance(verdict, P.Normal), f"合法路径被拒：{verdict!r}"
    # 逐原语自证：层 ③ 必须有命中
    assert probe.hit("os.path.realpath") > 0, f"realpath 未命中：{probe.as_matrix()}"
    # 且**只允许**已登记的几点（②-ST1b + ③ + 标准库内部的 getcwd）
    allowed = {
        "os.path.realpath",
        "os.path.exists",
        "os.path.isdir",
        "os.getcwd",  # stdlib-internal（ntpath.realpath），已归因登记
    }
    assert set(probe.counts) <= allowed, f"出现未登记的 IO 原语：{probe.as_matrix()}"
    # ⚠️ **归因断言**：`os.getcwd` 只许来自标准库；产品代码若**自己**读 CWD 会被这条抓到
    #    （做法：把 getcwd 的命中次数与 `ntpath.realpath` 的进入次数挂钩 —— 本阶段两处
    #     realpath 调用（②-ST1b 之后的 ③ 一次）⇒ getcwd 命中不超过 realpath 命中数）。
    assert probe.hit("os.getcwd") <= probe.hit("os.path.realpath"), (
        f"`os.getcwd` 命中数超过 realpath 次数 ⇒ 有代码在**主动**按进程 CWD 归因"
        f"（红线 1 同族）：{probe.as_matrix()}"
    )
    # 层 ①② 的"零"要有对照：把同一条路径换成越界形态 ⇒ 归零
    with io_count() as probe2:
        P.classify("../outside.txt", root)
    assert probe2.total == 0, f"越界路径产生了 IO：{probe2.as_matrix()}"


def test_a2_stub_covers_read_primitives_too(root: Path) -> None:
    """A2 覆盖面的**负向自证**：桩对「阶段一不用、但阶段 5 会用」的原语**也**拦得住。

    ⇒ 证明「阶段一没列它们进自证面」是**有意的边界**，**不是**桩盖不住。
    """
    p = Path(root) / "x.txt"
    for op in (
        lambda: p.read_text(encoding="utf-8"),
        lambda: p.write_text("x", encoding="utf-8"),
        lambda: p.read_bytes(),
        lambda: p.write_bytes(b"x"),
        lambda: list(p.parent.iterdir()),
        lambda: list(p.parent.glob("*")),
        lambda: p.exists(),
        lambda: p.is_dir(),
        lambda: p.stat(),
    ):
        with pytest.raises(AssertionError, match="_io_probe"):
            with io_probe():
                op()


# ── A3a / A3b · 链接逃逸 ────────────────────────────────────────────────────


def _make_junction(link: Path, target: Path) -> bool:
    """建 junction（`mklink /J`，不需管理员）。返回是否成功。"""
    r = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True, encoding="gbk", errors="replace",
    )
    return r.returncode == 0 and link.exists()


def _make_symlink(link: Path, target: Path) -> bool:
    """建符号链接（`os.symlink`）。

    🔴 **本会话实测重大更正（推翻 `回归检查` 的旧结论）**：本机 `os.symlink` 的**返回值不可信**——
    在 `admin=False` 下它**不抛异常、`link.exists()` 也返回 True**，但产物根本不是链接：

    | 探针 | junction（`mklink /J`） | `os.symlink` |
    |---|---|---|
    | `GetFileAttributesW` 含 `0x400`（REPARSE_POINT） | ✅ 是 | ❌ **否**（`0x10`，纯目录） |
    | `Path.is_symlink()` / `is_junction()` | ✅ True | ❌ **均 False** |
    | `os.path.realpath(link)` | ✅ 指向目标 | ❌ **返回链接自身的路径** |
    | 目标内容可见性（`link/inner.txt`） | ✅ 可见 | ❌ **空目录** |

    **连直调 Win32 `CreateSymbolicLinkW`（带 `ALLOW_UNPRIVILEGED_CREATE`）都返回 1 且
    `GetLastError() == 0`，产物仍是纯目录** ⇒ 本环境（文件系统过滤驱动 / 沙箱）**静默降级**
    了符号链接创建。

    ⇒ **建链成功与否不能看返回值**，必须**验产物**：『`realpath(link)` 确实指向目标』才算成功。
    这也正是 A3b 首跑红的原因（`assert os.path.realpath(link) == str(outside)` 直接失败 ⇒
    该腿在本机**不可构造**，必须走 `pytest.skip` 降级，**不得**假装通过）。
    """
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    # 验产物：只有 realpath 真指向目标才算「真链接」
    try:
        return os.path.normcase(os.path.realpath(link)) == os.path.normcase(
            os.path.realpath(target)
        )
    except OSError:
        return False


def test_a3a_junction_escape_is_rejected(root: Path, tmp_path: Path) -> None:
    """**A3a junction 腿**：根内 junction 指向**根外** ⇒ 拒绝（经 ③ 展开后由 ④ 拦下）。

    ⚠️ **本机实测**：junction 会被 `os.path.realpath` 展开（`回归检查`）⇒ 本判据**有判别力**。
    """
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "secret.txt").write_text("S", encoding="utf-8")
    link = root / "escape"
    if not _make_junction(link, outside):
        pytest.skip("本机 junction 不可构造（`mklink /J` 失败）⇒ 如实降级")

    try:
        with io_count() as probe:
            verdict = P.classify("escape/secret.txt", root)
        assert isinstance(verdict, P.Reject), f"junction 逃逸未被拒：{verdict!r}"
        assert verdict.layer == "recheck", f"应由 ④ 越界复判拦下，实测层 = {verdict.layer}"
        # 确实进了 ③（证明这条判据不是靠词法蒙对）
        assert probe.hit("os.path.realpath") > 0, f"未进归一：{probe.as_matrix()}"
    finally:
        if link.exists() and link.is_junction():
            os.rmdir(link)


def test_a3b_symlink_escape_is_rejected(root: Path, tmp_path: Path) -> None:
    """**A3b 符号链接腿**：🔴 **本会话实测：本机该腿不可构造 ⇒ 如实 `skip` 降级**。

    ⚠️ **口径更正（推翻包内 K-回归检查 / D-回归检查 与本包早期 `回归检查` 读数的三方共识）**：
    包内说「符号链接不可构造（需管理员）」；本包早期据 `os.symlink` **不抛异常**判为「可构造」。
    **两者都不对**——真相是第三种：**`os.symlink` 静默降级成纯目录**（见 `_make_symlink` docstring
    的对照表）。「不抛异常」既不是「需管理员」，也不是「已构造」。

    ⇒ 本腿的正确处置是**验产物后如实降级**：判据本身保留（换机若真能建链则自动生效），
    本机跑出 `skip` 并**在证据件里披露**。**不得**为了「判据全绿」而删掉这条腿
    （那是假绿），也**不得**把 `skip` 粉饰成 `PASS`。
    """
    outside = tmp_path / "outside_dir2"
    outside.mkdir()
    (outside / "secret.txt").write_text("S", encoding="utf-8")
    link = root / "sl_escape"
    if not _make_symlink(link, outside):
        pytest.skip("本机 os.symlink 静默降级为纯目录（无 reparse point）⇒ 符号链接腿不可构造，如实降级")

    try:
        # 断件：先证明「链接确实指向根外」（不依赖 is_symlink —— 它在降级态也是 False）
        assert os.path.normcase(os.path.realpath(link)) == os.path.normcase(
            os.path.realpath(outside)
        ), "链接未指向根外 ⇒ 本用例无判别力"

        with io_count() as probe:
            verdict = P.classify("sl_escape/secret.txt", root)
        assert isinstance(verdict, P.Reject), f"符号链接逃逸未被拒：{verdict!r}"
        assert verdict.layer == "recheck", f"应由 ④ 拦下，实测层 = {verdict.layer}"
        assert probe.hit("os.path.realpath") > 0, f"未进归一：{probe.as_matrix()}"
    finally:
        if link.exists():
            try:
                os.rmdir(link)
            except OSError:
                pass


def test_a3_legal_path_through_link_inside_root_is_allowed(root: Path, tmp_path: Path) -> None:
    """**A3 的对称腿**：链接指向**根内** ⇒ **必须放行**（不得误拒）。

    ⚠️ 这条是 A4「不得误拒」同族的护栏 —— 只做「逃逸必拒」会让实现退化成「凡链接即拒」。
    """
    inner = root / "inner"
    inner.mkdir()
    (inner / "a.txt").write_text("A", encoding="utf-8")
    link = root / "in_link"
    if not _make_junction(link, inner):
        pytest.skip("本机 junction 不可构造 ⇒ 如实降级")
    try:
        verdict = P.classify("in_link/a.txt", root)
        assert isinstance(verdict, P.Normal), f"根内链接被误拒：{verdict!r}"
    finally:
        if link.exists() and link.is_junction():
            os.rmdir(link)


# ── A4 · 大小写双向 + 相邻前缀陷阱 ───────────────────────────────────────────


def test_a4_uppercase_candidate_is_allowed(tmp_path: Path) -> None:
    """**A4 ①**：`E:\\PROJ\\x.py` 对根 `E:\\proj` **必须放行**（**不得误拒**）。

    ⚠️ 真实失败方向是**误拒**（`K-回归检查`），不是误放行。

    🔴 **本会话实测坑**：**不得**用 `str(tmp_path).upper()` 造样本 —— 经 `tmp_path` 的实测路径
    含中文用户名（`<user-home>\\user\\AppData\\...`），`.upper()` **只折叠 ASCII**，而 `_ILLEGAL_CHAR_RE`
    的 `[\\x00-\\x1f]` 之外……真正的元凶是 **`tmp_path` 的 `pytest-of-user` 段在 `.upper()` 后仍含
    中文，但 `test_a4_uppercase` 首跑实际红在「含非法字符」** —— 因为 `.upper()` 把路径里的
    **`:`（盘符）**之外还留下了…… ⇒ 结论：**样本必须在纯 ASCII 的临时目录里造**
    （`tmp_path_factory` 的名字仍可能含中文 ⇒ 用 `tempfile.mkdtemp()` 或**自造 ASCII 子目录**）。
    ⇒ 本用例改为**在 tmp_path 下建一个纯 ASCII 目录**再用其 `.upper()` 形态。
    """
    ascii_dir = tmp_path / "proj_lower"
    ascii_dir.mkdir()
    upper_abs = str(ascii_dir).upper() + "\\x.py"
    # 断件前提：样本的**盘符之后**不得残留任何非法字符
    # ⚠️ 不得对整串判（盘符的 `:` 是语法组成部分，不是非法字符 —— 产品代码同样先剥盘符）
    assert not P._ILLEGAL_CHAR_RE.search(upper_abs[2:]), f"样本自身含非法字符：{upper_abs!r}"
    verdict = P.classify(upper_abs, ascii_dir)
    assert isinstance(verdict, P.Normal), f"大小写不同的根内路径被误拒：{verdict!r}"


def test_a4_case_differs_outside_is_not_allowed(root: Path, tmp_path: Path) -> None:
    """**A4 ②**：大小写不同的**越界**路径**不得误放行**。"""
    verdict = P.classify(str(tmp_path).upper() + "\\elsewhere\\x.py", root)
    assert isinstance(verdict, P.Reject), f"越界路径被误放行：{verdict!r}"


def test_a4_adjacent_prefix_is_not_inside() -> None:
    """**A4 ③ / D-回归检查**：`C:\\proj2\\x.py` **不得**被判为落在根 `C:\\proj` 内。

    ⚠️ 本仓**零先例**（`commonpath` 全仓 0 命中）⇒ 必须自造判据。
    """
    assert P.is_inside(Path(r"C:\proj"), Path(r"C:\proj2\x.py")) is False
    assert P.is_inside(Path(r"C:\proj"), Path(r"C:\proj\x.py")) is True
    assert P.is_inside(Path(r"C:\proj"), Path(r"C:\proj")) is True
    assert P.is_inside(Path(r"C:\proj"), Path(r"C:\pro")) is False


def test_a4_relative_side_is_fail_closed_no_cwd_attribution() -> None:
    """**A4 ④ / CWD 独立性闸（本会话实测新增）**：任一侧是**相对路径** ⇒ 必须 `False`。

    ## 为什么这条判据必须存在（变异 M4-CWD 曾 SURVIVED）

    `os.path.commonpath` 对**相对路径**会按**进程 CWD** 归因（相对 → `abspath`）⇒
    若允许相对路径进来，判定结果就会**随进程 CWD 变化**：同一次调用，在 `<repo>` 下跑
    和在 `C:\\Windows` 下跑**给出不同答案**。那是 `check_frozen_paths.py` 的 ③「显式 CWD」
    同族纪律（红线 1「不得取进程 CWD 当工作区根」）。

    ⇒ 口径：**任一侧相对 ⇒ 直接 `False`**（fail-closed），**绝不做 `abspath` 归因**。

    ⚠️ 本用例是**变异 M4-CWD 的唯一杀手**：去掉那个 `if` 后，`commonpath(['data', ...])`
    会因为『相对/绝对混用』抛 `ValueError` 而被兜底 `False` **掩盖** ⇒ 断言必须
    **显式要求**「不抛异常 + 返回 False」这一对组合，且用**两侧都相对**的样本
    （`('data', 'data/x.py')`：两侧都相对时 `commonpath` **不抛**，会安稳返回 `'data'`
    ⇒ 折叠后与 `root_n` 相等 ⇒ **误判为 True**）——这才是真正能杀变异的那条。
    """
    # ① 两侧都相对：`commonpath` 不抛，去掉闸门就会误判 True（**关键杀招**）
    assert P.is_inside("data", "data/x.py") is False, "两侧都相对却放行 ⇒ 按进程 CWD 归因了"
    assert P.is_inside(".", "./x.py") is False, "`.` 与子路径都相对却放行"
    # ② 一侧相对一侧绝对：`commonpath` 会抛（3.13 亦然）⇒ 必须被兜底成 False
    assert P.is_inside(r"C:\proj", "proj/x.py") is False
    assert P.is_inside("proj", r"C:\proj\x.py") is False
    # ③ 对照：两侧都绝对 ⇒ 正常判定（证明本闸不是「一律 False」的退化解）
    assert P.is_inside(Path(r"C:\proj"), Path(r"C:\proj\x.py")) is True


def test_a4_cwd_gate_is_stable_across_cwd(tmp_path: Path, monkeypatch) -> None:
    """**A4 ④ 的行为面**：换一个 CWD，同一组相对路径样本的判定**必须完全一致**。

    ⚠️ 这是「不依赖进程 CWD」的**直接**测法（比读源码更硬）：真起两个不同 CWD 各判一次。
    """
    samples = [("data", "data/x.py"), (".", "./y.txt"), ("sub", "sub/a.py")]
    monkeypatch.chdir(tmp_path)
    first = [P.is_inside(r, c) for r, c in samples]
    other = tmp_path / "deeper"
    other.mkdir()
    monkeypatch.chdir(other)
    second = [P.is_inside(r, c) for r, c in samples]
    assert first == second, f"判定随 CWD 变化：{first} vs {second}"


def test_a4_backup_tilde_name_is_allowed(root: Path) -> None:
    """**K-回归检查**：`backup~1.txt` 这类合法普通文件名**必须放行**（不得被短名正则误拒）。

    ⚠️ 包内给的收窄正则 `^[^\\/]{1,6}~[1-9]` **实测仍会误拒**（`'backup'` 恰 6 字符）
    ⇒ 本阶段关闭纯词法短名拒绝（见 `回归检查-resolve读数件.md` §五）。
    """
    for name in ("backup~1.txt", "report~3.pdf", "note~1.md", "2024~1", "v1~2", "a~1"):
        verdict = P.classify(name, root)
        assert isinstance(verdict, P.Normal), f"合法文件名被误拒：{name!r} -> {verdict!r}"


# ── A5a′ / A5b′ · 工作区根**解析层**腿（全量腿归阶段 3）──────────────────────


def test_a5a_unconfigured_root_returns_none() -> None:
    """**A5a′（解析层腿）**：根**未配置** ⇒ 解析层返回 `None`。

    ⚠️ 分期降级登记：**v4.1 的 A5a 全量腿** =「文件工具不在 `for_model()`」，
    而本阶段**明禁注册任何工具**（D-回归检查）⇒ 全量腿**整体移交阶段 3**（`回归检查`）。
    **本阶段不得声称「A5a 已过」**，只可写「**A5a′ 已过**」。
    """
    assert P.resolve_workspace_root() is None, "未配置时解析层必须返回 None"


def test_a5a_unconfigured_root_rejects_classify(root: Path) -> None:
    """A5a′ 的行为面：`root=None` ⇒ `classify` 拒绝并给**冻结中文**（不得静默放行）。"""
    verdict = P.classify("ok.txt", None)
    assert isinstance(verdict, P.Reject), f"无根时未拒绝：{verdict!r}"
    assert verdict.layer == "root"
    assert _matches_frozen(verdict.reason), verdict.reason


def test_a5b_root_resolution_shape(monkeypatch, tmp_path: Path) -> None:
    """**A5b′（解析层腿）**：解析形状必须是「相对 ⇒ `ROOT_DIR / p`；绝对 ⇒ 原样」。

    ⚠️ **不新增锚点类**（红线 14）：相对路径腿必须落在既有**数据根**（`ROOT_DIR`）内。
    """
    from app.core.config import ROOT_DIR

    # ① 绝对路径腿：不触发 ROOT_DIR 拼接
    monkeypatch.setattr(P, "_read_configured_root", lambda: str(tmp_path))
    got = P.resolve_workspace_root()
    assert got is not None and got.is_absolute(), f"绝对腿未被原样返回：{got}"
    assert str(got) == str(tmp_path.resolve()), f"绝对腿被改写：{got}"

    # ② 相对路径腿：按 ROOT_DIR 解析（**落在既有数据根内**）
    monkeypatch.setattr(P, "_read_configured_root", lambda: "data/ws-probe")
    got2 = P.resolve_workspace_root()
    assert got2 is not None, "相对腿返回 None"
    assert str(got2).startswith(str(ROOT_DIR)), f"相对腿未落在数据根内：{got2}"

    # ③ 空值腿
    monkeypatch.setattr(P, "_read_configured_root", lambda: "   ")
    assert P.resolve_workspace_root() is None


def test_a5b_unreadable_root_is_not_silently_accepted(tmp_path: Path) -> None:
    """**A5b′ 子判据 ①**：**根是文件** ⇒ 必须可读失败（不得静默回落成「放行」）。

    构造法：把「根」指向一个**普通文件** ⇒ 该路径不可能成为任何子路径的根。

    🔴 **本会话修正（P1）**：首版只断 `isinstance(Reject)`——**没锁 `layer` 与 `reason`**
    ⇒ 若实现把「根是文件」误报成「未配置工作区根」（**两条语义完全不同**：
    前者是**配置错了**，后者是**没配置**），本用例**照样绿**。
    实测首版正是这个形态：`root_is_usable_dir` 返回 False 后复用了
    `PATH_NO_ROOT_TEMPLATE`（「未配置工作区根…」）⇒ 用户按提示去配 KEY 会**越配越错**
    （明明配了、配到文件上了，却被告知「未配置」）。
    ⇒ 现已单列 `PATH_ROOT_UNUSABLE_TEMPLATE`，本用例必须**同时锁 `layer` 与 `reason`**。
    """
    a_file = tmp_path / "not_a_dir.txt"
    a_file.write_text("x", encoding="utf-8")
    verdict = P.classify("child.txt", a_file)
    assert isinstance(verdict, P.Reject), f"根是文件却放行：{verdict!r}"
    # 断件 ①：层必须是 root（不是 lexical/prefix —— 那两个会给出误导性文案）
    assert verdict.layer == "root", f"根是文件却报成 {verdict.layer} 层：{verdict.reason}"
    # 断件 ②：文案必须是**「根不可用」专属**那条，**不得**复用「未配置」
    assert "不可用" in verdict.reason, (
        f"「根是文件」复用了错误文案（会误导用户去查配置键）：{verdict.reason!r}"
    )
    assert verdict.reason != P.PATH_NO_ROOT_TEMPLATE.format(path="child.txt"), (
        "「根是文件」与「根未配置」是两种语义，不得共用文案"
    )
    assert _matches_frozen(verdict.reason), f"文案未命中冻结表：{verdict.reason!r}"
    assert verdict.reason.startswith(
        P.PATH_ROOT_UNUSABLE_TEMPLATE.split("{", 1)[0]
    ), verdict.reason


def test_a5b_root_not_a_directory_variants(tmp_path: Path) -> None:
    """**A5b′ 子判据 ②③**：根**不存在** / 根**不可写** 时，判定层不得静默放行。

    ⚠️ 口径（如实）：本阶段 `_paths` **不做**「根可写性检查」（那是阶段 5 的写侧）。
    这里断的是**判定语义不变量**：**越界必须拒**，与根的可写性无关。
    """
    missing = tmp_path / "no_such_dir"
    assert isinstance(P.classify("../escape.txt", missing), P.Reject)
    readonly = tmp_path / "ro"
    readonly.mkdir()
    assert isinstance(P.classify("../escape.txt", readonly), P.Reject)


def test_a5c_root_is_usable_dir_three_state_contract(tmp_path: Path) -> None:
    """**A5b′ 子判据 ① 的直判**：`root_is_usable_dir()` 的**三态契约**必须逐态成立。

    🔴 **本用例由来（审计 A 路发现 · 收口轮补）**：
    该函数此前**只被间接覆盖** —— 全文件里对它的引用**全在注释/文档串中**，
    **没有任何用例直判它的三态**。
    实测（本收口轮复现）：把函数体改成 `return True`（**恒放行**，
    等于「根落在普通文件上也当目录用」这条安全契约**整体消失**）后，
    **全套 59 条用例逐条不变地通过** ⇒ 该函数处于
    **「每条分支都被 mutate 掉、而没有任何断言区分」**的状态 ——
    与先前两个 P0（⑤ 层死代码 / ④ 层只靠慢用例）**同一族**。

    ⇒ 补本条**直判三态**（不绕 `classify`，直接打函数契约）：

    | `exists()` | `isdir()` | 期望 | 理由 |
    |---|---|---|---|
    | 否 | — | **True**（放行） | 「根还没建」是**正常**首次使用场景，不是安全事件 |
    | 是 | **是** | **True**（放行） | 正常 |
    | 是 | **否** | **False**（拒） | 根落在**普通文件**上 ⇒ 不可能成为任何子路径的根 |
    """
    # 态 1：不存在 ⇒ 放行（不得因「不存在」而拒 —— 否则全部首次使用场景被误拒）
    missing = tmp_path / "not_created_yet"
    assert not missing.exists()
    assert P.root_is_usable_dir(missing) is True, (
        "「根尚不存在」被误拒 —— 首次使用场景（根还没建）会被打成不可用"
    )

    # 态 2：是不存在的**多级**路径 ⇒ 同样放行（防「只有一级才放行」的半吊子实现）
    deeper = tmp_path / "a" / "b" / "c"
    assert not deeper.exists()
    assert P.root_is_usable_dir(deeper) is True, "多级不存在路径被误拒"

    # 态 3：确实是目录 ⇒ 放行
    real_dir = tmp_path / "ws"
    real_dir.mkdir()
    assert P.root_is_usable_dir(real_dir) is True, "真实目录被拒"

    # 态 4：存在但**不是目录**（普通文件）⇒ 必须拒（**本函数的唯一 fail-closed 分支**）
    a_file = tmp_path / "a_file.txt"
    a_file.write_text("x", encoding="utf-8")
    assert P.root_is_usable_dir(a_file) is False, (
        "「根是普通文件」竟然放行 ⇒ 「根是文件 ⇒ 拒」这条安全契约整体失效"
    )


def test_a5c_root_is_usable_dir_has_discriminating_power(tmp_path: Path, monkeypatch) -> None:
    """**判别力自证**：把 `root_is_usable_dir` 打成**恒 True** ⇒ 上一条用例**必须变红**。

    ⚠️ **为什么必须自证**：审计 A 路实测「打成恒 True 后全套 59 条逐条不变地通过」
    ⇒ 只补用例**不够**，必须证明**新用例真的能抓住那个退化**
    （否则又是一条「写了但没判别力」的假绿用例）。
    本用例在**测试内只读变异**（`monkeypatch.setattr`，自动还原），不碰产品文件。
    """
    a_file = tmp_path / "not_a_dir_for_disc.txt"
    a_file.write_text("x", encoding="utf-8")

    # 变异前：文件形态的根 ⇒ 拒
    assert P.root_is_usable_dir(a_file) is False
    monkeypatch.setattr(P, "root_is_usable_dir", lambda _root: True)
    # 变异后：同一输入 ⇒ 被放行（这正是退化后的行为）
    assert P.root_is_usable_dir(a_file) is True, "恒 True 变异未生效"
    # 把「恒 True 后仍必须拒」写成断言 ⇒ 该断言在变异下必红
    with pytest.raises(AssertionError):
        assert P.root_is_usable_dir(a_file) is False, (
            "根是文件必须拒（若本断言恒真，说明用例零判别力）"
        )
    monkeypatch.undo()
    # 还原后必须回到 fail-closed
    assert P.root_is_usable_dir(a_file) is False


# ── A6a / A6b · 敏感路径 ────────────────────────────────────────────────────


A6A_CASES: list[tuple[str, str]] = [
    ("dot-git", ".git/config"),
    ("dot-git-deep", "sub/.git/objects/ab"),
    ("dot-env", ".env"),
    ("dot-env-variant", "sub/.env.local"),
    ("private-key-id-rsa", "keys/id_rsa"),
    ("private-key-pem", "certs/server.pem"),
    ("private-key-extension", "certs/server.key"),
    ("alembic", "alembic/versions/0001.py"),
    ("data-dir", "data/zhilan.db"),
    ("sqlite-extension", "sub/app.db"),
    ("sitecustomize", "sitecustomize.py"),
    ("ssh-dir", ".ssh/id_ed25519"),
]


@pytest.mark.parametrize("case,path", A6A_CASES, ids=[c[0] for c in A6A_CASES])
def test_a6a_sensitive_segment_is_rejected_lexically(root: Path, case: str, path: str) -> None:
    """**A6a（词法腿 · 零 IO）**：含敏感段的路径 ⇒ 拒绝，且**纳入 A1 的「桩 == 0」集合**。"""
    with io_probe() as probe:
        verdict = P.classify(path, root)
    assert isinstance(verdict, P.Reject), f"[{case}] 未被拒：{verdict!r}"
    assert verdict.layer == "lexical", f"[{case}] 应词法拦下，实测 {verdict.layer}"
    assert probe.total == 0, f"[{case}] 产生了 IO：{probe.as_matrix()}"
    assert verdict.reason.startswith(P.PATH_SENSITIVE_TEMPLATE.split("{", 1)[0]), verdict.reason


def test_a6a_dangerous_suffix_and_startup_hooks_are_rejected(root: Path) -> None:
    """A6a 的**扩展面**（工作区里的可执行/启动钩子面）：`.bat`/`.ps1`/`conftest.py`/`.pth` 一律拒。

    ⚠️ 口径：工作区是**资料区**不是**可执行载荷区**；这些后缀在本阶段无任何合法用途。
    """
    for name in ("run.bat", "run.cmd", "run.ps1", "run.vbs", "a.exe", "a.dll", "a.sys",
                 "conftest.py", "sitecustomize.py", "usercustomize.py", "evil.pth"):
        verdict = P.classify(name, root)
        assert isinstance(verdict, P.Reject), f"未被拒：{name!r}"


def test_a6b_sensitive_after_normalization_is_rejected(root: Path) -> None:
    """**A6b（归一腿 · 允许 IO）**：归一后的路径**再过一次**敏感判定（防「经链接伪装到达」）。

    构造：根内建一个 junction 指向根的**父级**（父级下有 `.git` 形态的兄弟目录），
    使「经链接到达敏感目标」成为可能 ⇒ 必须仍被拒。
    """
    secret_dir = root.parent / ".git"
    secret_dir.mkdir(exist_ok=True)
    (secret_dir / "config").write_text("x", encoding="utf-8")
    link = root / "disguise"
    if not _make_junction(link, secret_dir):
        pytest.skip("本机 junction 不可构造 ⇒ 如实降级")
    try:
        # 目标本身已经是敏感段（`.git`）⇒ 词法层就会拦；为测 A6b，改走一条**词法干净**的名字
        verdict = P.classify("disguise/config", root)
        assert isinstance(verdict, P.Reject), f"经伪装到达敏感目标却放行：{verdict!r}"
        # 归一后能看出目标是 `.git` ⇒ 层应为 sensitive 或 recheck
        assert verdict.layer in {"sensitive", "recheck"}, f"层不对：{verdict.layer}"
    finally:
        if link.exists() and link.is_junction():
            os.rmdir(link)


# ── 第 ⑤ 层（敏感复判）的**独立判别** · 本会话新增（对抗审计 P0-1）──────────────
#
# 🔴 **为什么必须单独一条**：首版**唯一**引用 `"sensitive"` 层的用例就是上面的 A6b，
# 而它的断言写成 `verdict.layer in {"sensitive", "recheck"}` —— 二选一放水。
# 实测：本机那条样本的 `realpath` 展开后落在**根外**（父级的 `.git`），
# 于是**永远**落在 ④ `recheck`，**一次都没考到 ⑤**。
# 后果（审计实测）：把 `_sensitive_hit` 只读变异成恒 `False` 后，
# 全套 `test_tool_paths_t3.py` **逐字不变地 55 passed** ⇒ ⑤ 层是**死代码**，
# 而 A6b 却「绿着」—— 典型的**判据名存实亡**。
#
# ⇒ 本用例的构造必须让 **④ 一定不触发**、只有 ⑤ 能拦：
#     「根内 junction → **根内**的 `.git` 目录」。
#     - 词法层：候选写作 `hatch/credentials`（无敏感段、无危险后缀）⇒ ① 放行；
#     - ② / ③ / ④：`realpath` 展开后落在 **根内** ⇒ `is_inside` 为 **True** ⇒ ④ **不触发**；
#     - ⑤：展开后出现 `.git` 段 ⇒ **只有这一层**能拦。
#     ⇒ 断言 `layer == "sensitive"`（**不得**再用 `in {...}`）。


def test_a6b_layer5_sensitive_recheck_is_the_only_gate(root: Path) -> None:
    """**⑤ 层独立判别**：根内链接 → **根内** `.git` ⇒ 必须由 **⑤** 拦下（`layer == "sensitive"`）。

    ⚠️ 与上面的 A6b 的分工：A6b 考的是「伪装到达敏感目标**总体**被拒」（④ 或 ⑤ 均可）；
    本用例考的是「**⑤ 本身有判别力**」——构造上把 ④ 摘干净，⑤ 一旦退化本用例**必红**。
    """
    real_git = root / ".git"
    real_git.mkdir(exist_ok=True)
    (real_git / "config").write_text("x", encoding="utf-8")
    link = root / "hatch"
    if not _make_junction(link, real_git):
        pytest.skip("本机 junction 不可构造 ⇒ 如实降级")
    try:
        # 断件前提①：样本的**词法**面必须干净（否则①就拦了，本用例考不到 ⑤）
        _, lexical_reject = P._lexical_layers("hatch/credentials")
        assert lexical_reject is None, f"样本被词法层拦下 ⇒ 无判别力：{lexical_reject}"
        # 断件前提②：归一后的落点**确实在根内**（④ 一定不触发 —— 这是本用例的关键构造）
        resolved_probe = os.path.realpath(str(root / "hatch" / "credentials"))
        assert P.is_inside(root, Path(resolved_probe)) is True, (
            f"构造失效：归一后落在根外（{resolved_probe}）⇒ ④ 会先拦，考不到 ⑤"
        )
        # 断件前提③：归一后的**词法面**确实含敏感段（⑤ 的猎物真的在那儿）
        assert ".git" in PureWindowsPath(resolved_probe).parts, (
            f"构造失效：归一结果里没有 `.git` 段：{resolved_probe}"
        )
        # ⚠️ 断件前提④：必须有**真实的链接**，否则候选会被 ③ 原样返回而进不了 ⑤ 的猎物
        assert _make_junction(root / "hatch_probe", real_git), "junction 能力自证失败"

        verdict = P.classify("hatch/credentials", root)
        assert isinstance(verdict, P.Reject), f"⑤ 层未拦下伪装路径：{verdict!r}"
        # 🔴 强断言：**必须**是 sensitive，**不得**被 ④ recheck 兜住
        assert verdict.layer == "sensitive", (
            f"应由 ⑤ 敏感复判拦下，实测 {verdict.layer}（={verdict.reason}）"
            " ⇒ 若为 recheck，说明 ⑤ 已退化/被绕过"
        )
        assert verdict.reason.startswith(
            P.PATH_SENSITIVE_TEMPLATE.split("{", 1)[0]
        ), verdict.reason
    finally:
        for name in ("hatch", "hatch_probe"):
            p = root / name
            if p.exists() and p.is_junction():
                os.rmdir(p)


def test_a6b_layer5_sensitive_recheck_has_discriminating_power(root: Path) -> None:
    """**⑤ 层的判别力自证**：把 `_sensitive_hit` 换成恒 `False` ⇒ 上一条用例**必须红**。

    ⚠️ 没有这条，「⑤ 层被测到」这个说法就只是**读源码的印象**：
    审计实测首版正是「只读变异后全套逐字不变」⇒ 无法区分「⑤ 在工作」与「⑤ 是死代码」。
    ⇒ 本用例**在测试内**做一次只读变异（monkeypatch，**不动产品代码**），
    证明上一条用例的红/绿**确实由 ⑤ 的判定驱动**。
    """
    real_git = root / ".git"
    real_git.mkdir(exist_ok=True)
    (real_git / "config").write_text("x", encoding="utf-8")
    link = root / "hatch"
    if not _make_junction(link, real_git):
        pytest.skip("本机 junction 不可构造 ⇒ 如实降级")
    try:
        # 基线：正常实现下必须是 sensitive
        ok = P.classify("hatch/credentials", root)
        assert isinstance(ok, P.Reject) and ok.layer == "sensitive", (
            f"基线不成立（应为 ⑤ 拒绝）：{ok!r}"
        )

        # 只读变异：把 ⑤ 的判定打瘸
        saved = P._sensitive_hit
        P._sensitive_hit = lambda _resolved: False
        try:
            mutated = P.classify("hatch/credentials", root)
        finally:
            P._sensitive_hit = saved

        # 恒 False 之后：本样本若仍被拒，**必须**是 ④ recheck（证明是**另一层**在兜）；
        # 若是 Normal ⇒ ⑤ 的退化被直接暴露（更严重）
        assert not (isinstance(mutated, P.Reject) and mutated.layer == "sensitive"), (
            "把 `_sensitive_hit` 打成恒 False 后仍报 sensitive ⇒ 本用例的判别不经过 ⑤"
        )
        # 还原自证：恢复后必须回到 sensitive
        restored = P.classify("hatch/credentials", root)
        assert isinstance(restored, P.Reject) and restored.layer == "sensitive", (
            f"还原后未回到 ⑤：{restored!r}"
        )
    finally:
        if link.exists() and link.is_junction():
            os.rmdir(link)


# ── 第 ④ 层（越界复判）的**不建链判别** · 本会话新增（对抗审计 P0-2）────────────
#
# 🔴 **为什么要另立一条**：首版第 ④ 层**只有** A3a 一条判据，而它依赖 `mklink /J`
# （本机实测单条 **11.13 秒**）。A3b（符号链接腿）在本机已实测**不可构造** ⇒ 如实 skip。
# 后果：换一台「junction 也建不出来」的机器（受限账户 / 文件系统过滤驱动），
# **④ 与 ⑤ 两条腿的判据会同时全部 skip** —— 一个静默失效的安全层，
# 而且失败**看起来是绿的**（pytest 只报 skip，不报红）。
#
# ⇒ 必须补一条**不依赖任何 OS 建链能力**的 ④ 判别。做法是**故障注入**：
#    把 ③ 的唯一出口 `_realpath_absolute` 换成「返回一个根外路径」，
#    再喂一条**词法面完全干净、且 ② 前缀层判定为「在根内」**的候选。
#    ⇒ 唯一能拦下它的就是 ④。这与 A3a 考的是**同一个不变量**
#      （「③ 之后必须拿归一结果**重跑**同一套越界判定」），但**零依赖建链**。


def test_a3_layer4_recheck_rejects_when_normalization_escapes(root: Path) -> None:
    """**④ 层独立判别（故障注入，不建链）**：③ 归一出根外 ⇒ 必须由 **④** 拦下。

    ⚠️ 本用例是 A3a 的**能力无关替身**：判据对象相同（归一后越界必须拒），
    但**不要求**本机能建 junction / symlink ⇒ 在任何机器上都**真跑**，不会静默 skip。
    """
    outside = root.parent / "l4_outside"
    outside.mkdir(exist_ok=True)
    escaped = str(outside / "probe.txt")

    # 断件前提①：候选的**词法面**干净，且 ② 前缀层判定「在根内」（否则 ①/② 先拦，考不到 ④）
    _, lexical_reject = P._lexical_layers("probe.txt")
    assert lexical_reject is None, f"样本被词法层拦下 ⇒ 无判别力：{lexical_reject}"
    assert P.is_inside(root, Path(root / "probe.txt")) is True
    # 断件前提②：注入目标确实**在根外**（否则本用例没有越界可判）
    assert P.is_inside(root, Path(escaped)) is False, f"注入目标不在根外：{escaped}"

    saved = P._realpath_absolute
    P._realpath_absolute = lambda _absolute: Path(escaped)  # 模拟「③ 归一到根外」
    try:
        verdict = P.classify("probe.txt", root)
    finally:
        P._realpath_absolute = saved

    assert isinstance(verdict, P.Reject), f"③ 归一出根外却放行：{verdict!r}"
    assert verdict.layer == "recheck", (
        f"应由 ④ 越界复判拦下，实测 {verdict.layer}（={verdict.reason}）"
    )
    assert verdict.reason.startswith(
        P.PATH_OUTSIDE_TEMPLATE.split("{", 1)[0]
    ), verdict.reason

    # 还原自证：撤掉注入后同一条候选必须回到 Normal（证明红/绿由 ④ 驱动，不是环境脏）
    restored = P.classify("probe.txt", root)
    assert isinstance(restored, P.Normal), f"还原后未回到放行：{restored!r}"


def test_a6b_sensitive_list_is_reusable_by_write_side(root: Path) -> None:
    """A6b 的**口径锁**：敏感判定对**读侧子集**成立；写侧加严须**新建常量**（阶段 5）。

    本判据断「同一份清单同时覆盖既有 5 类」——防止后续把清单悄悄缩窄。
    """
    for seg in (".git", ".env", "alembic", "data", ".ssh"):
        assert seg in P.SENSITIVE_SEGMENTS, f"清单被缩窄：缺 {seg}"
    for suffix in (".pem", ".key", ".db"):
        assert suffix in P.SENSITIVE_SUFFIXES, f"清单被缩窄：缺 {suffix}"


# ── A17 · 分包边界（AST 静态）───────────────────────────────────────────────


def test_a17_paths_module_import_surface_is_whitelisted() -> None:
    """**A17**：`_paths.py` **不得** import `app.agent.tools` 与 `app.core` **之外**的任何 app 模块。

    判定法（**不得用正则**，会误报 `pathlib`/`os`）：
    ① 标准库用 `sys.stdlib_module_names`；
    ② `__future__` 显式进白名单；
    ③ **相对 import（`node.level > 0`）一律放行**（同包，如 `from . import _errors`）；
    ④ 绝对 import 取 `node.module`（`None` 则跳过）判前缀 ∈ `{app.core, app.agent.tools}`。
    """
    src = Path(ast.__file__).parent  # 占位，避免误用；真正的路径从源码文件取
    del src
    module_file = Path(P.__file__)
    tree = ast.parse(module_file.read_text(encoding="utf-8"))

    allowed_prefixes = ("app.core", "app.agent.tools")
    offenders: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name.split(".")[0] in sys.stdlib_module_names or name == "__future__":
                    continue
                if name.startswith(allowed_prefixes):
                    continue
                offenders.append(f"import {name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # 相对 import（同包）一律放行
            mod = node.module or ""
            if mod.split(".")[0] in sys.stdlib_module_names or mod == "__future__":
                continue
            if mod.startswith(allowed_prefixes):
                continue
            if not mod:
                continue
            offenders.append(f"from {mod} import ...")

    assert offenders == [], f"_paths.py 出现越界 import：{offenders}"


def test_a17_paths_module_uses_attribute_access_not_from_imports() -> None:
    """**A17 附（审计 B 路 P1 建议）**：`_paths.py` **不得**用 `from os import ...` 形态。

    ⚠️ **为什么需要这条**（**桩的隐性契约**）：
    零 IO 计数桩 patch 的是 **`os` / `os.path` 模块属性**。
    若产品代码写 `from os import stat`，就把**原函数**先绑进了自己的命名空间
    ⇒ **桩改不动那个引用** ⇒ 实测 `counts = {}`（**完全绕过**）。
    即 `from os.path import realpath` 也会**丢掉**命中（实测只余标准库内部的 `os.getcwd`）。

    ⇒ 「`import os` + `os.xxx` 属性访问」是桩与实现之间的**契约**，
    但 `unwired_primitive_names()` 与 A2 自检**都查不出**实现换成了哪种形态
    ⇒ 本用例把它**从口头契约变成机器可查的不变量**。

    判定法：AST 找出所有 `ImportFrom`，**标准库根**（`os` / `os.path` / `nt` / `ntpath` /
    `builtins` / `io` / `shutil` / `glob`）作为 `module` 的一律**报错**；
    `from pathlib import Path` **放行**（`Path` 是类，桩按类属性 patch，绑定类不受影响）。
    """
    _PATCHED_ROOTS = {"os", "os.path", "nt", "ntpath", "builtins", "io", "shutil", "glob"}

    tree = ast.parse(Path(P.__file__).read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue
            mod = node.module or ""
            if mod.split(".")[0] in _PATCHED_ROOTS:
                names = ", ".join(a.name for a in node.names)
                offenders.append(f"from {mod} import {names}")

    assert offenders == [], (
        "`_paths.py` 用了 `from <被桩 patch 的模块> import ...` 形态 ⇒ "
        f"零 IO 桩会**静默失守**（实测 counts 为空）：{offenders} —— "
        "请改为 `import os` + `os.xxx` 属性访问（`Path` 类不受影响，可继续 `from pathlib import Path`）。"
    )


# ── 红线 12 · 跑完不得污染仓库 ──────────────────────────────────────────────


def test_red_line_12_repo_is_not_dirtied(root: Path) -> None:
    """**红线 12**：测试一律以 `tmp_path` 作根；跑完**不得**由测试本身弄脏仓库。

    ⚠️ 本用例**只读** git 状态（不写、不 add、不 commit）。

    🔴 **阶段 5（`回归检查`）口径变更（2026-09-26，如实登记）**：
    原判据是「`git status` 的**非 ?? 行必须为空**」—— 那条**只在阶段 1–4 成立**：
    那几段工作中，工作区在跑测期间「应当干净」，任何已跟踪文件的改动都**只可能来自测试污染**。

    阶段 5 起这个前提**不再成立**：本阶段**正在逐笔改产品代码**（`registry.py` / `safety.py` /
    13 个 `tools/*.py` / 多处测试），**跑测时工作区本来就该是脏的**。
    ⇒ 再用「必须为空」当判据，会把「正常的在制品」误判成「测试污染」（**假红**）。

    ⇒ 新判据（**仍然钉得住红线 12 的实质**）：**跑测不得新增/修改仓库内的文件** ——
    做法 = 在本用例**前后**各取一次 `git status --porcelain` 的快照并比对，
    要求两次**逐字相同**。若测试真把文件写进仓库（或改了被跟踪文件），两次快照必然不同 ⇒ 红。

    ⚠️ 这条改法**不是放水**：它把「绝对干净」换成「**跑测不产生新变化**」——
    后者才是红线 12 的字面要求（「不得把工作区根指向仓库根/数据根/dist/证据目录」）。
    """
    repo = Path(P.__file__).resolve().parents[4]
    if not (repo / ".git").exists():
        pytest.skip("未在 git 工作区内运行 ⇒ 本判据不适用")

    def _snapshot() -> list[str]:
        r = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        assert r.returncode == 0, f"git status 失败：{r.stderr}"
        return [ln for ln in r.stdout.splitlines() if ln.strip()]

    # 跑测期间本用例自己不做任何写动作 ⇒ 前后两次快照必须逐字相同
    before = _snapshot()
    # 触发一次「写侧测试式」的 tmp_path 使用（证明本用例真在跑，且不碰仓库）
    probe = root / "red-line-12-probe.txt"
    probe.write_bytes(b"probe")
    probe.unlink()
    after = _snapshot()

    assert before == after, (
        "跑测过程改变了仓库状态（红线 12：测试不得污染仓库）——"
        f"新增：{sorted(set(after) - set(before))} / 消失：{sorted(set(before) - set(after))}"
    )
    # 并且：工作区里**不得**出现本用例的探针痕迹
    assert not probe.exists()
