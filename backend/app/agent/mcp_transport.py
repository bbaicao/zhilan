"""MCP 真调用传输层（第 2 批 ③ · D-i…D-o / D23 / D24）。

**只放在本模块**：命名 / 危险过滤 / 默认钩子仍在 ``app/agent/tools/mcp.py``（零改动）。
``cubeloop.mcp`` 的 loader **顶层绑定**（A15 要 monkeypatch 模块属性）；SDK 的
``from mcp …`` 在上游函数体内 ⇒ 冻结态模块图不会因此首次拉入 ``mcp`` SDK。

核心顺序（D-i）：**先判许可，再发现**。任一不满足 ⇒ loader 零调用、零子进程。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from cubeloop.mcp import load_mcp_tools_http, load_mcp_tools_stdio  # noqa: F401 —— 顶层绑定
from pydantic import BaseModel, ConfigDict, create_model

from app.agent.registry import ToolSpec
from app.agent.tools.mcp import (
    OBSERVATION_CLIP,
    McpToolArgs,
    as_bool,
    as_command,
    dangerous_by_default,
    pack_tool_name,
    resolve_dangerous,
)
from app.agent.types import ToolObservation, clip
from app.core.config import settings

log = logging.getLogger(__name__)

#: 逐包发现超时（小于上游 30s 默认；D-m）
MCP_DISCOVERY_TIMEOUT = 8.0
#: 单次调用超时（D-m）
MCP_CALL_TIMEOUT = 20.0


def _ensure_mcp_tool_alias() -> None:
    """兼容垫片：``mcp`` 2.x 字段名 vs cubeloop 0.14.1 读的 camelCase。

    - ``Tool.input_schema`` ↔ 上游 ``desc.inputSchema``
    - ``CallToolResult.is_error`` ↔ 上游 ``resp.isError``
    不补则真发现 / isError 映射都会静默失真（实测）。
    """
    try:
        import mcp.types as mcp_types
    except Exception:  # noqa: BLE001
        return

    tool = getattr(mcp_types, "Tool", None)
    if tool is not None and not hasattr(tool, "inputSchema"):
        def _input_schema(self):  # noqa: ANN001
            return getattr(self, "input_schema", None)

        try:
            tool.inputSchema = property(_input_schema)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    call_result = getattr(mcp_types, "CallToolResult", None)
    if call_result is not None and not hasattr(call_result, "isError"):
        def _is_error(self):  # noqa: ANN001
            return bool(getattr(self, "is_error", False))

        try:
            call_result.isError = property(_is_error)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


_ensure_mcp_tool_alias()


@dataclass
class DiscoveryOutcome:
    """发现出口（D-i″）：上线的 specs + 被拒工具的结构化原因。"""

    specs: list[ToolSpec] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


def _slugify(raw: str) -> str:
    """与 ``mcp.py::_slugify`` / 老基线 ``skills.slugify`` 同口径（D24）。"""
    import re

    text = (raw or "").strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "-", text).strip("-")
    if not text:
        return "skill"
    if text[0].isdigit():
        text = f"s-{text}"
    return text[:64]


def parse_allowlist(raw: str | None) -> frozenset[str]:
    """``MCP_ALLOW_SERVERS`` / ``MCP_ALLOW_EGRESS``：逗号分隔归一化 slug、精确匹配。

    非法值（含 ``*``）⇒ **整键视为空** + warning（fail-closed，D24）。
    """
    text = str(raw or "")
    if not text.strip():
        return frozenset()
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if any(p == "*" or p == "" for p in parts):
        log.warning("MCP 许可键含通配或空项，整键按空处理：%r", raw)
        return frozenset()
    return frozenset(_slugify(p) for p in parts)


def _parse_servers_raw(raw: str | None) -> list[dict]:
    """严格读原始 JSON（不走 ``load_packs`` 的宽容归一，D-j）。"""
    text = str(raw if raw is not None else settings.MCP_SERVERS or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError as exc:
        log.warning("MCP_SERVERS 不是合法 JSON：%s", exc)
        return []
    if not isinstance(data, list):
        log.warning("MCP_SERVERS 顶层不是数组")
        return []
    return [item for item in data if isinstance(item, dict)]


def _node_reason(sub: Any) -> str | None:
    """**一个**参数节点是否超子集（``回归修复``：数组元素 / ``additionalProperties`` 也要看）。

    只加严、不放宽：既有三类原因串（``nested_object|untyped|composite``）与判定**顺序**逐字保留，
    把原先「只看一层」补成「沿 ``items`` / ``additionalProperties`` 一路看下去」—— 改前
    ``list[dict[str, Any]]``（塌成 ``{"items": {"type": "object", ...}}``）与裸 ``list``
    （``{"items": {}}``）都从这层漏过去、放行上线（与「塌成 ``dict[str, Any]`` 即不上线」的声称相反）。
    """
    if not isinstance(sub, dict) or not sub:
        return "untyped"
    if "$ref" in sub or "anyOf" in sub or "oneOf" in sub or "allOf" in sub:
        return "composite"
    if sub.get("type") == "object":
        return "nested_object"
    if "type" not in sub:
        return "untyped"
    if "additionalProperties" in sub:
        # 非 object 类型还带 additionalProperties：字段形态静态判不出 ⇒ 保守拒（只加严）
        return "nested_object"
    if sub.get("type") == "array":
        # 递归：数组元素同样必须过本判定（缺 items / items 为空 ⇒ "untyped"）
        return _node_reason(sub.get("items"))
    return None


def _schema_reason(parameters: Any) -> str | None:
    """D-i″ 保守拒绝：返回 ``nested_object|untyped|composite|no_fields`` 或 ``None``（可上线）。"""
    try:
        schema = parameters.model_json_schema() if hasattr(parameters, "model_json_schema") else {}
    except Exception:  # noqa: BLE001
        return "untyped"
    props = schema.get("properties") or {}
    if not props:
        return "no_fields"
    for _name, sub in props.items():
        reason = _node_reason(sub)
        if reason is not None:
            return reason
    return None


def _make_live_handler(agent_tool: Any, contract_name: str, raw_name: str) -> Any:
    """把上游 ``AgentTool.execute`` 包成本仓 ``handler``（D-l / D-m）。"""

    async def handler(args: BaseModel) -> ToolObservation:
        payload = args.model_dump() if hasattr(args, "model_dump") else dict(args or {})
        try:
            # 上游签名：``execute(tool_call_id, args)``（见 runner ``tool.execute(tool_call_id, args_model)``）
            result = await asyncio.wait_for(
                agent_tool.execute("mcp-call", args),
                timeout=MCP_CALL_TIMEOUT,
            )
        except TimeoutError:
            return ToolObservation(text=f"MCP 工具 {raw_name} 调用超时。", ok=False)
        except Exception as exc:  # noqa: BLE001
            # 回归修复：固定形状 + 原文只进日志（与 tools/mcp.py 同一口径）
            log.warning("MCP 工具 %s 执行异常：%s", raw_name, exc)
            return ToolObservation(
                text=f"MCP 工具 {raw_name} 执行失败（{exc.__class__.__name__}）。", ok=False
            )

        details = getattr(result, "details", None)
        aborted = bool(getattr(details, "aborted", False)) if details is not None else False
        if aborted:
            return ToolObservation(text=f"MCP 工具 {raw_name} 被中止。", ok=False)
        if getattr(result, "is_error", None):
            texts = []
            for block in getattr(result, "content", None) or []:
                t = getattr(block, "text", "") or ""
                if t:
                    texts.append(t)
            reason = "\n".join(texts).strip() or "远端返回错误"
            # 回归修复：远端错误原文同样只进日志（它可能带服务端内部细节 / 堆栈）
            log.warning("MCP 工具 %s 返回错误：%s", raw_name, reason)
            return ToolObservation(
                text=f"MCP 工具 {raw_name} 执行失败（远端返回错误）。", ok=False
            )

        texts = []
        for block in getattr(result, "content", None) or []:
            t = getattr(block, "text", None)
            if t:
                texts.append(str(t))
        if not texts:
            return ToolObservation(
                text="远端返回了非文本内容（本轮不支持）",
                ok=True,
            )
        # 回归修复：成功文本出口同样裁（MCP_CALL_TIMEOUT 只限速不限大小）；isError 的
        # reason 只进日志（上面），固定短文案路径不裁。常量从 tools/mcp 导入，防两把尺漂移。
        return ToolObservation(text=clip("\n".join(texts), OBSERVATION_CLIP), materials=[])

    return handler


def _spec_from_agent_tool(
    agent_tool: Any,
    *,
    pack_name: str,
    slug: str,
    raw_name: str,
    reason_holder: list,
    dangerous: bool | None = None,
) -> ToolSpec | None:
    """上游 AgentTool → 本仓 ToolSpec；schema 超子集 ⇒ None + 结构化原因。

    ``dangerous``（``回归修复`` A3 腿）＝**上游已判好的**高危结论。``None`` = 调用方没判
    ⇒ 回落到名字 / 描述规则（**逐字等价改动前**，只加严不放宽）。为什么要有这个参数：判定
    必须发生在拿得到包内配置项 ``conf`` 的循环里（只有那里知道用户有没有**显式**写
    ``dangerous``）；留在这里再无条件判一次 = 名字规则二次叠加 ⇒ 显式 ``false`` 关不掉。
    """
    parameters = getattr(agent_tool, "parameters", None)
    reason = _schema_reason(parameters) if parameters is not None else "no_fields"
    if reason is not None:
        reason_holder.append({"tool": raw_name, "reason": reason})
        return None
    description = str(getattr(agent_tool, "description", "") or "")
    is_dangerous = (
        dangerous_by_default(raw_name, description) if dangerous is None else bool(dangerous)
    )
    if is_dangerous:
        reason_holder.append({"tool": raw_name, "reason": "dangerous"})
        return None
    contract = pack_tool_name(slug, raw_name)
    return ToolSpec(
        name=contract,
        description=f"{pack_name or slug} · {raw_name}：{description}",
        parameters_schema=parameters.model_json_schema(),
        handler=_make_live_handler(agent_tool, contract, raw_name),
        dangerous=False,
        parameters=parameters,
        needs_web=False,
        needs_confirm=False,
    )


def discover_tools(*, raw: str | None = None) -> DiscoveryOutcome:
    """同步入口：按许可过滤配置包并发现（生产侧由 ``stream_agent_turn`` 调 async 版）。

    **先判许可，再发现**（D-i）。返回 ``None`` 语义由调用方决定（零许可传给
    ``build_registry(mcp_specs=None)`` 走老分支）。
    """
    outcome = DiscoveryOutcome()
    allow = parse_allowlist(settings.MCP_ALLOW_SERVERS)
    egress = parse_allowlist(settings.MCP_ALLOW_EGRESS)
    if not allow:
        return outcome

    for item in _parse_servers_raw(raw):
        enabled = as_bool(item.get("enabled"), True)
        available = as_bool(item.get("available"), True)
        slug = _slugify(str(item.get("slug") or item.get("name") or ""))
        transport = str(item.get("transport") or "").strip().lower()
        name = str(item.get("name") or slug)
        tools_raw = item.get("tools") or []
        tool_names = {
            str(t.get("name") if isinstance(t, dict) else t).strip()
            for t in tools_raw
        }
        tool_names.discard("")

        if not enabled or not available or not slug:
            continue
        if slug not in allow:
            log.warning("MCP 包 %s 未在 MCP_ALLOW_SERVERS，跳过发现", slug)
            continue
        if transport == "http" and slug not in egress:
            log.warning("MCP 包 %s 为 http 传输但不在 MCP_ALLOW_EGRESS，跳过发现", slug)
            continue

        command = as_command(item.get("command"))
        url = str(item.get("url") or "").strip()

        if transport == "stdio":
            if not command:
                log.warning("MCP 包 %s 缺 command，不发现", slug)
                outcome.skipped.append({"pack": slug, "reason": "no_command"})
                continue
            try:
                result = asyncio.get_event_loop().run_until_complete(
                    asyncio.wait_for(
                        load_mcp_tools_stdio(str(command[0]), [str(c) for c in command[1:]], timeout=MCP_DISCOVERY_TIMEOUT),
                        timeout=MCP_DISCOVERY_TIMEOUT,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("MCP 包 %s 发现失败：%s", slug, exc)
                outcome.skipped.append({"pack": slug, "reason": "discover_failed", "transport": transport})
                continue
        elif transport == "http":
            if not url:
                log.warning("MCP 包 %s 缺 url，不发现", slug)
                outcome.skipped.append({"pack": slug, "reason": "no_url"})
                continue
            try:
                result = asyncio.get_event_loop().run_until_complete(
                    asyncio.wait_for(
                        load_mcp_tools_http(url, transport="streamable_http", timeout=MCP_DISCOVERY_TIMEOUT),
                        timeout=MCP_DISCOVERY_TIMEOUT,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("MCP 包 %s 发现失败：%s", slug, exc)
                outcome.skipped.append({"pack": slug, "reason": "discover_failed", "transport": transport})
                continue
        else:
            # sse / 未知：严格层不支持（D-j；不依赖 load_packs 的归一）
            log.warning("MCP 包 %s transport=%r 不支持，不发现", slug, transport)
            outcome.skipped.append({"pack": slug, "reason": "unsupported_transport", "transport": transport})
            continue

        for agent_tool in getattr(result, "tools", None) or []:
            raw_name = str(getattr(agent_tool, "name", "") or "").strip()
            if not raw_name or (tool_names and raw_name not in tool_names):
                continue
            # 工具级：配置 tools[] 白名单 + enabled + ¬dangerous（配置显式 dangerous 听配置）
            conf = {}
            for t in tools_raw:
                if isinstance(t, dict) and str(t.get("name") or "").strip() == raw_name:
                    conf = t
                    break
            description = str(getattr(agent_tool, "description", "") or "")
            if conf and not as_bool(conf.get("enabled"), True):
                continue
            is_dangerous = resolve_dangerous(conf, raw_name, description)
            if is_dangerous:
                outcome.skipped.append({"tool": raw_name, "reason": "dangerous"})
                continue
            spec = _spec_from_agent_tool(
                agent_tool,
                pack_name=name,
                slug=slug,
                raw_name=raw_name,
                reason_holder=outcome.skipped,
                dangerous=is_dangerous,
            )
            if spec is not None:
                outcome.specs.append(spec)
    return outcome


async def adiscover_tools(
    *, raw: str | None = None, budget_seconds: float | None = None
) -> list[ToolSpec] | None:
    """生产接线点：零许可 ⇒ **返回 None**（空列表 ≠ None，D-i′）。

    「本轮许可包数 ≥ 1」才返回非 None（哪怕最终 specs 为空，也表示做过发现）。

    ``budget_seconds``（R2-04）：这一轮**合计**还能花在「发现」上的秒数（``None`` = 不限）。
    它是 keyword-only 且**默认 ``None``** ⇒ 不传时逐字等价改动前（``MCP_DISCOVERY_TIMEOUT``
    仍是唯一的时间闸门）。为什么由**调用方**给：逐包 8s 是包内的 ``asyncio.wait_for``，
    它**不知道**整轮还剩多少预算；而整轮预算的起点在 ``runner.stream_agent_turn``
    （且**早于**本函数，见 R2-03 的 D-回归检查）。
    """
    allow = parse_allowlist(settings.MCP_ALLOW_SERVERS)
    if not allow:
        return None
    outcome = await _adiscover(raw=raw, budget_seconds=budget_seconds)
    return outcome.specs


async def _adiscover(
    *, raw: str | None = None, budget_seconds: float | None = None
) -> DiscoveryOutcome:
    """async 发现（loader 本身是 async 友好的 wait 包装；这里用 wait_for）。

    ``budget_seconds``（R2-04）是**合计**上限：逐包 8s 的闸门（``MCP_DISCOVERY_TIMEOUT``）
    **不动**，但每包实际等的上限是 ``min(8s, 剩余预算)``，且**剩余 ≤ 0 时后面的包不再发现**。
    为什么必须做（本笔要治的现状）：逐包发现失败只 ``log.warning + continue``、循环**串行**
    （见下面 per-pack 循环）⇒ 最坏 ≈ 8s × 许可包数，**调用方取消不掉**。
    """
    outcome = DiscoveryOutcome()
    allow = parse_allowlist(settings.MCP_ALLOW_SERVERS)
    egress = parse_allowlist(settings.MCP_ALLOW_EGRESS)
    if not allow:
        return outcome

    #: 合计预算的到期时刻（``None`` = 不限 ⇒ 逐字等价改动前）。
    #: 非法值（非数值）按「不限」处理：**不许**因为一个坏参数把发现整段关掉。
    expires_at: float | None = None
    if budget_seconds is not None:
        try:
            expires_at = time.monotonic() + max(float(budget_seconds), 0.0)
        except (TypeError, ValueError):
            expires_at = None

    for item in _parse_servers_raw(raw):
        enabled = as_bool(item.get("enabled"), True)
        available = as_bool(item.get("available"), True)
        slug = _slugify(str(item.get("slug") or item.get("name") or ""))
        transport = str(item.get("transport") or "").strip().lower()
        name = str(item.get("name") or slug)
        tools_raw = item.get("tools") or []
        tool_names = {
            str(t.get("name") if isinstance(t, dict) else t).strip()
            for t in tools_raw
        }
        tool_names.discard("")
        if not enabled or not available or not slug:
            continue
        if slug not in allow:
            log.warning("MCP 包 %s 未在 MCP_ALLOW_SERVERS，跳过发现", slug)
            continue
        if transport == "http" and slug not in egress:
            log.warning("MCP 包 %s 为 http 传输但不在 MCP_ALLOW_EGRESS，跳过发现", slug)
            continue
        command = as_command(item.get("command"))
        url = str(item.get("url") or "").strip()
        # R2-04：本包能花的时间 = min(逐包上限, **合计**剩余)。
        # 剩余 ≤ 0 ⇒ 后面的包**不再发现**（如实 warning 后 break —— 不静默、也不继续白等）；
        # ⚠️ 逐包上限 ``MCP_DISCOVERY_TIMEOUT`` **本身不动**，这里只是把它再夹一次。
        if expires_at is not None:
            left = expires_at - time.monotonic()
            if left <= 0:
                log.warning(
                    "MCP 发现合计预算已用尽（%.3fs），剩余包不再发现：%s",
                    float(budget_seconds or 0.0),
                    slug,
                )
                break
            per_pack = min(MCP_DISCOVERY_TIMEOUT, left)
        else:
            per_pack = MCP_DISCOVERY_TIMEOUT
        try:
            if transport == "stdio":
                if not command:
                    log.warning("MCP 包 %s 缺 command，不发现", slug)
                    # 🔴 `回归修复`：**跳过原因必须记账**（形状与同步腿 `discover_tools` 逐字同：
                    # `{"pack": slug, "reason": "no_command"}`，无 transport 键）。
                    # 生产 async 腿只 warning 不 append ⇒ `DiscoveryOutcome.skipped` 恒缺这条
                    # ⇒ 调用方/审计看不到「这个包为什么没被发现」。
                    outcome.skipped.append({"pack": slug, "reason": "no_command"})
                    continue
                result = await asyncio.wait_for(
                    load_mcp_tools_stdio(
                        str(command[0]), [str(c) for c in command[1:]], timeout=per_pack
                    ),
                    timeout=per_pack,
                )
            elif transport == "http":
                if not url:
                    log.warning("MCP 包 %s 缺 url，不发现", slug)
                    # 🔴 `回归修复`：同上（与同步腿逐字同形状，无 transport 键）。
                    outcome.skipped.append({"pack": slug, "reason": "no_url"})
                    continue
                result = await asyncio.wait_for(
                    load_mcp_tools_http(
                        url, transport="streamable_http", timeout=per_pack
                    ),
                    timeout=per_pack,
                )
            else:
                log.warning("MCP 包 %s transport=%r 不支持，不发现", slug, transport)
                outcome.skipped.append({"pack": slug, "reason": "unsupported_transport", "transport": transport})
                continue
        except Exception as exc:  # noqa: BLE001
            log.warning("MCP 包 %s 发现失败：%s", slug, exc)
            # 🔴 `回归修复`：**发现失败也要记账**（与同步腿逐字同形状 —— 含 transport 键）。
            # ⚠️ 捕的仍是 `Exception`：`CancelledError` 继承 `BaseException`，**不得**改捕
            # `BaseException`（否则取消语义被吞）；既有 warning 文案逐字不动。
            outcome.skipped.append(
                {"pack": slug, "reason": "discover_failed", "transport": transport}
            )
            continue

        for agent_tool in getattr(result, "tools", None) or []:
            raw_name = str(getattr(agent_tool, "name", "") or "").strip()
            if not raw_name or (tool_names and raw_name not in tool_names):
                continue
            conf = {}
            for t in tools_raw:
                if isinstance(t, dict) and str(t.get("name") or "").strip() == raw_name:
                    conf = t
                    break
            description = str(getattr(agent_tool, "description", "") or "")
            if conf and not as_bool(conf.get("enabled"), True):
                continue
            is_dangerous = resolve_dangerous(conf, raw_name, description)
            if is_dangerous:
                outcome.skipped.append({"tool": raw_name, "reason": "dangerous"})
                continue
            spec = _spec_from_agent_tool(
                agent_tool,
                pack_name=name,
                slug=slug,
                raw_name=raw_name,
                reason_holder=outcome.skipped,
                dangerous=is_dangerous,
            )
            if spec is not None:
                outcome.specs.append(spec)
    return outcome


__all__ = [
    "DiscoveryOutcome",
    "MCP_CALL_TIMEOUT",
    "MCP_DISCOVERY_TIMEOUT",
    "adiscover_tools",
    "discover_tools",
    "load_mcp_tools_http",
    "load_mcp_tools_stdio",
    "parse_allowlist",
]
