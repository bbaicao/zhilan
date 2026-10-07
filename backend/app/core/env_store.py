"""本机 `.env` 的读写。

设置页的改动要落盘到仓库根 `.env`（真实 Key 只留本机，不进数据库、不进仓库）。
原实现只在 `settings_routes` 里，多服务配置落地后启动迁移也要写，故抽成独立模块。

约定：
- 只改值，不重排顺序、不删注释；命中同名键就替换那一行。
- 新增键追加到文件末尾，统一挂在 `# 知澜设置页写入` 之下。

**冻结态（打包后）的锚点**（阶段 6 · 条目 6.2b）：
`ROOT_DIR` 同时是 `.env` 与上传目录（`settings.upload_path`）的基准，冻结态下必须锚到
**可执行文件所在目录（安装目录）**——因为 `__file__` 会落在 PyInstaller 的临时解包目录，
进程退出即被清理，写进去的设置下次启动就丢了。开发态行为与改造前逐字一致。
"""

import os
import sys
from pathlib import Path


def _resolve_root_dir() -> Path:
    """数据/配置根目录：开发态是仓库根；冻结态是可执行文件所在目录（安装目录）。

    **为什么不锚 `sys._MEIPASS`**：那是**只读的临时解包目录**，`persist_env` 要往里写 `.env`、
    上传留档也要往里写文件——写进临时目录等于丢弃。故一律锚 `sys.executable` 的父目录：
    PyInstaller 一目录与一文件两种模式下它都指向用户实际运行的那个 exe，是可写且稳定的。

    ⚠️ **已知约束**：若用户把程序装到 `C:\\Program Files\\` 这类受保护目录，该目录不可写，
    设置页保存会失败。这条由安装程序（条目 6.5）默认装到可写目录 + 条目 6.6 干净机器验收覆盖。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


ROOT_DIR = _resolve_root_dir()
ENV_PATH = ROOT_DIR / ".env"


def resolve_data_root() -> Path:
    """数据/配置根（**调用时**解析）：冻结态 = 可执行文件所在目录；开发态 = 仓库根。

    与模块级 `ROOT_DIR` 的关系：`ROOT_DIR` 是本模块**导入期**的快照（既有调用方按它走，行为不变）；
    本函数是同一解析器的**调用时**入口，供「运行期才决定落点」的新调用方使用
    （例如 回归修复 的应用层日志出口：测试要能在冻结形态下钉出落点）。

    ⛔ 不新增配置键：数据根在 v2 里没有独立的环境变量键，唯一解析器就是 `_resolve_root_dir()`。
    """
    return _resolve_root_dir()

WRITE_MARKER = "# 知澜设置页写入"


def _env_value(value: str) -> str:
    """按 **python-dotenv** 的解析规则给值加引号 / 转义（**只在必要时**）。

    ## 为什么必须有它

    dotenv 对裸值有三条会**静默改义**的规则：行内注释（值里的「空格 + #」被吃掉）、
    首尾空白（`parse_unquoted_value` 末尾 rstrip）、换行（裸值到行尾截断）——命中的值必须包起来。

    ## 回归修复（2026-09-28）：旧实现把撇号**静默删除**

    旧实现是 `'"' + value.replace("'", "") + "'"`——值**同时**「命中加引号条件」且「含撇号」时
    （如 OBrien #1、' it's '），落盘的是**删掉撇号**的版本，而内存单例仍持原值
    （settings_routes 是「落盘成功才 setattr」）⇒ **磁盘/内存当场分歧、无任何告警**，
    重启后读入损坏值。现改为**双引号包裹 + 转义**（零拒答面：不拒绝任何值）。

    ## 解析规则逐条实测（python-dotenv 1.2.3，dotenv/parser.py；读数见本条交付）

    - **双引号内的转义**：只有反斜杠反斜杠、反斜杠撇号、反斜杠双引号、反斜杠 a/b/f/n/r/t/v
      这十种会被解码，**其余反斜杠序列原样保留** ⇒ 本函数把**反斜杠与双引号逐字符转义**，
      并把**换行 / 回车转成「反斜杠 n」「反斜杠 r」两个字符**（后者顺带让值在磁盘上保持**单行**
      ——多行值会让 persist_env 的按行重写把它截断）；
    - **双引号内的 # 是字面量**（注释只在引号外生效）；
    - **撇号不需要转义**：单引号在双引号里就是普通字符（这正是本条修法的关键）；
    - **值以引号开头必须包起来**：`KEY='abc` 会让 dotenv 把整行判为非法、**静默丢掉该键**
      （实测 could not parse statement）⇒ 触发条件里补「首 / 尾字符是引号」；
    - ⚠️ **美元花括号形态会被插值**：resolve_variables 对**所有**值生效（单引号形态也一样）
      ⇒ 含该形态的值**任何引号形态都过不了往返**。这是**既有**缺陷、与本笔无关
      （改前改后同值，已实测）；修它在**读侧**（`dotenv_values(..., interpolate=False)` 在
      app/core/config.py，不在本模块 scope）⇒ 本笔**如实登记、不修**，判据见
      tests/core/test_bug135_env_quote_escape.py 的登记腿。

    不含引号 / 空白 / 换行的值**逐字节原样返回**（负控腿钉住：既有输出一个字都不变）。
    """
    need_quote = (
        value != value.strip()
        or " #" in value
        or "\n" in value
        or "\r" in value
        or value[:1] in ("'", '"')
        or value[-1:] in ("'", '"')
    )
    if not need_quote:
        return value
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return '"' + escaped + '"'


def persist_env(updates: dict[str, str]) -> None:
    """把 updates 合并进 .env；已存在的键就地替换，其余追加。"""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={_env_value(updates[key])}")
                seen.add(key)
                continue
        out.append(line)
    missing = [key for key in updates if key not in seen]
    if missing:
        if out and out[-1].strip():
            out.append("")
        # 标记只留一条：多次保存不会再攒出一堆同样的注释（新键仍统一追加到文件末尾）
        if not any(line.strip() == WRITE_MARKER for line in out):
            out.append(WRITE_MARKER)
        for key in missing:
            out.append(f"{key}={_env_value(updates[key])}")
    # **原子落盘**（2026-09-18 加固，`回归修复` 的连带）：自启动迁移（`migrate_legacy_rerank`）起，
    # 「进程启动时写 .env」成了常态路径，而 `write_text` 是**原地截断再写**——断电 / 崩溃 / 杀进程
    # 会在磁盘上留下**被截断的 .env**（密钥与全部配置一起丢）。`os.replace` 在同一卷上是原子的
    # （Windows 亦然）：读到的要么是旧内容、要么是新内容，不会半个。写临时文件失败则清理后原样抛。
    tmp = ENV_PATH.with_name(ENV_PATH.name + ".tmp")
    try:
        tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
        os.replace(tmp, ENV_PATH)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
