"""技能 / MCP 注册表（任务清单 35；阶段 4 · 条目 `4.5.3` skills 域）。

一处真相：内置能力包、本机 skill 目录扫描、MCP server 配置的解析与状态判定都在这里；
界面照 `skills_payload()` 渲染（`GET /api/skills`），`PUT /api/skills` 整表写回。
**老基线对应模块**：`<legacy-repo>\\backend\\app\\rag\\skills.py`（只读对照，行为口径逐条对齐）。

**落盘键只有一个：`MCP_SERVERS`**（v2 在 4.1 已有该键，工具装配侧 `app/agent/tools/mcp.py`
已经在读它）。本机 skill 的启停/改名覆盖也存进**同一个数组**，用 `type` 区分：

- `type="mcp"` 的元素与 agent 侧完全同构（slug/name/transport/command/url/enabled/tools）；
- `type="local"` 的元素只是「覆盖项」（id/enabled/name/description），**没有 `tools`** ⇒
  agent 侧 `load_packs()` 解析出零条工具、`build_specs()` 产出零个 `ToolSpec`
  （栅栏见 `tests/api/test_skills_4_5_3.py`）。**为什么不新造第二个注册表**：
  `MCP_SERVERS` 已经是「MCP 包清单」的真相源，再开一个键就是两处真相
  （那份清单会被两处各自序列化，谁后写谁赢）。

**⚠️ 本模块不做的两件事**（都在本条目的文件白名单之外，已登记在证据文件）：

1. **技能提示词注入** —— 02 §7.2 的 `skill_ids` 目前「只落库、不注入提示词」
   （`app/api/chat_routes.py` 的明文注释），注入点在 `app/api/chat_routes.py` 与
   `app/generation/**`，两者都不在白名单内；
2. **MCP 工具的真调用通道** —— `app/agent/tools/mcp.py::call_tool_hook` 的注入点是
   `build_registry(call_tool=...)`，调用方在 `app/agent/runner.py`，同样在名单外。
   本条目交付的是**配置面**（`probe` 真的连 server 列出工具名）+ 契约端点。

**安全口径**（与老基线同）：本机 skill 只读 `SKILL.md` / `skill.json` 的**文本**，不执行任何代码；
MCP 只登记 server 与工具名，`dangerous` 的工具在 agent 侧**零暴露**（I6，见 `mcp.build_specs`）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.agent.tools.mcp import dangerous_by_default
from app.core.config import settings
from app.core.env_store import ROOT_DIR

#: 一个文件夹里有这些文件之一就算一个本机 skill（老基线 `LOCAL_MANIFEST_NAMES` 同）
LOCAL_MANIFEST_NAMES = ("SKILL.md", "skill.md", "skill.json")

#: slug 归一：只留 `[a-z0-9_-]`（逐字沿用老基线 `skills.slugify` 与
#: `app/agent/tools/mcp.py::_slugify`；两侧一致性由 `tests/api/test_skills_4_5_3.py` 钉住）。
_SLUG_INVALID_RE = re.compile(r"[^a-z0-9_-]+")
_SLUG_MAX_LEN = 64
_SLUG_FALLBACK = "skill"

#: MCP 允许的传输通道（`api.ts::SkillPack.transport` 是裸 str，前端按 `=== "http"` 分支）。
TRANSPORTS = ("stdio", "http")


def slugify(raw: str) -> str:
    """slug 归一（老基线逐字口径：小写、非法字符折 `-`、数字开头加 `s-`、截 64、空回落 `skill`）。"""
    text = (raw or "").strip().lower()
    text = _SLUG_INVALID_RE.sub("-", text).strip("-")
    if not text:
        return _SLUG_FALLBACK
    if text[0].isdigit():
        text = f"s-{text}"
    return text[:_SLUG_MAX_LEN]


def is_dangerous_tool(name: str, description: str = "") -> bool:
    """高危工具粗规则：**复用 agent 侧那一份**（`mcp.dangerous_by_default`，名字 + 描述都看）。

    为什么不照老基线只看名字：`SkillPack.tools[].dangerous` 是**同一个概念**，两处各写一份
    正则迟早漂移；而判宽了最坏是少一条工具（可用性损失），判窄了是危险工具进了模型清单（I6 违约）。
    """
    return dangerous_by_default(name, description)


def skill_dir_path() -> Path:
    """本机 skill 目录的绝对路径（相对路径按仓库根解析，口径同 `UPLOAD_DIR`）。"""
    raw = (settings.SKILL_DIR or "skills").strip().strip('"').strip("'")
    path = Path(raw or "skills").expanduser()
    return path if path.is_absolute() else (ROOT_DIR / path).resolve()


# --- 内置能力包 --------------------------------------------------------------


def builtin_skills() -> list[dict[str, Any]]:
    """内置能力包：不可删、不可改路径；knowledge 是默认问答路径。

    ``builtin:web``（任务清单 26）的启停由 `.env` 的 ``WEB_ENABLE`` 决定，不走 `MCP_SERVERS`；
    ``locked=True`` 表示界面不能在技能列表里直接开关它（改它要改联网设置）。
    """
    web_on = bool(settings.WEB_ENABLE)
    web_note = "" if web_on else "默认关闭。到设置 → 联网检索里开启后，对话输入 /web 即可联网搜索。"
    return [
        {
            "id": "builtin:knowledge",
            "slug": "knowledge",
            "name": "知识库问答",
            "type": "builtin",
            "description": "基于本机知识库检索回答，带引用来源（默认能力）",
            "enabled": True,
            "locked": True,
            "available": True,
            "status": "ok",
            "status_note": "",
            "tools": [],
            "path": "",
            "command": [],
            "url": "",
            "transport": "",
        },
        {
            "id": "builtin:web",
            "slug": "web",
            "name": "联网检索",
            "type": "builtin",
            "description": "联网搜索并抓取网页，清洗后并入回答上下文，URL 进入引用列表",
            "enabled": web_on,
            "locked": True,
            "available": web_on,
            "status": "ok" if web_on else "disabled",
            "status_note": web_note,
            "tools": [
                {"name": "web_search", "description": "按服务商搜索网页", "dangerous": False, "enabled": True},
                {"name": "fetch_page", "description": "抓取网页正文并清洗", "dangerous": False, "enabled": True},
            ],
            "path": "",
            "command": [],
            "url": "",
            "transport": "",
        },
    ]


# --- 配置解析（`MCP_SERVERS` 单行 JSON 数组）------------------------------------------------


def _load_packs_raw() -> list[dict[str, Any]]:
    """宽容解析 `MCP_SERVERS`：空 / 坏 JSON / 顶层不是数组 ⇒ 空列表（`.env` 是人手改得动的文件）。"""
    raw = (settings.MCP_SERVERS or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _normalize_tools(tools_raw: Any) -> list[dict[str, Any]]:
    """工具清单归一：裸字符串与对象两种写法都认（老基线同），坏条目静默丢弃。"""
    tools: list[dict[str, Any]] = []
    if not isinstance(tools_raw, list):
        return tools
    for tool in tools_raw:
        if isinstance(tool, str):
            name = tool.strip()
            if name:
                tools.append(
                    {"name": name, "description": "", "dangerous": is_dangerous_tool(name), "enabled": True}
                )
            continue
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        description = str(tool.get("description") or "").strip()
        dangerous = (
            bool(tool.get("dangerous"))
            if "dangerous" in tool
            else is_dangerous_tool(name, description)
        )
        tools.append(
            {
                "name": name,
                "description": description,
                "dangerous": dangerous,
                # 工具级启停（35 修订）：缺省当启用，旧配置无需迁移
                "enabled": bool(tool.get("enabled", True)),
            }
        )
    return tools


def normalize_mcp_pack(item: dict[str, Any]) -> dict[str, Any]:
    """校验并归一一条 MCP 配置 —— `SkillPack` **15 键**（`api.ts:1263-1279`）。

    **不返回 None**（老基线会）：`stdio` 没命令 / `http` 没地址是**用户手填到一半**的正常状态，
    静默丢掉这条 = 界面上打了一半的 server 消失 ⇒ 这里改成「保留 + `available=false` +
    `status="invalid"` + 可读 `status_note`」，界面置灰并说明原因（老基线的界面口径如此）。
    """
    pack_id = str(item.get("id") or "").strip()
    transport = str(item.get("transport") or "stdio").strip().lower()
    if transport not in TRANSPORTS:
        transport = "stdio"
    name = str(item.get("name") or "").strip()
    url = str(item.get("url") or "").strip()
    command_raw = item.get("command") or []
    if isinstance(command_raw, str):
        command_raw = command_raw.split()
    if not isinstance(command_raw, list):
        command_raw = []
    command = [str(part).strip() for part in command_raw if str(part).strip()]
    tools = _normalize_tools(item.get("tools"))

    if not pack_id:
        pack_id = f"mcp:{slugify(name or (command[0] if command else url or 'server'))}"
    elif ":" not in pack_id:
        pack_id = f"mcp:{pack_id}"
    if not name:
        name = pack_id.split(":", 1)[-1]

    status = "ok"
    status_note = ""
    available = True
    enabled = bool(item.get("enabled", True))
    if transport == "stdio" and not command:
        available = False
        status = "invalid"
        status_note = "stdio 需要填写启动命令"
    elif transport == "http" and not url:
        available = False
        status = "invalid"
        status_note = "http 需要填写接口地址"
    elif not enabled:
        status = "disabled"
        status_note = "已在设置里停用"

    return {
        "id": pack_id,
        "slug": slugify(str(item.get("slug") or name)),
        "name": name,
        "type": "mcp",
        "description": str(item.get("description") or "MCP server").strip(),
        "enabled": enabled,
        "locked": False,
        "available": available and enabled,
        "status": status,
        "status_note": status_note,
        "tools": tools,
        "path": "",
        "command": command,
        "url": url,
        "transport": transport,
    }


def mcp_packs() -> list[dict[str, Any]]:
    """`MCP_SERVERS` 里的 MCP 条目（`type` 缺省当 `mcp`，与老基线同口径）。"""
    packs: list[dict[str, Any]] = []
    for item in _load_packs_raw():
        if str(item.get("type") or "mcp").strip().lower() not in ("mcp", ""):
            continue
        packs.append(normalize_mcp_pack(item))
    return packs


def local_overrides() -> dict[str, dict[str, Any]]:
    """本机 skill 的启停 / 改名覆盖（存在 `MCP_SERVERS` 里，`type="local"`，按 id 匹配）。"""
    out: dict[str, dict[str, Any]] = {}
    for item in _load_packs_raw():
        if str(item.get("type") or "").strip().lower() != "local":
            continue
        pack_id = str(item.get("id") or "").strip()
        if pack_id:
            out[pack_id] = item
    return out


# --- 本机 skill 扫描 ---------------------------------------------------------


def _read_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """极简 YAML frontmatter：只认 `---` 包围的 `key: value`，不引第三方解析器（老基线同）。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    meta: dict[str, Any] = {}
    body_start = 1
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            body_start = index + 1
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if not key:
            continue
        meta[key] = value.strip().strip("\"'")
    else:
        # 没有闭合：整篇当正文，不剥 frontmatter
        return {}, text.strip()
    return meta, "\n".join(lines[body_start:]).strip()


