"""条目 CX-06′：材料**单条正文上限**（专项-上下文管理升级 · 档 2）。

被测对象 = ``app/retrieval/types.py`` 的 :func:`clip_material_body` /
:func:`render_materials`（+ 缓存结构版本口径）。

约束：命中回填仍须遵守上限；裁剪不得把材料裁为空，
并且只能在块内裁剪，不能丢弃整条来源。

| 用例 | 判据 |
|---|---|
| ``test_c1_under_cap_is_byte_identical`` | 未超限 ⇒ **逐字节不变**（正常材料零影响） |
| ``test_c2_over_cap_is_clipped_and_no_item_is_dropped`` | 超限 ⇒ 裁到上限内、**条数不变**、编号 ``[n]`` 完整 |
| ``test_c3_marker_carries_original_length`` | 标记写明**原字符数**（可读中文，不静默截断） |
| ``test_c4_head_and_tail_both_kept`` | **头尾都留**（不是「只留头」） |
| ``test_c5_all_source_kinds_are_capped`` | 文档 / 网页 / **笔记**（笔记取全文 ⇒ 最易无界）三类同受上限 |
| ``test_c6_empty_body_keeps_only_head_line`` | 空正文只留标记行（不产生悬空空行） |
| ``test_c7_pure_function`` | 同输入两次逐字节相同 |
| ``test_a8_cache_schema_bump_discards_pre_cap_snapshots`` | 结构版本 bump ⇒ 旧快照**按未命中丢弃**（命中轮不再回填超限正文） |
| ``test_m1_mutation_proof_** | **变异自证**：把裁剪换成恒等 ⇒ C2 真会红 |
| ``test_m2_mutation_proof_** | **变异自证**：改成「丢条」⇒ I3 同源（条数/编号）真会红 |

⚠️ 本文件**不连库、不连服务**：A8 走的是 ``store.lookup`` 在**版本不匹配时提前返回**的那条分支
（``cache/store.py:191-195``），它**在触库之前**就丢弃条目。
"""

from __future__ import annotations

import dataclasses
import time

import pytest

from app.cache import entry as entry_mod
from app.cache import store as store_mod
from app.core.config import settings
from app.retrieval import types as t

HEAD_1 = "[1]（文档：a.md）"
HEAD_2 = "[2]（文档：b.md）"


def _doc(serial: int, body: str, *, filename: str = "", **extra) -> t.Material:
    """造一条文档材料（``content`` 即正文；``filename`` 缺省按序号给）。"""
    return t.Material(
        chunk_id=serial,
        document_id=100 + serial,
        content=body,
        filename=filename or f"{chr(96 + serial)}.md",
        **extra,
    )


def _over_cap_body(extra: int = 500) -> str:
    return "长" * (t.MATERIAL_ITEM_MAX_CHARS + extra)


def test_c1_under_cap_is_byte_identical():
    """C1：未超限 ⇒ 渲染结果与「旧写法」（标记行 + 归一化正文）**逐字节相同**。"""
    body = "正文" * 50
    rendered = t.render_materials(t.number([_doc(1, body)]))
    assert rendered == HEAD_1 + "\n" + body
    assert t.MATERIAL_ITEM_TRUNCATION_MARK.split("{")[0] not in rendered


def test_c2_over_cap_is_clipped_and_no_item_is_dropped():
    """C2：超限只裁**这一条**，**条数不变**、编号 1..N 完整（I3 同源），正文长度 ≤ 上限。"""
    long_body = _over_cap_body()
    items = t.number([_doc(1, long_body), _doc(2, "短正文")])
    rendered = t.render_materials(items)

    blocks = rendered.split("\n\n")
    assert len(blocks) == 2, f"条数必须不变（宁裁不丢）：{len(blocks)}"
    assert blocks[0].startswith(HEAD_1 + "\n"), blocks[0][:40]
    assert blocks[1].startswith(HEAD_2 + "\n"), blocks[1][:40]

    body_1 = blocks[0].split("\n", 1)[1]
    assert len(body_1) <= t.MATERIAL_ITEM_MAX_CHARS, len(body_1)
    assert body_1 != long_body, "超限条必须真的被裁（否则上限形同虚设）"
    assert blocks[1].split("\n", 1)[1] == "短正文", "正常条不得被牵连"


def test_c2b_capped_materials_stay_non_empty_for_prompt():
    """C2b（审计 F-07）：只裁不丢 ⇒ ``prompt_has_material`` 的判据**不被本上限翻转**。

    ``answer.build_messages`` / ``system_prompt`` 靠「渲染正文非空」决定说「有资料」还是「没有资料」
    （``answer.py:200-205``、``prompt_has_material``；``chat_routes.py:1046`` 点名「评审判据 T1/A21」）。
    若上限改成「超限就丢条」，一条超长材料的会话会变成「系统提示说没资料、消息里也没资料」——
    那不是裁短，是把**有依据的回答**降级成**凭空作答**。
    """
    from app.generation.answer import prompt_has_material

    rendered = t.render_materials(t.number([_doc(1, _over_cap_body(100_000))]))
    assert prompt_has_material(rendered) is True, "裁完仍非空 ⇒ 仍走「有资料」分支"


def test_c3_marker_carries_original_length():
    """C3：标记写明**原字符数**（同 ``TOOL_CONTEXT_TRUNCATION_MARK`` 的口径：不静默截断）。"""
    long_body = _over_cap_body()
    rendered = t.render_materials(t.number([_doc(1, long_body)]))
    mark = t.MATERIAL_ITEM_TRUNCATION_MARK.format(chars=len(long_body))
    assert mark in rendered, "标记必须写明原量"
    assert str(len(long_body)) in rendered


