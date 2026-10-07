"""分块：约 800 字一块，块间 100 字重叠。

条目 2.2 新增 ``chunk_document``：在老基线 ``chunk_text`` 之上，输出带元数据的块
（标题层级 ``heading_path``、页码 ``page``、归一化全文偏移 ``start_offset`` / ``end_offset``）。
``normalize`` / ``chunk_text`` 保留老基线；文档路径保护 Markdown 结构。
"""

import ast
from bisect import bisect_right
import re
from dataclasses import dataclass

from app.core.config import settings
from app.pipeline.cleaning import (
    _FENCE, normalize_with_map, page_source_spans,
    protected_line_indices, source_slice,
)

_WS = re.compile(r"[ \t\u3000]+")
_BREAKS = "。！？；!?;\n"

# Markdown / office 标题行：``#*N 文本``（office 经 parsing 统一成 ``# 标题`` /
# ``## 幻灯片 i`` / ``## Sheet 名``）。要求 ``#`` 与内容间至少一个空白、内容非空。
_HEADING_RE = re.compile(r"^(#{1,6})\s+\S")


@dataclass(frozen=True)
class ChunkMeta:
    """带元数据的切块。

    ``start_offset`` / ``end_offset`` 指向 ``chunk_document`` 归一化后的全文
    （结构保护后的规范文本），满足 ``0 <= start_offset < end_offset <= len(normalized)``。
    跨块补上的代码围栏 / 表头属于展示上下文，不计入这些正文偏移。
    """

    text: str
    ordinal: int
    page: int | None = None             # 1 起；只有 PDF 有页边界时才给，其余 None
    start_offset: int | None = None     # 归一化全文里的字符起点（闭）
    end_offset: int | None = None       # 字符终点（开区间）
    heading_path: tuple[str, ...] = ()  # 该块所属标题层级链（不含 #）
    # ── 工具升级-3 · 回归检查：代码块的「源文件行号 + 符号路径」──────────────────────────
    # ⚠️ 三个字段**只在 ``chunk_code()`` 里被填写**；``chunk_document()`` 一律保持默认 None
    # （不得把 ``_split_blocks`` 的行索引填进来 —— 那是在改已冻结的函数行为，见 D-回归检查 第 5 条）。
    # ⚠️ 行号是**源文件**的 1 起闭区间行号，与 ``start_offset`` 的「归一化全文字符偏移」
    # **不是同一个坐标系**，两者并存、不得互相挪用（K-回归检查）。
    line_start: int | None = None       # 源文件 1 起行号（闭）；只有 chunk_code 填
    line_end: int | None = None         # 源文件 1 起行号（闭）；只有 chunk_code 填
    symbol_path: str | None = None      # 顶层函数/类名；非符号块为 None
    source_ranges: tuple[dict, ...] = ()  # 规范文本范围 → 原始解析文本范围；跨页全部保留


def normalize(text: str) -> str:
    """压缩空白，保留段落换行。"""
    lines = [_WS.sub(" ", line).strip() for line in text.replace("\r\n", "\n").split("\n")]
    out: list[str] = []
    for line in lines:
        if line:
            out.append(line)
        elif out and out[-1] != "":
            out.append("")
    return "\n".join(out).strip()


def _iter_splits(normalized_text: str, size: int, overlap: int):
    """内部唯一切分函数：对已归一化文本产出 (piece, start, end)。

    ``start`` / ``end`` 是 ``normalized_text`` 内的字符偏移；``piece`` 即
    ``normalized_text[start:end].strip()``。``chunk_text`` 与 ``chunk_document``
    共用此函数，保证「无标题文本须与 ``chunk_text`` 逐字相同」且只有一套切分逻辑。
    """
    total = len(normalized_text)
    start = 0
    while start < total:
        end = min(start + size, total)
        if end < total:
            window = normalized_text[start:end]
            cut = max(window.rfind(ch) for ch in _BREAKS)
            if cut >= int(size * 0.6):
                end = start + cut + 1
        piece = normalized_text[start:end].strip()
        if piece:
            yield piece, start, end
        if end >= total:
            break
        next_start = end - overlap
        start = next_start if next_start > start else start + 1


