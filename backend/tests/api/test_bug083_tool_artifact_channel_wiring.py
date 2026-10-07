"""`回归修复` 批次三 · **#30 工具结果通道降级**的接线判据（含批次三两处接线的端点可见面）。

🔴 口径（逐字，防误读）

· 钉的是「**降级出口存在且真的会动**」，**不是**「工具正文落盘失败已被消除」；
· 可见面 = `GET /api/agent/audit` 的 **`counters.derived`**（机器可读 + 人工 curl 的可见面）；
  ⛔ 不得写「用户已能看见派生失败」—— 前端对 `counters` 的消费 = **0**；
· `#30` 的接线点**必须**落在 :func:`app.agent.tool_artifacts._fail` **内部**：`save()` 的两条失败
  路径（超上限 / 写盘异常）都经它 ⇒ 若在两个调用点各记一次，**同一件失败被记两笔**。
  本文件 ③④⑤ 三条腿就是这条「恰一次」的检出器（删 bump ⇒ 红；在调用点补 bump ⇒ 也红）。

## 覆盖的腿

| 腿 | 断言 |
|---|---|
| ① 零态 | `tool_result_channel` 在 `COUNTER_KEYS` 里且为 0（缺席 = 读不到） |
| ② 负腿 | **成功**落盘 ⇒ 计数**不动**（否则退化成「调用即计数」） |
| ③ 正腿 | 超 2 MiB ⇒ `save()` 返回 `None` + warning，计数**恰 +1** |
| ④ 正腿 | 写盘异常（`artifact_dir` 注入必抛）⇒ 计数**恰 +1** |
| ⑤ | 连续两次失败 ⇒ **+2**（每件失败一笔，不合并、不丢失） |
| ⑥ 隔离 | 只动 `tool_result_channel`，其余键逐字不动 |
| ⑦ 源码 pin | `bump("tool_result_channel")` 在模块里**恰一处**（防将来在调用点补第二处） |
| ⑧ 端点可见面 | 真触发失败后经 `GET /api/agent/audit` 的 `counters.derived` 逐值可见 |
| ⑨ 端点可见面 | `#31`（`retrieval.link.expand_materials` 链接扩展失败）同样经该端点可见 —— 其行为腿在 `tests/retrieval/test_bug083_link_expand_wiring.py`，本腿只钉**同一条可见面** |

⚠️ 落盘根由 `tests/api/conftest.py` 的 autouse 夹具 `pin_tool_artifact_dir` 换成 `tmp_path`
（D-S1-13②）⇒ 本文件的成功腿**绝不碰真实数据根**。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.agent import tool_artifacts
from app.core import derived_counters
from app.retrieval import link as link_mod
from app.retrieval.types import Material
from tests.api.test_chat_contract import client  # noqa: F401

KEY = "tool_result_channel"
LINK_KEY = "doc_links"


@pytest.fixture(autouse=True)
def _reset_process_counters():
    """进程全局计数**跨用例泄漏**是本仓踩过的坑 ⇒ 每例前后清零。"""
    derived_counters.reset_failure_counts()
    yield
    derived_counters.reset_failure_counts()


def _counts() -> dict[str, int]:
    return {key: int(value) for key, value in derived_counters.ordered_counts().items()}


def _count(key: str = KEY) -> int:
    return _counts().get(key, 0)


class _BoomDir:
    """落盘目录不可用的假件（只读盘 / 盘满 / 父目录被删都长这样：`OSError`）。"""

    def __call__(self, today: str | None = None) -> Path:  # noqa: ARG002
        raise OSError("模拟落盘目录不可用（本用例故意）")


async def _boom_edges(*args, **kwargs):  # noqa: ANN002, ANN003
    """`link` 的边查询注入必抛（模拟表缺失 / 连接断）。"""
    raise RuntimeError("模拟链接边查询失败（本用例故意）")


# --------------------------------------------------------------------------
# ① 零态
# --------------------------------------------------------------------------


def test_zero_state_tool_result_channel_present_and_zero():
    """① 零态：键在权威表里且为 0（缺席 = 读不到）。"""
    assert KEY in derived_counters.COUNTER_KEYS
    assert _count() == 0


# --------------------------------------------------------------------------
# ② 负腿：成功不计数
# --------------------------------------------------------------------------


def test_successful_save_does_not_bump(tmp_path):
    """② 负腿：**成功**落盘（走 `tmp_path` 落盘根）⇒ 计数**一动不动**。"""
    assert tool_artifacts.save("一段会被成功落盘的正文" * 3) is not None
    assert _count() == 0, "成功路径计数 ⇒ 计数退化成「调用即计数」"


# --------------------------------------------------------------------------
# ③④⑤ 正腿：恰一次 / 恰两次
# --------------------------------------------------------------------------


def test_oversize_failure_bumps_exactly_once(caplog):
    """③ 正腿：超 2 MiB ⇒ 返回 `None` + warning，计数**恰 +1**（多一笔就是重复计数）。"""
    oversized = "w" * (tool_artifacts.MAX_ARTIFACT_BYTES + 10)
    with caplog.at_level(logging.WARNING, logger="app.agent.tool_artifacts"):
        assert tool_artifacts.save(oversized) is None
    assert any("落盘失败" in record.getMessage() for record in caplog.records), [
        record.getMessage() for record in caplog.records
    ]
    assert _count() == 1, _counts()


def test_write_failure_bumps_exactly_once(monkeypatch):
    """④ 正腿：写盘异常（`artifact_dir` 注入必抛）⇒ 计数**恰 +1**（同一件失败只记一笔）。"""
    monkeypatch.setattr(tool_artifacts, "artifact_dir", _BoomDir())
    assert tool_artifacts.save("会走写盘异常路径的正文") is None
    assert _count() == 1, _counts()


def test_two_failures_bump_twice(monkeypatch):
    """⑤ 两次失败 ⇒ **+2**（每件失败一笔；不合并、不丢失、不重复）。"""
    monkeypatch.setattr(tool_artifacts, "artifact_dir", _BoomDir())
    assert tool_artifacts.save("第一件") is None
    assert tool_artifacts.save("第二件") is None
    assert _count() == 2, _counts()


# --------------------------------------------------------------------------
# ⑥ 隔离 / ⑦ 源码 pin
# --------------------------------------------------------------------------


def test_only_the_channel_key_moves(monkeypatch):
    """⑥ 隔离：失败只动 `tool_result_channel`，其余派生键逐字不动。"""
    before = _counts()
    monkeypatch.setattr(tool_artifacts, "artifact_dir", _BoomDir())
    assert tool_artifacts.save("只该动一个键") is None
    after = _counts()
    assert after[KEY] == 1, after
    assert {k: v for k, v in after.items() if k != KEY} == {
        k: v for k, v in before.items() if k != KEY
    }, after


def test_bump_site_is_unique_in_the_module():
    """⑦ 源码 pin：`bump("tool_result_channel")` 在模块里**恰一处**。

    为什么要有这条（结构性腿，不承担行为判别力）：行为腿只能钉「恰 +1」，
    而「在调用点再补一处」这种改法在**另一条路径**上不一定会被行为腿抓到
    （例如只在超限路径补 ⇒ 写盘异常腿仍绿）。⇒ 用源码 pin 把「唯一出口」钉死。
    """
    source = Path(tool_artifacts.__file__).read_text(encoding="utf-8")
    assert source.count('bump("tool_result_channel")') == 1, "计数点必须唯一（唯一出口 `_fail` 内部）"


# --------------------------------------------------------------------------
# ⑧⑨ 端点可见面
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_result_channel_is_visible_on_the_audit_endpoint(client):  # noqa: ARG001
    """⑧ 真触发失败 ⇒ `GET /api/agent/audit` 的 `counters.derived` 里逐值可见。"""
    oversized = "w" * (tool_artifacts.MAX_ARTIFACT_BYTES + 10)
    assert tool_artifacts.save(oversized) is None
    assert _count() == 1

    resp = await client.get("/api/agent/audit")
    assert resp.status_code == 200, resp.text
    derived = (resp.json().get("counters") or {}).get("derived") or {}
    assert derived.get(KEY) == 1, derived


@pytest.mark.asyncio
async def test_link_expand_failure_is_visible_on_the_audit_endpoint(client, monkeypatch):  # noqa: ARG001
    """⑨ `#31` 的可见面：链接扩展失败 ⇒ 同一端点里 `doc_links` 逐值可见。

    行为腿（负腿 / 恰一次 / 开关关闭不查询）在 `tests/retrieval/test_bug083_link_expand_wiring.py`，
    本腿只负责「它确实进了**机器可读**的可见面」。
    """
    monkeypatch.setattr(link_mod.settings, "RAG_LINK_EXPAND", True)
    monkeypatch.setattr(link_mod, "load_wiki_adjacency", _boom_edges)
    seed = Material(chunk_id=1, document_id=7, content="种子正文", score=0.4)
    # 边加载已被注入必抛 ⇒ session 不会被真正用到（传 `object()` 即可）
    out = await link_mod.expand_materials(object(), [seed], max_links=1)  # type: ignore[arg-type]
    assert [row.chunk_id for row in out] == [1], "静默降级语义：返回值仍是原主命中"
    assert _count(LINK_KEY) == 1

    resp = await client.get("/api/agent/audit")
    assert resp.status_code == 200, resp.text
    derived = (resp.json().get("counters") or {}).get("derived") or {}
    assert derived.get(LINK_KEY) == 1, derived