def test_c4_head_and_tail_both_kept():
    """C4：**头尾都留**（只留头会把结论段/表尾整段丢掉）。"""
    head_text = "【开头】" + "甲" * (t.MATERIAL_ITEM_MAX_CHARS // 2)
    tail_text = "乙" * (t.MATERIAL_ITEM_MAX_CHARS // 2) + "【结尾】"
    rendered = t.render_materials(t.number([_doc(1, head_text + tail_text)]))
    assert head_text[:20] in rendered, "头部特征串必须在"
    assert tail_text[-20:] in rendered, "尾部特征串必须在"


@pytest.mark.parametrize(
    "kind",
    ["doc", "web", "note"],
)
def test_c5_all_source_kinds_are_capped(kind: str):
    """C5：文档 / 网页 / **笔记** 三类来源同受单条上限（笔记材料取全文 ⇒ 最易无界）。"""
    body = _over_cap_body()
    if kind == "doc":
        item = _doc(1, body)
    elif kind == "web":
        item = _doc(1, body, filename="网页标题", url="https://example.com/a")
    else:
        note_id = 7
        item = t.Material(
            chunk_id=t.note_chunk_id(note_id),
            document_id=0,
            content=body,
            filename="我的笔记",
            note_id=note_id,
        )
    rendered = t.render_materials(t.number([item]))
    assert len(rendered.split("\n\n")) == 1
    assert len(rendered.split("\n", 1)[1]) <= t.MATERIAL_ITEM_MAX_CHARS
    assert t.MATERIAL_ITEM_TRUNCATION_MARK.format(chars=len(body)) in rendered


def test_c6_empty_body_keeps_only_head_line():
    """C6：正文为空 ⇒ 只留标记行（不留悬空空行）——既有口径不得被本上限改动。"""
    rendered = t.render_materials(t.number([_doc(1, "   \n\n  ")]))
    assert rendered == HEAD_1


def test_c7_pure_function():
    """C7：纯函数 —— 同输入两次调用**逐字节相同**（不读全局、不随机）。"""
    items = t.number([_doc(1, _over_cap_body()), _doc(2, "短")])
    assert t.render_materials(items) == t.render_materials(items)
    assert t.clip_material_body("x" * 5000) == t.clip_material_body("x" * 5000)
    assert t.clip_material_body("短") == "短", "未超限必须原样返回"


@pytest.mark.asyncio
async def test_a8_cache_schema_bump_discards_pre_cap_snapshots(monkeypatch):
    """A8：``CACHE_SCHEMA_VERSION`` 已 bump ⇒ **上限之前**写入的快照按「未命中」丢弃。

    为什么必须钉这条：命中路径把 ``hit.entry.materials_text`` **原样回填**
    （``chat_routes`` 的 ``materials_text=hit.entry.materials_text``）⇒ 旧快照会**绕过**新上限，
    而命中轮恰恰是快路径。bump 版本 = 一次性作废旧条目，代价只是少命中一轮。
    """
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", True)
    store_mod.reset_cache()
    entry = entry_mod.CacheEntry(created_at=time.monotonic(), materials_text=_over_cap_body(0))
    await store_mod.store_snapshot("k" * 16, entry)

    record = store_mod._STORE["k" * 16]
    store_mod._STORE["k" * 16] = dataclasses.replace(
        record, version=entry_mod.CACHE_SCHEMA_VERSION - 1
    )
    stale_before = store_mod.failure_counts().get("stale", 0)

    hit = await store_mod.lookup(None, "k" * 16)  # type: ignore[arg-type]

    assert hit is None, "版本不匹配的条目必须当未命中"
    assert "k" * 16 not in store_mod._STORE, "必须就地丢弃，不能留着每轮白查"
    assert store_mod.failure_counts()["stale"] == stale_before + 1
    assert entry_mod.CACHE_SCHEMA_VERSION >= 2, "上限上线 ⇒ 结构版本必须 bump（否则旧快照仍会命中）"


def test_m1_mutation_proof_clipping_would_go_red(monkeypatch):
    """M1 · 变异自证：**就地把坏实现打开**（裁剪换成恒等）⇒ C2 的两条断言真会红。"""
    long_body = _over_cap_body()
    items = t.number([_doc(1, long_body), _doc(2, "短正文")])

    monkeypatch.setattr(t, "clip_material_body", lambda body, limit=None: body)

    rendered = t.render_materials(items)
    blocks = rendered.split("\n\n")
    body_1 = blocks[0].split("\n", 1)[1]

    assert body_1 == long_body, "坏实现（不裁）必须真的原样放出长正文"
    assert len(body_1) > t.MATERIAL_ITEM_MAX_CHARS, "⇒ C2 的「长度 ≤ 上限」必红"


def test_m2_mutation_proof_dropping_the_item_would_go_red():
    """M2 · 变异自证：把「块内裁」换成「**整条丢**」⇒ 条数 / 编号断言真会红（I3 同源）。

    这条不是替生产代码开口子（「丢条」在现实现里不存在），而是**把那种坏实现摆出来**，
    证明 C2 的「条数不变」与编号断言**有判别力**——因为「丢条」会直接改写溯源面。
    """
    items = t.number([_doc(1, "短正文"), _doc(2, _over_cap_body())])
    dropped = [item for item in items if len(item.content) <= t.MATERIAL_ITEM_MAX_CHARS]
    rendered = t.render_materials(dropped)

    assert HEAD_2 not in rendered, "坏实现（丢条）必须真的让 [2] 消失"
    assert len(rendered.split("\n\n")) == 1, "⇒ C2 的「条数不变 = 2」必红"
