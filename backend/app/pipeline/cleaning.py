"""数据清洗流水线（算法优化方案 M3 / 扩展·清洗数据）。

设计原则（对齐扩展.md 的「拿不准时不要偷偷删掉原文」）：

- **双份留存**：``raw_content`` 存原始解析文本，清洗只作用在入库文本上，原文永远可查看；
- **dry-run 与正式入库同一条函数路径**（``clean``），不做两套实现，保证 回归检查 的逐字符一致；
- **判不准只标记不删**：乱码段落写进 ``clean_report["flagged"]``，内容仍保留；
- **每步可关、逐步可核对**：``CLEAN_ENABLE`` 总开关 + 5 个分步开关，``clean_report`` 记录每步删了多少行/字；
- **幂等**：同一文本连续清洗多次结果一致（回归检查）。

六个步骤（与方案 M3.1 表格一一对应）：

===========  ==========================================================  ========
步骤          做法                                                        默认
===========  ==========================================================  ========
S1 编码统一   字节解码（utf-8 / utf-8-sig / gb18030 / latin-1）           强制
S2 空白归一   保护 Markdown 结构，其余行压缩空白                          强制
S3 页眉页脚   跨页重复行检测（出现率 ≥ 阈值且行长 ≤ 上限 → 删）            开
S4 重复段落   默认完整保留正文重复                                       开
S5 乱码控制符 剔除正文控制符；乱码只标记；仅折叠整行标点分隔线              开
S6 表格降级   先保留 tab / 双空格列分隔，再统一成 ``|`` 分隔              开
===========  ==========================================================  ========

编码统一这一步的实现在 ``parsing._decode``（解析层职责），本模块只负责文本层面的 S2~S6。
"""

import hashlib
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Iterable

from app.core.config import settings

# --- S5 用到的正则 ---
# C0 / C1 控制符，保留 \n 与 \t（表格降级要用到 tab 判断）
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# 连续替换符（乱码）：>3 视为乱码段落
_REPLACEMENT_RUN = re.compile(r"\ufffd{4,}")
# 行内空白（单个空格不算——表格降级后的 " | " 分隔符必须活下来）
_INLINE_WS = re.compile(r"[ \t\u3000]{2,}")
# 连续空行（2 行以上压成 1 行空行）
_BLANK_RUN = re.compile(r"\n{2,}")
# 表格行判定：2+ 连续空白，或单个 tab 也算列分隔
_TABLE_ROW = re.compile(r"[ \u3000]{2,}|\t")
# 表格列切分：2+ 连续空白，或任意 tab
_TABLE_SPLIT = re.compile(r"[ \u3000]{2,}|\t+")
RULE_VERSION = "opt3-tables-v1"
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def protected_line_indices(lines: list[str], *, only_code: bool = False) -> set[int]:
    """先识别围栏、缩进代码，再保护 Markdown 引用和表格的空白。"""
    protected: set[int] = set()
    fence = ""
    indented = False
    for index, line in enumerate(lines):
        marker = _FENCE.match(line)
        if fence:
            protected.add(index)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = ""
            continue
        if marker:
            protected.add(index)
            fence = marker[1]
            continue
        if line.startswith(("    ", "\t")):
            protected.add(index)
            indented = True
            continue
        if not line.strip() and indented:
            protected.add(index)
            continue
        indented = False
        if not only_code and ("|" in line or re.match(r"^ {0,3}>", line)):
            protected.add(index)
    return protected

# 页眉页脚检测只看每页首/末这么多行
_HEAD_TAIL_LINES = 3
# 表格行至少要有 2 列才算
_TABLE_MIN_COLUMNS = 2
# 表格降级时列分隔符
_TABLE_SEP = " | "

