"""``tests/api`` 的公共夹具（阶段 3 · 条目 3.1；2026-09-18 扩到入库层与重排）。

**为什么需要它**：3.1 把检索真的接进了 chat 链路。本机 ``.env`` 里配着真的 ``EMBED_*``
与 ``MODEL_ROLES.embed`` ⇒ 不加隔离时，**任何**走 ``/api/chat`` 的用例都会真打上游
``/embeddings``，并让 ``get_vector_store()`` 去连本机 Qdrant（``QDRANT_URL``）。

这与统筹 §3.4「单测不连真模型 / 真服务」冲突，也会让契约用例变慢、变脆（依赖网络与配额）。

**八个 autouse 夹具，各管一层**（2026-09-18 补齐 —— 独立复核发现原来只有检索层被隔离，
入库层与重排是「会静默真出网、且禁网日志也盖不住」的漏网：失败都被产品代码吞成
``vectorized=false`` / 「警告 + 回落」，所以有网、没网**两种情况都绿**；
2026-09-19 条目 5.2 补第 6 个 ``pin_semantic_cache_off``、条目 5.3 补第 7 个
``pin_graphrag_off``；2026-09-20 「推理等级登记表回流」补第 8 个
``pin_aux_model_delegates``；**2026-09-21 专项工具调用第 0 批补第 9 个
``pin_agent_mode_off``**；
**2026-09-23 专项工具升级-2 · R2-05 再补一条 ``pin_tool_artifact_dir``**；
**2026-09-28 缺陷清单-2 阶段七（`回归修复`）再补一对 —— ``pin_settings_put_keys``（统一键清单逐个 pin）+ ``restore_settings_singleton``（单例 diff 快照兜底）**）：

- ``isolate_retrieval_vectors``：**检索层**（``app.retrieval.vector`` 的两个模块级薄包装）
  → ``FakeEmbedding(dim=16)`` + 空结果假库（并记录收到的 ``filters``）；
- ``isolate_ingest_vectors``：**入库层**（``app.pipeline.ingest`` 的两个薄包装；``回归修复`` 家族）
  → embed/store **快速失败**，与 ``test_documents_contract.py::_isolate_vector`` 同口径
  ⇒ ``vectorized`` 恒 ``false``，既有断言不变。**为什么必须补**：``test_documents_endpoints.py``
  （upload / reindex / delete）**没有自己的隔离**，而 ``ingest.index_chunks()`` 与
  ``purge_document → get_vector_store()`` 是**无条件**调用的——本机上游可用时会真写真删本机 Qdrant，
  且失败被吞成 ``vectorized=false``，有网、没网**两种情况都绿**。
  （``test_documents_upload_image.py`` 目前两条用例都在**解析阶段**就 400、到不了入库，属**预防性**覆盖：
  日后加一条能解析成功的图片用例就会漏，所以一并纳入本夹具。）
- ``pin_rerank_off``：**重排**是检索层仅剩的出网点（``app.retrieval.rerank._call_rerank``）。
  ⚠️ **2026-09-18 起这句的前提变了**：`回归修复` 落地后，启动时的 `migrate_legacy_rerank`
  会把存量 `RERANK_MODEL` 迁成 `MODEL_ROLES.rerank` ⇒ 本机 `.env`（`RAG_ENABLE_RERANK=true`）
  **会在真启动时真的生效并出网**。所以这条钉子不再只是「防万一」，而是**套件不出网的必需条件**：
  用例内一律钉 ``False``（与 ``tests/eval`` 钉默认态同一口径）。

- ``pin_graphrag_off``：**图谱检索**（阶段 5 · 条目 5.3）→ 开关钉 `False` + 清零模块计数；
- ``pin_aux_model_delegates``：**辅助链路的模型装配**（推理等级回流）→ 让
  `resolve_aux_model()` 跟随 `resolve_chat_model()`。⚠️ **它不是「防本机 .env」的那种钉子**
  （那七条是），它的语义是**还原改动前的接线**：接推理参数之前，端点把同一个 `model`
  同时传给 `clarify_model` / `compaction_model` / `memory_model`。没有它，既有那三十处
  打在 `resolve_chat_model` 上的替身会**够不着**辅助链路 ⇒ 真装配、真出网。
  **A12–A15 用 `real_model_builders` 绕过它**（那些用例要真装配），A19 反过来钉它生效。

**关键词路不隔离**：它只读测试库，而 ``db`` 夹具每例已清表（``documents`` / ``chunks``），
本来就是确定性的——「库内零命中 ⇒ direct / refuse」这类既有断言因此保持原状。
要造「库内有资料」的用例，自己往 ``db`` 里插行即可（``tests/retrieval/conftest.py`` 有现成助手）。
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

#: `pin_aux_model_delegates` 钉住的那个替身（哨兵对象，**只作身份比较**）。
#: A12–A15 的用例用 :func:`real_model_builders` 换掉它（那些用例要跑真装配）。
AUX_DELEGATED = object()

#: `PUT /api/settings` 与 `PUT /api/skills` 能写进**进程级 `settings` 单例**的全部键
#: （`回归修复` 的「统一键清单」· 阶段七）。来源 = 两个端点**现读**：
#: `settings_routes.py` 的 `updates["KEY"]` 赋值（:484/:486/:487/:540/:551/:598/:603/:605）
#: ＋ `_resolve_group` 的 keys 映射（chat/embed 两组共 8 键，:529/:536）
#: ＋ `_resolve_translate` / `_resolve_reasoning` 的 dict 字面（:614/:643/:674）
#: ＋ `config.dump_registry` 的 2 键（:793-796）；`skill_routes.py` 的 2 键（:144/:190）。
#: ⚠️ **与端点的漂移由机器拦**：`tests/api/test_bug148_settings_singleton_pin.py::test_a1_*`
#: 用 AST 扫两个端点模块（**五类写入形态**：`updates["K"]=` / `_resolve_group` 第三实参 / `return {"K":…}`
#: / `settings.K=` / **`updates.update({"K":…})` 字面**）断言「清单 == 端点可写键」⇒ 端点加键而清单没跟，当场红。
#: 🔴 第五类形态 = A 路只读对抗审计实测补口（**REAL-148-1**，2026-09-28）。
SETTINGS_PUT_KEYS: tuple[str, ...] = (
    # settings_routes.py · 注册表与重排开关（:484/:486/:487 + config.dump_registry）
    "MODEL_SERVICES",
    "MODEL_ROLES",
    "RAG_ENABLE_RERANK",
    # settings_routes.py · chat / embed 两组（_resolve_group 的 keys 映射，:529/:536）
    "MODEL_PROVIDER",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_API_KEY",
    "EMBED_PROVIDER",
    "EMBED_BASE_URL",
    "EMBED_MODEL",
    "EMBED_API_KEY",
    # settings_routes.py · 单键段 + agent 三键（:540/:551/:598/:603/:605）
    "VISION_MODEL",
    "WORKSPACE_ROOT",
    "AGENT_MODE",
    "AGENT_MAX_ROUNDS",
    "AGENT_ENABLE_AUDIT",
    # settings_routes.py · translate / reasoning（dict 字面 :614/:643/:674）
    "DEEPL_API_KEY",
    "CHAT_REASONING",
    "WEB_ENABLE",
    "WEB_CHANNEL",
    "WEB_SEARCH_PROVIDER",
    "WEB_SEARCH_API_KEY",
    "WEB_MAX_RESULTS",
    "WEB_FETCH_TIMEOUT",
    "WEB_LLM_PROVIDER",
    "WEB_LLM_MODEL",
    "WEB_LLM_BASE_URL",
    "WEB_LLM_API_KEY",
    "WEB_LLM_MAX_SEARCHES",
    "WEB_LLM_SEARCH_TOOL",
    # skill_routes.py · PUT /api/skills（:144/:190/:198-199）
    "SKILL_DIR",
    "MCP_SERVERS",
)


class EmptyVectorStore:
    """空结果假向量库：**只记录调用参数**，不连任何服务。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def query(self, vector, limit: int = 5, filters=None):  # noqa: ANN001 —— 与协议同形
        self.calls.append({"vector": list(vector), "limit": limit, "filters": filters})
        return []


