"""`tests/api` 的**同步** ASGI 客户端助手（阶段 4 · 条目 `4.5.3`）。

**为什么不复用 `test_chat_contract.py` 的 `client` 夹具**：那个夹具请求 `db` / `engine`
⇒ 需要起 MySQL 容器；而 settings / skills / system / translate 四个域**都不碰数据库**
（`settings_routes` 无 `Depends(get_db)`），照开工指令包 §5 的要求用**轻量**路子。

**为什么不用 `fastapi.testclient.TestClient` / `pytest.mark.asyncio`**：本助手把
`httpx.ASGITransport` 包成同步调用（内部一次 `asyncio.run`），这样用例是**纯同步**的
——不依赖 `asyncio_mode` 配置、也不会因为「用例被别的插件装进事件循环」而报
「asyncio.run() cannot be called from a running event loop」。

⚠️ 本助手**只挂被断言的那一个 router**（不是 `main.app`）：`main.app` 会拉起 lifespan
（Redis / MySQL / Qdrant 自检），而本条目的四个域都不需要它们。
"""

from __future__ import annotations

import asyncio
from typing import Any


class SyncClient:
    """极简同步 ASGI 客户端：`get` / `put` / `post` / `delete` / `request`。"""

    def __init__(self, app: Any) -> None:
        self._app = app

    def request(self, method: str, url: str, **kwargs: Any):
        async def _run():
            from httpx import ASGITransport, AsyncClient

            transport = ASGITransport(app=self._app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.request(method, url, **kwargs)

        return asyncio.run(_run())

    def get(self, url: str, **kwargs: Any):
        return self.request("GET", url, **kwargs)

    def put(self, url: str, **kwargs: Any):
        return self.request("PUT", url, **kwargs)

    def post(self, url: str, **kwargs: Any):
        return self.request("POST", url, **kwargs)

    def delete(self, url: str, **kwargs: Any):
        return self.request("DELETE", url, **kwargs)


def build_app(*routers: Any):
    """把给定 router（可多个）挂到一个**干净的** FastAPI 应用上，前缀 `/api`。"""
    from fastapi import FastAPI

    app = FastAPI()
    for router in routers:
        app.include_router(router, prefix="/api")
    return app


__all__ = ["SyncClient", "build_app"]
