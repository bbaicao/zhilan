"""回归检查 · **写前快照 + 备份策略** —— 判据 **A21** 的载体（阶段 5 · 写通道）。

## A21 要判什么（逐字口径）

> **A21**：快照存在（**可手工找回**）+ 内容逐字等于**写前**内容 + 普通可读文件（**不加密、不藏**）。

三条腿在本文件里各自有独立的失败信息，**分开断**（不合并成一条 assert）——
合并会让「哪条腿红」不可辨，而本阶段（阶段 4 实测教训）**红点归因唯一**是硬要求。

## 🔴 本件最重要的一条：腿② 的「假绿」陷阱

**在调用写入函数之前**先把原文件字节读进变量 before_bytes，再断言 **备份 == before_bytes**。
若实现成「先写目标、再拿目标内容做备份」⇒ 备份的其实是**新**内容 ⇒ 腿② **恒真、结构性抓不到**
（这正是阶段 4 M-回归检查 同型的「腿内 no-op」）。故本文件的排序是**硬性的**：

1. 读 before_bytes；
2. 调 write_workspace_file(...)；
3. 由 outcome.snapshot_id 定位备份件；
4. 断言备份 == before_bytes。

## 🔴 锚点：**打的是 _write.resolve_backup_dir**（不是 settings 属性）

**执行期修红（2026-09-26 · task-17）**：本件初版按「settings 同型属性」打
monkeypatch.setattr(type(settings), "workspace_backup_dir", property(...))，
而 Lead 的实现（_write.py:104-113）导出的是 **模块级函数 resolve_backup_dir()**，
且 _take_snapshot()（_write.py:129）直接调它 —— **settings 属性从未被读**。
⇒ 夹具打空了：resolve_backup_dir() 仍返回**仓库内** data/workspace-backups，
三条腿去 tmp_path 找备份件自然找不到（**载体错，非实现错**）。

**现行打法**：monkeypatch.setattr(write_module, "resolve_backup_dir", lambda: backups)
（打**模块属性** ⇒ 覆盖 _take_snapshot 的查找路径）。
🔴 红线 12 因此更硬：**若不替换该锚点，测试会真往仓库内 data/ 写盘**。

## 夹具与纪律

tests/agent/ **没有 conftest.py**（先例 test_file_tools_t3_3.py:61）⇒ 夹具**自带**。
**红线 12**：工作区根与备份目录**一律 tmp_path**，**绝不用仓库内目录、绝不写 .env**。
**零迁移**：备份落文件系统，不建表。

## 与其它条目的边界

- 原子写本体（临时文件 + os.replace + 失败清理）归 **回归检查**，本件**不重复判**；
- **备份 ≠ 回滚** —— 只保证「有副本可手工找回」（A21 措辞），本件**不**声称可自动回滚；
- create=True（新建）时**不存在写前内容** ⇒ 本件对新建路径**只判「不产生假前像」**（见腿 D）。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


# ── 夹具（自带；tests/agent/ 无 conftest，先例 test_file_tools_t3_3.py:61）────


@pytest.fixture
def write_module():
    """取**真模块** app.agent.tools._write；拿到桩或不存在 ⇒ **直接失败**。

    防「真模块层的假绿」：先例 test_file_tools_t3_3.py:84-91 的 _real_module_or_fail。
    """
    module = importlib.import_module("app.agent.tools._write")
    assert getattr(module, "__file__", None), "_write 不是真模块（无 __file__）⇒ 真模块层无效"
    return module


@pytest.fixture
def workspace(write_module, tmp_path: Path, monkeypatch) -> Path:
    """一个**真**工作区根 + 指向 tmp_path 的备份目录（**红线 12**：绝不用仓库内目录）。

    🔴 锚点 = **resolve_backup_dir（_write 的模块级函数）** —— 见模块 docstring 的修红说明。
    用 write_module 夹具而非直接 import，保证「真模块」前置断已过、且打的是同一对象。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    backups = tmp_path / "backups"

    monkeypatch.setattr(write_module, "resolve_backup_dir", lambda: backups)
    # 前置正控：锚点**真的**被打上了（打空 ⇒ 后续腿会落到仓库内 data/，红线 12 当场被破）
    assert write_module.resolve_backup_dir() == backups, (
        "resolve_backup_dir 锚点没挂上 ⇒ 测试会往仓库内 data/workspace-backups 真写盘"
    )
    assert tmp_path in backups.parents or backups.parent == tmp_path, "备份目录必须落在 tmp_path 下"
    return ws


def _snapshot_of(write_module, outcome, backups: Path) -> Path:
    """由 outcome.snapshot_id 定位备份件（**不许**靠 glob 猜 —— 那会让「找不到」变成「找到别的」）。"""
    sid = outcome.snapshot_id
    assert sid, "A21 腿①：有写前内容时 snapshot_id 必须非空（否则无从手工找回）"
    hits = [
        p
        for p in backups.rglob("*")
        if p.is_file() and sid in str(p.relative_to(backups).as_posix())
    ]
    assert len(hits) == 1, f"A21 腿①：snapshot_id={sid!r} 未能唯一定位备份件，实测命中 {hits}"
    return hits[0]


