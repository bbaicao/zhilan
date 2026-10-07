"""知澜桌面端打包入口（阶段 6 · 条目 6.3）。

为什么单独一个入口文件，而不是直接拿 `client/__main__.py` 当脚本：
1. 打包需要一条**明确、稳定**的入口（`client/__main__.py` 是 `python -m client` 用的，语义不同）；
2. 提供 `--smoke` 自检：构建后用它验证**真实冻结环境**里的关键 import，
   把「懒加载模块没打进包」这类问题挡在构建期，而不是留到用户首启才炸。

用法：
    zhilan.exe              正常启动桌面窗口
    zhilan.exe --smoke      只做 import 自检并退出（构建脚本调用）
    zhilan.exe --smoke --smoke-out <file>
                            同上，并把结构化判据（frozen / 失败清单 / 三项资源命中）
                            写成 JSON 文件 —— **不依赖 stdout** 的出口（回归修复）：
                            `console=False` 时冻结态 `sys.stdout is None`，print 全部静默丢弃，
                            而构建门禁（build.ps1 L2）要消费的正是这些读数。
"""

from __future__ import annotations

import json
import os
import sys

# --------------------------------------------------------------------------------------
# 冻结态环境默认值（阶段 6 · 条目 6.5 · 裁决 3 的落点）
# --------------------------------------------------------------------------------------
# 为什么必须在这里钉（而不是写进桌面 `.env`）：`app/core/config.py` 的 pydantic 模型是
# `extra="ignore"`——**只有声明过的键**才认。下面这五个是 MinerU 自己直接 `os.getenv` 读的
# **真进程环境变量**，写进 `.env` 会被 pydantic 静默丢弃（连同名属性都不会有）。
# 2026-09-20 隔离 `.env` 探针实测：`MINERU_MODEL_SOURCE` / `HF_HOME` / `MODELSCOPE_CACHE`
# 三个写进 `.env` ⇒ `os.environ` 里是 `None`；而 `MINERU_MODEL_DIR` / `MINERU_DEVICE`
# 是 pydantic 键，写 `.env` 有效，随后由 `pipeline/mineru.py::_apply_mineru_env()` 映射成
# `MINERU_HOME` / `MINERU_DEVICE_MODE`（两个真环境变量）。
#
# 为什么不用「装机器/用户级环境变量」：污染全局、影响其它程序、卸载难回滚。
# 这里一律 `os.environ.setdefault` ⇒ **用户仍可用真环境变量覆盖**，且**只在 `sys.frozen`
# 为真时执行**（开发态逐字不变，由 `backend/tests/test_packaging_launcher_env_6_5.py` 钉住）。
#
# 路径锚点 = `Path(sys.executable).resolve().parent`（阶段 6 · 条目 6.2 定义的「数据根」，
# 也是 `.env` / `data/uploads` / `models` 的共同落点）。
_FROZEN_ENV_LITERALS = {
    # 硬项：不钉 ⇒ 上游 `mineru/model/download.py::_resolve_auto_model_source` 会
    # `requests.get("https://huggingface.co/models")` 再回落 modelscope ⇒ 撞「禁网 / 数据不出本机」。
    "MINERU_MODEL_SOURCE": "local",
    # 目标机假设无 GPU（阶段 6 §十），必须钉死，否则 MinerU 会自己去探测。
    "MINERU_DEVICE_MODE": "cpu",
}
# 值依赖安装目录的键 → 相对路径（`model_dir` 键由 `MINERU_HOME` 自身承担）。
_FROZEN_ENV_PATHS = {
    # MinerU appends "models" to HOME; the settings importer uses the same installation root.
    "MINERU_HOME": (),
    "HF_HOME": ("models", "hf"),
    "MODELSCOPE_CACHE": ("models", "ms"),
}

# 冻结态下这些模块全部是「函数内 import」或「importlib 动态 import」——PyInstaller 的静态分析
# 扫不到，必须由 spec 的 hiddenimports 显式带上。这里逐一列出并当场验证。
_SMOKE_MODULES = (
    # —— 知澜自身（顶层包与延迟导入的入口）——
    "main",                      # backend/main.py：uvicorn 实际加载的 ASGI 应用
    "app.core.config",
    "app.core.env_store",
    "app.db.session",
    "app.pipeline.parsing",
    "app.pipeline.mineru",       # ← 重依赖适配器，全程延迟 import
    "app.api",
    "app.system.offline_models",
    # —— Web 栈（uvicorn 的协议/生命周期实现是动态选取的）——
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    # —— 迁移（alembic 运行期从磁盘读 script_location）——
    "alembic",
    "alembic.config",
    "alembic.command",
    # —— 向量库 ——
    "qdrant_client",
    # —— 桌面壳 ——
    "webview",
    # —— MinerU（阶段 2 的适配器按需 import，最容易被漏掉的一族）——
    "mineru",
    "mineru.parser",
    "mineru.backend.analyze",
    "mineru.model.runtime.device",
)


