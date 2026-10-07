"""回归修复 应用层日志文件出口 · **非作者对抗核验**载体（批次 1 · task-5）。

## 独立性声明
- 作者 = fix-schema（其载体 = backend/tests/test_bug070_app_log_sink.py）。本文件由**非作者**编写：
  不 import、不复制作者载体的夹具 / 断言；断言均按产品**行为面**独立重写。
- 本文件**只读**产品代码：⛔ 不改 backend/app/**、⛔ 不改 backend/main.py —— 发现缺陷只回报 Lead，
  绝不就地修（就地修会毁掉非作者独立性）。
- 本文件**不承担**真 exe / PE Subsystem 两态取证（task-4 的面）。这里用「sys.stdout = sys.stderr =
  None + ZL_CONSOLE=0」复现冻结态在**日志侧**的等价条件，不篡改 PE。

## 判据腿与变异敏感度（变异体定义见 _bugfix10-scratch/fix-wire/rollback/task5/kill_task5.ps1）
- M1 = 去 handler 装配（app_logging.setup_app_logging 内 'logger.addHandler(handler)' → pass）
- M2 = 去装配点（main.py::lifespan 的 'setup_app_logging()' → None）
- M3 = 关脱敏（_RedactingFormatter.formatMessage 内 'masked = redact(original)' → masked = original）
- M4 = 去轮转（__init__ 调用点 'target, maxBytes=max_bytes' → 'target, maxBytes=10**9'）

| 腿 | 函数 | M1 | M2 | M3 | M4 |
|----|------|----|----|----|----|
| L1 | test_frozen_equivalent_writes_real_file_with_app_records | 红 | 绿 | 绿 | 绿 |
| L2 | test_rotation_threshold_and_backup_count_are_honoured | 红 | 绿 | 绿 | 红 |
| L3 | test_default_thresholds_and_formatter_are_the_board_values | 红 | 绿 | 绿 | 红 |
| L4 | test_constant_anchors_are_the_board_values（纯常量 pin） | 绿 | 绿 | 绿 | 绿 |
| L5 | test_decoy_env_key_is_ignored_and_frozen_root_is_executable_dir | 绿 | 绿 | 绿 | 绿 |
| L6 | test_fail_open_when_log_dir_cannot_be_created | 绿 | 绿 | 绿 | 绿 |
| L7 | test_runtime_write_failure_is_fail_open_and_counted | 红 | 绿 | 绿 | 绿 |
| L8 | test_redaction_only_on_file_face_console_and_root_see_raw | 红 | 绿 | 红 | 绿 |
| L9 | test_exception_stack_text_is_not_redacted_registration_leg | 红 | 绿 | 红 | 绿 |
| L10 | test_non_app_namespaces_do_not_reach_the_file | 红 | 绿 | 绿 | 绿 |
| L11 | test_idempotent_and_retarget_swaps_single_handler | 红 | 绿 | 绿 | 绿 |
| L12 | test_lifespan_assembly_point_installs_real_file_sink | 红 | 红 | 绿 | 绿 |
| L13 | test_redact_pure_function_attack_surface（纯函数） | 绿 | 绿 | 绿 | 绿 |
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
from types import SimpleNamespace

import pytest

from app.core import app_logging, env_store

#: 独立锚（不引用产品常量）：产品若改标记串/档位，本组断言必须一起改 —— 由 L4 逐字钉住。
EXPECTED_HANDLER_MARK = "_zhilan_app_file_sink"
EXPECTED_MIRROR_MARK = "_zhilan_app_stderr_mirror"

_FROZEN_LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} WARNING app\.verify\.frozen 冻结态等价记录-\S+$",
    re.M,
)


@pytest.fixture(autouse=True)
def _isolate_app_logger():
    """每条腿前后把 app 日志面还原成「未装配」——防跨用例 handler / 级别泄漏。"""
    logger = logging.getLogger(app_logging.APP_LOGGER_NAME)
    saved_handlers = list(logger.handlers)
    saved_level, saved_propagate, saved_disabled = logger.level, logger.propagate, logger.disabled
    app_logging.remove_app_logging()
    try:
        yield
    finally:
        app_logging.remove_app_logging()
        logger.handlers[:] = saved_handlers
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate
        logger.disabled = saved_disabled


@contextmanager
def _sink(log_dir: Path, **kwargs):
    """装配文件出口并在退出时拆除（本文件自己的夹具，不复用作者实现）。"""
    target = app_logging.setup_app_logging(log_dir=log_dir, **kwargs)
    assert target is not None, "装配返回 None：本腿的前提是「文件出口可用」"
    try:
        yield Path(target)
    finally:
        app_logging.remove_app_logging()


def _freeze(monkeypatch) -> None:
    """冻结态**日志侧**等价条件：两个标准流全为 None（发布形态 ZL_CONSOLE=0）。"""
    monkeypatch.setenv("ZL_CONSOLE", "0")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)


def _app_handlers() -> list[logging.Handler]:
    return list(logging.getLogger(app_logging.APP_LOGGER_NAME).handlers)


def _marked_handlers() -> list[logging.Handler]:
    return [h for h in _app_handlers() if getattr(h, EXPECTED_HANDLER_MARK, False)]


def _mirror_handlers() -> list[logging.Handler]:
    return [h for h in _app_handlers() if getattr(h, EXPECTED_MIRROR_MARK, False)]


# --------------------------------------------------------------------------- L1


def test_frozen_equivalent_writes_real_file_with_app_records(tmp_path, monkeypatch):
    """两条标准流全 None 时，文件真生成、含 app.* 记录且带 bootstrap 运行形态读数。"""
    _freeze(monkeypatch)
    with _sink(tmp_path / "logs") as log_path:
        assert app_logging.installed_log_file() == log_path
        assert len(_marked_handlers()) == 1
        logging.getLogger("app.verify.frozen").warning("冻结态等价记录-FROZENLEG")
    assert log_path.exists() and log_path.stat().st_size > 0
    text = log_path.read_text(encoding="utf-8")
    assert "应用层文件日志已装配" in text, "装配首条未落盘 ⇒ sink 没在写"
    assert "stdout_is_none=True" in text, "bootstrap 读数必须记下「标准流为 None」这一形态"
    assert "冻结态等价记录-FROZENLEG" in text
    assert _FROZEN_LINE.search(text), text[-500:]


# --------------------------------------------------------------------------- L2


def test_rotation_threshold_and_backup_count_are_honoured(tmp_path, monkeypatch):
    """入参 max_bytes/backup_count 必须真的生效（小阈值复算轮转），old 内容真被挤出。"""
    _freeze(monkeypatch)
    log_path = tmp_path / "logs" / "zhilan.log"
    with _sink(log_path.parent, max_bytes=2000, backup_count=2) as installed:
        handler = _marked_handlers()[0]
        assert handler.maxBytes == 2000 and handler.backupCount == 2
        logger = logging.getLogger("app.verify.rotate")
        for index in range(40):
            logger.warning("填充-%03d-%s", index, "x" * 180)
    live = installed.read_text(encoding="utf-8")
    assert "填充-039" in live, "最新一条必须在活文件里"
    assert len(live.encode("utf-8")) <= 2000 + 400, "活文件体积超阈值 ⇒ 真实轮转没生效"
    assert "填充-000" not in live, "老内容没被轮转挤出活文件"
    backup_1 = installed.with_name(installed.name + ".1")
    backup_2 = installed.with_name(installed.name + ".2")
    assert backup_1.exists() and backup_2.exists(), "backupCount=2 必须产生 .1/.2"
    assert not installed.with_name(installed.name + ".3").exists(), "多出的第 3 份 ⇒ 保留份数没生效"
    backup_text = backup_1.read_text(encoding="utf-8") + backup_2.read_text(encoding="utf-8")
    assert any(f"填充-{index:03d}" in backup_text for index in range(40)), "轮转走的历史记录不在备份里"


# --------------------------------------------------------------------------- L3


def test_default_thresholds_and_formatter_are_the_board_values(tmp_path):
    """默认档 = 5 MiB × 3、utf-8、_RedactingFormatter、app logger 级别 INFO。"""
    with _sink(tmp_path / "logs") as log_path:
        handler = _marked_handlers()[0]
        assert handler.maxBytes == 5 * 1024 * 1024 == app_logging.MAX_BYTES
        assert handler.backupCount == 3 == app_logging.BACKUP_COUNT
        assert str(handler.encoding).lower().replace("-", "") == "utf8"
        assert isinstance(handler.formatter, app_logging._RedactingFormatter)
        assert logging.getLogger("app").level == logging.INFO
    assert log_path.name == "zhilan.log" and log_path.parent.name == "logs"


# --------------------------------------------------------------------------- L4


def test_constant_anchors_are_the_board_values():
    """独立锚：常量字面值逐字钉死（改档位/标记/掩码串必须同笔改这条）。"""
    assert app_logging.LOG_DIR_NAME == "logs"
    assert app_logging.LOG_FILE_NAME == "zhilan.log"
    assert app_logging.APP_LOGGER_NAME == "app"
    assert app_logging.HANDLER_MARK == EXPECTED_HANDLER_MARK
    assert app_logging.MIRROR_MARK == EXPECTED_MIRROR_MARK
    assert app_logging.MAX_BYTES == 5 * 1024 * 1024
    assert app_logging.BACKUP_COUNT == 3
    assert app_logging.MAX_MESSAGE_CHARS == 2000
    assert app_logging.MASK == "***已脱敏***"


# --------------------------------------------------------------------------- L5


def test_decoy_env_key_is_ignored_and_frozen_root_is_executable_dir(tmp_path, monkeypatch):
    """数据根只认 env_store 既有解析器：诱饵 env 键必须被忽略；冻结态 = exe 所在目录。"""
    decoy = tmp_path / "decoy-data-root"
    monkeypatch.setenv("ZHILAN_DATA_DIR", str(decoy))
    resolved = app_logging.log_file_path()
    assert resolved == env_store.ROOT_DIR / "logs" / "zhilan.log"
    assert str(decoy) not in str(resolved), "有人新增了配置键 ⇒ 与「不新增配置键」的口径冲突"
    assert not decoy.exists(), "解析数据根不应建目录"

    fake_exe = tmp_path / "install" / "zhilan.exe"
    fake_exe.parent.mkdir(parents=True, exist_ok=True)
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    assert app_logging.resolve_data_root() == fake_exe.parent
    assert app_logging.resolve_log_dir() == fake_exe.parent / "logs"
    assert not (fake_exe.parent / "logs").exists(), "解析本身不该建目录（建目录只发生在装配时）"


# --------------------------------------------------------------------------- L6


def test_fail_open_when_log_dir_cannot_be_created(tmp_path, monkeypatch):
    """数据根不可用（父是普通文件 / 路径非法）⇒ 返回 None、不留 handler、主流程不受影响。"""
    _freeze(monkeypatch)
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("占位", encoding="utf-8")
    assert app_logging.setup_app_logging(log_dir=blocker / "logs") is None
    assert app_logging.installed_log_file() is None
    assert _marked_handlers() == [] and _mirror_handlers() == []
    # 另一类异常（ValueError 而非 OSError）：带 NUL 的路径也必须 fail-open
    assert app_logging.setup_app_logging(log_dir=Path("bad\0dir")) is None

    def _business_step() -> str:
        logging.getLogger("app.verify.business").error("数据根不可写：业务日志（应被静默吞掉）")
        return "ok"

    assert _business_step() == "ok", "装配失败后业务调用被日志面打断 ⇒ fail-open 破"


# --------------------------------------------------------------------------- L7


def test_runtime_write_failure_is_fail_open_and_counted(tmp_path, monkeypatch):
    """运行期写失败（真实已关闭文件对象）⇒ 调用点不抛、逐次计数、不摘 handler、可恢复。"""
    _freeze(monkeypatch)
    patch = pytest.MonkeyPatch()
    try:
        with _sink(tmp_path / "logs") as log_path:
            handler = _marked_handlers()[0]
            assert app_logging.write_failure_count() == 0
            closed = tmp_path / "closed-stream.bin"
            stream = open(closed, "w", encoding="utf-8")
            stream.write("x")
            stream.close()
            patch.setattr(handler, "stream", stream)  # 已关闭的真实文件：write/seek 抛 ValueError

            def _request_like_step() -> str:
                logging.getLogger("app.verify.writefail").warning("运行期写失败-CLOSEDSTREAM")
                return "served"

            assert _request_like_step() == "served"
            assert app_logging.write_failure_count() == 1, "写失败没被计数 ⇒ fail-open 不可观测"
            assert _marked_handlers()[0] is handler, "写失败把 handler 摘了 ⇒ 后续日志全丢"
            logging.getLogger("app.verify.writefail").warning("第二次写失败-CLOSEDSTREAM2")
            assert app_logging.write_failure_count() == 2, "计数不是逐次累加"

            patch.undo()  # 还原 handler.stream 到真实文件对象
            logging.getLogger("app.verify.writefail").warning("恢复后继续写-RECOVERED")
            text = log_path.read_text(encoding="utf-8")
            assert "恢复后继续写-RECOVERED" in text, "写失败恢复后 sink 没继续写"
            assert "运行期写失败-CLOSEDSTREAM" not in text, "失败的记录竟然落盘了"
            assert "第二次写失败-CLOSEDSTREAM2" not in text
    finally:
        patch.undo()


# --------------------------------------------------------------------------- L8


def test_redaction_only_on_file_face_console_and_root_see_raw(tmp_path, monkeypatch):
    """脱敏只作用于文件出口：root 上的其它 handler 与控制台镜像都拿到原文（零回归）。"""
    secret = "Authorization: Bearer TOKENABCDEFGH123456"
    console = io.StringIO()
    monkeypatch.setattr(sys, "stderr", console)  # console=True 形态：控制台面有值
    captured: list[str] = []

    class _RootCapture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    root_capture = _RootCapture()
    root_logger = logging.getLogger()
    root_logger.addHandler(root_capture)
    try:
        with _sink(tmp_path / "logs") as log_path:
            logging.getLogger("app.verify.console").warning("请求失败：%s", secret)
    finally:
        root_logger.removeHandler(root_capture)

    file_text = log_path.read_text(encoding="utf-8")
    assert "TOKENABCDEFGH123456" not in file_text, "文件面出现明文令牌 ⇒ 脱敏没生效"
    assert app_logging.MASK in file_text
    assert captured and "TOKENABCDEFGH123456" in captured[-1], "root/其它 handler 看到的是掩码 ⇒ 泄漏面被改坏"
    assert app_logging.MASK not in captured[-1]
    assert "TOKENABCDEFGH123456" in console.getvalue(), "控制台既有可见面丢了原文"
    assert app_logging.MASK not in console.getvalue()


# --------------------------------------------------------------------------- L9


def test_exception_stack_text_is_not_redacted_registration_leg(tmp_path, monkeypatch):
    """文件消息及异常堆栈都脱敏，同时保留可排障的异常类型与调用栈。"""
    _freeze(monkeypatch)
    with _sink(tmp_path / "logs") as log_path:
        logger = logging.getLogger("app.verify.exc")
        try:
            raise ValueError("上游返回 Authorization: Bearer EXCSECRET987654")
        except ValueError:
            logger.error("请求失败：Authorization: Bearer MSGSECRET123456", exc_info=True)
    file_text = log_path.read_text(encoding="utf-8")
    assert app_logging.MASK in file_text
    assert "MSGSECRET123456" not in file_text, "消息段明文落盘 ⇒ 脱敏没作用到消息"
    assert "Traceback" in file_text and "ValueError" in file_text, "堆栈根本没落盘，本腿前提不成立"
    assert "EXCSECRET987654" not in file_text, "异常堆栈明文落盘 ⇒ 脱敏未覆盖异常链"


# --------------------------------------------------------------------------- L10


def test_non_app_namespaces_do_not_reach_the_file(tmp_path, monkeypatch):
    """覆盖面限度（现读真相）：只有 app.* 进本文件，uvicorn/第三方命名空间不进。"""
    _freeze(monkeypatch)
    with _sink(tmp_path / "logs") as log_path:
        logging.getLogger("uvicorn.error").warning("壳侧记录-UVICORNONLY070")
        logging.getLogger("app.verify.scope").warning("应用记录-APPSCOPE070")
    file_text = log_path.read_text(encoding="utf-8")
    assert "应用记录-APPSCOPE070" in file_text
    assert "壳侧记录-UVICORNONLY070" not in file_text


# --------------------------------------------------------------------------- L11


def test_idempotent_and_retarget_swaps_single_handler(tmp_path, monkeypatch):
    """幂等（同落点不叠 handler）与换落点（先退旧再装新，历史记录不倒灌）。"""
    _freeze(monkeypatch)
    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    first = app_logging.setup_app_logging(log_dir=dir_a)
    second = app_logging.setup_app_logging(log_dir=dir_a)
    assert first == second == dir_a / "zhilan.log"
    assert len(_marked_handlers()) == 1 and len(_mirror_handlers()) == 1
    logging.getLogger("app.verify.swap").warning("换落点前-SWAPBEFORE")
    assert app_logging.setup_app_logging(log_dir=dir_b) == dir_b / "zhilan.log"
    assert app_logging.installed_log_file() == dir_b / "zhilan.log"
    assert len(_marked_handlers()) == 1 and len(_mirror_handlers()) == 1, "换落点后 handler 叠加了"
    logging.getLogger("app.verify.swap").warning("换落点后-SWAPAFTER")
    text_a = (dir_a / "zhilan.log").read_text(encoding="utf-8")
    text_b = (dir_b / "zhilan.log").read_text(encoding="utf-8")
    assert "换落点前-SWAPBEFORE" in text_a and "换落点后-SWAPAFTER" not in text_a
    assert "换落点后-SWAPAFTER" in text_b and "换落点前-SWAPBEFORE" not in text_b


# --------------------------------------------------------------------------- L12


def test_lifespan_assembly_point_installs_real_file_sink(tmp_path, monkeypatch):
    """走真 lifespan（重活全桩）⇒ 装配点真的把文件出口装上，且 lifespan 内记录真落盘。

    击杀两类变异：M1（setup 里不 addHandler）与 M2（lifespan 不再调用 setup_app_logging）。
    回归修复 备注：main.py 未 import close_vector_store（预存在缺陷，不属本笔）⇒ 这里用
    raising=False 桩掉；本文件不改产品代码。
    """
    main_module = importlib.import_module("main")
    logs_dir = tmp_path / "data-root" / "logs"
    calls: list[str] = []

    async def _async_step(name: str) -> None:
        calls.append(name)

    monkeypatch.setattr(app_logging, "resolve_log_dir", lambda: logs_dir)
    monkeypatch.setattr(main_module, "ensure_service_registry", lambda: calls.append("registry"))
    monkeypatch.setattr(main_module, "migrate_legacy_rerank", lambda: calls.append("rerank"))
    monkeypatch.setattr(main_module, "run_migrations", lambda: calls.append("migrations"))
    monkeypatch.setattr(
        main_module, "engine", SimpleNamespace(dispose=lambda: _async_step("dispose")), raising=False
    )
    monkeypatch.setattr(main_module, "close_redis", lambda: _async_step("close_redis"), raising=False)
    monkeypatch.setattr(
        main_module, "close_vector_store", lambda: calls.append("close_vector_store"), raising=False
    )

    async def _drive() -> None:
        async with main_module.lifespan(None):
            logging.getLogger("app.verify.lifespan").warning("生命周期装配后落盘-LIFESPANLEG")

    asyncio.run(_drive())
    assert app_logging.installed_log_file() == logs_dir / "zhilan.log"
    file_text = (logs_dir / "zhilan.log").read_text(encoding="utf-8")
    assert "生命周期装配后落盘-LIFESPANLEG" in file_text
    assert calls == [
        "registry",
        "rerank",
        "migrations",
        "dispose",
        "close_redis",
        "close_vector_store",
    ], f"lifespan 其余步骤的次序/次数被本笔改动波及：{calls}"


# --------------------------------------------------------------------------- L13


def test_redact_pure_function_attack_surface():
    """纯函数面：五类敏感形态逐一掩码 + 超长截断哨兵（与装配/格式化路径解耦）。"""
    cases = [
        ("Authorization: Bearer TOPSECRETTOKEN123", "TOPSECRETTOKEN123"),
        ("openai_api_key=sk-proj-ABCDEFGH123456", "sk-proj-ABCDEFGH123456"),
        ("client_secret: 'CSECRETVALUE987'", "CSECRETVALUE987"),
        ("https://x.test/cb?token=QT-TOKEN-987654&ok=1", "QT-TOKEN-987654"),
        ("password=Pa55w0rd!XYZ", "Pa55w0rd!XYZ"),
    ]
    for raw, must_hide in cases:
        out = app_logging.redact(raw)
        assert must_hide not in out, f"未掩码：{raw}"
        assert app_logging.MASK in out
    truncated = app_logging.redact("A" * 3005)
    assert truncated.endswith("……[已截断：原 3005 字符]")
    assert len(truncated) < 3005
    assert app_logging.redact("普通中文日志") == "普通中文日志"