class _NoEmbed:
    """入库层假 embed：**快速失败**（口径同 ``test_documents_contract.py::_isolate_vector``）。"""

    dim = 4

    async def embed_texts(self, texts):  # noqa: ANN001
        raise RuntimeError("测试隔离：不调用真实 embedding")


class _NoStore:
    """入库层假向量库：**快速失败**，不连 Qdrant。"""

    def ensure_collection(self, dim):  # noqa: ANN001
        raise RuntimeError("测试隔离：不连 Qdrant")

    def upsert(self, points):  # noqa: ANN001
        raise RuntimeError("测试隔离：不连 Qdrant")

    def delete(self, ids):  # noqa: ANN001
        raise RuntimeError("测试隔离：不连 Qdrant")


@pytest.fixture(autouse=True)
def isolate_retrieval_vectors(monkeypatch):
    """把**检索层**的向量路钉成假件（autouse：``tests/api`` 全部用例都不连真服务）。"""
    from app.retrieval import vector as retrieval_vector
    from app.vector.embed import FakeEmbedding

    store = EmptyVectorStore()
    monkeypatch.setattr(
        retrieval_vector, "get_embedding_client", lambda *, fake=False: FakeEmbedding(dim=16)
    )
    monkeypatch.setattr(retrieval_vector, "get_vector_store", lambda **kwargs: store)
    return store


