"""向量层入口：工厂 + 公共类型再导出。业务不直接 import qdrant_client。

**阶段 3 · 条目 3.0（B6 · 客户端生命周期）**：改造前 ``get_vector_store()`` **每次调用都新建
客户端**，而 `_default_store()`（``lru_cache``）**全仓零调用**、也没有任何 ``close()`` 调用点。
本地 Qdrant 是 **portalocker 独占锁**——同一目录第二次打开必然失败（同进程与子进程均
``RuntimeError``），于是「第二次打开就炸」。现在：

- **无参调用 = 应用级单例**（``_default_store`` 的缓存真正接上）⇒ 重复调用复用同一客户端；
- **显式传 path/url/collection = 照参数新建**（单测与多库场景仍要独立实例，语义不变）；
- ``close_vector_store()`` 释放单例（``main.py`` 退出时调用）；释放后**可重开**新客户端；
- 目录被别的进程占用时，构造点抛出**可读中文**的 ``VectorStoreUnavailable``
  （见 ``app/vector/qdrant_store.py``）。
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import settings
from app.vector.base import VectorHit, VectorPoint, VectorStore
from app.vector.embed import EmbeddingClient, FakeEmbedding, OpenAICompatEmbedding
from app.vector.qdrant_store import QdrantVectorStore, VectorStoreUnavailable

__all__ = [
    "VectorHit",
    "VectorPoint",
    "VectorStore",
    "QdrantVectorStore",
    "VectorStoreUnavailable",
    "EmbeddingClient",
    "FakeEmbedding",
    "OpenAICompatEmbedding",
    "get_vector_store",
    "close_vector_store",
    "get_embedding_client",
]


def get_vector_store(
    *,
    path: str | None = None,
    url: str | None = None,
    collection: str | None = None,
) -> QdrantVectorStore:
    """path 优先（本地/单测），否则 URL；缺省读 settings。

    **三个参数全缺省 = 应用级单例**（复用，配合 ``close_vector_store()`` 释放）；
    任一参数显式给出则照参数新建独立实例（单测隔离需要）。
    """
    if path is None and url is None and collection is None:
        return _default_store()
    return QdrantVectorStore(
        path=path if path is not None else settings.qdrant_path,
        url=url if url is not None else (settings.QDRANT_URL or None),
        collection=collection or settings.QDRANT_COLLECTION,
    )


@lru_cache(maxsize=1)
def _default_store() -> QdrantVectorStore:
    """应用级默认 store（缓存单例）——生产路径（``ingest.get_vector_store()`` 等）都走它。

    进程内**只开一次**客户端：本地模式的目录独占锁因此不再自我冲突。
    """
    return QdrantVectorStore(
        path=settings.qdrant_path,
        url=settings.QDRANT_URL or None,
        collection=settings.QDRANT_COLLECTION,
    )


def close_vector_store() -> None:
    """释放应用级默认 store（本地模式即释放目录独占锁）；**幂等**。

    调用之后 ``get_vector_store()`` 会**重开**（缓存已清）。这是「退出释放 +
    释放后重开」两项验收的落点；单测也可以用它做用例间隔离。

    从未用过向量库时**不会**顺手创建一个再关掉（退出路径不该多开一次独占锁）。
    """
    if _default_store.cache_info().currsize == 0:
        return
    try:
        store = _default_store()
    except Exception:  # noqa: BLE001 —— 从未建起来（或建立即失败）：只清缓存
        _default_store.cache_clear()
        return
    store.close()
    _default_store.cache_clear()


def get_embedding_client(*, fake: bool = False) -> EmbeddingClient:
    """生产路径返回真 OpenAI 兼容客户端；**不**静默回落 Fake。

    未配置 embed 时由 ``OpenAICompatEmbedding.embed_texts`` 抛错，
    调用方（``index_chunks``）捕获后写 ``vector_error``，不阻止 document 进 ready。
    单测显式传 ``fake=True`` 或自行 monkeypatch。
    """
    if fake:
        return FakeEmbedding()
    return OpenAICompatEmbedding()