#: 冻结态必须钉住的五项的键名（顺序固定，便于证据与用例逐条对照）。
FROZEN_ENV_KEYS = (
    "MINERU_MODEL_SOURCE",
    "MINERU_HOME",
    "HF_HOME",
    "MODELSCOPE_CACHE",
    "MINERU_DEVICE_MODE",
)


def _frozen_env_defaults() -> dict[str, str]:
    """冻结态要钉的「键 → 期望值」（**不去读也不去写进程环境**，纯函数，便于用例断言）。

    路径锚点 = `Path(sys.executable).resolve().parent`（数据根）；**不裸用 `__file__` /
    `parents[]` / `sys._MEIPASS`**——否则条目 6.2c 的冻结安全路径闸门会在构建前红灯。
    """
    from pathlib import Path

    root = Path(sys.executable).resolve().parent
    values = dict(_FROZEN_ENV_LITERALS)
    for key, rel in _FROZEN_ENV_PATHS.items():
        parts = (rel,) if isinstance(rel, str) else rel
        values[key] = str(root.joinpath(*parts))
    return values


def _apply_frozen_env_defaults() -> list[str]:
    """冻结态：把 §冻结态环境默认值 逐项 `setdefault` 进进程环境，返回**真正写入**的键。

    - **开发态（`sys.frozen` 为假）一个都不设**，直接返回空列表（逐字不变的那条纪律）；
    - 一律 `setdefault` 语义：**用户显式设过的真环境变量优先**，这里不覆盖；
    - 必须在 `client.app.main()` **之前**调用：`mineru/config.py::ModelConfig.base_dir` 的
      默认值是在 **import 期** 由 `MINERU_HOME` 算出来的常量，之后再设无效。
    """
    if not getattr(sys, "frozen", False):
        return []
    written: list[str] = []
    for key, value in _frozen_env_defaults().items():
        existed = key in os.environ
        os.environ.setdefault(key, value)
        if not existed:
            written.append(key)
    return written


def _smoke(out_path: str | None = None) -> int:
    """在冻结环境里逐个 import 关键模块，任一失败即非零退出。

    ``out_path``（`回归修复`）：把结构化判据写成 JSON 文件，作为**不依赖 stdout** 的出口。
    ⚠️ 文件写不出来 ⇒ **计为失败**（fail-closed）：build.ps1 的 L2 门禁以本文件为判据主源，
    缺了不能当通过——与「宁可保守红灯、不可假绿放行」既有口径一致。
    """
    import importlib

    failures: list[tuple[str, str]] = []
    for name in _SMOKE_MODULES:
        try:
            module = importlib.import_module(name)
            if name == "app.system.offline_models":
                module.load_manifest()
        except Exception as exc:  # noqa: BLE001 —— 自检要报告全部失败项，不要第一个就退出
            failures.append((name, f"{type(exc).__name__}: {exc}"))

    frozen = bool(getattr(sys, "frozen", False))
    print(f"[smoke] frozen={frozen} python={sys.version.split()[0]}")
    print(f"[smoke] 检查 {len(_SMOKE_MODULES)} 个模块，失败 {len(failures)} 个")
    for name, err in failures:
        print(f"[smoke]   FAIL {name} -> {err}")

    # 顺带验证资源定位（条目 6.2 的冻结态路径）确实指向打包后的布局
    res_root: str | None = None
    dist_hit = ini_hit = dir_hit = False
    env_values: dict[str, str | None] = {}
    try:
        from pathlib import Path

        import main as backend_main

        res, backend_root = backend_main._roots()
        res_root = str(res)
        dist_hit = (backend_main.frontend_dist() / "index.html").is_file()
        ini_hit = (backend_root / "alembic.ini").is_file()
        dir_hit = (backend_root / "alembic").is_dir()
        print(f"[smoke] 资源根={res}")
        print(f"[smoke] frontend/dist 命中 index.html: {dist_hit}")
        print(f"[smoke] alembic.ini 命中: {ini_hit}")
        print(f"[smoke] alembic 目录命中: {dir_hit}")
        from app.core import env_store

        print(f"[smoke] 配置根(.env 所在)={env_store.ROOT_DIR}")
        print(f"[smoke] 可执行文件={Path(sys.executable).resolve()}")
        # 条目 6.5 · 裁决 3 的读数出口：五项**真进程环境变量**的实际取值。
        # 判据不是「有没有赋值语句」，而是「跑起来的进程里读到的是什么」——故在这里打印实测值。
        for _key in FROZEN_ENV_KEYS:
            env_values[_key] = os.environ.get(_key)
            print(f"[smoke] env {_key}={env_values[_key]}")
    except Exception as exc:  # noqa: BLE001
        failures.append(("资源定位自检", f"{type(exc).__name__}: {exc}"))
        print(f"[smoke]   FAIL 资源定位自检 -> {exc}")

    result = "fail" if failures else "pass"
    if out_path:
        payload = {
            "result": result,
            "frozen": frozen,
            "python": sys.version.split()[0],
            "modules_total": len(_SMOKE_MODULES),
            "failures_count": len(failures),
            "failures": [{"module": name, "error": err} for name, err in failures],
            "res_root": res_root,
            "dist_hit": dist_hit,
            "ini_hit": ini_hit,
            "dir_hit": dir_hit,
            "env": env_values,
        }
        try:
            from pathlib import Path as _Path

            _Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8", newline="\n") as _fh:
                json.dump(payload, _fh, ensure_ascii=False, indent=2, sort_keys=True)
            print(f"[smoke] 结果 JSON 已写：{out_path}")
        except Exception as exc:  # noqa: BLE001 —— 门禁依赖本文件：写不出来 = fail-closed
            failures.append(("--smoke-out", f"{type(exc).__name__}: {exc}"))
            result = "fail"
            print(f"[smoke]   FAIL --smoke-out -> {exc}")

    if failures:
        print("[smoke] 结果：失败")
        return 1
    print("[smoke] 结果：通过")
    return 0