@pytest.fixture(autouse=True)
def isolate_ingest_vectors(monkeypatch):
    """把**入库层**的向量写入钉成假件（upload / import-file / reindex 都不真打上游）。"""
    from app.pipeline import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "get_embedding_client", lambda **kwargs: _NoEmbed())
    monkeypatch.setattr(ingest_mod, "get_vector_store", lambda **kwargs: _NoStore())


@pytest.fixture(autouse=True)
def pin_rerank_off(monkeypatch):
    """用例内钉住重排开关（默认态）：本机 ``.env`` 的 ``true`` 会盖过 ``Settings`` 默认值。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", False)


@pytest.fixture(autouse=True)
def pin_web_off(monkeypatch):
    """用例内钉住联网开关（阶段 4 · 条目 4.1）。

    **为什么必须钉**：本机 ``.env`` 里 ``WEB_ENABLE=true`` 且配了真搜索服务商（tavily），
    Agent 轮一旦把 ``web_search`` 放进模型清单，用例就会**真出网**——违反统筹 §3.4
    「单测不连真模型 / 真服务」，而且慢、脆、烧配额。默认关掉之后，只有显式
    ``monkeypatch.setattr(settings, "WEB_ENABLE", True)`` 的用例才会走到联网分支，
    而且那几条也必须自己注入假的 search/fetch 实现（见 ``tests/agent/test_web_*``）。

    与 ``pin_rerank_off`` 同型：都是「本机 ``.env`` 会盖过 ``Settings`` 默认值」的钉子。
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "WEB_ENABLE", False)


@pytest.fixture(autouse=True)
def pin_mcp_off(monkeypatch):
    """用例内钉住 MCP 三键为空（第 2 批 ③）：零许可 = default-deny = 不发现、零子进程。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "MCP_SERVERS", "")
    monkeypatch.setattr(settings, "MCP_ALLOW_SERVERS", "")
    monkeypatch.setattr(settings, "MCP_ALLOW_EGRESS", "")


@pytest.fixture(autouse=True)
def pin_memory_off(monkeypatch):
    """用例内钉住记忆开关（阶段 5 · 条目 5.1；默认态 = 关）。

    **为什么必须钉**：`MEMORY_ENABLE=true` 时，一轮成功回答收口会**多一次模型调用**
    （记忆抽取）并**多落一行用量**（复用 ``kind="chat"`` + ``meta={"source":"memory_extract"}``）
    ——既有用例里有按行数断言的（如 ``test_usage_export_4_5_4`` 的「一次成功回答 = 一条
    chat 用量行」），本机 ``.env`` 一旦写上这个键就会把它们打红。

    与 ``pin_rerank_off`` / ``pin_web_off`` 同型：**防的是本机 .env 覆盖 Settings 默认值**；
    开态用例自己 ``monkeypatch.setattr(settings, "MEMORY_ENABLE", True)`` 即可覆盖本钉子。
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "MEMORY_ENABLE", False)


