"""回归修复 载体：Filters.created_after / created_before 的 datetime 契约（02 §7.2）。

条目：契约把这两个字段冻结成 datetime|None，而实现声明成 str|None；坏日期进不了 422，
被 retrieval/types.py::_parse_dt 记一条 warning 后**静默放宽**成「该维度不限」——
与省略该字段同效，用户的范围限制被悄悄吃掉。

六条腿：
① 坏值 / 非文本值 ⇒ 422（静默放宽不再可达）；
② 合法 ISO 8601 串 ⇒ 200，且范围**真的到了检索层**（宿主范围带该维度、下推 SQL 出时间条件、
   落库回读仍是它）——不是「解析失败被丢掉」；
③ 省略该维度 ⇒ 仍「不限时间」（范围里没有该键、下推 SQL 无时间条件）；
④ 落库 / 缓存键形态：to_stored() 必须是 JSON 可序列化的 ISO 串（python 模式的 datetime
   会让 messages.filters（SQLAlchemy JSON 列）插库抛 TypeError、语义缓存键也 json.dumps 不了）；
⑤ **基本格式串 / 超长数字串**（审计 REAL-144-1 加固）："20260101" ⇒ 2026-01-01（**不得**
   被当 Unix 秒 ⇒ 1970-08-23）、"1748736000"（10 位纯数字）⇒ 422、两侧空白与 ISO 周日期
   恢复可用、offset 形态不受加固影响；
⑥ **小众偏移形态**（审计 REAL-144-2）：基本格式+Z / 基本格式+无冒号偏移 / date-only+Z /
   偏移前有空格 —— 四类都**不得 422**、与 _parse_dt 同值；扩展格式+偏移仍原样走 pydantic
   （落库形态逐字不变）。

跑法（定向、独占锁内）：
  cd <repo>\\backend; $env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH="<repo>/backend";
  ./.venv/Scripts/python.exe -m pytest tests/api/test_bug144_filters_datetime.py -m "not slow and not live" -q
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.api import chat_routes
from app.api.chat_schemas import Filters, _PYDANTIC_EXTENDED_OFFSET_RE
from app.retrieval.types import _parse_dt, document_where
from tests.api.test_chat_contract import client, use_faux  # noqa: F401 —— 两者都是夹具


# --- 腿①：坏值一律 422（改前：静默放宽成「该维度不限」） ---------------------


BAD_TIME_VALUES = [
    pytest.param("not-a-date", id="text"),
    pytest.param("2026-13-45", id="bad-month"),
    pytest.param("", id="empty"),
    pytest.param(20260919, id="int-not-unix"),
    pytest.param(1748736000.0, id="float"),
    pytest.param(True, id="bool"),
    pytest.param(["2026-01-01"], id="list"),
    pytest.param({"date": "2026-01-01"}, id="dict"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", BAD_TIME_VALUES)
async def test_created_after_bad_value_is_422(client, bad):  # noqa: F811
    resp = await client.post(
        "/api/chat",
        json={"message": "只找这之后的资料", "filters": {"created_after": bad}},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_created_before_bad_value_is_422(client):  # noqa: F811
    resp = await client.post(
        "/api/chat",
        json={"message": "只找这之前的资料", "filters": {"created_before": "上个月"}},
    )
    assert resp.status_code == 422, resp.text


def test_bad_value_raises_at_model_boundary():
    """模型层直判：坏值不许被收下，更不许退化成 None（None = 该维度不限 = 放宽）。"""
    for bad in ("not-a-date", "2026-13-45", "", 20260919, 1748736000.0, True, ["2026-01-01"]):
        with pytest.raises(ValidationError):
            Filters(created_after=bad)
        with pytest.raises(ValidationError):
            Filters(created_before=bad)


def test_valid_shapes_are_accepted_at_model_boundary():
    """正控：契约允许的形状（ISO 串 / datetime / None）都必须收下。"""
    assert Filters(created_after="2026-01-01").created_after == datetime(2026, 1, 1)
    assert Filters(created_after="2026-06-01T08:00:00+08:00").created_after == datetime(
        2026, 6, 1, 8, 0, tzinfo=timezone(timedelta(hours=8))
    )
    # Z / offset 形态保留 tzinfo（pydantic 原样收下，不做时区折算）——折算发生在 _parse_dt
    assert Filters(created_before="2026-06-01T00:00:00Z").created_before == datetime(
        2026, 6, 1, tzinfo=timezone.utc
    )
    assert Filters(created_after=datetime(2026, 3, 4, 5, 6)).created_after == datetime(
        2026, 3, 4, 5, 6
    )
    assert Filters().created_after is None
    assert Filters(created_after=None).created_after is None


# --- 腿②：合法 ISO 串仍通，且真的到了检索层（未放宽） -----------------------


@pytest.mark.asyncio
async def test_iso_string_passes_and_reaches_retrieval(client, monkeypatch, use_faux):  # noqa: F811
    seen: dict = {}

    async def _fake_retrieve(session, text, filters=None, *, stats=None):  # noqa: ANN001
        seen["filters"] = filters
        return [], "", None

    monkeypatch.setattr(chat_routes, "retrieve_materials", _fake_retrieve)
    use_faux("按范围回答。")

    resp = await client.post(
        "/api/chat",
        json={
            "message": "只找今年上半年的资料",
            "filters": {
                "created_after": "2026-01-01",
                "created_before": "2026-06-01T00:00:00Z",
            },
        },
    )
    assert resp.status_code == 200, resp.text

    # ② -a 范围真的进了检索层，且两个维度都在（不是解析失败被丢成 None）
    #    落库形态 = ISO 串：date-only 归一成带时刻的形态；Z / offset 形态原样保留
    assert seen["filters"] == {
        "created_after": "2026-01-01T00:00:00",
        "created_before": "2026-06-01T00:00:00Z",
    }, seen

    # ② -b 过生产下推函数：时间维度产出 SQL 条件（未放宽），且仍是 naive UTC 比较口径
    clauses = [str(clause) for clause in document_where(seen["filters"])]
    assert len(clauses) == 2, clauses
    assert all("created_at" in clause for clause in clauses), clauses
    assert _parse_dt(seen["filters"]["created_before"]) == datetime(2026, 6, 1)

    # ② -c 落库回读 == 同一份范围（JSON 列真写进去了；python 模式的 datetime 会在这里炸）
    detail = await client.get(f"/api/chats/{resp.json()['chat_id']}")
    assert detail.status_code == 200, detail.text
    stored_filters = [
        message["filters"] for message in detail.json()["messages"] if message.get("filters")
    ]
    assert stored_filters and stored_filters[0] == seen["filters"], stored_filters


# --- 腿③：省略该维度仍「不限时间」 ------------------------------------------


@pytest.mark.asyncio
async def test_omitted_time_dimension_stays_unbounded(client, monkeypatch, use_faux):  # noqa: F811
    seen: dict = {}

    async def _fake_retrieve(session, text, filters=None, *, stats=None):  # noqa: ANN001
        seen["filters"] = filters
        return [], "", None

    monkeypatch.setattr(chat_routes, "retrieve_materials", _fake_retrieve)
    use_faux("不限时间。")

    # ③ -a 整个 filters 省略 ⇒ 无范围
    resp = await client.post("/api/chat", json={"message": "随便找点资料"})
    assert resp.status_code == 200, resp.text
    assert seen["filters"] is None
    assert document_where(seen["filters"]) == []

    # ③ -b 只给别的维度 ⇒ 不该冒出时间键（该维度仍「不限」）
    resp = await client.post(
        "/api/chat", json={"message": "只在 7 号库里找资料", "filters": {"kb_ids": [7]}}
    )
    assert resp.status_code == 200, resp.text
    assert seen["filters"] == {"kb_ids": [7]}, seen
    clauses = [str(clause) for clause in document_where(seen["filters"])]
    assert clauses and all("created_at" not in clause for clause in clauses), clauses

    # ③ -c 显式 null 与省略同效
    resp = await client.post(
        "/api/chat",
        json={"message": "时间上不限制", "filters": {"created_after": None, "created_before": None}},
    )
    assert resp.status_code == 200, resp.text
    assert seen["filters"] is None, seen


# --- 腿④：落库 / 缓存键形态（JSON 可序列化的 ISO 串） ----------------------


def test_to_stored_keeps_json_ready_iso_shape():
    """to_stored() 必须是 JSON 可序列化的 ISO 串，且 _parse_dt 仍认得。

    python 模式的 datetime 会让 messages.filters（SQLAlchemy JSON 列，
    models/__init__.py:240）插库抛 TypeError、cache/entry.py::filters_token 的
    json.dumps 同样炸（前者 500、后者缓存静默降级）。
    """
    stored = Filters(
        created_after="2026-01-01", created_before="2026-06-01T08:00:00+08:00"
    ).to_stored()
    # date-only 归一成带时刻的 ISO；带 offset 的原样保留（与改前的「原样存字符串」同形）
    assert stored == {
        "created_after": "2026-01-01T00:00:00",
        "created_before": "2026-06-01T08:00:00+08:00",
    }, stored
    assert all(isinstance(value, str) for value in stored.values()), stored
    assert json.dumps(stored, sort_keys=True)  # 不抛 = JSON 列与缓存键都能吃

    # 回读口径不变：解析成 naive UTC，且不为 None（= 未放宽）
    assert _parse_dt(stored["created_after"]) == datetime(2026, 1, 1)
    assert _parse_dt(stored["created_before"]) == datetime(2026, 6, 1)

    # 进程内直传 datetime 同样落成 ISO 串（不是对象）
    assert Filters(created_after=datetime(2026, 3, 4, 5, 6)).to_stored() == {
        "created_after": "2026-03-04T05:06:00"
    }

    # 全空 / 只给 None ⇒ None（不写 JSON 'null'，维持 SQL NULL）
    assert Filters().to_stored() is None
    assert Filters(created_after=None).to_stored() is None

# --- 腿⑥：小众偏移形态（审计 REAL-144-2） ------------------------------------


#: pydantic **认不了**、但改前 _parse_dt 能正确解析的四类偏移形态（可见失败面）。
MINORITY_OFFSET_FORMS = (
    "20260101T000000Z",       # 基本格式 + Z
    "20260101T000000+0800",   # 基本格式 + 无冒号偏移
    "2026-01-01Z",            # date-only + Z
    "2026-01-01T00:00:00 Z",  # 偏移前多一个空格
)


def test_e6_minority_offset_forms_parse_like_parse_dt():
    """四类小众偏移形态**都不得 422**，且与 retrieval/types.py::_parse_dt 同值（负控）。

    改前 _parse_dt 是唯一入口 ⇒ 这些文本原本能**正确收窄**；REAL-144-2 的短路会让它们
    422（可见失败，不是静默错义）。逐形态断言：任一形态出问题，消息里带得出是哪一个。
    """
    for raw in MINORITY_OFFSET_FORMS:
        expected = _parse_dt(raw)
        assert expected is not None, f"构造前提不成立：_parse_dt 不认 {raw!r}"

        got = Filters(created_before=raw).created_before
        assert got == expected, (raw, got, expected)

        # 落库/回显形态必须是可 JSON 序列化的 ISO 串，且回读同值（不得变成 1970 / 空集）
        stored = Filters(created_before=raw).to_stored()["created_before"]
        assert isinstance(stored, str) and stored, (raw, stored)
        assert _parse_dt(stored) == expected, (raw, stored, _parse_dt(stored), expected)

        # 下推必须产出真条件（未放宽）
        clauses = document_where(Filters(created_before=raw).to_stored())
        assert len(clauses) == 1 and clauses[0].right.value == expected, (raw, clauses)


def test_e7_extended_offset_forms_still_go_through_pydantic_verbatim():
    """正控：**扩展格式 + 偏移**仍原样交回 pydantic ⇒ to_stored 形态逐字不变。

    防「把扩展格式也走成 fromisoformat」—— 那会把 …Z / …+08:00 的落库形态改掉。
    """
    assert Filters(created_before="2026-06-01T00:00:00Z").to_stored() == {
        "created_before": "2026-06-01T00:00:00Z"
    }
    assert Filters(created_before="2026-06-01T08:00:00+08:00").to_stored() == {
        "created_before": "2026-06-01T08:00:00+08:00"
    }

    # 判别器口径：扩展格式 + 偏移才命中（基本格式 / date-only 一律不命中 ⇒ 走自己解析）
    assert _PYDANTIC_EXTENDED_OFFSET_RE.search("2026-06-01T08:00:00+08:00")
    assert _PYDANTIC_EXTENDED_OFFSET_RE.search("2026-06-01 08:00Z")
    for raw in MINORITY_OFFSET_FORMS:
        assert not _PYDANTIC_EXTENDED_OFFSET_RE.search(raw), raw



# --- 腿⑤：基本格式串 / 超长数字串（审计 REAL-144-1 加固） --------------------


def test_e1_basic_format_date_string_is_parsed_not_unix_seconds():
    """YYYYMMDD 基本格式 ⇒ 2026-01-01（**严格负控：不得是 1970-08-23**）。

    加固前 "20260101" 被 pydantic 当 Unix 秒 ⇒ 1970-08-23T11:48:21Z，created_before
    几乎恒空集；而改前实现走 _parse_dt（fromisoformat 认基本格式）⇒ 2026-01-01。
    """
    note = Filters(created_before="20260101")
    assert note.created_before == datetime(2026, 1, 1), note.created_before
    assert note.to_stored() == {"created_before": "2026-01-01T00:00:00"}

    clauses = document_where(note.to_stored())
    assert len(clauses) == 1, clauses
    bound = clauses[0].right.value
    assert bound == datetime(2026, 1, 1), bound
    assert bound.year == 2026, "不得落到 1970（Unix 秒静默错义）"


def test_e2_long_numeric_string_is_422_not_unix_seconds():
    """超长纯数字（10 位 = Unix 秒形态）⇒ **422**（不得静默成 2025-06-01）。"""
    with pytest.raises(ValidationError):
        Filters(created_before="1748736000")
    with pytest.raises(ValidationError):
        Filters(created_after="1748736000")


def test_e3_padded_and_week_date_strings_recover():
    """两侧空白与 ISO 周日期恢复可用（照 _parse_dt 同源配方）。"""
    padded = Filters(created_after=" 2026-01-01 ")
    assert padded.created_after == datetime(2026, 1, 1)
    assert padded.to_stored() == {"created_after": "2026-01-01T00:00:00"}

    week = Filters(created_after="2026-W01-1")
    assert week.created_after == _parse_dt("2026-W01-1"), "与改前 _parse_dt 口径同源"
    assert week.created_after == datetime(2025, 12, 29), week.created_after


def test_e4_offset_forms_are_untouched_by_the_hardening():
    """加固不得改坏 offset 形态：Z / +08:00 仍原样落库（回归护栏）。"""
    assert Filters(created_before="2026-06-01T00:00:00Z").to_stored() == {
        "created_before": "2026-06-01T00:00:00Z"
    }
    assert Filters(created_before="2026-06-01T08:00:00+08:00").to_stored() == {
        "created_before": "2026-06-01T08:00:00+08:00"
    }


@pytest.mark.asyncio
async def test_e5_basic_format_string_over_http_reaches_retrieval_in_2026(
    client, monkeypatch, use_faux
):  # noqa: F811
    """端到端：created_before="20260101" ⇒ 200，下推上界落在 2026（不是 1970）。"""
    seen: dict = {}

    async def _fake_retrieve(session, text, filters=None, *, stats=None):  # noqa: ANN001
        seen["filters"] = filters
        return [], "", None

    monkeypatch.setattr(chat_routes, "retrieve_materials", _fake_retrieve)
    use_faux("按时间范围回答。")

    resp = await client.post(
        "/api/chat",
        json={"message": "只找 2026 年之前的资料", "filters": {"created_before": "20260101"}},
    )
    assert resp.status_code == 200, resp.text
    assert seen["filters"] == {"created_before": "2026-01-01T00:00:00"}, seen

    clauses = document_where(seen["filters"])
    assert len(clauses) == 1, clauses
    assert clauses[0].right.value == datetime(2026, 1, 1)
    assert clauses[0].right.value.year == 2026, "不得落到 1970（Unix 秒静默错义）"

