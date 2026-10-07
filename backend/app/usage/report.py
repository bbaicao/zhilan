"""用量汇总 / 明细查询 + CSV 组装（任务清单 27 / 阶段 4 · 条目 4.5.4）：**只读**。

口径逐条沿用老基线 ``<legacy-repo>\\backend\\app\\rag\\usage.py`` 的聚合部分：

- 窗口 ``1..90`` 天，越界夹取、认不出回 7（``clamp_days``）；
- 「今天」取**数据库时钟**（``DATE(CURRENT_TIMESTAMP)``）——``usage_events.created_at``
  是库时钟写的（``server_default=func.now()``），窗口与数据同钟，不受应用机与库的
  时区差影响；
- ``by_day`` 覆盖窗口内**每一天**（没有记录的天补零），从旧到新；
- 每天 ``by_kind`` **固定含四类**（chat / vision / embed / rerank），缺的补零——
  界面不用猜键；
- ``total_tokens`` 为 NULL 的那部分调用记 ``unknown_calls``（**按行判定**，
  与同组的已知行互不吞并：同一天既有「未知」行又有「已知」行时，
  ``total_tokens`` 是已知行的和，``unknown_calls`` 单列未知行的次数）；
- **表为空时**：``total`` 是全零格、``by_day`` 是「天数 × 4 类」的全零格，
  端点 200 返回，不报错、不编数字（前端本来就把 0 次调用渲染成「—」）。

相对老基线的两处**有意差异**（都写在 ``证据/回归检查4口径.md``）：

1. 查询函数**收会话**（``db``）而不是自开 ``SessionLocal``——与 v2 全仓
   「会话由调用方传入」的口径一致，单测里因此读的是**测试库**；
2. 日期分组用 ``func.date(...)`` 并把结果归一成 :class:`datetime.date`
   （MySQL 回 ``date``、SQLite 回 ``'YYYY-MM-DD'`` 字符串），两个方言同形状。
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UsageEvent
from app.usage.recorder import KINDS

#: 汇总窗口边界：展示最多回看 90 天，最少 1 天。
MIN_DAYS, MAX_DAYS = 1, 90

#: 认不出的 ``days`` 回落到 7（老基线同款）。
DEFAULT_DAYS = 7

#: CSV 表头（中文，Excel 直开）与「拿不到 tokens」的单元格文案。
CSV_HEADERS: tuple[str, ...] = (
    "日期",
    "类型",
    "提供商",
    "模型",
    "调用次数",
    "提示tokens",
    "补全tokens",
    "总tokens",
)
KIND_LABELS: dict[str, str] = {"chat": "对话", "vision": "视觉", "embed": "向量", "rerank": "重排序"}
UNKNOWN_CELL = "未知"


def clamp_days(days: object) -> int:
    """夹取 ``1..90``：不认识的一律回 7（``None`` / 非数字 / 越界都不会 500）。"""
    try:
        value = int(days)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return max(MIN_DAYS, min(MAX_DAYS, value))


def _as_int(value: object) -> int | None:
    """SUM 在 MySQL 里回 DECIMAL、在 SQLite 里回 int：统一收成 int（NULL 仍是 None=「未知」）。"""
    if value is None:
        return None
    return int(value)


def _empty_stat() -> dict:
    """一个统计格（字段与冻结前端 ``UsageStat`` 逐字一致）。"""
    return {
        "calls": 0,
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "unknown_calls": 0,
    }


def _stat_from_row(calls, prompt, completion, total, unknown) -> dict:
    return {
        "calls": _as_int(calls) or 0,
        "prompt_tokens": _as_int(prompt),
        "completion_tokens": _as_int(completion),
        "total_tokens": _as_int(total),
        "unknown_calls": _as_int(unknown) or 0,
    }


def _merge_stat(acc: dict, add: dict) -> dict:
    """把一格并进累加格：tokens 只在**取到值**时相加（None 不吞掉已知的和）。"""
    acc["calls"] += add["calls"]
    acc["unknown_calls"] += add["unknown_calls"]
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if add[key] is not None:
            acc[key] = (acc[key] or 0) + add[key]
    return acc


def _as_date(value: object) -> date | None:
    """把 ``func.date(...)`` 的结果归一成 :class:`datetime.date`（认不出回 ``None``）。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _day_start(day: date) -> datetime:
    """某天的零点（naive）：``created_at`` 与窗口都用库时钟，比较口径一致。"""
    return datetime.combine(day, time.min)


async def _db_today(db: AsyncSession) -> date:
    """数据库时钟的「今天」。

    ``date(CURRENT_TIMESTAMP)`` 两个方言都认（MySQL = 服务器本地时区的今天；
    SQLite = UTC 的今天，而 SQLite 侧的 ``created_at`` 也是 ``CURRENT_TIMESTAMP``
    写的 UTC —— 两边同钟）。
    """
    value = (await db.execute(select(func.date(func.current_timestamp())))).scalar()
    today = _as_date(value)
    if today is None:  # pragma: no cover —— 方言方言怪值时回落应用机时钟，绝不 500
        today = datetime.now().date()
    return today