def _smoke_out_arg(argv: list[str]) -> str | None:
    """取 ``--smoke-out <path>`` 的路径值；给了开关但没给值 ⇒ 返回空串（调用方报用法错误）。"""
    if "--smoke-out" not in argv:
        return None
    idx = argv.index("--smoke-out")
    return argv[idx + 1] if idx + 1 < len(argv) else ""


def main() -> int:
    # 条目 6.5：冻结态先把五项真进程环境变量钉好——**必须在任何 product / client import 之前**
    # （`mineru/config.py` 的模型根默认值是 import 期算的常量）。开发态本调用是空操作。
    _written = _apply_frozen_env_defaults()
    if _written:
        print("[launcher] 冻结态环境默认值已钉：" + ", ".join(_written))
    argv = sys.argv[1:]
    if "--smoke" in argv:
        out = _smoke_out_arg(argv)
        if out == "":
            print("[launcher] --smoke-out 需要一个文件路径参数（例：--smoke-out build\\smoke.json）")
            return 2
        return _smoke(out)
    from client.app import main as desktop_main

    return desktop_main()


if __name__ == "__main__":
    # ⚠️ 冻结态**必须**先调 `multiprocessing.freeze_support()`，且必须在做任何别的事之前。
    #
    # 为什么（条目 6.4 暴露、单点修）：冻结后 `multiprocessing` 的 spawn 子进程命令行是
    # `zhilan.exe --multiprocessing-fork parent_pid=… pipe_handle=…`（`multiprocessing/spawn.py`
    # 在 `sys.frozen` 下就是这么拼的）。PyInstaller 的 bootloader **不认识**这个参数，
    # 于是照常把本入口脚本当 `__main__` 跑一遍 ⇒ 没有这一行的话，子进程会走到 `desktop_main()`，
    # **再开一个窗口 + 再起一套后端**，而父进程在进程池的 `wait(timeout=300)` 上白等 5 分钟。
    #
    # 别指望「加了 PyInstaller 的 `pyi_rth_multiprocessing` 就没事」：那个运行时钩子只是把
    # `multiprocessing.freeze_support` **替换**成能识别 `--multiprocessing-fork` 的实现
    # （`hooks/rthooks/pyi_rth_multiprocessing.py` 末尾只有一行赋值：`… = _freeze_support`），
    # **它自己不会调用**。下面这一行才是触发点。
    #
    # 谁会触发 spawn：只要 PDF 解析走到 `docvortex` 的 PDF 渲染进程池（`ProcessPoolExecutor`，
    # Windows 默认 spawn；basic/standard/advanced 档都会走，**与页数无关**）。
    import multiprocessing

    multiprocessing.freeze_support()
    raise SystemExit(main())
