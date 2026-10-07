# -*- coding: utf-8 -*-
"""`回归修复` 判据载体：**跨端点「拒绝面零出网」对拍 + 逃生门两侧一致**（卡 `task-10`）。

## 本件与三件同族载体的**分工**（不重复判）

`081-C`（`test_net_guard_mcp_probe_bug081.py`）与 `081-D`（`test_net_guard_settings_bug081.py`）
各自只判**自己那条接线**；本件判的是**两侧之间**的东西 —— 这正是本卡独立成卡的理由：

| 本件判什么 | 为什么**只有**本件能判 |
|---|---|
| **E-01** 同一输入、两个端点、同一语义 | 两件各自都只喂自己的端点 ⇒ 「这一份输入在两个端点上的结论是否一致」无人判 |
| **E-02** 逃生门两侧一致 | 同上：单侧腿只能证「我这条接了开关」，证不了「另一条也接了」 |
| **E-03** 与 web 面**三处**结论一致 | 三个面（两个端点 + `ssrf` 本体）从未被放进**同一次**对拍 |
| **E-04** 分母自证（路由表对拍） | 防「手写一份端点清单」——清单必须**现从路由表取** |
| **E-05** 拒绝面**不 500 / 不抛穿** | 逐条异常面：产品把「用户填了坏地址」当**输入问题**，不是服务端故障 |

## 🔴 E-01 的口径（**两侧不同形**，别写成同形）

两个端点的**拒绝通道本来就不同**，本卡要求的是「**同一语义**」，不是「同一形状」：

- `POST /api/models`（+`/api/web/llm/models`）：拒绝 ⇒ **HTTP 400** + 中文 `detail`
  （用户填错地址 = 输入问题 ⇒ 400）；
- `POST /api/skills/mcp/probe`：拒绝 ⇒ **HTTP 200** + `{ok: false, error: 中文}`
  （设置页的「探测」按钮：连不上/被拒是**探测结果**，不是请求错误 —— `skill_routes.py` 逐字）。

⇒ 本件把「同一语义」落成**三条可判的对应关系**（而不是断两侧同码）：
① 两侧都必须**明确失败**（一侧 4xx、一侧 `ok=false`）；② 两侧的中文文案都必须
**指得出是哪件事被拒**（`拉取模型列表` / `MCP 服务器探测`）；③ 两侧都必须**零出网**。

## 🔴 E-02 为什么必须按**各自口径**判 WARNING 条数（不得写同数）

Lead 转述的实测事实：三处 WARNING 模板串**逐字相同**，但——

- **连接侧**（`pinning.py`，web 面）：**每 host 一次**；
- **client 侧**（`net_guard.assert_outbound_url_safe`）：**每请求一次**。

本件因此**只判「两侧都 ≥ 1 条同模板 WARNING」**，**不**去对拍「条数相等」——
那会把一个**已登记的口径差**误判成缺陷（假红）。⚠️ 这是**有意**的松紧取舍，
写在件头以免后人「顺手补一条等数断言」。

## 🔴 E-03 的立论：三处**必须**一致，因为判定本体只有一份

`app/core/net_guard.py` 的薄壳**不重写**判定：它模块级把
`ssrf.assert_url_is_safe` 绑成自己的 `assert_url_is_safe`（本件 E-03 用
`is` 身份腿把这条钉死）。⇒ 同输入下三处结论一致是**结构性**的，不是巧合。
⚠️ **但两者仍可能分叉**：`ssrf.assert_url_is_safe` 的 `allow_any_host` 是**形参**
（默认 `None` ⇒ 关），而薄壳**自己现读** `settings.WEB_SSRF_ALLOW_ANY_HOST`
（`net_guard.py` 逐字：「逃生门必须在调用点显式传」）。⇒ 三处对拍时**必须显式对齐
逃生门档位**，否则会把「开关没传」误读成「判定分叉」。本件在两档下各对拍一次。

## 零真出网（本件即使被误放在有网机器上也一个字节都不发）

1. **resolver 一律注桩**（`ssrf.resolve_host` 是**进程级全局** ⇒ 自带 autouse 复位夹具，
   只经 `monkeypatch` 改，照 `test_web_ssrf_r2.py:51` 先例）；
2. **httpx 工厂桩**：两处出网模块（`settings_routes` / `mcp_probe`）的 `httpx.AsyncClient`
   都换成**计数工厂** ⇒ 「真到了出网」与「拒绝路径零构造」读同一个数（判别力对偶）；
   ⚠️ 两处**分别**装：`mcp_probe` 是 `import httpx` 后**直接**用模块属性
   （`mcp_probe.httpx.AsyncClient`），`settings_routes` 同形 ⇒ 两处都打模块属性。
3. **`.env` 指到 `tmp_path`**：`GET /api/models` 读角色配置，不隔离会真打上游。

## 纪律

- 断言**一律无条件**（禁条件式断言）；
- 逃生门用 `monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", …)`（自动还原）；
- 本件**不改任何产品代码**（本卡是判据件，`write_scopes` 只有一个**新建**测试文件）。
"""

