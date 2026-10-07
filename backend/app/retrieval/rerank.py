"""可选重排（阶段 3 · 条目 3.1 · 已定口径 D-c）。

- **默认关**：新增配置键 ``RAG_ENABLE_RERANK``（``bool=False``）。关着的时候**行为与开启前逐字一致**
  ——连服务解析都不做；
- **开了但未绑定 rerank 服务**：记一条中文警告，**回落 RRF 顺序**（顺序不变）；
- **开了且调用失败 / 返回形状不对**：同样警告 + 回落（**不炸穿**，I5）；
- 开了且成功：写 ``rerank_score`` → **丢掉低于门槛的** → 按重排分降序（同分按 ``chunk_id``
  升序，保证可重复）。

**门槛**（02 §7.6 的 ``rerank.threshold``，回归修复 接上）：取值
``settings.RAG_MIN_RERANK_SCORE``（默认 :data:`DEFAULT_MIN_RERANK_SCORE` = ``0.20``，与老基线
同款）；语义 = **「低于门槛的片段不作为引用」**，``<= 0`` 视为不过滤（等价关掉门槛）。它
**只在重排成功这条路径上生效**——回落路径手里是 RRF 融合分（或压根没有分），与重排分
**不同量纲**，拿重排门槛去卡它只会凭空删材料。该键现在**已声明**在 ``app/core/config.py``
（``RAG_MIN_RERANK_SCORE: float = 0.20``）；这里仍一律 ``getattr`` 兜底读取（键缺失时回落
:data:`DEFAULT_MIN_RERANK_SCORE`），两边互不阻塞，且两个 ``0.20`` 有用例钉住不漂移。

服务从 ``MODEL_ROLES`` 的 **rerank 角色**取（与 chat / embed / vision 同一套注册表）；
未绑定时不做任何网络调用。``_call_rerank`` 是唯一出网点，单测 monkeypatch 它即可覆盖
「成功 / 失败 / 形状不对」三条路径，不需要真服务。

**两种调用形态**（回归修复：地址与请求体都得跟着**服务商**走，不能一套扁平体打天下）：

- **百炼原生**（``provider`` 归一化后为 ``dashscope``）：终点 = 截掉 ``/compatible-mode``
  后拼 ``/api/v1/services/rerank/text-rerank/text-rerank``，请求体是嵌套形态
  ``{"input": {...}, "parameters": {...}}``；
- **其余**（Jina / Cohere / 自建等）：终点 = ``base_url + "/rerank"``，请求体是 OpenAI 兼容
  扁平形态 ``{"model", "query", "documents", "top_n"}``。

判据来自旧基线**实现**（只读对照 ``<legacy-repo>\\backend\\app\\rag\\providers\\rerank.py:26-42``
与 ``rerank_spec.py:57`` 的 ``base_url_hint``）；响应两种形态都按 ``index`` 归位解析。
⚠️ 本机**未做真机重排调用**（纪律：不联网），「compatible-mode 没有 ``/rerank``」这条是
**依据旧适配器专门转换 + 厂商文档的推断**，不是实测。

**对抗性复核本轮实测抓到的三处形态判据缺陷**（均已在下面各函数里修掉）：

- **P3-2**：端点判据用**主机子串** ``"dashscope.aliyuncs.com" in base`` ⇒ 漏掉国际站
  ``dashscope-intl.aliyuncs.com``（子串不匹配）⇒ 落到「追加 ``/rerank``」分支 ⇒
  compatible-mode 下打不存在的 ``/rerank`` ⇒ 404。故端点选择改**按 provider**，主机只作兜底；
- **P3-3**：请求体形态原先看「端点字符串里有没有原生段」⇒ 自建网关
  ``https://gw.corp/api/v1/services/rerank/text-rerank``（≠ 百炼）被发嵌套体 ⇒ 上游 400。
  现在形态**只认 provider**；
- **P3-1**：原生地址加固只堵「完整路径」，用户填半截
  ``https://dashscope.aliyuncs.com/api/v1/services/rerank`` 仍被二次拼成
  ``…/services/rerank/api/v1/services/rerank/text-rerank/text-rerank``。判据放宽到**前缀**
  （:data:`_NATIVE_RERANK_PREFIX`）⇒ 原样使用，让上游去报 404。

另：门槛过滤**删掉条目**时会留一行 ``warning``（门槛值 + 删前/删后条数 + 被删分数区间）——
这是该行为**当前唯一的可观测痕迹**，详见 :func:`apply_rerank`。

⚠️ 本条目白名单限定 ``app/core/config.py`` **只新增 ``RAG_ENABLE_RERANK`` 一行**，故这里不新增
``settings.rerank_config`` 公开属性，而是复用既有唯一的「服务 → ModelConfig」解析
（``settings._service_config``，含厂商预设里的 base_url 兜底）——与 chat/embed/vision 走的是
**同一条**解析路径，不另立第二套。
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass

import httpx

from app.core.config import settings
from app.retrieval.types import Material
from app.usage import record_usage

log = logging.getLogger(__name__)


@dataclass
class RerankStats:
    """一次调用的出参；未执行过滤的 kept/removed 不适用，不能记为零。"""

    status: str | None = None
    model: str | None = None
    threshold: float | None = None
    candidates: int = 0
    kept: int | None = None
    removed: int | None = None
    removed_score_min: float | None = None
    removed_score_max: float | None = None
    query_rewrite: dict | None = None
    threshold_applied: bool = False
    links: dict | None = None
    recall: dict | None = None
    rerank_input: list[dict] | None = None
    rerank_output: list[dict] | None = None
    selected: list[dict] | None = None

    @property
    def empty_reason(self) -> str | None:
        if self.status == "no_candidates":
            return "no_recall"
        if self.status == "applied" and self.kept == 0 and self.candidates > 0:
            return "below_threshold"
        return None

    def payload(self) -> dict:
        data = asdict(self)
        rewrite = data.pop("query_rewrite")
        links = data.pop("links")
        payload = {"rerank": data, "empty_reason": self.empty_reason}
        if rewrite is not None:
            payload["query_rewrite"] = rewrite
        if links is not None:
            payload["links"] = links
        return payload

RERANK_ROLE = "rerank"
RERANK_TIMEOUT = 20.0
# 重排门槛的**兜底默认值**（02 §7.6 的 ``rerank.threshold``；老基线 ``RAG_MIN_RERANK_SCORE``
# 默认同为 0.20）。真正取值走 ``settings.RAG_MIN_RERANK_SCORE``，本常量只负责「键还没落地
# 时用什么」以及给用例一个数值锚点，避免 0.20 这个字面量散落多处。
# ⚠️ 它与 ``config.Settings.RAG_MIN_RERANK_SCORE`` 的默认值是**两份字面量**，漂移不会自动红；
# 用例 ``test_default_threshold_matches_config_key_default`` 把两边钉住（P3 级问题，顺手加固）。
DEFAULT_MIN_RERANK_SCORE = 0.20
# 百炼**原生**重排终点路径（回归修复）：compatible-mode 下没有 ``/rerank`` 路由，
# 必须截到域名再拼这一段。判据见 :func:`_rerank_endpoint`。
NATIVE_RERANK_PATH = "/api/v1/services/rerank/text-rerank/text-rerank"
# 原生重排路径的**前缀**（回归修复 复核 P3-1）：``base_url`` 里出现这一段就说明用户
# 填的是（完整或半截的）原生地址 ⇒ 一律**原样使用**，别再拼接。
# 它取代了原先那个「完整路径」判据 ``_NATIVE_RERANK_MARKER``：那个只堵完整地址，用户填到
# ``…/api/v1/services/rerank`` 会被二次拼成 ``…/services/rerank/api/v1/services/…``；
# 同时它也不再参与请求体形态判定（形态只认 provider，见 :func:`_rerank_payload`）。
_NATIVE_RERANK_PREFIX = "/api/v1/services/rerank"


def enabled() -> bool:
    """开关状态（读 ``RAG_ENABLE_RERANK``，默认 False）。"""
    return bool(getattr(settings, "RAG_ENABLE_RERANK", False))


def resolve_rerank_config():
    """取 rerank 角色的服务配置；未绑定 / 绑定已失效 → ``None``（不抛）。"""
    raw = (settings.MODEL_ROLES or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    item = data.get(RERANK_ROLE) if isinstance(data, dict) else None
    if not isinstance(item, dict):
        return None
    service = settings.service_by_id(str(item.get("service_id") or "").strip())
    model = str(item.get("model") or "").strip()
    if service is None or not model:
        return None
    try:
        return settings._service_config(service, model)
    except Exception as exc:  # noqa: BLE001 —— 配置坏数据按「没配好」处理
        log.warning("重排服务配置不可用：%s", exc)
        return None


def _rerank_endpoint(base_url: str, provider: str = "") -> str:
    """把服务商 base_url 解析成**重排调用地址**（回归修复；形态判据按**服务商**）。

    判据（顺序即优先级）：

    1. ``base`` 里含 :data:`_NATIVE_RERANK_PREFIX` ⇒ **原样返回**——用户填的已经是（完整或
       半截的）原生地址。**v2 加固（旧基线没有这一支，属有意改进）**：旧基线在「用户把原生
       终点地址当 base_url 填」时会**再拼一遍**（找不到 ``/compatible-mode`` 可截断 ⇒
       ``…/text-rerank/text-rerank/api/v1/…``）；迁移会让用户更容易看到并粘贴那个地址，故显式
       堵掉。本轮把判据从「完整路径」放宽到**前缀**（P3-1 实测）：只填到
       ``…/api/v1/services/rerank`` 时也原样使用，让上游去报 404，而不是拼出一串更离谱的东西；
    2. ``provider`` 归一化后是 ``dashscope``，**或**（provider 缺失时的兜底判据）``base`` 里
       **同时**含 ``dashscope`` 与 ``aliyuncs.com`` ⇒ 截掉 ``/compatible-mode`` 之后的部分再拼
       :data:`NATIVE_RERANK_PATH`；
    3. ``base`` 以 ``/rerank`` 结尾 ⇒ 原样使用（用户直接填了终点地址）；
    4. 其余 ⇒ ``base + "/rerank"``（OpenAI 兼容风格，Jina / Cohere / 自建）。

    **为什么 2 按 provider 而不是按主机名子串**（P3-2 实测）：旧判据
    ``"dashscope.aliyuncs.com" in base`` 是**子串**判断，国际站主机
    ``https://dashscope-intl.aliyuncs.com/compatible-mode/v1`` **不含**这个子串 ⇒ 落到第 4 步
    ⇒ 打到 compatible-mode 下并不存在的 ``/rerank`` ⇒ 404。改成按 ``provider`` 判形态后，
    **intl 主机同样走原生路径**（同一家服务商、同一套原生路由）；主机判据只作为「老 ``.env``
    只填地址、没填 provider」时的兜底，故两段关键词都要有，避免误伤路径里恰好带
    ``dashscope`` 一词的代理地址。

    为什么 DashScope 要单独走一条：百炼的 base_url 契约给的是 **compatible-mode** 地址
    （``rerank_spec.py:57`` 的 ``base_url_hint``，v2 的 dashscope 预设同址），而
    compatible-mode 下**没有** ``/rerank`` 路由——旧适配器专门做这次转换才调得通。
    ⚠️ 本机**未做真机重排调用**（纪律：不联网），故「compatible-mode 无 ``/rerank``」
    是**依据旧实现与厂商文档的推断**，不是实测。

    ``provider`` 默认空串，是为了让「只看地址」的老调用点行为不变；生产路径由
    :func:`_call_rerank` 从 ``ModelConfig.provider`` 显式传入（``getattr`` 兜底）。
    """
    base = (base_url or "").rstrip("/")
    if _NATIVE_RERANK_PREFIX in base:
        return base
    if (provider or "").strip().lower() == "dashscope" or (
        "dashscope" in base and "aliyuncs.com" in base
    ):
        return base.split("/compatible-mode")[0] + NATIVE_RERANK_PATH
    if base.endswith("/rerank"):
        return base
    return base + "/rerank"


def _rerank_payload(
    provider: str, model: str, query: str, documents: list[str], top_n: int
) -> dict:
    """按**服务商**选请求体（回归修复；两种形态同出旧基线 ``providers/rerank.py:38-42``）。

    - ``provider`` 归一化后是 ``dashscope`` ⇒ **原生嵌套体** ``{"model", "input": {"query",
      "documents"}, "parameters": {"top_n", "return_documents": False}}``，``top_n`` 照旧基线取
      ``min(top_n, len(documents))``；
    - 其余（Jina / Cohere / 自建 / 未知）⇒ **OpenAI 兼容扁平体**
      ``{"model", "query", "documents", "top_n"}``。

    **形态只认 provider**（P3-3 实测的成因正是「按端点字符串判形态」）：自建网关
    ``https://gw.corp/api/v1/services/rerank/text-rerank`` 的**路径恰好含原生段**，但它不是百炼，
    旧实现凭「端点里含该段」给它发嵌套体 ⇒ 上游 400。故这里**不再看端点/路径里有什么**，
    参数也从 ``endpoint`` 换成了 ``provider``（旧签名是形态的错觉来源，直接删掉）。

    已知的**有意不对称**：端点侧（:func:`_rerank_endpoint`）对「主机含 ``dashscope`` +
    ``aliyuncs.com``」仍兜底走原生**地址**，此时若 ``provider`` 不是 ``dashscope``，会出现
    「原生地址 + 扁平体」——provider 是权威判据，地址只是老配置的兜底，两者判据不强行求同。
    """
    if (provider or "").strip().lower() == "dashscope":
        return {
            "model": model,
            "input": {"query": query, "documents": documents},
            "parameters": {
                "top_n": min(top_n, len(documents)),
                "return_documents": False,
            },
        }
    return {"model": model, "query": query, "documents": documents, "top_n": top_n}


def _coerce_index(value: object) -> int | None:
    """把返回体里的 ``index`` 收敛成整数下标；认不出返回 None，整批拒绝。

    回归修复 复核 P3-4（实测）：原实现 ``int(index) if isinstance(index, int) else position``
    **只认 int**，于是 ``[{"index": "1"}, {"index": "0"}]`` 这种字符串下标被当成「没有 index」，
    按**返回顺序**对齐 ⇒ 两条分数直接错位。老基线是 ``int(item["index"])``（``providers/rerank.py:62``），
    比「只认 int」宽容，所以这里跟上：``int`` / **整数值** ``float``（``1.0``）/ 数字字符串（``"1"``、
    ``" 1 "``）都认。

    刻意**不认**的两类：``bool``（``True`` 当 1 用是意外，不是下标）、非整数
    （``1.9`` / ``"1.9"``）——不能截断或猜位置，把分数安到别人头上。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (float, str)):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        # NaN / inf 的 ``is_integer()`` 为 False，天然被挡在外面
        return int(number) if number.is_integer() else None
    return None


