"""compose 与基础设施回归（回归修复 / 回归修复）。

**为什么要有这些用例**：阶段 0 的「三服务 healthy」证据曾被**别的 compose 项目**
（老仓库，ASCII 路径）冒名顶替——v2 自己的 `docker-compose.yml` 在中文路径下根本跑不起来
（`project name must not be empty`），却没有任何用例发现。

这些用例是**纯静态核对**（读 `docker-compose.yml` / `.env`），不依赖 Docker 在跑，
所以能进常规单测集。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]  # backend/tests/x.py -> 仓库根
COMPOSE = REPO_ROOT / "docker-compose.yml"
ENV_FILE = REPO_ROOT / ".env"


@pytest.fixture(scope="module")
def compose_text() -> str:
    return COMPOSE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 回归修复：项目名必须显式声明且为 ASCII
# --------------------------------------------------------------------------


def test_compose_declares_explicit_project_name(compose_text):
    """顶层必须有 ``name:``——中文目录名下不写它就报 project name must not be empty。"""
    assert re.search(r"^name:\s*\S+", compose_text, re.M), (
        "docker-compose.yml 缺少顶层 name:。目录名一旦 slug 后为空（例如中文目录名），"
        "compose 从目录名推导项目名会得到空串并直接失败（回归修复）。"
    )


def test_compose_project_name_is_ascii(compose_text):
    """项目名必须是纯 ASCII（Docker 项目名不允许非 ASCII）。"""
    match = re.search(r"^name:\s*(\S+)", compose_text, re.M)
    assert match, "缺少 name:"
    name = match.group(1)
    assert name.isascii(), f"项目名含非 ASCII 字符：{name!r}"
    assert re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name), f"项目名不符合 Docker 规范：{name!r}"


# --------------------------------------------------------------------------
# 回归修复：compose 库名必须与 .env / config 一致
# --------------------------------------------------------------------------


def test_compose_database_matches_env(compose_text):
    """compose 的 ``MYSQL_DATABASE`` 必须与 ``.env`` 的 ``MYSQL_DATABASE`` 一致。

    不一致的后果：compose 起得来也只建错库，alembic 与 lifespan 迁移仍连不上。
    """
    if not ENV_FILE.is_file():
        pytest.skip(".env 不存在（不入库，CI 上可能没有）")

    env_db = None
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("MYSQL_DATABASE="):
            env_db = line.split("=", 1)[1].strip()
            break
    assert env_db, ".env 里没有 MYSQL_DATABASE"

    match = re.search(r"^\s*MYSQL_DATABASE:\s*(\S+)", compose_text, re.M)
    assert match, "docker-compose.yml 里没有 MYSQL_DATABASE"
    compose_db = match.group(1)

    assert compose_db == env_db, (
        f"库名不一致：compose={compose_db!r} .env={env_db!r}。"
        "compose 会建错库，迁移连不上（回归修复）。"
    )


# --------------------------------------------------------------------------
# 端口契约（阶段 0 验收标准 #1：老端口不变）
# --------------------------------------------------------------------------


def test_compose_ports_unchanged(compose_text):
    """三服务端口是契约：mysql 13306 / redis 16379 / qdrant 16333。"""
    for port in ("13306:3306", "16379:6379", "16333:6333"):
        assert port in compose_text, f"端口映射 {port} 丢失（老端口是契约，不可改）"


def test_qdrant_volume_present(compose_text):
    """Qdrant 必须持久化（卷），否则重启即丢向量。"""
    assert "qdrant_data" in compose_text
    assert "/qdrant/storage" in compose_text