from __future__ import annotations

import ipaddress
import json
import logging

import httpx
import pytest

from app.agent.web import ssrf
from app.api import mcp_probe, settings_routes, skill_routes
from app.core import env_store, net_guard
from app.core.config import settings

from tests.api._sync_client import SyncClient, build_app

#: 文件导入时抓一份**真**解析器（本件的前提是它**永不**被真调用）。
_REAL_RESOLVER = ssrf.resolve_host

#: 逃生门 WARNING 的**同一句模板**（三处逐字相同；本件只按前缀判，不复制整串）。
#: ⚠️ 判「同一个模板」而不是判「条数相等」—— 两侧口径本就不同（见件头 E-02）。
_HATCH_MESSAGE_PREFIX = "SSRF 逃生门已开启（WEB_SSRF_ALLOW_ANY_HOST）"

#: 两侧各自的 logger 名（caplog 是全 logger 收集 ⇒ 必须按名过滤，否则收到网络栈的 WARNING）。
_LOGGER_NET_GUARD = "app.core.net_guard"

#: 本件用的危险 URL 三形态（与 081-C/081-D 同款，**同一批输入**才能对拍）。
METADATA_URL = "http://169.254.169.254/latest/meta-data/"
LOOPBACK_URL = "http://127.0.0.1:9527/mcp"
FILE_URL = "file:///c:/windows/win.ini"
BAD_URLS = [METADATA_URL, LOOPBACK_URL, FILE_URL]

#: 两侧都必须说得出的「为了什么」（settings 的 _EGRESS_PURPOSE / mcp 的 purpose 字面值）。
_PURPOSE_SETTINGS = "拉取模型列表"
_PURPOSE_MCP = "MCP 服务器探测"

SVC_CHAT = "svc_chat"
CHAT_KEY = "sk-chat-secret0001"
BASE_URL = "https://example.invalid/v1"


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _stub_resolver(*addresses: str):
    """桩解析器：**域名**解析成给定地址；**字面地址原样返回**。

    「字面地址原样返回」是**必须**照 getaddrinfo 语义来（081-C 实测踩过）：
    一律返回给定地址会把 http://169.254.169.254/… 这个**字面内网地址**洗成公网
    ⇒ 守卫**正确地**放行 ⇒ 对拍腿红在自己造的假件上。
    """

    def resolve(host: str) -> list[str]:  # noqa: ANN001
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return list(addresses)
        return [host]

    return resolve


@pytest.fixture(autouse=True)
def _isolate_resolver(monkeypatch):
    """把模块级解析器钉回真实现：一条用例留下的桩不得漏给下一条。"""
    monkeypatch.setattr(ssrf, "resolve_host", _REAL_RESOLVER)


@pytest.fixture(autouse=True)
def _hatch_off(monkeypatch):
    """逃生门**默认关**（出厂档）：逐条用例显式钉一遍，隔离本机 .env。"""
    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", False)


