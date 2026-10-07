"""``list_files`` —— **只读**列出工作区内的文件（专项工具升级-3 · 阶段 3 · `回归检查`）。

## 三条纪律（照 `read_document.py` 的先例）

1. `build_spec()` + **闭包 handler**；
2. 失败**一律** `ToolObservation(ok=False)` 中文可读，**绝不炸穿**（I5）；
3. 观察文本**硬上限**（`OBSERVATION_LIMIT`，超限截断 + 显式标记）。

## 零材料（`A15` / 红线 8）

本工具**恒** `materials == []` —— **绝不**把文件内容当材料（那会造出带 `[n]` 的假引用）。

## 边界（`D-回归检查` / 红线 1 / 红线 2）

- **相对路径一律相对工作区根**（`resolve_workspace_root()`），**绝不**取进程 CWD；
- 越界 = 经 `_paths.classify(path, root)` 判 `Reject` ⇒ `ok=False` + 中文可读、**不夹紧**；
- ⚠️ **先取** `resolve_workspace_root()` **再** `classify`（`K-回归检查`：`classify(path, None)` 会走「未配置」分支 ⇒ 已配置时误拒）；
- `glob` = **`fnmatch` 风格**（`*` **不跨 `/`**、`**` 才跨），**禁正则**（`D-回归检查`）。

## 零写（`A18`）

本模块**只有读原语**（`os.walk` / `os.path.isdir` / `os.path.exists`）—— **无任何写**。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.agent.registry import ToolSpec
from app.agent.tools._errors import arg_error
from app.agent.tools._paths import Reject, classify, resolve_workspace_root
from app.agent.tools._source import DISK_SOURCE_PREFIX
from app.agent.types import ToolObservation

TOOL_NAME = "list_files"

DESCRIPTION = (
    "列出工作区根内的文件（只读；路径一律相对工作区根）。"
    "可按 glob 过滤（* 不跨目录、** 才跨），结果有界、超限时显式标记"
)

#: 单次默认列几个（照 `read_document.DEFAULT_MAX_CHUNKS` 的「默认 + 天花板」先例）
DEFAULT_MAX_ENTRIES = 200

#: 单次条数天花板（超出**不**夹紧，按坏参数拒绝 —— 「不得吞成不限」）
MAX_ENTRIES = 500

#: 观察文本硬上限（超限截断 + 显式标记）
OBSERVATION_LIMIT = 4000

#: 截断标记（与 `read_document.TRUNCATION_MARK` 同族口径；**显式**，不得静默截断）
TRUNCATION_MARK = "（已截断，原 {chars} 字符）"

#: 未配置工作区根时的失败文案
NO_ROOT_MESSAGE = "未配置工作区根，无法列出文件。请在设置页配置工作区根后重试。"

#: 根不可用（配到了普通文件）时的失败文案
ROOT_UNUSABLE_MESSAGE = "工作区根不可用（不是目录），无法列出文件。"

ROOT_LABEL = "."


class ListFilesArgs(BaseModel):
    """工具入参（同时是给模型的 JSON Schema 来源）。

    ⚠️ `max_entries` **故意不加** `ge` / `le`：越界要走 **handler** 产出中文失败观察，
    而不是被 pydantic 在装配层用英文校验错误拒掉（照 `ReadDocumentArgs` 的同款理由）。
    """

    path: str | None = Field(
        default=None,
        description="相对工作区根的路径（目录或文件）；省略 = 工作区根本身",
    )
    glob: str | None = Field(
        default=None,
        description="可选的文件名过滤（fnmatch 风格：* 不跨目录、** 才跨；不是正则）",
    )
    max_entries: int = Field(
        default=DEFAULT_MAX_ENTRIES,
        description=f"本次最多列几条（默认 {DEFAULT_MAX_ENTRIES}，不能超过 {MAX_ENTRIES}）",
    )


Args = ListFilesArgs


def _clip(text: str, limit: int = OBSERVATION_LIMIT) -> str:
    """观察文本有界截断 + **显式**标记（标记计入上限）。"""
    value = str(text or "")
    if len(value) <= limit:
        return value
    mark = TRUNCATION_MARK.format(chars=len(value))
    keep = max(0, limit - len(mark))
    return value[:keep].rstrip() + mark


def _seg_match(pattern: str, name: str) -> bool:
    """**单段**通配匹配：`*` 匹配任意字符（单段内不涉及 `/`）；**禁正则**（`D-回归检查`）。

    实现 = 经典两指针 + 星号回溯（`pi`/`ni`/`star_pi`/`star_ni`），
    **刻意不用 `re`** —— 避免「正则写错 ⇒ 回溯挂住整轮」这一类风险（与 `grep_files` 禁正则同源）。
    """
    pi = ni = 0
    star_pi = star_ni = -1
    plen, nlen = len(pattern), len(name)
    while ni < nlen:
        if pi < plen and pattern[pi] == name[ni]:
            pi += 1
            ni += 1
        elif pi < plen and pattern[pi] == "*":
            star_pi = pi
            star_ni = ni
            pi += 1
        elif star_pi != -1:
            pi = star_pi + 1
            star_ni += 1
            ni = star_ni
        else:
            return False
    while pi < plen and pattern[pi] == "*":
        pi += 1
    return pi == plen


def _matches_glob(pattern: str, rel_posix: str) -> bool:
    """`glob` 过滤（**`fnmatch` 风格、非正则**）：`*` **不跨 `/`**、`**` **才跨**。

    - 无 `**` ⇒ 按 `/` 切段，**段数必须相等**，逐段 `_seg_match`（⇒ `*` 不跨目录）；
    - 有 `**` ⇒ 「前缀段逐段匹配」+「后缀在剩余段里找连续窗口」（⇒ `**` 跨目录）。
    """
    if not pattern:
        return True
    if "**" in pattern:
        head, _, tail = pattern.partition("**")
        head_segs = [s for s in head.rstrip("/").split("/") if s]
        parts = rel_posix.split("/")
        if len(parts) < len(head_segs):
            return False
        for seg, pat in zip(parts, head_segs):
            if not _seg_match(pat, seg):
                return False
        rest = parts[len(head_segs):]
        tail_segs = [s for s in tail.lstrip("/").split("/") if s]
        if not tail_segs:
            return True
        for idx in range(len(rest)):
            window = rest[idx: idx + len(tail_segs)]
            if len(window) == len(tail_segs) and all(
                _seg_match(pat, seg) for pat, seg in zip(tail_segs, window)
            ):
                return True
        return False
    pat_segs = pattern.split("/")
    parts = rel_posix.split("/")
    if len(pat_segs) != len(parts):
        return False
    return all(_seg_match(pat, seg) for pat, seg in zip(pat_segs, parts))


def render_entries(base_rel: str, entries: list[str], *, truncated: bool, total: int) -> str:
    """拼观察文本：**来源标注（`A13f`/`A19`）** + 根 + 清单 + （如有）截断标记。"""
    shown_dir = base_rel or ROOT_LABEL
    head = DISK_SOURCE_PREFIX + f"工作区文件（根：{shown_dir}）：共 {total} 条"
    if truncated:
        head += "（已按上限截断）"
    return "\n".join([head, *(f"- {item}" for item in entries)])


def _resolve_target(ws: Path, rel: str) -> Path | None:
    """把相对路径解析成绝对路径（**相对工作区根**，绝不取 CWD —— 红线 1）。"""
    if rel in (ROOT_LABEL, ""):
        return ws
    candidate = Path(rel)
    full = candidate if candidate.is_absolute() else ws / candidate
    return full if os.path.exists(str(full)) else None


def _walk(ws: Path, base: Path) -> list[str]:
    """有界遍历：**目录不进清单**（`D-回归检查` 写死），只收文件；key = 相对根的 posix 形态。

    ⚠️ 只在内存里收集，条数上限由调用方在拿齐后裁 —— 遍历本身**不提前剪枝**，
    以免「先剪枝再过滤 glob」造成**漏项**（那会把 `max_entries` 的语义变成「前 N 个目录项」）。
    """
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(str(base)):
        dirnames.sort()
        for name in sorted(filenames):
            full = Path(dirpath) / name
            try:
                out.append(full.relative_to(ws).as_posix())
            except ValueError:  # 不该发生（walk 自 base 起）；防御性跳过
                continue
    return sorted(out)


def build_spec(*, root: Any = None) -> ToolSpec:
    """装配 `list_files` 工具声明。

    ``root`` 只用于测试注入（**默认 None ⇒ 每次调用现取** `resolve_workspace_root()`）——
    「同进程跃迁」（`A5b` 第 5 腿）与「根未配置」（`A5a`）两条判据都依赖「**现取**」这一形状。
    """

    async def handler(args: ListFilesArgs) -> ToolObservation:
        max_entries = int(args.max_entries)
        if max_entries < 1:
            return arg_error(
                tool=TOOL_NAME,
                field="max_entries",
                constraint="不小于 1 的整数",
                received=max_entries,
                parameters=ListFilesArgs,
            )
        if max_entries > MAX_ENTRIES:
            return arg_error(
                tool=TOOL_NAME,
                field="max_entries",
                constraint=f"不大于 {MAX_ENTRIES} 的整数",
                received=max_entries,
                parameters=ListFilesArgs,
            )

        # 🔴 先取根，再 classify（K-回归检查）；根未配置 ⇒ 门已关，这里兜底给可读失败
        ws = root if root is not None else resolve_workspace_root()
        if ws is None:
            return ToolObservation(text=NO_ROOT_MESSAGE, ok=False)
        ws = Path(ws)

        rel = (args.path or "").strip() or ROOT_LABEL
        # ⚠️ **省略 path（= 工作区根本身）时不得走 classify**：classify("") 会命中
        # `_paths` 的「路径为空」词法分支 ⇒ **把「列根目录」这个合法调用误拒**
        # （实测读数：路径越界：路径为空，已拒绝。）。根自身的边界由「它就是根」这一事实保证，
        # 无需再过一遍越界判定；**只有当用户真的给了 path 时**才判。
        if rel != ROOT_LABEL:
            verdict = classify(rel, ws)
            if isinstance(verdict, Reject):
                return ToolObservation(text=verdict.reason, ok=False)

        target = _resolve_target(ws, rel)
        if target is None:
            return ToolObservation(text=f"无法列出文件：路径 {rel} 不存在。", ok=False)
        if not os.path.isdir(str(target)):
            return ToolObservation(text=ROOT_UNUSABLE_MESSAGE, ok=False)

        try:
            # 回归修复：``_walk`` 是无界的同步 ``os.walk``（几万文件的根一次扫完），
            # 纯文件系统 IO、不碰任何事件循环态 ⇒ 整体丢线程，大工作区不再冻结整个后端。
            found = await asyncio.to_thread(_walk, ws, target)
        except OSError as exc:  # 读目录失败 ⇒ 可读中文失败，不炸穿（I5）
            return ToolObservation(text=f"无法列出文件：{exc.__class__.__name__}（{rel}）。", ok=False)

        pattern = (args.glob or "").strip()
        if pattern:
            found = [item for item in found if _matches_glob(pattern, item)]
        total = len(found)
        truncated = total > max_entries
        shown = found[:max_entries]
        base_rel = "" if rel == ROOT_LABEL else rel

        if shown:
            text = render_entries(base_rel, shown, truncated=truncated, total=total)
        else:
            text = DISK_SOURCE_PREFIX + "工作区文件（根：" + (base_rel or ROOT_LABEL) + "）：没有匹配的文件"
        # A15 / 红线 8：**恒零材料**
        return ToolObservation(text=_clip(text), ok=True, materials=[])

    return ToolSpec(
        name=TOOL_NAME,
        description=DESCRIPTION,
        parameters_schema=ListFilesArgs.model_json_schema(),
        handler=handler,
        parameters=ListFilesArgs,
        needs_confirm=False,
    )


__all__ = [
    "Args",
    "DESCRIPTION",
    "DEFAULT_MAX_ENTRIES",
    "MAX_ENTRIES",
    "OBSERVATION_LIMIT",
    "TOOL_NAME",
    "ListFilesArgs",
    "build_spec",
    "render_entries",
]
