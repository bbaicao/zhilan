"""应用层日志的**文件出口**（回归修复 剩余开放面 · 批次 1）。

## 冻结态日志的文件出口

冻结态（打包后）app.* 的日志无处可去：

- backend/app 侧 51 处 logging.* 全是 getLogger(__name__)，零 FileHandler / basicConfig / addHandler；
- console=True 时 app.* 只经 logging.lastResort 打到 sys.stderr；
- console=False（ZL_CONSOLE=0，2026-09-27 起为发布构建形态）下，**裸** windowed 冻结进程的
  sys.stdout / sys.stderr 为 None（PyInstaller 6.22.3 / CPython 3.13.14，四机制实测：Popen 继承 /
  explorer 等价双击 / ShellExecuteW / CreateProcessW 严格 NULL 句柄）⇒ print() 静默丢弃、
  lastResort 也写不出去；
- 但**本品**在 client/app.py:136 `import webview`，pywebview 的 webview/http.py:7-13（守卫
  `sys.platform == 'win32' and ('pythonw.exe' in sys.executable or sys.frozen)`）会把为 None 的流
  换成 `open(os.devnull, 'w')` ⇒ 产品态拿到的是 devnull 的 TextIOWrapper（本模块自证记录里
  stdout_is_none=False），**写入同样不可见**。⇒ 文件出口是 console=False 下唯一的持久输出面
  （task-4 真 exe 两态实测：console=1 / console=0、冻结态 <数据根>/logs/zhilan.log 均真落盘且含
  app.* 记录）。注：`--smoke` 路径不 import webview ⇒ 该路径下 sys.stdout 仍为 None（--smoke-out
  的 JSON 判据仍是必要设计，不因本条变更而放宽）。

本模块给 app.* 命名空间接一条**与 console 开关解耦**的文件出口：<数据根>/logs/zhilan.log。

## 设计要点（每条都对应一条判据腿，见 backend/tests/test_bug070_app_log_sink.py）

1. 落点 = <数据根>/logs/zhilan.log，数据根**复用既有解析器** app.core.env_store
   （冻结态 = 可执行文件所在目录；开发态 = 仓库根）。⛔ 本模块**不新增配置键**：
   v2 从来没有 ZHILAN_DATA_DIR 这个键（计划件 缺陷清单-修复计划.md:63 的举例），
   既有唯一解析器就是 env_store 的 ROOT_DIR / resolve_data_root()。
2. 与 console 解耦：装配不看 sys.stdout / sys.stderr 是否为 None，也不动 ZL_CONSOLE 任何面。
3. 轮转 = RotatingFileHandler（纯标准库）：maxBytes = 5 MiB、backupCount = 3。
4. fail-open：日志目录建不出来 / 文件打不开 ⇒ setup 返回 None 且不上抛（启动流程一字不变）；
   装配后**运行期写失败**（磁盘满 / 文件被锁）也一律吞掉并计数（write_failure_count()），
   绝不影响请求处理与主流程。
5. 脱敏（只作用于文件出口，⛔ 不动控制台既有输出）：Authorization / Bearer / api_key 类键值掩码；
   单条记录超 MAX_MESSAGE_CHARS 截断（把「不落正文全文」落成可判定事实）。
   实现放在**格式化时刻**（_RedactingFormatter），⛔ 不用 filter 改 LogRecord：同一条记录会被依次
   交给同一 logger 上的所有 handler 并沿 propagate 上行，就地改 record 会把掩码泄漏到控制台面。
6. 控制台降级镜像：挂上本模块的 handler 后 logging.lastResort 不再触发（callHandlers 只要找到一个
   handler 就不会用 lastResort）⇒ 为**零回归**复刻同语义镜像 handler（WARNING+ / 格式 %(message)s /
   sys.stderr 为 None 时静默跳过），保证 console=True 形态下 app.* WARNING 的既有可见面不失。

## 覆盖面限度（如实登记，不得粉饰）

- 只覆盖 app.* 命名空间。main.py::lifespan 三步迁移的 print 启动信息**仍只进控制台**，
  冻结态 console=False 下照旧不可见；uvicorn 自己的 access/error 日志（独立 handler、propagate=False）
  与第三方库（loguru 等）**不进本文件**。
- 文件出口对消息及完整异常因果链逐行脱敏，不改共享 LogRecord；控制台维持既有输出。
- 「脱敏」= 模式匹配 + 长度截断，不是内容级语义识别：不含敏感键名形态的长正文只靠截断兜住。
- 只在**后端进程启动**时装配（main.py::lifespan 首句）；--smoke 等不跑 lifespan 的路径不装配。
"""