def _parse_rerank_scores(body: object, count: int) -> list[float]:
    """把返回体解析成**与 documents 同序**的分数表；形状不对 ⇒ ``RuntimeError``。

    兼容两种常见返回：``results: [{index, relevance_score|score}]``（Jina / Cohere / 多数
    兼容实现）与 DashScope 原生 ``output.results``（同字段）。

    index 必须完整、唯一且在候选范围内；支持整数、整数浮点和数字字符串。
    分数必须明确给出且有限，负分合法。当前支持的协议没有无 index 原序保证；
    任一行不可可信匹配即整批拒绝，由 apply_rerank 回融合排序，不猜位置或补零分。
    """
    rows = body.get("results") if isinstance(body, dict) else None
    if rows is None and isinstance(body, dict):
        output = body.get("output")
        if isinstance(output, dict):
            rows = output.get("results")
    if not isinstance(rows, list) or len(rows) != count or not rows:
        raise RuntimeError("重排服务 results 与候选数量不一致")

    scores = [0.0] * count
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("重排服务 results 行必须为对象")
        index = _coerce_index(row.get("index"))
        if index is None or not 0 <= index < count or index in seen:
            raise RuntimeError("重排服务 index 缺失、非法或重复")
        value = row.get("relevance_score", row.get("score"))
        if isinstance(value, bool):
            raise RuntimeError("重排服务分数不能为布尔值")
        try:
            score = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("重排服务分数缺失或非法") from exc
        if not math.isfinite(score):
            raise RuntimeError("重排服务分数必须为有限数值")
        scores[index] = score
        seen.add(index)
    return scores


