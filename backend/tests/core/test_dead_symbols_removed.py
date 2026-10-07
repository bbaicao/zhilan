"""回归修复 · 零调用点死代码已删除（**判据载体**）。

**本条只证四个符号**：``_service_for_flat`` / ``ProviderRateLimited`` /
``EmbeddingUnavailable`` / ``DimensionMismatch``。⛔ **不证**「仓库已无死代码」——
那是范围外的全仓主张，本文件既无载体也不该有。

**为什么用 importlib + hasattr 而不是 grep 文本**（本文件的核心口径）：
文本 grep 会被**注释与 docstring** 骗到——``config.py`` 里那段
「这里原本有个 ``repair_missing_role_bindings``」的防复活注释**故意保留**，
它逐字带着那个名字，grep 照样命中。反过来，注释里提到某个**还活着**的符号时，
grep 也会「命中」而误判它没被删。所以判据一律走**模块命名空间**：
名字在 ``vars(模块)`` 里就等于它还能被 import / 还能被调用，
不在就等于模块**真的**不导出它了。

**反向腿（必读）**：下面 ``TestDiscriminatingPower`` 对**仍然存在**的邻近符号
（``errors.py`` 里在用的 ``ProviderAuthError`` / ``RagError``…、``config.py`` 里在用的
``build_registry_from_legacy``）做**同一条**「不存在」断言，并断言它**必须不成立**。
没有这条腿，``assert not hasattr(...)`` 全绿也可能只是因为**断言方式本身恒真**
（例如拼错了模块名、或 hasattr 永远返回 False）——那就成了空靶判据。
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

# 被测的四个符号：(模块全名, 符号名)
REMOVED_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("app.core.config", "_service_for_flat"),
    ("app.core.errors", "ProviderRateLimited"),
    ("app.core.errors", "EmbeddingUnavailable"),
    ("app.core.errors", "DimensionMismatch"),
)

# 反向腿用的邻近符号：(模块全名, 符号名) —— 都是**在用的**，必须仍然存在。
# 每条都与某个被删符号**同模块**，否则证不了「同一个 hasattr 在本模块上能区分存在/不存在」。
SURVIVING_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("app.core.errors", "RagError"),
    ("app.core.errors", "ProviderAuthError"),   # ProviderRateLimited 的父类，仍在用
    ("app.core.errors", "ProviderTimeout"),     # 与 ProviderAuthError 同层，仍在用
    ("app.core.config", "build_registry_from_legacy"),  # 与 _service_for_flat 同模块，仍在用
)


def _module(name: str) -> Any:
    return importlib.import_module(name)


class TestRemovedSymbols:
    """四个符号在模块命名空间里**不存在**。"""

    @pytest.mark.parametrize(("module_name", "symbol"), REMOVED_SYMBOLS)
    def test_symbol_absent_from_module_namespace(self, module_name: str, symbol: str) -> None:
        module = _module(module_name)
        assert not hasattr(module, symbol), (
            f"{module_name} 仍然导出 {symbol} —— 回归修复 的删除未生效"
            f"（或它被重新加了回来）。请检查移除符号是否被重新导出。"
        )

    @pytest.mark.parametrize(("module_name", "symbol"), REMOVED_SYMBOLS)
    def test_symbol_not_importable_by_name(self, module_name: str, symbol: str) -> None:
        """第二条出口：``from <模块> import <符号>`` 必须 ImportError。

        与上一条**不同轴**——上一条问「模块有没有这个属性」，这条问
        「``from ... import ...`` 还认不认」。两者都过才排除
        「属性删了但还挂在某个 import 钩子上」这类半删状态。
        """
        with pytest.raises(ImportError):
            exec(f"from {module_name} import {symbol}", {})  # noqa: S102 - 本用例就是验 import 行为


class TestDiscriminatingPower:
    """反向腿：证明上面的断言方式**真的会红**。"""

    @pytest.mark.parametrize(("module_name", "symbol"), SURVIVING_SYMBOLS)
    def test_surviving_symbol_is_visible(self, module_name: str, symbol: str) -> None:
        """在用的邻近符号必须 **hasattr == True**。

        ⇒ 同一条 ``hasattr`` 在本模块上对「存在」返回 True、对「不存在」返回 False
        ⇒ ``TestRemovedSymbols`` 的绿**是真的**，不是断言恒真的空靶。
        """
        module = _module(module_name)
        assert hasattr(module, symbol), (
            f"{module_name}.{symbol} 也不见了 —— 反向腿失效："
            f"要嘛本用例的定位方式坏了，要嘛删过头了。"
        )

    def test_surviving_symbol_importable(self) -> None:
        """同一条 import 出口对**在用**符号必须成功（与 ``test_symbol_not_importable_by_name`` 成对）。"""
        namespace: dict[str, Any] = {}
        exec("from app.core.errors import ProviderAuthError", namespace)  # noqa: S102
        assert namespace["ProviderAuthError"] is not None

    def test_method_would_go_red_on_a_present_symbol(self) -> None:
        """**直接**证明断言方法有判别力：对仍在的符号跑被删符号那一套断言形状，它必须炸。

        这是「反向腿」最直白的一条——不靠参数表，当场把同一条断言打在
        ``ProviderAuthError`` 上，``AssertionError`` 必须出现。
        """
        module = _module("app.core.errors")
        assert hasattr(module, "ProviderAuthError")
        with pytest.raises(AssertionError):
            assert not hasattr(module, "ProviderAuthError"), "反向腿：这一句必须红"


class TestNoConfusableResidue:
    """半删状态的守卫：名字不能在模块里以**别的身份**留下。"""

    @pytest.mark.parametrize(("module_name", "symbol"), REMOVED_SYMBOLS)
    def test_no_dunder_export(self, module_name: str, symbol: str) -> None:
        """``__all__`` 里不许残留（那会让 ``from x import *`` 仍然拿得到它）。

        ``app.core.config`` / ``app.core.errors`` 现读**都没有 ``__all__``**
        ⇒ 这条对它们恒真，留着是给「将来有人补 ``__all__``」预先上锁。
        """
        module = _module(module_name)
        exported = getattr(module, "__all__", None)
        if exported is None:
            pytest.skip(f"{module_name} 没有 __all__（现读如此）—— 本腿不适用，不计红")
        assert symbol not in exported
