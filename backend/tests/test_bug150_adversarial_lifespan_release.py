"""回归修复 非作者对抗核验载体（task-12 · fix-wire 独立撰写；作者 = fix-schema）。

绑定字节（2026-09-28 task-11 落地后现读，**工作区 CRLF 口径**）：
- `backend/main.py` = `8F22879D915C3071BF0A30151CB8177844CBE60A2E10E977100D3BADCE892C40` / 8,280 B
  （150 态：:27 `from app.vector import close_vector_store` + :121 退出段调用）
- `backend/app/vector/__init__.py` = `778174D4B30DC13D957B4971AD20AC669ED98010E676049A335B03DD6162D158` / 4,044 B（只读参照）
- 作者载体 `backend/tests/test_qdrant_lifecycle.py` = `2D8CCFCB066E618B04A04FCBF2BD88F45B64F1D44E8465FF785555A9B03649A9`\
  / 5,684 B —— **只读、不改、不复制其代码**（C3 的判别力由变异矩阵跑作者文件来证，而不是靠读源码）。

以下 V1–V6 用例直接检查退出流程和资源释放行为：
- V1 import 面：`main.close_vector_store` 必须**就是** `app.vector.close_vector_store`（删 import ⇒ 红）
- V2 退出段次序/次数：真跑 `main.lifespan`（重活全桩），记录 ==
  `["body", "engine.dispose", "close_redis", "close_vector_store"]`（调用提前 / 删除 ⇒ 红）
- V3 真单例释放：真开默认单例 → 真跑 lifespan → 退出后 `get_vector_store()` **换人**，且主体内仍是同一个
- V4 次序（真路径）：`close_redis` 时单例**还开着**（提前释放 ⇒ 红）
- V5 释放异常不得被静默吞掉（桩抛 `RuntimeError` ⇒ 必须冒泡）
- V6【登记腿 · 当前行为】退出段无 try/finally：`close_redis` 抛错 ⇒ 释放被**跳过**（**非本笔引入**；
  反证条件 = 本条变红即作者已加保护，届时改写成「即使 close_redis 抛错也必须释放」）
"""

from __future__ import annotations

import asyncio

import pytest

import main as main_module
from app.core.config import settings
from app.vector import close_vector_store, get_vector_store


@pytest.fixture(autouse=True)
def _isolate_default_store():
    close_vector_store()
    yield
    close_vector_store()


@pytest.fixture
def lifecycle(monkeypatch, tmp_path):
    """重活全桩 + 数据根导到 tmp；返回次序记录器。"""
    path = tmp_path / "qdrant"
    path.mkdir()
    monkeypatch.setattr(settings, "QDRANT_PATH", str(path))
    monkeypatch.setattr(settings, "QDRANT_URL", "")

    order: list[str] = []

    class _NoopEngine:
        async def dispose(self) -> None:
            order.append("engine.dispose")

    monkeypatch.setattr(main_module, "setup_app_logging", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "ensure_service_registry", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "migrate_legacy_rerank", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "run_migrations", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "engine", _NoopEngine())
    return order


def _run_lifespan(order: list[str] | None = None) -> None:
    async def _run() -> None:
        async with main_module.lifespan(None):
            if order is not None:
                order.append("body")

    asyncio.run(_run())


def test_v1_import_face_is_the_real_app_vector_function():
    """行为级 import 面：模块属性必须**就是** `app.vector` 里那个函数。

    若把 import 挪进 `lifespan` 内部、或删掉 import，本腿直接 AttributeError ⇒ 红。
    """
    import app.vector as vector_pkg

    assert hasattr(main_module, "close_vector_store"), "main 模块没有绑定 close_vector_store"
    assert main_module.close_vector_store is vector_pkg.close_vector_store
    assert main_module.close_vector_store.__module__ == "app.vector"


def test_v2_exit_calls_release_after_close_redis_exactly_once(lifecycle, monkeypatch):
    """退出段次序：`engine.dispose` → `close_redis` → `close_vector_store`，各恰一次。"""
    order = lifecycle

    async def _close_redis() -> None:
        order.append("close_redis")

    def _release() -> None:
        order.append("close_vector_store")

    monkeypatch.setattr(main_module, "close_redis", _close_redis)
    monkeypatch.setattr(main_module, "close_vector_store", _release)

    _run_lifespan(order)

    assert order == ["body", "engine.dispose", "close_redis", "close_vector_store"]
    assert order.count("close_vector_store") == 1


def test_v3_real_singleton_is_released_at_exit(lifecycle, monkeypatch):
    """真单例：退出段之后 `get_vector_store()` 换人（= 旧实例真被释放）。"""
    order = lifecycle
    first = get_vector_store()
    assert get_vector_store() is first

    async def _close_redis() -> None:
        order.append("close_redis")

    monkeypatch.setattr(main_module, "close_redis", _close_redis)

    async def _run() -> None:
        async with main_module.lifespan(None):
            assert get_vector_store() is first, "启动段就释放了默认单例"

    asyncio.run(_run())

    assert order == ["engine.dispose", "close_redis"]
    assert get_vector_store() is not first, "退出段没有释放默认向量库单例"


def test_v4_release_happens_only_after_close_redis_real_path(lifecycle, monkeypatch):
    """真路径次序：`close_redis` 那一刻单例**还必须开着**（提前释放 ⇒ 红）。"""
    order = lifecycle
    first = get_vector_store()

    async def _close_redis() -> None:
        order.append("close_redis")
        assert get_vector_store() is first, "close_redis 之前默认单例已被释放"

    monkeypatch.setattr(main_module, "close_redis", _close_redis)

    _run_lifespan(order)

    assert order == ["body", "engine.dispose", "close_redis"]
    assert get_vector_store() is not first


def test_v5_release_failure_is_not_swallowed(lifecycle, monkeypatch):
    """对抗：释放抛错必须冒泡（⛔ 不得被静默吞掉）。"""
    order = lifecycle

    async def _close_redis() -> None:
        order.append("close_redis")

    def _boom() -> None:
        order.append("close_vector_store-raised")
        raise RuntimeError("release failed (adversarial probe)")

    monkeypatch.setattr(main_module, "close_redis", _close_redis)
    monkeypatch.setattr(main_module, "close_vector_store", _boom)

    with pytest.raises(RuntimeError):
        _run_lifespan()  # 不记 body：本例只看退出段次序

    assert order == ["engine.dispose", "close_redis", "close_vector_store-raised"]


def test_v6_exit_is_not_exception_isolated_registration_leg(lifecycle, monkeypatch):
    """【登记腿 · 当前行为】退出段无 try/finally：`close_redis` 抛错 ⇒ 释放被跳过。

    前缀态同样如此（本笔只修 NameError），**不承担判别力**——它记录的是**退出释放的降级风险**：
    真进程退出时 Redis 连接异常 ⇒ 本地 Qdrant 目录锁不归还。反证条件 = 本腿变红，
    即说明作者已加保护，届时应改写成「即使 close_redis 抛错也必须释放」。
    """
    order = lifecycle

    async def _close_redis_boom() -> None:
        order.append("close_redis")
        raise RuntimeError("redis down (adversarial probe)")

    def _release() -> None:
        order.append("close_vector_store")

    monkeypatch.setattr(main_module, "close_redis", _close_redis_boom)
    monkeypatch.setattr(main_module, "close_vector_store", _release)

    with pytest.raises(RuntimeError):
        _run_lifespan()  # 不记 body：本例只看退出段次序

    assert order == ["engine.dispose", "close_redis"]
    assert "close_vector_store" not in order
