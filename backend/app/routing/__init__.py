"""意图路由 v1（阶段 1 · 条目 1.2）。

职责：判断一次提问该走**库内**（``internal_db``，去检索）还是**库外**
（``external``，直接回答），并留下可追溯的决策痕迹（I9）。

分层与边界：

- ``rules``    —— 正则规则集（纯函数，无 IO）
- ``llm``      —— LLM 兜底小调用（provider 可注入，FauxProvider 可测）
- ``decisions``—— 决策日志读写（落 ``session_events``，I9）
- ``router``   —— 编排：正则优先 → LLM 兜底 → 失败降级；对外唯一入口

本阶段只做**分路**，不做检索：``internal_db`` 只表示「该去库里找」，
**不代表一定有引用**（引用属阶段 3）。检索为空时的 direct/refuse 策略属 1.3。

依赖方向：本包只依赖 ``core`` / ``session``；不 import ``api`` / ``retrieval``。
"""

from app.routing.decision import RouteDecision, RouteSource, SourceType
from app.routing.router import route

__all__ = ["RouteDecision", "RouteSource", "SourceType", "route"]
