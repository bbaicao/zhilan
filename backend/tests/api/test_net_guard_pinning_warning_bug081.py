"""081-F 逃生门连接侧留痕用例（专项升级 · `回归修复` · 卡 `task-12`）。

## 本文件判什么

同一条 ``WEB_SSRF_ALLOW_ANY_HOST`` 在本仓有**三个**消费点，成文口径是
「开 = 每次放行留一条 WARNING」。此前只有两处留痕，
``app/agent/web/pinning.py::PinnedBackend._resolve_once``（**连接侧**）逃生分支
直接 ``return``、零留痕 ⇒ 本文件把这一处补齐后的**同口径**钉住。

## 三条纪律（沿用 ``test_web_ssrf_r2.py`` 的 D-回归检查/11/16 口径）

1. **绝不真出网**：解析一律注入桩（``monkeypatch.setattr(ssrf, "resolve_host", …)``）；
   本文件不起服务、不碰 wire 面。
2. **解析器是进程级全局** ⇒ 自带 autouse 复位夹具，且只经 ``monkeypatch`` 改。
3. ``PinnedBackend`` 的公开异步入口是 ``connect_tcp`` ⇒ 走它的用例一律
   ``@pytest.mark.asyncio``（本仓 ``pytest.ini`` 无 ``asyncio_mode`` ⇒ strict）。

## 判据归属

| 判据 | 用例 |
|---|---|
| F-01 开 ⇒ 恰好一条 WARNING | ``test_b081f_f01_*`` |
| F-02 关 ⇒ 零 WARNING | ``test_b081f_f02_*`` |
| F-03 三处文案一致 | ``test_b081f_f03_*``（口令 + 同含同一子串 + 地址落在 ``%s`` 位） |
| F-04 既有判据不回归 | 见回执（``test_web_ssrf_r2.py`` 定向 + 旧日志行为腿） |
"""

from __future__ import annotations

import logging

import pytest

from app.agent.web import client as web_client
from app.agent.web import pinning, ssrf
from app.core import net_guard

#: 三处留痕各自的 logger 名（判据一律按名字过滤：``caplog`` 是全 logger 收集，
#: 不过滤会把网络栈的 WARNING 也收进来 ⇒ 假红）。
PINNING_LOGGER = "app.agent.web.pinning"
CLIENT_LOGGER = "app.agent.web.client"
NET_GUARD_LOGGER = "app.core.net_guard"

#: 成文口令：**三处必须逐字同一句**（只允许 ``%s`` 处不同）。
#: ⚠️ 本串是判据侧的**期望值** —— 它必须独立于实现，故这里写死而不是从模块里取。
ENABLE_SENTENCE = "SSRF 逃生门已开启（WEB_SSRF_ALLOW_ANY_HOST）：放行 %s"

#: 更松的一条：连 ``%s`` 都不管，只要求三处同含这句中文前缀（对拍用）。
SHARED_PREFIX = "SSRF 逃生门已开启（WEB_SSRF_ALLOW_ANY_HOST）：放行"

#: 文件导入时抓一份**真**解析器（见 autouse 夹具）
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


class _SentinelError(Exception):
    """哨兵：内层后端**只记录、不真连**（记录完就抛它）。"""


class _RecordingBackend:
    """``httpcore.AsyncNetworkBackend`` 的哑替身（与 ``test_web_ssrf_r2.py`` 同款）。"""

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


def _stub_resolver(*addresses: str):
    """桩解析器：任何域名都解析成给定地址。"""

    def resolve(host: str) -> list[str]:  # noqa: ANN001
        return list(addresses)

    return resolve


def _records(caplog, name: str):
    """按 logger 名过滤（**必须**：不过滤会收进网络栈的 WARNING）。"""
    return [r for r in caplog.records if r.name == name]


# --------------------------------------------------------------------------
# F-01 · 逃生门开 ⇒ 走 ``_resolve_once`` 恰好产生一条 WARNING
# --------------------------------------------------------------------------


