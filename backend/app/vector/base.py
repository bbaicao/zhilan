"""VectorStore 协议层。

检索/业务只依赖本协议；Qdrant 细节锁在 qdrant_store 内（不散落 qdrant_client）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class VectorPoint:
    id: str
    vector: list[float]
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class VectorHit:
    id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PayloadRange:
    """数值区间，两端包含；时间使用 UTC 秒。"""

    gte: float | None = None
    lte: float | None = None


@dataclass(frozen=True)
class VectorFilter:
    """内部精确范围：同字段 any_of 为 OR，all_of 为 AND，字段之间 AND。

    空列表是空集，不等于不限；只需省略字段即可取消这一维限制。
    旧 dict 过滤接口仍按原精确匹配／列表 OR 解释。
    """

    any_of: dict[str, list[str | int]] = field(default_factory=dict)
    all_of: dict[str, list[str | int]] = field(default_factory=dict)
    ranges: dict[str, PayloadRange] = field(default_factory=dict)


class VectorStore(ABC):
    """向量库协议：add/query/delete/by_ids/collection。"""

    @abstractmethod
    def ensure_collection(self, dim: int) -> None:
        """集合不存在则创建；已存在则保证可写。"""

    @abstractmethod
    def upsert(self, points: list[VectorPoint]) -> None:
        ...

    @abstractmethod
    def query(
        self,
        vector: list[float],
        limit: int = 5,
        filters: dict[str, Any] | VectorFilter | None = None,
    ) -> list[VectorHit]:
        """内部类型化范围或兼容 payload 键值匹配；实现内组装。"""

    @abstractmethod
    def delete(self, ids: list[str]) -> None:
        ...

    @abstractmethod
    def set_payload(
        self,
        payload: dict[str, Any],
        *,
        ids: list[str] | None = None,
        filters: dict[str, Any] | VectorFilter | None = None,
    ) -> int:
        """**原地**改写命中点 payload 的指定键（不动向量、不改 id），返回受影响点数。

        选择器**二选一**：``ids``（显式点 id）或 ``filters``（payload 键值精确匹配，
        与 :meth:`query` 同一口径）；两个都给或都不给 ⇒ ``ValueError``。
        ``payload`` 里没提到的键保持原值（是合并，不是整包替换）。

        为什么协议里需要这个能力（S6 · 186）：知识库去重把旧 ``kb_id`` 的向量点改指到
        保留行上时，若只改 SQL 不改向量，检索按 ``kb_id`` 下推过滤会**整库漏召回**。
        旧办法（``get_by_ids`` 取回向量 + 重新 ``upsert``）要把向量一起回传再写回，
        慢且可能在往返中丢精度；``set_payload`` 只动 payload。
        """

    @abstractmethod
    def list_ids(
        self,
        *,
        filters: dict[str, Any] | VectorFilter | None = None,
        limit: int | None = None,
    ) -> list[str]:
        """按 payload 过滤**列出点 id**（只读；实现内部分页，``limit=None`` 表示全部）。

        为什么协议里要有这个只读入口（S6 · 186）：知识库去重把旧 ``kb_id`` 的向量点改指到
        保留行上之前，必须先把「到底会动哪些点」记成账本
        （``kb_merge_duplicates.py --repair-vector`` 的 ``vector-points-*.json``），
        改完之后还要按同一过滤器**回查残留**（``--verify-synced``）。``get_by_ids`` 要求
        先知道 id，做不到这两件事。过滤器口径与 ``query`` 一致。
        """

    @abstractmethod
    def get_by_ids(self, ids: list[str]) -> list[VectorHit]:
        ...

    @abstractmethod
    def count(self) -> int:
        ...

    @abstractmethod
    def collection_info(self) -> dict[str, Any]:
        ...