def _merge_blocks(
    blocks: list[tuple[int, int, tuple[str, ...]]],
    lines: list[str],
    line_starts: list[int],
    size: int,
) -> list[tuple[int, int, tuple[str, ...]]]:
    """把相邻的**小**标题块贪心合并，直到接近 ``size`` 上限。

    为什么需要：pptx（``## 幻灯片 i``）/ xlsx（``## Sheet 名``）/ 大纲类文档的小节常常
    只有几十字。若严格「一节一块」，40 页 PPT 会产出 40 个 ~30 字的碎块——检索难命中、
    引用片段过短。合并**只在装得下时**发生（合并后跨度仍 <= ``size``），所以每节都够大的
    文档（每节 > size/2）行为与逐节切块完全一致，不改变既有语义。

    合并块的 ``heading_path`` 取**首节**路径；被并入的后续小节标题行仍留在块文本内，
    检索时依旧看得见小节名。返回 (首行索引, 尾行索引, heading_path) 三元组。
    """
    merged: list[tuple[int, int, tuple[str, ...]]] = []
    cur_a: int | None = None
    cur_b = 0
    cur_path: tuple[str, ...] = ()

    for a, b, path in blocks:
        if cur_a is not None:
            span = line_starts[b] + len(lines[b]) - line_starts[cur_a]
            if span <= size:
                cur_b = b  # 装得下 → 并入当前块
                continue
            merged.append((cur_a, cur_b, cur_path))
        cur_a, cur_b, cur_path = a, b, path

    if cur_a is not None:
        merged.append((cur_a, cur_b, cur_path))
    return merged


def _split_blocks(normalized: str):
    """把归一化文本按标题行切成块，返回 (blocks, lines, line_starts)。

    ``blocks`` 为 (首行索引, 尾行索引, heading_path) 三元组；块文本即
    ``normalized[line_starts[a]:line_starts[b]+len(lines[b])]``，标题行本身留在块里。
    """
    lines = normalized.split("\n")
    line_starts: list[int] = []
    pos = 0
    for line in lines:
        line_starts.append(pos)
        pos += len(line) + 1  # 每行后一个 "\n"，末行无尾随换行

    blocks: list[tuple[int, int, tuple[str, ...]]] = []
    # 层号 -> 该层最近一次出现的标题文本（稀疏表，天然互斥同级：进入新标题写回本层，
    # 同级标题自然被替换而非累加；比 stack[:level-1] 安全——缺祖先层时也不会把同级当子级）。
    levels: dict[int, str] = {}
    cur_start: int | None = None
    cur_path: tuple[str, ...] = ()

    protected = protected_line_indices(lines)
    for i, line in enumerate(lines):
        match = _HEADING_RE.match(line) if i not in protected else None
        if match:
            if cur_start is not None:
                blocks.append((cur_start, i - 1, cur_path))
            level = len(match.group(1))
            # 进入 level 级标题：丢弃比它更深的层，写回本层（同级替换）；保留更浅的祖先层。
            levels = {k: v for k, v in levels.items() if k < level}
            levels[level] = line[match.end(1):].lstrip()
            cur_path = tuple(levels[k] for k in sorted(levels))
            cur_start = i
        elif cur_start is None:
            # 首个标题之前的普通内容，归属空标题链
            cur_start = i
    if cur_start is not None:
        blocks.append((cur_start, len(lines) - 1, cur_path))
    return blocks, lines, line_starts


def _iter_document_splits(text: str, size: int, overlap: int):
    """结构行优先切分；跨块补围栏和表头，正文坐标仍指向真实子串。"""
    lines = text.split("\n")
    protected = protected_line_indices(lines)
    if not protected:
        yield from _iter_splits(text, size, overlap)
        return
    starts: list[int] = []
    contexts: list[str] = []
    tables: dict[int, str] = {}
    fence = ""
    marker_text = ""
    table_start: int | None = None
    position = 0
    for index, line in enumerate(lines):
        starts.append(position)
        position += len(line) + 1
        marker = _FENCE.match(line)
        contexts.append(marker_text)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence, marker_text = "", ""
        elif marker:
            fence, marker_text = marker[1], line
        if "|" in line and index in protected and not contexts[-1] and not marker:
            if table_start is None:
                table_start = index
            header = lines[table_start]
            if table_start + 1 < len(lines) and re.fullmatch(r"[\s|:\-]+", lines[table_start + 1]):
                header += "\n" + lines[table_start + 1]
            if index > table_start and not re.fullmatch(r"[\s|:\-]+", line):
                tables[index] = header
        else:
            table_start = None
    total = len(text)
    start = 0
    while start < total:
        first = bisect_right(starts, start) - 1
        prefix = contexts[first] + "\n" if contexts[first] else ""
        if not prefix and start > starts[first] and lines[first].startswith(("    ", "\t")):
            prefix = re.match(r"[ \t]+", lines[first])[0]
        if first in tables:
            prefix += tables[first] + "\n"
        active_marker = _FENCE.match(contexts[first] or lines[first])
        closing_chars = len(active_marker[1]) + 1 if active_marker else 0
        allowance = max(1, size - len(prefix) - closing_chars)
        end = min(start + allowance, total)
        if end < total:
            cut = text.rfind("\n", start, end)
            if cut > start:
                end = cut + 1
            elif "|" in lines[first] and first in protected and start == starts[first]:
                # ponytail: 一条结构行最多沿用代码窗口的 4000 字上限；更长行回退字符窗。
                end = min(starts[first] + len(lines[first]) + 1, start + max(size, CODE_WINDOW_CHARS), total)
        last = bisect_right(starts, max(start, end - 1)) - 1
        piece = text[start:end].strip("\n")
        suffix = ""
        if end < total:
            following = bisect_right(starts, end) - 1
            active = contexts[following]
            if active:
                closing = _FENCE.match(active)
                suffix = "\n" + closing[1]
        if piece.strip():
            yield prefix + piece + suffix, start, end
        if end >= total:
            break
        next_start = max(start + 1, end - overlap)
        if first in protected or last in protected:
            candidate = bisect_right(starts, next_start) - 1
            next_start = starts[candidate] if starts[candidate] > start else end
        start = next_start


