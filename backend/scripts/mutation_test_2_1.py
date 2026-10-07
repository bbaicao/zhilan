"""条目 2.1 的**变异测试**：故意破坏 ingest.py 的关键实现，验证新回归用例真能抓住。

新增的回归用例必须
经过「变异 → 断言必须失败 → 还原」验证有效，否则它只是装饰。

本脚本每一组只跑**目标用例**（快），逐组打印「期望失败 / 实际结果」，
最后把全部变异还原并复跑全套确认干净。

用法::

    cd backend
    PYTHONPATH=. ./.venv/Scripts/python.exe scripts/mutation_test_2_1.py

（Windows 上用 Git Bash 时，若 `python.exe` 找不到系统依赖，按本机 Git 安装位置把
`<Git>/usr/bin` 加进 `PATH` 即可——那属本机环境，不写进仓库。）
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# 证据捕获口径（`回归修复`，2026-09-18 补的**代码侧**修法）：中文控制台默认 GBK 码页，
# 本脚本会 print 含 ✅/❌ 的中文；重定向 stdout 时按 GBK 编解码 ⇒ 中断脚本 + 日志乱码。
# 故在 import 之后立刻把两个流钉成 UTF-8，**不再依赖调用方记得设 PYTHONIOENCODING**。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 —— 非 TextIOWrapper（例如被 pytest 捕获）时没有这个方法
        pass

BACKEND = Path(__file__).resolve().parents[1]
TARGET = BACKEND / "app" / "pipeline" / "ingest.py"
PYTHON = BACKEND / ".venv" / "Scripts" / "python.exe"

# (标签, 原文片段, 变异后片段, 目标用例, 为什么这组值得测)
CASES: list[tuple[str, str, str, str, str]] = [
    (
        "M1 失败分支去掉 rollback",
        "    except Exception as exc:\n        await session.rollback()\n        failed = await session.get(Document, doc_id)",
        "    except Exception as exc:\n        failed = await session.get(Document, doc_id)",
        "tests/pipeline/test_ingest.py::test_flush_error_marks_failed_not_stuck_processing",
        "flush 阶段的 DB 错误会让 session 进 pending-rollback，不先 rollback 就 commit，"
        "「置 failed」自己会抛 PendingRollbackError，行永久卡在 processing",
    ),
    (
        "M2 去掉按 file_path 认领失败行",
        "    failed = await _find_failed_by_path(session, stored)\n    if failed is not None:",
        "    failed = None\n    if failed is not None:",
        "tests/pipeline/test_ingest.py::test_bad_file_reupload_matches_failed_row_by_path",
        "解析失败行没有 content_hash，只能按 file_path 认领；去掉后坏文件每重传一次多一行",
    ),
    (
        "M3 写新块前不清旧块",
        "        await session.execute(delete(Chunk).where(Chunk.document_id == doc_id))\n\n        chunks = await _persist_chunks(session, doc, prepared)",
        "        chunks = await _persist_chunks(session, doc, prepared)",
        "tests/pipeline/test_ingest.py::test_retry_document_clears_stale_chunks",
        "就地重试复用同 document_id，残留块与新块撞 uk_chunks_document_ordinal",
    ),
    (
        "M4 frontmatter 对所有格式解析",
        '    if suffix in ("md", "markdown"):\n        fm = parse_frontmatter(text)\n    else:\n        fm = Frontmatter(body=text)',
        "    fm = parse_frontmatter(text)",
        "tests/pipeline/test_ingest.py::test_frontmatter_only_stripped_for_markdown",
        "非 md 正文里以 --- 开头的段落会被当 YAML 剥掉（真丢内容），偏离老仓库基线",
    ),
    (
        "M5 overwrite 不再清理既有行",
        "        if overwrite:\n            # 覆盖重建：清掉既有行后继续用当前行入库（新 id，老仓库 overwrite 语义）\n            await purge_document(session, existing.id)",
        "        if overwrite:\n            pass",
        "tests/pipeline/test_ingest.py::test_overwrite_rebuilds_with_new_id",
        "覆盖重建要清掉旧行，否则唯一约束 uk_documents_content_hash 会把它顶回去",
    ),
]


def run(test: str) -> tuple[bool, str]:
    """跑一个用例，返回 (是否通过, 摘要末行)。"""
    proc = subprocess.run(
        [str(PYTHON), "-m", "pytest", *test.split(), "-q", "-p", "no:cacheprovider", "--no-header"],
        cwd=BACKEND,
        env={**__import__("os").environ, "PYTHONPATH": str(BACKEND)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    tail = [line for line in (proc.stdout or "").strip().splitlines() if line.strip()]
    return proc.returncode == 0, (tail[-1] if tail else "(无输出)")


def main() -> int:
    original = TARGET.read_text(encoding="utf-8")
    verdicts: list[tuple[str, bool, str]] = []
    try:
        for label, old, new, test, why in CASES:
            if old not in original:
                verdicts.append((label, False, "变异锚点未找到（源码已变？）"))
                print(f"[SKIP] {label}：变异锚点未找到", flush=True)
                continue
            TARGET.write_text(original.replace(old, new, 1), encoding="utf-8")
            try:
                passed, summary = run(test)
            finally:
                TARGET.write_text(original, encoding="utf-8")
            killed = not passed
            verdicts.append((label, killed, summary))
            print(f"[{'KILLED' if killed else 'SURVIVED'}] {label}", flush=True)
            print(f"    为什么该被杀：{why}", flush=True)
            print(f"    目标用例：{test}", flush=True)
            print(f"    实际：{summary}", flush=True)

        print("\n=== 还原后复跑全套（确认源码干净）===", flush=True)
        ok, summary = run("tests/pipeline/test_ingest.py tests/pipeline/test_storage.py tests/api/test_documents_contract.py")
        print(f"{'PASS' if ok else 'FAIL'}: {summary}", flush=True)
    finally:
        TARGET.write_text(original, encoding="utf-8")

    killed = sum(1 for _, k, _ in verdicts if k)
    print(f"\n=== 变异测试结果：{killed}/{len(verdicts)} 组被杀 ===", flush=True)
    for label, k, summary in verdicts:
        print(f"  {'✅' if k else '❌'} {label} —— {summary}", flush=True)
    return 0 if killed == len(verdicts) else 1


if __name__ == "__main__":
    sys.exit(main())