@pytest.fixture
def fake_dotenv(tmp_path, monkeypatch):
    """.env 指到 tmp_path：GET /api/models 读角色配置，不隔离会真打上游。"""
    path = tmp_path / ".env"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(env_store, "ENV_PATH", path)
    assert env_store.ENV_PATH != env_store.ROOT_DIR / ".env", "假 .env 落到了仓库真实路径上"
    return path


@pytest.fixture
def wired(monkeypatch, fake_dotenv):
    """注册表 + 角色绑定（存档态）；WEB_LLM_* 清空，免得本机值混进断言。"""
    monkeypatch.setattr(
        settings,
        "MODEL_SERVICES",
        _dump(
            [
                {
                    "id": SVC_CHAT,
                    "name": "对话服务",
                    "provider": "dashscope",
                    "base_url": BASE_URL,
                    "api_key": CHAT_KEY,
                    "models": ["m-chat"],
                    "model_roles": {"m-chat": ["chat"]},
                    "for_roles": ["chat"],
                }
            ]
        ),
    )
    monkeypatch.setattr(
        settings, "MODEL_ROLES", _dump({"chat": {"service_id": SVC_CHAT, "model": "m-chat"}})
    )
    monkeypatch.setattr(settings, "WEB_LLM_BASE_URL", "")
    monkeypatch.setattr(settings, "WEB_LLM_API_KEY", "")
    return settings

class _EgressCounter:
    """两处出网路共用的**计数桩状态**：只记「被构造了几次」，绝不真出网。

    E-01/E-02 读它「> 0」证明放行后**真走到了出网那一步**（否则「拒得对」可能是
    因为压根没接线 ⇒ 判据恒真）；零出网腿读它「== 0」证明拒绝路径**根本没过出网点**。
    """

    def __init__(self) -> None:
        self.constructed = 0
        self.get_calls = 0
        self.post_calls = 0


#: 桩被调到时抛出的**可辨识**报错前缀（两条测试腿按它判「真走到了出网」）。
#: ⚠️ 抛的必须是产品**认得的**异常类型（`httpx.ConnectError`）而**不是** `AssertionError`：
#: 两个端点对异常的处置**不同** —— `settings_routes._fetch_models` 只捕获
#: `TimeoutException` / `HTTPError`，抛别的会**穿出成 500**（本件实测：直接抛
#: `AssertionError` 会让模型侧炸穿）；而 `mcp_probe.probe_server` 收所有 `Exception`。
#: ⇒ 统一抛 `ConnectError`：两侧都走**既有**分支（503 / error 字符串），
#: 且报错文案里带着本前缀 ⇒ 两侧都能自证「真到了出网那一步」。
_NEVER_EGRESS = "判据不许真出网"


def _fake_client_factory(counter: "_EgressCounter", label: str, real_httpx):
    """造一个**计数客户端类**：被构造即 +1；`get`/`post` 一旦被调 ⇒ 抛可辨识的 `ConnectError`。

    ⚠️ 判据侧**绝不能**真出网，故这里不发请求、也不返回可用响应；抛出的
    `ConnectError` 让被测代码走**既有**的错误分支（不是判据造的假分支）。
    """

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            counter.constructed += 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:  # noqa: ANN002
            return False

        async def get(self, url, **kwargs):  # noqa: ANN001, ANN003
            counter.get_calls += 1
            raise real_httpx.ConnectError(f"{_NEVER_EGRESS}（{label}.get）：{url}")

        async def post(self, url, **kwargs):  # noqa: ANN001, ANN003
            counter.post_calls += 1
            raise real_httpx.ConnectError(f"{_NEVER_EGRESS}（{label}.post）：{url}")

    return _FakeClient