def _assign_page(probe: str, normalized_pages: list[str]) -> int | None:
    """旧调用兼容：完整片段只在唯一页命中时返回；生产分块不使用文本探针。"""
    probe = (probe or "").strip()
    if not probe:
        return None
    matches = [idx for idx, page_text in enumerate(normalized_pages, start=1) if probe in page_text]
    return matches[0] if len(matches) == 1 else None


def _source_ranges(
    source_map: list[dict], start: int, end: int, page_spans: list[dict], map_ends: list[int],
) -> tuple[dict, ...]:
    """每块直接截取原始范围；额外补上的围栏 / 表头不进入这些正文范围。"""
    out: list[dict] = []
    page_ends = [span["raw_end"] for span in page_spans]
    for span in source_slice(source_map, start, end, ends=map_ends):
        raw_start, raw_end = span["raw_start"], span["raw_end"]
        parts = []
        position = raw_start
        for index in range(bisect_right(page_ends, raw_start), len(page_spans)):
            page = page_spans[index]
            a, b = max(position, page["raw_start"]), min(raw_end, page["raw_end"])
            if a >= b:
                break
            if position < a:
                parts.append((position, a, None))
            parts.append((a, b, page["page"]))
            position = b
        if position < raw_end:
            parts.append((position, raw_end, None))
        linear = span["clean_end"] - span["clean_start"] == raw_end - raw_start
        for a, b, page in parts:
            clean_a = span["clean_start"] + a - raw_start if linear else span["clean_start"]
            clean_b = clean_a + b - a if linear else span["clean_end"]
            item = {
                "canonical_start": clean_a, "canonical_end": clean_b,
                "raw_start": a, "raw_end": b, "page": page,
            }
            if out and out[-1]["page"] == page and out[-1]["raw_end"] == a and out[-1]["canonical_end"] == clean_a:
                previous = out[-1]
                if (
                    previous["canonical_end"] - previous["canonical_start"] == previous["raw_end"] - previous["raw_start"]
                    and clean_b - clean_a == b - a
                ):
                    previous["raw_end"], previous["canonical_end"] = b, clean_b
                    continue
            out.append(item)
    return tuple(out)


