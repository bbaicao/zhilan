"""`回归修复` 的永久回归钉：**缓存指纹两侧同源（读侧归一）**。

## 缺陷是什么（缺陷清单-2 · 六A，2026-09-28）

写侧指纹（`chat_routes._cache_write_entry`）算的是 `Material.content`；读侧存活校验
（`entry.fingerprints_live`）原先只按「库内正文**全量**」算一个指纹。而**同一份库内正文在不同
生产方手里会变成不同长度的文本**（2026-09-28 探针实测，四种形态）：

| 材质生产方 | 交出来的 `content` | 实测 |
|---|---|---|
| 主命中块（`keyword.py:277` / `vector.py:182`） | 库内正文**原样全量**（不 strip、不截断） | `chunk_document` 出块 ≤ 800（探针：最大 800 / 超 800 块数 0） |
| 图通道文档块（`graph._node_materials`） | 同上（`row[..] or ""`） | 900 字块材料 900 字，与库内逐字相同 |
| 图通道笔记（`graph.py:625`） | `(note.content or "").strip()` **全量不截断** | 905 字源 → 材料 901 字 |
| **一跳扩展片段**（`link.py:140` 笔记 / `:199` 文档） | `strip()[:LINK_SNIPPET_CHARS]`（800） | 900 字源 → 材料 800 字；`fp(900字) != fp(800字截断)` |

⇒ **含 >800 字扩展片段的轮**：写侧指纹 = `fp(800 字截断)`、读侧 = `fp(库内全量)`
⇒ 必然失配 ⇒ `store.lookup` 判 stale 删条目 ⇒ **该轮永久 miss**（方向安全：只少命中、
不出错答；但把整轮缓存白扔了）。探针实测：写侧 `855be396…` / 读侧 `194c726b…` /
`fingerprints_live = False`。

⚠️ 写侧改不了：`plan.materials` 里**只有**已经投影过的文本，**拿不到**截断点之后的正文
（生产方当场截掉了）⇒ 按计划走**读侧归一**。

## 修法（本文件钉住的形态）

读侧对每个键算**产侧可能的两种投影**组成候选集，**命中任一即算活**（`entry.project_material_text`
就是「一跳扩展片段」那个投影，上限复用唯一定义处 `pipeline/wikilink.py::LINK_SNIPPET_CHARS`）：

- chunk 键：`{fp(库内原样), fp(strip(库内)[:800])}`
- note 键：`{fp(strip(库内)), fp(strip(库内)[:800])}`

## 腿构成（13 例 · 修前「6 红 / 4 绿」；`task-24` 之后补齐 11–13 腿）

| # | 用例 | 修前 | 作用 |
|---|---|---|---|
| 1 | >800 字**文档**一跳扩展片段 ⇒ 判活（含「截断指纹 ≠ 全量指纹」前提） | **红** | 缺陷本体 |
| 2 | >800 字**笔记**一跳扩展片段 ⇒ 判活 | **红** | 缺陷本体（笔记支路） |
| 3 | 主命中块 ≤800 仍活 + 块被硬删仍判不活 | 绿 | 回归 + 对照 |
| 4 | **>800 代码块**主命中仍活；**改动第 800 字之后 ⇒ 仍判不活** | 绿 | 断言「>800 主命中没有新增窗口」 |
| 5 | 图通道笔记（strip 全量、>800）仍活 | 绿 | 回归 |
| 6 | 内容被改（头部）⇒ 判不活 | 绿 | 负控：防「一律判活」的假修法 |
| 7 | 一跳扩展片段：**截断点之后**变化 ⇒ 判活 | **红** | 钉住**固有代价①**（写侧根本没有那段正文） |
| 8 | 追加的边界：**短于 800** 被追加 ⇒ 判不活；**恰好 800** 被追加 ⇒ 判活 | **红** | 钉住**新窗口②**的精确边界（登记在案，非期望行为） |
| 9 | 库内正文带首尾空白、写侧按 strip 投影 ⇒ 判活 | **红** | strip 不对称的覆盖 |
| 10 | 读侧投影 == 两个生产点实际交出来的 `content` | **红** | **同源钉**（防两侧各写一套 800 规则漂移） |
| 11 | 图通道 **strip 全量** 形态（>800 字、库里带首尾空白）⇒ 判活 | **红** | `task-24` 补漏（枚举完整性） |
| 12 | keyword 路 **raw 笔记** 形态（带尾随换行）⇒ 判活 | **红** | `task-24` 补漏：**实测的活缺陷**（补前永久 miss） |
| 13 | 只差**首尾空白**的改动（>800 字）⇒ 判活 | **红** | `task-24` 的**新代价③**（登记，非期望行为） |

## 已登记口径（交付同步）

- **固有代价①**：一跳扩展片段截断点之后的变化不可见——写侧根本没拿到那段正文，本笔修不掉
  （修复前是更保守的「整条永久 miss」）。
- **新窗口②**：写侧文本**恰好 800 字**（`RAG_CHUNK_SIZE` 的满块就是这种）时，库内正文在其后
  **末尾追加**（前 800 字逐字未变）会命中截断候选 ⇒ 判活。**短于 / 长于 800 字都没有这个窗口**；
  要彻底消掉 ② 必须让写侧把「材质文本长度」一并记进指纹（动 `CacheEntry.fingerprints` 的
  取值格式 + 版本号 + 写回侧），**本笔不做**。
  **把第 800 字之后改掉 / 删掉仍会被检出**。写侧材质**本身 >800** 时（`chunk_code` 的 4000 字符窗）**也没有新增窗口**；
  库内正文 ≤800 且无首尾空白时两个候选恒等 ⇒「内容变了 ⇒ 判不活」一点没放松。
- **新代价③（`task-24` 补）**：候选集同时含 `fp(raw)` 与 `fp(strip(raw))` ⇒ 库内正文只在**首尾**
  多/少空白时判活（`>800` 的正文才**新**暴露这一窗口；`≤800` 时截断候选本就等于 strip 候选）。
  取舍：不补这两个候选的代价是「该形态**永久 miss**」（已在生产入口实测：带尾随换行的笔记经 keyword 路
  交出 raw ⇒ 补漏前存活校验 False），首尾空白不进正文语义，影响面更小。
- 写侧三种形态的**枚举以生产方为准**（`keyword._row_to_material`/`_note_to_material` = raw ·
  `graph.py:652/625` = strip 全量 · `link.py:199/140` = strip+截断）。
- `task-24` **零写侧改动**、**零 cache 端点行为改动**，只动读侧一个函数的候选集与注释。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.cache import entry as entry_mod
from app.models import Chunk, Note
from app.pipeline.chunking import chunk_code
from app.retrieval import graph as graph_mod
from app.retrieval import keyword as keyword_mod
from app.retrieval import link as link_mod
from tests.retrieval.conftest import seed_document

pytestmark = pytest.mark.asyncio

#: 一跳扩展片段的截断上限（唯一定义处，别在本文件里再写一个 800）。
SNIPPET = link_mod.LINK_SNIPPET_CHARS


def _fp(text: str) -> str:
    return entry_mod.content_fingerprint(text)


async def _chunk_id_of(db, document_id: int) -> int:
    row = (
        await db.execute(
            select(Chunk.id).where(Chunk.document_id == document_id).order_by(Chunk.ordinal).limit(1)
        )
    ).scalar_one()
    return int(row)


# ==========================================================================
# 一、缺陷本体：>800 字一跳扩展片段（文档 / 笔记两支）
# ==========================================================================


async def test_document_link_fragment_over_800_is_alive(db):
    """**缺陷本体（文档支路）**：>800 字关联片段的写侧指纹必须被判「活」（修复前必红）。

    前提腿先把「截断指纹 ≠ 全量指纹」摆出来 —— 没有它，这条用例的「红」可能是别的原因。
    """
    raw_900 = "甲" * 900
    document = await seed_document(db, [raw_900], filename="bug140-doc.md", start_ordinal=0)
    assert _fp(raw_900) != _fp(raw_900[:SNIPPET]), (
        "前提：900 字与它的 800 字截断版指纹必须不同（否则这条用例没有判别力）"
    )

    fragment = await link_mod._document_fragment(db, int(document.id), seed_score=1.0)
    assert fragment is not None, "前提：ordinal==0 的块应当能拉出一条一跳扩展片段"
    assert len(fragment.content) == SNIPPET and fragment.content == raw_900[:SNIPPET]

    chunk_key = str(await _chunk_id_of(db, int(document.id)))
    assert str(int(fragment.chunk_id)) == chunk_key, "前提：片段带的是真实 chunks.id（写侧键形态）"

    assert await entry_mod.fingerprints_live(db, {chunk_key: _fp(fragment.content)}) is True, (
        "含 >800 字一跳扩展片段的轮不该被永久判不活（回归修复）"
    )


async def test_note_link_fragment_over_800_is_alive(db):
    """**缺陷本体（笔记支路）**：>800 字笔记关联片段必须判「活」（修复前必红）。"""
    note = Note(title="bug140 笔记", content="  乙" + "丙" * 900 + "  ", category="", kb_id=1)
    db.add(note)
    await db.commit()

    fragment = await link_mod._note_fragment(db, int(note.id), seed_score=1.0)
    assert fragment is not None
    assert len(fragment.content) == SNIPPET, "前提：笔记片段应当是 strip 后再截 800"
    note_key = f"note:{int(note.id)}"

    assert await entry_mod.fingerprints_live(db, {note_key: _fp(fragment.content)}) is True, (
        "含 >800 字笔记扩展片段的轮不该被永久判不活（回归修复）"
    )


# ==========================================================================
# 二、回归与对照：既有形态不许被带坏
# ==========================================================================


async def test_main_hit_block_under_800_still_alive(db):
    """**回归 + 对照**：主命中块（≤800、库内原样）照旧判活；块被硬删仍判不活。"""
    await seed_document(db, ["丙" * 300, "丁" * 300], filename="bug140-main.md")
    rows = (await db.execute(select(Chunk.id, Chunk.content))).all()
    fingerprints = {str(int(r[0])): _fp(r[1]) for r in rows}
    assert len(fingerprints) == 2
    assert await entry_mod.fingerprints_live(db, fingerprints) is True

    missing = dict(fingerprints)
    missing["999999999"] = _fp("根本不存在的块")
    assert await entry_mod.fingerprints_live(db, missing) is False, "块被硬删 ⇒ 仍必须判不活"


async def test_code_chunk_over_800_main_hit_still_alive_and_tail_edits_detected(db):
    """**>800 代码块的主命中形态**：仍活；**把第 800 字之后改掉 ⇒ 仍判不活**。

    这是本笔「没有新增窗口」的**判据腿**：写侧材质本身 >800 时，截断候选（≤800 字）的指纹
    不可能等于它的指纹 ⇒ 尾部改动照样检出。
    """
    metas = chunk_code("x = " + "z" * 9000)
    biggest = max(metas, key=lambda item: len(item.text))
    assert len(biggest.text) > 800, "前提：chunk_code 必须能产出 >800 字的块（回归修复）"

    document = await seed_document(db, [biggest.text], filename="bug140-code.py")
    chunk_id = await _chunk_id_of(db, int(document.id))
    key = str(chunk_id)
    assert await entry_mod.fingerprints_live(db, {key: _fp(biggest.text)}) is True, (
        ">800 字代码块的主命中不许被误判为不活（读侧不能无条件 [:800]）"
    )

    chunk = (await db.execute(select(Chunk).where(Chunk.id == chunk_id))).scalars().one()
    chunk.content = biggest.text[:800] + "被改过的尾部" + "!" * 100
    await db.commit()
    assert await entry_mod.fingerprints_live(db, {key: _fp(biggest.text)}) is False, (
        ">800 字主命中块的第 800 字之后被改掉 ⇒ 必须判不活（本笔没有新增窗口）"
    )


async def test_graph_note_full_text_still_alive(db):
    """**回归**：图通道笔记（strip 后**全量不截断**、>800 字）照旧判活。"""
    note = Note(title="bug140 长笔记", content="戊" * 900, category="", kb_id=1)
    db.add(note)
    await db.commit()

    key = f"note:{int(note.id)}"
    assert await entry_mod.fingerprints_live(db, {key: _fp(note.content.strip())}) is True


async def test_content_change_is_detected(db):
    """**负控**：内容被改 ⇒ 判不活（防「候选集 = 一律判活」的假修法）。"""
    document = await seed_document(db, ["己" * 300], filename="bug140-edit.md")
    chunk_id = await _chunk_id_of(db, int(document.id))
    key = str(chunk_id)
    assert await entry_mod.fingerprints_live(db, {key: _fp("己" * 300)}) is True

    chunk = (await db.execute(select(Chunk).where(Chunk.id == chunk_id))).scalars().one()
    chunk.content = "改过的正文"
    await db.commit()
    assert await entry_mod.fingerprints_live(db, {key: _fp("己" * 300)}) is False, (
        "≤800 的块内容一改就必须判不活（候选集在短正文上恒等，判据不放松）"
    )


# ==========================================================================
# 三、把两条「代价」也钉住（登记，不粉饰）
# ==========================================================================


async def test_link_fragment_tail_change_is_the_registered_inherent_cost(db):
    """**固有代价①（登记）**：一跳扩展片段**截断点之后**的变化看不见。

    写侧当时拿到的就只有前 800 字 ⇒ 后面那段怎么变都不可能被察觉。
    本腿钉的是**已知代价**，不是期望行为（期望行为是「不返回陈旧答案」，但信息本身不存在）。
    """
    raw_900 = "庚" * 900
    document = await seed_document(db, [raw_900], filename="bug140-tail.md", start_ordinal=0)
    key = str(await _chunk_id_of(db, int(document.id)))
    write_side = _fp(raw_900[:SNIPPET])  # 一跳扩展片段交出来的那一份

    chunk = (await db.execute(select(Chunk).where(Chunk.id == int(key)))).scalars().one()
    chunk.content = raw_900[:SNIPPET] + "截断点之后被改掉了"
    await db.commit()

    assert await entry_mod.fingerprints_live(db, {key: write_side}) is True, (
        "登记口径：截断点之后的变化不可见（写侧根本没有那段正文）——这条红说明登记过期了"
    )


async def test_appended_tail_window_is_bounded_by_the_snippet_limit(db):
    """**新窗口②（登记）与它的边界**：写侧文本**恰好 800 字**时，「末尾追加」才会命中截断候选
    ⇒ 判活；**短于 800 字的写侧文本被追加时照样检出**。

    ⚠️ 前半是本笔的**代价**（修复前会判不活）；后半是**边界**（证明不是「凡追加必判活」）。
    """
    # --- 边界之外：300 字原文 + 末尾追加 ⇒ 必须检出（截断候选 = 整段新正文，与写侧 300 字不等） ---
    short = "辛" * 300
    doc_short = await seed_document(db, [short], filename="bug140-append-short.md")
    key_short = str(await _chunk_id_of(db, int(doc_short.id)))
    chunk_short = (await db.execute(select(Chunk).where(Chunk.id == int(key_short)))).scalars().one()
    chunk_short.content = short + "后来又追加了很多内容" + "壬" * 600
    await db.commit()
    assert await entry_mod.fingerprints_live(db, {key_short: _fp(short)}) is False, (
        "写侧文本短于 800 时末尾追加必须被检出 —— 否则「候选集」就成了「凡追加必判活」"
    )

    # --- 窗口之内：写侧文本**恰好 800 字** + 末尾追加 ⇒ 命中截断候选（登记口径） ---
    exact = "癸" * SNIPPET
    doc_exact = await seed_document(db, [exact], filename="bug140-append-exact.md")
    key_exact = str(await _chunk_id_of(db, int(doc_exact.id)))
    write_side = _fp(exact)
    chunk_exact = (await db.execute(select(Chunk).where(Chunk.id == int(key_exact)))).scalars().one()
    chunk_exact.content = exact + "后来又追加了很多内容"
    await db.commit()
    assert await entry_mod.fingerprints_live(db, {key_exact: write_side}) is True, (
        "登记口径：写侧文本恰好达到截断上限时，「末尾追加」会命中截断候选 ⇒ 判活（这条红=登记过期）"
    )

# ==========================================================================
# 四、strip 不对称与「同源」钉
# ==========================================================================


async def test_whitespace_padded_chunk_matches_the_stripped_projection(db):
    """**strip 不对称的覆盖**：库内正文带首尾空白、写侧按 strip 投影 ⇒ 必须判活。

    一跳扩展片段的生产方先 strip 再截；库内行若残留首尾空白，修复前同样必然失配。
    """
    document = await seed_document(db, ["  癸字正文（前后带空白）  "], filename="bug140-ws.md")
    key = str(await _chunk_id_of(db, int(document.id)))
    stripped = "  癸字正文（前后带空白）  ".strip()
    assert await entry_mod.fingerprints_live(db, {key: _fp(stripped)}) is True


async def test_read_side_projection_equals_the_producers(db):
    """**同源钉**：读侧投影 == 两个生产点实际交出来的 `content`（防两侧规则漂移）。"""
    raw_900 = "子" * 900
    document = await seed_document(db, [raw_900], filename="bug140-same.md", start_ordinal=0)
    note = Note(title="bug140 同源", content="  丑" + "寅" * 900 + "  ", category="", kb_id=1)
    db.add(note)
    await db.commit()

    document_fragment = await link_mod._document_fragment(db, int(document.id), seed_score=1.0)
    note_fragment = await link_mod._note_fragment(db, int(note.id), seed_score=1.0)

    assert entry_mod.project_material_text(raw_900) == document_fragment.content, (
        "读侧投影必须与 retrieval/link.py 文档支路逐字一致"
    )
    assert entry_mod.project_material_text(note.content) == note_fragment.content, (
        "读侧投影必须与 retrieval/link.py 笔记支路逐字一致"
    )
    assert entry_mod.project_material_text(raw_900) == raw_900.strip()[:SNIPPET]
    assert entry_mod.project_material_text("  短正文  ") == "短正文", "≤800 时投影只做 strip"

# ==========================================================================
# 五、task-24 补齐：写侧另外两种形态 + 新登记的代价
# ==========================================================================


async def test_graph_channel_stripped_chunk_shape_is_alive(db):
    """**`task-24` 补漏**：图通道文档块交的是 **strip 全量**（`graph.py:652`）⇒ 必须判活。

    与 keyword 路的 raw 不同。当前所有 chunk 写入都会 strip ⇒ 两形态平时恒等；
    本腿用**库内带首尾空白 + >800 字**的行把差别造出来（>800 才能把「strip 全量」与
    「strip+截断」两个候选区分开——`≤800` 时它们本就相等，腿会失去判别力）。
    """
    raw = "  " + "子" * 900 + "  "
    document = await seed_document(db, [raw], filename="bug140-graph-strip.md", start_ordinal=0)
    key = str(await _chunk_id_of(db, int(document.id)))

    node = graph_mod._Node(
        kind="document", source_id=int(document.id), kb_id=1, title="bug140-graph-strip.md", category=""
    )
    materials = await graph_mod._node_materials(db, node, 0.5, 4, set())
    assert materials, "前提：图通道应当为该文档产出材料"
    stripped = materials[0].content
    assert stripped == raw.strip() and stripped != raw, "前提：图通道交出来的必须是 strip 后的文本"
    assert len(stripped) > SNIPPET, "前提：正文要 >800 字，否则截断候选会掩盖本腿"

    assert await entry_mod.fingerprints_live(db, {key: _fp(stripped)}) is True, (
        "图通道（strip 全量）那一轮不许永久 miss（task-24 补漏）"
    )


async def test_keyword_raw_note_shape_is_alive(db):
    """**`task-24` 补漏（实测为活缺陷）**：keyword 路笔记交的是 **raw** ⇒ 必须判活。

    笔记正文**逐字落库**（`api/note_routes.py:202/221` 不 strip），而 `keyword._note_to_material`
    取 `row[2] or ""`（不 strip）⇒ 带尾随换行的笔记写侧指纹是 `fp(raw)`；
    补漏前读侧只算 strip 口径 ⇒ 实测 `存活校验 False` ⇒ **该笔记参与的整轮永久 miss**。
    """
    note = Note(title="bug140 raw 笔记", content="被 keyword 路交出来的笔记正文。\n", category="", kb_id=1)
    db.add(note)
    await db.commit()

    rows = await keyword_mod._load_note_corpus(db, None)
    assert len(rows) == 1, "前提：本用例的独立测试库里只应有这一条笔记"
    material = keyword_mod._note_to_material(rows[0], score=1.0)
    assert int(material.note_id) == int(note.id)
    assert material.content == note.content, "前提：keyword 路交出来的必须是库内原样（含尾随换行）"
    assert material.content != material.content.strip()

    key = f"note:{int(note.id)}"
    assert await entry_mod.fingerprints_live(db, {key: _fp(material.content)}) is True, (
        "带尾随换行的笔记经 keyword 路进缓存 ⇒ 不许永久 miss（task-24 实测为活缺陷）"
    )


async def test_whitespace_only_change_is_the_registered_cost(db):
    """**新代价③（登记）**：**>800 字**正文只差首尾空白 ⇒ 判活。

    这条钉的是 `task-24` 补 `fp(strip(raw))` 带来的**新窗口**（补漏前这种改动会被检出）：
    首尾空白不进正文语义（渲染前一律 strip），影响面远小于「该形态永久 miss」。
    这条红 = 登记过期（候选集不再含 strip 候选）。
    """
    original = "卯" * 900
    document = await seed_document(db, [original], filename="bug140-ws-cost.md")
    key = str(await _chunk_id_of(db, int(document.id)))
    write_side = _fp(original)

    chunk = (await db.execute(select(Chunk).where(Chunk.id == int(key)))).scalars().one()
    chunk.content = "  " + original
    await db.commit()

    assert await entry_mod.fingerprints_live(db, {key: write_side}) is True, (
        "登记口径：>800 字正文只差首尾空白 ⇒ 判活（第 13 腿）——这条红说明登记过期了"
    )