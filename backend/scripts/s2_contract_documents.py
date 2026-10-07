"""条目 2.1/2.2 的 documents 域**真机**契约探针（阶段 2 证据 S2-契约documents.log 的产出脚本）。

为什么要有它：``tests/contract/test_endpoints_catalog.py`` 只证明端点**存在**（OpenAPI 层），
证明不了「真发一次 multipart 上传，响应体字段与 02 契约逐字一致、错误码语义正确」。
本脚本对**真起的服务**发真请求，逐条断言 02 契约 §1.1 documents 域与 §4 异常语义。

用法（先起服务，见 S2-契约documents.log 里的命令行）::

    cd backend
    BASE_URL=http://127.0.0.1:8011 ./.venv/Scripts/python.exe scripts/s2_contract_documents.py

设计注：
- 用 ``httpx.Client(trust_env=False)``：本机有系统代理（WinINET），httpx 默认会读它，
  打 127.0.0.1 会被代理拦成 502（阶段 1 契约测试为此踩过坑）。探针必须显式绕开代理。
- 不依赖 pytest：这是一个「给人看的证据脚本」，输出要能直接贴进验收日志。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8011")

# 02 契约 §7.4：DocumentOut 的 12 个字段，一字不多不少
DOCUMENT_OUT_FIELDS = {
    "id",
    "filename",
    "file_type",
    "status",
    "chunk_count",
    "vectorized",
    "vector_error",
    "category",
    "tags",
    "created_at",
    "message",
    "kb_id",
}
# 02 契约 §7.4：FolderScanOut 的 4 个字段
FOLDER_SCAN_FIELDS = {"path", "files", "skipped", "truncated"}

MD_BODY = f"""---
category: 契约探针
tags: [探针, 阶段2]
---

# 契约探针文档

## 第一节

这是条目 2.1 / 2.2 真机契约探针写入的正文，用于验证上传链路与切块元数据。
运行标识：{__import__('time').time_ns()}

### 小节

- 要点一
- 要点二

## 第二节

