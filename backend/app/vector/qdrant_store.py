"""Qdrant VectorStore 实现。qdrant_client 细节只出现在本模块。"""

from __future__ import annotations

from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from app.vector.base import VectorFilter, VectorHit, VectorPoint, VectorStore

__all__ = ["QdrantVectorStore", "VectorStoreUnavailable", "_point_id"]

# 本地模式是 **portalocker 独占锁**：同一目录同时只能有一个客户端（同进程、跨进程都一样）。
# 原始异常是英文 RuntimeError，产品侧读不懂，这里换成中文并保留底层原因（raise ... from）。
_LOCAL_BUSY_HINT = (
    "无法打开本地向量库目录 {path}：该目录已被另一个进程占用。"
    "Qdrant 本地模式是独占锁——同一份数据目录同时只能有一个知澜实例使用，"
    "请关闭其它知澜窗口/进程后重试"
)


class VectorStoreUnavailable(RuntimeError):
    """向量库不可用（本地目录被占用 / 连接失败 / 已关闭）；消息为可读中文。"""


def _point_id(value: str | int) -> int | str:
    """协议层 id 是 str；Qdrant 只接受 UUID 或 unsigned int。

    ``chunks.id`` 是 MySQL 自增整数，``str(chunk.id)`` 形如 ``"12"`` —— 必须转成
    int 再交给 client，否则 ``Point id 12 is not a valid UUID``。
    非纯数字串（UUID 等）原样透传。
    """
    if isinstance(value, int):
        return value
    text = str(value)
    if text.isdigit():
        return int(text)
    return text


