"""回归修复 剩余开放面判据载体：应用层日志的**文件出口**（批次 1）。

## 被测缺陷

冻结态（打包后）app.* 的日志无处可去：backend/app 侧零 FileHandler / basicConfig / addHandler；
console=True 时只经 logging.lastResort 落 sys.stderr；console=False（ZL_CONSOLE=0，2026-09-27 起为
发布构建形态）下 sys.stdout / sys.stderr 均为 None ⇒ print() 静默丢弃、lastResort 也写不出去。
修复物 = app/core/app_logging.py（<数据根>/logs/zhilan.log 文件出口，main.py::lifespan 首句装配）。

## 腿与判别力（「去掉 handler」回退实验 = 把 setup_app_logging 变成空实现）

- 腿 1（红）主腿：sys.stdout / sys.stderr = None（冻结态等价）下装配 ⇒ 文件真生成、含 app.* 记录、中文不乱码；
- 腿 2（红）：maxBytes=4096 / backupCount=2 下真出 .1 / .2，且不出 .3；
- 腿 3（红）：默认参数 = 5 MiB x 3 份 + utf-8 编码；
- 腿 4（**两态都绿，不承担修复判别力**）：数据根走 env_store 既有解析器（开发态 = 仓库根；
  冻结态 = 可执行文件所在目录）—— 钉的是「复用既有键、不新增配置键」这个决策，不是 handler 的存在性；
- 腿 5（红）：装配期 fail-open（数据根是普通文件 ⇒ 返回 None、不抛、主流程照常）；
- 腿 6（红）：运行期写失败 fail-open（吞掉 + 计数 +1，绝不打断调用点）；
- 腿 7（红）：脱敏 + 截断（Authorization / Bearer / sk- / key=value / 超长正文）；
- 腿 8（红）：脱敏只作用文件出口，控制台面一字不改（零回归边界）；
- 腿 9（红）：控制台降级镜像 —— stderr 有值 ⇒ WARNING 仍可见；stderr=None ⇒ 静默不抛；
- 腿 10（红）：同落点重复装配幂等（不重复出记录、只有一个文件 handler）；
- 腿 11（绿）：装配点接线 —— lifespan 首句装 sink、且早于三步迁移（桩替 setup，只验顺序）。

## 覆盖面限度（如实登记）

- ⛔ 本文件**不**承担「真 exe 两态（ZL_CONSOLE=1/0）的 PE Subsystem 与安装态落盘」取证（那是 task-4）；
  腿 1 构造的是**冻结态等价形态**（sys.stdout=None），不是真冻结进程。
- 腿 4 的「冻结态」面用 sys.frozen / sys.executable / os._MEIPASS 三处桩模拟，不是真 exe。
- ⛔ 不得由本文件推出「070 已修」「桌面端日志已可见」「安装态日志已可见」。
"""

from __future__ import annotations

import asyncio
import importlib
import io
import logging
import re
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.core import app_logging, env_store
from app.core.app_logging import (
    APP_LOGGER_NAME,
    BACKUP_COUNT,
    HANDLER_MARK,
    LOG_FILE_NAME,
    MASK,
    MAX_BYTES,
    setup_app_logging,
)

LOG_NAME = "app.demo." + LOG_FILE_NAME.replace(".log", "")


def _file_handlers() -> list[logging.Handler]:
    logger = logging.getLogger(APP_LOGGER_NAME)
    return [h for h in logger.handlers if getattr(h, HANDLER_MARK, False)]


@contextmanager
def _sink(tmp_path, **kwargs):
    """按落点装配文件出口，退出时拆除（不依赖 setup 的返回值 ⇒ 回退时腿真 F 而不是 error）。"""
    setup_app_logging(log_dir=tmp_path, **kwargs)
    try:
        yield
    finally:
        app_logging.remove_app_logging()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- 腿 1：主腿 —— 冻结态等价（sys.stdout / sys.stderr 均为 None）下文件出口仍真落盘 -----------------


def test_leg1_file_sink_survives_stdout_none(tmp_path, monkeypatch):
    """console=False 的等价形态：print() 与 lastResort 都无处可写，文件出口必须照写。"""
    log_path = tmp_path / LOG_FILE_NAME
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    with _sink(tmp_path, level=logging.INFO):
        logging.getLogger(LOG_NAME + ".leg1").warning("冻结态排障：中文不乱码，%s", "含占位符")
        logger = logging.getLogger(APP_LOGGER_NAME)
        # 装配后 app.* 至少有一个 handler ⇒ logging.lastResort 不再被使用（否则消息会静默消失）
        assert logger.handlers, "装配后 app.* 一个 handler 都没有 ⇒ lastResort 路径，文件出口没装"

    assert log_path.exists(), "sys.stdout/stderr 为 None 时日志文件未生成"
    text = _read(log_path)
    assert "中文不乱码" in text
    assert "含占位符" in text
    assert re.search(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} WARNING app\.demo\.zhilan\.leg1 ", text, re.M)


# --- 腿 2 / 腿 3：轮转阈值与份数 ---------------------------------------------------------------