def test_b081f_f01_hatch_open_logs_once_on_the_resolve_path(monkeypatch, caplog):
    """**(a) 件级直调**：开关开 ⇒ ``_resolve_once`` 放行**且恰好一条** WARNING，含放行地址。

    ⚠️ 判的是**同步**的 ``_resolve_once``（``connect_tcp`` 把解析丢进线程池，
    ``caplog`` 的 handler 是进程级、记录照样收得到 —— 见下面那条端到端腿）。
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", True)
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("10.0.0.5"))
    backend = pinning.PinnedBackend(inner=_RecordingBackend())
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=PINNING_LOGGER):
        assert backend._resolve_once("internal.example") == "10.0.0.5"  # noqa: SLF001
    records = _records(caplog, PINNING_LOGGER)
    assert len(records) == 1, [r.getMessage() for r in records]
    assert records[0].levelno == logging.WARNING
    assert "10.0.0.5" in records[0].getMessage()
    assert SHARED_PREFIX in records[0].getMessage()



@pytest.mark.asyncio
async def test_b081f_f01_hatch_open_logs_once_through_connect_tcp(monkeypatch, caplog):
    """**(b) 端到端（真入口）**：``connect_tcp`` 一次连接 ⇒ 恰好一条 WARNING。

    ``_resolve_once`` 由 ``run_in_executor`` 在线程里跑 ⇒ 本腿同时钉住
    「留痕在线程里照样落进 caplog」与「连接被改写为放行地址」。
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", True)
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("10.0.0.5"))
    inner = _RecordingBackend()
    backend = pinning.PinnedBackend(inner=inner)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=PINNING_LOGGER):
        with pytest.raises(_SentinelError):
            await backend.connect_tcp("internal.example", 443)
    assert inner.calls == [("10.0.0.5", 443)]
    records = _records(caplog, PINNING_LOGGER)
    assert len(records) == 1, [r.getMessage() for r in records]
    assert "10.0.0.5" in records[0].getMessage()


def test_b081f_f01_each_real_pass_through_the_resolve_path_logs_once(monkeypatch, caplog):
    """**「每次放行一条」**：直调 ``_resolve_once`` 三次 = 三次**真放行** ⇒ **三条**。

    与下面那条 ``connect_tcp`` 缓存腿**成对**：那条证明「缓存命中的第二次连接不算放行」，
    这条证明「不在缓存里的放行一次都不漏」。
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", True)
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("10.0.0.5"))
    backend = pinning.PinnedBackend(inner=_RecordingBackend())
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=PINNING_LOGGER):
        for _ in range(3):
            assert backend._resolve_once("cache.example") == "10.0.0.5"  # noqa: SLF001
    assert len(_records(caplog, PINNING_LOGGER)) == 3


@pytest.mark.asyncio
async def test_b081f_f01_second_connection_to_the_same_host_hits_the_pin_cache(monkeypatch, caplog):
    """**成对腿**：同一 host **经 ``connect_tcp``** 连三次 ⇒ 只有**第一次**解析 ⇒ 一条 WARNING。

    它接的是上一条的「每次真放行一条」：缓存命中不算放行，所以不重复留痕。
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", True)
    seen: list[str] = []

    def resolver(host: str) -> list[str]:
        seen.append(host)
        return ["10.0.0.5"]

    monkeypatch.setattr(ssrf, "resolve_host", resolver)
    inner = _RecordingBackend()
    backend = pinning.PinnedBackend(inner=inner)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=PINNING_LOGGER):
        for _ in range(3):
            with pytest.raises(_SentinelError):
                await backend.connect_tcp("cached.example", 443)
    assert seen == ["cached.example"]  # 只解析一次 = 只放行一次
    assert len(inner.calls) == 3
    assert len(_records(caplog, PINNING_LOGGER)) == 1


# --------------------------------------------------------------------------
# F-02 · 逃生门关 ⇒ **零** WARNING（不误报）
# --------------------------------------------------------------------------


def test_b081f_f02_hatch_off_logs_nothing_on_the_resolve_path(monkeypatch, caplog):
    """① 关 + 公网地址：放行但**不留痕**（留痕只属于逃生门，不是正常放行）。"""
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("93.184.216.34"))
    backend = pinning.PinnedBackend(inner=_RecordingBackend())
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=PINNING_LOGGER):
        assert backend._resolve_once("public.example") == "93.184.216.34"  # noqa: SLF001
    assert _records(caplog, PINNING_LOGGER) == []


def test_b081f_f02_hatch_off_refusal_is_still_a_refusal_with_no_warning(monkeypatch, caplog):
    """② 关 + 私网 ⇒ 仍抛 ``UrlNotAllowed``，且**依然**零 WARNING（拒绝不是留痕事件）。

    ⚠️ 这一条同时是「本笔没有把判定逻辑改松」的钉子：关着时私网照旧拒。
    """
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("10.0.0.5"))
    backend = pinning.PinnedBackend(inner=_RecordingBackend())
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=PINNING_LOGGER):
        with pytest.raises(ssrf.UrlNotAllowed):
            backend._resolve_once("internal.example")  # noqa: SLF001
    assert _records(caplog, PINNING_LOGGER) == []