class _HttpxShim:
    """透明代理：**只**把 `AsyncClient` 换成桩，其余模块级异常原样转发。

    ⚠️ 少转一个（如 `ConnectError`），被测函数的 `except httpx.XxxError` 就会炸成
    `AttributeError` —— 那是**桩的**故障，不是产品的（`081-D` 实测抓过）。
    ⚠️ 必须是**实例**：`_Shim.TimeoutException` 走**类**属性查找，类里的 `__getattr__` 管不到它。
    """

    def __init__(self, real, factory) -> None:  # noqa: ANN001
        self._real = real
        self.AsyncClient = factory

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self._real, name)


@pytest.fixture
def egress(monkeypatch):
    """把**两处**出网路的 `AsyncClient` 都换成计数桩（本件的「零真出网」地基）。

    🔴 **两处的接入方式【不同】—— 本件实测得到，不是笔误**：

    - `settings_routes`：**模块级** `import httpx` ⇒ 换**模块属性**
      `settings_routes.httpx`（`081-D` 的同款打法）；
    - `mcp_probe`：`httpx` **在函数体内** import（**故意**的 —— `list_tools_http` 的
      docstring 逐字：「守卫拒绝时压根不 import、不构造客户端」，那是「拒绝路径零出网」
      的**结构性**保证）⇒ 模块上**没有** `httpx` 属性，换不到 ⇒ 只能从
      `sys.modules["httpx"]` 那一层换 `AsyncClient`（函数内 `import httpx` 拿到同一个对象）。

    🔴 **但全局换 `AsyncClient` 会把 `SyncClient` 自己也打死**（实测）：`_sync_client.py`
    内部真的 `from httpx import ASGITransport, AsyncClient` 跑 ASGI 传输，
    换成纯桩后报 `'_FakeClient' object has no attribute 'request'` ⇒ **桩打死测试夹具**。
    ⇒ 用**分派桩**按调用形态分流：`AsyncClient(transport=…)`（`SyncClient` 用的形态）
    **原样放行给真类**，其余（`mcp_probe` 用的 `AsyncClient(timeout=…)`）走计数桩。

    ⚠️ 只装一处 ⇒ 另一侧真出网，而那是本件最不能发生的事。
    ⚠️ `get` / `post` 一旦被真调到 ⇒ **直接断言失败**（本件任何腿都不该发请求）。
    """
    counter = _EgressCounter()
    real_httpx = httpx
    real_async_client = real_httpx.AsyncClient

    monkeypatch.setattr(
        settings_routes,
        "httpx",
        _HttpxShim(real_httpx, _fake_client_factory(counter, "settings", real_httpx)),
    )

    mcp_fake = _fake_client_factory(counter, "mcp", real_httpx)

    class _Dispatcher:
        """`sys.modules["httpx"].AsyncClient` 的替身：按调用形态分流（见上面 docstring）。"""

        def __new__(cls, *args, **kwargs):  # noqa: ANN002, ANN003
            if "transport" in kwargs:
                # ASGI 传输（SyncClient 自己用）⇒ 原样给真类，否则本夹具自己就废了
                return real_async_client(*args, **kwargs)
            return mcp_fake(*args, **kwargs)

    monkeypatch.setattr(real_httpx, "AsyncClient", _Dispatcher)
    return counter


@pytest.fixture
def client():
    """两个 router 挂在**同一个**干净 app 上（前缀 /api）—— 跨端点对拍的前提。"""
    return SyncClient(build_app(settings_routes.router, skill_routes.router))


def _models_refusal(client, bad_url: str):
    """喂一条坏地址给 POST /api/models，返回 (status_code, detail, egress_constructed)。"""
    resp = client.post("/api/models", json={"service_id": SVC_CHAT, "base_url": bad_url})
    return resp


def _mcp_refusal(client, bad_url: str):
    """喂同一条坏地址给 POST /api/skills/mcp/probe，返回响应。"""
    return client.post("/api/skills/mcp/probe", json={"transport": "http", "url": bad_url})


# ---------------------------------------------------------------------------
# E-04 · 分母自证：端点清单**现从路由表取**，不得手写字面量
# ---------------------------------------------------------------------------


