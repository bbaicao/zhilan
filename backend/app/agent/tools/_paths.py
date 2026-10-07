"""工具侧**路径边界判定**的唯一实现（专项工具升级-3 · T3 阶段一 · 条目 `回归检查`/`回归检查`）。

## 这个模块解决什么

T3 路线要交付「Agent 能在工作区里真实读写文件」。在做任何工具之前，必须先回答一个
本仓**零先例**的问题：**用户给的路径，凭什么认为它落在工作区根里？**
（实测：``is_relative_to`` / ``os.path.commonpath`` 在 ``backend/app`` **全仓 0 命中**；
``Path.resolve()`` 的 97 处命中全是锚点解析或测试夹具，最接近的 ``storage._safe_name()``
**只管文件名、不管根边界**。）

⇒ 本模块是**从零新建**的那一层，形态照 ``agent/tools/_errors.py``（同包、单一职责、
冻结中文文案常量 + ``.format()``）。

## 执行序（**五步不可调换** · D-回归检查）

    ① 词法层（**零 IO**）→ ② 根前缀比对（**零 IO**）→ ③ realpath 归一（**允许 IO**）
    → ④ 越界复判（对归一结果重跑 ②）→ ⑤ 敏感路径复判

**为什么 ① 必须在最前**：UNC（``\\\\server\\share\\x.py``）一旦进了 ``resolve()`` /
``realpath()``，Windows 会**真发 SMB 请求**。本会话实测（``回归检查-resolve读数.log``）：

- ``Path(r'\\\\192.0.2.1\\share\\x.py').resolve()`` **阻塞 26.617 秒**；
- 重复调用因**负缓存**变 0.000 秒 ⇒ 「偶发不挂」**不等于**「不发网络请求」。

⇒ UNC 只能靠 ① 的词法判定拦下。这条同时撞矩阵的「禁网 ``TOTAL=0``」判据。

## 三层各自的可判面（D-回归检查 / D-回归检查）

| 层 | 允许 IO？ | 覆盖 |
|---|---|---|
| ① 词法 | **否**（计数桩 == 0） | UNC / ``\\\\?\\`` / ``\\.\\`` / 设备名 / ADS / 驱动器相对（``C:foo``） / 尾随点空格 / 保留字符 / NUL / ``..`` 归一 / 异盘 / 8.3 短名 / 敏感段 |
| ② 前缀比对 | **否** | 同盘根外（带分隔符比对，避 ``proj`` / ``proj2`` 陷阱） |
| ③ realpath 归一 | 是 | junction / 符号链接展开；短名展开 |
| ④ 越界复判 | 是（复用 ②） | 「归一后才越界」 |
| ⑤ 敏感复判 | 是 | 「经链接伪装到达敏感目标」 |

## 与既有实现的关系

- **不新增锚点类**（红线 14 / 阶段 6 纪律 1：只有资源根 / 数据根 / 后端根三类）——
  工作区根的解析**照抄** ``config.upload_path`` 的同型形状（见 :func:`resolve_workspace_root`）；
- **分包边界**（A17）：只允许 ``app.core`` / 同包 ``app.agent.tools`` / 标准库；
- **冻结点**：不得出现裸 ``__file__`` / 裸 ``parents[N]`` / 显式 CWD / 未锚定的相对路径消费
  （``scripts/check_frozen_paths.py`` 四类判定，恰好五项例外、不新增第六项）。
"""

from __future__ import annotations

import ntpath
import os
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Final

# ── 冻结中文文案（**唯一一份**；照 ``safety.DANGEROUS_BLOCKED_TEMPLATE`` 形状：常量 + .format()）──
#: 通用越界
PATH_OUTSIDE_TEMPLATE: Final[str] = "路径越界：{path} 不在工作区根内，已拒绝。"
#: 未配置工作区根
PATH_NO_ROOT_TEMPLATE: Final[str] = "未配置工作区根，路径 {path} 无法校验，已拒绝。"
#: **根不可用**（配了，但配到了一个**非目录**上 —— 与「未配置」是两种语义，**不得共用文案**）
PATH_ROOT_UNUSABLE_TEMPLATE: Final[str] = "工作区根不可用（不是目录），路径 {path} 已拒绝。"
#: UNC / 网络路径（**词法拦截，绝不进 realpath** —— 会真发 SMB）
PATH_UNC_TEMPLATE: Final[str] = "路径越界：{path} 是网络路径，已拒绝。"
#: Win32 设备命名空间 / 扩展长度前缀
PATH_DEVICE_NAMESPACE_TEMPLATE: Final[str] = "路径越界：{path} 使用了设备命名空间前缀，已拒绝。"
#: 设备名（CON / NUL / AUX / COM1…）
PATH_DEVICE_NAME_TEMPLATE: Final[str] = "路径越界：{path} 是 Windows 设备名，已拒绝。"
#: 驱动器相对路径（``C:foo``）
PATH_DRIVE_RELATIVE_TEMPLATE: Final[str] = "路径越界：{path} 是驱动器相对路径，已拒绝。"
#: 8.3 短名
PATH_SHORT_NAME_TEMPLATE: Final[str] = "路径越界：{path} 使用了 8.3 短名形式，已拒绝。"
#: 异盘
PATH_OTHER_DRIVE_TEMPLATE: Final[str] = "路径越界：{path} 与工作区根不在同一个盘，已拒绝。"
#: 尾随点 / 空格
PATH_TRAILING_TEMPLATE: Final[str] = "路径越界：{path} 的路径段以点或空格结尾，已拒绝。"
#: 保留字符 / 控制字符
PATH_ILLEGAL_CHAR_TEMPLATE: Final[str] = "路径越界：{path} 含非法字符，已拒绝。"
#: 敏感路径
PATH_SENSITIVE_TEMPLATE: Final[str] = "路径越界：{path} 命中敏感路径清单，已拒绝。"
#: 空路径
PATH_EMPTY_TEMPLATE: Final[str] = "路径越界：路径为空，已拒绝。"

