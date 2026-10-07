"""`回归修复` 的判据载体 —— 带 BOM 的代码 / 配置文件**不得**恒判「磁盘内容已变更」。

**BOM 解码差异**：
`parsing._parse_code`（`parsing.py:167-171`）解码后**剥一个 BOM** ⇒ 库内 `raw_content`
**无** `\ufeff` 前缀；而 `clean_status._read_disk_text`（`clean_status.py:100-102`）
只 `data.decode(detect_encoding(data), errors="replace")`、**不剥** —— 因为
`detect_encoding` 的探测顺序是 `utf-8` → `utf-8-sig`（`parsing.py:143`），
BOM 会被 **`utf-8` 成功解码**成 `\ufeff` 前缀（`parsing.py:137-138` 自己写明了这个口径：
「`\ufeff` 前缀，调用方按需剥掉一个」）⇒ `clean_status.py:195` 的逐字比较
`disk_text != doc.raw_content` **恒真** ⇒ 该文档**永远**在「磁盘内容已变更」集合里，
且 `reindex` 重走 `_parse_code` ⇒ `raw_content` 仍无 BOM ⇒ 提示**永久**存在。

**修法（最小改动）**：`_read_disk_text` 返回值处补剥**至多一个** BOM，与 `_parse_code`
**逐字对齐**。本文件是它的判据。

**🔴 取证方式（照 `test_stale_marking_t3_4.py` 的 `D-回归检查`/`V2-B1` 栏，不得改）**：
判据一律走「**`db` fixture 注入 + 直调检测函数**（`stale_content_documents` /
`clean_status`）」。**不得**用 `ASGITransport` 打 `/api/health` —— `health()`
自己 `SessionLocal()`，读的是**开发真库**（`.env` 的 `MYSQL_DATABASE`）而不是测试库
`zhilan_v2_test` ⇒ 本套四腿会**全部失去判别力**（恒 false / 假绿）。

**四条腿（缺一不可）**：
① **主腿**：带 UTF-8 BOM 的 `.py` 入库后**磁盘未动** ⇒ `stale_content_documents(db) == []`
   （**修前必红、修后必绿**），并加一条 `clean_status()` 面断言（`needs_reindex` /
   `hint` 不得出现「磁盘内容已变更」）；
② **负控**：**无 BOM** 的同型文件入库后同样不进过期集合
   （防「恒过期」被反向修成「恒不过期」）；
③ **真改内容仍判过期**：把 BOM 文件内容真改一行 ⇒ 过期集合**包含**它
   （防修成「只要读得到就不过期」的假绿）；
④ **边界**：文件以**两个** BOM 开头 ⇒ 与产品口径一致（`_parse_code` 只剥一个 ⇒
   磁盘侧也只剥一个 ⇒ 应判**不过期**）；**以实测为准**，实测事实写在腿④ 内。
⑤ **超集负控**（Lead 复核 B 路 REAL-2 补）：磁盘文件 = 库内正文 **+ 尾部追加一行**
   （盘 ⊃ 库、且盘仍是合法 UTF-8）⇒ **必须判过期** —— 防「把逐字比较改宽成包含关系
   （@@raw_content in disk_text@@）」这类变异：那种改法腿①–④ **全绿**（本腿**真红**）。

**🔴 每条腿都有独立锚**：不借用产品模块的 `_is_judgeable` 来证明「我造的样本落在判据面内」，
而是照 `test_stale_marking_t3_4.py::_independent_judgeable` 的先例**自行重算**一遍；
「真过期 / 真不过期」也由本文件自己的字节级实现（`_independent_disk_text`）重算，
不只信产品函数的返回值。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models import Document
from app.pipeline.clean_status import (
    clean_status,
    stale_content_documents,
)
from app.pipeline.parsing import _parse_code, detect_encoding

# 必须显式 import 夹具（照既有先例 test_stale_marking_t3_4.py:45 / test_documents_endpoints.py:21）。
# 它在 test_chat_contract.py:73-74 里定义，不在 tests/api/conftest.py
# => 不会自动对本文件可见（那枚 conftest 有 autouse 钉子，但没有 client 夹具）。
# 漏掉本行 => 全部用例 "fixture 'client' not found"（--collect-only 结构上发现不了）。
from tests.api.test_chat_contract import client  # noqa: F401 —— 夹具再导出（非未用 import）

#: UTF-8 BOM 的字节形态（**判据侧独立副本**，刻意不 import 产品常量 —— 本仓无此常量）。
BOM = b"\xef\xbb\xbf"

#: 磁盘内容过期那句（`clean_status.py:216` 逐字）。
_CONTENT_HINT = "有 {n} 篇文档的磁盘内容已变更，建议重建索引。"

#: 仅规则过期时**必须逐字保持**的既有句（`clean_status.py:214` 逐字）。
_RULE_HINT = "有 {n} 篇文档还不是按当前清洗规则生成的，建议重建索引。"

#: 判据面用的纯文本白名单（**判据载体侧独立副本**，见 :func:`_independent_judgeable`）。
#: ⚠️ 与产品 `_TEXT_SUFFIXES` 逐字同集：本套**只**用 `.py`（在两侧都在），
#: 故「`.md` 仍被整体排除」这条口径不受本文件影响（见 `test_bug108_md_still_excluded_from_text_suffixes`）。
_PLAIN_TEXT_SUFFIXES_FOR_TEST = frozenset({
    ".py", ".js", ".ts", ".java", ".go", ".rs", ".c", ".h", ".cpp", ".cs", ".rb", ".php",
    ".sh", ".sql", ".kt", ".swift",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml",
    ".jsx", ".tsx", ".vue", ".css", ".scss", ".html",
    ".txt",
})


# --------------------------------------------------------------------------
# 独立锚（本文件自算，不借产品判定）
# --------------------------------------------------------------------------


def _independent_judgeable(doc) -> bool:
    """**独立锚**：不借用产品 `_is_judgeable`，按判据面定义**自行**重算一遍。

    两条 `and`：① 有 `file_path` 且**不落在** `settings.upload_path` 之下（非留档）；
    ② 后缀在纯文本白名单内。**为什么另写一份**：四条腿都靠「我造的样本落在判据面内」成立 ——
    若只信 `_is_judgeable` 的返回值，它一旦被变异成恒值，这个前置就**自证**了（失去判别力）。
    """
    from app.core.config import settings

    file_path = doc.file_path or ""
    if not file_path:
        return False
    base = settings.upload_path
    candidate = Path(file_path)
    if candidate == base or base in candidate.parents:
        return False
    return candidate.suffix.lower() in _PLAIN_TEXT_SUFFIXES_FOR_TEST


def _independent_disk_text(path: Path) -> str:
    """**独立锚**：按判据面口径**自行**把磁盘字节重算成文本，**只剥至多一个** BOM。

    ⚠️ **刻意不复用产品实现**（既不调 `_read_disk_text`、也不调 `_parse_code`）：
    本函数的判别力来自「与产品**独立但等价**的写法」——用 `utf-8-sig` 一把解码
    （它**自己**吃掉**一个** BOM，这正是 `_parse_code` 剥一个的等价写法）。
    `errors="replace"` 与产品同款：`latin-1` 对任意字节串都成立 ⇒ 正常路径永不触发。
    """
    return path.read_bytes().decode("utf-8-sig", errors="replace")


def _raw_has_bom(raw: str | None) -> bool:
    """库内 `raw_content` 是否**仍带** `\ufeff` 前缀（= 产品没剥）。"""
    return bool(raw) and raw.startswith("\ufeff")


# --------------------------------------------------------------------------
# 造数（照 test_stale_marking_t3_4.py 范式）
# --------------------------------------------------------------------------


async def _import(client, session, path):
    """走产品端点造「可判文档」：`POST /api/documents/import-file` ⇒ `file_path` = 原路径。

    文件夹导入**不复制**文件（`document_routes.py:287-291`）⇒ `file_path` 就是 `tmp_path`
    里的那个文件，改盘上内容即「真改」。

    🔴 **为什么传 `session` 并在此 `rollback()`**：`client` 经
    `dependency_overrides[get_db]` 用**另一个** session/连接提交
    （`test_chat_contract.py:92-98`），而 `db` 会话若已持有开放事务的快照
    （MySQL REPEATABLE READ）⇒ **不结束事务就读不到刚提交的行**。
    """
    resp = await client.post("/api/documents/import-file", json={"path": str(path)})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ready", resp.text
    await session.rollback()
    return resp.json()["id"]


async def _doc(db, doc_id: int) -> Document:
    """取回该文档的**库内行**（`raw_content` 是判据的库侧一半）。"""
    from sqlalchemy import select

    return (
        await db.execute(select(Document).where(Document.id == doc_id))
    ).scalars().one()


#: 一份**最小可入库**的 Python 源码（带真实换行；`chunk_code` 对它切块非空）。
_PY_BODY = "def f():\n    return 1\n"


# --------------------------------------------------------------------------
# 腿① 主腿：带 BOM 的 .py 入库后**磁盘未动** ⇒ 不得进过期集合（修前必红 / 修后必绿）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug108_leg1_bom_file_is_not_stale(client, db, tmp_path):
    """腿① 主腿：带 UTF-8 BOM 的 `.py` ⇒ `stale_content_documents(db) == []`。

    **修前必红**（红点 = 下面 `s0` 那条断言：`\ufeff` 前缀让逐字比较恒不相等），
    **修后必绿**。另加 `clean_status()` 面的显式声明断言（`needs_reindex` 与 `hint`）。
    """
    target = tmp_path / "带BOM.py"
    target.write_bytes(BOM + _PY_BODY.encode("utf-8"))

    doc_id = await _import(client, db, target)
    doc = await _doc(db, doc_id)

    # ======================================================================
    # 独立锚 ①：我造的样本**确实**落在判据面内（不经产品 `_is_judgeable`）
    # ======================================================================
    assert _independent_judgeable(doc), "独立锚失败：BOM 样本不在可判面内 ⇒ 腿① 无判别力"

    # ======================================================================
    # 独立锚 ②：**修法口径**本身 —— 库内 `raw_content` 与「独立重算的磁盘文本」必须相等。
    #
    # 这条锚把缺陷的**机制**钉死在判据里（不只看产品的集合返回值）：
    #   · 库侧 = `_parse_code` 的产物 ⇒ **无** `\ufeff`（修前修后都该如此）；
    #   · 盘侧 = 本文件自算的「至多剥一个 BOM」文本 ⇒ 与库侧逐字相等；
    #   · 产品侧 = `_read_disk_text` 的返回值 ⇒ **修前**多一个 `\ufeff` 前缀 ⇒ 不相等。
    # ======================================================================
    assert doc.raw_content == _PY_BODY, f"库侧 raw_content 形态变了：{doc.raw_content!r}"
    assert not _raw_has_bom(doc.raw_content), "库侧 raw_content 不该带 BOM（_parse_code 已剥）"
    # 磁盘文本的**独立**重算（utf-8-sig 自吃一个 BOM = 与 _parse_code 等价）
    assert _independent_disk_text(target) == doc.raw_content, (
        "独立锚失败：磁盘文本（至多剥一个 BOM）与库内 raw_content 不等 ⇒ 判据面口径不自洽"
    )
    # 产品侧读法的**修前**形态（BOM 保留）—— 这是缺陷的机制描述，不是断言产品当前行为
    from app.pipeline.clean_status import _read_disk_text

    product_disk = _read_disk_text(str(target), doc_id)
    assert product_disk is not None, "产品读盘失败 ⇒ 本腿会因 fail-closed 误红，样本需重建"

    # ======================================================================
    # 🔴 腿① 的靶断言（**修前红点就在这里**）
    # ======================================================================
    s0 = await stale_content_documents(db)
    assert s0 == [], (
        f"带 BOM 的文件被误判「磁盘内容已变更」（修前红点）：{s0}；"
        f"产品盘侧首字符 = {product_disk[:1]!r}，库侧首字符 = {doc.raw_content[:1]!r}"
    )

    # ======================================================================
    # `clean_status()` 面：既不要求重建，也不得出现磁盘那半句
    # ======================================================================
    status = await clean_status(db)
    assert status["needs_reindex"] is False, f"仅 BOM 差异不得要求重建：{status}"
    assert "磁盘内容已变更" not in status["hint"], f"hint 不得出现磁盘过期句：{status['hint']!r}"
    # 键集仍是前端 `CleanStatus` 的五键（本笔不得动键集）
    assert set(status) == {
        "enabled", "rule_fingerprint", "stale_documents", "needs_reindex", "hint"
    }, f"键集被改动：{sorted(status)}"


@pytest.mark.asyncio
async def test_bug108_leg1b_bom_file_survives_reindex(client, db, tmp_path):
    """腿① 附：`POST /api/documents/reindex` 之后**仍然**不得进过期集合。

    需要保护的场景：「`reindex` reclean 重走 `_parse_code`
    ⇒ `raw_content` 仍无 BOM ⇒ 提示**永久**存在」。本腿把这个「清不掉」的性质钉住：
    重建**不改**结论 —— 修前恒过期（重建前红、重建后仍红），修后两侧皆绿。
    """
    target = tmp_path / "带BOM重建.py"
    target.write_bytes(BOM + _PY_BODY.encode("utf-8"))
    doc_id = await _import(client, db, target)

    assert _independent_judgeable(await _doc(db, doc_id)), "独立锚失败：样本不在可判面内"

    resp = await client.post("/api/documents/reindex", json={"document_id": doc_id})
    assert resp.status_code == 200, resp.text
    await db.rollback()

    doc = await _doc(db, doc_id)
    # 重建后库侧仍是「无 BOM 的原文」⇒ 与磁盘（至多剥一个 BOM）仍应逐字相等
    assert not _raw_has_bom(doc.raw_content), "重建后 raw_content 反而带上了 BOM ⇒ 口径漂移"
    assert _independent_disk_text(target) == doc.raw_content, "独立锚失败：重建后两侧不等"

    s_after = await stale_content_documents(db)
    assert s_after == [], f"重建后仍被判「磁盘内容已变更」（清不掉）：{s_after}"


# --------------------------------------------------------------------------
# 腿② 负控：无 BOM 的同型文件同样不进过期集合（防「恒过期」被反向修成「恒不过期」）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug108_leg2_no_bom_file_is_not_stale(client, db, tmp_path):
    """腿② 负控：**无 BOM** 的同型 `.py` ⇒ 同样不进过期集合。

    **为什么必须有这条**：腿① 只证「带 BOM 的不过期」。若把修复写成「恒返回 `[]`」
    （恒不过期），腿① 会**假绿**；腿② 与腿③ **成对**才能把它钉死 ——
    腿② 证「正常的确实不过期」、腿③ 证「真改的确实过期」。
    """
    target = tmp_path / "无BOM.py"
    target.write_bytes(_PY_BODY.encode("utf-8"))

    doc_id = await _import(client, db, target)
    doc = await _doc(db, doc_id)

    assert _independent_judgeable(doc), "独立锚失败：无 BOM 样本不在可判面内"
    assert not _raw_has_bom(doc.raw_content), "无 BOM 样本的库侧不该有 BOM"
    # 独立重算：两侧（盘 / 库）本就应该逐字相等
    assert _independent_disk_text(target) == doc.raw_content, "独立锚失败：无 BOM 两侧不等"

    assert await stale_content_documents(db) == [], "负控失败：未改动的无 BOM 文件被误标过期"

    status = await clean_status(db)
    assert status["needs_reindex"] is False, f"负控失败：{status}"
    assert "磁盘内容已变更" not in status["hint"], repr(status["hint"])


# --------------------------------------------------------------------------
# 腿③ 真改内容仍判过期（防修成「只要读得到就不过期」的假绿）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug108_leg3_really_changed_bom_file_is_stale(client, db, tmp_path):
    """腿③：把 BOM 文件的**内容真改一行** ⇒ 过期集合**包含**它。

    与腿① 是**成对**的判别力来源：
    · 腿①（`== []`）+ 腿③（`in`）⇒「BOM 不造成差异」**且**「真改仍被抓」；
    · 只做腿① ⇒ 「恒不过期」的假修法照样绿；
    · 只做腿③ ⇒ 「恒过期」的现状照样绿（正是本缺陷）。
    """
    target = tmp_path / "带BOM改动.py"
    target.write_bytes(BOM + _PY_BODY.encode("utf-8"))
    doc_id = await _import(client, db, target)
    assert _independent_judgeable(await _doc(db, doc_id)), "独立锚失败：样本不在可判面内"

    # 改**一行内容**（保持仍是带 BOM 的 UTF-8）
    target.write_bytes(BOM + "def f():\n    return 2\n".encode("utf-8"))

    # 独立锚：盘侧（至多剥一个 BOM）与库侧**确实**不等 —— 这是「真改」的字节级证据
    doc = await _doc(db, doc_id)
    assert _independent_disk_text(target) != doc.raw_content, (
        "独立锚失败：改完之后两侧仍相等 ⇒ 本腿构造无效"
    )

    stale_ids = await stale_content_documents(db)
    assert doc_id in stale_ids, f"真改内容后未进过期集合：{stale_ids}"

    status = await clean_status(db)
    assert status["needs_reindex"] is True
    assert "磁盘内容已变更" in status["hint"], repr(status["hint"])


# --------------------------------------------------------------------------
# 腿④ 边界：两个 BOM ⇒ 与产品口径一致（以实测为准）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug108_leg4_double_bom_matches_product_semantics(client, db, tmp_path):
    """腿④ 边界：文件以**两个** BOM 开头 ⇒ 与产品口径一致。

    **推理（修法落地前先写死）**：`_parse_code`（`parsing.py:167-171`）在
    `detect_encoding` 回 `utf-8` 时**只剥一个** `\ufeff` ⇒ 库侧 `raw_content`
    仍留**第二个** `\ufeff` 前缀；修复后的磁盘侧（`_read_disk_text`）同样只剥一个
    ⇒ 两侧**仍逐字相等** ⇒ **应判不过期**。

    🔴 **以实测为准**：本腿按上述推理断言，**实测事实**登记在下方「实测登记」栏；
    若实测与推理不符，以实测为准改断言与口径。

    **实测登记（2026-09-27 · 修后实跑，命令与输出逐字摘录）**：

    · **修前**（未剥 BOM）：
      @@$env:PYTEST_ARGS_JSON='["-v","--tb=short","tests/api/test_bug108_bom_content_stale.py"]'@@ +
      @@pwsh -File 证据/_bugfix3-scratch/pytest_locked.ps1@@ ⇒
      @@3 failed, 3 passed@@；本腿红点原文 =
      @@AssertionError: 双 BOM 文件被判过期 ⇒ 盘侧与库侧剥 BOM 数不一致：[5]@@。
    · **修后**（剥一个 BOM）：同一命令 ⇒ @@6 passed in 5.19s@@，本腿 PASSED ⇒ 与推理**一致**
      （两侧都只剥一个 ⇒ @@stale_content_documents(db) == []@@、@@needs_reindex is False@@）。

    ⚠️ **如实登记**：本腿在**修前也是红的**（不是只在修后才有意义）—— 它同时钉住
    「修前恒过期」与「修后两侧口径对齐」两态。

    ⚠️ 本腿**不是**「两个 BOM 是合法输入」的背书 —— 它钉的是**两侧口径对齐**这一条：
    盘侧剥几个，库侧就得剥几个。若将来 `_parse_code` 改成剥光所有 BOM，本腿会**真红**
    （两侧不再相等），这正是它该有的敏感性。
    """
    target = tmp_path / "双BOM.py"
    target.write_bytes(BOM + BOM + _PY_BODY.encode("utf-8"))

    doc_id = await _import(client, db, target)
    doc = await _doc(db, doc_id)
    assert _independent_judgeable(doc), "独立锚失败：双 BOM 样本不在可判面内"

    # 独立锚：库侧 = 只剥一个 BOM 的产物（留一个 `\ufeff` 前缀）
    assert doc.raw_content == "\ufeff" + _PY_BODY, (
        f"库侧形态与「只剥一个 BOM」的口径不符：{doc.raw_content[:3]!r}…"
    )
    # 独立锚：盘侧（本文件自算，utf-8-sig 自吃一个）与库侧逐字相等
    assert _independent_disk_text(target) == doc.raw_content, (
        "独立锚失败：双 BOM 下两侧口径不一致 ⇒ 判据面口径不自洽"
    )
    # 直接锚到产品口径：_parse_code 只剥一个（与上面的库侧断言互为对照）
    parsed_text, _pages = _parse_code(BOM + BOM + _PY_BODY.encode("utf-8"))
    assert parsed_text == doc.raw_content, "库内 raw_content 与 _parse_code 直调结果不一致"
    assert parsed_text.startswith("\ufeff"), "_parse_code 不再是「只剥一个」⇒ 本腿口径需重判"
    # 探测名如实登记：BOM 能被 utf-8 成功解码 ⇒ 回的是 utf-8（**不是** utf-8-sig）
    assert detect_encoding(BOM + BOM + _PY_BODY.encode("utf-8")) == "utf-8"

    # ======================================================================
    # 🔴 腿④ 靶断言（**实测结果**）：两侧都只剥一个 ⇒ 逐字相等 ⇒ **不过期**
    # ======================================================================
    stale_ids = await stale_content_documents(db)
    assert stale_ids == [], (
        f"双 BOM 文件被判过期 ⇒ 盘侧与库侧剥 BOM 数不一致：{stale_ids}"
    )

    status = await clean_status(db)
    assert status["needs_reindex"] is False, f"双 BOM 下不得要求重建：{status}"
    assert "磁盘内容已变更" not in status["hint"], repr(status["hint"])


# --------------------------------------------------------------------------
# 口径守卫：本笔**不得**把 .md 拉进判据面（_TEXT_SUFFIXES 一字不动）
# --------------------------------------------------------------------------


def test_bug108_md_still_excluded_from_text_suffixes():
    """口径守卫：`_TEXT_SUFFIXES` 里**仍然没有** `.md` / `.markdown`。

    后缀范围约束：「`.md` 当年因同族问题被整体排除出判据面
    （`clean_status.py:51-53`），代码后缀的 BOM 差异漏了」——本笔**只**补 BOM 剥法，
    **不得**顺手放宽后缀面。故这条守卫钉住「`.md` 仍被整体排除」。

    ⇒ **不得**据本笔写「所有文档都能检测内容过期 / 用户能看见自己的资料已过期」：
    真实语料以 `.md` 为绝对多数（约 95%），本笔覆盖的仍只是**代码 / 配置 / 前端资源 / `.txt`**。
    """
    from app.pipeline.clean_status import _TEXT_SUFFIXES

    assert ".md" not in _TEXT_SUFFIXES, "MD 后缀被拉进了判据面 ⇒ 越界（本笔不得改后缀面）"
    assert ".markdown" not in _TEXT_SUFFIXES, "MARKDOWN 后缀被拉进了判据面 ⇒ 越界"
    # 与判据载体侧的独立副本逐字同集（两侧漂移即红）
    assert set(_TEXT_SUFFIXES) == set(_PLAIN_TEXT_SUFFIXES_FOR_TEST), (
        "产品白名单与本文件独立副本已漂移："
        f"多出 {set(_TEXT_SUFFIXES) - set(_PLAIN_TEXT_SUFFIXES_FOR_TEST)} / "
        f"缺少 {set(_PLAIN_TEXT_SUFFIXES_FOR_TEST) - set(_TEXT_SUFFIXES)}"
    )
    # `.py` 在两侧都在（本套四腿全用它）
    assert ".py" in _TEXT_SUFFIXES


# --------------------------------------------------------------------------
# 腿⑤ 超集负控：盘 ⊃ 库（尾部追加一行）⇒ **必须判过期**
#    （Lead 复核 B 路 REAL-2：防「逐字比较被改宽成包含关系」的变异存活）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug108_leg5_disk_superset_is_stale(client, db, tmp_path):
    """腿⑤ 超集负控：磁盘文件 = 库内正文 **+ 尾部追加一行**（盘 ⊃ 库）⇒ **必须判过期**。

    **为什么必须补这条**（Lead 复核 B 路 REAL-2）：若把 `stale_content_documents` 的
    `disk_text != doc.raw_content` 改宽成**包含关系**（例如 `raw_content not in disk_text`），
    则「盘 ⊃ 库」会被误判成**未过期** —— 而腿①–④ 对这种变异**全部保持绿**
    （它们的盘侧文本与库侧要么相等、要么是「真改一行」这种**非包含**差异）⇒ 变异存活。
    本腿把「盘 ⊃ 库」这一形态钉成**必须过期**，该变异 ⇒ **真红**。

    **独立锚**：先用本文件自己的字节级实现证明「盘侧**确实**是库侧的超集、且不相等」
    （`disk.startswith(raw)` 且 `disk != raw`）—— 不只看产品的集合返回值。
    """
    target = tmp_path / "超集.py"
    target.write_bytes(BOM + _PY_BODY.encode("utf-8"))
    doc_id = await _import(client, db, target)
    doc = await _doc(db, doc_id)
    assert _independent_judgeable(doc), "独立锚失败：样本不在可判面内"

    # 盘上内容 = 库内正文 + **尾部追加一行**（仍是合法 UTF-8，仍带 BOM）
    target.write_bytes(BOM + (_PY_BODY + "def g():\n    return 3\n").encode("utf-8"))

    disk_text = _independent_disk_text(target)
    raw = doc.raw_content
    # 🔴 独立锚：构造出的确实是「**超集且不相等**」这一形态（包含关系变异会在此形态上误判）
    assert disk_text.startswith(raw), "独立锚失败：盘侧不是库侧的超集 ⇒ 本腿构造无效"
    assert disk_text != raw, "独立锚失败：盘侧与库侧相等 ⇒ 本腿构造无效"

    stale_ids = await stale_content_documents(db)
    assert doc_id in stale_ids, (
        f"盘侧是库侧超集（尾部追加了一行）却未被判过期 ⇒ 逐字比较被改宽了：{stale_ids}"
    )

    status = await clean_status(db)
    assert status["needs_reindex"] is True
    assert "磁盘内容已变更" in status["hint"], repr(status["hint"])
