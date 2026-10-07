# -*- coding: utf-8 -*-
"""081-D 接线判据：`/api/models` 三条端点的**出网前守卫**（专项升级 · `回归修复` · 卡 `task-8`）。

## 本文件判什么

判 `app/api/settings_routes.py` 里那三条「按地址拉模型」的端点 —— 它们的 URL 要么来自
**请求体草稿**，要么来自 **`settings` 里已保存的存档**，因此在**出网之前**必须过一遍
`app/core/net_guard.py` 的守卫。判据编号沿用卡内 `C-01`…`C-06`。

## 为什么判「`_fetch_models` 这一个点」就够了（**3/3 调用方**）

实测调用方 = **3**：`probe_models`（`POST /api/models`，草稿）·
`fetch_web_llm_models`（`POST /api/web/llm/models`，草稿 + 存档回落）·
`list_models`（`GET /api/models`，**不带请求体**，读 `settings` 存档）。
守卫**下沉进 `_fetch_models`**（单一 choke point）⇒ 一次覆盖 3/3。
⇒ 判据**逐端点各来一例**（`C-02`/`C-03`），其中 `GET /api/models` 一例**专门构造
「存档地址是云元数据地址」**——只接前两条的写法正是在这一腿上变红。

## 三件**绝不出网**的手段（本文件即使被误放在有网机器上也零出网）

1. **解析一律注桩**：`ssrf.resolve_host` 是**进程级全局** ⇒ 自带 autouse 复位夹具，
   且只经 `monkeypatch` 改（直接赋值会把桩漏给同进程后续用例）。
2. **HTTP 层挂计数器桩**：`settings_routes.httpx.AsyncClient` 换成只**计数**的假件
   ⇒ 判据 `C-01` 的「真到了出网那一步」与 `C-03` 的「拒绝路径**构造次数 = 0**」
   都读这一个数。**这是本文件的核心判别力**：只判「返回了 400」而不判「有没有出网」，
   把守卫挪到出网**之后**照样绿（那正是变异 `M-081-D2`）。
3. **`.env` 一律指到 `tmp_path`**：`GET /api/models` 会经 `_chat_config_or_400()`
   读角色配置，本机 `.env` 有真 Key / 真地址 ⇒ 不隔离就会真打上游。

## ⚠️ 一条**如实登记的已知限制**（⛔ 不得写「已消除」）

本接面的判定与真正的连接是**两次独立解析**——**没有**地址固定（pinning；web 面靠
`PinnedBackend` 把已校验地址钉进本次连接，本面没有等价物）⇒ **TOCTOU 窗口未消除**。
本条目只做「出网前判定」，`TOCTOU` 不属本条目范围，故**不写**任何「地址已绑定 /
窗口已关闭」的断言——写了就是假绿。同理，本文件也**不**断言「守卫在 `pinning` 里
也留痕」（那一侧另开 `081-F` / `task-12` 收口，不在本卡 write_scopes）。

## 判据归属

| 判据 | 用例 |
|---|---|
| C-01 正控（真走到出网） | `test_c081_c01_*`（三条端点各一例） |
| C-02 拒绝腿（逐端点） | `test_c081_c02_refusal_*` |
| C-03 零出网腿 | `test_c081_c03_refusal_never_builds_a_client_*` |
| C-04 逃生门 | `test_c081_c04_*` |
| C-05 既有三条校验逐字不变 | `test_c081_c05_*` |
| C-06 端点面不破 | 由 `backend/scripts/contract_diff.py`（53-53-53）承载，不在本文件 |
| 结构腿（防守卫被搬回调用点/顺序错） | `test_c081_structure_guard_stays_above_the_egress_line` |
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.api import settings_routes
from app.agent.web import ssrf
from app.core import env_store
from app.core.config import settings

from tests.api._sync_client import SyncClient, build_app

SVC_CHAT = "svc_chat"
CHAT_KEY = "sk-chat-secret0001"
BASE_URL = "https://example.invalid/v1"

#: 三条端点各一例的拒绝地址（覆盖 C-02 要求的三条形态）
METADATA_URL = "http://169.254.169.254"
LOOPBACK_URL = "http://127.0.0.1:9527"
FILE_URL = "file:///c:/windows"

_LOGGER_NAME = "app.core.net_guard"

#: 文件导入时抓一份**真**解析器：本文件的存在前提就是它不被真调用（见 autouse 夹具）
_REAL_RESOLVER = ssrf.resolve_host

#: 用来构造「请先填写接口地址」那条既有校验的厂商键（本腿会在用例里把它的预设地址**挖空**）。
#: ⚠️ **实测**：登记表里**所有** `kind="openai"` 厂商都带预设地址（`dashscope` / `openai` …），
#: 而 `probe_models` 在草稿为空时会回落到预设地址 ⇒ 光传空串**到不了**那条分支。
#: 故本腿就地挖空预设地址来构造它 —— 这样判的是**产品那条校验本身**，而不是登记表的现状。
_OPENAI_PRESET_KEY = "dashscope"


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@pytest.fixture(autouse=True)
def _isolate_resolver(monkeypatch):
    """把模块级解析器钉回真实现：一条用例留下的桩不得漏给下一条。

    配套：默认再装一条「任何域名都解析到私网」的桩？**不**——默认桩若生效，
    `C-01` 正控腿会被静默打成拒绝腿（假绿的反面：假红）。所以这里只做**复位**，
    桩由各用例自己按需注入（口径同 `test_web_ssrf_r2.py`）。
    """
    monkeypatch.setattr(ssrf, "resolve_host", _REAL_RESOLVER)


@pytest.fixture(autouse=True)
def _hatch_off(monkeypatch):
    """逃生门**默认关**（出厂档）：逐条用例显式钉一遍，隔离本机 `.env`。"""
    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", False)


@pytest.fixture
def fake_dotenv(tmp_path, monkeypatch) -> Path:
    """`.env` 指到 `tmp_path`：`GET /api/models` 会读角色配置，不隔离就会真打上游。"""
    path = tmp_path / ".env"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(env_store, "ENV_PATH", path)
    assert env_store.ENV_PATH != env_store.ROOT_DIR / ".env", "假 .env 落到了仓库真实路径上"
    return path


@pytest.fixture
def client():
    return SyncClient(build_app(settings_routes.router))


@pytest.fixture
def wired(monkeypatch, fake_dotenv):
    """注册表 + 角色绑定（存档态）；`WEB_LLM_*` 清空，免得本机值混进断言。

    ⚠️ 这里**不动** `_fetch_models`（与 `test_models_4_5_3.py` 的做法相反）：
    本文件判的就是**真函数内部**的守卫，换成桩等于把被测对象删掉 ⇒ 全部假绿。
    """
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
    monkeypatch.setattr(settings, "MODEL_ROLES", _dump({"chat": {"service_id": SVC_CHAT, "model": "m-chat"}}))
    monkeypatch.setattr(settings, "WEB_LLM_BASE_URL", "")
    monkeypatch.setattr(settings, "WEB_LLM_API_KEY", "")
    return settings


class _CountingClient:
    """假 httpx 客户端：**只计数**，绝不出网。

    `C-01` 读它「> 0」证明解析/判定都放行之后**真的走到了出网那一步**；
    `C-03` 读它「== 0」证明拒绝路径**根本没过出网点**。两腿读同一个数 ⇒ 判别力对偶。
    """

    #: 类级计数（每次实例化 +1）——夹具在用例间清零
    built = 0

    #: 真 httpx 模块（夹具注入）：`get` 要抛一个**产品代码认得的**异常，
    #: 才能让端点走既有 503 分支（抛别的会让产品炸穿成 500，判据读到的就不是产品行为了）。
    real = None

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        type(self).built += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:  # noqa: ANN002
        return False

    async def get(self, url, **kwargs):  # noqa: ANN001, ANN003
        # 到这儿就证明产品真的走到了出网点（`C-01` 要读的就是「到了」）。
        # 但**绝不出网**：直接抛一个 httpx 自己认得的错误，让端点按既有分支回 503。
        raise type(self).real.ConnectError(f"判据不许真出网：{url}")


@pytest.fixture
def egress_counter(monkeypatch):
    """把 `settings_routes` 里的 `httpx.AsyncClient` 换成计数桩，并**用例间清零**。

    只换被测模块里的那一个名字（`settings_routes.httpx` 是模块对象，
    直接换属性会污染全进程 ⇒ 用 monkeypatch 换模块属性，退出时自动还原）。
    """

    real_httpx = settings_routes.httpx

    class _HttpxShim:
        """透明代理：**只**把 `AsyncClient` 换成计数桩，其余属性（`TimeoutException` /
        `HTTPError` 等**模块级**异常）原样转发 —— 少转一个，被测函数的 `except` 就会炸成
        `AttributeError`，而那是**桩的**故障，不是产品的（本夹具第一版正是如此，实测抓出）。

        ⚠️ **必须是**实例，不能是类：``_Shim.TimeoutException`` 走的是**类**属性查找，
        `__getattr__` 定义在类里管不到它（只对实例生效）⇒ 透明转发必须由实例承担。
        """

        def __init__(self, real) -> None:  # noqa: ANN001
            self._real = real

        #: 计数桩（被测模块写的是 `httpx.AsyncClient`，实例属性同名即生效）
        AsyncClient = _CountingClient

        def __getattr__(self, name):  # noqa: ANN001
            return getattr(self._real, name)

    _CountingClient.built = 0
    _CountingClient.real = real_httpx
    monkeypatch.setattr(settings_routes, "httpx", _HttpxShim(real_httpx))
    return _CountingClient


def _stub_resolver(*addresses: str):
    """桩解析器：任何域名都解析成给定地址（与 `test_web_ssrf_r2.py` 同款）。"""

    def resolve(host: str) -> list[str]:  # noqa: ANN001
        return list(addresses)

    return resolve


# --------------------------------------------------------------------------
# C-01 · 正控：公网地址 ⇒ 真走到出网（计数 > 0 自证）
# --------------------------------------------------------------------------


def test_c081_c01_post_models_reaches_egress_for_a_public_url(client, wired, egress_counter, monkeypatch):
    """`POST /api/models` 正控：公网存档地址 ⇒ 构造了客户端（真到了出网那一步）。"""
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("93.184.216.34"))
    assert egress_counter.built == 0, "夹具没清零"
    client.post("/api/models", json={"service_id": SVC_CHAT, "base_url": "https://public.example/v1"})
    assert egress_counter.built > 0, "守卫放行了却根本没走到出网 ⇒ 正控失效（判据会恒真）"


def test_c081_c01_post_web_llm_models_reaches_egress_for_a_public_url(client, wired, egress_counter, monkeypatch):
    """`POST /api/web/llm/models` 正控（草稿态）。"""
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("93.184.216.34"))
    client.post(
        "/api/web/llm/models",
        json={"base_url": "https://public.example/v1", "api_key": "sk-draft"},
    )
    assert egress_counter.built > 0, "守卫放行了却根本没走到出网 ⇒ 正控失效"


def test_c081_c01_get_models_env_mode_reaches_egress_for_a_public_url(client, wired, egress_counter, monkeypatch):
    """`GET /api/models` 正控：**无注册表**（手改 `.env` 的老用户）时现场拉一次。"""
    monkeypatch.setattr(settings, "MODEL_SERVICES", "")
    monkeypatch.setattr(settings, "MODEL_ROLES", "")
    monkeypatch.setattr(settings, "MODEL_PROVIDER", "dashscope")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "https://public.example/v1")
    monkeypatch.setattr(settings, "LLM_API_KEY", "sk-flat")
    monkeypatch.setattr(settings, "LLM_MODEL", "m-flat")
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("93.184.216.34"))
    client.get("/api/models")
    assert egress_counter.built > 0, "守卫放行了却根本没走到出网 ⇒ 正控失效"


# --------------------------------------------------------------------------
# C-02 / C-03 · 拒绝腿（**逐端点**各一例）+ 零出网腿
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad_url", [METADATA_URL, LOOPBACK_URL, FILE_URL])
def test_c081_c02_post_models_refuses_with_readable_chinese(client, wired, egress_counter, bad_url):
    """`POST /api/models`：三条形态逐条 ⇒ **400** + 中文可读（不是 500、不是英文内部串）。"""
    resp = client.post("/api/models", json={"service_id": SVC_CHAT, "base_url": bad_url})
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, str) and detail, resp.text
    assert any("\u4e00" <= ch <= "\u9fff" for ch in detail), detail
    assert "拉取模型列表" in detail, f"拒绝文案没说清「为了什么」：{detail}"


@pytest.mark.parametrize("bad_url", [METADATA_URL, LOOPBACK_URL, FILE_URL])
def test_c081_c02_post_web_llm_models_refuses_with_readable_chinese(client, wired, egress_counter, bad_url):
    """`POST /api/web/llm/models`：草稿地址是坏地址 ⇒ 同上。"""
    resp = client.post("/api/web/llm/models", json={"base_url": bad_url, "api_key": "sk-draft"})
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert any("\u4e00" <= ch <= "\u9fff" for ch in detail), detail
    assert "拉取模型列表" in detail, detail


def test_c081_c02_get_models_stored_metadata_address_is_refused(client, wired, egress_counter, monkeypatch):
    """`GET /api/models` **存档态**：把 `settings` 里的地址指到云元数据地址 ⇒ 400 中文可读。

    ⚠️ **本腿是本文件的分水岭**：这条端点**不带请求体**，地址来自存档。
    只把守卫接在「草稿那两条」的写法在这一腿上**照样出网**（红）。
    """
    monkeypatch.setattr(settings, "MODEL_SERVICES", "")
    monkeypatch.setattr(settings, "MODEL_ROLES", "")
    monkeypatch.setattr(settings, "MODEL_PROVIDER", "dashscope")
    monkeypatch.setattr(settings, "LLM_BASE_URL", METADATA_URL)
    monkeypatch.setattr(settings, "LLM_API_KEY", "sk-flat")
    monkeypatch.setattr(settings, "LLM_MODEL", "m-flat")

    resp = client.get("/api/models")
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert any("\u4e00" <= ch <= "\u9fff" for ch in detail), detail
    assert "169.254.169.254" in detail, f"拒绝文案没带上被判掉的地址：{detail}"


def test_c081_c03_refusal_never_builds_a_client_post_models(client, wired, egress_counter):
    """`C-03` **核心判别力**：拒绝路径的 httpx 桩**构造次数 = 0**（一次都没到出网点）。

    只判「返回 400」而不判这一条，把守卫挪到出网**之后**照样绿（= 变异 `M-081-D2` 存活）。
    """
    resp = client.post("/api/models", json={"service_id": SVC_CHAT, "base_url": METADATA_URL})
    assert resp.status_code == 400, resp.text
    assert egress_counter.built == 0, f"拒绝路径仍然构造了 {egress_counter.built} 次客户端 ⇒ 守卫在出网之后"


def test_c081_c03_refusal_never_builds_a_client_post_web_llm_models(client, wired, egress_counter):
    """`C-03`（第二条端点）：同上。"""
    resp = client.post("/api/web/llm/models", json={"base_url": LOOPBACK_URL, "api_key": "sk-draft"})
    assert resp.status_code == 400, resp.text
    assert egress_counter.built == 0, f"拒绝路径仍然构造了 {egress_counter.built} 次客户端 ⇒ 守卫在出网之后"


def test_c081_c03_refusal_never_builds_a_client_get_models_stored(client, wired, egress_counter, monkeypatch):
    """`C-03`（存档态，`GET /api/models`）：同上 —— 这是 3/3 覆盖的最后一条腿。"""
    monkeypatch.setattr(settings, "MODEL_SERVICES", "")
    monkeypatch.setattr(settings, "MODEL_ROLES", "")
    monkeypatch.setattr(settings, "MODEL_PROVIDER", "dashscope")
    monkeypatch.setattr(settings, "LLM_BASE_URL", METADATA_URL)
    monkeypatch.setattr(settings, "LLM_API_KEY", "sk-flat")
    monkeypatch.setattr(settings, "LLM_MODEL", "m-flat")

    resp = client.get("/api/models")
    assert resp.status_code == 400, resp.text
    assert egress_counter.built == 0, f"存档态拒绝路径仍然构造了 {egress_counter.built} 次客户端"


# --------------------------------------------------------------------------
# C-04 · 逃生门腿：开 ⇒ 放行（真走到出网）且留 WARNING
# --------------------------------------------------------------------------


def test_c081_c04_hatch_on_allows_and_warns(client, wired, egress_counter, monkeypatch, caplog):
    """逃生门开：坏地址**放行** ⇒ 真走到出网（计数 > 0），且本模块 logger 留 WARNING。"""
    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", True)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        resp = client.post("/api/models", json={"service_id": SVC_CHAT, "base_url": METADATA_URL})
    assert resp.status_code != 400, f"逃生门打开时不该被守卫拒：{resp.text}"
    assert egress_counter.built > 0, "逃生门打开却没走到出网 ⇒ 判的可能是别的分支"
    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(records) == 1, [r.getMessage() for r in records]
    assert records[0].levelno == logging.WARNING
    assert "逃生门" in records[0].getMessage()


def test_c081_c04_hatch_off_refuses_and_writes_no_warning(client, wired, egress_counter, caplog):
    """成对反控：逃生门关（出厂档）⇒ 拒 + 本 logger **一条记录都没有**。"""
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        resp = client.post("/api/models", json={"service_id": SVC_CHAT, "base_url": METADATA_URL})
    assert resp.status_code == 400, resp.text
    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert records == [] and egress_counter.built == 0


# --------------------------------------------------------------------------
# C-05 · 既有三条校验：**文案与状态码逐字不变**（守卫排其后）
# --------------------------------------------------------------------------


def test_c081_c05_existing_three_checks_are_verbatim(client, wired, egress_counter, monkeypatch):
    """`_require_openai` 的三条原有校验：参数化对拍**文案 + 状态码**逐字不变。

    ⚠️ 这三条都**不**该走到守卫：它们排在新守卫**之前**（非 openai 系 / 空地址 / 空 Key）
    ⇒ 顺便把「守卫没把既有分支挤掉」也钉住（`egress_counter.built == 0`）。
    """
    resp = client.post("/api/models", json={"provider": "claude", "api_key": "sk-x"})
    assert (resp.status_code, resp.json()["detail"]) == (400, "该厂商不支持在线拉取模型列表，请手动填写型号")

    # ⚠️ 「空 base_url」这条**不能**靠给 dashscope 传空串来构造（实测）：草稿空 ⇒
    # `probe_models` 会回落到 `PROVIDER_PRESETS["dashscope"]["base_url"]`（非空）⇒ 走不到那条分支。
    # 故把该厂商的预设地址就地挖空，让「草稿空 + 预设空」同时成立（判的是产品那条校验本身）。
    from app.core.config import PROVIDER_PRESETS

    preset = PROVIDER_PRESETS[_OPENAI_PRESET_KEY]
    monkeypatch_original = preset.get("base_url")
    preset["base_url"] = ""
    try:
        resp = client.post("/api/models", json={"provider": _OPENAI_PRESET_KEY, "api_key": "sk-x"})
    finally:
        preset["base_url"] = monkeypatch_original
    assert (resp.status_code, resp.json()["detail"]) == (400, "请先填写接口地址")

    resp = client.post("/api/models", json={"provider": "dashscope", "base_url": BASE_URL})
    assert (resp.status_code, resp.json()["detail"]) == (400, "请先填写 API Key")

    assert egress_counter.built == 0, "既有校验分支不该走到出网"


def test_c081_c05_web_llm_two_checks_are_verbatim(client, wired, egress_counter):
    """`POST /api/web/llm/models` 的两条原有校验也逐字不变（同一次提交里先后缀，判据对称）。"""
    resp = client.post("/api/web/llm/models", json={})
    assert (resp.status_code, resp.json()["detail"]) == (400, "请先填写接口地址")

    resp = client.post("/api/web/llm/models", json={"base_url": BASE_URL})
    assert (resp.status_code, resp.json()["detail"]) == (400, "请先填写 API Key（对应平台的 API Key）")

    assert egress_counter.built == 0, "既有校验分支不该走到出网"


# --------------------------------------------------------------------------
# 结构腿：守卫必须**在出网点之上**（源码序），且**只有一处**判定入口
# --------------------------------------------------------------------------


def test_c081_structure_guard_stays_above_the_egress_line():
    """源码序腿：`_fetch_models` 体内 `assert_outbound_url_safe` 必须**早于**`AsyncClient(`。

    **为什么必须判源码序而不只判行为**：行为腿（`C-03`）能抓「挪到出网之后」，
    但抓不到「守卫写在函数**末尾**、而出网在更早的 try 里」这类**同函数内的顺序错**；
    顺序是`C-03`的语义前提，单列一条最便宜。判的是**文件**（交付物），不是内存 AST。
    """
    source = Path(settings_routes.__file__).read_text(encoding="utf-8")
    body = source.split("async def _fetch_models(", 1)[1].split("\ndef _require_openai(", 1)[0]
    guard_at = body.index("assert_outbound_url_safe(")
    egress_at = body.index("AsyncClient(")
    assert guard_at < egress_at, "守卫跑到出网点之后了"
    assert body.count("assert_outbound_url_safe(") == 1, "守卫应当只有一处调用"


def test_c081_structure_all_three_callers_go_through_the_guarded_choke_point():
    """接线覆盖腿：`_fetch_models` 的调用方**恰好 3 个**，且三个端点函数都经它出网。

    ⚠️ **它钉的不是「有没有 3 行调用」而是「有没有端点绕过它自己去建 httpx 客户端」**：
    若某端点另开一条出网路，本腿（经源码扫描）与 `C-01/C-02` 的逐端点腿会同时红。
    """
    source = Path(settings_routes.__file__).read_text(encoding="utf-8")
    assert source.count("await _fetch_models(") == 3, "调用方不再是 3 个 ⇒ 接线面变了，判据需同步复核"
    for endpoint in ("async def probe_models(", "async def fetch_web_llm_models(", "async def list_models("):
        body = source.split(endpoint, 1)[1].split("\n@router.", 1)[0]
        assert "_fetch_models(" in body, f"{endpoint} 没走 choke point"
