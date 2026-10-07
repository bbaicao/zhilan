"""端点层端到端：图片经 `POST /api/documents/upload` 的状态码与文案（条目 3.3 / 不变量 4）。

（2026-09-18 由 `tests/pipeline/test_image_ocr.py` 拆出：这两条需要完整 ASGI 栈 + `get_db` 覆盖，
与纯 pipeline 文件「不连 DB、不建 ASGI」的纪律不同。）

**为什么要假 session 而不是真库**：本文件只关心「端点是否拦图片」与「400 的文案」。
`ingest_document` 的顺序是「落盘 → 按路径认领失败行 → 建 pending 行 → **解析** → …」，
其中前几步只用到 `session.add` / `commit` / `get` / `execute` / `refresh`（全部是空操作或「查不到」），
**解析处就抛 `DocumentParseError`**，所以用假 session 就能把「路由 → 端点 → ingest → 解析 →
DocumentParseError → main.py 的 RagError handler → 400 + 中文 detail」这条**真链路**跑完，
既不碰真 MySQL、也不受并发跑测影响。
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.core.config import settings
from app.core.errors import DocumentParseError, RagError
from app.pipeline import mineru
from app.pipeline.ingest import ingest_document
from app.pipeline.parsing import IMAGE_OCR_DISABLED_HINT
from tests.pipeline.conftest import make_png


class _FakeResult:
    """SQLAlchemy `Result` 的最小替身：查不到任何行。"""

    def scalar_one_or_none(self):  # noqa: ANN201
        return None

    def scalars(self):  # noqa: ANN201
        return self

    def all(self):  # noqa: ANN201
        return []


class _FakeSession:
    """只支持「入库前几步」的假会话：写操作全部空转，查询一律「查不到」。"""

    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:  # noqa: ANN001
        self.added.append(obj)

    async def commit(self) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def refresh(self, obj) -> None:  # noqa: ANN001
        # 真库里 refresh 会回填自增 id；这里给个稳定假 id，够 `_to_out` 之类读取
        if getattr(obj, "id", None) is None:
            obj.id = 1

    async def delete(self, obj) -> None:  # noqa: ANN001
        return None

    async def execute(self, *_args, **_kwargs):  # noqa: ANN201
        return _FakeResult()

    async def get(self, *_args, **_kwargs):  # noqa: ANN201
        return None


@pytest_asyncio.fixture
async def client_with_fake_db(monkeypatch):
    """ASGI 异步客户端（`get_db` 覆盖为假会话；不进 lifespan，故不连 Redis/MySQL）。"""
    from httpx import ASGITransport, AsyncClient

    from app.db.session import get_db
    from main import app  # 后端根目录的入口模块（不是 app.main）

    session = _FakeSession()

    async def _override_get_db():
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _isolate_upload_dir(monkeypatch, tmp_path):
    """上传落档重定向到 tmp_path，避免污染仓库（照 `tests/api/test_documents_contract.py` 先例）。"""
    monkeypatch.setattr(type(settings), "upload_path", property(lambda self: tmp_path))
    yield


@pytest.mark.asyncio
async def test_upload_endpoint_returns_400_chinese_for_image_when_ocr_off(monkeypatch, client_with_fake_db):
    """默认档上传 `.png` → **400 + 中文可读错**（不是 500、不是裸异常）。

    为什么要有这条（2026-09-18 补，来自独立复核的 D3「不确定项」）：复核方在真 HTTP 层没能跑通，
    因为它先撞上「解析之前的 DB 去重查询」（`db=None` → AttributeError）。这里把 `get_db` 换成
    假会话后，真链路一路走到解析并抛错 ⇒ 能实测到 400 与 detail 文案。

    同时钉住契约口径（统筹 §4.2 不变量 4）：上传端点的状态码与错误语义不变——
    阶段 3 对图片只改了 detail 文案，不改 400 / 409 / 413 的分工。
    """
    monkeypatch.setattr(settings, "MINERU_ENABLE", False)  # 默认档
    monkeypatch.setattr(settings, "OCR_IMAGE_ENABLE", True)

    response = await client_with_fake_db.post(
        "/api/documents/upload", files={"file": ("照片.png", make_png(), "image/png")}
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert isinstance(detail, str) and detail  # detail 是**非空字符串**（前端直接展示）
    assert detail == IMAGE_OCR_DISABLED_HINT


@pytest.mark.asyncio
async def test_upload_endpoint_does_not_filter_image_suffix(monkeypatch, client_with_fake_db):
    """端点**不做**后缀拦截：开关齐备时图片能一路走到 `ingest_document` 的解析阶段（条目 3.3 规格 4）。

    断言手法：把 `mineru.parse_image` 换成「被调用即记录、返回空文本」的桩——返回空文本会让
    `_parse_image` 抛 `IMAGE_EMPTY_HINT`（正常业务路径），端点转 400。**只要 `parsed` 非空，
    就证明请求到达了解析层**，而不是被端点按后缀拒掉。
    """
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    monkeypatch.setattr(settings, "OCR_IMAGE_ENABLE", True)
    monkeypatch.setattr(mineru, "available", lambda: (True, "MinerU 可用"))

    parsed: list[int] = []

    def _spy_parse_image(data: bytes):
        parsed.append(len(data))
        return "", []  # 空文本 → 走「图片未识别到文字内容。」的可读错路径

    monkeypatch.setattr(mineru, "parse_image", _spy_parse_image)

    response = await client_with_fake_db.post(
        "/api/documents/upload", files={"file": ("照片.png", make_png(), "image/png")}
    )

    assert parsed == [len(make_png())], "请求应到达解析层（否则端点在后缀处就拦了图片）"
    assert response.status_code == 400  # 解析抛出的是 RagError 子类 → 400（非 500）


def test_image_ocr_errors_are_rag_errors_so_handler_maps_to_400():
    """链路前提：图片 OCR 的错误类型必须是 `RagError` 子类，才能被 `main.py` 的 handler 转 400。"""
    assert issubclass(DocumentParseError, RagError)
    assert hasattr(ingest_document, "__call__")
