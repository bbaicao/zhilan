"""出网守卫用例（专项「工具升级-2（R2）」· **阶段三**：`R2-09`…`R2-13`）。

## 为什么五条同住一个文件

清单 `R2-12` 卡点名本文件（``tests/api/test_web_ssrf_r2.py``）。五笔共用同一批假件
（桩解析器 / ``httpx.MockTransport`` / 假钟），分家会让「哪条用例属哪一笔」变成靠记忆判断的事。
本文件**不起服务、不碰 wire 面**（满足清单 §七 第 3 条的例外三条）。

## 三条纪律（开工包 D-回归检查 / D-回归检查 / D-回归检查）

1. **绝不真出网**：用 ``httpx.MockTransport`` + **真** ``httpx.AsyncClient``（经 ``monkeypatch``
   换掉 ``web_client.httpx.AsyncClient`` 的**工厂**，把 ``transport=`` 注进去）。
   既有 ``tests/agent/test_web_client.py`` 的 ``_FakeAsyncClient`` **不能**拿来做新用例：
   它**从不触发** ``event_hooks``（钩子类断言会退化成空断言），也没有
   ``headers.has_redirect_location`` / ``history`` / ``next_request`` / ``aread`` / ``aclose``，
   跑不了真重定向环。
2. **解析一律注入桩**：``ssrf.resolve_host`` 是**进程级全局** ⇒ 本文件自带 autouse 复位夹具，
   而且**只经 ``monkeypatch`` 改**（直接赋值会把桩漏给同进程后续用例，且没有任何旧判据能发现）。
3. **async 用例必须带 ``@pytest.mark.asyncio``**（本仓 ``pytest.ini`` 无 ``asyncio_mode`` ⇒ strict）。

## 判据归属（便于逐笔核对）

| 判据 | 条目 | 本节 |
|---|---|---|
| A21（件级） | `R2-09` | 本节 |
| A22（四条腿） | `R2-10` | 本节 |
| A23 | `R2-11` | 本节 |
| A24 / A26′ | `R2-12` | 本节 |
| A25 | `R2-13` | 本节 |
"""

from __future__ import annotations

import ast
import ipaddress
import logging
import socket
import threading
from pathlib import Path

import httpx
import pytest

from app.agent.web import client as web_client
from app.agent.web import pinning, ssrf
from app.core.config import settings

#: 文件导入时抓一份**真**解析器：本文件的存在前提就是它不被真调用（见 autouse 夹具）
_REAL_RESOLVER = ssrf.resolve_host


@pytest.fixture(autouse=True)
def _isolate_resolver(monkeypatch):
    """把模块级解析器钉回真实现：一条用例留下的桩不得漏给下一条（D-回归检查 / K37）。"""
    monkeypatch.setattr(ssrf, "resolve_host", _REAL_RESOLVER)


def _stub_resolver(*addresses: str, raises: Exception | None = None):
    """桩解析器：任何域名都解析成给定地址；``raises`` 时抛该异常（模拟 DNS 不可用）。"""

    def resolve(host: str) -> list[str]:  # noqa: ANN001
        if raises is not None:
            raise raises
        return list(addresses)

    return resolve


def _resolver_for_names(public: str):
    """桩解析器（**只改域名**）：域名 → ``public``；**字面地址原样返回**。

    ⚠️ 本文件第一版在这条腿上用的是「一律返回 ``public``」的桩 ⇒ 把重定向到的
    字面私网地址（``http://10.0.0.5/``）也**洗成了公网**，于是「第二跳必拒」那条腿
    变成了 21 跳循环（红在 ``TooManyRedirects`` 上）—— 桩必须照 ``getaddrinfo`` 的语义：
    字面地址解析成它自己。
    """

    def resolve(host: str) -> list[str]:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return [public]
        return [host]

    return resolve


# --------------------------------------------------------------------------
# A21（`R2-09`）· 白名单判定核心 —— **件级**（直调，不经 client.py）
# --------------------------------------------------------------------------


def test_a21_private_and_special_literal_addresses_are_refused():
    """字面地址腿：回环 / 私网 / 链路本地 / 云元数据 / 未指定地址一律拒。

    ⚠️ 这几条**不需要**桩解析器（``getaddrinfo`` 对字面 IP 不走 DNS），是守卫最外圈的回归腿。
    """
    for url in (
        "http://127.0.0.1:9527/x",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/",
        "http://[::1]/",
    ):
        with pytest.raises(ssrf.UrlNotAllowed) as excinfo:
            ssrf.assert_url_is_safe(url)
        assert "已拒绝访问" in str(excinfo.value), url


