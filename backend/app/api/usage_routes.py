"""token 用量端点（任务清单 27 / 阶段 4 · 条目 4.5.4）：查询与 CSV 导出。

两条端点由 ``app/api/system_routes.py`` include 进来（挂 ``/api`` 前缀下）：

- ``GET /api/usage?days=7`` —— 近 N 日汇总（按日 × 类型 + 总计 + ``unknown_calls``）；
- ``GET /api/usage/export?days=7`` —— 同窗口的明细 CSV 下载
  （UTF-8 **with BOM**，Excel 直接双击打开中文不乱码）。

**数据从哪来**：只来自 ``usage_events`` 的真实行，而写入点只有三处
（回答成功收口 / 嵌入成功 / 重排成功，见 ``app/usage/recorder.py``）。
**表为空时**：端点照样 200，``total`` 与 ``by_day`` 全是补零格（**不编数字**，
前端把 0 次调用渲染成「—」；CSV 只有表头）。

``days`` 越界 / 认不出：``clamp_days`` 夹到 1..90、认不出回 7 —— **不 500**
（老基线同款；``?days=abc`` 这类连 int 都转不了的由 FastAPI 给 422，也是契约 §4 的口径）。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.usage import report

router = APIRouter()


@router.get("/usage")
async def get_usage(
    days: int = report.DEFAULT_DAYS, db: AsyncSession = Depends(get_db)
) -> dict:
    """近 N 日（默认 7，夹取 1..90）的 tokens 与调用次数汇总。

    响应三键与冻结前端 ``UsageSummary`` 逐字一致：``days`` / ``total`` / ``by_day``。
    """
    return await report.usage_summary(db, report.clamp_days(days))


@router.get("/usage/export")
async def export_usage_csv(
    days: int = report.DEFAULT_DAYS, db: AsyncSession = Depends(get_db)
) -> StreamingResponse:
    """导出近 N 日用量明细为 CSV（``text/csv; charset=utf-8``，UTF-8 with BOM）。"""
    window = report.clamp_days(days)
    rows = await report.usage_detail(db, window)
    filename = f"usage-{date.today().isoformat()}.csv"
    return StreamingResponse(
        report.csv_rows(rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
