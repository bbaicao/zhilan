"""MCP 工具：把「一个 MCP server = 一个包」展开成契约名 ``mcp:{slug}:{tool}`` 的 ToolSpec。

**为什么不与其它三个工具同形**：``knowledge_search`` / ``web_search`` / ``fetch_page`` 是一工具
一模块、一模块一 ``ToolSpec``；MCP 是**一包一组工具**（一个包 = 一个 MCP server，包里每条工具
落一条 ToolSpec），所以本模块导出的是 :func:`build_specs` 而不是单条 ``spec``。契约名因此是
``mcp:{slug}:{tool}``——**冻结**，不许写成 ``mcp_tool_x`` 或裸工具名（冻结文档 §一 4.1.2 / §六）。

**4.1 的边界（`_work-4.1接口冻结.md` §十一.4）**：本条目只做**命名 / 注册 / dangerous 过滤 /
调用钩子**；真 transport 绑定随技能域条目（4.5.3）落地——v2 目前没有任何 MCP 配置入口，
提前绑 = 不可测的死代码。所以 :func:`call_tool_hook` 默认给的是**可读中文的诚实回答**
（「尚未绑定传输通道」），既不抛异常也不假装成功；真实现由后续条目通过 ``build_specs(call_tool=...)``
这个注入点替换，注入点之外本模块**不碰网络、不起进程**（测试里同样如此）。

**安全口径 I6（危险工具零暴露）**：``dangerous`` 的工具在 :func:`build_specs` 里**直接被丢掉**，
连 ``ToolSpec`` 都不生成——不是靠上层 ``for_model()`` 兜，而是根本不存在（老基线
``agent._mcp_tools`` 同口径：高危工具不进自动执行清单）。
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.agent.registry import ToolSpec
from app.agent.types import ToolObservation, clip
from app.core.config import settings

logger = logging.getLogger(__name__)

#: 契约名前缀（冻结：工具名形如 ``mcp:{slug}:{tool}``）。
TOOL_PREFIX = "mcp:"

#: 回归修复：MCP 返回文本进决策循环上下文前的裁剪上限（照 ``fetch_page`` 观察
#: 的 1200 尺度；MCP materials 恒空 ⇒ 4000 尺度无落点）。「stdio/http 真发现路径」
#: （``app/agent/mcp_transport.py``）从本模块导入**同一份**常量，防两把尺漂移；
#: ⚠️ 与 runner 侧 ``clip_tool_context_text``（4000/12000）是两把不同的尺，不许统一。
OBSERVATION_CLIP = 1200

#: 高危动词粗规则：**逐字沿用老基线** ``app/rag/skills.py::_DANGEROUS_TOOL_RE``。
#: 只读类工具默认不高危；写 / 删 / 执行 / 外发类动词命中即判高危。
_DANGEROUS_RE = re.compile(
    r"(delete|remove|drop|write|update|create|insert|exec|shell|command|run|rm|destroy|modify|send|post|put|patch|upload)",
    re.IGNORECASE,
)

# slug 归一：与老基线 ``skills.slugify`` 同口径（只留 [a-z0-9_-]）。
_SLUG_INVALID_RE = re.compile(r"[^a-z0-9_-]+")
_SLUG_MAX_LEN = 64
_SLUG_FALLBACK = "skill"

#: 允许的传输通道（冻结文档 §六：stdio | http | sse）。
TRANSPORTS = ("stdio", "http", "sse")


@dataclass(frozen=True)
class McpTool:
    """包内一条工具的声明（``name`` 是**包内原始名**，不带 ``mcp:`` 前缀）。"""

    name: str
    description: str = ""
    dangerous: bool = False
    enabled: bool = True


@dataclass(frozen=True)
class McpPack:
    """一个 MCP server 的配置快照（一个包 = 一组工具）。

    ``slug`` 是名字空间，出现在契约名里，故它必须是**稳定且可读**的（:func:`_slugify`）。
    ``available`` 与 ``enabled`` 分开：前者是「配置本身能不能用」，后者是「用户有没有关掉」，
    两者任一为假都整包跳过（冻结文档：包级过滤）。``tools`` 用 tuple 是为了让快照不可变——
    装配时（``build_specs``）的工具清单不该被下游偷偷改。
    """

    slug: str
    name: str = ""
    transport: str = "stdio"
    command: tuple[str, ...] = ()
    url: str = ""
    enabled: bool = True
    available: bool = True
    tools: tuple[McpTool, ...] = ()


class McpToolArgs(BaseModel):
    """MCP 工具的参数模型：**只有一个 extra="allow"**。

    为什么不按包内工具声明动态造字段：MCP 工具的入参 schema 由远端 server 在握手时给出，而 4.1
    不连真 server（见模块 docstring），照本地声明造字段只能**造假 schema**（比没有更糟：模型会
    当真），还会让 ``ToolSpec.parameters`` 变成每条工具一个匿名类、转 CubeLoop AgentTool 时更难复用。
    统一一个「任意 JSON 对象都收」的模型：``extra="allow"`` 让任意键通过校验，参数原样变成 dict
    交给注入的 ``call_tool``；真绑定（4.5.3）落地时再按 server 的声明收紧字段。
    """

    model_config = ConfigDict(extra="allow")


#: 注入点类型：真 transport 由它落地（4.5.3）。按位置收 5 参，与 :func:`call_tool_hook` 同序。
CallTool = Callable[..., Awaitable[str]]


def pack_tool_name(pack_slug: str, tool_name: str) -> str:
    """契约名 ``mcp:{slug}:{tool}``（冻结，勿改）。"""
    return f"{TOOL_PREFIX}{pack_slug}:{tool_name}"


def dangerous_by_default(tool_name: str, description: str = "") -> bool:
    """名字**或**描述命中高危动词 ⇒ 判 dangerous（正则逐字沿用老基线）。

    老基线 ``is_dangerous_tool`` 只看名字；本仓连描述一起看，理由：MCP 工具名常是包的私事
    （``call`` / ``invoke`` 这类空名），**真语义写在描述里**——只按名字判会让一个
    「删除服务器上所有文件」的工具看起来无害。判宽了最坏是少一条工具（可用性损失），
    判窄了是危险工具进了模型清单（I6 违约），显然后者不可接受。
    """
    return bool(_DANGEROUS_RE.search(tool_name or "") or _DANGEROUS_RE.search(description or ""))


def call_tool_hook(transport: str, command: tuple[str, ...], url: str, tool_name: str, args: dict) -> str:
    """默认调用钩子：**没绑真 transport 时的诚实回答**。

    契约要求「可读中文、不抛异常、不假装成功」——因为 4.1 之后 MCP 工具确实进得了模型清单
    （只要用户写了 ``MCP_SERVERS``），模型真调了就必须拿到一句人话，而不是一个空字符串或
    编造的返回值。真实现由后续条目替换本函数（注入 ``build_specs(call_tool=...)``）。

    ``tool_name`` 是**包内原始名**（真 server 只认自己的工具名）；契约名只在 ``ToolSpec.name`` 上。
    """
    return f"MCP 工具 {tool_name} 尚未绑定传输通道（真正的连接归技能域条目）。"


def build_specs(packs: Iterable[McpPack], *, call_tool: CallTool | None = None) -> list[ToolSpec]:
    """一包一组 ``ToolSpec``；过滤规则**逐条沿用老基线**（``agent._mcp_tools``）。

    - 包级：``enabled=False`` / ``available=False`` / ``slug`` 为空 → 整包跳过；
    - 工具级：``name`` 为空 / ``enabled=False`` / ``dangerous`` → 跳过（**危险工具绝不进清单，I6**）。

    产出的 spec 恒为 ``dangerous=False``（危险的在上面就被丢了）、``needs_web=False``
    （MCP 的联网与否是 server 的事，不是本仓 ``WEB_ENABLE`` 管得着的范围），
    ``parameters`` 指向 :class:`McpToolArgs`，``parameters_schema`` 是它的 JSON schema。

    除了 :class:`McpPack` 快照，也收**原始 JSON 配置条目**（装配侧 ``build_registry(mcp_packs=...)``
    的形参类型是裸 ``list``，直接塞解析结果是很自然的用法）：归一交给 :func:`_as_pack`，
    认不出的条目跳过而不是抛——注册发生在**每一轮请求**的路径上，这里炸穿等于整轮对话挂掉（I5）。
    """
    specs: list[ToolSpec] = []
    for item in packs:
        pack = _as_pack(item)
        if pack is None:
            continue
        if not pack.enabled or not pack.available or not str(pack.slug or "").strip():
            continue
        slug = str(pack.slug)
        for tool in pack.tools:
            tool_name = str(tool.name or "").strip()
            if not tool_name or not tool.enabled or tool.dangerous:
                continue
            specs.append(_spec_for(pack, slug, tool_name, tool.description, call_tool))
    return specs


def load_packs(*, raw: str | None = None) -> list[McpPack]:
    """从 ``settings.MCP_SERVERS``（单行 JSON 数组字符串）解析包清单。

    **坏配置绝不抛异常**：``.env`` 是人手改得动的文件，一个少写的括号不该让整个后端起不来，
    故「空 / 不是 JSON / 顶层不是数组」一律按**「没有 MCP 工具」**处理并记一条 warning
    （空字符串是正常状态，不记）。非 dict 的数组元素同样丢掉。

    每个包的 slug 走 :func:`_slugify`（与老基线 ``skills.slugify`` 同口径）；包内工具的
    ``dangerous`` 缺省由 :func:`dangerous_by_default` 按名字 + 描述判定（配置里显式写了就听配置的）。
    """
    text = str(settings.MCP_SERVERS if raw is None else raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError as exc:
        logger.warning("MCP_SERVERS 不是合法 JSON，按「没有 MCP 工具」处理：%s", exc)
        return []
    if not isinstance(data, list):
        logger.warning("MCP_SERVERS 顶层不是 JSON 数组，按「没有 MCP 工具」处理")
        return []
    return [_pack_from_json(item) for item in data if isinstance(item, dict)]


# --- 内部实现 ----------------------------------------------------------------


def _slugify(raw: str) -> str:
    """slug 归一：**逐字沿用老基线** ``skills.slugify``（只留 ``[a-z0-9_-]``、数字开头加 ``s-``、截 64）。

    为什么连「空 → ``skill``」这个兜底也照搬：slug 会直接进工具名，为空会让契约名退化成
    ``mcp::tool``（两个冒号连着，人读不出名字空间，也无法按前缀区分包）。
    """
    text = (raw or "").strip().lower()
    text = _SLUG_INVALID_RE.sub("-", text).strip("-")
    if not text:
        return _SLUG_FALLBACK
    if text[0].isdigit():
        text = f"s-{text}"
    return text[:_SLUG_MAX_LEN]


def _as_bool(value: Any, default: bool = True) -> bool:
    """手写 JSON 里的布尔：认 bool / 0-1 / ``"true|false|yes|no|on|off"``，认不出用 ``default``。

    为什么不像老基线那样直接 ``bool(...)``：``bool("false") is True``——配置里写字符串
    ``"false"`` 会把「关掉这个工具」变成「开着」。这里按可读意图解析。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off"):
        return False
    return default


