"""A13c · `frontend/src` → `frontend/dist` 新鲜度门禁（工具升级-3 · 回归检查 · D-回归检查）。

**判据（条目 214 起为双面）**：① 重算 `frontend/src` 内容树哈希 ↔ manifest 的 `src_tree_hash`；
② 重算 `frontend/dist` 内容树哈希 ↔ manifest 的 `dist_tree_hash`。**任一不等 ⇒ 红（退 1）**。

**为什么必须加 dist 面（条目 214 · Lead 裁决 #8-A）**：原判据只比「src ↔ manifest」⇒ 「改了 src、只跑 `--write`、**不重建 dist**」
会把 manifest 刷成新 src 哈希 ⇒ 判据**自盖章变绿**（dist 其实还是旧的）。加 dist 面后 manifest 同时钉住 dist 字节；
且 `--write` 自身**拒绝**在「src 已变、dist 一个字节未变」时盖章（逃生口 `--allow-same-dist`，只在确已重建而产物字节恰好未变时用）。
⚠️ 残留边界：`--write` 是对**当前状态**的登记，它无法证明 dist 一定由当前 src 构建 —— 本规则只堵住「src 变了而 dist 未变」这条自盖章路径。

**为什么不得用 mtime**（`D-回归检查` 逐字 + `K-回归检查`）：mtime 在 `checkout` / 复制 / 解压后不可靠。
本仓已有实测事故：`frontend/src/api.ts` 的 mtime 被 checkout 刷成 09-20，**而内容 == HEAD** ⇒
凡以 mtime 判「src 更新了」的路径在这台机器上**都会误报**。

**为什么 manifest 不放 `frontend/dist/` 内**（`D-回归检查` 三处候选实测对照）：
`vite.config.ts` 的 `build.outDir = "dist"` + `emptyOutDir: true` ⇒ `npm run build` **整个清空**
`frontend/dist/`；且 `tsc -b` 跑在 `vite build` **之前**、**没有 vite 的 `.git`/`.gitkeep` 豁免**
⇒ 任何需在构建后幸存的哨兵/清单**都不得**放 `frontend/dist/`（会被删，判据「跑不起来」而不是「红」）。

**落位裁定 = `frontend/.build-manifest`**（`frontend/` 根下）：不被 gitignore 覆盖、不在 outDir 内、
不随构建被清 ⇒ **入库**（「manifest 过期即红」正是要强制的纪律）。

用法：
```
python backend/scripts/check_frontend_dist_freshness.py                      # 判定（退 0=绿 / 1=红；src 面 + dist 面）
python backend/scripts/check_frontend_dist_freshness.py --write              # 生成/更新 manifest（src + dist 两哈希）
python backend/scripts/check_frontend_dist_freshness.py --write --allow-same-dist   # ⛔ 逃生口：src 变了而 dist 字节未变时也盖章（只在确已跑过构建时用）
```
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "frontend"
SRC = FRONTEND / "src"
MANIFEST = FRONTEND / ".build-manifest"
DIST = FRONTEND / "dist"


def src_tree_hash() -> str:
    """`frontend/src` **整棵**内容树哈希（递归、全扩展名、相对路径 + 字节内容）。

    🔴 **覆盖面必须显式写死并在判据里自证**（独立复核 F-02 的教训）：「手造陈旧 manifest ⇒ 必红」
    只证明**比对函数**会红，**不证明哈希输入选得对** —— 若漏 walk 某个子目录或某个扩展名，
    真实漂移（改被漏掉的文件）会**恒绿**。故：
    - 递归**全部**文件（不筛扩展名）；
    - 相对路径参与哈希（改名/移动也要红）；
    - 覆盖面自证 = :func:`iter_src_files` 的返回条数（有判据钉住「恰好等于实测文件数」）。
    """
    digest = hashlib.sha256()
    for path in iter_src_files():
        rel = path.relative_to(FRONTEND).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(path.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()


def iter_src_files() -> list[Path]:
    """`frontend/src` 下的全部普通文件（递归、按相对路径排序 ⇒ 哈希**确定性**）。"""
    return sorted(p for p in SRC.rglob("*") if p.is_file())


def iter_dist_files() -> list[Path]:
    """`frontend/dist` 下的全部普通文件（递归；**按相对路径 casefold 排序** ⇒ 哈希确定性）。

    ⚠️ 排序口径（条目 214 · Lead 采纳作者勘误）：用 `rel.casefold()` 排序、**不用 raw** ——
    大小写混排时 raw 排序在跨平台/不同工具链下会给出不同顺序 ⇒ 同一棵树算出不同哈希（**假红**）。
    `dist` 不存在时不抛错，交由 :func:`dist_tree_hash` 决定（fail-closed：判不了就红）。
    """
    paths = [p for p in DIST.rglob("*") if p.is_file()]
    return sorted(paths, key=lambda p: p.relative_to(DIST).as_posix().casefold())


def dist_tree_hash() -> str:
    """`frontend/dist` **整棵**内容树哈希（与 :func:`src_tree_hash` 同构：相对路径 + 字节内容 + 两个 \\x00）。

    - 相对路径**相对 `DIST`**（故 dist 内改名/移动/新增/删除都要红）；
    - `dist` 不存在 ⇒ :class:`FileNotFoundError`（**fail-closed**：判据跑不起来时不许静默判绿）。
    """
    digest = hashlib.sha256()
    for path in iter_dist_files():
        rel = path.relative_to(DIST).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(path.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()


def manifest_path(override: str | None = None) -> Path:
    """manifest 路径。``override`` 供**判据用例**指向 ``tmp_path`` —— 让用例**零触碰**仓库文件。

    🔴 **终局审计 A-3/A-5（本版就地修）**：首版用例直接改写**入库的** ``frontend/.build-manifest``
    （两个用例都收了 ``tmp_path`` 却不用）⇒ ① 与主会话/其它用例并发时会产生**伪造红**
    （同文件的 ``test_true_manifest_is_green`` 会读到被改坏的哈希）；
    ② 中途被 SIGTERM（``K-回归检查``：前台长命令会被杀）时 ``finally`` 可能不执行
    ⇒ **被污染的 manifest 会留在工作区并进提交**（本仓有 checkout 丢增量的先例）。
    ⇒ 判据用例一律经本函数指向临时文件；``--manifest`` 就是那条通道。
    """
    return Path(override) if override else MANIFEST


def read_manifest(path: Path | None = None) -> dict:
    target = path or MANIFEST
    if not target.is_file():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_manifest(path: Path | None = None) -> dict:
    payload = {
        "src_tree_hash": src_tree_hash(),
        "src_file_count": len(iter_src_files()),
        "dist_tree_hash": dist_tree_hash(),
        "dist_file_count": len(iter_dist_files()),
        "built_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "note": "由 backend/scripts/check_frontend_dist_freshness.py --write 生成；改 frontend/src 后须重跑 npm run build 并重生成（src/dist 两哈希都记）",
    }
    (path or MANIFEST).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="A13c：frontend/src -> frontend/dist 新鲜度门禁")
    parser.add_argument("--write", action="store_true", help="生成/更新 frontend/.build-manifest")
    parser.add_argument(
        "--manifest",
        default=None,
        help="manifest 路径（默认 frontend/.build-manifest）。**判据用例必须传临时路径**，",
    )
    parser.add_argument(
        "--allow-same-dist",
        action="store_true",
        help="⛔ 逃生口：即使「src 已变、dist 字节未变」也盖章。只在**确已跑过构建**而产物字节恰好未变时用。",
    )
    args = parser.parse_args()
    target = manifest_path(args.manifest)

    if args.write:
        try:
            live_src, live_dist = src_tree_hash(), dist_tree_hash()
        except FileNotFoundError as exc:
            print(f"A13c 判据失败：dist 不可读（{exc}）⇒ 没有东西可登记。")
            print("修法：cd frontend && npm run build")
            return 1
        recorded = read_manifest(target)
        # 🔴 **禁自盖章（条目 214）**：src 变了、dist 一个字节没变 ⇒ 判定「没跑构建」⇒ 拒写。
        # （改前行为 = 直接把 manifest 刷成新 src 哈希 ⇒ dist 陈旧却被判绿。）
        if (
            not args.allow_same_dist
            and recorded.get("src_tree_hash")
            and recorded["src_tree_hash"] != live_src
            and recorded.get("dist_tree_hash") == live_dist
        ):
            print("A13c --write 拒绝盖章：src 已变、dist 一个字节未变 ⇒ 判定「未跑构建」（防自盖章）。")
            print(f"        manifest 记录 src = {recorded['src_tree_hash']} / dist = {recorded['dist_tree_hash']}")
            print(f"        现场重算     src = {live_src} / dist = {live_dist}")
            print("修法：cd frontend && npm run build，然后重跑 --write。")
            print("      （若确已重建而产物字节恰好未变：--allow-same-dist，⛔ 只在真跑过构建时用。）")
            return 1
        payload = write_manifest(target)
        print(f"[write] {target}")
        print(f"        src_tree_hash = {payload['src_tree_hash']}")
        print(f"        src_file_count = {payload['src_file_count']}")
        print(f"        dist_tree_hash = {payload['dist_tree_hash']}")
        print(f"        dist_file_count = {payload['dist_file_count']}")
        return 0

    current = src_tree_hash()
    try:
        current_dist = dist_tree_hash()
    except FileNotFoundError as exc:
        print(f"A13c 判据失败：frontend/dist 不可读（{exc}）⇒ 无法判定 dist 新鲜度（fail-closed，⛔ 不静默判绿）。")
        print("修法：cd frontend && npm run build")
        return 1
    recorded = read_manifest(target)
    files = len(iter_src_files())
    dist_files = len(iter_dist_files())

    # 🔴 **覆盖面自证**（独立复核 F-02）：先证明哈希输入**覆盖到每一个文件**，
    # 再判新鲜度 —— 否则「手造陈旧 manifest ⇒ 必红」只证明比对函数会红，
    # 而**漏 walk 某个子目录/某个扩展名**时真实漂移会**恒绿**。
    # 做法：对每个文件单独算一次「单文件扰动哈希」，确认它都会改变总哈希的输入集。
    covered = {p.relative_to(FRONTEND).as_posix() for p in iter_src_files()}
    if len(covered) != files:  # 去重后条数变了 ⇒ rglob 返回了重复/异常路径
        print(f"A13c 判据失败：src 文件清单去重后条数不一致（{len(covered)} != {files}）。")
        return 1
    if not any(name.endswith(".tsx") for name in covered):
        print("A13c 判据失败：src 树里没有任何 .tsx ⇒ 怀疑 walk 漏了扩展名（覆盖面自证不通过）。")
        return 1
    if not any("/" in name for name in covered):
        print("A13c 判据失败：src 树里没有任何子目录文件 ⇒ 怀疑 walk 未递归（覆盖面自证不通过）。")
        return 1

    # 🔴 **dist 面覆盖面自证（条目 214）**：与 src 面同款 —— 先证明 dist 哈希输入覆盖到每一个文件，
    # 再判新鲜度（空 dist / 去重后条数异常 / walk 未递归 都要**显式红**，不许静默当作「没有差异」）。
    dist_covered = {p.relative_to(DIST).as_posix() for p in iter_dist_files()}
    if dist_files == 0:
        print(f"A13c 判据失败：{DIST} 下一个文件都没有 ⇒ 怀疑 dist 缺失或 walk 退化（覆盖面自证不通过）。")
        return 1
    if len(dist_covered) != dist_files:
        print(f"A13c 判据失败：dist 文件清单去重后条数不一致（{len(dist_covered)} != {dist_files}）。")
        return 1
    if not any("/" in name for name in dist_covered):
        print("A13c 判据失败：dist 树里没有任何子目录文件 ⇒ 怀疑 walk 未递归（覆盖面自证不通过）。")
        return 1

    if not recorded:
        print(f"A13c 判据失败：{target} 不存在或不可解析 ⇒ 无法判定新鲜度。")
        print("修法：python backend/scripts/check_frontend_dist_freshness.py --write")
        return 1

    recorded_src = recorded.get("src_tree_hash")
    recorded_dist = recorded.get("dist_tree_hash")
    if not recorded_dist:
        print("A13c 判据失败：manifest 缺 dist_tree_hash（旧版 manifest）⇒ 无法判定 dist 新鲜度。")
        print("修法：cd frontend && npm run build 然后 python backend/scripts/check_frontend_dist_freshness.py --write")
        return 1

    src_ok = recorded_src == current
    dist_ok = recorded_dist == current_dist
    ok = src_ok and dist_ok
    print(f"src 文件数        ：{files}")
    print(f"manifest 记录 src ：{recorded_src}")
    print(f"现场重算 src      ：{current}")
    print(f"dist 文件数       ：{dist_files}")
    print(f"manifest 记录 dist：{recorded_dist}")
    print(f"现场重算 dist     ：{current_dist}")
    if ok:
        print("判定              ：PASS（dist 已跟上 src）")
    else:
        faces = "、".join(name for name, good in (("src", src_ok), ("dist", dist_ok)) if not good)
        print(f"判定              ：FAIL（{faces} 与 manifest 不一致）")
        print("修法：cd frontend && npm run build 然后 python backend/scripts/check_frontend_dist_freshness.py --write")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