# ── 设备名（D-回归检查：**basename 去掉最后一个扩展名、折叠大小写后等于设备名**）──────────
DEVICE_NAMES: Final[frozenset[str]] = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

# ── 敏感路径段（A6a 词法腿 · 清单照阶段包 D-回归检查 的 8 项）──────────────────────────
#: 触**整个路径**即拒的盘符（连同系统盘 —— 工作区根落在别的盘时才用得上）
FORBIDDEN_DRIVES: Final[frozenset[str]] = frozenset({"a:", "b:"})
#: 触**根之下的首段**即拒的系统目录（与 `check_frozen_paths.py` 的「资源根 / 数据根 / 后端根」
#: 三类锚点同源：这些目录永远不该出现在工作区根之下）
SYSTEM_TOP_SEGMENTS: Final[frozenset[str]] = frozenset(
    {
        "windows",
        "program files",
        "program files (x86)",
        "programdata",
        "users",
        "system volume information",
        "$recycle.bin",
        "perflogs",
        "recovery",
        "msocache",
        "boot",
        "efi",
    }
)
#: 触**末段**即拒的后缀（可执行/脚本落盘面）
DANGEROUS_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".exe", ".dll", ".sys", ".com", ".scr", ".pif", ".cpl", ".msi", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".jse", ".wsf", ".wsh", ".hta", ".reg", ".lnk", ".url"}
)
#: 触**末段**即拒的启动钩子文件名（``sitecustomize`` 已单列；这几个是解释器级自动加载）
STARTUP_HOOK_FILENAMES: Final[frozenset[str]] = frozenset(
    {".pth", "usercustomize.py", "conftest.py"}
)

# ── 敏感路径（A6a 词法腿 · 清单照阶段包 D-回归检查）──────────────────────────────────
#: 命中即拒的**目录名 / 段名**（折叠大小写比对）
SENSITIVE_SEGMENTS: Final[frozenset[str]] = frozenset(
    {".git", ".env", "alembic", "data", ".ssh", ".gnupg"}
)
#: 命中即拒的**文件名**（折叠大小写比对）
SENSITIVE_FILENAMES: Final[frozenset[str]] = frozenset({"sitecustomize.py"})
#: 命中即拒的**后缀**
SENSITIVE_SUFFIXES: Final[frozenset[str]] = frozenset({".pem", ".key", ".db"})
#: 命中即拒的**前缀**（``.env.local`` / ``id_rsa`` / ``id_ed25519`` 等）
SENSITIVE_PREFIXES: Final[tuple[str, ...]] = (".env", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")

# ── 词法正则 ─────────────────────────────────────────────────────────────────
#: UNC 起始（含 ``\\\\?\\UNC\\`` 形态）
_UNC_RE: Final[re.Pattern[str]] = re.compile(r"^(?:\\\\|//)")
#: 扩展长度前缀 ``\\?\`` / 设备命名空间 ``\\.\`` / ``\\?\GLOBALROOT\``
_NAMESPACE_RE: Final[re.Pattern[str]] = re.compile(r"^[\\/]{2}[?.][\\/]")
#: 驱动器相对路径：盘符后**不跟**分隔符（``C:foo``）。⚠️ 实测 ``abspath('C:foo') == 'C:\\foo'``
_DRIVE_RELATIVE_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z]:(?![\\/])")
#: 8.3 短名（**收窄版**，D-回归检查）：末段形如 ``NAME~N`` 或 ``NAME~N.EXT``（EXT ≤ 3）
#: ⚠️ 宽版 ``^[^\\/]{1,8}~\d`` 实测会误拒 ``backup~1.txt`` / ``report~3.pdf`` / ``note~1.md``。
#: ⚠️ ⚠️ **v1.1 正则在实测下仍然误拒**（见 ``回归检查-短名判定实测.log``）：
#: 收窄到 ``{1,6}~[1-9]`` **仍然**命中 ``backup~1.txt``（basename 主体 ``backup`` 恰为 6 字符）。
#: ⇒ 本阶段**不做纯词法短名拒绝**，改为「**交给 ③ realpath 展开后由 ④ 复判**」——
#: 实测 ``Path(r'C:\\PROGRA~1').resolve() == 'C:\\Program Files'``（**会展开**）⇒
#: 落在根内的短名形态自然放行，指向根外的自然被 ④ 拒（**误拒为 0、误放行也为 0**）。
#: 保留本常量与 :data:`PATH_SHORT_NAME_TEMPLATE` 供**阶段 5**（写侧）按需启用。
_SHORT_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[^\\/]{1,6}~([1-9])(\.[^\\/.]{1,3})?$")
#: 是否启用纯词法短名拒绝。**阶段一实测结论：关闭**（理由见上）。
LEXICAL_SHORT_NAME_REJECT: Final[bool] = False
#: 保留字符（Windows 文件名非法字符）+ NUL 字节
_ILLEGAL_CHAR_RE: Final[re.Pattern[str]] = re.compile(r'[<>:"|?*\x00-\x1f]')
#: 盘符形（``C:\`` / ``C:/``）
_DRIVE_ABS_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z]:[\\/]")
#: 超长路径阈值（句法阈值；Win32 未加前缀时约 260）
MAX_PATH_LEN: Final[int] = 4096


