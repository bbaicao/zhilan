"""A13c · dist 新鲜度门禁的判据载体（工具升级-3 · 回归检查）。

判据：**改了 `frontend/src` 而未重建 dist ⇒ 必须能被发现**。
判据载体 = `backend/scripts/check_frontend_dist_freshness.py`（退 0 = 绿 / 退 1 = 红）；
本文件把它的**四条行为**钉成用例，使其成为可复跑门禁（而不是一次性读数）。

⚠️ **不得用 mtime**（`D-回归检查` / `K-回归检查`）：本仓有实测事故 —— `api.ts` 的 mtime 被 checkout 刷成 09-20，
**而内容 == HEAD**。故判据一律走**内容树哈希**。
⚠️ **必红自证 = 手造陈旧 manifest**（`D-回归检查`），**不是**「按当前状态跑一次」——
后者在自然顺序下**恒绿**（生成 manifest 时 src 与 dist 都已最新）。
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]      # backend/tests/pipeline/x.py -> 仓库根
SCRIPT = REPO_ROOT / "backend" / "scripts" / "check_frontend_dist_freshness.py"
MANIFEST = REPO_ROOT / "frontend" / ".build-manifest"


def _load():
    spec = importlib.util.spec_from_file_location("_a13c_mod", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # ⚠️ 必须先登记进 sys.modules 再 exec（6.2c 实测坑）
    sys.modules["_a13c_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(REPO_ROOT),
    )


def test_manifest_is_not_inside_dist():
    """落位裁定（`D-回归检查`）：manifest **不得**放 `frontend/dist/` 内。

    `vite.config.ts` 的 `emptyOutDir: true` 会**整个清空** dist ⇒ 放里面 = 判据「跑不起来」而不是「红」。
    """
    assert MANIFEST == REPO_ROOT / "frontend" / ".build-manifest"
    assert "dist" not in MANIFEST.parts


def test_src_tree_hash_covers_every_file():
    """覆盖面自证（独立复核 F-02）：哈希输入必须覆盖**全部** src 文件（递归 + 全扩展名）。

    没有这条，「手造陈旧 manifest ⇒ 必红」只证明**比对函数**会红；
    若漏 walk 某个子目录或扩展名，**真实漂移会恒绿**。
    """
    mod = _load()
    files = mod.iter_src_files()
    assert files, "frontend/src 下必须有文件"
    names = [p.relative_to(mod.FRONTEND).as_posix() for p in files]
    # 递归：必须有子目录文件
    assert any("/" in n for n in names), names[:5]
    # 全扩展名：既要有 .tsx 也要有 .ts（本仓现读两者都有）
    assert any(n.endswith(".tsx") for n in names), names[:5]
    assert any(n.endswith(".ts") for n in names), names[:5]
    # 与 manifest 记录的条数一致（防「哈希算 42 个、清单记 41 个」）
    recorded = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert recorded["src_file_count"] == len(files)
    # 🔴 **终局审计 A-9（本版就地修）**：上面几条 `any(...)` 是**存在性**断言 ——
    # 「只 walk 顶层」若顶层恰好有 .tsx 与 .ts 就**不会红**。⇒ 补一条**独立复算**的条数断言：
    # 用 pathlib 现数一遍 `frontend/src` 下全部普通文件，必须与 `iter_src_files()` 逐数相等。
    # ⚠️ 独立复算必须**限定同一个根**（frontend/src）—— iter_src_files() 走的是 SRC，
    # 而 rglob 若从 FRONTEND 起会多收顶层文件（实测 42 vs 32）⇒ 那是**复算范围写错**，不是 walk 漏项。
    independent = [p for p in (mod.FRONTEND / "src").rglob("*") if p.is_file()]
    assert len(files) == len(independent), (len(files), len(independent))
    assert len(files) >= 40, "src 文件数只有 " + str(len(files)) + "，疑似 walk 退化"
    # 递归面再独立核一次：子目录文件数必须 > 0，且与独立复算一致。
    # ⚠️ 两边的**基准必须对齐**：`names` 相对 `FRONTEND`（故形如 `src/components/x.tsx`），
    # 而 `independent` 相对 `frontend/src`（故子目录文件的父目录是 `.../src/components`）。
    # ⇒ 「递归收进来」在两边分别是「路径里有第二个 `/`」与「父目录 ≠ frontend/src」。
    nested = [p for p in independent if p.parent != (mod.FRONTEND / "src")]
    assert nested, "独立复算也找不到子目录文件 ⇒ rglob 语义异常"
    nested_in_names = [n for n in names if n.count("/") >= 2]
    assert len(nested_in_names) == len(nested), (len(nested_in_names), len(nested))


def test_hash_is_deterministic():
    """同一棵树两次哈希必须逐字相同（确定性 ⇒ 判据可复跑）。"""
    mod = _load()
    assert mod.src_tree_hash() == mod.src_tree_hash()


def test_stale_manifest_is_red(tmp_path):
    """🔴 **必红自证**（`D-回归检查`）：手造陈旧 manifest ⇒ 判据**必须红**（退出码 1）。

    这就是判别力所在：若把 `src_tree_hash` 写成一个常量（或漏覆盖），本用例**必红**。

    🔴 **终局审计 A-3/A-5（本版就地修）**：首版直接改写**入库的** `frontend/.build-manifest`
    ⇒ ① 与主会话/其它用例并发时会**伪造红**（同文件的 `test_true_manifest_is_green` 会读到坏哈希）；
    ② 中途被 SIGTERM 时 `finally` 可能不执行 ⇒ **被污染的 manifest 留在工作区并进提交**。
    ⇒ 现在一律经 `--manifest <tmp_path>` 指向**临时文件**，**零触碰仓库**。
    """
    target = tmp_path / "stale-manifest.json"
    #: 216：先**快照**仓库那份的逐字文本 —— 「零副作用」断言必须拿它比对，
    #: ⛔ 不能拿下面被改坏的 payload（原先那样比较恒假，被 `or True` 盖住 ⇒ 什么都没验）。
    before = MANIFEST.read_text(encoding="utf-8")
    payload = json.loads(before)
    payload["src_tree_hash"] = "0" * 64
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = _run("--manifest", str(target))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL" in result.stdout
    # 仓库那份必须**逐字未变**（本用例的副作用面为零）
    assert MANIFEST.read_text(encoding="utf-8") == before, "本用例不得改写仓库 manifest（副作用面必须为零）"
    assert MANIFEST.read_text(encoding="utf-8") == json.dumps(
        {**json.loads(MANIFEST.read_text(encoding="utf-8"))}, ensure_ascii=False, indent=2
    ) + "\n"


def test_true_manifest_is_green():
    """还原真值 ⇒ 必须转绿（退 0）。**这条 + 上一条 = 判别力对**。"""
    result = _run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


def test_missing_manifest_is_red_not_green(tmp_path):
    """manifest 缺失 ⇒ **红**（不是静默跳过）。

    `D-回归检查` 的「方案 B（不入库）」有个坑：文件不存在时若判「跳过」就等于**永久绿**。
    本轨取方案 A（入库）+ 缺失即红，这条钉住该口径。
    同 A-3/A-5：走 `--manifest <tmp_path>`，**不删仓库那份**。
    """
    absent = tmp_path / "does-not-exist.json"
    result = _run("--manifest", str(absent))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "不存在" in result.stdout
    assert MANIFEST.is_file(), "仓库那份不得被本用例删除"


# ─────────────────────────────────────────────────────────────────────────────
# 条目 214 · **dist 面**（2026-09-29 追加）
# 原判据只有 src 面 ⇒ 「改了 src、只 --write、不重建 dist」能把 manifest 刷成新 src 哈希
# ⇒ **判据自盖章变绿**。以下用例把新判据（src + dist 双面 + --write 拒章）钉成可复跑门禁。
# ⚠️ 一律走 --manifest <tmp_path>：仓库那份 frontend/.build-manifest **零触碰**。
# ─────────────────────────────────────────────────────────────────────────────


def _manifest_snapshot() -> str:
    return MANIFEST.read_text(encoding="utf-8")


def test_dist_tree_hash_is_deterministic():
    """dist 面哈希确定性：同一棵树两次算出的值逐字相同（64 位小写 hex）。

    排序口径 = 相对路径 `casefold()` 排序；raw 排序在大小写混排时顺序不定 ⇒ **跨平台假红**。
    """
    mod = _load()
    first, second = mod.dist_tree_hash(), mod.dist_tree_hash()
    assert first == second
    assert len(first) == 64 and all(c in "0123456789abcdef" for c in first), first


def test_dist_hash_covers_every_file():
    """dist 面覆盖面自证（与 src 面同款 · F-02）：递归 + 全扩展名 + 与 manifest 计数一致。"""
    mod = _load()
    files = mod.iter_dist_files()
    assert files, "frontend/dist 下必须有文件"
    names = [p.relative_to(mod.DIST).as_posix() for p in files]
    assert any("/" in n for n in names), names[:5]      # 递归：必须有 assets/ 子目录文件
    assert len(set(names)) == len(names), names          # 去重后条数不变
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert payload["dist_file_count"] == len(files), (payload["dist_file_count"], len(files))
    assert payload["dist_tree_hash"] == mod.dist_tree_hash()


def test_stale_dist_manifest_is_red(tmp_path):
    """src 哈希**正确**、dist 哈希**陈旧** ⇒ 必红（退 1）。这条钉的就是 214 修的自盖章路径。"""
    mod = _load()
    before = _manifest_snapshot()
    target = tmp_path / "stale-dist-manifest.json"
    target.write_text(json.dumps({
        "src_tree_hash": mod.src_tree_hash(),
        "src_file_count": len(mod.iter_src_files()),
        "dist_tree_hash": "0" * 64,
        "dist_file_count": len(mod.iter_dist_files()),
    }, ensure_ascii=False), encoding="utf-8")
    result = _run("--manifest", str(target))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL" in result.stdout
    assert "dist" in result.stdout, result.stdout
    # 副作用面为零：仓库那份逐字未变
    assert _manifest_snapshot() == before


def test_manifest_without_dist_field_is_red(tmp_path):
    """旧版 manifest（只有 src 字段）⇒ **红**（fail-closed，⛔ 不静默降级成「只判 src」）。"""
    mod = _load()
    target = tmp_path / "old-manifest.json"
    target.write_text(json.dumps({
        "src_tree_hash": mod.src_tree_hash(),
        "src_file_count": len(mod.iter_src_files()),
    }, ensure_ascii=False), encoding="utf-8")
    result = _run("--manifest", str(target))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "dist_tree_hash" in result.stdout, result.stdout
    assert "A13c 判据失败" in result.stdout, result.stdout


def test_write_refuses_to_stamp_when_dist_unchanged(tmp_path):
    """**禁自盖章**：src 已变、dist 字节未变 ⇒ `--write` 拒写（退 1）且**文件字节不变**。"""
    mod = _load()
    target = tmp_path / "refuse.json"
    target.write_text(json.dumps({
        "src_tree_hash": "0" * 64,                          # 假装「上次构建时的 src」≠ 现场
        "src_file_count": len(mod.iter_src_files()),
        "dist_tree_hash": mod.dist_tree_hash(),             # dist 与现场一致 ⇒ 没重建
        "dist_file_count": len(mod.iter_dist_files()),
    }, ensure_ascii=False), encoding="utf-8")
    before = target.read_bytes()
    result = _run("--write", "--manifest", str(target))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "拒绝盖章" in result.stdout, result.stdout
    assert target.read_bytes() == before, "拒章时不得写盘"


def test_write_accepts_when_dist_changed(tmp_path):
    """真跑过构建（dist 字节变了）⇒ `--write` 接受，两哈希都落盘（退 0）。"""
    mod = _load()
    target = tmp_path / "accept.json"
    target.write_text(json.dumps({
        "src_tree_hash": "0" * 64, "src_file_count": 1,
        "dist_tree_hash": "f" * 64, "dist_file_count": 1,
    }, ensure_ascii=False), encoding="utf-8")
    result = _run("--write", "--manifest", str(target))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["src_tree_hash"] == mod.src_tree_hash()
    assert payload["dist_tree_hash"] == mod.dist_tree_hash()
    assert payload["src_file_count"] == len(mod.iter_src_files())
    assert payload["dist_file_count"] == len(mod.iter_dist_files())


def test_allow_same_dist_is_explicit_only(tmp_path):
    """逃生口 `--allow-same-dist`：**默认拒**（上一条）+ **显式给出才放行**（本条）= 判别力对。"""
    mod = _load()
    target = tmp_path / "escape.json"
    target.write_text(json.dumps({
        "src_tree_hash": "0" * 64,
        "src_file_count": len(mod.iter_src_files()),
        "dist_tree_hash": mod.dist_tree_hash(),
        "dist_file_count": len(mod.iter_dist_files()),
    }, ensure_ascii=False), encoding="utf-8")
    result = _run("--write", "--allow-same-dist", "--manifest", str(target))
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(target.read_text(encoding="utf-8"))["src_tree_hash"] == mod.src_tree_hash()