class QdrantVectorStore(VectorStore):
    def __init__(
        self,
        *,
        url: str | None = None,
        path: str | None = None,
        collection: str = "zhilan_chunks",
    ) -> None:
        if path:
            try:
                self._client = QdrantClient(path=path)
            except Exception as exc:  # noqa: BLE001 —— 保留底层原因，换成可读中文
                raise VectorStoreUnavailable(
                    _LOCAL_BUSY_HINT.format(path=path) + f"（底层原因：{exc}）"
                ) from exc
            self._mode = "path"
        elif url:
            try:
                self._client = QdrantClient(url=url)
            except Exception as exc:  # noqa: BLE001
                raise VectorStoreUnavailable(
                    f"无法连接向量库 {url}：{exc}。请确认向量库服务已启动或改用本地路径模式"
                ) from exc
            self._mode = "url"
        else:
            raise ValueError("QdrantVectorStore 需要 url 或 path 之一")
        self.collection = collection
        self._owner: QdrantVectorStore | None = None

    def for_collection(self, collection: str) -> QdrantVectorStore:
        """复用同一个客户端读取另一集合，不再次打开本地独占目录。"""
        owner = self._owner or self
        client = self._require_client()
        if not isinstance(collection, str) or not collection:
            raise ValueError("向量集合名称不能为空")
        view = object.__new__(type(self))
        view._client = client
        view._mode = self._mode
        view._owner = owner
        view.collection = collection
        return view

    def for_space(self, space_id: str) -> QdrantVectorStore:
        """模型空间使用独立集合，共享根客户端的连接和本地目录锁。"""
        if not isinstance(space_id, str) or len(space_id) != 64 or any(
            c not in "0123456789abcdef" for c in space_id
        ):
            raise ValueError("向量空间编号必须是 SHA-256")
        owner = self._owner or self
        return self.for_collection(f"{owner.collection}__{space_id}")

    def _require_client(self):
        """取底层客户端；**已关闭**时给可读中文错误（不是 ``NoneType`` 属性报错）。"""
        if self._client is None:
            raise VectorStoreUnavailable(
                "向量库连接已关闭（本地目录锁已释放）；请重新调用 app.vector.get_vector_store() 获取"
            )
        if self._owner is not None:
            return self._owner._require_client()
        return self._client

    def close(self) -> None:
        """释放底层客户端（本地模式即**释放目录独占锁**）；**幂等**。

        阶段 3 · 条目 3.0（B6 · 释放后重开）：关闭后本实例不可再用
        （再调用会得到 ``VectorStoreUnavailable``），但**工厂可重新开**——
        ``app.vector.get_vector_store()`` 会丢掉缓存并新建客户端。
        """
        client = self._client
        if client is None:
            return
        self._client = None
        if self._owner is not None:
            return
        try:
            client.close()
        except Exception:  # noqa: BLE001 —— 退出路径上的关闭失败不该炸穿
            pass

    @staticmethod
    def _filters(filters: dict[str, Any] | VectorFilter | None) -> qmodels.Filter | None:
        if not filters:
            return None
        if isinstance(filters, VectorFilter):
            must = [
                qmodels.FieldCondition(key=key, match=qmodels.MatchAny(any=values))
                for key, values in filters.any_of.items()
            ]
            must += [
                qmodels.FieldCondition(key=key, match=qmodels.MatchValue(value=value))
                for key, values in filters.all_of.items()
                for value in values
            ]
            must += [
                qmodels.FieldCondition(
                    key=key, range=qmodels.Range(gte=bounds.gte, lte=bounds.lte)
                )
                for key, bounds in filters.ranges.items()
            ]
            return qmodels.Filter(must=must) if must else None
        must = [
            qmodels.FieldCondition(
                key=k,
                match=qmodels.MatchAny(any=v) if isinstance(v, list) else qmodels.MatchValue(value=v),
            )
            for k, v in filters.items()
            if v is not None
        ]
        if not must:
            return None
        return qmodels.Filter(must=must)

    def ensure_collection(self, dim: int) -> None:
        names = {c.name for c in self._require_client().get_collections().collections}
        if self.collection in names:
            info = self._require_client().get_collection(self.collection)
            # CollectionInfo.config.params.vectors 是 VectorParams(size=...)
            vectors = getattr(getattr(info, "config", None), "params", None)
            vectors = getattr(vectors, "vectors", None) if vectors is not None else None
            existing = None
            if vectors is not None:
                if hasattr(vectors, "size"):
                    existing = getattr(vectors, "size", None)
                elif isinstance(vectors, dict) and vectors:
                    first = next(iter(vectors.values()))
                    existing = getattr(first, "size", None)
            if existing is not None and int(existing) != int(dim):
                # 维度不一致绝不能静默 upsert（Qdrant 会 400，且错误难读）。
                # 不自动删集合——里面可能有有效数据；由运维/脚本显式重建。
                raise ValueError(
                    f"向量集合「{self.collection}」维度不匹配："
                    f"库内为 {existing}，本次 embedding 为 {dim}。"
                    f"请删除该集合后重建（或改用与 embed 模型一致的维度）。"
                )
            return
        self._require_client().create_collection(
            collection_name=self.collection,
            vectors_config=qmodels.VectorParams(size=dim, distance=qmodels.Distance.COSINE),
        )

    def upsert(self, points: list[VectorPoint]) -> None:
        if not points:
            return
        self._require_client().upsert(
            collection_name=self.collection,
            points=[
                qmodels.PointStruct(
                    id=_point_id(p.id), vector=p.vector, payload=p.payload
                )
                for p in points
            ],
            wait=True,
        )

    def query(
        self,
        vector: list[float],
        limit: int = 5,
        filters: dict[str, Any] | VectorFilter | None = None,
    ) -> list[VectorHit]:
        hits = self._require_client().query_points(
            collection_name=self.collection,
            query=vector,
            query_filter=self._filters(filters),
            limit=limit,
            with_payload=True,
        )
        return [
            VectorHit(id=str(h.id), score=float(h.score or 0.0), payload=dict(h.payload or {}))
            for h in hits.points
        ]

    def delete(self, ids: list[str]) -> None:
        if not ids:
            return
        self._require_client().delete(
            collection_name=self.collection,
            points_selector=qmodels.PointIdsList(points=[_point_id(i) for i in ids]),
            wait=True,
        )

    def set_payload(
        self,
        payload: dict[str, Any],
        *,
        ids: list[str] | None = None,
        filters: dict[str, Any] | VectorFilter | None = None,
    ) -> int:
        """原地改写命中点 payload 的指定键，返回**真的被改写**的点数（S6 · 186）。

        - ``ids``：先 ``retrieve`` 数出真实存在的点数，再 ``set_payload``（所以点不存在时
          返回 0，而不是把请求长度当结果）；
        - ``filters``：先用与 :meth:`query` 同一口径的 ``Filter`` 做 ``count(exact=True)``。
        - 两个都给/都不给 ⇒ ``ValueError``；``payload`` 为空 ⇒ ``ValueError``（空写是误用）。
        """
        if not payload:
            raise ValueError("set_payload 需要非空 payload")
        if (ids is None) == (filters is None):
            raise ValueError("set_payload 的 ids 与 filters 必须二选一")
        client = self._require_client()
        if ids is not None:
            if not ids:
                return 0
            point_ids = [_point_id(i) for i in ids]
            records = client.retrieve(collection_name=self.collection, ids=point_ids)
            if isinstance(records, tuple):  # 旧版返回 (list, next_offset)
                records = records[0]
            affected = len(records)
            selector: Any = qmodels.PointIdsList(points=point_ids)
        else:
            selector = self._filters(filters)
            if selector is None:
                raise ValueError("set_payload 的 filters 不能为空（或全为 None）")
            affected = int(
                client.count(
                    collection_name=self.collection, count_filter=selector, exact=True
                ).count
            )
        client.set_payload(
            collection_name=self.collection, payload=payload, points=selector, wait=True
        )
        return affected

    def list_ids(
        self,
        *,
        filters: dict[str, Any] | VectorFilter | None = None,
        limit: int | None = None,
    ) -> list[str]:
        """按 payload 过滤**只读**列出命中点 id（S6 · 186 的向量账本与同步校验用）。

        - 过滤器口径与 ``query`` / ``set_payload`` 完全一致（都走 ``_filters``）；
        - 只取 id：``with_payload=False`` / ``with_vectors=False``，不把 payload 或向量拉回本地；
        - 内部分页（每页 256），兼容旧版返回 ``(records, next_page_offset)`` 与新版
          ``ScrollResult.points/next_page_offset``；``limit`` 截断发生在**全部命中**之上。
        """
        client = self._require_client()
        selector = self._filters(filters)
        page = 256
        out: list[str] = []
        offset: Any = None
        while True:
            want = page if limit is None else min(page, limit - len(out))
            if want <= 0:
                break
            result = client.scroll(
                collection_name=self.collection,
                scroll_filter=selector,
                limit=want,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            if isinstance(result, tuple):  # 旧版返回 (records, next_page_offset)
                records, next_offset = result[0], result[1]
            else:
                records = getattr(result, "points", result)
                next_offset = getattr(result, "next_page_offset", None)
            out.extend(str(r.id) for r in records)
            if not records or next_offset is None:
                break
            if limit is not None and len(out) >= limit:
                break
            offset = next_offset
        return out

    def get_by_ids(self, ids: list[str]) -> list[VectorHit]:
        if not ids:
            return []
        result = self._require_client().retrieve(
            collection_name=self.collection,
            ids=[_point_id(i) for i in ids],
            with_payload=True,
        )
        # 新版返回 list[Record]；旧版可能返回 (list, next_offset)
        if isinstance(result, tuple):
            records = result[0]
        else:
            records = result
        out: list[VectorHit] = []
        for r in records:
            if isinstance(r, tuple):
                rid = str(r[0])
                payload = r[1] if len(r) > 1 and isinstance(r[1], dict) else {}
            else:
                rid = str(getattr(r, "id", r))
                payload = dict(getattr(r, "payload", None) or {})
            out.append(VectorHit(id=rid, score=0.0, payload=payload))
        return out

    def count(self) -> int:
        info = self._require_client().count(collection_name=self.collection, exact=True)
        return int(info.count)

    def collection_info(self) -> dict[str, Any]:
        names = {c.name for c in self._require_client().get_collections().collections}
        if self.collection not in names:
            return {"name": self.collection, "mode": self._mode, "exists": False}
        info = self._require_client().get_collection(self.collection)
        return {
            "name": self.collection,
            "mode": self._mode,
            "exists": True,
            "points_count": getattr(info, "points_count", None),
            "status": str(getattr(info, "status", "")),
        }
