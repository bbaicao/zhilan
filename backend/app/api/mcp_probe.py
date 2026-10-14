"""极简 MCP 客户端：只做 ``initialize`` + ``tools/list``（任务清单 35；阶段 4 · 条目 `4.5.3`）。

服务的端点只有一个：``POST /api/skills/mcp/probe`` —— 「连上 server 并列出它暴露的工具名」。
覆盖主流 stdio 与 HTTP JSON-RPC；**不调用工具**（高危执行留给用户确认后的后续轮次）。
老基线对应模块：``<legacy-repo>\\backend\\app\\rag\\mcp_client.py``（本文件是它的**只读移植**）。

**为什么 `call_tool_*` 没有一起搬过来**：真调用通道的注入点是
``app/agent/tools/mcp.py::build_specs(call_tool=...)``，调用方在 ``app/agent/runner.py``
（不在本条目的文件白名单内）⇒ ``call_tool_hook`` 继续给那句**可读中文的诚实回答**
（「尚未绑定传输通道」）。本模块只交付**探测**：它真的起进程 / 发 HTTP，失败一律给可读中文，
**不抛 500**（探测是给设置页用的，炸了等于设置页打不开）。

## HTTP 出网守卫（专项升级 · `回归修复`）

``url`` 是**用户在设置页里填的**（``POST /api/skills/mcp/probe`` 的 ``payload.url``）
⇒ HTTP 通道是一条**用户可控的出网面**。改前它**直接** ``httpx.AsyncClient(...).post(url)``，
判定与连接之间**没有任何守卫**：填 ``http://169.254.169.254/…`` 就真发出去（审计实测
``REACHED_HTTPX`` / ``event_hooks=None``）。

本笔在**出网之前**接上 ``app/core/net_guard.py``（081-A 的薄壳，判定本体仍是 ``ssrf.py`` 一份）
⇒ ``list_tools_http`` 的**第一件事**就是过守卫。

⚠️ **两条不越界的纪律**（改这个文件之前先读完）：

1. **只调薄壳的公开函数**（``assert_outbound_url_safe`` / ``OutboundBlocked``）——
   判定本体与地址黑名单**只在** ``ssrf.py`` 一份，本模块**不重判**（不抄符号名、不写地址字面量）。
2. **stdlib 通道（``list_tools_stdio``）一行都不改**：它走的是**子进程**（另一条独立的风险面，
   不经过 HTTP 客户端、也不经过本守卫），把守卫套到它身上既无着力点、又会改掉既有语义
   ⇒ 那是**另一条**待立项的面，本卡范围外。

🔴 **一条必须逐字登记的限制（不得含糊）**：本面**没有 pinning** —— 守卫做的是**判定**
（解析一次、判白名单），而 ``httpx`` 连接时**再独立解析一次**，两次解析之间理论上可被 DNS
重绑定插空。⇒ **TOCTOU 未被消除**。要消除它需要把守卫返回的那个已校验地址**固定到连接上**
（同 ``app/agent/web/pinning.py`` 的做法），本卡**不做**（登记为已知限制，见回执）。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from app.api.skills_spec import is_dangerous_tool
from app.core import net_guard

#: MCP 协议版本（老基线同：2024-11-05）。
PROTOCOL_VERSION = "2024-11-05"
DEFAULT_TIMEOUT = 8.0
CLIENT_INFO = {"name": "zhilan", "version": "0.1.6"}


#: 出网守卫的**唯一调用点**（`回归修复`）。
#:
#: ⚠️ 写成模块级名字**不是为了包例外**，而是两个用途（照 `net_guard` 自己的注释同款）：
#:   ① 让判据能**数引用点**——「本模块判定入口只此一处」是可结构判的；
#:   ② 让变异 `M-081-C1`（去掉守卫调用）有一个**可命名的锚点**，而不是散在调用链里。
#: 它是**公开**名字：接线方若只想调，不要改它。
assert_outbound_url_safe = net_guard.assert_outbound_url_safe


class McpError(RuntimeError):
    """连接失败 / 协议不对 / 超时。"""


def _encode(message: dict[str, Any]) -> bytes:
    return (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")


def _parse_tools(result: dict[str, Any]) -> list[dict[str, Any]]:
    """`tools/list` 的 `result` → `McpTool` 四键（`name`/`description`/`dangerous`/`enabled`）。"""
    tools_raw = result.get("tools")
    if not isinstance(tools_raw, list):
        return []
    tools: list[dict[str, Any]] = []
    for item in tools_raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        description = str(item.get("description") or "").strip()
        tools.append(
            {
                "name": name,
                "description": description,
                "dangerous": is_dangerous_tool(name, description),
                "enabled": True,
            }
        )
    return tools


async def _read_json_line(reader: asyncio.StreamReader, timeout: float) -> dict[str, Any]:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise McpError("MCP server 响应超时") from exc
    if not line:
        raise McpError("MCP server 已退出，没有返回数据")
    text = line.decode("utf-8", errors="replace").strip()
    if not text:
        raise McpError("MCP server 返回了空行")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise McpError(f"MCP server 返回的不是 JSON：{text[:120]}") from exc
    if not isinstance(data, dict):
        raise McpError("MCP server 返回的 JSON 不是对象")
    return data


async def list_tools_stdio(
    command: list[str],
    env: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """起子进程、握手、列工具、**用完即 kill**（一次性会话，不复用）。"""
    if not command:
        raise McpError("stdio 需要启动命令")
    process_env = os.environ.copy()
    if env:
        process_env.update({str(k): str(v) for k, v in env.items()})
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # 🔴 回归修复：stderr 必须是 DEVNULL —— 本文件对 stderr **零读取点**
            # （finally 只 kill+wait），用 PIPE 的话，唠叨的 server 写满管道缓冲
            # （约 64KB）后自己阻塞在 stderr 写上，连 initialize 都回不出来
            # ⇒ 8 秒误报「MCP server 响应超时」。stderr 产物无人消费 ⇒ 诊断面零损失
            # （改并发排空可保可诊断性，那是另一案，登记在缺陷账本不在此做）。
            stderr=asyncio.subprocess.DEVNULL,
            env=process_env,
        )
    except FileNotFoundError as exc:
        raise McpError(f"找不到可执行文件：{command[0]}") from exc
    except OSError as exc:
        raise McpError(f"无法启动 MCP server：{exc}") from exc

    assert process.stdin is not None and process.stdout is not None
    try:
        process.stdin.write(
            _encode(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": CLIENT_INFO,
                    },
                }
            )
        )
        await process.stdin.drain()
        init = await _read_json_line(process.stdout, timeout)
        if "error" in init:
            raise McpError(f"initialize 失败：{init['error']}")
        process.stdin.write(_encode({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        await process.stdin.drain()
        process.stdin.write(_encode({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}))
        await process.stdin.drain()
        listed = await _read_json_line(process.stdout, timeout)
    finally:
        try:
            process.kill()
        except ProcessLookupError:  # pragma: no cover - 进程已自己退出
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except asyncio.TimeoutError:  # pragma: no cover - 杀不掉的极端情况
            pass

    if "error" in listed:
        raise McpError(f"tools/list 失败：{listed['error']}")
    return _parse_tools(listed.get("result") or {})


async def list_tools_http(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """HTTP JSON-RPC 三连 POST：`initialize` → `notifications/initialized` → `tools/list`。

    🔴 **出网之前先过守卫**（`回归修复`）：``url`` 是用户填的，这是一条用户可控的出网面。
    判定由 ``app/core/net_guard.py`` 复用 ``ssrf.py`` 的唯一一份本体给出，本函数**不重判**。

    守卫拒绝 ⇒ 转成 :class:`McpError`（继承 ``RuntimeError``）⇒ 由 :func:`probe_server`
    收成 ``(tools=[], error=可读中文)`` ⇒ **设置页拿到的是「探测失败」而不是 500**
    （本模块的既有契约：探测失败是结果，不是错误）。

    ⚠️ ``httpx`` 仍**只**在真正出网的那条路径上 import（守卫拒绝时压根不 import、不构造客户端）
    —— 这正是 D-03「拒绝路径零出网」的结构性保证。
    """
    if not url:
        raise McpError("http 需要接口地址")
    # 🔴 守卫必须在**任何出网动作之前**（含 import httpx / 构造 client）。
    # 变异 `M-081-C2`（把这段挪到 post 之后）⇒ D-03 当场红（拒绝路径也会构造 client）。
    try:
        assert_outbound_url_safe(url, purpose="MCP 服务器探测")
    except net_guard.OutboundBlocked as exc:
        raise McpError(str(exc)) from exc

    import httpx
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            res = await client.post(url, json=payload, headers=headers or {})
            if res.status_code >= 400:
                raise McpError(f"MCP HTTP 初始化失败（{res.status_code}）")
            try:
                init = res.json()
            except ValueError as exc:
                raise McpError("MCP HTTP 返回内容无法解析") from exc
            if isinstance(init, dict) and "error" in init:
                raise McpError(f"initialize 失败：{init['error']}")
            await client.post(
                url,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=headers or {},
            )
            listed_res = await client.post(
                url,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                headers=headers or {},
            )
            if listed_res.status_code >= 400:
                raise McpError(f"MCP HTTP tools/list 失败（{listed_res.status_code}）")
            listed = listed_res.json()
    except McpError:
        raise
    except httpx.TimeoutException as exc:
        raise McpError("MCP HTTP 请求超时") from exc
    except httpx.HTTPError as exc:
        raise McpError(f"无法连接 MCP server：{exc}") from exc
    except ValueError as exc:  # `tools/list` 响应不是 JSON
        raise McpError("MCP HTTP tools/list 返回内容无法解析") from exc

    if not isinstance(listed, dict):
        raise McpError("MCP HTTP tools/list 返回格式不正确")
    if "error" in listed:
        raise McpError(f"tools/list 失败：{listed['error']}")
    return _parse_tools(listed.get("result") or {})


async def probe_server(
    transport: str,
    command: list[str],
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[list[dict[str, Any]], str]:
    """连接并列出工具；返回 ``(tools, error_message)``。成功时 error 为空串。

    **任何异常都收成 error 字符串**（包括 `BaseException` 之外的意外类型）——
    探测是设置页的按钮，它不该把整页打成 500。
    """
    try:
        channel = (transport or "stdio").lower()
        if channel == "http":
            tools = await list_tools_http(url, timeout=timeout)
        elif channel == "stdio":
            tools = await list_tools_stdio(command, timeout=timeout)
        else:
            # ``回归修复``：只认 http / stdio。改前这里是「不是 http 就走 stdio」，
            # 于是 ``sse``（skill 契约里**真实存在**的取值）会**误起一个 stdio 子进程**，
            # 既碰了不该碰的通道，又给出一句误导性的失败原因。
            # ⚠️ 空串仍按既有的「默认 stdio」语义（设置页既有行为不动）。
            return [], f"暂不支持的传输通道：{channel}（当前只支持 http / stdio）"
        return tools, ""
    except McpError as exc:
        return [], str(exc)
    except Exception as exc:  # noqa: BLE001 —— probe 不允许炸设置页
        return [], f"连接失败：{exc}"


__all__ = [
    "DEFAULT_TIMEOUT",
    "PROTOCOL_VERSION",
    "McpError",
    "list_tools_http",
    "list_tools_stdio",
    "probe_server",
]
