"""异常语义契约：从 main.py 源码核对 + 对不会炸的端点发坏参数看 400/422。

阶段0口径：
- 源码级核对（不依赖跑服务）：异常处理器映射关系
- live 安全探测：对只读端点发坏参数，验证 422/400 中文 detail
- MySQL hint 等只做源码级核对项

阶段1 v2 骨架口径：
- 源码级核对随 CONTRACT_TARGET 切换到 v2 main.py
- 业务语义 live 用例在 501 占位阶段标记 xfail（后续阶段逐域翻绿）

✅ **2026-09-19（阶段 4 · 条目 4.5 收口）：占位时代结束，本文件的两套「临时机制」都已撤除** ——
① `_xfail_if_v2_skeleton` 守卫（`pytest.xfail()` **立即短路** ⇒ 已实装却没摘守卫 = 假绿）
随四域逐域摘掉，**函数定义已删除**，不再有新调用点；
② `test_business_endpoints_501` 的样本表随端点全部实装而变空 ⇒ **整条用例已删除**
（占位语义由 `tests/api/test_placeholder_semantics.py` 以单元级钉住，不依赖起服务）。
至此 02 的 **53 条端点全部实装**、全仓零 `not_implemented()` 调用点。
"""

import ast
import re
from pathlib import Path

import pytest


def _function_body_code(source: str, name: str) -> str:
    """取某个 async 函数的**代码体**（剥掉首个 docstring 节点）——供源码级断言使用。

    为什么不用字符串切片：docstring 里为了解释口径经常提到被测的关键词（例如
    「不查 information_schema」），按整段源码断言会把「解释」误判成「查询」。
    """
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            statements = node.body
            if (
                statements
                and isinstance(statements[0], ast.Expr)
                and isinstance(statements[0].value, ast.Constant)
                and isinstance(statements[0].value.value, str)
            ):
                statements = statements[1:]
            return "\n".join(ast.unparse(item) for item in statements)
    raise AssertionError(f"未找到函数 {name}")


# --- 源码级核对（不依赖跑服务） -------------------------------------------


class TestErrorHandlersSource:
    """从 main.py 源码核对四类异常处理器的映射关系。"""

    @pytest.fixture(scope="class")
    def main_source(self, backend_root: Path) -> str:
        return (backend_root / "main.py").read_text(encoding="utf-8")

    def test_rag_error_handler_400(self, main_source):
        """RagError（一般）→ 400，detail 为 str(exc)。"""
        assert "RagError" in main_source
        # 找到 rag_error_handler 函数
        assert "async def rag_error_handler" in main_source
        # 400 分支
        assert re.search(r"status\s*=\s*400", main_source)

    def test_provider_timeout_504(self, main_source):
        """ProviderTimeout → 504。"""
        assert "ProviderTimeout" in main_source
        assert re.search(r"status\s*=\s*504", main_source)

    def test_provider_auth_401(self, main_source):
        """ProviderAuthError → 401。"""
        assert "ProviderAuthError" in main_source
        assert re.search(r"status\s*=\s*401", main_source)

    def test_mysql_operational_error_503(self, main_source):
        """MySQL OperationalError → 503，detail 为 MYSQL_HINT。"""
        assert "OperationalError" in main_source
        assert "MYSQL_HINT" in main_source
        assert re.search(r"status_code\s*=\s*503", main_source)

    def test_validation_error_422_chinese(self, main_source):
        """RequestValidationError → 422，detail 为固定中文文案。"""
        assert "RequestValidationError" in main_source
        assert "请求参数不正确，请检查后重试" in main_source
        assert re.search(r"status_code\s*=\s*422", main_source)

    def test_http_exception_passthrough(self, main_source):
        """HTTPException → 原码透传，detail 为 exc.detail。"""
        assert "HTTPException" in main_source
        assert "exc.detail" in main_source
        assert "exc.status_code" in main_source

    def test_detail_is_json_detail_key(self, main_source):
        """所有异常响应都用 {"detail": ...} 格式。"""
        # 至少有 4 处 JSONResponse({"detail": ...})
        matches = re.findall(r'JSONResponse\(\s*\{\s*"detail"', main_source)
        assert len(matches) >= 4, f"应有 >=4 处 JSONResponse({{\"detail\": ...}})，实际 {len(matches)}"


