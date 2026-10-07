"""S5 · 缺陷 210/213/211 判据载体：上传文件名安全化族（``backend/app/pipeline/storage.py``）。

覆盖**三条独立缺陷**（⛔ 不合并叙述：210 = 非法字符面 / 213 = 长度面 / 211 = 报错文案面）：

- ``210``：``_safe_name`` 不过滤 Windows 非法字符（``* ? " < > |`` 与冒号变体）⇒ 该类文件
  永远落不了盘（``save_upload`` 抛 ``RagError [WinError 123]``）。拍板 ``#14`` = **B
  （安全改名后落档，⛔ 不拒收）**。
- ``213``：``_safe_name`` 只截 stem、不截 suffix ⇒ 超长扩展名绕过限长 ⇒ 落档单段 > NTFS 255
  ⇒ 落盘失败。拍板 = ``_safe_name`` 输出 ≤ **200**（落档段长 ≤ 209 < 255）。
- ``211``：``RagError(f"文件落盘失败：{exc}")`` 原样回显 ⇒ 400 响应体把服务器绝对路径
  与 ``.tmp-`` 名给客户端；完整原文应只进日志。

纪律：
- 全部用 ``tmp_path`` 注入 ``upload_dir``，绝不往仓库里写测试文件；
- ⛔ 不为变绿放宽：三条判据只做「等价强化」；
- ``211`` 的 **HTTP 面（400 + detail）未实跑** ⇒ 本文件只钉函数级（执行记录标 UNVERIFIED）；
- 每条自带反向保护腿（既有 ``tests/pipeline/test_storage.py`` 同跑）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from app.core.errors import RagError
from app.pipeline import storage

#: Windows 文件名非法字符（与 ``backend/app/export/markdown.py:39`` 同表的核心项）。
_WIN_ILLEGAL = '*?"<>|:'
#: ``_safe_name`` 输出上限（拍板 213 = 200）。
_NAME_MAX = 200
#: 落档名前缀长度 = sha256 前 8 位 + ``-``。
_DIGEST_PREFIX = 9
#: 盘符 / 绝对路径形态（211 脱敏判据）。
_ABS_RE = re.compile(r"[A-Za-z]:[\\/]")
#: 300 字符扩展名（账本 213 实测触发形态）。
_LONG_SUFFIX = "b" * 300


def _has_illegal(name: str) -> bool:
    return any(ch in _WIN_ILLEGAL for ch in name) or any(ord(ch) < 0x20 for ch in name)


# --------------------------------------------------------------------------
# 210：Windows 非法字符
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("a*b.txt", "a_b.txt"),
        ("a?b.txt", "a_b.txt"),
        ('a"b.txt', "a_b.txt"),
        ("a<b.txt", "a_b.txt"),
        ("a>b.txt", "a_b.txt"),
        ("a|b.txt", "a_b.txt"),
        ("x.y:stream", "x.y_stream"),
    ],
)
def test_bug210_illegal_chars_replaced_by_underscore(raw, expected):
    """六类 Windows 非法字符 + 冒号变体 ⇒ 全部换成 ``_``（⛔ 不是拒收）。"""
    name = storage._safe_name(raw)
    assert name == expected
    assert not _has_illegal(name)


def test_bug210_save_upload_accepts_illegal_name(tmp_path):
    """非法字符名 ⇒ 安全改名后正常落档，且落档名可读。"""
    payload = b"payload"
    p = storage.save_upload("a*b?c.txt", payload, upload_dir=tmp_path)
    assert p.exists()
    assert p.read_bytes() == payload
    assert p.parent == tmp_path
    assert not _has_illegal(p.name)
    assert p.name.endswith(".txt")
    assert len(p.name) <= _DIGEST_PREFIX + _NAME_MAX


def test_bug210_same_content_idempotent_with_illegal_name(tmp_path):
    """同内容两次上传 ⇒ 复用同一路径（指纹定名幂等），磁盘不新增文件。"""
    first = storage.save_upload("q*r.txt", b"same", upload_dir=tmp_path)
    second = storage.save_upload("q*r.txt", b"same", upload_dir=tmp_path)
    assert first == second
    assert len(list(tmp_path.iterdir())) == 1


def test_bug210_path_separators_never_survive():
    """反向保护：``basename`` 先取 + 替换符为 ``_`` ⇒ 目录穿越仍被剥掉。"""
    name = storage._safe_name("..\\..\\secret/x.md")
    assert name == "x.md"
    assert "/" not in name and "\\" not in name


def test_bug210_empty_name_falls_back():
    """反向保护：空名 / 纯路径仍落一个合法名（不抛）。"""
    assert storage._safe_name("") == "upload"
    assert storage._safe_name("plain") == "plain"


# --------------------------------------------------------------------------
# 213：输出限长（优先保扩展名）
# --------------------------------------------------------------------------

def test_bug213_safe_name_output_is_bounded():
    """300 字符扩展名 ⇒ 输出 ≤ 200（原码 = 302，落档段长 311 > 255）。"""
    name = storage._safe_name(f"a.{_LONG_SUFFIX}")
    assert len(name) <= _NAME_MAX
    assert name.startswith("a")
    assert name[1] == "."  # 保留 ``.ext`` 形态


def test_bug213_long_suffix_save_upload_succeeds(tmp_path):
    """超长扩展名 ⇒ 落盘成功（原码抛 RagError，该类文件永久上传不了）。"""
    p = storage.save_upload(f"note.{_LONG_SUFFIX}", b"x", upload_dir=tmp_path)
    assert p.exists()
    assert p.read_bytes() == b"x"
    assert len(p.name) <= _DIGEST_PREFIX + _NAME_MAX


def test_bug213_extension_kept_when_stem_truncated():
    """反向保护：stem 截断时仍保扩展名，且 stem ≤ ``_MAX_STEM``（既有口径）。"""
    name = storage._safe_name("a" * 300 + ".markdown")
    assert name.endswith(".markdown")
    assert len(name) <= _NAME_MAX
    assert len(Path(name).stem) <= storage._MAX_STEM


def test_bug213_total_length_boundary():
    """边界：恰 200 不动、201 截到 200（扩展名优先）。"""
    at_limit = storage._safe_name("s" * 180 + "." + "e" * 19)  # 180 + 20 == 200
    assert len(at_limit) == _NAME_MAX
    assert at_limit.endswith("." + "e" * 19)
    over = storage._safe_name("s" * 180 + "." + "e" * 20)  # 180 + 21 == 201
    assert len(over) == _NAME_MAX
    assert over.endswith("." + "e" * 20)


def test_bug213_oversized_suffix_truncated_keeps_dot_form():
    """扩展名自身超过上限 ⇒ 截断后仍保留 ``.ext`` 形态，总长 == 200。"""
    name = storage._safe_name("a" * 50 + "." + "b" * 400)
    assert len(name) == _NAME_MAX
    assert name[1] == "."
    assert name.startswith("a")


# --------------------------------------------------------------------------
# 211：报错文案脱敏（HTTP 面未实跑 ⇒ 只钉函数级；仍抛 RagError ⇒ main.py 仍映 400）
# --------------------------------------------------------------------------

def _boom_os_replace(src, dst, *args, **kwargs):
    raise OSError(22, "Invalid argument", str(src), str(dst))


def test_bug211_oserror_text_is_sanitized(tmp_path, monkeypatch):
    """注入真实形态的 ``OSError`` ⇒ 文案不含盘符 / 绝对路径 / ``.tmp-``。"""
    monkeypatch.setattr(storage.os, "rename" if storage.os.name == "nt" else "link", _boom_os_replace)
    with pytest.raises(RagError) as ei:
        storage.save_upload("a.md", b"x", upload_dir=tmp_path)
    msg = str(ei.value)
    assert "文件落盘失败" in msg  # 中文可读原因仍在
    assert not _ABS_RE.search(msg)
    assert ".tmp-" not in msg
    assert str(tmp_path) not in msg
    assert msg.strip() != "文件落盘失败："  # ⛔ 不是空原因


def test_bug211_windows_style_strerror_survives(tmp_path, monkeypatch):
    """真实 Windows 形态（``strerror`` 为中文）⇒ 中文原因逐字保留且无路径。"""

    def _boom(src, dst, *args, **kwargs):
        raise OSError(123, "文件名、目录名或卷标语法不正确。", str(src), str(dst))

    monkeypatch.setattr(storage.os, "rename" if storage.os.name == "nt" else "link", _boom)
    with pytest.raises(RagError) as ei:
        storage.save_upload("a.md", b"x", upload_dir=tmp_path)
    msg = str(ei.value)
    assert "文件落盘失败" in msg
    assert "文件名、目录名或卷标语法不正确" in msg
    assert not _ABS_RE.search(msg)
    assert ".tmp-" not in msg


def test_bug211_real_filesystem_error_is_sanitized(tmp_path):
    """真文件系统错误腿（不 monkeypatch）：``upload_dir`` 指向已存在的普通文件。

    ``mkdir(exist_ok=True)`` 对「已存在的非目录」仍抛 ``OSError``（Windows 下为
    ``FileExistsError``）⇒ 与 ``os.replace`` 失败同一条 ``except OSError`` 出口。
    """
    blocker = tmp_path / "not-a-dir.txt"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(RagError) as ei:
        storage.save_upload("a.md", b"x", upload_dir=blocker)
    msg = str(ei.value)
    assert "文件落盘失败" in msg
    assert not _ABS_RE.search(msg)
    assert ".tmp-" not in msg
    assert str(blocker) not in msg


def test_bug211_full_error_text_still_logged(tmp_path, monkeypatch, caplog):
    """可观测性不丢：完整原文（含 ``.tmp-`` 与路径）必须进 WARNING 日志。"""
    monkeypatch.setattr(storage.os, "rename" if storage.os.name == "nt" else "link", _boom_os_replace)
    with caplog.at_level(logging.WARNING, logger="app.pipeline.storage"):
        with pytest.raises(RagError):
            storage.save_upload("a.md", b"x", upload_dir=tmp_path)
    assert ".tmp-" in caplog.text
    assert "Invalid argument" in caplog.text
