"""``grep_files`` —— **只读**在工作区内按**固定字符串**搜索（专项工具升级-3 · 阶段 3 · `回归检查`）。

## 三条纪律（照 `read_document.py` 的先例）

1. `build_spec()` + **闭包 handler**；
2. 失败**一律** `ToolObservation(ok=False)` 中文可读，**绝不炸穿**（I5）；
3. 观察文本**硬上限**（`OBSERVATION_LIMIT`，超限截断 + 显式标记）。

## 🔴 **固定字符串匹配 —— 禁正则**（`K-回归检查`）

**不得**用 `re`：正则回溯会**挂住整轮**（灾难性回溯）。本工具只用 `str.find` / `in`。

## 零材料（`A15` / 红线 8）

**恒** `materials == []` —— **绝不**把文件内容当材料。

## 二进制与解码（`D-回归检查` / `D-回归检查`）

- 含 ``\x00`` ⇒ **二进制 ⇒ 跳过该文件**（不返回乱码命中）；
- 解码失败 ⇒ **跳过该文件**（不返回乱码命中）。

## 有界（`回归修复`）

候选文件数（`MAX_GREP_FILES`）与命中数（`max_hits`）**双上限**，超限**显式**标记同一句
「（已按上限截断）」；候选收集与逐文件扫描都**丢线程**执行（大工作区不再冻结事件循环）。

## 边界（`D-回归检查` / 红线 1 / 红线 2）

- **相对路径一律相对工作区根**（`resolve_workspace_root()`），**绝不**取进程 CWD；
- 越界 = 经 `_paths.classify(path, root)` 判 `Reject` ⇒ `ok=False`、**不夹紧**；
- ⚠️ **先取** `resolve_workspace_root()` **再** `classify`（`K-回归检查`）。

## 零写（`A18`）

本模块**只有读原语**（`os.walk` / `open(..., "rb")`）—— **无任何写**。
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
from app.agent.tools.read_file import ENCODINGS, PROBE_BYTES
from app.agent.types import ToolObservation

TOOL_NAME = "grep_files"

DESCRIPTION = (
    "在工作区内按固定字符串搜索文本文件（只读；路径一律相对工作区根）。"
    "不是正则；二进制与无法解码的文件会被跳过，命中条数有界"
)

#: 单次默认返回几条命中
DEFAULT_MAX_HITS = 100

#: 单次命中数天花板（超出**不**夹紧，按坏参数拒绝）
MAX_HITS = 500

#: 观察文本硬上限
OBSERVATION_LIMIT = 4000

#: 截断标记（**显式**，不得静默截断）
TRUNCATION_MARK = "（已截断，原 {chars} 字符）"

#: 未配置工作区根
NO_ROOT_MESSAGE = "未配置工作区根，无法搜索文件。请在设置页配置工作区根后重试。"

#: 单个文件的读取上限（字节）：超过即只读前 N 字节（**有界**，不整份进内存）
MAX_FILE_BYTES = 1_000_000

#: 单次搜索的候选文件数上限（照 `folder_scan.MAX_FOLDER_FILES = 200` 的「上限 + 显式标记」
#: 先例）：冷门 pattern 是全盘扫，**零命中也必然扫完**全部文件，没有它就是无界遍历
#: （`回归修复`）。超限与命中数超限合并成**同一句**显式标记，绝不静默截断。
MAX_GREP_FILES = 200


class GrepFilesArgs(BaseModel):
    """工具入参（同时是给模型的 JSON Schema 来源）。

    ⚠️ `max_hits` **故意不加** `ge` / `le`：越界要走 handler 产出中文失败观察。
    """

    pattern: str = Field(description="要搜索的固定字符串（不是正则；必填）")
    path: str | None = Field(
        default=None,
        description="相对工作区根的目录或文件；省略 = 整个工作区根",
    )
    max_hits: int = Field(
        default=DEFAULT_MAX_HITS,
        description=f"本次最多返回几条命中（默认 {DEFAULT_MAX_HITS}，不能超过 {MAX_HITS}）",
    )


Args = GrepFilesArgs


def _clip(text: str, limit: int = OBSERVATION_LIMIT) -> str:
    """观察文本有界截断 + **显式**标记（标记计入上限）。"""
    value = str(text or "")
    if len(value) <= limit:
        return value
    mark = TRUNCATION_MARK.format(chars=len(value))
    keep = max(0, limit - len(mark))
    return value[:keep].rstrip() + mark


def _decode_head(head: bytes) -> str | None:
    """探测并解码文件头；二进制（含 NUL）或四档全失败 ⇒ `None`（**跳过该文件**）。

    ⚠️ **禁正则**不适用于本函数，但**同顺序**来自 `read_file.ENCODINGS`（两处实现须可**独立失效** ——
    `M-回归检查`）。
    """
    if b"\x00" in head:
        return None
    for enc in ENCODINGS:
        try:
            return head.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def search_text(text: str, pattern: str, rel: str, *, limit: int) -> list[str]:
    """在**已解码文本**里按**固定字符串**找命中，返回 `路径:行号: 正文` 形态（有界）。"""
    hits: list[str] = []
    if not pattern:
        return hits
    for lineno, line in enumerate(text.splitlines(), start=1):
        if pattern in line:  # 🔴 **固定字符串**（禁正则 —— K-回归检查）
            hits.append(f"{rel}:{lineno}: {line.strip()}")
            if len(hits) >= limit:
                break
    return hits


def render_hits(entries: list[str], *, truncated: bool, total: int, pattern: str) -> str:
    """拼观察文本：**来源标注** + 检索式 + 命中清单 + （如有）截断标记。"""
    head = DISK_SOURCE_PREFIX + f"搜索「{pattern}」：命中 {total} 条"
    if truncated:
        head += "（已按上限截断）"
    return "\n".join([head, *entries])


def _iter_candidate_files(ws: Path, base: Path) -> list[Path]:
    """收集候选文件（目录不进结果；文件按 posix 相对路径排序，输出稳定）。"""
    if os.path.isfile(str(base)):
        return [base]
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(str(base)):
        dirnames.sort()
        for name in sorted(filenames):
            out.append(Path(dirpath) / name)
    return out


def _scan_files(
    files: list[Path], ws: Path, pattern: str, max_hits: int
) -> tuple[list[str], bool]:
    """逐文件「读头 → 解码 → 固定字符串匹配」（**纯同步**；调用方丢线程执行 —— `回归修复`）。

    **多探一条**（`回归修复`）：每个文件向 ``search_text`` 多要一条
    （``limit = 余量 + 1``），拿到超额的那条 ⇒ 真有超出配额的命中 ⇒ 置 ``truncated``
    并裁回 ``max_hits`` —— 配额在**最后一个扫描文件**内用完也照样置标记；
    「恰好 ``max_hits`` 条全量」不误报（探不到超额那条就不置位）。
    """
    hits: list[str] = []
    truncated = False
    for file_path in files:
        try:
            with open(file_path, "rb") as fh:
                head = fh.read(MAX_FILE_BYTES)
        except OSError:
            continue  # 读不了 ⇒ 跳过该文件（不炸穿）
        text = _decode_head(head)
        if text is None:
            continue  # 二进制 / 解码失败 ⇒ **跳过该文件**（不返回乱码命中）
        try:
            rel_name = file_path.relative_to(ws).as_posix()
        except ValueError:
            rel_name = str(file_path)
        hits.extend(search_text(text, pattern, rel_name, limit=max_hits - len(hits) + 1))
        if len(hits) > max_hits:
            truncated = True
            hits = hits[:max_hits]
            break  # 已确证超出配额 ⇒ 后续文件不必再扫（与既有「达配额即停」语义一致）
    return hits, truncated


def build_spec(*, root: Any = None) -> ToolSpec:
    """装配 `grep_files` 工具声明。``root`` 只用于测试注入（默认现取工作区根）。"""

    async def handler(args: GrepFilesArgs) -> ToolObservation:
        pattern = str(args.pattern or "")
        if not pattern:
            return arg_error(
                tool=TOOL_NAME,
                field="pattern",
                constraint="非空字符串",
                received=pattern,
                parameters=GrepFilesArgs,
            )
        max_hits = int(args.max_hits)
        if max_hits < 1:
            return arg_error(
                tool=TOOL_NAME,
                field="max_hits",
                constraint="不小于 1 的整数",
                received=max_hits,
                parameters=GrepFilesArgs,
            )
        if max_hits > MAX_HITS:
            return arg_error(
                tool=TOOL_NAME,
                field="max_hits",
                constraint=f"不大于 {MAX_HITS} 的整数",
                received=max_hits,
                parameters=GrepFilesArgs,
            )

        # 🔴 先取根，再 classify（K-回归检查）
        ws = root if root is not None else resolve_workspace_root()
        if ws is None:
            return ToolObservation(text=NO_ROOT_MESSAGE, ok=False)
        ws = Path(ws)

        rel = (args.path or "").strip()
        target_rel = rel or ""
        if target_rel:
            verdict = classify(target_rel, ws)
            if isinstance(verdict, Reject):
                return ToolObservation(text=verdict.reason, ok=False)
            candidate = Path(target_rel)
            base = candidate if candidate.is_absolute() else ws / candidate
        else:
            base = ws
        if not os.path.exists(str(base)):
            return ToolObservation(text=f"无法搜索：路径 {target_rel or '.'} 不存在。", ok=False)

        try:
            # 回归修复：候选收集（os.walk 全树）与逐文件扫描都是无界同步 IO ⇒ 丢线程
            # （两段各自一次线程跳，不在循环上保留任何重段）。
            files = await asyncio.to_thread(_iter_candidate_files, ws, base)
        except OSError as exc:
            return ToolObservation(text=f"无法搜索：{exc.__class__.__name__}。", ok=False)

        # 候选文件数上限：先裁再扫；裁没裁要记住 —— 与命中数超限合并成同一句显式标记
        files_truncated = len(files) > MAX_GREP_FILES
        if files_truncated:
            files = files[:MAX_GREP_FILES]

        hits, truncated = await asyncio.to_thread(_scan_files, files, ws, pattern, max_hits)
        truncated = truncated or files_truncated

        total = len(hits)
        shown = hits[:max_hits]

        if not shown:
            # A15 / 红线 8：**恒零材料**
            return ToolObservation(
                text=DISK_SOURCE_PREFIX + f"搜索「{pattern}」：没有命中。",
                ok=True,
                materials=[],
            )
        text = render_hits(shown, truncated=truncated, total=total, pattern=pattern)
        return ToolObservation(text=_clip(text), ok=True, materials=[])

    return ToolSpec(
        name=TOOL_NAME,
        description=DESCRIPTION,
        parameters_schema=GrepFilesArgs.model_json_schema(),
        handler=handler,
        parameters=GrepFilesArgs,
        needs_confirm=False,
    )


__all__ = [
    "Args",
    "DEFAULT_MAX_HITS",
    "DESCRIPTION",
    "MAX_GREP_FILES",
    "MAX_HITS",
    "OBSERVATION_LIMIT",
    "TOOL_NAME",
    "GrepFilesArgs",
    "build_spec",
    "render_hits",
    "search_text",
]
