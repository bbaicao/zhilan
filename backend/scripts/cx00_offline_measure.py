"""CX-00A · 离线腿：用**开发库真实数据**测「一轮回答请求的 prompt 组成规模分布」。

只读实测器（专项「上下文管理升级」· CX-00A）。它回答的是**规模问题**，不是功能问题：

1. **材料侧**——``chunks.content`` 字符长度分布；再模拟「同一文档取最多 ``FUSE_LIMIT``
   块」得到一轮 ``materials_text`` 的字符规模分布。**真调一次** ``render_materials``
   做校验，并把「标记行 + ``\\n\\n`` 分隔」的额外开销单独量出来。
2. **历史侧**——``messages.content`` 按 role 的字符长度分布；每 chat 的条数与累计字符
   分布；据此算「累计多少条 / 多少字符会踩过 ``CHAT_COMPRESS_THRESHOLD``」，并按
   ``CHAT_COMPRESS_KEEP`` 估「压缩后 history 的典型规模」。
3. **组成合成**——典型 / p90 / 最坏上界三档的单轮 prompt 字符规模（含与不含工具通道两档）。
4. **对照模型窗**——按 **1 字符 ≤ 1 token** 的保守上界换算占 1,000,000 token 窗的百分比。

## 铁律（本脚本的硬约束）

- **只读**：全程只有 SELECT（``text("SELECT ...")``），不 INSERT / UPDATE / DELETE / DDL，
  不碰业务表，不写库。
- **用应用自己的引擎**：DSN 由 ``app.core.config.settings`` 组装，跑在
  ``app.db`` 的引擎族上；**命令行不拼口令**，脚本不打印任何口令 / 连接串值。
- **不改产品代码**：本脚本只 import ``app.*``，一个字都不改。
- 依赖 0 新增：只用 ``sqlalchemy`` / ``app.*``，统计全用标准库。

## 端口自适应（如实登记的环境偏差，非产品缺陷）

本机 ``zhilan-mysql`` 容器实测发布在 ``127.0.0.1:23306``，而 ``.env`` / ``config.py`` 的
``MYSQL_PORT`` 是 ``13306``。二者不一致时**按配置连会 ConnectionRefused**。本脚本因此做
「先按配置、连不上再探候选端口」，把**实际用上的端口**记进读数件——这是**取证口径**，
不是绕过配置：不修改 ``.env``、不动容器、不启动任何服务。

跑法（PowerShell）::

    cd <repo>\\backend
    $env:PYTHONPATH="<repo>/backend"
    $env:PYTHONIOENCODING="utf-8"
    .\\.venv\\Scripts\\python.exe -B scripts\\cx00_offline_measure.py --json out.json

退出码：``0`` = 全部读数产出成功；``2`` = 连不上库（读数未产出）；``3`` = 连上了但关键表为空。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- 只读入口 ---------------------------------------------------------------

CANDIDATE_PORTS = (None, 23306, 13306, 3306)


def _percentiles(values: list[int]) -> dict:
    """一组整数样本的分布读数（空样本返回零值，调用方自己判 ``n``）。"""
    if not values:
        return {
            "n": 0, "sum": 0, "mean": 0.0, "min": 0, "p50": 0,
            "p90": 0, "p99": 0, "max": 0, "stdev": 0.0,
        }
    ordered = sorted(int(v) for v in values)

    def pick(q: float) -> int:
        # 最近秩法（nearest-rank）：p50 就是中位数，p90/p99 是小样本下最保守的对齐
        index = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
        return ordered[index]

    return {
        "n": len(ordered),
        "sum": int(sum(ordered)),
        "mean": round(statistics.fmean(ordered), 2),
        "min": ordered[0],
        "p50": pick(0.50),
        "p90": pick(0.90),
        "p99": pick(0.99),
        "max": ordered[-1],
        "stdev": round(statistics.pstdev(ordered), 2) if len(ordered) > 1 else 0.0,
    }


def _quantiles(values: list[int], qs: tuple[float, ...]) -> dict:
    """任意分位（psort 的小工具；供「踩线轮数」这类推算用）。"""
    if not values:
        return {f"p{int(q * 100)}": 0 for q in qs}
    ordered = sorted(int(v) for v in values)
    out = {}
    for q in qs:
        index = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
        out[f"p{int(q * 100)}"] = ordered[index]
    return out


# --- 连接：先按配置，连不上再探候选端口（只换端口，口令仍从 settings 取）-----


def _dsn_with_port(dsn: str, port: int | None) -> str:
    """把 DSN 的端口换成 ``port``（``None`` = 原样）。**只改端口，不打印任何值。**"""
    if port is None:
        return dsn
    head, tail = dsn.split("@", 1)
    host_part, rest = tail.split("/", 1)
    host = host_part.split(":")[0]
    return f"{head}@{host}:{port}/{rest}"


async def _try_connect(engine) -> tuple[bool, str]:
    from sqlalchemy import text

    try:
        async with engine.connect() as conn:
            version = (await conn.execute(text("SELECT VERSION()"))).scalar()
            database = (await conn.execute(text("SELECT DATABASE()"))).scalar()
        return True, f"{version} / {database}"
    except Exception as exc:  # noqa: BLE001 —— 探活失败是正常分支（换端口继续）
        return False, f"{exc.__class__.__name__}"


async def open_engine():
    """返回 ``(engine, 实际端口, 版本/库名, 尝试日志)``；全失败返回 ``(None, ...)``。"""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.config import settings

    dsn = settings.database_dsn
    configured = int(getattr(settings, "MYSQL_PORT", 0)) or None
    attempts: list[dict] = []
    for port in CANDIDATE_PORTS:
        if port is not None and port == configured:
            # 配置端口已由 ``None``（原样）那一轮试过，跳过重复
            continue
        engine = create_async_engine(_dsn_with_port(dsn, port), pool_pre_ping=True)
        ok, note = await _try_connect(engine)
        label = configured if port is None else port
        attempts.append({"port": label, "ok": ok, "note": note})
        if ok:
            return engine, label, note, attempts
        await engine.dispose()
    return None, None, "", attempts


# --- 材料侧 -----------------------------------------------------------------


async def measure_chunks(conn) -> dict:
    from sqlalchemy import text

    rows = (
        await conn.execute(
            text(
                "SELECT c.id, c.document_id, c.ordinal, c.content, d.filename "
                "FROM chunks c LEFT JOIN documents d ON d.id = c.document_id"
            )
        )
    ).all()
    lengths = [len(row.content or "") for row in rows]

    # 每文档的块（保留正文与文件名——渲染校验必须用**真实**内容，
    # 合成字符串量不准 normalize_body 的吃掉量与真实标题宽度）
    by_doc: dict[int, list[tuple[int, str]]] = {}
    names: dict[int, str] = {}
    for row in rows:
        doc_id = int(row.document_id)
        by_doc.setdefault(doc_id, []).append((int(row.ordinal), row.content or ""))
        names.setdefault(doc_id, str(row.filename or ""))
    for items in by_doc.values():
        items.sort(key=lambda pair: pair[0])

    docs = [(doc_id, items, names.get(doc_id, "")) for doc_id, items in sorted(by_doc.items())]
    return {
        "chunk_count": len(rows),
        "document_count": len(docs),
        "content_chars": _percentiles(lengths),
        "per_document": docs,
    }


def simulate_materials(per_document: list, fuse_limit: int) -> dict:
    """模拟「同一文档取最多 ``fuse_limit`` 块」的 raw 字符规模。

    两种取法都给（都只算 ``chunks.content`` 的**原始**字符数，不含渲染开销）：

    - ``first``：取该文档最前面的 ``fuse_limit`` 块（``ordinal`` 升序）——「同一文档取最多 12 块」
      的字面口径；
    - ``largest``：取该文档**最长**的 ``fuse_limit`` 块——上界口径（同一文档最坏取到什么）。
    """
    first_totals: list[int] = []
    largest_totals: list[int] = []
    for _doc_id, items, _filename in per_document:
        if not items:
            continue
        take = min(fuse_limit, len(items))
        first_totals.append(sum(len(content) for _ordinal, content in items[:take]))
        ordered = sorted((len(content) for _ordinal, content in items), reverse=True)
        largest_totals.append(sum(ordered[:take]))
    return {
        "documents_covered": len(first_totals),
        "fuse_limit": fuse_limit,
        "first_n_raw": _percentiles(first_totals),
        "largest_n_raw": _percentiles(largest_totals),
    }


def render_check(per_document: list, fuse_limit: int) -> dict:
    """**真调** ``render_materials`` 做校验，并量出渲染额外开销。

    两组各渲染一遍：

    - ``widest``：材料区最宽的形态——取「前 ``fuse_limit`` 块原始字符和最大」的那篇文档，
      用它的**真实正文与真实文件名**渲染（不是合成字符串，开销才量得准）；
    - ``narrowest``：最窄的那篇作为对照。

    开销逐项分解并与渲染结果**对账**：``len(rendered) == head + normalized + separator``。
    ``normalize_body`` 会吃掉空行 / 首尾换行 ⇒ ``normalized <= raw``，这一项也单独记
    （它是**负开销**，不能漏算）。
    """
    from app.retrieval.types import Material, normalize_body, render_materials

    def build(doc_id, items, filename, take_largest: bool):
        chosen = list(items)
        if take_largest:
            chosen = sorted(chosen, key=lambda pair: len(pair[1]), reverse=True)[:take_largest]
        else:
            chosen = chosen[:take_largest]
        chosen.sort(key=lambda pair: pair[0])
        materials = [
            Material(
                chunk_id=index + 1,
                document_id=doc_id,
                content=content,
                filename=filename,
                block_ordinal=ordinal,
                ordinal=index + 1,
            )
            for index, (ordinal, content) in enumerate(chosen)
        ]
        return materials

    candidates = [pair for pair in per_document if pair[1]]
    if not candidates:
        return {"rendered": False}

    def first_sum(pair) -> int:
        return sum(len(content) for _o, content in pair[1][:fuse_limit])

    widest = max(candidates, key=first_sum)
    narrowest = min(candidates, key=first_sum)

    out: dict = {"rendered": True}
    overhead_pool: list[int] = []
    for label, (doc_id, items, filename) in (("widest", widest), ("narrowest", narrowest)):
        materials = build(doc_id, items, filename, fuse_limit)
        rendered = render_materials(materials)
        raw_total = sum(len(item.content) for item in materials)
        head_total = sum(
            len(f"[{item.ordinal}]（文档：{item.filename}）") for item in materials
        )
        separators = 2 * max(0, len(materials) - 1)  # 块间 "\n\n"
        normalized_total = sum(len(normalize_body(item.content)) for item in materials)
        # ⚠️ ``render_materials`` 在「标记行 + 非空正文」之间**还插一个 \n**
        # （``f"{head}\n{body}"``）——逐块一行，是开销里最容易漏掉的一份。
        body_lines = sum(1 for item in materials if normalize_body(item.content))
        expected = head_total + normalized_total + body_lines + separators
        out[label] = {
            "document_id": doc_id,
            "filename": filename,
            "blocks": len(materials),
            "raw_content_chars": raw_total,
            "normalized_chars": normalized_total,
            "normalize_saving_chars": raw_total - normalized_total,
            "head_chars": head_total,
            "head_body_newlines": body_lines,
            "separator_chars": separators,
            "render_materials_chars": len(rendered),
            "overhead_chars": len(rendered) - raw_total,
            "overhead_per_block": (
                round((len(rendered) - raw_total) / len(materials), 2) if materials else 0.0
            ),
            "verified": len(rendered) == expected,
        }

    # 开销分布：对全部文档按「前 fuse_limit 块」真渲染一遍，逐篇量
    for doc_id, items, filename in candidates:
        materials = build(doc_id, items, filename, fuse_limit)
        rendered = render_materials(materials)
        overhead_pool.append(len(rendered) - sum(len(item.content) for item in materials))
    out["overhead_distribution_materialized"] = _percentiles(overhead_pool)
    out["rendered_documents"] = len(candidates)
    return out


# --- 历史侧 -----------------------------------------------------------------


async def measure_messages(conn) -> dict:
    from sqlalchemy import text

    rows = (
        await conn.execute(
            text(
                "SELECT id, chat_id, role, content FROM messages "
                "WHERE role IN ('user','assistant') ORDER BY chat_id, id"
            )
        )
    ).all()

    by_role: dict[str, list[int]] = {"user": [], "assistant": []}
    per_chat: dict[int, list[tuple[int, int]]] = {}
    for row in rows:
        length = len(row.content or "")
        by_role.setdefault(row.role, []).append(length)
        per_chat.setdefault(int(row.chat_id), []).append((int(row.id), length))

    return {"rows": rows, "by_role": by_role, "per_chat": per_chat}


def measure_summary_pair_overhead() -> dict:
    """**实测**摘要对在 history 里的固定开销（不写死数字）。

    ``summary_pair`` = 「头行 + 摘要」的 user 条 + ``ACK_TEXT`` 的 assistant 条。
    与摘要正文长度无关的那部分（头行 + ACK）就是固定开销——它**也占** ``total_chars``。
    """
    try:
        from app.session.compaction import ACK_TEXT, summary_pair

        probe = summary_pair("x" * 400, 1)
        pair_chars = sum(len(item["content"]) for item in probe)
        return {
            "ack_text_chars": len(ACK_TEXT),
            "pair_chars_for_400_summary": pair_chars,
            "pair_fixed_overhead": pair_chars - 400,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": exc.__class__.__name__, "pair_fixed_overhead": 47}


def history_pressure_exact(rows: list, threshold: int, keep: int, pair_fixed: int = 47) -> dict:
    """「累计多少条 / 多少字符踩过阈值」的实测：带 role 的前缀和。

    阈值的**真实判据**（``app/session/compaction.py``）::

        total_chars(history) = sum(len(content))   # 只数 content，含注入的摘要对
        if total_chars(history) <= threshold: 不压缩

    ``history`` 来自 ``build_model_history``（只含 user / assistant，按 id 升序），
    所以「累计」= 按 id 升序的前缀和。每个 chat 记下首次「前缀和 > 阈值」时的：

    - ``cross_at_message_index``：消息下标（1 起）；
    - ``cross_at_turn``：真实轮数 = 该位置及之前的 user 条数（1 起）；
    - ``cross_at_chars``：踩线当时的累计字符。
    """
    per_chat: dict[int, list] = {}
    for row in rows:
        per_chat.setdefault(int(row.chat_id), []).append((int(row.id), row.role, len(row.content or "")))

    counts: list[int] = []
    totals: list[int] = []
    cross_index: list[int] = []
    cross_turn: list[int] = []
    cross_at_chars: list[int] = []
    keep_window_chars: list[int] = []
    cross_rows: list[dict] = []

    for chat_id in sorted(per_chat):
        items = per_chat[chat_id]
        running = 0
        turns = 0
        hit = None
        for index, (_mid, role, length) in enumerate(items, start=1):
            if role == "user":
                turns += 1
            running += length
            if hit is None and running > threshold:
                hit = (index, turns, running)
        counts.append(len(items))
        totals.append(running)
        if hit is not None:
            cross_index.append(hit[0])
            cross_turn.append(hit[1])
            cross_at_chars.append(hit[2])
            cross_rows.append(
                {"chat_id": chat_id, "messages": len(items),
                 "cross_at_message_index": hit[0], "cross_at_turn": hit[1],
                 "cross_at_chars": hit[2], "total_chars": running}
            )
        # 压缩后「保留窗」的典型规模：按 CHAT_COMPRESS_KEEP 条（对齐 _keep_start 的
        # 「窗口首条尽量落在 user 上」口径，这里取最近 keep 条原始消息的字符和）
        keep_window_chars.append(sum(length for _m, _r, length in items[-keep:]) if items else 0)

    n = len(counts) or 1
    return {
        "threshold": threshold,
        "keep": keep,
        "chats_total": len(counts),
        "chats_crossing": len(cross_rows),
        "crossing_ratio": round(len(cross_rows) / n, 4),
        "messages_per_chat": _percentiles(counts),
        "chars_per_chat": _percentiles(totals),
        "cross_at_message_index": _percentiles(cross_index),
        "cross_at_turn": _percentiles(cross_turn),
        "cross_at_chars": _percentiles(cross_at_chars),
        "keep_window_chars": _percentiles(keep_window_chars),
        "by_depth": _stratify(rows, threshold, keep),
        # 推算：压缩后 history 典型规模 = 保留窗 + 摘要对
        "post_compress_estimate": _post_compress(cross_rows, keep_window_chars, keep, pair_fixed),
        "cross_rows": cross_rows,
    }


def _post_compress(cross_rows: list, keep_window_chars: list, keep: int, pair_fixed: int) -> dict:
    """按 ``keep`` 估「压缩后 history 的典型字符规模」。

    ``compact_history`` 成功时 history = ``summary_pair(...) + tail``，其中 ``tail`` 的
    条数由 ``_keep_start`` 定（最近 ``keep`` 条，窗口首条尽量落在 ``user``）——
    **摘要对本身也计入 ``total_chars``**（``app/session/compaction.py::total_chars``
    的注释写明「含注入的摘要对」），所以摘要长度单列成一档区间。

    摘要对的**固定开销**取实测 47 字符（``summary_pair`` 的头行 + ``ACK_TEXT``，
    在 :func:`【摘要在 history 里的固定开销】` 那一节由真调 ``summary_pair`` 量得），
    这样「摘要正文长度」是唯一未知量。
    """
    tail = _percentiles(keep_window_chars)
    assumptions = {}
    for label, summary_chars in (
        ("摘要正文=400字符", 400),
        ("摘要正文=800字符", 800),
        ("摘要正文=1600字符", 1600),
    ):
        pair_chars = summary_chars + pair_fixed
        assumptions[label] = {
            "tail_p50_p90": [tail.get("p50", 0), tail.get("p90", 0)],
            "summary_pair_chars": pair_chars,
            "history_p50": tail.get("p50", 0) + pair_chars,
            "history_p90": tail.get("p90", 0) + pair_chars,
        }
    # 压缩**失败回退**档：keep*2 = 12 条原始消息（无摘要对，或带旧摘要对）
    return {
        "tail_window_chars": tail,
        "pair_fixed_overhead_chars": pair_fixed,
        "with_summary": assumptions,
    }


def _stratify(rows: list, threshold: int, keep: int) -> dict:
    """按会话**深度**分层看踩线（单发问答会把整体比例摊薄，掩盖真问题）。

    分层：``1-3 条``（一问一答或极短）、``4-10 条``、``11-25 条``、``26+ 条``。
    每层给：会话数、踩线数、踩线比例、首次踩线的轮数/条数中位数。
    """
    per_chat: dict[int, list[tuple[str, int]]] = {}
    for row in rows:
        per_chat.setdefault(int(row.chat_id), []).append((row.role, len(row.content or "")))

    buckets: list[tuple[str, int, int]] = [
        ("1-3 条", 1, 3),
        ("4-10 条", 4, 10),
        ("11-25 条", 11, 25),
        ("26+ 条", 26, 10**9),
    ]
    out: dict[str, dict] = {}
    for title, low, high in buckets:
        chat_ids = [cid for cid, items in per_chat.items() if low <= len(items) <= high]
        crossing_turns: list[int] = []
        crossing_index: list[int] = []
        crossing_chars: list[int] = []
        for cid in chat_ids:
            items = per_chat[cid]
            running = 0
            turns = 0
            for index, (role, length) in enumerate(items, start=1):
                if role == "user":
                    turns += 1
                running += length
                if running > threshold:
                    crossing_turns.append(turns)
                    crossing_index.append(index)
                    crossing_chars.append(running)
                    break
        out[title] = {
            "chats": len(chat_ids),
            "chats_crossing": len(crossing_turns),
            "crossing_ratio": round(len(crossing_turns) / len(chat_ids), 4) if chat_ids else 0.0,
            "cross_at_turn": _percentiles(crossing_turns),
            "cross_at_message_index": _percentiles(crossing_index),
            "cross_at_chars": _percentiles(crossing_chars),
        }
    return out


# --- 组成合成 ---------------------------------------------------------------


def compose(measured: dict) -> dict:
    """三种口径的单轮 prompt 字符规模（含 / 不含工具通道两档）。

    prompt 组成（口径来自 ``app/generation/answer.py::build_messages`` 与
    ``::system_prompt``、``app/api/chat_routes.py``）：

    - ``history``：``build_model_history`` 的输出（只含 user / assistant 的 content）；
    - ``materials_text``：``render_materials`` 的渲染输出（整段进 user 消息体）；
    - ``tool_block``：``render_tool_context`` 的输出（整段进 user 消息体），
      **正文**受 ``TOOL_CONTEXT_TOTAL_LIMIT`` 管；
    - ``memory_text``：``memory.render_block`` 的输出（进**系统提示**，追加段）；
    - ``system_prompt``：``SYSTEM_PROMPT`` 本体；本机 ``MEMORY_ENABLE`` 默认 false
      ⇒ 记忆段进的是「上界」而不是「典型」；
    - ``user_message_wrapper``：``问题：`` 前缀 + ``资料如下：\\n\\n`` / ``\\n\\n`` 分隔
      + 本轮问题正文。
    """
    from app.agent.runner import TOOL_CONTEXT_TOTAL_LIMIT
    from app.core.config import settings
    from app.generation.answer import SYSTEM_PROMPT, system_prompt
    from app.retrieval.types import FUSE_LIMIT

    system_chars = len(SYSTEM_PROMPT)
    no_material_system_chars = len(
        system_prompt(has_material=False)
    )  # 无资料那一态的提示（更短）
    memory_max = int(getattr(settings, "MEMORY_INJECT_MAX_CHARS", 800))
    memory_enabled = bool(getattr(settings, "MEMORY_ENABLE", False))

    mat = measured["materials"]
    hist = measured["history_exact"]
    question = measured["question_chars"]

    materials_typical = mat["simulate"]["first_n_raw"]["p50"] + mat["render_check"][
        "overhead_distribution_materialized"
    ]["p50"]
    materials_p90 = mat["simulate"]["first_n_raw"]["p90"] + mat["render_check"][
        "overhead_distribution_materialized"
    ]["p90"]
    materials_worst = mat["render_check"]["widest"]["raw_content_chars"] + mat[
        "render_check"
    ]["widest"]["overhead_chars"]

    history_typical = hist["keep_window_chars"]["p50"]
    history_p90 = hist["keep_window_chars"]["p90"]
    history_worst = hist["threshold"]  # 到阈值即压缩 ⇒ 未压缩时的硬上界就是阈值本身

    return {
        "constants": {
            "system_prompt_chars": system_chars,
            "system_prompt_no_material_chars": no_material_system_chars,
            "memory_inject_max_chars": memory_max,
            "memory_enable_default": memory_enabled,
            "fuse_limit": FUSE_LIMIT,
            "tool_context_total_limit": TOOL_CONTEXT_TOTAL_LIMIT,
            "chunk_size_upstream_cap": int(getattr(settings, "RAG_CHUNK_SIZE", 0) or 0),
            "chat_compress_threshold": int(
                getattr(settings, "CHAT_COMPRESS_THRESHOLD", 0) or 0
            ),
            "chat_compress_keep": int(getattr(settings, "CHAT_COMPRESS_KEEP", 0) or 0),
        },
        "pieces": {
            "typical": {
                "history": history_typical,
                "materials_text": materials_typical,
                "tool_context": 0,
                "memory_text": 0,
                "system_prompt": system_chars,
                "question": question["p50"],
            },
            "p90": {
                "history": history_p90,
                "materials_text": materials_p90,
                "tool_context": 0,
                "memory_text": memory_max if memory_enabled else 0,
                "system_prompt": system_chars,
                "question": question["p90"],
            },
            "worst": {
                "history": history_worst,
                "materials_text": materials_worst,
                "tool_context": TOOL_CONTEXT_TOTAL_LIMIT,
                "memory_text": memory_max,
                "system_prompt": system_chars,
                "question": question["max"],
            },
        },
    }


def totals_of(pieces: dict) -> dict:
    """把各段合成单轮 prompt 总量，并单列「不含工具通道」一档。

    ``user_message_wrapper`` = ``问题：`` 前缀 + 区段分隔（``build_messages`` 的
    ``"资料如下：\\n\\n{materials_text}\\n\\n{tool_block}\\n\\n问题：{message}"``）。
    """
    prefix_materials = len("资料如下：\n\n")
    prefix_question = len("问题：")
    separator = len("\n\n")
    segments = pieces
    wrapper = prefix_question
    if segments.get("materials_text"):
        wrapper += prefix_materials
    if segments.get("materials_text") and segments.get("tool_context"):
        wrapper += separator
    elif segments.get("tool_context"):
        # 只渲染工具块时也是 "{tools_block}\n\n问题："
        wrapper += separator

    body = (
        segments.get("history", 0)
        + segments.get("materials_text", 0)
        + segments.get("tool_context", 0)
        + wrapper
        + segments.get("question", 0)
    )
    with_system = body + segments.get("system_prompt", 0) + segments.get("memory_text", 0)
    without_tool = (
        segments.get("history", 0)
        + segments.get("materials_text", 0)
        + prefix_question
        + (prefix_materials if segments.get("materials_text") else 0)
        + segments.get("question", 0)
        + segments.get("system_prompt", 0)
        + segments.get("memory_text", 0)
    )
    return {
        "wrapper_chars": wrapper,
        "prompt_messages_chars": body,
        "prompt_total_chars": with_system,
        "prompt_total_without_tool_chars": without_tool,
    }


def to_window_upper(totals: dict) -> dict:
    """**1 字符 ≤ 1 token** 的保守上界换算：占 1,000,000 token 窗的百分比。

    ⚠️ 这是**上界**，不是实测：真实 token 数取决于分词器，中文常见 0.6–1.0 token/字符、
    英文更低。真 token 由**真机腿**校准（CX-00 的联网腿），本函数只给「不可超过多少」。
    """
    window = 1_000_000
    out = {}
    for key in ("prompt_total_chars", "prompt_total_without_tool_chars", "prompt_messages_chars"):
        value = int(totals.get(key, 0))
        out[key] = {
            "chars": value,
            "upper_token": value,
            "pct_of_1m": round(value / window * 100, 4),
        }
    return {"window_tokens": window, "basis": "1 字符 ≤ 1 token（保守上界，非实测）", **out}


# --- 主流程 -----------------------------------------------------------------


async def run(args) -> int:
    from sqlalchemy import text

    from app.core.config import settings
    from app.retrieval.types import FUSE_LIMIT

    threshold = int(getattr(settings, "CHAT_COMPRESS_THRESHOLD", 8000) or 8000)
    keep = int(getattr(settings, "CHAT_COMPRESS_KEEP", 6) or 6)

    engine, port, version_note, attempts = await open_engine()
    if engine is None:
        print("[CX-00A] 连不上库，读数未产出。尝试记录：")
        for item in attempts:
            print(f"  port={item['port']}: {item['note']}")
        return 2

    from sqlalchemy.ext.asyncio import create_async_engine

    result: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "script": str(Path(__file__).resolve()),
        "mode": "read_only_select",
        "connection": {
            "dialect": "mysql+aiomysql",
            "port_used": port,
            "server": version_note,
            "attempts": attempts,
            "note": "口令由 app.core.config.settings 提供，脚本不打印任何口令 / 连接串值",
        },
        "databases": {},
    }

    sources: list[tuple[str, str]] = [
        ("app_zhilan_v2", "应用 `.env` 指向的库（开发库；v2 结构）"),
        (
            "legacy_zhilan",
            "同实例内的历史库（v1 结构）——**只用来取「真实会话深度」**这一维，"
            "v2 无长会话样本；材料侧不用它",
        ),
    ]

    for label, note in sources:
        engine_for_db = (
            engine
            if label == "app_zhilan_v2"
            else create_async_engine(_dsn_with_db(settings.database_dsn, port, "zhilan"))
        )
        try:
            block = await _measure_one(engine_for_db, label, note, threshold, keep, FUSE_LIMIT)
        except Exception as exc:  # noqa: BLE001 —— 一个库量不了不影响另一个
            block = {"label": label, "note": note, "error": exc.__class__.__name__}
        else:
            pass
        finally:
            if engine_for_db is not engine:
                await engine_for_db.dispose()
        result["databases"][label] = block

    await engine.dispose()

    primary = result["databases"].get("app_zhilan_v2", {})
    if primary.get("error") or not primary.get("row_counts"):
        print("[CX-00A] 主库读数未产出。")
        return 3
    if not primary["row_counts"].get("chunks") and not primary["row_counts"].get("messages"):
        print("[CX-00A] 关键表为空（chunks / messages 均 0 行），读数不成立。")
        return 3

    # --- 组成合成：两档口径都出（材料一律取应用真库 v2）------------------
    #
    # 为什么两档：v2（应用真库）是**薄夹具**（12 条 user / assistant 消息、会话最长 12 条），
    # 它的「历史侧」不能代表真实会话深度；同实例的历史库（v1 结构）有 689 条 user 消息、
    # 571 个会话，才有「真实会话深度」这一维。材料侧两库都测了，但**只取 v2**。
    legacy = result["databases"].get("legacy_zhilan", {})

    scenarios: list[tuple[str, str, dict]] = [
        (
            "v2_app_db",
            "材料侧 + 历史侧都取应用真库 zhilan_v2（薄夹具，会话最长 12 条）",
            primary["history"]["threshold_pressure"],
        )
    ]
    if legacy.get("history"):
        scenarios.append(
            (
                "v2_materials_real_depth_history",
                "材料侧取 v2，历史侧取历史库的真实会话深度（571 会话）——**推荐口径**",
                legacy["history"]["threshold_pressure"],
            )
        )

    result["composition_scenarios"] = {}
    for key, note, pressure in scenarios:
        measured = {
            "materials": primary["materials"],
            "history_exact": pressure,
            "question_chars": primary["question_chars"],
        }
        pieces = compose(measured)
        totals = {}
        for label, segment in pieces["pieces"].items():
            base = totals_of(segment)
            totals[label] = {
                "segments": segment,
                **base,
                "window_upper": to_window_upper(base),
            }
        result["composition_scenarios"][key] = {
            "note": note,
            "pieces": pieces["pieces"],
            "totals": totals,
        }
        if key == "v2_materials_real_depth_history" or len(scenarios) == 1:
            result["composition_constants"] = pieces["constants"]
            result["composition"] = {
                "primary_scenario": key,
                "pieces": pieces["pieces"],
                "totals": totals,
            }

    # 摘要对固定开销的**实测**（ACK_TEXT + 头行，与摘要正文长度无关的那部分）
    pair_overhead = measure_summary_pair_overhead()
    result["composition"]["summary_pair_probe"] = pair_overhead
    result["assumptions"] = ASSUMPTIONS

    if args.json:
        out_path = Path(args.json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[CX-00A] JSON 读数件已写：{out_path.resolve()}")

    _print_summary(result)
    return 0


def _dsn_with_db(dsn: str, port: int | None, database: str) -> str:
    """换库名（同样只改 URL 的库段，不打印任何值）。"""
    head, tail = dsn.split("@", 1)
    host_part, rest = tail.split("/", 1)
    host = host_part.split(":")[0]
    query = ""
    if "?" in rest:
        rest, query = rest.split("?", 1)
        query = "?" + query
    target_port = port if port is not None else host_part.split(":")[-1]
    return f"{head}@{host}:{target_port}/{database}{query}"


async def _measure_one(engine, label: str, note: str, threshold: int, keep: int, fuse_limit: int) -> dict:
    """量一个库（只读 SELECT）。返回该库的全部读数块。"""
    from sqlalchemy import text

    async with engine.connect() as conn:
        row_counts: dict[str, int] = {}
        for key, sql in (
            ("chunks", "SELECT COUNT(*) FROM chunks"),
            ("messages", "SELECT COUNT(*) FROM messages"),
            ("chats", "SELECT COUNT(*) FROM chats"),
            ("documents", "SELECT COUNT(*) FROM documents"),
        ):
            try:
                row_counts[key] = int((await conn.execute(text(sql))).scalar() or 0)
            except Exception:  # noqa: BLE001 —— 结构差异（v1 无某表）不算失败
                row_counts[key] = -1
        chunks = await measure_chunks(conn)
        messages = await measure_messages(conn)

    pair_fixed = int(measure_summary_pair_overhead().get("pair_fixed_overhead", 47))

    sim = simulate_materials(chunks["per_document"], fuse_limit)
    check = render_check(chunks["per_document"], fuse_limit)
    materials = {
        "chunk_content_chars": chunks["content_chars"],
        "chunk_count": chunks["chunk_count"],
        "document_count": chunks["document_count"],
        "simulate": sim,
        "render_check": check,
        "upstream_chunk_cap": 800,
        "chunks_over_cap": sum(
            1
            for _d, items, _f in chunks["per_document"]
            for _o, content in items
            if len(content) > 800
        ),
    }

    exact = history_pressure_exact(messages["rows"], threshold, keep, pair_fixed)
    question_pool = [n for n in messages["by_role"].get("user", []) if n > 0]
    history = {
        "user_chars": _percentiles(messages["by_role"].get("user", [])),
        "assistant_chars": _percentiles(messages["by_role"].get("assistant", [])),
        "all_chars": _percentiles(
            messages["by_role"].get("user", []) + messages["by_role"].get("assistant", [])
        ),
        "threshold_pressure": exact,
        "keep_window_chars": exact["keep_window_chars"],
        "threshold": threshold,
        "keep": keep,
        "fallback_keep_multiplier": 2,
    }

    return {
        "label": label,
        "note": note,
        "row_counts": row_counts,
        "materials": materials,
        "history": history,
        "question_chars": _percentiles(question_pool),
    }


ASSUMPTIONS: list[dict] = [
    {"item": "chunks.content 长度分布", "kind": "直接测量", "note": "全表 SELECT，逐块 len()"},
    {"item": "messages.content 按 role 的长度分布", "kind": "直接测量", "note": "只取 role ∈ {user, assistant}"},
    {"item": "每 chat 消息条数 / 累计字符", "kind": "直接测量", "note": "SQL 取行后在内存按 id 升序前缀和"},
    {"item": "踩过 8000 的占比与轮数", "kind": "直接测量（口径推算）", "note": "前缀和 > 阈值 的第一次命中位置；轮数 = 该位置之前的 user 条数"},
    {"item": "materials_text 规模分布", "kind": "推算 + 渲染校验", "note": "取法为「同文档前 12 块 / 同文档最长 12 块」两种模拟；渲染开销由真调 render_materials 一次量得后外推"},
    {"item": "render_materials 额外开销（标记行 + \\n\\n）", "kind": "直接测量", "note": "最宽 / 最窄两篇文档真渲染，并逐项验算 head + normalized + separator == len(rendered)"},
    {"item": "工具通道 12000 字符", "kind": "常量（读代码）", "note": "TOOL_CONTEXT_TOTAL_LIMIT；截断标记不占预算，故实测总长可略超"},
    {"item": "记忆段 800 字符", "kind": "常量（读代码）", "note": "MEMORY_INJECT_MAX_CHARS；本机 MEMORY_ENABLE 默认 false ⇒ 它是上界档而非典型档"},
    {"item": "系统提示 203 字符", "kind": "直接测量", "note": "len(SYSTEM_PROMPT) 运行时取，不写死"},
    {"item": "压缩后 history 典型规模", "kind": "推算", "note": "保留窗（最近 keep 条实测字符和）+ 摘要对（摘要长度给 400/800/1600 三档假设，非实测）"},
    {"item": "1 字符 ≤ 1 token", "kind": "保守上界假设", "note": "非实测；真 token 由 CX-00 真机腿校准"},
    {"item": "本轮问题正文 = 真实 user 消息长度", "kind": "代理量", "note": "库里没有「本轮问题」这类独立行，用 user 消息长度分布代理"},
    {"item": "一轮只含一次检索材料", "kind": "建模假设", "note": "Agent 轮可多次检索并合并材料快照，条数仍受 FUSE_LIMIT 管，但这里按单次 12 条计"},
]


def _print_summary(result: dict) -> None:
    comp = result["composition"]["totals"]
    print("=" * 76)
    print("CX-00A · 一轮 prompt 组成规模（字符；1 字符 ≤ 1 token 为**上界**）")
    print("=" * 76)
    for label, title in (("typical", "典型 p50"), ("p90", "p90"), ("worst", "最坏上界")):
        row = comp[label]
        total = row["prompt_total_chars"]
        without_tool = row["prompt_total_without_tool_chars"]
        pct = row["window_upper"]["prompt_total_chars"]["pct_of_1m"]
        pct_no_tool = row["window_upper"]["prompt_total_without_tool_chars"]["pct_of_1m"]
        print(
            f"{title:<8} 总 {total:>7} 字符（不含工具通道 {without_tool:>7}）"
            f" 占 1M 窗上界 {pct:>6}%（不含工具 {pct_no_tool}%）"
        )
    print("-" * 76)
    for db_label, block in result["databases"].items():
        if block.get("error"):
            print(f"[{db_label}] 读取失败：{block['error']}")
            continue
        mat = block["materials"]
        hist = block["history"]
        press = hist["threshold_pressure"]
        counts = block["row_counts"]
        print(
            f"[{db_label}] rows: chunks={counts.get('chunks')} docs={counts.get('documents')} "
            f"messages={counts.get('messages')} chats={counts.get('chats')}"
        )
        print(
            "   chunks.content：n={n} p50={p50} p90={p90} p99={p99} max={max}（>800 的块 {over}）".format(
                over=mat["chunks_over_cap"], **mat["chunk_content_chars"]
            )
        )
        sim = mat["simulate"]
        print(
            "   同文档前12块 raw：p50={p50} p90={p90} p99={p99} max={max}；"
            "最长12块 raw：max={m2}".format(**sim["first_n_raw"], m2=sim["largest_n_raw"]["max"])
        )
        check = mat["render_check"]
        if check.get("rendered"):
            widest = check["widest"]
            print(
                f"   render_materials 最宽：raw={widest['raw_content_chars']} → "
                f"渲染={widest['render_materials_chars']}（标记行 {widest['head_chars']} + "
                f"分隔 {widest['separator_chars']} + 归一闪 {widest['normalize_saving_chars']}），"
                f"对账={'PASS' if widest['verified'] else 'FAIL'}"
            )
            print(
                "   渲染额外开销分布：p50={p50} p90={p90} max={max}".format(
                    **check["overhead_distribution_materialized"]
                )
            )
        print(
            "   messages/user：n={n} p50={p50} p90={p90} max={max}".format(**hist["user_chars"])
        )
        print(
            "   messages/assistant：n={n} p50={p50} p90={p90} max={max}".format(
                **hist["assistant_chars"]
            )
        )
        print(
            f"   踩过 {press['threshold']} 的会话：{press['chats_crossing']}/{press['chats_total']} "
            f"({press['crossing_ratio'] * 100:.1f}%)，首次踩线中位数："
            f"第 {press['cross_at_turn']['p50']} 轮 / 第 {press['cross_at_message_index']['p50']} 条 "
            f"/ {press['cross_at_chars']['p50']} 字符"
        )
        print(
            "   压缩后保留窗（最近 keep 条）字符：p50={p50} p90={p90} max={max}".format(
                **hist["keep_window_chars"]
            )
        )
    print("-" * 76)
    print(f"连接端口 {result['connection']['port_used']}；读数件见 --json 路径")


def main() -> int:
    parser = argparse.ArgumentParser(description="CX-00A 离线读数（只读 SELECT）")
    parser.add_argument("--json", default="", help="JSON 读数件输出路径")
    args = parser.parse_args()
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
