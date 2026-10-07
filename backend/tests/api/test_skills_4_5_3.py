"""技能 / MCP 四个端点（阶段 4 · 条目 `4.5.3` skills 域）。

覆盖：

- `GET /api/skills` 的 `SkillsOut` 全量形状（`items` / `skill_dir` / `skill_dir_exists` / `counts`）
  与 `SkillPack` **15 键**（`api.ts:1263-1279`）；
- 本机 skill 目录扫描（`SKILL.md` frontmatter + `skill.json` 两条路，含坏清单的置灰口径）；
- `PUT /api/skills` 的整表替换、`skill_dir` 写入、三条 400（stdio 缺命令 / http 缺地址 / id 重复）；
- `POST /api/skills/scan`；
- `POST /api/skills/mcp/probe`：**真的**连一个 stdio MCP server（用 `sys.executable -c` 起一个
  最小 JSON-RPC 应答进程，不联网、不装依赖），以及两条入参 400。

**落盘一律走假 `.env`**（`env_store.ENV_PATH` → `tmp_path`），**绝不许碰仓库真实 `.env`**。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from app.api import mcp_probe, skill_routes, skills_spec
from app.core import env_store
from app.core.config import settings

from tests.api._sync_client import SyncClient, build_app

PACK_KEYS = [
    "id",
    "slug",
    "name",
    "type",
    "description",
    "enabled",
    "locked",
    "available",
    "status",
    "status_note",
    "tools",
    "path",
    "command",
    "url",
    "transport",
]

#: 最小 MCP server：读一行 JSON-RPC、回一行结果（`initialize` 与 `tools/list` 两种）。
#: `\\n` 是**父进程源码里的转义**：子进程 `-c` 收到的是 `"\n"` 字面量。
_MCP_SERVER = """
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg.get("method") == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {}}
    else:
        result = {"tools": [
            {"name": "read_file", "description": "读取文件"},
            {"name": "delete_file", "description": "删除文件"},
        ]}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\\n")
    sys.stdout.flush()
