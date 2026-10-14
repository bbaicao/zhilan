r"""文档相似边全量重建（专项「导入资料自动连线」· 2026-10-08）。

**为什么需要一个脚本**：生产链路只在两个点重算相似边——构建发布后（`app/pipeline/builds.py`）
与重建索引收尾（`app/pipeline/reindex.py`）。开关打开那一刻，**已入库的历史资料没有人替它们算**。
本脚本就是这个补算入口：按库全量算一遍，走的是与生产**同一个**函数
（`app.pipeline.graphlink.recompute_kb_edges`），不另写一套算法，所以「脚本算的」与
「以后自动算的」逐条同构。

**关态拒绝运行**：`GRAPHLINK_ENABLE` 未开时本脚本不做任何事、退出码 2。
理由——它是全仓唯一可能在关态写新表的路径，而关态的承诺是「这张表不读不写」，
不留这个例外（要补算，先开开关）。

用法::

    # ① 试算（只读：算完打印会写多少条边，一行不删不插）
    python scripts/rebuild_similarity_edges.py --dsn "sqlite:///E:/tmp/graphlink.db" --dry-run

    # ② 真写（缺省 = 库中所有「有未软删文档」或「已有旧边」的库）
    python scripts/rebuild_similarity_edges.py --dsn "mysql://user:changeme@127.0.0.1:3306/zhilan"

    # ③ 只补某几个库（--kb 可重复，也可逗号分隔）
    python scripts/rebuild_similarity_edges.py --dsn "sqlite:///E:/tmp/graphlink.db" --kb 1,2 --kb 5

退出码：``0`` 成功（含试算）/ ``1`` 有库没算成（`failed`/`stale`/`oversize`）或异常 /
``2`` 关态拒绝或参数错误。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_REFUSED = 2

#: 这些状态说明「这个库这一次没有被写成」⇒ 脚本按失败退出，不让部分成功看起来像全成功。
_NOT_WRITTEN = {"failed", "stale", "oversize"}


def to_async_dsn(dsn: str) -> str:
    """把同步风格 DSN 换成异步驱动。

    为什么不写一套同步实现：维护链只有一份算法（`graphlink` 是 async 的），
    第二套同步逻辑一定会与生产漂移。所以脚本只做 DSN 翻译，其余全部复用生产函数。
    """
    text = dsn.strip()
    if "+aiosqlite" in text or "+aiomysql" in text:
        return text
    if text.startswith("sqlite://"):
        return "sqlite+aiosqlite://" + text[len("sqlite://"):]
    if text.startswith("mysql+pymysql://"):
        return "mysql+aiomysql://" + text[len("mysql+pymysql://"):]
    if text.startswith("mysql://"):
        return "mysql+aiomysql://" + text[len("mysql://"):]
    return text


def parse_kbs(values: list[str]) -> list[int]:
    """`--kb 1,2 --kb 5` ⇒ `[1, 2, 5]`；空表 ⇒ 空清单（表示「缺省全库」）。"""
    picked: set[int] = set()
    for value in values:
        for part in str(value).split(","):
            part = part.strip()
            if part:
                picked.add(int(part))
    return sorted(picked)


async def run_rebuild(args: argparse.Namespace) -> dict:
    from app.pipeline import graphlink
    from app.vector import get_vector_store

    engine = create_async_engine(to_async_dsn(args.dsn), future=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    results: list[dict] = []
    try:
        async with maker() as session:
            kb_ids = parse_kbs(args.kb) or await graphlink.known_kb_ids(session)
            # 只在显式给了向量库定位参数时才自建 store；否则用应用默认单例（读 .env）。
            store = None
            if args.qdrant_url or args.qdrant_path:
                store = get_vector_store(path=args.qdrant_path or None, url=args.qdrant_url or None)
            for kb_id in kb_ids:
                results.append(await graphlink.recompute_kb_edges(
                    session,
                    kb_id,
                    store=store,
                    dry_run=bool(args.dry_run),
                ))
    finally:
        await engine.dispose()
    return {"dry_run": bool(args.dry_run), "results": results}


def _format(payload: dict) -> str:
    lines = ["模式：%s" % ("试算（只读）" if payload["dry_run"] else "写入")]
    if not payload["results"]:
        lines.append("没有需要处理的库（库中无未软删文档、也无旧边）。")
    for item in payload["results"]:
        lines.append(
            "kb=%s status=%s 边=%s 文档=%s 缺向量分块=%s"
            % (
                item.get("kb_id"),
                item.get("status"),
                item.get("edges"),
                item.get("documents"),
                item.get("missing"),
            )
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按库全量重建文档相似边（导入资料自动连线）")
    parser.add_argument("--dsn", required=True, help="目标库 DSN（同步或异步驱动均可）")
    parser.add_argument(
        "--kb",
        action="append",
        default=[],
        metavar="ID",
        help="只处理这些知识库（可重复，也可逗号分隔）；缺省 = 自动列举",
    )
    parser.add_argument("--dry-run", action="store_true", help="只算不写（一行不删不插）")
    parser.add_argument("--qdrant-url", default="", help="向量库 URL（缺省用应用配置）")
    parser.add_argument("--qdrant-path", default="", help="向量库本地目录（缺省用应用配置）")
    parser.add_argument("--json", action="store_true", help="只打印 JSON 结果")
    args = parser.parse_args(argv)

    try:
        parse_kbs(args.kb)
    except ValueError:
        print("REFUSED：--kb 只接受整数（可逗号分隔 / 可重复）", file=sys.stderr)
        return EXIT_REFUSED

    from app.pipeline import graphlink

    if not graphlink.enabled():
        print(
            "REFUSED：GRAPHLINK_ENABLE 未开，本脚本拒绝运行。\n"
            "理由：本脚本是全仓唯一可能在关态写 document_similarity_edges 的路径，"
            "而关态承诺是「这张表不读不写」（连列举待算库都会读表）。\n"
            "要补算历史资料，先设 GRAPHLINK_ENABLE=true 再重跑。",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    try:
        payload = asyncio.run(run_rebuild(args))
    except Exception as exc:  # noqa: BLE001 —— 脚本级失败：如实退出 1，不吞异常
        print("FAILED：%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return EXIT_FAIL

    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str) if args.json else _format(payload))
    bad = [item for item in payload["results"] if item.get("status") in _NOT_WRITTEN]
    if bad:
        print(
            "有 %d 个库这次没有写成：%s（旧边原样保留，可重跑）"
            % (len(bad), ", ".join("kb=%s:%s" % (item.get("kb_id"), item.get("status")) for item in bad)),
            file=sys.stderr,
        )
    return EXIT_FAIL if bad else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