class TestMysqlHintSource:
    """MYSQL_HINT 文案核对（源码级，不触发真实 MySQL 错误）。"""

    def test_mysql_hint_text(self, backend_root: Path):
        verify_src = (backend_root / "app" / "db" / "verify.py").read_text(encoding="utf-8")
        assert "MYSQL_HINT" in verify_src
        assert "docker compose up" in verify_src
        assert "无法连接 MySQL" in verify_src


class TestDbUnavailableHintDialects:
    """503 提示的**方言双分支**（阶段 3 · 条目 3.0 交付物 ④ / 待修正事项 B1·B2）。

    改造前只有源码级断言「main.py 里含 ``MYSQL_HINT``」——它既锁不住 SQLite 分支
    是否存在，也锁不住「MySQL 分支逐字不变」。这里补**行为级**双分支断言：
    MySQL 分支与既有常量逐字相等，SQLite 分支走新增桌面文案（且不再让用户跑 docker compose）。
    """

    def test_mysql_branch_is_verbatim(self):
        from app.db.verify import MYSQL_HINT
        from main import database_unavailable_hint

        assert database_unavailable_hint("mysql") == MYSQL_HINT
        assert "无法连接 MySQL" in MYSQL_HINT
        assert "docker compose up" in MYSQL_HINT

    def test_sqlite_branch_is_new_desktop_hint(self):
        from app.db.verify import MYSQL_HINT, SQLITE_HINT
        from main import database_unavailable_hint

        hint = database_unavailable_hint("sqlite")
        assert hint == SQLITE_HINT
        assert hint != MYSQL_HINT
        assert "无法打开本地数据库文件" in hint
        # 桌面语境不得再让用户去跑 docker compose（改造前 503 只有那一句）
        assert "docker compose" not in hint

    def test_default_branch_follows_settings_backend(self):
        from app.core.config import settings
        from app.db.verify import MYSQL_HINT, SQLITE_HINT
        from main import database_unavailable_hint

        expected = SQLITE_HINT if settings.is_sqlite else MYSQL_HINT
        assert database_unavailable_hint() == expected

    def test_sqlite_selfcheck_never_queries_information_schema(self):
        """SQLite 自检分支不得查 ``information_schema``（SQLite 没有这张表）。

        断言取**函数体代码**（ast 剥掉 docstring）而不是整段源码：
        docstring 里为了解释口径会提到 ``information_schema`` 这个词，
        按整段源码断言会把「解释」误判成「查询」。

        路径不用 ``backend_root``：那个夹具随 ``contract_target`` 指向**老仓库**
        （v1 目标），本用例要断言的是**本仓库** v2 的 ``verify.py``。
        """
        verify_path = Path(__file__).resolve().parents[2] / "app" / "db" / "verify.py"
        src = verify_path.read_text(encoding="utf-8")
        assert "SQLITE_HINT" in src
        sqlite_body = _function_body_code(src, "_check_sqlite")
        assert "information_schema" not in sqlite_body
        # 反向守住 MySQL 分支仍是 information_schema + MYSQL_HINT（逐字不变）
        mysql_body = _function_body_code(src, "_check_mysql")
        assert "information_schema" in mysql_body
        assert "MYSQL_HINT" in mysql_body


# --- live 安全探测（对不会改数据的端点发坏参数） ---------------------------