@dataclass(frozen=True)
class Normal:
    """**放行**判定结果：携带① — ④ 各层的产物，供调用方继续做 §5 与文件名构造。"""

    #: 调用方**原样**给的原始字符串
    raw: str
    #: ③ realpath 归一后的绝对路径
    resolved: Path
    #: ② 层比对用的绝对路径（未经 ③ 的链接展开；词法绝对化 + normpath）
    absolute: Path


@dataclass(frozen=True)
class Reject:
    """**拒绝**判定结果：只能来自这五层的某一层，且**必带中文原因**（I5 形态：不抛）。"""

    raw: str
    layer: str
    reason: str


Verdict = Normal | Reject


# ── ① 词法层（**零 IO**）─────────────────────────────────────────────────────


def _lexical_layers(
    raw: str, *, is_root: bool = False, root_prefix: Path | None = None
) -> tuple[str, str | None]:
    """① 词法判定：返回 ``(用于 ② 的字符串, 拒绝文案 or None)``。**本函数零 IO**。

    零 IO 的含义是可判的：它只做字符串处理与正则，**不**调用 ``resolve`` / ``exists`` /
    ``stat`` / ``open`` / ``iterdir`` 中的任何一个 —— 由
    ``tests/agent/test_tool_paths_t3.py`` 的计数桩按层自证（A1 第 4 断件 / A2）。

    ## ``is_root`` 开关（**本会话实测新增**）

    同一个词法判据用在**两个不同语义**的输入上，**不能一刀切**：

    | 判定站 | 对**候选路径** | 对**工作区根** |
    |---|---|---|
    | UNC / 设备命名空间 / 驱动器相对 / 非法字符 / NUL / 设备名 | 拒 | **拒**（根也不该长这样） |
    | ST6c 系统目录（`Windows` / `Users` / `Program Files` …） | **拒** | **不适用** |
    | 尾随点空格 / 敏感段 / 危险后缀 | 拒 | **不适用** |

    实测踩坑：把 ST6c 套到**根**上时，`<user-home>\\x\\ws`（本机 `tmp_path` 的**真实**形态、
    也是绝大多数用户的工作区位置）**整条被打成 Reject** ⇒ `classify` 的**全部**合法路径腿
    跟着红（`layer='root'`）。根**当然**可能落在 `<user-home>\\...` 之下 —— 那是正常位置，
    不是越界。⇒ 引入本开关，根只过「形态合法性」那几条，不过「语义敏感性」那几条。
    """
    if not raw or not raw.strip():
        return raw, PATH_EMPTY_TEMPLATE.format(path=raw or "")

    # ⚠️ **尾随点 / 空格的判定必须在 `strip()` 之前**（本会话实测，**首版真缺陷**）：
    #   首版判在 `strip()` **之后** ⇒ `'sub/foo '` 已被剥成 `'sub/foo'`，判定**永远命不中**
    #   （A1 矩阵的 `trailing-space` 行直接红）。⇒ 先按**原串**逐段判，再 strip。
    # 现状口径：**段内与末段**的尾随点 / 空格一律拒 —— Win32 会把它规范化成同名，
    # 于是「检查的名字」与「实际使用的名字」**指向不同文件**（经典绕过面）。
    # ⚠️ ``is_root`` 时不适用（根允许以点结尾的目录名，见 `is_root` 开关说明）。
    # ⚠️ 整串**两端**的空白仍视为「调用方格式噪声」予以忽略（已由上面的空值判定兜住 `""`）。
    if not is_root:
        for seg in re.split(r"[\\/]+", raw.replace("\\", "/")):
            if seg in ("", ".", ".."):
                continue
            if seg.endswith(".") or seg.endswith(" "):
                return raw, PATH_TRAILING_TEMPLATE.format(path=raw)

    candidate = raw.strip(" \t\r\n")
    # 归一分隔符：后续正则统一按 ``/`` 处理（Windows 两者等价）
    unified = candidate.replace("\\", "/")

    # ST1 设备命名空间 / 扩展长度前缀（**必须在 UNC 之前**：``\\?\UNC\...`` 两者都命中，
    # 文案取更具体的设备命名空间）
    if _NAMESPACE_RE.match(unified):
        return candidate, PATH_DEVICE_NAMESPACE_TEMPLATE.format(path=candidate)

    # ST2 UNC（含 ``\\?\UNC\`` 已被上一条拦下）
    if _UNC_RE.match(unified):
        return candidate, PATH_UNC_TEMPLATE.format(path=candidate)

    # ST3 非法字符 + NUL（在分段之前判：``\x00`` 会让 C 层路径截断）
    # 🔴 **本会话实测重大缺陷修正**：首版对 ``candidate`` **整串**做非法字符检查，
    # 而 ``:`` 在正则在表内 ⇒ **任何盘符绝对路径**（``<user-home>\x\ws\x.py``）**全被拒**，
    # 报「含非法字符」。这是**方向错误**的缺陷（把合法路径拒掉），比漏拦更危险。
    # ⇒ 必须先**剥掉盘符**再判：盘符的 ``:`` 是语法组成部分，不是非法字符。
    # （``C:foo`` 这类驱动器相对路径由 ST4 单独拦；``sub/a:b.txt`` 的 ADS 冒号仍在表内 → 照拒。）
    _body_for_illegal = unified[2:] if _DRIVE_ABS_RE.match(unified) else unified
    if _ILLEGAL_CHAR_RE.search(_body_for_illegal):
        return candidate, PATH_ILLEGAL_CHAR_TEMPLATE.format(path=candidate)

    # ST4 驱动器相对路径 ``C:foo``（**不是**绝对路径，但 ``abspath`` 会静默变成 ``C:\foo``）
    if _DRIVE_RELATIVE_RE.match(candidate):
        return candidate, PATH_DRIVE_RELATIVE_TEMPLATE.format(path=candidate)

    # ST5 超长
    if len(candidate) > MAX_PATH_LEN:
        return candidate, PATH_OUTSIDE_TEMPLATE.format(path=candidate[:120] + "…")

    # 分段（``/`` 与 ``\`` 都算分隔符；重复分隔符与空段由 normpath 收掉）
    segments = [s for s in re.split(r"[\\/]+", unified) if s]

    # ST6 已上移至 `strip()` **之前**（见函数开头；此处不再重复判定）

    # ST6b 触**整个路径**即拒的盘符（A: / B: 软驱位；UNC / 驱动器相对已在 ST2/ST4 拦下）
    drive = ntpath.splitdrive(candidate)[0].lower()
    if drive in FORBIDDEN_DRIVES:
        return candidate, PATH_OTHER_DRIVE_TEMPLATE.format(path=candidate)

    # ST6c 触**系统目录**即拒（``C:\Windows\x.py`` / ``C:\Program Files\x.py`` …）
    #
    # ⚠️ **口径（本会话两轮实测修正）**：
    # ① 只能管「**盘符绝对路径**」的首段 —— 相对路径的首段是**相对工作区根**的
    #    （工作区根若落在 ``E:\Users\me\ws``，则 ``Users/a.txt`` 指的是根内的 ``Users``，
    #     **不是** ``<user-home>``）。按 segments[1] 套到相对路径 ⇒ **误拒**。
    # ② 即使对绝对路径，也**必须排除「候选本来就在根内」的情形** ——
    #    工作区根**自己**就落在 ``<user-home>\\<你>\\...`` 下是**最常见**的布置
    #    （本机 `tmp_path` 即 `<user-home>\\user\\AppData\\Local\\Temp\\...`）⇒
    #    此时 ``<user-home>\\<你>\\ws\\x.py`` 的首段是 ``Users``，但它**完全合法**。
    # ⇒ 判定条件收紧为：**绝对路径 + 首段是系统目录 + 候选不在根内**（`root_prefix` 为准）。
    if (
        not is_root
        and root_prefix is not None
        and _DRIVE_ABS_RE.match(unified)
        and len(segments) > 1
        and ":" in segments[0]
    ):
        top = segments[1].strip().lower()
        if top in SYSTEM_TOP_SEGMENTS:
            # 候选若落在根本身之内 ⇒ 是正常位置（根就在系统目录下），放行给 ② 前缀层处理
            if not is_inside(root_prefix, candidate):
                return candidate, PATH_OUTSIDE_TEMPLATE.format(path=candidate)

    # ST7 设备名（basename 去掉**最后一个**扩展名后折叠大小写命中设备名）
    basename = segments[-1] if segments else ""
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    # ⚠️ 设备名判定**只对裸名成立**：实测 ``NUL`` / ``CON`` / ``AUX`` / ``COM1`` 的 ``os.open``
    # 成功（确为设备），而 ``NUL.log`` / ``CON.txt`` 报「找不到文件」⇒ 不是设备。
    # 但「去扩展名后的 basename 命中设备名」仍按 D-回归检查 一律拒（保守，且不误伤普通文件）。
    if stem.strip().lower() in DEVICE_NAMES:
        return candidate, PATH_DEVICE_NAME_TEMPLATE.format(path=candidate)

    # ST8 8.3 短名：**阶段一不启用纯词法拒绝**（收窄正则实测仍误拒 ``backup~1.txt``）⇒
    # 交给 ③ realpath（实测会展开短名）+ ④ 复判。见 ``LEXICAL_SHORT_NAME_REJECT``。
    if LEXICAL_SHORT_NAME_REJECT and segments and _SHORT_NAME_RE.match(segments[-1]):
        return candidate, PATH_SHORT_NAME_TEMPLATE.format(path=candidate)

    # ST9 可执行 / 脚本 / 启动钩子后缀（工作区里放这些 = 把工作区变成可执行载荷面）
    if basename:
        lowered_base = basename.strip().lower()
        for suffix in DANGEROUS_SUFFIXES:
            if lowered_base.endswith(suffix):
                return candidate, PATH_SENSITIVE_TEMPLATE.format(path=candidate)
        if lowered_base in STARTUP_HOOK_FILENAMES or lowered_base.endswith(".pth"):
            return candidate, PATH_SENSITIVE_TEMPLATE.format(path=candidate)

    # ST10 敏感**段**（词法腿 A6a：``.git/**`` / ``.env`` 等 —— 归一前就能判）
    # ⚠️ ``is_root`` 时**不适用**：根**自身**叫什么名字是业主的自由（照 ST6c 同理）。
    if not is_root:
        for seg in segments:
            lowered = seg.strip().lower()
            if lowered in SENSITIVE_SEGMENTS:
                return candidate, PATH_SENSITIVE_TEMPLATE.format(path=candidate)
            if lowered in SENSITIVE_FILENAMES:
                return candidate, PATH_SENSITIVE_TEMPLATE.format(path=candidate)
            if lowered.startswith(SENSITIVE_PREFIXES):
                return candidate, PATH_SENSITIVE_TEMPLATE.format(path=candidate)
            for suffix in SENSITIVE_SUFFIXES:
                if lowered.endswith(suffix):
                    return candidate, PATH_SENSITIVE_TEMPLATE.format(path=candidate)

    # 走到这里 = ① 放行（**仍可能有 ``..``** —— 交给 ② 的纯字符串归一 + 根比对）
    return candidate, None


