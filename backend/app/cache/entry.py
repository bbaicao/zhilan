"""缓存条目与缓存键（阶段 5 · 条目 `5.2`，子条目 `5.2.1`）。

本模块只做**纯函数级的三件事**，不碰存储、不碰事件循环：

1. :class:`CacheEntry` —— 命中时要能**把一轮重放完整**的快照结构（§2.3）；
2. :func:`build_key` —— 10 个成分的**精确键**（§2.2），`sha256(规范化 JSON)`；
3. :func:`serialize_materials` / :func:`deserialize_materials` —— ``Material`` 与
   ``dict`` 的**全字段**互转（缺一个字段，命中的那轮引用/归因就会与未命中轮不一致）。

## 相似度扩展点（**只在 docstring 预留，不实装、不写死接口**）

首版是**精确键缓存**（`:func:`build_key` 的 10 个成分全部相等才命中），
**不做**向量相似度检索。理由是实测过的代价对照：

- 参照物 ``GPTCache`` 的**默认档本身就是精确匹配**——``string_embedding`` 是恒等函数
  （``embedding/string.py:1-3``）+ ``MapDataManager.search`` 的字典查找
  （``manager/data_manager.py:166-170``）；一旦嵌入函数返回 ``np.ndarray``（不可哈希）
  就 KeyError **静默 miss**。⇒ 「搬语义档却搬成默认档」得到的是一个永远 miss 的缓存。
- 真做语义档要付：**每次提问多一次 ``/embeddings``**（命中也要付，因为得先算查询向量；
  默认 30s 超时 + 失败重试 1 次 ⇒ 最坏 2×30s）+ **Qdrant 第二个集合**
  （本仓本地模式是独占目录锁，``get_vector_store(collection=…)`` 必然新建第二个
  ``QdrantClient`` ⇒ **同进程也会失败**）+ 第二套一致性面。
  而桌面默认没有 embed Key ⇒ 不接住就破 I5，接住就是永久 100% 未命中 + 每问一条降级告警。

**将来若真要加相似档**，照 ``GPTCache`` 的这套形状做，不要只抄那个 ``0.8``：

- 协议两方法 ``evaluation(src_dict, cache_dict) -> float`` + ``range() -> (min, max)``
  （``similarity_evaluation/similarity_evaluation.py:5-41``）；
- 阈值是**区间内的相对比例**、由 ``range()`` 归一化
  （``adapter/adapter.py:98-107``）——同一个 ``0.8`` 配不同 ``range()`` 得到的绝对门槛完全不同；
  ⚠️ ``config.py:11-12`` 的 docstring（「0 时没有命中、1 时全部命中」）与代码**反向**，别照抄文档；
- **两段式**「召回 top-k → 逐候选判定 → 首个过阈值即命中」（``adapter.py:108-177``）；
- **「嵌什么」与「存什么」双字段解耦**（``pre_embedding_data`` vs ``pre_store_data``，
  ``adapter.py:65-70``）——正是本模块「范围进判等材料、正文单独存」的形状；
- miss 回写**吞异常只 warn** 的降级姿态（``adapter.py:255-285``）。

改这一件事**只该动 ``app/cache/`` 这一个包**：``build_key`` 是唯一的键构造点，
``store`` 是唯一的存储/失效点。

⚠️ **范围语义在 GPTCache 源码里没有对应机制**（全仓 grep ``filter|scope|namespace`` 0 命中）；
唯一沾边的 ``session.py`` + ``processor/check_hit.py`` 的语义是「本 session 问过就不再命中」，
与「限定检索范围」**方向相反** —— 所以范围语义是本条目的自建面（见 §2.2 第 ② 条）。

## 为什么不复用 ``db/redis.py`` 的垫片

``app/db/redis.py:44-80`` 的进程内垫片只有 ``ping`` / ``set`` / ``get`` / ``delete`` /
``aclose`` 五个方法（**没有** ``expire`` / ``scan`` / ``keys``），且它的模块 docstring
明文要求「将来若真要缓存或分布式锁，**应先扩本模块并登记**」——扩它会牵动 health 契约面与
冻结键表，故首版**不扩**。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Chunk, Document, Note, active_chunks

logger = logging.getLogger(__name__)

#: 条目结构版本号：字段一改就整体失效（旧条目读出来按「未命中」处理并丢弃）。
#:
#: ⚠️ **升到 2（2026-09-22 · 专项-上下文 CX-06′）**：``retrieval.render_materials`` 新增
#: **单条正文上限** ⇒ 版本 1 的快照里可能存着**超限的** ``materials_text``，命中时被原样回填
#: （``chat_routes`` 的 ``materials_text=hit.entry.materials_text``）⇒ **命中轮会绕过新上限**，
#: 而命中轮恰恰是快路径。处置 = **按版本丢弃**（进程内缓存，代价只是少命中一轮），**不做迁移**。
# U6：生产条目保存完整来源指纹，旧投影片段不能作为完整来源证据。
CACHE_SCHEMA_VERSION = 3

#: ``chunks`` 存活校验的**单批** id 数上限。
#:
#: 分批的理由（开工包 §9 第 2 条留给实施者定的那点）：SQLite 对 ``IN (...)`` 的**变量个数**
#: 有硬上限（本机 3.50.4 实测由 ``SQLITE_LIMIT_VARIABLE_NUMBER`` 控制，默认 32766），
#: MySQL 侧则只受 ``max_allowed_packet`` 约束。一轮材料最多 ``FUSE_LIMIT = 12`` 条，
#: 真实调用**永远只有一批**；这个常量只是把「将来放宽融合上限」时的爆炸半径收住。
_LIVENESS_BATCH = 200

_WS_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------
# 键成分的规范化
# --------------------------------------------------------------------------


def normalize_question(text: str) -> str:
    """① 规范化问题：折叠所有空白序列为一个空格 + 去首尾（与 ``memory._normalize`` 同口径）。"""
    return _WS_RE.sub(" ", str(text or "")).strip()


def filters_token(filters: dict | None) -> str:
    """② 范围的**稳定**序列化。

    ⚠️ **必须 ``sort_keys=True``**：``Filters.to_stored()``（``api/chat_schemas.py:42``）
    只做「丢空值」，返回的 dict 保持**字段声明序**——同一个范围用不同键序构造会得到两个
    dict，不排序就是**假 miss**（缓存命中率白掉，而且随调用方写法抖动）。
    """
    if not filters:
        return "{}"
    return json.dumps(filters, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def history_token(history: Iterable[dict] | None) -> str:
    """⑦ 历史的**内容指纹**：只取 ``role`` / ``content`` 两键，规范化 JSON 的 sha256。

    ``build_model_history`` 的产物形状就是这两键（压缩开启时开头多一对摘要消息，
    同样只有这两键）；这里**不取整个 dict**，是为了将来往 history 项上加内部键时
    不把缓存整体打失效（那属于实现细节，不影响答案）。
    """
    items = [
        {
            "role": str((item or {}).get("role") or ""),
            "content": str((item or {}).get("content") or ""),
        }
        for item in (history or [])
    ]
    blob = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _reasoning_fingerprint() -> dict[str, Any]:
    """⑨ 的 ``reasoning`` 子对象：**实际会发给厂商**的思考参数片段。

    与 ⑨ 的其余成分同源同责：**块没变、配置变了**——开着思考那一轮算出的答案，
    在关掉思考之后仍然命中，就是把一条**当前配置下拿不到**的回答交出去
    （与 ``graph`` 子对象是同一条道理）。

    ⚠️ **这里不能吞异常**（与 :func:`retrieval_fingerprint` 里那两处 `try` 的取舍相反）：
    本函数的作用**恰恰**是把「配置解析失败」与「正常不发参数」**区分开**
    ——返回 ``{}`` 会把「读不到配置」伪装成「本来就没有思考参数」⇒ 缓存**错命中**。
    抛出去由 :func:`build_key` 的调用方（`chat_routes._cache_key_for_turn` 的
    ``except Exception``）收敛成「**本轮不缓存**」——那才是正确的降级。

    取值口径**与 `answer._build_bound_model` 逐字同源**：同一个 `settings.chat_config`
    （含注册表绑定与 `.env` 扁平键两条路）、同一个 `reasoning.request_body_for`、
    同一个 `CHAT_REASONING`。两处一致性由 ``tests/cache/test_cache_reasoning_key.py``
    的「键里的指纹 == 真发出去的 extra_body」断言钉住——**它就是本条目的核心不变量**：
    指纹必须由**实际会发出去的参数**算出来。
    """
    from app.core import reasoning as reasoning_mod

    config = settings.chat_config
    if config is None or not str(getattr(config, "model", "") or ""):
        raise ValueError("尚未配置对话模型")
    provider = str(getattr(config, "provider", "") or "")
    raw = str(getattr(settings, "CHAT_REASONING", "") or "")
    return reasoning_mod.request_body_for(provider, config.model, raw) or {}


def _chat_turn_knobs() -> dict[str, Any]:
    """⑨ 的 `turn_knobs` 子对象：**生成 / 检索期的三样旋钮**（`回归修复`）。

    **为什么必须进键**：这三样**都不改块、也不改「选哪些块」** —— `embed` / `rerank` /
    `graph` 那几支查的是「怎么选块」，`chunks` 内容指纹查的是「块变了没有」，
    而这三样改的是「**这一轮怎么答、材料面怎么拼**」：

    - `direct_answer_enabled`（`CHAT_DIRECT_ANSWER_ENABLE`）：决定系统提示选
      `SYSTEM_PROMPT` / `NO_MATERIAL_SYSTEM_PROMPT` 还是逐字回退
      `LEGACY_SYSTEM_PROMPT`（`generation/answer.py:197`），以及「无资料」时消息体
      带不带旧后缀（`:260`）⇒ **同一道题会得到两套完全不同的正文**；
    - `link_expand_enabled`（`RAG_LINK_EXPAND`）：决定主命中要不要沿链接边多拉一跳
      （`retrieval/link.py:227`）⇒ **材料面本身就不同**；
    - `temperature`：采样温度（下面的取证口径）。

    取证口径（2026-09-28 本笔实测，**如实登记**）：

    - `CHAT_DIRECT_ANSWER_ENABLE`：**出厂默认 `True`**（`core/config.py:207`）。读法与
      消费点同款：`bool(getattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", True))`
      （`chat_routes.py:1085` 就是这个写法）—— ⚠️ 这里的 `True` 是 `getattr` 的
      **属性缺失回落值**（字段一直存在 ⇒ 平时永不生效），**不是**出厂默认值。键缺失 / `None` 也能跑，
      不抛；`None` 归一成 `False` 与消费点同义（`answer.py` 的判据是
      「`not settings.CHAT_DIRECT_ANSWER_ENABLE` ⇒ 回退 legacy」）；
    - `RAG_LINK_EXPAND`：**出厂默认 `True`**（`core/config.py:318`）。读法与唯一消费点
      `link.py:227` 逐字同款：`bool(getattr(settings, "RAG_LINK_EXPAND", False))` ——
      ⚠️ 这里的 `False` 是 `getattr` 的**属性缺失回落值**（只在「settings 上根本没这个属性」
      或「值为 `None`」时生效），**不是**「默认关闭」；正常运行读到的就是出厂默认 `True`。
      回落取同一个值是硬要求：「键里记的值」必须与「那一轮消费点实际看到的值」同源，否则属性缺失时会出现
      「键说开着、通道却没开」的双真相源（`回归修复` ② 的同一条教训）；
    - `link_max`（`RAG_LINK_MAX`，`task-26` 补）：一跳扩展的**名额**旋钮。出厂默认见
      `core/config.py:319`；读法与唯一消费点 `retrieval/link.py:56` 逐字同款
      （`int(getattr(settings, "RAG_LINK_MAX", 0) or 0)` —— 含 `or 0`；**属性缺失 / None ⇒ 0 名额**，
      与消费点同义）。**审计探针实测**：把 `RAG_LINK_MAX` 4→99 ⇒ `link_budget(main_count=2)` 由
      **4 变 10**（材料条数真的变了），而补漏前 `retrieval_fingerprint()` **逐字不变** ⇒ 同一道题会命中
      「按另一套名额拼出来的材料」那一轮；
    - ⚠️ **link 族里其余三个不是旋钮**：`LINK_SEEDS` / `LINK_SCORE_FACTOR` / `LINK_SNIPPET_CHARS`
      是 `pipeline/wikilink.py` 的**模块常量**（不是 settings 键）⇒ **不进键**，理由 = 「不可运行时变」
      （改它们等于改代码 + 重启 ⇒ 重启后进程内缓存本来就空了）；
    - `temperature`：**主回答链路目前压根不发送温度** —— `answer.answer_direct` 的
      `bound.generate(typed, system_prompt=…, max_output_tokens=DIRECT_MAX_TOKENS)`
      没有 `temperature` 形参，装配侧 `_build_bound_model` 只带 `extra_body`（推理参数），
      而 `core.reasoning.request_body_for` 的模板**只有** `thinking` /
      `reasoning_effort`（`core/reasoning/specs.py`）⇒ **仓内没有回答链路的温度旋钮**
      （`core/config.py` 无 temperature 键；`generation/answer.py:492` 的注释也写明
      「`temperature` 一律原样不动」）。**所以本成分当前恒为 `None`（= 不发温度），
      是一个惰性常量：值不变 ⇒ 键不变。** 读取点取 `app.generation.answer` 模块上的
      `DIRECT_TEMPERATURE`（**当前该名字不存在**，故为 `None`）：一旦有实现真的把主链路
      改成显式发温度，只要在那个模块上落同值常量，本键**自动跟随**，不必再改缓存代码；
    - ⚠️ 用 `sys.modules` **探测**而不是 import 它：本模块在请求热路径上，
      多一个 import 面就多一个「取指纹时炸掉」的机会（与 `graph.enabled` 的延迟 import
      同一条取舍）；真实一轮里该模块必然已加载（本轮答案就是它生成的），故探测口径与真实装配同源；
    - ⚠️ `routing/llm.py:32` 的 `TEMPERATURE` / `clarify.CLARIFY_TEMPERATURE` /
      `compaction.SUMMARY_TEMPERATURE` / `memory.EXTRACT_TEMPERATURE` **都不进本键**：
      它们分别服务**意图分类 / 澄清 / 压缩 / 记忆抽取**四条别的链路，与「缓存里那条答案正文」无关；
      算进来是**假耦合**（改一次澄清温度就会把整片回答缓存打失效）。

    🔴 **口径更正（`task-26`，只改措辞、不改代码）——本子对象是「防御性加固」，不是「修掉一条生产可达的错命中」**：
    上面四个旋钮（`CHAT_DIRECT_ANSWER_ENABLE` / `RAG_LINK_EXPAND` / `RAG_LINK_MAX` / 温度来源）
    **一个都不在 `api/settings_schemas.py::SettingsIn` 的字段里**（设置页能写的是 `MODEL_*` /
    `RAG_ENABLE_RERANK` / `CHAT_REASONING` / `AGENT_*` / `WORKSPACE_ROOT` 等，
    实测枚举见载体登记腿），而本缓存是 **进程内 `OrderedDict`（`cache/store.py`，重启即空）**
    ⇒ 「改旋钮」只有两条路径：① 改 `.env` + **重启**（重启后缓存已空 ⇒ 根本不存在跨配置的旧条目）；
    ② 测试里 monkeypatch。⇒ **生产进程内当前不可达**：本子对象的价值是「**键更完整**」（口径统一、
    防将来把旋钮接进设置页时留下一条无人记得的错命中窗口），**⛔ 不得写成「修掉一条生产可达的错命中」**。
    判据侧：载体有一条**可证伪的登记腿**——一旦有人把这四个名字接进设置页，那条腿会红，提醒重评本段。
    """
    direct_enabled = bool(getattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", True))
    link_expand_enabled = bool(getattr(settings, "RAG_LINK_EXPAND", False))
    # `task-26`：名额旋钮与唯一消费点 `retrieval/link.py:56` 的读法逐字同款
    # （`int(getattr(settings, "RAG_LINK_MAX", 0) or 0)` —— 含 `or 0` 兜 None / 缺键）
    link_max = int(getattr(settings, "RAG_LINK_MAX", 0) or 0)
    answer_mod = sys.modules.get("app.generation.answer")
    temperature = (
        getattr(answer_mod, "DIRECT_TEMPERATURE", None) if answer_mod is not None else None
    )
    return {
        "direct_answer_enabled": direct_enabled,
        "link_expand_enabled": link_expand_enabled,
        "link_max": link_max,
        "temperature": temperature,
    }


def retrieval_fingerprint() -> dict[str, Any]:
    """⑨ **检索 + 生成配置指纹**：`MODEL_ROLES.embed` / `MODEL_ROLES.rerank` + 重排开关与门槛
    + **图通道**（阶段 5 · 条目 5.3 补的子对象）+ **推理参数**（推理等级回流补的子对象）。

    **为什么必须进键**：换 embed / rerank 模型、或改重排开关与门槛 ⇒ **检索结果会变**
    （选块的方式变了）。而 §2.3 第 3 条的 ``chunks`` **内容指纹查不出这一层**——
    块一条都没变，变的是「怎么选块」。所以这是「连答案一起缓存」比「只缓存检索」
    **多出来**的一条。

    ``graph`` 子对象同理，而且更锋利：**块没变、开关变了**——「开着图通道算出的答案」
    在关掉图通道之后仍然命中，就会把一条**当前配置下拿不到**的材料当成答案交出去
    （与 5.2 ⑨ 是同一条道理）。子对象含 6 个 `GRAPHRAG_*` 键（开关 + 5 个旋钮）：
    开工包 §2.5 只点名了 5 个（``enabled`` / ``top_k`` / ``score_factor`` / ``hop_decay`` /
    ``weak_alias_weight``），这里补上 ``max_terms`` ——「改了 `GRAPHRAG_*` ⇒ 换键」这条
    断言（A12③）要求它也在内，否则单独调 ``GRAPHRAG_MAX_TERMS`` 会命中旧答案。

    ``reasoning`` 子对象（推理等级回流）是第三例，而且与答案**直接**相关：思考开启与否
    会让正文本身就不同 ⇒ 换档必须换键（A17）。配置解析失败时本函数抛异常
    （见 :func:`_reasoning_fingerprint` 的注释）——读不到配置就当**不缓存**。

    ``turn_knobs`` 子对象（`回归修复` + `task-26`）是第四例 —— 一句话概括：
    「**同一道题、两套配置**」：`CHAT_DIRECT_ANSWER_ENABLE` 换的是系统提示与消息体那一套
    （`generation/answer.py:197` / `:260`）、`RAG_LINK_EXPAND` 换的是材料面
    （`retrieval/link.py:227`）、`RAG_LINK_MAX` 换的是材料**条数**
    （`retrieval/link.py:56` 的 `link_budget`）、`temperature` 换的是采样。
    四样都不动块、也不动「选哪些块」⇒ 本函数其余成员与 `chunks` 内容指纹**都查不出来**。
    ⚠️ **口径（`task-26` 更正）**：这四个旋钮都不在设置页（`SettingsIn`）且本缓存进程内、
    重启即空 ⇒ 生产进程内**当前不可达**；本子对象的定性是**防御性加固（键更完整）**，
    **⛔ 不得写成「修掉一条生产可达的错命中」**。逐条取证与可达性论证见 :func:`_chat_turn_knobs`。

    ⚠️ **加这两个子键会让存量缓存条目一次性全部失效**（键是 `sha256` 十六进制、没有版本位）。
    这是刻意的：进程内缓存、重启即空，条目的正确性优先于命中率。
    `回归修复` 补 `turn_knobs` 是**第三次**付这个代价（拍板单 F 已决：
    一次性失效可接受，交付里登记）。⚠️ 本子对象**不会**抛异常（温度那支恒可读）⇒ 不影响上面那条
    「读不到配置 ⇒ 本轮不缓存」的口径。

    U2 使用完整 embedding 服务／模型／维度身份，仅解析配置，不发模型请求。
    U6 同时记录实际解析的 chat/rerank 服务地址；身份不含密钥。
    """
    roles = getattr(settings, "roles", None) or {}
    from app.vector.embed import OpenAICompatEmbedding
    from app.vector.identity import embedding_identity

    embed_identity = embedding_identity(OpenAICompatEmbedding())
    from app.retrieval.rerank import resolve_rerank_config

    def resolved(config) -> dict[str, str] | None:
        if config is None:
            return None
        return {
            name: (
                str(getattr(config, name, "") or "").strip().rstrip("/")
                if name == "base_url" else str(getattr(config, name, "") or "").strip()
            )
            for name in ("kind", "provider", "base_url", "model")
        }

    def bound(role: str) -> dict[str, str] | None:
        item = roles.get(role) if isinstance(roles, dict) else None
        if not isinstance(item, dict):
            return None
        return {
            "service_id": str(item.get("service_id") or ""),
            "model": str(item.get("model") or ""),
        }

    def knob(name: str, default):
        """读一个 ``GRAPHRAG_*`` 旋钮（**键缺失也能跑**；含 ``None`` 时按默认值记）。

        这里刻意不 import ``app.retrieval.graph``：本模块在**请求热路径**上，且要做
        「读配置失败 ⇒ 当未命中」的收敛——多一个 import 面就多一个「取指纹时炸掉」的机会。
        取值口径与 ``graph.py`` 的同名读取函数一致（``getattr`` + 同款默认值），
        两边的一致性由 ``tests/retrieval/test_graph_channel.py`` 的默认值钉子守住。
        """
        value = getattr(settings, name, default)
        return default if value is None else value

    # 🔴 **开关键不在这里读**（```回归修复``` ②）：```GRAPHRAG_ENABLE``` 的**唯一**读取点是
    # ```app.retrieval.graph.enabled()```（该函数 docstring 逐字如此声明）。此前这里写的是
    # ```bool(knob("GRAPHRAG_ENABLE", False))``` —— **第二次直读同一个键** ⇒
    # 「唯一读取点」这句话与代码不符（双真相源：改一处忘另一处就会「指纹说开着、通道却没开」）。
    # 这里改为**委托**该唯一入口。延迟 import 的理由与 ```deserialize_materials``` / ```rebuild_stats``` 同款：
    # 本模块在请求热路径上 ⇒ 把 import 面收进函数内，```graph``` 这次导入出问题也不牵连整个取指纹。
    # ⚠️ **逐字等价**：```enabled()``` 的实现就是 ```bool(getattr(settings, "GRAPHRAG_ENABLE", False))```，
    # 与原那行对本键**任意取值同结果**（默认值同为 ```False```）。
    from app.retrieval.graph import enabled as _graph_enabled

    graph_fingerprint = {
        "enabled": bool(_graph_enabled()),
        "top_k": knob("GRAPHRAG_TOP_K", 6),
        "score_factor": knob("GRAPHRAG_SCORE_FACTOR", 0.5),
        "hop_decay": knob("GRAPHRAG_HOP_DECAY", 0.5),
        "weak_alias_weight": knob("GRAPHRAG_WEAK_ALIAS_WEIGHT", 0.5),
        "max_terms": knob("GRAPHRAG_MAX_TERMS", 8),
    }

    return {
        "embed": embed_identity,
        "rerank": bound("rerank"),
        "chat": {"binding": bound("chat"), "resolved": resolved(settings.chat_config)},
        "rerank_resolved": resolved(resolve_rerank_config()),
        "rerank_enabled": bool(getattr(settings, "RAG_ENABLE_RERANK", False)),
        "rerank_min_score": str(getattr(settings, "RAG_MIN_RERANK_SCORE", "") or ""),
        "graph": graph_fingerprint,
        # 推理参数（推理等级回流）：**空 dict = 不发思考参数**，与「配置读不到」在键上不同
        # ——后者由 :func:`_reasoning_fingerprint` 抛异常表达（⇒ 本轮不缓存）。
        "reasoning": _reasoning_fingerprint(),
        # 生成 / 检索期旋钮（`回归修复`）：见 :func:`_chat_turn_knobs`——这三样
        # 都不改块、也不改「选哪些块」，但会改「这一轮怎么答 / 材料面怎么拼」⇒ 不进键就是错命中。
        "turn_knobs": _chat_turn_knobs(),
    }


@dataclass(frozen=True)
class CacheKeyInputs:
    """:func:`build_key` 的 10 个成分（**缺一不可**，逐条理由见 §2.2）。"""

    #: ① 规范化问题（:func:`normalize_question`）
    question: str
    #: ② 范围：``Filters.to_stored()`` 的产物（:func:`filters_token` 会排序）
    filters: dict | None
    #: ③ 本轮**生效**的 agent_mode（``chat_routes.resolve_agent_mode`` 的返回值）
    agent_mode: str
    #: ④ ``skill_ids``（``builtin:web`` 决定联网工具门控 ⇒ 断按键内自己排序）
    skill_ids: Iterable[str]
    #: ⑤ 模型身份（``chat_routes.attempt_provider_model()`` → ``provider/model``）
    model_label: str
    #: ⑥ 5.1 的记忆注入段（**少不得**：漏了就「5.1 的注入被 5.2 静默吞掉」）
    memory_text: str
    #: ⑦ 历史内容指纹（:func:`history_token` 的产物）
    history: Iterable[dict] | None
    #: ⑧ 是否带图（带图轮**首版直接不缓存**，这条只是把键写全）
    has_images: bool
    #: ⑨ 检索配置指纹（:func:`retrieval_fingerprint` 的产物）
    retrieval_config: dict[str, Any] = field(default_factory=dict)
    #: ⑩ 同轮**路由结论**（``routing.RoutingDecision.source_type``：``internal_db`` / ``external``）。
    #: **必须进键**（``回归修复``）：同一句话走库内检索与走库外直答是**两个不同的问题**
    #: （前者答「我库里怎么说」、后者答「一般怎么说」），键里没有它就是**错命中** ——
    #: 第二轮判 external 却回放第一轮 internal_db 的答案 + 引用行，而同轮 ``routing/decided``
    #: 事件写的是 external。
    #:
    #: 默认 ``"internal_db"`` 的口径（**不是**在命中路径写死路由结论）：省略这一项的调用点都在
    #: 「按能存下条目的那一类轮次算键」——``source_map.resolve_kind`` 下 ``kind == "normal"``
    #: ⟹ ``source_type == "internal_db"`` 且有材料，而 ``cache/store.py`` 的 ``STORED_KINDS``
    #: 只收 ``normal`` ⟹ 存量条目**只**可能来自库内轮 ⇒ 省略时取 ``internal_db`` 与修复前
    #: **唯一可能**的取值相同 ⇒ 既有**测试**调用点零改动（``tests/api/test_semantic_cache_http.py``
    #: ``:609`` / ``:671`` / ``:753`` / ``:757`` / ``:815``、
    #: ``tests/cache/test_cache_reasoning_key.py:197``、
    #: ``tests/api/test_tool_context_extra_t0.py:196`` / ``:213``）。
    #: ⚠️ **后果（⛔ 不是「键逐字一致」）**：``build_key`` 的 ``key_material`` **多了一个
    #: ``"source_type"`` 键** ⇒ 同一个调用点算出的 sha256 **必然变** ⇒ 升级前写入的存量条目
    #: **键值必然不再匹配**（进程内表、重启即空，且 TTL / LRU 会自然回收）⇒ **无需 bump
    #: ``CACHE_SCHEMA_VERSION``**：条目**结构**未变，先例
    #: ``tests/cache/test_bug139_config_knobs_key.py:59``「条目结构没变，变的是键」。
    #: 生产两处（``chat_routes`` 缓存点 / ``chat_cache.maybe_store_cache``）**一律显式传参**；
    #: ⛔ 新增调用点必须显式传 —— 漏传时库外轮会与库内轮同键（残余面已登记）。
    source_type: str = "internal_db"
    retrieval_query: str | None = None
    rewrite_history: Iterable[dict] | None = None


def build_key(inputs: CacheKeyInputs) -> str:
    """把 10 个成分哈希成缓存键（``sha256`` 十六进制，64 位）。

    **不进键的三样**（都要每轮现铸 / 是请求身份而非内容身份）：

    - ``request_id`` —— 每请求都不同 ⇒ 进了它命中率恒 0，而且它是**停止登记键**；
    - ``chat_id`` —— 答案不依赖「哪个会话」，只依赖历史 + 记忆 + 范围 + 模型；
      对**跨会话复用**是负收益（同一个人开新会话问同一个问题本可命中）；
    - ``turn`` / ``attempt_id`` / ``retrieval_id`` —— 每轮**必须现铸**（§2.4 第 1 条）。
    """
    from app.retrieval import query as query_mod

    key_material = {
        "question": normalize_question(inputs.question),
        "filters": filters_token(inputs.filters),
        "agent_mode": str(inputs.agent_mode or ""),
        "skill_ids": sorted(str(item) for item in (inputs.skill_ids or [])),
        "model": str(inputs.model_label or ""),
        "memory_text": str(inputs.memory_text or ""),
        "history": history_token(inputs.history),
        "has_images": bool(inputs.has_images),
        "retrieval_config": inputs.retrieval_config or {},
        "source_type": str(inputs.source_type or ""),
        # 普通提问也带策略版本；旧策略及同摘要下不同原始指代上下文不能错命中。
        "query_rewrite_version": query_mod.QUERY_REWRITE_VERSION,
        "retrieval_query": normalize_question(inputs.retrieval_query or inputs.question),
        "rewrite_history": history_token(inputs.rewrite_history),
    }
    blob = json.dumps(
        key_material, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 条目结构
# --------------------------------------------------------------------------


@dataclass
class CacheEntry:
    """一条缓存：**够把一整轮重放完整**的快照（字段一个都不能少，§2.3）。

    存的是「这一轮当时**是什么**」：材料快照 + 渲染正文 + 派生原因 + 模型原始正文 +
    思考全文。**不存派生值**（``refs`` / ``kind`` / ``answer_source`` / ``retrieval_id``）
    ——那些在命中的那一轮**现算**，这样与未命中路径**同源**，不可能漂移（§2.4 第 2 条）。
    """

    #: 结构版本号（:data:`CACHE_SCHEMA_VERSION`；不匹配 ⇒ 丢弃）
    version: int = CACHE_SCHEMA_VERSION
    #: 写入时刻的**单调时钟**（``time.monotonic()``）。TTL **按它现算**，
    #: 不在写入时烤一个 ``expires_at`` —— 改配置对已有条目即刻生效（§2.3）。
    created_at: float = 0.0
    #: 规范化后的问题原文（**只作诊断**，不参与判等——判等在 :func:`build_key`）
    question: str = ""
    #: 材料快照（``dataclasses.asdict`` 的 dict 列表，全字段 round-trip）
    materials: list[dict] = field(default_factory=list)
    #: 渲染好的资料区正文（``retrieval.render_materials`` 的产物）
    materials_text: str = ""
    #: 向量路降级提示（``retrieve()`` 第三返回值；可为 ``None``）
    retrieval_warning: str | None = None
    #: 当年那次检索的 ``RerankStats.payload()``（``{"rerank": {...}, "empty_reason": ...}``）。
    #: 存整份是给命中轮的 ``retrieval/result`` 事件用的——字段与未命中轮**逐字同形**。
    retrieval_stats: dict | None = None
    #: 空结果的派生原因（``no_recall`` / ``below_threshold`` / ``None``）。
    #:
    #: ⚠️ **为什么单独存一份**：``RerankStats.empty_reason`` 是**只读 property**
    #: （``retrieval/rerank.py:85-91``，由 ``status`` / ``kept`` / ``candidates`` **派生**），
    #: **不能赋值**。命中时若直接塞一个空 ``RerankStats()``，``final_material_state``
    #: 会给出 ``None`` 而不是当时的原因 ⇒ ``turn/end`` 与 ``done`` 的字段值与未命中轮**不一致**。
    #: 存派生值、命中时原样回填是唯一稳的做法。
    final_empty_reason: str | None = None
    #: 模型原始正文（**未加展示前缀**；展示前缀由 ``get_turn_prefix`` 在命中轮现算）
    raw_answer: str = ""
    #: 思考全文（可空串；``done.reasoning`` 与 ``reasoning`` 帧都用它）
    reasoning: str = ""
    #: U6 ``{source:类型:id: 完整来源 hash}``；旧直接 helper 的内容短哈希仍可校验。
    fingerprints: dict[str, str] = field(default_factory=dict)
    #: 模型身份（诊断用；它**已在键里**）
    model_label: str = ""
    #: 5.1 注入段（诊断用；它**已在键里**）
    memory_text: str = ""


def content_fingerprint(text: str) -> str:
    """分块内容的**短哈希**（16 位十六进制保留宽度）。

    用途只有一个：判「这个 ``chunk_id`` 现在的内容与缓存时**是不是同一份**」——
    只查「id 还在不在」拦不住 ``reindex``（新块的 ``(document_id, ordinal)`` 与旧的相同
    ⇒ ``chunk_id`` 可能照样存在，**内容却变了**），那会给出基于旧内容的陈旧答案。
    """
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


async def _source_fingerprints(
    db: AsyncSession, *, chunk_filter=None, note_ids: Iterable[int] = (),
) -> tuple[dict[str, str], set[tuple[int, int]]]:
    """捕获和校验共用同一列投影；不复制正文到缓存，只保留完整来源的 hash。"""
    result: dict[str, str] = {}
    positions: set[tuple[int, int]] = set()

    def record(kind: str, row) -> None:
        payload = json.dumps(
            dict(row._mapping), sort_keys=True, ensure_ascii=False, default=str,
        )
        result[f"source:{kind}:{int(row.id)}"] = hashlib.sha256(
            payload.encode("utf-8")
        ).hexdigest()

    if chunk_filter is not None:
        rows = (await db.execute(select(
            Chunk.id, Chunk.document_id, Chunk.ordinal, Chunk.content, Chunk.build_id,
            Chunk.page, Chunk.start_offset, Chunk.end_offset,
            Document.active_build_id, Document.content_build_id, Document.filename,
            Document.kb_id, Document.category, Document.tags, Document.created_at,
            Document.index_status,
        ).join(Document, Document.id == Chunk.document_id).where(
            chunk_filter, active_chunks(), Document.deleted_at.is_(None),
        ))).all()
        for row in rows:
            record("chunk", row)
            positions.add((int(row.document_id), int(row.ordinal)))
    note_ids = sorted(set(int(value) for value in note_ids))
    for start in range(0, len(note_ids), _LIVENESS_BATCH):
        rows = (await db.execute(select(
            Note.id, Note.content, Note.title, Note.kb_id, Note.category, Note.tags,
            Note.source_document_id, Note.created_at,
        ).where(
            Note.id.in_(note_ids[start:start + _LIVENESS_BATCH]),
            Note.deleted_at.is_(None),
        ))).all()
        for row in rows:
            record("note", row)
    return result, positions


async def capture_fingerprints(db: AsyncSession, materials) -> dict[str, str] | None:
    """准备阶段捕获全部覆盖源块及笔记原文；缺行/查库失败则本轮不写缓存。"""
    anchors: set[int] = set()
    positions: set[tuple[int, int]] = set()
    note_ids: set[int] = set()
    clauses = []
    try:
        for item in materials or []:
            if getattr(item, "url", None):
                return None
            if getattr(item, "note_id", None):
                note_ids.add(int(item.note_id))
                continue
            chunk_id = int(getattr(item, "chunk_id", 0) or 0)
            document_id = int(getattr(item, "document_id", 0) or 0)
            if chunk_id <= 0 or document_id <= 0:
                return None
            anchors.add(chunk_id)
            covered = tuple(getattr(item, "covered_ordinals", ()) or ())
            if covered:
                positions.update((document_id, int(value)) for value in covered)
                clauses.append(
                    (Chunk.document_id == document_id) & Chunk.ordinal.in_(covered)
                )
        if anchors:
            clauses.append(Chunk.id.in_(anchors))
        snapshots, found_positions = await _source_fingerprints(
            db, chunk_filter=or_(*clauses) if clauses else None, note_ids=note_ids,
        )
        expected = {f"source:chunk:{value}" for value in anchors}
        expected.update(f"source:note:{value}" for value in note_ids)
        if not expected.issubset(snapshots) or not positions.issubset(found_positions):
            return None
        return snapshots
    except Exception as exc:  # noqa: BLE001 —— 缓存失败不影响问答
        logger.warning("缓存来源捕获失败（本轮不写缓存）：%s", exc.__class__.__name__)
        return None


# --------------------------------------------------------------------------
# Material ↔ dict（全字段 round-trip）
# --------------------------------------------------------------------------


def serialize_materials(materials) -> list[dict]:
    """材料快照 → dict 列表（``dataclasses.asdict``，**全字段**）。"""
    out: list[dict] = []
    for item in materials or []:
        if hasattr(item, "__dataclass_fields__"):
            out.append(asdict(item))
        elif isinstance(item, dict):
            out.append(dict(item))
        else:  # pragma: no cover —— 形状不对时不做假数据
            logger.warning("缓存写回遇到无法序列化的材料（已跳过）：%r", type(item))
    return out


def deserialize_materials(rows) -> list:
    """dict 列表 → ``Material`` 快照（**读回时重建 dataclass**）。

    重建而不是留着 dict：``citation.refs_from_materials`` / ``final_material_state`` /
    ``attribution_rows`` 全都按 ``Material`` 的属性与 ``is_web`` / ``is_note`` /
    ``chunk_key`` 三个 property 工作——把 dict 直接塞进 ``TurnPlan`` 会让整条引用链在读属性时炸。
    """
    from app.retrieval.types import Material

    fields = set(Material.__dataclass_fields__)
    out: list = []
    for row in rows or []:
        if not isinstance(row, dict):
            logger.warning("缓存条目里的材料形状不对（已跳过）：%r", type(row))
            continue
        known = {key: value for key, value in row.items() if key in fields}
        try:
            out.append(Material(**known))
        except TypeError as exc:  # pragma: no cover —— 结构版本不匹配时不该炸穿
            logger.warning("缓存材料重建失败（已跳过）：%s", exc)
    return out


def rebuild_stats(payload: dict | None, *, empty_reason: str | None = None):
    """把缓存里的 ``RerankStats.payload()`` 读回成 :class:`RerankStats`。

    ``empty_reason`` 是**派生 property**，不能赋值 ⇒ 恢复 ``rerank`` 原始字段及外层改写／关系统计；
    派生值由调用方从 :attr:`CacheEntry.final_empty_reason` **原样回填**。
    形状不认识（键对不上 / 值类型不对）⇒ 返回 ``None``，调用方按「没有统计」处理——

    ⚠️ **返回 ``None`` 比返回空对象安全**：``final_material_state`` 只在
    ``stats is not None`` 时才写 ``final_empty_reason``；塞一个空对象进去会写出 **``null``**
    而不是当时的真实原因，反而制造出「命中轮少一个真实值」的假差异。
    """
    if not isinstance(payload, dict):
        return None
    rows = payload.get("rerank")
    if not isinstance(rows, dict):
        return None
    from app.retrieval.rerank import RerankStats

    fields = set(RerankStats.__dataclass_fields__)
    values = {key: value for key, value in rows.items() if key in fields}
    for key in ("query_rewrite", "links"):
        if isinstance(payload.get(key), dict):
            values[key] = dict(payload[key])
    try:
        return RerankStats(**values)
    except TypeError:  # pragma: no cover
        return None


# --------------------------------------------------------------------------
# 存活校验（**「命中直接给答案」这套设计唯一的正确性支点**）
# --------------------------------------------------------------------------


def _link_snippet_chars() -> int:
    """一跳扩展片段的截断上限（**唯一定义处**：`pipeline/wikilink.py::LINK_SNIPPET_CHARS`）。

    延迟 import 的理由与本模块其余几处同款：调用点在**请求热路径**上，而 `app.pipeline`
    是个**重包**（它的 `__init__` 会把 parsing / mineru / storage 全拉起来）——
    模块级 import 等于给「取指纹」凭空接一个 import 面。真实一轮里它必然已在 `sys.modules`
    里（`retrieval/link.py:36` 取的就是同一个常量）；而调用点在
    :func:`fingerprints_live` 的 `try` 内 ⇒ 万一 import 失败也只是「本轮按未命中处理」。
    """
    from app.pipeline.wikilink import LINK_SNIPPET_CHARS

    return int(LINK_SNIPPET_CHARS)


def project_material_text(text: str) -> str:
    """**一跳扩展片段**对正文的投影：`(text or "").strip()[:LINK_SNIPPET_CHARS]`。

    与两个生产点**逐字同款**（`retrieval/link.py:134-140` 笔记 /
    `:193-199` 文档）：两处的写法都是 `content = (x.content or "").strip()`
    再 `content[:LINK_SNIPPET_CHARS]`。上限走 :func:`_link_snippet_chars`
    （复用唯一定义处，不另写一个 800）。

    ⚠️ 本函数**只给读侧**用：写侧存的是**已经投影过**的 `Material.content`
    （写回侧拿不到截断点之后的那段正文），读侧拿库内全量正文**反推同一个投影**，
    两侧才可能同源（`回归修复`）。
    """
    return (text or "").strip()[: _link_snippet_chars()]


async def fingerprints_live(db: AsyncSession, fingerprints: dict) -> bool:
    """存活校验：被引用的分块**属于当前内容版本** **且** **内容没变**。

    U6 生产条目使用 ``source:chunk:id`` / ``source:note:id``，完整原文、
    所有覆盖块、构建身份和来源/范围元数据必须相同；不受窗口裁剪限制。
    下列三候选口径只保留给既有直接 helper 及手工条目，不用于生产写回。

    两条都要过（**不能只查存在性**）：

    1. 行数与 ``id`` 集合与缓存时**完全一致**（拦「块被硬删」——``purge_document``
       是级联硬删；软删进回收站**不动** ``chunks`` 行 ⇒ 由下面那条 SQL 的 ``documents``
       inner join 单独拦 —— ``回归修复``，见本函数末段）；
    2. 每行 ``content`` 的短哈希与 :attr:`CacheEntry.fingerprints` 里记的**一致**
       （拦 ``reindex`` 后「``chunk_id`` 未变、内容已变」）。

    任一不符 ⇒ 返回 ``False``，调用方**当未命中**（走真检索 + 真模型，**两者都重跑**）。

    **为什么必须有**（2026-09-19 侦察实测的两条理由）：软删不删向量、块可被硬删
    （``purge_document`` 级联）；``reindex`` 会「删块重建」，若新块的
    ``(document_id, ordinal)`` 与旧的相同，``chunk_id`` 可能**照样存在**但内容已经变了
    ⇒ 只查「还在不在」会给出**基于旧内容**的陈旧答案（现在还会连答案一起是旧的）。

    这一次 SELECT **只在命中时发生**，比一次完整检索（双路召回 + 融合 + 可选重排 +
    一跳扩展）便宜一个数量级。

    ⚠️ **为什么不在键里放「语料版本指纹」**（与常见做法不同，**有意**）：键里放指纹只能拦
    「语料变了」，拦不住「指纹取不到 / 取不准」（如 ``max(updated_at)`` 在软删、reindex、
    直接改库三种路径下语义各不相同）；而**命中时校验真实引用的分块内容**是**直接证据**，
    不依赖任何版本的近似。代价只是命中时那一次 SELECT，而它本来就为「不许悬空引用」而必须做。

    ``fingerprints`` 为空（该轮零材料）⇒ **恒为真**（没有可悬空的东西，不需要查库）。

    **三类键各查各的表**（阶段 5 · `5.6` 的 `回归修复` 修复）：

    - **纯数字键** = ``chunks.id`` ⇒ 查 ``chunks`` **inner join ``documents``**，两条一起过：
      ``chunks`` 行还在（拦硬删）、属于当前内容版本（拦重建后旧块缓存）、
      **且** ``documents.deleted_at IS NULL``（拦软删进回收站）；
    - **``note:<note_id>`` 键** = **笔记材料** ——图通道带回的笔记用负数 ``chunk_id`` 占位，
      负号**不能**当键（它既不是 ``chunks.id``，也认不出该查哪张表）；而笔记**有自己的 ``notes`` 行**，
      所以要按**与文档块同强**的口径校验：``notes.deleted_at IS NULL``（笔记是**软删**，行会留着）
      **且** ``content_fingerprint((note.content or "").strip())`` 与记录一致
      （⚠️ ``.strip()`` 必须加：图通道构造 ``Material.content`` 时取的就是 ``(note.content or "").strip()``，
      见 ``app/retrieval/graph.py`` 的笔记分支；少这一步会**永远不命中**）；
    - **其余键**（网页材料等没有库内行的材料）⇒ 跳过，它们本来就没有可悬空的东西。

    ⚠️ **``回归修复``：两侧同源（读侧归一）**。写侧指纹算的是 `Material.content`——
    而**同一份库内正文在不同生产方手里会变成三种形态的文本**（2026-09-28 探针逐条实测，枚举以生产方为准）：

    - **raw（原样全量）**：`keyword._row_to_material`（`keyword.py:277`）与
      `keyword._note_to_material`（`:340`）——不 strip、不截断；文档块可达 4000 字
      （`回归修复`）；⚠️ **笔记正文是逐字落库的**（`api/note_routes.py:202/221` 不 strip）
      ⇒ 带首尾空白的笔记真的会以 raw 形态进写侧；
    - **strip 全量**：图通道 `graph.py:652`（文档块）/ `:625`（笔记）——`(x or "").strip()`、不截断；
    - **strip + 截断**：一跳扩展片段 `link.py:199`（文档）/ `:140`（笔记）——
      `strip()[:LINK_SNIPPET_CHARS]`（800）。

    修复前读侧只按「库内全量」算一个指纹 ⇒ 后两种形态的轮指纹必然失配、**整条缓存永久 miss**
    （探针实测：900 字源 → 写侧 `855be396…` / 读侧 `194c726b…` / 存活校验 False；
    **另一条独立实测**：带尾随换行的笔记经 keyword 路交出 raw，写侧 `7dfee331…` 对读侧 strip 口径
    `a2cbaace…` ⇒ 存活校验 False）。
    修复 = 读侧对每个键算**产侧全部三种投影**组成候选集（:func:`project_material_text` 是其中之一），
    **命中任一即算活**。方向与取舍见下一条。

    ⚠️ **候选集的语义代价（登记，不粉饰）**：判活口径变成「写侧那段文本 == 当前库内正文的
    **某一个投影**」，于是有三处「看不到的变化」：

    ① **一跳扩展片段**：写侧**根本没有**截断点之后的正文（生产方当场截掉了）⇒ 那一段的变化
       永远不可见。这是**信息本身不存在的固有代价**，本笔修不掉（修复前是「整条永久 miss」，
       更保守，但把整轮缓存白扔了）。
    ② **「恰好 800 字」的写侧文本 + 库内正文在其后追加**：截断候选是
       `project_material_text(当前正文)` = `strip(当前正文)[:800]`，
       它要与写侧文本逐字相等只有两种可能 —— 当前正文 == 写侧文本（正常命中），或者
       **写侧文本长度正好是 800** 且当前正文是它「原样 + 末尾追加」。后一种 ⇒ 判活。
       **这是本笔引入的新窗口**（修复前会判不活），条件是「长度恰好 800 + 只追加 + 前 800 字逐字未变」。
       ⚠️ 反过来，把第 800 字之后**改掉 / 删掉**仍会被检出（那已不是「原文本的延长」）。
       ⚠️ 要消掉 ② 必须让写侧把「材质文本长度」一并记进指纹（动 `CacheEntry.fingerprints`
       的取值格式 + 版本号 + 写回侧），**本笔不做**，登记在案。
    ③ **只差首尾空白的变化不可见**（`task-24` 补齐「raw / strip 全量」两个候选带来的**新代价**）：
       候选集同时含 `fp(raw)` 与 `fp(strip(raw))` ⇒ 库内正文若只是在**首尾**多/少空白
       （例如模型生成的笔记末尾那个换行被去掉），会被判活。修复前会判不活。
       ⚠️ 取舍：**不补这两个候选的代价是「该形态永久 miss」**——已在生产入口上实测到（带尾随换行的笔记
       经 keyword 路交出 raw，读侧只按 strip 算 ⇒ 存活校验 False）⇒ 两害相权取后者：首尾空白**不进
       正文语义**（渲染前一律 strip），影响面远小于「整轮缓存白扔」。

    ⚠️ **窗口边界（别一概而论）**：写侧材质**本身 >800 字**时（如 `回归修复` 之后
    `chunk_code` 的 4000 字符窗），截断候选只有 800 字，它的指纹**不可能**等于 >800 字材质的
    指纹 ⇒ **没有任何新增窗口**，第 800 字之后的改动照样检出；写侧材质**短于 800** 时截断候选就是
    整段 ⇒ 只有正文逐字未变才命中，**也没有新增窗口**；库里正文无首尾空白时 raw 与 strip 两个候选
    **恒等**（`content_fingerprint` 相同，集合自动去重）⇒「内容变了 ⇒ 判不活」在这些形态上
    **一点没放松**。写侧材质**恰好 800 字**（`RAG_CHUNK_SIZE` 的满块就是这种）时，才落在 ② 那个窗口里。

    ⚠️ **修复前的写法**只收正整数键、把非正 id **一律跳过** ⇒ 笔记材料**既不进指纹表、也永不被校验**
    （写回侧 `_cache_write_entry` 同样的过滤），于是「**笔记被删掉之后缓存照样命中、``refs`` 里还带着
    那条已经不存在的笔记**」——由 `5.6` 的增强组合矩阵在**三开格**里用只读探针实测暴露。

    ⚠️ **``回归修复`` 修的是同一条 SQL 的另一半**（2026-09-28 · 缺陷清单-2 六A）：软删
    **不删** ``chunks`` 行（``api/document_routes.delete_document`` 只置 ``documents.deleted_at``），
    所以「行还在 + 内容没变」这两条对**软删**完全不设防 —— 删掉被引用的文档之后同题再问，
    旧指纹照样比对得上 ⇒ 交出一条**引用回收站文档**的整轮答案（TTL 内持续）。修复 = chunk 支路
    **inner join ``documents`` 并滤 ``deleted_at IS NULL``**，与检索两支路
    （``retrieval/vector.py:162`` / ``retrieval/keyword.py:253`` 的 ``Document.deleted_at.is_(None)``）
    同源。软删文档的块**根本不进 ``found``**（不是「进了但值不同」——两条判据在最终结果上不可区分，
    但前者少一次内容比对）。边界两条：**孤儿块**（``document_id`` 指向不存在的行；正常路径不产生）
    由「算活」变「算不活」，方向保守（多一次未命中，不会错命中）；本支路**只滤 ``deleted_at``、
    不滤 ``index_status``** —— 文档正在 reindex（``processing``）而块内容尚未变时仍会命中，
    那是「内容确实没变」的另一回事，本笔不扩面（登记为残余面）。
    """
    entries = fingerprints or {}
    source_entries = {str(key): value for key, value in entries.items() if str(key).startswith("source:")}
    if source_entries:
        try:
            source_chunk_ids = []
            source_note_ids = []
            for key in source_entries:
                _, kind, identifier = key.split(":")
                if not identifier.isdigit() or int(identifier) <= 0:
                    return False
                if kind == "chunk":
                    source_chunk_ids.append(int(identifier))
                elif kind == "note":
                    source_note_ids.append(int(identifier))
                else:
                    return False
            found_sources: dict[str, str] = {}
            for start in range(0, len(source_chunk_ids), _LIVENESS_BATCH):
                batch, _ = await _source_fingerprints(
                    db, chunk_filter=Chunk.id.in_(source_chunk_ids[start:start + _LIVENESS_BATCH]),
                )
                found_sources.update(batch)
            notes, _ = await _source_fingerprints(db, note_ids=source_note_ids)
            found_sources.update(notes)
            if found_sources != source_entries:
                return False
        except Exception as exc:  # noqa: BLE001 —— 同既有查库失败降级
            logger.warning("缓存完整来源校验失败（本轮未命中）：%s", exc.__class__.__name__)
            return False
    chunk_ids = sorted(int(key) for key in entries if str(key).isdigit() and int(key) > 0)
    note_ids = sorted(
        int(str(key).split(":", 1)[1])
        for key in entries
        if str(key).startswith("note:") and str(key).split(":", 1)[1].isdigit()
    )
    if not chunk_ids and not note_ids:
        return True
    try:
        # 键 → **候选指纹集**（`回归修复`）：产侧可能交给写侧的每一种投影各算一个。
        # 命中任一即算「这份材料还是那一份」；≤800 且无首尾空白时集合里只有一个元素。
        found: dict[str, set[str]] = {}
        for start in range(0, len(chunk_ids), _LIVENESS_BATCH):
            batch = chunk_ids[start : start + _LIVENESS_BATCH]
            # 🔴 `回归修复`：**必须 join `documents` 并滤 `deleted_at IS NULL`**。
            # 理由见本函数 docstring 末段：软删不删 `chunks` 行 ⇒ 只查「块还在不在」对回收站
            # 完全不设防。口径与检索两支路（`retrieval/vector.py` / `retrieval/keyword.py`）一致。
            # ⚠️ 分批循环里这条 SQL **是唯一一条** chunk 查询，每个批次都带这个 join。
            rows = (
                await db.execute(
                    select(Chunk.id, Chunk.content)
                    .join(Document, Document.id == Chunk.document_id)
                    .where(Chunk.id.in_(batch), Document.deleted_at.is_(None), active_chunks())
                )
            ).all()
            for row in rows:
                content = row[1]
                # 产侧三种形态各算一个候选（`task-24` 由「两候选」补齐为「三候选」）：
                # ① **raw**：`keyword._row_to_material`（`keyword.py:277` 的 `row[2] or ""`）；
                # ② **strip 全量**：图通道文档块（`graph.py:652` 的 `(row[1] or "").strip()`）；
                # ③ **strip + 截断**：一跳扩展片段（`link.py:199`，走 :func:`project_material_text`）。
                # 当前所有 chunk 写入都会 strip ⇒ ①与②平时恒等（集合自动去重）；留 ② 是为了**枚举完整**
                # —— 库内行带首尾空白时 ①≠②，只留 ① 就是「图通道那一轮永久 miss」。
                found[str(int(row[0]))] = {
                    content_fingerprint(content),
                    content_fingerprint((content or "").strip()),
                    content_fingerprint(project_material_text(content)),
                }
        note_found: dict[str, set[str]] = {}
        for start in range(0, len(note_ids), _LIVENESS_BATCH):
            batch = note_ids[start : start + _LIVENESS_BATCH]
            rows = (
                await db.execute(
                    select(Note.id, Note.content, Note.deleted_at).where(Note.id.in_(batch))
                )
            ).all()
            for row in rows:
                if row[2] is not None:  # 软删 = 对使用者来说已经不在了
                    continue
                raw = row[1] or ""
                # 产侧三种形态（同 chunk 支路，`task-24` 补齐）：
                # ① **raw**：`keyword._note_to_material`（`keyword.py:340` 的 `row[2] or ""`）
                #    —— 笔记正文是**逐字落库**的（`note_routes.py:202/221` 不 strip）⇒ 真会出现；
                # ② **strip 全量**：图通道笔记（`graph.py:625`）；
                # ③ **strip + 截断**：一跳扩展笔记片段（`link.py:140`，走 :func:`project_material_text`）。
                note_found[f"note:{int(row[0])}"] = {
                    content_fingerprint(raw),
                    content_fingerprint(raw.strip()),
                    content_fingerprint(project_material_text(raw)),
                }
    except Exception as exc:  # noqa: BLE001 —— 校验查不动 ⇒ 保守当未命中（绝不炸穿，I5）
        logger.warning(
            "缓存存活校验查询失败（本轮按未命中处理）：%s: %s", exc.__class__.__name__, exc
        )
        return False
    for key, digest in ((str(k), str(v)) for k, v in entries.items()):
        # 候选集里**命中任一即算活**（`回归修复`）；键没有候选（行不在 / 软删）⇒ 不命中。
        if key.startswith("note:"):
            if digest not in note_found.get(key, ()):
                return False
        elif key.isdigit():
            if digest not in found.get(key, ()):
                return False
    return True


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "CacheEntry",
    "CacheKeyInputs",
    "build_key",
    "capture_fingerprints",
    "content_fingerprint",
    "deserialize_materials",
    "filters_token",
    "fingerprints_live",
    "history_token",
    "normalize_question",
    "project_material_text",
    "rebuild_stats",
    "retrieval_fingerprint",
    "serialize_materials",
]
