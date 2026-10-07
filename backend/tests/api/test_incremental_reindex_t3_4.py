"""`回归检查` · **单文件增量重建** 的判据载体 —— 工具升级-3 · 阶段 4 · 语料一致性轨。

判据（开工包 §1 判据表 / `D-回归检查` 定档取**丙**）：

| 腿 | 断言 | 变异靶子 |
|---|---|---|
| **增量性腿** | 未改动文档的 ``chunks`` **内容与条数逐字未变**、``documents.vectorized`` / ``chunk_count`` **未被触碰**；被选中文档**确进重建路径**（**桩记录**，**不看** ``ReindexResult`` 返回值） | **`M-回归检查`**（改成全量） |
| 🔴 **B1 内容刷新「硬闸腿」** | **断言「回读磁盘」当前不成立**：改盘上文件 ⇒ 调重建 ⇒ **库内 `chunks` 仍为旧值** | ——（**记录现状，不挂变异**） |

## 🔴 B1 硬闸腿的读法（**不得含糊**）

本件**不**声称「阶段 4 已解决 B1」，也**不**声称「改了文件检索就能拿到新内容」。
依据（现读逐字，`D-回归检查`）：
- ``backend/app/pipeline/reindex.py:109-110``：**默认路径** = 「读已有 chunks，调 ``index_chunks`` 覆盖重写向量」；
- ``backend/app/pipeline/ingest.py:224-241``：``index_chunks`` **只 select 库内既有 `Chunk`**（从不回读磁盘）；
- ``backend/app/pipeline/reindex.py:44-48``：唯一重解析分支的判据 ``_needs_reclean`` 比的是**清洗规则指纹**，**与磁盘内容无关**。

⇒ **硬闸腿的 docstring 逐字**（照开工包 `D-回归检查` 原文）：

> **若此腿转红，不是回归，是口径变更**（回读能力已落地）⇒ 改动者须同笔更新本包与小结件。

⚠️ **前提**：本阶段**不改** `reindex_documents` 的既有签名与既有调用点行为、**不动**全量 `reindex` 语义、
   **不**把重建注册成模型可自主调用的工具（单⑤(b) = **手动**，见 `D-回归检查` / 红线 13）。

## 🔴 口径登记：本件原有一版「装饰性断言」（B 路审计 REAL-⑪，已修）

**逐字登记（把踩过的坑留在代码里）**：

> 本件原有一版四条「逐字未变」断言在 `M-回归检查` 下**全部仍绿**（装饰性）—— 原因为 `index_chunks` 被
> 完全下桩 + `_seed` 初值已是 True；现补真写入面判据（**取「`_seed` 时把被选中文档的 `vectorized`
> 置 False ⇒ 重建后必须变 True，而未选中文档必须仍为 False」这一种**）使其对被测决策敏感。

**机理（为什么原来是装饰性）**：默认路径（`reindex.py:110-114`）在 `index_chunks` 被下桩后，
**唯一副作用**是 `doc.vectorized = ok`；`chunk_count` 不被碰、`chunks` 表**零写入**；
而旧版 `_seed` 把 `vectorized` 初值就设成 `True` ⇒ **写入值与初值逐字相同** ⇒ 四条观察量
对 `M-回归检查` **不敏感**（当时真正打红的只有桩断言 ⇒ 不是空靶，但**判据强度被高估**）。
⇒ 这是 §4 硬纪律①「**观察量必须对被测决策敏感**」的弱化版违反。

**修后的判别力归属（唯一靶腿纪律）**：
- `M-回归检查`（增量改全量）⇒ 打红 **① 桩断言**（`:183` 一带）**与** **①′ 真写入面断言**
  （未选中文档 `vectorized` 被翻）；② 那四条**仍不会红**（如实保留，见用例内注释）。
- 硬闸腿（`test_b1_hard_gate_*`）**不挂变异**（记录现状）。

U2 把 ``vectorized`` 写入移至共享索引发布入口；成功桩模拟已发布并提交，
增量选择器的目标/旁证观察量保持原有判别力。
"""

from __future__ import annotations

import pytest