class TestErrorSemanticsLive:
    """对只读端点发坏参数，验证异常码与中文 detail。"""

    def test_chat_empty_message_400(self, http_client, contract_target):
        """POST /api/chat 空 message → 400「请输入问题」。

        阶段 1 条目 1.5 已实装该端点 → **不再 xfail**，本断言真实生效。
        """
        resp = http_client.post("/api/chat", json={"message": ""})
        # 可能 400（未配置模型时 require_chat_ready 先拦）或 400（请输入问题）
        assert resp.status_code == 400
        detail = resp.json().get("detail", "")
        assert isinstance(detail, str)
        assert len(detail) > 0

    def test_chat_stream_empty_message_400(self, http_client, contract_target):
        """POST /api/chat/stream 空 message → 400（已实装，不再 xfail）。"""
        resp = http_client.post("/api/chat/stream", json={"message": ""})
        assert resp.status_code == 400
        detail = resp.json().get("detail", "")
        assert isinstance(detail, str)

    def test_chat_bad_agent_mode_400(self, http_client, contract_target):
        """POST /api/chat 非法 agent_mode → 400（已实装，不再 xfail）。"""
        resp = http_client.post("/api/chat", json={"message": "test", "agent_mode": "invalid_mode_xyz"})
        assert resp.status_code == 400
        detail = resp.json().get("detail", "")
        assert "Agent" in detail or "模式" in detail or "agent" in detail.lower()

    def test_notes_invalid_id_422_or_404(self, http_client):
        """PUT /api/notes/{id} 非法 id → 422（参数校验）或 404（不存在）。"""
        # v2 骨架：路径参数校验已生效，422；实现后允许 404
        resp = http_client.put("/api/notes/not_a_number", json={"title": "x"})
        assert resp.status_code in (422, 404)

    def test_trash_invalid_kind_422(self, http_client, contract_target):
        """DELETE /api/trash/{kind}/{id} 非法 kind → 422（阶段 4 · 4.5.4 已实装 → 不再 xfail）。"""
        resp = http_client.delete("/api/trash/invalid_kind/1")
        assert resp.status_code == 422
        detail = resp.json().get("detail", "")
        # 422 的 detail 是固定中文文案
        assert "请求参数不正确" in detail or isinstance(detail, (list, dict))

    def test_translate_empty_text_400(self, http_client, contract_target):
        """POST /api/translate 空 text → 400（阶段 4 · 4.5.3 已实装 → **不再 xfail**）。"""
        resp = http_client.post("/api/translate", json={"text": "", "target": "en"})
        assert resp.status_code == 400
        detail = resp.json().get("detail", "")
        assert isinstance(detail, str)

    def test_translate_bad_target_400(self, http_client, contract_target):
        """POST /api/translate 非法 target → 400（已实装 → 不再 xfail）。"""
        resp = http_client.post("/api/translate", json={"text": "hello", "target": "fr"})
        assert resp.status_code == 400

    def test_settings_put_unknown_provider_400(self, http_client, contract_target):
        """PUT /api/settings 未知厂商 → 400（已实装 → 不再 xfail）。

        打的是**旧扁平段** ``chat``（``api.ts:1173``，``mode="env"`` 时前端真会发这个字段），
        不是 ``services`` 整表替换那条路。
        """
        resp = http_client.put(
            "/api/settings",
            json={"chat": {"provider": "nonexistent_provider_xyz"}},
        )
        assert resp.status_code == 400

    def test_chat_groups_empty_name_400(self, http_client, contract_target):
        """POST /api/chat-groups 空 name → 400 或 422（已实装 → 不再 xfail）。"""
        resp = http_client.post("/api/chat-groups", json={"name": ""})
        assert resp.status_code in (400, 422)

    def test_skills_mcp_probe_stdio_no_command_400(self, http_client, contract_target):
        """POST /api/skills/mcp/probe stdio 无 command → 400（阶段 4 · 4.5.3 已实装 → 不再 xfail）。"""
        resp = http_client.post(
            "/api/skills/mcp/probe",
            json={"transport": "stdio", "command": []},
        )
        assert resp.status_code == 400

    def test_export_markdown_no_path_422(self, http_client, contract_target):
        """POST /api/export/markdown 缺 path → 422（必填字段校验；已实装 → 不再 xfail）。"""
        resp = http_client.post("/api/export/markdown", json={})
        assert resp.status_code == 422

    def test_error_detail_always_chinese_string(self, http_client, contract_target):
        """抽样验证：错误响应的 detail 是非空字符串（中文可读）。"""
        cases = [
            ("POST", "/api/chat", {"message": ""}),
            ("POST", "/api/translate", {"text": "", "target": "en"}),
            ("DELETE", "/api/trash/bad_kind/1", None),
        ]
        for method, path, body in cases:
            if method == "POST":
                resp = http_client.post(path, json=body or {})
            elif method == "DELETE":
                resp = http_client.delete(path)
            else:
                resp = http_client.request(method, path)
            assert resp.status_code >= 400
            data = resp.json()
            assert "detail" in data, f"{method} {path} 响应缺少 detail 键"
            detail = data["detail"]
            # detail 可以是 str 或 list（422 校验错误列表）
            assert detail, f"{method} {path} detail 为空"