"""


@pytest.fixture
def fake_dotenv(tmp_path, monkeypatch) -> Path:
    path = tmp_path / ".env"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(env_store, "ENV_PATH", path)
    assert env_store.ENV_PATH != env_store.ROOT_DIR / ".env", "假 .env 落到了仓库真实路径上"
    return path


@pytest.fixture
def client():
    return SyncClient(build_app(skill_routes.router))


@pytest.fixture
def clean_state(monkeypatch, fake_dotenv, tmp_path):
    """初始态：没有 MCP、没有本机 skill 目录（`SKILL_DIR` 指向一个不存在的 tmp 路径）。"""
    monkeypatch.setattr(settings, "MCP_SERVERS", "")
    monkeypatch.setattr(settings, "SKILL_DIR", str(tmp_path / "skills-missing"))
    return settings


def _make_local_skill(root: Path, name: str, *, body: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(body, encoding="utf-8")
    return folder


def _env_value(path: Path, key: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    raise AssertionError(f".env 里没有 {key}")


# --- GET /api/skills -------------------------------------------------------


def test_get_skills_shape_and_builtins(client, clean_state):
    resp = client.get("/api/skills")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert sorted(body.keys()) == ["counts", "items", "skill_dir", "skill_dir_exists"]
    assert sorted(body["counts"].keys()) == ["builtin", "enabled", "local", "mcp", "total"]

    ids = [item["id"] for item in body["items"]]
    assert ids == ["builtin:knowledge", "builtin:web"]
    for item in body["items"]:
        assert list(item.keys()) == PACK_KEYS
        assert item["type"] == "builtin"
        assert item["locked"] is True
    knowledge = body["items"][0]
    assert knowledge["enabled"] is True and knowledge["available"] is True
    assert body["skill_dir"].endswith("skills-missing")
    assert body["skill_dir_exists"] is False
    assert body["counts"] == {"total": 2, "enabled": 1, "local": 0, "mcp": 0, "builtin": 2}


def test_get_skills_web_builtin_follows_web_enable(client, clean_state, monkeypatch):
    monkeypatch.setattr(settings, "WEB_ENABLE", True)
    web = next(item for item in client.get("/api/skills").json()["items"] if item["id"] == "builtin:web")
    assert web["enabled"] is True and web["available"] is True and web["status"] == "ok"
    assert [tool["name"] for tool in web["tools"]] == ["web_search", "fetch_page"]

    monkeypatch.setattr(settings, "WEB_ENABLE", False)
    web = next(item for item in client.get("/api/skills").json()["items"] if item["id"] == "builtin:web")
    assert web["enabled"] is False and web["status"] == "disabled" and web["status_note"]


def test_local_scan_reads_skill_md_and_skill_json(client, clean_state, tmp_path, monkeypatch):
    root = tmp_path / "skills-dir"
    root.mkdir()
    _make_local_skill(
        root,
        "My Skill",
        body="---\nname: 我的技能\ndescription: 一个说明\n---\n正文提示词\n",
    )
    json_folder = root / "json-skill"
    json_folder.mkdir()
    (json_folder / "skill.json").write_text(
        json.dumps({"name": "JSON 技能", "description": "来自 json"}), encoding="utf-8"
    )
    (root / "not-a-skill").mkdir()  # 没有清单 ⇒ 不算
    (root / ".hidden").mkdir()  # 点开头 ⇒ 跳过
    monkeypatch.setattr(settings, "SKILL_DIR", str(root))

    body = client.get("/api/skills").json()
    local = [item for item in body["items"] if item["type"] == "local"]
    # 扫描顺序 = 目录名小写排序（json-skill < my skill < not-a-skill）
    assert [item["id"] for item in local] == ["local:json-skill", "local:my-skill"]
    by_id = {item["id"]: item for item in local}
    assert by_id["local:my-skill"]["name"] == "我的技能"
    assert by_id["local:my-skill"]["description"] == "一个说明"
    assert by_id["local:json-skill"]["name"] == "JSON 技能"
    for item in local:
        assert list(item.keys()) == PACK_KEYS
        assert item["available"] is True and item["status"] == "ok"
        assert item["transport"] == "" and item["tools"] == []
    assert body["skill_dir_exists"] is True
    assert body["counts"]["local"] == 2
    assert body["counts"]["total"] == 4


def test_local_scan_marks_broken_manifest_unavailable(client, clean_state, tmp_path, monkeypatch):
    root = tmp_path / "skills-broken"
    folder = root / "broken"
    folder.mkdir(parents=True)
    (folder / "skill.json").write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(settings, "SKILL_DIR", str(root))

    item = next(i for i in client.get("/api/skills").json()["items"] if i["type"] == "local")
    assert item["available"] is False
    assert item["status"] == "invalid"
    assert item["status_note"] == "skill.json 无法解析"


def test_get_skills_survives_broken_mcp_servers(client, clean_state, monkeypatch):
    """坏配置绝不炸（`.env` 是人手改得动的文件）：`GET` 仍 200，MCP 条目按空处理。"""
    monkeypatch.setattr(settings, "MCP_SERVERS", "{ not json")
    resp = client.get("/api/skills")
    assert resp.status_code == 200
    assert resp.json()["counts"]["mcp"] == 0

    monkeypatch.setattr(settings, "MCP_SERVERS", json.dumps([{"slug": "demo", "tools": ["read_file"]}]))
    body = client.get("/api/skills").json()
    assert body["counts"]["mcp"] == 1
    pack = next(item for item in body["items"] if item["type"] == "mcp")
    assert list(pack.keys()) == PACK_KEYS
    assert pack["status"] == "invalid" and "stdio" in pack["status_note"]


# --- PUT /api/skills -------------------------------------------------------


def test_put_skills_persists_mcp_packs(client, clean_state, fake_dotenv):
    resp = client.put(
        "/api/skills",
        json={
            "packs": [
                {
                    "id": "mcp:demo",
                    "type": "mcp",
                    "slug": "demo",
                    "name": "演示服务",
                    "description": "说明",
                    "enabled": True,
                    "transport": "stdio",
                    "command": ["npx", "-y", "demo-server"],
                    "url": "",
                    "tools": [
                        {"name": "read_file", "description": "读文件"},
                        {"name": "delete_file", "description": "删文件", "dangerous": True},
                    ],
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    pack = next(item for item in resp.json()["items"] if item["id"] == "mcp:demo")
    assert pack["type"] == "mcp" and pack["available"] is True and pack["status"] == "ok"
    assert [tool["name"] for tool in pack["tools"]] == ["read_file", "delete_file"]
    assert pack["tools"][0]["dangerous"] is False and pack["tools"][1]["dangerous"] is True

    raw = json.loads(_env_value(fake_dotenv, "MCP_SERVERS"))
    assert raw[0]["slug"] == "demo"
    assert raw[0]["command"] == ["npx", "-y", "demo-server"]
    assert raw[0]["tools"][1] == {
        "name": "delete_file",
        "description": "删文件",
        "dangerous": True,
        "enabled": True,
    }
    assert settings.MCP_SERVERS == _env_value(fake_dotenv, "MCP_SERVERS")


def test_put_skills_requires_command_or_url(client, clean_state, fake_dotenv):
    before = fake_dotenv.read_text(encoding="utf-8")
    resp = client.put(
        "/api/skills",
        json={"packs": [{"id": "mcp:x", "type": "mcp", "name": "X", "transport": "stdio", "command": []}]},
    )
    assert resp.status_code == 400
    assert "stdio 需要启动命令" in resp.json()["detail"]
    assert fake_dotenv.read_text(encoding="utf-8") == before

    resp = client.put(
        "/api/skills",
        json={"packs": [{"id": "mcp:y", "type": "mcp", "name": "Y", "transport": "http", "url": ""}]},
    )
    assert resp.status_code == 400
    assert "http 需要接口地址" in resp.json()["detail"]
    assert fake_dotenv.read_text(encoding="utf-8") == before


def test_put_skills_rejects_duplicate_id(client, clean_state, fake_dotenv):
    before = fake_dotenv.read_text(encoding="utf-8")
    resp = client.put(
        "/api/skills",
        json={
            "packs": [
                {"id": "mcp:d", "name": "A", "command": ["a"], "tools": []},
                {"id": "mcp:d", "name": "B", "command": ["b"], "tools": []},
            ]
        },
    )
    assert resp.status_code == 400
    assert "技能 id 重复" in resp.json()["detail"]
    assert fake_dotenv.read_text(encoding="utf-8") == before


def test_put_skills_writes_skill_dir_and_local_override(client, clean_state, fake_dotenv, tmp_path):
    root = tmp_path / "skills-live"
    _make_local_skill(root, "my-skill", body="# hello\n")

    resp = client.put(
        "/api/skills",
        json={
            "packs": [
                {
                    "id": "local:my-skill",
                    "type": "local",
                    "name": "改过的名字",
                    "description": "改过的说明",
                    "enabled": False,
                }
            ],
            "skill_dir": str(root),
        },
    )
    assert resp.status_code == 200, resp.text
    assert _env_value(fake_dotenv, "SKILL_DIR") == str(root)
    assert settings.SKILL_DIR == str(root)

    item = next(i for i in resp.json()["items"] if i["id"] == "local:my-skill")
    assert item["enabled"] is False and item["available"] is False and item["status"] == "disabled"
    assert item["name"] == "改过的名字"
    # 覆盖项与 MCP 同存一处（`MCP_SERVERS`），用 type 区分
    raw = json.loads(_env_value(fake_dotenv, "MCP_SERVERS"))
    assert raw == [
        {
            "id": "local:my-skill",
            "type": "local",
            "enabled": False,
            "name": "改过的名字",
            "description": "改过的说明",
        }
    ]


def test_put_skills_rejects_blank_skill_dir(client, clean_state, fake_dotenv):
    before = fake_dotenv.read_text(encoding="utf-8")
    resp = client.put("/api/skills", json={"packs": [], "skill_dir": "   "})
    assert resp.status_code == 400
    assert "不能为空" in resp.json()["detail"]
    assert fake_dotenv.read_text(encoding="utf-8") == before


def test_put_skills_local_override_yields_no_mcp_tool_spec(client, clean_state, fake_dotenv):
    """本机覆盖项与 MCP 同存 `MCP_SERVERS` ⇒ **必须**产出零条 MCP 工具（否则是安全面问题）。"""
    from app.agent.tools import mcp as mcp_tools

    resp = client.put(
        "/api/skills",
        json={"packs": [{"id": "local:x", "type": "local", "name": "X", "enabled": True}]},
    )
    assert resp.status_code == 200, resp.text
    packs = mcp_tools.load_packs(raw=settings.MCP_SERVERS)
    assert len(packs) == 1 and packs[0].tools == ()
    assert mcp_tools.build_specs(packs) == []


# --- POST /api/skills/scan -------------------------------------------------


def test_rescan_returns_same_payload(client, clean_state):
    assert client.post("/api/skills/scan").json() == client.get("/api/skills").json()


# --- POST /api/skills/mcp/probe -------------------------------------------


def test_probe_requires_command_or_url(client, clean_state):
    """live 契约用例 `test_skills_mcp_probe_stdio_no_command_400` 钉的就是这一条。"""
    resp = client.post("/api/skills/mcp/probe", json={"transport": "stdio", "command": []})
    assert resp.status_code == 400
    assert isinstance(resp.json()["detail"], str) and resp.json()["detail"]

    resp = client.post("/api/skills/mcp/probe", json={"transport": "http", "url": ""})
    assert resp.status_code == 400


def test_probe_talks_to_a_real_stdio_server(client, clean_state):
    """**真连**一个 stdio MCP server（`sys.executable -c` 起的最小 JSON-RPC 应答进程）。"""
    resp = client.post(
        "/api/skills/mcp/probe",
        json={"transport": "stdio", "command": [sys.executable, "-c", _MCP_SERVER]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["error"] == ""
    tools = {tool["name"]: tool for tool in body["tools"]}
    assert set(tools) == {"read_file", "delete_file"}
    assert tools["read_file"]["dangerous"] is False
    assert tools["delete_file"]["dangerous"] is True, "高危工具必须被标出来（I6 的前置）"
    assert all(tool["enabled"] is True for tool in body["tools"])
    assert body["hint"]


def test_probe_failure_is_readable_not_500(client, clean_state):
    resp = client.post(
        "/api/skills/mcp/probe",
        json={"transport": "stdio", "command": ["definitely-not-an-executable-xyz"]},
    )
    assert resp.status_code == 200, "探测失败是结果、不是错误（设置页要把它显示在按钮脚下）"
    body = resp.json()
    assert body["ok"] is False and body["tools"] == []
    assert "找不到可执行文件" in body["error"]
    assert body["hint"]


# --- 回归修复：`sse` 必须**显式不支持**（不得误走 stdio / 起子进程） -------


def _probe_counters(monkeypatch) -> dict:
    """把两个 loader 换成计数桩（这一组用例只判「走没走对通道」，不起子进程、不出网）。"""
    calls = {"stdio": 0, "http": 0}

    async def fake_stdio(command, env=None, timeout=None):  # noqa: ANN001
        calls["stdio"] += 1
        return []

    async def fake_http(url, headers=None, timeout=None):  # noqa: ANN001
        calls["http"] += 1
        return []

    monkeypatch.setattr(mcp_probe, "list_tools_stdio", fake_stdio)
    monkeypatch.setattr(mcp_probe, "list_tools_http", fake_http)
    return calls


def test_probe_endpoint_rejects_sse_at_schema_level(client, clean_state):
    """**端点面**（可达性实测）：`McpProbeIn.transport` 是 `Literal["stdio","http"]`
    ⇒ `sse` **在进 `probe_server` 之前**就被 422 拒掉（`skill_routes.py:82`）。

    ⇒ 这条缺陷的**可达面只有函数面**（见下一条）：端点没漏，但 `mcp_probe.probe_server`
    是公开函数、且 skill 契约里 `sse` 是合法取值（`tools/mcp.py::TRANSPORTS`）
    ⇒ 一旦哪天把 Literal 放开，函数就会把 `sse` 误当 stdio 起子进程。本用例把这条边界钉住。
    """
    resp = client.post(
        "/api/skills/mcp/probe",
        json={"transport": "sse", "command": [sys.executable, "-c", _MCP_SERVER], "url": ""},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("transport", ["sse", "websocket", "SSE", "grpc"])
def test_probe_unsupported_transport_never_touches_a_loader(transport, clean_state, monkeypatch):
    """**函数面**：`sse` / 未知通道一律「不成通道」—— 回可读中文，且**零子进程、零出网**。

    改前 `probe_server` 是「不是 http 就走 stdio」⇒ 这些取值会拿 `command` 当可执行名去
    **起一个 stdio 子进程**（既有风险面 + 一句误导性的失败原因）。
    """
    calls = _probe_counters(monkeypatch)
    tools, error = asyncio.run(
        mcp_probe.probe_server(transport, [sys.executable, "-c", _MCP_SERVER], "")
    )
    assert tools == []
    assert error and "不支持" in error, error
    assert transport.lower() in error.lower(), error
    assert calls == {"stdio": 0, "http": 0}, calls


def test_probe_http_stdio_and_empty_keep_their_behaviour(clean_state, monkeypatch):
    """正控：`http` / `stdio` / **空串**三态行为逐字不变（空串仍是既有的「默认 stdio」）。"""
    calls = _probe_counters(monkeypatch)
    assert asyncio.run(mcp_probe.probe_server("http", [], "http://x/mcp")) == ([], "")
    assert asyncio.run(mcp_probe.probe_server("stdio", ["x"], "")) == ([], "")
    assert asyncio.run(mcp_probe.probe_server("", ["x"], "")) == ([], "")
    assert calls == {"stdio": 2, "http": 1}, calls


# --- 归一规则的一致性钉子 ---------------------------------------------------


@pytest.mark.parametrize("raw", ["My Server", "我的服务", "123abc", "a" * 80, "", "  "])
def test_slugify_matches_agent_side(raw):
    """两处 slug 归一必须同口径（`skills_spec.slugify` vs `agent.tools.mcp._slugify`）。"""
    from app.agent.tools import mcp as mcp_tools

    assert skills_spec.slugify(raw) == mcp_tools._slugify(raw)


def test_normalize_mcp_pack_registers_id_prefix(client, clean_state):
    pack = skills_spec.normalize_mcp_pack({"id": "demo", "name": "Demo", "command": ["x"]})
    assert pack["id"] == "mcp:demo"
    assert pack["slug"] == "demo"
    assert pack["type"] == "mcp"
    assert list(pack.keys()) == PACK_KEYS