def test_a21_non_http_schemes_keep_the_legacy_wording():
    """回归腿：``file://`` / ``ftp://`` 在 ``fetch_page`` 入口就已被拒（守卫之前）。

    文案**沿用**既有串「不是合法的网页地址」——``fetch_page`` 用的就是这一句，
    既有用例 ``test_web_client.py`` 用 ``in`` 做**子串**断言钉着它。
    """
    for url in ("file:///etc/passwd", "ftp://example.com/a"):
        with pytest.raises(ssrf.UrlNotAllowed) as excinfo:
            ssrf.assert_url_is_safe(url)
        assert "不是合法的网页地址" in str(excinfo.value), url


def test_a21_url_with_embedded_credentials_is_refused():
    """内嵌凭据腿：``http://user:pw@host/`` 是「把口令写进日志/引用卡片」的现成通道。"""
    with pytest.raises(ssrf.UrlNotAllowed) as excinfo:
        ssrf.assert_url_is_safe("http://user:secret@93.184.216.34/")
    assert "内嵌用户名或口令" in str(excinfo.value)


def test_a21_private_domain_through_stub_resolver_is_refused(monkeypatch):
    """**真判别腿**：字面 IP 早就被拒；「域名解析到私网」只有真走解析才拦得住。"""
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("10.0.0.5"))
    with pytest.raises(ssrf.UrlNotAllowed) as excinfo:
        ssrf.assert_url_is_safe("http://internal.example/")
    assert "10.0.0.5" in str(excinfo.value)


def test_a21_mixed_public_and_private_answers_are_refused(monkeypatch):
    """混答腿：一条公网 + 一条私网 ⇒ **整条拒绝**（只取首条会留下轮询绕过窗口）。"""
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("93.184.216.34", "10.0.0.5"))
    with pytest.raises(ssrf.UrlNotAllowed):
        ssrf.assert_url_is_safe("http://mixed.example/")


def test_a21_unresolvable_domain_is_refused_as_readable_chinese(monkeypatch):
    """解析失败腿：DNS 不可用必须变成**可读中文拒绝**，不能漏一个 ``gaierror`` 出去。"""
    monkeypatch.setattr(
        ssrf, "resolve_host", _stub_resolver(raises=socket.gaierror("dns down"))
    )
    with pytest.raises(ssrf.UrlNotAllowed) as excinfo:
        ssrf.assert_url_is_safe("http://offline.example/")
    assert "无法解析该域名" in str(excinfo.value)


def test_a21_nat64_embedded_private_address_is_refused(monkeypatch):
    """**本笔唯一的真判别点**：``64:ff9b::/96`` 整体 ``is_global=True``（Python 3.13 实测）。

    内嵌的是 ``127.0.0.1`` —— 不解低 32 位就看不见它，NAT64 于是成了绕过守卫的通道。
    """
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("64:ff9b::7f00:1"))
    with pytest.raises(ssrf.UrlNotAllowed) as excinfo:
        ssrf.assert_url_is_safe("http://[64:ff9b::7f00:1]/")
    assert "127.0.0.1" in str(excinfo.value)


def test_a21_effective_ip_unwraps_only_embedded_ipv4():
    """``_effective_ip`` 的形态腿：有内嵌才解，没有就原样（判定的收紧只能来自这里）。"""
    assert ssrf._effective_ip("93.184.216.34") == "93.184.216.34"
    assert ssrf._effective_ip("2606:4700::1111") == "2606:4700::1111"
    assert ssrf._effective_ip("64:ff9b::7f00:1") == "127.0.0.1"
    assert ssrf._effective_ip("::ffff:8.8.8.8") == "8.8.8.8"
    assert ssrf._effective_ip("2002:7f00:1::1") == "127.0.0.1"
    # `回归修复`：IPv4-compatible（RFC 4291 已废）与 IPv4-translated（RFC 2765）同样有内嵌 IPv4
    assert ssrf._effective_ip("::127.0.0.1") == "127.0.0.1"
    assert ssrf._effective_ip("::ffff:0:127.0.0.1") == "127.0.0.1"
    # ⚠️ ``::`` 与 ``::1`` 也落在 ``::/96`` 里，但**不**当内嵌解（它们本身已被 ``is_global`` 拒；
    #    解成 0.0.0.0 / 0.0.0.1 只会让判据更难读）
    assert ssrf._effective_ip("::") == "::"
    assert ssrf._effective_ip("::1") == "::1"