def _lexical_absolute(raw: str, root: Path) -> str:
    """把 ① 放行的字符串**纯字符串地**绝对化（``..`` 在此被折叠）。**零 IO**。"""
    unified = raw.replace("\\", "/")
    if _DRIVE_ABS_RE.match(unified):
        absish = unified
    else:
        absish = str(root).replace("\\", "/").rstrip("/") + "/" + unified
    # ``normpath`` 折叠 ``..``/``.``，**纯字符串**（不碰文件系统）
    return ntpath.normpath(absish.replace("/", "\\"))


# ── ② 根前缀比对（**零 IO**）──────────────────────────────────────────────────


def is_inside(root: Path, candidate: Path | str) -> bool:
    """``candidate`` 是否落在 ``root`` 之内（**含 ``root`` 自身**）。

    ⚠️ **三条不能省的实现要求**（三条都有实测支撑）：

    1. **不得用裸 ``str.startswith``** —— ``C:\\proj2\\x.py`` 会被判成落在 ``C:\\proj`` 内
       （K-回归检查）。本函数用 ``commonpath`` + 分隔符边界双重判定；
    2. **必须折叠大小写** —— Windows 大小写不敏感，裸比对会**误拒** ``E:\\PROJ\\x.py``
       （K-回归检查；实测 ``commonpath([r'C:\\proj', r'C:\\PROJ\\x.py']) == 'C:\\proj'`` 本身
       已折叠，但 ``normcase`` 是显式的第二道）；
    3. **必须兜 ``ValueError``** —— ``os.path.commonpath`` 在异盘抛
       ``ValueError: Paths don't have the same drive``（K-回归检查，实测）。
       ⚠️ **实测澄清**：该异常**同样**会因「相对路径 + 绝对路径混用」抛出
       （本会话实测 ``os.path.commonpath(['data', r'C:\\x'])`` ⇒ ``ValueError``，3.13 亦然）
       ⇒ 本函数**只对归一后的两个绝对路径调用** ``commonpath``，并对任意 ``ValueError``
       兜底返回 ``False``（fail-closed：判不出来就拒绝）。

    4. **必须排除 CWD 归因** —— 见下面的「CWD 独立性闸」。相对路径一侧 ⇒ 直接 ``False``。
    """
    root_s = ntpath.normpath(str(root))
    cand_s = ntpath.normpath(str(candidate))
    root_n = os.path.normcase(root_s)
    cand_n = os.path.normcase(cand_s)

    # 🔴 **CWD 独立性闸（本会话实测新增）**：`os.path.commonpath` 对**相对路径**会按
    # **进程 CWD** 归因（相对路径 → `abspath`）。若本函数被喂相对根，判定结果就会
    # **随进程 CWD 变化**——那是 `check_frozen_paths.py` 的 ③「显式 CWD」同族纪律
    # （红线 1「不得取进程 CWD 当工作区根」）。
    # ⇒ 只要有一侧是相对路径，直接 **fail-closed**（返回 False），**不做任何 CWD 归因**。
    # ⚠️ **不得**改成「先 abspath 再比」—— 那正是红线 1 禁止的形态。
    if not ntpath.isabs(root_s) or not ntpath.isabs(cand_s):
        return False

    if root_n == cand_n:
        return True
    # 分隔符边界：``C:\proj`` 后面必须是 ``\``（避免 ``proj2`` 被当成根内）
    if not root_n.endswith("\\"):
        root_n_sep = root_n + "\\"
    else:
        root_n_sep = root_n
    if not cand_n.startswith(root_n_sep):
        return False
    # 再用 commonpath 做第二道（两条都过才放行；任一条给出「不在内」即拒）
    try:
        common = os.path.commonpath([root_s, cand_s])
    except ValueError:
        # 异盘（或 3.12 的相对/绝对混用）⇒ fail-closed
        return False
    return os.path.normcase(ntpath.normpath(common)) == root_n


