"""占位语义的**单元级**钉子（`回归修复` 的连带修复）。

**为什么单列**：`tests/contract/test_error_semantics.py::test_business_endpoints_501` 属 **live** 套件
（打真端口；没起服务时整批 ERROR），它只能拿「此刻尚未实装」的端点当样本 —— 端点一实装，样本就过时，
**而过时不会被发现**（没起服务时那条用例根本不跑）。本轮就抓到三行过时样本（两行在条目 3.4 就已实装）。

本文件把「占位语义**本身**」钉在单元层：只要某个路由走 `app.api.not_implemented()`，
就必须是 **501 + `detail == "v2 重构中"`**；且这个文案**全仓只有一处字面量**（防止新路由各写一份、悄悄漂移）。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.api import not_implemented

REPO_ROOT = Path(__file__).resolve().parents[3]  # backend/tests/api/x.py -> 仓库根
APP_API = REPO_ROOT / "backend" / "app" / "api"
PLACEHOLDER_DETAIL = "v2 重构中"


def test_placeholder_is_501_with_fixed_detail():
    """占位响应形状：501 + 固定中文 `detail`（02 §7.6 / 老基线口径）。"""
    resp = not_implemented()

    assert resp.status_code == 501
    assert json.loads(bytes(resp.body).decode("utf-8"))["detail"] == PLACEHOLDER_DETAIL


def test_placeholder_detail_literal_has_one_definition_site():
    """`v2 重构中` 全仓只应有 **一处** 字面量：`app/api/placeholder.py`。

    落地时它被复制到了 **10 个**路由模块（6 个各自定义 `not_implemented()`、4 个在端点里写死），
    任何一次措辞改动都要改 10 处、漏一处就漂移；而漂移**只在起真服务的 live 套件里可见**
    （没起服务时整批 ERROR，看不见）。本用例把「单一实现」钉死（`回归修复`）。
    """
    hits = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in sorted(APP_API.rglob("*.py"))
        if PLACEHOLDER_DETAIL in path.read_text(encoding="utf-8")
    ]
    assert hits == ["backend/app/api/placeholder.py"], (
        f"`{PLACEHOLDER_DETAIL}` 应只有一处定义（app/api/placeholder.py），实际出现在：{hits}"
    )


def test_placeholder_helper_is_the_same_object_everywhere():
    """各路由模块拿到的必须是**同一个函数对象**（`app.api.not_implemented` 是再导出，不是副本）。"""
    from app.api import not_implemented as reexported
    from app.api.placeholder import not_implemented as canonical

    assert reexported is canonical
