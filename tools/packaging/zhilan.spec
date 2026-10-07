# -*- mode: python ; coding: utf-8 -*-
"""知澜桌面端 PyInstaller 打包配置（阶段 6 · 条目 6.3 交付物 2）。

设计要点（逐条对应条目 6.3 规格与 §七 三大纪律）：

1. **onedir，不是 onefile**（§二.2 工程默认）。onefile 每次启动都要把近 1GB 解包到临时
   目录，首启慢到不可接受，且与「数据根必须可写」直接冲突；onedir 的杀软误报率也低得多。
   PyInstaller 6.x 的 onedir 布局：`dist/zhilan/zhilan.exe` + `dist/zhilan/_internal/**`，
   运行期 `sys._MEIPASS` == `<exe目录>/_internal`。

2. **三类锚点落地**（§七 纪律 1）：`datas` 的落点全部是资源根（只读），
   即 `_internal/frontend/dist`、`_internal/alembic`、`_internal/alembic.ini`；
   `backend/main.py::_roots()` 冻结态按 `_MEIPASS → exe_dir/_internal → exe_dir` 依次探测，
   三者都能命中。数据根（`.env` / `data/uploads`）由 exe 目录承担，**不进 datas**。

3. **`alembic/versions/*.py` 必须当 data**：alembic 是**运行期从磁盘**按 script_location
   读版本的，只把它当模块打进 PYZ 是不够的（§七 纪律 5 的坑）。故整个 `backend/alembic/`
   以 `Tree` 形式原样落到资源根的 `alembic/`。

4. **hiddenimports 不手抄**：直接从 `zhilan_launcher.py` 读 `_SMOKE_MODULES`。
   理由——那份元组就是构建后 `--smoke` 的检查清单，手抄一份必然漂移；而漂移的后果是
   「自检清单说不缺、实际缺」，正是本条目要消灭的那类缺陷。见下方 `_load_smoke_modules()`。

5. **excludes 摘 gradio 一族**（§三 可瘦身项，约 400MB，只服务 MinerU 的 WebUI）。
   摘完必须过 `--smoke`——由 `build.ps1` 强制。

6. **产物落仓库根 `dist/`、中间物落 `build/`**（都在 E 盘）。本 spec 通过环境变量
   `ZL_DISTPATH` / `ZL_WORKPATH` 接收，缺省值即仓库根的 `dist` / `build`，与 `.gitignore`
   已忽略的两行对齐。

运行方式（不要手敲 PyInstaller 参数，走脚本）：
    $env:ZL_BUILD_PYTHON = '<构建专用 venv 目录>/Scripts/python.exe'   # 正斜杠即可，Windows 两种都认
    pwsh -File tools/packaging/build.ps1
等价命令（本机路径一律走环境变量，不写死在文件里）：
    $env:PYTHONIOENCODING = 'utf-8'
    & $env:ZL_BUILD_PYTHON -m PyInstaller --noconfirm tools/packaging/zhilan.spec
"""

import importlib.util
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files


# --------------------------------------------------------------------------------------
# 0. 路径基准：spec 文件自身的位置是唯一可靠的锚（不依赖 CWD，也不依赖谁调用了它）
# --------------------------------------------------------------------------------------
# ⚠️ 必须排在文件最前（回归修复）：`REPO_ROOT` 在下面的 0a 节就被用到
# （runtime hook 的落盘目录），定义晚于使用会让「未设 ZL_WORKPATH 时直跑
# pyinstaller zhilan.spec」在文件头部即 NameError。
SPEC_DIR = Path(SPECPATH).resolve()          # <仓库根>/tools/packaging
REPO_ROOT = SPEC_DIR.parents[1]              # <仓库根>
BACKEND_DIR = REPO_ROOT / "backend"


