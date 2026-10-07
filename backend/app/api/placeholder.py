"""501 占位响应的**唯一实现**（`回归修复`）。

**为什么单列一个模块**：``"v2 重构中"`` 这个 ``detail`` 是**契约文案**——``tests/contract/`` 断言它，
老基线也逐字如此。落地时它被复制进了 **10 个**路由模块（6 个各自写了一份 ``not_implemented()``、
4 个直接在端点里写死字面量）⇒ 任何一次措辞改动都要同时改 10 处，漏一处就是「同一文案两种说法」，
而这类漂移**只在起了真服务的 live 套件里才会暴露**（没起服务时整批 ERROR，看不见）——
与 `回归修复`（同一措辞多处出现、改了一处）同型。

故：文案与响应在这里定义**一处**，各路由模块一律 ``from app.api.placeholder import not_implemented``。
放在 ``app/api/`` 下的**叶子模块**（不 import 兄弟路由）⇒ 不引入循环依赖：
``app/api/__init__.py`` 与各兄弟模块都可以安全地 import 它。

钉子：``tests/api/test_placeholder_semantics.py``
（形状断言 + 「该字面量全仓只允许出现在本文件」的扫描）。
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

#: 未实装端点的占位 detail（契约文案，逐字冻结；改动必须同时更新契约测试的断言）
PLACEHOLDER_DETAIL = "v2 重构中"


def not_implemented() -> JSONResponse:
    """未实装端点的占位响应：**501 + 固定中文 detail**。"""
    return JSONResponse({"detail": PLACEHOLDER_DETAIL}, status_code=501)


__all__ = ["PLACEHOLDER_DETAIL", "not_implemented"]
