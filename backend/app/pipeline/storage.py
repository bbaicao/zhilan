"""条目 2.1：上传文件落盘策略（纯函数 + 文件系统，不碰库、不碰网络）。

职责只有一个：把上传的字节安全、幂等地落到磁盘某个绝对路径，并能在重试时
把那个路径解析回一个存在的文件。

设计要点（统筹 §3.6「禁止整文件搬运」落地之处）：
- v2 **不把整个源文件塞进知识库**；只在 ``data/uploads``（``settings.upload_path``）
  留一份物理副本，作为「失败可重试」的回放源——解析/切块失败后能直接读它重跑，
  不必让用户重新上传同一个文件；
- 文件名只取 ``basename``，Windows 非法字符换 ``_``，并统一限长（≤ ``_NAME_MAX``，优先保扩展名）⇒ 防目录穿越、非法文件名与超长单段；
- 常规落盘名 = ``<sha256(content)[:8]>-<safe name>``：
  * **内容指纹定名 → 同内容重复上传复用同一路径（幂等，不重复写盘）**；
  * 复用前核对完整摘要；短摘要同名冲突改用完整摘要，保留两份；
- 写入用临时文件原子发布，并发占用也不能覆盖另一份留档；
- 任何 ``OSError`` → ``RagError``（只带 ``strerror`` / 异常类名的中性原因；完整原文进
  日志），不让绝对路径与临时名透到前端。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from pathlib import Path

from app.core.config import settings
from app.core.errors import RagError

log = logging.getLogger(__name__)

# 文件名里的非法字符：Windows 保留字符（< > : " / \ | ? *）+ C0 控制符。
# ⚠️ 与 ``backend/app/export/markdown.py`` 的 ``_ILLEGAL_RE`` **同表**（替换符 "_"），
# 避免「导出面清洗过、上传面没清洗」两套口径。
_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# 安全名主体最长（给 8 位指纹前缀 + 扩展名留余量）
_MAX_STEM = 180
# 安全名总长上限：落档单段 = 8 位指纹 + "-" + 本值 = 209 < NTFS 单段 255。
_NAME_MAX = 200


def _safe_name(filename: str, *, max_length: int = _NAME_MAX) -> str:
    """文件名安全化：只留 basename，非法字符换 "_"，限长（优先保扩展名）。"""
    raw = Path(filename or "").name
    raw = _ILLEGAL_RE.sub("_", raw)
    if not raw:
        raw = "upload"
    stem = Path(raw).stem
    suffix = Path(raw).suffix
    if len(stem) > _MAX_STEM:
        stem = stem[:_MAX_STEM]
    if len(suffix) > max_length - 1:
        # 扩展名自身超长：截断但保留 ".ext" 形态（⛔ 不丢那个点）
        suffix = suffix[: max_length - 1]
    if len(stem) + len(suffix) > max_length:
        stem = stem[: max_length - len(suffix)]
    return f"{stem}{suffix}"


def save_upload(filename: str, data: bytes, *, upload_dir: Path | None = None) -> Path:
    """把上传字节落盘，返回落档绝对路径。

    同内容重复上传 → 直接复用已存在的落档路径（不重复写盘）；不同内容同名
    文件 → 不同指纹前缀共存。写入经临时文件 + 原子发布，半截文件不会被当成留档。
    """
    base = Path(upload_dir) if upload_dir is not None else settings.upload_path
    try:
        base.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(data).hexdigest()
        prefix = digest[:8]
        tmp = base / f".tmp-{digest[:8]}-{uuid.uuid4().hex}"
        try:
            tmp.write_bytes(data)
            while True:
                safe = _safe_name(filename, max_length=_NAME_MAX - (len(prefix) - 8))
                dest = base / f"{prefix}-{safe}"
                try:
                    # Windows rename 原子且拒覆盖，也支持没有硬链接的可移动磁盘。
                    (os.rename if os.name == "nt" else os.link)(tmp, dest)
                    return dest
                except FileExistsError:
                    if hashlib.sha256(dest.read_bytes()).hexdigest() == digest:
                        return dest
                    prefix = digest if prefix == digest[:8] else f"{digest}-{uuid.uuid4().hex}"
        finally:
            tmp.unlink(missing_ok=True)
    except OSError as exc:
        # ⛔ 不把 exc 原文回显给客户端：``str(OSError)`` 里带盘符绝对路径与 ``.tmp-``
        #   临时名（会经 main.py 的通用 handler 进 400 响应体）。中性原因给用户，
        #   完整原文只进日志（排查仍可定位）。
        log.warning("文件落盘失败：%s", exc)
        reason = getattr(exc, "strerror", None) or exc.__class__.__name__
        raise RagError(f"文件落盘失败：{reason}") from exc


def resolve_stored(path: str | Path) -> Path:
    """把留档路径转绝对路径；文件丢失 → RagError（中文可读）。

    相对路径按 ``settings.upload_path`` 解析；``retry_document`` 在重读留档时用它。
    """
    p = Path(path)
    if not p.is_absolute():
        p = settings.upload_path / p
    p = p.resolve()
    if not p.exists():
        raise RagError("留档文件已丢失，请重新上传。")
    return p
