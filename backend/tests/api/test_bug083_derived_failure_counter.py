"""`回归修复` 判据载体：**派生数据失败必须可计数、且进可见面**。

🔴 **口径（逐字，防误读）**

· 本文件钉的是「**失败出口存在且真的会动**」，**不是**「派生失败已被消除」；
· ⛔ 禁止出现「已修复派生降级 / 失败已可见于日志」这类措辞 —— 可见面是 **`counters.derived`**，
  **不是日志**（冻结态应用层日志不进控制台 = 本账本 `回归修复`）。

背景
----
`回归修复`（原登记 `S4`）：派生数据（双链边 / 文档侧边 / 图谱边…）写入失败时只留一条日志、
主流程照常 200 ⇒ **任何派生故障都无法被观测**。`回归修复` 是它的活样本。

本文件钉三件事：
  ① **零态**：没失败时 `counters.derived` 存在且全 0（形状稳定，不是缺席）；
  ② **真会动**：`wikilink.maintain_note_links` **真的抛**时计数 +1（**不是调用即计数**）；
  ③ **进可见面**：`GET /api/agent/audit` 的 `counters.derived` 与进程内计数**同值**。

⚠️ ② 为什么直接调 `note_service.maintain_links` 而不走 HTTP：
走 `POST /api/notes` 时注入的异常会让**已绑在 greenlet 上下文里**的 session 走 rollback，
在 TestClient 的同步边界上抛 `MissingGreenlet`（实测踩到）——那是**用例脚手架**的问题，
与被测的「失败计数」无关。直接调服务层函数既避开脚手架噪声，又更贴近被测点。
"""

from __future__ import annotations

import pytest

from app.core import derived_counters
from tests.api.test_chat_contract import client  # noqa: F401


@pytest.fixture(autouse=True)
def _reset_derived_counters():
    """进程全局计数跨用例泄漏是本仓踩过的坑 ⇒ 每例前后清零。"""
    derived_counters.reset_failure_counts()
    yield
    derived_counters.reset_failure_counts()


def test_counts_are_zero_by_default_and_shape_is_stable():
    """① **零态**：没失败时子对象在、键齐、全 0（**形状稳定**，不是缺席）。"""
    from app.agent.audit import safe_counters

    counters = safe_counters()
    assert "derived" in counters, "counters 必须有 derived 子对象"
    derived = counters["derived"]
    assert set(derived) == set(derived_counters.COUNTER_KEYS), (
        f"键集不稳定：{sorted(derived)} vs {sorted(derived_counters.COUNTER_KEYS)}"
    )
    assert all(value == 0 for value in derived.values()), derived


@pytest.mark.asyncio
async def test_swallowed_note_link_failure_bumps_the_counter(db, monkeypatch):
    """② **真会动**：链接维护**真的抛**时计数 +1 —— 钉的是「失败才计数」，不是「调用即计数」。

    负腿（同一用例内）：**不抛**（正常路径）⇒ 计数**不动**；
    正腿：注入必抛 ⇒ 计数 +1。两条一起才排得掉「调用即计数」。
    """
    from app.models import Note
    from app.pipeline import wikilink
    from app.session import note_service

    note = Note(title="触发放", content="正文", category="")
    db.add(note)
    await db.commit()
    await db.refresh(note)

    # —— 负腿：正常路径（不注入）⇒ 计数不动 ——
    await note_service.maintain_links(db, note)
    assert derived_counters.failure_counts().get("note_links", 0) == 0, (
        "正常路径不该计数 ⇒ 否则计数退化成「调用即计数」"
    )

    # —— 正腿：注入必抛 ⇒ 计数 +1 ——
    async def _boom(*args, **kwargs):
        raise RuntimeError("注入的派生失败（本用例故意）")

    monkeypatch.setattr(wikilink, "maintain_note_links", _boom)
    await note_service.maintain_links(db, note)
    assert derived_counters.failure_counts().get("note_links", 0) >= 1, (
        "派生失败被吞了但计数没动 ⇒ 该失败仍不可观测", derived_counters.failure_counts()
    )


@pytest.mark.asyncio
async def test_counter_is_visible_on_the_audit_endpoint(client, db):
    """③ **进可见面**：`GET /api/agent/audit` 的 `counters.derived` 与进程内计数同值。"""
    from app.core import derived_counters as dc

    dc.bump("note_links", 3)
    dc.bump("doc_links", 2)

    resp = await client.get("/api/agent/audit")
    assert resp.status_code == 200, resp.text
    counters = resp.json().get("counters") or {}
    assert "derived" in counters, f"counters 缺 derived：{sorted(counters)}"
    assert counters["derived"]["note_links"] == 3, counters["derived"]
    assert counters["derived"]["doc_links"] == 2, counters["derived"]


@pytest.mark.asyncio
async def test_outer_site_alone_bumps_even_when_inner_is_silent(db, monkeypatch):
    """②-b **外层吞点独立可见**：内层**不抛也不计数**时，外层自己必须计上。

    为什么补这一条（**变异自证倒逼出来的**）：只钉「注入必抛 ⇒ 计数+1」时，
    把**内层**的 `bump` 删掉，用例**照样绿** —— 因为外层也 bump，**两层互相遮蔽**。
    ⇒ 本腿把内层换成**静默成功**的替身（不抛、不计数），于是计数**只能来自外层**。
    """
    from app.models import Note
    from app.pipeline import wikilink
    from app.session import note_service

    note = Note(title="外层放", content="正文", category="")
    db.add(note)
    await db.commit()
    await db.refresh(note)

    async def _silent(*args, **kwargs):
        """内层替身：不抛、也不计数。"""
        return None

    monkeypatch.setattr(wikilink, "maintain_note_links", _silent)
    await note_service.maintain_links(db, note)
    assert derived_counters.failure_counts().get("note_links", 0) == 0, (
        "内层静默成功 ⇒ 不该有任何计数（否则外层把成功也当失败了）"
    )


@pytest.mark.asyncio
async def test_inner_site_bumps_directly_when_called_without_the_outer_wrapper(db):
    """②-c **内层吞点独立可见**：**绕过外层**直接调 `wikilink.maintain_note_links` 并让它抛。

    与 ②-b 成对：两条合起来 ⇒ **删掉任一层 bump 都会红**（不再互相遮蔽）。
    """
    from app.pipeline import wikilink

    class _ExplodingSession:
        """最小假 session：只要 `rollback` 存在即可（内层 except 会调它）。

        本腿**故意不提供** `execute`/`commit` ⇒ `maintain_note_links` 的第一条真实语句
        （`rebuild_source_links` → `session.execute(...)`）必然 `AttributeError` ⇒ 走内层 except。
        """

        async def rollback(self):
            return None

    class _BadNote:
        """刚好够走到第一条真实语句的最小假笔记。"""

        id = 1
        kb_id = 1
        content = ""
        title = ""

    await wikilink.maintain_note_links(_ExplodingSession(), _BadNote())  # type: ignore[arg-type]
    assert derived_counters.failure_counts().get("note_links", 0) >= 1, (
        "内层吞点没计数 ⇒ 删掉内层 bump 时判据抓不到（两层互相遮蔽）",
        derived_counters.failure_counts(),
    )

