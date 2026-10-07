"""`回归修复` 批次三 · **#31 链接扩展失败**的接线判据（`retrieval/link.py` 的「已静默跳过」）。

🔴 口径（逐字，防误读）

· 钉的是「**降级出口存在且真的会动**」，**不是**「链接扩展失败已被消除」——扩展是纯增益步骤，
  F6-B 起就是**静默降级**（返回值仍是原主命中，不往 warnings 掺内部细节）；
· 键 = **既有键 `doc_links`**（键义 = 文档侧链接边维护失败；扩链就是这条边上的纯增益步骤），
  ⛔ **不新增键**（`COUNTER_KEYS` 只增不改义，本笔不扩键集）；
· 可见面 = `GET /api/agent/audit` 的 `counters.derived`（端点腿在
  `tests/api/test_bug083_tool_artifact_channel_wiring.py::test_link_expand_failure_is_visible_on_the_audit_endpoint`）
  ⇒ 只能写「机器可读 + 人工 curl 的可见面」，⛔ 不得写「用户已能看见派生失败」。

## 覆盖的腿

| 腿 | 断言 |
|---|---|
| ① 零态 | `doc_links` 在 `COUNTER_KEYS` 里且为 0 |
| ② 负腿 | 正常扩展（无边可扩）⇒ 计数**不动**，返回值 = 原主命中 |
| ③ 正腿 | 边查询必抛 ⇒ `doc_links` **恰 +1**，返回值**逐条不变**（静默降级语义未变） |
| ④ | 连续两次失败 ⇒ **+2** |
| ⑤ 隔离 | 只动 `doc_links`，其余派生键逐字不动 |
| ⑥ 短路 | `RAG_LINK_EXPAND=False` ⇒ **连查询都不发**且不计数（既有 E11 口径不变） |
| ⑦ 短路 | 名额 0（`max_links=0`）⇒ 同上（`link_budget` 既有口径不变） |
| ⑧ 源码 pin | `bump("doc_links")` 在本模块**恰一处**（防将来在片断函数里补第二处） |

⚠️ `tests/retrieval/` **没有 conftest 级 autouse 钉子** ⇒ 两把开关（`RAG_LINK_EXPAND` /
`RAG_LINK_MAX`）与 `load_wiki_adjacency`（边查询）都在本文件里**显式钉死**，不依赖本机 `.env`。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core import derived_counters
from app.core.config import settings
from app.retrieval import link as link_mod
from app.retrieval.types import Material

KEY = "doc_links"
SEED = Material(chunk_id=11, document_id=7, content="种子正文", score=0.4)


@pytest.fixture(autouse=True)
def _reset_process_counters(monkeypatch):
    """每例前后清零 + 钉住两把开关（本机 `.env` 没有这两个键的钉子，靠默认值兜底会漂）。"""
    derived_counters.reset_failure_counts()
    monkeypatch.setattr(settings, "RAG_LINK_EXPAND", True)
    monkeypatch.setattr(settings, "RAG_LINK_MAX", 4)
    yield
    derived_counters.reset_failure_counts()


def _counts() -> dict[str, int]:
    return {key: int(value) for key, value in derived_counters.ordered_counts().items()}


def _count() -> int:
    return _counts().get(KEY, 0)


async def _boom_edges(*args, **kwargs):  # noqa: ANN002, ANN003
    raise RuntimeError("模拟链接边查询失败（本用例故意）")


# --------------------------------------------------------------------------
# ① 零态
# --------------------------------------------------------------------------


def test_zero_state_doc_links_present_and_zero():
    """① 零态：键在权威表里且为 0（缺席 = 读不到）。"""
    assert KEY in derived_counters.COUNTER_KEYS
    assert _count() == 0


# --------------------------------------------------------------------------
# ② 负腿
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_expand_does_not_bump(monkeypatch):
    """② 负腿：正常扩展（假件无边）⇒ 不计数；返回值 = 主命中（无扩展片段）。"""

    async def _no_edges(*args, **kwargs):  # noqa: ANN002, ANN003
        return {}

    monkeypatch.setattr(link_mod, "load_wiki_adjacency", _no_edges)
    out = await link_mod.expand_materials(object(), [SEED], max_links=1)  # type: ignore[arg-type]
    assert [row.chunk_id for row in out] == [SEED.chunk_id]
    assert _count() == 0, "正常路径计数 ⇒ 计数退化成「调用即计数」"


# --------------------------------------------------------------------------
# ③④⑤ 正腿
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edge_query_failure_bumps_doc_links_exactly_once(monkeypatch):
    """③ 正腿：边查询必抛 ⇒ `doc_links` **恰 +1**，且返回值**逐条不变**（静默降级未变）。"""
    monkeypatch.setattr(link_mod, "load_wiki_adjacency", _boom_edges)
    out = await link_mod.expand_materials(object(), [SEED], max_links=1)  # type: ignore[arg-type]
    assert [row.chunk_id for row in out] == [SEED.chunk_id], "F6-B 的静默降级语义被改了"
    assert _count() == 1, _counts()


@pytest.mark.asyncio
async def test_two_failures_bump_twice(monkeypatch):
    """④ 两次失败 ⇒ **+2**（每件失败一笔）。"""
    monkeypatch.setattr(link_mod, "load_wiki_adjacency", _boom_edges)
    await link_mod.expand_materials(object(), [SEED], max_links=1)  # type: ignore[arg-type]
    await link_mod.expand_materials(object(), [SEED], max_links=1)  # type: ignore[arg-type]
    assert _count() == 2, _counts()


@pytest.mark.asyncio
async def test_only_the_doc_links_key_moves(monkeypatch):
    """⑤ 隔离：只动 `doc_links`，其余派生键逐字不动。"""
    before = _counts()
    monkeypatch.setattr(link_mod, "load_wiki_adjacency", _boom_edges)
    await link_mod.expand_materials(object(), [SEED], max_links=1)  # type: ignore[arg-type]
    after = _counts()
    assert after[KEY] == 1, after
    assert {k: v for k, v in after.items() if k != KEY} == {
        k: v for k, v in before.items() if k != KEY
    }, after


# --------------------------------------------------------------------------
# ⑥⑦ 短路（开关 / 名额）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_off_sends_no_query_and_does_not_bump(monkeypatch):
    """⑥ E11 口径不变：开关关闭 ⇒ **连查询都不发**，也不计数。"""

    async def _must_not_run(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("开关关闭时不该发起边查询")

    monkeypatch.setattr(settings, "RAG_LINK_EXPAND", False)
    monkeypatch.setattr(link_mod, "load_wiki_adjacency", _must_not_run)
    out = await link_mod.expand_materials(object(), [SEED], max_links=1)  # type: ignore[arg-type]
    assert [row.chunk_id for row in out] == [SEED.chunk_id]
    assert _count() == 0, _counts()


@pytest.mark.asyncio
async def test_zero_budget_sends_no_query_and_does_not_bump(monkeypatch):
    """⑦ 名额 0（`max_links=0`）⇒ 同上（`link_budget` 的既有短路口径不变）。"""

    async def _must_not_run(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("名额为 0 时不该发起边查询")

    monkeypatch.setattr(link_mod, "load_wiki_adjacency", _must_not_run)
    out = await link_mod.expand_materials(object(), [SEED], max_links=0)  # type: ignore[arg-type]
    assert [row.chunk_id for row in out] == [SEED.chunk_id]
    assert _count() == 0, _counts()


# --------------------------------------------------------------------------
# ⑧ 源码 pin
# --------------------------------------------------------------------------


def test_bump_site_is_unique_in_the_module():
    """⑧ 源码 pin：`bump("doc_links")` 在本模块**恰一处**（防将来在片断函数里补第二处）。

    结构性腿：行为腿钉「恰 +1」，但「在 `_note_fragment` / `_document_fragment` 里各补一处」
    这种改法只在**别的路径**上被行为腿抓到；源码 pin 把本笔的落点钉死。
    """
    source = Path(link_mod.__file__).read_text(encoding="utf-8")
    assert source.count('bump("doc_links")') == 1, "本笔只该有一处计数点（末尾 except 内）"
