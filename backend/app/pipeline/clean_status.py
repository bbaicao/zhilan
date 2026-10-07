"""清洗状态（``/api/health`` 与知识库页共用）。

**为什么单独成模块**：``cleaning.py`` 是**纯函数**模块（规则参数、指纹、清洗本身），
全部可无依赖单测；这里要读库（``documents.clean_report``），所以分开放，
不把 IO 混进纯函数模块。

口径照老仓库 ``app/rag/cleaning.py::stale_clean_documents``（只读对照，非整文件搬运）：
两种文档都算「分块不是按当前清洗规则生成的」——**清洗上线前入库**的
（``clean_report`` 为空，该列是 S2 才加的）与**规则改过之后**仍留着旧指纹的。
这里只做**检测与提示**，消除提示靠 reindex（它会按当前规则重新清洗）。

**工具升级-3 · 回归检查（2026-09-26）补一条并列的检测函数**：本模块新增
「读库 + **只读**文件字节」的检测函数 :func:`stale_content_documents`（磁盘内容 vs
库内 ``raw_content``）。**仍不写文件、不引入写通道**——只 ``Path.read_bytes()``，
不做 watcher / 不做后台轮询 / 不做 lifespan 启动扫描；**调用方 = 宿主/用户显式发起；
本阶段无自动调用点**。两条检测函数**并列、互不改写**：前者判「清洗规则过期」，
后者判「磁盘内容过期」，各自的名字与语义都不得互相污染。

**一条必须写明的口径（判据面的如实收窄）**：**上传类文档的 ``file_path`` 是内容寻址
留档副本**（``pipeline/storage.py::save_upload`` 按内容 sha256 前 8 位定名），
磁盘内容过期对其**不适用** ⇒ 一律跳过；**非纯文本类（PDF / DOCX / 图片…）一律跳过**
（它们的库内 ``raw_content`` 是**解析所得文本**，与磁盘字节不同量纲）。
``raw_content`` 对 ``.md`` 是 **frontmatter 剥离后**的正文 ⇒ 带 YAML frontmatter 的
markdown 也跳过。⇒ **不得**据此声称「所有文档都能检测内容过期」。

🔴 **一条必须如实登记的规格后果（工具升级-3 · B 路审计实测）**：本机真实语料**以 ``.md`` 为
绝对多数（约 95%）**，而 `.md` 被**整体**排除在判据面之外 ⇒ **「磁盘内容已变更」这条显式声明在
真实用户语料上大概率不出现**，能生效的主要是**代码 / 配置类（`回归检查` 新增的 29 个后缀）**
与 `.txt`。这不是缺陷，是 `D-回归检查`「判据面收窄」的自然后果；**据此不得写「用户能看见自己的
资料已过期」这类话术**（撞 `D-回归检查` 边界句）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Document
from app.pipeline.cleaning import rule_fingerprint

log = logging.getLogger(__name__)

#: 走「磁盘文本 ↔ 库内 ``raw_content``」对比的**纯文本类**后缀（白名单，非黑名单）。
#: 与 ``parsing._parse_code`` 同口径：这几类后缀在入库时**原文直读**（``clean``
#: 对代码段是 ``enable=False`` 的原文照入），因此「磁盘文本」与「库内 ``raw_content``」
#: 量纲一致、可直接逐字比对。
#: **为什么不含 `.md`**：``raw_content`` 对 markdown 是 **frontmatter 剥离后**的正文字
#: （``ingest.py`` 只对 `md`/`markdown` 走 ``parse_frontmatter``），带 YAML frontmatter
#: 的 `.md` 会**恒判过期**（与内容变没变无关）。
_TEXT_SUFFIXES = frozenset({
    # 代码 16
    ".py", ".js", ".ts", ".java", ".go", ".rs", ".c", ".h", ".cpp", ".cs", ".rb", ".php",
    ".sh", ".sql", ".kt", ".swift",
    # 配置 7
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml",
    # 前端资源 6
    ".jsx", ".tsx", ".vue", ".css", ".scss", ".html",
    # 纯文本 1
    ".txt",
})

def _is_archived(file_path: str) -> bool:
    """``file_path`` 是否落在 ``settings.upload_path`` 之下 = 上传留档副本。

    留档副本按内容寻址定名、**永不被用户改动** ⇒ 对它做「磁盘内容过期」判定是**空判据面**
    （恒不成立），故一律跳过。路径比对**不做 resolve**（留档路径是产物、不与工作区交叉）。
    """
    try:
        candidate = Path(file_path)
        base = Path(settings.upload_path)
    except (TypeError, ValueError):  # pragma: no cover —— 路径不可构造：按「可判」处理（fail-closed 方向）
        return False
    return candidate == base or base in candidate.parents


def _read_disk_text(file_path: str, doc_id: int) -> str | None:
    """按**入库时的同一编码探测**读回磁盘文本；取不到一律回 ``None``（由调用方 fail-closed）。

    编码探测复用 ``parsing.detect_encoding`` 的严格文本准入。
    文件已删、无权限、IO 失败或明确非文本均返回 ``None``，不能替换乱码后假装可读。

    🔴 **返回前剥「至多一个」BOM —— 与 `parsing._parse_code` 逐字对齐**（`回归修复`）：
    库内 `raw_content` 是 `_parse_code` 的产物，它解码后**同样只剥一个** `\ufeff`
    （`parsing.py:167-171`）；而 `detect_encoding` 把 `utf-8` 排在 `utf-8-sig` **之前**
    （`parsing.py:143`；其 docstring `:137-138` 自己写明「`\ufeff` 前缀，调用方按需剥掉一个」）
    ⇒ 这里**不剥**的话，磁盘侧会比库侧**多一个** `\ufeff` 前缀 ⇒ :func:`stale_content_documents`
    的逐字比较**恒不相等** ⇒ 带 BOM 的代码 / 配置文件**永远**判「磁盘内容已变更」，
    且 `reindex` 重走 `_parse_code` 也清不掉（重洗一遍 `raw_content` 仍无 BOM）。

    **为什么是「至多一个」而不是「剥光」**：口径必须与 `_parse_code` **同源** ——
    它只剥一个，这里就只剥一个；**两个** BOM 的文件两侧各留一个 ⇒ 逐字比较仍相等 ⇒ 仍判不过期
    （若哪天 `_parse_code` 改成剥光，这里必须同步改，否则两侧口径又会分叉）。

    **`.md` 排除口径一字不动**：本函数**不碰** `_TEXT_SUFFIXES`（`.md` 仍被整体排除 ——
    理由是 `raw_content` 对 md 是 frontmatter 剥离后的正文，见该常量处的注释）；
    本笔只补「剥一个 BOM」，**不**放宽后缀面。

    🔴 **只对已进判据面的文档调用**（调用方先过 :func:`_is_judgeable`）—— 故本函数的
    「读取失败」日志**不会撒谎**：它只在**真的会据此判过期**时才打；留档 / 非纯文本类
    文档根本到不了这里，不会留下与结论相反的日志。
    """
    if not file_path:
        return None
    try:
        data = Path(file_path).read_bytes()
    except OSError as exc:
        # 文件已删 / 无权限 / 读失败 —— **不吞成「未过期」**，交回调用方按 fail-closed 判。
        log.warning("磁盘内容过期检测：读取失败（文档 %s 判为过期）：%s：%s", doc_id, file_path, exc)
        return None
    from app.pipeline.parsing import detect_encoding

    from app.core.errors import DocumentParseError
    try:
        text = data.decode(detect_encoding(data))
    except DocumentParseError:
        log.warning("磁盘内容过期检测：文档 %s 的留档不能按文本读取。", doc_id)
        return None
    # 与 `parsing._parse_code` 同源：**至多剥一个** BOM（理由见本函数 docstring 的 回归修复 段）。
    # 只剥一个 ⇒ 两个 BOM 的文件两侧各留一个 ⇒ 逐字比较仍相等（判不过期）。
    if text.startswith("\ufeff"):
        text = text[1:]
    return text


def _is_judgeable(doc: Document) -> bool:
    """该文档是否进「磁盘内容过期」的**判据面**（可判 ⇒ True）。**纯判定，不读文件内容。**

    三条**全部**满足才可判：① 有 ``file_path`` 且**不是**上传留档副本；
    ② 后缀在纯文本白名单 ``_TEXT_SUFFIXES`` 内。

    🔴 **为什么这里不读文件**：本函数是**判据面的入口闸** —— 它先于任何 ``read_bytes`` 执行，
    非可判文档（留档 / 非纯文本）**一个字节都不读**（B 路审计实测：本机开发库 80 篇里 78 篇
    `.md` 会被整体跳过，原写法却先 ``read_bytes`` 了一遍 ⇒ 纯浪费）。
    「文件读不到」不在这里判 —— 那是 **fail-closed**，由 :func:`stale_content_documents`
    在 ``_read_disk_text`` 回 ``None`` 且本函数为真时**判过期**。

    ⚠️ `.md`/`.markdown` 由 ``_TEXT_SUFFIXES`` **整体排除**（``K-回归检查``：``raw_content``
    对 md 是 frontmatter 剥离后的 body ⇒ 带 YAML frontmatter 的 md 会恒判过期）
    ⇒ **此处无需**再单列 frontmatter 分支。
    """
    file_path = doc.file_path or ""
    if not file_path or _is_archived(file_path):
        return False
    return Path(file_path).suffix.lower() in _TEXT_SUFFIXES


async def stale_clean_documents(session: AsyncSession) -> int:
    """统计「分块不是按当前清洗规则生成」的文档数。

    回归修复（2026-09-27）：统计**与重建目标同口径排除回收站**（``deleted_at IS NULL``，
    对齐 ``reindex._load_targets`` 的同一过滤）。理由：重建目标不含回收站文档 ⇒ 它们的
    指纹过期**永远清不掉**，算进「建议重建」只会造出一条「建议 → 重建 → 仍在」的
    无出口循环，而该文档在知识库列表不可见、用户无从自查。
    """
    current = rule_fingerprint()
    rows = (
        await session.execute(
            select(Document.clean_report).where(Document.deleted_at.is_(None))
        )
    ).scalars().all()
    stale = 0
    for report in rows:
        if not isinstance(report, dict):
            # 清洗功能上线前的历史文档：没有报告，同样需要重建
            stale += 1
            continue
        if report.get("rule_fingerprint") != current:
            stale += 1
    return stale


async def stale_content_documents(session: AsyncSession) -> list[int]:
    """**磁盘内容已变更**的文档 id 列表（工具升级-3 · 回归检查 · ``A10-2`` 的检测入口）。

    **调用契约**：**调用方 = 宿主/用户显式发起**（吃**显式注入**的 ``session``）；**本阶段无
    自动调用点** —— 不做文件系统 watcher、不做后台轮询、不做 lifespan 启动扫描。

    **只读**：只 ``read_bytes()`` 磁盘文件与 ``select`` 库内行，**不写**任何文件、
    **不**commit、**不**触发重建（单⑤ 取 (b)：打标与重建**两步分离**）。

    **判据面（如实收窄）**：仅对「非留档（``file_path`` 不落在 ``settings.upload_path`` 之下）
    ∩ 纯文本类（``_TEXT_SUFFIXES``）」的文档生效；比对 = **按入库时的同一编码探测**
    读出的磁盘文本 ↔ 库内 ``Document.raw_content``。PDF / DOCX / 图片等**一律跳过**。

    **fail-closed**：文件已删 / 无权限 / 读失败 ⇒ **判为过期**（不得静默当未过期）。

    **空集合语义**：零文档 / 零可判文档 ⇒ 返回 ``[]``，**不抛错**。

    ⚠️ **如实登记的覆盖面后果**：本机真实语料以 ``.md`` 为绝对多数（约 95%），而 `.md` 被判据面
    整体排除 ⇒ 本条声明在真实用户语料上**大概率不出现**（实测覆盖主要落在代码 / 配置类与 `.txt`）。
    **不得**据此声称「用户能看见自己的资料已过期」（`D-回归检查` 边界句）。

    **不与其他检测混用**：本函数**只**管「磁盘内容过期」；「清洗规则过期」由
    :func:`stale_clean_documents` 独立统计 —— 两者**互不改写**（``M-回归检查`` 打的正是
    「把规则过期统计也拿磁盘过期污染」这条）。

    回归修复（2026-09-27）：与 :func:`stale_clean_documents` 同一笔——统计**排除回收站**
    （``deleted_at IS NULL``，对齐重建目标）：回收站文档磁盘再怎么变，重建也碰不到它们。
    """
    rows = (
        await session.execute(
            select(Document).where(Document.deleted_at.is_(None))
        )
    ).scalars().all()
    stale_ids: list[int] = []
    for doc in rows:
        # 🔴 **先过判据面入口闸，再读盘**：非可判文档（留档 / 非纯文本）**零 IO**。
        if not _is_judgeable(doc):
            continue
        disk_text = _read_disk_text(doc.file_path or "", doc.id)
        if disk_text is None:
            # 进了判据面却读不到（已删 / 无权限 / 读失败）⇒ **fail-closed 判过期**
            stale_ids.append(doc.id)
            continue
        if disk_text != (doc.raw_content or ""):
            stale_ids.append(doc.id)
    return stale_ids


async def clean_status(session: AsyncSession) -> dict:
    """给 ``/api/health`` 与知识库页用的清洗状态（键名与前端 ``CleanStatus`` 对齐）。

    **取 (甲)**（``D-回归检查`` 定档）：**不新增键、不改键集** —— 磁盘内容过期的结果**汇进既有
    ``needs_reindex`` 的「或」语义，``hint`` 按两种原因**分列**：
    · **仅规则过期** ⇒ 保持既有句**原样、逐字不改**（腿③：既有读数逐字不变）；
    · **仅磁盘内容过期** ⇒ ``有 {n} 篇文档的磁盘内容已变更，建议重建索引。``；
    · **两者皆有** ⇒ 两句并列（先规则、后磁盘，逗号连接）。
    """
    stale = await stale_clean_documents(session)
    content_stale = len(await stale_content_documents(session))

    hints: list[str] = []
    if stale:
        hints.append(f"有 {stale} 篇文档还不是按当前清洗规则生成的，建议重建索引。")
    if content_stale:
        hints.append(f"有 {content_stale} 篇文档的磁盘内容已变更，建议重建索引。")

    return {
        "enabled": bool(settings.CLEAN_ENABLE),
        "rule_fingerprint": rule_fingerprint(),
        "stale_documents": stale,
        "needs_reindex": bool(stale or content_stale),
        # 不提「规则已变更」——对老文档那句话不成立；点「重建索引」会真的重新清洗。
        "hint": "，".join(hints),
    }


__all__ = ["clean_status", "stale_clean_documents", "stale_content_documents"]
