"""Qdrant 内嵌客户端生命周期（阶段 3 · 条目 3.0 · B6；对应验收 ⑧）。

四项：**应用内复用 / 退出释放 / 释放后重开 / 被占用时清楚中文提示**。
验收**不要求**「两个独立客户端同时打开同一目录都成功」——那是上游 portalocker 独占锁的
设计约束，本文件只要求「二次打开不炸 + 提示可读」。

全部走本地 ``path`` 模式（临时目录），不依赖 Qdrant 容器。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.config import settings
from app.vector import close_vector_store, get_vector_store
from app.vector.qdrant_store import QdrantVectorStore, VectorStoreUnavailable


@pytest.fixture(autouse=True)
def _isolate_default_store():
    """用例前后都释放默认单例：否则临时目录被持锁、且状态跨用例串味。"""
    close_vector_store()
    yield
    close_vector_store()


@pytest.fixture
def local_qdrant(tmp_path, monkeypatch) -> Path:
    """把 settings 指向临时目录，走本地（path）模式。"""
    path = tmp_path / "qdrant"
    path.mkdir()
    monkeypatch.setattr(settings, "QDRANT_PATH", str(path))
    monkeypatch.setattr(settings, "QDRANT_URL", "")
    return path


# --------------------------------------------------------------------------
# 复用
# --------------------------------------------------------------------------


def test_default_store_is_reused(local_qdrant):
    """无参 ``get_vector_store()`` 返回**同一个**客户端（改造前每次调用都新建）。"""
    first = get_vector_store()
    second = get_vector_store()
    assert first is second


# --------------------------------------------------------------------------
# 退出释放 + 释放后重开
# --------------------------------------------------------------------------


def test_close_then_reopen_succeeds(local_qdrant):
    """释放后能**重开**：新实例可用（本地目录锁已归还）。"""
    first = get_vector_store()
    close_vector_store()

    second = get_vector_store()
    assert second is not first
    info = second.collection_info()
    assert info["exists"] is False
    assert info["mode"] == "path"


def test_use_after_close_raises_readable_chinese(local_qdrant):
    """已释放的实例再用：抛可读中文，而不是 ``NoneType`` 属性错误。"""
    store = get_vector_store()
    store.close()

    with pytest.raises(VectorStoreUnavailable) as excinfo:
        store.count()
    assert "已关闭" in str(excinfo.value)


def _store_is_open(store) -> bool:
    """行为级判据：已释放的实例再用必须抛 ``VectorStoreUnavailable``。

    用 ``collection_info()`` 而不是 ``count()``：两者都先过 ``_require_client()`` 这道
    关闭检查（``qdrant_store.py:205`` / ``:201``），但 ``count()`` 在「开着但集合还不
    存在」时会抛 ``ValueError``（本地模式），会把「开着」误判成异常。
    """
    try:
        store.collection_info()
    except VectorStoreUnavailable:
        return False
    return True


def test_lifespan_releases_the_store(local_qdrant, monkeypatch):
    """退出释放的落点：**真跑** ``main.py::lifespan``，观察退出段是否释放默认单例。

    旧版是源码字符串包含断言（``"close_vector_store()" in main.py 文本``）—— 源码里写着
    名字但运行时名字压根没绑定（缺 import ⇒ ``NameError``）时它照样绿，属于假绿。
    这里改为行为级：真建单例 → 真跑 lifespan（engine/close_redis 用桩，避免真连
    MySQL/Redis）→ 退出段之后断言单例**确实被释放**，且释放发生在 ``close_redis`` 之后。
    """
    import main as main_module

    store = get_vector_store()
    assert _store_is_open(store) is True

    order: list[str] = []

    class _NoopEngine:
        async def dispose(self) -> None:
            order.append("engine.dispose")

    async def _close_redis_probe() -> None:
        order.append("close_redis")
        assert _store_is_open(store) is True, "close_redis 时向量库已经提前关了"

    monkeypatch.setattr(main_module, "setup_app_logging", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "ensure_service_registry", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "migrate_legacy_rerank", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "run_migrations", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "engine", _NoopEngine())
    monkeypatch.setattr(main_module, "close_redis", _close_redis_probe)

    async def _run() -> None:
        async with main_module.lifespan(None):
            pass

    asyncio.run(_run())

    assert order == ["engine.dispose", "close_redis"]
    assert _store_is_open(store) is False, "lifespan 退出段没有释放默认向量库单例"


# --------------------------------------------------------------------------
# 被占用时的中文提示
# --------------------------------------------------------------------------


def test_busy_directory_gives_chinese_hint(local_qdrant):
    """同一目录已被占用时：**二次打开不炸成英文堆栈**，而是可读中文提示。"""
    holder = get_vector_store()  # 持有目录独占锁
    try:
        with pytest.raises(VectorStoreUnavailable) as excinfo:
            QdrantVectorStore(path=str(local_qdrant), collection="zhilan_chunks")
    finally:
        holder.close()

    message = str(excinfo.value)
    assert "已被另一个进程占用" in message
    assert str(local_qdrant) in message  # 提示里带路径，用户知道是哪个目录