async def _call_rerank(config, query: str, documents: list[str]) -> list[float]:
    """调重排服务，返回**与 documents 同序**的分数。

    地址与请求体都按**服务商**（``config.provider``）选形态（见 :func:`_rerank_endpoint` /
    :func:`_rerank_payload`）：百炼走原生嵌套体，其余走 OpenAI 兼容扁平体；响应两种形态都能
    解析（:func:`_parse_rerank_scores`）。``top_n = len(documents)``、超时 ``RERANK_TIMEOUT``、
    ``>= 400`` 抛 ``RuntimeError``（带响应体前 200 字）——都由 :func:`apply_rerank` 兜住回落。

    回归修复 复核 P3-2：``provider`` 在这里**显式透传**（``getattr`` 兜底，允许测试里的
    简易配置对象）——端点选择不再只看 ``base_url`` 主机子串，国际站主机才不会落错分支。

    每次实际 HTTP 尝试由 observed_post 记录，包含失败与停止；厂商未给 usage 的列为
    NULL，费用保持未知。HTTP 成功不证明计费或应用接受；形状校验与调用数独立。
    ``_call_rerank`` 是**唯一出网点**，单测 monkeypatch 它即可完全绕开记录（不碰真库）。
    """
    provider = getattr(config, "provider", "")
    endpoint = _rerank_endpoint(config.base_url, provider)
    payload = _rerank_payload(provider, config.model, query, documents, len(documents))
    headers = {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}
    from app.usage.instrumentation import observed_post
    from app.core.app_logging import redact
    async with httpx.AsyncClient(timeout=RERANK_TIMEOUT) as client:
        resp = await observed_post(
            client, endpoint, config=config, kind="rerank", headers=headers, json=payload
        )
        if resp.status_code >= 400:
            text = resp.text.replace(str(config.api_key), "***已脱敏***") if config.api_key else resp.text
            raise RuntimeError(f"HTTP {resp.status_code} {redact(text)[:200]}")
        body = resp.json()
    return _parse_rerank_scores(body, len(documents))