def chunk_document(
    text: str,
    pages: list[str] | None = None,
    size: int | None = None,
    overlap: int | None = None,
    *,
    source_map: list[dict] | None = None,
    page_spans: list[dict] | None = None,
) -> list[ChunkMeta]:
    """带元数据的分块：保留标题层级 + 页码 + 归一化偏移。

    - 标题行开启新小节，标题行留在块内；块内维护 ``heading_path``（取该块**首节**路径）。
    - **小节合并**：相邻小节在「合并后跨度仍 <= ``size``」时合并成一块，避免 PPT/表格/
      大纲这类「节多而短」的文档产出大量几十字的碎块（检索难命中、引用片段过短）。
      每节都够大的文档（每节 > size/2）不会被合并，行为与逐节切块一致。
    - 因此块边界**只落在小节边界**，或落在**单个超长小节内部**（该小节跨度 > ``size`` 时
      交给 ``_iter_splits`` 再切）；绝不会把一个短小节从中间切断。
    - 普通正文复用既有字符窗；结构区先保留空白，跨块补围栏 / 表头。
    - ``page`` 来自解析器直传的原始范围；首个正文字符无页依据时为 ``None``。
    - 已有 ``source_map`` 时 ``text`` 就是规范文本，不重复归一而使已有偏移失效。
    - 与冻结基线 ``chunk_text`` 的一处**有意差异**：``chunk_text`` 沿用老仓库的
      ``size or 默认`` 写法（``size=0`` 回退默认值），本函数按条目 2.2 规格对
      ``size <= 0`` 直接抛 ``ValueError``。
    """
    size = settings.RAG_CHUNK_SIZE if size is None else size
    overlap = settings.RAG_CHUNK_OVERLAP if overlap is None else overlap
    if size <= 0:
        raise ValueError("分块长度必须大于 0")
    overlap = max(0, min(overlap, size - 1))

    if source_map is None:
        normalized, source_map = normalize_with_map(text)
        page_spans = page_source_spans(text, pages) if page_spans is None else page_spans
    else:
        normalized = text
    page_spans = page_spans or []
    if not normalized:
        return []
    map_ends = [span["clean_end"] for span in source_map]

    blocks, lines, line_starts = _split_blocks(normalized)
    blocks = _merge_blocks(blocks, lines, line_starts, size)
    result: list[ChunkMeta] = []
    ordinal = 0
    for a, b, path in blocks:
        block_start = line_starts[a]
        block_end = line_starts[b] + len(lines[b])
        block_text = normalized[block_start:block_end]
        for piece, local_start, local_end in _iter_document_splits(block_text, size, overlap):
            g_start = block_start + local_start
            g_end = block_start + local_end
            ranges = _source_ranges(source_map, g_start, g_end, page_spans, map_ends)
            body = normalized[g_start:g_end]
            first = g_start + len(body) - len(body.lstrip())
            page = next((span["page"] for span in ranges
                         if span["canonical_start"] <= first < span["canonical_end"]), None)
            result.append(
                ChunkMeta(
                    text=piece,
                    ordinal=ordinal,
                    page=page,
                    start_offset=g_start,
                    end_offset=g_end,
                    heading_path=path,
                    source_ranges=ranges,
                )
            )
            ordinal += 1
    return result


def chunk_text(
    text: str,
    size: int | None = None,
    overlap: int | None = None,
) -> list[str]:
    """把长文本切成带重叠的块。

    **冻结基线**：本函数行为与老仓库 ``app/rag/chunking.py::chunk_text`` 逐字一致，
    包括 ``size or 默认值`` 这一写法——``size=0`` 时回退默认块长而**不**抛错
    （负数仍抛 ``ValueError``）。改用 ``size is None`` 判断会改变该边界行为，
    属语义变更，故此处必须保持 ``or``。内部切分已抽到 ``_iter_splits`` 与其共用。
    """
    size = size or settings.RAG_CHUNK_SIZE
    overlap = settings.RAG_CHUNK_OVERLAP if overlap is None else overlap
    if size <= 0:
        raise ValueError("分块长度必须大于 0")
    overlap = max(0, min(overlap, size - 1))

    text = normalize(text)
    if not text:
        return []

    return [piece for piece, _, _ in _iter_splits(text, size, overlap)]

# ===========================================================================
# 代码切块（工具升级-3 · 回归检查 · D-回归检查）
# ===========================================================================
#: 代码行窗切块的窗口行数（D-回归检查 第 6 条写死）
CODE_WINDOW_LINES = 80
#: 代码行窗切块的重叠行数（D-回归检查 第 6 条写死）
CODE_WINDOW_OVERLAP = 10
#: 代码单块的**字符**上限（``回归修复`` 新增）—— 行窗只管「行数」，管不住「单行超长」。
#:
#: **为什么需要**：窗宽只有 :data:`CODE_WINDOW_LINES` 行，而单行压缩型文件
#: （minified JS / 单行 JSON）**整份 = 1 行** ⇒ 1 个块 = 全文。实测（``回归修复`` 探针）：
#: 200,009 字符的单行 `.js` 产出 **1 块 / 200,009 字**；该块入库成功但 embed 侧 4xx
#: （`vector/embed.py:120-123`）⇒ `vectorized=false` —— **优雅失败、不报错**，所以一直没被发现。
#:
#: **取值依据**：
#: ① **不得低于仓内既有合法块的量级** —— :func:`_emit` 的 B-1 注释记录过 **1983 字**的单块
#:    （那是 80 行行窗的正常产物、不是缺陷），`tests/pipeline/test_code_lines_t3_2_4.py`
#:    的样本一个行窗实测 **1031 字** ⇒ 取 4000 保证**这些样本**的行窗不被切。
#: ② **与仓内「单条」刻度同口径** —— `retrieval/types.py` 的 `MATERIAL_ITEM_MAX_CHARS`、
#:    `agent/runner.py` 的 `TOOL_CONTEXT_ITEM_LIMIT`、`tools/fetch_page` 的
#:    `FETCH_CONTENT_CLIP`、`settings.WEB_MAX_CONTENT` **都是 4000** ⇒ 消费侧对**单条**正文的
#:    上限本来就是 4000，产出更大的块没有消费价值（只会让 embed 面更早失败）。
#: ③ 🔴 **触发条件是「行窗拼接总字长 > 4000」，不是「单行 > 4000」**（**口径更正**，来源 = 全批审计 B
#:    的 137-1）：任何行窗只要拼起来超 4000 字就会被切 —— **普通多行代码同样触发**。实测（本仓载体
#:    第 ⑪ 腿 + 回退击杀两态读数）：100 行 × 54 字的普通 Python 赋值（**无任何超长行**），
#:    80 行行窗拼起来 **4399 字** ⇒ 同一份样本由旧实现 **2 块 / 最大 4399 字** 变成
#:    现实现 **3 块 / 最大 3959 字**（片长表 ``[3959, 439, 1649]``）。
#:    ⇒ 正确口径是「**只有窗口拼接总字长 ≤ 4000 的行窗**，切分才与加字符窗之前逐字相同」；
#:    ⛔ 不得写成「只有病理级长行才触发」「正常代码切分逐字不变」。
#: ⚠️ **4000 > `RAG_CHUNK_SIZE`（800）**：本常量**不**恢复「主命中块 ≤ 800」的既有口径，
#: 它只把上界从**无界**收到 4000（口径见 :func:`chunk_code` 的 R-4）。
CODE_WINDOW_CHARS = 4000


