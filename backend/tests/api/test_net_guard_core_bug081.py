"""081-A 出网守卫薄壳用例（专项升级 · `回归修复` · 卡 `task-7`）。

## 本文件判什么

判 ``app/core/net_guard.py`` 这层**薄壳**：判定本体是否**原样**落到 web 面那份
（``app/agent/web/ssrf.py``）、逃生门是否**现读**而非固化、拒绝是否变成
``OutboundBlocked``（``ValueError``，**不是** ``HTTPException``）。

## ⚠️ 一条判据**落点**上的实测更正（卡内原文照抄会做出假绿，如实登记）

卡内 B-03/B-04 的措辞是「开 ⇒ 放行」。**实测**：只把
``settings.WEB_SSRF_ALLOW_ANY_HOST`` 置 ``True`` **不会**放行 ——
``ssrf.assert_url_is_safe`` 的逃生门是它**自己的关键字参数**（默认 ``None`` ⇒ 关），
它根本不认识 ``settings``（该模块 docstring 原文：「本模块不认识配置」）。
⇒ 薄壳的正确接法是**把现读到的开关传下去**：
``assert_url_is_safe(url, allow_any_host=allow_any_host())``。
这一条正是本层存在的**第二个**理由（web 面把它做在 ``client.py::_guard_hook`` 里；
非 web 面没有那个钩子，只剩这一层）。

⇒ 判据据此落成：**关（出厂档）** = 薄壳即便被调用也拒；**开** = 先把薄壳依赖的那一个
判定函数换成「钉死 ``allow_any_host=True``」的直通桩，再验薄壳的**放行与留痕**。
被换掉的那一条正是「开关有没有传下去」，故另有**源码腿**独立钉住它 —— 否则
「把开关忘了传」这种缺陷会被同一个桩**掩盖成假绿**。

## 三条纪律（沿用 ``test_web_ssrf_r2.py`` 的 D-回归检查/11/16 口径）

1. **绝不真出网**：解析一律注入桩（``monkeypatch.setattr(ssrf, "resolve_host", …)``）；
   本文件**不起服务、不碰 wire 面**。
2. **解析器是进程级全局** ⇒ 自带 autouse 复位夹具，且只经 ``monkeypatch`` 改
   （直接赋值会把桩漏给同进程后续用例，且没有任何旧判据能发现）。
3. 本文件**没有** async 用例（薄壳是同步的）⇒ 不需要 ``@pytest.mark.asyncio``。

## 判据归属

| 判据 | 用例 |
|---|---|
| B-01 正控 | ``test_b081_b01_*``（两条） |
| B-02 拒绝腿 | ``test_b081_b02_refusal_legs_*``（参数化五条）+ 私有域名腿 |
| B-03 逃生门 | ``test_b081_b03_*``（关/开成对 + 计数 + 留痕 host）+ 源码接线腿 |
| B-04 不固化 | ``test_b081_b04_*``（两条） |
| B-05 薄壳自证 | ``test_b081_b05_*``（源码三条腿） |
| 件级 | ``test_b081_exception_is_a_value_error_*`` |
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from app.agent.web import ssrf
from app.core import net_guard

#: 本模块的 logger 名（判据一律按它过滤：caplog 是全 logger 收集，不过滤会收进
#: httpx / httpcore / asyncio 的 WARNING ⇒ 假红）
LOGGER_NAME = "app.core.net_guard"

#: 文件导入时抓一份**真**解析器：本文件的存在前提就是它不被真调用（见 autouse 夹具）
_REAL_RESOLVER = ssrf.resolve_host


@pytest.fixture(autouse=True)
def _isolate_resolver(monkeypatch):
    """把模块级解析器钉回真实现：一条用例留下的桩不得漏给下一条。"""
    monkeypatch.setattr(ssrf, "resolve_host", _REAL_RESOLVER)


@pytest.fixture(autouse=True)
def _hatch_off(monkeypatch):
    """逃生门**默认关**：出厂档就是 ``False``，逐条用例显式钉一遍（本机 `.env` 除外干扰）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", False)


def _stub_resolver(*addresses: str):
    """桩解析器：任何域名都解析成给定地址（与 ``test_web_ssrf_r2.py`` 同款）。"""

    def resolve(host: str) -> list[str]:  # noqa: ANN001
        return list(addresses)

    return resolve


