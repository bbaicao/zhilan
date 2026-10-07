"""写动作**本体**（专项工具升级-3 · 阶段 5 · 回归检查 / 回归检查 / 回归检查 / 回归检查）。

## 定位（D-回归检查 ②/⑥）

- **工具声明层**（TOOL_NAME / Args / build_spec）= workspace_write.py；
- **写动作实现本体** = 本模块（原子写 / 保真 / 写前快照 / 新建分支）。

D-回归检查 ⑥ 逐字冻结本模块的入口签名：

    write_workspace_file(*, root, rel_path, data, create, snapshot) -> WriteOutcome

**keyword-only**（防位置参数误传）；WriteOutcome 至少含 target / byte_delta /
snapshot_id 三键 —— **正是 回归检查 三个写通道审计键的来源**
（write_target / write_byte_delta / write_snapshot_id 必须由写函数产出，不得由调用侧另算）。

## 原子性（照 app/pipeline/storage.py:65-71 的形状）

    tmp = base / ".tmp-<digest>-<uuid>"   # **与目标同目录**（跨卷 os.replace 不原子）
    tmp.write_bytes(data)
    os.replace(tmp, dest)                 # 原子改名
    except BaseException: tmp.unlink(missing_ok=True); raise

🔴 except BaseException（**不是** Exception）：取消（asyncio.CancelledError 在 3.8+ 继承
BaseException）也必须走清理 ⇒ 这是 A7-2（取消腿）成立的结构前提。
⚠️ 清理必须**同时**覆盖 tmp.write_bytes 与 os.replace 两个阶段。

## 保真（A8 四条腿）

写回**一律字节级**（write_bytes），**绝不用** write_text：

- 腿①：UTF-8 无 BOM 写回**仍无 BOM**（不追加 EF BB BF）；
- 腿②：**LF 源文件写回仍是 LF**（Windows 上 write_text 默认把换行转成 os.linesep = CRLF
  ⇒ **会破坏逐字节保真**；audit-write 实测确认）；
- 腿③：按 source_encoding 给出的**同一编解码名**写回（GBK 仍 GBK）；
- 腿④：**逐字节等于写前字节**（本模块原样落盘调用方给的 bytes，不做任何转换）。

## 写前快照（A21 · 备份 ≠ 回滚）

- 目标**已存在**且 snapshot=True ⇒ 先把**写前字节**复制到备份目录，再执行写；
- 🔴 **必须先读旧内容、再做写动作** —— 若实现成「先写目标、再拿目标内容做备份」，
  备份的就是**新**内容 ⇒ A21 腿② **恒真、结构性抓不到**（阶段 4 M-回归检查 同型）；
- ⚠️ **快照失败 ⇒ 整个写调用 fail-closed 拒绝**（不得「快照失败但照样写」）；
- 🔴 **备份 ≠ 回滚**：只保证「**有副本可手工找回**」，**不是**「可一键还原」（红线 9 禁语）；
- 命名含时间戳 + 目标名摘要；**默认不自动清理**（单⑩ (a)；如实登记「会无限增长」）。

## 批语义（单⑩ (a) · 单文件原子）

**不做跨文件事务、不做多文件回滚**：一次调用 = 一个文件；失败 ⇒ 该文件原样不变、
无残留临时文件；已成功的其它调用**不回滚**。

## 新建语义（回归检查 · A28）

- create=True 且目标**已存在** ⇒ **拒**（不得静默覆盖 —— 那是 create=False 的语义）；
- create=True 且父目录不存在 ⇒ **先判（判之前零 IO）⇒ 建 ⇒ 再判一遍（防 TOCTOU）**；
- 失败时**清理本笔新建的空目录**。

## 从备份**手工找回**（阶段 6 · `回归检查` · 判据 `回归检查`）

本模块另导出**只读**的找回入口两件：

    list_backups() -> BackupListing     # 列出 data/workspace-backups/ 里的快照
    read_backup(name) -> bytes          # 按快照名取回**写前**字节

🔴 **入口形态定档 = 【函数】**（不是端点）：本阶段**不加端点** ——
新增端点须先改 `接口说明/02-API契约冻结清单.md`，撞红线 7 与 `D-回归检查`「契约面零改动」。
⇒ 本阶段**只**提供「宿主侧 / 脚本可调的函数入口」，界面入口**未做**（移交阶段 7）。

🟢 **入口打不开时的兜底**：快照件的**命名是可读的**（`_snapshot_name`：UTC 时间戳 +
目标名摘要 + 原名）⇒ 用户**可以在文件管理器里手工打开**
`data/workspace-backups/` 直接取件 —— 这正是「普通可读文件，不加密、不藏」的含义。

🔴 **备份 ≠ 回滚**：本模块**只**回答「有哪些副本可以手工找回」——
**不提供**任何「一键」形态的自动恢复（红线 9 禁语，逐字见 §0.5 的禁语表）——
写回工作区仍走 :func:`write_workspace_file`（受凭证闸与工作区根门支配）。

⚠️ **不假定目录存在**（§0.2 行 14 实测：`data/` 下只有 `uploads/`）⇒
:func:`list_backups` 在目录缺失时返回**空列表 + 中文提示**，**绝不抛**。
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.agent.tools import _write_paths

#: 备份目录相对数据根的形状（零迁移；目录**自建**，不得假定 data/ 已存在）
BACKUP_DIRNAME = "workspace-backups"

#: 备份目录**不存在**时的中文提示（A27 腿①：**不得抛**）
BACKUP_DIR_MISSING_HINT = "备份目录尚未创建（{path}）—— 目前没有任何写前快照，属正常状态。"

#: 按快照名取回时的失败文案（中文可读；不含绝对路径）
BACKUP_NOT_FOUND_MESSAGE = "找不到该快照：{name}（可先用 list_backups() 查看现有快照名）。"

#: 快照名里的**路径分隔符**禁令（防止按快照名逃出备份目录 —— 同 _write_paths 的越界精神）
_BACKUP_NAME_FORBIDDEN = ("/", chr(92), ":")

#: 快照件的加密 / 压缩后缀伪装（**不加密、不藏**的可判形态）
_BACKUP_DISGUISE_SUFFIXES: tuple[str, ...] = (".enc", ".encrypted", ".bin", ".gz", ".zip", ".dat")


@dataclass(frozen=True)
class BackupEntry:
    """备份目录里的一份**写前快照**（A27 腿①：普通可读文件，不加密、不藏）。"""

    #: 快照标识 = 备份目录内的**文件名**（= :func:`_snapshot_name` 的产物）
    name: str
    #: 快照件绝对路径（备份目录**之外**的调用方应只用 name 定位）
    path: Path
    #: 字节数（**可判真假**：普通文件的大小）
    size: int


@dataclass(frozen=True)
class BackupListing:
    """**备份列表**的产物（A27 腿①）。

    🔴 为什么是 dataclass 而不是裸 list：目录**不存在**时既要「返回空列表」又要
    「带上中文提示」（需求逐字）⇒ 两者必须落在**同一个返回值**里，调用方无法只看其一。
    """

    #: 快照件，按名字**升序**（名字含时间戳 ⇒ 升序 = 时间序，且**可判真假**）
    entries: list[BackupEntry]
    #: 备份目录的绝对路径（**诊断用**；目录不存在时也给出，便于用户手工去看）
    directory: Path
    #: 目录不存在（或无快照）时的**中文提示**；有快照时为空串
    hint: str = ""


def _is_plain_snapshot(path: Path) -> bool:
    """是不是「普通可读文件」形态的快照件（A27 腿①：**不加密、不藏**）。

    🔴 过滤条件**必须**逐条为真，且**每一条都可判真假**（本函数是变异 `M23` 的靶点）：
      ① 是真文件（不是目录）；
      ② 名字**不以 ``.`` 开头** —— 隐藏件 = 「藏」；
      ③ 名字**不以 ``.tmp-`` 开头** —— 那是原子写的临时件，不是快照；
      ④ 没有加密 / 压缩后缀伪装 —— 与 `test_write_snapshot_t3_5.py` 的口径同源。

    为什么不能省掉 ②③④ 里的任何一条：省掉 ⇒ 列表会把**临时件 / 隐藏件 / 伪装件**
    一并报成「可手工找回的副本」⇒ A27 腿① 的「普通可读文件」当场失真。
    """
    if not path.is_file():
        return False
    name = path.name
    if name.startswith("."):
        return False
    lowered = name.lower()
    for suffix in _BACKUP_DISGUISE_SUFFIXES:
        if lowered.endswith(suffix):
            return False
    return True


def list_backups() -> BackupListing:
    """**列出**备份目录里的写前快照（A27 腿①）。

    🔴 三条硬口径：
      ① 目录**可能不存在**（§0.2 实测：``data/`` 下只有 ``uploads/``）⇒ **返回空列表
         + 中文提示，绝不抛**；
      ② 只读**普通可读文件**（不加密、不藏）—— 过滤条件见 :func:`_is_plain_snapshot`；
      ③ 走 **:func:`resolve_backup_dir`** 这一个锚点（**不得**改用
         ``settings.workspace_backup_dir``：那条路**无测试态守卫** ⇒ 会绕过 `D-回归检查`）。

    ⚠️ **备份 ≠ 回滚**：本函数只回答「**有哪些副本可以手工找回**」——
    **不提供**任何「一键」形态的自动恢复（红线 9 禁语）。
    """
    backup_dir = resolve_backup_dir()
    if not backup_dir.is_dir():
        return BackupListing(
            entries=[],
            directory=backup_dir,
            hint=BACKUP_DIR_MISSING_HINT.format(path=backup_dir),
        )

    try:
        candidates = list(backup_dir.iterdir())
    except OSError as exc:
        # 目录存在但读不了（权限 / 占用）⇒ 同样**不抛**，退化成「空列表 + 提示」
        return BackupListing(
            entries=[],
            directory=backup_dir,
            hint=f"备份目录无法读取（{exc.__class__.__name__}）：{backup_dir}",
        )

    entries = [
        BackupEntry(name=item.name, path=item, size=item.stat().st_size)
        for item in candidates
        if _is_plain_snapshot(item)
    ]
    entries.sort(key=lambda entry: entry.name)

    if not entries:
        return BackupListing(
            entries=[],
            directory=backup_dir,
            hint=BACKUP_DIR_MISSING_HINT.format(path=backup_dir),
        )
    return BackupListing(entries=entries, directory=backup_dir)


def read_backup(name: str) -> bytes:
    """按快照名**取回**写前内容（A27 腿②：**手工**找回，逐字节）。

    :param name: 快照标识（= :func:`list_backups` 给出的 ``entry.name``）
    :raises WriteError: 名字为空 / 含路径分隔符 / 快照不存在 —— 均为中文可读

    🔴 **只读**：本函数**只**调 ``read_bytes()``，**没有任何**写回工作区的动作 ——
    「找回」是**用户手工**把内容贴回去（备份 ≠ 回滚，红线 9 禁语）。
    """
    raw = (name or "").strip()
    if not raw:
        raise WriteError("快照名不能为空（可先用 list_backups() 查看现有快照名）。")
    for token in _BACKUP_NAME_FORBIDDEN:
        if token in raw:
            raise WriteError(f"快照名不得含路径分隔符：{raw}（只接受备份目录内的文件名）。")

    path = resolve_backup_dir() / raw
    if not path.is_file():
        raise WriteError(BACKUP_NOT_FOUND_MESSAGE.format(name=raw))
    try:
        return path.read_bytes()
    except OSError as exc:
        raise WriteError(f"读取快照失败：{exc.__class__.__name__}（{raw}）。") from exc


@dataclass(frozen=True)
class WriteOutcome:
    """一次写调用的产物（D-回归检查 ⑥ 逐字：至少三键）。

    ⚠️ 三键是 回归检查 三个写通道审计键的**唯一来源** ——
    write_target / write_byte_delta / write_snapshot_id **不得由调用侧另算**。
    """

    #: 落点绝对路径
    target: Path
    #: **字节增量** = len(新内容) - len(旧内容)（新建时旧内容按 0 计）
    #: 🔴 **在内存里算**，**不得**用「文件 size 差」替代（B-B3 逐字口径）
    byte_delta: int
    #: 写前快照的标识；None = 本次**没有**快照（新建 / snapshot=False）
    snapshot_id: str | None = None
    #: 本次是否走了**新建**分支（create=True 且目标原先不存在）
    created: bool = False
    #: 供观察文本用的派生字段（workspace_write 的 handler 取它）
    text: str = ""
    #: 额外审计键（回归检查 预留；默认空 ⇒ 既有行为不受影响）
    extra: dict = field(default_factory=dict)
    #: **写前 vs 写后**的行级 diff（回归检查 · 判据 ``回归检查``）。
    #:
    #: 🔴 **旧内容必须是【写盘前读到的】那份字节**（``before``）—— 反过来（先写、再把目标
    #: 内容当旧内容）会让 diff **恒为空**、判据**结构性恒真**（阶段 4 ``M-回归检查`` / 阶段 5
    #: ``M-回归检查`` 是同型陷阱）。本模块的 :func:``write_workspace_file`` 本来就在写动作**之前**
    #: 读 ``before``（快照纪律），diff 直接复用那一份 ⇒ 不另读、也不回读目标反推。
    #: ⚠️ **派生字段**（不是审计三键之一）⇒ 不进 ``write_meta`` 的键集，``回归检查`` 的口径不变；
    #: 它只喂给用户可见的步骤帧。
    write_diff: str = ""


class WriteError(RuntimeError):
    """写通道的中文可读失败（I5 形态：调用侧捕获后转 ok=False 观察）。

    裸 OSError 的 errno 文案不适合直接给用户看，且会泄露绝对路径 ——
    故本模块**统一**包装成中文。
    """


class TestModeRepoWriteError(RuntimeError):
    """🔴 **测试态试图把快照写进仓库内的 `data/`** —— 结构化 fail-closed。

    出处：阶段 5 的**实测事故**（`开发约定` 与 `回归检查-阶段小结.md` §四 缺陷 ⑤）：
    载体夹具**打空锚点** ⇒ 快照落进**真实数据目录** `data/workspace-backups/`，
    第一轮累计 **65 件**、第二轮累计 **170 件**（清理两次）。

    为什么**不是**「靠每个载体自觉 monkeypatch」：实测证明**会漏**（两轮都漏了）——
    「每个作者都记得打锚点」是**没有安全网**的约定。⇒ 改成**结构性拒绝**：
    测试态（`PYTEST_CURRENT_TEST` 存在）下解析出的路径**落在仓库根内** ⇒ **直接抛**。
    """


def resolve_backup_dir() -> Path:
    """备份目录的绝对路径（**属性锚点**，可被测试 monkeypatch 到 tmp_path）。

    形状照 config.upload_path 同型：ROOT_DIR / "data/workspace-backups"。
    🔴 **不假定 data/ 已存在**（§0.2 B 组实测：data/ 下只有 uploads/）
    ⇒ 调用方用 mkdir(parents=True, exist_ok=True) 自建。

    ## 🔴 测试态守卫（`D-回归检查` 的具体实现 · 2026-09-26 加）

    若 `PYTEST_CURRENT_TEST` 存在（= 正在跑 pytest）**且**解析出的路径**落在仓库根内**
    ⇒ **抛 :class:`TestModeRepoWriteError`**（fail-closed）。

    ⚠️ **只影响测试态**：生产态（无该环境变量）**逐字不变**；
    测试里 monkeypatch 到 `tmp_path` 的载体**也不受影响**（那条路径在仓库外）。
    """
    from app.core.config import ROOT_DIR

    path = ROOT_DIR / "data" / BACKUP_DIRNAME
    if os.environ.get("PYTEST_CURRENT_TEST"):
        try:
            path.resolve().relative_to(Path(ROOT_DIR).resolve())
        except ValueError:
            pass  # 仓库外（如 tmp_path）⇒ 放行
        else:
            raise TestModeRepoWriteError(
                "测试态禁止把快照写进仓库内的 data/workspace-backups/ —— "
                "请用 monkeypatch 把 `_write.resolve_backup_dir` 锚到 tmp_path（红线 12）。"
                f"（实测解析值：{path}）"
            )
    return path


def _snapshot_name(target: Path) -> str:
    """快照文件名：时间戳 + 目标名摘要（照 storage.py 的「指纹定名」精神）。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    digest = hashlib.sha256(str(target).encode("utf-8")).hexdigest()[:8]
    return f"{stamp}-{digest}-{target.name}"


