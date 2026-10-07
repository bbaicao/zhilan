"""回归检查 · **从备份手工找回** —— 判据 **回归检查** 的载体（阶段 6 · 可见面）。

## 回归检查 要判什么（逐字口径 · 本包 §2 首次定档）

> **回归检查（可手工找回）**
> ① 能列出 `data/workspace-backups/` 里的快照（**普通可读文件**，不加密、不藏）；
>    ⚠️ **目录不存在 ⇒ 返回空列表 + 中文提示，不得抛**；
> ② 能按快照**手工**取回内容（逐字节等于快照内容）；
> ③ 🟢 **（v1.1 新增腿 · 补真交付）** 存在**手工找回的「入口」**（函数/端点**二选一**，须在实现时定档并登记）；
> ④ 🔴 **措辞**：只保证「**有副本可手工找回**」—— **不得**出现「一键」形态的自动恢复字样（红线 9；
>    逐字禁语见 `BANNED_RESTORE_PHRASES`，本件 腿④ 直接扫它）。

## 🔴 入口形态**定档**（腿③ 的前置，逐字）

**取【函数】档，不加端点**：`_write.list_backups()` + `_write.read_backup(name)`。
理由：新增端点须先改 `接口说明/02-API契约冻结清单.md` ⇒ 撞**红线 7** 与 `D-回归检查`「契约面零改动」
（开工包 §4.1 逐字：「如新增只读端点**须先改** `02`（**默认不加**）」）。⇒ 本阶段**只**落函数入口。

## 🔴 本件的夹具**必须**打 `resolve_backup_dir` 锚点（K-回归检查）

`_write.resolve_backup_dir()`（`_write.py` 的**模块级函数**）带**测试态守卫**（`回归检查` / `D-回归检查`）：
`PYTEST_CURRENT_TEST` 存在且路径落在仓库根内 ⇒ 抛 `TestModeRepoWriteError` ——
**该守卫连「读」都拦**（它没有读写形参）⇒ 本件**每一处**都用
`monkeypatch.setattr(write_module, "resolve_backup_dir", ...)` 锚到 `tmp_path`。
🔴 若漏打：`list_backups()` 会**当场抛** `TestModeRepoWriteError`（不是「返回仓库内目录的列表」）——
这正是守卫的价值；本件另有一条腿**显式钉住**这个 fail-closed 行为。

## 与其它条目的边界

- **写入 / 原子写 / 批语义** 归 `回归检查` / `回归检查` / `回归检查`，本件**不重复判**；
- **备份 ≠ 回滚**：本件只判「有副本可手工找回」，**不**声称可自动回滚、**不**出现禁语（腿④）；
- 本件**不碰仓库内 `data/`**（红线 12）：工作区根与备份目录**一律 `tmp_path`**；
- `tests/agent/` **没有 conftest.py**（先例 `test_file_tools_t3_3.py:61`）⇒ 夹具**自带**。

## 三条腿的失败信息**分开写**（不合并成一条 assert）

合并会让「哪条腿红」不可辨，而「红点归因唯一」是本项目的硬要求（阶段 4 实测教训）。
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import pytest


# ── 夹具（自带；tests/agent/ 无 conftest，先例 test_file_tools_t3_3.py:61）────


@pytest.fixture
def write_module():
    """取**真模块** `app.agent.tools._write`；拿到桩或不存在 ⇒ **直接失败**。

    防「真模块层的假绿」：先例 `test_write_snapshot_t3_5.py` 的同名夹具。
    """
    module = importlib.import_module("app.agent.tools._write")
    assert getattr(module, "__file__", None), "_write 不是真模块（无 __file__）⇒ 真模块层无效"
    return module


@pytest.fixture
def backup_dir(write_module, tmp_path: Path, monkeypatch) -> Path:
    """把备份目录锚到 `tmp_path`（**红线 12**：绝不用仓库内目录）+ **正控**锚点真被替换。

    🔴 正控不是「某属性存在」而是「**打后的值 == 期望的临时值**」——
    只断「存在」是**假正控**（阶段 5 夹具的实测教训，`K-回归检查`）。
    """
    backups = tmp_path / "workspace-backups"
    monkeypatch.setattr(write_module, "resolve_backup_dir", lambda: backups)
    assert write_module.resolve_backup_dir() == backups, (
        "resolve_backup_dir 锚点没挂上 ⇒ 会落到仓库内 data/workspace-backups，红线 12 当场被破"
    )
    assert tmp_path in backups.parents, "备份目录必须落在 tmp_path 之下"
    return backups


def _snapshot_names(listing) -> list[str]:
    """从列表产物里取快照名（**不摸私有字段**；只读公开的 `entries[].name`）。"""
    return [entry.name for entry in listing.entries]


# ── 腿①：能列出快照（普通可读文件）+ 缺失目录 ⇒ 空列表 + 中文提示、不抛 ──────────


def test_a27_leg1_lists_plain_readable_snapshots(write_module, backup_dir: Path) -> None:
    """**A27 腿① 正腿**：目录里**有**快照时，列表能列出来。

    为什么先跑一遍真写入：快照件必须由**产品路径**产出（`write_workspace_file`），
    而不是测试自己 `write_bytes` 造一个 —— 否则本腿与「产品真的会产快照」脱钩。
    """
    ws = backup_dir.parent / "ws"
    ws.mkdir()
    (ws / "a.txt").write_bytes("写前内容 A\n".encode("utf-8"))

    outcome = write_module.write_workspace_file(
        root=ws, rel_path="a.txt", data="写后内容\n".encode("utf-8"), create=False, snapshot=True
    )
    assert outcome.snapshot_id, "前置：有写前内容时 snapshot_id 必须非空（否则本腿无样本可判）"

    listing = write_module.list_backups()

    names = _snapshot_names(listing)
    assert names == [outcome.snapshot_id], (
        f"A27 腿①：列表里的快照名应恰好是被写入那件的 snapshot_id，实测 {names}"
    )

    entry = listing.entries[0]
    # 「普通可读文件」的可判形态：真文件 + 名字不藏（不以 . 开头）
    assert entry.path.is_file(), "A27 腿①：列出的项必须是**真文件**"
    assert not entry.name.startswith("."), f"A27 腿①：快照名不得以 . 开头（藏）：{entry.name}"
    assert entry.size > 0, f"A27 腿①：快照件字节数应 > 0，实测 {entry.size}"


def test_a27_leg1_directory_absent_returns_empty_list_with_chinese_hint(
    write_module, backup_dir: Path
) -> None:
    """**A27 腿① 缺失腿**：目录**不存在** ⇒ 返回**空列表 + 中文提示**，**不得抛**。

    实测底数（开工包 §0.2 行 14）：真机上 `data/` 下**只有 `uploads/`** ——
    备份目录**从未被创建** ⇒ 这条是**真实默认态**，不是边角。
    """
    # 前置正控：目录**确实**不存在（否则本腿判的就不是「缺失」路径）
    assert not backup_dir.exists(), "前置：本腿要求备份目录**不存在**"

    listing = write_module.list_backups()  # 🔴 不得抛 —— 抛了就是本腿红

    assert listing.entries == [], f"A27 腿①：目录不存在应返回**空列表**，实测 {listing.entries}"
    assert listing.hint, "A27 腿①：目录不存在必须给**中文提示**（空提示 = 用户无从知道为什么是空的）"
    hint = str(listing.hint)
    assert any("\u4e00" <= ch <= "\u9fff" for ch in hint), (
        f"A27 腿①：提示必须是**中文**可读文案，实测 {hint!r}"
    )
    assert not backup_dir.exists(), "A27 腿①：列列表**不得**顺手创建备份目录（那是写动作）"


def test_a27_leg1_empty_existing_directory_also_yields_hint(write_module, backup_dir: Path) -> None:
    """**A27 腿① 空目录腿**：目录**存在但空** ⇒ 同样空列表 + 提示（区别于「缺失」与「有件」）。

    为什么单列：`is_dir()` 与 `iterdir()` 是两条不同的代码路径；
    只判「缺失」会让「存在但空」这条路径**零覆盖**（关掉分支仍全绿 ⇒ `K-回归检查` 同型）。
    """
    backup_dir.mkdir()
    assert backup_dir.is_dir() and not list(backup_dir.iterdir()), "前置：目录存在且为空"

    listing = write_module.list_backups()

    assert listing.entries == [], f"A27 腿①：空目录应返回空列表，实测 {listing.entries}"
    assert listing.hint, "A27 腿①：空目录也要给中文提示（否则用户以为功能坏了）"


def test_a27_leg1_filter_excludes_hidden_and_disguised_entries(
    write_module, backup_dir: Path
) -> None:
    """**A27 腿① 过滤腿**：只列**普通可读文件** —— 隐藏件 / 临时件 / 伪装后缀件**不得**入列。

    🔴 这条是变异 **`M23`** 的靶腿：把过滤条件改宽（不过滤）⇒ 本腿**必红**。
    分母自证：目录里**确实**放了 2 件该列、3 件不该列 —— 而不是「什么都没放所以过了」。
    """
    backup_dir.mkdir()
    (backup_dir / "20260926T000000000000-aaaaaaaa-keep.txt").write_bytes(b"keep-1")
    (backup_dir / "20260926T000000000001-bbbbbbbb-keep2.txt").write_bytes(b"keep-2")
    # 不该入列的三件（各代表一条过滤条件）
    (backup_dir / ".hidden-snapshot").write_bytes(b"hidden")
    (backup_dir / ".tmp-inflight").write_bytes(b"tmp")
    (backup_dir / "20260926T000000000002-cccccccc-sneaky.bin").write_bytes(b"disguised")

    listing = write_module.list_backups()

    names = _snapshot_names(listing)
    assert names == [
        "20260926T000000000000-aaaaaaaa-keep.txt",
        "20260926T000000000001-bbbbbbbb-keep2.txt",
    ], f"A27 腿①：只应列出**普通可读文件**（隐藏/临时/伪装件必须排除），实测 {names}"
    # 反向正控：被排除的三件**确实**在磁盘上（否则「排除」可能只是因为它们不存在）
    assert (backup_dir / ".hidden-snapshot").is_file(), "反向正控：隐藏件应真实存在于磁盘"
    assert (backup_dir / ".tmp-inflight").is_file(), "反向正控：临时件应真实存在于磁盘"
    assert (backup_dir / "20260926T000000000002-cccccccc-sneaky.bin").is_file(), (
        "反向正控：伪装件应真实存在于磁盘"
    )


def test_a27_leg1_sorted_ascending_and_no_temp_left_behind(write_module, backup_dir: Path) -> None:
    """**A27 腿① 排序腿**：列出的顺序**升序**（名字含时间戳 ⇒ 升序 = 时间序，**可判真假**）。

    为什么判排序可判：快照名 = `_snapshot_name()`（UTC 时间戳前缀）⇒ 名字序 **就是** 时间序；
    乱序会让「哪份是最近一份写前像」在多件副本时**不可判**。
    """
    backup_dir.mkdir()
    for stamp in ("20260101T000000000000", "20260202T000000000000", "20260303T000000000000"):
        (backup_dir / f"{stamp}-00112233-x.txt").write_bytes(b"x")

    listing = write_module.list_backups()

    names = _snapshot_names(listing)
    assert names == sorted(names), f"A27 腿①：快照列表必须按名字升序（= 时间序），实测 {names}"
    assert len(names) == 3, f"A27 腿①：分母自证 —— 应列出 3 件，实测 {len(names)}"


# ── 腿②：按快照取回内容 —— 逐字节等于快照内容 ─────────────────────────────────


def test_a27_leg2_read_backup_returns_snapshot_bytes_verbatim(
    write_module, backup_dir: Path
) -> None:
    """**A27 腿②**：按快照**手工**取回 → 逐字节等于**写前**内容。

    🔴 排序硬性（照 `test_write_snapshot_t3_5.py` 的假绿陷阱）：
    **先**读 `before_bytes` ⇒ **再**调写入 ⇒ **然后**才取回快照比对。
    反过来（取回后才知道内容）会让本腿**恒真**。

    用**含 BOM + CRLF + 多字节**的字节串：`str` 层的「看起来一样」会掩盖编码往返损失。
    """
    ws = backup_dir.parent / "ws2"
    ws.mkdir()
    target = ws / "bom.txt"
    before_bytes = "\ufeff第一行\r\n第二行 − 符号\r\n".encode("utf-8")
    target.write_bytes(before_bytes)

    outcome = write_module.write_workspace_file(
        root=ws, rel_path="bom.txt", data=b"new\n", create=False, snapshot=True
    )
    # 前置正控：写入**真的**生效（否则「快照 == 写前」可能只是压根没写）
    assert target.read_bytes() == b"new\n", "前置正控：写入没生效 ⇒ 本腿其余断言无意义"

    got = write_module.read_backup(outcome.snapshot_id)

    assert got == before_bytes, (
        "A27 腿②：按快照取回的内容 != 写前字节（很可能是快照取的是**写后**内容）"
    )
    assert got != b"new\n", "A27 腿② 反向正控：取回的是**写后**内容 ⇒ 腿② 假绿"
    assert isinstance(got, bytes), f"A27 腿②：取回必须是 bytes（逐字节语义），实测 {type(got)}"


def test_a27_leg2_second_snapshot_of_same_target_stays_distinguishable(
    write_module, backup_dir: Path
) -> None:
    """**A27 腿② 多次写腿**：同一目标写**两次** ⇒ 两份快照**各自**取回各自的前像。

    为什么必要：只写一次时，「按名字取回」与「取回唯一那份」不可区分；
    两件同名不同批的快照才让「名字**真的**参与了定位」可判（判别力腿）。
    """
    ws = backup_dir.parent / "ws3"
    ws.mkdir()
    target = ws / "twice.txt"
    first = b"v1\n"
    second = b"v2-longer\n"

    target.write_bytes(first)
    o1 = write_module.write_workspace_file(
        root=ws, rel_path="twice.txt", data=second, create=False, snapshot=True
    )
    o2 = write_module.write_workspace_file(
        root=ws, rel_path="twice.txt", data=b"v3\n", create=False, snapshot=True
    )
    assert o1.snapshot_id != o2.snapshot_id, "两次写的快照必须是**两件**（否则无从区分）"

    assert write_module.read_backup(o1.snapshot_id) == first, "A27 腿②：第一份快照应等于 v1"
    assert write_module.read_backup(o2.snapshot_id) == second, "A27 腿②：第二份快照应等于 v2"

    names = _snapshot_names(write_module.list_backups())
    assert set(names) == {o1.snapshot_id, o2.snapshot_id}, (
        f"A27 腿②：列表应恰好含这两件，实测 {names}"
    )


def test_a27_leg2_unknown_name_is_a_readable_chinese_failure(write_module, backup_dir: Path) -> None:
    """**A27 腿② 负控**：名字不存在 / 空串 / 含路径分隔符 ⇒ **中文可读失败**，不漏路径。

    为什么要有：若 `read_backup` 对坏名字**静默返回空 bytes**，调用方会以为「找回了一份空副本」
    —— 那是**静默假成功**（阶段 5 `回归修复` 同型）。
    """
    backup_dir.mkdir()

    with pytest.raises(write_module.WriteError) as missing:
        write_module.read_backup("20260926T000000000000-deadbeef-nope.txt")
    assert "找不到" in str(missing.value), f"A27 腿②：缺件应是中文可读文案，实测 {missing.value!r}"

    with pytest.raises(write_module.WriteError) as blank:
        write_module.read_backup("")
    assert "空" in str(blank.value), f"A27 腿②：空名应是中文可读文案，实测 {blank.value!r}"

    # 越界负控：快照名不得当路径用（否则可拿来读备份目录**之外**的件）
    for escape in ("../outside.txt", "sub/inner.txt"):
        with pytest.raises(write_module.WriteError) as bad:
            write_module.read_backup(escape)
        assert "分隔符" in str(bad.value), f"A27 腿②：越界名应被拒，实测 {bad.value!r}"


# ── 腿③：存在「入口」（函数/端点二选一，本阶段定档 = **函数**） ──────────────────


def test_a27_leg3_entry_points_exist_and_are_callable(write_module) -> None:
    """**A27 腿③**：手工找回的**入口**真存在且**可调用**（本阶段定档 = 函数档）。

    🔴 v1.0 只定义腿①② ⇒ 照它做完 = **零产品交付**；本腿就是补上的那件真交付。
    ⚠️ 断言「存在」**不够**（那就是「某属性存在」式假正控）⇒ 必须同时断**可调用** + **签名可取**。
    """
    for name in ("list_backups", "read_backup"):
        entry = getattr(write_module, name, None)
        assert entry is not None, f"A27 腿③：找回入口 {name} 不存在"
        assert callable(entry), f"A27 腿③：找回入口 {name} 不可调用"
        # 取签名本身会让「桩对象 / 非函数」当场报错 ⇒ 这是「真函数」的正控
        signature = inspect.signature(entry)
        assert signature is not None, f"A27 腿③：取不到 {name} 的签名"

    assert "list_backups" in write_module.__all__, "A27 腿③：list_backups 应挂在 __all__（对外的入口面）"
    assert "read_backup" in write_module.__all__, "A27 腿③：read_backup 应挂在 __all__（对外的入口面）"


def test_a27_leg3_entry_shape_is_frozen_as_functions_not_endpoints(write_module) -> None:
    """**A27 腿③ 定档腿**：入口形态 = **函数**（`() -> BackupListing` / `(name) -> bytes`）。

    为什么判签名可判真假：形态一改（加端点 / 改形参）⇒ 本腿**必红**，
    且**钉住**「本阶段不加端点」（红线 7 / `D-回归检查`）这条口径。
    """
    listing_sig = inspect.signature(write_module.list_backups)
    assert list(listing_sig.parameters) == [], (
        f"A27 腿③：list_backups 应**零形参**，实测 {list(listing_sig.parameters)}"
    )

    read_sig = inspect.signature(write_module.read_backup)
    assert list(read_sig.parameters) == ["name"], (
        f"A27 腿③：read_backup 应恰有一个 name 形参，实测 {list(read_sig.parameters)}"
    )


def test_a27_leg3_endpoint_is_not_added(write_module) -> None:
    """**A27 腿③ 反面腿**：本笔**不得**新增端点（`D-回归检查`「契约面零改动」/ 红线 7）。

    判法 = 读 `_write.py` 源码文本，断言**没有**任何路由装饰器 / `APIRouter` 出现。
    为什么这条可判：`_write.py` 是**工具本体**模块，一旦出现 `@router.get` 之类，
    那必然是新增端点 —— 与「只加函数入口」的定档直接冲突。
    """
    source = Path(write_module.__file__).read_text(encoding="utf-8")
    for marker in ("@router.", "@app.get", "@app.post", "APIRouter("):
        assert marker not in source, f"A27 腿③：_write.py 出现端点标记 {marker} ⇒ 撞红线 7（不得新增端点）"


# ── 腿④：措辞红线（不得自称可做「一键」形态的自动恢复）────────────────────────


#: 红线 9 禁语 —— **判据面唯一一份**（用逐字拼接构造，见下条自查腿的说明）
BANNED_RESTORE_PHRASES: tuple[str, ...] = ("一键" + "还原", "一键" + "回滚", "回滚" + "到")


def test_a27_leg4_entry_source_carries_no_restore_wording(write_module) -> None:
    """**A27 腿④（红线 9）**：**找回入口**的源码不得自称能做「自动恢复」。

    🔴 **扫描面 = 本笔新增的两个入口函数**（`list_backups` / `read_backup`），
    **不是**整个 `_write.py` —— 全模块扫会把**无关的合法字样**一起判红：
    该模块另有 DB 事务语义下的 `rollback` / 「回滚」（`db.rollback()` 的既有用法），
    那是 `回归检查-*` 已验收的正文，与红线 9 的「**工具能力自称**」无关。
    ⇒ 判据面必须**收窄到本笔的交付面**，否则它一开始就是红的（无判别力、也无法「改前绿」）。

    为什么用 `inspect.getsource(函数)` 而不是读整个文件：前者**跟着符号走**，
    函数被改名 / 移走时本腿会**当场报错**（而不是静默扫描 0 行 ⇒ 假绿）。
    """
    for entry in (write_module.list_backups, write_module.read_backup):
        source = inspect.getsource(entry)
        # 正控：真的取到了函数体（取空 ⇒ 本腿退化成恒真）
        assert source.strip(), f"A27 腿④：取不到 {entry.__name__} 的源码 ⇒ 本腿无判别力"
        assert any(word in source for word in ("快照", "备份")), (
            f"A27 腿④ 正控：{entry.__name__} 的源码里应出现「快照 / 备份」（确认取到的是本入口）"
        )
        offender = [word for word in BANNED_RESTORE_PHRASES if word in source]
        assert offender == [], (
            f"A27 腿④（红线 9）：{entry.__name__} 出现禁语 {offender} —— 只可保证「有副本可手工找回」"
        )


def test_a27_leg4_this_carrier_avoids_the_banned_wording_outside_its_judge_literals() -> None:
    """**A27 腿④ 自查腿**：本载体的**散文部分**不得出现禁语（判据件自己不能是反例）。

    🔴 **为什么必须排除「判据字面量所在行」**：本件恰恰**必须**把禁语作为**判据字面量**
    写出来（`BANNED_RESTORE_PHRASES` 那一行）—— 否则「扫描」二字无从落地。
    ⇒ 自查的合理面 = **其余所有行**（模块 docstring 等散文 + 其余代码）。
    「判据字面量所在行」的判定谓词 = 该行**含符号名** `BANNED_RESTORE_PHRASES`
    —— 纯字符串包含，**机器可判真假**；且同一谓词**多排除一行也无害**（只会让自查更严）。
    """
    source = Path(__file__).read_text(encoding="utf-8")
    scanned = "\n".join(
        line
        for line in source.splitlines()
        if "BANNED_RESTORE_PHRASES" not in line
    )
    # 正控：扫描面**确实**非空且**确实**排除了判据字面量行（否则本腿退化成恒真）
    assert scanned.strip(), "A27 腿④：自查扫描面为空 ⇒ 本腿无判别力"
    assert ("一键" + "还原") not in scanned, (
        "A27 腿④ 正控：判据字面量行没被排除 ⇒ 自查腿必然恒红（判据形状被污染）"
    )
    offender = [word for word in BANNED_RESTORE_PHRASES if word in scanned]
    # 🔴 无条件断言（**不得**写 `if offender: assert ...` —— 条件式断言 = 死代码，K-回归检查）
    assert offender == [], f"A27 腿④：本载体散文出现禁语 {offender} ⇒ 判据件被污染"


# ── 守卫腿：测试态锚点未替换 ⇒ fail-closed（不是「默默列出仓库内目录」）──────────


def test_test_mode_guard_still_fails_closed_on_the_repo_data_dir(write_module) -> None:
    """**回归钉**：不打锚点时 —— 测试态读**仓库内** `data/` ⇒ **当场拒**（`回归检查`）。

    为什么本件必须有这条：它是 K-回归检查 的**结构性理由** ——
    守卫连「读」都拦 ⇒ 本件**必须**打锚点；
    同时它也钉住「守卫不是文档，而是真的会抛」。
    ⚠️ 本腿**不写任何文件**（抛发生在任何 IO 之前）。
    """
    with pytest.raises(write_module.TestModeRepoWriteError):
        write_module.resolve_backup_dir()  # 未打锚点 ⇒ 真锚点（仓库内 data/）⇒ 必须抛


def test_module_missing_hint_constant_matches_the_runtime_hint(write_module, backup_dir: Path) -> None:
    """**常量一致性腿**：`BACKUP_DIR_MISSING_HINT` 与运行时提示**同源**（不是两份文案）。

    为什么必要：文案若各写一份，改一处漏一处 ⇒ 用户看到的提示与常量声明漂移，
    而**两侧都「有中文」**的弱断言抓不到它。
    """
    assert not backup_dir.exists(), "前置：本腿要求备份目录不存在"

    listing = write_module.list_backups()

    assert listing.hint == write_module.BACKUP_DIR_MISSING_HINT.format(path=backup_dir), (
        f"A27 腿①：运行时提示与常量不同源，实测 {listing.hint!r}"
    )

