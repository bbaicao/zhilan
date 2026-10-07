"""出网白名单判定核心（专项「工具升级-2（R2）」· R2-09）。

## 为什么单独一个文件

本模块是**判定核心**：只依赖标准库（``ipaddress`` / ``socket`` / ``urllib.parse``），
**不 import httpx、不 import 本仓任何其它模块** —— 保住「零外部依赖、可纯单测、
可在任意调用点复用」这条性质。真正把判定接到连接上的两处接缝在同包的
:mod:`app.agent.web.pinning`（连接后端不落一次解析）与 :mod:`app.agent.web.client`
（``request`` 钩子，每个出站请求 + 每个重定向跳都过一遍）。

## 口径：白名单，不是黑名单

判据是 ``ipaddress.is_global``（**白名单式**：只有公网地址放行）。黑名单（列举
``10/8``、``127/8``、``169.254/16`` …）永远列不全——回环别名、云元数据地址、CGNAT、
``0.0.0.0``、IPv6 的各种内嵌形态都是现成的漏网之鱼。

## 为什么必须解嵌（:func:`_effective_ip`）

Python **3.13 实测**：``is_global("64:ff9b::7f00:1") == True`` ——
``64:ff9b::/96``（NAT64 公认前缀）整体落在公网前缀里，但它的**低 32 位就是内嵌 IPv4**，
``::7f00:1`` 翻出来是 ``127.0.0.1``。不做解嵌，NAT64 就是一条绕过守卫的现成通道
（**本模块唯一的真判别点**，只写 ``64:ff9b::808:808`` 放行的正控会是恒真断言）。

``::ffff:0:0/96``（IPv4-mapped）与 ``2002::/16``（6to4）由 ``ipaddress`` 自己处置：
实测 ``is_global("::ffff:127.0.0.1") == False``、``is_global("2002:7f00:1::1") == False``。
本模块对**原始地址与解嵌地址各判一次**（:func:`assert_url_is_safe` 第 ④ 步）——
这样 NAT64 私网被拒，而 ``ipaddress`` 本来就拒的形态（6to4 / teredo）**不会**因为
解嵌反倒被放行。解嵌只用来**收紧**，从不用来放宽。

## 白名单 = ``is_global`` **减掉**实测漏网段（`回归修复`）

``is_global`` **不等于**「能路由到某个公网主机」。下列形态 Python **3.13.14 实测**判 ``True``
却绝不能放行（本模块显式拒，回归钉见 ``tests/api/test_web_ssrf_r2.py`` 的两条 ``回归修复`` 腿）：

| 形态 | 实测读数 | 为什么危险 |
|---|---|---|
| ``::127.0.0.1`` / ``::169.254.169.254``（IPv4-compatible，RFC 4291 已废） | ``is_global is True`` | 低 32 位就是内嵌 IPv4，部分栈按 IPv4 回环 / 元数据地址处理 |
| ``::ffff:0:127.0.0.1``（IPv4-translated，RFC 2765） | ``is_global is True`` | 同上（``ipv4_mapped`` **不**认这一段） |
| ``fec0::1``（site-local，RFC 3879 已废） | ``is_global is True`` | 站点内部地址 |
| ``ff02::1`` / ``224.0.0.1``（组播） | ``is_global is True`` | 不是「某台主机的公网地址」，且是 LAN 发现（SSDP 等）的入口 |
| ``192.88.99.1``（6to4 中继 anycast，RFC 7526 已废） | ``is_global is True`` | 特殊路由，不是普通主机地址 |
| ``192.0.0.9``（IETF 协议保留 ``/24``） | ``is_global is True``（同段 ``192.0.0.8`` 却 False） | 半开的保留段 |

⇒ 判定 = ``is_global(原始)`` **且** ``is_global(解嵌)`` **且** 两者都不落在 :data:`_NOT_PUBLIC_NETWORKS`。

## 判定顺序（冻结；开工包 §0.0 **D-回归检查**）

① scheme ∈ {http, https} ② host 非空 ③ URL 不得内嵌凭据
④ 解析出的**每一个**地址（原始形态 + 解嵌形态）都必须 ``is_global``
⑤ 返回第一个已校验的地址（供 :mod:`app.agent.web.pinning` 固定本次连接）。

## 可注入的两处（**都不做 import 期固化**）

- ``resolver``：``None`` 时**在调用时**查模块全局 :func:`resolve_host` ——
  所以 ``monkeypatch.setattr(ssrf, "resolve_host", fake)`` 生效（同型先例：
  web 工具 handler 的 ``impl or fetch_page``）。⚠️ 该全局是**进程级状态**，
  用例必须自带复位夹具（见 ``tests/api/test_web_ssrf_r2.py``）。
- ``allow_any_host``：**逃生门的真值由调用方（``client`` 的请求钩子）每次调用时
  从 ``settings`` 现读**后传进来 —— 本模块不认识配置，也就不可能把开关固化成常量
  （固化会让「改 ``.env`` 后生效」变成「重启才生效」）。
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlparse

#: NAT64 公认前缀（RFC 6052）：低 32 位是内嵌 IPv4，必须解出来才能判
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")

#: 另外两种「低 32 位是内嵌 IPv4、而 ``ipaddress`` **不**替我们解」的形态（`回归修复` 实测）：
#: ``::/96`` IPv4-compatible（RFC 4291 已废）与 ``::ffff:0:0:0/96`` IPv4-translated（RFC 2765）。
_V4_COMPATIBLE_PREFIX = ipaddress.ip_network("::/96")
_V4_TRANSLATED_PREFIX = ipaddress.ip_network("::ffff:0:0:0/96")

#: ``::`` 与 ``::1`` 也落在 ``::/96`` 里，但它们**不是**「内嵌 IPv4」语义（且本就被 ``is_global`` 拒）
#: ⇒ 解包时排除，免得 ``_effective_ip("::1")`` 变成 ``0.0.0.1`` 这种读不懂的形状。
_V6_UNSPECIFIED = ipaddress.ip_address("::")
_V6_LOOPBACK = ipaddress.ip_address("::1")

#: ``ipaddress.is_global`` **自己漏掉**、而白名单绝不能放行的网段（Python 3.13.14 逐条实测）：
#:   · ``224.0.0.0/4``    IPv4 组播（实测 ``is_global("224.0.0.1") is True``）
#:   · ``192.88.99.0/24`` 6to4 中继 anycast（RFC 7526 已废；实测 True）
#:   · ``192.0.0.0/24``   IETF 协议保留（实测 ``192.0.0.9`` 被放过、``192.0.0.8`` 被拒 —— 半开）
#:   · ``fec0::/10``      site-local（RFC 3879 已废；实测 True）
#:   · ``ff00::/8``       IPv6 组播（实测 ``ff02::1`` 被放过）
_NOT_PUBLIC_NETWORKS = (
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("192.88.99.0/24"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("fec0::/10"),
    ipaddress.ip_network("ff00::/8"),
)

#: 非 http(s) 的拒绝文案**沿用**既有串（``client.py`` 的 ``fetch_page`` 用的是同一句，
#: ``tests/agent/test_web_client.py`` 用子串断言钉着它）
_BAD_SCHEME = "不是合法的网页地址"


class UrlNotAllowed(Exception):
    """出网地址被安全判定拒绝（消息是**可读中文**，上层包成 ``WebToolError`` 给用户看）。"""


def resolve_host(host: str) -> list[str]:
    """域名 → IP 列表（**模块级默认解析器**，用例注入桩的锚点）。

    返回全部地址而不是第一条：一条 A 记录公网、一条私网这种「混答」必须整体拒绝，
    只取第一条会留下「DNS 轮流应答」的绕过窗口。
    """
    addresses: list[str] = []
    for item in socket.getaddrinfo(host, None):
        address = str(item[4][0])
        if address not in addresses:
            addresses.append(address)
    return addresses


def _effective_ip(ip: str) -> str:
    """解出 IPv6 里内嵌的 IPv4（NAT64 / IPv4-mapped / 6to4 / IPv4-compatible / IPv4-translated）。

    没有内嵌就原样返回。只**收紧**判定用：调用方拿它和原始地址各判一次，任一个不是公网就拒。
    """
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return str(ip)
    if not isinstance(address, ipaddress.IPv6Address):
        return str(address)
    embedded = address.ipv4_mapped
    if embedded is None:
        embedded = address.sixtofour
    if embedded is None and address in _NAT64_PREFIX:
        # 低 32 位 = 内嵌 IPv4（RFC 6052 的 /96 形态）
        embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    if embedded is None and address in _V4_TRANSLATED_PREFIX:
        # IPv4-translated（``::ffff:0:0:0/96``，RFC 2765）：``ipv4_mapped`` **不**认这一段
        embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    if (
        embedded is None
        and address in _V4_COMPATIBLE_PREFIX
        and address not in (_V6_UNSPECIFIED, _V6_LOOPBACK)
    ):
        # IPv4-compatible（``::a.b.c.d``，RFC 4291 已废）：低 32 位也是内嵌 IPv4
        embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return str(embedded) if embedded is not None else str(address)


def _is_public(ip: str) -> bool:
    """白名单判定：``is_global`` **再减掉** :data:`_NOT_PUBLIC_NETWORKS` 那几段。

    连 IPv4/IPv6 都不是的串（畸形 host）一律判「不是公网」。
    """
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if not address.is_global:
        return False
    return not any(address in network for network in _NOT_PUBLIC_NETWORKS)


def assert_url_is_safe(
    url: str,
    *,
    resolver: Callable[[str], list[str]] | None = None,
    allow_any_host: bool | None = None,
) -> str:
    """校验 URL 是否允许出网；通过则返回**第一个已校验的地址**（供连接侧固定）。

    拒绝一律抛 :class:`UrlNotAllowed`（可读中文）。``resolver`` / ``allow_any_host``
    为 ``None`` 时分别取模块级 :func:`resolve_host` 与「逃生门关闭」。
    """
    text = str(url or "")
    try:
        parsed = urlparse(text)
        scheme = (parsed.scheme or "").lower()
        host = parsed.hostname or ""
        credentials = (parsed.username, parsed.password)
    except ValueError as exc:  # 畸形 URL（如未闭合的 ``[``）不该让调用方炸穿
        raise UrlNotAllowed(f"{_BAD_SCHEME}：{text[:80]}") from exc

    if scheme not in ("http", "https"):
        raise UrlNotAllowed(f"{_BAD_SCHEME}：{text[:80]}")
    if not host:
        raise UrlNotAllowed(f"{_BAD_SCHEME}：缺少主机名（{text[:80]}）")
    if credentials[0] or credentials[1]:
        raise UrlNotAllowed(f"网页地址里不允许内嵌用户名或口令，已拒绝访问：{host}")

    # ⚠️ **调用时**查模块全局（不是默认参数绑定）——注入桩才生效
    resolve = resolver if resolver is not None else resolve_host
    try:
        addresses = list(resolve(host))
    except (OSError, UnicodeError) as exc:
        # ⚠️ `回归修复`：`getaddrinfo` 对畸形 label（空 label / 超 64 字符 / 全角句号）抛的是
        # `UnicodeEncodeError` —— 它是 `UnicodeError ⊂ ValueError`，**不是** `OSError`；
        # 只写 `except OSError` 会让英文内部串穿到用户面前。
        raise UrlNotAllowed(f"无法解析该域名：{host}") from exc
    if not addresses:
        raise UrlNotAllowed(f"无法解析该域名：{host}")

    if allow_any_host:
        # 逃生门（默认关）：只解析、不判定；「放行了什么」由调用方留 WARNING
        return str(addresses[0])

    for address in addresses:
        effective = _effective_ip(address)
        if not _is_public(address) or not _is_public(effective):
            raise UrlNotAllowed(
                f"该地址不是公网地址（{effective}），已拒绝访问：{host}"
            )
    return str(addresses[0])


__all__ = [
    "UrlNotAllowed",
    "assert_url_is_safe",
    "resolve_host",
]