@dataclass
class StepStat:
    """单步清洗统计：删了多少行、多少字。"""

    name: str
    before_lines: int = 0
    after_lines: int = 0
    before_chars: int = 0
    after_chars: int = 0
    detail: dict = field(default_factory=dict)

    @property
    def lines_removed(self) -> int:
        return max(0, self.before_lines - self.after_lines)

    @property
    def chars_removed(self) -> int:
        return max(0, self.before_chars - self.after_chars)

    def as_dict(self) -> dict:
        out = {
            "step": self.name,
            "lines_removed": self.lines_removed,
            "chars_removed": self.chars_removed,
        }
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass
class CleanResult:
    """清洗结果：清洗后文本 + 可核对的报告 + 原始文本。"""

    text: str
    raw: str
    steps: list[StepStat] = field(default_factory=list)
    flagged: list[dict] = field(default_factory=list)
    enabled: bool = True
    source_map: list[dict] = field(default_factory=list)
    page_spans: list[dict] = field(default_factory=list)
    removed_ranges: list[dict] = field(default_factory=list)
    chunk_provenance: list[dict] = field(default_factory=list)

    @property
    def removed_ratio(self) -> float:
        if not self.raw:
            return 0.0
        return max(0.0, 1.0 - len(self.text) / len(self.raw))

    def report(self) -> dict:
        """落库用的 clean_report。"""
        return {
            "enabled": self.enabled,
            "rule_version": RULE_VERSION,
            "rule_fingerprint": rule_fingerprint(),
            "raw_chars": len(self.raw),
            "clean_chars": len(self.text),
            "raw_lines": self.raw.count("\n") + 1 if self.raw else 0,
            "clean_lines": self.text.count("\n") + 1 if self.text else 0,
            "steps": [step.as_dict() for step in self.steps],
            "flagged": self.flagged,
            "flagged_count": len(self.flagged),
            "source_coordinate": "raw_content_unicode",
            "canonical_coordinate": "clean_text_unicode",
            "source_map": self.source_map,
            "page_spans": self.page_spans,
            "removed_ranges": self.removed_ranges,
            "chunk_provenance": self.chunk_provenance,
        }


# --------------------------------------------------------------------------
# 规则指纹：清洗规则参数一旦变化，历史向量就不再对新文本有效（回归检查）
# --------------------------------------------------------------------------

def clean_rule_params() -> dict:
    """参与「清洗规则已变更」判定的参数集合。"""
    return {
        "version": RULE_VERSION,
        # 总开关也是规则的一部分：关掉清洗入库的文档，重新打开时必须被判为「建议重建」
        "enabled": bool(settings.CLEAN_ENABLE),
        "min_repeat_ratio": float(settings.CLEAN_MIN_REPEAT_RATIO),
        "max_header_len": int(settings.CLEAN_MAX_HEADER_LEN),
        "max_repeat_char": int(settings.CLEAN_MAX_REPEAT_CHAR),
        "header_footer": bool(settings.CLEAN_HEADER_FOOTER),
        "duplicate_para": bool(settings.CLEAN_DUPLICATE_PARA),
        "control_char": bool(settings.CLEAN_CONTROL_CHAR),
        "table_downgrade": bool(settings.CLEAN_TABLE_DOWNGRADE),
    }


def rule_fingerprint() -> str:
    """规则参数的指纹：参数一变指纹就变，用来提示「建议重建索引」。"""
    payload = "|".join(f"{key}={value}" for key, value in sorted(clean_rule_params().items()))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# 各步骤实现：全部是纯函数，可单独单测
# --------------------------------------------------------------------------

def _append_span(out: list[dict], start: int, end: int, raw_start: int, raw_end: int) -> None:
    if start >= end:
        return
    span = {"clean_start": start, "clean_end": end, "raw_start": raw_start, "raw_end": raw_end}
    if out:
        previous = out[-1]
        if (
            previous["clean_end"] == start and previous["raw_end"] == raw_start
            and previous["clean_end"] - previous["clean_start"] == previous["raw_end"] - previous["raw_start"]
            and end - start == raw_end - raw_start
        ):
            previous["clean_end"], previous["raw_end"] = end, raw_end
            return
    out.append(span)


def source_slice(source_map: list[dict], start: int, end: int, *, ends: list[int] | None = None) -> list[dict]:
    """截取已知字符映射；长度改变的空白替换保留完整原始范围，不按比例猜位置。"""
    out: list[dict] = []
    ends = ends if ends is not None else [span["clean_end"] for span in source_map]
    for index in range(bisect_right(ends, start), len(source_map)):
        span = source_map[index]
        if span["clean_start"] >= end:
            break
        a, b = max(start, span["clean_start"]), min(end, span["clean_end"])
        if a >= b:
            continue
        raw_a, raw_b = span["raw_start"], span["raw_end"]
        if span["clean_end"] - span["clean_start"] == raw_b - raw_a:
            raw_a += a - span["clean_start"]
            raw_b = raw_a + b - a
        _append_span(out, a, b, raw_a, raw_b)
    return out