class TestSkeletonContract:
    """v2 骨架阶段特有断言：health 200 + 422 中文（不依赖业务实现）。

    ⚠️ **`test_business_endpoints_501` 已于 2026-09-19 删除**（阶段 4 · 条目 4.5 收口）：
    它的样本表是「此刻尚未实装」的端点，而 02 的 **53 条端点至此全部实装、全仓零 `not_implemented()`
    调用点** ⇒ 样本表为空、用例变成「零断言恒绿」的假绿（正是它 docstring 里警告过的那种）。
    占位语义本身（501 + `detail == "v2 重构中"`、且该字面量全仓只有一处定义）由
    ``tests/api/test_placeholder_semantics.py`` 以**单元级**钉住，不依赖起服务。
    """

    def test_health_200(self, http_client, contract_target):
        if contract_target != "v2":
            pytest.skip("骨架断言仅对 v2 目标生效")
        resp = http_client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "ok"

    def test_validation_422_chinese(self, http_client, contract_target):
        if contract_target != "v2":
            pytest.skip("骨架断言仅对 v2 目标生效")
        resp = http_client.put("/api/notes/not_a_number", json={"title": "x"})
        assert resp.status_code == 422
        assert "请求参数不正确" in resp.json().get("detail", "")

# --- 行为级核对（不依赖跑服务 · 不连库）------------------------------------