@pytest.fixture(autouse=True)
def pin_semantic_cache_off(monkeypatch):
    """用例内钉住语义缓存开关（阶段 5 · 条目 5.2；默认态 = 关）+ **清空进程内缓存表**。

    **为什么必须钉**：缓存是**进程内**状态（``app/cache/store.py`` 的模块级 ``OrderedDict``），
    本机 ``.env`` 一旦写上 ``SEMANTIC_CACHE_ENABLE=true``，任何走 ``/api/chat`` 的用例都会
    开始复用上一条用例算出的答案 ⇒ 「同一问题连问两次得到不同回答」这类既有断言会**随机变红**
    （本仓踩过「进程全局计数 + 逐条断言」的同类坑）。

    **为什么还要清表**：``monkeypatch`` 会在用例结束时还原开关，但**缓存条目本身不会消失**
    ——被测代码只要在开态下写过一条，它就会跨用例泄漏；而键里含 ``history`` 指纹与
    ``memory_text``，泄漏的那条什么时候被命中几乎不可预测。清表把这件事变成确定的。

    与 ``pin_rerank_off`` / ``pin_web_off`` / ``pin_memory_off`` 同型；开态用例自己
    ``monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", True)`` 覆盖本钉子，
    **并且要自己再调一次 ``app.cache.reset_cache()``**（写清「开态用例自己负责清」）。
    """
    from app.cache import reset_cache, reset_failure_counts
    from app.core.config import settings

    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", False)
    reset_cache()
    reset_failure_counts()
    yield
    # 用例结束后再清一次：开态用例写进去的条目不带开关状态，必须显式清掉
    reset_cache()
    reset_failure_counts()


@pytest.fixture(autouse=True)
def pin_graphrag_off(monkeypatch):
    """用例内钉住图谱检索开关（阶段 5 · 条目 5.3；默认态 = 关）+ **清零模块内计数**。

    **为什么必须钉**：`app/retrieval/graph.py` 的通道会**多查几次库**（别名 SQL + 一跳
    邻居）并把「不在主命中里」的块并进材料快照 ⇒ 任何走 ``/api/chat`` 的用例里，
    「材料条数」「hits 计数」「refs 顺序」这类**既有断言**都会随之漂移；本机 ``.env``
    一旦写上 `GRAPHRAG_ENABLE=true`，那是整片套件随机变红。

    **为什么还要清计数**：``failure_counts()`` 读的是**模块级**字典（可见性设计：不加端点、
    不加帧键）。``monkeypatch`` 会在用例结束时还原开关，但计数**不会自己消失**——
    被测代码只要在开态下跑过一轮，后面任何一条按计数断言的用例都会读到脏值
    （本仓踩过「进程全局计数跨用例泄漏」）。**开态用例自己也要在开头调一次**
    ``app.retrieval.graph.reset_counters()``（写清「开态用例自己负责清」）。

    与 ``pin_rerank_off`` / ``pin_web_off`` / ``pin_memory_off`` / ``pin_semantic_cache_off``
    同型；开态用例自己 ``monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", True)`` 覆盖本钉子。
    """
    from app.core.config import settings
    from app.retrieval.graph import reset_counters

    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", False)
    reset_counters()
    yield
    # 用例结束后再清一次：开态用例跑过之后计数里留着痕迹，必须显式清掉
    reset_counters()


