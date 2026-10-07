"""语义缓存簇（阶段 5 · 条目 5.2；回归修复 S2 切分）。

本模块内容自 app/api/chat_routes.py **逐字节搬迁**（只换包裹层）：在途写回任务集 _CACHE_TASKS，
以及语义缓存段（键构造 / 命中路径 / 写回）下的七个函数。chat_routes 保留同名 re-export，既有外部面不变。

两处**函数级延迟 import**（attempt_provider_model / answer_succeeded）是刻意的，禁止提到模块级：
这两个名字的外部 monkeypatch 栅栏打在 chat_routes 上（backend/tests/api/test_cache_note_liveness_5_6.py:74
与 backend/tests/api/test_agent_side_effects_t1.py:343），模块级绑定会让随迁过来的调用点绕过栅栏 ⇒ 静默 no-op。

TurnPlan 从 app/api/turn_plan.py 导入（不是在 chat_routes 里）：绕开 chat_cache <-> chat_routes 循环 import。
"""

from __future__ import annotations

import asyncio
import logging
import time
from sqlalchemy.ext.asyncio import AsyncSession
from app import observe, retrieval
from app.api.turn_plan import TurnPlan
from app.cache import entry as cache_entry, store as cache_store
from app.generation import answer as answer_mod, citation as citation_mod, source_map
from app.models import Chat
from app.retrieval.rerank import RerankStats

logger = logging.getLogger("app.api.chat_routes")  # 087 S2：沿用切分前的日志器名，日志口径逐字不变

# 在途缓存写回任务的**强引用**（asyncio 只留弱引用，不攥着可能中途被 GC）。
# 与 `session/memory.py::_PENDING` 同型；`drain_cache_tasks()` 是唯一等待点。
_CACHE_TASKS: set[asyncio.Task] = set()

# --------------------------------------------------------------------------
# 语义缓存（阶段 5 · 条目 5.2）：键构造 / 命中路径 / 写回
# --------------------------------------------------------------------------

