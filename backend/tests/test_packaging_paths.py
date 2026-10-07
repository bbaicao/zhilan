"""阶段 6 · 条目 6.2：冻结态路径解析的回归测试。

**为什么必须有这个文件**：`_roots()` / `backend_dir()` / `log_file()` 是「开发态必须逐字不变、
冻结态必须能命中资源」的双模式函数——正是后续重构最容易悄悄改坏的一类代码
（阶段 1/2 的教训：只靠手工验一次，回归时没人发现）。这里把两种模式的契约都钉死。

纪律：纯路径解析，**不连库、不联网、不起服务**；`backend/main.py` 在 import 期会读配置、
挂路由与静态资源，但都是进程内的只读动作。
"""

from __future__ import annotations

import sys

import pytest

CLIENT_DIR = "client"


@pytest.fixture
def frozen(monkeypatch):
    """进入「冻结态」并在用例结束后还原——避免污染其它用例。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    yield monkeypatch
    monkeypatch.delattr(sys, "frozen", raising=False)


@pytest.fixture
def dev(monkeypatch):
    """显式回到开发态（`getattr(sys, "frozen", False)` 为假）。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    yield monkeypatch


def _bundle(tmp_path, *, under_internal: bool, with_resources: bool = True):
    """造一个「打包后布局」，返回 (exe 路径, 资源根)。"""
    install = tmp_path / "install"
    root = install / "_internal" if under_internal else install
    root.mkdir(parents=True, exist_ok=True)
    if with_resources:
        (root / "frontend" / "dist" / "assets").mkdir(parents=True, exist_ok=True)
        (root / "frontend" / "dist" / "index.html").write_text("<html>bundled</html>", encoding="utf-8")
        (root / "alembic").mkdir(exist_ok=True)
        (root / "alembic.ini").write_text("[alembic]\n", encoding="utf-8")
    exe = install / "zhilan.exe"
    exe.write_bytes(b"")
    return exe, root


# ---------------------------------------------------------------------------
# backend/main.py
# ---------------------------------------------------------------------------
def test_main_dev_paths_match_original_expressions():
    """开发态必须与改造前的原始表达式**逐字一致**（这是 6.2 的核心验收标准）。"""
    import main
    from pathlib import Path

    here = Path(main.__file__).resolve()
    assert main.frontend_dist() == here.parents[1] / "frontend" / "dist"
    res, backend = main._roots()
    assert backend / "alembic.ini" == here.parent / "alembic.ini"
    assert backend / "alembic" == here.parent / "alembic"
    assert res == here.parents[1]


def test_main_dev_resources_actually_exist():
    """开发态的资源必须真的找到——否则界面会 503、迁移会失败。"""
    import main

    res, backend = main._roots()
    assert (main.frontend_dist() / "index.html").is_file()
    assert (backend / "alembic.ini").is_file()
    assert (backend / "alembic").is_dir()


@pytest.mark.parametrize("under_internal", [True, False], ids=["onedir-_internal", "onedir-sibling"])
def test_main_frozen_resolves_bundle_layout(tmp_path, frozen, monkeypatch, under_internal):
    """冻结态的两种常见落地位置都要能命中（PyInstaller 各版本布局不同）。"""
    import main

    exe, root = _bundle(tmp_path, under_internal=under_internal)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(root), raising=False)

    res, backend = main._roots()
    assert (main.frontend_dist() / "index.html").is_file()
    assert (backend / "alembic.ini").is_file()
    assert (backend / "alembic").is_dir()


def test_main_frozen_onefile_uses_meipass(tmp_path, frozen, monkeypatch):
    """一文件模式：资源都在 ``_MEIPASS`` 临时目录。"""
    import main

    exe, root = _bundle(tmp_path, under_internal=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(root), raising=False)

    res, backend = main._roots()
    assert res == root
    assert backend == root