# --------------------------------------------------------------------------------------
# 0a. 冻结态 stdout 编码修正（**必须在 Analysis 之前放进 runtime_hooks**）
# --------------------------------------------------------------------------------------
# 实测（2026-09-18）：冻结 exe 往**管道/重定向**写 stdout 时用的是系统 ANSI 代码页（中文机
# 上是 cp936），而且 `PYTHONIOENCODING=utf-8` 与 `PYTHONUTF8=1` **都不起作用**（PyInstaller
# 引导程序自己初始化解释器，这两个环境变量在冻结态被绕过）。后果是构建日志/证据文件里
# 中文全是替换字符，事后无法核对——与 回归修复 同族，但发生在冻结态而非常规 py 进程。
# 已验证的修法：在 frozen 入口最早期对 `sys.stdout/stderr` 做 `reconfigure(encoding='utf-8')`
# （实测：同一段中文，未修 → GBK 字节；修复后 → 纯 UTF-8，U+FFFD 计数 0）。
# runtime hook 好处有二：不依赖任何外部环境变量；`console=False` 时 stdout 为 None 也已兜住。
_RUNTIME_HOOK_SOURCE = """# -*- coding: utf-8 -*-
# 由 tools/packaging/zhilan.spec 生成——冻结态强制 stdout/stderr 走 UTF-8。
# 详见 spec 内「冻结态 stdout 编码修正」一节与 证据/S6-6.3-打包配置.md。
import sys

for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is None or not hasattr(_stream, "reconfigure"):
        continue
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
"""
_RUNTIME_HOOK_PATH = os.path.join(
    os.environ.get("ZL_WORKPATH") or str(REPO_ROOT / "build"),
    "_zl_runtime_hook_utf8.py",
)
os.makedirs(os.path.dirname(_RUNTIME_HOOK_PATH), exist_ok=True)
with open(_RUNTIME_HOOK_PATH, "w", encoding="utf-8", newline="\n") as _fh:
    _fh.write(_RUNTIME_HOOK_SOURCE)
print(f"[zhilan.spec] 已生成 UTF-8 runtime hook：{_RUNTIME_HOOK_PATH}")

FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"
ALEMBIC_DIR = BACKEND_DIR / "alembic"
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
LAUNCHER = SPEC_DIR / "zhilan_launcher.py"

DIST_PATH = os.environ.get("ZL_DISTPATH") or str(REPO_ROOT / "dist")
WORK_PATH = os.environ.get("ZL_WORKPATH") or str(REPO_ROOT / "build")

# 调试版留控制台（可排障），发布版在 6.5 切 console=False（§二.2 工程默认）。
CONSOLE = os.environ.get("ZL_CONSOLE", "1") not in ("0", "false", "False", "")


# --------------------------------------------------------------------------------------
# 1. 从 launcher 读自检清单（杜绝「spec 与 --smoke 清单漂移」）
# --------------------------------------------------------------------------------------
# 兜底清单：只在下面动态读取失败时使用。**与 launcher._SMOKE_MODULES 必须同步**——
# 任何一处增删都要两处一起改；动态读取成功时这份清单不参与构建，仅作告警对照。
_SMOKE_MODULES_FALLBACK = (
    "main",
    "app.core.config",
    "app.core.env_store",
    "app.db.session",
    "app.pipeline.parsing",
    "app.pipeline.mineru",
    "app.api",
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "alembic",
    "alembic.config",
    "alembic.command",
    "qdrant_client",
    "webview",
    "mineru",
    "mineru.parser",
    "mineru.backend.analyze",
    "mineru.model.runtime.device",
)


def _load_smoke_modules():
    """按文件路径加载 launcher 并取回 `_SMOKE_MODULES`（**不执行启动逻辑**）。

    - `module_from_spec` + `exec_module` 只跑 launcher 的顶层定义；它的 `if __name__ ==
      "__main__"` 块不会触发（exec 模块名不是 `__main__`），所以既不会开窗口也不会跑自检。
    - 同步带一处交叉校验：把读到的清单和兜底清单比一次，只告警、不中断——真正的一致性
      由 `--smoke` 自己保证（它检查的就是 launcher 里那份元组）。
    """
    if not LAUNCHER.is_file():
        raise SystemExit(f"[zhilan.spec] 找不到打包入口脚本：{LAUNCHER}")
    spec = importlib.util.spec_from_file_location("_zhilan_launcher_for_spec", LAUNCHER)
    if spec is None or spec.loader is None:
        raise SystemExit(f"[zhilan.spec] 无法从 {LAUNCHER} 建立模块规格")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    names = getattr(module, "_SMOKE_MODULES", None)
    if not names:
        print(f"[zhilan.spec] 警告：{LAUNCHER} 里读不到 _SMOKE_MODULES，改用兜底清单"
              "（该清单与 launcher._SMOKE_MODULES 必须同步）")
        return list(_SMOKE_MODULES_FALLBACK)
    names = tuple(names)
    drift = set(names) ^ set(_SMOKE_MODULES_FALLBACK)
    if drift:
        print(f"[zhilan.spec] 提示：自检清单与 spec 内兜底清单有差异（以 launcher 为准）："
              f"{sorted(drift)}")
    print(f"[zhilan.spec] 已从 {LAUNCHER.name} 读入 _SMOKE_MODULES：{len(names)} 个")
    return list(names)