def build_cache_key(
    *,
    question: str,
    filters: dict | None,
    agent_mode: str,
    skill_ids: list[str],
    images: list[str] | None,
    history: list[dict] | None,
    memory_text: str,
    source_type: str = "internal_db",
    retrieval_query: str | None = None,
    query_history: list[dict] | None = None,
) -> str | None:
    """构造精确缓存键（既有成分＋消歧策略／查询／原历史）；**本轮不缓存时返回 ``None``**。

    返回 ``None`` = 本轮**不进缓存面**（Agent 轮 / 带图轮，§0.0 第 17 行）——
    调用方照旧走完整链路，**零特殊逻辑**。关态也返回 ``None``（不白算哈希）。
    这两个条件与 ``cache.is_cacheable_plan`` 的前两条**同源同值**（常量取自那个模块）；
    第三条（``kind ∈ {normal}``）在 ``build_turn_plan`` 里判不了（此刻还没有 ``plan``），
    由收口侧的 ``maybe_store_cache`` 把关。

    模型身份（⑤）与检索配置指纹（⑨）在这里现算：

    - ⑤ = ``attempt_provider_model()`` 的 ``provider/model``——换模型后旧答案不得继续命中
      （否则「答案来源不可解释」）。读不到就是空串（与 ``assistant/attempt`` 同口径）；
    - ⑨ = :func:`app.cache.entry.retrieval_fingerprint`（embed / rerank 绑定 + 重排开关与门槛）
      ——**内容指纹查不出这一层**（块没变，变的是「怎么选块」）；
    - ⑩ = ``source_type``（同轮 ``RoutingDecision.source_type``，回归修复）：库内检索与库外
      直答是**两个不同的问题**，不进键就会「第二轮判 external 却回放第一轮的库内答案 + 引用行」。
      生产两处一律显式传 ``decision.source_type``（本函数的默认 ``"internal_db"`` 只为让既有**测试**
      调用点零改动：省略时取的值与修复前**唯一可能**的取值相同，见
      ``cache/entry.py::CacheKeyInputs.source_type`` 的口径；⚠️ 加成分后同一轮算出的 sha256 必然变
      ⇒ 存量条目键值不再匹配 —— **无需 bump ``CACHE_SCHEMA_VERSION``**（条目结构未变）；
      ⛔ 新增调用点必须显式传，漏传时库外轮会与库内轮同键 —— 残余面已登记）。

    任何一步读出坏值都只记警告并返回 ``None``（= 本轮不缓存）：缓存是**优化项**，
    绝不该因为取指纹失败而影响用户这一轮（I5）。
    """
    if not cache_store.enabled():
        return None
    if str(agent_mode or "off") not in cache_store.CACHEABLE_AGENT_MODES:
        return None
    if images:
        return None
    try:
        # 延迟 import（回归修复 S2）：attempt_provider_model 被 tests 按 chat_routes 属性 monkeypatch（backend/tests/api/test_cache_note_liveness_5_6.py:74），
        # 模块级 import 会与 chat_routes 那份绑定分叉 ⇒ 栅栏对随迁调用点变静默 no-op，故在调用点按模块属性解析：
        from app.api.chat_routes import attempt_provider_model
        provider, model_name = attempt_provider_model()
        inputs = cache_entry.CacheKeyInputs(
            question=question,
            filters=filters,
            agent_mode=agent_mode,
            skill_ids=skill_ids,
            model_label=f"{provider}/{model_name}",
            memory_text=memory_text,
            history=history,
            has_images=bool(images),
            retrieval_config=cache_entry.retrieval_fingerprint(),
            source_type=source_type,
            retrieval_query=retrieval_query,
            rewrite_history=query_history,
        )
        return cache_entry.build_key(inputs)
    except Exception as exc:  # noqa: BLE001 —— 键算不出来就当「本轮不缓存」
        logger.warning("缓存键构造失败（本轮不进缓存）：%s: %s", exc.__class__.__name__, exc)
        return None


