"""设置读写与模型列表（阶段 4 · 条目 `4.5.3` settings 域；独立挂载）。

原则（逐条照老基线 `<legacy-repo>\\backend\\app\\api\\settings_routes.py` 的模块 docstring）：

- 真实 Key 只写本机 `.env`，接口**永远不回传 Key 本体**，只回 `has_key`；
- 一个角色（对话 / 向量 / 视觉）最多绑定「一条服务 + 一个模型」；
- `services` 传了就是整表替换，不传就是不动；`api_key` 留空表示保留原 Key；
- 保存**先全部校验（400、零落盘）→ 落盘 `.env` → 才改内存**；写盘失败 = 500 且内存不动；
- 没有服务注册表时仍支持旧的扁平格式（`chat` / `embed` / `vision` 段），方便直接手改 `.env` 的老用户。

**本条目（`4.5.3`）在 v2 落地的三块**：

1. `GET/PUT /api/settings` 的完整契约面（12 个顶层键；`rerank` 按**口径 A**：落盘通道 =
   `MODEL_ROLES.rerank` + `RAG_ENABLE_RERANK`，**不新增任何永久 `RERANK_*` 扁平键**）；
2. 同文件的 `/models` 三条（`POST /api/models` / `POST /api/web/llm/models` / `GET /api/models`）——
   专包 §1.2 把这三条划给「其它域条目」是为了给另一个会话让路，按域提交则必须一起做；
3. `回归修复` —— 「注册表在、角色缺绑定」做成**显式可见**（`active` 标注 + 启动一行日志），
   **不**恢复「自动改写 `MODEL_ROLES`」类实现（那是已删的地雷，见 `config.py` 的注释与
   `tests/test_model_roles.py::test_repair_missing_role_bindings_stays_deleted`）。

⚠️ **不要**把本 router 再 `include` 进 `app/api/__init__.py`：02 §1.3 把 settings 列为
「独立挂载路由」，`main.py` 是正确挂点；同路径注册两次会让先注册的那个静默胜出。

字段形状（入参模型 / 出参组装）在 `app/api/settings_schemas.py`；
重排序登记表在 `app/api/rerank_spec.py`。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException

from app.api import rerank_spec
from app.api.offline_model_routes import router as offline_model_router
from app.api.settings_schemas import (
    DATA_PUT_REJECTED,
    WEB_PUT_REJECTED,
    AGENT_MODES,
    ModelsIn,
    SettingsIn,
    ServiceIn,
    RoleIn,
    WebModelsIn,
    current_payload,
    rerank_target,
)
from app.core.net_guard import OutboundBlocked, assert_outbound_url_safe

from app.core.config import (
    ALL_ROLE_KEYS,
    PROVIDER_PRESETS,
    RERANK_ROLE,
    ROLE_KEYS,
    ROLE_LABELS,
    build_registry_from_legacy,
    dump_registry,
    ensure_service_registry,
    new_service_id,
    parse_model_roles,
    parse_role_list,
    settings,
)
from app.core.env_store import persist_env
from app.core.errors import ProviderNotConfigured

logger = logging.getLogger(__name__)

router = APIRouter()

router.include_router(offline_model_router)


# --- 回归修复：缺角色绑定要显式可见（启动一行日志）------------------------------------------------
#
# 背景：`Settings._resolve_role` 在「有注册表」时**只查角色绑定**，不再回落 `.env` 的扁平键
# ⇒ 注册表里少一个角色 → `settings.vision_config is None` 而**零提示**（图片问答整条链路
# 静默报「未配置」）。老仓库曾用 `repair_missing_role_bindings` 自动补绑，而那个函数是
# 「整串重写 MODEL_ROLES」的地雷（`回归修复` 已整函数删除）⇒ 本条只做**可见化**。
#
# 三处出口：① `GET.active` 的 `error` 标注（`settings_schemas.active_of`）；
# ② `GET.roles` 如实缺（`settings.roles` 天然如此，不做补齐）；③ 下面这行**启动日志**。
def missing_role_bindings() -> list[str]:
    """有服务注册表、却**没有任何绑定**的前端可见角色（chat / embed / vision）。

    没有注册表时返回空 —— 那时走的是 `.env` 扁平键，缺失与否由 `active.configured` 表达，
    不是这条缺陷说的场景。
    """
    if not settings.has_service_registry:
        return []
    return [role for role in ROLE_KEYS if role not in settings.roles]


def log_missing_role_bindings() -> list[str]:
    """把「注册表在、角色缺」打**一行**可读中文警告（启动时调一次）。返回缺的角色名。

    ⚠️ 只警告、**绝不自动补绑**：补绑必然要改写 `MODEL_ROLES`，而那正是被删掉的那个地雷的
    行为（「配置改了但没告诉用户」的反面 —— 「配置改了而且用户不知道」）。
    """
    missing = missing_role_bindings()
    if missing:
        labels = "、".join(ROLE_LABELS.get(role, role) for role in missing)
        logger.warning(
            "服务注册表已接管配置，但以下角色没有任何绑定：%s。"
            "对应的扁平键（MODEL_PROVIDER/LLM_*/EMBED_*/VISION_MODEL）不再兜底，"
            "这些角色会按「未配置」处理（GET /api/settings 的 active 里带原因）；"
            "请在设置页指定服务与型号。",
            labels,
        )
    return missing


#: 启动一行日志：`main.py` 在本进程启动时 import 本模块（独立挂载路由），
#: 所以这一行就是「启动日志」。任何异常都不许影响 import（I5：探活/配置提示不该拖垮启动）。
try:  # pragma: no cover - 纯日志，失败无行为影响
    log_missing_role_bindings()
except Exception as exc:  # noqa: BLE001
    logger.debug("缺角色绑定检查未完成：%s", exc)


# --- 小工具 --------------------------------------------------------------


def _parse_json_list(raw: str) -> list[dict]:
    """宽容解析单行 JSON 数组（`.env` 是人手改得动的文件：坏数据一律当空）。"""
    text = (raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _parse_json_object(raw: str) -> dict:
    """宽容解析单行 JSON 对象（同上）。"""
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return dict(data) if isinstance(data, dict) else {}


def _dump(items: Any) -> str:
    """写回 `.env` 单行 JSON（与 `config.dump_registry` 同一手法：不带多余空格）。"""
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def _with_unknown_roles(roles: dict[str, dict[str, str]]) -> dict:
    """在已知角色表之上**保留 `MODEL_ROLES` 里用户手写的未知角色键**。

    为什么必须做（`回归修复` 对抗性复核 P2-4 的同一课）：`Settings.roles` 只遍历
    `ALL_ROLE_KEYS`（chat/embed/vision/rerank），而 `dump_registry` 是**整串重写**——
    不合并就等于把用户手写的 `translate` 之类未知键静默删掉。
    """
    raw = _parse_json_object(settings.MODEL_ROLES)
    out: dict[str, Any] = dict(roles)
    for key, value in raw.items():
        if key not in out and isinstance(value, dict) and value:
            out[key] = value
    return out


def _stored_value(key: str, value: str) -> Any:
    """`.env` 里存的是字符串，写回内存时按字段类型还原。

    bool 字段必须还原：`setattr(settings, "RAG_ENABLE_RERANK", "false")` 存的是字符串
    `"false"`，而 `bool("false")` 为真 —— 关不掉开关、还找不到原因。
    int / float 同理：字符串没法当数字比大小，任何按数值读这个键的地方都会悄悄失灵。
    str 字段原样返回。
    """
    field = type(settings).model_fields.get(key)
    annotation = getattr(field, "annotation", None)
    if annotation is bool:
        return value.strip().lower() in ("1", "true", "yes", "on")
    if annotation is int:
        try:
            return int(str(value).strip())
        except ValueError:
            return value
    if annotation is float:
        try:
            return float(str(value).strip())
        except ValueError:
            return value
    return value


# --- 入参归一（老基线 `_normalize_services` / `_normalize_roles` 同语义）--------------------------


def _normalize_services(items: list[ServiceIn], current: list[dict]) -> list[dict]:
    """`services` 的**整表替换**归一化；任何不合法一律 400（此时一个字都还没落盘）。

    - `provider` 必须在 `PROVIDER_PRESETS`；`kind=="openai"` 且 `base_url` 空 ⇒ 400；
    - `id` 缺 ⇒ `new_service_id(provider, taken)`；表内重复 ⇒ 400；
    - `api_key` 留空 ⇒ **按 id 保留原 Key**（前端不回显 Key，「没改」与「留空」是同一种表达）；
    - `models` 去重去空；`model_roles` 走 `parse_model_roles`（入参缺省 = 沿用现值）；
    - `for_roles` 走 `parse_role_list`（空 = 沿用现值）。

    ⚠️ **`for_roles` 故意不做派生**（开工指令包 §7 R2）：`Settings.services` 属性会
    `derived or ["chat"]`，若归一化也派生，PUT→GET 的 `for_roles` 就可能与前端送来的不同
    ⇒ 第二次 PUT 与第一次不等、幂等破。派生只留在属性里。
    """
    known_keys = {item["id"]: item["api_key"] for item in current}
    known_roles = {item["id"]: list(item["for_roles"]) for item in current}
    known_model_roles = {item["id"]: dict(item.get("model_roles") or {}) for item in current}
    taken = [item["id"] for item in current]
    out: list[dict] = []
    for item in items:
        provider = (item.provider or "").strip().lower()
        preset = PROVIDER_PRESETS.get(provider)
        if not preset:
            raise HTTPException(status_code=400, detail=f"未知的模型厂商：{item.provider}")
        base_url = (item.base_url or preset.get("base_url", "")).strip().rstrip("/")
        if preset.get("kind", "openai") == "openai" and not base_url:
            raise HTTPException(status_code=400, detail=f"厂商 {provider} 需要填写接口地址。")

        service_id = (item.id or "").strip() or new_service_id(provider, taken)
        if any(row["id"] == service_id for row in out):
            raise HTTPException(status_code=400, detail=f"服务 id 重复：{service_id}")
        taken.append(service_id)

        incoming_key = (item.api_key or "").strip()
        models: list[str] = []
        for model in item.models:
            name = str(model).strip()
            if name and name not in models:
                models.append(name)

        for_roles = parse_role_list(item.for_roles) or known_roles.get(service_id, [])
        model_roles = (
            parse_model_roles(item.model_roles, models)
            if item.model_roles is not None
            else known_model_roles.get(service_id, {})
        )

        out.append(
            {
                "id": service_id,
                "name": (item.name or "").strip() or provider,
                "provider": provider,
                "base_url": base_url,
                "api_key": incoming_key or known_keys.get(service_id, ""),
                "models": models,
                "model_roles": model_roles,
                "for_roles": for_roles,
            }
        )
    return out


def _normalize_roles(items: dict[str, RoleIn], services: list[dict]) -> dict[str, dict[str, str]]:
    """`roles` 的归一化：**只对 `ROLE_KEYS` 做整表替换**，`rerank` 从现值**继承**（P5）。

    - 未知角色名 ⇒ 400「未知的模型角色：{role}」；
    - 入参里出现 `rerank` ⇒ **不 400**，按整表替换语义处理（比老仓更宽容，且与「前端从不发」
      不冲突：前端 `SettingsDialog.tsx:35,443-448` 永远只发 chat/vision，`api.ts:899` 的
      `ModelRole` 也只有三个）；
    - 传空对象 = 三个角色**全部解绑**（前端语义，`api.ts:1170`）；
    - 入参指向的 `service_id` 不在新 `services` 表内 ⇒ 400。

    ⚠️ **绝不能把 `rerank` 一起擦掉**（开工指令包 §7 R1，最致命的一条）：老仓归一化的 `out`
    只由入参构造 + `role not in ROLE_KEYS` 直接 400 ⇒ 照抄会静默抹掉 `MODEL_ROLES.rerank`，
    而此时前端**永远不发** rerank。回归栅栏：`tests/test_model_roles.py::
    test_dump_registry_round_trip_keeps_rerank` + 本条 A9。
    """
    valid_ids = {item["id"] for item in services}
    out: dict[str, dict[str, str]] = {}
    for role, item in items.items():
        if role not in ALL_ROLE_KEYS:
            raise HTTPException(status_code=400, detail=f"未知的模型角色：{role}")
        service_id = (item.service_id or "").strip()
        model = (item.model or "").strip()
        if not service_id or not model:
            continue  # 传空表示解绑
        if service_id not in valid_ids:
            raise HTTPException(
                status_code=400, detail=f"{ROLE_LABELS.get(role, role)}模型指向的服务不存在：{service_id}"
            )
        out[role] = {"service_id": service_id, "model": model}

    if RERANK_ROLE not in items:
        existing = settings.roles.get(RERANK_ROLE)
        if existing and existing["service_id"] in valid_ids:
            out[RERANK_ROLE] = dict(existing)
    return out


def _resolve_group(group, current: dict[str, str], keys: dict[str, str], updates: dict[str, str]) -> None:
    """把一段**旧扁平**修改合并进 `updates`（老基线 `_resolve_group` 逐条同语义）。

    `current` 为现有值，`keys` 为「字段 → 环境变量名」的映射。**只攒 updates、不落盘**：
    任何 400 都发生在 `persist_env` 之前（这正是 live 契约用例
    `test_settings_put_unknown_provider_400` 依赖的顺序）。
    """
    provider = current["provider"]
    if group.provider is not None:
        provider = (group.provider or "").strip().lower()
        if provider and provider not in PROVIDER_PRESETS:
            raise HTTPException(status_code=400, detail=f"未知的模型厂商：{group.provider}")
    preset = PROVIDER_PRESETS.get(provider, {})

    base_url = current["base_url"]
    if group.base_url is not None:
        base_url = group.base_url.strip().rstrip("/")
    elif group.provider is not None and provider:
        base_url = preset.get("base_url", base_url)

    model = current["model"]
    if group.model is not None:
        model = group.model.strip()

    if preset.get("kind", "openai") == "openai" and provider and not base_url:
        raise HTTPException(status_code=400, detail=f"厂商 {provider} 需要填写接口地址。")

    updates[keys["provider"]] = provider
    updates[keys["base_url"]] = base_url
    updates[keys["model"]] = model
    if group.api_key is not None and group.api_key.strip():
        updates[keys["api_key"]] = group.api_key.strip()


# --- 重排序（条目 28 / 口径 A）------------------------------------------------


def _current_rerank_model() -> str:
    binding = settings.rerank_binding
    return (binding or {}).get("model", "") if binding else ""


def _resolve_rerank(item) -> dict:
    """校验重排序设置，返回**落盘计划**；不合法一律 400 且一个字都不落盘。

    R-1..R-4 四条校验（文案中文、把「缺什么怎么补」写清楚）+ 目标服务的选择与保护。
    真正的写盘在 `_apply_rerank`（它要动 `MODEL_SERVICES` / `MODEL_ROLES` 两张表，
    而那两张表的最终内容还取决于同一请求里的 `services` / `roles` 段）。
    """
    # R-1：未知服务商
    if item.provider is not None and rerank_spec.provider_spec(item.provider) is None:
        allowed = "、".join(rerank_spec.provider_ids())
        raise HTTPException(
            status_code=400, detail=f"未知的重排序服务商：{item.provider}。可选：{allowed}"
        )

    target, source = rerank_target()
    current_provider = rerank_spec.normalize_provider(str((target or {}).get("provider") or ""))
    provider = rerank_spec.normalize_provider(
        item.provider if item.provider is not None else current_provider
    )
    spec = rerank_spec.provider_spec(provider)

    enabled = bool(settings.RAG_ENABLE_RERANK) if item.enabled is None else bool(item.enabled)
    model = (
        item.model if item.model is not None else _current_rerank_model()
    ).strip()
    base_url = (
        item.base_url if item.base_url is not None else str((target or {}).get("base_url") or "")
    ).strip().rstrip("/")
    incoming_key = (item.api_key or "").strip()

    # R-2：开着但没型号
    if enabled and not model:
        names = "、".join(spec.models) if spec is not None else ""
        hint = f"（{spec.label} 常用：{names}）" if names else ""
        raise HTTPException(status_code=400, detail=f"开启重排序需要先填重排序型号{hint}。")

    # R-3：开着但依赖不就绪（v2 的 bge 恒中）
    ok, detail = rerank_spec.dependency_status(provider)
    if enabled and not ok:
        raise HTTPException(status_code=400, detail=detail)

    # 目标服务：绑定那条 → 沿用「向量模型」那条 → 可一次性迁移的扁平键 → 都没有就 400
    need_migration = False
    if target is None:
        preview = build_registry_from_legacy(settings)
        if preview:
            preview_services, preview_roles = preview
            embed = preview_roles.get("embed")
            if embed:
                preview_target = next(
                    (row for row in preview_services if row["id"] == embed["service_id"]), None
                )
                if preview_target is not None:
                    target, source, need_migration = preview_target, "embed", True
    if target is None and (enabled or model or base_url or incoming_key):
        raise HTTPException(
            status_code=400, detail="请先在设置页配置一条向量模型服务，再开启重排序。"
        )

    # 地址保护：地址本身就是「向量模型」那条服务的 ⇒ 不许单独改（改了会静默换掉向量路的地址）
    embed = settings.roles.get("embed") or {}
    is_embed_service = bool(target) and embed.get("service_id") == target["id"]
    if base_url and target is not None and base_url != str(target.get("base_url") or ""):
        if is_embed_service:
            raise HTTPException(
                status_code=400,
                detail=(
                    "重排序的接口地址请与「向量模型」那条供应商保持一致"
                    "（本版本沿用向量供应商，不单独托管地址）。"
                ),
            )

    # R-4：开着、需要 Key 而没有可用 Key
    if enabled and spec is not None and spec.needs_key:
        has_key = bool(incoming_key) or bool(str(target.get("api_key") or "").strip())
        if not has_key and not _is_local_service(target):
            raise HTTPException(
                status_code=400, detail="开启重排序需要可用的 API Key。" + spec.requirement
            )

    return {
        "provider": provider,
        "enabled": enabled,
        "model": model,
        "base_url": base_url,
        "incoming_key": incoming_key,
        "target_id": (target or {}).get("id", ""),
        "source": source,
        "need_migration": need_migration,
        "is_embed_service": is_embed_service,
    }


def _is_local_service(service: dict | None) -> bool:
    """本机地址允许空 Key（口径同 `app/api/__init__.py::_require_config`）。"""
    base = str((service or {}).get("base_url") or "")
    return "127.0.0.1" in base or "localhost" in base


def _apply_rerank(plan: dict, updates: dict[str, str]) -> None:
    """把校验通过的落盘计划写进 `updates`（**口径 A**：只碰注册表 + 开关，零 `RERANK_*`）。

    三处刻意的「不整串重写」：

    - `MODEL_SERVICES` **从原始 JSON 起手**、只改目标服务的那两个字段 —— 用
      `settings.services`（属性会规范化）会顺手丢掉用户手写的未知字段、给 `api_key` 去空格、
      给 `base_url` 去尾斜杠、按 `models` 裁剪 `model_roles`（`回归修复` 复核 P2-1 的现场）；
    - `MODEL_ROLES` 同理：从原始 JSON 起手，只动 `rerank` 一个键（P2-4）；
    - 没有被改动的表**根本不进 `updates`** ⇒ 不落盘 = 不可能顺手改写。
    """
    if plan["need_migration"]:
        # 幂等的一次性迁移（`config.ensure_service_registry`）：把 `.env` 的旧扁平配置收成注册表。
        # 只有走到这里（用户要开重排、而注册表还不存在）才会发生，且它自己负责落盘与失败提示。
        ensure_service_registry(settings)

    services_raw = _parse_json_list(updates.get("MODEL_SERVICES") or settings.MODEL_SERVICES)
    roles_raw = _parse_json_object(updates.get("MODEL_ROLES") or settings.MODEL_ROLES)
    services_changed = False

    target = next((row for row in services_raw if str(row.get("id") or "") == plan["target_id"]), None)
    if target is None:
        # 计划里的目标服务在**最终注册表**里找不到（同一请求里 `services` 整表替换把它删了）
        raise HTTPException(
            status_code=400, detail="重排序要用的服务不在本次提交的服务列表里，请先保存服务列表。"
        )

    if plan["incoming_key"]:
        target["api_key"] = plan["incoming_key"]
        services_changed = True
    if plan["base_url"] and plan["base_url"] != str(target.get("base_url") or ""):
        if plan["is_embed_service"]:
            # 校验阶段已拦；这里是防御性兜底（同一请求里 embed 绑定可能刚被改到这条服务上）
            raise HTTPException(
                status_code=400,
                detail=(
                    "重排序的接口地址请与「向量模型」那条供应商保持一致"
                    "（本版本沿用向量供应商，不单独托管地址）。"
                ),
            )
        target["base_url"] = plan["base_url"]
        services_changed = True

    if plan["model"]:
        roles_raw[RERANK_ROLE] = {"service_id": plan["target_id"], "model": plan["model"]}

    if services_changed:
        updates["MODEL_SERVICES"] = _dump(services_raw)
    if plan["model"]:
        updates["MODEL_ROLES"] = _dump(roles_raw)
    updates["RAG_ENABLE_RERANK"] = "true" if plan["enabled"] else "false"


# --- 接口 ----------------------------------------------------------------


@router.get("/settings")
async def get_settings() -> dict:
    """设置读（02 §7.6）：12 个顶层键，**任何配置状态下都 200**（I5）。

    `rerank` 段按口径 A 组装（16 键）；`has_key` 只回状态、绝不回传 Key 本体或任何片段。
    """
    return current_payload()


@router.put("/settings")
async def put_settings(payload: SettingsIn) -> dict:
    """设置写（02 §7.6）：**先全部校验（400、零落盘）→ 落盘 → 才改内存**。

    写 `.env` 失败 ⇒ **500 + 中文**，且内存**一个字都不动**（02 `:342`）。
    """
    # ① 归一化 + 校验：这一段跑完之前不碰磁盘、也不碰内存
    services = (
        _normalize_services(payload.services, settings.services)
        if payload.services is not None
        else settings.services
    )
    if payload.roles is not None:
        roles = _normalize_roles(payload.roles, services)
    else:
        # 服务被删掉时，指向它的绑定一并失效（不留悬空引用）；其余原样保留（含 rerank）
        alive = {item["id"] for item in services}
        roles = {role: bound for role, bound in settings.roles.items() if bound["service_id"] in alive}

    updates: dict[str, str] = {}
    notice = ""
    if payload.services is not None or payload.roles is not None:
        updates.update(dump_registry(services, _with_unknown_roles(roles)))
    if payload.chat is not None:
        _resolve_group(
            payload.chat,
            {"provider": settings.MODEL_PROVIDER, "base_url": settings.LLM_BASE_URL, "model": settings.LLM_MODEL},
            {"provider": "MODEL_PROVIDER", "base_url": "LLM_BASE_URL", "model": "LLM_MODEL", "api_key": "LLM_API_KEY"},
            updates,
        )
    if payload.embed is not None:
        _resolve_group(
            payload.embed,
            {"provider": settings.EMBED_PROVIDER, "base_url": settings.EMBED_BASE_URL, "model": settings.EMBED_MODEL},
            {"provider": "EMBED_PROVIDER", "base_url": "EMBED_BASE_URL", "model": "EMBED_MODEL", "api_key": "EMBED_API_KEY"},
            updates,
        )
    if payload.vision is not None and payload.vision.model is not None:
        updates["VISION_MODEL"] = payload.vision.model.strip()

    if payload.agent is not None:
        updates.update(_resolve_agent(payload.agent))
    if payload.translate is not None:
        updates.update(_resolve_translate(payload.translate))
    # 工作区根（专项工具升级-3 · 阶段 3 · 回归检查）：**显式 if-chain 加一条**。
    # ⚠️ 漏了这条 ⇒ `updates` 为空 ⇒ :556 提前 return ⇒ **一个字都不落盘**（静默不生效）。
    # 口径：**空串 = 清空根**（`K-回归检查`，必须能落盘成空值 ⇒ 走本分支而非「跳过」）；
    # 随后的 `:567` `setattr(settings, "WORKSPACE_ROOT", "")` 让注册门**当场关掉**。
    if payload.workspace_root is not None:
        updates["WORKSPACE_ROOT"] = payload.workspace_root.strip()
    if payload.reasoning is not None:
        updates.update(_resolve_reasoning(payload.reasoning))
    if payload.web is not None:
        updates.update(_resolve_web(payload.web))
    if payload.data is not None:
        raise HTTPException(status_code=400, detail=DATA_PUT_REJECTED)

    rerank_plan = _resolve_rerank(payload.rerank) if payload.rerank is not None else None
    if rerank_plan is not None:
        _apply_rerank(rerank_plan, updates)

    if not updates:
        # 空 payload（`{}`）或「传了但没有任何变化」：一个字都不写，直接回现值
        return current_payload()

    # ② 落盘（唯一写入口 `env_store.persist_env`）：失败 = 500 + 中文，内存不动
    try:
        persist_env(updates)
    except OSError:
        raise HTTPException(status_code=500, detail="写入 .env 失败，请检查文件权限") from None

    # ③ 落盘成功后才改内存单例（避免出现「内存比磁盘新」）
    for key, value in updates.items():
        setattr(settings, key, _stored_value(key, value))

    return current_payload(notice)


def _resolve_web(item) -> dict[str, str]:
    from app.agent.web.spec import provider_spec, normalize_provider

    if "ssrf_allow_any_host" in item.model_fields_set:
        raise HTTPException(status_code=400, detail=WEB_PUT_REJECTED)
    updates = {}
    updates.update({
        "WEB_ENABLE": item.enabled,
        "WEB_CHANNEL": item.channel,
        "WEB_SEARCH_PROVIDER": normalize_provider(item.provider) if item.provider is not None else None,
        "WEB_SEARCH_API_KEY": (item.api_key or "").strip() or None,
        "WEB_MAX_RESULTS": item.max_results,
        "WEB_FETCH_TIMEOUT": item.timeout,
        "WEB_LLM_PROVIDER": item.llm_provider,
        "WEB_LLM_MODEL": item.llm_model,
        "WEB_LLM_BASE_URL": item.llm_base_url,
        "WEB_LLM_API_KEY": (item.llm_api_key or "").strip() or None,
        "WEB_LLM_MAX_SEARCHES": item.llm_max_searches,
        "WEB_LLM_SEARCH_TOOL": item.llm_search_tool,
    })
    if item.channel is not None and item.channel not in {"api", "llm"}:
        raise HTTPException(status_code=400, detail="联网渠道必须是 api 或 llm")
    if item.provider is not None and provider_spec(normalize_provider(item.provider)) is None:
        raise HTTPException(status_code=400, detail="未登记的搜索服务商")
    if item.llm_provider is not None and item.llm_provider not in {"mimo", settings.WEB_LLM_PROVIDER}:
        raise HTTPException(status_code=400, detail="大模型搜索目前支持 MiMo")
    for value, low, high, label in (
        (item.max_results, 1, 10, "搜索结果数"),
        (item.timeout, 3, 60, "搜索超时"),
        (item.llm_max_searches, 1, 6, "搜索关键词数"),
    ):
        if value is not None and not low <= value <= high:
            raise HTTPException(status_code=400, detail=f"{label}必须在 {low} 到 {high} 之间")
    if item.llm_search_tool is not None and item.llm_search_tool not in {"", "web_search"}:
        raise HTTPException(status_code=400, detail="不支持的联网插件")
    return {
        key: str(value).lower() if isinstance(value, bool) else str(value).strip()
        for key, value in updates.items() if value is not None
    }


def _resolve_agent(item) -> dict[str, str]:
    """Agent 编排（条目 18）：**真实现**（`AGENT_MODE` / `AGENT_MAX_ROUNDS` / `AGENT_ENABLE_AUDIT`
    三条键的消费方已在 4.1/4.2/4.3 落地）。

    与老基线 `_resolve_agent` 同范式：只对**传进来的**字段做校验并攒 `updates`；
    `max_rounds` 夹在 1..12（太大等于没有上限，模型死循环烧 Key），**非 int 一律拒绝、
    不悄悄转换**；`mode` 非法 400。任何一项不合法 ⇒ 整个请求不写 `.env`。
    """
    updates: dict[str, str] = {}
    if item.mode is not None:
        mode = (item.mode or "").strip()
        allowed = [row["value"] for row in AGENT_MODES]
        if mode not in allowed:
            raise HTTPException(
                status_code=400,
                detail="未知的 Agent 模式，可选：off / react / plan_execute",
            )
        updates["AGENT_MODE"] = mode
    if item.max_rounds is not None:
        rounds = item.max_rounds
        if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 1 or rounds > 12:
            raise HTTPException(status_code=400, detail="工具循环轮次上限需在 1..12 之间")
        updates["AGENT_MAX_ROUNDS"] = str(int(rounds))
    if item.audit is not None:
        updates["AGENT_ENABLE_AUDIT"] = "true" if item.audit else "false"
    return updates


def _resolve_translate(item) -> dict[str, str]:
    """翻译（条目 23）：`api_key` 空串 = 保留现值（前端不回显 Key），**一个字都不写**。"""
    incoming = (item.api_key or "").strip()
    if not incoming:
        return {}
    return {"DEEPL_API_KEY": incoming}


def _resolve_reasoning(item) -> dict[str, str]:
    """思考程度 / 推理等级（设置页 `reasoning` 段）：校验通过才攒 `CHAT_REASONING`。

    只做「**校验并组装一行 JSON**」，落盘仍走 `put_settings` 既有的
    `persist_env` + 「落盘成功才改内存」——本函数**不碰磁盘、不碰内存**，
    任何一项不合法 ⇒ 抛 400 ⇒ 整个请求一个字都不写（R-4 同款零落盘口径）。

    取值链（与老基线 `_resolve_reasoning` 同判据，逐条给理由）：

    1. ``param`` 空 ⇒ **清空**（设置页选「厂商默认」走的就是这条路），写空串而不是删键；
    2. ``param`` 非空 ⇒ 必须先有绑定的对话模型（`chat_config`）：否则「给哪个型号设的」
       无从谈起，400；绑定指向已删服务时 `chat_config` 抛 `ValueError` ⇒ 同样按「没配好」400；
    3. 型号**未登记** ⇒ 400：登记表里没有官方参数可校验，我们不能凭 `param` 猜一个键发出去
       （未登记型号的手写通道是**直接改 `.env`**，见 `reasoning.store.request_body_for`）；
    4. ``param`` 与登记表里的官方参数名不一致 ⇒ 400（这就是老基线那句
       「取值归属的校验键」的意思：换了型号后旧 `param` 不该被接受）；
    5. 取值过不了 `coerce_value`（枚举外 / 非布尔 / 越界）⇒ 400；
    6. 都过 ⇒ 落一行 ``{"param":…,"value":…,"model":<当前型号>}``。

    **写型号**是给 `stale` 用的：换了对话型号之后，这条设置会被判「不属于当前型号」
    而不生效，并在界面上说明原因（`is_stale`），**不静默**。
    """
    from app.core.reasoning import coerce_value, dump_stored, spec_for

    param = (item.param or "").strip()
    if not param:
        return {"CHAT_REASONING": ""}
    try:
        config = settings.chat_config
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if config is None or not (config.model or "").strip():
        raise HTTPException(
            status_code=400,
            detail="请先绑定对话模型，再调整思考程度（选项由该型号的厂商规定）。",
        )
    spec = spec_for(config.provider, config.model)
    if spec is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"当前对话型号（{config.model}）没有登记官方思考参数，"
                "请在设置页改用手写通道：直接改本机 .env 的 CHAT_REASONING。"
            ),
        )
    if param != spec.param:
        allowed = "、".join(_reasoning_params())
        raise HTTPException(
            status_code=400,
            detail=f"该型号的官方思考参数是 {spec.param}，不是 {param}（已知参数：{allowed}）。",
        )
    value = coerce_value(spec, item.value)
    if value is None:
        raise HTTPException(
            status_code=400,
            detail=f"{param} 的取值不合法：请从界面给出的选项里选一个。",
        )
    return {"CHAT_REASONING": dump_stored(param, value, config.model)}


def _reasoning_params() -> list:
    """登记表里出现过的参数名（**只用于报错信息**，去重且保序）。"""
    from app.core.reasoning import SPECS

    seen: list = []
    for spec in SPECS:
        if spec.param not in seen:
            seen.append(spec.param)
    return seen


# --- 模型列表（同文件另三条端点）---------------------------------------------------------------

#: 三条「按地址拉模型」端点在**被守卫拒绝时**给用户看的「为了什么」
#: （`回归修复`）。三条端点（`POST /api/models` / `POST /api/web/llm/models` /
#: `GET /api/models`）对用户而言是同一件事 ⇒ 用同一个常量，而不是给 `_fetch_models`
#: 加 `purpose` 形参（那会改签名、动三个调用方，收益仅是一句文案的差别）。
#: 它是**常量**：判定与放行行为一个字都不受它影响（薄壳 docstring 已写死口径）。
_EGRESS_PURPOSE = "拉取模型列表"


async def _fetch_models(base_url: str, api_key: str) -> list[str]:
    """打厂商的 OpenAI 兼容 `GET {base_url}/models`；失败给可读中文（照老基线逐条同文案）。

    **本函数是「按地址拉模型」这条出网路的唯一 choke point**（`回归修复`）：
    它的三个调用方 —— `probe_models`（草稿）· `fetch_web_llm_models`（草稿 + 存档回落）·
    `list_models`（**只读 `settings` 里已保存的地址，不带请求体**）—— 因此一并受守卫。
    这正是「把守卫下沉到这里」而不是「在三个调用点各接一次」的理由：后者只要漏一处，
    存档态就是一条不设防的出网路（本条目立项时的原始缺口）。

    ⚠️ **已知限制（如实登记，⛔ 不得写「已消除」）**：这里的判定与真正的连接是
    **两次独立解析** —— 本接面**没有**地址固定（pinning；web 面靠 `PinnedBackend`
    把已校验地址钉进本次连接，本面没有等价物）⇒ **TOCTOU 窗口未消除**：
    本条目只做「出网前判定」，不做「把地址绑到连接上」。

    签名**保持不变**（不加 `purpose` 之类形参）：三个调用方因此无需改动；
    拒绝文案里的「为了什么」由 `_EGRESS_PURPOSE` 统一给出（三条端点本就是一回事）。
    """
    try:
        # 守卫必须**排在真正出网之前**：下一行是本函数唯一的出网点，
        # 守卫落在它**上方**是判据 C-03「拒绝路径零出网」的前提（挪到下面 = M-081-D2）。
        assert_outbound_url_safe(base_url, purpose=_EGRESS_PURPOSE)
    except OutboundBlocked as exc:
        # 薄壳给的是 `ValueError`（core 层不认识 fastapi）⇒ 到这里才映射成 HTTP 语义。
        # 400 + 中文可读：用户填了不该填的地址是**输入问题**，不是服务端故障（不得 500）。
        raise HTTPException(status_code=400, detail=str(exc)) from None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"})
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="拉取模型列表超时，请稍后重试") from None
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="无法连接模型服务，请检查网络或代理") from None

    if res.status_code == 401:
        raise HTTPException(status_code=401, detail="模型服务的 Key 无效或已过期")
    if res.status_code >= 400:
        raise HTTPException(status_code=400, detail=f"模型列表拉取失败（{res.status_code}）")

    try:
        data = res.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="模型服务返回内容无法解析") from None
    ids = sorted({item.get("id") for item in data.get("data", []) if isinstance(item, dict) and item.get("id")})
    if not ids:
        raise HTTPException(status_code=400, detail="厂商没有返回可用模型")
    return ids


def _require_openai(base_url: str, api_key: str, provider: str) -> str:
    if PROVIDER_PRESETS.get(provider, {}).get("kind", "openai") != "openai":
        raise HTTPException(status_code=400, detail="该厂商不支持在线拉取模型列表，请手动填写型号")
    if not base_url:
        raise HTTPException(status_code=400, detail="请先填写接口地址")
    if not api_key:
        raise HTTPException(status_code=400, detail="请先填写 API Key")
    return base_url


@router.post("/models")
async def probe_models(payload: ModelsIn) -> dict:
    """按草稿（还没保存的地址 / Key）拉模型列表（02 §7.6「各独立端点行为」）。

    给了 `service_id` 就用存好的 Key 兜底，前端只需在用户改了 Key 时传 `api_key`。
    """
    service = None
    if payload.service_id:
        service = settings.service_by_id(payload.service_id)
        if service is None:
            raise HTTPException(status_code=400, detail="服务不存在，请先保存后再拉取")

    provider = (payload.provider or (service or {}).get("provider") or "").strip().lower()
    if not PROVIDER_PRESETS.get(provider):
        raise HTTPException(status_code=400, detail="请先选择模型厂商")
    base_url = (payload.base_url or (service or {}).get("base_url") or "").strip().rstrip("/")
    base_url = base_url or PROVIDER_PRESETS[provider].get("base_url", "")
    api_key = (payload.api_key or "").strip() or (service or {}).get("api_key", "")

    base_url = _require_openai(base_url, api_key, provider)
    return {"models": await _fetch_models(base_url, api_key)}


@router.post("/web/llm/models")
async def fetch_web_llm_models(payload: WebModelsIn) -> dict:
    """联网检索·大模型搜索：按草稿拉模型列表（02 §7.6）。

    草稿没填就回落已保存的 `WEB_LLM_*` 配置 —— 界面上改完 Key / 地址不用先保存就能拉，
    避免「型号和服务商对不上」的手填事故。

    ⚠️ 与老基线的差异（**已登记**）：老基线还会回落 `web_spec.llm_provider_spec(...)` 的
    `base_url_hint` 与 `key_hint`；v2 **没有联网服务商登记表**（`web` 段的设置页写入通道未实装），
    故只在「地址 / Key 都没有」时给通用中文提示。
    """
    from app.agent.web.llm import resolved_config

    _provider, effective_base, effective_key, _model = resolved_config()
    saved_base = (settings.WEB_LLM_BASE_URL or "").strip().rstrip("/")
    # The implicit official endpoint is usable only with a configured credential.
    base_url = ((payload.base_url or "").strip() or saved_base or
                (effective_base if effective_key else "")).rstrip("/")
    api_key = (payload.api_key or "").strip()
    if not api_key and base_url == effective_base:
        api_key = effective_key
    if not base_url:
        raise HTTPException(status_code=400, detail="请先填写接口地址")
    if not api_key:
        raise HTTPException(status_code=400, detail="请先填写 API Key（对应平台的 API Key）")
    return {"models": await _fetch_models(base_url, api_key)}


@router.get("/models")
async def list_models() -> dict:
    """对话页模型菜单（02 §7.6「各独立端点行为」）：

    只列「对话角色绑定的那条供应商」里**标了「对话」**的型号；一条标记都没有时回退旧口径 =
    全部已勾选型号；没有服务注册表（还在手改 `.env`）时保持旧行为：现场拉一次。
    """
    try:
        config = _chat_config_or_400()
    except HTTPException:
        raise
    except (ProviderNotConfigured, ValueError) as exc:  # pragma: no cover - 防御
        raise HTTPException(status_code=400, detail=str(exc)) from None
    bound = settings.roles.get("chat") or {}
    service = settings.service_by_id(bound.get("service_id", "")) if bound else None
    if service is None:
        if config.kind != "openai":
            raise HTTPException(status_code=400, detail="当前厂商不支持在线拉取模型列表，请手动填写型号")
        if not config.api_key:
            raise HTTPException(status_code=400, detail="请先配置 API Key")
        return {
            "models": await _fetch_models(config.base_url, config.api_key),
            "service_id": "",
            "service_name": "",
            "current": config.model,
            "note": "",
        }

    tagged = [m for m in service["models"] if "chat" in (service.get("model_roles") or {}).get(m, [])]
    models = tagged or list(service["models"])
    return {
        "models": models,
        "service_id": service["id"],
        "service_name": service["name"] or service["provider"],
        "current": config.model,
        "note": "" if models else "这条供应商还没勾选型号：到设置页的供应商表单里拉取模型后，勾选要用的型号。",
    }


def _chat_config_or_400():
    """对话角色的当前配置；未配置 ⇒ **400 可读中文**（不依赖全局 `RagError` 处理器）。

    判据（本机地址允许空 Key）与 `app/api/__init__.py::_require_config` **逐字同口径**——
    这里刻意不复用那个私有函数（它在 4.5.2 正在改的文件里），改由
    `tests/api/test_models_4_5_3.py` 钉住两侧文案一致，避免「同一措辞两种说法」的漂移。
    """
    try:
        config = settings.chat_config
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if config is None or not (config.model or "").strip():
        raise HTTPException(
            status_code=400,
            detail="尚未配置对话模型，请在「设置 → API Key 服务」中配置（或直接改 .env）。",
        )
    base = config.base_url or ""
    if config.kind == "openai" and not (config.api_key or "").strip():
        if "127.0.0.1" not in base and "localhost" not in base:
            raise HTTPException(
                status_code=400, detail="尚未配置对话模型的 API Key，请在设置页的对应服务里填写。"
            )
    return config


__all__ = [
    "get_settings",
    "log_missing_role_bindings",
    "missing_role_bindings",
    "put_settings",
    "router",
]
