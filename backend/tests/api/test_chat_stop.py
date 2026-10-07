"""停止注册表用例（阶段 3 · 条目 3.4）：`request_id` → `asyncio.Event` 的登记/注销/置位。

纯内存，不碰 DB、不起服务——竞态语义（覆盖登记不误删）在这里钉死。
"""

from __future__ import annotations

import asyncio

import pytest

from app.api import chat_stop


@pytest.fixture(autouse=True)
def _clean_registry():
    chat_stop.reset()
    yield
    chat_stop.reset()


def test_register_and_request_stop():
    event = chat_stop.register("req-1")
    assert isinstance(event, asyncio.Event)
    assert chat_stop.is_registered("req-1") is True

    assert chat_stop.request_stop("req-1") is True
    assert event.is_set() is True


def test_request_stop_is_idempotent_and_safe_when_absent():
    assert chat_stop.request_stop("never-registered") is False

    chat_stop.register("req-2")
    assert chat_stop.request_stop("req-2") is True
    assert chat_stop.request_stop("req-2") is True  # 重复点停止不报错


def test_blank_request_id_is_not_registered():
    assert chat_stop.register(None) is None
    assert chat_stop.register("") is None
    assert chat_stop.active_count() == 0


def test_unregister_only_removes_its_own_registration():
    """同一 ``request_id`` 被后一轮覆盖登记时，先结束的那一轮不许误删后一轮的信号。"""
    first = chat_stop.register("req-3")
    second = chat_stop.register("req-3")

    # 回归修复：覆盖登记必须**先置位旧一轮的 Event**——否则先到的那一轮再也收不到
    # 停止信号（用户点停止只打在覆盖后的新一轮上）。今日此断言为红，修复后为绿。
    assert first.is_set() is True
    assert second.is_set() is False  # 新一轮不受影响（尚未收到停止）

    chat_stop.unregister("req-3", first)  # 老一轮结束
    assert chat_stop.is_registered("req-3") is True  # 新一轮的登记还在

    chat_stop.unregister("req-3", second)
    assert chat_stop.is_registered("req-3") is False


def test_unregister_without_event_removes_entry():
    chat_stop.register("req-4")
    chat_stop.unregister("req-4")
    assert chat_stop.is_registered("req-4") is False


def test_re_register_sets_the_previous_event():
    """回归修复：同 ``request_id`` 的第二次登记 ⇒ 第一轮拿到停止信号（它必须能停）。

    ``request_id`` 由客户端生成，正常不复用；一旦复用（重试 / 上层复用 id），老一轮的登记
    会被覆盖，此后**没有任何入口**能置位它的 Event ⇒ 那一路流只能跑到自然结束。覆盖时
    置位旧 Event 把这条路径收口：老一轮照常走它自己的停止检查点。
    """
    first = chat_stop.register("req-5")
    second = chat_stop.register("req-5")

    assert first.is_set() is True
    assert second.is_set() is False

    # 用户点停止仍然只打在**当前**在册的那一轮
    assert chat_stop.request_stop("req-5") is True
    assert second.is_set() is True
    assert chat_stop.is_registered("req-5") is True

    chat_stop.unregister("req-5", second)
    assert chat_stop.is_registered("req-5") is False


def test_cross_unregister_is_still_blocked():
    """反例（CC-EDGE-03）：先结束的一轮的 ``unregister`` **不得**摘掉后一轮的登记。

    本用例是 182 修复的**反向护栏**：置位旧 Event 之后，注销侧的身份守卫必须原样生效——
    否则后一轮会被先结束的那一轮顺手摘掉 ⇒ 后一轮永远停不下来（交叉误停）。
    """
    first = chat_stop.register("req-6")
    second = chat_stop.register("req-6")

    chat_stop.unregister("req-6", first)
    assert chat_stop.is_registered("req-6") is True, "交叉误停：老一轮摘掉了新一轮的登记"

    assert chat_stop.request_stop("req-6") is True
    assert second.is_set() is True