def _hatch_pinned_judgement(address: str = "10.0.0.5"):
    """把薄壳依赖的那一个**判定函数**换成「逃生门已开」的直通桩。

    ⚠️ 只换 ``net_guard`` 里那一个名字，**不碰** ``ssrf.py``：判的是薄壳的**留痕与放行**，
    而不是 ``ssrf`` 自己的逃生门（那条已由 ``test_web_ssrf_r2.py`` 的 A24 腿钉着）。

    ⚠️ 桩**必须带** ``allow_any_host`` 关键字并断言它已被传下来 —— 否则
    「薄壳忘了把开关传下去」会被同一个桩掩盖成假绿。
    """

    def fake(url: str, *, allow_any_host: bool | None = None) -> str:  # noqa: ANN001
        assert allow_any_host is True, "薄壳没有把现读到的逃生门传下来"
        return address

    return fake


def _shell_source() -> str:
    """本模块源码（判据读的是**文件**，不是内存 AST —— 文件才是交付物）。"""
    return Path(net_guard.__file__).read_text(encoding="utf-8")


#: 地址字面量 / 判定符号名：出现在 ``net_guard.py`` 里就等于本模块开了**第二份**判定。
#: 这些串**只在对本模块源码做静态扫描时**出现，不是运行时判定逻辑。
_FORBIDDEN_IN_SHELL = (
    "169.254",
    "127.0.0.1",
    "is_global",
    "ipaddress",
    "ip_network",
    "urlparse",
    "_effective_ip",
    "_is_public",
    "_NAT64_PREFIX",
    "getaddrinfo",
    "http",
)


def test_b081_b05_shell_module_contains_no_address_literals_or_judgement_symbols():
    """薄壳自证腿：源码里**不出现**任何地址字面量 / 判定符号 ⇒ 判定只有 ``ssrf.py`` 一份。

    ⚠️ 本模块**刻意不 import ``urllib.parse``**：解析属于判定本体（``ssrf.py``）的职责，
    这里再解析一次等于开第二个真相源。取日志用的 host 由 :func:`net_guard._host_of`
    用 ``str.split`` 取（本项目里 ``http`` 这条判据同时挡掉「scheme 白名单被复制过来」。）
    """
    source = _shell_source()
    for token in _FORBIDDEN_IN_SHELL:
        assert token not in source, token


def test_b081_b05_shell_module_imports_only_stdlib_and_the_two_allowed_layers():
    """导入期 import 腿：顶层只许标准库 + ``app.core`` + ``app.agent.web.ssrf``。

    这一条是本模块对**分包边界**的机器判据：本模块**不得** import ``app.api.*``，
    也不得 import ``app.agent.runner``（见 ``net_guard.py`` docstring 的边界一节）。
    """
    tree = ast.parse(_shell_source())
    top_level: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:  # 相对 import（``from .x import y``）一律不是标准库
                module = "." * node.level + module
            top_level.extend(f"{module}.{alias.name}" for alias in node.names)
    # 允许清单**恰好**是这四条：多一条、少一条都算违约（少一条 = 本腿没覆盖到真源）。
    # 实测这三条本仓 import 边都不会破边界（见 net_guard.py docstring 的实测登记）：
    #   ``app.agent.web.ssrf`` = 判定本体（纯标准库）；``app.core.config`` = 配置单例。
    assert sorted(top_level) == [
        "__future__.annotations",
        "app.agent.web.ssrf",
        "app.core.config.settings",
        "logging",
    ], top_level
    # 显式反例（可读性 > 只靠上面那条精确相等）：核心层不许碰 api 层与 runner
    assert not any(module.startswith("app.api") for module in top_level), top_level
    assert not any(module.startswith("app.agent.runner") for module in top_level), top_level


# --------------------------------------------------------------------------
# B-01 · 正控：公网 + 假 resolver ⇒ 不抛，且返回**已校验**的地址
# --------------------------------------------------------------------------


def test_b081_b01_public_url_passes_and_returns_the_checked_address(monkeypatch):
    """正控腿：域名经桩解析到公网 ⇒ 不抛，返回值 = 桩给的地址（不是域名、不是空串）。"""
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("93.184.216.34"))
    got = net_guard.assert_outbound_url_safe("http://example.com/a", purpose="用例")
    assert got == "93.184.216.34"


def test_b081_b01_literal_public_address_round_trips_without_dns():
    """字面公网地址腿：**不碰 DNS**（``getaddrinfo`` 对字面 IP 不走解析）⇒ 断网机器也绿。"""
    got = net_guard.assert_outbound_url_safe("http://93.184.216.34/x", purpose="用例")
    assert got == "93.184.216.34"


