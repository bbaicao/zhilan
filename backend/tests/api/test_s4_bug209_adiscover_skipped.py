"""S4 · 回归修复 判据载体：async 发现腿的三条失败必须逐条记进 DiscoveryOutcome.skipped。

## 规格（交接件 §2 · 209）

- stdio 缺 command ⇒ {"pack": slug, "reason": "no_command"}
- http 缺 url ⇒ {"pack": slug, "reason": "no_url"}
- loader 抛异常 ⇒ {"pack": slug, "reason": "discover_failed", "transport": transport}
- 三条都**不抛**、specs == []（上游异常原文不进可见面）
- 反向保护：同步腿 discover_tools 的三态形状逐字不变；既有 unsupported_transport 条目不新增

## 为什么必须记账（判据的存在理由）

adiscover_tools 只把 specs 交给调用方，skipped 是运维侧唯一的
「发现过、失败了、原因是它」的证据。同步腿（discover_tools）早已逐条 append，
async 腿不 append ⇒ 两条腿**口径不对称**：同一个包在同步链路有原因可查，
在 async 链路（ReAct 主链路）只是零证据的「没发现」。

## 判据形态

**整表精确相等**（不是 in / len(...) >= 1）——条目形状与同步腿同构，
多一个键/少一个键都必须暴露；每条腿单独一个 slug，互不串味。
"""

from __future__ import annotations

import json

import pytest

from app.agent import mcp_transport
from app.core.config import settings


def _raw(*, slug: str, transport: str = "stdio", command: list[str] | None = None, url: str = "") -> str:
    """单个 MCP 包的 raw 配置（与 test_mcp_framing_c._raw 同构）。"""
    pack = {
        "slug": slug,
        "name": "S4-209 " + slug,
        "transport": transport,
        "enabled": True,
        "available": True,
        "command": list(command or []),
        "url": url,
        "tools": [],
    }
    return json.dumps([pack], ensure_ascii=False)


def _allow(monkeypatch: pytest.MonkeyPatch, *slugs: str, egress: str = "") -> None:
    monkeypatch.setattr(settings, "MCP_ALLOW_SERVERS", ",".join(slugs), raising=False)
    monkeypatch.setattr(settings, "MCP_ALLOW_EGRESS", egress, raising=False)


# ── 正向 discriminator：三条失败腿各记一条 ────────────────────────────────────

@pytest.mark.asyncio
async def test_bug209_stdio_missing_command_is_recorded(monkeypatch):
    slug = "s4-209-nocmd"
    _allow(monkeypatch, slug)

    outcome = await mcp_transport._adiscover(raw=_raw(slug=slug, command=[]))

    assert outcome.specs == []
    assert outcome.skipped == [{"pack": slug, "reason": "no_command"}]


@pytest.mark.asyncio
async def test_bug209_http_missing_url_is_recorded(monkeypatch):
    slug = "s4-209-nourl"
    # ⚠️ slug 必须同时在 MCP_ALLOW_SERVERS 与 MCP_ALLOW_EGRESS：否则会先在
    # 「http 包未在 MCP_ALLOW_EGRESS」那道闸 continue，腿就测不到缺 url 那条。
    _allow(monkeypatch, slug, egress=slug)

    outcome = await mcp_transport._adiscover(raw=_raw(slug=slug, transport="http", url=""))

    assert outcome.specs == []
    assert outcome.skipped == [{"pack": slug, "reason": "no_url"}]


@pytest.mark.asyncio
async def test_bug209_loader_failure_is_recorded(monkeypatch):
    slug = "s4-209-loadfail"

    async def _boom(*args, **kwargs):
        raise RuntimeError("S4-209 假 loader 爆炸")

    _allow(monkeypatch, slug)
    monkeypatch.setattr(mcp_transport, "load_mcp_tools_stdio", _boom)

    outcome = await mcp_transport._adiscover(raw=_raw(slug=slug, command=["s4-209-fake-mcp"]))

    assert outcome.specs == []
    assert outcome.skipped == [
        {"pack": slug, "reason": "discover_failed", "transport": "stdio"}
    ]


# ── 反向保护腿：既有口径不得破 ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bug209_reverse_async_unsupported_transport_entry_unchanged(monkeypatch):
    """既有 unsupported_transport 条目（async 腿）形状逐字冻结。"""
    slug = "s4-209-grpc"
    _allow(monkeypatch, slug)

    outcome = await mcp_transport._adiscover(raw=_raw(slug=slug, transport="grpc"))

    assert outcome.specs == []
    assert outcome.skipped == [
        {"pack": slug, "reason": "unsupported_transport", "transport": "grpc"}
    ]


def test_bug209_reverse_sync_leg_shapes_are_frozen(monkeypatch):
    """同步腿（discover_tools）三态形状逐字不变 —— 209 只补 async 腿。"""
    nocmd, nourl, weird = "s4-209-sync-nocmd", "s4-209-sync-nourl", "s4-209-sync-grpc"
    _allow(monkeypatch, nocmd, nourl, weird, egress=nourl)

    assert mcp_transport.discover_tools(raw=_raw(slug=nocmd)).skipped == [
        {"pack": nocmd, "reason": "no_command"}
    ]
    assert mcp_transport.discover_tools(raw=_raw(slug=nourl, transport="http")).skipped == [
        {"pack": nourl, "reason": "no_url"}
    ]
    assert mcp_transport.discover_tools(raw=_raw(slug=weird, transport="grpc")).skipped == [
        {"pack": weird, "reason": "unsupported_transport", "transport": "grpc"}
    ]


def test_bug209_reverse_sync_loader_failure_shape(monkeypatch):
    """同步腿 loader 失败仍是带 transport 的 discover_failed。

    ⚠️ 本腿必须写成**同步**用例：同步腿在内部自己跑事件循环，在 async 用例里调用
    会撞「loop already running」而被 except 吞成 discover_failed —— 形状对、原因错，
    等于测了 harness 而不是产品。
    """
    slug = "s4-209-sync-loadfail"

    async def _boom(*args, **kwargs):
        raise RuntimeError("S4-209 同步假 loader 爆炸")

    _allow(monkeypatch, slug)
    monkeypatch.setattr(mcp_transport, "load_mcp_tools_stdio", _boom)

    outcome = mcp_transport.discover_tools(raw=_raw(slug=slug, command=["s4-209-fake-mcp"]))

    assert outcome.skipped == [
        {"pack": slug, "reason": "discover_failed", "transport": "stdio"}
    ]
