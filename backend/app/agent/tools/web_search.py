"""web_search：联网搜索并抓取网页片段（条目 4.1.2，契约工具名冻结）。

**契约工具名**：``web_search``（不要改成 ``search_web`` —— 那是客户端的函数名）。

## 分工

- 搜索/抓取的真本事在 :mod:`app.agent.web.client`（本模块只做「参数 → 工具产物」）；
- **观察文本的编号由运行器统一重渲染**（本轮快照编号，I3）：本模块渲染的 ``[n]`` 是
  兜底形态（单次调用的局部序号），运行器会用材料快照编号覆盖它；
- ``needs_web=True`` ⇒ ``web_enabled=False`` 时**根本不进模型清单**
  （见 :meth:`app.agent.registry.ToolRegistry.for_model`），所以这里不判 ``WEB_ENABLE``。

## 一次调用做两件事

搜索结果只有标题/链接/摘要，摘要往往不足以上下文用 ⇒ 本工具**顺手抓正文**，
把它作为 ``web_material`` 的 ``content``（抓不到就退回摘要，并记一条可读警告）。
摘要仍留在观察文本里给决策模型看（正文太重，给决策模型看反而干扰它决定下一步）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field

from app.agent.registry import ToolSpec
from app.agent.types import ToolObservation, clip
from app.agent.web.client import WebHit, WebToolError, fetch_page, search_web, web_material
from app.core.config import settings

logger = logging.getLogger(__name__)

TOOL_NAME = "web_search"

DESCRIPTION = "联网搜索并抓取网页，返回网页片段（带 URL，可作为引用来源）"

#: 观察文本里单条摘要的截断（与运行器 ``BLOCK_CLIP`` 同口径：300）
SNIPPET_CLIP = 300

#: 单条材料正文的硬上限（条目 4.1 契约口径：每页正文最多 4000 字进材料；
#: ``WEB_MAX_CONTENT`` 是配置侧的上限，这里再钉一道，配置写大也不会把上下文撑爆）
MATERIAL_CLIP = 4000

def _now() -> float:
    """计时基元（**模块内唯一**）：所有「剩余预算」判断都走它（R2-13 · D-回归检查）。

    单测打假钟**只 patch 这一处**（``monkeypatch.setattr(web_search, "_now", fake)``）：
    不要 patch ``time.monotonic`` 全局（会连带影响别人），也别用 ``asyncio.sleep`` 真等 ——
    预算 0.05 + 每条 0.05 时，第 1 条抓取前剩余仍 > 0 ⇒ 第 1 条照抓，只跳过 2 条，
    与「恰 3 条警告」不可兼容（开工包 v3 已把那条写法删掉）。
    """
    return time.monotonic()


#: 搜索实现签名（默认 :func:`app.agent.web.client.search_web`），单测注入假件用
SearchFn = Callable[..., Awaitable[list[WebHit]]]
#: 抓取实现签名（默认 :func:`app.agent.web.client.fetch_page`）
FetchFn = Callable[..., Awaitable[tuple[str, str]]]


class WebSearchArgs(BaseModel):
    """工具入参（同时是给模型的 JSON Schema 来源）。"""

    query: str = Field(description="检索查询文本")


def build_spec(*, search: SearchFn | None = None, fetch: FetchFn | None = None) -> ToolSpec:
    """装配 web_search 工具声明。

    ``search`` / ``fetch`` 是**注入点**（单测注入假件，绝不真出网）；
    ``None`` 时在 handler 内取真实现——**调用时**才查模块全局，这样
    ``monkeypatch.setattr(web_search_module, "search_web", fake)`` 也能生效。
    """
    impl = search
    fetch_impl = fetch

    async def handler(args: WebSearchArgs) -> ToolObservation:
        query = (args.query or "").strip()
        if not query:
            return ToolObservation(text='缺少 query 参数：{"query": "检索词"}', ok=False)

        fn = impl or search_web
        # 抓取预算（R2-13）：**起点在搜索调用之前**（搜索本身也花时间），
        # `0` = 不限（回滚用）；档位读的是**每次调用时**的 settings（不得 import 期固化）。
        budget = float(getattr(settings, "WEB_SEARCH_BUDGET_SECONDS", 20.0) or 0.0)
        started = _now()
        try:
            hits = list(await fn(query))
        except WebToolError as exc:
            # 可读中文原样回观察：消息即用户文案，warnings 会被追加到回答末尾
            return ToolObservation(text=str(exc), ok=False, warnings=[str(exc)])
        except Exception as exc:  # noqa: BLE001 —— 工具失败转观察，绝不炸穿（I5）
            # 回归修复：固定形状 + 原文**只进日志**；warnings 同样只带这一句（不含原文）
            logger.warning("联网搜索执行异常：%s", exc)
            text = f"联网搜索 执行失败（{exc.__class__.__name__}）。"
            return ToolObservation(text=text, ok=False, warnings=[text])

        if not hits:
            # 与 knowledge_search 同口径：空结果的观察文本逐字一致（模型据此改写查询或换工具）
            return ToolObservation(text="没有检索到相关资料。")

        limit = max(200, int(settings.WEB_MAX_CONTENT or 4000))
        grab = fetch_impl or fetch_page
        entries: list[tuple[str, str, str, str]] = []  # (url, 标题, 摘要, 正文)
        warnings: list[str] = []
        seen: set[str] = set()
        failed = 0
        for hit in hits:
            url = (hit.url or "").strip()
            if not url or url in seen:
                continue  # 同一 URL 只留一条（老基线同款：重复的引用卡片没有意义）
            seen.add(url)
            title = (hit.title or "").strip() or url
            snippet = (hit.snippet or "").strip()
            body = ""
            over_budget = budget > 0 and (_now() - started) >= budget
            if over_budget:
                # 预算用尽 ⇒ **本条不再抓正文**，但仍作为结果进材料（body 空 ⇒ 下面
                # `body or snippet` 天然退回摘要）。三条口径钉死：**不 continue 掉该条**、
                # **不动 failed 计数**（否则 :113 的总账会多一条假文案）、`ok=True` 不变。
                warnings.append(f"网页抓取超出预算：{url}")
            else:
                try:
                    page_title, page_text = await grab(url)
                    body = str(page_text or "")[:limit]
                    # 网页自己的标题通常比搜索结果的标题更完整（老基线同款：更长才换）
                    if page_title and len(page_title.strip()) > len(title):
                        title = page_title.strip()
                except Exception:  # noqa: BLE001 —— 抓不到正文不影响这条结果仍可引用（退回摘要）
                    # WebToolError 与意外异常**同一句警告**：用户只需知道「这页没抓到」，
                    # 具体原因（403/超时/非 HTML）留在客户端里给排障用，不往回答末尾堆
                    failed += 1
                    warnings.append(f"网页抓取失败：{url}")
            entries.append((url, title, snippet, body or snippet))

        if not entries:
            return ToolObservation(text="没有检索到相关资料。", warnings=warnings)
        if failed >= len(entries):
            # 老基线同款口径：全抓挂时给一条总账，免得用户只看到 N 条零散警告
            warnings.append("搜索有结果，但网页都抓取失败，未能并入上下文。")

        # ``[n]`` 是本观察内的 1 起序号（兜底形态）；运行器随后按快照编号重渲染（I3）
        text = "\n\n".join(
            f"[{index}] {title}\n{clip(snippet, SNIPPET_CLIP)}"
            for index, (_url, title, snippet, _body) in enumerate(entries, start=1)
        )
        materials = [
            web_material(title, url, body[:MATERIAL_CLIP], snippet=snippet)
            for url, title, snippet, body in entries
        ]
        return ToolObservation(text=text, materials=materials, warnings=warnings)

    return ToolSpec(
        name=TOOL_NAME,
        description=DESCRIPTION,
        parameters_schema=WebSearchArgs.model_json_schema(),
        handler=handler,
        dangerous=False,
        parameters=WebSearchArgs,
        needs_web=True,
        needs_confirm=False,
    )


#: 冻结 §五 的模块形状别名（每个工具模块都导出 ``Args``）
Args = WebSearchArgs

#: 默认声明（真实现 + 真抓取；运行器按轮次 ``build_spec(search=...)`` 覆盖依赖）
spec = build_spec()


__all__ = [
    "DESCRIPTION",
    "MATERIAL_CLIP",
    "SNIPPET_CLIP",
    "Args",
    "FetchFn",
    "SearchFn",
    "TOOL_NAME",
    "WebSearchArgs",
    "build_spec",
    "spec",
]
