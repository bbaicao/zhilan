"""条目 4.5.4 · 用量（写入层 + 两条端点）与导出（POST /api/export/markdown）。

分三段，各自钉一条硬口径：

**① 用量：每个数字都必须追到 ``usage_events`` 的真实行**（不伪造）
- 空表 ⇒ 200 + 全零格（``by_day`` 覆盖窗口内每一天、``by_kind`` 固定四类），**不报错**；
- 有数据 ⇒ ``total`` / ``by_day`` / ``unknown_calls`` 与直插的行**逐数对得上**
  （``total_tokens`` 为 NULL 的行只进 ``unknown_calls``，不吞掉同组已知行的和）；
- 三天真实写入点各写一条：回答收口（HTTP 走 faux 模型）、embed（桩 httpx）、
  rerank（桩 httpx）——三种都验「成功才写、写进测试库、tokens 拿不到就是 NULL」；
- ``record_usage`` 绝不抛：落库炸了只 warning（主流程零影响）。

**② CSV 导出**：BOM + 中文表头 + 「未知」单元格。

**③ 导出**：入参校验（``path`` 必填 422 / 路径不可用 400 / 两类都不勾 400 / 范围为空 400
且**不建目录**）、出参计数与 ``errors``、按分类分子文件夹、软删的行不导出、范围取交集。
**一律写到 ``tmp_path``**，绝不写仓库真实目录。
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

from app.models import Chunk, Document, Note, UsageEvent
from app.session import repository as repo
from app.usage import recorder
from app.usage.report import CSV_HEADERS, usage_summary
from tests.api.test_chat_contract import client, use_faux  # noqa: F401 —— 公共夹具（词表/SSE 同源）

pytestmark = pytest.mark.asyncio

MARK = "t454-"  # 本文件造的用量行统一前缀，断言与清理都靠它跟别的用例划清界限


@pytest_asyncio.fixture
async def clean_extra(db):
    """补清 ``usage_events`` / ``notes`` / ``wiki_links``（公共夹具的清表清单没覆盖）。"""
    for table in ("wiki_links", "notes", "usage_events"):
        await db.execute(text(f"DELETE FROM {table}"))
    await db.commit()
    return db


async def _db_today(db) -> date:
    """数据库时钟的「今天」——窗口口径与实现同源（``date(CURRENT_TIMESTAMP)``）。"""
    value = (await db.execute(select(func.date(func.current_timestamp())))).scalar()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


async def _insert_usage(db, created_at: datetime, provider: str, **kwargs) -> UsageEvent:
    row = UsageEvent(created_at=created_at, provider=provider, model="m", **kwargs)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _usage_rows(db, provider: str) -> list[UsageEvent]:
    await db.commit()  # 结束当前事务，免得读到旧快照（MySQL REPEATABLE READ）
    rows = (
        await db.execute(
            select(UsageEvent).where(UsageEvent.provider == provider).order_by(UsageEvent.id)
        )
    ).scalars().all()
    return list(rows)


async def _all_usage_rows(db) -> list[UsageEvent]:
    """全部用量行（同样先结束事务——写入是**另一个连接**提交的）。"""
    await db.commit()
    return list((await db.execute(select(UsageEvent).order_by(UsageEvent.id))).scalars().all())


# ==========================================================================
# ① GET /api/usage
# ==========================================================================


async def test_usage_summary_empty_table_is_zero_filled(client, clean_extra):  # noqa: F811
    """空表：200 + 全零形状（窗口内每天 × 四类都在，**不报错、不编数字**）。"""
    today = await _db_today(clean_extra)
    resp = await client.get("/api/usage")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert set(data) == {"days", "total", "by_day"}
    assert data["days"] == 7
    assert data["total"] == {
        "calls": 0,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "unknown_calls": 0,
    }
    assert len(data["by_day"]) == 7
    assert sorted(row["date"] for row in data["by_day"]) == sorted(
        (today - timedelta(days=offset)).isoformat() for offset in range(7)
    )
    for row in data["by_day"]:
        assert set(row) == {"date", "by_kind"}
        assert set(row["by_kind"]) == {"chat", "vision", "embed", "rerank"}
        for stat in row["by_kind"].values():
            assert stat["calls"] == 0 and stat["total_tokens"] is None and stat["unknown_calls"] == 0


async def test_usage_summary_aggregates_real_rows(client, clean_extra):  # noqa: F811
    """有数据：汇总数与直插的行逐数对得上；未知 tokens 只进 ``unknown_calls``。"""
    today = await _db_today(clean_extra)
    noon = datetime.combine(today, time(12, 0))
    await _insert_usage(
        clean_extra,
        noon,
        f"{MARK}chat-known",
        kind="chat",
        calls=2,
        prompt_tokens=60,
        completion_tokens=40,
        total_tokens=100,
    )
    await _insert_usage(clean_extra, noon, f"{MARK}chat-null", kind="chat", calls=1)
    await _insert_usage(
        clean_extra, noon, f"{MARK}embed", kind="embed", calls=1, total_tokens=50
    )

    data = (await client.get("/api/usage?days=7")).json()
    today_row = [row for row in data["by_day"] if row["date"] == today.isoformat()][0]

    assert data["total"]["calls"] == 4
    assert data["total"]["total_tokens"] == 150, "NULL 行不参与求和，也不吞掉已知行的和"
    assert data["total"]["unknown_calls"] == 1
    assert data["total"]["prompt_tokens"] == 60
    assert today_row["by_kind"]["chat"] == {
        "calls": 3,
        "prompt_tokens": 60,
        "completion_tokens": 40,
        "total_tokens": 100,
        "unknown_calls": 1,
    }
    assert today_row["by_kind"]["embed"]["total_tokens"] == 50
    assert today_row["by_kind"]["vision"]["calls"] == 0


async def test_usage_summary_window_and_clamping(client, clean_extra):  # noqa: F811
    """窗口：旧行按窗口进出；``days`` 夹到 1..90、非数字走 422（不是 500）。"""
    today = await _db_today(clean_extra)
    await _insert_usage(
        clean_extra,
        datetime.combine(today, time(9, 0)),
        f"{MARK}today",
        kind="chat",
        calls=1,
        total_tokens=10,
    )
    await _insert_usage(
        clean_extra,
        datetime.combine(today - timedelta(days=10), time(9, 0)),
        f"{MARK}old",
        kind="chat",
        calls=1,
        total_tokens=999,
    )

    week = (await client.get("/api/usage?days=7")).json()
    assert week["days"] == 7 and week["total"]["total_tokens"] == 10
    month = (await client.get("/api/usage?days=30")).json()
    assert month["days"] == 30 and month["total"]["total_tokens"] == 1009

    assert (await client.get("/api/usage?days=0")).json()["days"] == 1
    assert (await client.get("/api/usage?days=200")).json()["days"] == 90
    assert (await client.get("/api/usage?days=abc")).status_code == 422


async def test_usage_summary_is_read_only_and_repeatable(client, clean_extra):  # noqa: F811
    """两次调用同一结果（端点只读；顺带钉住「端点自身不写库」）。"""
    first = (await client.get("/api/usage")).json()
    second = (await client.get("/api/usage")).json()
    assert first == second


async def test_usage_summary_days_defaults_to_seven(client, clean_extra):  # noqa: F811
    """S6 · 185 补腿：省略 days ⇒ 默认窗口 7 天（响应 days 回显实际窗口）。

    既有腿只跑了 ?days=7 显式值；「省略走 DEFAULT_DAYS=7」此前没有判据 ——
    默认值被改成 30 / 0 / 必填，本腿必红。
    """
    today = await _db_today(clean_extra)
    await _insert_usage(
        clean_extra,
        datetime.combine(today, time(9, 0)),
        f"{MARK}default-today",
        kind="chat",
        calls=1,
        total_tokens=10,
    )
    await _insert_usage(
        clean_extra,
        datetime.combine(today - timedelta(days=10), time(9, 0)),
        f"{MARK}default-old",
        kind="chat",
        calls=1,
        total_tokens=999,
    )

    omitted = await client.get("/api/usage")
    explicit = await client.get("/api/usage?days=7")
    assert omitted.status_code == 200, omitted.text
    assert omitted.json()["days"] == 7, "省略 days 必须回默认窗口 7（不是 30、不是 0）"
    assert omitted.json() == explicit.json()
    assert omitted.json()["total"]["total_tokens"] == 10, "10 天前的旧行不许落进默认窗口"


# ==========================================================================
# ①.5 写入层：record_usage 与三个写入点
# ==========================================================================


async def test_record_usage_writes_a_real_row(db, clean_extra):  # noqa: F811
    """写入层落地：``db`` 落的是**它所属引擎**的库（单测里 = 测试库）。"""
    recorder.record_usage(
        kind="chat", provider=f"{MARK}writer", model="m-1", prompt_tokens=3, total_tokens=5, db=db
    )
    await recorder.drain_pending()

    rows = await _usage_rows(db, f"{MARK}writer")
    assert len(rows) == 1
    assert (rows[0].kind, rows[0].model, rows[0].calls) == ("chat", "m-1", 1)
    assert (rows[0].prompt_tokens, rows[0].total_tokens) == (3, 5)


async def test_record_usage_null_tokens_is_unknown(db, clean_extra):  # noqa: F811
    """拿不到 tokens 就是 NULL（不是 0）——界面按「未知」展示，合计不被 0 污染。"""
    recorder.record_usage(kind="chat", provider=f"{MARK}unknown", model="m", db=db)
    await recorder.drain_pending()

    rows = await _usage_rows(db, f"{MARK}unknown")
    assert len(rows) == 1
    assert rows[0].prompt_tokens is None
    assert rows[0].completion_tokens is None
    assert rows[0].total_tokens is None


async def test_record_usage_never_raises_when_storage_is_broken(monkeypatch, db, clean_extra):  # noqa: F811
    """落库炸了只 warning：``record_usage`` **不抛**（主流程零影响）。"""

    class _BrokenSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def add(self, _row):
            return None

        async def commit(self):
            raise RuntimeError("模拟落库失败")

    monkeypatch.setattr(recorder, "SessionLocal", lambda: _BrokenSession())
    recorder.record_usage(kind="chat", provider=f"{MARK}broken", model="m")  # 不抛
    await recorder.drain_pending()
    assert await _usage_rows(db, f"{MARK}broken") == []


async def test_report_never_raises_on_empty_table(db, clean_extra):  # noqa: F811
    """空表直查（不经 HTTP）也不炸：形状齐、全零。"""
    data = await usage_summary(db, 3)
    assert data["days"] == 3 and len(data["by_day"]) == 3
    assert data["total"]["calls"] == 0 and data["total"]["total_tokens"] is None


async def test_answer_success_predicate():
    """「回答成功」的唯一判据：不需要模型 / 模型侧异常 / 生成失败都不算成功。"""
    from app.api.chat_routes import answer_succeeded

    plan = SimpleNamespace(needs_model=True)
    assert answer_succeeded(plan, "好的。") is True
    assert answer_succeeded(SimpleNamespace(needs_model=False), "资料中没有提到。") is False
    failed = SimpleNamespace(needs_model=True, generation_status="failed")
    assert answer_succeeded(failed, "模型响应超时，请稍后重试。") is False
    assert answer_succeeded(failed, "回答生成失败，请稍后重试。（TimeoutError）") is False
    assert answer_succeeded(plan, "尚未配置或调用失败时，请检查设置。") is True
    assert answer_succeeded(plan, "") is False


async def test_answer_recording_does_not_duplicate_provider_calls(db, clean_extra):  # noqa: F811
    """最终答案收口不是实际 provider send，任何终态均不单独增加调用数。"""
    from app.api.chat_routes import record_answer_usage

    await record_answer_usage(db, SimpleNamespace(needs_model=False), "资料中没有提到。")
    await record_answer_usage(db, SimpleNamespace(needs_model=True, generation_status="failed"), "回答生成失败，请稍后重试。（X）")
    await db.commit()
    assert await _all_usage_rows(clean_extra) == []

    await record_answer_usage(db, SimpleNamespace(needs_model=True), "这是回答正文。")
    await db.commit()  # 模拟调用点那次「既有提交」
    rows = await _all_usage_rows(clean_extra)
    assert rows == []


async def test_chat_endpoint_faux_model_does_not_fabricate_provider_usage(  # noqa: F811
    client, db, clean_extra, use_faux
):
    """HTTP 全链：faux 模型返回正文但未 send，不得虚构 provider 调用。

    用「今天天气怎么样」而不是「你好」：后者会被澄清判定拦成 ``kind=clarify`` 的预置文案轮
    （**不调模型** ⇒ 本就不该记用量），拿它测写入点会得到假绿/假红。
    """
    use_faux("好的。")
    resp = await client.post("/api/chat", json={"message": "今天天气怎么样"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] != "clarify", "这条用例要的是**真调模型**的那一轮"
    await recorder.drain_pending()

    rows = await _all_usage_rows(clean_extra)
    assert rows == []


async def test_clarify_round_records_nothing(client, db, clean_extra, use_faux):  # noqa: F811
    """预置文案轮（clarify：压根没调模型）**一条都不记**——「只记成功」的正面钉子。"""
    use_faux("请问你想了解哪一部分？")
    resp = await client.post("/api/chat", json={"message": "你好"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "clarify"
    await recorder.drain_pending()
    assert await _all_usage_rows(clean_extra) == []


async def test_embed_write_point_records_provider_usage(monkeypatch, engine, clean_extra):  # noqa: F811
    """embed 写入点：真 ``OpenAICompatEmbedding`` + 桩 httpx ⇒ 一条 embed 行（tokens 来自响应）。"""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.config import settings
    from app.vector import embed as embed_mod

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(recorder, "SessionLocal", factory)

    class _Resp:
        status_code = 200
        text = "ok"

        @staticmethod
        def json():
            return {
                "data": [{"index": 0, "embedding": [0.1] * 8}],
                "usage": {"prompt_tokens": 5, "total_tokens": 7},
            }

    class _Client:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, _url, headers=None, json=None):  # noqa: A002 —— 对齐 httpx 签名
            return _Resp()

    fake_cfg = SimpleNamespace(model="m-embed", base_url="http://embed.test/v1", api_key="k")
    monkeypatch.setattr(type(settings), "embed_config", property(lambda self: fake_cfg))
    monkeypatch.setattr(embed_mod.httpx, "AsyncClient", _Client)

    vectors = await embed_mod.OpenAICompatEmbedding(dim=8).embed_texts(["一段正文"])
    await recorder.drain_pending()
    assert len(vectors) == 1

    rows = await _all_usage_rows(clean_extra)
    assert len(rows) == 1
    assert rows[0].kind == "embed" and rows[0].model == "m-embed"
    assert (rows[0].prompt_tokens, rows[0].total_tokens) == (5, 7)


async def test_rerank_write_point_records_input_tokens(monkeypatch, engine, clean_extra):  # noqa: F811
    """rerank 写入点：真 ``_call_rerank`` + 桩 httpx ⇒ 一条 rerank 行（``input_tokens`` 兼容）。"""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.retrieval import rerank as rerank_mod

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(recorder, "SessionLocal", factory)

    class _Resp:
        status_code = 200
        text = "ok"

        @staticmethod
        def json():
            return {
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 1, "relevance_score": 0.0},
                ],
                "usage": {"input_tokens": 11, "total_tokens": 11},
            }

    class _Client:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, _url, headers=None, json=None):  # noqa: A002
            return _Resp()

    monkeypatch.setattr(rerank_mod.httpx, "AsyncClient", _Client)
    config = SimpleNamespace(
        provider="jina", model="m-rerank", base_url="http://rerank.test/v1", api_key="k"
    )
    try:
        scores = await rerank_mod._call_rerank(config, "问题", ["甲", "乙"])
    finally:
        await recorder.drain_pending()
    assert scores == [0.9, 0.0]

    rows = await _all_usage_rows(clean_extra)
    assert len(rows) == 1
    assert rows[0].kind == "rerank" and rows[0].model == "m-rerank"
    assert rows[0].prompt_tokens == 11 and rows[0].total_tokens == 11


# ==========================================================================
# ② GET /api/usage/export（CSV）
# ==========================================================================


async def test_usage_csv_has_bom_chinese_header_and_unknown_cells(client, clean_extra):  # noqa: F811
    """CSV：UTF-8 BOM + 中文表头 + 逐行明细（拿不到 tokens 的格子写「未知」）。"""
    today = await _db_today(clean_extra)
    noon = datetime.combine(today, time(12, 0))
    await _insert_usage(
        clean_extra,
        noon,
        f"{MARK}csv",
        kind="chat",
        calls=2,
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
    )
    await _insert_usage(clean_extra, noon, f"{MARK}csv", kind="embed", calls=1)

    resp = await client.get("/api/usage/export?days=7")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    body = resp.text
    assert body.startswith("\ufeff"), "BOM 是 Excel 中文不乱码的前提"
    lines = [line for line in body.lstrip("\ufeff").splitlines() if line]
    assert lines[0].split(",") == list(CSV_HEADERS)

    table = "\n".join(lines[1:])
    assert re.search(rf"{today.isoformat()},对话,{MARK}csv,m,2,10,20,30", table)
    assert re.search(rf"{today.isoformat()},向量,{MARK}csv,m,1,未知,未知,未知", table)


async def test_usage_csv_on_empty_table_is_header_only(client, clean_extra):  # noqa: F811
    """空表导出：只有 BOM + 表头（200；不是 404、不是空响应体错误）。"""
    resp = await client.get("/api/usage/export?days=7")
    assert resp.status_code == 200
    lines = [line for line in resp.text.lstrip("\ufeff").splitlines() if line]
    assert lines == [",".join(CSV_HEADERS)]


async def test_usage_csv_entry_follows_same_days_rules(client, clean_extra):  # noqa: F811
    """S6 · 185 补腿：CSV 入口与 /api/usage **同规则**（省略=7、0⇒1、200⇒90、非法⇒422）。

    CSV 响应**不回声** days（只有 Content-Disposition 头）⇒ 窗口只能用**行集**证明：
    今天 / 昨天区分 1 天与默认 7 天；89 / 90 天前区分 90 天窗口的含入与排除边界。
    路由与 usage_detail 均夹取：仅去掉路由的重复夹取不改变结果，不要求打红。
    本腿钉住实际窗口和缺省值，避免将“10 天前可见”误当成“精确夹到 90”。
    """

    def providers(body: str) -> list[str]:
        lines = [line for line in body.lstrip("\ufeff").splitlines() if line]
        assert lines[0].split(",") == list(CSV_HEADERS), "表头必须逐列等于 CSV_HEADERS"
        return [line.split(",")[2] for line in lines[1:]]  # 第 3 列 = 提供商

    today = await _db_today(clean_extra)
    await _insert_usage(
        clean_extra,
        datetime.combine(today, time(9, 0)),
        f"{MARK}csv-default-today",
        kind="chat",
        calls=1,
        total_tokens=11,
    )
    await _insert_usage(
        clean_extra,
        datetime.combine(today - timedelta(days=10), time(9, 0)),
        f"{MARK}csv-default-old",
        kind="chat",
        calls=1,
        total_tokens=999,
    )
    for age, label in ((1, "yesterday"), (89, "day90"), (90, "day91")):
        await _insert_usage(
            clean_extra,
            datetime.combine(today - timedelta(days=age), time(9, 0)),
            f"{MARK}csv-default-{label}",
            kind="chat",
            calls=1,
            total_tokens=age,
        )

    week = await client.get("/api/usage/export?days=7")
    omitted = await client.get("/api/usage/export")
    assert week.status_code == 200 and omitted.status_code == 200, (week.text, omitted.text)
    assert sorted(providers(week.text)) == sorted(
        [f"{MARK}csv-default-today", f"{MARK}csv-default-yesterday"]
    )
    assert providers(omitted.text) == providers(week.text), "CSV 省略 days 必须等价于 days=7"
    day = await client.get("/api/usage/export?days=0")
    assert day.status_code == 200, day.text
    assert providers(day.text) == [f"{MARK}csv-default-today"], "0 夹到1：昨天必须排除"
    assert sorted(providers((await client.get("/api/usage/export?days=200")).text)) == sorted(
        [
            f"{MARK}csv-default-today",
            f"{MARK}csv-default-yesterday",
            f"{MARK}csv-default-old",
            f"{MARK}csv-default-day90",
        ]
    ), "200 夹到90：89天前须含入，90天前须排除"
    assert (await client.get("/api/usage/export?days=abc")).status_code == 422


# ==========================================================================
# ③ POST /api/export/markdown
# ==========================================================================


async def _note(db, title, *, category="", tags=None, deleted=False) -> Note:
    row = Note(
        title=title,
        content=f"# {title}\n\n正文",
        category=category,
        tags=tags,
        kb_id=1,
        deleted_at=repo.utc_now() if deleted else None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _document(db, filename, *, category="", tags=None, raw="原文", chunks=None, deleted=False):
    row = Document(
        filename=filename,
        file_type="md",
        status="ready",
        index_status="ready",
        kb_id=1,
        category=category,
        tags=tags,
        raw_content=raw,
        deleted_at=repo.utc_now() if deleted else None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    for index, content in enumerate(chunks or []):
        db.add(Chunk(document_id=row.id, content=content, ordinal=index))
    await db.commit()
    return row


async def test_export_missing_path_is_422(client):  # noqa: F811
    """缺 ``path``：422（必填字段校验，走校验层而不是业务层 400）。"""
    resp = await client.post("/api/export/markdown", json={})
    assert resp.status_code == 422


async def test_export_bad_path_is_400_chinese(client, clean_extra, tmp_path):  # noqa: F811
    """路径不存在 / 是文件：400 + 可读中文（不是 500、不静默建目录）。"""
    missing = await client.post(
        "/api/export/markdown", json={"path": str(tmp_path / "不存在")}
    )
    assert missing.status_code == 400
    assert "导出目录不存在" in missing.json()["detail"]

    a_file = tmp_path / "文件.txt"
    a_file.write_text("x", encoding="utf-8")
    as_file = await client.post("/api/export/markdown", json={"path": str(a_file)})
    assert as_file.status_code == 400
    assert "是一个文件" in as_file.json()["detail"]


async def test_export_requires_at_least_one_kind(client, clean_extra, tmp_path):  # noqa: F811
    """两类都不勾：400 + 可读中文（老基线的把关口径）。"""
    resp = await client.post(
        "/api/export/markdown",
        json={"path": str(tmp_path), "include_notes": False, "include_documents": False},
    )
    assert resp.status_code == 400
    assert "至少勾选" in resp.json()["detail"]


async def test_export_empty_scope_is_400_and_creates_no_dir(client, clean_extra, tmp_path):  # noqa: F811
    """范围为空：400，且**绝不**留下一个只有时间戳名的空目录。"""
    resp = await client.post("/api/export/markdown", json={"path": str(tmp_path)})
    assert resp.status_code == 400
    assert "没有可导出的内容" in resp.json()["detail"]
    assert list(tmp_path.iterdir()) == []


async def test_export_writes_notes_and_documents(client, clean_extra, tmp_path):  # noqa: F811
    """正路：出参计数与文件树都对；软删的行不导出；只给分块的文档用分块兜底。"""
    await _note(clean_extra, "工作笔记", category="工作", tags=["复盘"])
    await _note(clean_extra, "散笔记")
    await _note(clean_extra, "已删笔记", deleted=True)
    await _document(clean_extra, "报告.md", category="资料", raw="报告原文")
    await _document(clean_extra, "无原文.md", raw="", chunks=["块一", "块二"])
    await _document(clean_extra, "已删文档.md", deleted=True)

    resp = await client.post("/api/export/markdown", json={"path": str(tmp_path)})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert set(data) == {"output_dir", "notes_exported", "documents_exported", "errors"}
    assert (data["notes_exported"], data["documents_exported"]) == (2, 2)
    assert data["errors"] == []

    root = Path(data["output_dir"])
    assert root.parent == tmp_path and root.is_dir(), "输出目录必须是所选目录下新建的时间戳目录"

    note_file = root / "notes" / "工作" / "工作笔记.md"
    assert note_file.is_file()
    text_of_note = note_file.read_text(encoding="utf-8")
    assert text_of_note.startswith("---\ntitle: 工作笔记\ncategory: 工作\ntags: [复盘]\n")
    assert "正文" in text_of_note
    assert (root / "notes" / "未分类" / "散笔记.md").is_file()
    assert not (root / "notes" / "未分类" / "已删笔记.md").exists(), "回收站里的笔记不导出"

    assert (root / "documents" / "资料" / "报告.md").read_text(encoding="utf-8").endswith("报告原文")
    fallback = (root / "documents" / "未分类" / "无原文.md").read_text(encoding="utf-8")
    assert fallback.endswith("块一\n\n块二"), "没有 raw_content 时按 ordinal 拼分块兜底"
    assert not (root / "documents" / "未分类" / "已删文档.md").exists()


async def test_export_scope_filters_are_intersected(client, clean_extra, tmp_path):  # noqa: F811
    """范围：分类 / 标签 / 自选 id 各维度生效（写死的分类里挑，其余不导出）。"""
    keep = await _note(clean_extra, "留", category="工作", tags=["A"])
    await _note(clean_extra, "走", category="生活", tags=["B"])

    by_category = await client.post(
        "/api/export/markdown", json={"path": str(tmp_path), "categories": ["工作"]}
    )
    assert by_category.status_code == 200
    assert by_category.json()["notes_exported"] == 1

    by_tag = await client.post(
        "/api/export/markdown", json={"path": str(tmp_path), "tags": ["B"]}
    )
    assert by_tag.json()["notes_exported"] == 1

    by_id = await client.post(
        "/api/export/markdown", json={"path": str(tmp_path), "note_ids": [keep.id]}
    )
    assert by_id.json()["notes_exported"] == 1

    none = await client.post(
        "/api/export/markdown", json={"path": str(tmp_path), "note_ids": [999999]}
    )
    assert none.status_code == 400
    assert "没有可导出的内容" in none.json()["detail"]


async def test_export_reports_skipped_document_in_errors(client, clean_extra, tmp_path):  # noqa: F811
    """没正文也没分块的文档：进 ``errors``（失败不静默），其余照导。"""
    await _note(clean_extra, "有内容")
    await _document(clean_extra, "空文档.md", raw="", chunks=[])

    resp = await client.post(
        "/api/export/markdown",
        json={"path": str(tmp_path), "include_notes": True, "include_documents": True},
    )
    data = resp.json()
    assert data["notes_exported"] == 1 and data["documents_exported"] == 0
    assert len(data["errors"]) == 1
    assert "空文档.md" in data["errors"][0]