def _same_drive(a: str, b: str) -> bool:
    """两个路径的**盘符**是否相同（UNC / 无盘符一律视为不同）。**零 IO**。"""
    drive_a = ntpath.splitdrive(str(a))[0].lower()
    drive_b = ntpath.splitdrive(str(b))[0].lower()
    if not drive_a or not drive_b:
        return False
    return drive_a == drive_b


def root_is_usable_dir(root: Path) -> bool:
    """**工作区根**是否可用。**本函数会产生 IO** —— 见下。

    ## 为什么它必须存在（A5b′ 子判据 ①，「根是文件 ⇒ 不得放行」）

    实测：单靠 ② 的纯字符串前缀比对**拦不住**「根是文件」——
    ``is_inside(Path(r'...\\a.txt'), Path(r'...\\a.txt\\child.txt'))`` 返回 ``True``
    （前缀确实以 ``...\\a.txt\\`` 开头）⇒ 一个**文件**被当成了根，候选被**误放行**。

    判定「一个路径是不是目录」**只能靠 IO**（`os.path.isdir` 是 C builtin，无法被词法替代）。

    ## ⚠️ 判据必须**只拦「确证是文件」**，**不得**拦「不存在」

    本会话实测踩过：首版写成「`isdir()` 为假即拒」⇒ **任何尚不存在的根**（含全部单元测试
    用的 `<user-home>/x/ws` 这种虚构路径、以及**尚未创建工作区目录的真实首次使用场景**）
    **全部被误拒**，`classify` 的合法路径腿一起红。

    ⇒ 正确语义（**三态**）：

    | `exists()` | `isdir()` | 判定 | 理由 |
    |---|---|---|---|
    | 否 | — | **放行**（交给 ③ 归一 / 后续层的写侧检查） | 「根还没建」是**正常**场景（首次使用），不是安全事件 |
    | 是 | 是 | 放行 | 正常 |
    | 是 | 否 | **拒**（fail-closed） | 根落在**普通文件**上 ⇒ 不可能成为任何子路径的根 |

    这样 A5b′ 子判据 ①（根是文件 ⇒ 拒）成立，同时**不误伤**「根不存在」的合法场景
    （`test_a5b_root_not_a_directory_variants` 用的是「根不存在 ⇒ 越界输入仍须拒」，
    该断言由 ② 前缀层独立满足，与本函数无关）。
    """
    try:
        s = str(root)
        if not os.path.exists(s):
            return True  # 尚不存在 ⇒ 不是安全事件（首次使用 / 测试虚构路径）
        return os.path.isdir(s)
    except OSError:  # 权限 / 非法形态 ⇒ fail-closed
        return False