def _window_slices(total_lines: int, window: int, overlap: int) -> list[tuple[int, int]]:
    """把 ``[0, total_lines)`` 按窗口切成 0 起**半开**行区间 ``[a, b)``，相邻区间重叠 ``overlap`` 行。

    与 :func:`_iter_splits` 是同一套「步进 + 重叠」形状，但这里切的是**行**而不是字符。
    不变量：① 区间按 a 升序且相邻重叠恰为 ``overlap``；② 并集覆盖全部行；
    ③ ``b - a <= window``；④ 行号连续推进（``next_a = b - overlap``，且必须真前进）。
    """
    if total_lines <= 0:
        return []
    step = max(1, window - max(0, min(overlap, window - 1)))
    out: list[tuple[int, int]] = []
    start = 0
    while start < total_lines:
        end = min(start + window, total_lines)
        out.append((start, end))
        if end >= total_lines:
            break
        start += step
    return out


def _char_windows(lines: list[str], a: int, b: int) -> list[tuple[int, int, str]]:
    """**字符窗**（两级窗口里的第二级）：把 1 起闭区间行段 ``[a, b]`` 切成 ``(line_start, line_end, text)``。

    ``回归修复`` 新增。行窗（:data:`CODE_WINDOW_LINES`）只管**行数** ⇒ 单行压缩型文件
    （minified JS / 单行 JSON）整份 = 1 行，行窗完全失效（实测 1 块 / 200,009 字）。本函数补上字符维。

    🔴 **判据 = 这一片拼起来的总字长（行内字符数之和 + 行间换行数）≤ :data:`CODE_WINDOW_CHARS`**
    —— ⚠️ **不是**「单行长度 ≤ 上限」（**口径更正**，来源 = 全批审计 B 的 137-1）：**多行行窗只要拼起来
    超限就会被切**，普通代码一样触发（实测 100 行 × 60 字 ⇒ 80 行行窗 4879 字被切成 2 片）。

    切法（**两级窗口的顺序不可颠倒**：先 :func:`_window_slices` 行窗、再在本函数里过字符窗）：

    - **行边界优先**（多行超限走这支）：逐行装填，装得下就并入当前片，装不下就断在**行边界**上
      ⇒ 每片 ``text`` = 若干**完整源行**的 ``"\n"`` 拼接，行号仍是源文件物理行号
      （与 :func:`chunk_code` 第 5 条同口径）；
    - **单行本身超上限**（``len(line) > CODE_WINDOW_CHARS``）：该行自己按上限切片，各片
      ``line_start == line_end == 该行号``、``text`` 是该行的**连续子串**（不是整行）
      ⇒ 这是本笔引入的**新语义**，已在 :func:`chunk_code` 的 **R-4** 登记。

    **重叠口径**：字符窗一级**不重叠**（重叠只保留在行窗一级的 :data:`CODE_WINDOW_OVERLAP` 行）。
    理由：行内片若也带重叠，压缩型长行会被成倍复制进语料（200,009 字的单行按 4000 窗口 + 重叠
    能膨胀数倍），而**行窗重叠已经提供了跨块上下文**。

    ⚠️ **不调用** :func:`normalize`：切点按**传入字符串**算，与行号坐标系一致（D-回归检查 第 7 条）。
    """
    out: list[tuple[int, int, str]] = []
    cur_a: int | None = None          # 当前片的起始行号（1 起）；None = 还没有攒起任何行
    cur_len = 0                       # 当前片拼好后的字符数（含行间那个 ``"\n"``）
    for i in range(a, b + 1):
        line = lines[i - 1]
        if len(line) > CODE_WINDOW_CHARS:
            # 先把攒起来的那片收掉（它必须断在这一行**之前**，否则本片也就超限了）
            if cur_a is not None:
                out.append((cur_a, i - 1, "\n".join(lines[cur_a - 1 : i - 1])))
                cur_a, cur_len = None, 0
            for start in range(0, len(line), CODE_WINDOW_CHARS):
                out.append((i, i, line[start : start + CODE_WINDOW_CHARS]))
            continue
        add = len(line) if cur_a is None else len(line) + 1   # +1 = 行间那个 "\n"
        if cur_a is not None and cur_len + add > CODE_WINDOW_CHARS:
            out.append((cur_a, i - 1, "\n".join(lines[cur_a - 1 : i - 1])))
            cur_a, cur_len = i, len(line)
        else:
            if cur_a is None:
                cur_a = i
            cur_len += add
    if cur_a is not None:
        out.append((cur_a, b, "\n".join(lines[cur_a - 1 : b])))
    return out


