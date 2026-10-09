"""知澜 v2 FastAPI 入口（阶段 1 骨架）。"""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.api import router
from app.api.agent_routes import router as agent_routes_router
from app.api.settings_routes import router as settings_router
from app.api.skill_routes import router as skills_router
from app.api.system_routes import router as system_router
from app.core.app_logging import setup_app_logging
from app.core.config import ensure_service_registry, migrate_legacy_rerank, settings
from app.core.errors import ProviderAuthError, ProviderTimeout, RagError
from app.core.kb_identity import (
    KnowledgeBaseMaintenanceRequired,
    ensure_kb_maintenance_complete,
    kb_vector_target_identity,
)
from app.db.redis import close_redis
from app.db.session import engine
from app.db.verify import MYSQL_HINT, SQLITE_HINT
from app.vector import close_vector_store


BUILD_HINT_HTML = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>知澜</title></head>
<body style="font-family:sans-serif;padding:48px;line-height:1.7">
  <h1>知澜</h1>
  <p>请先执行前端构建</p>
  <pre>cd frontend
npm run build</pre>
</body>
</html>
"""


def _roots() -> tuple[Path, Path]:
    """返回 ``(资源根, 后端根)``，兼容开发态与冻结态（阶段 6 · 条目 6.2）。

    **为什么需要它**：两处资源在**不同层级**，不能共用一个基准——
    - `frontend/dist` 在**仓库根**下（`<仓库根>/frontend/dist`）；
    - `alembic.ini` 与 `alembic/` 在**后端目录**下（`<仓库根>/backend/alembic.ini`）。

    打包后 `__file__` 指向 PyInstaller 的临时解包目录，仓库布局不复存在，硬编相对层数必然失效
    （界面 503、迁移失败）。故冻结态下把两者都锚到资源根；一体化布局（把 `alembic/` 也打进资源根）
    是条目 6.3 的打包要求。

    冻结态按候选根依次探测（PyInstaller 一文件/一目录的落地位置不同），
    **找不到也不抛异常**——回落最后一个候选，由调用方的 `is_file()/is_dir()` 给出可读的 503 提示，
    保证窗口仍能打开。
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates: list[Path] = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass))
        candidates += [exe_dir / "_internal", exe_dir]
        for root in candidates:
            if (root / "frontend" / "dist").is_dir() or (root / "alembic").is_dir():
                return root, root
        fallback = Path(meipass) if meipass else exe_dir
        return fallback, fallback
    # 开发态：行为与改造前**逐字一致**（frontend/dist 取仓库根，alembic 取后端目录）
    here = Path(__file__).resolve()
    return here.parents[1], here.parent


def frontend_dist() -> Path:
    return _roots()[0] / "frontend" / "dist"