# ── ③ realpath 归一（**允许 IO**）────────────────────────────────────────────


def _realpath_absolute(absolute: str) -> Path:
    """③ realpath 归一层。**唯一允许产生 IO 的两处之一**。

    用 ``os.path.realpath``（= ``ntpath.realpath`` → C 实现 ``nt._getfinalpathname``）
    而**不是** ``Path.resolve()`` —— 本会话实测（``回归检查``）：

    - ``Path.resolve()`` 会**绕过一切 ``Path.*`` 桩**（patch 掉 ``Path.stat`` / ``Path.open`` /
      ``Path.is_dir`` / ``Path.exists`` / ``Path.iterdir`` 之后，``resolve()`` 照常成功、
      命中 0 次）；
    - 而 ``os.path.realpath`` / ``ntpath.realpath`` **可以**被 patch 拦住。

    ⇒ 用模块级入口，让「零 IO」计数桩**真接得上**（本模块的头号假绿风险）。
    """
    return Path(os.path.realpath(absolute))


# ── 编排壳（①②③④⑤）─────────────────────────────────────────────────────────


def classify(path: str, root: Path | None) -> Verdict:
    """**唯一入口**：把用户给的路径判成 :class:`Normal` 或 :class:`Reject`。

    A1 / A2 的判据都**只经由本函数**——「同一入口只换路径参数」是计数桩可归因的前提
    （D-回归检查 三件套 ①）。
    """
    if root is None:
        return Reject(raw=path, layer="root", reason=PATH_NO_ROOT_TEMPLATE.format(path=path))

    root_path = Path(root)

    # ②-ST0a **根本身形态闸**（本会话实测新增，A5b′ 子判据 ①）
    # 根必须先自证是「形态合法的绝对路径」——① 词法层过一遍（但**只过形态合法性**：
    # UNC / 设备命名空间 / 驱动器相对 / 非法字符 / 设备名；**不过**「系统目录 / 敏感段 / 尾随点」
    # 那几条语义判定，理由见 `_lexical_layers` 的 `is_root` 开关说明）。
    # ⚠️ **不得**用 ``root.exists()`` / ``is_dir()`` 当闸（那是 IO，会让 A1 的「零 IO」断件红）：
    #    「根是文件」的判定见下方 ②-ST1b 的 `root_is_usable_dir`（那处**必须**有 IO，
    #    且越界样本在 ①/② 就返回、走不到那里 ⇒ A1 的「桩 == 0」不受影响）。
    _, root_reject = _lexical_layers(str(root), is_root=True)
    if root_reject is not None:
        # 根不合法 ⇒ 一律 fail-closed（**不抛异常**：调用方拿到 Reject 才能给冻结文案）
        return Reject(raw=path, layer="root", reason=root_reject)
    if not ntpath.isabs(str(root)):
        return Reject(raw=path, layer="root", reason=PATH_NO_ROOT_TEMPLATE.format(path=path))

    # ① 词法层（零 IO）
    candidate, reject = _lexical_layers(path, root_prefix=root_path)
    if reject is not None:
        return Reject(raw=path, layer="lexical", reason=reject)

    # ② 根前缀比对（零 IO）：先把候选纯字符串绝对化，再与根比
    absolute = _lexical_absolute(candidate, root_path)

    # ②-ST0 异盘（词法可判：盘符不同 ⇒ 拒；UNC 已在 ① 拦下，这里兜住「根是 UNC」等形态）
    if not _same_drive(absolute, str(root_path)):
        return Reject(raw=path, layer="prefix", reason=PATH_OTHER_DRIVE_TEMPLATE.format(path=path))

    if not is_inside(root_path, Path(absolute)):
        return Reject(raw=path, layer="prefix", reason=PATH_OUTSIDE_TEMPLATE.format(path=path))

    # ②-ST1b **根可用性闸**（A5b′ 子判据 ①②）：根**是文件** / 根**不存在** ⇒ fail-closed。
    # 这是 ③ 之前唯一会碰 IO 的一处（`os.path.isdir` 是 C builtin，词法替代不了），
    # 位置与理由见 `root_is_usable_dir` 的 docstring（越界样本在 ①/② 就返回了，到不了这里）。
    if not root_is_usable_dir(root_path):
        # ⚠️ **文案必须与「未配置」分开**（对抗审计 P1）：首版两条语义共用
        # `PATH_NO_ROOT_TEMPLATE`（「未配置工作区根…」）⇒ 用户明明**配了**（只是配到了
        # 一个普通文件上），却被告知「未配置」⇒ 按提示去翻配置键会**越查越错**。
        # 这是方向性的误导（与「盘符冒号被误判非法字符」同族：把**两种不同**的输入
        # 折叠成同一种输出）。⇒ 单列 `PATH_ROOT_UNUSABLE_TEMPLATE`。
        return Reject(
            raw=path, layer="root", reason=PATH_ROOT_UNUSABLE_TEMPLATE.format(path=path)
        )

    # ③ realpath 归一（**允许 IO**）
    resolved = _realpath_absolute(absolute)

    # ④ 越界复判（对归一结果**重跑同一个 is_inside**，D-回归检查）
    if not is_inside(root_path, resolved):
        return Reject(raw=path, layer="recheck", reason=PATH_OUTSIDE_TEMPLATE.format(path=path))

    # ⑤ 敏感路径复判（归一后的**每一段**再过一次，防「经链接伪装到达敏感目标」）
    if _sensitive_hit(resolved):
        return Reject(raw=path, layer="sensitive", reason=PATH_SENSITIVE_TEMPLATE.format(path=path))

    return Normal(raw=path, resolved=resolved, absolute=Path(absolute))


