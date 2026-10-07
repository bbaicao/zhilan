"""``scripts/export_agent_trace.py`` 的行为钉子（阶段 5 · 条目 `5.5.3`）。

**为什么单独立一个文件**：这个脚本是「用户自己拿 JSONL 喂外部后端」的唯一出口，
而它的四条硬性质此前**一条别的用例都没看着**：

1. **默认 dry-run 不落盘**（``--out`` 必填、``--write`` 才写）——写错一个分支，用户下次
   随手跑导出时会**悄悄多出一个文件**（甚至落在 C 盘 / 用户目录，而 ``--out`` 无默认值
   正是为了堵住这件事）；
2. **字段名 = OTel GenAI semconv**（属性名从 ``cubeloop.tracing.schema`` 引用）——常量改名
   而脚本没跟上时，外部后端**静默丢字段**：不报错，只是查不到；
3. **坏参数给可读报错 + 退出码 2**——命令行工具对用户唯一的反馈面；
4. **零出网 / 零后台线程 / 零新增依赖**——这是 `5.5.3` 判「不接 Langfuse」的落地口径
   （开工包 §1.2 三条硬理由 + §2.3「出网」「并发」两行），一旦有人顺手 ``import httpx``
   或起个上传线程，契约就废了，而**盘上没有别的钉子**看住它。

**两件事分开放**（失败时能一眼看出坏在哪一层）：

- **CLI 面**（dry-run / ``--write`` / 坏参数）把 ``collect`` 钉成替身，**不连任何库**——
  产品库的存量行数不该决定「每行都能 ``json.loads``」这类断言是不是**空跑**（空库时它会
  对 0 行假绿）；``main()`` 的落盘、打印、退出码三条链路仍是真的。
- **读库面**（投影 / 过滤 / ``limit``）用仓库既有的 ``db`` 夹具造真行、走真 SQL，
  读到的就是本用例刚写进去的那几行。

⚠️ 本文件在 ``tests/agent/`` 下，``tests/api/conftest.py`` 的 autouse 隔离钉子**不生效**；
这里用不上它们——``collect`` / ``build_spans`` 只读 ``agent_runs`` / ``agent_steps`` 两张表，
不碰向量库、重排、联网、记忆、模型。

**CLI 用例为什么写成 async**：``main()`` 内部 ``asyncio.run()``，跑不到 pytest-asyncio
已经起好的事件循环里（会 ``RuntimeError: asyncio.run() cannot be called from a running
event loop``）。本仓 `tests/api/_sync_client.py` 的绕法是「把用例写成同步」，但本文件按
开工指令统一 ``pytestmark = pytest.mark.asyncio``，故把 ``main()`` 丢到**工作线程**里跑
（线程没有事件循环，``asyncio.run`` 正常）——见 :func:`_run_cli`。

**本文件未执行**：按开工指令「只写不跑」（本仓跑测独占约定，另有手工 API 探针在跑）。
"""

from __future__ import annotations

import ast
import asyncio
import io
import json
import re
import sys
import tokenize
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 先把 ``backend/scripts`` 与 ``backend`` 补进 sys.path，再导入被测脚本
# ---------------------------------------------------------------------------
# 照 ``tests/test_script_evidence_encoding.py`` 的先例：脚本不在包内，只能靠 sys.path。
# 补 ``backend`` 是给 ``app.*`` 与 ``cubeloop.*`` 用的（``cubeloop`` 是 venv 里的
# **纯常量**模块，``from cubeloop.tracing.schema import ...`` 不会拉起 opentelemetry-sdk）。
# 从 ``backend/`` 跑 pytest 时这两行幂等；从仓库根跑也照样能 import。
BACKEND = Path(__file__).resolve().parents[2]
SCRIPTS = BACKEND / "scripts"
SCRIPT_PATH = SCRIPTS / "export_agent_trace.py"

for _candidate in (SCRIPTS, BACKEND):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

try:
    import export_agent_trace as exp
except ImportError as exc:  # pragma: no cover —— 脚本被删/改名时给一条读得懂的中文原因
    raise ImportError(
        f"导入 {SCRIPT_PATH} 失败（已把 {SCRIPTS} 与 {BACKEND} 补进 sys.path）：{exc}"
    ) from exc

from app.agent import audit as agent_audit  # noqa: E402 —— 只为钉「夹取口径与审计端点一致」
from app.models import AgentRun, AgentStep  # noqa: E402 —— 造真行用
from cubeloop.tracing.schema import (  # noqa: E402 —— 期望值**一律引用常量**，不硬编码字符串
    CUBELOOP_METADATA_PREFIX,
    CUBELOOP_RUN_ID,
    CUBELOOP_RUN_OUTCOME,
    CUBELOOP_TOOL_IS_ERROR,
    ERROR_TYPE,
    GEN_AI_CONVERSATION_ID,
    GEN_AI_OPERATION_NAME,
    GEN_AI_TOOL_CALL_ARGUMENTS,
    GEN_AI_TOOL_CALL_ID,
    GEN_AI_TOOL_CALL_RESULT,
    GEN_AI_TOOL_NAME,
    OP_EXECUTE_TOOL,
    OP_INVOKE_AGENT,
    SPAN_NAME_EXECUTE_TOOL,
    SPAN_NAME_INVOKE_AGENT,
)