# ── 腿 A / B / C：快照存在 · 逐字等于写前内容 · 普通可读不加密不藏 ────────────────


def test_a21_snapshot_exists_and_content_equals_before_bytes(
    write_module, workspace: Path, monkeypatch
) -> None:
    """**A21 腿①②**：快照**存在**，且内容**逐字等于写前内容**。

    🔴 排序硬性：**先读 before_bytes，再调写入**（见模块 docstring 的假绿陷阱）。
    """
    backups = write_module.resolve_backup_dir()
    target = workspace / "note.txt"
    before_bytes = "原始内容\n第二行\n".encode("utf-8")
    target.write_bytes(before_bytes)

    outcome = write_module.write_workspace_file(
        root=workspace,
        rel_path="note.txt",
        data="改写后的内容\n".encode("utf-8"),
        create=False,
        snapshot=True,
    )

    # 前置正控：目标**真的被改了** —— 否则「备份 == 写前」可能只是因为压根没写
    assert target.read_bytes() != before_bytes, "写入没生效 ⇒ 本腿的其余断言无意义（前置正控失败）"

    snapshot = _snapshot_of(write_module, outcome, backups)

    # 腿①：存在
    assert snapshot.is_file(), "A21 腿①：快照件不存在"

    # 腿②：**逐字等于写前内容**（用 bytes 比 —— 文本比会掩盖 BOM / 行尾差异）
    assert snapshot.read_bytes() == before_bytes, (
        "A21 腿②：快照内容 != 写前内容（很可能是「先写目标再备份」⇒ 备份成了新内容）"
    )
    # 反向正控：快照**不得**是新内容（否则与腿② 同义反复，判别力为 0）
    assert snapshot.read_bytes() != "改写后的内容\n".encode("utf-8"), "快照是**写后**内容 ⇒ 腿② 假绿"


def test_a21_snapshot_is_plain_readable_unencrypted_file(
    write_module, workspace: Path, monkeypatch
) -> None:
    """**A21 腿③**：普通可读文件 —— **不加密、不藏**。

    判法 = ① 文件名**不以 . 开头**（不藏）；② 无加密/压缩后缀伪装；③ 字节**可直接解出原文**
    （真加密必然解不出，故这条同时钉住「没做任何混淆」）。
    """
    backups = write_module.resolve_backup_dir()
    target = workspace / "plain.md"
    plain_text = "# 标题\n\n正文内容，含中文与符号 −。\n"
    before_bytes = plain_text.encode("utf-8")
    target.write_bytes(before_bytes)

    outcome = write_module.write_workspace_file(
        root=workspace, rel_path="plain.md", data=b"new\n", create=False, snapshot=True
    )
    snapshot = _snapshot_of(write_module, outcome, backups)

    # ③-①：不藏
    assert not snapshot.name.startswith("."), f"A21 腿③：快照是隐藏文件（{snapshot.name}）——不得藏"

    # ③-②：无加密/压缩后缀伪装
    disguised = (".enc", ".encrypted", ".bin", ".gz", ".zip", ".dat")
    assert not snapshot.name.lower().endswith(disguised), (
        f"A21 腿③：快照名疑似加密/压缩伪装：{snapshot.name}"
    )

    # ③-③：字节可直接解出原文（真加密 / 混淆必红）
    try:
        decoded = snapshot.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - 只有实现真做了加密才走到
        pytest.fail(f"A21 腿③：快照不是可直接读的明文（{exc}）")
    assert decoded == plain_text, "A21 腿③：明文解出的内容与写前内容不一致"


def test_a21_multibyte_content_survives_byte_exact(
    write_module, workspace: Path, monkeypatch
) -> None:
    """**A21 腿② 的多字节加强腿**：中文 / 符号 / 行尾逐字保真。

    为什么单独一条：str 层的「看起来一样」会掩盖 **BOM 与 CRLF** 差异；
    本条用**含 CRLF + BOM + 多字节**的字节串，把「编码往返」的损失面顶出来。
    """
    backups = write_module.resolve_backup_dir()
    target = workspace / "bom.txt"
    before_bytes = "\ufeff第一行\r\n第二行 − 符号\r\n".encode("utf-8")
    target.write_bytes(before_bytes)

    outcome = write_module.write_workspace_file(
        root=workspace, rel_path="bom.txt", data=b"x", create=False, snapshot=True
    )
    snapshot = _snapshot_of(write_module, outcome, backups)

    snapshot_bytes = snapshot.read_bytes()
    assert snapshot_bytes == before_bytes, "A21：BOM / CRLF 在快照里没保住（逐字比必红）"
    assert snapshot_bytes[:3] == b"\xef\xbb\xbf", "A21：BOM 丢失"


# ── 腿 D / E：无前像路径 与 关闭开关 ────────────────────────────────────────────


