"""Redis 垫片的钉子（`回归修复` 收尾，2026-09-18）。

**为什么单独立文件**：桌面交付（阶段 6：SQLite + 内嵌 Qdrant，**不带任何服务**）要求
「没有任何代码依赖真 Redis」。这句话此前只写在注释与证据里，**没有任何用例钉它**——
本机恰好跑着 Redis 容器时，把 `app/db/redis.py` 换回真客户端（`redis.asyncio.Redis`），
既有 health 用例照样全绿。本文件把它变成断言：

1. **源码级（AST）**：`backend/app/**` 里任何模块都不许 import 第三方 `redis`
   ——包括 `importlib.import_module("redis")` / `__import__("redis")` 这类**常量实参**的动态写法；
2. **对象级**：`get_redis_client()` 必须是 `app.db.redis.InMemoryRedis` **身份**（不是「名字像」），
   且在 `REDIS_*` 指向不可达端口时 `ping` / `set` / `get` / `delete` 照常往返。

⚠️ **两条别踩的坑**（都是实测踩出来的）：

- **不要用「`redis` 不在 `sys.modules` 里」当判据**：`portalocker`（Qdrant 本地模式的锁依赖）在自己的
  `__init__` 里做 ``try: from .redis import RedisLock except ImportError: RedisLock = None``——
  只要第三方的 `redis` 包**装在这个环境里**，`import portalocker` 就会把它拉进进程。那是别的库的
  可选集成，与「我们的代码要不要依赖 Redis」无关；拿它当判据只会得到一条本机环境决定成败的假红。
- **不要试图用 patch `socket` 来钉「不出网」**：Windows 的 Proactor 事件循环走 IOCP，
  异步连接**不过** Python 层的 `socket.socket.connect`（本仓 `开发约定` 有这条）。
  所以这里用「把 `REDIS_*` 指到必然不可达的端口，方法仍必须照常返回」来钉——真客户端在那里必然失败。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.core.config import settings
from app.db import redis as redis_mod

REPO_ROOT = Path(__file__).resolve().parents[2]  # backend/tests/x.py -> 仓库根
APP_DIR = REPO_ROOT / "backend" / "app"

# 兜底正则（只在文件无法解析成 AST 时使用；**不作为主判据**——它挡不住动态导入，还会误判文档里的示例）
_FALLBACK_IMPORT = re.compile(r"^\s*(?:import\s+redis\b|from\s+redis(?:\.|\s|import))", re.M)


def _imports_third_party_redis(source: str) -> bool:
    """这段源码是否 import 了第三方 ``redis``（AST 判据，含常量实参的动态导入）。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:  # 解析不了就退回正则（宁可误报，也别漏）
        return bool(_FALLBACK_IMPORT.search(source))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "redis" or a.name.startswith("redis.") for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            # 只认**绝对**导入：`from .redis import X`（同包兄弟模块）不算
            if node.level == 0 and (
                (node.module or "") == "redis" or (node.module or "").startswith("redis.")
            ):
                return True
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in {"import_module", "__import__"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    value = first.value
                    if value == "redis" or value.startswith("redis."):
                        return True
    return False


def test_scanner_catches_real_forms_and_ignores_docs():
    """**自证扫描有效**（变异式）：该抓的写法一个都不能漏，文档/注释里的示例一个都不能误报。

    没有这条，第 1 条钉子可能只是「正则写得像」——被 `importlib.import_module("redis")`
    或反斜杠续行绕过时它照样绿。
    """
    must_catch = [
        "import redis",
        "import redis.asyncio",
        "import redis as r",
        "from redis import client",
        "from redis.asyncio import Redis",
        "from redis import (client)",
        "import importlib; redis = importlib.import_module('redis')",
        "import importlib\nimportlib.import_module('redis.asyncio')",
        "__import__('redis')",
        "x = 1; import redis",
        "import \\\n    redis",
    ]
    for source in must_catch:
        assert _imports_third_party_redis(source), f"扫描漏判：{source!r}"

    must_not_catch = [
        "from app.db import redis",  # 我们自己的同名模块（应用包内）
        "from .redis import RedisLock",  # 同包兄弟模块（portalocker 那种写法）
        '"""文档里的示例：import redis"""',
        "# 注释：from redis import client",
        "src = 'import redis'",  # 字符串常量（不是真导入语句）
        "from app.session import events",
    ]
    for source in must_not_catch:
        assert not _imports_third_party_redis(source), f"扫描误报：{source!r}"


def test_no_app_module_imports_the_third_party_redis_client():
    """`backend/app/**` 一个都不许 import 第三方 `redis`（垫片的全部意义就在这）。"""
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in sorted(APP_DIR.rglob("*.py"))
        if _imports_third_party_redis(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        f"以下模块 import 了第三方 redis 客户端：{offenders}——桌面交付不带 Redis 服务，"
        "契约字段由 app/db/redis.py 的进程内垫片如实提供（回归修复）"
    )


@pytest.mark.asyncio
async def test_redis_client_is_the_in_process_shim(monkeypatch):
    """身份 + 行为：是**我们那个类**的对象，且 `REDIS_*` 不可达时五个方法照常往返。

    身份用 `isinstance`（`app.db.redis.InMemoryRedis`）而不是「类名字符串相等」——
    后者一次纯改名重构就红，而同名继承真客户端的实现反而能过。
    """
    monkeypatch.setattr(settings, "REDIS_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "REDIS_PORT", 1)  # 必然不可达：真客户端在这里必然失败
    monkeypatch.setattr(settings, "REDIS_PASSWORD", "")

    client = redis_mod.get_redis_client()
    assert isinstance(client, redis_mod.InMemoryRedis)
    assert type(client).__module__ == "app.db.redis"

    assert await client.ping() is True
    await client.set("k", "v", ex=60)
    assert await client.get("k") == "v"
    assert await client.delete("k") == 1
    assert await client.get("k") is None
    await client.aclose()
    assert await client.ping() is True, "垫片无外部资源，关闭后仍可继续用（见模块 docstring）"
