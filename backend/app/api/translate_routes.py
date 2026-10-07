"""翻译路由（条目 23；阶段 4 · 条目 `4.5.3`）：``POST /api/translate``。

单个端点：选中文本 / 笔记 / 回答的中英互译。校验（空文本 / 超长 / 非法目标语言 / 未配置 Key）
**全部 400 可读中文**；:class:`app.api.translate_spec.DeepLError` 按类别映射：
鉴权失败 → 401、超时 → 504、其余（429 限频 / 456 配额 / 网络错误 / 其他状态码）→ 502，
detail 全部可读中文。Key 只存本机 `.env`，响应任何位置都不出现 Key（条目 2 的密钥策略）。

⚠️ **两条校验刻意写在函数体里而不是 pydantic 约束里**：live 契约用例
`test_translate_empty_text_400` / `test_translate_bad_target_400` 要求**空文本**与
**非法 target** 回 **400**（可读中文）；若用 `Field(min_length=1)` / `Literal["zh","en"]`，
pydantic 会先一步回 **422**（`main.py` 的全局校验处理器给的是通用文案，分不清是哪一个错了）。

挂载说明：本文件 router 前缀是 ``/translate``，由 ``app/api/__init__.py`` 聚合 router
（main.py 以 ``prefix="/api"`` 挂载）include 进来，最终路径即契约要求的 ``/api/translate``
—— 这里若直接写 ``prefix="/api/translate"`` 会变成 ``/api/api/translate``。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.translate_spec import (
    MAX_TRANSLATE_CHARS,
    NOT_CONFIGURED_HINT,
    TARGET_LANGS,
    DeepLError,
    translate_text,
)
from app.core.config import settings

router = APIRouter(prefix="/translate", tags=["translate"])

#: `DeepLError.kind` → HTTP 状态码（未列出的 kind 一律 502）
_ERROR_STATUS = {
    "bad_request": 400,
    "not_configured": 400,
    "auth": 401,
    "timeout": 504,
}


class TranslateRequest(BaseModel):
    """`translateText({text, target})`（`api.ts:1456`）。两个字段都可缺省，校验在函数体里做。"""

    text: str = ""
    target: str = ""


class TranslateResponse(BaseModel):
    """`TranslateOut`（`api.ts:1449-1453`）三键。"""

    text: str
    detected_source_language: str
    target: str


@router.post("", response_model=TranslateResponse)
async def translate(payload: TranslateRequest) -> TranslateResponse:
    """把一段文本翻译成中文或英文（DeepL）。"""
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="请提供要翻译的文本")
    if len(text) > MAX_TRANSLATE_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"要翻译的文本超过 {MAX_TRANSLATE_CHARS} 字上限（当前 {len(text)} 字），请分段翻译。",
        )
    target = (payload.target or "").strip().lower()
    if target not in TARGET_LANGS:
        raise HTTPException(status_code=400, detail="目标语言仅支持 zh（中文）或 en（英文）")
    # Key 未配置也走 400（`translate_text` 内的同一判据）：这里先拦一次，避免多建一个 HTTP 客户端
    if not (settings.DEEPL_API_KEY or "").strip():
        raise HTTPException(status_code=400, detail=NOT_CONFIGURED_HINT)

    try:
        result = await translate_text(text, target)
    except DeepLError as exc:
        raise HTTPException(status_code=_ERROR_STATUS.get(exc.kind, 502), detail=str(exc)) from None
    except Exception as exc:  # noqa: BLE001 —— 规格模块之外的意外错误也不能裸崩
        raise HTTPException(
            status_code=502, detail=f"翻译请求失败：{type(exc).__name__}，请稍后再试。"
        ) from exc

    return TranslateResponse(
        text=result["text"],
        detected_source_language=result["detected_source_language"],
        target=target,
    )


__all__ = ["router"]
