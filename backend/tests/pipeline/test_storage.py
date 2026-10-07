"""storage.py 纯函数测试：安全化 / 指纹去重 / 原子写 / 丢失报错 / 空字节。

全部用 ``tmp_path`` 注入 ``upload_dir``，绝不往仓库里写测试文件（纪律）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import RagError
from app.pipeline import storage


def test_safe_name_strips_path_traversal():
    """``..\\..\\x.md`` 只取 basename，且不含任何分隔符（防目录穿越）。"""
    name = storage._safe_name("..\\..\\secret/x.md")
    assert name == "x.md"
    assert "\\" not in name and "/" not in name


def test_safe_name_keeps_extension_on_truncation():
    """超长名截断时保留扩展名。"""
    long_stem = "a" * 300
    name = storage._safe_name(f"{long_stem}.markdown")
    assert name.endswith(".markdown")
    assert len(Path(name).stem) <= storage._MAX_STEM


def test_safe_name_no_extension_falls_back():
    """无扩展名也能落一个合法名（不抛）。"""
    name = storage._safe_name("plain")
    assert name == "plain"


def test_save_upload_is_idempotent_for_same_content(tmp_path):
    """同内容 + 同文件名两次上传 → 同一路径（指纹定名，不重复写盘）。"""
    data = b"# hello\n\nworld\n"
    p1 = storage.save_upload("a.md", data, upload_dir=tmp_path)
    p2 = storage.save_upload("a.md", data, upload_dir=tmp_path)
    assert p1 == p2
    assert p1.read_bytes() == data
    # 磁盘上只有一个文件（不残留重复留档）
    assert len(list(tmp_path.glob("*.md"))) == 1


def test_save_upload_different_content_no_overwrite(tmp_path):
    """不同内容同名文件 → 不同指纹前缀共存，不互相覆盖。"""
    p1 = storage.save_upload("same.md", b"one", upload_dir=tmp_path)
    p2 = storage.save_upload("same.md", b"two", upload_dir=tmp_path)
    assert p1 != p2
    assert p1.read_bytes() == b"one"
    assert p2.read_bytes() == b"two"


def test_save_upload_no_tmp_leftover(tmp_path):
    """原子写后临时文件不残留（半截文件不会被当成留档）。"""
    storage.save_upload("x.md", b"payload", upload_dir=tmp_path)
    tmp_files = list(tmp_path.glob(".tmp-*"))
    assert tmp_files == [], f"残留临时文件：{tmp_files}"


def test_resolve_stored_missing_raises(tmp_path):
    """落档丢失 → RagError（中文可读）。"""
    with pytest.raises(RagError, match="留档文件已丢失"):
        storage.resolve_stored(tmp_path / "nope.md")


def test_resolve_stored_absolute(tmp_path):
    """绝对路径能解析回一个存在的文件。"""
    p = storage.save_upload("y.md", b"z", upload_dir=tmp_path)
    assert p.is_absolute()
    assert storage.resolve_stored(str(p)).exists()


def test_empty_bytes_are_written(tmp_path):
    """空字节可落盘（调用方 ingest 负责判空并报「文件内容为空」）。"""
    p = storage.save_upload("empty.md", b"", upload_dir=tmp_path)
    assert p.exists()
    assert p.read_bytes() == b""
