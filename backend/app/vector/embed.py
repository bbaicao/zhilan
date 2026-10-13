"""Embedding 封装：真上游 OpenAI 兼容 + 单测用 Fake。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import logging
import math
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx

from app.core.app_logging import redact
from app.core.config import ModelConfig, settings
from app.usage import record_usage

log = logging.getLogger(__name__)


@runtime_checkable
class EmbeddingClient(Protocol):
    dim: int

    def identity(self) -> dict[str, str | int]:
        ...

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        ...


class _EmbeddingRequestError(RuntimeError):
    """参数、鉴权与响应形状等确定性错误；不重试，原文须先脱敏。"""


class FakeEmbedding:
    """确定性假向量：同文本同向量，供单测不连网。"""

    def __init__(self, dim: int | None = None) -> None:
        self.dim = dim or settings.EMBED_DIM

    def identity(self) -> dict[str, str | int]:
        return {"provider": "fake", "service_id": "", "base_url": "", "model": "fake", "dim": self.dim}

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = []
        for i in range(self.dim):
            raw.append((digest[i % len(digest)] / 255.0) - 0.5)
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]


def _record_embed_usage(cfg, usage: object) -> None:
    """一批嵌入**成功**后记一条用量（阶段 4 · 条目 4.5.4；fire-and-forget，绝不抛）。

    口径照老基线 ``app/rag/providers/openai_compat.py`` 的 embed 出口：**按批**记一条
    （``calls=1``），厂商响应里给了 ``usage`` 就写 tokens，没给就是 NULL=「未知」——
    **绝不**拿「这一批几条文本」之类的东西冒充 tokens（那是伪造数字）。

    ``provider`` 用 ``getattr`` 兜底：单测里的简易配置对象（``SimpleNamespace``）只带
    model / base_url / api_key，**没有** provider 字段，缺了就当空串，不影响记录本身。
    """
    data = usage if isinstance(usage, dict) else {}
    record_usage(
        kind="embed",
        provider=str(getattr(cfg, "provider", "") or ""),
        model=str(getattr(cfg, "model", "") or ""),
        prompt_tokens=data.get("prompt_tokens"),
        completion_tokens=data.get("completion_tokens"),
        total_tokens=data.get("total_tokens"),
    )


class OpenAICompatEmbedding:
    """OpenAI 兼容 /embeddings；按批切分，完整校验后返回，临时失败最多重试 1 次。

    ``_MAX_EMBED_BATCH`` 的来由（阶段 5 · 条目 5.3 真机抽查抓到的缺陷 `回归修复`，实测值）：

    原先本方法把**整篇文档的所有分块**塞进一次请求，而服务端有**单次批量上限**——DashScope
    的兼容端点直接回 400 原文：
    ``InvalidParameter: Value error, batch size is invalid, it should not be larger than 20.: input.contents``。
    实测边界（拿真文档的 97 个真实分块打真接口）：batch=1/5/10/16/20 **成功**，batch=25/50 **失败**。
    后果是「任何 >20 块的真实文档**完全无法向量化**」（16 页论文 = 97 块 ⇒ 全军覆没），
    而失败是优雅的（文档仍 ready、只记 `vectorized=false`），所以一直没被发现。
    ⇒ 取 **16**（低于实测上限 20，留余量；不同厂商上限不同，改这一个常量即可）。
    """

    # 单次请求的最大输入条数（实测依据见类 docstring；不引入新配置键——它是对特定服务端限制的适配）
    _MAX_EMBED_BATCH = 16
    _MAX_ATTEMPTS = 2
    _RETRY_BACKOFF = 0.5
    _RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(
        self, dim: int | None = None, timeout: float = 30.0, *, config: ModelConfig | None = None
    ) -> None:
        self.dim = settings.EMBED_DIM if dim is None else dim
        if type(self.dim) is not int or self.dim <= 0:
            raise ValueError("embedding 维度必须为正整数")
        self.timeout = float(timeout)
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("embedding timeout 必须为正且有限")
        # 同一次构建的所有批次使用同一模型快照，设置页中途换型号只影响下一次调用。
        self._config = deepcopy(settings.embed_config if config is None else config)
        self._service_id = str(getattr(self._config, "service_id", "") or "")
        if config is None and getattr(settings, "has_service_registry", False):
            self._service_id = str(settings.roles.get("embed", {}).get("service_id", ""))

    def identity(self) -> dict[str, str | int]:
        cfg = self._config
        if cfg is None:
            # 无模型仍可准备正文版本；embed_texts 明确失败后走首次关键词降级。
            return {"provider": "", "service_id": "", "base_url": "", "model": "", "dim": self.dim}
        # 身份会留档；移除 URL 中的凭证与参数，API Key 不参与向量空间身份。
        parts = urlsplit(str(cfg.base_url))
        base_url = parts._replace(
            netloc=parts.netloc.rsplit("@", 1)[-1], query="", fragment=""
        ).geturl().rstrip("/")
        return {
            "provider": str(getattr(cfg, "provider", "") or ""),
            "service_id": self._service_id,
            "base_url": base_url,
            "model": str(cfg.model),
            "dim": self.dim,
        }

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        cfg = self._config
        if cfg is None:
            raise RuntimeError("未配置 embed 模型（EMBED_* 或 MODEL_ROLES）")
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._MAX_EMBED_BATCH):
            batch = texts[start : start + self._MAX_EMBED_BATCH]
            vectors.extend(await self._embed_batch(cfg, batch))
        if len(vectors) != len(texts):
            raise RuntimeError("embedding 返回条数与输入不一致")
        return vectors

    @staticmethod
    def _index_positions(rows: list, count: int) -> list[int] | None:
        """响应行 index 唯一且完整覆盖输入时给出「行 → 输入位置」映射；否则 None（调用方按返回顺序兜底）。

        实际厂商（DashScope 兼容端点实测）对多输入会一律回 ``index=0``：此时 index 不能用作对齐依据。
        """
        positions: list[int] = []
        seen: set[int] = set()
        for row in rows:
            index = row.get("index") if isinstance(row, dict) else None
            if type(index) is not int or not 0 <= index < count or index in seen:
                return None
            seen.add(index)
            positions.append(index)
        return positions

    async def _embed_batch(self, cfg, texts: list[str]) -> list[list[float]]:
        """一批最多两次请求、退避 0.5 秒；总耗时上限 = ``2 * timeout + 0.5``。

        HTTPX 的 timeout 分连接／读／写阶段，不能单独限制整批耗时，因此另加总 deadline。
        响应不完整时不返回任何向量；仅 429、可恢复 5xx 和传输异常重试。
        """
        headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}
        payload = {"model": cfg.model, "input": texts}
        url = f"{cfg.base_url.rstrip('/')}/embeddings"
        last = "未返回有效向量"
        from uuid import uuid4
        from app.usage.instrumentation import observed_post
        call_id = uuid4().hex
        try:
            async with asyncio.timeout(self.timeout * self._MAX_ATTEMPTS + self._RETRY_BACKOFF):
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    for attempt in range(self._MAX_ATTEMPTS):
                        try:
                            resp = await observed_post(
                                client, url, config=cfg, kind="embed", call_id=call_id,
                                attempt=attempt + 1, headers=headers, json=payload,
                            )
                        except httpx.TransportError as exc:
                            # TransportError 原文可能包含带凭证的 URL；仅报告类型，避免落库／日志泄露。
                            last = type(exc).__name__
                        else:
                            if not 200 <= resp.status_code < 300:
                                text = resp.text
                                if cfg.api_key:
                                    text = text.replace(str(cfg.api_key), "***已脱敏***")
                                last = f"HTTP {resp.status_code} {redact(text)[:300]}"
                                if resp.status_code not in self._RETRY_STATUS:
                                    raise _EmbeddingRequestError(last)
                            else:
                                try:
                                    body = resp.json()
                                except ValueError:
                                    raise _EmbeddingRequestError("embedding 返回不是合法 JSON") from None
                                rows = body.get("data") if isinstance(body, dict) else None
                                if not isinstance(rows, list) or len(rows) != len(texts):
                                    raise _EmbeddingRequestError("embedding 返回条数与输入不一致")
                                positions = self._index_positions(rows, len(texts))
                                if positions is None:
                                    # 行数与向量都合法时，index 缺失／重复／越界不再整批拒绝：按返回顺序对齐。
                                    # 只记形状（条数），不记文本、密钥或 URL 凭证。
                                    log.warning(
                                        "embedding 响应 index 未唯一覆盖输入（%d 条）⇒ 按返回顺序对齐", len(rows)
                                    )
                                    positions = list(range(len(texts)))
                                vectors: list[list[float]] = [[] for _ in texts]
                                for position, row in zip(positions, rows):
                                    raw = row.get("embedding") if isinstance(row, dict) else None
                                    if not isinstance(raw, list) or len(raw) != self.dim:
                                        raise _EmbeddingRequestError(f"embedding 向量维度应为 {self.dim}")
                                    if any(type(value) not in (int, float) for value in raw):
                                        raise _EmbeddingRequestError("embedding 向量必须为有限数值")
                                    try:
                                        vector = [float(value) for value in raw]
                                    except (OverflowError, ValueError):
                                        raise _EmbeddingRequestError("embedding 向量必须为有限数值") from None
                                    if not all(math.isfinite(value) for value in vector):
                                        raise _EmbeddingRequestError("embedding 向量必须为有限数值")
                                    vectors[position] = vector
                                return vectors
                        if attempt + 1 < self._MAX_ATTEMPTS:
                            await asyncio.sleep(self._RETRY_BACKOFF)
        except _EmbeddingRequestError as exc:
            raise RuntimeError(f"embedding 调用失败：{exc}") from None
        except TimeoutError:
            raise RuntimeError("embedding 调用失败：本批重试总时限已到") from None
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise RuntimeError(f"embedding 调用失败：{type(exc).__name__}") from None
        raise RuntimeError(f"embedding 调用失败：{last}") from None