#: 全文件统一用模块级 asyncio 标记（house 口径，见 `tests/api/test_agent_audit_endpoint.py`）
pytestmark = pytest.mark.asyncio

#: 一条 span 记录必须**恰好**有这七个键：多一个少一个，外部后端都按自己的规则静默忽略
RECORD_KEYS = frozenset(
    {"trace_id", "span_id", "parent_span_id", "name", "start_time", "end_time", "attributes"}
)

#: 32 位 / 16 位**小写十六进制**（W3C trace context 的字符串形态）
TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")

#: 造行用的固定时间：**整秒**。MySQL 的 ``DATETIME`` 没有小数位，带微秒的输入会被截断，
#: 断言里的 ISO 串就会凭空少六位 ⇒ 在 MySQL 方言下假红（本仓两种方言都跑）。
BASE_TIME = datetime(2026, 9, 20, 9, 0, 0)

#: CLI 用例的假行主键（与真实自增 id 无关，只用来对账）
CLI_RUN_ID_A = 11
CLI_RUN_ID_B = 12


# ---------------------------------------------------------------------------
# CLI 帮手：把 main() 丢到工作线程，并把 collect 钉成替身
# ---------------------------------------------------------------------------


async def _run_cli(argv: list[str]) -> int:
    """在**工作线程**里跑 ``main(argv)``，返回它的退出码。

    ``main()`` 内部 ``asyncio.run(collect(...))``：本用例本身已经跑在事件循环里，直接调会
    报「asyncio.run() cannot be called from a running event loop」。线程里没有事件循环
    ⇒ ``main()`` 自己那次 ``asyncio.run`` 正常；``collect`` 是替身、不做 IO，
    所以不存在「同一个 AsyncSession 跨事件循环用」的风险。
    """
    return await asyncio.to_thread(exp.main, argv)


async def _cli_exit_code(argv: list[str]) -> int:
    """跑 CLI 并**归一化**退出码：argparse 拦下的错误是 ``SystemExit``，脚本自己拦下的是
    ``return 2``——对用户来说两者都是「退出码 2」，用例钉的是这个对外口径而不是实现位置。
    """
    try:
        return await _run_cli(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


def _stub_collect(monkeypatch) -> dict:  # noqa: ANN001
    """把脚本的 ``collect`` 钉成**返回假行**的替身，并把收到的关键字原样记下来。

    记录关键字是为了另一条用例：漏传 ``--run-id`` 这类「参数没接上」的 bug 不会报错，
    只会给出一份「看起来对了」的全量导出。
    """
    runs, steps = _cli_rows()
    seen: dict = {}

    async def _fake(**kwargs):  # noqa: ANN003 —— 与 collect 的形参同形（只转发）
        seen.update(kwargs)
        return runs, steps

    monkeypatch.setattr(exp, "collect", _fake)
    return seen


def _cli_rows() -> tuple[list[dict], list[dict]]:
    """CLI 用例的假行：键与 ``_run_row`` / ``_step_row`` 逐键一致（不多不少）。

    为什么不从产品库取：CLI 那一面钉的是「参数 → 打印 → 落盘」，产品库的存量行数不该决定
    「每行都能 ``json.loads``」这类断言是不是**空跑**（空库时它对 0 行假绿）。
    特意留了 ``chat_id=None`` / ``meta=None`` / ``args=None`` 三种空值，用于钉住
    「值为 ``None`` 的属性不写」（OTel 不允许 null 属性值，见脚本 docstring 末段）。
    """
    started = datetime(2026, 9, 20, 12, 0, 0)
    runs = [
        {
            "id": CLI_RUN_ID_A, "chat_id": 101, "message_id": 7, "mode": "react", "status": "done",
            "started_at": started, "finished_at": started + timedelta(seconds=9),
            "meta": {"retrieval_id": "rl-cli", "turn": 1},
        },
        # 第二个 run 的三个可空字段全空：钉住「None 的属性不写」
        {
            "id": CLI_RUN_ID_B, "chat_id": None, "message_id": None, "mode": "off",
            "status": "stopped", "started_at": started + timedelta(minutes=1),
            "finished_at": None, "meta": None,
        },
    ]
    steps = [
        {
            "id": 21, "run_id": CLI_RUN_ID_A, "step_no": 1, "type": "tool_call",
            "tool_name": "web_search", "tool_call_id": "tc-cli-1", "content": "观察-cli-1",
            "created_at": started + timedelta(seconds=1),
            "meta": {"ok": True, "ms": 5, "args": {"query": "蓝鲸"}},
        },
        {
            "id": 22, "run_id": CLI_RUN_ID_A, "step_no": 2, "type": "tool_call",
            "tool_name": "knowledge_search", "tool_call_id": "tc-cli-2", "content": "观察-cli-2",
            "created_at": started + timedelta(seconds=2),
            "meta": {"ok": False, "ms": 6, "args": None},
        },
        # 全空的一条：工具名之外一个可选属性都没有
        {
            "id": 23, "run_id": CLI_RUN_ID_B, "step_no": 1, "type": "tool_call",
            "tool_name": "note_save", "tool_call_id": None, "content": None,
            "created_at": started + timedelta(seconds=61), "meta": None,
        },
    ]
    return runs, steps


def _read_jsonl(path: Path) -> list[dict]:
    """读 JSONL：**每一行**都要能 ``json.loads``（这是外部后端的第一道门槛）。"""
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [json.loads(line) for line in lines]


# ---------------------------------------------------------------------------
# 造真行（读库面的三条用例用）
# ---------------------------------------------------------------------------


async def _seed_chat(db, title: str) -> int:  # noqa: ANN001 —— 与仓内其他造行用例同形
    """造一个**真会话行**并返回其 id。

    ``agent_runs.chat_id`` 有外键（``ON DELETE SET NULL``），手搓一个不存在的 ``chat_id``
    会直接撞 FK——所以每条造行用例都先落一行 ``chats``。
    """
    from app.session import repository as repo

    chat = await repo.create_chat(db, title=title)
    await db.flush()
    return chat.id


async def _seed_run(
    db,  # noqa: ANN001
    *,
    chat_id: int,
    started_at: datetime,
    steps: int = 2,
    mode: str = "react",
    status: str = "done",
    run_meta: dict | None = None,
) -> tuple[int, list[int]]:
    """造 1 行 ``agent_runs`` + ``steps`` 行 ``agent_steps``，返回 ``(run_id, [step_id 升序])``。

    照 ``tests/api/test_agent_audit_endpoint.py`` 的造行口径（**直接写表、不跑 Agent 循环**，
    末尾 ``await db.commit()``）：「读出来对不对」与「写进去对不对」是两件事，本文件只管前者。

    ``started_at`` 由调用方给**显式**值：倒序 / ``limit`` 保住最新一轮这类断言不能靠
    ``server_default=now()``——同一秒落的两行时间戳相同，断言就变成猜。
    """
    run = AgentRun(
        chat_id=chat_id,
        mode=mode,
        status=status,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=30),
        meta=run_meta if run_meta is not None else {"retrieval_id": "rl-trace-1", "turn": 2},
    )
    db.add(run)
    await db.flush()
    step_ids: list[int] = []
    for index in range(1, steps + 1):
        step = AgentStep(
            run_id=run.id,
            step_no=index,
            type="tool_call",
            tool_name="knowledge_search",
            tool_call_id=f"tc-{run.id}-{index}",
            content=f"观察 {index}",
            created_at=started_at + timedelta(seconds=index),
            # 第 2 条起 ok=False：``cubeloop.tool.is_error`` 是**取反**的（失败恰好最需要回放）
            meta={"ok": index != 2, "ms": 10 * index, "args": {"query": "蓝鲸", "top_k": 3}},
        )
        db.add(step)
        await db.flush()
        step_ids.append(step.id)
    await db.commit()
    return run.id, step_ids


