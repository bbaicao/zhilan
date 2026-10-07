"""条目 2.4 · 从文档生成笔记草稿（``generate-note`` 同步 / SSE 共用）。

分层：`generation` 不 import `api` / FastAPI；模型可注入（单测 FauxProvider）。

输出契约（02 §7.4 GeneratedNoteOut）：title / content / category / tags /
source_document_id / source_filename / completeness。**不落库**——保存笔记是
notes 域的事，本模块只产草稿。

一期 completeness 用 **regex 检查器**（`checker="regex"`）：从源正文抽出日期与
显著数字，检查是否出现在生成 content 里；不编造 missing 项。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.core.errors import NOT_CONFIGURED_HINT, RagError
from app.generation import answer as answer_mod

logger = logging.getLogger(__name__)

NOTE_MAX_TOKENS = 4096

SYSTEM_PROMPT = (
    "你是知澜的笔记生成助手。请根据下面提供的文档正文，生成一份结构化笔记草稿。\n"
    "输出要求（二选一，优先 JSON）：\n"
    "1. 严格 JSON 对象，不要包 markdown 代码围栏以外的内容：\n"
    '{"title": "笔记标题", "content": "笔记正文（Markdown）", '
    '"category": "可选分类", "tags": ["可选标签"]}\n'
    "2. 若无法输出 JSON，则输出 Markdown：第一行 `# 标题`，其后为正文。\n"
    "约束：content 必须覆盖文档中的关键事实、数字与日期；不要编造文档里没有的内容；"
    "正文用中文，条理清晰。"
)

# 日期 / 显著数字：一期 completeness 检查器从源正文抽取的两类事实
_DATE_RE = re.compile(
    r"\d{4}[-/年.]\d{1,2}[-/月.]\d{1,2}日?|\d{4}年\d{1,2}月|\d{4}-\d{2}-\d{2}"
)
_NUM_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?%?)(?![\w.])")
_HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)

# 检查器最多核对这么多项，避免长文档把 missing 刷爆
_MAX_CHECK_ITEMS = 30


OnToken = Callable[[str], Awaitable[None]]
OnStatus = Callable[[str], Awaitable[None]]


@dataclass
class MissingItem:
    item: str
    kind: str
    context: str

    def as_dict(self) -> dict:
        return {"item": self.item, "kind": self.kind, "context": self.context}


@dataclass
class GeneratedNote:
    """生成笔记草稿（端点层再映射成 GeneratedNoteOut）。"""

    title: str
    content: str
    category: str
    tags: list[str] | None
    source_document_id: int
    source_filename: str
    completeness: dict


def raise_if_model_error(text: str) -> None:
    """模型把错误当答案时守卫（老仓库 raise_if_model_error 的简化版）。"""
    stripped = (text or "").strip()
    if not stripped:
        raise RagError("模型返回为空，无法生成笔记。")
    lowered = stripped.lower()
    markers = (
        "invalid api key",
        "incorrect api key",
        "rate limit",
        "rate_limit",
        "quota exceeded",
        "authentication",
    )
    # 整段就是错误串时才拦（正文里偶尔出现 error 单词不算）
    if len(stripped) < 400 and any(m in lowered for m in markers):
        raise RagError(f"模型返回错误，无法生成笔记：{stripped[:200]}")


def parse_note_output(
    raw: str, *, fallback_title: str = "", default_category: str = "", default_tags: list | None = None
) -> GeneratedNote:
    """稳健解析模型输出：JSON 优先，回落 Markdown 标题 + 正文。"""
    text = (raw or "").strip()
    title = ""
    content = text
    category = default_category or ""
    tags = list(default_tags) if default_tags else None

    parsed = _try_parse_json(text)
    if parsed is not None:
        title = str(parsed.get("title") or "").strip()
        content = str(parsed.get("content") or "").strip()
        if parsed.get("category") is not None:
            category = str(parsed.get("category") or "").strip()
        raw_tags = parsed.get("tags")
        if isinstance(raw_tags, list):
            tags = [str(t).strip() for t in raw_tags if str(t).strip()]
        if not content:
            content = text  # JSON 没给正文时退回原文，避免空笔记
    else:
        match = _HEADING_RE.search(text)
        if match:
            title = match.group(1).strip()
            content = text[match.end() :].strip("\n")
        else:
            lines = text.splitlines()
            if lines:
                title = lines[0].strip().lstrip("#").strip()
                content = "\n".join(lines[1:]).strip() or text

    if not title:
        title = fallback_title or "未命名笔记"

    return GeneratedNote(
        title=title,
        content=content,
        category=category,
        tags=tags,
        source_document_id=0,
        source_filename="",
        completeness={"complete": True, "checker": "regex", "checked": 0, "missing": []},
    )


def _try_parse_json(text: str) -> dict | None:
    """从纯 JSON / 代码围栏 / 夹杂文本里抠出 JSON 对象；抠不出返回 None。"""
    candidates = [text]
    fence = _JSON_FENCE_RE.search(text)
    if fence:
        candidates.insert(0, fence.group(1).strip())
    # 整段以 { 开头以 } 结尾时也试一次（模型偶尔不包围栏）
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.insert(0, text[start : end + 1])

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def check_completeness(content: str, source_body: str) -> dict:
    """一期 regex 完整性检查：源正文里的日期/显著数字是否出现在笔记 content 中。"""
    body = source_body or ""
    note = content or ""
    items: list[tuple[str, str, str]] = []  # (item, kind, context)

    seen: set[str] = set()
    for match in _DATE_RE.finditer(body):
        token = match.group(0).strip()
        if token and token not in seen:
            seen.add(token)
            items.append((token, "date", _context(body, match.start(), match.end())))
        if len(items) >= _MAX_CHECK_ITEMS:
            break

    if len(items) < _MAX_CHECK_ITEMS:
        for match in _NUM_RE.finditer(body):
            token = match.group(1).strip()
            # 过短数字噪声（序号、页码）：长度 < 2 的跳过
            if len(token) < 2 or token in seen:
                continue
            seen.add(token)
            items.append((token, "number", _context(body, match.start(), match.end())))
            if len(items) >= _MAX_CHECK_ITEMS:
                break

    missing: list[dict] = []
    for token, kind, context in items:
        if token not in note:
            missing.append(MissingItem(item=token, kind=kind, context=context).as_dict())

    return {
        "complete": not missing,
        "checker": "regex",
        "checked": len(items),
        "missing": missing,
    }


def _context(text: str, start: int, end: int, radius: int = 20) -> str:
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    return text[lo:hi].replace("\n", " ").strip()


async def _extract_text(result) -> str:
    return "".join(
        getattr(block, "text", "")
        for block in getattr(result, "content", [])
        if getattr(block, "type", "") == "text"
    )


async def _call_model(
    bound,
    user_prompt: str,
    *,
    on_token: OnToken | None = None,
) -> str:
    """走 generate 或 stream 取回正文；有 on_token 时走 stream 逐段回调。"""
    from cubeloop.providers.base import TextContent, UserMessage

    messages = [UserMessage(content=[TextContent(text=user_prompt)])]

    if on_token is None:
        from app.usage.instrumentation import usage_scope
        with usage_scope(role="note_summary"):
            result = await bound.generate(
                messages, system_prompt=SYSTEM_PROMPT, max_output_tokens=NOTE_MAX_TOKENS
            )
        if getattr(result, "stop_reason", "") == "error":
            detail = getattr(result, "error_message", "") or "模型返回错误"
            raise RagError(f"笔记生成失败，请稍后重试。（{detail}）")
        return await _extract_text(result)

    # 流式：BoundModel.stream 不接受 max_output_tokens（与 chat 同款限制）
    from app.usage.instrumentation import usage_scope
    with usage_scope(role="note_summary"):
        stream = await bound.stream(messages, system_prompt=SYSTEM_PROMPT)
    pieces: list[str] = []
    async for event in stream:
        if getattr(event, "type", "") == "text_delta":
            delta = getattr(event, "delta", "") or ""
            if delta:
                pieces.append(delta)
                await on_token(delta)
    return "".join(pieces)


async def generate_note(
    *,
    document_id: int,
    filename: str,
    body: str,
    category: str = "",
    tags: list | None = None,
    model=None,
    on_token: OnToken | None = None,
    on_status: OnStatus | None = None,
) -> GeneratedNote:
    """从文档正文生成笔记草稿。模型可注入；不落库。

    ⚠️ 默认装配走 **``build_aux_model()``**（辅助链路，不带用户配置的思考参数）；
    `document_routes._resolve_note_model()` 也走同一个入口——两条笔记路径
    （同步 + 流式）的请求体因此都不可能带上思考键（A15）。
    """
    source = (body or "").strip()
    if not source:
        raise RagError("该文档没有可用正文，无法生成笔记。")

    bound = model if model is not None else answer_mod.build_aux_model()
    if bound is None:
        raise RagError(NOT_CONFIGURED_HINT)

    if on_status is not None:
        await on_status("正在整理文档要点…")

    user_prompt = (
        f"文档文件名：{filename}\n"
        f"文档分类：{category or '（无）'}\n"
        f"文档正文：\n{source}"
    )
    raw = await _call_model(bound, user_prompt, on_token=on_token)
    raise_if_model_error(raw)

    if on_status is not None:
        await on_status("正在核对笔记完整性…")

    parsed = parse_note_output(
        raw,
        fallback_title=filename.rsplit(".", 1)[0] if filename else "",
        default_category=category or "",
        default_tags=tags,
    )
    parsed.source_document_id = document_id
    parsed.source_filename = filename
    parsed.completeness = check_completeness(parsed.content, source)
    return parsed


__all__ = [
    "GeneratedNote",
    "MissingItem",
    "check_completeness",
    "generate_note",
    "parse_note_output",
    "raise_if_model_error",
]
