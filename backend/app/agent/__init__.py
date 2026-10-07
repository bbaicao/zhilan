"""Agent 层：CubeLoop 决策循环 + 契约工具注册表（阶段 4 · 条目 4.1）。

三块：

- :mod:`app.agent.registry` —— 工具声明与注册表（**模型可见清单的唯一出口** ``for_model``，
  在那一处同时过滤 ``dangerous``（I6）与联网门控）；
- :mod:`app.agent.bootstrap` —— 装配：静态视图 :func:`build_default_registry` 与
  每轮视图 :func:`build_registry`（把会话/范围闭合进 handler）；
- :mod:`app.agent.runner` —— 跑一轮：:func:`stream_agent_turn` 产出 ``("agent", step)`` 帧，
  结束时把材料快照与轨迹写在 :class:`AgentTurn` 上。

依赖方向：``app.agent`` 可依赖 ``retrieval`` / ``generation`` / ``session`` / ``core``；
**不 import ``app.api``**（统筹 §3 分包边界）。
"""

from app.agent.bootstrap import (
    build_default_registry,
    build_registry,
    get_default_registry,
    reset_default_registry,
)
from app.agent.registry import (
    ToolAlreadyRegisteredError,
    ToolNotFoundError,
    ToolRegistry,
    ToolSpec,
)
from app.agent.runner import AgentTurn, agent_max_rounds, stream_agent_turn
from app.agent.types import ToolObservation

__all__ = [
    "AgentTurn",
    "ToolAlreadyRegisteredError",
    "ToolNotFoundError",
    "ToolObservation",
    "ToolRegistry",
    "ToolSpec",
    "agent_max_rounds",
    "build_default_registry",
    "build_registry",
    "get_default_registry",
    "reset_default_registry",
    "stream_agent_turn",
]