# 端点层（``app/api/document_routes.py:45``）用的就是这个模块对象；此处直接 import 产品模块本身。
from app.pipeline import reindex as reindex_mod  # noqa: F401
from app.pipeline.ingest import CONTENT_HASH_VERSION
from app.models import Document as _Document
from tests.api.test_chat_contract import client  # noqa: F401 —— 复用 ASGI 客户端夹具

#: 被选中文档的正文（与「未改动文档」刻意用**不同**的句子，便于逐字比对）
TARGET_TEXT = "[库内目标] 目标文档的正文，重建之后它自己的内容不该被磁盘改写。"
#: 未改动的旁证文档正文（重建**不得**碰它）
BYSTANDER_TEXT = "[库内旁证] 旁证文档的正文，重建另一篇时它必须逐字不动。"
FROZEN_FP = "t3_4_frozen_rule_fingerprint"


def _freeze_rule_fingerprint(monkeypatch) -> str:
    """把 ``rule_fingerprint()`` 钉成固定串 ⇒ 让文档**稳态走默认路径**。

    ``reindex.py:90`` 的 ``_needs_reclean(report, current_fp)`` 拿 ``report['rule_fingerprint']``
    与 ``current_fp = rule_fingerprint()`` 比；**不等**就走 ``:93 if stale and stored:`` 的 **reclean**
    分支（``retry_document`` ⇒ 会重解析重切块，毁掉「内容逐字未变」的观察基线）。
    ⇒ 本助手把 ``rule_fingerprint()`` 钉成 :data:`FROZEN_FP`，并给文档写同一串 ⇒ **稳定走默认路径**。
    （`K-回归检查` 的教训：别只看签名，要看函数体走哪条分支。）
"""
    from app.pipeline import reindex as reindex_module

    monkeypatch.setattr(reindex_module, "rule_fingerprint", lambda: FROZEN_FP)
    return FROZEN_FP


async def _seed(
    db,
    text: str,
    *,
    filename: str = "增量重建.md",
    file_path: str | None = "D:/t3_4_workspace/增量重建.md",
    rule_fingerprint: str | None = FROZEN_FP,
    chunk_count: int = 2,
    vectorized: bool = False,
):
    """造一条「文件夹导入类」文档（``file_path`` 非空）+ 它的分块，**直接 commit**。

    ⚠️ 用 `test_documents_contract.py:203-215` 造数法的**等价形态**：真正要的是
    「``file_path`` 非空（= 有留档 ⇒ ``stored`` 为真）+ ``clean_report`` 带**当前**规则指纹」。
    这里直接建行而**不走 HTTP 导入**，是为了把「重建」这一步单独隔离出来观察
    （HTTP 那条路要跑解析/切块/embed，且本机 ``.env`` 的开关会污染观察量）。

    🔴 ``vectorized`` 默认 **``False``** —— 这是**判据强度**的要求，别改回 ``True``：
    成功索引桩模拟共享发布入口，将 ``vectorized`` 提交为 True；
    若初值已是 ``True``，写入值与初值**逐字相同** ⇒ 那条观察量
    变成**装饰性**（对被测决策不敏感）。置 ``False`` 后「重建确改了这一行」才**可观察**。

    ⚠️ **REAL-⑫ 警示（B 路审计）**：``file_path`` 指向的是**盘上不存在**的路径
    （``D:/t3_4_workspace/…``）。对本件两腿**无实质影响**（默认路径**不回读磁盘**，
    见 `reindex.py:109-110`）；**但**将来若有人拿这些行去跑「磁盘内容 vs ``raw_content``」
    比对（``stale_content_documents`` 一类），会因**指纹取不到 ⇒ fail-closed 判过期**而
    得到「全部过期」的假象。**别复用本助手造那种用例的数据。**
    """
    from app.models import Chunk, Document

    document = Document(
        filename=filename,
        file_type="md",
        status="ready",
        index_status="ready",
        file_path=file_path,
        # ⚠️ `documents.uk_documents_content_hash` 是**唯一索引** ⇒ 每篇必须唯一
        #    （原写死常量 ⇒ 同例建两篇即 `IntegrityError 1062`）。
        content_hash=f"t3_4_seed_hash_{filename}",
        raw_content=text,
        clean_report={
            "rule_fingerprint": rule_fingerprint,
            "content_identity": {"version": CONTENT_HASH_VERSION},
        } if rule_fingerprint else None,
        kb_id=1,
        chunk_count=chunk_count,
        vectorized=vectorized,
    )
    db.add(document)
    await db.flush()
    for offset in range(1, chunk_count + 1):
        db.add(Chunk(document_id=document.id, content=f"{text}（第 {offset} 块）", ordinal=offset, page=None))
    await db.commit()
    return document