def test_a21_ipv6_compat_translated_and_site_local_forms_are_refused(monkeypatch):
    """`回归修复`（对抗审计实测暴露）：``is_global`` **自己放过**的三种 IPv6 形态。

    实测（Python 3.13.14）：``is_global("::127.0.0.1") is True``、``is_global("::ffff:0:127.0.0.1") is True``、
    ``is_global("fec0::1") is True`` —— 只靠 ``is_global`` 的白名单会把回环 / 云元数据 / site-local 放出去。
    """
    for address in (
        "::127.0.0.1",
        "::10.0.0.5",
        "::169.254.169.254",
        "::ffff:0:127.0.0.1",
        "fec0::1",
        "ff02::1",
    ):
        monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver(address))
        with pytest.raises(ssrf.UrlNotAllowed) as excinfo:
            ssrf.assert_url_is_safe(f"http://[{address}]/")
        assert "已拒绝访问" in str(excinfo.value), address

    # 反控（同用例成对）：同一族的**公网内嵌**形态照旧放行 —— 只封私网形态，不封整段
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("::8.8.8.8"))
    assert ssrf.assert_url_is_safe("http://[::8.8.8.8]/") == "::8.8.8.8"
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("::ffff:0:8.8.8.8"))
    assert ssrf.assert_url_is_safe("http://[::ffff:0:8.8.8.8]/") == "::ffff:0:8.8.8.8"


def test_a21_ipv4_multicast_and_reserved_leaks_are_refused(monkeypatch):
    """`回归修复`：IPv4 侧同样有 ``is_global`` 放过的段（组播 / 6to4 anycast / IETF 保留）。"""
    for address in ("224.0.0.1", "239.255.255.250", "233.252.0.1", "192.88.99.1", "192.0.0.9"):
        monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver(address))
        with pytest.raises(ssrf.UrlNotAllowed):
            ssrf.assert_url_is_safe(f"http://{address}/")

    # 反控：正常公网地址照旧放行
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("8.8.8.8"))
    assert ssrf.assert_url_is_safe("http://8.8.8.8/") == "8.8.8.8"


def test_a21_malformed_hosts_are_refused_as_readable_chinese():
    """`回归修复`：解析期异常（``UnicodeEncodeError`` ⊂ ``ValueError``，**不是** ``OSError``）。

    ``getaddrinfo`` 对畸形 label（空 label、超 64 字符 label、全角句号）抛 ``UnicodeEncodeError``；
    只写 ``except OSError`` 接不住 ⇒ 异常穿出守卫，用户看到的是
    ``'idna' codec can't encode character …`` 这种英文内部串，而 ``client.py`` 自述口径是
    「不夹英文异常名与堆栈」。（fail-closed，不是绕过 —— 但把可读拒绝面打穿了。）
    """
    for url in ("http://a..b/", "http://./", "http://。 /", "http://" + "a" * 64 + ".example/"):
        with pytest.raises(ssrf.UrlNotAllowed) as excinfo:
            ssrf.assert_url_is_safe(url)
        assert "无法解析该域名" in str(excinfo.value), url


@pytest.mark.asyncio
async def test_a21_malformed_url_becomes_a_readable_tool_error(monkeypatch):
    """`回归修复` 的**接线腿**：``httpx.InvalidURL`` 不是 ``httpx.HTTPError``（MRO = ``InvalidURL→Exception``）。

    ``fetch_page`` 只接 ``TimeoutException`` / ``HTTPError`` ⇒ 它一路穿到工具层，
    用户看到 ``Invalid IPv4 address: '0177.0.0.1'``。
    """
    _no_proxy(monkeypatch)
    # 两条腿的**原因不同**，可读文案也就不同：httpx 在建 Request 时就看不懂 `0177.0.0.1`；
    # `a..b` 能建出 Request，是**解析期**失败（守卫给「无法解析该域名」）。
    for url, expected in (
        ("http://0177.0.0.1/", "不是合法的网页地址"),
        ("http://a..b/", "无法解析该域名"),
    ):
        with pytest.raises(web_client.WebToolError) as excinfo:
            await web_client.fetch_page(url)
        assert expected in str(excinfo.value), url
        # 关键词：**不能**是英文内部串
        assert "codec can't encode" not in str(excinfo.value)
        assert "Invalid IPv4" not in str(excinfo.value)