# ---------------------------------------------------------------------------
# 1. 默认 dry-run：一个字节都不落盘
# ---------------------------------------------------------------------------


async def test_dry_run_writes_nothing_and_leaves_the_directory_untouched(
    tmp_path, monkeypatch, capsys
):  # noqa: ANN001
    """默认（不写 ``--write``）**不落盘**，连父目录都不建，目录清单与 mtime 逐项不变。

    快照的是「目录里有什么 + 每个条目的 mtime」而不是只看目标文件：只断言目标文件不存在，
    会把「改成写别的名字 / 写进 cwd」放过去——而这两件事正是本条目要防的（绝不写 C 盘 /
    用户目录）。``monkeypatch.chdir`` 是故意的：相对落点若真有 bug，也会落在 tmp_path 里。
    """
    monkeypatch.chdir(tmp_path)
    _stub_collect(monkeypatch)
    out = tmp_path / "nested" / "trace.jsonl"
    before = {path.name: path.stat().st_mtime_ns for path in sorted(tmp_path.iterdir())}

    code = await _run_cli(["--out", str(out)])
    captured = capsys.readouterr()

    assert code == 0
    assert not out.exists(), "dry-run 写出了文件"
    assert not out.parent.exists(), "dry-run 连父目录都不该建（mkdir 只在 --write 分支里）"
    after = {path.name: path.stat().st_mtime_ns for path in sorted(tmp_path.iterdir())}
    assert after == before, f"dry-run 后目录清单/时间戳变了：{before} → {after}"
    assert "DRY RUN" in captured.out, "dry-run 必须在 stdout 上说清自己没落盘"
    assert str(out) in captured.out, "要把落点回显出来，用户才知道 --write 会写到哪"


async def test_cli_arguments_reach_collect_unchanged(tmp_path, monkeypatch):  # noqa: ANN001
    """四个过滤参数**原样**传到 ``collect``：漏传一个，用户会拿到一份「看起来对了」的全量导出。"""
    seen = _stub_collect(monkeypatch)

    code = await _run_cli(
        [
            "--out",
            str(tmp_path / "trace.jsonl"),
            "--chat-id",
            "101",
            "--run-id",
            "11",
            "--since",
            "2026-09-20T09:00:00",
            "--limit",
            "3",
        ]
    )

    assert code == 0
    assert seen == {
        "chat_id": 101,
        "run_id": 11,
        "since": datetime(2026, 9, 20, 9, 0, 0),
        "limit": 3,
    }, "CLI 参数没有原样落到 collect 上"