def _load_local_manifest(folder: Path) -> dict[str, Any] | None:
    """读一个本机 skill 文件夹的清单（`SKILL.md` 优先，其次 `skill.json`）。"""
    md_path = folder / "SKILL.md"
    if not md_path.is_file():
        md_path = folder / "skill.md"
    if md_path.is_file():
        text = md_path.read_text(encoding="utf-8", errors="replace")
        meta, body = _read_frontmatter(text)
        name = str(meta.get("name") or folder.name).strip()
        description = str(meta.get("description") or "").strip()
        return {
            "name": name,
            "description": description or "本机 skill（未写 description）",
            "source": str(md_path),
            "invalid": False,
        }
    json_path = folder / "skill.json"
    if json_path.is_file():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except ValueError:
            return _invalid_manifest(folder, json_path, "skill.json 无法解析")
        if not isinstance(data, dict):
            return _invalid_manifest(folder, json_path, "skill.json 根节点不是对象")
        return {
            "name": str(data.get("name") or folder.name).strip(),
            "description": str(data.get("description") or "本机 skill（未写 description）").strip(),
            "source": str(json_path),
            "invalid": False,
        }
    return None


def _invalid_manifest(folder: Path, path: Path, note: str) -> dict[str, Any]:
    return {"name": folder.name, "description": note, "source": str(path), "invalid": True, "note": note}