def _as_command(value: Any) -> tuple[str, ...]:
    """启动命令：字符串按空白切分（老基线同口径），列表逐项转字符串并去掉空项。"""
    if isinstance(value, str):
        parts: list[Any] = value.split()
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        return ()
    return tuple(str(part).strip() for part in parts if str(part).strip())


#: 公开别名（**只增**）：发现层（``app/agent/mcp_transport.py``）复用本模块**唯一一份**
#: 配置解析语义，禁止在别处再复制第三份实现（收口包 D-C4）。私有名保留是为了不动既有调用点。
as_bool = _as_bool
as_command = _as_command


def resolve_dangerous(conf: Mapping[str, Any] | None, tool_name: str, description: str = "") -> bool:
    """这条工具是否判高危：**显式声明优先**（:func:`_tool_from_json` 同口径，``回归修复``）。

    配置里**显式写了** ``dangerous`` ⇒ 听配置（走 :func:`_as_bool`，故 ``"false"`` 也认）；
    **没写**才回落到 :func:`dangerous_by_default` 的名字 + 描述正则。

    为什么要做成**唯一一份**：发现层与注入面（:func:`build_specs`）都会对同一份
    ``MCP_SERVERS`` 判这件事；两处各写一遍「或」表达式就会漂移成「配置写 ``false`` 仍被
    名字规则叠加覆盖」（显式声明形同虚设）。
    """
    if conf is not None and "dangerous" in conf:
        return _as_bool(conf.get("dangerous"), False)
    return dangerous_by_default(tool_name, description)