# ---------------------------------------------------------------------------
# 2. --write 真落盘，且内容能解析
# ---------------------------------------------------------------------------


async def test_write_lands_jsonl_that_parses_line_by_line(
    tmp_path, monkeypatch, capsys
):  # noqa: ANN001
    """``--write`` 落 JSONL：**一行一个 span**，每行都能 ``json.loads``，父目录一并建出来。"""
    _stub_collect(monkeypatch)
    out = tmp_path / "nested" / "trace.jsonl"

    code = await _run_cli(["--out", str(out), "--write"])
    captured = capsys.readouterr()

    assert code == 0
    assert out.is_file(), "--write 就该落盘（父目录一并建出来）"
    text = out.read_text(encoding="utf-8")
    assert text.endswith("\n"), "一行一个 span（末尾接换行，追加写不会黏行）"

    records = _read_jsonl(out)
    assert len(records) == 5, "2 个 run + 3 个 step = 5 行 span"
    assert [record["name"] for record in records] == [
        SPAN_NAME_INVOKE_AGENT,
        SPAN_NAME_INVOKE_AGENT,
        SPAN_NAME_EXECUTE_TOOL,
        SPAN_NAME_EXECUTE_TOOL,
        SPAN_NAME_EXECUTE_TOOL,
    ], "先 run 后 step，且同一个 run 的 step 排在一起"
    for record in records:
        assert set(record) == RECORD_KEYS, f"span 记录的键不是那七个：{sorted(record)}"
    assert "已写入" in captured.out

    # 空值边界：值为 None 的属性**一个都不许写**（OTel 不允许 null 属性值）
    run_b = records[1]["attributes"]
    assert GEN_AI_CONVERSATION_ID not in run_b, "chat_id 为 NULL 的 run 不该带会话 id"
    assert ERROR_TYPE not in run_b
    step_c = records[-1]["attributes"]
    assert GEN_AI_TOOL_NAME in step_c, "工具名是这个 step 唯一必有的属性"
    for key in (GEN_AI_TOOL_CALL_ID, GEN_AI_TOOL_CALL_ARGUMENTS, GEN_AI_TOOL_CALL_RESULT):
        assert key not in step_c, f"{key} 在空值 step 上不该出现"
    assert CUBELOOP_TOOL_IS_ERROR not in step_c, "meta 缺失 ⇒ 成败未知，别编一个 False 出来"


async def test_write_format_json_lands_the_same_records_as_a_json_array(
    tmp_path, monkeypatch
):  # noqa: ANN001
    """``--format json`` 落**一个数组**，且与 JSONL 逐条等价（不然两种格式对不上账）。"""
    _stub_collect(monkeypatch)
    jsonl_path = tmp_path / "trace.jsonl"
    json_path = tmp_path / "trace.json"

    assert await _run_cli(["--out", str(jsonl_path), "--write"]) == 0
    assert await _run_cli(["--out", str(json_path), "--write", "--format", "json"]) == 0

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert isinstance(payload, list), "--format json 落的是数组，不是对象"
    assert payload == _read_jsonl(jsonl_path), "同一批行两种格式必须逐条等价"


# ---------------------------------------------------------------------------
# 3. 坏参数：中文可读报错 + 退出码 2
# ---------------------------------------------------------------------------


async def test_missing_out_is_rejected_and_writes_nothing(tmp_path, monkeypatch, capsys):
    """``--out`` 必填：不给落点就**什么都别做**（没有默认落点 ⇒ 绝不会顺手写 C 盘）。

    这条路径由 argparse 自己接管（``required=True``）：英文 usage + 退出码 2，
    **不是** traceback——命令行工具的坏参数不该给用户一段堆栈。
    """
    monkeypatch.chdir(tmp_path)
    _stub_collect(monkeypatch)
    before = sorted(path.name for path in tmp_path.iterdir())

    code = await _cli_exit_code(["--write"])
    captured = capsys.readouterr()

    assert code == 2
    assert "--out" in captured.err, "报错要指名道姓说是哪个参数缺了"
    assert "Traceback" not in captured.err, "坏参数只该给一行可读报错"
    assert sorted(path.name for path in tmp_path.iterdir()) == before, "参数没过校验还写了东西"


async def test_bad_since_returns_two_with_a_chinese_message(tmp_path, capsys):  # noqa: ANN001
    """坏 ``--since``：**main 返回 2**（不抛异常）+ 中文报错 + 回显用户输入的原文。

    为什么要回显原文：一条命令行里可能写了好几个参数，报错不带坏值，用户得自己猜是哪一个。
    这条路径**碰不到数据库**（校验在 ``collect`` 之前），所以不需要造行、也不需要替身。
    """
    out = tmp_path / "trace.jsonl"

    code = await _run_cli(["--out", str(out), "--since", "notatime"])
    captured = capsys.readouterr()

    assert code == 2, "坏参数必须以退出码 2 结束（而且 main 是 return，不是抛）"
    assert "参数错误" in captured.err
    assert "--since" in captured.err
    assert "notatime" in captured.err, "报错要把坏值原样回显出来"
    assert "Traceback" not in captured.err
    assert not out.exists(), "参数都没通过校验，不该落盘"