def test_e04_endpoint_inventory_comes_from_the_real_route_table():
    """**E-04**：本件覆盖的端点**逐个**来自真实路由表（path + methods 对拍）。

    为什么必要：手写一份端点清单，路由改名/挪走后本件**照样绿**（判据与产品脱钩）。
    本腿把「清单」锚在 router.routes 上 ⇒ 端点被挪走当场红。
    """
    expected = {
        ("/models", "POST"): settings_routes.router,
        ("/web/llm/models", "POST"): settings_routes.router,
        ("/skills/mcp/probe", "POST"): skill_routes.router,
    }

    actual: dict = {}
    for router in (settings_routes.router, skill_routes.router):
        for route in router.routes:
            for method in getattr(route, "methods", set()) or set():
                actual[(route.path, method)] = router

    # 分母自证：路由表**非空**（否则下面的循环一条都不跑，判据恒真）
    assert len(actual) >= 4, f"路由表只扫到 {len(actual)} 条 ⇒ 扫描面可疑（空靶风险）"

    # 逐条对拍（**不是**「集合相等」——集合相等在「两边都空」时也成立，那是空靶）
    for key, router in expected.items():
        assert key in actual, f"路由表里没有 {key} ⇒ 产品把端点挪走了，本件须同步复核"
        assert actual[key] is router, f"{key} 挂在别的 router 上（实测 {actual[key]!r}）"


def test_e04_web_side_guard_is_the_same_object_as_net_guard_binding():
    """**E-04（本体唯一性）**：薄壳的判定入口**就是** ssrf.assert_url_is_safe 本身。

    这条是 E-03「三处结论必须一致」的**立论前提**：薄壳若自己抄了一份判定，
    E-03 的「一致」就只是巧合（三处各判各的，随时可分叉）。本腿用 is 身份钉死
    「只有一份本体」—— 一旦有人在 net_guard.py 里写第二份实现，本腿当场红。
    """
    assert net_guard.assert_url_is_safe is ssrf.assert_url_is_safe, (
        "薄壳把判定入口换成了别的对象（≈ 出现了第二份判定本体）"
    )
    # 反向自证：ssrf 的公开面**只有**这三个名字（薄壳没有偷加判定符号）
    assert tuple(sorted(ssrf.__all__)) == ("UrlNotAllowed", "assert_url_is_safe", "resolve_host")
    # 薄壳的公开面：allow_any_host / assert_outbound_url_safe 是**新增**接缝，不是第二份判定
    assert set(net_guard.__all__) == {
        "OutboundBlocked",
        "allow_any_host",
        "assert_outbound_url_safe",
        "assert_url_is_safe",
    }


# ---------------------------------------------------------------------------
# E-01 · 同一输入、两个端点、同一语义（对拍）+ E-05 不 500 / 不抛穿
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_url", BAD_URLS)
def test_e01_same_input_is_refused_on_both_sides_with_matching_semantics(
    client, wired, egress, bad_url
):
    """**E-01 + E-05**：同一条坏地址 ⇒ **两侧都明确失败**，且语义可对应。

    三条对应关系（见件头）：① 两侧都明确失败；② 两侧文案都指得出「为了什么」；
    ③ 两侧都**零出网**。⚠️ 两侧**形状不同**（400 / 200-ok=false）是**既有契约**，
    本腿判的是**语义**对应，不是同码 —— 写成同码就是改产品契约。
    """
    resp_models = _models_refusal(client, bad_url)
    resp_mcp = _mcp_refusal(client, bad_url)

    # ① 两侧都明确失败（形状各自合规，不是 500 / 不是抛穿）
    assert resp_models.status_code == 400, f"模型侧应 400（输入问题），实测 {resp_models.text!r}"
    assert resp_mcp.status_code == 200, f"MCP 侧应 200（探测结果），实测 {resp_mcp.text!r}"

    detail = resp_models.json()["detail"]
    body = resp_mcp.json()
    assert body["ok"] is False, f"MCP 侧必须 ok=false，实测 {body!r}"
    error = body["error"]
    assert body["tools"] == [], f"MCP 拒绝路径不该有工具：{body!r}"

    # ② 两侧文案都指得出「为了什么」（可读中文，不得是英文内部串）
    assert _PURPOSE_SETTINGS in detail, f"模型侧没写清「为了什么」：{detail!r}"
    assert _PURPOSE_MCP in error, f"MCP 侧没写清「为了什么」：{error!r}"
    for text in (detail, error):
        assert any("\u4e00" <= ch <= "\u9fff" for ch in text), f"拒绝文案不是可读中文：{text!r}"

    # ③ 两侧都**零出网**（对拍的核心：拒绝面一个连接都不该建）
    assert egress.constructed == 0, (
        f"拒绝路径仍然构造了 httpx 客户端（{egress.constructed} 次）⇒ 守卫在出网之后"
    )
    assert egress.get_calls == 0 and egress.post_calls == 0, "拒绝路径不该发出任何请求"