def _record_rerank_usage(config, body: object) -> None:
    """一次重排调用成功后记一条用量（fire-and-forget，绝不抛；拿不到 tokens 就 NULL）。"""
    usage = body.get("usage") if isinstance(body, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    prompt = usage.get("prompt_tokens")
    if prompt is None:
        prompt = usage.get("input_tokens")  # 百炼原生体用 input_tokens
    record_usage(
        kind="rerank",
        provider=str(getattr(config, "provider", "") or ""),
        model=str(getattr(config, "model", "") or ""),
        prompt_tokens=prompt,
        completion_tokens=usage.get("completion_tokens"),
        total_tokens=usage.get("total_tokens"),
    )


async def apply_rerank(
    question: str, materials: list[Material], *, stats: RerankStats | None = None
) -> list[Material]:
    """按开关与配置决定是否重排；**任何异常路径都返回原顺序**（不抛穿）。

    重排成功时额外做一层**门槛过滤**：写完 ``rerank_score`` 后，**低于门槛的片段不作为
    引用**（直接从快照里删掉），再按分降序返回。门槛 = ``settings.RAG_MIN_RERANK_SCORE``
    （← 02 §7.6 的 ``rerank.threshold``；默认 :data:`DEFAULT_MIN_RERANK_SCORE`）；
    ``threshold <= 0`` 时**不过滤**，等价于把门槛关掉。判定直接用**刚写入**的
    ``item.rerank_score``，不重解析 ``scores``（同一份真相，少一次解析就少一处能对不上）。

    三条刻意的边界：

    - **只在成功路径过滤**。回落路径（开关关 / 未绑定 / 调用失败 / 返回形状不对）手里是
      **RRF 融合分**或压根没有分，与重排分**不同量纲**；拿重排门槛去卡它，会把本来合规的
      材料凭空删掉，故那些分支逐条原样返回。
    - **过滤后可能全空，而这是预期语义**：该轮没有达标材料 ⇒ 调用方（``retrieve()`` →
      ``render_materials`` → 「无资料」分支）按「没有查到能引用的东西」继续。「至少留一条」
      的保底会让门槛形同虚设（分数再低也留一条），故**不保底**。
    - **只删条目、不重编号**。``Material.ordinal`` 是本轮**快照**编号，渲染正文的 ``[n]``
      与条目 3.2 的 ``Ref.ordinal`` 共用同一份；若就地重编，会让「模型看到的编号」与引用
      卡片错位（编号是快照契约，引用同源），所以宁可让编号**留空档**（例如剩 1、3）也不
      重编。常规 ``retrieve()`` 路径在**本函数之后**才调 ``number()``，那时编号天然连续，
      空档只在「喂进来的快照已编号」时出现（如单元测试、后续调用方）。

    **可观测性**：过滤**确实删掉了条目**时，
    这里打一行 ``log.warning``，内容含**门槛值 + 过滤前条数 + 过滤后条数 + 被删条目的分数区间**
    （形如「重排门槛 0.20 过滤掉 3/7 条材料（被删分数区间 0.02–0.19）：本次仅保留 4 条」）。
    为什么必须留痕：实测该过滤原先**零日志**，而下游 ``session_events`` 的 ``retrieval/result``
    只记 ``hits=0 / degraded=false / warning=null``——事后**分不出**「真零命中」与「全被门槛删」
    （后者会拿一段库外直答）。分数区间取**被删**那批（区间上界必然低于门槛，可一眼核对是不是
    门槛在起作用）；``%.2f`` 只够看量级，精确值仍以 ``item.rerank_score`` 为准。

    ``stats`` 出参记录本次分支与过滤数字，供基线检索及 Agent 工具事件各自落库；
    ``retrieve()`` 的三元组不变。全删后的分路语义不变，API 层按最终材料状态添加来源说明。
    """
    stats = stats if stats is not None else RerankStats()
    from app.retrieval.types import material_identities
    # 重用出参时也先清空，不能把上一次成功过滤的数据带进回落出口。
    stats.__dict__.update(RerankStats(
        candidates=len(materials), query_rewrite=stats.query_rewrite, links=stats.links,
        recall=stats.recall, rerank_input=material_identities(materials),
    ).__dict__)
    if not materials:
        stats.status = "no_candidates"
        return materials
    if not enabled():
        stats.status = "disabled"
        return materials
    config = resolve_rerank_config()
    if config is None:
        stats.status = "unbound"
        log.warning(
            "已开启 RAG_ENABLE_RERANK，但未在设置里绑定 rerank 服务：本次回落 RRF 顺序。"
        )
        return materials
    stats.model = config.model
    try:
        scores = await _call_rerank(config, question, [item.content for item in materials])
    except Exception as exc:  # noqa: BLE001 —— 重排失败不炸穿，回落融合顺序
        stats.status = "failed"
        log.warning("重排调用失败（%s：%s）：本次回落 RRF 顺序。", exc.__class__.__name__, exc)
        return materials
    if not scores or len(scores) != len(materials) or any(
        isinstance(score, bool) or not isinstance(score, (int, float))
        or not math.isfinite(score)
        for score in scores
    ):
        stats.status = "shape_mismatch"
        log.warning("重排返回的条数与候选不一致：本次回落 RRF 顺序。")
        return materials

    for item, score in zip(materials, scores):
        item.rerank_score = float(score)
    stats.rerank_output = material_identities(sorted(
        materials, key=lambda item: (-(item.rerank_score or 0.0), int(item.chunk_id))
    ))
    # 「低于门槛的片段不作为引用」：只在这一条成功路径上过滤。threshold <= 0 ⇒ 不过滤。
    threshold = float(getattr(settings, "RAG_MIN_RERANK_SCORE", DEFAULT_MIN_RERANK_SCORE))
    stats.status = "applied"
    stats.threshold = threshold
    stats.threshold_applied = threshold > 0
    stats.kept = len(materials)
    stats.removed = 0
    if threshold > 0:
        before = len(materials)
        kept: list[Material] = []
        removed: list[float] = []  # 被删条目的重排分（用于日志里的分数区间）
        for item in materials:
            score = float(item.rerank_score or 0.0)
            if score >= threshold:
                kept.append(item)
            else:
                removed.append(score)
        stats.kept = len(kept)
        stats.removed = len(removed)
        if removed:
            stats.removed_score_min = min(removed)
            stats.removed_score_max = max(removed)
            # 「门槛把材料删空」以前是**静默**的：下游只会看到 hits=0，分不出真零命中（P2-4）。
            log.warning(
                "重排门槛 %.2f 过滤掉 %d/%d 条材料（被删分数区间 %.2f–%.2f）：本次仅保留 %d 条。",
                threshold,
                len(removed),
                before,
                min(removed),
                max(removed),
                len(kept),
            )
        materials = kept
    ranked = sorted(
        materials, key=lambda item: (-(item.rerank_score or 0.0), int(item.chunk_id))
    )
    return ranked


__all__ = [
    "DEFAULT_MIN_RERANK_SCORE",
    "RERANK_ROLE",
    "apply_rerank",
    "enabled",
    "resolve_rerank_config",
]
