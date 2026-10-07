"""guard 器官用例（阶段 3 · 条目 3.3）：收尾**恰好一次**。"""

from __future__ import annotations

from app.observe.guard import DONE, ERROR, StreamGuard


def test_done_is_accepted_once():
    guard = StreamGuard()
    assert guard.finished is False
    assert guard.terminal is None

    assert guard.done() is True
    assert guard.finished is True
    assert guard.terminal == DONE

    # 第二次收尾一律被拒——调用方据此不再发帧
    assert guard.done() is False
    assert guard.error() is False
    assert guard.terminal == DONE


def test_error_wins_if_it_comes_first():
    guard = StreamGuard()
    assert guard.error() is True
    assert guard.done() is False
    assert guard.terminal == ERROR


def test_fresh_guard_is_reusable_across_rounds():
    """流是一轮一个守卫（Agent 多步复用同一个守卫，但每轮请求新建）。"""
    first = StreamGuard()
    first.error()
    second = StreamGuard()
    assert second.finished is False and second.done() is True
