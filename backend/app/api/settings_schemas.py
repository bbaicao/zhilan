"""`/api/settings` 的入参模型与出参组装（阶段 4 · 条目 `4.5.3`；命名对齐 `chat_schemas.py`）。

**一处分界**：本文件只管「形状」——pydantic 入参模型 + 十二个顶层键的**组装**；
「校验 → 落盘 → 改内存」的顺序与 400/500 口径在 `settings_routes.py`。

字段形状的**唯一真相源**是冻结前端 `frontend/src/api.ts`（I2，只读）：
`SettingsOut`(`:1137`)、`SettingsIn`(`:1167`)、`ServiceIn`(`:1153`)、`ModelProbe`(`:1202`)、
`ReasoningState`(`:989`)、`RerankState`(`:1034`)、`WebState`(`:1110`)、`AgentSettings`(`:1128`)、
`DataState`(`:964`)、`translate`(`:1150`)。行号只是线索，按**字段名**对齐。

**四段当前状态**（`reasoning` 段自「推理等级回流」条目起是**真实现**，不再是空态）：

1. **真实现（读到就真用）**：`reasoning`（厂商思考参数登记表 + `CHAT_REASONING` 读写通道，
   见 `app/core/reasoning/`）、`rerank`（口径 A 落 `MODEL_ROLES.rerank`）、
   `agent`（`AGENT_*` 三条键的消费方已在 4.1/4.2/4.3 落地）；
2. **联网设置**：`web` 支持持久化配置、传统搜索及 MiMo 原生联网；SSRF 放行字段只读。
3. **诚实空态 + 明确 400**：`data` 没有目录搬迁能力
   ——`GET` 侧键**必须齐**（冻结前端逐字读，缺键 = 弹窗崩），值必须**诚实**；
   `PUT` 侧给**明确中文 400**，绝不「接受后丢弃」（那正是 `022`/`016` 这一族
   静默缺陷的根因）；
4. **旧扁平段**：`chat` / `embed` / `vision`（env 模式下冻结前端真的会发）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.api import rerank_spec
from app.core.config import PROVIDER_PRESETS, ROLE_KEYS, ROLE_LABELS, settings

# --- 入参 ----------------------------------------------------------------


class ServiceIn(BaseModel):
    """一条服务（整表替换用）。字段形状照 `api.ts:1153-1165`。"""

    id: str | None = None
    name: str = ""
    provider: str
    base_url: str = ""
    #: 留空表示保留原 Key（前端不回显 Key，「没改」和「留空」是同一种表达）
    api_key: str | None = None
    models: list[str] = []
    #: 型号用途标记 {型号: [角色...]}；None（缺省）= 沿用库内现值
    model_roles: dict[str, list[str]] | None = None
    #: 这条供应商要出现在哪些角色的弹窗里（对话弹窗按 chat/vision 过滤）
    for_roles: list[str] = []


class RoleIn(BaseModel):
    """角色绑定（`api.ts:928-931`）。两个字段都为空 = 解绑。"""

    service_id: str = ""
    model: str = ""


class GroupIn(BaseModel):
    """旧扁平格式（`mode="env"` 时用；`api.ts:1173`）。

    前端 `App.tsx:498-501` 在「没有服务注册表」时确实走这条路（顶栏切模型），
    所以它在 v2 **必须是活的**：接受后丢弃会让 env 模式下的模型切换静默无效。
    """

    provider: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None


class VisionIn(BaseModel):
    model: str | None = None


class DataIn(BaseModel):
    """数据服务（条目 3）：向量库的存放位置。**v2 只收不改**——见 `DATA_UNAVAILABLE_HINT`。"""

    chroma_dir: str = ""
    migrate: bool = False


class ReasoningIn(BaseModel):
    """思考程度（条目 5）。**v2 只收不改**。"""

    param: str = ""
    value: Any = None


class RerankIn(BaseModel):
    """检索增强（条目 28）：`api.ts:1179`。字段都可省略；`api_key` 留空 = 保留原来那把。

    ⚠️ **不收 `threshold`**（`api.ts:1179` 也没有这个字段）：门槛键是 `RAG_MIN_RERANK_SCORE`，
    它的写入通道是**手改 `.env`**，本端点不偷偷接受它。
    """

    enabled: bool | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class WebIn(BaseModel):
    """联网检索设置；密钥留空保留，SSRF 逃生门只读。"""

    enabled: bool | None = None
    channel: str | None = None
    provider: str | None = None
    api_key: str | None = None
    max_results: int | None = None
    timeout: float | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_max_searches: int | None = None
    llm_search_tool: str | None = None
    ssrf_allow_any_host: bool | None = None


class AgentIn(BaseModel):
    """Agent 编排（条目 18）：**v2 真实现**（4.1/4.2/4.3 已落地消费方）。

    ``max_rounds`` 故意收 ``Any`` 而不是 ``int``：pydantic 对非法字符串会直接回 422，
    而这里的约定是「非 int / 越界」都走统一的 **400 中文文案**，所以收进来自己判。
    """

    mode: str | None = None
    max_rounds: Any = None
    audit: bool | None = None


class TranslateIn(BaseModel):
    """翻译（条目 23）：DeepL Key。留空（或空白）表示保留现值（`api.ts:1199`）。"""

    api_key: str | None = None


class SettingsIn(BaseModel):
    """`PUT /api/settings` 的请求体（`api.ts:1167-1200`）。

    ``extra="ignore"``：前端可能先于后端发布而多带字段（I1 只加不减），多出来的键忽略比 422 稳。
    """

    model_config = ConfigDict(extra="ignore")

    services: list[ServiceIn] | None = None
    roles: dict[str, RoleIn] | None = None
    chat: GroupIn | None = None
    embed: GroupIn | None = None
    vision: VisionIn | None = None
    data: DataIn | None = None
    reasoning: ReasoningIn | None = None
    rerank: RerankIn | None = None
    web: WebIn | None = None
    agent: AgentIn | None = None
    translate: TranslateIn | None = None
    # 工作区根（专项工具升级-3 · 阶段 3 · 回归检查）：**顶层路径类键**，与 upload/sqlite/skill 同族。
    # ⚠️ 漏了这个字段 ⇒ `extra="ignore"`（:146）直接吞掉 ⇒ **静默不生效**（不报 400 也不报 422）。
    workspace_root: str | None = None


class ModelsIn(BaseModel):
    """`POST /api/models`（`ModelProbe`，`api.ts:1202-1208`）。"""

    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    service_id: str | None = None


class WebModelsIn(BaseModel):
    """`POST /api/web/llm/models`（`api.ts:1239`）：都可省略，省略就用已保存的配置。"""

    base_url: str | None = None
    api_key: str | None = None


# --- 段级口径常量（界面上要显示的原文，一处定义）--------------------------------

#: `PUT` 对 `web` 段的明确 400。⚠️ **措辞刻意只说「设置页写入没实装」**，
#: 不写「联网检索尚未实现」——4.1 已把 api 渠道实装并接线，那句话是**假的**
#:（`WEB_ENABLE=true` 时运行时真的会联网检索）。这条差异已登记在证据文件。
WEB_PUT_REJECTED = "联网检索的设置页写入尚未在本版本实装，请直接改本机 .env 的 WEB_* 键。"

#: `PUT.data` 的 400：v2 不做目录搬迁，向量库位置只有 `.env` 一条通道。
DATA_PUT_REJECTED = (
    "向量库位置请直接改本机 .env 的 QDRANT_PATH / QDRANT_URL，设置页暂不支持搬迁。"
)

#: `GET.agent.modes`：三个取值来自 02 §7.2 的 `agent_mode` 与 `config.py::AGENT_MODE` 的注释，
#: **不是自造选项**（前端 `SettingsView.tsx:248` 直接渲染这个清单）。
AGENT_MODES: list[dict[str, str]] = [
    {"value": "off", "label": "直答（默认 RAG 流程）"},
    {"value": "react", "label": "ReAct 工具循环"},
    {"value": "plan_execute", "label": "计划执行 Plan-and-Execute"},
]


# --- 出参组装 ------------------------------------------------------------


def public_service(service: dict, roles: dict[str, dict[str, str]]) -> dict:
    """服务对象的 **9 键**（02 §7.6「has_key 口径」）：`id`/`name`/`provider`/`base_url`/
    `has_key`/`models`/`model_roles`/`for_roles`/`roles`。

    **绝不回传 `api_key` 本体或任何片段**——只回 `has_key: bool`。
    `name` 空则回落 `provider`（老基线同）；`roles` 是从 `settings.roles` 派生的三角色
    （前端零使用，实测 `frontend/src/**` 只读 `for_roles`，但契约里有这个键）。
    """
    return {
        "id": service["id"],
        "name": service["name"] or service["provider"],
        "provider": service["provider"],
        "base_url": service["base_url"],
        "has_key": bool(service["api_key"]),
        "models": list(service["models"]),
        "model_roles": dict(service.get("model_roles") or {}),
        "for_roles": list(service["for_roles"]),
        "roles": [role for role in ROLE_KEYS if roles.get(role, {}).get("service_id") == service["id"]],
    }


def _config_of(role: str):
    return {
        "chat": lambda: settings.chat_config,
        "embed": lambda: settings.embed_config,
        "vision": lambda: settings.vision_config,
    }[role]()


def missing_role_error(role: str) -> str:
    """「注册表在、这个角色没绑定」时的可读中文（`回归修复` 的 `active` 标注）。

    ⚠️ **这是提示，不是补绑**：扁平键**不再兜底**（`Settings._resolve_role` 只查角色绑定），
    这里把「为什么这个角色是未配置」**说出来**——原先的洞是「`vision_config is None` 而零提示」。
    **禁止**据此恢复「自动改写 `MODEL_ROLES`」类实现（`repair_missing_role_bindings` 是已删的地雷，
    栅栏见 `tests/test_model_roles.py::test_repair_missing_role_bindings_stays_deleted`）。
    """
    label = ROLE_LABELS.get(role, role)
    return (
        f"{label}模型还没有绑定服务：服务注册表已接管配置，.env 的扁平键不再兜底。"
        f"请到设置页的「{label}」里指定一条服务与型号。"
    )


def active_of(role: str, roles: dict[str, dict[str, str]], has_registry: bool) -> dict:
    """`ActiveModel` **6 键**：当前真正生效的配置（服务注册表或 `.env` 扁平键都算）。

    **`ValueError` 必须就地吞掉并写进 `error`**（I5）：`Settings._resolve_role` 在「绑定指向
    已删服务」时会抛，而 `GET /api/settings` 是设置页启动时必调的一次——读侧**一切不抛**。
    """
    try:
        config = _config_of(role)
    except ValueError as exc:
        return _inactive(error=str(exc))
    if config is None or not config.model:
        # 028：有注册表却缺这个角色的绑定时，**明确标注原因**（不再是一句空 error）
        if has_registry and role not in roles:
            return _inactive(error=missing_role_error(role))
        return _inactive()
    bound = roles.get(role, {})
    service = settings.service_by_id(bound.get("service_id", "")) if has_registry else None
    return {
        "configured": True,
        "service_id": service["id"] if service else "",
        "service_name": (service["name"] or service["provider"]) if service else "",
        "provider": config.provider,
        "model": config.model,
        "error": "",
    }


def _inactive(error: str = "") -> dict:
    return {
        "configured": False,
        "service_id": "",
        "service_name": "",
        "provider": "",
        "model": "",
        "error": error,
    }


def reasoning_payload() -> dict:
    """`ReasoningState` **15 键**（`api.ts:989-1010`）——**登记表驱动，读侧不抛**。

    组装逻辑在 `app/core/reasoning/payload.py`（纯函数：吃 `provider` / `model` / `raw`
    吐 15 键）；本函数只负责**取值**与异常口径：

    - 取值来源 = `settings.chat_config`（与 `answer.build_chat_model` **同一来源**，
      含 `MODEL_ROLES.chat` 绑定与 `.env` 扁平键两条路）+ `settings.CHAT_REASONING`；
    - `ValueError`（绑定指向已删服务 / 未知厂商）按「**没配好**」吞掉 ⇒ 回落空态，
      与 `active_of` 同口径。`GET /api/settings` 是设置页启动时必调的一次，**读侧一切不抛**（I5）。
    """
    from app.core.reasoning import payload as reasoning_state

    try:
        config = settings.chat_config
    except ValueError:
        config = None
    provider = str(getattr(config, "provider", "") or "")
    model = str(getattr(config, "model", "") or "")
    raw = str(getattr(settings, "CHAT_REASONING", "") or "")
    return reasoning_state(provider, model, raw)


def web_payload() -> dict:
    """`WebState` **10 个必填键**（`api.ts:1110-1125`）+ 可选的 `channel`/`channel_options`/`llm`
    与 `ssrf_allow_any_host`（回归修复 · 2026-09-27 拍板②，见段末）。

    **读侧全真**：服务商与渠道清单来自 `app/agent/web/spec.py`（条目 4.1 已落地的**唯一真相源**，
    设置页与工具层共用）；`enabled` / `has_key` / `max_results` / `timeout` 如实读 `.env`；
    `effective` / `blocked_by` 的语义严格照契约（「开着且**真的能跑起来**」）—— 只有真的缺 Key /
    服务商未登记才算 `blocked_by`。**本版本没有「假的 blocked_by」**：`WEB_ENABLE=true` 且配置齐全时
    `effective=true`（api 渠道确实能跑，`app/agent/web/client.py` 已实装）。

    **诚实缺口（已登记）**：`providers[].cost` / `note` / `source` 恒空串 —— v2 的服务商登记表
    有意只留八项（`spec.py` 的模块注释写明「没有读者的字段搬过来只会腐烂」）。
    前端 `CostNote` 在 `cost/note/source` 全空时返回 `null`（`WebDialog.tsx:78`），不会渲染空块。

    `llm`（可选）回读 MiMo 搜索配置；缺省地址使用官方接口，同厂商官方对话绑定
    可提供缺省型号与密钥。有效配置只表示可以发起请求，不保证远端权限和额度。

    **回归修复（2026-09-27 拍板②）**：`ssrf_allow_any_host`（可选布尔键）= SSRF 逃生门
    `WEB_SSRF_ALLOW_ANY_HOST`（`config.py`，默认 False）的**只读暴露**——机器可读，
    前端**零消费**（「零前端改动」是有意拍板口径，本键只服务机器 / 排障读数）。
    **只进 GET、不进 PUT**：写 `ssrf_allow_any_host` 仍返回 400；
    其余联网配置支持保存。改 `.env` + 重启仍是这条逃生开关的唯一通道。值**现读**（`getattr` 兜底 False），
    与守卫**同源**（`net_guard.allow_any_host()` 与 `agent/web/client.py` 的 `_guard_hook`
    看到的是同一个单例上的同一个值）。
    """
    from app.agent.web import spec as web_spec

    provider = web_spec.normalize_provider(settings.WEB_SEARCH_PROVIDER or web_spec.DEFAULT_PROVIDER)
    spec = web_spec.provider_spec(provider)
    enabled = bool(settings.WEB_ENABLE)
    has_key = bool((settings.WEB_SEARCH_API_KEY or "").strip())
    channel = (settings.WEB_CHANNEL or web_spec.CHANNEL_API).strip().lower()
    if channel not in (web_spec.CHANNEL_API, web_spec.CHANNEL_LLM):
        channel = web_spec.CHANNEL_API

    blocked_by = ""
    if enabled:
        if channel == web_spec.CHANNEL_LLM:
            from app.agent.web.llm import configuration_error
            blocked_by = configuration_error()
        elif spec is None:
            allowed = "、".join(row.id for row in web_spec.WEB_PROVIDERS)
            blocked_by = f"未登记的搜索服务商：{provider}。可选：{allowed}"
        elif spec.needs_key and not has_key:
            blocked_by = f"{spec.label} 需要 API Key：{spec.key_hint or '请到设置页填写'}"

    from app.agent.web.llm import resolved_config
    llm_provider, llm_base, llm_key, llm_model = resolved_config()
    return {
        "enabled": enabled,
        "channel": channel,
        "channel_options": [dict(row) for row in web_spec.CHANNELS],
        "provider": provider,
        "provider_label": spec.label if spec is not None else provider,
        "has_key": has_key,
        "providers": [
            {
                "id": row.id,
                "label": row.label,
                "summary": row.summary,
                "needs_key": row.needs_key,
                "key_hint": row.key_hint,
                # v2 的服务商登记表有意不带这三项（见 docstring），如实回空串
                "cost": "",
                "note": "",
                "source": "",
            }
            for row in web_spec.WEB_PROVIDERS
        ],
        "llm": {
            "provider": llm_provider,
            "provider_label": "MiMo" if llm_provider == "mimo" else llm_provider,
            "model": llm_model,
            "models": [],
            "base_url": llm_base,
            "has_key": bool(llm_key),
            "max_searches": int(settings.WEB_LLM_MAX_SEARCHES),
            "search_tool": (settings.WEB_LLM_SEARCH_TOOL or "").strip(),
            "search_tool_note": (
                "MiMo 通过服务商联网插件搜索并返回来源；需使用支持 web_search 的型号。"
            ),
            "providers": [{"id": "mimo", "label": "MiMo"}],
        },
        "max_results": int(settings.WEB_MAX_RESULTS),
        "timeout": float(settings.WEB_FETCH_TIMEOUT),
        "effective": enabled and not blocked_by,
        "blocked_by": blocked_by,
        "note": "",
        # 回归修复（2026-09-27 拍板②）：逃生门状态只读暴露——机器可读，前端未消费；
        # PUT 仍不可写（改 .env + 重启是唯一开关通道）。
        # 现读（getattr 兜底 False），与守卫同源（net_guard.allow_any_host / web client _guard_hook）。
        "ssrf_allow_any_host": bool(getattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", False)),
    }


def agent_payload() -> dict:
    """`AgentSettings` **4 键**（`api.ts:1128-1135`）——**真实现**（不是空态）。

    `AGENT_MODE` / `AGENT_MAX_ROUNDS` / `AGENT_ENABLE_AUDIT` 三条键与它们的消费方
    （`app/agent/runner.py`、`app/agent/audit.py`）已在 4.1/4.2/4.3 落地，
    所以这里如实回读，`PUT` 也如实落盘（详见 `settings_routes._resolve_agent`）。
    """
    return {
        "mode": settings.AGENT_MODE,
        "max_rounds": int(settings.AGENT_MAX_ROUNDS),
        "audit": bool(settings.AGENT_ENABLE_AUDIT),
        "modes": [dict(item) for item in AGENT_MODES],
    }


def translate_payload() -> dict:
    """`translate` 段（`api.ts:1150`，字段可选）：**只回有没有 Key，不回 Key 本体**。"""
    return {"has_key": bool((settings.DEEPL_API_KEY or "").strip())}


def data_payload(notice: str = "") -> dict:
    """`DataState` **7 键**（`api.ts:964-976`）——**诚实回显**，不做任何向量库 I/O。

    ⚠️ 老基线 `current_payload` 用 `asyncio.to_thread(data_payload)` 打开向量库（冷启动约 0.5s）；
    v2 这一版**不做**（开工指令包 §7 R9）：`GET /api/settings` 会被设置页启动时调一次，
    为一个「诚实空态」的段去连 Qdrant 不值当，而且失败面（断库）会传染到整个设置页。

    口径：

    - `chroma_dir` / `chroma_path` = `QDRANT_PATH`（空则回落 `QDRANT_URL`），**如实回显配置值**，
      v2 不做绝对化（老基线那套路径规范化属于 `app/core/data_dir.py`，v2 没有这个模块）；
    - `exists` 只对**本地路径**做一次 `is_dir()`（单个 stat，不是 I/O 扇出）；指向 URL 时恒 `false`；
    - `files` / `size_bytes` 恒 `0`、`vectors` 恒 `-1` —— **未统计**，不是「真的没有」；
      `-1` 的契约语义是「读不到 / 未实现」（`api.ts:972`），前端据此显示「还没有向量集合」。
    """
    local_path = (settings.QDRANT_PATH or "").strip()
    raw = local_path or (settings.QDRANT_URL or "").strip()
    exists = False
    if local_path:
        try:
            exists = Path(local_path).expanduser().is_dir()
        except OSError:  # pragma: no cover —— 非法路径（Windows 保留名等）不该炸设置页
            exists = False
    return {
        "chroma_dir": raw,
        "chroma_path": raw,
        "exists": exists,
        "files": 0,
        "size_bytes": 0,
        "vectors": -1,
        "notice": notice,
    }


def rerank_target() -> tuple[dict | None, str]:
    """重排序**会用到的那条服务**与它的来源说明（口径 A 的服务选择，见开工指令包 §4.5）。

    ① 绑定的服务仍存在 ⇒ 用它；② 否则用 **embed 绑定**那条（百炼的 rerank 与 embedding
    同一套鉴权，遗留 `RERANK_*` 也从来没有独立的 key/url 管理入口）；
    ③ 都没有 ⇒ `None`（调用方决定是 400 还是只回显）。

    返回的第二个值是**来源标签**，给 `key_source` 用：
    ``"binding"`` / ``"embed"`` / ``""``。
    """
    binding = settings.rerank_binding
    if binding:
        service = settings.service_by_id(binding["service_id"])
        if service is not None:
            return service, "binding"
    embed = settings.roles.get("embed")
    if embed:
        service = settings.service_by_id(embed["service_id"])
        if service is not None:
            return service, "embed"
    return None, ""


def rerank_payload() -> dict:
    """`RerankState` **16 键**（`api.ts:1034-1058`）—— 本条目唯一的「新能力」段。

    逐键口径见开工指令包 §4.4；三处**推定**（不在决策包里、由本条目补全）已逐条登记在证据文件：

    1. `key_source` 的两句文案（`重排序绑定的服务「X」的 Key` / `沿用「向量模型」那条供应商的 Key`）；
    2. `base_url` 的语义 = 「绑定那条服务上写的地址」（v2 **没有**独立的 `RERANK_BASE_URL` 键）；
    3. `has_key` 的「本机地址允许空 Key」判据沿用 `app/api/__init__.py::_require_config` 的口径。

    **读侧一切不抛**（I5）：整段吞异常并回落空值，`GET /api/settings` 恒 200。
    """
    # 缺省服务商恒为 `dashscope`：v2 没有 `RERANK_PROVIDER` 键（口径 A：不落独立键），
    # 服务商一律**由目标服务推出来**；推不出来时就是「还没配」的状态，给百炼这条默认。
    provider = rerank_spec.DASHSCOPE_ID
    binding = settings.rerank_binding
    target, source = rerank_target()

    # 绑定那条服务写的是什么厂商 ⇒ 就是当前的服务商（口径 A：`provider` 是**规范 id**，
    # 由服务推出来，不落成独立键）
    if target is not None:
        provider = rerank_spec.normalize_provider(str(target.get("provider") or ""), provider)
    spec = rerank_spec.provider_spec(provider)
    enabled = bool(settings.RAG_ENABLE_RERANK)
    model = (binding or {}).get("model", "") if binding else ""
    base_url = str((target or {}).get("base_url") or "")

    effective_base_url = ""
    if target is not None and model:
        try:
            effective_base_url = settings._service_config(target, model).base_url
        except ValueError:
            effective_base_url = ""
    if not effective_base_url:
        effective_base_url = base_url

    has_key, key_source = _rerank_key_state(target, source, spec)
    dep_ok, dep_detail = rerank_spec.dependency_status(provider)

    blocked_by = ""
    if enabled:
        if not binding or not model:
            blocked_by = "还没有绑定重排序型号，开启后不会生效。"
        elif settings.service_by_id(binding["service_id"]) is None:
            blocked_by = (
                f"重排序绑定的服务已不存在（{binding['service_id']}），请在设置页重新指定。"
            )
        elif not dep_ok:
            blocked_by = dep_detail
        elif spec is not None and spec.needs_key and not has_key:
            blocked_by = "没有可用的 API Key：留空即沿用「向量模型」那条供应商的 Key。"

    return {
        "enabled": enabled,
        "provider": provider,
        "provider_label": spec.label if spec is not None else provider,
        "model": model,
        "base_url": base_url,
        "effective_base_url": effective_base_url,
        "has_key": has_key,
        "key_source": key_source,
        "models": rerank_spec.model_options(provider),
        "providers": rerank_spec.provider_options(),
        "dependency_ok": dep_ok,
        "dependency_detail": dep_detail,
        "threshold": float(settings.RAG_MIN_RERANK_SCORE),
        "effective": enabled and not blocked_by,
        "blocked_by": blocked_by,
        "note": rerank_spec.ENABLE_CONDITION,
    }


def _is_local_base_url(base_url: str) -> bool:
    """本机地址允许空 Key（口径同 `app/api/__init__.py::_require_config`）。"""
    base = base_url or ""
    return "127.0.0.1" in base or "localhost" in base


def _rerank_key_state(target: dict | None, source: str, spec) -> tuple[bool, str]:
    """重排序能用的 Key 从哪来：目标服务上的 `api_key`；本机地址允许空 Key。

    ⚠️ **口径 A 下没有"重排序专用 Key"**（没有 `RERANK_API_KEY` 键）：Key 只可能来自
    目标服务（绑定那条，或沿用的「向量模型」那条）。**推定处 1**，见本模块 docstring。
    """
    if target is None:
        return False, ""
    if (target.get("api_key") or "").strip():
        if source == "embed":
            return True, "沿用「向量模型」那条供应商的 Key"
        label = target.get("name") or target.get("provider") or target["id"]
        return True, f"重排序绑定的服务「{label}」的 Key"
    if spec is not None and not spec.needs_key:
        return False, ""
    if _is_local_base_url(str(target.get("base_url") or "")):
        return True, "本机地址（127.0.0.1 / localhost 无需 Key）"
    return False, ""


def current_payload(notice: str = "") -> dict:
    """`GET /api/settings` 的**全部 12 个顶层键**（02 §7.6）。

    顺序与契约一致：`services` / `roles` / `active` / `mode` / `providers` / `reasoning` /
    `rerank` / `web` / `agent` / `translate` / `data` / `workspace_root`。

    **同步函数**：不做任何磁盘 I/O（`data` 段是纯回显）、不抛异常 —— `GET` 侧恒 200 是 I5 的
    明文要求（设置页启动时会调它一次，抛了整页显示「后端未连接」）。
    """
    roles = settings.roles
    has_registry = settings.has_service_registry
    return {
        "services": [public_service(item, roles) for item in settings.services],
        "roles": roles,
        "active": {role: active_of(role, roles, has_registry) for role in ROLE_KEYS},
        "mode": "services" if has_registry else "env",
        "providers": PROVIDER_PRESETS,
        "reasoning": reasoning_payload(),
        "rerank": rerank_payload(),
        "web": web_payload(),
        "agent": agent_payload(),
        "translate": translate_payload(),
        "data": data_payload(notice),
        # 第 12 键（专项工具升级-3 · 阶段 3 · 回归检查）：工作区根**原始字符串**（空串 = 未配置）。
        # ⚠️ 与 `Settings.workspace_root` **同一形状**（只回配置原始值、不做归一）——
        # 归一唯一在 `agent/tools/_paths.py::resolve_workspace_root()`，此处**不得**再写一份。
        "workspace_root": settings.workspace_root,
    }


__all__ = [
    "AGENT_MODES",
    "DATA_PUT_REJECTED",
    "WEB_PUT_REJECTED",
    "AgentIn",
    "DataIn",
    "GroupIn",
    "ModelsIn",
    "ReasoningIn",
    "RerankIn",
    "RoleIn",
    "ServiceIn",
    "SettingsIn",
    "TranslateIn",
    "VisionIn",
    "WebIn",
    "WebModelsIn",
    "active_of",
    "agent_payload",
    "current_payload",
    "data_payload",
    "missing_role_error",
    "public_service",
    "reasoning_payload",
    "rerank_payload",
    "rerank_target",
    "translate_payload",
    "web_payload",
]