def _cached_turn_plan(
    *,
    chat: Chat,
    text: str,
    images: list[str],
    history: list[dict],
    filters: dict | None,
    skill_ids: list[str],
    turn: int,
    agent_mode: str,
    assume: bool,
    memory_text: str,
    source_type: str,
    hit: cache_store.CacheHit,
) -> TurnPlan:
    """命中轮的一份 :class:`TurnPlan`（照正常出口的形状造，§2.4）。

    **缓存的是材料快照 + 模型原始正文 + 思考全文**；派生值一律**现算**，这样与未命中路径
    同源、不可能漂移：

    - ``retrieval_id`` **现铸**（**不许复用缓存里的**——它是「本轮检索快照 id」，
      复用会让两轮共享一个 id，3.3 的 trace 配对语义随之坏掉）；
    - ``all_refs`` 用**新 id** 重跑 ``citation.refs_from_materials``；
    - ``kind`` / ``answer_source`` 重新 ``source_map`` 判一次（缓存条目只存 ``normal``，
      但判据仍与未命中轮同一份实现）；
    - ``source_type`` **取同轮路由结论**（``decision.source_type``，回归修复）：
      ⛔ 不得写死 ``internal_db``——命中只可能发生在「键里的 ``source_type`` == 本轮决策」时
      （键的 ⑩ 成分就是它），所以这里的决定与同轮 ``routing/decided`` 事件**必然一致**；
      一旦写成字面量，两轮同题判不同 ``source_type`` 时就会出现「本轮说 external、答案与引用
      却按 internal_db 回放」的自相矛盾；
    - ``materials_finalized=True``——否则 ``final_material_state`` 返回 ``{}`` ⇒
      ``turn/end`` 会**少两个键**，违反 02 的「只增不减」；
    - ``final_empty_reason`` 从缓存条目的 ``retrieval_stats``（**派生结果的快照**）
      原样回填（``RerankStats.empty_reason`` 是只读 property ⇒ 不能赋值，只能存再塞回）。

    ⚠️ 这里**不写** ``refs``：它由 ``finalize_citations`` 在正文产出后按 ``[n]`` 定
    ——与未命中轮**同一个函数**（两条链路都走它），所以命中轮不用自己算一遍。
    """
    materials = list(hit.materials or [])
    retrieval_id = retrieval.new_retrieval_id()
    all_refs = citation_mod.refs_from_materials(materials, retrieval_id)
    kind, answer_source, _refs = source_map.map_round(
        source_type,
        has_materials=answer_mod.has_materials(materials=materials or None),
        direct_enabled=True,
    )
    stats = hit.stats
    if stats is None and hit.entry.final_empty_reason is not None:
        # 统计形状读不回来 ⇒ 用缓存里记的**派生原因**把它反推回来，保证
        # `final_material_state` 给出的 `final_empty_reason` 与未命中轮**一致**。
        stats = RerankStats()
        if hit.entry.final_empty_reason == "no_recall":
            stats.status = "no_candidates"
        elif hit.entry.final_empty_reason == "below_threshold":
            stats.status = "applied"
    return TurnPlan(
        chat=chat,
        question=text,
        images=images,
        history=history,
        filters=filters,
        skill_ids=skill_ids,
        source_type=source_type,
        kind=kind,
        answer_source=answer_source,
        # 材料快照已带缓存时的编号 1..N（`number()` 在写回前已执行），不再重编
        materials=materials,
        materials_text=hit.entry.materials_text,
        all_refs=all_refs,
        retrieval_id=retrieval_id,
        needs_model=True,
        assume=assume,
        turn=turn,
        agent_mode=agent_mode,
        retrieval_stats=stats,
        retrieval_warning=hit.entry.retrieval_warning,
        materials_finalized=True,
        memory_text=memory_text,
        cached=True,
        cached_answer=hit.entry.raw_answer,
        cached_reasoning=hit.entry.reasoning,
    )

async def _log_cached_retrieval(db: AsyncSession, plan: TurnPlan) -> None:
    """命中轮补上 ``retrieval/*`` **一对事件**（§2.4 第 7 条）。

    真检索没发生，但事件**照发**，理由两条：

    1. 4.4 的「本轮事件完整性」按 ``(chat_id, turn)`` 分桶——缺这两条会被记成「本轮缺行」；
    2. 3.3 的 trace 语义是「一次检索 = 一对事件」，命中轮**仍是一次「取得材料」的过程**。

    ⚠️ 这是本包的一处**工程裁决**（另一种做法是「命中轮不发、并同步放宽分桶判据」，
    那要动 4.4 的机制）——选前者：**不动 4.4**，事件照发。

    ⚠️ 载荷里的 ``warning`` **沿用缓存条目里那一个**（语义是「向量路降级」，
    **不是**「命中缓存」）：``log_retrieval_result`` 会由它派生 ``degraded``，
    乱塞一句中文会让 ``degraded`` 在命中轮凭空变 true，与未命中轮**不一致**。
    「这一轮的材料来自缓存」由 ``hits`` / ``rerank`` 等**既有字段**如实反映
    （材料条数、重排状态与数字都在），**不另外加键**（加键会打红既有精确断言）。
    """
    await observe.log_retrieval_start(
        db,
        plan.chat.id,
        question=plan.question,
        filters=plan.filters,
        retrieval_id=plan.retrieval_id,
        turn=plan.turn,
    )
    await observe.log_retrieval_result(
        db,
        plan.chat.id,
        materials=plan.materials,
        retrieval_id=plan.retrieval_id,
        warning=plan.retrieval_warning,
        turn=plan.turn,
        stats=plan.retrieval_stats,
    )