@pytest.mark.parametrize("bad_url", BAD_URLS)
def test_e05_refusal_is_never_a_server_error_on_either_side(client, wired, egress, bad_url):
    """**E-05**：逐条异常面 —— 两侧都**不得 500**、也不得把异常抛穿成 5xx。

    「用户填了坏地址」是**输入问题**（400 / 探测结果），不是服务端故障。
    本腿逐条 URL 各断一次「状态码 < 500」，并把「响应体是可解析 JSON」也断上
    —— 抛穿的响应体不是产品契约里的 JSON。
    """
    resp_models = _models_refusal(client, bad_url)
    resp_mcp = _mcp_refusal(client, bad_url)

    assert resp_models.status_code < 500, f"模型侧 5xx：{resp_models.text!r}"
    assert resp_mcp.status_code < 500, f"MCP 侧 5xx：{resp_mcp.text!r}"
    # 响应体必须是可解析 JSON 且带契约键（抛穿的响应体不会有这两个键）
    assert isinstance(resp_models.json().get("detail"), str), resp_models.text
    assert set(resp_mcp.json()) >= {"ok", "tools", "error"}, resp_mcp.text


def test_e01_both_sides_refuse_a_public_url_that_resolves_to_a_private_ip(
    client, wired, egress, monkeypatch
):
    """**E-01（DNS 重绑定形态）**：域名看着像公网、**解析结果**是内网 ⇒ 两侧都拒。

    这条补的是「按**解析结果**判，不是按**字面域名**判」：只按字符串黑名单的写法
    在这条腿两侧同时红。构造 = 桩 resolver 把任何域名都解析成 169.254.169.254。
    """
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("169.254.169.254"))

    resp_models = client.post(
        "/api/models", json={"service_id": SVC_CHAT, "base_url": "https://innocent.example/v1"}
    )
    resp_mcp = client.post(
        "/api/skills/mcp/probe",
        json={"transport": "http", "url": "https://innocent.example/mcp"},
    )

    assert resp_models.status_code == 400, resp_models.text
    assert resp_mcp.json()["ok"] is False, resp_mcp.text
    assert "169.254.169.254" in resp_models.json()["detail"]
    assert egress.constructed == 0, "解析结果是内网 ⇒ 两侧都不该建连接"

# ---------------------------------------------------------------------------
# E-02 · 逃生门两侧一致（开 ⇒ 两侧都放行且各留 WARNING；关 ⇒ 两侧都拒）
# ---------------------------------------------------------------------------


def _hatch_warnings(caplog) -> list[str]:
    """只取 net_guard logger 上、且是本模板的 WARNING 文案。

    ⚠️ 按 logger 名过滤 + 按模板前缀过滤，**两层都做**：caplog 是全 logger 收集，
    不过滤会把网络栈（传输层客户端 / 事件循环）的 WARNING 收进来（假红）。
    """
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _LOGGER_NET_GUARD and r.getMessage().startswith(_HATCH_MESSAGE_PREFIX)
    ]