def test_leg2_rotation_threshold_and_backup_count(tmp_path):
    with _sink(tmp_path, max_bytes=4096, backup_count=2) as _:
        logger = logging.getLogger(LOG_NAME + ".leg2")
        for i in range(60):
            logger.info("轮转填充 %03d %s", i, "x" * 300)

    assert (tmp_path / LOG_FILE_NAME).exists()
    assert (tmp_path / (LOG_FILE_NAME + ".1")).exists(), "未滚出 .1 ⇒ 轮转没生效"
    assert (tmp_path / (LOG_FILE_NAME + ".2")).exists(), "未滚出 .2 ⇒ 份数不足"
    assert not (tmp_path / (LOG_FILE_NAME + ".3")).exists(), "滚出 .3 ⇒ backupCount 上限失守"


def test_leg3_default_limits_and_encoding(tmp_path):
    with _sink(tmp_path) as _:
        handler = _file_handlers()[0]
        assert handler.maxBytes == MAX_BYTES == 5 * 1024 * 1024
        assert handler.backupCount == BACKUP_COUNT == 3
        assert str(getattr(handler, "encoding", "")).lower() == "utf-8"
        logging.getLogger(LOG_NAME + ".leg3").info("默认档写入：中文")


# --- 腿 4：数据根（两态都绿，不承担修复判别力） ---------------------------------------------------


def test_leg4_data_root_two_states(tmp_path, monkeypatch):
    logger = logging.getLogger(APP_LOGGER_NAME)
    assert app_logging.resolve_log_dir() == env_store.resolve_data_root() / app_logging.LOG_DIR_NAME
    # 开发态：仓库根 = backend 的上一级（本文件在 backend/tests/ 下）
    repo_root = Path(__file__).resolve().parents[2]
    assert app_logging.resolve_data_root() == repo_root
    assert env_store.resolve_data_root() == repo_root

    # 冻结态：数据根 = 可执行文件所在目录 ⇒ 日志落 <exe 目录>/logs/（普通用户可写，⛔ 不是 _MEIPASS)
    exe_dir = tmp_path / "installed"
    exe_dir.mkdir()
    exe = exe_dir / "zhilan.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "meipass"), raising=False)

    assert app_logging.resolve_data_root() == exe_dir
    assert app_logging.log_file_path() == exe_dir / app_logging.LOG_DIR_NAME / LOG_FILE_NAME
    assert not (exe_dir / app_logging.LOG_DIR_NAME).exists(), "只读解析不应建目录"
    assert not logger.handlers or not _file_handlers()


# --- 腿 5 / 腿 6：fail-open（装配期 / 运行期） --------------------------------------------------


