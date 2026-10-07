"""guard 器官（阶段 3 · 条目 3.3）：**异常路径也必有一条收尾**（I5 的唯一控制点）。

SSE 三条纪律里最容易破的一条是「绝不静默结束」：生成中途抛错、落库失败、某条分支提前
``return`` 而忘了收尾——用户界面就会一直转圈。本类把**收尾**变成一个**只能成功一次**的登记：

```python
guard = StreamGuard()
...
if guard.done():          # 第一次登记 → True，调用方才发 done 帧
    yield sse({"type": "done", ...})
...
if not guard.finished:    # 兜底：任何路径忘了收尾，这里补一条 error
    guard.error()
    yield sse({"type": "error", "detail": "回答流程异常结束，请重试。"})
```

为什么是「登记 + 调用方发帧」而不是「本类直接产 SSE 字节」：器官层**不 import**
``app.api``（依赖方向 `api → observe`），帧格式属于契约适配层。

阶段 4 的 Agent 流（``agent_trace`` / 多步长跑）复用同一个守卫——它同样是「长跑 + 必须收尾」。
"""

from __future__ import annotations

DONE = "done"
ERROR = "error"
# 阶段 3 · 条目 3.4：用户主动停止也是一种**收尾**（SSE 的 `stopped` 帧），同样只允许一次
STOPPED = "stopped"


class StreamGuard:
    """收尾登记器：``done`` / ``error`` / ``stopped`` **各只能被接受一次**，三者互斥。"""

    def __init__(self) -> None:
        self._terminal: str | None = None

    @property
    def finished(self) -> bool:
        """是否已经收尾（无论 done 还是 error）。"""
        return self._terminal is not None

    @property
    def terminal(self) -> str | None:
        """已登记的收尾类型（``"done"`` / ``"error"`` / ``None``）。"""
        return self._terminal

    def done(self) -> bool:
        """登记 ``done``；返回**是否被接受**（已收尾 → False，调用方据此不再发帧）。"""
        return self._mark(DONE)

    def error(self) -> bool:
        """登记 ``error``；返回**是否被接受**（已收尾 → False）。"""
        return self._mark(ERROR)

    def stopped(self) -> bool:
        """登记 ``stopped``（用户主动停止）；返回**是否被接受**（已收尾 → False）。"""
        return self._mark(STOPPED)

    def _mark(self, kind: str) -> bool:
        if self._terminal is not None:
            return False
        self._terminal = kind
        return True


__all__ = ["DONE", "ERROR", "STOPPED", "StreamGuard"]
