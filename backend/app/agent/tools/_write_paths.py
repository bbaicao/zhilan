"""写侧路径判定（专项工具升级-3 · 阶段 5 · 回归检查 · D-回归检查 取 (甲) 档）。

## 为什么是**新文件**，而不是给 _paths.py 加参数

_paths.py 是**阶段 1 已验收件**（回归检查…回归检查）。classify 只回答「**能不能读**」
（Verdict = Normal | Reject，:179），而写侧要回答「**能不能写**」
（只读文件 / 目录 / 符号链接 / 父目录不可写 / 已存在策略）。

D-回归检查 三档里取 **(甲)**：**新增独立写侧函数**、不动 classify。代价实测差：

| 档 | 代价 |
|---|---|
| **(甲)** OK | **零改阶段 1 件**；新函数自带判据 |
| (乙) | 动 classify 签名 ⇒ 须复跑 test_tool_paths_t3.py **36 例** + test_workspace_root_t3_3.py **17 例** = **53 例**，且 tests/_io_probe.py 的拦截面会变 ⇒ A1/A2/A3 的「零 IO」判据须重新自证 |
| (丙) | 写侧只复用读侧结果 ⇒ 目标可能是只读文件 / 目录 / 符号链接目标，读侧 Normal **不足以**放行写 |

## 零 IO 分层（A9 的命门）

本模块的判定次序**写死**，且**越界样本必须在任何 IO 之前返回**：

| 步 | 内容 | IO |
|---|---|---|
| 0 | 空串 / **绝对路径**（盘符、UNC、设备命名空间）⇒ **直接拒** | **零**（纯字符串） |
| 1 | _paths.classify(rel_path, root) —— **越界在此早退** | **零**（见下） |
| 2 | 写侧敏感清单（段 / 后缀）⇒ 拒 | **零**（纯词法） |
| 3 | 父目录逐级在根内 + 父目录敏感 ⇒ 拒 | **零**（纯字符串） |
| 4 | ——— 以下才允许 IO ——— | |
| 4a | 目标存在？符号链接？目录？只读？ | IO |
| 4b | 目标不存在 ⇒ 父目录存在且可写？ | IO |

原因：越界样本在 **① 词法层**（_paths.py:493-495）或 **② 根前缀层**（:497-505）返回，
两层纯字符串；_paths.py 的第一个 IO 点是 :510 root_is_usable_dir、第二个是 :521 _realpath_absolute
—— **两者都在 :504 is_inside 通过之后** ⇒ 越界样本**结构上到不了任何 IO**。

实测（patch 三个原语计数后调 classify；路径字面量见证据件 回归检查-审计-B路-开工包.md）：
  · 上级逃逸样本   -> Reject，layer=prefix ，realpath=0 / isdir=0 / exists=0
  · 异盘绝对样本   -> Reject，layer=lexical，realpath=0 / isdir=0 / exists=0
  · 合法相对对照   -> Normal，           realpath=1 / isdir=1 / exists=1

⇒ 本模块**必须先调 classify**（不得自己重写边界逻辑 —— 两份实现必然漂移），
   且**不得**在 classify 之前对候选目标做任何 resolve/stat/exists 预探。

## 写侧敏感清单 = **新建常量**（不得改 SENSITIVE_SEGMENTS）

C-R1(甲) 后半句逐字要求：**写侧若要额外拒绝某类路径，必须【新建写侧常量】**，
不得把新项塞进 _paths.SENSITIVE_SEGMENTS —— 那会**改变读侧行为**（阶段 1 已验收件），
且既有腿**拦不住加项**（出处 tests/agent/test_tool_paths_t3.py 的
test_a6b_sensitive_list_is_reusable_by_write_side 口径锁）。

⇒ 本模块的写侧表**只加不减**：在 _paths.SENSITIVE_* 之上追加写侧专属项，**读侧清单一字不动**。

## 如实登记的边界（不得含糊）

- _paths.classify **只判「路径在不在根内」**，**不判 ACL / 权限**（阶段 3 A5b 同型口径）。
  ⇒ 本模块**不得**声称「写权限已被完整校验」；能判的写侧形态 = 下面那五条，
    **覆盖不到的（如 Windows ACL 细粒度、EFS 加密、文件被占用）如实登记为不覆盖**。
- os.access(path, os.W_OK) 在 Windows 上**不看 ACL**（只看只读属性 + 目录权限），
  故它对「只读文件」有效、对 ACL 细粒度**无效** —— 这是第二条如实登记。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.agent.tools import _paths

# ── 写侧敏感清单（**新建**；只加不减，见模块 docstring） ──────────────────────
#: 写侧额外拒绝的**目录名 / 段名**（在 _paths.SENSITIVE_SEGMENTS 之上追加）
WRITE_EXTRA_SEGMENTS: frozenset[str] = frozenset(
    {
        ".svn",
        ".hg",
        "node_modules",
        ".venv",
        "__pycache__",
        ".idea",
        ".vscode",
    }
)

#: 写侧额外拒绝的**后缀**（在 _paths.SENSITIVE_SUFFIXES 之上追加）
WRITE_EXTRA_SUFFIXES: frozenset[str] = frozenset({".pfx", ".p12", ".jks", ".keystore"})


def _write_sensitive_hit(path: Path | str) -> bool:
    """写侧敏感判定：**读侧清单 并上 写侧追加清单**。

    与读侧的关系（只加严，红线 2「范围只能收窄」）：
      · 读侧判「不得读」的，写侧**同样判「不得写」**；
      · 写侧另有专属追加项。
    """
    if _paths._sensitive_hit(path):  # noqa: SLF001 —— 同包私有复用，比复制一份清单更安全
        return True
    parts = Path(os.path.normpath(str(path))).parts
    for part in parts:
        lowered = part.strip().lower()
        if not lowered:
            continue
        if lowered in WRITE_EXTRA_SEGMENTS:
            return True
        for suffix in WRITE_EXTRA_SUFFIXES:
            if lowered.endswith(suffix):
                return True
    return False


@dataclass(frozen=True)
class WriteTarget:
    """**放行**判定结果（照 _paths.Normal 的同族形态）。

    exists 是**单观测量、双语义**：create=True 时它是**拒的依据**（已存在 ⇒ 拒），
    create=False 时它是**快照的依据**（有旧内容 ⇒ 写前快照）。
    必须在同一个判定点产出，不得两处各取一次（防 TOCTOU）。
    """

    raw: str
    #: 归一后的绝对路径（来自 Normal.resolved）
    resolved: Path
    #: 目标是否已存在
    exists: bool
    #: 目标的父目录（绝对）
    parent: Path
    #: 父目录是否**已存在**（create=True 且父目录不存在 ⇒ 由写函数负责创建，见 D-回归检查）
    parent_exists: bool = True


@dataclass(frozen=True)
class WriteReject:
    """**拒绝**判定结果：**必带中文原因**（I5 形态：不抛）。"""

    raw: str
    layer: str
    reason: str


WriteVerdict = WriteTarget | WriteReject

#: 拒绝文案（**冻结**，写进用例）
WRITE_ABS_PATH_TEMPLATE = "只允许写入工作区根内的相对路径，绝对路径已拒绝：{path}"
WRITE_EMPTY_TEMPLATE = "路径为空，已拒绝：{path}"
WRITE_SENSITIVE_TEMPLATE = "敏感路径不可写入，已拒绝：{path}"
WRITE_SYMLINK_TEMPLATE = "目标是符号链接（不追随链接），已拒绝：{path}"
WRITE_IS_DIR_TEMPLATE = "目标是目录（本阶段只写单个文件），已拒绝：{path}"
WRITE_READONLY_TEMPLATE = "目标文件只读，已拒绝：{path}"
WRITE_PARENT_UNWRITABLE_TEMPLATE = "目标父目录不可写，已拒绝：{path}"
#: 目标不存在，且**父目录也不存在** —— 本模块**不**创建目录（目录创建归 回归检查 的 create 分支）
WRITE_NO_PARENT_TEMPLATE = "目标父目录不存在，已拒绝：{path}"


def _is_absolute_form(raw: str) -> bool:
    """绝对路径形态（**纯字符串判定，零 IO**）。

    D-回归检查 ⑦ 逐字：「path 只接受相对路径；**绝对路径 ⇒ 拒**（**不**先归一再判 —— 直接拒，最保守）」
    ⇒ 这里**不**调用任何 resolve / stat，按形态直接判。
    """
    text = (raw or "").strip()
    if not text:
        return False
    # 盘符形 C:\ / C:/ ；UNC 双反斜杠或双斜杠；设备命名空间同 UNC 前缀
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        return True
    return text.startswith("\\\\") or text.startswith("//")


def resolve_write_target(rel_path: str, root: Path | None) -> WriteVerdict:
    """**唯一入口**：把「工作区根相对路径」判成 WriteTarget 或 WriteReject。

    判定次序**写死**（见模块 docstring 的表）——**越界先于 IO** 是 A9 成立的结构前提。

    :param rel_path: 相对工作区根的路径（**绝对路径直接拒**）
    :param root: 工作区根（None ⇒ 拒，fail-closed）
    """
    raw = rel_path or ""

    # ── 第 0 步：纯字符串（零 IO） ──────────────────────────────────────────
    if not raw.strip():
        return WriteReject(raw=raw, layer="lexical", reason=WRITE_EMPTY_TEMPLATE.format(path=raw))
    if _is_absolute_form(raw):
        return WriteReject(
            raw=raw, layer="lexical", reason=WRITE_ABS_PATH_TEMPLATE.format(path=raw)
        )

    # ── 第 1 步：读侧边界（复用阶段 1 的五层；越界在此早退，零 IO） ──────────
    verdict = _paths.classify(raw, root)
    if isinstance(verdict, _paths.Reject):
        # 原样透传 layer/reason（读侧文案已冻结，不另造一套）
        return WriteReject(raw=raw, layer=verdict.layer, reason=verdict.reason)

    resolved = verdict.resolved
    #: ② 层词法绝对化产物（**未经** ③ 层 realpath 展开）——末段链接判定必须用它
    #: （`回归修复`：`resolved` 的末段链接已被展开，`islink(resolved)` 恒假）
    absolute = verdict.absolute

    # ── 第 2 步：写侧敏感清单（纯词法，零 IO；必须在任何 stat 之前） ──────────
    if _write_sensitive_hit(resolved):
        return WriteReject(
            raw=raw, layer="sensitive", reason=WRITE_SENSITIVE_TEMPLATE.format(path=raw)
        )

    parent = resolved.parent
    # ── 第 3 步：父目录逐级仍在根内 + 父目录不敏感（纯字符串，零 IO） ─────────
    if root is not None and not _paths.is_inside(Path(root), parent):
        return WriteReject(
            raw=raw,
            layer="recheck",
            reason=_paths.PATH_OUTSIDE_TEMPLATE.format(path=raw),
        )
    if _write_sensitive_hit(parent):
        return WriteReject(
            raw=raw, layer="sensitive", reason=WRITE_SENSITIVE_TEMPLATE.format(path=raw)
        )

    # ── 第 4 步：以下才允许 IO ────────────────────────────────────────────
    # 符号链接判定必须排在「目录」之前：否则指向根外的链接会被 is_dir 追随，判出错误形态
    # 🔴 `回归修复`：末段是否链接必须判在 `absolute` 上——旧写法 `islink(resolved)`
    # 恒假（realpath 已把末段链接展开）⇒ 分支形同虚设，指向根内目标的链接被写到
    # **目标本体**而非拒绝。末段是链接 ⇒ 一律拒（冻结文案透传；破碎链接
    # ``lexists(absolute)==True`` 同样进本分支）。
    # ⚠️ **边界登记（不归本条）**：Windows junction 的 ``os.path.islink`` 返回 False
    # ⇒ 根内 junction 现状仍放行（指向根外的 junction 由第 1 步 realpath 复判拦下）。
    if os.path.lexists(str(absolute)) and os.path.islink(str(absolute)):
        return WriteReject(
            raw=raw, layer="write_target", reason=WRITE_SYMLINK_TEMPLATE.format(path=raw)
        )
    if os.path.lexists(str(resolved)):
        if os.path.isdir(str(resolved)):
            return WriteReject(
                raw=raw, layer="write_target", reason=WRITE_IS_DIR_TEMPLATE.format(path=raw)
            )
        if not os.access(str(resolved), os.W_OK):
            return WriteReject(
                raw=raw, layer="write_target", reason=WRITE_READONLY_TEMPLATE.format(path=raw)
            )
        return WriteTarget(raw=raw, resolved=resolved, exists=True, parent=parent)

    # 目标**不存在** ⇒ 本函数**不建目录**（mkdir 归写函数的 create 分支，见 D-回归检查）
    # 父目录逐级是否在根内 / 是否敏感，已在第 3 步（纯字符串）判过 ⇒ 这里只区分「在」与「不在」。
    if not os.path.isdir(str(parent)):
        # 🔴 父目录不存在**不是**拒绝理由：create=True 时要**创建**它（A28 腿①）。
        #    但父目录的**可写性**此刻无从判（它还不存在）⇒ 交由写函数在 mkdir 之后复判。
        return WriteTarget(
            raw=raw, resolved=resolved, exists=False, parent=parent, parent_exists=False
        )
    if not os.access(str(parent), os.W_OK):
        return WriteReject(
            raw=raw, layer="write_parent", reason=WRITE_PARENT_UNWRITABLE_TEMPLATE.format(path=raw)
        )
    return WriteTarget(raw=raw, resolved=resolved, exists=False, parent=parent, parent_exists=True)


#: 从读侧**转出**的冻结文案（写侧越界直接复用读侧那一套，不另造一份 —— 防两份文案漂移）。
#: ⚠️ 载体（``test_write_create_t3_5.py``）按 ``_write_paths.PATH_OUTSIDE_TEMPLATE`` 取它，
#:    故这里必须**转出**（否则 AttributeError ⇒ 腿自己崩，而不是判到行为）。
PATH_OUTSIDE_TEMPLATE = _paths.PATH_OUTSIDE_TEMPLATE
PATH_SENSITIVE_TEMPLATE = _paths.PATH_SENSITIVE_TEMPLATE

__all__ = [
    "PATH_OUTSIDE_TEMPLATE",
    "PATH_SENSITIVE_TEMPLATE",
    "WRITE_EXTRA_SEGMENTS",
    "WRITE_EXTRA_SUFFIXES",
    "WriteReject",
    "WriteTarget",
    "WriteVerdict",
    "resolve_write_target",
]