async def test_unwritable_out_is_a_chinese_error_and_exit_two(tmp_path, monkeypatch, capsys):  # noqa: ANN001
    """落点不可写（盘不存在 / 只读目录）：给**中文可读报错 + 退出码 2**，不许甩原始 traceback。

    ⚠️ 这条是**对抗性审计抓到的**真缺陷：``--write`` 原先直接 ``mkdir`` + ``write_text``，
    落点不可写时抛 ``FileNotFoundError`` 原始栈、**退出码 1**，与 ``main()`` docstring 写死的
    「0 成功 / 2 参数错误」不符——而「落点写不进去」正是这个脚本最常见的失败（用户手打路径）。

    用「父路径是个**文件**」来造不可写：跨平台、不需要管理员权限、也不用碰别的盘。
    （Windows 上 ``Z:\\`` 这种不存在的盘也能造，但那是本机特例，不该写进通用用例。）
    """
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("这不是目录", encoding="utf-8")
    out = blocker / "trace.jsonl"  # 父路径是文件 ⇒ mkdir 必失败
    _stub_collect(monkeypatch)

    code = await _run_cli(["--out", str(out), "--write"])
    captured = capsys.readouterr()

    assert code == 2, "落点不可写应当是可判定的失败（退出码 2），而不是 traceback 崩掉"
    assert "落点不可写" in captured.err
    assert "Traceback" not in captured.err, f"给使用者的不该是堆栈：{captured.err[-300:]}"
    assert not out.exists()


async def test_bad_limit_reports_the_offending_value_and_exits_two(
    tmp_path, capsys
):  # noqa: ANN001
    """坏 ``--limit``：函数面给中文 ``ValueError``，命令行面退出码 2 且**同样是中文**报错。

    ⚠️ 这条**曾经**如实登记过一处差异：``--limit`` 原写成 ``type=int``，坏值在 argparse 阶段
    就被英文 usage 拦下，``clamp_limit`` 的中文分支**从命令行永远不可达**（违反「坏参数可读中文
    报错」这条验收项）。现已由 ``_limit_arg`` 转换器修掉 ⇒ 两处都是中文、都回显坏值。
    保留这条用例正是为了钉住「别再退回 ``type=int``」。
    """
    with pytest.raises(ValueError) as excinfo:
        exp.clamp_limit("abc")
    assert "--limit" in str(excinfo.value)
    assert "abc" in str(excinfo.value), "函数面的中文报错要带上坏值"

    out = tmp_path / "trace.jsonl"
    code = await _cli_exit_code(["--out", str(out), "--limit", "abc"])
    captured = capsys.readouterr()

    assert code == 2
    assert "abc" in captured.err, "命令行报错同样要回显坏值"
    assert "--limit" in captured.err, "命令行报错要指名是哪个参数"
    assert any("\u4e00" <= ch <= "\u9fff" for ch in captured.err), (
        f"命令行面的坏参数报错也必须是**可读中文**（不许退回 argparse 的英文 usage）：{captured.err}"
    )
    assert "Traceback" not in captured.err
    assert not out.exists()


async def test_limit_is_clamped_into_one_through_five_hundred():
    """``--limit`` 夹在 1..500，且**与 ``GET /api/agent/audit`` 的夹取口径相等**。

    两处各写一份夹取规则、只有一处跟着改，是这类「读接口 + 脚本」组合的典型漂移
    （``app.agent.audit`` 有意不 import 脚本，只能靠用例钉住相等）。
    """
    assert exp.clamp_limit(0) == 1
    assert exp.clamp_limit(-7) == 1
    assert exp.clamp_limit(1) == 1
    assert exp.clamp_limit(500) == 500
    assert exp.clamp_limit(9999) == 500
    assert exp.MAX_RUN_LIMIT == 500 == agent_audit.MAX_AUDIT_LIMIT
    assert exp.DEFAULT_RUN_LIMIT == 200, "默认 200（与审计端点的非法值回落同值）"


# ---------------------------------------------------------------------------
# 4. 空库：0 行、不炸
# ---------------------------------------------------------------------------


async def test_empty_tables_export_nothing_and_do_not_crash(db):  # noqa: ANN001
    """空库（新装 / 刚清表）导出 0 行，**不抛异常**——这是部署后第一次自检的必经路径。

    ``db`` 夹具每例先清 ``agent_runs`` / ``agent_steps``，所以这里的会话确实是空的。
    """
    runs, steps = await exp.collect(session=db)
    assert runs == []
    assert steps == []

    assert exp.build_spans([], []) == []
    assert exp.serialize([], "jsonl") == "", "0 行 ⇒ 空文件（不是一个只有换行的文件）"
    assert json.loads(exp.serialize([], "json")) == []


# ---------------------------------------------------------------------------
# 5. 读库面的投影：键 / semconv 常量 / id 形态 / 父子挂接
# ---------------------------------------------------------------------------


