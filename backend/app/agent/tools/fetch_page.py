"""fetch_page：抓取指定网页正文（条目 4.1.2，契约工具名冻结）。

**契约工具名**：``fetch_page``。

## 为什么它和 web_search 分开

``web_search`` 是「问搜索引擎」，``fetch_page`` 是「打开我已知的那一页」——
后者用于补齐搜索结果里最关键的那条（例如摘要里提到的官方文档页），
不重新搜一遍。老基线把 ``fetch_page`` 无条件放进工具清单，本仓按
`接口冻结 §十一` 偏差登记 1 把它一并纳入联网门控（``needs_web=True``）：
不联网就不该暴露联网工具。

观察文本只带正文前 1200 字（决策模型看**够不够用**即止，全文进材料给回答链路），
材料正文上限 4000 字。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field

from app.agent.registry import ToolSpec
from app.agent.types import ToolObservation
from app.agent.web.client import WebToolError, fetch_page, web_material

logger = logging.getLogger(__name__)

TOOL_NAME = "fetch_page"

DESCRIPTION = "抓取指定网页的正文（只读），适合补全搜索结果里的关键页面"

#: 观察文本里正文的截断（条目 4.1 契约：前 1200 字）
OBSERVATION_CLIP = 1200

#: 材料正文上限（条目 4.1 契约：4000 字）
MATERIAL_CLIP = 4000

#: 抓取实现签名（默认 :func:`app.agent.web.client.fetch_page`），单测注入假件用
FetchFn = Callable[..., Awaitable[tuple[str, str]]]


class FetchPageArgs(BaseModel):
    """工具入参（同时是给模型的 JSON Schema 来源）。"""

    url: str = Field(description="要抓取的网页地址（http/https）")


def build_spec(*, fetch: FetchFn | None = None) -> ToolSpec:
    """装配 fetch_page 工具声明。

    ``fetch`` 是**注入点**（单测注入假件，绝不真出网）；``None`` 时在 handler 内取真实现
    ——**调用时**才查模块全局，这样 monkeypatch 模块属性也能生效。
    """
    impl = fetch

    async def handler(args: FetchPageArgs) -> ToolObservation:
        url = (args.url or "").strip()
        if not url:
            return ToolObservation(text='缺少 url 参数：{"url": "https://…"}', ok=False)

        fn = impl or fetch_page
        try:
            title, text = await fn(url)
        except WebToolError as exc:
            # 可读中文原样回观察（消息即用户文案）；失败不终止循环，模型可改参数再试
            return ToolObservation(text=str(exc), ok=False)
        except Exception as exc:  # noqa: BLE001 —— 工具失败转观察，绝不炸穿（I5）
            # 回归修复：固定形状 + 原文只进日志
            logger.warning("抓取网页执行异常：%s", exc)
            return ToolObservation(
                text=f"抓取网页 执行失败（{exc.__class__.__name__}）。", ok=False
            )

        title = (title or "").strip() or url
        body = str(text or "")
        material = web_material(title, url, body[:MATERIAL_CLIP])
        return ToolObservation(
            text=f"已抓取「{title}」，正文如下：\n{body[:OBSERVATION_CLIP]}",
            materials=[material],
        )

    return ToolSpec(
        name=TOOL_NAME,
        description=DESCRIPTION,
        parameters_schema=FetchPageArgs.model_json_schema(),
        handler=handler,
        dangerous=False,
        parameters=FetchPageArgs,
        needs_web=True,
        needs_confirm=False,
    )


#: 冻结 §五 的模块形状别名（每个工具模块都导出 ``Args``）
Args = FetchPageArgs

#: 默认声明（真实现；运行器按轮次 ``build_spec(fetch=...)`` 覆盖依赖）
spec = build_spec()


__all__ = [
    "DESCRIPTION",
    "MATERIAL_CLIP",
    "OBSERVATION_CLIP",
    "Args",
    "FetchFn",
    "FetchPageArgs",
    "TOOL_NAME",
    "build_spec",
    "spec",
]