async def _snapshot(db, document_id: int) -> dict:
    """取该文档的**可观察面**：(分块内容按 ordinal 排序的列表, 条数, vectorized, chunk_count)。

    ⚠️ 观察量**不含** ``index_status`` —— 该列被冻结契约明文禁止进出参
    （`test_documents_contract.py:117`），开工包判据表也已把它移出判据面
    （v1.1 写「断 ``index_status`` 未变」**不可判**；`K-回归检查` 另注：它是无枚举约束的 `String(32)`）。
"""
    from sqlalchemy import select

    from app.models import Chunk, Document

    rows = (
        (await db.execute(select(Chunk).where(Chunk.document_id == document_id).order_by(Chunk.ordinal)))
        .scalars()
        .all()
    )
    doc = await db.get(Document, document_id)
    return {
        "contents": [row.content for row in rows],
        "count": len(rows),
        "vectorized": doc.vectorized,
        "chunk_count": doc.chunk_count,
    }


class _IndexChunksRecorder:
    """``app.pipeline.reindex.index_chunks`` 的**桩**：记录目标并模拟成功发布后的提交。

    🔴 **为什么必须下桩**（开工包 `D-回归检查`）：``ReindexResult``（``reindex.py:31-41``）**只回聚合计数**
    （``documents`` / ``vectors``…），对**单个 id 无出口** ⇒ 只看返回值**证明不了**「重建只发生在被选中的
    那一篇上」。桩记录的是**函数级调用事实**，才对被测决策敏感。

    ⚠️ 桩吞掉 ``index_chunks`` 也**顺带**隔离了真 embed / 真 Qdrant（本仓测试一律不连真服务）。
"""

    def __init__(self) -> None:
        self.document_ids: list[int] = []

    async def __call__(self, session, document) -> tuple[bool, str | None]:  # noqa: ANN001
        self.document_ids.append(document.id)
        document.vectorized = True
        await session.commit()
        return True, None


# ---------------------------------------------------------------------------
# 增量性腿：只重建被选中文档；未改动文档逐字未变
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incremental_reindex_touches_only_the_selected_document(db, monkeypatch):
    """**增量性腿**：``document_id=<目标>`` ⇒ 重建路径**只**发生在该文档上，旁证文档**逐字未变**。

    两条断言缺一不可：
    ① **桩记录** ⇒ ``recorder.document_ids == [target.id]``（被选中文档**确进**重建路径，且**只有它**）；
    ② **观察量对比** ⇒ 旁证文档重建前后的 (内容, 条数, ``vectorized``, ``chunk_count``) **全等**。

    ⚠️ **不得**只看 ``ReindexResult`` —— 它只回聚合计数（见桩的 docstring）。

    ⇒ **`M-回归检查`（增量选择器改成全量）** 会在**两条**断言上变红：桩会多出旁证文档的 id，
       旁证文档的观察量也会被触碰。
    """
    from app.pipeline import reindex as reindex_module

    _freeze_rule_fingerprint(monkeypatch)
    target = await _seed(db, TARGET_TEXT, filename="目标.md")
    bystander = await _seed(db, BYSTANDER_TEXT, filename="旁证.md")

    before = await _snapshot(db, bystander.id)
    assert before["count"] == 2, "前置：旁证文档得有分块（否则本用例是空靶）"

    recorder = _IndexChunksRecorder()
    monkeypatch.setattr(reindex_module, "index_chunks", recorder)

    result = await reindex_module.reindex_documents(db, document_id=target.id)

    # ① 被选中文档**确进**重建路径 —— 用桩，不用返回值
    assert recorder.document_ids == [target.id], (
        f"重建路径处理了 {recorder.document_ids}，期望恰好 [{target.id}]（`M-T4-4` 全量变异会多出旁证 id）"
    )
    assert result.documents == 1, f"聚合读数应当只有 1 篇，实际 {result.documents}"
    assert result.failed == 0, f"本条不该有失败：{result.messages}"

    # ①′ **真写入面**（本条对 `M-回归检查` **唯一敏感**的观察量，见模块 docstring 的口径登记）：
    #    被选中文档 `_seed` 时 `vectorized=False` ⇒ 成功发布桩真把它提交为 True；
    #    而**未选中文档必须仍未 False**。全量变异下旁证文档也会被翻 ⇒ **这条真红**。
    target_row = await db.get(_Document, target.id)
    bystander_row = await db.get(_Document, bystander.id)
    assert target_row.vectorized is True, (
        "被选中文档重建后应被翻成 `vectorized=True`（真写入面没发生 ⇒ 本用例对 `M-T4-4` 会失去判别力）"
    )
    assert bystander_row.vectorized is False, (
        "🔴 未选中文档的 `vectorized` 被翻动了 ⇒ 增量选择器**改成全量**了（`M-T4-4`）"
    )

    # ② 未改动文档**逐字未变**（⚠️ 见模块 docstring：这四条在 `M-回归检查` 下**原本全部仍绿**，
    #    属**装饰性**；真正打红的是 ① 的桩断言与 ①′ 的真写入面断言。保留它们是为**语义完整性**，
    #    并防「将来有人让重建真改 chunks」时无人察觉。）
    after = await _snapshot(db, bystander.id)
    assert after["contents"] == before["contents"], "旁证文档的分块**内容**被改了（增量性破了）"
    assert after["count"] == before["count"], "旁证文档的分块**条数**变了（增量性破了）"
    assert after["vectorized"] == before["vectorized"], "旁证文档的 `vectorized` 被触碰了"
    assert after["chunk_count"] == before["chunk_count"], "旁证文档的 `chunk_count` 被触碰了"