async def test_span_keys_ids_and_semconv_names_match_the_schema_module(db):  # noqa: ANN001
    """七个键、semconv 常量名、id 形态、父子挂接——错一个，外部后端就**静默丢字段**。

    期望值**一律引用 ``cubeloop.tracing.schema`` 的常量**（不硬编码字符串）：脚本与 semconv
    是同一份常量的两个使用方，硬编码会让「改名了但只改一处」这种漂移失去检测能力。
    """
    chat_id = await _seed_chat(db, "导出-字段形态")
    first_run, _ = await _seed_run(db, chat_id=chat_id, started_at=BASE_TIME, steps=2)
    second_run, _ = await _seed_run(
        db, chat_id=chat_id, started_at=BASE_TIME + timedelta(minutes=5), steps=1
    )

    runs, steps = await exp.collect(session=db)
    records = exp.build_spans(runs, steps)
    assert len(records) == 5, "2 个 run + 3 个 step = 5 行 span"

    for record in records:
        assert set(record) == RECORD_KEYS, f"span 记录的键不是那七个：{sorted(record)}"
        assert TRACE_ID_RE.match(record["trace_id"]), "trace_id 必须是 32 位小写十六进制"
        assert SPAN_ID_RE.match(record["span_id"]), "span_id 必须是 16 位小写十六进制"
        if record["parent_span_id"] is not None:
            assert SPAN_ID_RE.match(record["parent_span_id"])

    run_spans = [record for record in records if record["name"] == SPAN_NAME_INVOKE_AGENT]
    step_spans = [record for record in records if record["name"] == SPAN_NAME_EXECUTE_TOOL]
    assert len(run_spans) == 2
    assert len(step_spans) == 3

    assert {span["attributes"][CUBELOOP_RUN_ID] for span in run_spans} == {
        str(first_run),
        str(second_run),
    }
    for run_span in run_spans:
        assert run_span["parent_span_id"] is None, "run span 是这条 trace 的根"
        assert run_span["attributes"][GEN_AI_OPERATION_NAME] == OP_INVOKE_AGENT
    assert len({span["trace_id"] for span in run_spans}) == 2, "两条 run 不能共用一条 trace_id"
    assert len({record["span_id"] for record in records}) == len(records), (
        "span_id 撞了：run 与 step 必须分开命名空间（同号主键也不能撞）"
    )

    by_span_id = {span["span_id"]: span for span in run_spans}
    for step_span in step_spans:
        assert step_span["parent_span_id"] in by_span_id, (
            "step 的 parent_span_id 指不到任何 run span（父子关系断了）"
        )
        parent = by_span_id[step_span["parent_span_id"]]
        assert parent["trace_id"] == step_span["trace_id"], "step 与它的 run 必须在同一条 trace 里"
        assert step_span["attributes"][GEN_AI_OPERATION_NAME] == OP_EXECUTE_TOOL
        for key in (
            GEN_AI_TOOL_NAME,
            GEN_AI_TOOL_CALL_ID,
            GEN_AI_TOOL_CALL_ARGUMENTS,
            GEN_AI_TOOL_CALL_RESULT,
        ):
            assert key in step_span["attributes"], f"step span 少了 {key}（工具调用四件套）"


# ---------------------------------------------------------------------------
# 6. 读库面的投影：字段值与造出来的行对得上
# ---------------------------------------------------------------------------


async def test_projection_carries_the_seeded_values(db):  # noqa: ANN001
    """真库投影：行里的每个字段都**原样**落到该落的属性上（含时间与 None 的两种收口）。"""
    chat_id = await _seed_chat(db, "导出-字段值")
    run_id, step_ids = await _seed_run(
        db, chat_id=chat_id, started_at=BASE_TIME, steps=2, mode="plan_execute"
    )

    runs, steps = await exp.collect(session=db)
    assert [row["id"] for row in runs] == [run_id], "用例只造了一轮"
    assert [row["id"] for row in steps] == step_ids, "同一个 run 的 step 按 (run_id, step_no) 升序"

    records = exp.build_spans(runs, steps)
    assert len(records) == 3

    run_span, first_step, second_step = records
    assert run_span["name"] == SPAN_NAME_INVOKE_AGENT
    assert run_span["start_time"] == BASE_TIME.isoformat()
    assert run_span["end_time"] == (BASE_TIME + timedelta(seconds=30)).isoformat()

    attrs = run_span["attributes"]
    assert attrs[GEN_AI_CONVERSATION_ID] == str(chat_id), "会话 id 从 chat_id 来"
    assert attrs[CUBELOOP_RUN_ID] == str(run_id)
    assert attrs[CUBELOOP_RUN_OUTCOME] == "done"
    assert attrs[f"{CUBELOOP_METADATA_PREFIX}mode"] == "plan_execute"
    assert attrs[f"{CUBELOOP_METADATA_PREFIX}retrieval_id"] == "rl-trace-1"
    assert attrs[f"{CUBELOOP_METADATA_PREFIX}turn"] == "2", "meta.turn 是数字，属性里是字符串"
    assert ERROR_TYPE not in attrs, "meta 里没有 error ⇒ 这个键不写（OTel 不收 null）"

    assert first_step["name"] == SPAN_NAME_EXECUTE_TOOL
    assert first_step["parent_span_id"] == run_span["span_id"], "step 要挂在它那轮的 run span 下"
    assert first_step["trace_id"] == run_span["trace_id"]
    assert first_step["start_time"] == (BASE_TIME + timedelta(seconds=1)).isoformat()
    assert first_step["end_time"] is None, "step 表没有结束时间列 ⇒ 如实不给，不拿别的字段假装"
    step_attrs = first_step["attributes"]
    assert step_attrs[GEN_AI_TOOL_NAME] == "knowledge_search"
    assert step_attrs[GEN_AI_TOOL_CALL_ID] == f"tc-{run_id}-1"
    assert step_attrs[GEN_AI_TOOL_CALL_RESULT] == "观察 1"
    assert step_attrs[GEN_AI_TOOL_CALL_ARGUMENTS] == json.dumps(
        {"query": "蓝鲸", "top_k": 3}, ensure_ascii=False, sort_keys=True
    ), "args 要按 (ensure_ascii=False, sort_keys=True) 序列化——中文不该被转义、键序要稳"
    assert step_attrs[f"{CUBELOOP_METADATA_PREFIX}ms"] == "10"
    assert step_attrs[CUBELOOP_TOOL_IS_ERROR] is False, "meta.ok=True ⇒ is_error=False"
    assert second_step["attributes"][CUBELOOP_TOOL_IS_ERROR] is True, "meta.ok=False ⇒ 取反"


