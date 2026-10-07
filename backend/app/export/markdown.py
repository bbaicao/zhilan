"""全库 Markdown 导出（条目 17）的真正实现：笔记 + 文档原文 → 本机文件夹里的 Markdown。

**为什么 v2 要新建这个模块**（本轮实测）：``POST /api/export/markdown`` 在 v2 里只有
501 骨架，**没有地基模块**——老基线的实现落在 ``app/rag/export.py``，而 v2 没有
``app/rag`` 这一层。本模块是等价实现（口径逐条对照老基线，差异见下）。

**相对老基线的三处有意差异**（都写在 ``证据/回归检查4口径.md``）：

1. **收会话**（``db``）而不是自开 ``SessionLocal`` —— 与 v2 全仓「会话由调用方传入」
   的口径一致，单测里因此读的是**测试库**，不会把开发库/真实目录当导出源；
2. **相对路径按** :data:`app.core.env_store.ROOT_DIR` **解析** —— 老基线是
   ``ROOT_DIR / "backend" / path``（它当时的 ``ROOT_DIR`` 是仓库根、代码在 ``backend/`` 下）；
   v2 的 ``ROOT_DIR`` 就是仓库根，再插一层 ``backend`` 会指到不存在的位置；
3. **标签维度在 Python 侧收口** —— 老基线用 MySQL 专用的 ``JSON_CONTAINS``，
   v2 要同时支持 SQLite（桌面态），故与 ``app/search/service.py`` 同款做法：
   标签用应用层判定，其余维度仍下推 SQL。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.env_store import ROOT_DIR
from app.models import Chunk, Document, Note, active_chunks
from app.search.query import parse_date_span

#: 空分类的文件夹名（老基线逐字）。
UNCATEGORIZED = "未分类"
#: 写权限探针文件名（写完即删；参考数据目录的做法）。
_PROBE_NAME = ".zhilan-export-probe"
#: Windows 文件名非法字符（含控制字符），统一换成下划线。
_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
#: Windows 保留设备名：单独出现时不能当文件 / 目录名。
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
#: 标题 / 文件名最长 255，留足目录层级与 ``-2`` 后缀的余量。
_STEM_MAX = 120


class ExportPathError(ValueError):
    """导出目标不可用：消息直接给界面（400 + 可读中文），不静默。"""


class ExportScopeError(ValueError):
    """范围导出条件不合法 / 范围内没有可导出的内容：消息直接给界面，不静默。"""


@dataclass
class ExportResult:
    """一次导出的结果：输出目录、各类型数量与逐条失败原因（字段 = 冻结前端 ExportMarkdownOut）。"""

    output_dir: str
    notes_exported: int = 0
    documents_exported: int = 0
    errors: list[str] = field(default_factory=list)


def _is_reserved_name(name: str) -> bool:
    """是否命中 Windows 设备保留名（CON / NUL / COM1…，大小写不敏感）。

    判定口径 = **去扩展名后的 stem**（"CON.txt" 在 Windows 上仍是设备名），且必须
    **整段相等**而非前缀匹配（"CONS" / "nul2" 都是合法普通名字）。rstrip(" ") 兜住
    "CON .txt" 这类 Windows 也会折叠成设备名的写法。
    """
    stem = name.split(".", 1)[0].rstrip(" ")
    return stem.upper() in _RESERVED


def sanitize_name(text: str) -> str:
    """目录名 / 文件名清洗：非法字符换下划线，保留名加前缀，结尾点与空格去掉。"""
    cleaned = _ILLEGAL_RE.sub("_", (text or "").strip())
    # 先去掉结尾的点与空格、**再**判保留名："NUL." / "nul.." 经 Windows 归一化后就是设备名
    # NUL（落档时设备名已生效 ⇒ 计数成功但文件不可用 / 进 errors[]），改前「先判后 rstrip」
    # 正好漏掉这一族形态。
    cleaned = cleaned.rstrip(". ")
    if not cleaned:
        return ""
    if _is_reserved_name(cleaned):
        cleaned = f"_{cleaned}"
    return cleaned


#: 需要加引号（JSON 转义）的字符集合：YAML 结构性字符 + **控制字符 / 行终止符 / 非字符**。
#:
#: - 结构性字符 ``: # [ ] { } " ' \ ,`` 会破坏 plain scalar 的语法；
#: - ``\x00-\x1f``：C0，**含 TAB(0x09) / CR(0x0d) / LF(0x0a)**；
#: - ``\x7f-\x9f``：DEL + C1（含 **NEL = U+0085**，YAML 1.1 的行终止符之一）；
#: - ``\u2028`` / ``\u2029``：LS / PS（YAML 1.1 行终止符）；
#: - ``\ufffe`` / ``\uffff``：非字符（PyYAML 读入期直接拒）。
#:
#: ⚠️ **这份清单是实测枚举出来的，不是拍脑袋**：本机探针 ``probe136_sweep.py`` 对
#: **BMP 全扫 63,488 码点 + 星体面抽样 10,811 码点**，逐个找「``_yaml_scalar`` 不给它加引号、
#: 而 ``yaml.safe_load`` 报错」的字符 ⇒ 实测**恰好 67 个**，与本类**逐一对上**：
#: U+0000–U+0008 / U+0009(TAB) / U+000B / U+000C / U+000E–U+001F / U+007F / U+0080–U+009F /
#: U+2028 / U+2029 / U+FFFE / U+FFFF。（U+000A 与 U+000D 本就被旧判据的 ``\n`` 与
#: ``text != text.strip()`` 兜住；星体面 0 个失败。）
_YAML_QUOTE_RE = re.compile(r'[:#\[\]{}"\'\\,\x00-\x1f\x7f-\x9f\u2028\u2029\ufffe\uffff]')

#: 上面那批里 **``json.dumps(ensure_ascii=False)`` 不会转义**的子集（DEL / C1 / LS / PS / 非字符）
#: ⇒ 命中它们时改走 ``ensure_ascii=True``（把非 ASCII 一并写成 ``\uXXXX`` 转义）。
#: **实测依据**（把这些 raw 字符直接落进双引号内会怎样，``probe136_quotes.py``）：
#: ``DEL`` / ``C1`` / ``U+FFFE`` ⇒ ``ReaderError``（整块解析失败）；``NEL`` ⇒ 解析"成功"但
#: **值被折成空格**（``"a\x85b"`` 读回 ``'a b'`` —— 静默错值，比报错更坏）；
#: ``LS`` / ``PS`` ⇒ 本机 PyYAML 在引号标量内能原样往返（转义**不是必需、是防御**：YAML 1.1
#: 把它们列为行终止符 ⇒ 不留版本依赖）。⇒ 这四类一律走 ASCII 转义。
_YAML_ASCII_ESCAPE_RE = re.compile(r"[\x7f-\x9f\u2028\u2029\ufffe\uffff]")


def _yaml_scalar(text: str) -> str:
    """手写 YAML 标量：含特殊字符时用双引号包住（JSON 转义），``safe_load`` 能原样读回。

    🔴 **回归修复（就地修）**：判据原来只有 ``\n``、**没有** ``\r``
    ⇒ **中间**带裸 CR 的值（``"标题\r副标题"``；API 建笔记**不做 CR 过滤**）会**不加引号**
    原样写进 ``title:`` 行，而 YAML 把孤立 CR 当**行分隔**（YAML 1.1 的行终止符 =
    ``\n`` / ``\r\n`` / ``\r``）⇒ ``safe_load`` 侧劈行、元数据解析错乱或整块失败。
    ⚠️ **只有「中间 CR」在窗外**：首 / 尾 CR 本来就被 ``text != text.strip()`` 兜住。

    🔴 **审计加固（136-1，就地扩面）**：同一条实测暴露的**同族残余窗口** —— TAB / 其余 C0 /
    DEL / C1 / NEL / LS / PS / U+FFFE-FFFF **同样**会让 ``safe_load`` 报错（TAB 与 NEL/LS/PS 是
    ``ScannerError``，其余是 ``ReaderError``），而产品自己的解析器
    （``pipeline/frontmatter.py:79`` 的 ``yaml.safe_load``）也吃这一套 ⇒ 导出后再入库会让
    **整块元数据解析失败**（category / tags / created_at 全丢）。``NoteIn.title`` 没有字符白名单
    ⇒ 中间带 TAB 的标题可经 API 落库。
    ⇒ 判据不再逐字列 ``\n`` / ``\r``，改为**一条覆盖全类**的字符类（:data:`_YAML_QUOTE_RE`，
    清单与实测依据见其注释），并对 ``json.dumps(ensure_ascii=False)`` 不转义的那一子集
    （:data:`_YAML_ASCII_ESCAPE_RE`）改走 ``ensure_ascii=True``。

    ✅ **已覆盖**（改后探针全扫：BMP 63,488 + 星体面抽样 10,811 ⇒ **0 个失败**）：
    YAML 结构性字符 + 全部 C0（含 TAB / CR / LF）+ DEL + 全部 C1（含 NEL）+ LS + PS + U+FFFE/FFFF。

    ⚠️ **未覆盖 / 不做（如实登记，不得写「任何字符都能原样读回」）**：
    ① **代理区 U+D800–U+DFFF**：Python ``str`` 可以携带，但 ``write_text(encoding="utf-8")``
       会先抛 ``UnicodeEncodeError``（根本写不到文件）⇒ 不在本判据面内；
    ② **非 BMP 的非字符**（U+1FFFE / U+1FFFF … 每平面末两码）：PyYAML 读入期**不拒**
       （实测星体面抽样 0 失败）⇒ 不纳入；
    ③ **值形态歧义（本条不做）**：plain scalar 的类型歧义是**另一根轴** —— ``category: 123`` /
       ``true`` / ``2026-01-01`` 会被 ``safe_load`` 读成 int / bool / date ⇒ 元数据取回时
       **类型不对**（下游判「应是文本」而丢弃）。这与本函数「转义」的职责不同（要修得改成
       「凡形态像别的类型就加引号」），**本条不扩面**，已登记供裁定（载体
       ``test_value_shape_ambiguity_is_registered`` 钉住现状、不假装已修）。
    """
    if not text or _YAML_QUOTE_RE.search(text) or text != text.strip():
        if _YAML_ASCII_ESCAPE_RE.search(text):
            return json.dumps(text, ensure_ascii=True)
        return json.dumps(text, ensure_ascii=False)
    return text


def _frontmatter(lines: list[tuple[str, str]]) -> str:
    body = "\n".join(f"{key}: {value}" for key, value in lines)
    return f"---\n{body}\n---\n"


def _tags_of(value: object) -> list[str]:
    """``tags`` 列历史上有列表 / 字典两种形态，统一成字符串列表。"""
    if isinstance(value, dict):
        return [str(key) for key in value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item is not None]
    return []


def _note_text(note: Note) -> str:
    """笔记的导出文本：frontmatter + 正文原样（正文紧跟闭合 ``---``，再导入时逐字读回）。"""
    fields = [
        ("title", _yaml_scalar(note.title or "")),
        ("category", _yaml_scalar(note.category or "")),
    ]
    tags = _tags_of(note.tags)
    if tags:
        fields.append(("tags", "[" + ", ".join(_yaml_scalar(tag) for tag in tags) + "]"))
    if note.created_at is not None:
        fields.append(("created_at", note.created_at.strftime("%Y-%m-%d")))
    if note.source_document_id is not None:
        fields.append(("source_document_id", str(int(note.source_document_id))))
    return _frontmatter(fields) + (note.content or "")


def _document_text(document: Document, content: str) -> str:
    """文档的导出文本：frontmatter（``source_file`` 记录原始文件名）+ 可读文本。"""
    stem = Path(document.filename or "").stem
    fields = [
        ("title", _yaml_scalar(stem)),
        ("category", _yaml_scalar(document.category or "")),
        ("source_file", _yaml_scalar(document.filename or "")),
    ]
    tags = _tags_of(document.tags)
    if tags:
        fields.append(("tags", "[" + ", ".join(_yaml_scalar(tag) for tag in tags) + "]"))
    if document.created_at is not None:
        fields.append(("created_at", document.created_at.strftime("%Y-%m-%d")))
    return _frontmatter(fields) + content


def _stem(text: str) -> str:
    return sanitize_name(text)[:_STEM_MAX] or "未命名"


def _category_dir(parent: Path, category: str) -> Path:
    return parent / (sanitize_name(category)[:_STEM_MAX] or UNCATEGORIZED)


def _unique(directory: Path, stem: str) -> Path:
    """同目录同 stem 冲突时加 ``-2``、``-3`` 后缀（输出目录是新建的，exists 即冲突）。"""
    candidate = directory / f"{stem}.md"
    suffix = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{suffix}.md"
        suffix += 1
    return candidate


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _check_writable(path: Path) -> None:
    """路径真实性判据：真的写一个文件再删掉（os.access 在 Windows 上对「存在但不可写」不可靠）。

    - 探针名带 uuid4 后缀 ⇒ 与用户同名文件 / 并发导出互不踩踏；
    - O_CREAT|O_EXCL 独占创建 ⇒ 绝不覆盖、绝不删除别人的东西；
    - 撞名（FileExistsError）或同名项是目录 ⇒ **放行**：写权限已由别的进程 / 已有目录证明，
      此时不建不删（是否清理由 created 标志控制，只删自己造的那个文件）；
    - 清理放 finally ⇒ 写入失败（磁盘满 / 配额）也必须不留残骸。
    """
    probe = path / f"{_PROBE_NAME}-{uuid4().hex}"
    if probe.is_dir():
        return
    created = False
    try:
        try:
            fd = os.open(str(probe), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return
        created = True
        try:
            os.write(fd, b"ok")
        finally:
            os.close(fd)
    except OSError:
        raise ExportPathError(f"导出目录 {path} 不可写，请检查权限或换一个位置。") from None
    finally:
        if created:
            try:
                probe.unlink()
            except OSError:
                pass


def resolve_root(root_path: str | Path) -> Path:
    """校验导出根目录：不存在 / 是文件 / 不可写都抛可读中文。

    相对路径按 :data:`ROOT_DIR`（仓库根 / 安装目录）解析——见模块 docstring 的差异 2。
    写探针是必要的：只判断 ``os.access`` 在 Windows 上对「目录存在但不可写」并不可靠，
    真的写一个文件再删掉才是实测；具体做法与边界见 _check_writable 的 docstring。
    """
    text = str(root_path or "").strip().strip('"').strip("'")
    if not text:
        raise ExportPathError("导出目录不能为空，请先填写或选择一个文件夹。")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = ROOT_DIR / path
    if not path.exists():
        raise ExportPathError(f"导出目录不存在：{path}，请先选择一个已存在的文件夹。")
    if not path.is_dir():
        raise ExportPathError(f"导出位置 {path} 是一个文件，不能当作导出目录，请选择文件夹。")
    _check_writable(path)
    return path


def _make_output_dir(root: Path) -> Path:
    """带时间戳的导出子目录；同一秒内导第二次时加 ``-2`` 后缀，绝不写进已有目录。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = root / f"知澜导出-{stamp}"
    suffix = 2
    while candidate.exists():
        candidate = root / f"知澜导出-{stamp}-{suffix}"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


