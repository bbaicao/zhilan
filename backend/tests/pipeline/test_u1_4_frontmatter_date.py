"""U1.4：坏日期沿用警告口径，正文可用；合法 YAML 日期与 BOM 入口保持可用。"""

from datetime import datetime

import pytest

from app.pipeline.frontmatter import parse_frontmatter
from app.pipeline.ingest import prepare_document


@pytest.mark.parametrize("created", ["2026-02-30", "'2026-02-30'"])
def test_impossible_date_warns_and_preserves_body(created):
    source = (
        "---\ncategory: 研究\ntags: [资料]\n"
        f"created_at: {created}\n---\n正文第一行\n\n正文第二行"
    )
    result = parse_frontmatter(source)

    assert result.present
    assert result.body == "正文第一行\n\n正文第二行"
    assert result.created_at is None
    assert len(result.warnings) == 1
    if created.startswith("'"):
        assert result.category == "研究"
        assert result.tags == ["资料"]
        assert "created_at" in result.warnings[0]
        assert "已忽略" in result.warnings[0]
    else:
        # 裸日期由 YAML 构造器拒绝，整个头沿用既有非法 YAML 语义。
        assert result.category == ""
        assert result.tags == []
        assert "本文件元数据未入库，正文不受影响" in result.warnings[0]


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_valid_yaml_date_prepares_body_with_or_without_bom(bom):
    source = (
        "---\ncategory: 研究\ntags: [资料]\n"
        "created_at: 2026-02-28\n---\n"
        "正文第一行，保留有效日期与文件头元数据。\n\n正文第二行。"
    )
    result = prepare_document("笔记.md", bom + source.encode("utf-8"))

    assert result.category == "研究"
    assert result.tags == ["资料"]
    assert result.created_at == datetime(2026, 2, 28)
    assert result.meta_warnings == []
    assert result.raw == "正文第一行，保留有效日期与文件头元数据。\n\n正文第二行。"
    assert result.pieces