def _code_fallback_chunks(
    lines: list[str], *, symbol_path: str | None = None
) -> list[tuple[int, int, str | None, str]]:
    """行窗 + 字符窗兜底：产出 ``(line_start, line_end, symbol_path, text)`` 四元组（1 起**闭**区间）。

    **重叠只在行窗一级**（:data:`CODE_WINDOW_OVERLAP` 行）—— 相邻行窗共享的整行会**同时属于两个块**，
    这是刻意的（与 ``chunk_text`` 的重叠口径同源）；**字符窗一级不重叠**（见 :func:`_char_windows`）。
    所以本函数的输出**允许行号重叠**，判据必须按「每个块自成一段连续区间、端点指向真实行」来断，
    不得断「块间行号互不相交」，也不得断「两块文本无公共前缀」（行窗重叠处本来就会重复）。
    """
    out: list[tuple[int, int, str | None, str]] = []
    for a, b in _window_slices(len(lines), CODE_WINDOW_LINES, CODE_WINDOW_OVERLAP):
        for la, lb, text in _char_windows(lines, a + 1, b):
            if text.strip():
                out.append((la, lb, symbol_path, text))
    return out


def _top_level_symbol_spans(tree) -> list[tuple[int, int, str]]:
    """抽出顶层函数 / 类的 ``(start_lineno, end_lineno, 名字)``（1 起闭区间，保持 AST 顺序）。

    - 只取**顶层**（``tree.body``）⇒ 方法留在所属类块里，与 ``D-回归检查`` 第 6 条一致；
    - 异步函数与同步函数同待遇（都是顶层可调用体）；
    - ``decorator_list`` 会把起点上移到装饰器行（``min`` 取两者）—— 否则装饰器会被切到前一个块里，
      而「符号块应包含它自己的装饰器」是代码阅读的常识。
    """
    spans: list[tuple[int, int, str]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = min(
            [node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]
        )
        end = node.end_lineno or node.lineno
        spans.append((start, end, node.name))
    return spans


def chunk_code(text: str) -> list[ChunkMeta]:
    """按符号切代码（Python 走 stdlib ``ast``，其余语言走固定行窗兜底）。

    **规则（``D-回归检查`` 写死，不再另议；第 3/4 条的窗口在 ``回归修复`` 补上「字符」这一维）**：
    1. ``.py`` 且 ``ast.parse`` 成功 ⇒ **每个顶层函数 / 类一块**（方法留在类块里），
       ``symbol_path`` = 函数名或类名，行号 = ``lineno``/``end_lineno``（1 起，闭区间）；
    2. 符号前的前言、符号之间的空档，**非空就各自成块**（``symbol_path`` 为空）；
    3. 单个符号块超过 :data:`CODE_WINDOW_LINES` 行 ⇒ **只在该块内部**再按行窗切开，
       ``symbol_path`` 保持不变；**每个行窗内部**再过一遍字符窗
       （:data:`CODE_WINDOW_CHARS`，``回归修复`` 新增，见 R-4）——
       字符窗的判据是**该行窗拼接总字长**超限，**多行普通代码同样会触发**（不是只切病理级长行）；
    4. 语法错误、以及其他 28 种后缀 ⇒ **整份按行窗 + 字符窗切**，``symbol_path`` 为空；
    5. **禁止调用** :func:`normalize` —— 行号按**传入字符串的原始换行**计算，
       归一化会改变行序（``D-回归检查`` 第 7 条）。

    **为什么只用 stdlib ``ast``**：``D-回归检查`` 依据段写死「未要求多语言 AST」⇒ 用 stdlib 解析 Python、
    其余语言行窗兜底，**零新依赖**且行为完全可判（禁止 tree-sitter 等）。

    ``ordinal`` 从 0 起按产出顺序递增；``page`` / ``start_offset`` / ``end_offset`` 保持 ``None``
    （代码没有页边界，字符偏移语义是 ``chunk_document`` 归一化坐标系里的，不适用）。

    **三条如实登记的语义（独立复核 R-1/R-2/R-3 补写，判据不得违反）**：
    - **R-1 行号可以重叠**：行窗切块带 :data:`CODE_WINDOW_OVERLAP` 行重叠 ⇒ 同一符号切出的
      两个块**行号区间真重叠**（实测 `行 1-80` + `行 71-120`）。这是与 ``chunk_text`` 同源的重叠口径，
      **刻意如此、不是 bug**。⇒ 断「块间行号互不相交」「并集等于全文」的判据**必假红**，不得写。
    - **R-2 行号覆盖可以有洞**：符号之间的**纯空白区间不产块**（``any(ln.strip())`` 判空）
      ⇒ 行号覆盖**不保证完整覆盖源文件每一行**。⇒ A14 只可断「每块自成连续区间且端点指向真实行」。
    - **R-3 ``symbol_path`` 是「顶层符号名」，不是限定路径**：嵌套的类方法留在类块里
      （符合 ``D-回归检查`` 第 6 条），且**同一文件的两个同名顶层符号会产出相同 ``symbol_path``**、
      彼此不可区分 —— **本阶段已知限度**，如实登记（列名含 path 但语义是 name）。
    - **R-4 字符窗会切「超限的行窗」，单行超限时还会切行内（``回归修复``，如实登记）**：
      第二级的判据是**行窗拼接总字长**（行内字符数之和 + 行间换行数）> :data:`CODE_WINDOW_CHARS`：

      ① **多行行窗超限 ⇒ 断在行边界**（``text`` 仍是整行拼接、行号仍是物理行号、行号区间自洽），
         ⚠️ **普通代码同样会触发**：「只有病理级长行才触发 / 正常代码的切分与加字符窗之前逐字相同」
         这两句**与实测不符、已作废**（来源 = 全批审计 B 的 137-1；实测 100 行 × 54 字的普通 Python
         赋值，80 行行窗拼起来 **4399 字** ⇒ 由旧实现 **2 块 / 最大 4399 字** 变成现实现
         **3 块 / 最大 3959 字**；两态读数见载体第 ⑪ 腿的回退击杀）。
         **正确口径**：只有「**窗口拼接总字长 ≤ 4000**」的行窗，切分才与加字符窗之前逐字相同；
      ② **单行 ``len(line) > `` 上限 ⇒ 行内切分**：该行被切成多片，每片的
         ``line_start == line_end ==`` **这一行的物理行号**，而 ``text`` 是**该行的连续子串**
         （**不是**整行）⇒ R-2 之后「块文本 == 源行区间」这条可回溯定义式**只对行边界片成立**，
         行内片的正确形式是「``text`` 是 ``lines[line_start-1]`` 的某段连续子串」。

      连带两条登记：① **行内片不携带列号**（``ChunkMeta`` 无列字段、本笔不建迁移）
      ⇒ 读侧只能回溯到「第 N 行」，**不能**回溯到「第 N 行第 M 列」；② **字符窗一级不重叠**
      （重叠只保留在行窗一级的 :data:`CODE_WINDOW_OVERLAP` 行）⇒ 行内片之间既不重叠、
      也不保证覆盖该行的首尾空白（各片 ``strip()`` 后首尾空白会丢）。
      ⇒ 判据**不得**断「每块文本 == 源行区间」（超限行窗与行内片都必假红），
      也**不得**断「块间行号互不相交」（R-1 已钉）。

    ⚠️ **与 ``RAG_CHUNK_SIZE``（800）的关系（如实登记，别过度声称）**：字符窗的上界
    :data:`CODE_WINDOW_CHARS` = 4000 **大于** 800 ⇒ 本函数**不**承诺「一个代码块 ≤ ``RAG_CHUNK_SIZE``」。
    该口径**在加字符窗之前就已经被打破**（80 行行窗可以产出 1983 字的合法单块，见 :func:`_emit`
    的 B-1 注释），本笔只把上界**从无界收到 4000**，**不恢复** 800；消费侧对单条的既有上限也是
    4000（见 :data:`CODE_WINDOW_CHARS` 的取值依据）。
    """
    # 🔴 **行尾归一（终局审计 A-1 的 CRLF/CR 反例，本版就地修）**：
    # 行号是「**源文件物理行号**」，而物理行的定义必须吃三种行尾（`\r\n` / `\n` / `\r`）——
    # 与 Python 的 universal newlines 同口径。首版只按 `"\n"` 切 ⇒ **`\r`-only 的文件整份算一行**
    # ⇒ 实测「121 物理行的文件」被报成 `line_start=1, line_end=80`，**行号与源文件解耦**。
    # `\r\n` 首版能过是因为 `"\n"` 恰好也是它的分隔符；`\r`-only 则完全漏掉。
    # ⚠️ **只归一换行、不动其余字符** —— 缩进 / tab / BOM 语义不受影响（它们不是行尾）。
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    # 末尾那一个空串是「文件以换行结尾」的产物，不是真行；不摘掉会把行数多算 1。
    if lines and lines[-1] == "":
        lines = lines[:-1]

    spans: list[tuple[int, int, str | None, str]] = []

    def _emit(a: int, b: int, name: str | None) -> None:
        """把一个 1 起闭区间行段入列；**行窗 + 字符窗两级切开**（四处调用点共用一处实现）。

        🔴 **独立复核 B-1（BLOCKER，首版就地修）**：首版只对**符号块**做行窗再切，
        前言 / 空档 / 尾巴三处是**无界 append** ⇒ 实测产出过 **200 行 / 1983 字**的单块
        （``RAG_CHUNK_SIZE`` = 800 的 2.5 倍），且**直接进向量化**。
        ``D-回归检查`` 第 6 条的原话是「**单块**超过 80 行时，只在该块内部再按行窗切开」——
        「该块」指**每一个块**（含前言/空档/尾巴），不是只指符号块。
        ⇒ 三处无界 append 全部收进本函数，一处实现。

        🔴 **回归修复（本版就地修）**：行窗只限**行数** ⇒ 单行压缩型文件（整份 = 1 行）
        照样产出**无界**单块（实测 200,009 字 / 1 块 ⇒ embed 4xx、``vectorized=false``）。
        ⇒ 每个行窗**内部**再过一遍 :func:`_char_windows`（:data:`CODE_WINDOW_CHARS`）。
        **两级顺序不可颠倒**：先行窗（保住「行数 ≤ 80」与重叠口径），后字符窗（判据 = 该行窗
        **拼接总字长** ≤ 上限）。⚠️ 字符窗**不是**只对病理级长行生效 —— **多行行窗拼起来超限
        同样会被切**（口径更正见 :data:`CODE_WINDOW_CHARS` 第 ③ 条与 R-4）。

        ⚠️ 未超行窗的短行段也走 :func:`_window_slices`（它返回单窗 ``(0, total)``，
        等价于旧写法的直接 ``append((a, b))``）—— 一处实现、不另开分支。
        """
        if b < a:
            return
        for sub_a, sub_b in _window_slices(b - a + 1, CODE_WINDOW_LINES, CODE_WINDOW_OVERLAP):
            for la, lb, text in _char_windows(lines, a + sub_a, a + sub_b - 1):
                spans.append((la, lb, name, text))

    tree = None
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        tree = None
    if tree is not None:
        symbols = _top_level_symbol_spans(tree)
        cursor = 1  # 1 起下一行
        for start, end, name in symbols:
            # 符号前的前言/空档：非空就成块（区间本身可能很长 ⇒ 交给 _emit 按窗口切）
            if start > cursor and any(ln.strip() for ln in lines[cursor - 1 : start - 1]):
                _emit(cursor, start - 1, None)
            _emit(start, end, name)
            cursor = max(cursor, end + 1)
        if cursor <= len(lines) and any(ln.strip() for ln in lines[cursor - 1 :]):
            _emit(cursor, len(lines), None)
    else:
        spans = _code_fallback_chunks(lines, symbol_path=None)

    result: list[ChunkMeta] = []
    for a, b, name, raw in spans:
        # 文本由窗口层给（行边界片 = 若干整行拼接；行内片 = 该行的连续子串）
        # ⇒ 这里只做 strip 去空，**不再**按行号重新拼 —— 行内片按行号拼会把整行拼回来（见 R-4）
        piece = raw.strip()
        if not piece:
            continue
        result.append(
            ChunkMeta(
                text=piece,
                ordinal=len(result),
                page=None,
                start_offset=None,
                end_offset=None,
                heading_path=(),
                line_start=a,
                line_end=b,
                symbol_path=name,
            )
        )
    return result

