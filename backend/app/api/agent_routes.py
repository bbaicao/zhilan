"""Agent 审计路由（阶段 4 · 条目 4.3.2；阶段 5 · 条目 5.5 扩查询面）。

**只读**——不提供任何修改 / 删除入口（老基线原话，照做）。数据源是 v2 自己的两张表
``agent_runs`` / ``agent_steps``（不是老基线的 JSONL，理由见 :mod:`app.agent.audit` 的模块 docstring）。

响应形状沿用老基线的 ``{"items": [...]}``：前端**零消费**这个端点（实测 ``frontend/src`` 里搜
``agent/audit`` 零命中）⇒ 形状不受 I1 强约束，但真相源优先级是「02 > 老基线 > 实现细节」，
照老基线做、不自创 ``{"records": ...}`` 这类形状。

## 条目 `5.5` 的**只增**扩展（2026-09-19）

条目原文写「只增**端点**或字段」，而 `4.5` 包的成文纪律写「**不新造端点**：``02`` 的 53 条是全集
……擅自加一条同样是违约」⇒ **取严 = 只加字段 / 只加查询参数**（开工包 §0.0 第 1 条）。
故本文件只做两件事，**路由注册面一行未改**：

1. **新增 6 个查询参数**（``run_id`` / ``status`` / ``type`` / ``since`` / ``until`` / ``offset``），
   坏值一律 **422 + 可读中文**（**不许静默忽略**：那会把「拼错的状态名」伪装成「查过了、没有」）；
2. **新增 3 个响应顶层键**（``runs`` / ``query`` / ``counters``）+ ``items[]`` 的第 12 键 ``run``。

**四律一条不动**：``{"items": [...]}`` 外层 / ``limit`` 夹 **1..500**（坏值回落 **200**）/
``id`` 倒序 / **只读**。契约手续（``02`` 变更记录 ``v1.0.4``）见
``接口说明/02-API契约冻结清单.md``。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import audit as agent_audit
from app.db.session import get_db

router = APIRouter()


@router.get("/agent/audit")
async def get_agent_audit(
    chat_id: int | None = None,
    limit: int = 200,
    run_id: int | None = None,
    status: str | None = None,
    type: str | None = None,  # noqa: A002 —— 查询参数名就契约面那个字，改不得
    since: str | None = None,
    until: str | None = None,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """工具调用审计查询：``chat_id`` 过滤单个会话；``limit`` 夹在 1..500；**最新在前**。

    ``since`` / ``until`` 是 ISO 8601，作用在 ``agent_steps.created_at`` 的**半开区间**
    ``[since, until)``；``offset`` 与 ``limit`` 组合分页（负值夹 0）。

    **空值 = 没给过滤**（``since`` / ``until`` / ``status`` / ``type`` 的空串与纯空白一律按「没给」
    处理——这是「用户清空筛选框」的自然语义，也是表单类客户端提交空字段时的行为；**只有真的解析不了
    才 422**，不许把坏值当成「没给」）。``query.count`` 是**本页返回条数**，不是命中总数
    （判断有没有下一页：``count == limit``）。
    """
    try:
        # 空值 = 没给过滤（与 status / type 同口径）；真解析不了才走 422
        since_text = (since or "").strip()
        until_text = (until or "").strip()
        since_at = agent_audit.parse_audit_time(since_text, field="since") if since_text else None
        until_at = agent_audit.parse_audit_time(until_text, field="until") if until_text else None
        run_status = agent_audit.normalize_run_status(status)
    except ValueError as exc:
        # 422 可读中文：坏过滤条件**必须**说出来，不能当成「没有过滤」
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    step_type = (type or "").strip() or None
    effective_limit = agent_audit.safe_limit(limit)
    effective_offset = agent_audit.safe_offset(offset)
    items = await agent_audit.read_audit(
        db,
        chat_id=chat_id,
        limit=effective_limit,
        run_id=run_id,
        status=run_status,
        step_type=step_type,
        since=since_at,
        until=until_at,
        offset=effective_offset,
    )
    return {
        # 既有键（逐字不变）
        "items": items,
        # --- 以下 3 个顶层键是条目 5.5 的只增扩展 ---------------------------
        "runs": agent_audit.summarize_runs(items),
        "query": {
            "limit": effective_limit,
            "offset": effective_offset,
            # ⚠️ count = **本页返回条数**，不是命中总数（判断有没有下一页：count == limit）。
            # 没有 total：那要再加一次 COUNT 查询，而这条端点的用途是排查/面板，不值这个代价。
            "count": len(items),
            "filters": {
                "chat_id": chat_id,
                "run_id": run_id,
                "status": run_status,
                "type": step_type,
                "since": agent_audit.iso_time(since_at),
                "until": agent_audit.iso_time(until_at),
            },
        },
        "counters": agent_audit.safe_counters(),
    }


__all__ = ["get_agent_audit", "router"]