HIDDEN_IMPORTS = _load_smoke_modules()

# 清单之外、但冻结态跑起来同样需要的模块（不属自检面，属补充面）：
HIDDEN_IMPORTS += [
    # pywebview 的 Windows 后端是**按平台动态 import** 的，静态分析扫不到。
    "webview.platforms",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    # client/app.py::window_size() 用 tkinter 取屏幕尺寸算窗口大小（失败才回落常量）。
    "tkinter",
    # SQLAlchemy 的 MySQL 方言在 create_async_engine 时 import DBAPI 驱动（懒加载）。
    "aiomysql",
    "pymysql",
    # SQLAlchemy 方言名（`mysql+aiomysql`）→ 模块的动态注册表。
    "sqlalchemy.dialects.mysql",
    "sqlalchemy.dialects.mysql.aiomysql",
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.sqlite.aiosqlite",
    # app/db/verify.py 等处的自省与异步驱动。
    "aiosqlite",
    "greenlet",
    # alembic 运行期要按 script_location 从磁盘加载 versions/*.py（主要靠 datas 落盘）。
    "alembic.script",
    "alembic.runtime.migration",
    "alembic.ddl",
    "alembic.ddl.mysql",
]

# --------------------------------------------------------------------------------------
# 2. datas：资源根（只读）三件套
# --------------------------------------------------------------------------------------
for _required, _kind in ((FRONTEND_DIST, "dir"), (ALEMBIC_DIR, "dir"), (ALEMBIC_INI, "file")):
    if _kind == "dir" and not _required.is_dir():
        raise SystemExit(f"[zhilan.spec] 缺少必需资源目录：{_required}（先执行 `cd frontend && npm run build`）")
    if _kind == "file" and not _required.is_file():
        raise SystemExit(f"[zhilan.spec] 缺少必需资源文件：{_required}")

# ⚠️ `datas` 里**只能放 `(源, 目标目录)` 两元组**（源可以是单个文件、目录或 glob）。
# PyInstaller 6.22.3 的 `Analysis.__init__` 用 `format_binaries_and_datas(datas)` 逐项解包，
# 目录会被递归展开；**Tree 对象不能塞进来**（实测报
# `ValueError: too many values to unpack (expected 2)`）。
# `__pycache__` / `*.pyc` 的排除放在 Analysis 之后做（见下方 `_drop_compiled_artifacts()`）——
# 仓库里的 .pyc 与目标机 Python 无关，带进去只会污染产物；alembic 只按 .py 源码读版本。
datas = [
    (str(FRONTEND_DIST), "frontend/dist"),
    (str(SPEC_DIR / "models-manifest.txt"), "app/system"),
    # ⚠️ alembic/versions/*.py 是 alembic **运行期从磁盘读**的，必须走 data（条目 6.3 规格）。
    (str(ALEMBIC_DIR), "alembic"),
    # ⚠️ 目标目录必须写 "."（资源根自身），**不能写 "alembic.ini"**：
    # `format_binaries_and_datas()` 对单个文件做的是 `join(目标目录, basename)`，
    # 写成 `"alembic.ini"` 会落到 `alembic.ini/alembic.ini`——那样
    # `backend/main.py::run_migrations()` 与 `client/app.py::run_alembic()` 都找不到它。
    # 实测见 证据\回归检查-打包配置.md（构建期自证会直接拦下这种错）。
    (str(ALEMBIC_INI), "."),
]