def run_migrations() -> None:
    config = Config(str(_roots()[1] / "alembic.ini"))
    command.upgrade(config, "head")
    url = make_url(settings.database_dsn)
    sync_engine = create_engine(url.set(drivername=url.drivername.replace("+aiomysql", "+pymysql").replace("+aiosqlite", "")))
    try:
        with sync_engine.connect() as connection:
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
            if revision is not None:
                indexes = inspect(connection).get_indexes("knowledge_bases")
                if not any(index["name"] == "uk_knowledge_bases_path_or_url_key"
                           and index["unique"] and index["column_names"] == ["path_or_url_key"]
                           for index in indexes):
                    raise KnowledgeBaseMaintenanceRequired(
                        f"S6-186 数据库版本为 {revision}，但知识库唯一索引缺失或不一致；请保持停写并恢复约束后再启动。"
                    )
    finally:
        sync_engine.dispose()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 回归修复（批次 1）：应用层日志的文件出口 = <数据根>/logs/zhilan.log。
    # ① 必须排在下面三步迁移**之前**——它们要打印启动信息，而冻结态 console=False 下 print 全丢；
    # ② 装配点就选这里：这是后端进程真正的启动路径（uvicorn.Server 跑 lifespan，
    #    而 client/app.py:162-166 是在壳进程内起 uvicorn），⛔ 不是壳侧 client/app.py::log_file()；
    # ③ 日志出口失败仍 fail-open；知识库维护门独立阻断未完成的数据维护。
    setup_app_logging()
    try:
        expected_vector_target = kb_vector_target_identity(settings.QDRANT_URL or None, settings.qdrant_path)
    except ValueError:
        expected_vector_target = {"mode": "unconfigured", "location": ""}
    ensure_kb_maintenance_complete(settings.database_dsn, expected_vector_target=expected_vector_target)

    # 首次启动把 .env 里的旧扁平模型配置迁移成「API Key 服务」注册表（幂等）
    try:
        migrated = await asyncio.to_thread(ensure_service_registry)
        if migrated:
            print("已把 .env 中的模型配置迁移为设置页的 API Key 服务（MODEL_SERVICES / MODEL_ROLES）")
    except Exception as exc:
        print(f"模型服务注册表迁移失败（不影响旧配置继续使用）：{exc}")

    # 存量 RERANK_* 扁平键 → MODEL_ROLES.rerank 的**一次性**迁移（回归修复）。
    # 排在 ensure_service_registry **之后**：先让注册表各就各位（迁移要复用 embed 那条服务的 id），
    # 再补 rerank 绑定。已迁移过 / 没有遗留键 / 没有 embed 绑定 → 返回空字典，什么都不做（幂等）。
    try:
        rerank_migrated = await asyncio.to_thread(migrate_legacy_rerank)
        if rerank_migrated:
            print("已把 .env 中遗留的 RERANK_MODEL 迁移为 MODEL_ROLES 的 rerank 绑定")
    except Exception as exc:
        print(f"rerank 绑定迁移失败（不影响重排回落 RRF 顺序）：{exc}")

    try:
        await asyncio.to_thread(run_migrations)
    except Exception as exc:
        if getattr(exc, "kb_maintenance_required", False):
            raise
        # 窗口仍需打开，界面用健康检查提示启动 Docker
        print(f"数据库迁移未完成：{exc}")
    # 已在 head 的库不会重跑 0007；维护工具留下的 pending 仍必须阻断启动。
    ensure_kb_maintenance_complete(settings.database_dsn, expected_vector_target=expected_vector_target)
    yield
    await engine.dispose()
    await close_redis()
    # 阶段 3 · 条目 3.0（B6）：退出释放向量库客户端——本地 Qdrant 是目录独占锁，
    # 不释放会让「第二次打开」失败（复用/释放/重开三项验收的落点）。
    close_vector_store()


app = FastAPI(title="知澜", version="0.1.1", lifespan=lifespan)
app.include_router(router, prefix="/api")
app.include_router(agent_routes_router, prefix="/api")
app.include_router(settings_router, prefix="/api")
app.include_router(system_router, prefix="/api")
app.include_router(skills_router, prefix="/api")

_assets = frontend_dist() / "assets"
if _assets.is_dir():
    app.mount("/assets", StaticFiles(directory=_assets), name="frontend-assets")


@app.get("/")
async def index_page():
    index = frontend_dist() / "index.html"
    if index.is_file():
        return FileResponse(index)
    return HTMLResponse(BUILD_HINT_HTML, status_code=503)


@app.exception_handler(RagError)
async def rag_error_handler(_request: Request, exc: RagError) -> JSONResponse:
    if isinstance(exc, ProviderTimeout):
        status = 504
    elif isinstance(exc, ProviderAuthError):
        status = 401
    else:
        status = 400
    return JSONResponse({"detail": str(exc)}, status_code=status)


def database_unavailable_hint(backend: str | None = None) -> str:
    """数据库不可用时的 503 detail，按**方言**选文案（阶段 3 · 条目 3.0 兼容规则 ①②）。

    - MySQL 分支：``MYSQL_HINT`` **逐字不变**——02 异常语义冻结（I1 只加不减），
      文案与老仓库一致，契约测试的源码级断言依赖它；
    - SQLite 分支：``SQLITE_HINT``（**新增独立常量**）——桌面交付没有
      「docker compose up -d mysql redis」这回事，提示必须可用。

    ``backend`` 显式传入时按参数判定（便于契约测试做双分支断言），
    缺省读 ``settings.DB_BACKEND``。
    """
    mode = (backend if backend is not None else settings.DB_BACKEND) or ""
    return SQLITE_HINT if mode.strip().lower() == "sqlite" else MYSQL_HINT


@app.exception_handler(OperationalError)
async def mysql_error_handler(_request: Request, _exc: OperationalError) -> JSONResponse:
    return JSONResponse({"detail": database_unavailable_hint()}, status_code=503)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, _exc: RequestValidationError) -> JSONResponse:
    return JSONResponse({"detail": "请求参数不正确，请检查后重试"}, status_code=422)


@app.exception_handler(HTTPException)
async def http_error_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def unknown_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    logging.getLogger("app.api.errors").error(
        "未处理的服务错误", exc_info=(type(exc), exc, exc.__traceback__)
    )
    return JSONResponse(
        {"detail": "服务处理失败，请稍后重试；如持续出现，请查看应用日志。"},
        status_code=500,
    )