# ---------------------------------------------------------------------------
# 7. 过滤与 limit（limit 作用在 run 上，不是 step 上）
# ---------------------------------------------------------------------------


async def test_run_id_and_chat_id_filters_narrow_the_run_set(db):  # noqa: ANN001
    """``run_id`` / ``chat_id`` 真的收窄了 run 集合，且**只带出这些 run 的 step**。"""
    keep_chat = await _seed_chat(db, "导出-过滤-A")
    other_chat = await _seed_chat(db, "导出-过滤-B")
    keep_run, keep_steps = await _seed_run(db, chat_id=keep_chat, started_at=BASE_TIME, steps=2)
    other_run, _ = await _seed_run(
        db, chat_id=other_chat, started_at=BASE_TIME + timedelta(minutes=1), steps=3
    )

    all_runs, all_steps = await exp.collect(session=db)
    assert {row["id"] for row in all_runs} == {keep_run, other_run}, "不传过滤 = 跨会话全给"
    assert len(all_steps) == 5

    by_run, by_run_steps = await exp.collect(session=db, run_id=keep_run)
    assert [row["id"] for row in by_run] == [keep_run]
    assert [row["id"] for row in by_run_steps] == keep_steps, "别把别人家的 step 也带出来"

    by_chat, by_chat_steps = await exp.collect(session=db, chat_id=other_chat)
    assert [row["id"] for row in by_chat] == [other_run]
    assert {row["run_id"] for row in by_chat_steps} == {other_run}
    assert len(by_chat_steps) == 3

    missing, missing_steps = await exp.collect(session=db, run_id=999999)
    assert missing == []
    assert missing_steps == [], "没有 run 命中时不该去查 step（更不该报错）"


async def test_limit_caps_runs_not_steps(db):  # noqa: ANN001
    """``limit`` 作用在 **run 条数**上：一次 trace 导出的单位是「一轮」，按 step 截断会把
    某一轮劈成半个——那对排查没有价值。倒序（最新在前）在 ``started_at`` 相同也靠 id 兜底。
    """
    chat_id = await _seed_chat(db, "导出-limit")
    old_run, _ = await _seed_run(db, chat_id=chat_id, started_at=BASE_TIME, steps=2)
    new_run, new_steps = await _seed_run(
        db, chat_id=chat_id, started_at=BASE_TIME + timedelta(minutes=1), steps=2
    )

    runs, steps = await exp.collect(session=db, limit=1)
    assert [row["id"] for row in runs] == [new_run], "倒序：最新的一轮在前"
    assert [row["id"] for row in steps] == new_steps, (
        "limit=1 若作用在 step 上，这里只会剩 1 条（把一轮劈成半个）"
    )
    assert len(steps) == 2

    wide_runs, wide_steps = await exp.collect(session=db, limit=5)
    assert {row["id"] for row in wide_runs} == {old_run, new_run}
    assert len(wide_steps) == 4, "limit 没到上限就该全给"


# ---------------------------------------------------------------------------
# 8. 源码扫描：零出网 / 零后台线程 / 零新增依赖
# ---------------------------------------------------------------------------

#: 禁用**模块**：出网客户端 + 起后台线程/进程的机制（开工包 §2.3「出网」「并发」两行）。
#: 前缀匹配 ⇒ ``import urllib.request`` / ``from http.client import X`` 这类都算。
_FORBIDDEN_MODULES = (
    "langfuse",
    "opentelemetry",
    "requests",
    "httpx",
    "aiohttp",
    "socket",
    "urllib.request",
    "http.client",
    "atexit",
    "threading",
    "concurrent.futures",
    "multiprocessing",
)

#: 禁用**标识符**（tokenize 之后的 NAME 记号）：导入面看着没问题、用法却起了线程池时靠这层兜底。
_FORBIDDEN_IDENTIFIERS = ("Thread", "Executor")