def test_e02_hatch_on_allows_both_sides_and_each_leaves_a_warning(
    client, wired, egress, monkeypatch, caplog
):
    """**E-02（开档）**：逃生门开 ⇒ **两侧都放行**（真走到出网）且**各留 WARNING**。

    🔴 **只判「两侧各 ≥ 1 条」**，**不**判条数相等 —— 连接侧是「每 host 一次」、
    client 侧是「每请求一次」（081-F 已登记的口径差）。写成等数会把已登记差异误判成缺陷。
    """
    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", True)
    # 逃生门在**解析之后、判定之前**返回 ⇒ 字面内网地址也能过（不桩 resolver 更贴近真行为）
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("169.254.169.254"))

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NET_GUARD):
        # 模型侧：桩在 `get` 上抛 `AssertionError` ⇒ 由端点收成 503（**证明真走到了出网**）
        resp_models = _models_refusal(client, METADATA_URL)
        # MCP 侧：桩抛的异常由 `probe_server` 的 `except Exception` 收成 error 字符串
        # ⚠️ 故**不能**用 `pytest.raises` 判「走到出网」—— 端点契约就是「不抛」（见件头 E-01）。
        #    改判「响应体里带上了桩的报错」⇒ 同样是「真到了那一步」的确证。
        resp_mcp = _mcp_refusal(client, METADATA_URL)

    # 两侧都**放行**（没被守卫拒）：模型侧不是 400、MCP 侧不是「已拒绝访问」
    assert resp_models.status_code != 400, f"逃生门开着却被拒：{resp_models.text!r}"
    assert "已拒绝访问" not in resp_mcp.json()["error"], f"逃生门开着却被拒：{resp_mcp.text!r}"
    # 且两侧都**真走到了出网那一步** —— 读计数桩（本仓既有口径：
    # 「放行」的证据是**真到了出网点**，不是响应看起来没报错）。
    # ⚠️ **不**按响应文案判：两个端点各自把底层异常折成**自己的**中文文案
    # （模型侧 503「无法连接模型服务」、MCP 侧 error「无法连接 MCP server」），
    # 都不会回显底层串 ⇒ 按文案判会假红（本件实测踩过）。
    assert egress.constructed >= 2, (
        f"两侧都该真走到出网（各建一次客户端），实测 {egress.constructed} 次"
    )
    assert egress.get_calls >= 1, f"模型侧应发出 get，实测 {egress.get_calls}"
    assert egress.post_calls >= 1, f"MCP 侧应发出 post，实测 {egress.post_calls}"

    messages = _hatch_warnings(caplog)
    assert len(messages) >= 2, (
        f"两侧各应留 ≥1 条逃生门 WARNING（按各自口径，不判等数），实测 {messages!r}"
    )


def test_e02_hatch_off_refuses_both_sides_and_leaves_no_warning(
    client, wired, egress, caplog
):
    """**E-02（关档，成对反控）**：逃生门关 ⇒ **两侧都拒** + **一条 WARNING 都没有**。

    没有这条，「恒留 WARNING」的实现也能过上一条；而且这条把「两侧都真的接了开关」
    钉成对偶 —— 只给一条路接开关的写法，会在「开档放行」或「关档拒绝」其中一侧红。
    """
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NET_GUARD):
        resp_models = _models_refusal(client, METADATA_URL)
        resp_mcp = _mcp_refusal(client, METADATA_URL)

    assert resp_models.status_code == 400, resp_models.text
    assert resp_mcp.json()["ok"] is False, resp_mcp.text
    assert _hatch_warnings(caplog) == [], "逃生门关着却留了 WARNING"
    assert egress.constructed == 0, "关档 + 坏地址 ⇒ 两侧都不该建连接"


# ---------------------------------------------------------------------------
# E-03 · 与 web 面口径一致：**三处**结论必须一致（同一次对拍）
# ---------------------------------------------------------------------------


