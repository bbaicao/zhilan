"""token 用量域（任务清单 27 / 阶段 4 · 条目 4.5.4）。

分两层，**写入与读取分开**（与 ``app/observe`` 的分工同型）：

- :mod:`app.usage.recorder` —— **唯一写入入口**。fire-and-forget：绝不抛、绝不阻塞、
  绝不影响主流程；shared provider send 观测记录成功、失败、重试与停止的实际尝试，
  拿不到厂商 tokens 就写 NULL（界面按「未知」展示）。SDK 与原生 embed/rerank
  共用此写入层；回答收口不重复记账，未发生 send 不虚构调用；
- :mod:`app.usage.report` —— **只读**：近 N 日汇总（日 × 类型）、导出用明细、
  CSV 组装。表为空时返回「窗口内每天每类都补零」的形状，端点恒 200。

``usage_events`` 表由阶段 0 的 0003 迁移建好（列集见 ``app.models.UsageEvent``）。
本包**不新增列、不碰 alembic**：要让界面显示数字，唯一的办法是让真实调用真的写进这张表。
"""

from __future__ import annotations

from app.usage.recorder import KINDS, record_usage, record_usage_on
from app.usage.report import MAX_DAYS, MIN_DAYS, clamp_days, usage_detail, usage_summary

__all__ = [
    "KINDS",
    "MAX_DAYS",
    "MIN_DAYS",
    "clamp_days",
    "record_usage",
    "record_usage_on",
    "usage_detail",
    "usage_summary",
]