def _cache_write_entry(plan: TurnPlan, raw_answer: str, reasoning: str | None):
    """把这一轮打包成 :class:`app.cache.entry.CacheEntry`（写回侧，§2.5）。"""
    stats = plan.retrieval_stats
    captured = getattr(plan, "cache_fingerprints", None)
    fingerprints: dict[str, str] = dict(captured) if captured is not None else {}
    # 旧直接 helper 调用未带准备阶段字段；生产 TurnPlan 一律使用完整来源快照。
    for item in ([] if captured is not None else plan.materials or []):
        # 笔记材料的 `chunk_id` 是负数占位（`note_chunk_id`），**不能**拿它当键——它既不是
        # `chunks.id`、也认不出该去查哪张表。笔记有**自己的** `notes` 行，所以按 `note:<id>` 单列
        # （`回归修复` 修复：修复前非正 id 一律被过滤 ⇒ 笔记材料既不进指纹、也永不被存活校验，
        # 于是「笔记被删掉之后缓存照样命中、refs 里还带着那条已经不存在的笔记」）。
        note_id = getattr(item, "note_id", None)
        if note_id is not None:
            fingerprints[f"note:{int(note_id)}"] = cache_entry.content_fingerprint(item.content)
            continue
        chunk_id = int(getattr(item, "chunk_id", 0) or 0)
        if chunk_id > 0:
            fingerprints[str(chunk_id)] = cache_entry.content_fingerprint(item.content)
        # 其余（网页材料等）没有库内行 ⇒ 本就没有可悬空的东西，继续跳过
    # 延迟 import（回归修复 S2）：attempt_provider_model 被 tests 按 chat_routes 属性 monkeypatch（backend/tests/api/test_cache_note_liveness_5_6.py:74），
    # 模块级 import 会与 chat_routes 那份绑定分叉 ⇒ 栅栏对随迁调用点变静默 no-op，故在调用点按模块属性解析：
    from app.api.chat_routes import attempt_provider_model
    provider, model_name = attempt_provider_model()
    return cache_entry.CacheEntry(
        created_at=time.monotonic(),
        question=cache_entry.normalize_question(plan.question),
        materials=cache_entry.serialize_materials(plan.materials),
        materials_text=plan.materials_text or "",
        # 向量路降级提示：写回时一并存下 ⇒ 命中轮的 retrieval/result 事件与未命中轮**逐字相同**
        # （`log_retrieval_result` 由它派生 `degraded`；乱塞一句「来自缓存」会让 degraded 凭空变 true）
        retrieval_warning=plan.retrieval_warning,
        retrieval_stats=(stats.payload() if stats is not None else None),
        final_empty_reason=(stats.empty_reason if stats is not None else None),
        raw_answer=str(raw_answer or ""),
        reasoning=str(reasoning or ""),
        fingerprints=fingerprints,
        model_label=f"{provider}/{model_name}",
        memory_text=plan.memory_text or "",
    )