def _tool_from_json(item: Any) -> McpTool | None:
    """一条工具声明；裸字符串也认（老基线 ``"tools": ["read_file"]`` 写法）。"""
    if isinstance(item, str):
        tool_name = item.strip()
        if not tool_name:
            return None
        return McpTool(name=tool_name, dangerous=dangerous_by_default(tool_name))
    if not isinstance(item, Mapping):
        return None
    tool_name = str(item.get("name") or "").strip()
    if not tool_name:
        return None
    description = str(item.get("description") or "").strip()
    if "dangerous" in item:
        # 显式声明优先（老基线同口径）：调用方比正则更清楚这条工具干了什么。
        dangerous = _as_bool(item.get("dangerous"), False)
    else:
        dangerous = dangerous_by_default(tool_name, description)
    return McpTool(
        name=tool_name,
        description=description,
        dangerous=dangerous,
        enabled=_as_bool(item.get("enabled"), True),
    )


def _pack_from_json(item: Mapping[str, Any]) -> McpPack:
    """一条包配置 → :class:`McpPack`（缺项一律走 dataclass 默认值）。"""
    name = str(item.get("name") or "").strip()
    transport = str(item.get("transport") or "stdio").strip().lower()
    if transport not in TRANSPORTS:
        # 未知通道按 stdio 归一（老基线 normalize_mcp_pack 同口径）：4.1 反正绑不了真通道，
        # 但一个越界的字符串不该留在快照里让下游「看起来支持」。
        transport = "stdio"
    tools_raw = item.get("tools")
    tools: list[McpTool] = []
    if isinstance(tools_raw, list):
        for raw_tool in tools_raw:
            tool = _tool_from_json(raw_tool)
            if tool is not None:
                tools.append(tool)
    return McpPack(
        # slug 缺省回落展示名（老基线 ``slugify(item.get("slug") or name)`` 同口径）：用户手写
        # 配置时常常只填 name，硬要两个都填只会逼人写重复内容。
        slug=_slugify(str(item.get("slug") or name or "")),
        name=name,
        transport=transport,
        command=_as_command(item.get("command")),
        url=str(item.get("url") or "").strip(),
        enabled=_as_bool(item.get("enabled"), True),
        # available 只认显式值：4.1 不绑 transport，「stdio 没写 command 就算不可用」是老基线的
        # **界面**口径，搬到这里只会把手工写的包静默丢掉——静默少一条工具比多一条更难排查。
        available=_as_bool(item.get("available"), True),
        tools=tuple(tools),
    )