# --------------------------------------------------------------------------------------
# 2a. datas：文档解析三包的**数据文件**（回归修复，条目 6.6 安装态验收抓到）
# --------------------------------------------------------------------------------------
# ⚠️ **这条是 6.6 在安装态抓到的真缺陷的修复**（不是"补个稳妥"）：
#   交付默认档（`MINERU_ENABLE=true` + `MINERU_BACKEND=basic`）下，MinerU 的解析链要读**包内数据**：
#     · `magika/models/**`（文件类型判定模型）· `magika/config/content_types_kb.min.json`
#     · `docvortex/resources/fonts/**`（含 `DroidSansFallbackFull.ttf` 与 `manifest.json`）
#     · `mineru/resources/**`
#   而 PyInstaller 的模块图**只收 .py**，这些数据一个都不会进产物 ⇒ 安装态下：
#     · 图片：`MinerU 图片 OCR 失败：model dir not found at …\_internal\magika\models\standard_v3_3`
#     · 扫描件 PDF：MinerU 抛错后**静默回落 pypdf** ⇒ 400「该 PDF 未提取到文字，请上传文字版或 txt/md。」
#   （`_internal` 实测**没有** magika / docvortex / mineru 三个包目录；开发态与打包 venv 里都正常。）
#   构建必须携带这些包的非 Python 数据文件；
#   永久回归钉见 `backend/tests/test_packaging_spec_data_files_6_6.py`。
#
# `collect_data_files` 只收**非 .py**（代码仍走模块图），也不收 `.dist-info`；
# 实测三包合计 ≈ 14.9 MB（magika 3.2 MB / docvortex 6.1 MB / mineru 5.7 MB）。
for _doc_parse_pkg in ("magika", "docvortex", "mineru"):
    datas += collect_data_files(_doc_parse_pkg)

# --------------------------------------------------------------------------------------
# 2b. binaries：**不收集 llama.cpp 运行时**（阶段 6 · 条目 6.4 改造，见阶段6/设计说明 §九 Q4）
# --------------------------------------------------------------------------------------
# 历史（条目 6.3）：这里曾有 `binaries = _collect_llama_bin()`，把 `mineru_llama_cpp/bin/` 下
# 的 DLL/EXE（含 CPU 后端 `ggml-cpu.dll`，实测该目录 10 文件 / 70,373,888 B）显式收进产物，
# 原因是 PyInstaller 收不全它们（`collect_dynamic_libs` 只扫包顶层、ggml 后端是运行期按名
# 动态加载的）。那一段**已整段删除**，删的理由如下：
#
#   §九 Q4 定案：交付包**只带许可干净的官方 ONNX 件**（818.45MB / 13 件），**不带**第三方 GGUF、
#   **不带** `mineru_llama_cpp`（该包 METADATA **无 License 字段** ⇒ 没有可依附的许可文本，
#   D3 = 不接受无许可分发风险）。VLM 改为**用户自备**的可选增强（`MINERU_VLM_ENABLE` 默认 `False`）。
#   ⇒ 交付形态的运行时**不需要 llama.cpp**，显式收集它只会把无许可件打进交付包。
#
# ⚠️ **两条独立通路，缺一即假绿**（6.4 开工包 §0.0 裁决 3 / §5 坑 2）：
#   ① 依赖图通路 —— 由下方 `excludes` 里的 `mineru_llama_cpp` 挡住。
#      `mineru/model/vlm/runtime.py` 有 `from mineru_llama_cpp import Engine`，**只加 exclude
#      之前的实测产物里确实有** `_mineru_llama_cpp.cp313-win_amd64.pyd`（1,004,544 B）。
#   ② 显式喂入通路 —— 就是本节：`binaries=` 是**显式**喂给 `Analysis` 的，与依赖图无关，
#      `excludes` **拦不住**它（这是 6.3 那版必须存在的理由，也是 6.4 必须同时删掉的理由）。
#   ⇒ 故 `binaries` 置空，并由下方「5. 构建期反向自证」在 **COLLECT 之后按产物实盘**断言
#     「产物里没有 `mineru_llama_cpp`」——反着写，而不是继续断言「DLL 必须在」。
binaries = []

