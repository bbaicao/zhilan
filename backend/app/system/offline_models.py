"""设置页的本地离线模型导入；只接受随程序携带的可信清单。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import threading
import zipfile

from app.core.config import settings

MODEL_REPO = "MinerU-4_models_onnx"
MODEL_COUNT = 13
MODEL_BYTES = 858204914
CHUNK_SIZE = 1024 * 1024


class ImportBusy(RuntimeError):
    pass


def model_root() -> Path:
    home = (os.environ.get("MINERU_HOME") or "").strip()
    home = home or (settings.MINERU_MODEL_DIR or "").strip()
    base = Path(home).expanduser() if home else Path.home() / ".mineru"
    # MinerU interprets a relative HOME against the process working directory.
    # Do not resolve links here: assert_regular_path must see every original component.
    return Path(os.path.abspath(base)) / "models" / MODEL_REPO


def load_manifest() -> dict[str, tuple[str, int]]:
    from main import _roots

    resources = _roots()[0]
    path = (
        resources / "app" / "system" / "models-manifest.txt"
        if getattr(sys, "frozen", False)
        else resources / "tools" / "packaging" / "models-manifest.txt"
    )
    result = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([0-9a-fA-F]{64})  ([A-Za-z0-9_./-]+)  ([0-9]+)", line)
        if not match:
            raise ValueError("程序内的模型清单格式不正确，请重新安装完整安装包。")
        digest, name, size = match.groups()
        if any(part in ("", ".", "..") for part in name.split("/")) or name in result:
            raise ValueError("程序内的模型清单包含不安全或重复的路径。")
        result[name] = (digest.lower(), int(size))
    if (
        len(result) != MODEL_COUNT
        or len({name.casefold() for name in result}) != MODEL_COUNT
        or sum(size for _, size in result.values()) != MODEL_BYTES
    ):
        raise ValueError("程序内的模型清单不是本批 13 件官方模型，请重新安装完整安装包。")
    return result


def assert_regular_path(path: Path) -> None:
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"目标路径含目录链接或符号链接，已停止导入：{part}")


def matches_file(path: Path, digest: str, size: int) -> bool:
    if not path.is_file() or path.stat().st_size != size:
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == digest


class OfflineModelImporter:
    def __init__(self, manifest=None, root=None):
        self._manifest = manifest
        self._root = root
        self._lock = threading.Lock()
        self._status = {
            "state": "idle",
            "phase": "",
            "completed_files": 0,
            "total_files": MODEL_COUNT,
            "imported_files": 0,
            "skipped_files": 0,
            "detail": "选择完整的知澜离线模型 ZIP 包后导入。",
            "model_dir": "",
        }

    def snapshot(self) -> dict:
        with self._lock:
            result = dict(self._status)
        root = Path(result["model_dir"]) if result["model_dir"] else self._get_root()
        result["model_dir"] = str(root)
        # ponytail: polling only checks existence/length; SHA verification happens during import.
        try:
            manifest = self._get_manifest()
            assert_regular_path(root)
            present = 0
            for name, (_, size) in manifest.items():
                target = root / name
                assert_regular_path(target)
                if target.is_file() and target.stat().st_size == size:
                    present += 1
            result["present_files"] = present
        except (OSError, ValueError):
            result["present_files"] = 0
        return result

    def _get_manifest(self):
        return self._manifest if self._manifest is not None else load_manifest()

    def _get_root(self):
        return Path(self._root) if self._root is not None else model_root()

    def start(self, raw_path: str) -> dict:
        with self._lock:
            if self._status["state"] == "running":
                raise ImportBusy("正在导入模型，请等待当前导入完成。")
            if not raw_path.strip() or "\x00" in raw_path:
                raise ValueError("请先选择知澜离线模型 ZIP 包。")
            path = Path(raw_path.strip()).expanduser()
            if not path.is_absolute() or path.suffix.lower() != ".zip" or not path.is_file():
                raise ValueError("请选择本机存在的完整 ZIP 文件，或填写它的绝对路径。")
            root = self._get_root()
            self._status.update(
                state="running", phase="checking", completed_files=0,
                imported_files=0, skipped_files=0,
                detail="正在检查离线模型包和已有模型，请稍候。",
                model_dir=str(root),
            )
            initial = dict(self._status)
            # ponytail: one worker per desktop process; multi-process servers need a shared lock.
            thread = threading.Thread(target=self._run, args=(path, root), daemon=True)
            try:
                thread.start()
            except Exception:
                self._status.update(state="failed", detail="无法启动导入任务，请重试。")
                raise
        initial["present_files"] = 0
        return initial

    def _update(self, **values):
        with self._lock:
            self._status.update(values)

    def _run(self, path: Path, root: Path):
        try:
            manifest = self._get_manifest()
            with zipfile.ZipFile(path) as archive:
                prefix = MODEL_REPO + "/"
                directories = {prefix}
                for name in manifest:
                    pieces = name.split("/")
                    directories.update(prefix + "/".join(pieces[:i]) + "/" for i in range(1, len(pieces)))
                entries = {}
                seen = set()
                for entry in archive.infolist():
                    name = entry.filename
                    mode = entry.external_attr >> 16
                    if (
                        name.casefold() in seen
                        or "\\" in name or ":" in name or "\x00" in entry.orig_filename
                        or any(part in (".", "..") for part in name.split("/"))
                        or stat.S_ISLNK(mode)
                        or (entry.external_attr & 0x400)
                        or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))
                        or entry.flag_bits & 1
                    ):
                        raise ValueError("压缩包包含重复、越界或链接路径，已停止导入。")
                    seen.add(name.casefold())
                    if entry.is_dir():
                        if name not in directories:
                            raise ValueError("压缩包包含不属于本批模型的目录，已停止导入。")
                        continue
                    relative = name[len(prefix):] if name.startswith(prefix) else ""
                    if (
                        relative not in manifest
                        or entry.file_size != manifest[relative][1]
                        or stat.S_ISDIR(mode)
                    ):
                        raise ValueError("压缩包包含额外文件、缺失模型或长度不符，已停止导入。")
                    entries[relative] = entry
                if set(entries) != set(manifest):
                    raise ValueError("压缩包不是完整的本批 13 件模型，请选择正确的离线包。")
                self._update(phase="checking-existing", detail="正在校验已有模型，内容不同会停止并保留原文件。")
                missing_size = 0
                # Preflight EVERY existing target before the first write.
                for name, (digest, size) in manifest.items():
                    target = root / name
                    assert_regular_path(target)
                    if target.exists():
                        if not matches_file(target, digest, size):
                            raise ValueError(f"已有文件内容不同，已停止并保留原文件：{name}。请核对版本与来源。")
                    else:
                        missing_size += size
                ancestor = root
                while not ancestor.exists():
                    ancestor = ancestor.parent
                if shutil.disk_usage(ancestor).free < missing_size:
                    raise ValueError("模型目录所在磁盘的剩余空间不足，请腾出空间后重试。")
                self._update(phase="importing", detail="正在逐件导入并校验 SHA256。")
                imported = skipped = 0
                for number, (name, (digest, size)) in enumerate(manifest.items(), 1):
                    target = root / name
                    assert_regular_path(target)
                    if target.exists():
                        if not matches_file(target, digest, size):
                            raise ValueError(f"已有文件在导入期间发生变化，已停止并保留现场：{name}")
                        skipped += 1
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        assert_regular_path(target)
                        checksum = hashlib.sha256()
                        written = 0
                        with archive.open(entries[name]) as source, target.open("xb") as destination:
                            while chunk := source.read(CHUNK_SIZE):
                                written += len(chunk)
                                if written > size:
                                    raise ValueError(f"模型解压长度超过清单上限：{name}")
                                checksum.update(chunk)
                                destination.write(chunk)
                        if written != size or checksum.hexdigest() != digest:
                            raise ValueError(f"模型内容校验失败：{name}。已保留本次写入现场，请核对离线包。")
                        imported += 1
                    self._update(completed_files=number, imported_files=imported, skipped_files=skipped)
            self._update(
                state="succeeded", phase="completed",
                detail=f"{len(manifest)} 件模型全部通过 SHA256 校验；新增 {imported} 件，保留一致文件 {skipped} 件。如已尝试过解析，请重启知澜后使用。",
            )
        except Exception as exc:
            self._update(
                state="failed", phase="failed",
                detail=f"导入失败：{exc}。原文件和本次写入现场均已保留。",
            )


importer = OfflineModelImporter()