def test_a21_public_controls_pass_and_return_the_resolved_address(monkeypatch):
    """**正控（与上面各拒绝腿成对）**：没有它，「永远拒绝」的实现也能全绿。"""
    monkeypatch.setattr(ssrf, "resolve_host", _resolver_for_names("93.184.216.34"))
    assert ssrf.assert_url_is_safe("http://example.test/") == "93.184.216.34"

    # NAT64 的**公网**内嵌形态必须放行（只写了「内嵌私网要拒」而把 NAT64 整段封死就过不了这条）
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("64:ff9b::808:808"))
    assert ssrf.assert_url_is_safe("http://[64:ff9b::808:808]/") == "64:ff9b::808:808"

    # 字面公网 IP（走真解析器：``getaddrinfo`` 对字面地址不查 DNS，断网机器同样成立）
    monkeypatch.setattr(ssrf, "resolve_host", _REAL_RESOLVER)
    assert ssrf.assert_url_is_safe("http://93.184.216.34/") == "93.184.216.34"


# --------------------------------------------------------------------------
# A22（`R2-10`）· DNS pinning —— 四条腿（开工包 D-回归检查 的 (a)(b)(c)(d)）
# --------------------------------------------------------------------------


class _SentinelError(Exception):
    """哨兵：内层后端**只记录、不真连**（记录完就抛它）。"""