def scan_local_skills() -> list[dict[str, Any]]:
    """扫描 `SKILL_DIR` 下的子目录：每个含 `SKILL.md` / `skill.json` 的文件夹算一个 skill。

    只读清单文件的**文本**，不 import、不执行（安全口径见模块 docstring）。
    """
    root = skill_dir_path()
    if not root.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for folder in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        manifest = _load_local_manifest(folder)
        if manifest is None:
            continue
        slug = slugify(folder.name)
        invalid = bool(manifest.get("invalid"))
        note = str(manifest.get("note") or "") if invalid else ""
        found.append(
            {
                "id": f"local:{slug}",
                "slug": slug,
                "name": manifest["name"],
                "type": "local",
                "description": manifest["description"],
                "enabled": True,
                "locked": False,
                "available": not invalid,
                "status": "invalid" if invalid else "ok",
                "status_note": note,
                "tools": [],
                "path": str(folder),
                "command": [],
                "url": "",
                "transport": "",
            }
        )
    return found


def list_skills() -> list[dict[str, Any]]:
    """设置页 / 对话 `/` 菜单共用的完整列表（内置 + 本机扫描 + MCP）。"""
    skills: list[dict[str, Any]] = list(builtin_skills())
    overrides = local_overrides()
    for item in scan_local_skills():
        override = overrides.get(item["id"]) or {}
        if "enabled" in override:
            item["enabled"] = bool(override["enabled"])
            if not item["enabled"]:
                item["available"] = False
                item["status"] = "disabled"
                item["status_note"] = "已在设置里停用"
        if override.get("name"):
            item["name"] = str(override["name"]).strip()
        if override.get("description"):
            item["description"] = str(override["description"]).strip()
        skills.append(item)
    skills.extend(mcp_packs())
    return skills


