"""零依赖 trace 导出（阶段 5 · 条目 `5.5.3`）：``agent_runs`` / ``agent_steps`` → OTel 形状 span。

## 为什么是「脚本」而不是端点、也不是 Langfuse

- **不加端点**：``02`` 的 53 条是全集，「擅自加一条同样是违约」（`4.5` 包成文纪律）；
  条目原文的「只增**端点**或字段」取严解释 = **只加字段 / 参数**（见 `5.5` 开工包 §0.0 第 1、7 条）；
- **不接 Langfuse**：它存在理由就是**出网送 trace**（与 `5.4` 判 ragas「CI 里出现 ``import ragas``
  即违约」同型），依赖面三处 0 命中、要接得连整条 OTel SDK 链进 ``requirements.txt``
  （动发布面 = 重生 lock + 重跑 L0/``--smoke``），且 **C 盘零污染面无法从盘上证实**
  （参考快照里没有 Langfuse 的 ``pyproject.toml`` 可读）。⇒ 裁决 = **不装、不 import、不进
  requirements**（开工包 §1.2 三条硬理由）；「可选 Langfuse」的「可选」= **允许不选**。
- **本脚本是替代物**：用户自己拿 JSONL 喂给任何后端（Langfuse / OTLP / jq），v2 侧**永不出网**。

## 规格（开工包 §2.3）

| 项 | 做法 |
|---|---|
| 默认行为 | **dry-run**：只读查库、只打印「导出多少 run / 多少 step / 落到哪个路径」，**不写任何文件**（⚠️「dry」指的是**不落盘**，不是零 IO——不读库就数不出条数） |
| 真写 | 必须显式 ``--write``；``--out`` **必填**（**没有默认落点** ⇒ 绝不写 C 盘 / 用户目录） |
| 数据源 | ``agent_runs`` + ``agent_steps``（**只读**；不碰 ``session_events``） |
| 输出 | JSONL（一行一个 span）或 ``--format json``（数组） |
| 字段 | ``trace_id`` / ``span_id`` / ``parent_span_id`` / ``name`` / ``start_time`` / ``end_time`` / ``attributes``；**属性名常量从 ``cubeloop.tracing.schema`` 引用**（OTel GenAI semconv v1.41.0，纯常量模块、零 OTel import） |
| 出网 | **零**（纯本地读库 + 写文件）；不用网络客户端 |
| 依赖 | **零新增**（stdlib + 既有 SQLAlchemy 栈） |
| 并发 | **不起后台线程、不 ``atexit.register``**（那正是 §7.2 判 ragas 违约的两条机制） |

## 如实登记的边界

- ``trace_id`` 由 ``run.id`` **确定性派生**（32 位十六进制）——v2 侧没有真 W3C trace 上下文
  （也没引 OTel SDK），**同一次导出同一行永远是同一个 id**，便于外部后端去重；
- 首版**只导两张审计表**：``agent_steps`` 目前只有 ``tool_call`` 一种 ``type``
  （``thought/tool_result/message/plan`` 四类**无生产者**），故 span 只有
  ``invoke_agent``（run）与 ``execute_tool``（step）两种名字；``session_events`` 的 18 类事件
  导出**不在本条**（要另做，见开工包 §9.2）；
- ``attributes`` 里值为 ``None`` 的键**不写**（OTel 不允许 null 属性值），故
  ``chat_id`` 为 NULL 的 run（``ON DELETE SET NULL`` 留下的）不会带 ``gen_ai.conversation.id``。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

#: 让 ``python scripts/export_agent_trace.py`` 在**任何 cwd** 下都能 import 到 ``app``
#: （不必先设 ``PYTHONPATH``）——这个脚本是给用户自己跑的工具，少一个环境依赖就少一个坑。
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from cubeloop.tracing.schema import (  # noqa: E402 —— 必须排在 sys.path 补齐之后
    CUBELOOP_METADATA_PREFIX,
    CUBELOOP_RUN_ID,
    CUBELOOP_RUN_OUTCOME,
    CUBELOOP_TOOL_IS_ERROR,
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

try:  # 中文 / 符号输出不因控制台编码中断或乱码（回归修复 同型：脚本自己钉 stdout）
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 —— 老 Python / 非文本 stdout 下退化为默认编码
    pass

#: ``--limit`` 的默认与上限（run 条数；口径与 ``GET /api/agent/audit`` 的 ``limit`` 夹取一致）
DEFAULT_RUN_LIMIT = 200
MAX_RUN_LIMIT = 500

#: 生成的 trace/span id 的**命名空间前缀**（同库多来源共存时不会与别的系统撞 id）
_ID_NAMESPACE = "zhilan"


# --- 纯函数（无 IO，可直接单测）-------------------------------------------


def _hex_id(seed: str, length: int) -> str:
    """确定性派生 id：``sha256(seed)`` 的前 ``length`` 位十六进制。"""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:length]


def trace_id_for_run(run_id: int) -> str:
    """某 run 的 ``trace_id``（32 位十六进制；一个 run = 一条 trace）。"""
    return _hex_id(f"{_ID_NAMESPACE}:agent_run:{int(run_id)}", 32)


def span_id_for_run(run_id: int) -> str:
    """run span 的 ``span_id``（16 位）。与 :func:`trace_id_for_run` **不同种子**，不会互为前缀。"""
    return _hex_id(f"{_ID_NAMESPACE}:agent_run:span:{int(run_id)}", 16)


def span_id_for_step(step_id: int) -> str:
    """step span 的 ``span_id``（16 位）。与 run span 命名空间分开 ⇒ 同号主键不撞 id。"""
    return _hex_id(f"{_ID_NAMESPACE}:agent_step:span:{int(step_id)}", 16)


def _iso(value: datetime | None) -> str | None:
    """``datetime`` → ISO 字符串（``None`` 原样透传）。"""
    return value.isoformat() if value else None


def _attrs(pairs: dict) -> dict:
    """丢掉 ``None`` 值的属性（OTel 不允许 null 属性值），其余原样。"""
    return {key: value for key, value in pairs.items() if value is not None}


def _text(value) -> str | None:  # noqa: ANN001
    """转成字符串（``None`` / 空串 ⇒ ``None``，即不写这个属性）。"""
    if value is None:
        return None
    text = str(value)
    return text or None


def build_spans(runs: list[dict], steps: list[dict]) -> list[dict]:
    """把两张表的行投影成 span 列表（**纯函数**：不读库、不写文件）。

    - 每个 run 一个 ``invoke_agent`` span（``end_time`` = ``finished_at``，未收口时 ``None``）；
    - 每个 step 一个 ``execute_tool`` span，``parent_span_id`` = 它所属 run 的 span id；
    - **同一个 run 的 step 排在一起**（输入顺序 = ``(run_id, step_no)``，见 :func:`collect`）。
    """
    records: list[dict] = []
    for run in runs:
        run_id = int(run["id"])
        meta = run.get("meta") if isinstance(run.get("meta"), dict) else {}
        records.append(
            {
                "trace_id": trace_id_for_run(run_id),
                "span_id": span_id_for_run(run_id),
                "parent_span_id": None,
                "name": SPAN_NAME_INVOKE_AGENT,
                "start_time": _iso(run.get("started_at")),
                "end_time": _iso(run.get("finished_at")),
                "attributes": _attrs(
                    {
                        GEN_AI_OPERATION_NAME: OP_INVOKE_AGENT,
                        GEN_AI_CONVERSATION_ID: _text(run.get("chat_id")),
                        CUBELOOP_RUN_ID: str(run_id),
                        CUBELOOP_RUN_OUTCOME: _text(run.get("status")),
                        f"{CUBELOOP_METADATA_PREFIX}mode": _text(run.get("mode")),
                        f"{CUBELOOP_METADATA_PREFIX}message_id": _text(run.get("message_id")),
                        f"{CUBELOOP_METADATA_PREFIX}retrieval_id": _text(meta.get("retrieval_id")),
                        f"{CUBELOOP_METADATA_PREFIX}turn": _text(meta.get("turn")),
                        # ⚠️ 报错**正文**走 ``cubeloop.metadata.error``，**不写** ``error.type``：
                        # ``schema.py`` 把 ``error.type`` 定义为「Error classification（CR 用）」
                        # （``is_aborted_attrs()`` 就是按它的两个固定取值判读的）⇒ 往里塞一段自由文本
                        # 会让下游按它聚合时拿到无界基数，cubeloop 自己的读法也读不出「失败」。
                        f"{CUBELOOP_METADATA_PREFIX}error": _text(meta.get("error")),
                    }
                ),
            }
        )
    for step in steps:
        run_id = int(step["run_id"])
        # ⚠️ 这里**不**校验 ``run_id`` 是否出现在 ``runs`` 里：契约是「调用方先按 run 收敛 step」
        # （:func:`collect` 就是这么做的），在此重复防御属多余；真喂进不匹配的输入，产出的是
        # 一条指向「本轮没导出的 run」的父 id（悬空引用），由调用方保证不这么用。
        meta = step.get("meta") if isinstance(step.get("meta"), dict) else {}
        arguments = meta.get("args")
        records.append(
            {
                "trace_id": trace_id_for_run(run_id),
                "span_id": span_id_for_step(int(step["id"])),
                "parent_span_id": span_id_for_run(run_id),
                "name": SPAN_NAME_EXECUTE_TOOL,
                "start_time": _iso(step.get("created_at")),
                # step 表没有结束时间列 ⇒ 如实不给（不拿别的字段假装）
                "end_time": None,
                "attributes": _attrs(
                    {
                        GEN_AI_OPERATION_NAME: OP_EXECUTE_TOOL,
                        GEN_AI_TOOL_NAME: _text(step.get("tool_name")),
                        GEN_AI_TOOL_CALL_ID: _text(step.get("tool_call_id")),
                        CUBELOOP_TOOL_IS_ERROR: (
                            None if meta.get("ok") is None else (not bool(meta.get("ok")))
                        ),
                        GEN_AI_TOOL_CALL_ARGUMENTS: (
                            None
                            if arguments is None
                            else json.dumps(arguments, ensure_ascii=False, sort_keys=True)
                        ),
                        GEN_AI_TOOL_CALL_RESULT: _text(step.get("content")),
                        f"{CUBELOOP_METADATA_PREFIX}step_no": _text(step.get("step_no")),
                        f"{CUBELOOP_METADATA_PREFIX}ms": _text(meta.get("ms")),
                        f"{CUBELOOP_METADATA_PREFIX}error": _text(meta.get("error")),
                    }
                ),
            }
        )
    return records


def serialize(records: list[dict], fmt: str = "jsonl") -> str:
    """span 列表 → 落地文本。``jsonl`` 一行一个 span；``json`` 一个数组。"""
    if fmt == "json":
        return json.dumps(records, ensure_ascii=False, indent=2) + "\n"
    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)


def parse_since(value: str | None, *, field: str = "--since") -> datetime | None:
    """解析 ``--since``（ISO 8601，作用在 ``agent_runs.started_at``）；坏值抛中文 ``ValueError``。

    带时区的输入按**本机时区**折算成 naive（与库内时间的写入口径一致，同
    :func:`app.agent.audit.parse_audit_time`）。
    """
    text = str(value or "").strip()
    if not text:
        return None
    candidate = text[:-1] + "+00:00" if text[-1] in ("Z", "z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"{field} 不是合法的 ISO 8601 时间：{text}") from exc
    if parsed.tzinfo is not None:
        # 与 `GET /api/agent/audit` 的 `since` **同一套折算规则**（单一真相源）：
        # 折到**库内时钟**所在时区（SQLite 是 UTC、MySQL 是服务器本地时间）——
        # 折到「本机时区」在 SQLite 上会整段偏一个时区。延迟 import：脚本 --help 不碰这些。
        from app.agent.audit import fold_to_db_clock

        parsed = fold_to_db_clock(parsed)
    return parsed


def clamp_limit(value) -> int:  # noqa: ANN001
    """``--limit`` 夹在 ``1..MAX_RUN_LIMIT``（坏值抛中文 ``ValueError``）。"""
    try:
        return max(1, min(int(value), MAX_RUN_LIMIT))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"--limit 需要整数，收到 {value!r}") from exc


def _limit_arg(value: str) -> int:
    """``--limit`` 的 argparse 转换器：坏值翻成**中文** ``ArgumentTypeError``（退出码 2）。

    ⚠️ **不能写 ``type=int``**：那样坏值会被 argparse 用自己的英文 usage 挡掉，
    ``clamp_limit`` 的中文报错**永远不可达**——而「坏参数可读中文报错」是本条的验收项
    （开工包 §2.3 的测试清单第 4 条）。实测踩到过，故留这条注释。
    """
    try:
        return clamp_limit(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


# --- 读库（只读两张表）----------------------------------------------------


def _run_row(run) -> dict:  # noqa: ANN001
    """``AgentRun`` → 纯 dict（**在 session 关闭前**投影，避免 detached 对象读属性炸）"""
    return {
        "id": int(run.id),
        "chat_id": run.chat_id,
        "message_id": run.message_id,
        "mode": run.mode,
        "status": run.status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "meta": run.meta,
    }


def _step_row(step) -> dict:  # noqa: ANN001
    """``AgentStep`` → 纯 dict（同上）。"""
    return {
        "id": int(step.id),
        "run_id": int(step.run_id),
        "step_no": int(step.step_no),
        "type": step.type,
        "tool_name": step.tool_name,
        "tool_call_id": step.tool_call_id,
        "content": step.content,
        "created_at": step.created_at,
        "meta": step.meta,
    }


async def _query(db, *, chat_id, run_id, since, limit) -> tuple[list[dict], list[dict]]:  # noqa: ANN001
    """在给定 session 上查两表（**只读**，不 commit / 不 flush）。"""
    from sqlalchemy import select

    from app.models import AgentRun, AgentStep

    stmt = select(AgentRun).order_by(AgentRun.started_at.desc(), AgentRun.id.desc()).limit(limit)
    if chat_id is not None:
        stmt = stmt.where(AgentRun.chat_id == int(chat_id))
    if run_id is not None:
        stmt = stmt.where(AgentRun.id == int(run_id))
    if since is not None:
        stmt = stmt.where(AgentRun.started_at >= since)
    runs = [_run_row(row) for row in (await db.execute(stmt)).scalars().all()]
    run_ids = [row["id"] for row in runs]
    steps: list[dict] = []
    if run_ids:
        # ⚠️ 按 ``(run_id, step_no)`` 排：``step_no`` 是**完成序**（并行工具调用下 ≠ 发起序），
        # 对账请用 ``tool_call_id``（与 `GET /api/agent/audit` 同一口径）。
        step_stmt = (
            select(AgentStep)
            .where(AgentStep.run_id.in_(run_ids))
            .order_by(AgentStep.run_id, AgentStep.step_no)
        )
        steps = [_step_row(row) for row in (await db.execute(step_stmt)).scalars().all()]
    return runs, steps


async def collect(
    *,
    chat_id: int | None = None,
    run_id: int | None = None,
    since: datetime | None = None,
    limit: int = DEFAULT_RUN_LIMIT,
    session=None,  # noqa: ANN001 —— 给了就用它（用例注入测试库），不给就开产品库
) -> tuple[list[dict], list[dict]]:
    """读 ``agent_runs`` / ``agent_steps``，返回 ``(runs, steps)`` 两份纯 dict。

    ``--limit`` 作用在 **run 条数**上（不是 step 条数）：一次 trace 导出的单位是「一轮」，
    按 step 截断会把某一轮劈成半个——那对排查没有价值。
    """
    if session is not None:
        return await _query(session, chat_id=chat_id, run_id=run_id, since=since, limit=limit)
    from app.db.session import SessionLocal, engine

    try:
        async with SessionLocal() as db:
            return await _query(db, chat_id=chat_id, run_id=run_id, since=since, limit=limit)
    finally:
        # ⚠️ **必须显式释放连接池**：``engine`` 是进程级单例，脚本用 ``asyncio.run`` 起一次性事件循环；
        # 不 dispose 的话，aiomysql 的连接会在**循环关闭之后**才被 GC，于是每次运行都往 stderr 吐
        # 一串「Exception ignored in: Connection.__del__ … Event loop is closed」——证据日志里
        # 一堆假异常，读的人会以为脚本真的失败了（实测踩到）。
        await engine.dispose()


# --- CLI ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """命令行面（照 `5.5` 开工包 §2.3 的表；``--out`` 必填、``--write`` 才落盘）。"""
    parser = argparse.ArgumentParser(
        prog="export_agent_trace.py",
        description="把 agent_runs / agent_steps 摊成 OTel GenAI 形状的 span（默认 dry-run，零出网）",
    )
    parser.add_argument("--out", type=Path, required=True, help="落点路径（必填；文件，不是目录）")
    parser.add_argument("--write", action="store_true", help="真的落盘（默认只打印将导出什么）")
    parser.add_argument("--chat-id", type=int, default=None, help="只导某个会话的 run")
    parser.add_argument("--run-id", type=int, default=None, help="只导某一个 run")
    parser.add_argument("--since", default=None, help="只导 started_at >= 该 ISO 8601 时间的 run")
    parser.add_argument(
        "--limit",
        type=_limit_arg,
        default=DEFAULT_RUN_LIMIT,
        help=f"最多导多少个 run（1..{MAX_RUN_LIMIT}）",
    )
    parser.add_argument("--format", choices=("jsonl", "json"), default="jsonl", help="输出格式")
    return parser


def main(argv: list[str] | None = None) -> int:
    """入口。返回退出码：``0`` 成功 / ``2`` 参数错误（**坏参数给中文可读报错**）。"""
    args = build_parser().parse_args(argv)
    try:
        since = parse_since(args.since)
        limit = clamp_limit(args.limit)
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2
    runs, steps = asyncio.run(
        collect(chat_id=args.chat_id, run_id=args.run_id, since=since, limit=limit)
    )
    records = build_spans(runs, steps)
    print(
        f"{'导出' if args.write else 'DRY RUN'}：run {len(runs)} 个 / step {len(steps)} 个 "
        f"/ span {len(records)} 行"
    )
    print(f"落点：{args.out}（格式 {args.format}）")
    if not args.write:
        print("未写任何文件（加 --write 才落盘）；零出网、零新增依赖。")
        return 0
    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(serialize(records, args.format), encoding="utf-8")
    except OSError as exc:
        # 落点不可写（盘不存在 / 只读目录 / 权限）：给**中文可读**报错与退出码 2，
        # 别把原始 traceback 甩出来——这个脚本是给人手跑的，栈对使用者没有信息量（实测踩到）。
        print(f"落点不可写：{args.out}（{exc}）", file=sys.stderr)
        return 2
    print(f"已写入：{args.out}（{len(records)} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