def _web_side_verdict(url: str, *, hatch: bool) -> str:
    """web 工具面既有守卫（ssrf 本体）对同一条 URL 的结论：ok / blocked。

    ⚠️ **逃生门必须显式传**：ssrf.assert_url_is_safe 的 allow_any_host 是形参
    （默认 None ⇒ 关），而薄壳自己现读 settings ⇒ 三处对拍时不对齐档位就会
    把「开关没传」误读成「判定分叉」。
    """
    try:
        ssrf.assert_url_is_safe(url, allow_any_host=hatch)
    except ssrf.UrlNotAllowed:
        return "blocked"
    return "ok"


def _net_guard_verdict(url: str) -> str:
    """薄壳（client 侧，自己现读 settings 开关）对同一条 URL 的结论。"""
    try:
        net_guard.assert_outbound_url_safe(url, purpose="对拍")
    except net_guard.OutboundBlocked:
        return "blocked"
    return "ok"


@pytest.mark.parametrize("bad_url", BAD_URLS)
def test_e03_three_faces_agree_that_the_bad_url_is_blocked(
    client, wired, egress, bad_url, monkeypatch
):
    """**E-03（关档）**：两个**端点** + **ssrf 本体** ⇒ 三处结论**一致 = 全拒**。

    这是「同源缺口已收口」的判据形态：判定本体只有一份（E-04 的身份腿），
    故三处一致是结构性的。⚠️ 若实测不一致 ⇒ **是发现，登记并报 Lead**，
    **不**在产品侧抹平（本卡只出判据件）。
    """
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("10.0.0.5"))

    resp_models = _models_refusal(client, bad_url)
    resp_mcp = _mcp_refusal(client, bad_url)
    web_verdict = _web_side_verdict(bad_url, hatch=False)
    guard_verdict = _net_guard_verdict(bad_url)

    assert resp_models.status_code == 400, resp_models.text
    assert resp_mcp.json()["ok"] is False, resp_mcp.text
    assert web_verdict == "blocked", f"web 面本体放行了 {bad_url}（三处分叉）"
    assert guard_verdict == "blocked", f"薄壳放行了 {bad_url}（与本体分叉）"
    assert egress.constructed == 0, "三处都拒 ⇒ 一个连接都不该建"


def test_e03_three_faces_agree_under_the_hatch_too(client, wired, egress, monkeypatch):
    """**E-03（开档）**：逃生门开 ⇒ 三处**一致放行**（证明不是「本体拒、薄壳放行」的分叉）。

    只判关档一致，抓不到「薄壳压根没把开关传给本体」这类分叉：那时关档三处仍全拒，
    开档却只有两处放行。本腿把开档也钉上 ⇒ 三处对拍**两档都成立**。
    """
    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", True)
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("169.254.169.254"))

    assert _web_side_verdict(METADATA_URL, hatch=True) == "ok"
    assert _net_guard_verdict(METADATA_URL) == "ok"

    # 两处端点也必须放行 ⇒ 真走到出网。读计数桩（同 E-02：不按响应文案判，
    # 两个端点各自折成自己的中文文案，都不回显底层串）。
    _models_refusal(client, METADATA_URL)
    _mcp_refusal(client, METADATA_URL)
    assert egress.constructed >= 2, f"开档下两侧都该放行并走到出网，实测 {egress.constructed}"
    assert egress.get_calls >= 1 and egress.post_calls >= 1, (
        f"两侧都该真的发出请求（get={egress.get_calls}, post={egress.post_calls}）"
    )


def test_e03_public_url_is_allowed_by_all_three_faces(monkeypatch):
    """**E-03（正控）**：公网地址 ⇒ 三处**一致放行**。

    没有这条，E-03 的三条「一致拒绝」腿会被一个**无差别全拒**的实现全绿（假绿）。
    """
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("93.184.216.34"))

    public = "https://public.example/v1"
    assert _web_side_verdict(public, hatch=False) == "ok"
    assert _net_guard_verdict(public) == "ok"