def _sensitive_hit(resolved: Path | str) -> bool:
    """归一后的路径是否命中敏感清单（A6b 腿）。"""
    parts = PureWindowsPath(str(resolved)).parts
    for part in parts:
        lowered = part.strip().lower()
        if not lowered:
            continue
        if lowered in SENSITIVE_SEGMENTS or lowered in SENSITIVE_FILENAMES:
            return True
        if lowered.startswith(SENSITIVE_PREFIXES):
            return True
        for suffix in SENSITIVE_SUFFIXES:
            if lowered.endswith(suffix):
                return True
    return False


# ── 工作区根解析层（**不新增锚点类** · D-回归检查 / D-回归检查）───────────────────────


def resolve_workspace_root() -> Path | None:
    """把「工作区根」从配置里解析成绝对路径；**未配置 ⇒ ``None``**。

    ⚠️ 本函数**只做解析**（D-回归检查：配置键 ``WORKSPACE_ROOT`` 的建立与「默认空 ⇒ 不注册
    任何工具」的注册门**归阶段 3** 的 ``回归检查``；本阶段零新增 ``.env`` 键）。

    形状**照抄** ``config.upload_path`` / ``config.sqlite_file``（同型解析形状）：

        raw = (settings.<键> or "").strip().strip('"').strip("'")
        path = Path(raw or <默认值>).expanduser()
        return path if path.is_absolute() else ROOT_DIR / path

    ⇒ **相对路径落在既有「数据根」锚点内**，**不新增锚点类**（红线 14）。
    本阶段因「键归阶段 3」而**不读键**，改为支持**显式传值**（阶段 3 接线时把
    ``settings.WORKSPACE_ROOT`` 接进来即可，本函数签名不变）。
    """
    from app.core.config import ROOT_DIR  # 局部 import：与冻结态锚点同源

    raw = _read_configured_root()
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        return None
    # ① 词法层先过一遍：工作区根本身不得是 UNC / 设备命名空间 / 驱动器相对 / 非法字符
    # ⚠️ **不得抛异常**（首版 `raise ValueError(reject)` 实测有两个问题）：
    #   ① 调用方（阶段 3 的工具注册门）拿不到「冻结中文文案」而是一个异常栈；
    #   ② pytest 的 `tmp_path` 含 `:` 形态的临时段时会把**合法根**打成异常——
    #      本会话实测：`ValueError: 路径越界：C:\\...\\pytest-of-user\\...\\ws 含非法字符`。
    # ⇒ 契约统一为「**不合法 ⇒ 当作未配置**」（返回 `None` ⇒ 上层 fail-closed 拒绝所有路径），
    #   与 :func:`classify` 的 I5 形态（不抛、给 Reject）一致。
    _, reject = _lexical_layers(raw, is_root=True)
    if reject is not None:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path.resolve()