# --------------------------------------------------------------------------
# B-02 · 拒绝腿（逐条各一例）：五条地址形态 ⇒ OutboundBlocked + 可读中文
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "fragment"),
    [
        ("http://169.254.169.254/latest/meta-data/", "已拒绝访问"),  # 云元数据
        ("http://127.0.0.1:9527/", "已拒绝访问"),  # 回环 + 端口
        ("http://[::ffff:127.0.0.1]/", "已拒绝访问"),  # IPv4-mapped 内嵌回环
        ("file:///c:/windows/win.ini", "不是合法的网页地址"),  # 非 http(s)
        ("http://user:pw@example.com/", "内嵌用户名或口令"),  # 内嵌凭据
    ],
)
def test_b081_b02_refusal_legs_raise_outbound_blocked_with_readable_chinese(url, fragment):
    """五条拒绝腿逐条各一例：异常类型、可读中文子串，以及**目的**都在消息里。"""
    with pytest.raises(net_guard.OutboundBlocked) as excinfo:
        net_guard.assert_outbound_url_safe(url, purpose="订阅源抓取")
    message = str(excinfo.value)
    assert fragment in message, url
    assert "订阅源抓取" in message, url
    # 中文可读 = 消息里真的有汉字（不是英文内部串）
    assert any("\u4e00" <= ch <= "\u9fff" for ch in message), url


def test_b081_b02_private_domain_through_stub_resolver_is_refused(monkeypatch):
    """**真判别腿**：字面地址早就被拒；「域名解析到私网」只有真走解析才拦得住。"""
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("10.0.0.5"))
    with pytest.raises(net_guard.OutboundBlocked) as excinfo:
        net_guard.assert_outbound_url_safe("http://internal.example/", purpose="文件导入")
    assert "10.0.0.5" in str(excinfo.value)


def test_b081_b02_purpose_is_absent_from_the_message_when_not_given(monkeypatch):
    """成对反控：不给 ``purpose`` 时**不得**凭空多出一对空括号（文案仍可读）。"""
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("10.0.0.5"))
    with pytest.raises(net_guard.OutboundBlocked) as excinfo:
        net_guard.assert_outbound_url_safe("http://internal.example/")
    message = str(excinfo.value)
    assert "已拒绝访问" in message
    assert "（）" not in message


# --------------------------------------------------------------------------
# B-03 · 逃生门腿：关 ⇒ 拒；开 ⇒ 放行 **且恰好一条 WARNING**
# --------------------------------------------------------------------------


def test_b081_b03_hatch_off_refuses_and_writes_no_warning(monkeypatch, caplog):
    """① 关（出厂档）：薄壳即便被调用，私网仍拒，且本 logger **一条记录都没有**。

    ⚠️ **这条腿不换判定函数** —— 判的就是「开关关着时薄壳不给自己开后门」。
    """
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("10.0.0.5"))
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        with pytest.raises(net_guard.OutboundBlocked):
            net_guard.assert_outbound_url_safe("http://10.0.0.5/secret", purpose="文件导入")
    records = [r for r in caplog.records if r.name == LOGGER_NAME]
    assert records == []


def test_b081_b03_hatch_on_allows_and_writes_exactly_one_warning(monkeypatch, caplog):
    """② 开：放行（返回判定给的地址）**且恰好一条** WARNING，文案里带 host。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", True)
    monkeypatch.setattr(net_guard, "assert_url_is_safe", _hatch_pinned_judgement())
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        address = net_guard.assert_outbound_url_safe(
            "http://10.0.0.5/secret", purpose="文件导入"
        )
    assert address == "10.0.0.5"
    records = [r for r in caplog.records if r.name == LOGGER_NAME]
    assert len(records) == 1, [r.getMessage() for r in records]
    assert records[0].levelno == logging.WARNING
    assert "10.0.0.5" in records[0].getMessage()
    assert "逃生门" in records[0].getMessage()


def test_b081_b03_hatch_logs_once_per_call_not_once_per_module(monkeypatch, caplog):
    """②的**计数腿**「每次放行一条」：连放三次 ⇒ **三条**（不是 import 期一条）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", True)
    monkeypatch.setattr(net_guard, "assert_url_is_safe", _hatch_pinned_judgement())
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        for _ in range(3):
            net_guard.assert_outbound_url_safe("http://10.0.0.5/a")
    records = [r for r in caplog.records if r.name == LOGGER_NAME]
    assert len(records) == 3