@pytest.fixture(autouse=True)
def pin_aux_model_delegates(monkeypatch):
    """把**辅助链路**的模型装配钉成「跟随 `resolve_chat_model`」。

    **为什么需要它**：思考参数只作用于最终答案（`answer.build_chat_model()`），
    所以其余五处（Agent 决策 / 澄清 / 压缩 / 记忆抽取 / 笔记生成）改走
    `answer.build_aux_model()`，端点里由 `chat_routes.resolve_aux_model()` 注入。
    而本目录既有的**大约十个文件、三十处**替换是钉在 `resolve_chat_model` 上的
    （`monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: fake)`）；
    没有这条钉子，它们的**辅助链路会绕过替身去真装配**——本机 `.env` 里
    `MODEL_ROLES.chat` 指着真厂商且带 Key，那就不只是「测不准」，而是**真出网 + 烧配额**。

    **它为什么不是「把测试钉进实现细节」**：本钉子的语义 = **改动前的行为**
    ——接推理参数之前，端点把同一个 `model` 同时传给 `clarify_model` / `compaction_model` /
    `memory_model`（见 `chat_routes` 的端点注释），所以「辅助链路用回答链路那个模型」
    正是既有的、被断言的语义。本条目只把「带思考参数的那一个」区分出去，
    测试侧的等价表达就是让 aux 跟随 chat（**而不是**跟随真实配置）。

    ⚠️ **A12–A15 必须绕过本钉子**（它们要的是**真装配 + 真 Provider**，只在发送边界换假传输）：
    用 :func:`real_model_builders` 就地换回真函数；A19 反过来**断言本钉子生效**
    （`resolve_aux_model()` 与 `resolve_chat_model()` 返回**同一个对象**，且两个真装配
    函数调用计数为 0）。

    ⚠️ **为什么每次调用都重钉一次，而不是 setup 时抓一个绑定**（2026-09-20 实测）：
    本目录是**共享会话**，`chat_routes.resolve_chat_model` 这个模块属性会被上游用例
    改写成真函数（既有的、与本条目无关的泄漏；本条目之前**没有任何用例**依赖它，
    所以一直没人发现）。一旦它在**本钉子 setup 之后**被改写，任何「setup 时抓一次」的
    写法都会静默退化——`aux` 返**真装配出来的 BoundModel**，A19 当场红，
    而且**只在跨文件连跑时**出现（单文件跑不出来，本文件为这条踩了整整一轮）。
    ⇒ 就在这里**每次调用重钉一次**（幂等、只写模块属性、不碰磁盘），
    用两条纪律兜底：**不放行 `None`**（否则辅助链路会自己去真装配）、
    **不覆盖 `answer.build_*` 真函数**（那是 :func:`real_model_builders` 的退出通道）。
    """
    from app.api import chat_routes
    from app.generation import answer as answer_mod

    def _aux_follows_chat():
        """每次调用都看**当前**绑定，并按两条纪律分派（不写模块属性，无状态）。"""
        entry = chat_routes.resolve_chat_model
        if entry is answer_mod.build_chat_model:
            # 退出通道生效（A12–A15）：aux 走真装配
            return answer_mod.build_aux_model()
        if entry is None:  # pragma: no cover —— 防御：绝不把 None 放给辅助链路
            return None
        return entry()

    monkeypatch.setattr(chat_routes, "resolve_aux_model", _aux_follows_chat)
    return AUX_DELEGATED