#: 禁用记号的小写形态：出现在**可执行文本**里就算（不要求它一定是 import 语句）
_FORBIDDEN_TOKENS = (
    "langfuse",
    "opentelemetry",
    "requests",
    "httpx",
    "aiohttp",
    "socket",
    "atexit",
    "threading",
    "urllib",
)


def _imported_names(source: str) -> list[str]:
    """源码里所有**绝对导入**的模块名（含常量实参的动态导入）。

    照 ``tests/test_redis_shim.py`` 的同一门手艺：AST 判据为主（挡得住 ``import a.b`` 与
    ``from a.b import c``），并识别 ``importlib.import_module("x")`` / ``__import__("x")``
    这类常量实参写法。文档字符串与注释天然不参与——它们不是 import 节点。
    """
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:  # 只认绝对导入：`from .x import y` 是自家兄弟模块
                names.append(node.module)
        elif isinstance(node, ast.Call):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if called in {"import_module", "__import__"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    names.append(first.value)
    return names


def _code_identifiers(source: str) -> list[str]:
    """源码里所有**可执行记号**中的标识符（字符串字面量、注释、docstring 一律不算）。

    ⚠️ **为什么不能直接对整份源码做子串扫描**：本脚本的模块 docstring 为了如实登记边界，
    **自己就写了** ``Langfuse``（第 3 行）、``atexit.register``（第 25 行）这些词——对整份
    源码扫子串会把「如实登记的边界」判成「违规」，而那样的假阳性钉子最后只会被人删掉。
    ``tokenize`` 之后只取 NAME 记号，字符串与注释自然被排除，``Thread(`` 这类用法照样抓得到
    （``Thread`` 一定是个 NAME 记号）。
    """
    names: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.NAME:
            names.append(token.string)
    return names


def _scan_offenders(source: str) -> list[str]:
    """回一份**命中的禁用项**清单：空清单 = 干净（用例断言的就是这个）。"""
    offenders: list[str] = []
    for name in _imported_names(source):
        for pattern in _FORBIDDEN_MODULES:
            if name == pattern or name.startswith(pattern + "."):
                offenders.append(f"import {name}")

    names = _code_identifiers(source)
    tokens = set(names)
    offenders.extend(name for name in _FORBIDDEN_IDENTIFIERS if name in tokens)
    lowered = {name.lower() for name in names}
    offenders.extend(token for token in _FORBIDDEN_TOKENS if token in lowered)
    return sorted(set(offenders))


async def test_source_has_no_forbidden_dependency_or_mechanism():
    """脚本源码里**一个禁用项都不许有**：出网客户端 / OTel SDK / 后台线程 / ``atexit``。

    这是 `5.5.3` 判「不接 Langfuse」的落地口径（开工包 §1.2 三条硬理由 + §2.3 两行），
    也是本文件里唯一**不靠运行行为**就能失效的钉子：一次 ``import httpx`` 就足以让
    「永不出网」从事实变成一句声明。
    """
    assert SCRIPT_PATH.is_file(), f"被测脚本不在盘上：{SCRIPT_PATH}"
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert len(_code_identifiers(source)) > 50, "扫到的标识符太少 ⇒ 扫描本身可能已经坏了（空跑）"

    offenders = _scan_offenders(source)
    assert offenders == [], (
        f"{SCRIPT_PATH.name} 里出现了禁用项：{offenders} —— 导出脚本必须零出网、零后台线程、"
        "零新增依赖（v2 侧只出 JSONL，外部后端由用户自己接）"
    )


async def test_source_scan_is_proven_by_mutation_forms():
    """**自证扫描有效**（变异式）：该抓的一个不漏，docstring / 注释 / 字符串里的词一个不误报。

    没有这条，「零出网 / 零后台线程」可能只是「写得像」——被 ``importlib.import_module``
    或 ``__import__`` 绕过时它照样绿；反过来，一条会对 docstring 误报的扫描会被后人删掉。
    """
    must_catch = [
        "import langfuse",
        "import opentelemetry.sdk.trace",
        "import requests",
        "import httpx",
        "import aiohttp",
        "import socket",
        "import urllib.request",
        "from http.client import HTTPConnection",
        "import atexit\natexit.register(cleanup)",
        "import threading\nthreading.Thread(target=go).start()",
        "from concurrent.futures import ThreadPoolExecutor",
        "import importlib\nimportlib.import_module('httpx')",
        "__import__('socket')",
    ]
    for snippet in must_catch:
        assert _scan_offenders(snippet), f"扫描漏判（这正是要禁的写法）：{snippet!r}"

    must_not_catch = [
        '"""登记边界：不接 Langfuse、不起后台线程、不 import httpx、不 atexit.register"""',
        "# 注释里写 import httpx / atexit.register 都不算执行面",
        "NOTE = 'httpx 与 requests 都不许 import（字符串里提到不算）'",
        "from app.db import session",
        "from cubeloop.tracing.schema import GEN_AI_TOOL_NAME",
        "URL = 'http://127.0.0.1:8000/health'",
    ]
    for snippet in must_not_catch:
        assert _scan_offenders(snippet) == [], f"扫描误报（这些都不是违约）：{snippet!r}"