def _window(db_today: date, window: int) -> tuple[date, datetime]:
    """窗口起点与「第一天的零点」（查询下界）。"""
    start = db_today - timedelta(days=window - 1)
    return start, _day_start(start)


async def usage_summary(db: AsyncSession, days: object = DEFAULT_DAYS) -> dict:
    """近 N 日用量：按 日 × 类型 汇总 tokens / calls + 总计 + ``unknown_calls``。

    返回体字段与冻结前端 ``UsageSummary`` 逐字一致：``days`` / ``total`` / ``by_day``。
    """
    window = clamp_days(days)
    start_date, start_at = _window(await _db_today(db), window)
    unknown_expr = func.sum(
        case((UsageEvent.total_tokens.is_(None), UsageEvent.calls), else_=0)
    )
    rows = (
        await db.execute(
            select(
                func.date(UsageEvent.created_at),
                UsageEvent.kind,
                func.sum(UsageEvent.calls),
                func.sum(UsageEvent.prompt_tokens),
                func.sum(UsageEvent.completion_tokens),
                func.sum(UsageEvent.total_tokens),
                unknown_expr,
            )
            .where(UsageEvent.created_at >= start_at)
            .group_by(func.date(UsageEvent.created_at), UsageEvent.kind)
        )
    ).all()

    by_date: dict[date, dict[str, dict]] = {}
    total = _empty_stat()
    for day, kind, calls, prompt, completion, token_total, unknown in rows:
        stat = _stat_from_row(calls, prompt, completion, token_total, unknown)
        the_day = _as_date(day)
        if the_day is not None:
            kinds = by_date.setdefault(the_day, {})
            _merge_stat(kinds.setdefault(str(kind), _empty_stat()), stat)
        _merge_stat(total, stat)

    by_day = []
    for offset in range(window):
        day = start_date + timedelta(days=offset)
        kinds = by_date.get(day, {})
        by_day.append(
            {
                "date": day.isoformat(),
                "by_kind": {kind: kinds.get(kind) or _empty_stat() for kind in KINDS},
            }
        )
    return {"days": window, "total": total, "by_day": by_day}


async def usage_detail(db: AsyncSession, days: object = DEFAULT_DAYS) -> list[dict]:
    """导出用的明细：按 日 × 类型 × 提供商 × 模型 聚合（CSV 每行一份）。"""
    window = clamp_days(days)
    start_date, start_at = _window(await _db_today(db), window)
    rows = (
        await db.execute(
            select(
                func.date(UsageEvent.created_at),
                UsageEvent.kind,
                UsageEvent.provider,
                UsageEvent.model,
                func.sum(UsageEvent.calls),
                func.sum(UsageEvent.prompt_tokens),
                func.sum(UsageEvent.completion_tokens),
                func.sum(UsageEvent.total_tokens),
            )
            .where(UsageEvent.created_at >= start_at)
            .group_by(
                func.date(UsageEvent.created_at),
                UsageEvent.kind,
                UsageEvent.provider,
                UsageEvent.model,
            )
            .order_by(func.date(UsageEvent.created_at), UsageEvent.kind)
        )
    ).all()
    return [
        {
            "date": (_as_date(day) or start_date).isoformat(),
            "kind": str(kind),
            "provider": str(provider or ""),
            "model": str(model or ""),
            "calls": int(calls or 0),
            "prompt_tokens": _as_int(prompt),
            "completion_tokens": _as_int(completion),
            "total_tokens": _as_int(token_total),
        }
        for day, kind, provider, model, calls, prompt, completion, token_total in rows
    ]


def csv_rows(rows: list[dict]) -> Iterator[str]:
    """把明细行拼成 CSV 文本块：**首块带 BOM**（Excel 双击直开中文不乱码）。"""
    # BOM：Excel 直接双击打开才认 UTF-8 中文
    yield "\ufeff"
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_HEADERS)
    yield buffer.getvalue()
    for row in rows:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            (
                row["date"],
                KIND_LABELS.get(row["kind"], row["kind"]),
                row["provider"],
                row["model"],
                row["calls"],
                UNKNOWN_CELL if row["prompt_tokens"] is None else row["prompt_tokens"],
                UNKNOWN_CELL if row["completion_tokens"] is None else row["completion_tokens"],
                UNKNOWN_CELL if row["total_tokens"] is None else row["total_tokens"],
            )
        )
        yield buffer.getvalue()


__all__ = [
    "CSV_HEADERS",
    "DEFAULT_DAYS",
    "KIND_LABELS",
    "MAX_DAYS",
    "MIN_DAYS",
    "UNKNOWN_CELL",
    "clamp_days",
    "csv_rows",
    "usage_detail",
    "usage_summary",
]
