# -*- coding: utf-8 -*-
"""回归检查：folder_scan 不得跟进 junction（自环有界 + 不越出用户选择目录）。

缺陷 = `回归修复`（P2）：`backend/app/pipeline/folder_scan.py:84` 的
`for dirpath, dirnames, filenames in os.walk(root_path)` 没有任何链接判定。Windows junction 的
`os.path.islink` **恒 False**（本机实测见 `test_junction_capability_selfcheck`），而 `os.walk` 的
`followlinks` 只看 `islink` ⇒ **默认的 `followlinks=False` 照样跟进 junction**：用户选中一个含自环
junction 的目录时，扫描会一直向内递归（自然终止点取决于绝对路径长度，与运行期锚点
`files 128 / truncated False` / 实验田 `files 200 / truncated True` 是**同形态不同终止点**），
真树文件被挤出 200 上限；含指向目录外的 junction 时，**目录外的文件会被列进待导入清单**。

修法（拍板 #15 = A）：**不跟进链接类目录项**（junction / 符号链接），命中即记进**既有** `skipped`
（`FolderSkip(path, name, reason)` 形状不变、⛔ 不新增响应字段）；递归与非递归两条腿都判。

本载体 6 例：① 能力自证 + 机制复现 · ② 自环有界（打红）· ③ 不越界（打红）·
④ 记进既有 skipped + 三个数据结构形状不变 · ⑤ 非递归腿同样不跟进（打红）· ⑥ 回归护栏（普通子目录）。

⛔ 本条目只动 `backend/app/pipeline/folder_scan.py`：`MAX_FOLDER_FILES=200` / `max_bytes` /
隐藏目录过滤 / 排序口径逐字不动；`backend/app/api/document_routes.py` 不动（响应模型不变）。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import app.pipeline.folder_scan as folder_scan_mod
from app.pipeline.folder_scan import FolderScan, FolderSkip


def _make_junction(link: Path, target: Path) -> bool:
    """建 junction（`mklink /J`，不需管理员）。返回是否**真的**建成。"""
    if link.exists() or link.is_symlink():
        return False
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    return link.exists() and os.path.isjunction(link)


def _remove_junction(link: Path) -> None:
    """拆 junction：`os.rmdir` 只删链点、不碰目标（⛔ 绝不用 rmtree，它会跟进 junction 删目标）。"""
    try:
        if link.exists() or link.is_symlink():
            os.rmdir(link)
    except OSError:
        pass


@dataclass
class _Lab:
    root: Path
    outside: Path


@pytest.fixture
def lab(tmp_path: Path) -> _Lab:
    """实验田：`root` 下真树 2 目录 + 3 个可导入文件，外加 `loop`（自环→root）与 `escape`（→root 外）。"""
    root = tmp_path / "root"
    (root / "real1").mkdir(parents=True)
    (root / "real2").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "top.md").write_text("根", encoding="utf-8")
    (root / "real1" / "a.md").write_text("甲", encoding="utf-8")
    (root / "real2" / "b.md").write_text("乙", encoding="utf-8")
    (outside / "escaped.md").write_text("越界", encoding="utf-8")
    loop, escape = root / "loop", root / "escape"
    if not (_make_junction(loop, root) and _make_junction(escape, outside)):
        _remove_junction(loop)
        _remove_junction(escape)
        pytest.skip("本机 junction 不可构造（mklink /J 失败）⇒ 如实降级；能力自证腿仍会硬断言")
    try:
        yield _Lab(root=root, outside=outside)
    finally:
        _remove_junction(loop)
        _remove_junction(escape)


def test_junction_capability_selfcheck(tmp_path: Path) -> None:
    """能力自证 + 机制复现：junction 可构造 · `islink` 恒 False · `os.walk` 三态读数相同。"""
    root = tmp_path / "root"
    root.mkdir()
    (root / "sub").mkdir()
    target = tmp_path / "target"
    (target / "inner").mkdir(parents=True)
    (target / "inner" / "x.md").write_text("x", encoding="utf-8")
    link = root / "link"
    try:
        assert _make_junction(link, target), "junction 构造失败 ⇒ 本载体 junction 腿判别力失效（⛔ 不静默降级）"
        # 机制 ①：islink 对 junction 恒 False ⇒ 靠 followlinks=False 拦不住
        assert os.path.islink(link) is False
        assert os.path.isjunction(link) is True
        # 机制 ②：默认 / followlinks=False / followlinks=True 三态读数逐字相同 = junction 一律被跟进
        counts: dict[object, int] = {}
        for follow in (None, False, True):
            kwargs = {} if follow is None else {"followlinks": follow}
            counts[follow] = len(list(os.walk(root, **kwargs)))
        assert len(set(counts.values())) == 1, f"三态读数不同（本机预期同读数）：{counts}"
        assert counts[None] >= 4, f"junction 未被跟进（读数过低，本腿失去意义）：{counts}"
    finally:
        _remove_junction(link)


def test_selfloop_junction_is_not_followed(lab: _Lab) -> None:
    """自环 junction ⇒ 扫描有界：只列真树 3 个文件，链点不进结果、不重复递归。"""
    scan = folder_scan_mod.scan_folder(str(lab.root))
    assert sorted(item.name for item in scan.files) == ["a.md", "b.md", "top.md"]
    assert all("loop" not in Path(item.path).parts for item in scan.files)
    assert scan.truncated is False


def test_escape_junction_never_leaves_root(lab: _Lab) -> None:
    """越界 junction ⇒ 目录外文件不得出现在结果里（逐条按 realpath 复判）。"""
    scan = folder_scan_mod.scan_folder(str(lab.root))
    real_root = os.path.realpath(lab.root)
    real_outside = os.path.realpath(lab.outside)
    assert not any(item.name == "escaped.md" for item in scan.files), "目录外文件被列进待导入清单"
    for item in scan.files:
        real = os.path.realpath(item.path)
        assert not real.startswith(real_outside + os.sep), f"越界命中（落在 root 外）：{item.path}"
        assert real.startswith(real_root + os.sep), f"越界命中：{item.path}"


def test_junctions_are_recorded_in_existing_skipped_shape(lab: _Lab) -> None:
    """命中一律记进**既有** skipped（形状不变、不新增响应字段，拍板 #15 = A）。"""
    scan = folder_scan_mod.scan_folder(str(lab.root))
    reasons = {item.name: item.reason for item in scan.skipped}
    assert set(reasons) == {"loop", "escape"}, reasons
    assert all("链接" in reason for reason in reasons.values()), reasons
    assert {Path(item.path).name for item in scan.skipped} == {"loop", "escape"}
    # 形状护栏：三个数据结构逐字不变
    assert set(FolderSkip.__dataclass_fields__) == {"path", "name", "reason"}
    assert set(FolderScan.__dataclass_fields__) == {"path", "files", "skipped", "truncated"}
    assert scan.truncated is False


def test_non_recursive_leg_also_skips_junctions(lab: _Lab) -> None:
    """非递归支路（`iterdir`）同样不得跟进 junction，且同样记进 skipped。"""
    scan = folder_scan_mod.scan_folder(str(lab.root), recursive=False)
    assert [item.name for item in scan.files] == ["top.md"]
    reasons = {item.name: item.reason for item in scan.skipped}
    assert set(reasons) == {"loop", "escape"}, reasons
    assert all("链接" in reason for reason in reasons.values()), reasons


def test_plain_subdirectory_is_still_scanned(tmp_path: Path) -> None:
    """回归护栏：普通子目录照旧递归、无跳过项（不需要 junction 能力，任何机器都真跑）。"""
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "inner.md").write_text("里", encoding="utf-8")
    scan = folder_scan_mod.scan_folder(str(root))
    assert [item.name for item in scan.files] == ["inner.md"]
    assert scan.skipped == []
    assert scan.truncated is False
