"""`回归修复`：`PUT /api/skills` 的 id 去重必须落在**归一化之后**的 id 上。

缺陷形态（改前）：一次整表提交同时带 `id="foo"` 与 `id="mcp:foo"` 两条 MCP 包 ⇒ 去重按**原始 id**
各自通过；而 `normalize_mcp_pack`（`skills_spec.py:207-210`）会把两条都归一成 `mcp:foo`，
`dump_packs` 于是落盘**两行同 id** ⇒ `GET /api/skills` 出现两条同名同 id 的 MCP server
（agent 侧装配面重复）。本机 `local` 分支不归一、却与 MCP 共用同一个 `seen_ids`，所以
「local 的 `mcp:shared`」与「mcp 的 `shared`（归一后 `mcp:shared`）」也是同一条缺陷的两个面。

判别力（回退实验口径）：腿 ①②③⑤⑥ 在**改前必红**（改前 200 / 或 400 文案不同），腿 ④⑦⑧ 是
**对照腿**（改前改后都绿：证明修法不是「一律 400」式的恒真断言）；腿 ⑨ 是**机制腿**
（钉「落盘层确实允许两行同 id」，改前改后同值，不承担判别力）。

落盘一律走假 `.env`（`env_store.ENV_PATH` → `tmp_path`），绝不许碰仓库真实 `.env`。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.api import skill_routes, skills_spec
from app.core import env_store
from app.core.config import settings

from tests.api._sync_client import SyncClient, build_app


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
    """初始态：没有 MCP、没有本机 skill 目录。"""
    monkeypatch.setattr(settings, "MCP_SERVERS", "")
    monkeypatch.setattr(settings, "SKILL_DIR", str(tmp_path / "skills-missing"))
    return settings


def _mcp_pack(pack_id: str, name: str, command: tuple[str, ...] = ("cmd",)) -> dict:
    """一条最简 MCP 入参（`transport` 缺省 stdio ⇒ 必须给 command）。"""
    return {
        "id": pack_id,
        "type": "mcp",
        "name": name,
        "transport": "stdio",
        "command": list(command),
        "tools": [],
    }


def _local_pack(pack_id: str, name: str) -> dict:
    return {"id": pack_id, "type": "local", "name": name, "enabled": True}


def _env_value(path: Path, key: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    raise AssertionError(f".env 里没有 {key}")


def _stored_ids(path: Path) -> list[str]:
    return [row["id"] for row in json.loads(_env_value(path, "MCP_SERVERS"))]


# --- 判别腿 ------------------------------------------------------------------


def test_a1_foo_and_mcp_prefixed_foo_in_one_table_are_rejected(client, clean_state, fake_dotenv):
    """① 同一整表里的 `foo` 与 `mcp:foo` 归一后同 id ⇒ 400，且**零落盘**。

    改前：去重看原始 id（`foo` ≠ `mcp:foo`）⇒ 200，`.env` 里落下两行 id 均为 `mcp:foo`。
    """
    before = fake_dotenv.read_text(encoding="utf-8")
    resp = client.put("/api/skills", json={"packs": [_mcp_pack("foo", "A"), _mcp_pack("mcp:foo", "B")]})
    assert resp.status_code == 400, resp.text
    assert "技能 id 重复：mcp:foo" in resp.json()["detail"]
    assert fake_dotenv.read_text(encoding="utf-8") == before, "400 必须零落盘"


def test_a2_three_way_alias_collision_is_rejected(client, clean_state, fake_dotenv):
    """② 同批第三条以另一种写法撞上已登记 id ⇒ 仍 400（去重集合跨条目持续生效）。"""
    before = fake_dotenv.read_text(encoding="utf-8")
    resp = client.put(
        "/api/skills",
        json={"packs": [_mcp_pack("alpha", "A"), _mcp_pack("beta", "B"), _mcp_pack("mcp:alpha", "C")]},
    )
    assert resp.status_code == 400, resp.text
    assert "技能 id 重复：mcp:alpha" in resp.json()["detail"]
    assert fake_dotenv.read_text(encoding="utf-8") == before


def test_a3_local_and_mcp_normalizing_to_same_id_are_rejected(client, clean_state, fake_dotenv):
    """③ local 分支不归一、但与 MCP 共用 `seen_ids` ⇒ local 的 `mcp:shared` 与 mcp 的 `shared`
    归一后同为 `mcp:shared`，必须 400。

    改前：local 登记 `mcp:shared`，mcp 用原始 `shared` 去重 ⇒ 不撞 ⇒ 200，落盘两行同 id
    （一行 `type=local`、一行 `type=mcp`）。
    """
    before = fake_dotenv.read_text(encoding="utf-8")
    resp = client.put(
        "/api/skills",
        json={"packs": [_local_pack("mcp:shared", "本机覆盖"), _mcp_pack("shared", "MCP")]},
    )
    assert resp.status_code == 400, resp.text
    assert "技能 id 重复：mcp:shared" in resp.json()["detail"]
    assert fake_dotenv.read_text(encoding="utf-8") == before


def test_a4_duplicate_message_uses_the_normalized_id(client, clean_state, fake_dotenv):
    """④ 两条无冒号 id 撞车时，400 文案里的 id 取**归一后**形态（`mcp:bar`）。

    改前同一场景给的是「技能 id 重复：bar」（原始 id）—— 这是本条修法带来的**已知口径差异**
    （已登记）：文案模板与状态码不变，只有 `<id>` 由原始形态改成落盘形态。
    """
    resp = client.put("/api/skills", json={"packs": [_mcp_pack("bar", "A"), _mcp_pack("bar", "B")]})
    assert resp.status_code == 400, resp.text
    assert "技能 id 重复：mcp:bar" in resp.json()["detail"]


def test_a5_dedup_still_precedes_the_stdio_command_check(client, clean_state, fake_dotenv):
    """⑤ 顺序未破：去重仍排在 stdio/http 必填项校验**之前**（同改前口径）。

    第三条 `mcp:dup` 与第二条 `dup` 归一后同 id、且自身缺 command ⇒ 必须报「重复」而不是
    「stdio 需要启动命令」。改前：`mcp:dup` 与原始 `dup` 不撞 ⇒ 落到 command 校验 ⇒ 报缺命令。
    """
    before = fake_dotenv.read_text(encoding="utf-8")
    resp = client.put(
        "/api/skills",
        json={
            "packs": [
                _mcp_pack("ok", "OK"),
                _mcp_pack("dup", "D1"),
                _mcp_pack("mcp:dup", "D2", command=()),
            ]
        },
    )
    assert resp.status_code == 400, resp.text
    assert "技能 id 重复：mcp:dup" in resp.json()["detail"]
    assert fake_dotenv.read_text(encoding="utf-8") == before


# --- 对照腿（改前改后都绿）----------------------------------------------------


def test_a6_legal_table_still_persists_unique_ids(client, clean_state, fake_dotenv):
    """⑥ 正控：合法整表照旧 200，落盘 id 唯一且各自被归一（`demo` → `mcp:demo`）。

    这一条同时证明修法**没有**把「不同 id 但都无冒号」误判成重复。
    """
    resp = client.put(
        "/api/skills",
        json={"packs": [_mcp_pack("demo", "演示"), _mcp_pack("mcp:other", "另一个"), _mcp_pack("", "nameless")]},
    )
    assert resp.status_code == 200, resp.text
    ids = _stored_ids(fake_dotenv)
    assert ids == ["mcp:demo", "mcp:other", "mcp:nameless"], ids
    assert len(ids) == len(set(ids)), f"落盘 id 必须唯一：{ids}"
    listed = [item["id"] for item in resp.json()["items"] if item["type"] == "mcp"]
    assert listed == ids
    assert len(listed) == len(set(listed)), f"GET 面不得出现同 id 两条：{listed}"


def test_a7_local_duplicate_still_rejected(client, clean_state, fake_dotenv):
    """⑦ 对照：local×local 同 id 仍 400（local 分支的既有去重没有被挪动破坏）。"""
    before = fake_dotenv.read_text(encoding="utf-8")
    resp = client.put(
        "/api/skills",
        json={"packs": [_local_pack("local:x", "X"), _local_pack("local:x", "X2")]},
    )
    assert resp.status_code == 400, resp.text
    assert "技能 id 重复：local:x" in resp.json()["detail"]
    assert fake_dotenv.read_text(encoding="utf-8") == before


def test_a8_missing_command_400_text_unchanged(client, clean_state, fake_dotenv):
    """⑧ 对照：无重复的整表里，stdio 缺命令的 400 文案与零落盘照旧。"""
    before = fake_dotenv.read_text(encoding="utf-8")
    resp = client.put("/api/skills", json={"packs": [_mcp_pack("mcp:x", "X", command=()) ]})
    assert resp.status_code == 400, resp.text
    assert "「X」stdio 需要启动命令" in resp.json()["detail"]
    assert fake_dotenv.read_text(encoding="utf-8") == before


# --- 机制腿（证明「为什么必须归一化后才去重」，改前改后同值）---------------------


def test_a9_dump_layer_keeps_two_rows_with_the_same_normalized_id():
    """⑨ 机制：`normalize_mcp_pack` + `dump_packs` **本身不去重** —— 两条别名的原始 id
    （`foo` / `mcp:foo`）归一后都是 `mcp:foo`，照样落两行。这解释了缺陷为什么必须在**路由层**
    用归一化 id 去重（本腿改前改后同值，不承担判别力；判别力见腿 ①②③）。
    """
    mcp = [
        skills_spec.normalize_mcp_pack(_mcp_pack("foo", "A")),
        skills_spec.normalize_mcp_pack(_mcp_pack("mcp:foo", "B")),
    ]
    assert [pack["id"] for pack in mcp] == ["mcp:foo", "mcp:foo"]
    raw = json.loads(skills_spec.dump_packs(mcp, []))
    assert [row["id"] for row in raw] == ["mcp:foo", "mcp:foo"]