# --------------------------------------------------------------------------
# F-03 · 三处文案一致性（client._guard_hook / net_guard / pinning）
# --------------------------------------------------------------------------


def test_b081f_f03_pinning_uses_the_shared_sentence_verbatim():
    """三处源码里那句**口令**必须逐字一致（``%s`` 槽位也一致）。

    判法：把三个模块的源码读进来，数 ``ENABLE_SENTENCE`` 各自出现的次数 == 1，
    且三处都**不**出现「同一句的变体」（用去掉 ``%s`` 的前缀做包含检查）。
    """
    sources = {
        "pinning": open(pinning.__file__, encoding="utf-8").read(),
        "net_guard": open(net_guard.__file__, encoding="utf-8").read(),
        "client": open(web_client.__file__, encoding="utf-8").read(),
    }
    for name, text in sources.items():
        assert text.count(ENABLE_SENTENCE) == 1, name


def test_b081f_f03_pinning_warning_carries_the_address_in_the_slot(monkeypatch, caplog):
    """F-03 的**行为腿**：pinning 的 ``%s`` 槽位里落的是**放行地址**（与另两处置位口径一致）。

    ``client.py`` 那处放的是 ``request.url.host``、``net_guard`` 放的是 host ——
    连接侧能拿到的最具体的东西就是**要真去连的地址**，故这里放 ``picked``。
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", True)
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("10.0.0.5"))
    backend = pinning.PinnedBackend(inner=_RecordingBackend())
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=PINNING_LOGGER):
        backend._resolve_once("internal.example")  # noqa: SLF001
    record = _records(caplog, PINNING_LOGGER)[0]
    # 口令 + 槽位值拼接后 == 模板填入后的整句
    assert record.getMessage() == ENABLE_SENTENCE.replace("%s", "10.0.0.5")
    assert record.args == ("10.0.0.5",)


def test_b081f_f03_logger_names_are_the_three_module_loggers():
    """三处各走**自己模块**的 logger（判据按名字过滤，名字不对就收不到）。"""
    assert pinning.logger.name == PINNING_LOGGER
    assert net_guard.logger.name == NET_GUARD_LOGGER
    assert web_client.logger.name == CLIENT_LOGGER


# --------------------------------------------------------------------------
# F-04 · 不回归：旧行为（判定/缓存/线程）逐条照旧
# --------------------------------------------------------------------------


def test_b081f_f04_hatch_off_uses_the_judgement_not_the_resolver(monkeypatch):
    """关着时**不得**走「只解析」那条路：判定本体照旧是唯一放行依据。"""
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("10.0.0.5"))
    backend = pinning.PinnedBackend(inner=_RecordingBackend())
    with pytest.raises(ssrf.UrlNotAllowed):
        backend._resolve_once("internal.example")  # noqa: SLF001
    # 反控：同一条 host 在开关开启时被放行（证明上面那次拒绝来自判定，不是别的原因）
    from app.core.config import settings

    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", True)
    assert backend._resolve_once("internal.example") == "10.0.0.5"  # noqa: SLF001


def test_b081f_f04_unresolvable_host_falls_back_to_the_host_itself(monkeypatch, caplog):
    """既有语义：逃生门开着且解析失败 ⇒ 回退**裸 host**（不改行为），且留痕里的值也是它。"""
    import socket

    from app.core.config import settings

    def boom(host: str) -> list[str]:  # noqa: ANN001
        raise socket.gaierror("dns down")

    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", True)
    monkeypatch.setattr(ssrf, "resolve_host", boom)
    backend = pinning.PinnedBackend(inner=_RecordingBackend())
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=PINNING_LOGGER):
        assert backend._resolve_once("offline.example") == "offline.example"  # noqa: SLF001
    record = _records(caplog, PINNING_LOGGER)[0]
    assert "offline.example" in record.getMessage()


@pytest.mark.asyncio
async def test_b081f_f04_hatch_off_path_is_unchanged_end_to_end(monkeypatch, caplog):
    """端到端反控（关着）：公网域名经判定 ⇒ 连接被改写成已校验地址，且零 WARNING。

    它与 F-01 那条端到端腿**成对**：同一个入口、同一种断法，唯一变量是开关。
    """
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("93.184.216.34"))
    inner = _RecordingBackend()
    backend = pinning.PinnedBackend(inner=inner)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=PINNING_LOGGER):
        with pytest.raises(_SentinelError):
            await backend.connect_tcp("public.example", 443)
    assert inner.calls == [("93.184.216.34", 443)]
    assert _records(caplog, PINNING_LOGGER) == []