def _as_pack(item: Any) -> McpPack | None:
    """包快照 or 一条原始 JSON 配置：都收（认不出的条目返回 ``None``，由调用方跳过）。"""
    if isinstance(item, McpPack):
        return item
    if isinstance(item, Mapping):
        return _pack_from_json(item)
    return None


def _spec_for(
    pack: McpPack,
    slug: str,
    tool_name: str,
    tool_description: str,
    call_tool: CallTool | None,
) -> ToolSpec:
    """装配一条 MCP 工具的 ``ToolSpec``（描述口径：老基线 ``agent._mcp_tool`` 逐字同形）。"""
    return ToolSpec(
        name=pack_tool_name(slug, tool_name),
        # 描述措辞老基线逐字同形：`{展示名 or slug} · {工具名}：{工具描述}`
        description=f"{pack.name or slug} · {tool_name}：{tool_description or ''}",
        parameters_schema=McpToolArgs.model_json_schema(),
        handler=_make_handler(pack, slug, tool_name, call_tool),
        dangerous=False,
        parameters=McpToolArgs,
        needs_web=False,
        needs_confirm=False,
    )


def _make_handler(
    pack: McpPack,
    slug: str,
    tool_name: str,
    call_tool: CallTool | None,
) -> Callable[[McpToolArgs], Awaitable[ToolObservation]]:
    """把「包 + 工具名 + 注入的传输」闭合进一个 ``async def handler(args)``。

    闭包变量全部在**构造时**取值（不读包的后续状态），这样多个工具之间不会互相串味
    （老基线用默认参数固定闭包变量，是同一个理由）。
    """
    transport = pack.transport
    command = tuple(pack.command or ())
    url = pack.url
    resolved = call_tool or call_tool_hook
    # 默认钩子 = 真通道尚未绑定：这种情况回 ok=False，别让「没通道」看起来像成功
    # （``types.py``：ok=False 的语义就是「没拿到有用结果——工具抛错 / 未配置 / 参数缺失」）。
    unbound = resolved is call_tool_hook
    contract_name = pack_tool_name(slug, tool_name)

    async def handler(args: McpToolArgs) -> ToolObservation:
        """调用 MCP 工具；**任何异常都转成 ok=False 的观察**（I5：绝不炸穿 Agent 循环）。"""
        try:
            payload = _args_payload(args)
            result = resolved(transport, command, url, tool_name, payload)
            if inspect.isawaitable(result):
                result = await result
            # 回归修复：只有成功文本出口裁（MCP_CALL_TIMEOUT 只限速不限大小）；异常 /
            # 固定短文案路径（下面 except 的 detail）本来就是固定形状，不裁。
            text = "" if result is None else clip(str(result), OBSERVATION_CLIP)
        except Exception as exc:  # noqa: BLE001 —— 工具失败不终止循环，观察回给模型自纠（老基线口径）
            # 回归修复：固定形状 + 原文**只进日志**；`warnings` 同样不得携带原文（K-C7）
            logger.warning("工具 %s 执行异常：%s", contract_name, exc)
            detail = f"MCP 工具 {contract_name} 执行失败（{exc.__class__.__name__}）。"
            return ToolObservation(text=detail, ok=False, warnings=[detail])
        # 材料恒空（冻结文档 §六）：MCP 的返回文本无法对应本仓的 chunks 表，故**不可作为引用来源**。
        return ToolObservation(text=text, ok=not unbound)

    return handler


def _args_payload(args: Any) -> dict[str, Any]:
    """参数模型 → 普通 dict（MCP 的 args 就是任意 JSON 对象；已经是 dict 的原样用）。"""
    dump = getattr(args, "model_dump", None)
    if callable(dump):
        data = dump()
        return dict(data) if isinstance(data, Mapping) else {}
    if isinstance(args, Mapping):
        return dict(args)
    return {}


__all__ = [
    "CallTool",
    "McpPack",
    "McpTool",
    "McpToolArgs",
    "OBSERVATION_CLIP",
    "TOOL_PREFIX",
    "TRANSPORTS",
    "as_bool",
    "as_command",
    "build_specs",
    "call_tool_hook",
    "dangerous_by_default",
    "load_packs",
    "pack_tool_name",
    "resolve_dangerous",
]
