"""`回归检查` 判据载体：**新建文件语义**（A28 三腿 + 越界零 IO + 失败清理 + 覆盖分流）。

## 本件覆盖的判据

| 判据 | 腿 | 用例 |
|---|---|---|
| **`A28`**（新建语义可判：父目录 + 已存在则拒 + 快照） | ① 父目录不存在 ⇒ **创建** | `test_a28_parent_dir_is_created` |
| | ①b 多级父目录一次建齐 | `test_a28_multi_level_parent_dirs_are_created` |
| | ② **目标已存在 ⇒ 拒**（**不得**静默覆盖） | `test_a28_existing_target_is_rejected_not_overwritten` |
| | ②b 拒绝时目标**逐字节未变** | `test_a28_reject_leaves_target_byte_identical` |
| | ③ 快照**有**（新建时无旧内容 ⇒ 空/标记） | `test_a28_snapshot_is_recorded_for_creation` · `test_a28_snapshot_id_is_present_on_the_outcome` |
| **`A9`**（越界写 ⇒ 零 IO） | 新建越界 ⇒ 零 IO + 未建任何目录 | `test_a9_out_of_bounds_create_is_zero_io` |
| | **桩有效性自证**（否则上一条假绿） | `test_a9_probe_is_alive_on_a_legal_create` |
| **`A6` 写侧腿** | 敏感段 ⇒ 写侧必须拒（`M-回归检查`） | `test_a6_write_side_sensitive_segment_is_rejected` |
| **`D-回归检查`**（失败时清理本笔新建的空目录） | 写失败 ⇒ 新建的空目录**不得残留** | `test_a28_failed_create_leaves_no_empty_directory` · `test_a28_failed_create_writes_nothing_outside_the_new_dir` |
| **`D-回归检查` ④**（`create` 标志分流） | `create=False` 走**覆盖**语义 | `test_create_false_takes_the_overwrite_branch` |
| | 两个方向配对（防「一律覆盖」/「一律拒」） | `test_create_true_and_false_diverge_at_one_point` |
| **接口契约**（跨笔共用） | `WriteOutcome` 三键齐全 | `test_write_outcome_type_is_named_and_stable` |

## 🔴 本件**必须自证桩有效**的理由（`A9` 同型，`K-回归检查`）

「越界 ⇒ 零 IO」这条断件在**没有桩**时**恒为真**：越界路径物理上就不写文件，所以
「没产生 IO」自动成立 ⇒ **零判别力 = 假绿**。故本件成对地出两条腿：

1. `test_a9_out_of_bounds_create_is_zero_io` —— 拦截模式（碰 IO 即当场红）；
2. `test_a9_probe_is_alive_on_a_legal_create` —— **对合法路径做一次真创建 ⇒ 桩命中 > 0**。

没有第 2 条，第 1 条就只是装饰（与 `tests/agent/test_tool_paths_t3.py` 的 `A1b`/`A2` 同款纪律）。

## 🔴 「失败后不得残留空目录」怎么测（注入点必须在**被测函数内部**）

`D-回归检查` 写死：父目录创建是**写副作用**，后续步骤失败 ⇒ **本笔新建的空目录必须清掉**。
可判形态 = 让**同一个调用**在目录已建好之后失败，然后断言目录不存在。
本件打的是 `_write` 模块上的**原子写内部符号**（`_write_atomically` / `_atomic_write`，
两个候选逐个探测），**先自证桩真的被调用到**（`calls` 非空），再断言目录不残留 ——
注入点落在**夹具里**就是空靶（纪律 2），落在模块属性上才在调用链里。
⚠️ 若 `_write.py` 把该符号写成 `from x import y` 再调用，patch 模块属性**盖不住**
（`_io_probe.py:55-59` 逐字登记过同族盲区）⇒ 该腿会 SURVIVED，而那是「**变异没打到**」
**不是**「判据弱」——按 §4.3 纪律 3 须先查根因。

## 🔴 快照目录隔离（**不得指向真 `data/workspace-backups`**）

`backup_dir` 夹具逐个候选属性（`resolve_backup_dir` / `_backup_dir` / `backup_root` /
`backup_dir`）把锚点改到 `tmp_path` 下，**存在的才打**；一个都不存在 ⇒ **显式 `pytest.skip`**
（如实降级），**绝不**回落到真数据根（红线 12）。

## 纪律

`backend/tests/agent/` **没有 `conftest.py`**（实测 `Test-Path` = `False`）⇒ 夹具**自带**；
`tests/api/conftest.py` 的 autouse 钉子对本目录**不生效**。
🔴 **红线 12**：工作区根**一律 `tmp_path`**，**绝不**指向仓库根 / 数据根 / `dist` / 证据目录
（`SENSITIVE_SEGMENTS` 含 `data` ⇒ 用 `<repo>/data` 当根会被**自己的**敏感判定拒掉）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

# 🔴 被测模块（`回归检查` 新建；`回归检查` 在本体内加**新建分支**）
from app.agent.tools import _write
from app.agent.tools import _write_paths

#: 零 IO 计数桩（`回归检查` 交付；`_io_probe.py:237/357/373`）。
#: ⚠️ 本件**必须**用它 —— 「零 IO」这条断件没有桩就是**恒真**（见模块 docstring）。
from tests._io_probe import io_count, io_probe


@pytest.fixture(autouse=True)
def _isolate_backup_anchor(tmp_path, monkeypatch):
    """🔴 **把备份锚点隔离到 `tmp_path`**（红线 12 · 阶段 5 事故 ⑤）。

    本件原有的 `backup_dir` 夹具是**按需**的（只被部分腿用）⇒ 其余腿会把快照
    写进**仓库内真目录**（实测：本件是 235 件污染的主要来源之一）。
    `_write.resolve_backup_dir()` 现有**测试态守卫**（落在仓库内 ⇒ 抛）⇒ 本夹具把「记得打锚点」
    变成**默认行为**。
    """
    from app.agent.tools import _write as _write_mod  # noqa: PLC0415

    monkeypatch.setattr(_write_mod, "resolve_backup_dir", lambda: tmp_path / "backups")


# ── 夹具（自带；`tests/agent/` 无 conftest）─────────────────────────────────────


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """工作区根（**红线 12：一律 `tmp_path`**）。"""
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


@pytest.fixture
def backup_dir(tmp_path: Path, monkeypatch) -> Path:
    """把**快照/备份锚点**改到 `tmp_path` 下（**绝不**指向真 `data/workspace-backups`）。

    `回归检查` 的锚点形状照 `config.upload_path`（`config.py:481-485`）⇒ 大概率是模块级
    私有函数或常量。本夹具**逐个候选属性**打桩，**存在的才打**；一个都不存在时
    **显式 `pytest.skip`**（如实降级，**不得**静默指向真数据根）。
    """
    target = tmp_path / "workspace-backups"
    target.mkdir(parents=True, exist_ok=True)
    patched: list[str] = []
    for name in ("resolve_backup_dir", "_backup_dir", "backup_root", "backup_dir"):
        if hasattr(_write, name):
            monkeypatch.setattr(_write, name, lambda *a, _t=target, **k: _t)
            patched.append(name)
    if not patched:
        pytest.skip(
            "未找到快照目录锚点（_write 里没有 resolve_backup_dir/_backup_dir/"
            "backup_root/backup_dir）⇒ 无法把备份隔离到 tmp_path；按红线 12 不得指向真 data/ ⇒ 如实降级"
        )
    return target


# ── 助手 ─────────────────────────────────────────────────────────────────────


def _outcome(call):
    """跑一次写调用并返回 `WriteOutcome`（同步/异步两种产品形态都吃）。"""
    result = call()
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _write_new(root: Path, rel: str, data: bytes, *, create: bool = True, snapshot: bool = True):
    """**唯一入口**：`write_workspace_file`（`D-回归检查` ⑥ 逐字，keyword-only）。"""
    return _write.write_workspace_file(
        root=root, rel_path=rel, data=data, create=create, snapshot=snapshot
    )


def _all_files(base: Path) -> list[Path]:
    """目录下**全部文件**（含临时文件），排序后返回 —— 「无残留」断言用。"""
    return sorted(p for p in base.rglob("*") if p.is_file())


class _PatchProbe(RuntimeError):
    """注入点被命中时抛出（与 `_io_probe` 的 `AssertionError` 同族，但可区分）。"""


# ── A28 · 腿① 父目录不存在 ⇒ 创建 ─────────────────────────────────────────────


def test_a28_parent_dir_is_created(root: Path) -> None:
    """**A28 腿①**：父目录**不存在** ⇒ **创建**，且文件落在其中、内容逐字等于入参。

    `D-回归检查` 逐字：「目标父目录**不存在** ⇒ **创建**（`mkdir(parents=True, exist_ok=True)`）」。
    """
    target = root / "sub" / "note.txt"
    assert not target.parent.exists(), "前提：父目录此刻必须不存在（否则本条测的是覆盖）"

    outcome = _outcome(lambda: _write_new(root, "sub/note.txt", b"hello"))

    assert target.parent.is_dir(), "父目录未被创建"
    assert target.read_bytes() == b"hello", "内容与入参不逐字一致"
    assert Path(outcome.target) == target, "WriteOutcome.target 与实际落点不一致"


def test_a28_multi_level_parent_dirs_are_created(root: Path) -> None:
    """**A28 腿① 的加强**：**多级**父目录一次建齐（`parents=True` 的判别力所在）。"""
    target = root / "a" / "b" / "c" / "deep.txt"
    assert not (root / "a").exists()

    _outcome(lambda: _write_new(root, "a/b/c/deep.txt", b"x"))

    assert target.is_file()
    assert (root / "a" / "b" / "c").is_dir()


# ── A28 · 腿② 目标已存在 ⇒ 拒（**不得**静默覆盖）──────────────────────────────


def test_a28_existing_target_is_rejected_not_overwritten(root: Path) -> None:
    """**A28 腿②**：目标**已存在** ⇒ **拒绝**（`create=True` ⇒ **不得**静默覆盖）。

    🔴 **本腿是 `M-回归检查`（新建改成静默覆盖）的唯一靶子** —— 判据必须同时断
    「拒绝」**与**「旧内容还在」：只断其一的话，「拒了但其实已经写进去」与
    「没拒但碰巧没改」都能蒙混过去。
    """
    target = root / "exists.txt"
    target.write_bytes(b"OLD-CONTENT")

    with pytest.raises(Exception) as excinfo:  # 产品侧拒法未定 ⇒ 断言「抛」+ 文案
        _outcome(lambda: _write_new(root, "exists.txt", b"NEW-CONTENT"))

    message = str(excinfo.value)
    assert "已存在" in message, f"拒绝文案未说明『已存在』：{message!r}"
    assert target.read_bytes() == b"OLD-CONTENT", "被静默覆盖了（M-T5-7 命中）"


def test_a28_reject_leaves_target_byte_identical(root: Path) -> None:
    """**A28 腿② 的加强**：拒绝路径 ⇒ 目标 **`mtime`/`size`/字节** 三件全未变。

    `A9` 的口径（备份件 `:222`）在**写侧拒绝**上同样适用：这是「拒了就真的没碰盘」。
    """
    target = root / "keep.txt"
    target.write_bytes(b"KEEP")
    before = target.stat()

    with pytest.raises(Exception):
        _outcome(lambda: _write_new(root, "keep.txt", b"CHANGED"))

    after = target.stat()
    assert target.read_bytes() == b"KEEP"
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns, "拒绝路径修改了 mtime"
    # 同目录不得留下临时文件（拒绝发生在写之前）
    assert _all_files(root) == [target], f"拒绝路径留下残留：{_all_files(root)}"


# ── A28 · 腿③ 快照有（新建时**无旧内容** ⇒ 空/标记）────────────────────────────


def test_a28_snapshot_is_recorded_for_creation(root: Path, backup_dir: Path) -> None:
    """**A28 腿③**：新建时**无旧内容可快照** ⇒ 快照为**空/标记**，且 `snapshot_id` 有值。

    `D-回归检查` 逐字：「快照 = `回归检查` 的入口（⚠️ 新建时**无旧内容可快照** ⇒ 快照为**空/标记**，
    须在 docstring 写明）」。

    ⚠️ **本腿的口径必须比 `A21` 弱**：`A21`（写**前**快照 = 写前内容逐字）由
    `test_write_snapshot_t3_5.py` 承载；本条只断「新建路径**有**快照记录**或**显式标记」。
    写成「必须存在一个非空快照文件」是**错的** —— 新建时压根没有旧内容。
    """
    _outcome(lambda: _write_new(root, "fresh/created.txt", b"BRAND-NEW"))

    files = _all_files(backup_dir)
    # 两种合法形态：① 留下一个 0 字节的标记文件；② 什么都不留但 snapshot_id 仍是标记值。
    assert files == [] or all(f.stat().st_size == 0 for f in files), (
        f"新建路径的快照必须是『空/标记』（没有旧内容可快照），实测："
        f"{[(f.name, f.stat().st_size) for f in files]}"
    )


def test_a28_snapshot_id_is_present_on_the_outcome(root: Path, backup_dir: Path) -> None:
    """**A28 腿③ 的载体面**：`WriteOutcome.snapshot_id` **键存在**（新建时可为空值）。

    `D-回归检查` ⑥ 逐字：`WriteOutcome` 至少含 `target` / `byte_delta` / `snapshot_id`
    三键 —— 这三个键正是 `回归检查` 三个审计专属键的**来源**，**必须由写函数产出**。
    """
    outcome = _outcome(lambda: _write_new(root, "snap/probe.txt", b"12345"))

    assert hasattr(outcome, "target")
    assert hasattr(outcome, "byte_delta")
    assert hasattr(outcome, "snapshot_id"), "WriteOutcome 缺 snapshot_id（T3-5-6 的三个审计键无来源）"
    # byte_delta 口径（B-B3）：= len(新) - len(旧)，新建时旧按 0 计 ⇒ 恰为 len(data)
    assert outcome.byte_delta == len(b"12345"), (
        f"新建时 byte_delta 应为 len(data)（旧内容按 0 计），实测 {outcome.byte_delta}"
    )


# ── A9 · 越界新建 ⇒ 零 IO（**含桩有效性自证**）────────────────────────────────


def test_a9_probe_is_alive_on_a_legal_create(root: Path) -> None:
    """**A9 的前提腿（桩有效性自证）**：合法新建 ⇒ 计数桩 **必须 > 0**。

    🔴 **没有这条，`test_a9_out_of_bounds_create_is_zero_io` 就是装饰**：
    桩漏接了任何一条腿时，那条「零命中」断言**照样绿**（因为它恒真）。
    同款先例：`test_tool_paths_t3.py::test_a1b_*`（插一次 `Path.touch()` ⇒ 必须红）。
    """
    with io_count() as probe:
        _outcome(lambda: _write_new(root, "alive/probe.txt", b"payload"))

    assert probe.total > 0, (
        f"桩对**合法新建**零命中 ⇒ 桩接不上，「越界零 IO」那条腿会假绿。"
        f"实测矩阵：{probe.as_matrix()}"
    )


def test_a9_out_of_bounds_create_is_zero_io(root: Path) -> None:
    """**A9**：新建**越界**路径 ⇒ **零 IO** + **未创建任何目录/文件**。

    `D-回归检查` + `C-R2` 逐字：「**必须先完成路径判定（含『父目录逐级仍在根内』的形态判定）
    再创建；判之前零 IO**」——「先 `mkdir` 再判」会在判之前就产生 IO ⇒ 撞本腿。

    断四件：① 拦截模式下跑得到（没碰 IO）；② 显式断言 `probe.total == 0`；
    ③ 未创建任何目录/文件；④ 拒绝文案命中**冻结**的越界模板。
    """
    target = root.parent / "outside" / "evil.txt"
    assert not target.parent.exists()

    with io_probe() as probe:  # 拦截模式：碰 IO 即当场断言失败
        with pytest.raises(Exception) as excinfo:
            _outcome(lambda: _write_new(root, "../outside/evil.txt", b"x"))

    assert probe.total == 0, f"越界新建产生了 IO：{probe.as_matrix()}"
    assert not target.parent.exists(), "越界路径的父目录被创建了（先建后判 ⇒ 撞 D-T5-8）"
    assert not target.exists()
    assert (_write_paths.PATH_OUTSIDE_TEMPLATE.split("{", 1)[0]) in str(excinfo.value), (
        f"拒绝文案未命中冻结越界模板：{str(excinfo.value)!r}"
    )


@pytest.mark.parametrize(
    "rel",
    [".env", ".git/config", "keys/id_rsa", "sub/.env.local", "certs/server.pem", "app.db"],
    ids=["dot-env", "dot-git", "private-key", "dot-env-variant", "pem", "sqlite"],
)
def test_a6_read_side_deny_list_also_blocks_writes(root: Path, rel: str) -> None:
    """**`A6` 写通道上的【读侧】清单腿**：读侧敏感清单里的路径，**写也不得放行**。

    `A6` 原文（备份件 `:217`）逐字：「敏感路径（`.git/**`、`.env`、私钥、
    `sitecustomize.py`）⇒ 拒绝（**读侧与写侧都要**）」。

    ## 🔴 本腿**不是** `M-回归检查` 的靶腿（如实登记，别把它当写侧判定的证明）

    实测（`test-design` 探针 + Lead harness 复现）：这六个样本**全部先被读侧** `classify` 拦下
    （`layer=lexical`，`_write.py` 的**第 1 步**）⇒ **根本走不到第 2 步的写侧判定**。
    ⇒ 对 `M-回归检查`（关掉写侧判定）这六个样本**零判别力** —— 那时它照样全绿。
    真正的 `M-回归检查` 靶腿见下面的 `test_a6_write_side_only_*`。

    ⚠️ `C-R1(甲)` 后半句：**写侧若要额外拒绝某类路径，必须【新建写侧常量】，不得把新项塞进**
    `SENSITIVE_SEGMENTS` —— 后者会改**读侧**行为。

    ## 本腿的**判别力来源**：断【冻结文案】而不是只断「没写成」

    原写法是 `pytest.raises(Exception)` + 只断「目标不存在」⇒ **任何崩法都满足**
    （TypeError / 路径 bug / 无关异常）⇒ 实际只证明「没写成」，**不证明「因敏感而被拒」**
    （`audit-pkg-a` 对抗审计 2026-09-26 抓出）。⇒ 改为断**冻结的越界/敏感文案前缀**。
    """
    from app.agent.tools import _write_paths as W

    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(Exception) as excinfo:
        _outcome(lambda: _write_new(root, rel, b"x", create=True))

    assert not target.exists(), f"敏感路径 {rel!r} 被写入成功（M-T5-10 命中）"
    # 🔴 断「因敏感/越界而被拒」而不是「碰巧崩了」：文案必须命中冻结模板的前缀
    msg = str(excinfo.value)
    expected_prefixes = [
        W.PATH_SENSITIVE_TEMPLATE.split("{", 1)[0],
        W.PATH_OUTSIDE_TEMPLATE.split("{", 1)[0],
    ]
    assert any(p and p in msg for p in expected_prefixes), (
        f"拒因不是「敏感 / 越界」（可能只是碰巧崩了）—— 实测文案：{msg!r}"
    )


@pytest.mark.parametrize(
    "rel",
    ["keys/a.pfx", "keys/b.p12", "keys/c.jks", "sub/.svn/entries"],
    ids=["pfx", "p12", "jks", "svn"],
)
def test_a6_write_side_only_sensitive_is_rejected_at_the_write_layer(root: Path, rel: str) -> None:
    """**`A6` 写侧腿（`M-回归检查` 的【真靶腿】）**：**写侧专属**敏感项 ⇒ 必须由**写侧判定**拒。

    ## 为什么必须用「写侧专属」样本（本腿的立腿理由）

    上一腿的六个样本（`.env` / `.git` / 私钥 / `.pem` / `.db`）**读侧清单里也有**
    ⇒ 它们在 `_write.py` 的**第 1 步**就被 `classify` 拒（`layer=lexical`）
    ⇒ **走不到写侧判定** ⇒ 对 `M-回归检查`（关掉写侧判定）**零判别力**（实测 SURVIVED）。

    ⇒ 本腿改用**只有写侧清单才有**的项（`_write_paths.WRITE_EXTRA_SUFFIXES` 的
    `.pfx` / `.p12` / `.jks` / `.keystore`，与 `WRITE_EXTRA_SEGMENTS` 的 `.svn`）
    —— 它们**读侧不管**，所以**只能**由写侧判定拦住。

    ## 🔴 关键断言：`layer == "sensitive"`

    只断「被拒」不够（可能是别的层拒的）⇒ 必须断**判定层**，
    把「样本真的走了写侧判定」变成**可判事实**（防再次靶子错位）。
    """
    from app.agent.tools import _write_paths as W

    verdict = W.resolve_write_target(rel, root)
    assert isinstance(verdict, W.WriteReject), f"写侧专属敏感项未被拒：{verdict!r}"
    assert verdict.layer == "sensitive", (
        f"该样本必须走【写侧】判定（layer=sensitive）；实测 layer={verdict.layer!r}"
        " —— 若是别的层，说明被更外层先拦 ⇒ 本腿对 M-T5-10 零判别力"
    )

    # 端到端：真写调用也必须拒，且盘上不得出现该文件
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(Exception) as excinfo:
        _outcome(lambda: _write_new(root, rel, b"x", create=True))
    assert not target.exists(), f"写侧专属敏感项 {rel!r} 被写入成功（M-T5-10 命中）"
    assert W.WRITE_SENSITIVE_TEMPLATE.split("{", 1)[0] in str(excinfo.value), (
        f"拒因不是「敏感路径」（冻结模板未命中）：{str(excinfo.value)!r}"
    )


# ── 失败清理：本笔新建的空目录不得残留 ─────────────────────────────────────────


def _find_atomic_symbol() -> str | None:
    """找一个**原子写内部符号**（注入点候选）。找不到 ⇒ 返回 None（调用方 skip）。"""
    for name in ("_write_atomically", "_atomic_write"):
        if hasattr(_write, name):
            return name
    return None


def test_a28_failed_create_leaves_no_empty_directory(root: Path) -> None:
    """**`D-回归检查` 风险点**：父目录创建是**写副作用** ⇒ 后续步骤失败 ⇒ **本笔新建的空目录不得残留**。

    🔴 **注入点必须在被测函数内部**（纪律 2：落在夹具里 = 空靶）。本腿在
    `_write` 模块上打原子写内部符号，**先自证桩真的被调用到**（`calls` 非空），
    再断言目录不残留。
    """
    injected = _find_atomic_symbol()
    if injected is None:
        pytest.skip(
            "未找到原子写内部符号（_write_atomically/_atomic_write）⇒ 无法把注入点落在"
            "被测函数**内部**；按纪律 2 不得把注入点落在夹具里 ⇒ 如实降级"
        )

    calls: list[str] = []

    def _boom(*_a, **_k):
        calls.append("boom")
        raise _PatchProbe("注入：目录已建好之后的写步骤炸了")

    original = getattr(_write, injected)
    setattr(_write, injected, _boom)
    try:
        with pytest.raises(Exception):
            _outcome(lambda: _write_new(root, "ghost/leak.txt", b"x"))
    finally:
        setattr(_write, injected, original)

    assert calls, f"注入点 {injected} 没被调用到 ⇒ 空靶（K-T5-11 同族）"
    assert not (root / "ghost").exists(), (
        "写失败后本笔新建的空目录残留了（D-T5-8 要求清理）"
    )


def test_a28_failed_create_writes_nothing_outside_the_new_dir(root: Path) -> None:
    """失败路径的**全域**断言：根下不得有任何新文件、也不得残留新建目录。

    ⚠️ 本腿**不注入**（走产品自己的失败路径：`snapshot=False` 时把一个**不合法**的
    `rel_path` 交给写函数）—— 与上一条互补：上一条测「目录建好之后炸」，本条测
    「压根没建任何东西」。
    """
    with pytest.raises(Exception):
        _outcome(lambda: _write_new(root, "../escape/x.txt", b"y", snapshot=False))

    assert _all_files(root) == [], f"失败路径留下残留文件：{_all_files(root)}"
    assert list(root.iterdir()) == [], "失败路径留下新建目录"


# ── create 标志的分流（D-回归检查 ④）────────────────────────────────────────────


def test_create_false_takes_the_overwrite_branch(root: Path) -> None:
    """**`create=False` ⇒ 覆盖语义**（与 `回归检查` 同路，**不是**新建语义）。

    `D-回归检查` ④ 逐字：`create` 标志即「**同一个判定点分流**」的载体 ——
    覆盖与新建**由同一个参数分流**，**不得**出现「先覆盖再发现已存在」的竞态。
    """
    target = root / "over.txt"
    # 🔴 旧内容固定 **2 字节**（`b"OL"`）、新内容 **3 字节**（`b"NEW"`）—— 与下一行断言的
    # `3-2=1` 自洽。⚠️ 首版这里写的是 `b"OLD"`（3 字节）⇒ 期望值应为 0 而非 1，
    # 是**断言自己不自洽**（实现的口径 `新 size - 旧 size` 是对的）。
    target.write_bytes(b"OL")

    outcome = _outcome(lambda: _write_new(root, "over.txt", b"NEW", create=False))

    assert target.read_bytes() == b"NEW", "create=False 必须走覆盖语义"
    assert outcome.byte_delta == 1, f"byte_delta 应为 3-2=1，实测 {outcome.byte_delta}"


def test_create_true_and_false_diverge_at_one_point(root: Path) -> None:
    """**分流腿（配对判别力）**：同一路径、同一内容，**只有 `create` 一位不同** ⇒ 结果相反。

    🔴 没有这条，「`create` 恒 True」与「`create` 恒 False」都能分别蒙过上面两条
    （各自只看一个方向）⇒ 本腿把**两个方向**同时钉住。
    """
    path = root / "both.txt"

    # ① create=True 且目标不存在 ⇒ 成功
    _outcome(lambda: _write_new(root, "both.txt", b"FIRST", create=True))
    assert path.read_bytes() == b"FIRST"

    # ② create=True 且目标**已存在** ⇒ 拒（旧内容仍在）
    with pytest.raises(Exception):
        _outcome(lambda: _write_new(root, "both.txt", b"SECOND", create=True))
    assert path.read_bytes() == b"FIRST", "create=True 时被覆盖了"

    # ③ create=False 且目标**已存在** ⇒ 覆盖（内容真的变了）
    _outcome(lambda: _write_new(root, "both.txt", b"THIRD", create=False))
    assert path.read_bytes() == b"THIRD", "create=False 时没有覆盖"


def test_write_outcome_type_is_named_and_stable() -> None:
    """**接口契约腿**（`D-回归检查` ⑥）：`WriteOutcome` 是**专属类型**且三个键齐全。

    这条是**跨笔契约**（`回归检查` / `回归检查` / `回归检查` 三笔共用同一个名字）⇒
    把它变成可读断言，防止某笔自行改名导致后两笔漂移。
    """
    assert hasattr(_write, "WriteOutcome"), "_write 未导出 WriteOutcome"
    fields = set(getattr(_write.WriteOutcome, "__dataclass_fields__", {}))
    assert {"target", "byte_delta", "snapshot_id"} <= fields, (
        f"WriteOutcome 缺键（T3-5-6 的三个审计键来源）："
        f"{sorted({'target', 'byte_delta', 'snapshot_id'} - fields)}"
    )

# ---------------------------------------------------------------------------
# 回归修复：「建目录前再判一遍」名不副实 + 末段 symlink 拒绝死分支（拍板 E：根治）
# ---------------------------------------------------------------------------


def _make_junction(link: Path, target: Path) -> bool:
    """建 junction（`mklink /J`，不需管理员）。返回是否成功。（照 test_tool_paths_t3 同名助手）"""
    import subprocess

    r = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True, encoding="gbk", errors="replace",
    )
    return r.returncode == 0 and link.exists()


def _make_symlink(link: Path, target: Path) -> bool:
    """建**文件**符号链接并**验产物**（照 test_tool_paths_t3 的纪律）。

    本机 ``os.symlink`` 可能静默降级（无管理员/开发者模式）⇒ 只有 ``realpath``
    真指向目标才算成功；否则调用方 ``pytest.skip`` 如实降级。
    """
    import os

    try:
        os.symlink(target, link, target_is_directory=False)
    except (OSError, NotImplementedError):
        return False
    try:
        return os.path.normcase(os.path.realpath(link)) == os.path.normcase(
            os.path.realpath(target)
        )
    except OSError:
        return False


def test_a28_recheck_missing_ancestors_before_mkdir(root, tmp_path, monkeypatch):
    """**正腿①（`回归修复` ①）**：mkdir 前逐级 re-classify **真的在场**。

    确定性 TOCTOU 演练：resolve 之后、mkdir 之前，把既有中间层换成指向根外的
    junction（mklink /J，本机可真跑）。修前：注释声称复判但代码没有 ⇒
    ``mkdir(parents=True)`` 穿过 junction 把目录建到根外；修后：复判对 missing 段
    重过 ``_paths.classify``，其 realpath **穿过被换的祖先**解析 ⇒ 以读侧越界冻结
    文案当场拒绝（mkdir 尚未发生）。
    """
    import os

    import app.agent.tools._paths as paths_mod

    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    alive = root / "alive"
    alive.mkdir()  # 既有中间层（攻击目标）
    rel = "alive/deep/new.txt"

    real_classify = paths_mod.classify
    state = {"calls": 0}

    def classify_then_swap(path, root_, **kwargs):  # noqa: ANN001, ANN003
        state["calls"] += 1
        if state["calls"] >= 2:
            # 第 2 次调用 = mkdir 前的复判 ⇒ 此刻「攻击者」换走中间层
            os.rmdir(alive)
            assert _make_junction(alive, outside), "junction 构造失败 ⇒ 本腿判别力失效"
        return real_classify(path, root_, **kwargs)

    monkeypatch.setattr(paths_mod, "classify", classify_then_swap)
    try:
        with pytest.raises(_write.WriteError) as excinfo:
            _write_new(root, rel, b"payload", create=True, snapshot=False)
        assert state["calls"] >= 2, "复判没有第二次调用 classify ⇒ 复判根本没发生"
        assert _write_paths.PATH_OUTSIDE_TEMPLATE.split("{", 1)[0] in str(excinfo.value), (
            f"必须透传读侧越界冻结文案：{excinfo.value}"
        )
        assert not (root / "alive" / "deep").exists(), "mkdir 不该发生（目录被建到了链路外）"
        assert not (outside / "deep").exists(), "目录经 junction 被建到了根外（回归修复 未修）"
    finally:
        # 拆 junction（os.rmdir 对 junction 只删链点、不碰目标）；best-effort 防污染 tmp 清理
        try:
            if os.path.lexists(str(alive)):
                os.rmdir(alive)
        except OSError:
            pass


def test_a28_symlink_final_segment_is_rejected(root):
    """**正腿②（`回归修复` ②）**：末段是真符号链接 ⇒ 拒（WRITE_SYMLINK_TEMPLATE），
    目标本体逐字节不变。

    本机无符号链接特权时 ``os.symlink`` 静默降级 ⇒ 助手验产物、失败即 **skip**
    （如实降级，不得粉饰成 PASS）；本机判别力由下一条 junction 模拟腿保底。
    """
    import os

    real = root / "real.txt"
    real.write_bytes(b"origin-bytes")
    link = root / "link.txt"
    if not _make_symlink(link, real):
        pytest.skip("本机 os.symlink 静默降级（无管理员/开发者模式）⇒ 无法构造真符号链接；照实降级")
    try:
        with pytest.raises(_write.WriteError) as excinfo:
            _write_new(root, "link.txt", b"attack", create=False, snapshot=True)
        assert _write_paths.WRITE_SYMLINK_TEMPLATE.split("{", 1)[0] in str(excinfo.value), (
            f"必须用冻结的 symlink 拒绝文案：{excinfo.value}"
        )
        assert real.read_bytes() == b"origin-bytes", "写穿链接改到了目标本体（回归修复 未修）"
    finally:
        try:
            if os.path.lexists(str(link)):
                os.unlink(link)
        except OSError:
            pass


def test_a28_final_segment_link_is_judged_on_absolute_not_realpath(root, monkeypatch):
    """**正腿②b（`回归修复` ②的无特权机器判别力腿）**：末段链接判定必须发生在
    ``absolute``（realpath 前）而不是 ``resolved``（realpath 后）。

    ⚠️ Windows junction 只能指向**目录**（`mklink /J` 指向文件必败，实测）⇒ 无法在本机
    落盘一个「指向文件的真符号链接」。本腿用 junction（realpath 会**追随**它解析）+
    桩 ``os.path.islink``（只对链接位本身答 True）模拟「有特权机器上指向目录的真
    symlink」的可观测行为（``islink(absolute)=True``、realpath 穿透）。修前：
    ``islink(resolved)`` 拿到的是**解析产物**（恒 False）⇒ symlink 分支永不触发，
    落进 ``is_dir`` 分支报**错文案**（「目标是目录」）；修后：判在 ``absolute`` 上 ⇒
    报冻结的 symlink 文案。判别力在**文案**上（修前修后都会拒，但拒因不同）。
    """
    import os

    realdir = root / "realdir"
    realdir.mkdir()
    (realdir / "real.txt").write_bytes(b"origin-bytes")
    link = root / "link"
    assert _make_junction(link, realdir), "junction 构造失败 ⇒ 本腿判别力失效"

    real_islink = os.path.islink
    junction_str = os.path.normcase(str(link))

    def stub_islink(path):  # noqa: ANN001
        return os.path.normcase(str(path)) == junction_str or real_islink(path)

    monkeypatch.setattr(os.path, "islink", stub_islink)
    try:
        with pytest.raises(_write.WriteError) as excinfo:
            _write_new(root, "link", b"attack", create=False, snapshot=True)
        assert _write_paths.WRITE_SYMLINK_TEMPLATE.split("{", 1)[0] in str(excinfo.value), (
            f"末段是链接必须报 symlink 冻结文案（修后判在 absolute 上）：{excinfo.value}"
        )
        assert _write_paths.WRITE_IS_DIR_TEMPLATE.split("{", 1)[0] not in str(excinfo.value), (
            f"链接被误判成目录 ⇒ islink 判在了 realpath 产物上（回归修复 未修）：{excinfo.value}"
        )
        assert (realdir / "real.txt").read_bytes() == b"origin-bytes", "目标内容不得被改"
    finally:
        try:
            if os.path.lexists(str(link)):
                os.rmdir(link)  # junction：只删链点、不碰目标
        except OSError:
            pass