def skills_payload() -> dict[str, Any]:
    """`GET /api/skills` 的出参（`SkillsOut`：`items` / `skill_dir` / `skill_dir_exists` / `counts`）。"""
    items = list_skills()
    enabled = [item for item in items if item.get("enabled") and item.get("available")]
    root = skill_dir_path()
    return {
        "items": items,
        "skill_dir": str(root),
        "skill_dir_exists": root.is_dir(),
        "counts": {
            "total": len(items),
            "enabled": len(enabled),
            "local": sum(1 for item in items if item["type"] == "local"),
            "mcp": sum(1 for item in items if item["type"] == "mcp"),
            "builtin": sum(1 for item in items if item["type"] == "builtin"),
        },
    }


# --- 落盘 ----------------------------------------------------------------


def dump_packs(mcp: list[dict[str, Any]], local: list[dict[str, Any]] | None = None) -> str:
    """把 MCP + 本机覆盖收成**单行 JSON**（写进 `MCP_SERVERS` 的那个值）。

    `local=None` 表示**保留原有覆盖**（老基线同）；`local=[]` 表示清空覆盖。

    ⚠️ 本函数**只序列化、不落盘**：落盘由调用方（`app/api/skill_routes.py`）统一做 ——
    一次 `PUT` 可能同时要写 `SKILL_DIR`，而 `env_store.persist_env` 是**一次调用写多个键**的，
    分两次写会出现「第一个键写成功、第二个键失败」的半截状态。
    """
    packs: list[dict[str, Any]] = []
    for pack in mcp:
        packs.append(
            {
                "id": pack["id"],
                "type": "mcp",
                "slug": pack.get("slug") or "",
                "name": pack.get("name") or "",
                "description": pack.get("description") or "",
                "enabled": bool(pack.get("enabled", True)),
                "transport": pack.get("transport") or "stdio",
                "command": list(pack.get("command") or []),
                "url": pack.get("url") or "",
                "tools": [
                    {
                        "name": tool.get("name"),
                        "description": tool.get("description") or "",
                        "dangerous": bool(tool.get("dangerous")),
                        "enabled": bool(tool.get("enabled", True)),
                    }
                    for tool in pack.get("tools") or []
                ],
            }
        )
    source_local = local if local is not None else list(local_overrides().values())
    for pack in source_local:
        packs.append(
            {
                "id": pack.get("id") or "",
                "type": "local",
                "enabled": bool(pack.get("enabled", True)),
                "name": pack.get("name") or "",
                "description": pack.get("description") or "",
            }
        )
    return json.dumps(packs, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "LOCAL_MANIFEST_NAMES",
    "TRANSPORTS",
    "builtin_skills",
    "dump_packs",
    "is_dangerous_tool",
    "list_skills",
    "local_overrides",
    "mcp_packs",
    "normalize_mcp_pack",
    "scan_local_skills",
    "skill_dir_path",
    "skills_payload",
    "slugify",
]
