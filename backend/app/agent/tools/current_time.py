"""current_time：读取本机系统时钟（专项工具调用第 2 批 · 第 1 笔 · D-a/D-h）。

**契约工具名**：``current_time``（零参数）。只读**本机时间**，不读主机名 / 用户 /
路径 / 环境变量 / 时区配置（§2.1 第 6 条）。

## 两只钟分列（D-a）

- 观察正文写**本机钟**（带 UTC offset，格式 ``YYYY-MM-DDTHH:MM:SS±HH:MM``）；
- ``ToolContextItem.collected_at`` 仍由运行器写**库钟**（表头「库内时钟」，第 0 批 D7 不动）。

⚠️ 落点**不得**是 ``runner.py``（该文件有源码级负向钉子：不许 ``datetime.now(``）。
"""

from __future__ import annotations

import logging
from datetime import datetime

from pydantic import BaseModel

from app.agent.registry import ToolSpec
from app.agent.types import ToolObservation

logger = logging.getLogger(__name__)

TOOL_NAME = "current_time"

DESCRIPTION = "读取本机系统时钟的当前时间（含时区偏移），供回答对齐日期与时刻"

#: 观察文本的单行模板（A6 用 ``re.fullmatch`` 逐字判；**不要再加第二行**）。
#: 占位符 = ISO 8601 带 offset 的时刻（秒必填、offset 必须带冒号）。
OBSERVATION_TEMPLATE = "读取本机时间：{stamp}（本机系统时钟）"


class CurrentTimeArgs(BaseModel):
    """零字段参数模型（合法：``runner`` 只跳过 ``parameters is None``）。"""


Args = CurrentTimeArgs


def _now() -> datetime:
    """唯一取值点（可打桩接缝；失败路径用 ``monkeypatch.setattr`` 构造）。"""
    return datetime.now().astimezone()


def format_stamp(moment: datetime) -> str:
    """``YYYY-MM-DDTHH:MM:SS±HH:MM``（秒必填、offset 带冒号）。"""
    return moment.isoformat(timespec="seconds")


def build_spec() -> ToolSpec:
    """装配 current_time 工具声明（无依赖注入点）。"""

    async def handler(_args: CurrentTimeArgs) -> ToolObservation:
        try:
            stamp = format_stamp(_now())
        except Exception as exc:  # noqa: BLE001 —— 工具失败转观察，绝不炸穿（I5）
            # 回归修复：固定形状 + 原文只进日志
            logger.warning("读取本机时间执行异常：%s", exc)
            return ToolObservation(
                text=f"读取本机时间 执行失败（{exc.__class__.__name__}）。", ok=False
            )
        return ToolObservation(text=OBSERVATION_TEMPLATE.format(stamp=stamp))

    return ToolSpec(
        name=TOOL_NAME,
        description=DESCRIPTION,
        parameters_schema=CurrentTimeArgs.model_json_schema(),
        handler=handler,
        dangerous=False,
        parameters=CurrentTimeArgs,
        needs_web=False,
        needs_confirm=False,
    )


spec = build_spec()


__all__ = [
    "DESCRIPTION",
    "OBSERVATION_TEMPLATE",
    "Args",
    "CurrentTimeArgs",
    "TOOL_NAME",
    "build_spec",
    "format_stamp",
    "spec",
]
