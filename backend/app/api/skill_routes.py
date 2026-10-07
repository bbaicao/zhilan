"""技能 / MCP 配置接口（任务清单 35；阶段 4 · 条目 `4.5.3` skills 域；独立挂载）。

与设置页同一 IA：只增本路由，不另起 Key 真相源。**MCP 与本机 skill 覆盖都落在 `.env` 的
`MCP_SERVERS`**（4.1 已有的键，工具装配侧 `app/agent/tools/mcp.py` 已经在读它），
本机 skill 目录落在 `SKILL_DIR`。解析与状态判定在 `app/api/skills_spec.py`，
MCP 连接探测在 `app/api/mcp_probe.py`。

四个端点（02 §1.3）：

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/api/skills` | `SkillsOut` 全量（内置 + 本机扫描 + MCP），**只读、恒 200** |
| PUT | `/api/skills` | 整表替换 MCP +（可选）本机覆盖与 skill 目录；**先校验、后落盘、再改内存** |
| POST | `/api/skills/scan` | 重新扫描本机 skill 目录（不改配置，只刷新列表） |
| POST | `/api/skills/mcp/probe` | 连 MCP server 列工具名；失败给**可读中文**，不抛 500 |

⚠️ **不要**把本 router 再 `include` 进 `app/api/__init__.py`：02 §1.3 把 skill 列为
「独立挂载路由」，`main.py` 是正确挂点（同路径注册两次会让先注册者静默胜出）。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api import mcp_probe, skills_spec
from app.core.config import settings
from app.core.env_store import persist_env

router = APIRouter()


# --- 入参 ----------------------------------------------------------------


class McpToolIn(BaseModel):
    """`McpTool`（`api.ts:1255-1261`）：`dangerous` 缺省由后端按名字 + 描述判。"""

    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    dangerous: bool | None = None
    enabled: bool = True


class PackIn(BaseModel):
    """界面提交的一条 skill 配置。

    ``transport`` 收 ``str`` 不收 ``Literal``（老基线踩过的坑，逐字沿用它的注释）：GET 回显的
    **本机 skill** 条目 `transport` 是空串，整表保存时会原样传回 —— 收紧成 `Literal` 会让
    「存在本机 skill 时的保存」必然 422。空值 / 未知值按 `skills_spec.normalize_mcp_pack` 归一。

    ``extra="ignore"``：前端送的是**整个 `SkillPack`**（含 `locked` / `available` / `status` /
    `status_note` / `path`），那些字段是**服务端算出来的**，不做入参（送回来也不采纳）。
    """

    model_config = {"extra": "ignore"}

    id: str = ""
    type: Literal["mcp", "local"] = "mcp"
    slug: str = ""
    name: str = Field(default="", max_length=128)
    description: str = Field(default="", max_length=512)
    enabled: bool = True
    transport: str = "stdio"
    command: list[str] = Field(default_factory=list)
    url: str = Field(default="", max_length=1024)
    tools: list[McpToolIn] = Field(default_factory=list)


class SkillsPut(BaseModel):
    """`SkillsPut`（`api.ts:1302-1305`）：`packs` 是**整表**（builtin 不进提交列表）。"""

    packs: list[PackIn] = Field(default_factory=list)
    skill_dir: str | None = None


class McpProbeIn(BaseModel):
    """`probeMcp`（`api.ts:1311`）。"""

    transport: Literal["stdio", "http"] = "stdio"
    command: list[str] = Field(default_factory=list)
    url: str = Field(default="", max_length=1024)
    id: str = ""


# --- 归一 ----------------------------------------------------------------


def _pack_from_in(item: PackIn) -> dict[str, Any]:
    """一条入参 → 落盘用的包目录（老基线 `_pack_from_in` 同语义）。"""
    tools: list[dict[str, Any]] = []
    for tool in item.tools:
        dangerous = tool.dangerous
        if dangerous is None:
            dangerous = skills_spec.is_dangerous_tool(tool.name, tool.description)
        tools.append(
            {
                "name": tool.name.strip(),
                "description": (tool.description or "").strip(),
                "dangerous": bool(dangerous),
                "enabled": bool(tool.enabled),
            }
        )
    if item.type == "local":
        # 本机 skill 只看启停 / 改名：路径以扫描结果为准（不采纳入参里的 path）
        return {
            "id": (item.id or "").strip() or f"local:{skills_spec.slugify(item.name)}",
            "type": "local",
            "enabled": bool(item.enabled),
            "name": item.name.strip(),
            "description": item.description.strip(),
        }
    pack = dict(item.model_dump())
    pack["tools"] = tools
    pack["id"] = (item.id or "").strip()
    return pack


# --- 端点 ----------------------------------------------------------------


@router.get("/skills")
async def get_skills() -> dict[str, Any]:
    """技能 / MCP 列表（`GET /api/skills`）：只读、不落盘、恒 200。"""
    return skills_spec.skills_payload()


@router.put("/skills")
async def put_skills(payload: SkillsPut) -> dict[str, Any]:
    """整表替换 MCP packs（+ 可选本机覆盖与 skill 目录）。builtin 不在此维护。

    顺序与 `/api/settings` 同一口径：**先全部校验（400、零落盘）→ 一次落盘 → 才改内存**；
    写盘失败 ⇒ **500 + 中文**且内存不动。落盘是**一次 `persist_env` 写两个键**
    （`MCP_SERVERS` + 可选的 `SKILL_DIR`），不会出现半截状态。
    """
    updates: dict[str, str] = {}

    if payload.skill_dir is not None:
        cleaned = payload.skill_dir.strip()
        if not cleaned:
            raise HTTPException(status_code=400, detail="本机 skill 目录不能为空")
        updates["SKILL_DIR"] = cleaned

    mcp: list[dict[str, Any]] = []
    local: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in payload.packs:
        pack = _pack_from_in(item)
        if pack["type"] == "local":
            if pack["id"] in seen_ids:
                raise HTTPException(status_code=400, detail=f"技能 id 重复：{pack['id']}")
            seen_ids.add(pack["id"])
            local.append(pack)
            continue

        if not pack["id"]:
            pack["id"] = f"mcp:{skills_spec.slugify(pack.get('name') or (pack['command'][0] if pack.get('command') else pack.get('url') or 'server'))}"
        # 去重键必须取**归一化之后**的 id：`normalize_mcp_pack`（`skills_spec.py:207-210`）会给
        # 无冒号 id 补 `mcp:` 前缀，而 `dump_packs` 落盘用的就是归一化后的 id ⇒ 若在这里按原始 id
        # 去重，同一次整表提交里的 `foo` 与 `mcp:foo` 会各自通过，落盘成**两行同 id**
        # （`回归修复`：`GET /api/skills` 出现两条同名同 id 的 MCP server）。
        # 归一化是**纯函数**（不改入参、不落盘）⇒ 这里只为取 id 调一次；下面的落盘归一化点与
        # 「先全部校验 → 一次落盘 → 才改内存」顺序均未动。本机 local 分支不归一，但 `seen_ids`
        # 与本键共用 ⇒ local 的 `mcp:foo` 与 mcp 的 `foo`（归一后同为 `mcp:foo`）也会撞上。
        dedup_id = skills_spec.normalize_mcp_pack(pack)["id"]
        if dedup_id in seen_ids:
            raise HTTPException(status_code=400, detail=f"技能 id 重复：{dedup_id}")
        seen_ids.add(dedup_id)

        transport = (pack.get("transport") or "stdio").strip().lower()
        if transport not in skills_spec.TRANSPORTS:
            transport = "stdio"
        pack["transport"] = transport
        command = [str(part).strip() for part in pack.get("command") or [] if str(part).strip()]
        pack["command"] = command
        url = (pack.get("url") or "").strip()
        pack["url"] = url
        name = (pack.get("name") or "").strip() or pack["id"]
        pack["name"] = name
        if transport == "stdio" and not command:
            raise HTTPException(status_code=400, detail=f"「{name}」stdio 需要启动命令")
        if transport == "http" and not url:
            raise HTTPException(status_code=400, detail=f"「{name}」http 需要接口地址")

        # 归一后**再**过一遍注册表的判定（`slug` 缺省回落展示名、`status`/`available` 服务端算）
        mcp.append(skills_spec.normalize_mcp_pack(pack))

    updates["MCP_SERVERS"] = skills_spec.dump_packs(mcp, local)

    try:
        persist_env(updates)
    except OSError:
        raise HTTPException(status_code=500, detail="写入 .env 失败，请检查文件权限") from None

    if "SKILL_DIR" in updates:
        settings.SKILL_DIR = updates["SKILL_DIR"]
    settings.MCP_SERVERS = updates["MCP_SERVERS"]
    return skills_spec.skills_payload()


@router.post("/skills/scan")
async def rescan_skills() -> dict[str, Any]:
    """重新扫描本机 skill 目录（不改配置，只刷新列表）。

    列表本来就是**每次现扫**（`skills_spec.list_skills()` 不缓存），所以这里与 `GET` 同实现；
    端点存在的意义是给界面一个明确的「重新扫描」动作与刷新语义。
    """
    return skills_spec.skills_payload()


@router.post("/skills/mcp/probe")
async def probe_mcp(payload: McpProbeIn) -> dict[str, Any]:
    """连接 MCP server 并列出工具名；失败返回可读 `error`，**不抛 500**。

    「空命令 / 空地址」是**入参问题** ⇒ 400（live 契约用例
    `test_skills_mcp_probe_stdio_no_command_400` 就钉这一条）；「连不上 / 协议不对 / 超时」
    是**探测结果** ⇒ 200 + `ok=false` + 可读 `error`（设置页要把它显示在按钮脚下，不是弹崩）。
    """
    command = [str(part).strip() for part in payload.command if str(part).strip()]
    url = payload.url.strip()
    if payload.transport == "stdio" and not command:
        raise HTTPException(status_code=400, detail="stdio 需要填写启动命令")
    if payload.transport == "http" and not url:
        raise HTTPException(status_code=400, detail="http 需要填写接口地址")

    tools, error = await mcp_probe.probe_server(payload.transport, command, url)
    return {
        "ok": not error,
        "tools": tools,
        "error": error,
        "hint": (
            "工具已列出；高危工具默认不会被自动执行。"
            if not error
            else "请检查命令 / 地址是否可启动，以及是否已安装依赖。"
        ),
    }


__all__ = ["router"]
