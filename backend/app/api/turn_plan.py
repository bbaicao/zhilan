"""一轮问答的装配数据类型（阶段 5 · 条目 5.2；回归修复 S2 切分）。

TurnPlan 自 app/api/chat_routes.py **逐字节搬迁**而来（只换包裹层，块内容零改动）。
切分理由（结构性断环）：app/api/chat_cache.py 需要 TurnPlan 做类型注解，若它从
app.api.chat_routes 导入，就会形成 chat_cache -> chat_routes -> chat_cache 的循环 import；
本模块只依赖 app.models / app.retrieval.rerank，**不 import app.api.chat_routes**，故环消失。

chat_routes 保留同名 re-export（from app.api.turn_plan import TurnPlan），既有外部面不变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from app.models import Chat
from app.retrieval.rerank import RerankStats
from app.retrieval.query import QueryRewrite


@dataclass
class TurnPlan:
    """一轮问答在「生成正文之前」的全部决定（同步/流式共用同一份）。"""

    chat: Chat
    question: str
    images: list[str]
    history: list[dict]
    filters: dict | None
    skill_ids: list[str]
    source_type: str
    kind: str
    answer_source: str | None
    # 最终引用（**正文产出后**由 ``finalize_citations`` 按 [n] 判定填入；此前恒空）
    refs: list = field(default_factory=list)
    # 本轮材料快照（融合后已编号 1..N）与**渲染正文**：同步与流式共用同一份，
    # 保证「模型看到的 [n]」与引用卡片同源（接线四条之 2）。
    materials: list = field(default_factory=list)
    materials_text: str = ""
    # **普通工具结果通道**（专项工具调用第 0 批 · ①）：``AgentTurn.tool_context`` 收口的
    # **结构化条目列表**（``ToolContextItem`` 列表），两个入口共用同一份（D14）。
    # ``[]`` = 本轮没有「未进资料区」的工具结果 ⇒ 最终回答请求体**逐字节 no-op**（D8）。
    # ⚠️ **这里存列表、不存渲染好的字符串**：渲染是 ``answer.render_tool_context`` 的职责，
    # 且只在 ``build_messages`` 那**一处**做。若在这里先渲染一次、到 ``build_messages``
    # 又渲染一次，那串字符串会被当成「一个没有字段的对象」再渲染一遍——正文全被吃掉，
    # 只剩一串「未知工具：成功」（**实测踩到过**：改后 A2 仍假红，排查点就在这）。
    # **一处渲染**是这条通道能成立的前提。
    # ⚠️ **不进** ``prompt_has_material()``（D3）；但按 D24 参与 ``system_prompt`` 的**选择**。
    tool_context: list = field(default_factory=list)
    # 本轮**全部**资料的 Ref 列表：引用判定的分母（越界判定用它，不是过滤后的集合）
    all_refs: list = field(default_factory=list)
    # 本轮检索快照 id（``rl-<12hex>``）：每条 Ref 与归因都带它，保证可溯源
    retrieval_id: str = ""
    # 需要模型生成的（clarify/refuse 不需要）
    needs_model: bool = False
    # 澄清轮的追问原文 / 拒答文案（needs_model=False 时直接用）
    preset_answer: str = ""
    # 内部生成结果；正文不是错误码，length 保留正文但不算完整成功。
    generation_status: str = "completed"
    finish_reason: str | None = None
    error_hint: str | None = None
    # 已达追问上限（正文前加 ASSUME_PREFIX）
    assume: bool = False
    turn: int = 1
    # Agent 编排（阶段 4 · 条目 4.1）：本轮**生效**模式（请求 → 设置页 → 带图强制 off）。
    # ``off`` = 走原来的单轮链路，行为与接 Agent 之前逐字一致。
    agent_mode: str = "off"
    # Agent 轮跑完后的轨迹（``None`` = 本轮没跑 Agent）。SSE ``done.agent_trace`` 与
    # ``messages.agent_trace`` 共用同一份（02 §7.1：非流式 /chat 恒 None）。
    agent_trace: dict | None = None
    # 本轮 ``agent_runs.id``（阶段 4 · 条目 4.3.2）：落 assistant 消息后回填 ``message_id`` 要用它。
    # ``None`` = 本轮没建审计行（审计开关关 / 没 db / 没 chat_id）。
    audit_run_id: int | None = None
    # 本轮**模型尝试**的 id（阶段 4 · 条目 4.4）：一次尝试恰好一个终态，
    # ``assistant/attempt`` / ``stream/interrupted`` / ``assistant/message`` 三处共用它。
    # ``None`` = 本轮不需要模型（clarify / refuse 走预置文案，不算一次尝试）。
    attempt_id: str | None = None
    retrieval_stats: RerankStats | None = None
    materials_finalized: bool = False
    # 跨会话记忆的**注入段**（阶段 5 · 条目 5.1；``""`` = 不注入）。由 ``build_turn_plan``
    # 调 ``memory.recall`` + ``memory.render_block`` 渲染好放这里，两条链路各自透传给
    # ``answer.system_prompt``。**内部字段**：``TurnPlan`` 不进任何帧，也不是 ``history`` 的一部分
    # （记忆进 history 会顶偏轮次号 / 污染澄清判定，见 ``app/session/memory.py`` 硬口径 2）。
    memory_text: str = ""
    # 原问继续给回答与落库；查询只给路由/检索。消歧必须使用消息表原历史，不能用压缩摘要。
    query_rewrite: QueryRewrite | None = None
    query_history: list[dict] = field(default_factory=list)
    # 本轮是不是**缓存命中**（阶段 5 · 条目 5.2；内部字段，不进任何帧）。
    # 命中的轮**没调模型**（连答案一起从缓存返回）⇒ ``persist_turn`` 据它跳过用量与记忆抽取
    # （``answer_succeeded`` 只认正文字面、拦不住），见 §0.0 第 5 行。
    # 它**不是**契约字段：``done`` 帧一个键都不加（两态键集必须完全一致）。
    cached: bool = False
    # 本轮准备前的缓存世代；准备/检索/生成中失效时，写回作废。内部字段，不进帧。
    cache_generation: int | None = None
    cache_key: str | None = None
    cache_fingerprints: dict[str, str] | None = None
    # 命中轮**缓存下来的模型原始正文**（未加展示前缀）；未命中轮恒 ``""``。
    # 展示前缀由 ``get_turn_prefix`` 在命中轮**现算**（它要按本轮 `final_empty_reason` 判）。
    cached_answer: str = ""
    # 命中轮缓存下来的**思考全文**（``done.reasoning`` 与 ``reasoning`` 帧的来源）。
    cached_reasoning: str = ""
    # 本轮检索的**向量路降级提示**（``retrieve()`` 第三返回值；阶段 5 · 条目 5.2 新增字段）。
    # 它**不是**新造语义：原先只在 ``build_turn_plan`` 里用完即弃（只喂给 ``retrieval/result``
    # 事件），5.2 的缓存条目要把它**一并存下**——命中轮的 ``retrieval/result`` 得与未命中轮
    # **逐字相同**（``degraded`` 由它派生，乱塞「来自缓存」会让它凭空变 true）。
    retrieval_warning: str | None = None

    @property
    def retrieval_query(self) -> str:
        return self.query_rewrite.retrieval_query if self.query_rewrite else self.question
