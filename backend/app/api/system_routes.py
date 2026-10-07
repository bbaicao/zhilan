"""本机系统能力：设置页「选择文件夹」用的原生目录对话框 + include export / usage。

浏览器里拿不到绝对路径（File System Access API 只给目录名），所以选择框放在后端弹：
桌面客户端（pywebview）与本地 `dsh web` 场景下它就是同机的一个系统窗口。
**拿不到对话框时如实返回 `available: false`**，界面退回手填路径，不静默失败
（`frontend/src/api.ts:1243-1251` 的注释就是这条约定：「available = false 表示本机弹不出来
（无图形界面等），界面应退回手填」）。

⚠️ 两处刻意的实现细节：

- **不在模块级 import tkinter**（开工指令包 §6 明文）：`tkinter` 在精简版 Python 里可能不存在，
  模块级 import 会让**整个 main.py 起不来**（本路由是独立挂载的，import 发生在启动路径上）。
  故它在 `_ask_directory` 体内 import，且失败只翻成可读中文。
- **无图形界面时不返回 4xx/500**：契约里这个端点的形状是 `{available, path, detail}`，
  「弹不出来」是**正常结果**而不是错误 —— 回 400 会让冻结前端把可读原因当异常弹窗，
  而不是按设计退回手填。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.export_routes import router as export_routes_router

router = APIRouter()

#: 用户可能开着对话框慢慢找，给足时间；超时如实报错（不静默返回空路径）
PICK_TIMEOUT = 300.0


class PickDirIn(BaseModel):
    """`pickDirectory(initial)`（`api.ts:1246`）。体可缺省（`payload=None` 时按空初值处理）。"""

    initial: str = ""


class PickerUnavailable(RuntimeError):
    """本机没有可用的系统对话框。"""


def _ask_directory(initial: str) -> str:
    """弹系统「选择文件夹」；返回空串表示用户取消。**只在函数体内 import tkinter**。"""
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError as exc:  # 精简版 Python 可能没带 tkinter
        raise PickerUnavailable("本机 Python 没有 tkinter，弹不出选择框，请手动填写路径。") from exc

    try:
        root = tkinter.Tk()
    except Exception as exc:  # noqa: BLE001 —— 无图形环境（例如纯服务端）时 Tk 起不来
        raise PickerUnavailable(f"本机没有可用的图形界面（{exc}），请手动填写路径。") from exc

    try:
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:  # noqa: BLE001 —— 个别 Tk 版本不认 -topmost，不影响主流程
            pass
        return filedialog.askdirectory(
            title="选择知澜数据存放目录",
            initialdir=initial or None,
            mustexist=False,
        ) or ""
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001 —— 销毁失败不该盖住用户的选择结果
            pass


@router.post("/system/pick-directory")
async def pick_directory(payload: PickDirIn | None = None) -> dict:
    """返回 ``{available, path, detail}``；``available=false`` 时界面应退回手填。"""
    initial = (payload.initial if payload is not None else "") or ""
    try:
        path = await asyncio.wait_for(asyncio.to_thread(_ask_directory, initial), PICK_TIMEOUT)
    except PickerUnavailable as exc:
        return {"available": False, "path": "", "detail": str(exc)}
    except asyncio.TimeoutError:
        return {"available": False, "path": "", "detail": "选择目录超时，请重试或手动填写路径。"}
    return {"available": True, "path": path, "detail": ""}


router.include_router(export_routes_router)

from app.api.usage_routes import router as usage_routes_router  # noqa: E402

router.include_router(usage_routes_router)
