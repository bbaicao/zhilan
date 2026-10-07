"""任务清单 39：文件夹导入的扫描规格（纯文件系统，不碰库、不碰网络）。

扫描只回答「哪些文件能进既有入库链路、哪些被跳过、为什么」；真正入库走
``ingest_document``（上传同一条路）。这里不做任何解析——白名单按扩展名判断，
内容合法性交给入库时的清洗与解析（空文件 / 损坏文件会在那里得到可读报错）。
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from app.core.errors import RagError
from app.pipeline.parsing import SUPPORTED_FORMATS_HINT, SUPPORTED_SUFFIXES

# 单批导入的文件数上限：批量生成笔记是逐篇 LLM 调用（几十秒/篇），
# 上千文件的文件夹不该在一次会话里无提示地铺开；超出部分显式 truncated 让前端说明。
MAX_FOLDER_FILES = 200


class FolderScanError(RagError):
    """扫描入口参数不合法（路径不存在 / 不是文件夹）。"""


@dataclass
class FolderFile:
    path: str
    name: str
    size_bytes: int


@dataclass
class FolderSkip:
    path: str
    name: str
    reason: str


@dataclass
class FolderScan:
    path: str
    files: list[FolderFile] = field(default_factory=list)
    skipped: list[FolderSkip] = field(default_factory=list)
    truncated: bool = False


def scan_folder(root: str, recursive: bool = True, max_bytes: int = 20 * 1024 * 1024) -> FolderScan:
    """枚举文件夹里受支持格式的文件；跳过项一律给出中文原因，不静默。

    ``max_bytes`` 由调用方传入（与上传同一限制），避免这里再抄一份常量。

    链接类目录项（符号链接 / Windows junction）**一律不跟进**，命中即记进 ``skipped``：
    ``os.path.islink`` 对 junction 恒 False，靠 ``os.walk(followlinks=False)`` 拦不住，
    自环会一直向内递归、指向目录外的 junction 会把用户没选中的文件列出来。
    """
    root_path = Path(root)
    if not root_path.exists():
        raise FolderScanError(f"文件夹不存在：{root}")
    if not root_path.is_dir():
        raise FolderScanError(f"不是文件夹：{root}")

    scan = FolderScan(path=str(root_path))

    def _classify(file_path: Path) -> None:
        name = file_path.name
        suffix = file_path.suffix.lower()
        if name.startswith("."):
            scan.skipped.append(FolderSkip(str(file_path), name, "隐藏文件，已跳过"))
            return
        if suffix not in SUPPORTED_SUFFIXES:
            scan.skipped.append(
                FolderSkip(str(file_path), name, f"暂不支持 {suffix or '无后缀'} 格式，可导入 {SUPPORTED_FORMATS_HINT}")
            )
            return
        try:
            size = file_path.stat().st_size
        except OSError:
            scan.skipped.append(FolderSkip(str(file_path), name, "无法读取文件信息"))
            return
        if size == 0:
            scan.skipped.append(FolderSkip(str(file_path), name, "空文件"))
            return
        if size > max_bytes:
            scan.skipped.append(FolderSkip(str(file_path), name, f"文件超过 {max_bytes // (1024 * 1024)}MB 上传限制"))
            return
        scan.files.append(FolderFile(str(file_path), name, size))

    def _is_link_entry(entry: Path) -> bool:
        """链接类目录项：符号链接，或 Windows junction。

        ``os.path.islink`` 对 junction **恒 False**（本机实测），而 ``os.walk`` 的 ``followlinks`` 判据
        就是它 ⇒ 默认的 ``followlinks=False`` 拦不住 junction，必须在这里显式识别。
        """
        if entry.is_symlink():
            return True
        isjunction = getattr(os.path, "isjunction", None)
        return bool(isjunction is not None and isjunction(entry))

    def _skip_link(entry: Path) -> None:
        """链接命中一律记进既有 ``skipped``（形状不变、不新增字段）：不跟进、也不静默。"""
        scan.skipped.append(FolderSkip(str(entry), entry.name, "链接（符号链接 / junction），已跳过，不跟进"))

    if recursive:
        for dirpath, dirnames, filenames in os.walk(root_path):
            # 隐藏目录（如 .git）整棵剪掉；os.walk 原地改 dirnames 才生效
            kept: list[str] = []
            for dirname in dirnames:
                if dirname.startswith("."):
                    continue
                child = Path(dirpath) / dirname
                if _is_link_entry(child):
                    # 链接目录不进 dirnames ⇒ os.walk 不跟进（自环与越界都由此截断）
                    _skip_link(child)
                    continue
                kept.append(dirname)
            dirnames[:] = kept
            for filename in filenames:
                child = Path(dirpath) / filename
                if _is_link_entry(child):
                    _skip_link(child)
                    continue
                _classify(child)
    else:
        for entry in root_path.iterdir():
            if _is_link_entry(entry):
                _skip_link(entry)
            elif entry.is_file():
                _classify(entry)

    scan.files.sort(key=lambda item: item.path.lower())
    scan.skipped.sort(key=lambda item: item.path.lower())
    if len(scan.files) > MAX_FOLDER_FILES:
        scan.files = scan.files[:MAX_FOLDER_FILES]
        scan.truncated = True
    if len(scan.skipped) > MAX_FOLDER_FILES:
        scan.skipped = scan.skipped[:MAX_FOLDER_FILES]
        scan.truncated = True
    return scan