def test_leg5_setup_fails_open_when_data_root_unusable(tmp_path):
    """数据根位置被一个**普通文件**占住 ⇒ 建不出 logs/ ⇒ 返回 None 且不上抛。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("我是文件，不是目录", encoding="utf-8")

    assert setup_app_logging(log_dir=blocker) is None
    logging.getLogger(LOG_NAME + ".leg5").warning("装配失败后主流程照常记录")
    assert not _file_handlers(), "装配失败却还留了 handler"


def test_leg6_runtime_write_failure_is_swallowed(tmp_path, monkeypatch):
    with _sink(tmp_path):
        handler = _file_handlers()[0]
        before = app_logging.write_failure_count()

        class _BrokenStream:
            """磁盘满 / 文件被锁的等价形态：底层 write/flush/seek/tell 一律抛 OSError。"""

            def write(self, _data):
                raise OSError(28, "No space left on device")

            def flush(self):
                raise OSError(28, "No space left on device")

            def seek(self, *_args):
                raise OSError(28, "No space left on device")

            def tell(self):
                raise OSError(28, "No space left on device")

        # ⛔ 不打 stdlib 基类（一改 logging.FileHandler.emit，pytest 自己的捕获 handler 也跟着炸：
        #    实测 self = <_FileHandler \.\nul>，OSError 直接穿过 Handler.handle 上抛）。
        #    只把**本实例的底层流**换坏 ⇒ shouldRollover 先炸，BaseRotatingHandler.emit(:71) 的
        #    except 转 handleError，本类把它复写成只计数 ⇒ 绝不上抛到调用点。
        monkeypatch.setattr(handler, "stream", _BrokenStream())
        logging.getLogger(LOG_NAME + ".leg6").warning("写失败时也不许抛回调用点")
        monkeypatch.undo()

        assert app_logging.write_failure_count() >= before + 1, "写失败没被计数 ⇒ 无法观测"
        assert handler in _file_handlers(), "写失败后 handler 被摘掉 ⇒ 事后无法继续写"
        logging.getLogger(LOG_NAME + ".leg6").info("恢复后继续写文件")
        assert "恢复后继续写文件" in _read(tmp_path / LOG_FILE_NAME)


# --- 腿 7：脱敏与截断 ------------------------------------------------------------------------


def test_leg7_redaction_and_truncation(tmp_path):
    with _sink(tmp_path) as _:
        logger = logging.getLogger(LOG_NAME + ".leg7")
        logger.warning("Authorization: Bearer abcdefghijklmnop 访问失败")
        logger.warning("调用模型 sk-proj-AbCdEf1234567890 返回 401")
        logger.warning("openai_api_key=%s 与 password: %s 不得落盘", "sk-proj-ZZZZZZZZZZ123456", "hunter2")
        logger.warning("https://api.example.com/v1?api_key=SEKRET123&x=1 请求超时")
        logger.warning("超长正文：%s", "甲" * 3000)

    text = _read(tmp_path / LOG_FILE_NAME)
    assert "abcdefghijklmnop" not in text
    assert "sk-proj-AbCdEf1234567890" not in text
    assert "sk-proj-ZZZZZZZZZZ123456" not in text
    assert "hunter2" not in text
    assert "SEKRET123" not in text
    assert text.count(MASK) >= 5
    assert "已截断：原 3005 字符" in text
    assert "甲" * 3000 not in text


# --- 腿 8：脱敏只作用文件出口，控制台面一字不改（零回归边界） --------------------------------------


def test_leg8_redaction_does_not_touch_console(tmp_path, monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)

    with _sink(tmp_path) as _:
        record = logging.getLogger(LOG_NAME + ".leg8").makeRecord(
            LOG_NAME + ".leg8",
            logging.WARNING,
            __file__,
            1,
            "Authorization: Bearer abcdefghijklmnop",
            (),
            None,
        )
        # 控制台镜像面：脱敏在文件 handler 的**格式化时刻**做、不改 LogRecord ⇒ 原文照旧（零回归）
        logging.getLogger(APP_LOGGER_NAME).handle(record)

    assert "abcdefghijklmnop" in stream.getvalue()
    assert "abcdefghijklmnop" not in _read(tmp_path / LOG_FILE_NAME)


# --- 腿 9：控制台降级镜像（lastResort 语义兼容） -------------------------------------------------


def test_leg9_console_mirror_keeps_warning_visible(tmp_path, monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)

    with _sink(tmp_path) as _:
        logging.getLogger(LOG_NAME + ".leg9").warning("控制台仍应可见")
        logging.getLogger(LOG_NAME + ".leg9").info("INFO 不进控制台")  # lastResort 是 WARNING 档

    assert "控制台仍应可见" in stream.getvalue()
    assert "INFO 不进控制台" not in stream.getvalue()


def test_leg9b_console_mirror_silent_when_stderr_none(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "stdout", None)

    with _sink(tmp_path) as _:
        logging.getLogger(LOG_NAME + ".leg9b").warning("stderr 为 None 时不许抛")

    assert "stderr 为 None 时不许抛" in _read(tmp_path / LOG_FILE_NAME)


# --- 腿 10：幂等 -----------------------------------------------------------------------------


def test_leg10_setup_is_idempotent(tmp_path):
    setup_app_logging(log_dir=tmp_path)
    setup_app_logging(log_dir=tmp_path)
    try:
        assert len(_file_handlers()) == 1, "重复装配出现多只文件 handler ⇒ 日志会重复"
        logging.getLogger(LOG_NAME + ".leg10").warning("唯一一次装配")
    finally:
        app_logging.remove_app_logging()

    text = _read(tmp_path / LOG_FILE_NAME)
    assert text.count("唯一一次装配") == 1


# --- 腿 11：装配点接线（lifespan 首句、早于三步迁移） ---------------------------------------------
# ⚠️ 本腿用桩替换 setup_app_logging ⇒ 与文件出口是否真的存在**无关**，只证接线位置。


def test_leg11_lifespan_wires_sink_before_migrations(monkeypatch):
    main = importlib.import_module("main")
    order: list[str] = []

    monkeypatch.setattr(main, "setup_app_logging", lambda **kw: order.append("log_sink"))
    monkeypatch.setattr(main, "ensure_service_registry", lambda *a, **k: order.append("registry"))
    monkeypatch.setattr(main, "migrate_legacy_rerank", lambda *a, **k: order.append("rerank"))
    monkeypatch.setattr(main, "run_migrations", lambda: order.append("migrations"))

    class _NoopEngine:
        async def dispose(self):
            order.append("dispose")

    # 退出段三件桩掉：本腿只验启动段顺序，不让无关面（真 redis / 真向量库）干扰。
    # ⚠️ close_vector_store 用 raising=False —— main.py 现读**没有** import 它（预存在 NameError，
    #    不在本批修复范围，已单列进回报件）；否则本腿会因无关缺陷红掉。
    async def _close_redis():  # main.py:117 是 `await close_redis()` ⇒ 桩必须是协程
        order.append("close_redis")

    monkeypatch.setattr(main, "engine", _NoopEngine())
    monkeypatch.setattr(main, "close_redis", _close_redis)
    monkeypatch.setattr(main, "close_vector_store", lambda: order.append("close_vector_store"), raising=False)

    # asynccontextmanager 的产物只支持 async with（同步 with 会 TypeError）
    async def _run():
        async with main.lifespan(None):
            pass

    asyncio.run(_run())

    assert order[:4] == ["log_sink", "registry", "rerank", "migrations"], f"启动段接线顺序不对：{order}"