class _RecordingBackend:
    """``httpcore.AsyncNetworkBackend`` 的哑替身：记录 ``connect_tcp`` 收到的 host 后抛哨兵。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None]] = []

    async def connect_tcp(  # noqa: PLR0913 —— 照 httpcore 的签名
        self,
        host: str,
        port: int | None = None,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: object = None,
    ):
        self.calls.append((host, port))
        raise _SentinelError("不真连")

    async def connect_unix_socket(self, path: str, timeout=None, socket_options=None):  # noqa: ANN001
        raise _SentinelError("不真连")

    async def sleep(self, seconds: float) -> None:
        return None


def _no_proxy(monkeypatch) -> None:  # noqa: ANN001
    """隔离环境与系统代理——(b)(c) 必须走无代理路径，显式代理反控不调用本函数。"""
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(key, raising=False)
    # httpx 已 from urllib.request import getproxies；清环境变量仍会回退到 WinINET。
    monkeypatch.setattr(httpx._utils, "getproxies", lambda: {})


@pytest.mark.asyncio
async def test_a22_leg_a_backend_rewrites_the_connecting_host(monkeypatch):
    """**(a) 件级**：``connect_tcp`` 拿去连的 host = pins 里那个地址；不在 pins 的走解析。"""
    inner = _RecordingBackend()
    backend = pinning.PinnedBackend(inner=inner, pins={"stub.example": "93.184.216.34"})
    with pytest.raises(_SentinelError):
        await backend.connect_tcp("stub.example", 443)
    assert inner.calls == [("93.184.216.34", 443)]

    # 同用例内反控：pins 外的 host **不被改写**（走解析 ⇒ 取到桩给的地址，而不是域名本身）
    # ⚠️ 桩地址必须是**真公网**：``198.51.100.0/24``（TEST-NET-2）实测 ``is_global=False``，
    #    拿它当反控会先被守卫拒掉（本用例第一版就踩了这一脚）。
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("8.8.4.4"))
    with pytest.raises(_SentinelError):
        await backend.connect_tcp("other.example", 80)
    assert inner.calls[1] == ("8.8.4.4", 80)


@pytest.mark.asyncio
async def test_a22_leg_a2_first_answer_is_reused_for_the_same_host(monkeypatch):
    """**钉住的语义**：同一个 host 只解析一次，之后一律用第一次那个地址。"""
    seen: list[str] = []

    def resolver(host: str) -> list[str]:
        seen.append(host)
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf, "resolve_host", resolver)
    backend = pinning.PinnedBackend(inner=_RecordingBackend())
    for _ in range(3):
        with pytest.raises(_SentinelError):
            await backend.connect_tcp("twice.example", 443)
    assert seen == ["twice.example"]
    assert backend.pins == {"twice.example": "93.184.216.34"}


def test_a22_injection_is_null_safe_for_transportless_doubles():
    """**K33（A26 的命门）**：既有假件没有 ``_transport`` ⇒ 注入必须 ``getattr`` 安全取用、绝不抛。"""
    web_client._maybe_pin(object())  # 不抛即通过（拿不到接缝就静默跳过）


@pytest.mark.asyncio
async def test_a22_leg_b_make_client_installs_the_pinned_backend(monkeypatch):
    """**(b) 接线级**：走**真构造路径**拿到真 client ⇒ 池里的后端就是 ``PinnedBackend``。

    ⚠️ 构造是惰性的 ⇒ 零出网（不需要发任何请求）；⚠️ 必须清代理，否则走的是 (d) 的降级腿。
    """
    _no_proxy(monkeypatch)
    client = web_client._make_client(timeout=1.0)
    try:
        assert isinstance(client._transport._pool._network_backend, pinning.PinnedBackend)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_a22_leg_c_end_to_end_connecting_host_is_the_resolved_one(monkeypatch):
    """**(c) 端到端腿**：判定用的 IP = 连接用的 IP（真 client + 桩解析器 + 记录后端）。"""
    _no_proxy(monkeypatch)
    monkeypatch.setattr(ssrf, "resolve_host", _resolver_for_names("93.184.216.34"))
    recorded = _RecordingBackend()
    monkeypatch.setattr(
        pinning,
        "make_backend",
        lambda inner, pins=None: pinning.PinnedBackend(recorded, pins),
    )
    try:
        await web_client.fetch_page("http://stub.example/")
    except BaseException:  # noqa: BLE001 —— 哨兵（或 httpcore 把它包装成的连接错误）
        pass
    else:
        raise AssertionError("哨兵没抛：说明这条腿真出网了")
    assert recorded.calls, "内层后端一次都没被调到 ⇒ 这条腿没走到连接接缝"
    assert recorded.calls[0][0] == "93.184.216.34"


@pytest.mark.asyncio
async def test_a22_leg_d_environment_proxy_degrades_to_no_pinning_with_a_warning(
    monkeypatch, caplog
):
    """**(d) 代理降级腿**：代理在场 ⇒ 不 pin + warning（**fail-open，如实登记**）。

    ⚠️ 判定必须看 ``client._mounts``：实测代理挂在那里，``_transport._pool`` 仍是
    ``AsyncConnectionPool`` ⇒ 只看 ``_pool`` 会在真代理下**静默失效、连 warning 都没有**。
    """
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8888")
    with caplog.at_level(logging.WARNING, logger="app.agent.web.client"):
        client = web_client._make_client(timeout=1.0)
    try:
        assert client._mounts, "代理没挂上 ⇒ 这条腿根本没测到降级路径"
        assert not isinstance(client._transport._pool._network_backend, pinning.PinnedBackend)
        records = [r for r in caplog.records if r.name == "app.agent.web.client"]
        assert any("代理" in r.getMessage() for r in records)
    finally:
        await client.aclose()


# --------------------------------------------------------------------------
# A23（`R2-11`）· 重定向每跳重验 + A21 的**接线腿**（跟随重定向的那 2 处）
# --------------------------------------------------------------------------


def _mock_transport_factory(handler):
    """把 ``web_client.httpx.AsyncClient`` 换成「**真** AsyncClient + MockTransport」工厂。

    ⚠️ 必须把调用方传进来的 ``opts``（``event_hooks`` / ``max_redirects`` /
    ``follow_redirects``）**原样透传** —— 工厂吞掉 opts 就等于没装钩子，那条腿是假绿。
    """
    real = httpx.AsyncClient

    def factory(**opts):
        opts["transport"] = httpx.MockTransport(handler)
        return real(**opts)

    return factory


@pytest.mark.asyncio
async def test_a23_second_hop_to_a_private_address_is_refused(monkeypatch):
    """**每跳重验**：第一跳公网、第二跳私网 ⇒ 第二跳发不出去。

    ⚠️ 驱动 ``fetch_page``（全文件只有 ``_search_duckduckgo`` 与它跟随重定向 ⇒
    在其余 5 处上**写不出第二跳**）。
    """
    _no_proxy(monkeypatch)
    monkeypatch.setattr(ssrf, "resolve_host", _resolver_for_names("93.184.216.34"))
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://10.0.0.5/secret"})

    monkeypatch.setattr(web_client.httpx, "AsyncClient", _mock_transport_factory(handler))
    with pytest.raises(web_client.WebToolError) as excinfo:
        await web_client.fetch_page("http://93.184.216.34/a")
    assert "10.0.0.5" in str(excinfo.value)
    # 「第二跳真的没发出去」——否则断言只证明了「最后抛了错」
    assert hops == ["http://93.184.216.34/a"]


@pytest.mark.asyncio
async def test_a23_redirect_loop_beyond_the_cap_is_refused_without_breaking_through(monkeypatch):
    """逾限腿：**21 跳**假件 ⇒ 拒绝且**不炸穿**（``TooManyRedirects ⊂ httpx.HTTPError`` ⇒ 被既有 except 接住）。"""
    _no_proxy(monkeypatch)
    monkeypatch.setattr(ssrf, "resolve_host", _resolver_for_names("93.184.216.34"))
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        return httpx.Response(
            302, headers={"Location": f"http://93.184.216.34/hop{len(hops)}"}
        )

    monkeypatch.setattr(web_client.httpx, "AsyncClient", _mock_transport_factory(handler))
    with pytest.raises(web_client.WebToolError) as excinfo:
        await web_client.fetch_page("http://93.184.216.34/start")
    assert "抓取失败" in str(excinfo.value)  # 可读中文，不是英文异常名
    assert len(hops) == 21  # 判定是 len(history) > max_redirects ⇒ 20 允许 21 次请求后拒


@pytest.mark.asyncio
async def test_a21_wiring_leg_duckduckgo_client_is_guarded(monkeypatch):
    """**A21 接线腿（第 1 处）**：``_search_duckduckgo`` 那个 client 也装了钩子。

    ⚠️ 它的请求 URL 是**域名**（``html.duckduckgo.com``）⇒ 必须注入桩解析器，
    否则这条用例会在断网机器上红（也会真查一次 DNS）。
    """
    _no_proxy(monkeypatch)
    monkeypatch.setattr(ssrf, "resolve_host", _resolver_for_names("93.184.216.34"))
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1:9527/evil"})

    monkeypatch.setattr(web_client.httpx, "AsyncClient", _mock_transport_factory(handler))
    with pytest.raises(web_client.WebToolError) as excinfo:
        await web_client._search_duckduckgo("hello", timeout=5.0)
    assert "127.0.0.1" in str(excinfo.value)
    assert len(hops) == 1


# --------------------------------------------------------------------------
# A24 + A26′（`R2-12`）· 逃生门 + 7/7 接线
# --------------------------------------------------------------------------


def test_a24_logger_channel_is_the_module_logger():
    """J7：逃生门 / 降级两类记录都走**本模块**的 logger（判据一律按名字过滤）。"""
    assert web_client.logger.name == "app.agent.web.client"


def test_a24_escape_hatch_default_is_off_at_the_field_level():
    """A24 第①腿的**字段侧**：出厂默认就是 ``False``（不是「本机 ``.env`` 恰好没写」）。"""
    assert type(settings).model_fields["WEB_SSRF_ALLOW_ANY_HOST"].default is False


@pytest.mark.asyncio
async def test_a24_hatch_off_refuses_and_hatch_on_allows_with_a_warning(monkeypatch, caplog):
    """A24 的**成对判据**（D-回归检查）：① 默认关 ⇒ 私网仍拒且该 logger 无记录；② 显式开 ⇒ 放行 + 含 host 的 WARNING。

    ⚠️ 过滤必须按 logger 名（``caplog.records`` 是**全 logger** 收集，httpx/httpcore/asyncio 的
    WARNING 会造成假红）。
    """
    _no_proxy(monkeypatch)
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("10.0.0.5"))
    body = "<html><head><title>内网页</title></head><body>" + "正文" * 40 + "</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    monkeypatch.setattr(web_client.httpx, "AsyncClient", _mock_transport_factory(handler))

    # ① 默认关（显式钉 False，等价于出厂默认）
    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", False)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.agent.web.client"):
        with pytest.raises(web_client.WebToolError) as excinfo:
            await web_client.fetch_page("http://10.0.0.5/secret")
    assert "10.0.0.5" in str(excinfo.value)
    assert [r for r in caplog.records if r.name == "app.agent.web.client"] == []

    # ② 显式开 ⇒ 放行 **且** 每次放行留一条含 host 的 WARNING
    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", True)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.agent.web.client"):
        title, text = await web_client.fetch_page("http://10.0.0.5/secret")
    assert title == "内网页" and text.startswith("正文")
    records = [r for r in caplog.records if r.name == "app.agent.web.client"]
    assert any("10.0.0.5" in r.getMessage() for r in records)


@pytest.mark.asyncio
async def test_a24_hatch_open_is_read_at_call_time_not_frozen_at_import(monkeypatch):
    """逃生门必须**每次调用时现读** ``settings``：import 期固化成模块常量的话这条腿会红。"""
    _no_proxy(monkeypatch)
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("10.0.0.5"))
    monkeypatch.setattr(
        web_client.httpx,
        "AsyncClient",
        _mock_transport_factory(
            lambda request: httpx.Response(
                200,
                text="<html><title>T</title><body>" + "字" * 60 + "</body></html>",
                headers={"content-type": "text/html"},
            )
        ),
    )
    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", False)
    with pytest.raises(web_client.WebToolError):
        await web_client.fetch_page("http://10.0.0.5/a")
    # 同一个进程、同一个模块，只把 settings 改掉 ⇒ 立刻放行（证明读的是**调用时**的值）
    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", True)
    title, _text = await web_client.fetch_page("http://10.0.0.5/a")
    assert title == "T"


# --------------------------------------------------------------------------
# A27（缺陷清单-2 · 回归修复）· 守卫钩子的 DNS 解析离开事件循环
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a27_guard_hook_resolves_off_the_event_loop(monkeypatch):
    """**回归修复**：``_guard_hook`` 里的白名单判定（→ ``resolve_host`` →
    ``socket.getaddrinfo``）必须在线程里跑 —— 钩子是**每请求每跳**都走的高频路径，
    在事件循环上直接调阻塞 DNS，慢 DNS（DNS 宕 / captive portal / Windows 重试 ~12s）
    时整个后端停摆。修法照 ``pinning.py:36-40`` 自立的纪律：解析一律丢线程。

    判别法：桩解析器记录**执行线程**；``fetch_page`` 跑在测试协程的事件循环上，
    桩的执行线程必须**全部**异于循环线程。回退（钩子内同步直调）⇒ 桩回到循环线程 ⇒ 红。
    """
    _no_proxy(monkeypatch)
    loop_tid = threading.get_ident()
    tids: list[int] = []

    def resolver(host: str) -> list[str]:  # noqa: ANN001
        tids.append(threading.get_ident())
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf, "resolve_host", resolver)
    monkeypatch.setattr(
        web_client.httpx,
        "AsyncClient",
        _mock_transport_factory(
            lambda request: httpx.Response(
                200,
                text="<html><title>T</title><body>" + "字" * 60 + "</body></html>",
                headers={"content-type": "text/html"},
            )
        ),
    )
    title, _text = await web_client.fetch_page("http://stub.example/")
    assert title == "T"
    assert tids, "桩解析器没被调到 ⇒ 这条腿没走到守卫钩子，判据无效"
    assert all(t != loop_tid for t in tids), (
        "解析器跑在事件循环线程上 ⇒ 守卫钩子仍在循环上做阻塞 DNS（回归修复 未修）"
    )


def test_a26_prime_one_guarded_construction_and_seven_call_sites():
    """**A26′（接线腿）**：``client.py`` 的 AST 判据。

    它挡的正是「只给 ``fetch_page`` 装钩子、6 处搜索不装」这种**全绿假象** ——
    m6 变异把 7 处中的一处换回直调，这条断言当场红。
    """
    tree = ast.parse(Path(web_client.__file__).read_text(encoding="utf-8"))
    direct = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "AsyncClient"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "httpx"
    ]
    assert len(direct) == 1, "客户端直构点必须收敛到 1 处（统一构造器）"
    keywords = {kw.arg for kw in direct[0].keywords}
    assert {"event_hooks", "max_redirects"} <= keywords, "唯一的直构点必须带守卫与重定向上限"

    made = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_make_client"
    ]
    assert len(made) == 7, "7 处出网 client 必须全部经 _make_client（7/7 接线）"


@pytest.mark.asyncio
async def test_a22_resolution_failure_raises_url_not_allowed_not_oserror(monkeypatch):
    """**J5（对抗审计建议）**：解析失败在**连接后端**这一层也只能是 ``UrlNotAllowed``（不许漏 ``OSError``）。"""
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver(raises=socket.gaierror("dns down")))
    backend = pinning.PinnedBackend(inner=_RecordingBackend())
    with pytest.raises(ssrf.UrlNotAllowed):
        await backend.connect_tcp("offline.example", 443)


# --------------------------------------------------------------------------
# A25（`R2-13`）· 抓取预算（**假钟**，不真等）
# --------------------------------------------------------------------------


def _ticking_clock(values: list[float]):
    """假钟：按给定序列出值（**首次 = 搜索起点**，之后每次 = 逐条抓取前的检查）。

    ⚠️ 序列取尽再取 ⇒ ``StopIteration``（用例失败）：这本身也是一条判据 —— ``_now()`` 的
    **调用次数**被钉住了（多调一次就红）。
    """
    ticks = iter(values)

    def now() -> float:
        return next(ticks)

    return now


def _nine_hits():
    from app.agent.web.client import WebHit

    return [
        WebHit(title=f"标题{i}", url=f"http://site{i}.example/", snippet=f"摘要{i}")
        for i in (1, 2, 3)
    ]


@pytest.mark.asyncio
async def test_a25_over_budget_skips_bodies_but_keeps_every_hit(monkeypatch):
    """A25 主腿：预算用尽 ⇒ **不抓正文但仍进材料**（三条例口：不 ``continue`` / 不动 ``failed`` / ``ok=True``）。"""
    from app.agent.tools import web_search as web_search_mod

    monkeypatch.setattr(settings, "WEB_SEARCH_BUDGET_SECONDS", 20.0)
    monkeypatch.setattr(web_search_mod, "_now", _ticking_clock([0.0, 30.0, 30.0, 30.0]))
    grabbed: list[str] = []

    async def fake_search(query: str):
        return _nine_hits()

    async def fake_fetch(url: str):
        grabbed.append(url)
        return ("网页标题", "正文" * 50)

    obs = await web_search_mod.build_spec(search=fake_search, fetch=fake_fetch).handler(
        web_search_mod.WebSearchArgs(query="q")
    )

    assert obs.ok is True  # ③ ok=True 不变
    assert grabbed == []  # 预算已用尽 ⇒ 一条都不抓
    assert len(obs.materials) == 3  # ① 不得 continue 掉该条（3 条命中不能变 2 条）
    assert len(obs.warnings) == 3  # ② 不动 failed ⇒ 不会多出「网页都抓取失败」那条总账
    for index in (1, 2, 3):
        assert f"网页抓取超出预算：http://site{index}.example/" in obs.warnings
        assert f"[{index}]" in obs.text  # 三条都还在观察文本里
    assert obs.materials[0].content.strip() == "摘要1"  # 正文为空 ⇒ 天然退回摘要

    # **反控（同用例内）**：预算充足 ⇒ 3 条全抓、warnings 空（否则「永远跳过抓取」也能绿）
    monkeypatch.setattr(web_search_mod, "_now", _ticking_clock([0.0, 0.0, 0.0, 0.0]))
    grabbed.clear()
    ok_obs = await web_search_mod.build_spec(search=fake_search, fetch=fake_fetch).handler(
        web_search_mod.WebSearchArgs(query="q")
    )
    assert len(grabbed) == 3
    assert ok_obs.warnings == []
    assert all("正文" in material.content for material in ok_obs.materials)


@pytest.mark.asyncio
async def test_a25_budget_zero_means_unlimited(monkeypatch):
    """``0`` = **不限**（回滚口径）：假钟给「无穷远」的时间也必须全抓 —— 这条腿不经过剩余预算比较。"""
    from app.agent.tools import web_search as web_search_mod

    monkeypatch.setattr(settings, "WEB_SEARCH_BUDGET_SECONDS", 0.0)
    monkeypatch.setattr(web_search_mod, "_now", _ticking_clock([0.0, 9e9, 9e9, 9e9]))
    grabbed: list[str] = []

    async def fake_search(query: str):
        return _nine_hits()

    async def fake_fetch(url: str):
        grabbed.append(url)
        return ("t", "正文" * 50)

    obs = await web_search_mod.build_spec(search=fake_search, fetch=fake_fetch).handler(
        web_search_mod.WebSearchArgs(query="q")
    )
    assert len(grabbed) == 3
    assert obs.warnings == []
    assert len(obs.materials) == 3