@pytest.mark.asyncio
async def test_incremental_reindex_leaves_other_documents_rows_identical(db, monkeypatch):
    """**增量性腿（补）**：旁证文档的 ``chunks`` **行 id 集合**也不变（不是删了重插）。

    「内容与条数未变」还不足以排除**删旧行 + 插等长新行** —— 那样 §3.3 的引用链会整体失效
    （旧的 ``chunk_id`` 悬空）。故补一条**行身份**断言。
    """
    from sqlalchemy import select

    from app.models import Chunk
    from app.pipeline import reindex as reindex_module

    _freeze_rule_fingerprint(monkeypatch)
    target = await _seed(db, TARGET_TEXT, filename="目标.md")
    bystander = await _seed(db, BYSTANDER_TEXT, filename="旁证.md")

    async def _ids(document_id: int) -> list[int]:
        return list(
            (await db.execute(select(Chunk.id).where(Chunk.document_id == document_id).order_by(Chunk.id)))
            .scalars()
            .all()
        )

    before_ids = await _ids(bystander.id)
    assert before_ids, "前置：旁证文档得有分块（否则是本用例的空靶）"

    monkeypatch.setattr(reindex_module, "index_chunks", _IndexChunksRecorder())
    await reindex_module.reindex_documents(db, document_id=target.id)

    assert await _ids(bystander.id) == before_ids, "旁证文档的分块**行身份**变了（删旧插新 ⇒ 引用链会悬空）"


@pytest.mark.asyncio
async def test_reindex_all_still_covers_every_document(db, monkeypatch):
    """**既有语义不破**：``document_id=None`` 仍是**全量**（本笔**不动**全量 `reindex` 语义）。

    这条是**对照腿**：它保证上一条的「只碰一篇」不是因为「重建路径压根不工作」而**假绿**。
    """
    from app.pipeline import reindex as reindex_module

    _freeze_rule_fingerprint(monkeypatch)
    first = await _seed(db, TARGET_TEXT, filename="甲.md")
    second = await _seed(db, BYSTANDER_TEXT, filename="乙.md")

    recorder = _IndexChunksRecorder()
    monkeypatch.setattr(reindex_module, "index_chunks", recorder)

    result = await reindex_module.reindex_documents(db, document_id=None)

    assert sorted(recorder.document_ids) == sorted([first.id, second.id]), (
        f"全量重建应覆盖两篇，实际处理了 {recorder.document_ids}"
    )
    assert result.documents == 2, f"全量读数应当是 2，实际 {result.documents}"