class TestErrorHandlerBehavior:
    """**直接调用** ``main.rag_error_handler``，核对 401 / 504 的**方向性**（``回归修复``）。

    为什么另开这一类：上面的 ``TestErrorHandlersSource`` 是**源码文本核对**
    （``assert "ProviderTimeout" in main_source`` + ``re.search(r"status\s*=\s*504", …)``）。
    🔴 **它抓不到把 401 与 504 对调** —— 对调后两条文本断言**各自仍然成立**，而 ``02`` §4 的契约已经错了。
    这属 ``回归修复`` 家族（**判据面空转**：断言形状本身没有判别力）。

    本类改走**行为面**：真调处理器、真读 ``status_code`` ⇒ 方向性可判、对调必红。

    ⚠️ **一处口径限定（独立审计方实测后补 · 2026-09-26）**：上面「对调后文本断言仍然成立」这条结论
    **在两种口径下都成立，但成因不同，别混读** ——
    · **默认口径**（``CONTRACT_TARGET`` 未设 ⇒ 默认 ``v1``）：``backend_root`` 指向的其实是**老仓**,
      那 7 条文本腿**压根不看 v2** ⇒ 它们绿**不能**用来说明 v2 的判据强度；
    · **显式 ``CONTRACT_TARGET=v2`` + ``OLD_REPO_BACKEND`` 指向 v2 副本**：文本腿**真读 v2** ⇒
      变异后**仍 7/7 PASSED**（``status = 504`` / ``status = 401`` 两个**字面量各自都还在**，正则照样命中）⇒
      **这才是「判据面空转」的正面实证**。
    ⇒ 结论不变：**只有本类的行为面腿能抓对调**（实测变异 ⇒ 本类 **3 红**、文本腿 **7 绿**）。

    ⚠️ **一条既有夹具性质（非本笔引入 · 实测登记）**：``tests/contract/conftest.py`` 的 ``backend_root``
    **无条件请求** ``old_repo_backend``，后者未配置即 ``pytest.skip`` ⇒
    **``CONTRACT_TARGET=v2`` 且未配老仓路径时，依赖 ``backend_root`` 的用例一律 skip**。
    ⇒ 复跑本文件时请**显式**给 ``CONTRACT_TARGET`` / ``OLD_REPO_BACKEND``，否则读数会被 skip 掩盖。

    ⚠️ **两条如实登记**（回归修复 落地后更正 · 2026-09-29）：
    ① **历史（2026-09-29 前登记）**：``ProviderTimeout`` / ``ProviderAuthError`` 全仓**零生产者**
       （只有定义 + ``main.py`` 的判定）⇒ 这两条分支**当时不可达**；
       🔴 **现生产者**（``回归修复`` 落地）= ``backend/app/generation/answer.py`` 的（下引行号 = 2026-09-29 快照）
       ``_upstream_failure_as_rag_error``（``:728-732`` ``except RagError: raise`` +
       ``:732-746`` ``raise contract_error from exc``；字段形态出口 ``:763-780``）
       ⇒ 504/401 在**同步** ``POST /api/chat`` 路径**已可达**；
       ⚠️ 但 SSE ``POST /chat/stream`` 在响应开始后 401/504 **仍结构性不可达**（流内失败由
       ``回归修复`` 的 error 帧承载）⇒ 「不可达」如今**只对 SSE 入口**成立。
    ② 但 ``02`` 契约**明文声明**了它们（``:231-232``）⇒ **保留分支、不删**（删 = 违铁律 5「对外契约不变」）。
    ⇒ 本类的作用 = 把**已声明**的契约方向钉住（504/401 在 154 前不可达、154 后同步路径已可达），
    且本类行为腿**直调** ``main.rag_error_handler``（不经 ``answer.py``）⇒ **不受 154 影响**。
    """

    @staticmethod
    async def _call(exc):
        import main as main_mod
        from starlette.requests import Request

        request = Request({"type": "http", "method": "GET", "path": "/probe", "headers": []})
        return await main_mod.rag_error_handler(request, exc)

    def test_provider_timeout_is_504_not_401(self):
        """``ProviderTimeout`` → **504**（⛔ 不是 401）：``02`` §4 表逐字。"""
        import asyncio

        from app.core.errors import ProviderTimeout

        resp = asyncio.run(self._call(ProviderTimeout("模型响应超时")))
        assert resp.status_code == 504, (
            f"ProviderTimeout 应映射 504（02 §4），实得 {resp.status_code}。"
            " 把 401/504 对调会让本条变红 —— 而源码文本断言不会。"
        )

    def test_provider_auth_is_401_not_504(self):
        """``ProviderAuthError`` → **401**（⛔ 不是 504）。"""
        import asyncio

        from app.core.errors import ProviderAuthError

        resp = asyncio.run(self._call(ProviderAuthError("模型服务认证失败")))
        assert resp.status_code == 401, (
            f"ProviderAuthError 应映射 401（02 §4），实得 {resp.status_code}。"
        )

    def test_generic_rag_error_is_400(self):
        """一般 ``RagError`` → **400**（``02`` §4 第一行）。"""
        import asyncio

        from app.core.errors import RagError

        resp = asyncio.run(self._call(RagError("一般错误")))
        assert resp.status_code == 400

    def test_detail_is_str_of_exception(self):
        """detail **逐字** ``str(exc)``（``02`` §4「同响」列 + ``:237``「一律中文、用户可读」）。"""
        import asyncio

        from app.core.errors import ProviderAuthError

        resp = asyncio.run(self._call(ProviderAuthError("认证失败或限流")))
        assert resp.status_code == 401
        assert "认证失败或限流" in resp.body.decode("utf-8")


# --- S6 · 188：OperationalError handler 的 HTTP 面（503 + 中文 hint）--------