def test_a21_create_path_must_not_fabricate_a_snapshot(write_module, workspace: Path) -> None:
    """**A21 边界腿**：create=True 且目标**不存在** ⇒ **不存在「写前内容」**。

    本腿**只判「不造假」**：**不得**凭空产生一份「写前像」（那会把「备份 == 写前内容」这条判据
    在新建路径上变成**永真** ⇒ 判别力归零）。
    ⇒ 取定：此路径下 **snapshot_id 为假值**（None 或空串），**且**备份目录里
    **不得**出现把「新内容」冒充成「前像」的件。
    """
    backups = write_module.resolve_backup_dir()
    assert not (workspace / "brand_new.txt").exists(), "前置：本腿要求目标**不存在**"

    outcome = write_module.write_workspace_file(
        root=workspace,
        rel_path="brand_new.txt",
        data=b"fresh\n",
        create=True,
        snapshot=True,
    )

    assert (workspace / "brand_new.txt").read_bytes() == b"fresh\n", "新建路径没落盘"
    assert not outcome.snapshot_id, (
        f"A21 边界腿：新建路径下 snapshot_id 应为假值，实测 {outcome.snapshot_id!r}"
        "（凭空造前像会让腿② 在新建路径上永真）"
    )
    leftover = [p for p in backups.rglob("*") if p.is_file()] if backups.exists() else []
    assert not leftover, f"A21 边界腿：新建路径不得产生任何备份件，实测 {leftover}"


def test_snapshot_flag_off_produces_no_backup(write_module, workspace: Path) -> None:
    """snapshot=False ⇒ **不落备份**（且不编造 snapshot_id）。

    这条是腿① 的**前提**：若关掉开关仍写备份，说明实现里快照不可关 ⇒ 与「按需备份」语义不符。
    """
    backups = write_module.resolve_backup_dir()
    target = workspace / "no_snap.txt"
    target.write_bytes(b"before\n")

    outcome = write_module.write_workspace_file(
        root=workspace, rel_path="no_snap.txt", data=b"after\n", create=False, snapshot=False
    )

    assert target.read_bytes() == b"after\n", "关掉快照不应影响写入本身"
    assert not outcome.snapshot_id, (
        f"snapshot=False 时不得有 snapshot_id，实测 {outcome.snapshot_id!r}"
    )
    leftover = [p for p in backups.rglob("*") if p.is_file()] if backups.exists() else []
    assert not leftover, f"snapshot=False 却落了备份件：{leftover}"


# ── 接线腿：锚点真的可替换（防「硬编码仓库内 data/」）───────────────────────────


def test_backup_dir_anchor_is_replaceable_and_shaped_correctly(write_module) -> None:
    """**接线腿**：resolve_backup_dir 是**可替换的模块级锚点**，形状固定为 data/workspace-backups。

    为什么必要：若实现里写死路径、又不经这个函数，测试的 monkeypatch **打不到它**
    ⇒ 红线 12 当场被破（测试会往仓库内 data/ 真写盘），而**其余腿全绿**。
    本腿在**不写文件**的前提下断言锚点可替换 + 形状正确 + 确定性。
    """
    # 🔴 本腿要断的是**真锚点**的形状（仓库内 `data/workspace-backups`），
    # 而该锚点在**测试态**下已被守卫拦下（`TestModeRepoWriteError`，阶段 5 事故 ⑤ 的修法）
    # ⇒ 这里**临时移除 `PYTEST_CURRENT_TEST`** 来观察**生产态**的锚点形状
    # （守卫只影响测试态，生产态逐字不变 —— 这正是本腿要钉的那条边界）。
    import os as _os  # noqa: PLC0415

    saved = _os.environ.pop("PYTEST_CURRENT_TEST", None)
    try:
        real = write_module.resolve_backup_dir()
        assert isinstance(real, Path), "resolve_backup_dir 必须返回 Path"
        # 形状腿：末两级固定为 data/workspace-backups（相对 ROOT_DIR 解析）
        assert real.parts[-2:] == ("data", "workspace-backups"), f"备份目录形状不符，实测 {real}"
        # 确定性（A21「可手工找回」要求同一配置两次读同值）
        assert write_module.resolve_backup_dir() == real, "备份目录读两次不一致"
    finally:
        if saved is not None:
            _os.environ["PYTEST_CURRENT_TEST"] = saved

    # 🔴 **守卫本身也要有腿**（否则「测试态 fail-closed」只是文档）：
    # 恢复 `PYTEST_CURRENT_TEST` ⇒ 真锚点**必须抛**（这就是 235 件污染的结构性防法）。
    with pytest.raises(write_module.TestModeRepoWriteError):
        write_module.resolve_backup_dir()
    # A21 的「可手工找回」还要求**名字**可寻址：BACKUP_DIRNAME 常量与形状一致
    assert write_module.BACKUP_DIRNAME == "workspace-backups", (
        f"BACKUP_DIRNAME 与形状不一致，实测 {write_module.BACKUP_DIRNAME!r}"
    )
