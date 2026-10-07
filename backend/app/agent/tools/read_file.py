"""``read_file`` —— **只读**按行区间读工作区内的文本文件（专项工具升级-3 · 阶段 3 · `回归检查`）。

## 三条纪律（照 `read_document.py` 的先例）

1. `build_spec()` + **闭包 handler**；
2. 失败**一律** `ToolObservation(ok=False)` 中文可读，**绝不炸穿**（I5）；
3. 观察文本**硬上限**（`OBSERVATION_LIMIT`，超限截断 + 显式标记）。

## 零材料（`A15` / 红线 8）

**恒** `materials == []` —— **绝不**把文件内容当材料（那会造出带 `[n]` 的假引用）。

## 按行区间读（`K-回归检查`）

**不整份读进内存再截**（GB 级文件会 OOM）—— 逐行流式读到 `start_line + max_lines` 即停。

## 编码探测（`D-回归检查` 取**甲**）

**不复用** `app.pipeline.parsing.detect_encoding`（那会新增跨层 import 边，撞 `A17`）⇒
自带极简探测，同顺序 `utf-8 → utf-8-sig → gb18030 → latin-1`。
⚠️ **代价如实登记**：编码探测**有两份实现** ⇒ 变异 `M-回归检查` 须证明两处可**独立失效**
（不得互相顶替）。

含 ``\x00`` ⇒ 按**二进制** ⇒ `ok=False` + 中文提示（**不返回乱码正文**）。

## 边界（`D-回归检查` / 红线 1 / 红线 2）

- **相对路径一律相对工作区根**（`resolve_workspace_root()`），**绝不**取进程 CWD；
- 越界 = 经 `_paths.classify(path, root)` 判 `Reject` ⇒ `ok=False` + 中文可读、**不夹紧**；
- ⚠️ **先取** `resolve_workspace_root()` **再** `classify`（`K-回归检查`）；
- **行号 1 起**；超上限**截断 + 显式标记**。

## 零写（`A18`）

本模块**只有读原语**（`open(..., "rb")` / `os.path.isfile`）—— **无任何写**。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.agent.registry import ToolSpec
from app.agent.tools._errors import arg_error
from app.agent.tools._paths import Reject, classify, resolve_workspace_root
from app.agent.tools._source import DISK_SOURCE_PREFIX
from app.agent.types import ToolObservation

TOOL_NAME = "read_file"

DESCRIPTION = (
    "按行区间读取工作区内的文本文件（只读；路径一律相对工作区根）。"
    "行号从 1 起，单次有界；二进制文件与越界路径会被拒绝"
)

#: 单次默认读几行
DEFAULT_MAX_LINES = 400

#: 单次行数天花板（超出**不**夹紧，按坏参数拒绝）
MAX_LINES = 2000

#: 观察文本硬上限
OBSERVATION_LIMIT = 4000

#: 截断标记（**显式**，不得静默截断）
TRUNCATION_MARK = "（已截断，原 {chars} 字符）"

#: 未配置工作区根
NO_ROOT_MESSAGE = "未配置工作区根，无法读取文件。请在设置页配置工作区根后重试。"

#: 二进制拒读文案（含 ``\x00``）
BINARY_MESSAGE = "无法读取文件：{path} 看起来是二进制文件（含 NUL 字节），已拒绝返回内容。"

#: 编码探测失败（四档全挂）
DECODE_FAILED_MESSAGE = "无法读取文件：{path} 的文本编码无法识别（已试 utf-8 / utf-8-sig / gb18030 / latin-1）。"

#: 探测顺序（**唯一一份**；与 `parsing.detect_encoding` **同顺序但独立实现**）
ENCODINGS: tuple[str, ...] = ("utf-8", "utf-8-sig", "gb18030", "latin-1")

#: 探测取样字节数（只读文件头，避免把大文件整份读进内存）
PROBE_BYTES = 65536


class ReadFileArgs(BaseModel):
    """工具入参（同时是给模型的 JSON Schema 来源）。

    ⚠️ `start_line` / `max_lines` **故意不加** `ge` / `le`：越界要走 handler 产出中文失败观察。
    """

    path: str = Field(description="相对工作区根的文件路径（必填）")
    start_line: int = Field(default=1, description="起始行号（从 1 起）")
    max_lines: int = Field(
        default=DEFAULT_MAX_LINES,
        description=f"本次最多读几行（默认 {DEFAULT_MAX_LINES}，不能超过 {MAX_LINES}）",
    )


Args = ReadFileArgs


def _clip(text: str, limit: int = OBSERVATION_LIMIT) -> str:
    """观察文本有界截断 + **显式**标记（标记计入上限）。"""
    value = str(text or "")
    if len(value) <= limit:
        return value
    mark = TRUNCATION_MARK.format(chars=len(value))
    keep = max(0, limit - len(mark))
    return value[:keep].rstrip() + mark


def probe_encoding(head: bytes) -> str | None:
    """极简编码探测：同顺序 `utf-8 → utf-8-sig → gb18030 → latin-1`。

    ⚠️ **本函数是 `D-回归检查`（甲）的代价** —— 与 `app.pipeline.parsing.detect_encoding`
    **同顺序、两份实现**（跨层 import 会撞 `A17` 白名单）。两处须能**独立失效**（`M-回归检查`）。

    `latin-1` 恒可解码 ⇒ 返回 `None` 只可能发生在空输入之外的理论情形；
    保留 `None` 分支是为了「不静默用错编码」。
    """
    for enc in ENCODINGS:
        try:
            head.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        return enc
    return None


def _is_binary(head: bytes) -> bool:
    """含 NUL 字节 ⇒ 二进制（**不返回乱码正文**）。"""
    return b"\x00" in head


def read_line_range(path: Path, start_line: int, max_lines: int) -> tuple[list[str], int, bool]:
    """**流式**按行区间读：返回 ``(行文本, 已读到的总行数, 是否还有更多)``。

    ⚠️ **不整份读进内存再截**（`K-回归检查`）：逐行走到 `start_line - 1 + max_lines` 就停，
    之后只再探一行用于判「是否还有更多」。
    """
    end_line = start_line - 1 + max_lines
    collected: list[str] = []
    total = 0
    more = False
    with open(path, "rb") as fh:
        head = fh.read(PROBE_BYTES)
        if _is_binary(head):
            raise BinaryFileError(str(path))
        enc = probe_encoding(head)
        if enc is None:
            raise UndecodableError(str(path))
        fh.seek(0)
        for raw in fh:
            total += 1
            if total < start_line:
                continue
            if total > end_line:
                more = True
                break
            collected.append(raw.decode(enc, errors="replace").rstrip("\r\n"))
    return collected, total, more


class BinaryFileError(ValueError):
    """文件含 NUL ⇒ 按二进制处理。"""


class UndecodableError(ValueError):
    """四档编码全失败。"""


def render_body(path_rel: str, start_line: int, lines: list[str], *, more: bool) -> str:
    """拼观察文本：**来源标注** + 行号前缀 + （如有）还有更多提示。"""
    parts = [DISK_SOURCE_PREFIX + f"文件 {path_rel}（第 {start_line} 行起，共 {len(lines)} 行）"]
    for offset, text in enumerate(lines):
        parts.append(f"{start_line + offset}\t{text}")
    if more:
        parts.append("（后续还有更多行，未在本段显示）")
    return "\n".join(parts)


def build_spec(*, root: Any = None) -> ToolSpec:
    """装配 `read_file` 工具声明。``root`` 只用于测试注入（默认现取工作区根）。"""

    async def handler(args: ReadFileArgs) -> ToolObservation:
        start_line = int(args.start_line)
        max_lines = int(args.max_lines)
        if start_line < 1:
            return arg_error(
                tool=TOOL_NAME,
                field="start_line",
                constraint="不小于 1 的整数",
                received=start_line,
                parameters=ReadFileArgs,
            )
        if max_lines < 1:
            return arg_error(
                tool=TOOL_NAME,
                field="max_lines",
                constraint="不小于 1 的整数",
                received=max_lines,
                parameters=ReadFileArgs,
            )
        if max_lines > MAX_LINES:
            return arg_error(
                tool=TOOL_NAME,
                field="max_lines",
                constraint=f"不大于 {MAX_LINES} 的整数",
                received=max_lines,
                parameters=ReadFileArgs,
            )

        rel = (args.path or "").strip()
        if not rel:
            return arg_error(
                tool=TOOL_NAME,
                field="path",
                constraint="非空字符串",
                received=rel,
                parameters=ReadFileArgs,
            )

        # 🔴 先取根，再 classify（K-回归检查）
        ws = root if root is not None else resolve_workspace_root()
        if ws is None:
            return ToolObservation(text=NO_ROOT_MESSAGE, ok=False)
        ws = Path(ws)

        verdict = classify(rel, ws)
        if isinstance(verdict, Reject):
            return ToolObservation(text=verdict.reason, ok=False)

        candidate = Path(rel)
        full = candidate if candidate.is_absolute() else ws / candidate
        if not os.path.isfile(str(full)):
            return ToolObservation(text=f"无法读取文件：{rel} 不存在或不是文件。", ok=False)

        try:
            lines, _total, more = read_line_range(full, start_line, max_lines)
        except BinaryFileError:
            return ToolObservation(text=BINARY_MESSAGE.format(path=rel), ok=False)
        except UndecodableError:
            return ToolObservation(text=DECODE_FAILED_MESSAGE.format(path=rel), ok=False)
        except OSError as exc:
            return ToolObservation(text=f"无法读取文件：{exc.__class__.__name__}（{rel}）。", ok=False)

        if not lines:
            return ToolObservation(
                text=DISK_SOURCE_PREFIX + f"文件 {rel}：第 {start_line} 行起没有内容。",
                ok=True,
                materials=[],
            )
        # A15 / 红线 8：**恒零材料**
        return ToolObservation(
            text=_clip(render_body(rel, start_line, lines, more=more)),
            ok=True,
            materials=[],
        )

    return ToolSpec(
        name=TOOL_NAME,
        description=DESCRIPTION,
        parameters_schema=ReadFileArgs.model_json_schema(),
        handler=handler,
        parameters=ReadFileArgs,
        needs_confirm=False,
    )


__all__ = [
    "Args",
    "BINARY_MESSAGE",
    "DEFAULT_MAX_LINES",
    "DESCRIPTION",
    "ENCODINGS",
    "MAX_LINES",
    "OBSERVATION_LIMIT",
    "TOOL_NAME",
    "ReadFileArgs",
    "build_spec",
    "probe_encoding",
    "read_line_range",
    "render_body",
]