第二段正文，用来让切块数量大于 1，从而验证 ordinal / offset 是否真的落库。
"""

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, label))
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {label}"
    if detail:
        line += f" —— {detail}"
    print(line, flush=True)


def main() -> int:
    with httpx.Client(base_url=BASE_URL, timeout=60.0, trust_env=False) as client:
        # --- 0) 服务活着 ---
        health = client.get("/api/health")
        check(health.status_code == 200, "GET /api/health → 200", f"status={health.json().get('status')}")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # --- 1) 上传真实 MD ---
            md_path = tmp_path / "契约探针.md"
            md_path.write_text(MD_BODY, encoding="utf-8")
            resp = client.post(
                "/api/documents/upload",
                files={"file": (md_path.name, md_path.read_bytes(), "text/markdown")},
            )
            check(resp.status_code == 200, "POST /api/documents/upload → 200", f"body={resp.text[:200]}")
            document = resp.json() if resp.status_code == 200 else {}
            check(
                set(document) == DOCUMENT_OUT_FIELDS,
                "DocumentOut 恰好 12 字段（02 §7.4）",
                f"多={sorted(set(document) - DOCUMENT_OUT_FIELDS)} 少={sorted(DOCUMENT_OUT_FIELDS - set(document))}",
            )
            check(document.get("status") == "ready", "上传后 status=ready", f"status={document.get('status')}")
            check(
                (document.get("chunk_count") or 0) > 0,
                "上传后 chunk_count>0",
                f"chunk_count={document.get('chunk_count')}",
            )
            check(
                document.get("vectorized") in (True, False),
                "vectorized 字段存在且为布尔（2.3 起真 embedding 可为 True）",
                f"vectorized={document.get('vectorized')} vector_error={document.get('vector_error')!r}",
            )
            check(
                document.get("category") == "契约探针",
                "frontmatter 的 category 生效",
                f"category={document.get('category')!r}",
            )
            document_id = document.get("id")

            # --- 2) 列表端点（裸数组 + 字段集） ---
            listing = client.get("/api/documents")
            check(listing.status_code == 200, "GET /api/documents → 200")
            items = listing.json() if listing.status_code == 200 else []
            check(isinstance(items, list), "列表是裸数组（02 §1.1 老仓库口径）", f"type={type(items).__name__}")
            if items:
                bad = [i.get("id") for i in items if set(i) != DOCUMENT_OUT_FIELDS]
                check(not bad, "列表中每一项都是 12 字段", f"异常项 id={bad[:5]}")
            check(
                any(i.get("id") == document_id for i in items),
                "列表里能查到刚上传的文档",
                f"document_id={document_id}",
            )

            # --- 3) 重复内容 → 409 对象形状 ---
            dup = client.post(
                "/api/documents/upload",
                files={"file": ("契约探针副本.md", md_path.read_bytes(), "text/markdown")},
            )
            check(dup.status_code == 409, "重复内容 → 409", f"status={dup.status_code} body={dup.text[:200]}")
            detail = dup.json().get("detail") if dup.status_code == 409 else {}
            check(
                isinstance(detail, dict)
                and {"code", "message", "existing_id", "filename"} <= set(detail)
                and detail.get("code") == "duplicate_content",
                "409 detail 是 {code,message,existing_id,filename} 对象",
                f"detail={json.dumps(detail, ensure_ascii=False)[:200]}",
            )

            # --- 4) 不支持的后缀 → 400 中文 ---
            bad = client.post(
                "/api/documents/upload",
                files={"file": ("x.exe", b"whatever", "application/octet-stream")},
            )
            check(bad.status_code == 400, "不支持后缀 → 400", f"status={bad.status_code}")
            check(
                isinstance(bad.json().get("detail"), str) and "暂不支持" in bad.json()["detail"],
                "400 detail 是中文可读串",
                f"detail={bad.json().get('detail')!r}",
            )

            # --- 5) 文件夹扫描 ---
            folder = tmp_path / "语料"
            folder.mkdir()
            (folder / "a.md").write_text("# a\n正文", encoding="utf-8")
            (folder / "b.txt").write_text("正文", encoding="utf-8")
            (folder / "c.exe").write_bytes(b"x")
            (folder / ".hidden.md").write_text("x", encoding="utf-8")
            scan = client.post("/api/documents/import-folder/scan", json={"path": str(folder), "recursive": True})
            check(scan.status_code == 200, "POST import-folder/scan → 200", f"status={scan.status_code}")
            payload = scan.json() if scan.status_code == 200 else {}
            check(
                set(payload) == FOLDER_SCAN_FIELDS,
                "FolderScanOut 恰好 4 字段",
                f"实际={sorted(payload)}",
            )
            names = {item["name"] for item in payload.get("files", [])}
            skipped = {item["name"] for item in payload.get("skipped", [])}
            check(
                names == {"a.md", "b.txt"} and "c.exe" in skipped and ".hidden.md" in skipped,
                "扫描分类正确（支持/不支持/隐藏）",
                f"files={sorted(names)} skipped={sorted(skipped)}",
            )
            missing = client.post(
                "/api/documents/import-folder/scan", json={"path": str(tmp_path / "不存在"), "recursive": True}
            )
            check(missing.status_code == 400, "扫描不存在目录 → 400", f"status={missing.status_code}")

            # --- 6) 单文件导入（category 回退成文件夹名） ---
            single = folder / "c.md"
            single.write_text(
                f"# c\n条目 2.1 的单文件导入探针正文。\n运行标识：{__import__('time').time_ns()}\n",
                encoding="utf-8",
            )
            imported = client.post("/api/documents/import-file", json={"path": str(single)})
            check(imported.status_code == 200, "POST import-file → 200", f"status={imported.status_code} body={imported.text[:200]}")
            imported_body = imported.json() if imported.status_code == 200 else {}
            check(
                imported_body.get("category") == "语料",
                "frontmatter 未写 category 时回退成所在文件夹名",
                f"category={imported_body.get('category')!r}",
            )
            not_found = client.post("/api/documents/import-file", json={"path": str(tmp_path / "没有这个.md")})
            check(not_found.status_code == 404, "import-file 路径不存在 → 404", f"status={not_found.status_code}")

    passed = sum(1 for ok, _ in results if ok)
    failed = [label for ok, label in results if not ok]
    print(f"\n=== 探针结果：{passed}/{len(results)} 通过 ===", flush=True)
    if failed:
        print("失败项：", flush=True)
        for label in failed:
            print(f"  - {label}", flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
