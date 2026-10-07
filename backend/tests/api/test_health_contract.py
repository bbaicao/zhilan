"""``/api/health`` 契约与降级回归（回归修复）。

**为什么单独立文件**：health 是**外壳依赖**的端点——前端 `api.ts::Health` 类型
逐字依赖它的 11 个键。骨架期把它实现成 3 字段占位（`database`/`redis` 恒为
`"skipped"`）而无人察觉，正是因为**没有用例断言过它的形状**。本文件补上：

1. **键集完整性**：11 键一个不少（缺一个前端就退化成「请先配置 API Key」+ 双红灯）；
2. **真探活**：`database`/`redis` 是 `ok|error`，不是占位字符串；
3. **降级矩阵（I5）**：任一探针失败**都不抛异常**，且互不连累；
4. **状态码恒 200**：健康状况由 `status` 字段表达，不用 HTTP 码；
5. **垫片钉子（2026-09-18 补，`回归修复` 收尾）**：`redis` 探针**不依赖真 Redis 服务**
   ——`REDIS_*` 指向不可达端口仍报 `ok`（行为级）。⚠️ 这第 5 条是**另加**的：上面第 3 条只钉
   「取值随探针变化」，在本机恰有 Redis 容器时，把实现换回真客户端它照样绿——而桌面交付不带
   Redis 服务，「零外部依赖」必须是断言而非注释。
   （「`app/**` 不得 import 第三方 redis」与垫片身份/往返的钉子在同目录之上的
   `tests/test_redis_shim.py`；**不要**用 `sys.modules` 判断——`portalocker` 会把它拉进进程。）
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.api import _require_config
from app.api import health as health_endpoint
from app.core.config import settings
from app.core.errors import ProviderNotConfigured
from app.db import redis as redis_mod
from app.db.session import engine


@pytest.fixture(autouse=True)
def _isolate_module_singletons():
    """每个用例前后重置模块级单例（Redis 客户端 + MySQL 连接池）。

    两处都是**测试夹具**问题、不是产品代码问题：真实服务全程只有一个事件循环，
    模块级单例（连接复用）正是想要的行为。而 pytest-asyncio 是 **function 级
    事件循环**——上个用例在 loop A 里建的连接，下个用例在 loop B 里复用时，
    底层传输对象仍绑在已关闭的 loop 上：

    - Redis：``RuntimeError: Event loop is closed``
    - MySQL 池：``'NoneType' object has no attribute 'send'``，探测被记成 ``error``

    不清理就会「单独跑绿、一起跑红」（本文件初版正是如此）。

    **夹具保持同步**：``engine.dispose()`` 返回的是 awaitable，用
    ``asyncio.run`` 关掉即可。写成 async 夹具会让本文件里的同步用例
    （``_require_config`` 那几条）全部 ERROR。
    """
    redis_mod._client = None
    asyncio.run(engine.dispose())
    yield
    redis_mod._client = None
    asyncio.run(engine.dispose())

# 前端 `api.ts::Health` 逐字依赖的键（改这里必须先改 02 契约 §7.5）
REQUIRED_KEYS = (
    "status",
    "database",
    "redis",
    "llm_configured",
    "llm_provider",
    "llm_model",
    "embed_provider",
    "embed_model",
    "vision_provider",
    "vision_model",
    "clean",
)

CLEAN_KEYS = ("enabled", "rule_fingerprint", "stale_documents", "needs_reindex", "hint")


class _BoomSession:
    """``async with SessionLocal()`` 直接炸，模拟 MySQL 不可用。"""

    async def __aenter__(self):
        raise RuntimeError("mysql down")

    async def __aexit__(self, *exc):
        return False


class _BoomRedis:
    """``get_redis_client().ping()`` 抛异常，模拟 Redis 不可用。"""

    async def ping(self):
        raise RuntimeError("redis down")


def _redis_boom():
    """把 ``app.api.get_redis_client`` 换成返回 ``_BoomRedis`` 的桩。"""
    return patch("app.api.get_redis_client", lambda: _BoomRedis())


# --------------------------------------------------------------------------
# 1. 键集完整性（前端契约）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_returns_all_frontend_keys():
    """11 键齐备——少一个前端就显示错状态（回归修复 的根因）。"""
    result = await health_endpoint()

    assert set(REQUIRED_KEYS) <= set(result), (
        f"缺少前端依赖的键：{set(REQUIRED_KEYS) - set(result)}"
    )


@pytest.mark.asyncio
async def test_clean_subobject_shape():
    """``clean`` 子对象是前端 ``CleanStatus`` 的 5 个键。"""
    result = await health_endpoint()

    assert set(CLEAN_KEYS) <= set(result["clean"])
    assert isinstance(result["clean"]["enabled"], bool)
    assert isinstance(result["clean"]["stale_documents"], int)
    assert isinstance(result["clean"]["needs_reindex"], bool)
    assert isinstance(result["clean"]["hint"], str)


@pytest.mark.asyncio
async def test_provider_fields_are_str_or_none():
    """六个 provider/model 字段是 ``str | None``（前端按 null 判「未配」）。"""
    result = await health_endpoint()

    for key in (
        "llm_provider",
        "llm_model",
        "embed_provider",
        "embed_model",
        "vision_provider",
        "vision_model",
    ):
        assert result[key] is None or isinstance(result[key], str), key


# --------------------------------------------------------------------------
# 2. 真探活（不是占位字符串）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_database_redis_are_real_probes():
    """``database``/``redis`` 只能是 ``ok``/``error``——**不许再是 ``skipped``**。

    骨架期硬编码 ``"skipped"`` 让前端把健康的库判成红点；这条断言把占位钉死。
    """
    result = await health_endpoint()

    assert result["database"] in {"ok", "error"}
    assert result["redis"] in {"ok", "error"}
    assert result["database"] != "skipped"
    assert result["redis"] != "skipped"


@pytest.mark.asyncio
async def test_status_is_derived_from_probes():
    """``status`` 是 database/redis 的合成：都为 ok 才 ok。"""
    result = await health_endpoint()

    expected = "ok" if result["database"] == "ok" and result["redis"] == "ok" else "error"
    assert result["status"] == expected


# --------------------------------------------------------------------------
# 3. 降级矩阵（I5：绝不炸穿，且探针互不连累）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redis_failure_does_not_affect_database():
    """Redis 挂 → ``redis=error``、``status=error``，但 ``database`` 不受连累、不抛异常。"""
    with _redis_boom():
        result = await health_endpoint()

    assert result["redis"] == "error"
    assert result["status"] == "error"
    assert result["database"] == "ok", "Redis 失败不该连累 database 探针"


@pytest.mark.asyncio
async def test_mysql_failure_does_not_affect_redis():
    """MySQL 挂 → ``database=error``，``redis`` 仍独立探测、不抛异常。"""
    with patch("app.api.SessionLocal", lambda: _BoomSession()):
        result = await health_endpoint()

    assert result["database"] == "error"
    assert result["status"] == "error"
    assert result["redis"] == "ok", "MySQL 失败不该连累 redis 探针"


@pytest.mark.asyncio
async def test_both_down_still_returns_full_body():
    """两库全挂 → 仍返回**完整键集**（前端要能渲染出「两个红点」而不是崩）。"""
    with _redis_boom(), patch("app.api.SessionLocal", lambda: _BoomSession()):
        result = await health_endpoint()

    assert set(REQUIRED_KEYS) <= set(result)
    assert result["database"] == "error"
    assert result["redis"] == "error"
    assert result["status"] == "error"


# --------------------------------------------------------------------------
# 4. 垫片钉子（回归修复 收尾，2026-09-18）
#
# 为什么单列一节：上面那组用例钉的是「redis 的**取值**随探针变化」，它们**钉不住
# 「根本不依赖真 Redis」**——本机恰有 Redis 容器时，把实现偷偷换回真客户端
# （`redis.asyncio.Redis`）那几条照样绿。桌面交付（阶段 6）不带 Redis 服务，
# 所以「零外部服务依赖」本身必须是断言，而不是注释里的承诺。
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_write_failures_is_reported(monkeypatch):
    """第 12 键（`回归修复`）：把 `safe_append_event` 吞掉的次数**如实报出来**。

    失败隔离（I5）是设计，但它让「事件没写」在库里查不到——这个进程内计数是唯一可观测口径。
    既断言「有这一键且是 int」，也断言它**真是从 `app.session.events` 读的**（桩成 7 就要回 7），
    否则写死一个 0 也能过。
    """
    from app.session import events as events_mod

    result = await health_endpoint()
    assert isinstance(result["event_write_failures"], int)
    assert result["event_write_failures"] >= 0

    monkeypatch.setattr(events_mod, "event_write_failures", lambda: 7)
    assert (await health_endpoint())["event_write_failures"] == 7


@pytest.mark.asyncio
async def test_redis_probe_needs_no_real_redis(monkeypatch):
    """`REDIS_*` 指向不可达端口，`redis` 仍须报 `ok`。

    `REDIS_*` 三键本就**没有读取点**（`回归修复`：垫片不读配置），这里把端口
    指到必然不可达的 1 号端口，就是为了在**用例层面**把这个事实钉死：一旦有人
    改回真客户端，本用例在「本机 Redis 恰好在跑」的机器上也会红。
    （垫片自身的钉子——「`app/**` 不得 import 第三方 redis」+ 五方法调用面——在
    `tests/test_redis_shim.py`。）
    """
    monkeypatch.setattr(settings, "REDIS_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "REDIS_PORT", 1)
    monkeypatch.setattr(settings, "REDIS_PASSWORD", "")

    result = await health_endpoint()

    assert result["redis"] == "ok", (
        "redis 探针依赖了真服务：本机 REDIS_PORT=1 必然不可达，只有进程内垫片才会在此时报 ok"
        "（回归修复）"
    )


@pytest.mark.asyncio
async def test_clean_probe_failure_is_swallowed():
    """清洗状态探测失败**不影响** health（它是增益，不是关卡）。"""
    with patch(
        "app.pipeline.clean_status.clean_status",
        side_effect=RuntimeError("clean boom"),
    ):
        result = await health_endpoint()

    assert result["database"] == "ok"
    assert result["clean"]["stale_documents"] == 0
    assert result["clean"]["needs_reindex"] is False


# --------------------------------------------------------------------------
# 4. 模型配置判定（llm_configured 口径）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_not_configured_when_no_model():
    """没配模型 → ``llm_configured=False``（前端据此显示「请先配置 API Key」）。"""
    with patch("app.api._safe_config", return_value=None):
        result = await health_endpoint()

    assert result["llm_configured"] is False
    assert result["llm_provider"] is None
    assert result["llm_model"] is None


class _Cfg:
    def __init__(self, *, model="m", api_key="k", base_url="https://api.example.com/v1", kind="openai"):
        self.kind = kind
        self.provider = "openai"
        self.base_url = base_url
        self.api_key = api_key
        self.model = model


def test_require_config_accepts_configured():
    cfg = _Cfg()
    assert _require_config(cfg, "对话") is cfg


def test_require_config_rejects_missing_model():
    with pytest.raises(ProviderNotConfigured):
        _require_config(_Cfg(model=""), "对话")


def test_require_config_rejects_missing_key_on_remote():
    with pytest.raises(ProviderNotConfigured):
        _require_config(_Cfg(api_key=""), "对话")


def test_require_config_allows_local_without_key():
    """本机地址（Ollama 等）允许空 Key——口径同老仓库。"""
    cfg = _Cfg(api_key="", base_url="http://127.0.0.1:11434/v1")
    assert _require_config(cfg, "对话") is cfg