def test_b081_b03_warning_carries_host_for_domain_ipv6_and_port(monkeypatch, caplog):
    """留痕腿：文案里的 host 对**域名**与 **IPv6 字面量**都取得对（``::1`` 不能变成 ``[``）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", True)
    monkeypatch.setattr(net_guard, "assert_url_is_safe", _hatch_pinned_judgement())
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        net_guard.assert_outbound_url_safe("http://internal.example:8080/x")
        net_guard.assert_outbound_url_safe("http://[::1]:9527/x")
    messages = [r.getMessage() for r in caplog.records if r.name == LOGGER_NAME]
    assert len(messages) == 2
    assert "internal.example" in messages[0]
    assert "[::1]" in messages[1]


def test_b081_b03_shell_passes_the_hatch_down_to_the_judgement():
    """**源码接线腿（独立于上面那个桩）**：薄壳必须把现读到的开关**传下去**。

    ⚠️ 这条腿是必需的：上面几条都换了判定函数，而换掉的那一条正是「有没有传开关」。
    腿的做法 = AST 直查：判定的**唯一引用点**是一个模块级赋值，且它的右侧只有一次
    ``.assert_url_is_safe``；调用它的那次调用必须带 ``allow_any_host=allow_any_host()``。
    没有它，「把开关忘了传」会被那个桩**掩盖成假绿**（实测：只设 settings 为 True、
    不传开关，薄壳仍拒）。
    """
    tree = ast.parse(_shell_source())
    # ① 引用点只许一处（模块级赋值，右侧 = 属性取值）
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "assert_url_is_safe" for t in node.targets)
    ]
    assert len(assignments) == 1, "薄壳只应有**一处**判定引用点"
    rhs = assignments[0].value
    assert isinstance(rhs, ast.Attribute) and rhs.attr == "assert_url_is_safe"
    assert isinstance(rhs.value, ast.Name) and rhs.value.id == "ssrf"
    # ② 调用点也只许一处，且必须把现读到的开关传下去
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "assert_url_is_safe"
    ]
    assert len(calls) == 1, "薄壳只应有**一处**判定调用点"
    keywords = {kw.arg: kw.value for kw in calls[0].keywords}
    assert "allow_any_host" in keywords, "薄壳没有把逃生门作为关键字传下去"
    value = keywords["allow_any_host"]
    assert isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
    assert value.func.id == "allow_any_host"
    assert value.args == [] and value.keywords == []


# --------------------------------------------------------------------------
# B-04 · 不固化腿：同一进程里改 settings ⇒ 再次调用**立刻**生效
# --------------------------------------------------------------------------


def test_b081_b04_hatch_is_read_at_call_time_not_frozen_at_import(monkeypatch):
    """同一个进程、同一个模块：``False`` → ``True`` → ``False``，取值每次都跟着变。

    ``allow_any_host()`` 若是 import 期固化的常量，第二次往返会照旧读到旧值 ⇒ 本腿红。
    （只判 ``allow_any_host()`` 的**现值**，放行腿见 B-03。）
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", False)
    assert net_guard.allow_any_host() is False

    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", True)
    assert net_guard.allow_any_host() is True

    # 再翻回去也照样生效（双向，排除「只读第一次 / 只读最后一次」这类巧合）
    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", False)
    assert net_guard.allow_any_host() is False


def test_b081_b04_hatch_value_is_threaded_into_the_judgement_call(monkeypatch):
    """**行为腿**：开关从 False 翻到 True，薄壳传给判定的值**跟着变**（同一条 URL 两次调用）。

    ⚠️ 这条腿**不**判「开着就一定放行」：逃生门的放行权在判定本体（``ssrf.py``）手里，
    那里只认自己的关键字参数；本腿判的是薄壳**有没有把现读到的值送出去**。
    """
    from app.core.config import settings

    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("10.0.0.5"))
    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", False)
    with pytest.raises(net_guard.OutboundBlocked):
        net_guard.assert_outbound_url_safe("http://10.0.0.5/a")

    seen: list[bool | None] = []

    def spy(url: str, *, allow_any_host: bool | None = None) -> str:  # noqa: ANN001
        seen.append(allow_any_host)
        raise ssrf.UrlNotAllowed("（本腿的假判定）")

    monkeypatch.setattr(net_guard, "assert_url_is_safe", spy)
    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", True)
    with pytest.raises(net_guard.OutboundBlocked):
        net_guard.assert_outbound_url_safe("http://10.0.0.5/a")
    assert seen == [True]


# --------------------------------------------------------------------------
# 件级：异常类型（**不抛 HTTPException** 的那条纪律）
# --------------------------------------------------------------------------


def test_b081_exception_is_a_value_error_and_not_an_http_exception():
    """``OutboundBlocked ⊂ ValueError``，且 MRO 上**不含**任何 ``*HTTPException*``。

    ``core`` 层不认识 fastapi：抛 ``HTTPException`` 会把 web 框架拖进核心层。
    本仓库**没有** fastapi（它只在 ``requirements-dev.txt``）⇒ 这里不 import fastapi，
    只按**类名**判，断的是「薄壳没有引入 web 框架的异常」这条性质。
    """
    assert issubclass(net_guard.OutboundBlocked, ValueError)
    names = [cls.__name__ for cls in net_guard.OutboundBlocked.__mro__]
    assert names[:3] == ["OutboundBlocked", "ValueError", "Exception"]
    assert not any("HTTPException" in name for name in names)
    assert not hasattr(net_guard, "HTTPException")
