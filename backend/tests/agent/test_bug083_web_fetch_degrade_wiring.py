"""`回归修复` 第二批判据载体：**网页抓取降级链（S18–S19）的吞点接线**。

🔴 口径（逐字，防误读）

· 钉的是「**降级出口存在且真的会动**」，**不是**「降级已被消除」；
· 可见面 = `GET /api/agent/audit` 的 **`counters.derived`**，键 = **`web_fetch_degrade`**；
· S19（代理在场 ⇒ DNS pinning fail-open 不固定）是**安全面降级**，与 S18（清洗/标题回落）
  同键计数——键名按「网页抓取降级」这条链命名，不分安全/质量子类（`COUNTER_KEYS` 只增不改义）。

## 覆盖的吞点（吞点地图 S18–S19）

| 吞点 | 落点（web/client.py） | 行为 |
|---|---|---|
| 回归检查 | `html_to_text` 的解析 except | 正文清洗降级 ⇒「粗暴去标签」粗糙文本（原先连 log 都没有） |
| 回归检查 | `_title_from_url` 的解析 except | 标题降级 ⇒ 回落 netloc / URL |
| S19 | `_maybe_pin` 代理在场分支 | fail-open 不 pin + WARNING（安全面） |

⚠️ **不计数的一条**（有意，防计数器噪声）：`_maybe_pin` 的「传输接缝不存在」debug 分支——
既有 60 余条 web 用例全用无接缝假件 ⇒ 计它会把每次测试跑都刷成「降级」；
该分支已有 debug 留痕（对抗审计 c-2 口径），本载体用负腿钉住「它不计数」。
"""

from __future__ import annotations

from urllib.parse import urlparse as _real_urlparse

import pytest

from app.agent.web import client as web_client
from app.core import derived_counters


@pytest.fixture(autouse=True)
def _reset_process_counters():
    """进程全局计数**跨用例泄漏**是本仓踩过的坑 ⇒ 每例前后清零。"""
    derived_counters.reset_failure_counts()
    yield
    derived_counters.reset_failure_counts()


def _counts() -> int:
    return int(derived_counters.failure_counts().get("web_fetch_degrade", 0))


# --------------------------------------------------------------------------
# 零态
# --------------------------------------------------------------------------


def test_zero_state_web_fetch_degrade_present_and_zero():
    """零态：`web_fetch_degrade` 在 `COUNTER_KEYS` 里且为 0（缺席 = 读不到）。"""
    counts = derived_counters.ordered_counts()
    assert counts.get("web_fetch_degrade") == 0
    assert "web_fetch_degrade" in derived_counters.COUNTER_KEYS


# --------------------------------------------------------------------------
# 回归检查：正文清洗降级
# --------------------------------------------------------------------------


class _ExplodingParser:
    """假解析器：`feed` 必抛（模拟 HTMLParser 对畸形输入抛异常的形态）。"""

    def feed(self, _html: str) -> None:
        raise RuntimeError("模拟 HTMLParser 解析异常")

    def close(self) -> None:
        return None

    def text(self) -> str:
        return ""


def test_s18a_html_clean_degrade_bumps(monkeypatch):
    """回归检查：解析器抛异常 ⇒ 粗糙文本回落 + `web_fetch_degrade` +1（原先完全静默）。"""
    monkeypatch.setattr(web_client, "_TextExtractor", _ExplodingParser)

    title, text = web_client.html_to_text("<html><body><p>hello</p></body></html>")

    assert title == ""
    assert "hello" in text and "<p>" not in text, "回落文本应是去标签的粗糙正文"
    assert _counts() >= 1, f"清洗降级被吞但计数没动 ⇒ 接线失效：{derived_counters.failure_counts()}"


# --------------------------------------------------------------------------
# 回归检查：标题提取降级
# --------------------------------------------------------------------------


def test_s18b_title_fallback_bumps(monkeypatch):
    """回归检查：urlparse 首次调用抛异常 ⇒ 标题回落 URL + 计数（原先静默）。"""
    state = {"calls": 0}

    def _flake(url, *args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise ValueError("模拟 urlparse 对畸形串抛异常")
        return _real_urlparse(url, *args, **kwargs)

    monkeypatch.setattr(web_client, "urlparse", _flake)

    title = web_client._title_from_url("https://example.com/some-page")

    assert state["calls"] == 2, "异常后回落链应继续走 urlparse（netloc 兜底）"
    assert title == "example.com", "回落链终点 = netloc（第二次 urlparse 已恢复）"
    assert _counts() >= 1, "标题降级被吞但计数没动 ⇒ 接线失效"


# --------------------------------------------------------------------------
# S19：代理在场 ⇒ pinning fail-open
# --------------------------------------------------------------------------


def test_s19_proxy_failopen_bumps():
    """S19：client 带 `_mounts`（代理判定）⇒ 不 pin + `web_fetch_degrade` +1。"""

    class _ProxiedClient:
        _mounts = {"https://": object()}

    web_client._maybe_pin(_ProxiedClient())  # type: ignore[arg-type]

    assert _counts() >= 1, "代理 fail-open 被吞但计数没动 ⇒ 接线失效"


# --------------------------------------------------------------------------
# 负腿：不计数分支钉住
# --------------------------------------------------------------------------


def test_negative_paths_do_not_bump(monkeypatch):
    """负腿：正常清洗 / 正常标题 / 无代理无接缝（假件形态）都**不**计数。"""
    title, text = web_client.html_to_text("<html><title>T</title><body><p>hi</p></body></html>")
    assert title == "T" and "hi" in text

    assert web_client._title_from_url("https://example.com/page") == "page"

    class _FakeClient:
        """既有 web 用例的假件形态：无 `_mounts`、无 `_transport`（走 debug 分支）。"""

        _mounts: dict = {}

    web_client._maybe_pin(_FakeClient())  # type: ignore[arg-type]

    assert _counts() == 0, f"正常路径/接缝缺失分支不该计数：{derived_counters.failure_counts()}"
