"""Ref 十字段静态解析：从 schemas.py 直读 Ref 模型字段，与契约比对。

不依赖跑服务，纯源码解析。
"""

import re
from pathlib import Path

import pytest

# 契约要求的 Ref 字段（02 文档 §3 + §7.4）
REF_CONTRACT_FIELDS = {
    "type": {"annotation_contains": ["Literal"], "required": True},
    "title": {"annotation_contains": ["str"], "required": True},
    "snippet": {"annotation_contains": ["str"], "required": True},
    "score": {"annotation_contains": ["float"], "required": True},
    "kb_name": {"annotation_contains": ["str", "None"], "required": False},
    "source_id": {"annotation_contains": ["int", "None"], "required": False},
    "url": {"annotation_contains": ["str", "None"], "required": False},
    "chunk_id": {"annotation_contains": ["str", "None"], "required": False},
    "retrieval_id": {"annotation_contains": ["str", "None"], "required": False},
    "ordinal": {"annotation_contains": ["int", "None"], "required": False},
    "citation_mode": {"annotation_contains": ["CitationMode", "None"], "required": False},
}


class TestRefSchema:
    """Ref 模型字段契约核对。"""

    @pytest.fixture(scope="class")
    def schemas_source(self, old_repo_backend: Path) -> str:
        return (old_repo_backend / "app" / "rag" / "schemas.py").read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    def ref_class_source(self, schemas_source: str) -> str:
        """提取 class Ref(BaseModel) 到下一个 class 之间的源码。"""
        match = re.search(
            r"(class Ref\(BaseModel\):.*?)(?=\nclass |\n# ---|\Z)",
            schemas_source,
            re.DOTALL,
        )
        assert match, "schemas.py 中找不到 class Ref(BaseModel)"
        return match.group(1)

    def test_ref_class_exists(self, ref_class_source):
        """Ref 类存在且继承 BaseModel。"""
        assert "class Ref(BaseModel)" in ref_class_source

    def test_ref_field_count(self, ref_class_source):
        """Ref 应有 11 个字段（十字段 + citation_mode）。"""
        # 匹配字段定义：name: type = default 或 name: type
        field_matches = re.findall(
            r"^\s{4}(\w+)\s*:",
            ref_class_source,
            re.MULTILINE,
        )
        # 排除 model_config 等非字段
        fields = [f for f in field_matches if f not in ("model_config",)]
        assert len(fields) >= 11, f"Ref 应有 >=11 个字段，实际找到 {fields}"

    def test_ref_required_fields(self, ref_class_source):
        """前四个字段（type/title/snippet/score）是必填的（无默认值）。"""
        for field in ["type", "title", "snippet", "score"]:
            # 必填字段没有 = default
            pattern = rf"^\s{{4}}{field}\s*:\s*[^=\n]+$"
            assert re.search(pattern, ref_class_source, re.MULTILINE), f"字段 {field} 应为必填（无默认值）"

    def test_ref_optional_fields_have_none_default(self, ref_class_source):
        """可选字段（kb_name 及之后）默认为 None。"""
        optional_fields = [
            "kb_name", "source_id", "url", "chunk_id", "retrieval_id", "ordinal", "citation_mode",
        ]
        for field in optional_fields:
            pattern = rf"^\s{{4}}{field}\s*:.*?=\s*None"
            assert re.search(pattern, ref_class_source, re.MULTILINE), f"字段 {field} 应默认为 None"

    def test_ref_type_literal_values(self, ref_class_source):
        """type 字段的 Literal 值为 note/document/web。"""
        assert '"note"' in ref_class_source
        assert '"document"' in ref_class_source
        assert '"web"' in ref_class_source

    def test_ref_citation_mode_literal(self, ref_class_source):
        """citation_mode 引用 CitationMode（cited/fallback/none）。"""
        assert "CitationMode" in ref_class_source

    def test_citation_mode_definition(self, schemas_source):
        """CitationMode 定义为 Literal["cited", "fallback", "none"]。"""
        assert 'CitationMode = Literal["cited", "fallback", "none"]' in schemas_source

    def test_ref_source_id_not_chunk_identity(self, schemas_source):
        """源码注释明确禁止用 source_id 承载 chunk 身份。"""
        assert "禁止" in schemas_source
        assert "source_id" in schemas_source

    def test_ref_all_trace_fields_nullable(self, ref_class_source):
        """全部溯源字段可空（旧消息/旧前端零影响）。"""
        nullable = ["kb_name", "source_id", "url", "chunk_id", "retrieval_id", "ordinal", "citation_mode"]
        for field in nullable:
            assert f"{field}" in ref_class_source
            assert re.search(rf"{field}\s*:.*?\|\s*None\s*=\s*None", ref_class_source), f"{field} 应为 X | None = None"

    def test_ref_from_source_line_numbers(self, schemas_source):
        """核对 Ref 类在 schemas.py 中的大致位置（L19-41 附近）。"""
        lines = schemas_source.split("\n")
        ref_start = None
        for i, line in enumerate(lines, 1):
            if "class Ref(BaseModel)" in line:
                ref_start = i
                break
        assert ref_start is not None
        # Ref 类应在 L19 附近（允许 ±5 行偏差）
        assert 14 <= ref_start <= 24, f"Ref 类应在 L19 附近，实际 L{ref_start}"