# ---------------------------------------------------------------------------
# 🔴 B1 内容刷新「硬闸腿」（必须落，**记录现状**）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b1_hard_gate_disk_edits_are_not_read_back(db, monkeypatch, tmp_path):
    """🔴 **B1 内容刷新「硬闸腿」**：**「回读磁盘」当前不成立** —— 改盘上文件 ⇒ 调重建 ⇒ **库内仍为旧值**。

    #### 逐字口径（照开工包 `D-回归检查` / `§2 回归检查` 交付面第 3 条，**不得改写**）

    > **若此腿转红，不是回归，是口径变更**（回读能力已落地）⇒ 改动者须同笔更新本包与小结件。

    #### 这条腿在断言什么

    它**不是**「期望永远是旧值」，而是**如实登记当前口径**：
    - ``reindex.py:109-110`` 默认路径读的是**库内既有** ``chunks``；
    - ``ingest.py:224-241`` 的 ``index_chunks`` **从不回读磁盘**；
    - 唯一重解析分支（``:93 if stale and stored``）的判据是**清洗规则指纹**，与磁盘内容**无关**。
    ⇒ 所以「**改了盘上文件**再调重建」**不会**更新库内内容。将来有人补上回读能力 ⇒ **本腿转红** ⇒
       按上面的逐字口径触发**口径变更提醒**（而**不是**被当成回归去修）。

    #### 构造（本机可真跑、不碰工作区真实文件）

    用 ``tmp_path`` 当「工作区」：建文档时盘上写**旧**内容 ⇒ **改盘上文件为新内容** ⇒
    调 ``reindex_documents(document_id=…)`` ⇒ 断言：
    ① 盘上**确已**是新内容（否则「改盘」这一步没生效，本腿是空靶）；
    ② 库内 ``chunks`` **仍是旧值**（**这就是硬闸**）。

    ⚠️ **不得**把「调用只发生在一个 id 上」当成「内容已刷新」（**两回事**，见 `D-回归检查` 明令禁止）；
       本腿与增量性腿**分属两个面**，互不替代。
"""
    from app.pipeline import reindex as reindex_module

    disk_old = "磁盘上的**旧**内容：这一句在重建之后不应当出现在库内。"
    disk_new = "磁盘上的**新**内容：重建本应把它读回来，但当前口径做不到。"

    workspace = tmp_path / "t3_4_workspace"
    workspace.mkdir()
    document_path = workspace / "磁盘稿.md"
    document_path.write_text(disk_old, encoding="utf-8")

    _freeze_rule_fingerprint(monkeypatch)
    document = await _seed(
        db,
        "[库内旧值] 这是入库时切出来的分块，重建之后应当**逐字不变**。",
        filename="磁盘稿.md",
        file_path=str(document_path),
    )
    before = await _snapshot(db, document.id)

    # —— 改盘上文件（磁盘内容真的变了）
    document_path.write_text(disk_new, encoding="utf-8")
    assert document_path.read_text(encoding="utf-8") == disk_new, "「改盘」这一步没生效 ⇒ 本腿会变成空靶"

    # —— 走既有产品路径调重建（不改签名、不改既有调用点行为）
    monkeypatch.setattr(reindex_module, "index_chunks", _IndexChunksRecorder())
    await reindex_module.reindex_documents(db, document_id=document.id)

    # —— 硬闸：库内内容**仍是旧值**
    after = await _snapshot(db, document.id)
    assert after["contents"] == before["contents"], (
        "🔴 B1 硬闸腿转红了：库内 `chunks` 被磁盘新内容更新了。\n"
        "   **这不是回归，是口径变更**（回读能力已落地）。\n"
        "   ⇒ 改动者须**同笔**更新开工包 `D-T4-7` 与阶段小结件，并重写本条腿的断言方向。"
    )
    assert disk_new not in "\n".join(after["contents"]), (
        "🔴 库内出现了磁盘上的新内容 ⇒ 同上：**口径变更**，不是回归。"
    )