from __future__ import annotations

import logging
import copy
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core import env_store

LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "zhilan.log"
APP_LOGGER_NAME = "app"

#: 挂在本模块装配的文件 handler 上的标记（幂等 / 拆除时只认自己这一只）。
HANDLER_MARK = "_zhilan_app_file_sink"
#: 挂在控制台降级镜像 handler 上的标记。
MIRROR_MARK = "_zhilan_app_stderr_mirror"

#: 轮转阈值与份数（拍板档：5 MiB x 3 份）。
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3
#: 单条记录落盘的最大字符数（超出即截断：一条日志不该带走整段正文）。
MAX_MESSAGE_CHARS = 2000

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 掩码占位串（测试与排障都按它判定「这一处已被掩掉」）。
MASK = "***已脱敏***"

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization 头（含 Bearer 形态）：整段值掩码
    (re.compile(r"(?i)\b(authorization)\b[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?[^\s,;\"'\)\]\}]+"), r"\1: " + MASK),
    # 裸 Bearer 令牌
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/=]{6,}"), r"\1 " + MASK),
    # OpenAI 系明文 Key（sk-... 形态）
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), "sk-" + MASK),
    # key=value / "key": "value" 形态的敏感键（含 openai_api_key 这类带前缀的键名）
    (
        re.compile(
            r"(?i)([A-Za-z0-9_\-]*(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token"
            r"|auth[_-]?token|client[_-]?secret|secret|password|passwd|pwd))"
            r"\b[\"']?\s*[:=]\s*[\"']?[^\s\"',;\)\]\}]+[\"']?"
        ),
        r"\1=" + MASK,
    ),
    # URL 查询串里的敏感参数
    (re.compile(r"(?i)([?&](?:key|api[_-]?key|token|access[_-]?token|password)=)[^&\s]+"), r"\1" + MASK),
)

_STATE: dict[str, object] = {"path": None, "previous_level": None, "write_failures": 0}


def resolve_data_root() -> Path:
    """数据根（调用时解析）——**复用既有解析器**，本模块不引入任何配置键。"""
    return env_store.resolve_data_root()


def resolve_log_dir() -> Path:
    """日志目录 = <数据根>/logs。"""
    return resolve_data_root() / LOG_DIR_NAME


def log_file_path(log_dir: Path | None = None) -> Path:
    """日志文件路径；log_dir 省略时按 <数据根>/logs 解析（纯函数，不建目录、不写盘）。"""
    base = resolve_log_dir() if log_dir is None else Path(log_dir)
    return base / LOG_FILE_NAME


def redact(text: str) -> str:
    """对单条日志文本做脱敏 + 长度截断（纯函数，可单测）。"""
    out = text
    for pattern, repl in _REDACTIONS:
        out = pattern.sub(repl, out)
    if len(out) > MAX_MESSAGE_CHARS:
        out = out[:MAX_MESSAGE_CHARS] + "……[已截断：原 " + str(len(out)) + " 字符]"
    return out


class _RecordView:
    """样式层只需要一个带 __dict__ 的对象（stdlib 三种 Formatter 样式都从 record.__dict__ 取值）。"""

    __slots__ = ("__dict__",)

    def __init__(self, values: dict) -> None:
        self.__dict__ = values


class _RedactingFormatter(logging.Formatter):
    """文件出口专用：在**格式化时刻**脱敏，⛔ 不改动调用方的 LogRecord。

    为什么不能用 filter 就地改 record.msg：同一条 LogRecord 会被**依次交给同一个 logger 上的所有
    handler**，也会沿 propagate 交给上层 handler —— 就地改它会让控制台镜像、root 上的捕获 handler
    （pytest 的 caplog 面）与第三方 handler 一起看到掩码后的文本，把「控制台既有输出零变化」这条
    零回归边界打红（首轮载体腿8 实测就是这个红）。改成格式化时刻处理后，其它 handler 依旧拿到原文。
    """

    def format(self, record: logging.LogRecord) -> str:
        view = copy.copy(record)
        view.msg, view.args = redact(record.getMessage()), ()
        # Another handler may have cached an unredacted traceback.
        view.exc_text = None
        return super().format(view)

    def formatException(self, exc_info) -> str:  # noqa: N802
        # Bound each line while preserving frames and both ends of a cause chain.
        return "\n".join(redact(line) for line in super().formatException(exc_info).split("\n"))

    def formatStack(self, stack_info: str) -> str:  # noqa: N802
        return "\n".join(redact(line) for line in super().formatStack(stack_info).split("\n"))


