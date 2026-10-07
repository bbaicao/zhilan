"""进程内 Redis 垫片（阶段 3 · 条目 3.0 · 交付物 ②）。

**为什么是垫片而不是真 Redis**：桌面交付（阶段 6 决议：SQLite + 内嵌 Qdrant +
Redis 垫片，不带任何服务）不能依赖 Redis 服务；但 ``/api/health`` 的 ``redis``
字段是**冻结契约**——前端 ``App.tsx`` 的状态灯判据是
``database !== "ok" || redis !== "ok" || !llm_configured``，``redis`` 不为
``ok`` 就永远不转绿。

实测口径（``回归修复``）：**Redis 在本项目两代实现里从未承载任何业务数据**
（消息真相源在 MySQL，``app/session/repository.py`` 明写禁止放进 Redis），
全仓对它的调用面**总共只有 5 个方法**：

- ``ping()``                                    （``app/api/__init__.py::health``）
- ``set(key, value, ex=)`` / ``get`` / ``delete`` / ``aclose()``
  （``app/db/verify.py::_check_redis`` 启动自检）

故保留 ``get_redis_client`` / ``close_redis`` 两个**函数签名**，把实现换成
**零外部依赖**的进程内对象：契约字段如实报 ``ok``、前端转绿、
**不需要解冻 ``frontend/src``**，也不需要用户装 Redis。

语义对齐（只覆盖上面的调用面，不做全量 Redis 兼容）：

- ``ping()`` → ``True``（调用点只判「不抛异常」，真 Redis 返回 ``PONG``）
- ``set(key, value, ex=秒)`` → 写入，``ex`` 秒后过期（省略 ``ex`` 即不过期）
- ``get(key)`` → 命中返回字符串；未命中或已过期返回 ``None``
- ``delete(*keys)`` → 返回实际删除的条数（与 redis-py 一致，调用点不使用）
- ``aclose()`` → 幂等释放。**垫片无外部资源，关闭后仍可继续使用**——
  启动自检会在 ``finally`` 里关闭它，而 health 探针随后还要 ``ping()``；
  真 Redis 是「关掉即失效」，这里刻意不模拟（契约只要求如实报可用）。

**边界（不是 Redis 等价物）**：无持久化、无跨进程共享、无发布订阅/事务/管道，
键值只活在当前进程内存里，**重启即丢**。将来若真要缓存或分布式锁，
应先扩本模块并登记（``开发约定`` / 缺陷清单），不要在调用点绕过。
"""

from __future__ import annotations

import time
from typing import Any

_client: "InMemoryRedis | None" = None


class InMemoryRedis:
    """进程内键值垫片；语义细节见模块 docstring。"""

    def __init__(self) -> None:
        # key -> (value, 过期时刻 | None)；用 monotonic 秒，免受系统时间调整影响
        self._data: dict[str, tuple[str, float | None]] = {}

    async def ping(self) -> bool:
        return True

    async def set(
        self,
        key: str,
        value: Any,
        ex: int | float | None = None,
        **_ignored: Any,
    ) -> bool:
        expire_at = (time.monotonic() + float(ex)) if ex else None
        self._data[str(key)] = (str(value), expire_at)
        return True

    async def get(self, key: str) -> str | None:
        name = str(key)
        item = self._data.get(name)
        if item is None:
            return None
        value, expire_at = item
        if expire_at is not None and time.monotonic() >= expire_at:
            self._data.pop(name, None)
            return None
        return value

    async def delete(self, *keys: str) -> int:
        return sum(1 for key in keys if self._data.pop(str(key), None) is not None)

    async def aclose(self) -> None:
        return None


def get_redis_client() -> InMemoryRedis:
    """进程内单例；签名与原实现一致（``REDIS_HOST/PORT/PASSWORD`` 不再读取）。"""
    global _client
    if _client is None:
        _client = InMemoryRedis()
    return _client


async def close_redis() -> None:
    """退出时释放（``main.py::lifespan`` 调用）；下次 ``get_redis_client()`` 会新建。"""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