@dataclass
class _Scope:
    """范围导出的筛选条件：各维度之间取交集，空维度不过滤。"""

    categories: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    date_start: datetime | None = None
    date_end: datetime | None = None
    note_ids: list[int] = field(default_factory=list)
    document_ids: list[int] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """所有维度都没给 = 全库（错误提示里要说清这一点）。"""
        return not (
            self.categories
            or self.tags
            or self.note_ids
            or self.document_ids
            or self.date_start is not None
            or self.date_end is not None
        )


def _clean_names(values: list[str] | None, *, keep_empty: bool) -> list[str]:
    """先 strip 再按首次出现顺序去重；``keep_empty=False`` 时丢弃空串。

    categories 的 ``""`` 表示「未分类」，要保留；tags 的空串没有意义，丢掉。
    """
    cleaned: list[str] = []
    for item in values or []:
        text = (item or "").strip()
        if not text and not keep_empty:
            continue
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def _clean_ids(values: list[int] | None) -> list[int]:
    """自选条目 id 去重（保序）。"""
    ids: list[int] = []
    for item in values or []:
        if item not in ids:
            ids.append(item)
    return ids


def _parse_scope_dates(date_from: str | None, date_to: str | None) -> tuple[datetime | None, datetime | None]:
    """``date_from`` 取区间起点、``date_to`` 取右开终点（止端含末日，与 ``date:`` 操作符同口径）。"""
    start: datetime | None = None
    end: datetime | None = None
    from_text = (date_from or "").strip()
    to_text = (date_to or "").strip()
    if from_text:
        span = parse_date_span(from_text)
        if span is None or span[0] is None:
            raise ExportScopeError(f"无法识别的日期「{from_text}」，支持 2026 / 2026-08 / 2026-08-01。")
        start = span[0]
    if to_text:
        span = parse_date_span(to_text)
        if span is None or span[1] is None:
            raise ExportScopeError(f"无法识别的日期「{to_text}」，支持 2026 / 2026-08 / 2026-08-01。")
        end = span[1]
    if start is not None and end is not None and start >= end:
        raise ExportScopeError("日期范围无效：开始日期不能晚于结束日期。")
    return start, end


