"""U1 退出检查：三个真实 HTTP 入口共用保真和日期处理，不调用模型。"""

import pytest

from app.core.config import settings
from app.models import Document
from app.pipeline import ingest
from tests.api.test_chat_contract import client  # noqa: F401


@pytest.mark.asyncio
async def test_upload_import_preview_preserve_body_and_date_warnings(client, db, monkeypatch, tmp_path):
    monkeypatch.setattr(type(settings), "upload_path", property(lambda self: tmp_path / "uploads"))
    monkeypatch.setattr(settings, "MINERU_ENABLE", False)

    async def no_vectors(*_args, **_kwargs):
        return False, "U1 入口检查不调用模型"

    monkeypatch.setattr(ingest, "index_chunks", no_vectors)
    for entry in ("upload", "import-file", "clean-preview"):
        for index, created in enumerate(("2026-02-30", "'2026-02-30'", "2026-02-28")):
            body = (
                f"# {entry}-{index}\n\n"
                "金额 1000000000，账号 6222222222222222222。\n"
                "重复业务记录\n重复业务记录\n\n"
                "```python\n    s = 'a  b'\n    total = 1000000000\n```"
            )
            data = ("\ufeff---\ncategory: 研究\ntags: [资料]\n"
                    f"created_at: {created}\n---\n{body}").encode("utf-8")
            filename = f"{entry}-{index}.md"
            if entry == "import-file":
                source = tmp_path / filename
                source.write_bytes(data)
                response = await client.post("/api/documents/import-file", json={"path": str(source)})
            else:
                response = await client.post(
                    f"/api/documents/{entry}",
                    files={"file": (filename, data, "text/markdown")},
                )
            assert response.status_code == 200, (entry, created, response.text)
            result = response.json()
            if entry == "clean-preview":
                assert result["raw_content"] == body
                assert result["clean_content"] == body
                warnings = result["frontmatter"]["warnings"]
                reported_date = result["frontmatter"]["created_at"]
            else:
                # HTTP client 使用独立会话；换事务才能读到其提交，不只清 ORM 缓存。
                await db.rollback()
                document = await db.get(Document, result["id"])
                assert document is not None
                assert document.raw_content == body
                assert document.index_status == "ready"
                report = await client.get(f"/api/documents/{document.id}/clean-report")
                assert report.status_code == 200, report.text
                assert report.json()["clean_content"] == body
                warnings = document.clean_report.get("frontmatter_warnings", [])
                reported_date = document.created_at.isoformat()
            if index < 2:
                assert len(warnings) == 1
                assert ("正文不受影响" if index == 0 else "已忽略") in warnings[0]
                if entry == "clean-preview":
                    assert reported_date is None
            else:
                assert not warnings
                assert reported_date.startswith("2026-02-28")
