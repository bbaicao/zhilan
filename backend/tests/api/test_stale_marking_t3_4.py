"""``A10-2``（显式声明可见）的判据载体 —— 工具升级-3 · ``回归检查``。

**为什么单独立文件**：``回归检查`` 给 ``clean`` 子对象加了「磁盘内容过期」这一路现算读数
（``D-回归检查`` 取 (甲)：汇进既有 ``needs_reindex`` 的「或」语义、**不新增键**）。
既有 ``tests/api/test_health_contract.py:118`` 对 ``clean`` 是**子集**断言
（``assert set(CLEAN_KEYS) <= set(...)``）—— 它**拦不住「新增键」**，也拦不住
「既有语义被磁盘过期污染」⇒ 本文件把四腿补上。

**🔴 取证方式（``D-回归检查`` 的 ``V2-B1`` 栏 · 不得改）**：判据一律走
「**``db`` fixture 注入 + 直调检测函数**」。**不得**用 ``ASGITransport`` 打 ``/api/health`` ——
``app/api/__init__.py:77`` 的 ``health()`` **无 ``db`` 参数**、`:130` 自己 ``SessionLocal()``
⇒ 读的是**开发真库**（`.env` 的 ``MYSQL_DATABASE``），不是测试库 ``zhilan_v2_test``
⇒ 腿① 恒 false / 腿② 恒真（假绿）/ 腿③ 无判别力。``clean`` 读数用
``test_health_contract.py`` 同型的**直调 ``health_endpoint()``** 形态。

**四条腿**：
① **正控**：改盘上文件 ⇒ 检测入口的过期集合**包含该文档**，且 ``clean.needs_reindex == true``；
② **负控**：**未改动**的文件**不得**进过期集合；
③ **既有语义不破**：**仅规则过期**时读数与改前**逐字相同**；
④ **前端可见**：``set(clean)`` **全等五键** + 过期时 ``hint`` **非空**。

**🔴 首条断言**：``assert 可判文档数 >= 1`` —— 无样本 ⇒ 四腿按**空靶**处理、**不得记绿**。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.api import health as health_endpoint
from app.models import Document
from app.pipeline.clean_status import (
    clean_status,
    stale_clean_documents,
    stale_content_documents,
)

# 必须显式 import 夹具（照既有先例 test_documents_endpoints.py:21）。
# 它在 test_chat_contract.py:73-74 里定义，不在 tests/api/conftest.py
# => 不会自动对本文件可见（K-回归检查 只说本目录有 conftest、可复用 autouse 钉子，
#    但那枚 conftest 没有 client 夹具 —— 实测 grep "def client" conftest.py 零命中）。
# 漏掉本行 => 7 条用例全部 "fixture 'client' not found"（--collect-only 结构上发现不了）。
from tests.api.test_chat_contract import client  # noqa: F401 —— 夹具再导出（非未用 import）

# 前端 ``CleanStatus``（``frontend/src/api.ts:45-49``）逐字五键。
CLEAN_KEYS = ("enabled", "rule_fingerprint", "stale_documents", "needs_reindex", "hint")

#: 判据面用的纯文本白名单（**判据载体侧独立副本**，见 :func:`_independent_judgeable`）。
_PLAIN_TEXT_SUFFIXES_FOR_TEST = frozenset({
    ".py", ".js", ".ts", ".java", ".go", ".rs", ".c", ".h", ".cpp", ".cs", ".rb", ".php",
    ".sh", ".sql", ".kt", ".swift",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml",
    ".jsx", ".tsx", ".vue", ".css", ".scss", ".html",
    ".txt",
})

#: 仅规则过期时**必须逐字保持**的既有句（``D-回归检查`` / §2 第 6 条）。
_RULE_HINT = "有 {n} 篇文档还不是按当前清洗规则生成的，建议重建索引。"
#: 仅磁盘内容过期时的新增句（**逐字**）。
_CONTENT_HINT = "有 {n} 篇文档的磁盘内容已变更，建议重建索引。"


async def _import(client, session, path):
    """走产品端点造「可判文档」：``POST /api/documents/import-file`` ⇒ ``file_path`` = 原路径。

    造数一律 ``tmp_path`` + `.txt`/`.py`（🔴 **不用 `.md`** —— ``K-回归检查`：``raw_content``
    对 md 是 frontmatter 剥离后的 body ⇒ 带 YAML frontmatter 的 md 会恒判过期）。

    🔴 **为什么要传 ``session`` 并在此 ``rollback()``**：``client`` 经
    ``dependency_overrides[get_db]`` 用**另一个** session/连接提交
    （``test_chat_contract.py:92-98``），而 ``db`` 会话若已持有开放事务的快照
    （MySQL REPEATABLE READ）⇒ **不结束事务就读不到刚提交的行**。
    照既有先例：``test_documents_endpoints.py:205``（同一 ``client, db`` 组合）也先
    ``await db.rollback()`` 再经 ``db`` 断言。把它收敛进本助手 ⇒ 每个调用点都不必各自记得。
    """
    resp = await client.post("/api/documents/import-file", json={"path": str(path)})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ready", resp.text
    await session.rollback()  # 结束可能存在的旧快照，保证后续经 session 的读能看到已提交行
    return resp.json()["id"]


async def _all_docs(session):
    return (await session.execute(select(Document))).scalars().all()


#: 仓库根（``api.ts`` 相对它的位置固定：``frontend/src/api.ts``）。
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _api_ts_clean_status_keys() -> list[str]:
    """**真读** ``frontend/src/api.ts``，取 ``CleanStatus`` 类型体的**五个键名**。

    🔴 **不得硬编成常量**（Lead 裁决 ④ / B 路实测）：硬编的话，将来前端给 ``CleanStatus``
    加/删键，这个锚**不会红** ⇒ 等于白落。这里按 ``export type CleanStatus = {`` 与紧随的
    ``}`` 之间**逐行解析** ``键: 类型;`` 形态，只取键名与**顺序**。
    """
    source = (_REPO_ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
    lines = source.splitlines()
    try:
        start = next(
            i for i, line in enumerate(lines) if line.strip().startswith("export type CleanStatus")
        )
    except StopIteration:  # pragma: no cover —— 前端类型被改名 ⇒ 判据该红
        raise AssertionError("frontend/src/api.ts 里找不到 export type CleanStatus") from None
    keys: list[str] = []
    for line in lines[start + 1:]:
        stripped = line.strip()
        if stripped.startswith("}"):
            break
        if ":" in stripped:
            keys.append(stripped.split(":", 1)[0].strip())
    return keys


def _independent_judgeable(doc) -> bool:
    """**独立锚**：不借用产品模块的 `_is_judgeable`，按 `D-回归检查` 的判据面定义**自行**重算一遍。

    三条``and``：① 有 `file_path` 且**不落在** `settings.upload_path` 之下（非留档）；
    ② 后缀在纯文本白名单内；③ 不是带 YAML frontmatter 的 ``.md`/`.markdown``。
    **为什么另写一份**：腿①/首条断言都靠「可判文档 ≥ 1」成立 —— 若只信 `_is_judgeable` 的返回值，
    它一旦被变异成恒值，该前置就**自证**了（失去判别力）。两份独立实现互为对照。
    """
    from app.core.config import settings

    file_path = doc.file_path or ""
    if not file_path:
        return False
    base = settings.upload_path
    candidate = Path(file_path)
    if candidate == base or base in candidate.parents:
        return False
    suffix = candidate.suffix.lower()
    if suffix not in _PLAIN_TEXT_SUFFIXES_FOR_TEST:
        return False
    if suffix in (".md", ".markdown"):
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
                if handle.readline().rstrip("\r\n").strip() == "---":
                    return False
        except OSError:
            pass
    return True


@pytest.fixture
def two_judgeable(client, tmp_path):
    """造两篇可判文档（`.txt` 与 `.py`），返回 ``(folder, changed, untouched)``。

    ``changed`` 供用例「改内容」用；``untouched`` 全程不动 ⇒ 是**负控**的样本。
    """
    folder = tmp_path / "资料"
    folder.mkdir()
    changed = folder / "a.txt"
    untouched = folder / "b.py"
    changed.write_text("第一版正文。\n第二行。\n", encoding="utf-8")
    untouched.write_text("def f():\n    return 1\n", encoding="utf-8")
    return folder, changed, untouched


# --------------------------------------------------------------------------
# 前置：可判文档数 >= 1（🔴 本件第一条断言；缺此 ⇒ 空靶、不得记绿）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a10_2_precondition_has_judgeable_document(client, db, two_judgeable):
    """🔴 前置腿：可判文档数 ≥ 1，且**未改动**时两条检测都干净（无过期）。"""
    _, changed, untouched = two_judgeable
    await _import(client, db, changed)
    await _import(client, db, untouched)

    docs = await _all_docs(db)
    # 🔴 **刻意 import 私有名**（Lead 裁决 ⑤）：``_is_judgeable`` 是 ``D-回归检查`` **写死的产品判定基准**
    # （「``file_path`` 不落在 ``settings.upload_path`` 之下 ∩ 纯文本类」），**属产品语义、不是测试脚手架**
    # ⇒ 虽未进 ``__all__``，本载体**有意**直接引用它来算「可判文档数」。
    # ⚠️ **后人不得据此「清理未导出名」而删掉它**；同时本腿另有一条**不依赖它**的独立锚（见下）。
    from app.pipeline.clean_status import _is_judgeable  # noqa: PLC0415 —— 判据面定义即被测面

    judgeable = [d for d in docs if _is_judgeable(d)]
    assert len(judgeable) >= 1, "空靶：造不出可判文档 ⇒ 本套四腿不得记绿"

    # 🔴 **独立锚（Lead 裁决 ①）**：不经 `_is_judgeable` 再证一次「我造的两篇确实落在可判面内」
    # —— 否则 `_is_judgeable` 若被变异成**恒 True/恒 False**，本套会失去第二条独立锚。
    assert {_independent_judgeable(d) for d in docs} == {True}, (
        "独立锚失败：造出的文档未落在可判面内"
    )

    # 刚入库、磁盘未动 ⇒ 磁盘内容过期集合为空（也顺带证明没有「恒过期」）
    assert await stale_content_documents(db) == []


# --------------------------------------------------------------------------
# 腿① 正控：改盘上文件 ⇒ 过期集合含该文档，且 clean.needs_reindex 为 True
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a10_2_leg1_changed_file_is_marked(client, db, two_judgeable):
    """腿① 正控：改盘上文件 ⇒ 检测入口的过期集合**包含该文档**，``needs_reindex == true``。"""
    _, changed, untouched = two_judgeable
    changed_id = await _import(client, db, changed)
    await _import(client, db, untouched)

    # 🔴 **独立锚（Lead 裁决 ①）**：被我改的那篇**确实**落在可判面内（不经产品 `_is_judgeable`）
    target = next(d for d in await _all_docs(db) if d.id == changed_id)
    assert _independent_judgeable(target), "独立锚失败：被改文档不在可判面内 ⇒ 腿① 无判别力"

    # 🔴 **归因唯一化（Lead 裁决 ⑨ · 取 (b)）**：判别链写成 S0/S1 两态，**两条半句都留在腿①内**。
    # 为什么不用「改动前 == []」：那条在 `M-回归检查`（恒过期）下会**先红**，让红点同时落进腿① 与腿②
    # ⇒ 破坏 `D-回归检查` 的「唯一靶腿」。改成 `target not in S0` 后：
    #   · `M-回归检查`（恒未过期）⇒ 打红 `target in S1`（腿①）
    #   · `M-回归检查`（恒过期）  ⇒ 打红 `target not in S0`（**仍在腿①内**）
    s0 = await stale_content_documents(db)
    assert changed_id not in s0, f"改动前该文档不应已过期（S0={s0}）"

    # 同一路径、内容变（判据面写死的「制造过期」法）
    changed.write_text("第二版正文，已经改过了。\n", encoding="utf-8")

    s1 = await stale_content_documents(db)
    assert changed_id in s1, f"被改文档未进过期集合：{s1}"

    status = await clean_status(db)
    assert status["needs_reindex"] is True  # 断具体值，不是真值性


# --------------------------------------------------------------------------
# 腿② 负控：未改动的文件不得进过期集合（防「恒 true」假绿）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a10_2_leg2_untouched_file_is_not_marked(client, db, two_judgeable):
    """腿② 负控：改一篇 ⇒ **另一篇未改动**的**不得**进过期集合。"""
    _, changed, untouched = two_judgeable
    await _import(client, db, changed)
    untouched_id = await _import(client, db, untouched)

    changed.write_text("第二版正文，已经改过了。\n", encoding="utf-8")

    stale_ids = await stale_content_documents(db)
    # 🔴 **只断负控**（Lead 裁决 ⑦）：本条**不**放「changed_id in stale_ids」的正控对照 ——
    # 否则「恒未过期」类变异会同时打红腿①/腿②/fail-closed **三腿**，破坏 `D-回归检查` 的
    # 「唯一靶腿」纪律（`M-回归检查` 的靶腿**必须只有腿①**）。
    assert untouched_id not in stale_ids, "负控失败：未改动的文件被误标过期"


# --------------------------------------------------------------------------
# 腿③ 既有语义不破：仅规则过期时读数与改前逐字相同
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a10_2_leg3_rule_only_readout_is_byte_identical(client, db, two_judgeable, monkeypatch):
    """腿③：**仅规则过期**（磁盘一字未动）时，``clean`` 读数与改前**逐字相同**。

    🔴 **必须自建**：既有 ``test_health_contract.py:118`` 是**子集**断言（``<=``），
    拦不住「磁盘过期污染规则过期读数」。构造法 = 把 ``CLEAN_*`` 规则指纹改成「不匹配」，
    让 ``stale_clean_documents` 把两篇都算成规则过期，而磁盘内容**完全没动**。
    """
    _, changed, untouched = two_judgeable
    await _import(client, db, changed)
    await _import(client, db, untouched)

    # 改前：规则指纹一致 ⇒ 无规则过期、无磁盘过期
    before = await clean_status(db)
    assert before["stale_documents"] == 0
    assert before["hint"] == ""

    from app.pipeline import clean_status as module

    monkeypatch.setattr(module, "rule_fingerprint", lambda: "变异指纹：与库内报告不匹配")
    # 🔴 **本腿的靶断言（Lead 裁决 ⑩ · v2 如实改写）**：磁盘**一字未动** ⇒ 磁盘内容过期必须**仍然为零**。
    #
    # ⚠️ **本注释曾声称「这一条在本腿内先红 ⇒ 红点落在腿③ 自身、归因唯一」—— 被 `M-回归检查` 实测证伪
    # （2026-09-26 · auditor-2）**：本构造下磁盘一字未动 ⇒ `content_stale` **恒为 0** ⇒
    # 「把 content_stale 混进 stale」（`M-回归检查`）在本腿里是 **no-op** ⇒ 本腿**全绿**、红点落在**腿④**。
    # ⇒ **如实描述本条的敏感性**：
    #   · 对 `M-回归检查`（恒未过期）/ `M-回归检查`（恒过期）**敏感**（它正是 `== []` 型断言的判别来源）；
    #   · 对 `M-回归检查`（仅污染、不改过期判定本身）**不敏感**。
    # ⇒ **污染面由下面的「混合场景」块（本次新增）捕获，并另有腿④ 的 `hint` 全等兜底**。
    assert await stale_content_documents(db) == []

    after = await clean_status(db)
    stale = await stale_clean_documents(db)
    assert stale == 2, f"构造失效：规则过期数应为 2，实得 {stale}"
    assert after["stale_documents"] == 2
    assert after["needs_reindex"] is True
    # 🔴 既有句**逐字**不变（不得被磁盘过期那半句污染）
    assert after["hint"] == _RULE_HINT.format(n=2), repr(after["hint"])
    # 磁盘那半句**任何条数**都不得出现（否则 = 规则读数被磁盘过期污染）
    assert "磁盘内容已变更" not in after["hint"], repr(after["hint"])
    # 键集/类型与改前一致
    assert set(after) == set(before)

    # ======================================================================
    # 🔴 **混合场景（2026-09-26 补 · auditor-2 发现腿③ 对 `M-回归检查` 无判别力）**
    #
    # 上面那段是「**仅**规则过期、磁盘一字未动」⇒ `content_stale == 0` ⇒ **`M-回归检查` 打不到它**。
    # 本段制造「**规则过期 > 0 且 磁盘内容过期 > 0**」的混合态，让 `M-回归检查`
    # （把 `content_stale` 混进 `stale_documents`）**必然改变读数** ⇒ 本腿对它有判别力。
    #
    # 构造：两篇文档都规则过期（指纹已被 monkeypatch 改掉）；再把**其中一篇**的盘上内容改掉
    # ⇒ 期望 `stale_documents` **只数规则过期那 2 篇**、**不被磁盘过期那 1 篇再加一次**。
    #
    # ⚠️ **敏感性如实评估（Lead 护栏 ③）**：本条是 `== 2`（**精确等值**）断言 ⇒
    #   · `M-回归检查`（污染）⇒ **红**（会变成 3）—— **这是本条存在的唯一理由**；
    #   · `M-回归检查/2`（恒未过期 / 恒过期）⇒ **同样会红** ⇒ 本条**不是**这两条变异的唯一靶腿，
    #     它们的唯一靶腿仍是腿①/腿②（见 `D-回归检查`）。此处**如实登记**，不含混记「归因唯一」。
    # ======================================================================
    changed.write_text("混合场景：这一篇的盘上内容已改。\n", encoding="utf-8")
    mixed_content = await stale_content_documents(db)
    assert len(mixed_content) == 1, f"混合场景构造失效：磁盘过期应恰 1 篇，实得 {mixed_content}"
    mixed_rule = await stale_clean_documents(db)
    assert mixed_rule == 2, f"混合场景构造失效：规则过期应仍为 2 篇，实得 {mixed_rule}"

    mixed = await clean_status(db)
    # 🔴 **靶断言**：规则过期数**只数规则过期那 2 篇**，不得被磁盘过期那 1 篇污染（`M-回归检查` ⇒ 会变 3 ⇒ 红）
    assert mixed["stale_documents"] == 2, (
        f"`stale_documents` 被磁盘过期污染：期望 2（仅规则），实得 {mixed['stale_documents']}"
    )
    assert mixed["stale_documents"] == mixed_rule, "规则读数必须恒等于 `stale_clean_documents` 的返回值"
    assert mixed["needs_reindex"] is True
    # 🔴 **靶断言**：两句**并列**（先规则、后磁盘），逗号连接
    assert mixed["hint"] == (
        _RULE_HINT.format(n=2) + "，" + _CONTENT_HINT.format(n=1)
    ), repr(mixed["hint"])
    # 顺序写死：规则句在前、磁盘句在后
    assert mixed["hint"].index("还不是按当前清洗规则生成的") < mixed["hint"].index("磁盘内容已变更")
    # 键集仍全等五键（混合态不得长出第六个键）
    assert set(mixed) == set(before)


# --------------------------------------------------------------------------
# 腿④ 前端可见：键集**全等**五键 + 过期时 hint 非空
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a10_2_leg4_clean_key_set_is_exactly_five_and_hint_nonempty(
    client, db, two_judgeable, monkeypatch
):
    """腿④：``set(clean)`` **全等五键**（§2 第 4 条：既有 `:118` 是子集、拦不住新增键）
    + 过期时 ``hint`` **非空**，且 ``clean`` 真出现在 health 出参里（前端消费端）。"""
    _, changed, _ = two_judgeable
    await _import(client, db, changed)

    # 前端消费端：health 出参里的 clean 段（直调形态，与 test_health_contract.py 同型）
    monkeypatch.setattr("app.api.SessionLocal", lambda: _DbCtx(db))

    health = await health_endpoint()
    clean = health["clean"]

    # 🔴 **(甲) 前端类型锚（Lead 裁决 ④）**：**真读** `frontend/src/api.ts` 的 `CleanStatus`
    # 类型体，取键名与顺序，与后端产出的键集**全等**比对 —— **不得**硬编五键常量。
    api_keys = _api_ts_clean_status_keys()
    assert api_keys == list(CLEAN_KEYS), (
        f"api.ts 的 CleanStatus 键变了：{api_keys}（本文件常量 {list(CLEAN_KEYS)}）"
    )
    assert set(clean) == set(api_keys), (
        f"后端 clean 键集 != 前端 CleanStatus 键集："
        f"多出 {set(clean) - set(api_keys)} / 缺少 {set(api_keys) - set(clean)}"
    )
    assert set(clean) == set(CLEAN_KEYS), f"键集不等：多出 {set(clean) - set(CLEAN_KEYS)} / 缺少 {set(CLEAN_KEYS) - set(clean)}"
    assert isinstance(clean["enabled"], bool)
    assert isinstance(clean["rule_fingerprint"], str)
    assert isinstance(clean["stale_documents"], int)
    assert isinstance(clean["needs_reindex"], bool)
    assert isinstance(clean["hint"], str)

    # 未改动 ⇒ hint 空；改盘上文件 ⇒ hint 非空（**显式声明**对用户可见）
    assert clean["hint"] == ""
    changed.write_text("第三版正文。\n", encoding="utf-8")
    after = await clean_status(db)
    assert after["needs_reindex"] is True
    assert after["hint"], "磁盘内容过期时 hint 必须非空（显式声明）"
    assert after["hint"] == _CONTENT_HINT.format(n=1), repr(after["hint"])
    assert set(after) == set(CLEAN_KEYS)


# --------------------------------------------------------------------------
# fail-closed：指纹取不到（文件被删）⇒ 判过期
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a10_2_fail_closed_deleted_file_is_stale(client, db, two_judgeable):
    """fail-closed：文件**已删**（读失败）⇒ 判为过期，**不得**静默当未过期。"""
    _, changed, _ = two_judgeable
    changed_id = await _import(client, db, changed)
    assert await stale_content_documents(db) == []

    changed.unlink()

    # 🔴 **只断本腿的靶**（Lead 裁决 ⑦）：不再叠一条 `needs_reindex is True` 的正控 ——
    # 那条属腿① 的语义，叠在这里会让「恒未过期」变异多打红一条腿、破坏唯一靶腿纪律。
    assert changed_id in await stale_content_documents(db)


# --------------------------------------------------------------------------
# 空集合语义：零文档 ⇒ 恒「无过期」且不抛错
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a10_2_empty_corpus_is_no_stale(db):
    """空集合语义：零文档 ⇒ 过期集合为 ``[]``、``needs_reindex`` 假、``hint`` 空、**不抛错**。"""
    assert await stale_content_documents(db) == []
    status = await clean_status(db)
    assert status["needs_reindex"] is False
    assert status["hint"] == ""
    assert set(status) == set(CLEAN_KEYS)


# --------------------------------------------------------------------------
# 判据面收窄：留档类（上传）与非纯文本类一律**跳过**，不得恒判过期
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a10_2_uploaded_archive_copy_is_skipped(client, db, tmp_path, _upload_dir_stub):
    """上传类文档的 ``file_path`` 是**内容寻址留档副本** ⇒ 不进判据面（恒不成立，不得恒过期）。"""
    resp = await client.post(
        "/api/documents/upload",
        files={"file": ("note.txt", b"uploaded body\n", "text/plain")},
    )
    assert resp.status_code == 200, resp.text
    # 🔴 **必须先 ``rollback()`````**：``client`` 经 ``dependency_overrides[get_db]`` 用**另一个**
    # session/连接提交（``test_chat_contract.py:92-98``），而本 ``db`` 会话可能已持有开放事务的
    # 快照（MySQL REPEATABLE READ）⇒ 不结束事务就读**看不到**刚提交的行。
    # 照既有先例：``test_documents_endpoints.py:205``（同一 ``client, db`` 组合）此处也先 ``await db.rollback()``。
    await db.rollback()
    doc = (await _all_docs(db))[0]
    # 留档副本落在 upload_path 之下
    assert str(_upload_dir_stub) in str(doc.file_path)
    assert await stale_content_documents(db) == [], "留档副本被误判为磁盘内容过期"


@pytest.fixture
def _upload_dir_stub(monkeypatch, tmp_path):
    """把 ``upload_path`` 重定向到 ``tmp_path``（照 ``test_documents_contract.py:35-37`` 先例）。"""
    from app.core.config import settings

    upload = tmp_path / "uploads"
    upload.mkdir()
    monkeypatch.setattr(type(settings), "upload_path", property(lambda self: upload))
    return upload


class _DbCtx:
    """``async with SessionLocal() as db`` 的替身：把 health 的库会话接到测试会话上。

    ⚠️ 只在**腿④（前端可见）**用：那里要的是 health **出参形状**。腿①/②/③ 一律走
    ``db`` fixture 直调 —— 见本件模块 docstring 的 ``V2-B1`` 栏。

    🔴 **本替换的隐式依赖（必须写明 · Lead 裁决 ⑤ / B 路对抗补充）**：本替身**只**
    ``__aenter__`` 回原 session、**不实现 ``execute``**，而 ``health()`` 里
    ``if database == "ok"`` 的门控依赖它自己的 ``SELECT 1`` 探活 —— 该探活用的是
    **同一个被替换的 ``SessionLocal`` 名** ⇒ 现在能过，是因为「回的就是那个真 session」。
    ⇒ **本替换依赖 ``health()`` 的探活与 ``clean_status()`` 共用同一个 ``SessionLocal`` 名**；
    **若将来 ``health()`` 改成各自取 session，本锚会静默失效** ⇒ 那时须改用 (甲) 单独取证
    （即只用 ``api.ts`` 键集锚 + 直调 ``clean_status()``，不再打 health）。
    """

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False