def _build_scope(
    categories: list[str] | None,
    tags: list[str] | None,
    date_from: str | None,
    date_to: str | None,
    note_ids: list[int] | None,
    document_ids: list[int] | None,
) -> _Scope:
    date_start, date_end = _parse_scope_dates(date_from, date_to)
    return _Scope(
        categories=_clean_names(categories, keep_empty=True),
        tags=_clean_names(tags, keep_empty=False),
        date_start=date_start,
        date_end=date_end,
        note_ids=_clean_ids(note_ids),
        document_ids=_clean_ids(document_ids),
    )


def _scope_clauses(model, ids: list[int], scope: _Scope) -> list:
    """单个模型的**下推 SQL** 范围条件（标签维度不走这里，见 :func:`_matches_tags`）。"""
    clauses: list = []
    if scope.categories:
        clauses.append(model.category.in_(scope.categories))
    if scope.date_start is not None:
        clauses.append(model.created_at >= scope.date_start)
    if scope.date_end is not None:
        clauses.append(model.created_at < scope.date_end)  # 右开区间，止端含末日
    if ids:
        clauses.append(model.id.in_(ids))
    return clauses


def _matches_tags(tags_value: object, wanted: list[str]) -> bool:
    """标签维度（应用层判定，两方言一致）：命中**任一**所选标签即算命中。"""
    if not wanted:
        return True
    have = set(_tags_of(tags_value))
    return any(tag in have for tag in wanted)