def _apply_edits(
    text: str, source_map: list[dict], edits: list[tuple[int, int, str]],
    *, step: str = "", removed: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    """把明确位置的修改串接到原文映射，重复文本不会参与匹配。"""
    parts: list[str] = []
    mapped: list[dict] = []
    position = output = 0
    ends = [span["clean_end"] for span in source_map]
    for start, end, replacement in sorted(edits):
        parts.append(text[position:start])
        for span in source_slice(source_map, position, start, ends=ends):
            _append_span(mapped, output + span["clean_start"] - position,
                         output + span["clean_end"] - position, span["raw_start"], span["raw_end"])
        output += start - position
        previous = source_slice(source_map, start, end, ends=ends)
        parts.append(replacement)
        if replacement and previous:
            _append_span(mapped, output, output + len(replacement),
                         previous[0]["raw_start"], previous[-1]["raw_end"])
        elif not replacement and removed is not None:
            removed.extend({"step": step, "raw_start": span["raw_start"], "raw_end": span["raw_end"]}
                           for span in previous)
        output += len(replacement)
        position = end
    parts.append(text[position:])
    for span in source_slice(source_map, position, len(text), ends=ends):
        _append_span(mapped, output + span["clean_start"] - position,
                     output + span["clean_end"] - position, span["raw_start"], span["raw_end"])
    return "".join(parts), mapped


def normalize_with_map(
    text: str, source_map: list[dict] | None = None, removed: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    """S2 与位置映射共用明确的空白修改；结构区原样保留。"""
    mapping = source_map if source_map is not None else (
        [{"clean_start": 0, "clean_end": len(text), "raw_start": 0, "raw_end": len(text)}] if text else []
    )
    current, mapping = _apply_edits(
        text, mapping, [(match.start(), match.end(), "\n") for match in re.finditer(r"\r\n|\r", text)],
        step="normalize", removed=removed,
    )
    lines = current.split("\n")
    protected = protected_line_indices(lines)
    edits: list[tuple[int, int, str]] = []
    kept: list[str] = []
    position = 0
    for index, line in enumerate(lines):
        end = position + len(line)
        if index in protected:
            kept.append(line)
        elif not line.strip():
            if kept and kept[-1] != "":
                kept.append("")
                if line:
                    edits.append((position, end, ""))
            else:
                edits.append((position, min(end + 1, len(current)), ""))
        else:
            left = len(line) - len(line.lstrip())
            right = len(line.rstrip())
            hard_break = line.endswith("  ")
            if left:
                edits.append((position, position + left, ""))
            for match in _INLINE_WS.finditer(line, left, right):
                edits.append((position + match.start(), position + match.end(), " "))
            if right < len(line):
                edits.append((position + right, end - 2 if hard_break else end, ""))
            kept.append(line[left:right] + ("  " if hard_break else ""))
        position = end + 1
    current, mapping = _apply_edits(current, mapping, edits, step="normalize", removed=removed)
    left, right = len(current) - len(current.lstrip("\n")), len(current.rstrip("\n"))
    trim = [(0, left, "")] if left else []
    if right < len(current) and right >= left:
        trim.append((right, len(current), ""))
    return _apply_edits(current, mapping, trim, step="normalize", removed=removed)


def normalize_blank_lines(text: str) -> str:
    """结构保护的规范文本，与来源映射共用同一个实现。"""
    return normalize_with_map(text)[0]


def page_source_spans(text: str, pages: Iterable[str] | None) -> list[dict]:
    """只接受解析器直传范围，或可以逐字核对的整页拼接，不搜索正文探针。"""
    if pages is None:
        return []
    explicit = getattr(pages, "source_spans", None)
    if explicit is not None:
        return [dict(span) for span in explicit]
    items = list(pages)
    present = [(page, value) for page, value in enumerate(items, 1) if value]
    for separator in ("\n\n", "\n"):
        if separator.join(value for _, value in present) != text:
            continue
        spans: list[dict] = []
        position = 0
        for page, value in present:
            spans.append({"page": page, "raw_start": position, "raw_end": position + len(value)})
            position += len(value) + len(separator)
        return spans
    from app.pipeline.mineru import _first_page_marker

    marker = _first_page_marker(text)
    if marker:
        parts = text.split(marker)
        if [part.strip() for part in parts] == items:
            spans = []
            position = 0
            for page, part in enumerate(parts, 1):
                start = position + len(part) - len(part.lstrip())
                end = position + len(part.rstrip())
                if start < end:
                    spans.append({"page": page, "raw_start": start, "raw_end": end})
                position += len(part) + len(marker)
            return spans
    return []


def _line_ranges(text: str) -> list[tuple[int, int, str]]:
    return [(match.start(), match.end(), match[0].rstrip("\r\n"))
            for match in re.finditer(r"[^\r\n]*(?:\r\n|\r|\n|$)", text) if match.end() > match.start()]


def _header_footer_edits(text: str, page_spans: list[dict]) -> tuple[list[tuple[int, int, str]], set[str]]:
    """同值正文与页边行分别处理，只删除跨页证据确认的实际行位置。"""
    if len(page_spans) < 2:
        return [], set()
    records = _line_ranges(text)
    protected = protected_line_indices([line for _, _, line in records])
    protected_starts = {records[index][0] for index in protected}
    page_rows = [
        [(span["raw_start"] + a, span["raw_start"] + b, normalize_blank_lines(line).strip())
         for a, b, line in _line_ranges(text[span["raw_start"]:span["raw_end"]])]
        for span in page_spans
    ]
    hits = detect_header_footer([[line for _, _, line in rows] for rows in page_rows])
    edits = []
    for rows in page_rows:
        edges = set(range(min(_HEAD_TAIL_LINES, len(rows)))) | set(range(max(0, len(rows) - _HEAD_TAIL_LINES), len(rows)))
        edits.extend((start, end, "") for index, (start, end, line) in enumerate(rows)
                     if index in edges and line in hits and start not in protected_starts)
    return edits, hits


def _pages_to_lines(pages: Iterable[str] | None, text: str) -> list[list[str]]:
    """把「按页文本」或「整段文本」统一成「每页的行列表」。

    没有页信息（txt/md）时，整篇当作一页——此时页眉页脚检测自然退化为空操作，
    不会误删正常文档里的重复行。

    每页先过一遍空白归一：**检测域必须和后面「按行删」的文本域一致**。否则页眉里
    含 2+ 连续空白（``页  眉`` 归一成 ``页 眉``）时，检测命中的字符串与正文里的行
    对不上，会出现「报告说检测到了、正文却一行没删」的假通过。
    """
    if not pages:
        return [text.split("\n")]
    out: list[list[str]] = []
    for page in pages:
        if (page or "").strip():
            out.append(normalize_blank_lines(page).split("\n"))
    return out or [text.split("\n")]


def _page_edge_lines(page_lines: list[str]) -> list[str]:
    """一页的首尾若干行（页眉页脚只可能在这些位置）。"""
    head = page_lines[:_HEAD_TAIL_LINES]
    tail = page_lines[-_HEAD_TAIL_LINES:] if len(page_lines) > _HEAD_TAIL_LINES else []
    return head + tail


def detect_header_footer(pages: list[list[str]]) -> set[str]:
    """S3 跨页重复行检测：返回被判为页眉页脚的行文本集合。

    判定规则（对齐方案 M3.1 S3）：某行出现在 ≥ ``CLEAN_MIN_REPEAT_RATIO`` 比例的页的
    首/末位置，且长度 ≤ ``CLEAN_MAX_HEADER_LEN``，即判为页眉页脚。

    只有一页时不做判定——重复率必然是 1.0，会把正文首行整行删掉。
    """
    if len(pages) < 2:
        return set()

    threshold = max(0.0, min(1.0, float(settings.CLEAN_MIN_REPEAT_RATIO)))
    max_len = int(settings.CLEAN_MAX_HEADER_LEN)
    counts: dict[str, int] = {}
    for page_lines in pages:
        # 同一页内重复出现的同一行只算一次，避免单页内重复行刷高比例
        for line in set(_page_edge_lines(page_lines)):
            stripped = line.strip()
            if stripped:
                counts[stripped] = counts.get(stripped, 0) + 1

    pages_total = len(pages)
    hits: set[str] = set()
    for line, count in counts.items():
        if len(line) > max_len:
            continue
        if count / pages_total >= threshold:
            hits.add(line)
    return hits


def drop_duplicate_paragraphs(lines: list[str]) -> tuple[list[str], dict]:
    """S4 保留重复正文：相同文字并不能证明是解析噪声。"""
    # ponytail: 不推测正文是否重复；有明确页边证据的噪声统一交给 S3。
    return list(lines), {
        "duplicate_blocks_removed": 0, "folding_runs_collapsed": 0,
        "policy": "preserve_body",
    }


def strip_control_chars(text: str) -> tuple[str, dict]:
    """S5 上半：剔除 C0/C1 控制符（保留 ``\\n`` 与 ``\\t``）。"""
    edits = _control_edits(text)
    return _apply_edits(text, [], edits)[0], {
        "control_chars_removed": sum(end - start for start, end, _ in edits),
    }


def collapse_repeats(text: str, max_repeat: int | None = None) -> tuple[str, dict]:
    """S5 下半：只折叠整行标点分隔线，不改金额、账号、序号和业务标识。"""
    edits = _repeat_edits(text, max_repeat)
    return _apply_edits(text, [], edits)[0], {"repeat_chars_collapsed": len(edits)}


def flag_garbled(text: str) -> tuple[str, list[dict]]:
    """S5 的标记部分：``\\ufffd`` 连续 >3 视为乱码，**只标记不删**。

    乱码段本身可能是编码损坏的正文，删掉就是知识损失；标记出来让人来判断。
    """
    flagged: list[dict] = []
    for match in _REPLACEMENT_RUN.finditer(text):
        line_no = text.count("\n", 0, match.start()) + 1
        flagged.append({"reason": "garbled_text", "line": line_no, "length": match.end() - match.start()})
    return text, flagged


def downgrade_tables(lines: list[str]) -> tuple[list[str], dict]:
    """仅转换连续同列、数据行含共同数值列的表格；普通排版空白不作结构证据。"""
    out = list(lines)
    converted = 0
    protected = protected_line_indices(lines)
    rows: list[tuple[int, list[str]]] = []
    def finish():
        nonlocal converted
        # ponytail: 保守接受数值表；纯文字表保持原排版，若需扩大先提供明确结构样例。
        data_rows = rows[1:]
        numeric = r"[+-]?\d+(?:[.,]\d+)*(?:%|元|个)?"
        if len(rows) >= 2 and any(
            all(re.fullmatch(numeric, cells[column]) for _, cells in data_rows)
            for column in range(len(rows[0][1]))
        ):
            for index, cells in rows:
                out[index] = _TABLE_SEP.join(cells)
                converted += 1
        rows.clear()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if (index in protected or not stripped or not _TABLE_ROW.search(line)
                or re.match(r"^(?:[-+*]|\d+[.)]|#{1,6})\s", stripped)
                or re.search(r"[。！？!?；;]|[.]$", stripped)):
            finish()
            continue
        columns = [part.strip() for part in _TABLE_SPLIT.split(stripped) if part.strip()]
        if len(columns) < _TABLE_MIN_COLUMNS:
            finish()
            continue
        if rows and len(columns) != len(rows[0][1]):
            finish()
        rows.append((index, columns))
    finish()
    return out, {"table_rows_converted": converted}


def _table_edits(text: str) -> list[tuple[int, int, str]]:
    """保留每个单元格原位置，只有列间空白转换为 Markdown 分隔符。"""
    lines = text.split("\n")
    converted, _ = downgrade_tables(lines)
    edits: list[tuple[int, int, str]] = []
    position = 0
    for line, replacement in zip(lines, converted):
        if line != replacement:
            cells: list[tuple[int, int]] = []
            start = 0
            for match in [*_TABLE_SPLIT.finditer(line), None]:
                end = match.start() if match else len(line)
                cell = line[start:end]
                if cell.strip():
                    cells.append((start + len(cell) - len(cell.lstrip()), start + len(cell.rstrip())))
                start = match.end() if match else end
            edits.append((position, position + cells[0][0], ""))
            edits.extend((position + left[1], position + right[0], _TABLE_SEP)
                         for left, right in zip(cells, cells[1:]))
            edits.append((position + cells[-1][1], position + len(line), ""))
        position += len(line) + 1
    return edits


def _control_edits(text: str) -> list[tuple[int, int, str]]:
    lines = text.split("\n")
    code = protected_line_indices(lines, only_code=True)
    edits = []
    position = 0
    for index, line in enumerate(lines):
        if index not in code:
            edits.extend((position + match.start(), position + match.end(), "") for match in _CONTROL.finditer(line))
        position += len(line) + 1
    return edits


def _repeat_edits(text: str, max_repeat: int | None = None) -> list[tuple[int, int, str]]:
    limit = int(settings.CLEAN_MAX_REPEAT_CHAR if max_repeat is None else max_repeat)
    if limit <= 0:
        return []
    lines = text.split("\n")
    code = protected_line_indices(lines, only_code=True)
    pattern = re.compile(r"^([^\w\s])\1{%d,}$" % limit)
    edits = []
    position = 0
    for index, line in enumerate(lines):
        if index not in code and pattern.fullmatch(line):
            edits.append((position + limit, position + len(line), ""))
        position += len(line) + 1
    return edits


# --------------------------------------------------------------------------
# 流水线入口
# --------------------------------------------------------------------------

def _split_lines(text: str) -> list[str]:
    return text.split("\n")


def clean(
    text: str,
    pages: Iterable[str] | None = None,
    enable: bool | None = None,
) -> CleanResult:
    """跑完整清洗流水线，返回清洗结果与报告。

    :param text: 解析得到的原始文本（会作为 ``raw`` 留档）
    :param pages: 可选的分页文本；有页信息时才能做页眉页脚检测
    :param enable: 覆盖 ``CLEAN_ENABLE`` 开关（dry-run 与入库都用同一路径）
    """
    raw = text or ""
    page_spans = page_source_spans(raw, pages)
    source_map = (
        [{"clean_start": 0, "clean_end": len(raw), "raw_start": 0, "raw_end": len(raw)}] if raw else []
    )
    on = settings.CLEAN_ENABLE if enable is None else enable
    if not on:
        # 回退口径就是「按原文入库」（5.8 节）：连空白都不再动，入库文本与 raw 完全一致
        lines = raw.count("\n") + 1 if raw else 0
        return CleanResult(
            text=raw, raw=raw, steps=[StepStat("disabled", lines, lines, len(raw), len(raw))], enabled=False,
            source_map=source_map, page_spans=page_spans,
        )

    steps: list[StepStat] = []
    flagged: list[dict] = []
    removed: list[dict] = []
    current = raw

    # --- S3 页眉页脚 ---
    if settings.CLEAN_HEADER_FOOTER:
        before, before_lines = current, len(_split_lines(current))
        edits, hits = _header_footer_edits(current, page_spans)
        current, source_map = _apply_edits(
            current, source_map, edits, step="header_footer", removed=removed,
        )
        steps.append(
            StepStat(
                "header_footer",
                before_lines,
                len(_split_lines(current)),
                len(before),
                len(current),
                {"patterns": sorted(hits)},
            )
        )

    # 列分隔必须在 S2 折空白前处理，代码 / Markdown 表格 / 引用不做降级。
    if settings.CLEAN_TABLE_DOWNGRADE:
        before, before_lines = current, len(_split_lines(current))
        kept, detail = downgrade_tables(_split_lines(current))
        current, source_map = _apply_edits(
            current, source_map, _table_edits(current), step="table_downgrade", removed=removed,
        )
        steps.append(StepStat("table_downgrade", before_lines, len(kept), len(before), len(current), detail))

    # --- S2 空白归一（强制）---
    before, before_lines = current, len(_split_lines(current))
    current, source_map = normalize_with_map(current, source_map, removed)
    steps.append(StepStat("normalize", before_lines, len(_split_lines(current)), len(before), len(current)))

    # --- S4 重复段落 ---
    if settings.CLEAN_DUPLICATE_PARA:
        before, before_lines = current, len(_split_lines(current))
        kept, detail = drop_duplicate_paragraphs(_split_lines(current))
        current = "\n".join(kept)
        steps.append(StepStat("duplicate_paragraph", before_lines, len(_split_lines(current)), len(before), len(current), detail))

    # --- S5 乱码与控制符 ---
    if settings.CLEAN_CONTROL_CHAR:
        before, before_lines = current, len(_split_lines(current))
        control_edits = _control_edits(current)
        control_detail = {"control_chars_removed": sum(end - start for start, end, _ in control_edits)}
        current, source_map = _apply_edits(
            current, source_map, control_edits, step="control_char", removed=removed,
        )
        repeat_edits = _repeat_edits(current)
        repeat_detail = {"repeat_chars_collapsed": len(repeat_edits)}
        current, source_map = _apply_edits(
            current, source_map, repeat_edits, step="repeat_separator", removed=removed,
        )
        current, garbled = flag_garbled(current)
        flagged.extend(garbled)
        steps.append(
            StepStat(
                "control_char",
                before_lines,
                len(_split_lines(current)),
                len(before),
                len(current),
                {**control_detail, **repeat_detail, "garbled_flagged": len(garbled)},
            )
        )

    page_starts = [span["raw_start"] for span in page_spans]
    for item in removed:
        index = bisect_right(page_starts, item["raw_start"]) - 1
        item["page"] = (
            page_spans[index]["page"] if index >= 0 and item["raw_start"] < page_spans[index]["raw_end"] else None
        )
    return CleanResult(
        text=current, raw=raw, steps=steps, flagged=flagged, enabled=True,
        source_map=source_map, page_spans=page_spans, removed_ranges=removed,
    )