class TestDbUnavailableHandlerHttp:
    """**走真 HTTP 路径**核对 OperationalError handler：503 + {"detail": <hint>}。

    为什么另开一类：TestDbUnavailableHintDialects（:111-164）只**直调**
    database_unavailable_hint(...)，没经过 handler —— handler 的注册、状态码
    （503，不是 500 / 不是 200）与响应体形状（{"detail": …}）此前**没有行为面判据**。

    本类不依赖 http_client / backend_root（那两个夹具在 CONTRACT_TARGET=v2
    且未配 OLD_REPO_BACKEND 时一律 skip ⇒ 判据会假绿）：from main import app
    取**真品** handler，挂到干净 FastAPI 的探针路由上，经 SyncClient
    （真 httpx.ASGITransport）读 HTTP 读数。

    判别力：handler 的 status_code 改成 500 或别的值 / handler 被摘掉 ⇒ 本类必红；
    把 database_unavailable_hint 的 mode.strip().lower() 删掉 ⇒ " SQLite "
    与大小写分支腿必红（会退回 MYSQL_HINT）；常量接错 ⇒ 逐字腿必红。
    """

    @staticmethod
    def _client():
        from fastapi import APIRouter
        from main import app as main_app
        from sqlalchemy.exc import OperationalError

        from tests.api._sync_client import SyncClient, build_app

        handler = None
        for exc_type, func in main_app.exception_handlers.items():
            if isinstance(exc_type, type) and issubclass(exc_type, OperationalError):
                handler = func
                break
        assert handler is not None, "main.app 上没有注册 OperationalError handler"

        router = APIRouter()

        @router.get("/probe-db-down")
        async def _probe_db_down():  # pragma: no cover —— 必然抛
            raise OperationalError("SELECT 1", {}, Exception("S6-188 探针"))

        app = build_app(router)
        app.add_exception_handler(OperationalError, handler)
        return SyncClient(app)

    def test_operational_error_is_503_with_chinese_detail(self, monkeypatch):
        """MySQL 形态：503 + detail 逐字 MYSQL_HINT（不是 500、不是英文）。"""
        from app.core.config import settings
        from app.db.verify import MYSQL_HINT

        monkeypatch.setattr(settings, "DB_BACKEND", "mysql")
        resp = self._client().get("/api/probe-db-down")

        assert resp.status_code == 503, resp.text
        assert resp.json() == {"detail": MYSQL_HINT}
        assert "无法连接 MySQL" in resp.json()["detail"]

    def test_detail_follows_db_backend_dialect(self, monkeypatch):
        """方言分支：strip().lower() 口径（大小写 / 前后空白 / 空串）。"""
        from app.core.config import settings
        from app.db.verify import MYSQL_HINT, SQLITE_HINT

        client = self._client()
        for raw, expected in (
            (" SQLite ", SQLITE_HINT),
            ("SQLITE", SQLITE_HINT),
            ("sqlite", SQLITE_HINT),
            ("mysql", MYSQL_HINT),
            (" MySQL ", MYSQL_HINT),
            ("", MYSQL_HINT),
        ):
            monkeypatch.setattr(settings, "DB_BACKEND", raw)
            resp = client.get("/api/probe-db-down")
            assert resp.status_code == 503, (raw, resp.text)
            assert resp.json() == {"detail": expected}, raw

    def test_sqlite_branch_is_the_desktop_hint_verbatim(self, monkeypatch):
        """SQLite 形态：detail 逐字 SQLITE_HINT（⛔ 不含 docker compose 那段）。"""
        from app.core.config import settings
        from app.db.verify import MYSQL_HINT, SQLITE_HINT

        monkeypatch.setattr(settings, "DB_BACKEND", "sqlite")
        resp = self._client().get("/api/probe-db-down")

        assert resp.status_code == 503, resp.text
        detail = resp.json()["detail"]
        assert detail == SQLITE_HINT
        assert detail != MYSQL_HINT
        assert detail.startswith("无法打开本地数据库文件")
        assert "docker compose" not in detail