def test_main_frozen_missing_resources_does_not_raise(tmp_path, frozen, monkeypatch):
    """资源缺失也**绝不能抛异常**——窗口仍要能打开，由调用方给可读的 503 提示。"""
    import main

    exe, root = _bundle(tmp_path, under_internal=False, with_resources=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(root), raising=False)

    main._roots()  # 不抛即通过
    assert main.frontend_dist().name == "dist"


# ---------------------------------------------------------------------------
# client/app.py
# ---------------------------------------------------------------------------
def _import_client():
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from client import app

    return app


def test_client_dev_paths_match_original_constants():
    """开发态：`backend_dir()` / `log_file()` 必须等于改造前的 `BACKEND` / `LOG` 常量。"""
    app = _import_client()

    assert app.FROZEN is False
    assert app.backend_dir() == app.ROOT / "backend"
    assert app.log_file() == app.ROOT / "client" / "start.log"
    assert (app.backend_dir() / "alembic.ini").is_file()


def test_client_frozen_anchors_to_executable(tmp_path, frozen, monkeypatch):
    """冻结态：后端目录锚到资源根，日志锚到可执行文件旁边（那里一定可写）。"""
    app = _import_client()

    exe, root = _bundle(tmp_path, under_internal=True)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(root), raising=False)

    assert app.backend_dir() == root
    assert app.log_file() == exe.resolve().parent / "start.log"


def test_client_frozen_without_meipass_falls_back_to_exe_dir(tmp_path, frozen, monkeypatch):
    """拿不到 `_MEIPASS` 时回落到可执行文件目录，而不是抛异常。"""
    app = _import_client()

    exe, _root = _bundle(tmp_path, under_internal=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    assert app.backend_dir() == exe.resolve().parent


def test_client_contract_switches_still_present():
    """外壳的两个契约开关（统筹 §3.3）不得在改造中被删或改义。"""
    app = _import_client()
    import inspect

    assert callable(app.allow_downloads)
    source = inspect.getsource(app.allow_downloads)
    assert 'settings["ALLOW_DOWNLOADS"] = True' in source
    assert "text_select=True" in inspect.getsource(app.main)


# ---------------------------------------------------------------------------
# app/core/env_store.py —— `.env` 与上传目录的共同锚点（条目 6.2b）
# ---------------------------------------------------------------------------
def test_env_store_dev_root_is_repo_root():
    """开发态：ROOT_DIR 必须等于仓库根，配置目标和示例配置也位于根目录。

    这里是「`.env` + 上传目录」的**唯一锚点**：一旦它错了，数据库连接、API Key、
    上传留档会一起失效——比前端 503 严重得多。
    """
    from pathlib import Path

    from app.core import env_store

    assert env_store.ROOT_DIR == Path(env_store.__file__).resolve().parents[3]
    assert env_store.ENV_PATH == env_store.ROOT_DIR / ".env"
    assert (env_store.ROOT_DIR / ".env.example").is_file(), "源码仓库根应提供示例配置"


def test_env_store_frozen_root_is_executable_dir(tmp_path, frozen, monkeypatch):
    """冻结态：锚到**可执行文件所在目录**（安装目录），而不是临时解包目录。

    锚 `_MEIPASS` 会让 `persist_env` 把设置写进进程退出即清理的临时目录，下次启动就丢。
    """
    from app.core import env_store

    exe, root = _bundle(tmp_path, under_internal=True)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(root), raising=False)

    resolved = env_store._resolve_root_dir()
    assert resolved == exe.resolve().parent
    assert resolved != root.resolve(), "不得锚到只读的临时解包目录"


def test_upload_path_is_absolute_under_root():
    """上传留档目录必须以 ROOT_DIR 为基准解析成绝对路径，否则冻结态会写到不可预期的位置。"""
    from app.core.config import Settings
    from app.core.env_store import ROOT_DIR

    path = Settings(_env_file=None, UPLOAD_DIR="data/uploads").upload_path
    assert path.is_absolute(), "upload_path 必须给出绝对路径"
    assert path.name == "uploads"
    assert ROOT_DIR in path.parents, "相对路径必须锚在 ROOT_DIR 之下"