@pytest.fixture(autouse=True)
def pin_agent_mode_off(monkeypatch):
    """用例内钉住**设置页的默认 Agent 模式**（``settings.AGENT_MODE = "off"``）。

    **为什么必须钉**（专项「工具调用能力升级」第 0 批 · ② 落地时实测）：

    ``settings`` 是**进程级单例**，而 ``PUT /api/settings`` 的 ``agent`` 段会**真的改它**
    （``settings_routes`` 的「落盘成功后才改内存单例」那一步：``setattr(settings, key,
    _stored_value(key, value))``）。``tests/api/test_settings_contract.py::test_a14b_agent_and_translate_are_real``
    因此在本进程里把 ``AGENT_MODE`` 置成 ``react``；它的 ``fake_dotenv`` 只把**磁盘**上的
    ``env_store.ENV_PATH`` 指到 ``tmp_path``，**管不到内存里那份单例**，所以用例结束后
    ``settings.AGENT_MODE`` 仍是 ``"react"``，一直漏给后续文件。

    改前这件事**看不见**：``/api/chat``（同步入口）对合法 ``react`` / ``plan_execute``
    是**静默忽略**的（``chat()`` 函数体里压根没有 agent 分支）⇒ 泄漏值不影响任何断言。
    第 0 批把口径改成「按**解析后的有效模式**判断、不静默忽略」（业主拍板 1 / 裁决 D10）
    之后，泄漏值会让**后续文件里所有不带 ``agent_mode`` 的同步请求**当场 400
    （实测：``test_usage_export_4_5_4`` 的两条 + ``test_skill_preflight_041`` 的一条）。
    复现只需两条：``test_settings_contract.py::test_a14b_agent_and_translate_are_real``
    后接 ``test_usage_export_4_5_4.py::test_clarify_round_records_nothing``。

    **为什么钉 ``off`` 而不是别的**：``off`` 就是 ``config.py::AGENT_MODE`` 的**出厂默认值**
    ⚠️ **现读（2026-09-28 实测）**：本机 ``.env`` **设了** ``AGENT_MODE=react``（``<repo>/.env:116``，
    2026-09-24 起）⇒ 本钉子把「测试进程读到的那份配置」还原成**产品的默认态**，
    与 ``pin_rerank_off`` / ``pin_web_off`` / ``pin_memory_off`` / ``pin_semantic_cache_off``
    / ``pin_graphrag_off`` 同一性质（那五条也是「防本机 .env 盖过 Settings 默认值」）。
    **它不改任何既有断言的期望值**：改前所有 ``tests/api`` 用例看到的 ``agent_mode``
    也都是 ``off``（要么请求显式给，要么泄漏值从未被消费）——本钉子只是把这件事从
    「碰巧」变成「钉住」，而不是把测试钉进实现细节。
    ⚠️ **本 pin 依赖「节点对象」**（回归修复 诊断）：``_pytest`` 把 autouse 夹具名记在
    ``_node_autousenames[节点]`` 上、``_getautousenames`` 沿 ``node.listchain()`` 查表；本仓实测
    存在「``tests`` Package 被重复 collect ⇒ 出现第二个 ``tests/api`` Package 节点」的场景
    （根因与复现见 ``_bugfix10-scratch/fix-split/T10-F1载体聚合4红-诊断.md``）⇒ 落在第二个节点下
    的 item 会**整片丢掉本文件的 autouse 面**（含本钉子）。自证型载体请就地自钉（先例：
    ``test_bug083_trace_persist_wiring.py::_pin_agent_mode_off``）。

    **要测 Agent 轮的用例不受影响**：``agent_mode`` 的取值链是
    「**请求显式值** → 设置页 ``AGENT_MODE`` → ``off``」⇒ 请求体里带 ``agent_mode`` 的
    用例（``test_chat_agent.py`` / 本批的 ``test_tool_context_t0.py``）照旧生效；
    要测「设置页默认迁移态」的用例自己
    ``monkeypatch.setattr(settings, "AGENT_MODE", "react")`` 覆盖本钉子即可
    （既有的 ``test_chat_agent.py:251`` / ``test_reasoning_aux_calls.py:259`` 就是那么写的）。
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "AGENT_MODE", "off")


@pytest.fixture(autouse=True)
def pin_tool_artifact_dir(monkeypatch, tmp_path):
    """把**工具正文落盘根**钉到 ``tmp_path``（专项工具升级-2 · R2-05 · D-S1-13⑤）。

    **为什么必须钉**：R2-06 起 ``runner.clip_tool_context_text`` / ``cap_tool_context`` 在
    「把正文换成标记」的四条分支上**先落盘**。既有用例里就有会走这些分支的
    （``test_tool_context_t0.py:396`` 的 ``"x"*5000``、``:426`` 的 ``"y"*9000`` —— **实测**
    经 ``tool_context_items()`` 走截断）⇒ 不钉的话它们会**真往仓库
    ``<repo>/data/tool_artifacts`` 写文件**，而 ``data/`` 被 ``.gitignore:43`` 忽略
    ⇒ 那是 **``git status`` 看不见的静默污染**。

    **形态为什么是「整属性替换」**：D-S1-13② 的首选接缝
    （``monkeypatch.setattr(type(settings), "tool_artifact_path", property(lambda self: tmp_path))``，
    同型先例 ``tests/api/test_documents_endpoints.py:24-27`` 等 4 处）。
    ⚠️ **不得**改用 D-S1-13③ 的 ``config_mod.ROOT_DIR`` 形态 —— 它同时改掉 ``upload_path``
    与 ``sqlite_file``（**实测**），而本夹具是 autouse、覆盖面又广，会打红不相关的既有用例。
    需要 ③ 形态的窄面用例（A9 的「父目录 == 数据根下的 ``data/``」）**自己就地换回原始属性**
    —— 见 ``test_tool_artifact_r2.py::test_a9_artifact_root_sits_under_the_data_root``。

    ⚠️ **必须与 R2-05 同笔落地（D-S1-13⑥）**：属性不存在时
    ``monkeypatch.setattr(..., raising=True)`` 会抛 ``AttributeError``，让 ``tests/api/``
    **每一次**用例都在该 autouse 夹具处报错（含 R2-02 的新用例）。
    """
    from app.core.config import settings

    monkeypatch.setattr(type(settings), "tool_artifact_path", property(lambda self: tmp_path))


@pytest.fixture(autouse=True)
def pin_settings_put_keys(monkeypatch):
    """把两个 PUT 端点的**可写键**逐个钉住「现值」（`回归修复` · 机制①）。

    **为什么需要**：`monkeypatch.setattr` 只还原**它自己钉过的键**；两个 PUT 端点却是
    **裸 `setattr(settings, key, …)`**（`settings_routes.py:576` / `skill_routes.py:198-199`）
    ⇒ 只要端点写到一个**本目录没钉的键**，该值就活过用例、活过文件，留在**进程级单例**里。
    实例 = 主账本 `087`（`AGENT_MODE` 漏移）：`test_settings_contract.py::test_a14b_*` 把它写成
    `react`，后续文件里不带 `agent_mode` 的同步 `/api/chat` 当场 400。

    **形态**：setup 把「现值」原样写回（**不改任何行为** —— 现值就是现值），但由此在
    `monkeypatch` 里登记了这条键 ⇒ **teardown 必然还原到用例开始前的值**。
    清单在 :data:`SETTINGS_PUT_KEYS`（来源与漂移判据见那里的注释）。
    """
    from app.core.config import settings

    for key in SETTINGS_PUT_KEYS:
        if hasattr(settings, key):
            monkeypatch.setattr(settings, key, getattr(settings, key))
        # 键被删/改名 ⇒ 这里**不**抛（否则整个 tests/api 每例都在夹具处 ERROR，
        # 一条真缺陷会被掩盖成成片红）；由 test_bug148 的 test_a1/test_a2 两条静态腿判红。


@pytest.fixture(autouse=True)
def restore_settings_singleton(monkeypatch):  # noqa: ARG001 —— 有意请求它：本夹具 teardown 必须早于 monkeypatch.undo()
    """用例结束把单例上**任何**被改动的键还原（`回归修复` · 机制②，兜底栏杆）。

    机制①只覆盖「清单内的键」；本夹具用 **diff 快照**兜住**清单外**的一切改动（端点将来
    新增而清单尚未跟上的键、以及用例里的裸赋值）：用例开始前拍一份 `vars(settings)` 浅快照，
    结束时对**值发生变化**的键直接写回，对**新增属性**直接删掉。

    ✅ **删除型也已闭合（B 路审计 MINOR-1，2026-09-28）**：条件写成 `key not in after or after[key] != value`
    ⇒ `delattr(settings, KEY)` 也会被还原；判据 = 载体 `test_e1`/`test_e2`（删 → 下一例必须仍在且值相同）。

    ⚠️ **浅拷贝边界（A 路审计 MINOR-148-1 登记）**：`before` 是 `vars(settings)` 的**浅**拷贝 ⇒ 对**原地变更**
    （`settings.X.append(...)` / `settings.X["k"] = …`）检不出。当前 :data:`SETTINGS_PUT_KEYS` 各键均为
    **不可变标量/字符串**（`MODEL_SERVICES`/`MODEL_ROLES` 都是单行 JSON 串）⇒ 端点面不可达；唯一可变容器类是
    类级 `PROVIDER_PRESETS`（端点不可写、全仓测试零原地写）⇒ 如实登记为边界，⛔ 不得写「任何改动都能还原」。

    ⚠️ **还原必须自己 `setattr`，不能借 `monkeypatch`**：本夹具的 teardown 早于 `monkeypatch`
    的 `undo()`；若在这里 `monkeypatch.setattr(settings, key, 旧值)`，登记下来的「旧值」正是
    **被污染的值**，随后的 `undo()` 会把它**改回去**（等于没还原）。—— 本条为实测纪律。
    """
    from app.core.config import settings

    before = dict(vars(settings))
    yield
    after = vars(settings)
    for key, value in before.items():
        # 🔴 条件必须含「键**不在** after」这一支（B 路审计 MINOR-1，2026-09-28）：
        # 原写法 `if key in after and …` 会让 `delattr(settings, KEY)` 型污染**直接跳过**
        # （实测：删除后其后访问抛 AttributeError ⇒ 下游响亮红，但那是「没还原」而非「还原了」）。
        if key not in after or after[key] != value:
            setattr(settings, key, value)
    for key in [k for k in after if k not in before]:
        try:
            delattr(settings, key)
        except (AttributeError, TypeError):  # pragma: no cover —— pydantic 内部属性，删不掉属正常
            pass


def real_model_builders(monkeypatch):
    """就地把两个装配入口换成**真函数**，并让 monkeypatch 在用例末**还原原绑定**。

    两条入口的语义（与本目录其它钉子正交）：

    - `resolve_chat_model` / `resolve_aux_model` → 直接指向
      `answer.build_chat_model` / `answer.build_aux_model`（**真装配 + 真 Provider**，
      只在发送边界换假传输，见 `tests/test_reasoning_isolation.py`）；
    - **不动** `build_turn_plan` / `persist_turn` 的形参注入 —— 那正是被测对象
      （A13/A14/A15 要验的就是「端点真的把 aux 传下去了」）。

    ⚠️ **必须先记下原绑定再写，并在退出时显式还原**（2026-09-20 实测踩到）：
    只写 `monkeypatch.setattr(...)` 是**够不着**的 —— `monkeypatch` 还原的是
    「它自己替换掉的那份」，而模块属性一旦被改写成真函数，后续用例（包括
    `pin_aux_model_delegates` 里的 `chat_routes.resolve_chat_model()` 调用）
    读到的就是**真函数**：A19 的「aux 跟随 chat」当场变红，而且**只在整包连跑时**出现
    （单文件跑不出来）。所以这里手工记/还，退出时机由调用方用 `try/finally` 兜住。

    用法（**不要**写成 `with`）：``real_model_builders(monkeypatch)`` 直接调用即可，
    `monkeypatch` 会在用例末统一还原（本函数额外注册了一次显式还原，幂等）。
    """
    from app.api import chat_routes
    from app.generation import answer as answer_mod

    original_chat = chat_routes.resolve_chat_model
    original_aux = chat_routes.resolve_aux_model

    def _restore() -> None:
        chat_routes.resolve_chat_model = original_chat
        chat_routes.resolve_aux_model = original_aux

    monkeypatch.setattr(chat_routes, "resolve_chat_model", answer_mod.build_chat_model)
    monkeypatch.setattr(chat_routes, "resolve_aux_model", answer_mod.build_aux_model)
    monkeypatch.setattr(chat_routes, "_restore_real_model_builders", _restore, raising=False)
    return _restore