# --------------------------------------------------------------------------------------
# 3. excludes：可瘦身项
# --------------------------------------------------------------------------------------
excludes = [
    # ⭐ 阶段 6 · 条目 6.4：**交付包不带 llama.cpp 运行时**（§九 Q4 定案）。
    # 该包 METADATA 无 License 字段（无许可文本可依附）⇒ 不进交付包；VLM 由用户自备。
    # 它不是"可选依赖"：`mineru/model/vlm/runtime.py` 里有一句
    # `from mineru_llama_cpp import Engine`，PyInstaller 的模块图**会**把它收进来
    # （实测未加本项时产物里有 `_mineru_llama_cpp.cp313-win_amd64.pyd`）。
    # ⚠️ 与 2b 节是**两条独立通路**：本项挡依赖图，2b 节（已把 `binaries` 置空）挡显式喂入。
    "mineru_llama_cpp",
    # 阶段 6.1 实测：gradio 一族约 400MB，只服务 MinerU 的 WebUI，知澜用不到（§三 可瘦身项）。
    # 摘完必须过 --smoke —— 由 build.ps1 强制，不由本文件自我声明。
    "gradio",
    "hf_gradio",
    "gradio_client",
    # 构建机也确实没装 torch（6.1 的 onnx/cpu 路线），显式列出只为防未来被间接拉入。
    "torch",
    "torchvision",
    "torchaudio",
    # 开发/测试期才用的东西，不该进发布包。
    "pytest",
    "_pytest",
    "IPython",
    "matplotlib",
]