def _read_configured_root() -> str:
    """读配置里的工作区根原始字符串（**本函数是全仓唯一的配置读点**）。

    **阶段 3（`回归检查`）已接线**：键 = ``settings.WORKSPACE_ROOT``（落**顶层**，
    与 `upload`/`sqlite`/`skill` 同族路径类配置；**不进 `agent` 段**）。
    **默认空串** ⇒ 恒返回空串 ⇒ ``resolve_workspace_root()`` 返 ``None``
    ⇒ `bootstrap.build_registry()` 的注册门**跳过三个只读文件工具的 register**
    （单⑨ (c)「不设默认」⇒ 功能默认不可用）。

    ⚠️ 本函数**签名不变、仍是唯一配置读点**（阶段 1 的判据 `A5b′` 腿③ 用
    ``monkeypatch.setattr(P, "_read_configured_root", …)`` 打**模块属性**，见
    `tests/agent/test_tool_paths_t3.py::test_a5b_root_resolution_shape`）——
    ``K-回归检查``：本文件是阶段 1 已验收交付物，阶段 3 **只消费、不修改**其余任何一行。

    ⚠️ 归一（strip 引号 / `expanduser` / 相对路径锚 `ROOT_DIR` / `resolve()`）
    **一律不在这里做**，唯一落点是调用方 ``resolve_workspace_root()`` ——
    两处实现会产生两套语义。
    """
    from app.core.config import settings  # 局部 import：与 :570 同型，避免模块级 app.* 依赖

    return settings.WORKSPACE_ROOT or ""


# ── 路径 → 边界内相对路径（**留待阶段 5**）───────────────────────────────────


def describe(root: Path, resolved: Path) -> str:
    """把归一后的路径渲染成「相对根的形态」（用于观察文本；不泄露根的绝对前缀）。

    本阶段（只读地基）**无调用点** —— 它是为阶段 3/5 的工具侧准备的**同一份**实现，
    避免「每个工具各写一份相对化逻辑」（R2-01 同型的「唯一实现」纪律）。
    未在根内时返回 ``str(resolved)``。
    """
    try:
        root_s = os.path.normcase(ntpath.normpath(str(root)))
        cand_s = os.path.normcase(ntpath.normpath(str(resolved)))
        if cand_s == root_s:
            return "."
        sep = root_s if root_s.endswith("\\") else root_s + "\\"
        if cand_s.startswith(sep):
            rel = ntpath.relpath(str(resolved), str(root))
            return rel.replace("\\", "/")
    except ValueError:  # 异盘
        pass
    return str(resolved)


__all__ = [
    "DANGEROUS_SUFFIXES",
    "DEVICE_NAMES",
    "FORBIDDEN_DRIVES",
    "LEXICAL_SHORT_NAME_REJECT",
    "MAX_PATH_LEN",
    "Normal",
    "PATH_DEVICE_NAME_TEMPLATE",
    "PATH_DEVICE_NAMESPACE_TEMPLATE",
    "PATH_DRIVE_RELATIVE_TEMPLATE",
    "PATH_EMPTY_TEMPLATE",
    "PATH_ILLEGAL_CHAR_TEMPLATE",
    "PATH_NO_ROOT_TEMPLATE",
    "PATH_OTHER_DRIVE_TEMPLATE",
    "PATH_OUTSIDE_TEMPLATE",
    "PATH_ROOT_UNUSABLE_TEMPLATE",
    "PATH_SENSITIVE_TEMPLATE",
    "PATH_SHORT_NAME_TEMPLATE",
    "PATH_TRAILING_TEMPLATE",
    "PATH_UNC_TEMPLATE",
    "Reject",
    "SENSITIVE_FILENAMES",
    "SENSITIVE_PREFIXES",
    "SENSITIVE_SEGMENTS",
    "SENSITIVE_SUFFIXES",
    "STARTUP_HOOK_FILENAMES",
    "SYSTEM_TOP_SEGMENTS",
    "Verdict",
    "classify",
    "describe",
    "is_inside",
    "resolve_workspace_root",
    "root_is_usable_dir",
]
