"""文件夹导入扫描（老仓库 test_folder_import.py 扫描用例对拍）。

import-file / 生成笔记胶水用例依赖 ingest + API，随 2.4 迁入；
本文件只对拍 `scan_folder` 纯文件系统行为。
"""

import pytest

import app.pipeline.folder_scan as folder_scan_mod


def test_scan_supported_and_unsupported(tmp_path):
    (tmp_path / "a.txt").write_text("正文", encoding="utf-8")
    (tmp_path / "b.MD").write_text("# 大写后缀也算", encoding="utf-8")
    (tmp_path / "c.pdf").write_bytes(b"%PDF-1.4 fake")
    (tmp_path / "d.exe").write_bytes(b"MZ")
    (tmp_path / "e.doc").write_bytes(b"old binary")
    result = folder_scan_mod.scan_folder(str(tmp_path))
    names = {item.name for item in result.files}
    assert names == {"a.txt", "b.MD", "c.pdf"}, names
    reasons = {item.name: item.reason for item in result.skipped}
    assert set(reasons) == {"d.exe", "e.doc"}
    assert "不支持" in reasons["d.exe"] and "不支持" in reasons["e.doc"]
    assert not result.truncated


def test_scan_hidden_files_and_dirs(tmp_path):
    (tmp_path / ".hidden.md").write_text("隐藏", encoding="utf-8")
    # ⚠️ 工具升级-3 · 回归检查：`.ini` 已进白名单（配置后缀）⇒ `desktop.ini` **不再是**
    # 「被跳过的隐藏文件」样本，改用仍不在白名单里的隐藏文件（`.exe`）。
    (tmp_path / ".hidden.exe").write_text("x", encoding="utf-8")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config.md").write_text("x", encoding="utf-8")
    sub = tmp_path / "笔记"
    sub.mkdir()
    (sub / "正常.md").write_text("正文", encoding="utf-8")
    result = folder_scan_mod.scan_folder(str(tmp_path))
    assert [item.name for item in result.files] == ["正常.md"]
    reasons = {item.name: item.reason for item in result.skipped}
    assert set(reasons) == {".hidden.md", ".hidden.exe"}
    assert "隐藏" in reasons[".hidden.md"]
    # .git 整棵剪掉，里面的文件连跳过列表都不进
    assert all(".git" not in item.path for item in result.skipped)


def test_scan_empty_and_oversize(tmp_path):
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    (tmp_path / "big.md").write_text("x" * 500, encoding="utf-8")
    (tmp_path / "ok.txt").write_text("正文", encoding="utf-8")
    result = folder_scan_mod.scan_folder(str(tmp_path), max_bytes=100)
    assert [item.name for item in result.files] == ["ok.txt"]
    reasons = {item.name: item.reason for item in result.skipped}
    assert reasons["empty.txt"] == "空文件"
    assert "MB" in reasons["big.md"]


def test_scan_recursive_flag(tmp_path):
    (tmp_path / "root.md").write_text("根", encoding="utf-8")
    sub = tmp_path / "子"
    sub.mkdir()
    (sub / "inner.md").write_text("里", encoding="utf-8")
    flat = folder_scan_mod.scan_folder(str(tmp_path), recursive=False)
    assert [item.name for item in flat.files] == ["root.md"]
    deep = folder_scan_mod.scan_folder(str(tmp_path), recursive=True)
    assert {item.name for item in deep.files} == {"root.md", "inner.md"}
    # 递归时分类回退用的是文件所在文件夹名（API 层职责，这里只验证路径带子目录）
    assert "子" in deep.files[0].path or "子" in deep.files[1].path


def test_scan_truncated(monkeypatch, tmp_path):
    for index in range(3):
        (tmp_path / f"f{index}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(folder_scan_mod, "MAX_FOLDER_FILES", 2)
    result = folder_scan_mod.scan_folder(str(tmp_path))
    assert len(result.files) == 2
    assert result.truncated is True


def test_scan_empty_dir(tmp_path):
    result = folder_scan_mod.scan_folder(str(tmp_path))
    assert result.files == [] and result.skipped == [] and result.truncated is False


def test_scan_bad_paths(tmp_path):
    with pytest.raises(folder_scan_mod.FolderScanError) as exc_info:
        folder_scan_mod.scan_folder(str(tmp_path / "不存在"))
    assert "文件夹不存在" in str(exc_info.value)
    file_path = tmp_path / "a.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(folder_scan_mod.FolderScanError) as exc_info:
        folder_scan_mod.scan_folder(str(file_path))
    assert "不是文件夹" in str(exc_info.value)
