"""回归修复（2026-09-27 业主真机反馈）：「重建索引完成后『建议重建索引』提示仍在」。

根因（账本已定位，一处 UI 缺口 × 三处机制断点）：

- ① **前端确定缺口**：``handleReindex`` 完成后只 ``refreshDocs()``、不拉 ``health``
  ⇒ 提示条吃 ``health.clean`` 快照，只有 30s 轮询兜底 ⇒ 即使重建真清零，横幅也残留 ≤30s；
- ② **回收站盲区（结构性）**：``stale_clean_documents`` / ``stale_content_documents``
  统计**不过滤 ``deleted_at``**，而重建目标排除回收站 ⇒ 回收站里的过期文档
  「重建永远清不掉、横幅永远建议、列表还看不见」；
- ③ **无留档跳过 / reclean 失败不刷新指纹**——诊断信息只在 ``result.messages``，
  前端只 ``console.warn`` ⇒ 用户不可见。

修法（账本「最小修 + 根治」并做）：

- 前端：重建完成后 ``await refreshHealth()``（横幅立即更新/撤下）+ ``result.messages``
  展示进完成横幅（「失败 1 项」有可见出口）；
- 后端：两条 stale 统计**与重建目标同口径排除回收站**（``deleted_at IS NULL``）。
  ⚠️ ``hint`` 的既有句、五键集、``needs_reindex`` 的「或」语义**逐字不动**（02 v1.0.13 只登记统计范围）。

判据：
- 腿①–③（行为）：回收站文档不进两条 stale 统计 / 仅回收站过期 ⇒ ``needs_reindex`` 假 /
  重建目标仍排除回收站（既有行为钉）；
- 腿④（前端静态）：``handleReindex`` 含 ``refreshHealth`` + messages 进横幅 +
  ``console.warn`` 独占通道已移除。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models import Document
from app.pipeline import reindex as reindex_module
from app.pipeline import ingest
from app.pipeline.clean_status import (
    clean_status,
    stale_clean_documents,
    stale_content_documents,
)
from app.vector.embed import FakeEmbedding
from app.vector.qdrant_store import QdrantVectorStore

from tests.api.test_chat_contract import client  # noqa: F401 —— 夹具再导出

_REPO_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_APP = _REPO_ROOT / "frontend" / "src" / "App.tsx"

CLEAN_KEYS = ("enabled", "rule_fingerprint", "stale_documents", "needs_reindex", "hint")


def _app_source() -> str:
    """读 ``App.tsx`` 全文并**剔除整行注释**（099 载体同款纪律：注释掉 = 删掉，都必须红）。"""
    lines = FRONTEND_APP.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("//"))


def _handle_reindex_block() -> str:
    source = _app_source()
    begin = source.index("async function handleReindex(")
    end = source.index("async function removeDoc", begin)
    return source[begin:end]


async def _seed_doc(
    db,
    filename: str,
    *,
    file_path: str | None = None,
    clean_report: dict | None = None,
    deleted: bool = False,
) -> Document:
    """直建一条文档行（``tests/api/test_incremental_reindex_t3_4.py::_seed`` 同款造数法）。"""
    document = Document(
        filename=filename,
        file_type="txt",
        status="ready",
        index_status="ready",
        file_path=file_path,
        content_hash=f"bug100_seed_hash_{filename}",
        raw_content="第一版正文。\n",
        clean_report=clean_report,
        kb_id=1,
        chunk_count=0,
        vectorized=False,
    )
    if deleted:
        document.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(document)
    await db.commit()
    return document


async def _delete_doc(db, document_id: int) -> None:
    """软删（产品 ``DELETE /documents/{id}`` 同款：只置 ``deleted_at``）。"""
    doc = await db.get(Document, document_id)
    doc.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()


@pytest.fixture
def _working_vectors(monkeypatch, tmp_path):
    """reclean 计数要求新版向量验证成功；模型与向量库均只在本地替身中运行。"""
    embedding = FakeEmbedding(dim=4)
    store = QdrantVectorStore(path=str(tmp_path / "vectors"))
    monkeypatch.setattr(ingest, "get_embedding_client", lambda **kw: embedding)
    monkeypatch.setattr(ingest, "get_vector_store", lambda **kw: store)
    try:
        yield store
    finally:
        store.close()


# --- 腿①：回收站文档不进两条 stale 统计 ---------------------------------------


@pytest.mark.asyncio
async def test_stale_statistics_exclude_recycle_bin(db):
    """指纹过期 / 无报告的文档，一旦软删就**不得**再计入「建议重建」统计。"""
    active = await _seed_doc(db, "甲_在库.txt", clean_report=None)
    trashed = await _seed_doc(db, "乙_回收站.txt", clean_report=None)

    # 前置：两篇都无报告 ⇒ 都算 stale（排除回收站之前的历史行为）
    assert await stale_clean_documents(db) == 2

    await _delete_doc(db, trashed.id)
    assert await stale_clean_documents(db) == 1, "回收站文档不得计入 stale_clean"

    # 磁盘内容过期同口径：软删后不再统计（fail-closed 判据对回收站失效）
    await _delete_doc(db, active.id)
    assert await stale_clean_documents(db) == 0
    assert await stale_content_documents(db) == []


@pytest.mark.asyncio
async def test_stale_content_excludes_recycle_bin_with_real_files(db, tmp_path):
    """可判文档（.txt，真实文件）磁盘内容变更：在库的照常报过期，软删的不报。"""
    folder = tmp_path / "资料"
    folder.mkdir()
    active_file = folder / "在库.txt"
    trashed_file = folder / "回收站.txt"
    active_file.write_text("第一版正文。\n", encoding="utf-8")
    trashed_file.write_text("第一版正文。\n", encoding="utf-8")

    active = await _seed_doc(db, "在库.txt", file_path=str(active_file), clean_report=None)
    trashed = await _seed_doc(db, "回收站.txt", file_path=str(trashed_file), clean_report=None)

    # 两篇都在库 ⇒ 内容变更都算（前置腿：判据面确实生效，防空靶假绿）
    active_file.write_text("第二版正文，磁盘已改。\n", encoding="utf-8")
    trashed_file.write_text("第二版正文，磁盘已改。\n", encoding="utf-8")
    before = await stale_content_documents(db)
    assert set(before) == {active.id, trashed.id}

    await _delete_doc(db, trashed.id)
    after = await stale_content_documents(db)
    assert after == [active.id], "回收站文档不得计入磁盘内容过期"


# --- 腿②：仅回收站过期 ⇒ 不再建议重建（集成读数） ------------------------------


@pytest.mark.asyncio
async def test_needs_reindex_false_when_only_recycle_bin_is_stale(db):
    """回收站里只剩过期文档 ⇒ ``needs_reindex`` 假、``hint`` 空——建议循环的出口。"""
    await _seed_doc(db, "只有回收站过期.txt", clean_report=None, deleted=True)

    status = await clean_status(db)
    assert set(status) == set(CLEAN_KEYS)
    assert status["stale_documents"] == 0
    assert status["needs_reindex"] is False
    assert status["hint"] == ""


# --- 腿③：重建目标仍排除回收站（既有行为钉，防「统计放开重建也放开」的过修） ----


@pytest.mark.asyncio
async def test_reindex_targets_still_exclude_recycle_bin(db, tmp_path, _working_vectors):
    """全量重建**不碰**回收站文档（``deleted_at IS NULL`` 是 reindex 既有口径）；
    在库的无报告文档照常被 reclean 并刷新指纹。"""
    folder = tmp_path / "留档"
    folder.mkdir()
    stored = folder / "在库留档.txt"
    stored.write_text("重建会按当前规则重新清洗我。\n", encoding="utf-8")
    trashed_file = folder / "回收站留档.txt"
    trashed_file.write_text("回收站里的我。\n", encoding="utf-8")

    active = await _seed_doc(db, "在库留档.txt", file_path=str(stored), clean_report=None)
    trashed = await _seed_doc(db, "回收站留档.txt", file_path=str(trashed_file), clean_report=None, deleted=True)
    active_id, trashed_id = active.id, trashed.id

    result = await reindex_module.reindex_documents(db, document_id=None)

    assert result.recleaned == 1, "只有在库那篇该被 reclean"
    assert result.failed == 0
    await db.rollback()
    refreshed = await db.get(Document, active_id)
    trashed_after = await db.get(Document, trashed_id)
    assert isinstance(refreshed.clean_report, dict), "在库文档重建后必须拿到清洗报告"
    assert trashed_after.clean_report is None, "回收站文档的 clean_report 不得被重建触碰"
    assert await stale_clean_documents(db) == 0


# --- 腿④：前端静态——重建完成后拉 health + 明细进横幅 --------------------------


def test_handle_reindex_refreshes_health_and_shows_messages():
    """``handleReindex`` 必须：完成后 ``refreshHealth``（横幅立即更新）+ ``messages`` 进横幅。"""
    block = _handle_reindex_block()
    assert "await refreshHealth();" in block, "重建完成后必须立刻拉一次 health"
    assert "result.messages" in block, "跳过/失败明细必须展示给用户"
    assert "详情：" in block and "join(" in block, "明细要以可读形态拼进完成横幅"
    assert "console.warn" not in block, "明细不得只剩 console.warn（用户不可见）"
    # 顺序：先 refreshDocs 再 refreshHealth 皆可，但 refreshHealth 必须在 flash 之后、
    # try 块内（catch 分支的 flash 不该被它覆盖）
    assert block.index("flash(") < block.index("await refreshHealth();")
