"""A17：缓存**换档必须失效**（推理等级回流；开工包 §3.4 与 §5）。

**为什么单列一个文件**：`tests/cache/test_cache.py` 测的是 9 成分键的纯函数面；
本条要的是**端到端**：同一个问题连问三次，中间只改思考档位，
第三次**必须**重跑检索 + 重跑模型（**不得**命中第一次落下的缓存条目）。

**为什么必须失效**：缓存存的是「那一整轮当时的结果」；思考开与关，
厂商返回的正文本身就可能不同（思考内容还会重新分配输出预算）。
不换键就等于把一条**当前配置下拿不到**的回答交出去 —— 与 `graph` 子对象是同一条道理。

**夹具**：`client` / `db` 复用既有夹具（`test_chat_contract.client` 是 ASGI 直连 + 真测试库）；
「有材料」那一态由**关键词路**在真测试库上产出（`seed_document`，同 `test_semantic_cache_http`）。
本文件额外**自带**一条清缓存的 autouse —— `tests/cache` 没有 `tests/api` 的那八条钉子。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.api import chat_routes
from app.cache import entry as entry_mod
from app.core.config import settings
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401 —— client 是夹具
from tests.retrieval.conftest import seed_document

pytestmark = pytest.mark.usefixtures("db")

QUESTION = "缓存的思考档位换档测试"
MATERIAL = "换档测试用的资料：思考档位决定回答的推理深度。"

MIMO_SERVICE = {
    "id": "svc_c",
    "name": "mimo",
    "provider": "mimo",
    "base_url": "https://x.invalid/v1",
    "api_key": "sk-x",
    "models": ["mimo-v2.5"],
    "model_roles": {"mimo-v2.5": ["chat"]},
    "for_roles": ["chat"],
}
CLOSED_LEVEL = '{"param":"thinking.type","value":"disabled","model":"mimo-v2.5"}'
OPEN_LEVEL = '{"param":"thinking.type","value":"enabled","model":"mimo-v2.5"}'


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class _FakeStream:
    """最小流：按片段产 ``text_delta``（与 CubeLoop 的事件形状一致）。"""

    def __init__(self, parts: list[str]) -> None:
        self._parts = parts

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for part in self._parts:
            yield SimpleNamespace(type="text_delta", delta=part)


class _CountingModel:
    """零出网回答替身：记调用次数；正文在**每次安装时**给定（用来区分两轮回答）。

    ⚠️ 形状要做全（`generate` / `stream` / `.spec`）：`settings.AGENT_MODE` 是
    **跨测试文件遗留状态**，一旦本轮走上 Agent 路径，缺 `.spec` 会让替身抛
    `AttributeError` ⇒ 被产品兜底渲染成 `error` 帧 ⇒ 症状（没有 done 帧）
    与「缓存没命中」**长得一模一样**（2026-09-20 实测踩过）。
    """

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def generate(self, messages, *args, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append({"mode": "generate", "messages": messages})
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.reply)], stop_reason="end_turn"
        )

    async def stream(self, messages, *args, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append({"mode": "stream", "messages": messages})
        return _FakeStream([self.reply])

    @property
    def spec(self):
        return SimpleNamespace(id="counting-fake")

    @property
    def count(self) -> int:
        """**回答生成**的调用次数（只数 `stream`）。"""
        return len([call for call in self.calls if call["mode"] == "stream"])

    @property
    def summarizes(self) -> int:
        """非流式调用次数（压缩摘要 / 记忆抽取 / 澄清追问 —— 与本条断言无关）。

        ⚠️ **为什么必须分开数**（2026-09-20 实测）：`tests/api/test_chat_contract.py` 的
        `client` 夹具会把 `QUERY_CLARIFY_ENABLE` 钉成 `True`，而它是**跨文件共享**的
        `settings` 单例 ⇒ 本文件里那几条用例实际**也会**走一次澄清判定调用。
        把「总调用数」当「回答调用数」用，就会在跨文件连跑时把一次澄清误读成
        「缓存没命中」。
        """
        return len([call for call in self.calls if call["mode"] == "generate"])

    @property
    def answer_calls(self) -> int:
        return self.count


@pytest.fixture(autouse=True)
def _clean_cache():
    """**本文件自带**的缓存清理（`tests/cache` 没有 `tests/api` 那八条 autouse 钉子）。"""
    from app.cache import reset_cache, reset_failure_counts

    reset_cache()
    reset_failure_counts()
    yield
    reset_cache()
    reset_failure_counts()


@pytest.fixture(autouse=True)
def _zero_outbound(monkeypatch):
    """本文件自带的三条隔离：重排 / 图通道 / 记忆一律关（缺一条就静默真出网）。

    ⚠️ **还要把 `AGENT_MODE` 钉成 `off`**（2026-09-20 实测）：它是**跨测试文件遗留状态**
    ——`tests/api` 里改过它的用例不会还原到「本文件期望的默认档」，而 Agent 轮
    **结构性不进缓存**（`cache.CACHEABLE_AGENT_MODES == ("off",)`）⇒ 那条「同问题第二问
    必须命中缓存」的判据会在跨文件连跑时假红。本文件依赖什么就自己钉什么，
    不要继承环境。
    """
    from app.retrieval.graph import reset_counters

    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", False)
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", False)
    monkeypatch.setattr(settings, "MEMORY_ENABLE", False)
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", False)
    monkeypatch.setattr(settings, "ROUTING_MODE", "regex")
    monkeypatch.setattr(settings, "AGENT_MODE", "off")
    reset_counters()


@pytest.fixture
def mimo(monkeypatch):
    """对话绑定 = mimo-v2.5（**已登记**型号），语义缓存开，档位空。"""
    monkeypatch.setattr(settings, "MODEL_SERVICES", _dump([MIMO_SERVICE]))
    monkeypatch.setattr(
        settings, "MODEL_ROLES", _dump({"chat": {"service_id": "svc_c", "model": "mimo-v2.5"}})
    )
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", True)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_TTL_SECONDS", 86400)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_MAX_ENTRIES", 256)
    monkeypatch.setattr(settings, "CHAT_REASONING", "")
    return settings


def _install(monkeypatch, reply: str) -> _CountingModel:
    """装回答替身 + 路由分类器（`internal_db` ⇒ 走真检索 ⇒ `kind == normal` 才可缓存）。

    ⚠️ 辅助链路（澄清 / 压缩 / 记忆）在 `tests/api` 里由 `pin_aux_model_delegates`
    跟随替身；本文件在 `tests/cache/`，**没有那条钉子** —— 所以这里一并把
    `resolve_aux_model` 也钉上（否则压缩 / 记忆会去真装配、真出网）。
    """
    model = _CountingModel(reply)
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: model)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db")
    )
    return model


async def _seed(db) -> None:
    await seed_document(db, [MATERIAL], filename="换档测试.md")


async def _ask(client, message: str) -> tuple[dict, str]:
    """打一轮流式问答，返回 ``(done 帧, token 帧拼起来的正文)``。

    ⚠️ ``done`` 帧**不带正文**（契约九字段里没有 message）；正文只能从 ``token`` 帧拼。
    """
    resp = await client.post("/api/chat/stream", json={"message": message})
    assert resp.status_code == 200, resp.text
    events = parse_sse(resp.text)
    done = next((event for event in events if event["type"] == "done"), None)
    assert done is not None, f"流里没有 done 帧：{resp.text[:400]}"
    body = "".join(event.get("text", "") for event in events if event["type"] == "token")
    return done, body


def _key(*, question: str):
    return chat_routes.build_cache_key(
        question=question,
        filters=None,
        agent_mode="off",
        skill_ids=[],
        memory_text="",
        history=[],
        images=[],
    )


# --- A17 主断言 -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a17_reasoning_change_changes_key_and_defeats_cache(client, db, monkeypatch, mimo):
    """关 → 开：**键不同** **且** 第三次**不返回**第一条答案（模型真的又跑了一次）。"""
    await _seed(db)
    monkeypatch.setattr(settings, "CHAT_REASONING", CLOSED_LEVEL)
    first_model = _install(monkeypatch, "第一条答案（关档）")

    # ① 第一次提问：真检索 + 真落库 + 写回缓存（写回是 fire-and-forget，用例自己等）
    first_done, first_text = await _ask(client, QUESTION)
    await chat_routes.drain_cache_tasks()
    assert first_done["kind"] == "normal", first_done
    assert first_text == "第一条答案（关档）", first_text
    assert first_model.count == 1

    # ② 同问题、同档位 ⇒ **必须命中**（证明缓存真的在工作，否则后面的断言没有判据）
    same_done, same_text = await _ask(client, QUESTION)
    assert first_model.count == 1, "同档位同问题没有命中缓存 ⇒ 本用例失去判据"
    assert same_text == first_text, f"命中轮正文与未命中轮不同：{same_text!r} != {first_text!r}"
    assert same_done["kind"] == "normal"

    # ③ 换档：开档的指纹必须不同（断言的是**实际会发出去的参数**）
    monkeypatch.setattr(settings, "CHAT_REASONING", OPEN_LEVEL)
    assert entry_mod.retrieval_fingerprint()["reasoning"] == {"thinking": {"type": "enabled"}}

    # ④ 再问同一问题：**必须**重跑模型，且新答案与旧答案不同
    second_model = _install(monkeypatch, "第四条答案（开档）")
    changed_done, changed_text = await _ask(client, QUESTION)
    assert second_model.count == 1, "换档后没有重跑模型 ⇒ 命中了旧档答案（A17 失败）"
    assert changed_done["kind"] == "normal"
    assert changed_text == "第四条答案（开档）", changed_text
    assert changed_text != same_text


@pytest.mark.asyncio
async def test_a17_key_differs_across_the_three_states(client, db, monkeypatch, mimo):
    """同问题、同模型、只差档位 ⇒ **三个键两两不同**（纯键面，不依赖缓存实现细节）。

    三种状态互不相等，这就是「缓存换档失效」的全部内容：

    - 没设过档位（``{}``，请求体里不塞任何键）；
    - 显式 ``enabled``（``{"thinking":{"type":"enabled"}}``）；
    - 显式 ``disabled``。

    ⚠️ 「没设过」与「显式设成厂商默认档」**不能**当作同一条：后者是用户的选择，
    请求体上真的会多一个 ``thinking`` 键；把它们视为等价，会让「用户显式开思考」
    命中「用户没表态」那一轮的缓存。
    """
    await _seed(db)
    _install(monkeypatch, "档位键面")

    absent = _key(question=QUESTION)
    assert absent is not None, "档位为空时键应当算得出来"
    assert entry_mod.retrieval_fingerprint()["reasoning"] == {}

    monkeypatch.setattr(settings, "CHAT_REASONING", OPEN_LEVEL)
    enabled = _key(question=QUESTION)
    assert entry_mod.retrieval_fingerprint()["reasoning"] == {"thinking": {"type": "enabled"}}

    monkeypatch.setattr(settings, "CHAT_REASONING", CLOSED_LEVEL)
    disabled = _key(question=QUESTION)
    assert entry_mod.retrieval_fingerprint()["reasoning"] == {"thinking": {"type": "disabled"}}

    assert len({absent, enabled, disabled}) == 3, "三种档位的缓存键必须两两不同"


@pytest.mark.asyncio
async def test_a17_unreadable_chat_config_means_no_cache(client, db, monkeypatch, mimo):
    """`chat_config` 读不到 ⇒ **本轮不缓存**（键算不出来），**不是**「按空档位缓存」。"""
    monkeypatch.setattr(settings, "MODEL_SERVICES", "")
    monkeypatch.setattr(settings, "MODEL_ROLES", "")
    for key in ("MODEL_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL"):
        monkeypatch.setattr(settings, key, "")

    assert _key(question=QUESTION) is None, "配置读不到时必须收敛成「本轮不缓存」"
    with pytest.raises(ValueError):
        entry_mod.retrieval_fingerprint()