# --------------------------------------------------------------------------------------
# 4. Analysis / PYZ / EXE / COLLECT
# --------------------------------------------------------------------------------------
a = Analysis(  # noqa: F821 —— PyInstaller 在 exec 本文件时注入
    [str(LAUNCHER)],
    pathex=[str(REPO_ROOT), str(BACKEND_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[_RUNTIME_HOOK_PATH],   # 冻结态 stdout 强制 UTF-8（见文件头 0a 节）
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821


def _drop_compiled_artifacts(analysis):
    """把 `__pycache__` / `*.pyc` 从 datas 里剔掉（Analysis 展开目录时会一并收进来）。

    为什么不在 `datas` 里筛：PyInstaller 6.x 的 `datas` 只认两元组、由
    `format_binaries_and_datas()` 展开目录，没有排除入口；而 `analysis.datas` 是一个
    **普通列表**（`build_main.py` 里就是 `self.datas = [...]` 的赋值），
    Analysis 构建完成后在 spec 里过滤它是受支持的做法。
    被剔掉的文件仍然会被 PyInstaller 当作**源码**编译进 PYZ（这才是它该在的地方）。
    """
    before = len(analysis.datas)
    kept, dropped = [], []
    for dest_name, src_name, typecode in analysis.datas:
        parts = dest_name.replace("\\", "/").split("/")
        if "__pycache__" in parts or dest_name.endswith((".pyc", ".pyo")):
            dropped.append(dest_name)
        else:
            kept.append((dest_name, src_name, typecode))
    analysis.datas[:] = kept
    print(f"[zhilan.spec] datas 剔除编译产物：{before} → {len(kept)}（剔除 {len(dropped)} 项）")
    if dropped:
        print(f"[zhilan.spec]   例如：{dropped[:5]}")


_drop_compiled_artifacts(a)

# 构建期自证：资源三件套确实进了 datas（缺一条就该在建包时炸，而不是留给用户首启）
# 注意：dest_name 是 Windows 反斜杠分隔的相对路径（`alembic\versions\x.py`），比较前先归一化。
_data_dests = {dest.replace("\\", "/") for dest, _src, _tc in a.datas}
for _probe in ("frontend/dist/index.html", "alembic.ini", "alembic/env.py", "app/system/models-manifest.txt"):
    _ok = _probe in _data_dests
    print(f"[zhilan.spec] datas 命中 {_probe}: {_ok}")
    if not _ok:
        raise SystemExit(f"[zhilan.spec] datas 缺少必需资源：{_probe}（构建中止）")
for _probe_prefix in ("alembic/versions/",):
    _hits = sorted(d for d in _data_dests if d.startswith(_probe_prefix))
    print(f"[zhilan.spec] datas 命中 {_probe_prefix}*: {len(_hits)} 个 → {[h.split('/')[-1] for h in _hits]}")
    if not _hits:
        raise SystemExit(f"[zhilan.spec] datas 缺少 {_probe_prefix}（alembic 运行期读不到版本脚本）")

# 反向探针（条目 6.4，见 2b 节）：`mineru_llama_cpp` **必须不在**依赖图里。
# ⚠️ 这里**只报不断**：真正的硬断言放在 COLLECT **之后**按产物实盘做（见文件末「5.」）——
#    若在这里 `raise`，构建会在 COLLECT **之前**中止，上一次的 `dist\zhilan` 原样留着，
#    「去掉 excludes 再构建」的变异实验就会**读到上一轮的干净产物而假绿**。
_llama_graph_hits = sorted(
    str(_dest).replace("\\", "/")
    for _dest, _src, _tc in list(a.binaries) + list(a.datas) + list(a.pure)
    if "mineru_llama_cpp" in str(_dest).replace("\\", "/")
)
print(f"[zhilan.spec] 反向探针（信息）· 依赖图里 mineru_llama_cpp 条目数 = {len(_llama_graph_hits)}")
if _llama_graph_hits:
    print(f"[zhilan.spec] ⚠️ 依赖图已收入 mineru_llama_cpp：{_llama_graph_hits[:8]}")

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # onedir：不把依赖塞进 exe
    name="zhilan",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX 会显著抬高杀软误报率（§八 R6），本期不用
    console=CONSOLE,
    icon=str(SPEC_DIR / "zhilan.ico"),  # 应用图标（「书页化澜」，源稿 tools/packaging/icon/）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="zhilan",
)

# --------------------------------------------------------------------------------------
# 5. 构建期**反向自证**（条目 6.4 的核心判据）：产物里**必须没有** `mineru_llama_cpp`
# --------------------------------------------------------------------------------------
# 位置是刻意的：**在 COLLECT 之后**、按 `dist\zhilan` 的**实盘**断言。
# `COLLECT.assemble()` 是「先 `_make_clean_directory(dist\zhilan)`、再逐件拷贝」，所以本行执行时
# 产物已经落盘 ⇒ 这条断言与验收判据 A4（`dist\zhilan\_internal\mineru_llama_cpp\` 不存在）
# 是**同一件事**，不是它的近似替代。
# 断言失败时**先打印命中清单再抛**：变异实验（把该项从 `excludes` 去掉）要求「产物里又出现该包」
# 可观察——先打印、后中止，正好同时给出「构建红灯」与「命中清单」两份证据。
_PRODUCT_DIR = Path(DIST_PATH) / "zhilan"
_llama_product_hits = sorted(
    _rel
    for _rel in (
        str(_p.relative_to(_PRODUCT_DIR)).replace("\\", "/")
        for _p in _PRODUCT_DIR.rglob("*")
    )
    if "mineru_llama_cpp" in _rel.lower()
)
print(f"[zhilan.spec] 反向探针（判据）· 产物内 mineru_llama_cpp 命中数 = {len(_llama_product_hits)}")
if _llama_product_hits:
    for _hit in _llama_product_hits[:20]:
        print(f"[zhilan.spec]   ✗ 产物内命中：{_hit}")
    if len(_llama_product_hits) > 20:
        print(f"[zhilan.spec]   …（共 {len(_llama_product_hits)} 项，只列前 20）")
    raise SystemExit(
        "[zhilan.spec] 产物里出现了 mineru_llama_cpp —— 交付包不得包含它"
        "（该包无许可文本，见 2b 节与 阶段6/设计说明 §九 Q4）；"
        "请同时检查 excludes 与 2b 节的 binaries 两条通路"
    )

# --------------------------------------------------------------------------------------
# 6. 构建期自证：把「读了几个隐藏 import / 摘了什么」打进构建日志，事后可核
# --------------------------------------------------------------------------------------
print(f"[zhilan.spec] distpath  = {DIST_PATH}")
print(f"[zhilan.spec] workpath  = {WORK_PATH}")
print(f"[zhilan.spec] console   = {CONSOLE}")
print(f"[zhilan.spec] hiddenimports 共 {len(HIDDEN_IMPORTS)} 项")
print(f"[zhilan.spec] excludes 共 {len(excludes)} 项：{', '.join(excludes)}")
print(f"[zhilan.spec] datas 落点：frontend/dist、alembic、alembic.ini（均在资源根）")