class _FailOpenRotatingFileHandler(RotatingFileHandler):
    """写失败一律 fail-open：不上抛、不打断调用点（计数供排障与判据使用）。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except Exception:
            _STATE["write_failures"] = int(_STATE["write_failures"]) + 1

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        """默认实现会往 sys.stderr 打（冻结态 stderr 是 None）⇒ 这里只计数。"""
        _STATE["write_failures"] = int(_STATE["write_failures"]) + 1


class _StderrMirrorHandler(logging.Handler):
    """复刻 logging.lastResort 的可见面：WARNING+ / 裸消息 / 动态 sys.stderr / None 时静默。"""

    terminator = "\n"

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        stream = sys.stderr
        if stream is None:
            return
        try:
            rendered = (
                _RedactingFormatter("%(message)s").format(record)
                if record.exc_info or record.exc_text else self.format(record)
            )
            stream.write(rendered + self.terminator)
            stream.flush()
        except Exception:
            pass


def write_failure_count() -> int:
    """装配以来被吞掉的写失败次数（fail-open 的可观测出口）。"""
    return int(_STATE["write_failures"])


def installed_log_file() -> Path | None:
    """当前已装配的文件出口路径；未装配返回 None。"""
    handler = _installed_file_handler()
    if handler is None:
        return None
    return Path(str(getattr(handler, "baseFilename", "")))


def _installed_file_handler() -> logging.Handler | None:
    for handler in logging.getLogger(APP_LOGGER_NAME).handlers:
        if getattr(handler, HANDLER_MARK, False):
            return handler
    return None


def _detach(logger: logging.Logger, handler: logging.Handler) -> None:
    logger.removeHandler(handler)
    try:
        handler.close()
    except Exception:
        pass


def setup_app_logging(
    *,
    log_dir: Path | None = None,
    level: int = logging.INFO,
    max_bytes: int = MAX_BYTES,
    backup_count: int = BACKUP_COUNT,
) -> Path | None:
    """装配 app.* 的文件出口（幂等）。任何失败都 fail-open：返回 None，绝不上抛。"""
    try:
        target = log_file_path(log_dir)
        logger = logging.getLogger(APP_LOGGER_NAME)

        current = _installed_file_handler()
        if current is not None:
            if Path(str(getattr(current, "baseFilename", ""))) == target:
                return target
            _detach(logger, current)  # 换落点（测试 / 热重载）：先退旧再装新

        target.parent.mkdir(parents=True, exist_ok=True)
        handler = _FailOpenRotatingFileHandler(
            target, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        handler.setLevel(level)
        handler.setFormatter(_RedactingFormatter(LOG_FORMAT, DATE_FORMAT))
        setattr(handler, HANDLER_MARK, True)
        logger.addHandler(handler)

        if not any(getattr(h, MIRROR_MARK, False) for h in logger.handlers):
            mirror = _StderrMirrorHandler()
            setattr(mirror, MIRROR_MARK, True)
            logger.addHandler(mirror)

        # 只提不降：既有显式级别（例如别人钉的 WARNING）不被本模块放松。
        if logger.level == logging.NOTSET or logger.level > level:
            _STATE["previous_level"] = logger.level
            logger.setLevel(level)

        _STATE["path"] = target
        _STATE["write_failures"] = 0
        _bootstrap(handler)
        return target
    except Exception:
        return None


def _bootstrap(handler: logging.Handler) -> None:
    """装配成功后的首条记录：证明 sink 真的在写，并留下排障需要的运行形态读数。"""
    logging.getLogger(__name__).info(
        "应用层文件日志已装配：file=%s frozen=%s stdout_is_none=%s level=%s maxBytes=%s backupCount=%s",
        getattr(handler, "baseFilename", "?"),
        bool(getattr(sys, "frozen", False)),
        sys.stdout is None,
        logging.getLevelName(logging.getLogger(APP_LOGGER_NAME).level),
        getattr(handler, "maxBytes", "?"),
        getattr(handler, "backupCount", "?"),
    )


def remove_app_logging() -> None:
    """拆除本模块装配的 handler（测试 / 热重载用）；级别还原为装配前的值。"""
    logger = logging.getLogger(APP_LOGGER_NAME)
    for handler in list(logger.handlers):
        if getattr(handler, HANDLER_MARK, False) or getattr(handler, MIRROR_MARK, False):
            _detach(logger, handler)
    previous = _STATE.get("previous_level")
    if isinstance(previous, int):
        logger.setLevel(previous)
    _STATE["path"] = None
    _STATE["previous_level"] = None
    _STATE["write_failures"] = 0