@pytest.mark.asyncio
async def test_b1_hard_gate_reclean_branch_is_reachable(db, monkeypatch, tmp_path):
    """🔴 **B1 硬闸腿（旁证）**：**规则指纹过期**那条分支真实存在且被走到 —— 但它**照样不解决 B1**。

    依据 `D-回归检查` 的 **(甲) 行**（已被否证为解法）：``retry_document`` 重读的是
    ``backend/app/pipeline/storage.py`` 的**内容寻址留档副本**，**不是工作区原文件**。
    本条只**记录**「另一条分支存在、且被走到」，保证「B1 未解决」这一登记**不是**独腿独撑。

    ⚠️ 注意：本节**下桩** ``retry_document`` ⇒ 库内内容必然未变；这条**证明的是「分支存在且被走到」**，
       不是「内容刷新失败」——「内容刷新失败」由 `test_b1_hard_gate_disk_edits_are_not_read_back` 承载。
"""
    from types import SimpleNamespace

    from app.pipeline import reindex as reindex_module

    document_path = tmp_path / "留档稿.md"
    document_path.write_text("盘上的内容。", encoding="utf-8")

    _freeze_rule_fingerprint(monkeypatch)
    document = await _seed(
        db,
        "[库内旧值] reclean 分支的观察样本。",
        filename="留档稿.md",
        file_path=str(document_path),
        rule_fingerprint="t3_4_STALE_fingerprint",
    )
    before = await _snapshot(db, document.id)

    calls: list[int] = []

    async def _fake_retry(session, document_id):  # noqa: ANN001
        calls.append(document_id)
        refreshed = await session.get(_Document, document_id)
        refreshed.vectorized = True
        await session.commit()
        return SimpleNamespace(chunk_count=before["count"], vectorized=True)

    monkeypatch.setattr(reindex_module, "retry_document", _fake_retry)
    result = await reindex_module.reindex_documents(db, document_id=document.id)

    assert calls == [document.id], "指纹过期 + 有留档 ⇒ 本应走 reclean 分支（构造没生效，本腿是空靶）"
    assert result.recleaned == 1, f"`recleaned` 应当是 1，实际 {result.recleaned}"
    after = await _snapshot(db, document.id)
    assert after["contents"] == before["contents"], "本腿下桩后库内内容不该变（变了说明有别的写入面）"


# ---------------------------------------------------------------------------
# 边界自查：不得把重建注册成模型可自主调用的工具（`D-回归检查` / 红线 13）
# ---------------------------------------------------------------------------


def test_reindex_is_not_registered_as_a_model_tool():
    """**单⑤(b) = 手动** ⇒ 重建入口**不得**进 ``for_model()`` 工具清单（**零新增**）。

    红线 13：不得把「用户已确认」的判据放在只有模型能读到的通道上 —— 所以「模型自己决定重建」
    这条路**结构性地**不许存在。
"""
    # ⚠️ `for_model` 的真身在 `app/agent/registry.py:81`（`ToolRegistry` 的方法）；
    #    注册表由 `app/agent/bootstrap.py:62` 的 `build_registry()` **每轮**构造
    #    —— **不存在** `app.agent.tools.registry` 这个模块（那是猜的）。
    from app.agent.bootstrap import build_default_registry

    specs = build_default_registry().for_model()
    names = {getattr(item, "name", "") for item in specs}
    assert names, "工具清单为空 ⇒ 本用例的观察量失效（空靶）"
    assert not any("reindex" in name.lower() or "rebuild" in name.lower() for name in names), (
        f"重建语义出现在模型工具清单里：{sorted(names)}"
    )


@pytest.mark.asyncio
async def test_reindex_endpoint_contract_shape_is_unchanged(client, db, monkeypatch):
    """**既有调用点行为未改**：``POST /api/documents/reindex`` 的响应键集与 ``02 §7.4`` 一致。

    本笔**不改** ``reindex_documents`` 的签名与既有调用点行为 ⇒ 契约面必须保持原样。
"""
    _freeze_rule_fingerprint(monkeypatch)
    await _seed(db, TARGET_TEXT, filename="契约面.md")

    resp = await client.post("/api/documents/reindex", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {
        "documents",
        "notes",
        "vectors",
        "failed",
        "orphans_removed",
        "recleaned",
        "messages",
    }, f"`ReindexOut` 键集变了：{sorted(body)}"
    assert body["notes"] == 0, "本阶段不重建笔记向量（与既有口径一致）"
    assert body["orphans_removed"] == 0, "覆盖写路径不做跨库孤儿扫描（与既有口径一致）"