def _scope_text(scope: _Scope) -> str:
    """范围的人话描述，拼进空范围的报错信息里。"""
    if scope.is_empty:
        return "全部内容"
    parts: list[str] = []
    if scope.categories:
        parts.append("分类：" + "、".join(item or UNCATEGORIZED for item in scope.categories))
    if scope.tags:
        parts.append("标签：" + "、".join(scope.tags))
    if scope.date_start is not None or scope.date_end is not None:
        left = scope.date_start.strftime("%Y-%m-%d") if scope.date_start else ""
        # 右开终点换算回「含末日」的日期再展示
        right = (scope.date_end - timedelta(days=1)).strftime("%Y-%m-%d") if scope.date_end else ""
        if left and right:
            parts.append(f"日期：{left} ~ {right}")
        elif left:
            parts.append(f"日期：{left} 之后")
        else:
            parts.append(f"日期：{right} 之前")
    if scope.note_ids:
        parts.append("笔记 id：" + "、".join(str(item) for item in scope.note_ids))
    if scope.document_ids:
        parts.append("文档 id：" + "、".join(str(item) for item in scope.document_ids))
    return "；".join(parts)


async def _load_notes(db: AsyncSession, scope: _Scope) -> list[Note]:
    """范围内（活库）的笔记行：下推 SQL 的部分走查询，标签在 Python 侧收口。"""
    stmt = (
        select(Note)
        .where(Note.deleted_at.is_(None), *_scope_clauses(Note, scope.note_ids, scope))
        .order_by(Note.id)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return [row for row in rows if _matches_tags(row.tags, scope.tags)]


async def _load_documents(db: AsyncSession, scope: _Scope) -> list[Document]:
    """范围内（活库）的文档行（同上）。"""
    stmt = (
        select(Document)
        .where(
            Document.deleted_at.is_(None),
            *_scope_clauses(Document, scope.document_ids, scope),
        )
        .order_by(Document.id)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return [row for row in rows if _matches_tags(row.tags, scope.tags)]


async def _load_chunks(db: AsyncSession, document_ids: list[int]) -> dict[int, list[str]]:
    """文档当前内容版本的分块正文（按 ``ordinal`` 升序）：只在 ``raw_content`` 为空时兜底用。"""
    if not document_ids:
        return {}
    rows = (
        await db.execute(
            select(Chunk.document_id, Chunk.content)
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.document_id.in_(document_ids), active_chunks())
            .order_by(Chunk.document_id, Chunk.ordinal)
        )
    ).all()
    chunks: dict[int, list[str]] = {}
    for document_id, content in rows:
        chunks.setdefault(int(document_id), []).append(content or "")
    return chunks


async def export_markdown(
    db: AsyncSession,
    root_path: str | Path,
    include_notes: bool = True,
    include_documents: bool = True,
    *,
    categories: list[str] | None = None,
    tags: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    note_ids: list[int] | None = None,
    document_ids: list[int] | None = None,
) -> ExportResult:
    """导出为 Markdown：不传范围参数时整库，给了范围则各维度取交集只导命中的条目；
    范围内没有任何可导出内容时抛 :class:`ExportScopeError`，**绝不创建空目录**。

    ``include_notes`` / ``include_documents`` **至少勾一个**：两个都 False 抛
    :class:`ExportPathError`（契约 §4：400 + 可读中文）。
    """
    if not include_notes and not include_documents:
        raise ExportPathError("请至少勾选「包含笔记」或「包含文档」中的一项。")
    root = resolve_root(root_path)
    scope = _build_scope(categories, tags, date_from, date_to, note_ids, document_ids)

    notes = await _load_notes(db, scope) if include_notes else []
    documents = await _load_documents(db, scope) if include_documents else []
    # 空范围在建输出目录之前就拦下：绝不留一个只有时间戳名的空目录
    if not notes and not documents:
        raise ExportScopeError(
            f"所选范围内没有可导出的内容（{_scope_text(scope)}），请调整范围或改回「全部」。"
        )

    out_dir = _make_output_dir(root)
    result = ExportResult(output_dir=str(out_dir))

    if include_notes:
        notes_dir = out_dir / "notes"
        for note in notes:
            try:
                category_dir = _category_dir(notes_dir, note.category or "")
                category_dir.mkdir(parents=True, exist_ok=True)
                target = _unique(category_dir, _stem(note.title or ""))
                _write_text(target, _note_text(note))
                result.notes_exported += 1
            except OSError as exc:
                result.errors.append(f"笔记「{note.title}」写入失败：{exc}")

    if include_documents:
        chunks = await _load_chunks(db, [int(item.id) for item in documents])
        documents_dir = out_dir / "documents"
        for document in documents:
            try:
                raw = document.raw_content or ""
                text = raw if raw.strip() else "\n\n".join(chunks.get(int(document.id), []))
                if not text.strip():
                    result.errors.append(
                        f"文档「{document.filename}」没有可导出的文本（原文与分块均为空），已跳过。"
                    )
                    continue
                category_dir = _category_dir(documents_dir, document.category or "")
                category_dir.mkdir(parents=True, exist_ok=True)
                stem = _stem(Path(document.filename or "").stem)
                target = _unique(category_dir, stem)
                _write_text(target, _document_text(document, text))
                result.documents_exported += 1
            except OSError as exc:
                result.errors.append(f"文档「{document.filename}」写入失败：{exc}")

    return result


__all__ = [
    "UNCATEGORIZED",
    "ExportPathError",
    "ExportResult",
    "ExportScopeError",
    "export_markdown",
    "resolve_root",
    "sanitize_name",
]
