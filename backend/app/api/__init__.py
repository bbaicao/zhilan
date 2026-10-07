"""主路由：27 端点骨架 + include trash / chat_group / translate / document_routes 等。

``/api/health`` 例外——它**不是占位**，是桌面壳与设置页依赖的**真探活**（回归修复）。
documents 域的全部端点（含 reindex / clean-preview / clean-report / generate-note /
generate-note/stream / DELETE，条目 2.4）已由 ``app/api/document_routes.py`` 实装并
include 进来，本文件不再保留这些路径的 501 占位。

阶段 4 · 条目 4.5.2 同理：notes / wiki / graph / knowledge-bases 共 9 条占位已删除，
实装落在 ``app/api/note_routes.py`` 并由本文件 include。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.chat_schemas import ChatDetailOut, ChatOut, MessageOut

# 占位响应只有一处实现：`app/api/placeholder.py`（回归修复）。本域（4.5.2）落地后
# 它在本文件里**不再被调用**，但这行 `import` 仍是**向后兼容的再导出**：契约测试
# `tests/api/test_placeholder_semantics.py::test_placeholder_helper_is_the_same_object_everywhere`
# 断言 `app.api.not_implemented is app.api.placeholder.not_implemented` ⇒ 这行不能删。
from app.api.placeholder import not_implemented  # noqa: F401
from app.core.config import settings
from app.core.errors import ProviderNotConfigured
from app.db.redis import get_redis_client
from app.db.session import SessionLocal, get_db
from app.session import recover as recover_mod
from app.session import repository as repo

logger = logging.getLogger(__name__)

router = APIRouter()

# 占位响应只有一处实现：`app/api/placeholder.py`（回归修复）。
# 这里 `from app.api.placeholder import not_implemented` 同时充当**向后兼容的再导出**
# ——`app.api.not_implemented` / `app.api.placeholder.not_implemented` 是同一个函数对象。


def _provider_fields(cfg) -> tuple[str | None, str | None]:
    """``(provider, model)``；未配置返回两个 ``None``。"""
    if cfg is None:
        return None, None
    return cfg.provider, cfg.model


def _require_config(config, what: str):
    """未配置时给统一提示（口径照老仓库 ``providers/base.py::require_config``）。

    本机地址（127.0.0.1 / localhost）允许空 Key——本地推理服务（如 Ollama）
    本来就不需要鉴权，这也是老仓库的既有口径。
    """
    if config is None or not (config.model or "").strip():
        raise ProviderNotConfigured(
            f"尚未配置{what}模型，请在「设置 → API Key 服务」中配置（或直接改 .env）。"
        )
    base = config.base_url or ""
    if config.kind == "openai" and not (config.api_key or "").strip():
        if "127.0.0.1" not in base and "localhost" not in base:
            raise ProviderNotConfigured(f"尚未配置{what}模型的 API Key，请在设置页的对应服务里填写。")
    return config


def _safe_config(attr: str):
    """读角色配置；配置坏数据（绑定已失效等）按「未配置」处理，不让 health 炸。"""
    try:
        return getattr(settings, attr)
    except Exception as exc:  # noqa: BLE001 —— health 是探活，任何异常都不该 500
        logger.warning("读取 %s 配置失败：%s", attr, exc)
        return None


@router.get("/health")
async def health():
    """健康检查：**真实探测** MySQL / Redis，并回报模型配置状态。

    返回体字段是**契约**（前端 ``api.ts::Health`` 逐字依赖，外壳冻结不可改）：

    ``status`` / ``database`` / ``redis`` / ``llm_configured`` /
    ``llm_provider`` / ``llm_model`` / ``embed_provider`` / ``embed_model`` /
    ``vision_provider`` / ``vision_model`` / ``clean``。

    三个状态字段的语义与老仓库一致：``database``、``redis`` 各自独立探测，
    任一失败只影响自己那一项；``status`` 是二者的合成（都为 ``ok`` 才 ``ok``）。
    """
    database = "error"
    try:
        async with SessionLocal() as db:
            await db.execute(text("SELECT 1"))
        database = "ok"
    except Exception as exc:  # noqa: BLE001 —— 探活失败就是 error，不抛
        logger.warning("health: MySQL 探测失败：%s", exc)

    redis_status = "error"
    try:
        await get_redis_client().ping()
        redis_status = "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("health: Redis 探测失败：%s", exc)

    chat = _safe_config("chat_config")
    embed = _safe_config("embed_config")
    vision = _safe_config("vision_config")

    llm_provider, llm_model = _provider_fields(chat)
    embed_provider, embed_model = _provider_fields(embed)
    vision_provider, vision_model = _provider_fields(vision)

    try:
        _require_config(chat, "对话")
        llm_configured = True
    except (ProviderNotConfigured, ValueError):
        llm_configured = False

    # 清洗规则变更提示：规则参数一变，历史向量就和新文本对不上
    clean = {
        "enabled": bool(getattr(settings, "CLEAN_ENABLE", False)),
        "rule_fingerprint": None,
        "stale_documents": 0,
        "needs_reindex": False,
        "hint": "",
    }
    if database == "ok":
        try:
            from app.pipeline.clean_status import clean_status

            async with SessionLocal() as db:
                clean = await clean_status(db)
        except Exception as exc:  # noqa: BLE001 —— 清洗状态是增益，探测失败不降级 health
            logger.warning("health: 清洗状态探测失败：%s", exc)

    # 会话事件写入失败计数（回归修复）：`safe_append_event` 的失败隔离是硬要求（I5），
    # 被它吞掉的事件在库里查不到 ⇒ 这个进程内计数是**唯一**可观测口径。探测失败不影响其他键（I5）。
    event_write_failures = 0
    try:
        from app.session.events import event_write_failures as _event_write_failures

        event_write_failures = int(_event_write_failures())
    except Exception as exc:  # noqa: BLE001 —— 观测项，绝不影响 health 的其它字段
        logger.warning("health: 事件写入失败计数读取失败：%s", exc)

    return {
        "status": "ok" if database == "ok" and redis_status == "ok" else "error",
        "database": database,
        "redis": redis_status,
        "llm_configured": llm_configured,
        "llm_provider": llm_provider,
        "llm_model": llm_model,
        "embed_provider": embed_provider,
        "embed_model": embed_model,
        "vision_provider": vision_provider,
        "vision_model": vision_model,
        "clean": clean,
        # 第 12 键（新增，回归修复）：进程内累计的会话事件写入失败次数，0 = 正常。
        # 只加不改义：前端不依赖它，少了它前端照常（冻结的 `Health` 类型只看上面 11 键）。
        "event_write_failures": event_write_failures,
    }


@router.get("/chats", response_model=list[ChatOut])
async def list_chats(
    archived: bool = False,
    db: AsyncSession = Depends(get_db),
) -> list[ChatOut]:
    """会话列表（阶段 4 · 条目 4.5.1；契约 ``02 §7.4`` 的 ``ChatOut``）。

    ``archived`` 是**只增不改义**的可选参数（老基线条目 40）：

    - 缺省 / ``false``：只回**在「最近」里的**会话（排除已归档）；
    - ``true``：只回已归档的（侧栏「已归档」入口用）。

    两种取值都排除**回收站里的**会话（``deleted_at`` 非空）——删除与归档是两件事：
    归档只是从「最近」隐藏、可原地恢复；删除进回收站、恢复要走 ``/api/trash/restore``。

    排序与老基线一致：``id`` 倒序（新的在前）。响应五键与契约逐字一致
    （``id`` / ``title`` / ``created_at`` / ``group_id`` / ``archived_at``），
    冻结前端 ``api.ts::ChatItem`` 按它渲染侧栏。
    """
    chats = await repo.list_chats(db, archived=archived)
    return [
        ChatOut(
            id=chat.id,
            title=chat.title,
            created_at=chat.created_at,
            group_id=chat.group_id,
            archived_at=chat.archived_at,
        )
        for chat in chats
    ]


@router.get("/chats/{chat_id}", response_model=ChatDetailOut)
async def get_chat(chat_id: int, db: AsyncSession = Depends(get_db)):
    """会话详情（**只读**；阶段 4 · 条目 4.4 · D2）。

    ⚠️ **归属改写（2026-09-19 已签字）**：本端点的只读实现从 ``4.5.1`` **前移**到 ``4.4``
    ——它是 ``MessageOut.recovered`` 的**唯一出口**（契约 ``02:244/246``；SSE 那条路是堵死的，
    决策包 §1.2 逐字核实过：冻结前端组装 ``done`` 时**不接** ``ev.recovered``）。
    没有它，4.4 的交货在界面上不可见、**无法自证**。``4.5.1`` 保留 ``list`` / ``delete``
    及其余 chats 域（``GET /chats/{chat_id}/...`` 的分组/归档仍归 4.5）。

    三件事，顺序固定：

    1. 取会话（回收站里的按不存在处理 ⇒ 404「会话不存在」，与老基线同口径）；
    2. **读之前先补平上一次崩溃留下的工具配对缺口**（``ensure_balanced``：只追加事件、
       不改任何 ``messages`` 行），然后 ``commit()`` 让新事件真的落库——
       v2 的事件层只做 SAVEPOINT、提交归调用方（老基线用自开会话，见 ``recover`` 模块 docstring）；
    3. ``recovered`` 由 ``recovery/synthesized`` 事件**现算**（不是列），
       并**并上本次刚合成的那几条**——本请求的读事务快照在步骤 1 就定了，
       并集才是「这次请求实际看到的全部恢复痕迹」（老基线同款理由）。
    """
    chat = await repo.get_chat(db, chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    synthesized = await recover_mod.ensure_balanced(db, chat_id)
    if synthesized:
        await db.commit()

    recovered_ids = await recover_mod.recovered_message_ids(db, chat_id) | {
        item.message_id for item in synthesized if item.message_id
    }
    messages = await repo.list_messages(db, chat_id)
    return ChatDetailOut(
        id=chat.id,
        title=chat.title,
        created_at=chat.created_at,
        group_id=chat.group_id,
        archived_at=chat.archived_at,
        messages=[
            MessageOut(
                id=row.id,
                role=row.role,
                content=row.content,
                refs=row.refs,
                kind=row.kind,
                filters=row.filters,
                reasoning=row.reasoning,
                thinking=row.thinking,
                skill_ids=row.skill_ids,
                agent_trace=row.agent_trace,
                images=row.images,
                answer_source=row.answer_source,
                recovered=int(row.id) in recovered_ids,
                created_at=row.created_at,
            )
            for row in messages
        ],
        context_summary=chat.context_summary,
        compressed_up_to=chat.compressed_up_to,
    )


@router.delete("/chats/{chat_id}")
async def delete_chat(chat_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    """删除会话（阶段 4 · 条目 4.5.1）：**先进回收站**，消息行原样保留。

    口径与老基线（条目 25）逐字一致，也与 v2 自己的回收站域自洽：

    - 只置 ``chats.deleted_at``（软删）⇒ ``GET /api/chats`` 不再列出、``GET /api/chats/{id}``
      按 404 处理；``messages`` **一行不动**，``POST /api/trash/restore`` 能原样恢复；
    - 「彻底删除」是回收站的职责（``DELETE /api/trash/{kind}/{id}``，归 4.5.4）——
      那时才连消息一起清（``repository.delete_messages``）。
    - 已在回收站里的会话再删一次 ⇒ 404「会话不存在」（老基线同：回收站里的会话对用户不可见）。
    """
    chat = await repo.get_chat(db, chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    chat.deleted_at = repo.utc_now()
    await db.commit()
    return {"ok": True, "message": "已移入回收站，可在回收站恢复或彻底删除"}


# 阶段 1 · 条目 1.5：/chat 与 /chat/stream 实装（本文件不再注册这两个路径）
from app.api.chat_routes import router as chat_router  # noqa: E402

router.include_router(chat_router)

# 阶段 3 · 条目 3.4：/search 实装（替代原本的 501 占位）
from app.api.search_routes import router as search_router  # noqa: E402

router.include_router(search_router)

# 阶段 2 · 条目 2.1：documents 域上传 / 文件夹扫描 / 导入（替代原本的 501 占位）
from app.api.document_routes import router as document_router  # noqa: E402

router.include_router(document_router)

# 阶段 4 · 条目 4.5.2：notes / wiki / graph / knowledge-bases 四域 9 端点
# （替代原本的 501 占位；**同一路径只在这里注册一次**）
from app.api.note_routes import router as note_router  # noqa: E402

router.include_router(note_router)

from app.api.chat_group_routes import router as chat_group_router  # noqa: E402
from app.api.translate_routes import router as translate_router  # noqa: E402
from app.api.trash_routes import router as trash_router  # noqa: E402

router.include_router(trash_router)
router.include_router(chat_group_router)
router.include_router(translate_router)
