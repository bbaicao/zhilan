"""U1.2：实际清洗与分块都保护 Markdown 结构，不依赖外部模型。"""

import pytest

from app.pipeline.chunking import chunk_document
from app.pipeline.cleaning import clean
from app.pipeline.ingest import prepare_document


@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_fenced_code_keeps_indentation_literals_and_blank_lines(fence):
    body = (
        f"# 规则\n{fence}python\n"
        "def total():\n    amount = 1000000000\n"
        "\taccount = '6222222222222222222'\n\n\n"
        "    # 这是代码注释\n    return 'a  b\\t----------'\n"
        f"{fence}\n正文。"
    )
    prepared = prepare_document("规则.md", body.encode())
    assert prepared.text == body
    assert "\n".join(c.text for c in prepared.pieces) == body
    assert all(c.heading_path == ("规则",) for c in prepared.pieces)
    assert clean(clean(body).text).text == body


def test_indented_code_quote_and_markdown_table_keep_spaces():
    body = "说明\n\n    if ready:\n        print('a  b')\n\n> 引用  保留\n| 名称 | 金额 |\n| :--- | ---: |\n| a  b | 1000000000 |"
    assert clean(body).text == body
    assert chunk_document(body)[0].text == body


@pytest.mark.parametrize("separator", ["  ", "\t"])
def test_column_separator_is_converted_before_whitespace_normalization(separator):
    body = f"名称{separator}金额{separator}账号\n项目甲{separator}1000000000{separator}6222222222222222222"
    expected = "名称 | 金额 | 账号\n项目甲 | 1000000000 | 6222222222222222222"
    result = prepare_document("表格.txt", body.encode())
    assert result.text == expected
    assert result.pieces[0].text == expected


def test_long_code_is_chunked_with_fences_and_its_real_heading():
    lines = [f"    total_{i} = {i}  # value" for i in range(20)]
    body = "# 算法\n```python\n# 不是文档标题\n" + "\n".join(lines) + "\n```"
    chunks = chunk_document(clean(body).text, size=100, overlap=10)
    assert len(chunks) > 2
    for line in lines:
        assert any(line in c.text for c in chunks), line
    for c in chunks:
        assert c.heading_path == ("算法",)
        assert "```python" in c.text
        assert c.text.endswith("```")


def test_long_table_retains_header_and_complete_rows():
    header = "| 项目 | 金额 |\n| --- | ---: |"
    rows = [f"| 项目{i} | {1000000000 + i} |" for i in range(15)]
    body = "# 账目\n" + header + "\n" + "\n".join(rows)
    chunks = chunk_document(clean(body).text, size=110, overlap=10)
    assert len(chunks) > 2
    for row in rows:
        assert any(row in c.text for c in chunks), row
    for c in chunks:
        if any(row in c.text for row in rows):
            assert header in c.text
        assert c.heading_path == ("账目",)