def _take_snapshot(target: Path, before: bytes) -> str | None:
    """**写前**快照：把 before 落到备份目录，返回快照标识。

    🔴 **顺序**：本函数必须在任何写动作**之前**被调用，且 before 必须是**写前**读到的字节。
    反过来（先写再备份）会让 A21 腿② 恒真（阶段 4 M-回归检查 同型陷阱）。
    """
    backup_dir = resolve_backup_dir()
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        name = _snapshot_name(target)
        snapshot_path = backup_dir / name
        tmp = backup_dir / f".tmp-{uuid.uuid4().hex}"
        try:
            tmp.write_bytes(before)
            os.replace(tmp, snapshot_path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return name
    except OSError as exc:
        # 快照失败 ⇒ **整个写调用 fail-closed 拒绝**（不得「快照失败但照样写」）
        raise WriteError(f"写前快照失败，已拒绝写入：{exc.__class__.__name__}") from exc


def _recheck_write_target(dest: Path, root: Path) -> None:
    """复用写路径校验，拒绝 resolve 后变更的目录身份。"""
    verdict = _write_paths.resolve_write_target(os.path.relpath(dest, root), root)
    if isinstance(verdict, _write_paths.WriteReject):
        raise WriteError(verdict.reason)
    if verdict.resolved != dest:
        raise WriteError("写入路径已变化，已拒绝写入")


def _write_atomically(dest: Path, data: bytes, *, create: bool = False, root: Path | None = None) -> None:
    """同目录临时文件原子发布；create 使用无覆盖链接，覆盖使用 replace。

    🔴 except BaseException（不是 Exception）—— 取消也必须清理（A7-2 的结构前提）。
    ⚠️ 临时文件**必须与目标同目录**，否则 os.replace 跨卷不原子（K-回归检查）。
    """
    tmp = dest.parent / f".tmp-{hashlib.sha256(data).hexdigest()[:8]}-{uuid.uuid4().hex}"
    try:
        if root is not None:
            _recheck_write_target(dest, root)
        tmp.write_bytes(data)
        if root is not None:
            # ponytail: 关闭受测替换窗口；复判与发布仍非句柄级原子绑定，不能证明任意外部竞态。
            _recheck_write_target(dest, root)
        if create:
            os.link(tmp, dest)
            try:
                tmp.unlink()
            except OSError as exc:
                # 发布已经成功，清理失败不能使凭证、审计与真实文件状态相反。
                import logging
                logging.getLogger(__name__).warning("写入已完成，临时文件清理失败：%s", exc.__class__.__name__)
        else:
            os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


#: 兼容别名：回归检查 的载体按 _write_atomically / _atomic_write 两个名字找注入点
_atomic_write = _write_atomically


def _cleanup_empty_dirs(paths: list[Path], stop_at: Path) -> None:
    """失败时清理**本笔新建的**空目录（自内向外，遇到非空/根即停）。

    D-回归检查 逐字：「失败时清理本笔新建的空目录」—— 不得残留。
    """
    for path in paths:
        current = path
        while True:
            try:
                if current == stop_at or not current.is_dir():
                    break
                current.rmdir()  # 仅空目录可删；非空会抛 OSError ⇒ 由下面兜住
            except OSError:
                break
            current = current.parent


def write_workspace_file(
    *,
    root: Path,
    rel_path: str,
    data: bytes,
    create: bool,
    snapshot: bool,
    confirm_token: str | None = None,
) -> WriteOutcome:
    """把 data 写入 root/rel_path（**唯一写入口**）。

    :param root: 工作区根（**必须**由调用侧给定；本模块不读进程 CWD —— 红线 1）
    :param rel_path: 相对工作区根的路径（**绝对路径 ⇒ 拒**，见 _write_paths）
    :param data: 要写入的**完整字节**（本模块原样落盘，**不做任何编码/行尾转换**）
    :param create: True ⇒ 新建语义（**已存在则拒**）；False ⇒ 覆盖语义
    :param snapshot: True ⇒ 覆盖前先做写前快照（**备份 ≠ 回滚**）
    :param confirm_token: 宿主侧签发的凭证（校验闸归 回归检查；本笔**原样透传**）
    :raises WriteError: 路径被拒 / 已存在（create）/ 快照失败 / IO 失败 —— 均为中文可读
    """
    verdict = _write_paths.resolve_write_target(rel_path, root)
    if isinstance(verdict, _write_paths.WriteReject):
        raise WriteError(verdict.reason)

    dest = verdict.resolved
    _recheck_write_target(dest, root)

    # ── create=True 且目标已存在 ⇒ 拒（不得静默覆盖；与 create=False 在同一判定点分流）──
    if create and verdict.exists:
        raise WriteError(f"目标已存在，create=True 拒绝覆盖：{rel_path}")

    before: bytes | None = None
    if verdict.exists:
        try:
            before = dest.read_bytes()
        except OSError as exc:
            raise WriteError(f"读取原文件失败，已拒绝写入：{exc.__class__.__name__}") from exc

    # ── 写前快照（**必须在写动作之前**；失败 ⇒ fail-closed 拒绝） ──────────────
    snapshot_id: str | None = None
    if snapshot and before is not None:
        snapshot_id = _take_snapshot(dest, before)

    # ── 新建分支：父目录不存在 ⇒ 先判（判之前零 IO）⇒ 建 ⇒ 再判一遍（防 TOCTOU） ──
    # 🔴 什么时候建目录：**仅当 create=True**（新建语义，A28 腿①）。
    #    create=False（覆盖语义）时父目录不存在 ⇒ 拒绝（那是「覆盖一个不存在的文件」，语义不通）。
    created_dirs: list[Path] = []
    if not dest.parent.is_dir():
        if not create:
            raise WriteError(f"目标父目录不存在（覆盖语义不建目录）：{rel_path}")
        missing: list[Path] = []
        probe = dest.parent
        while not probe.is_dir() and probe != probe.parent:
            missing.append(probe)
            probe = probe.parent
        # 🔴 `回归修复`：建之前**真的**再判一遍（此前注释声称复判、代码里没有）。
        # 对每个待建段重过 `_paths.classify`：missing 段虽不存在，其 realpath 会**穿过
        # 全部既有祖先**解析——祖先若在 resolve 之后被换成指向根外的链接，这里当场
        # 拒绝（读侧越界冻结文案 `Reject.reason` 原样透传，不另造文案）。
        # ⚠️ 越界样本在 resolve 层早退、根本进不到本分支 ⇒ A9「越界零 IO」腿不受影响；
        # ⚠️ **边界登记**：根内 junction（islink=False）现状仍放行，不归本条。
        from app.agent.tools import _paths  # noqa: PLC0415

        for seg in missing:
            seg_rel = os.path.relpath(seg, root).replace("\\", "/")
            seg_verdict = _paths.classify(seg_rel, root)
            if isinstance(seg_verdict, _paths.Reject):
                raise WriteError(seg_verdict.reason)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WriteError(f"创建父目录失败：{exc.__class__.__name__}") from exc
        created_dirs = list(reversed(missing))

    # ── 原子写 ────────────────────────────────────────────────────────────────
    try:
        _write_atomically(dest, data, create=create, root=root)
    except BaseException:
        # 失败 ⇒ 清理本笔新建的空目录（D-回归检查），再把异常交出去
        _cleanup_empty_dirs(created_dirs, stop_at=Path(root))
        raise

    # ``byte_delta`` 口径 = **落点文件的 size 变化**（新建时旧 size 按 0 计）。
    # 🔴 **在内存/一次 stat 里算**，不由调用侧回读 —— 且**新建时**旧 size 恒 0。
    # ⚠️ 为什么不用 ``len(before)``：载体（``test_write_create_t3_5.py``）显式要求
    #    「落盘后再读回的真实 size 差」（它对已存在文件写 3 字节 / 原 2 字节 ⇒ 期望 1）。
    try:
        new_size = dest.stat().st_size
    except OSError:  # 理论不可达（刚写完）；真发生也不得让写调用失败
        new_size = len(data)
    byte_delta = new_size - (len(before) if before is not None else 0)

    # ── 清缓存接线（D-回归检查 第 1 条 · 阶段 4 回归检查 的接线点，落在本笔 回归检查） ──────
    # 🔴 **必须在写【成功之后】**（失败路径不得清 —— 否则成功与失败不可区分）
    # 🔴 清缓存（B3） ≠ 重建（B1）：**不得**顺手触发 reindex（那会越过单⑤ (b)「手动重建」，撞红线 4）
    # ⚠️ 接线失败**不得**让整个写调用失败（写盘已经成功）—— 只记 warning，与 audit 的 safe_* 同口径
    try:
        from app.cache import store as _cache_store

        _cache_store.invalidate_all(reason=f"workspace_write:{rel_path}")
    except Exception as exc:  # noqa: BLE001 —— 清缓存是正确性动作，但写盘已成功 ⇒ 不因它回滚
        import logging

        logging.getLogger(__name__).warning(
            "写完清缓存失败（写盘已成功，不回滚）：%s: %s", exc.__class__.__name__, exc
        )

    return WriteOutcome(
        target=dest,
        byte_delta=byte_delta,
        snapshot_id=snapshot_id,
        created=not verdict.exists,
        text=f"已写入工作区文件：{rel_path}（{byte_delta:+d} 字节）",
        extra={},
        # 旧内容 = **写盘前读到的** before（新建时为空字节）⇒ diff 说的是真话
        write_diff=render_diff(before or b"", data),
    )


#: 行级 diff 的上下文行数（前后各留几行未变行）
DIFF_CONTEXT = 3

#: diff 的 hunk 头（统一格式；``@@ -a,b +c,d @@``）
_DIFF_HUNK = "@@ -{old_start},{old_len} +{new_start},{new_len} @@"


def _split_lines(data: bytes) -> list[str]:
    """把字节按行切开（**保真**：只按 ``\\n`` 切，行尾不还原）。

    ⚠️ 用 ``data.split(b"\\n")`` 而不是 ``decode().splitlines()``：后者会一并吃掉
    ``\\r``、还依赖编码，让「行」的定义随内容变；本函数只要**稳定的行切分**，
    文本解码交给调用方且 ``errors="replace"``（编码坏掉也**不得抛** —— 那会让**写成功**
    的一次调用在 diff 这一步炸掉）。
    """
    if not data:
        return []
    parts = data.split(b"\n")
    # 末尾正好一个 \n ⇒ split 出一个空尾巴，那不是一行
    if parts and parts[-1] == b"":
        parts = parts[:-1]
    return [part.decode("utf-8", errors="replace") for part in parts]


def render_diff(before: bytes, after: bytes) -> str:
    """**行级 diff**（``回归检查``）：旧字节 vs 新字节 ⇒ 统一格式的文本。

    ## 口径（逐条写死）

    - 未变行以**一个空格**开头；新增行 ``+``、删除行 ``-``（统一 diff 惯例）；
    - **相同的首尾不显示** —— 只输出「变化点前后各 :data:``DIFF_CONTEXT`` 行」的窗口，
      窗口之间断开即另起一个 hunk（头 = ``_DIFF_HUNK``）；
    - **内容未变** ⇒ 返回**空串**（不是「全文都算未变行」那一堆上下文）；
    - **新建**（``before`` 为空）⇒ 全部为新增行（``+`` 前缀），**不得**返回空串、
      **不得**抛（判据 ``回归检查`` 腿② 的逐字要求）；
    - 纯函数 / 零 IO / 不抛：任何输入都返回 ``str``。
    """
    old = _split_lines(before)
    new = _split_lines(after)
    # ── LCS 动态规划（行级；两两相等才算「未变」）──────────────────────────────
    n, m = len(old), len(new)
    # 表大小 = (n+1)×(m+1)；写工具的单次写是**用户可编辑的文本文件**，
    # 与 ``_encode_for_target` 的全量读口径一致（不另立阈值），故不做上界裁剪。
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        row, nxt = dp[i], dp[i + 1]
        oi = old[i]
        for j in range(m - 1, -1, -1):
            if oi == new[j]:
                row[j] = nxt[j + 1] + 1
            else:
                row[j] = nxt[j] if nxt[j] >= row[j + 1] else row[j + 1]

    # ── 回溯出「操作序列」：(' ', old_idx, new_idx) / ('-', i, None) / ('+', None, j) ──
    ops: list[tuple[str, int | None, int | None]] = []
    i = j = 0
    while i < n and j < m:
        if old[i] == new[j]:
            ops.append((" ", i, j))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            ops.append(("-", i, None))
            i += 1
        else:
            ops.append(("+", None, j))
            j += 1
    while i < n:
        ops.append(("-", i, None))
        i += 1
    while j < m:
        ops.append(("+", None, j))
        j += 1

    # ── 只保留「变化点前后各 DIFF_CONTEXT 行」的窗口 ─────────────────────────────
    keep = [False] * len(ops)
    for idx, (tag, _oi, _nj) in enumerate(ops):
        if tag == " ":
            continue
        lo = max(0, idx - DIFF_CONTEXT)
        hi = min(len(ops), idx + DIFF_CONTEXT + 1)
        for k in range(lo, hi):
            keep[k] = True
    if not any(keep):
        return ""  # 内容未变 ⇒ **空 diff**（腿② 的负控要的就是这条）

    out: list[str] = []
    k = 0
    while k < len(ops):
        if not keep[k]:
            k += 1
            continue
        start = k
        while k < len(ops) and keep[k]:
            k += 1
        chunk = ops[start:k]
        old_start = (chunk[0][1] if chunk[0][1] is not None else chunk[0][2]) or 0
        new_start = (chunk[0][2] if chunk[0][2] is not None else chunk[0][1]) or 0
        old_len = sum(1 for tag, _a, _b in chunk if tag != "+")
        new_len = sum(1 for tag, _a, _b in chunk if tag != "-")
        out.append(
            _DIFF_HUNK.format(
                old_start=old_start + 1, old_len=old_len,
                new_start=new_start + 1, new_len=new_len,
            )
        )
        for tag, oi, nj in chunk:
            text = old[oi] if tag != "+" else new[nj]
            out.append(tag + (text if text is not None else ""))
    return "\n".join(out)


def _rel_of(path: Path, root: Path | None) -> Path:
    """把绝对路径还原成「相对根的形态」（仅用于父目录复判；失败则返回空）。"""
    if root is None:
        return Path()
    try:
        return path.relative_to(Path(root))
    except ValueError:
        return Path()


__all__ = [
    "BACKUP_DIRNAME",
    "BACKUP_DIR_MISSING_HINT",
    "BACKUP_NOT_FOUND_MESSAGE",
    "BackupEntry",
    "BackupListing",
    "WriteError",
    "WriteOutcome",
    "_atomic_write",
    "_write_atomically",
    "list_backups",
    "read_backup",
    "render_diff",
    "resolve_backup_dir",
    "write_workspace_file",
]
