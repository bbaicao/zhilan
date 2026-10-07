"""OPT4.7：三个确定性写竞争窗口与两个真实凭证正常对照。

仅在 tmp_path 操作真实文件/junction；覆盖限定窗口，不宣称消灭任意外部 TOCTOU。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess

import pytest

from app.agent.tools import _confirm, _write, _write_paths, workspace_write
from app.core.config import settings


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(root))
    monkeypatch.setattr(settings, "WORKSPACE_CONFIRM_SECRET", "opt4-test-only-secret")
    monkeypatch.setattr(settings, "WEB_ENABLE", False)
    monkeypatch.setattr(_write, "resolve_backup_dir", lambda: tmp_path / "backups")
    _confirm.reset_used_for_tests()
    try:
        yield root
    finally:
        _confirm.reset_used_for_tests()


def _junction(link: Path, target: Path) -> None:
    if os.name != "nt":
        pytest.skip("本判据要求 Windows 真实 junction")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
    )
    if result.returncode != 0:
        pytest.skip("当前环境无法构造 Windows junction")
    assert link.is_junction(), "mklink 返回成功但产物不是 junction"
    assert os.path.normcase(os.path.realpath(link)) == os.path.normcase(
        os.path.realpath(target)
    ), "junction 未真实指向控制目录"


def _remove_junction(link: Path) -> None:
    # 只拆本例创建的链接点；不递归，不碰链接目标。
    if link.is_junction():
        os.rmdir(link)


def _write_at(root: Path, rel: str, *, create: bool):
    return _write.write_workspace_file(
        root=root, rel_path=rel, data=b"NEW", create=create, snapshot=False
    )


def test_create_competing_file_before_publish_is_not_overwritten(
    isolated_workspace, monkeypatch
):
    root = isolated_workspace
    target = root / "note.txt"
    original_write = Path.write_bytes
    injected = []

    def write_tmp_then_compete(path, data):
        result = original_write(path, data)
        if path.parent == root and path.name.startswith(".tmp-") and not injected:
            injected.append(path)
            original_write(target, b"COMPETITOR")
        return result

    monkeypatch.setattr(Path, "write_bytes", write_tmp_then_compete)
    with pytest.raises((OSError, _write.WriteError)):
        _write_at(root, "note.txt", create=True)
    assert len(injected) == 1, "竞争必须发生在真实临时文件写完、发布之前"
    assert target.read_bytes() == b"COMPETITOR"
    assert not list(root.glob(".tmp-*"))


def test_existing_parent_swapped_after_resolve_is_rejected(
    isolated_workspace, tmp_path, monkeypatch
):
    root = isolated_workspace
    parent = root / "parent"
    parent.mkdir()
    (parent / "note.txt").write_bytes(b"ORIGINAL")
    saved = root / "saved"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.txt").write_bytes(b"OUTSIDE")
    original_resolve = _write_paths.resolve_write_target
    injected = []

    def resolve_then_swap(rel, root_=None):
        verdict = original_resolve(rel, root_)
        if rel == "parent/note.txt" and not injected:
            assert isinstance(verdict, _write_paths.WriteTarget)
            parent.rename(saved)
            _junction(parent, outside)
            injected.append(True)
        return verdict

    monkeypatch.setattr(_write_paths, "resolve_write_target", resolve_then_swap)
    try:
        with pytest.raises(_write.WriteError):
            _write_at(root, "parent/note.txt", create=False)
        assert injected == [True], "必须在真实 resolve 完成之后替换父目录"
        assert (outside / "note.txt").read_bytes() == b"OUTSIDE"
        assert (saved / "note.txt").read_bytes() == b"ORIGINAL"
        assert sorted(p.name for p in outside.iterdir()) == ["note.txt"]
    finally:
        _remove_junction(parent)


def test_new_parent_swapped_after_mkdir_is_rejected(
    isolated_workspace, tmp_path, monkeypatch
):
    root = isolated_workspace
    parent = root / "new"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"OUTSIDE")
    original_mkdir = Path.mkdir
    injected = []

    def mkdir_then_swap(path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if path == parent and not injected:
            assert path.is_dir(), "先完成真实 mkdir，再演练竞争"
            os.rmdir(parent)
            _junction(parent, outside)
            injected.append(True)
        return result

    monkeypatch.setattr(Path, "mkdir", mkdir_then_swap)
    try:
        with pytest.raises(_write.WriteError):
            _write_at(root, "new/note.txt", create=True)
        assert injected == [True], "必须在真实 mkdir 完成之后替换父目录"
        assert (outside / "sentinel.txt").read_bytes() == b"OUTSIDE"
        assert sorted(p.name for p in outside.iterdir()) == ["sentinel.txt"]
    finally:
        _remove_junction(parent)


def _confirmed_write(root: Path, *, create: bool):
    content = "NORMAL"
    token = _confirm.issue_write_confirm(
        root=root, rel_path="note.txt", content=content.encode("utf-8")
    )
    spec = workspace_write.build_spec()
    args = workspace_write.WorkspaceWriteArgs(
        path="note.txt", content=content, create=create, confirm_token=token
    )

    async def run():
        first = await spec.handler(args)
        second = await spec.handler(args)
        return first, second

    first, second = asyncio.run(run())
    assert first.ok is True, first.text
    assert (root / "note.txt").read_bytes() == b"NORMAL"
    assert second.ok is False and "replayed" in second.text
    assert _confirm._USED == {token}
    assert not list(root.glob(".tmp-*"))
    return first


def test_normal_create_keeps_real_credential_gate(isolated_workspace):
    root = isolated_workspace
    assert not (root / "note.txt").exists()
    _confirmed_write(root, create=True)


def test_published_create_stays_successful_when_tmp_cleanup_fails(
    isolated_workspace, monkeypatch, caplog
):
    root = isolated_workspace
    original = Path.unlink
    blocked = []

    def unlink(path, *args, **kwargs):
        if path.name.startswith(".tmp-") and (root / "note.txt").exists() and not blocked:
            blocked.append(path)
            raise PermissionError("OPT4 synthetic cleanup sharing error")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    token = _confirm.issue_write_confirm(root=root, rel_path="note.txt", content=b"NORMAL")
    args = workspace_write.WorkspaceWriteArgs(path="note.txt", content="NORMAL", create=True, confirm_token=token)
    spec = workspace_write.build_spec()
    first = asyncio.run(spec.handler(args))
    assert blocked and first.ok, "已发布的文件不能因清理失败而被报告为写入失败"
    assert (root / "note.txt").read_bytes() == b"NORMAL"
    assert asyncio.run(spec.handler(args)).ok is False and _confirm._USED == {token}
    assert "临时文件清理失败" in caplog.text
    for path in blocked:
        original(path, missing_ok=True)


def test_normal_overwrite_keeps_real_credential_gate_and_snapshot(
    isolated_workspace, tmp_path
):
    root = isolated_workspace
    (root / "note.txt").write_bytes(b"ORIGINAL")
    observation = _confirmed_write(root, create=False)
    snapshot_id = observation.write_meta["write_snapshot_id"]
    assert snapshot_id
    snapshot = tmp_path / "backups" / snapshot_id
    assert snapshot.is_file()
    assert snapshot.read_bytes() == b"ORIGINAL"