def maybe_store_cache(plan: TurnPlan, raw_answer: str, reasoning: str | None) -> None:
    """未命中路径的**写回**（挂在端点里，与 ``retrieval/*`` 的收口同一位置）。

    §2.5 的三条口径：

    - **只在成功收口之后写**（材料与正文都已定案）；
    - **只写「成功且值得缓存」的轮**：``answer_succeeded`` 为真 **且** ``plan.kind == "normal"``
      （``refuse`` / ``clarify`` 走预置文案、``direct`` 无材料 ⇒ 首版都不缓存，减少状态面）；
    - **写失败一律吞**（``store_snapshot`` 内部 warning + 计数）：缓存是优化项，
      **绝不**因为写不进去而影响本轮（I5）。

    U6：使用准备阶段的键和完整来源快照。收口仍复算配置指纹，
    配置在准备/生成之间发生改变时拒写，防止把旧答案挂到新配置的键下。
    """
    if plan.cached:
        return
    if plan.cache_generation is None:
        return
    original_key = getattr(plan, "cache_key", None)
    if not original_key or getattr(plan, "cache_fingerprints", None) is None:
        return
    # 收口判据**只有一份**（`cache.is_cacheable_plan`）：agent_mode == off **且** 无图
    # **且** kind ∈ {normal}。三条全过才写回——``refuse`` / ``clarify`` 走预置文案、
    # ``direct`` 无材料，首版都不缓存（§2.5，写进证据的「有意限制」）。
    if not cache_store.is_cacheable_plan(plan):
        return
    # 延迟 import（回归修复 S2）：answer_succeeded 被 tests 按 chat_routes 属性 monkeypatch（backend/tests/api/test_agent_side_effects_t1.py:343），
    # 模块级 import 会与 chat_routes 那份绑定分叉 ⇒ 栅栏对随迁调用点变静默 no-op，故在调用点按模块属性解析：
    from app.api.chat_routes import answer_succeeded
    if not answer_succeeded(plan, raw_answer):
        return
    cache_key = build_cache_key(
        question=plan.question,
        filters=plan.filters,
        agent_mode=plan.agent_mode,
        skill_ids=plan.skill_ids,
        images=plan.images,
        history=plan.history,
        memory_text=plan.memory_text,
        # ⑩ 取 plan 定案后的 ``source_type``：能走到这一行 ⟹ ``kind ∈ {normal}``（上面那道
        # ``is_cacheable_plan`` 关），而 ``resolve_kind`` 只有「``internal_db`` 且有材料」才给
        # ``normal`` ⇒ 此刻 ``plan.source_type`` 与同轮 ``decision.source_type`` **必然相等**
        # （Agent 面那三处改写 source_type 的分支只在 ``agent_mode != "off"`` 时发生，
        # 而那种轮已被 ``is_cacheable_plan`` 挡掉）。故写回键与查找键同源、不可能分叉。
        source_type=plan.source_type,
        retrieval_query=plan.retrieval_query,
        query_history=plan.query_history,
    )
    if not cache_key or cache_key != original_key:
        return
    schedule_cache_write(
        cache_key, _cache_write_entry(plan, raw_answer, reasoning),
        generation=plan.cache_generation,
    )


def schedule_cache_write(cache_key: str, entry, *, generation: int | None = None) -> None:
    """把写回排成一个**后台任务**（不在请求路径上等它）。

    为什么不直接 ``await``：写回要抢 ``app/cache/store.py`` 的模块级 ``asyncio.Lock``，
    而**并发请求会互相等**；写回又完全不影响本轮结果（失败了也只是少一条缓存）。
    与 ``session/memory.py::schedule_extract`` 同型：fire-and-forget + 强引用集合 + 吞异常。

    :func:`drain_cache_tasks` 是**唯一等待点**（测试收尾用；生产路径不调用）。

    端点写回携带**本轮准备前世代**；准备、检索、生成或排写后发生 ``invalidate_all``，
    旧快照都在**落表前**整条丢弃。直接排写的调用点未传世代时，仍在排期时捕获，
    保持既有接口；**同一代内**仍是「第一次写入者胜出」。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 纯同步上下文：不写缓存，记一行说明
        logger.warning("缓存写回未执行（无可用事件循环）：key=%s…", str(cache_key)[:8])
        return
    if generation is None:
        generation = cache_store.current_generation()
    task = loop.create_task(
        cache_store.store_snapshot(cache_key, entry, generation=generation)
    )
    _CACHE_TASKS.add(task)
    task.add_done_callback(_CACHE_TASKS.discard)


async def drain_cache_tasks() -> None:
    """等在途缓存写回任务跑完（**唯一等待点**；测试收尾用，生产路径不调用）。"""
    for _ in range(100):
        if not _CACHE_TASKS:
            return
        await asyncio.gather(*list(_CACHE_TASKS), return_exceptions=True)
        await asyncio.sleep(0)
