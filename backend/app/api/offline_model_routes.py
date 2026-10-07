"""仅供同机设置页使用的离线模型导入与原生 ZIP 选择。"""

from __future__ import annotations

import asyncio
import ipaddress
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.api.system_routes import PICK_TIMEOUT, PickerUnavailable
from app.system.offline_models import ImportBusy, importer

router = APIRouter()


def _loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def require_local_request(request: Request) -> None:
    allowed = request.client is not None and _loopback(request.client.host)
    try:
        host = urlsplit("http://" + request.headers.get("host", ""))
        allowed = allowed and _loopback(host.hostname) and not (
            host.username or host.password or host.path or host.query or host.fragment
        )
        host_port = host.port if host.port is not None else (443 if request.url.scheme == "https" else 80)
        origin_header = request.headers.get("origin")
        if origin_header is not None:
            origin = urlsplit(origin_header)
            origin_port = origin.port if origin.port is not None else (443 if origin.scheme == "https" else 80)
            allowed = allowed and (
                origin.scheme == request.url.scheme
                and origin.hostname == host.hostname
                and origin_port == host_port
                and not (origin.username or origin.password or origin.path or origin.query or origin.fragment)
            )
    except ValueError:
        allowed = False
    if not allowed:
        raise HTTPException(status_code=403, detail="离线模型导入仅允许从本机知澜设置页操作。")


class ImportIn(BaseModel):
    path: str


class PickArchiveIn(BaseModel):
    initial: str = ""


@router.get("/settings/offline-models", dependencies=[Depends(require_local_request)])
def offline_models_status() -> dict:
    return importer.snapshot()


@router.post("/settings/offline-models/import", status_code=202, dependencies=[Depends(require_local_request)])
def import_offline_models(payload: ImportIn) -> dict:
    try:
        return importer.start(payload.path)
    except ImportBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _ask_model_archive(initial: str) -> str:
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError as exc:
        raise PickerUnavailable("本机没有文件选择窗口，请手动填写离线模型 ZIP 包的完整路径。") from exc
    try:
        root = tkinter.Tk()
    except Exception as exc:
        raise PickerUnavailable("本机暂时无法打开文件选择窗口，请手动填写离线模型 ZIP 包的完整路径。") from exc
    try:
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        initial_path = Path(initial) if initial else None
        directory = (
            initial_path if initial_path is not None and initial_path.is_dir()
            else initial_path.parent if initial_path is not None else None
        )
        return filedialog.askopenfilename(
            title="选择知澜离线模型包",
            initialdir=str(directory) if directory is not None else None,
            filetypes=[("知澜离线模型包", "*.zip")],
        ) or ""
    finally:
        try:
            root.destroy()
        except Exception:
            pass


@router.post("/system/pick-model-archive", dependencies=[Depends(require_local_request)])
async def pick_model_archive(payload: PickArchiveIn | None = None) -> dict:
    try:
        path = await asyncio.wait_for(
            asyncio.to_thread(_ask_model_archive, payload.initial if payload else ""),
            PICK_TIMEOUT,
        )
    except PickerUnavailable as exc:
        return {"available": False, "path": "", "detail": str(exc)}
    except asyncio.TimeoutError:
        return {"available": False, "path": "", "detail": "选择文件超时，请重试或手动填写 ZIP 包路径。"}
    return {"available": True, "path": path, "detail": ""}
