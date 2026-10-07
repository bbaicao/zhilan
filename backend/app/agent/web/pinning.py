"""DNS pinning 连接后端（专项「工具升级-2（R2）」· R2-10）。

## 解决什么

:mod:`app.agent.web.ssrf` 在**请求发出前**判「这个地址能不能去」；
本模块负责「**去的就是刚才判过的那个地址**」—— 两次解析之间的窗口（TOCTOU）与
「判定解析一次、连接时再解析一次」的 DNS rebinding 都在这里关上：
**解析 → 判定 → 记进 ``pins`` → 连接用记下的地址**，四步同一处完成、之后不再解析。

## 为什么是 httpcore 的私有接缝

``httpx.AsyncHTTPTransport`` 的实例化签名**没有** ``network_backend`` 参数（实测），
但它内部的 ``httpcore.AsyncConnectionPool`` **有** ``_network_backend``，而且
**实例化后立刻存在**（``transport._pool`` 实测可取）⇒ 换掉 ``pool._network_backend``
是唯一能在**不改 httpx 调用形状**的前提下接管连接解析的落点。
接缝是私有的 ⇒ 上游升级会让它失效：故 ``client._maybe_pin`` 一律 ``getattr`` 取用、
拿不到就降级（那里还有「代理在场 ⇒ 降级 + warning」一条腿）。

## 三级行为

1. **无 pin 记录**：解析 + **白名单判定**（走 :func:`app.agent.web.ssrf.assert_url_is_safe`），
   把返回的地址**记进 pins**；同一个 host 之后**只认第一次那个地址**（这就是「钉住」）。
2. **已有 pin**：直接用记下的地址，**不再解析**。
3. **逃生门开着**（``settings.WEB_SSRF_ALLOW_ANY_HOST``）：只解析、不判定，
   **每次放行留一条 WARNING**（`回归修复` 补齐；文案与另两处逐字一致，见下）。
   ⚠️ 开关**每次连接时**现读 ``settings``（不得在 import 期固化成常量）。

## ⚠️ 逃生门的三处留痕必须同口径（`回归修复`）

同一条开关在本仓有**三个**消费点，此前只有两处留痕：
``client.py::_guard_hook`` ✅ / ``core/net_guard.py::assert_outbound_url_safe`` ✅ /
**本模块 ``_resolve_once`` ❌（静默放行）**。统一口令的本体**只在调用点出现一次**
（见 :meth:`PinnedBackend._resolve_once` 里那条 ``logger.warning``），``%s`` 取**要放行的地址**；
判据按 logger 名过滤对拍（本模块 logger = ``app.agent.web.pinning``）。

## ⚠️ 解析放到线程里跑

``socket.getaddrinfo`` 是**阻塞**调用，而连接后端跑在事件循环线程上 ⇒ 直接调会把整个
后端卡住（断网机器上最坏挂到 DNS 超时，桌面窗口整片无响应）⇒ 一律走
``loop.run_in_executor``。

## ⚠️ 拒绝时抛的是 :class:`app.agent.web.ssrf.UrlNotAllowed`

不是 ``httpcore.ConnectError``：后者会被 httpx 映射成「网络错误」的笼统文案，
把「这个地址禁不起判定」这件**可执行**的信息丢掉。``UrlNotAllowed`` 一路传到工具层，
由既有的 ``except WebToolError`` / ``except Exception`` 收成 ``ok=False``（不炸穿，I5）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.agent.web import ssrf
from app.core.config import settings

# 本模块的日志通道（`回归修复` 新建）：逃生门开启时「每次放行留一条 WARNING」。
# 判据侧一律**按 logger 名过滤**：``caplog`` 是全 logger 收集，不过滤会把网络栈
# （传输层客户端 / 事件循环）的 WARNING 也收进来（假红）。
logger = logging.getLogger(__name__)


def _host_url(host: str) -> str:
    """把**裸 host** 还原成可判定的 URL（IPv6 字面量要把方括号加回去）。"""
    if ":" in host and not host.startswith("["):
        return f"http://[{host}]/"
    return f"http://{host}/"


class PinnedBackend:
    """把 ``connect_tcp(host, …)`` 的 host 换成**判定过的地址**的连接后端。

    只实现 ``httpcore.AsyncNetworkBackend`` 的三个方法（``connect_tcp`` /
    ``connect_unix_socket`` / ``sleep``），其余一律走 ``inner``。
    """

    def __init__(self, inner: Any, pins: dict[str, str] | None = None) -> None:
        self._inner = inner
        #: host → 已判定/已解出的地址（**一个 client 一份**：钉住的是「这台机器这次连接」）
        self.pins: dict[str, str] = pins if pins is not None else {}

    # ------------------------------------------------------------------
    # 解析 + 判定（唯一入口）
    # ------------------------------------------------------------------

    def _resolve_once(self, host: str) -> str:
        """**同步**解析 + 判定（调用方负责丢进线程）。返回要真正去连的地址。"""
        if bool(getattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", False)):
            # 逃生门（默认关，R2-12 起才有这个字段）：只解析、不判定
            try:
                addresses = ssrf.resolve_host(host)
            except OSError:
                addresses = []
            picked = str(addresses[0]) if addresses else host
            # `回归修复`：连接侧逃生门此前**零留痕** ⇒ 与另两处（`client.py::_guard_hook` /
            # `core/net_guard.py::assert_outbound_url_safe`）成文口径「开 = 每次放行一条 WARNING」
            # 不符。文案与另两处**逐字对齐**（同一句中文），便于 caplog 按 logger 名过滤对拍。
            # ⚠️ 每次**放行**一条 —— 同 host 的第二跳起走 ``pins`` 缓存、`_resolve_once` 不再被调，
            #    所以不会「每跳刷屏」。
            logger.warning(
                "SSRF 逃生门已开启（WEB_SSRF_ALLOW_ANY_HOST）：放行 %s", picked
            )
            return picked
        return ssrf.assert_url_is_safe(_host_url(host))

    async def _pinned_host(self, host: str) -> str:
        cached = self.pins.get(host)
        if cached:
            return cached
        loop = asyncio.get_running_loop()
        address = await loop.run_in_executor(None, self._resolve_once, host)
        self.pins[host] = address
        return address

    # ------------------------------------------------------------------
    # httpcore.AsyncNetworkBackend 的三个方法
    # ------------------------------------------------------------------

    async def connect_tcp(
        self,
        host: str,
        port: int | None = None,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        return await self._inner.connect_tcp(
            await self._pinned_host(host),
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any = None
    ) -> Any:
        # unix socket 没有 host 可判（也不出网）⇒ 原样透传
        return await self._inner.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def make_backend(inner: Any, pins: dict[str, str] | None = None) -> PinnedBackend:
    """工厂（**唯一**构造入口）。

    单测把这一处换成假件即可整体替换内层后端 —— 端到端腿靠它做到「零真实出网」
    （``monkeypatch.setattr(pinning, "make_backend", lambda inner, pins=None: …)``）。
    """
    return PinnedBackend(inner, pins)


__all__ = [
    "PinnedBackend",
    "make_backend",
]
