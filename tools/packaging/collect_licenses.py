# -*- coding: utf-8 -*-
"""知澜桌面端「随包许可」收集脚本（阶段 6 · 条目 6.5 · 规格 4 / 裁决 7）。

**它解决什么问题**：项目采用 Apache-2.0，其 §4(d) 与本项目依赖的一批 Apache-2.0 组件共同要求
「分发二进制时必须随包携带许可证与声明」。安装目录里的 `licenses\\` 就是这个义务的落点。
手工攒这份目录迟早会漏（依赖一升级就漂），故把它做成机械流程：

    构建专用 venv 的 site-packages\\*\\dist-info  ──按 requirements-lock 取交集、按 spec 的
    `excludes` 做减法──▶  暂存目录（`build\\licenses-stage\\`）──▶ 由 `zhilan.iss` 打进
    `<安装目录>\\licenses\\`

**清单范围**：按 `requirements-lock.txt` 的包名与现场 dist-info 取交集，再减去
`zhilan.spec` 的显式 `excludes`。这里不读取 PyInstaller 模块图或实际成品，因此不能
证明列出的包都已随包，也不能证明实际分发组件全集已覆盖。锁定清单里的构建期包
（如 `pyinstaller` / `setuptools`）若未被显式排除，也可能出现在收集清单中。

**判据不是「文件数」**（规格 4 原文）：而是① **MinerU 的 `LICENSE.md` 全文能在安装目录里
逐字找到**（自定义 Apache 变体，**不可用通用文本替代**）、② 文件头能被打开读。
故本脚本末尾有**硬断言**：MUST_HAVE 各件与 Inno 许可必须在内，且 MinerU 那件的
sha256 与已知值一致；PASS 仅代表这些硬断言成立。其他无全文组件会登记，但不全都
阻断退出，因此 PASS 不表示许可已收全，也不是完整许可合规或实际产物全集证明。

**vendor 回退（回归修复）**：有 9 个依赖的 wheel 内不含许可全文（dist-info 与包目录都没有），
只有 METADATA 里的一句声明。这 9 件的许可材料由人工存进仓库
`tools/packaging/licenses-vendor/<包名>/LICENSE`（每份末尾附来源注记）；其中 cobble
是依据上游声明整理的 BSD 模板，其余记录为上游文本，不能把九件全称为上游原文。
master/main 来源尚未逐件绑定不可变版本，proxy_tools 的原文与 wheel 声明差异也须核对。
本脚本遇到「无全文」的包
先查 vendor 目录，命中即把全文随包（`licenses\\<包名>\\LICENSE`），METADATA 声明文件仍一并写出作佐证。
本脚本自身依旧**零出网**——来源整理是人工动作，不在构建流程里。已有输出中的
「上游全文」为旧标签，具体来源以每份注记为准；本次仅更正文档，不改生成逻辑。

纪律（同 `6.4` 的 `models_manifest.py`）：**stdlib only**、**零出网**、**零子进程**、
只读 `site-packages` 与仓库内文本，**绝不碰仓库 `.env`**（含真实密钥）。

用法：
    python tools/packaging/collect_licenses.py \\
        --site-packages <build-environment> \\
        --out build\\licenses-stage \\
        --inno-license "<Inno Setup 6>\\license.txt"

产物（`--out` 下）：
    LICENSE                               ← 仓库根 LICENSE（随包，放安装目录根）
    NOTICE                                ← 仓库根 NOTICE（随包，放安装目录根）
    licenses\\Inno-Setup-license.txt       ← 安装器自身的许可全文（裁决 8）
    licenses\\00-许可证清单.txt            ← 机械生成的索引（含每件 sha256 与来源），供逐项核对
    licenses\\<包名>\\<相对路径>            ← 各随包组件的许可全文
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 必须在内、且**逐字可核**的三件（规格 4 的验收面；缺一件即非零退出）。
#: 值是 sha256（小写十六进制）；`None` 表示「只要求在内、不锁字节」。
MUST_HAVE: tuple[tuple[str, str | None], ...] = (
    # ① MinerU：自定义 Apache 变体 —— 被判据明确点名，**不可用通用 Apache 文本替代**
    ("mineru/LICENSE.md", "2d9aeb5d15159329a20dd804f251c64860ba954fbd26ec02f802220e6d71cf5c"),
    # ② OpenCV：wheel 自带的第三方堆许可全文（183,251 B）
    ("opencv_python/LICENSE-3RD-PARTY.txt", None),
    # ③ pypdfium2：wheel 自带的三份许可（Apache-2.0 / BSD-3-Clause / CC-BY-4.0）
    ("pypdfium2/LICENSES/Apache-2.0.txt", None),
    ("pypdfium2/LICENSES/BSD-3-Clause.txt", None),
    ("pypdfium2/LICENSES/CC-BY-4.0.txt", None),
)

#: dist-info 里算「许可文本」的文件名（大小写不敏感的前缀匹配）+ 目录名（整体收下）。
_LICENSE_FILE_PREFIXES = ("license", "licence", "copying", "notice", "copyright")
_LICENSE_DIR_NAMES = ("licenses", "licence", "build_licenses", "third_party_licenses")

#: wheel 内无许可全文的包，从 vendor 目录取带来源注记的许可材料（含人工模板）。
_VENDOR_DIR = Path(__file__).resolve().parent / "licenses-vendor"


def _norm(name: str) -> str:
    """按 PEP 503 归一化（`-`/`.`/`_` 一律当 `_`，且小写）——dist-info 目录名与依赖名口径不同。"""
    return name.strip().lower().replace("-", "_").replace(".", "_")


def _parse_lock(lock_file: Path) -> set[str]:
    """读 requirements-lock.txt 的**包名集合**（忽略注释、空行、版本号、extras、环境标记）。"""
    names: set[str] = set()
    for raw in lock_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", ";", "[", " "):
            idx = line.find(sep)
            if idx > 0:
                line = line[:idx]
        if line:
            names.add(_norm(line))
    return names


def _parse_spec_excludes(spec_file: Path) -> set[str]:
    """从 `zhilan.spec` 的 `excludes = [...]` 读取显式排除项；不分析实际成品模块图。

    用 **AST 静态解析**而不是 import：spec 是给 PyInstaller 执行的文件，import 它会触发
    `Analysis(...)` 等构建期对象。AST 只读语法树，不执行任何东西。
    """
    tree = ast.parse(spec_file.read_text(encoding="utf-8"), filename=str(spec_file))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "excludes" in targets and isinstance(node.value, ast.List):
                values = [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
                return {_norm(v) for v in values if isinstance(v, str)}
    raise SystemExit(f"[licenses] 在 {spec_file} 里找不到 `excludes = [...]`（判据不能靠猜，中止）")


def _is_license_file(path: Path) -> bool:
    return path.name.lower().startswith(_LICENSE_FILE_PREFIXES)


def _dist_name(dist_info: Path) -> str:
    """`mineru-4.0.0.dist-info` → `mineru`（版本段是最后一段 `-` 之后的部分）。"""
    stem = dist_info.name[: -len(".dist-info")]
    return stem.rsplit("-", 1)[0]


def _iter_license_files(dist_info: Path) -> list[tuple[Path, Path]]:
    """返回 `(源文件, 相对安装目录 licenses/ 下的相对路径)`；相对路径已把 dist-info 去掉一层。"""
    found: list[tuple[Path, Path]] = []
    for child in sorted(dist_info.iterdir()):
        if child.is_dir():
            if child.name.lower() in _LICENSE_DIR_NAMES:
                # `dist-info\\licenses\\X` → `licenses\\<包>\\X`（把那一层 `licenses` 吃掉，读起来更顺；
                # 但 `BUILD_LICENSES\\` / `LICENSES\\` 这类带语义的子目录名保留）
                strip = child.name.lower() in ("licenses", "licence")
                for item in sorted(child.rglob("*")):
                    if item.is_file():
                        rel = item.relative_to(child) if strip else Path(child.name) / item.relative_to(child)
                        found.append((item, rel))
        elif child.is_file() and _is_license_file(child):
            found.append((child, Path(child.name)))
    return found


def _top_level_dirs(dist_info: Path, site_packages: Path) -> list[Path]:
    """从 `top_level.txt` 找出该发行版实际安装的包目录（包目录名常与发行版名不同）。"""
    top = dist_info / "top_level.txt"
    if not top.is_file():
        return []
    dirs: list[Path] = []
    for line in top.read_text(encoding="utf-8", errors="replace").splitlines():
        name = line.strip()
        if not name:
            continue
        candidate = site_packages / name
        if candidate.is_dir():
            dirs.append(candidate)
    return dirs


def _fallback_license_files(dist_info: Path, site_packages: Path) -> list[tuple[Path, Path]]:
    """dist-info 里没有许可文本时的兜底：到包目录里找（不少 wheel 把 `LICENSE` 放在包内）。

    实测（2026-09-20）：`onnxruntime` 的 dist-info **没有**许可文件，但包目录里有
    `onnxruntime\\LICENSE`（1,094 B）——`onnxruntime` 是随包分发的核心件，漏了它就是实打实的合规缺口。
    """
    found: list[tuple[Path, Path]] = []
    for pkg_dir in _top_level_dirs(dist_info, site_packages):
        # 包目录名与发行版名相同时不再套一层（`onnxruntime\LICENSE` 而不是 `onnxruntime\onnxruntime\LICENSE`）
        flatten = _norm(pkg_dir.name) == _norm(_dist_name(dist_info))
        for item in sorted(pkg_dir.rglob("*")):
            if item.is_file() and _is_license_file(item):
                rel = item.relative_to(pkg_dir) if flatten else Path(pkg_dir.name) / item.relative_to(pkg_dir)
                found.append((item, rel))
    return found


def _declared_license(dist_info: Path) -> str:
    """从 METADATA 取该发行版**声明的**许可（`License-Expression:` 优先，其次 `License:`）。

    这不是「许可全文」，只是兜底登记——防止「没找到全文」这件事被静默吞掉（见下方清单里的单列）。
    """
    meta = dist_info / "METADATA"
    if not meta.is_file():
        return ""
    expression, plain = "", ""
    for raw in meta.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw[:1].isalpha():
            break  # 头部字段结束（空行之后是长描述，里面出现的 "License:" 不算）
        key, _, value = raw.partition(":")
        key = key.strip().lower()
        if key == "license-expression" and not expression:
            expression = value.strip()
        elif key == "license" and not plain:
            plain = value.strip()
    return expression or plain


def _declared_license_label(dist_info: Path) -> str:
    """清单显示用：METADATA 的声明；连 `License:` 头字段都没有时退到 Classifier（如 cobble 0.1.4）。

    只作展示口径，不是法律判断——cobble 的 Classifier 只说「BSD License」，按其 README 的
    BSD-2-Clause 链接处理为 BSD-2-Clause（vendor 文件里已如实注明）。
    """
    label = _declared_license(dist_info)
    if label:
        return label
    meta = dist_info / "METADATA"
    if not meta.is_file():
        return ""
    for raw in meta.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw[:1].isalpha():
            break  # 头部字段结束（同 `_declared_license` 的口径）
        key, _, value = raw.partition(":")
        stripped = value.strip()
        if key.strip().lower() == "classifier" and stripped.lower().startswith("license ::"):
            return f"Classifier「{stripped.split('::')[-1].strip()}」"
    return ""


def _vendor_license_file(name: str) -> Path | None:
    """wheel 无许可全文时，取仓库内 vendor 的许可材料（包括上游文本及人工模板）。

    vendor 目录 = `tools/packaging/licenses-vendor/<包名>/LICENSE`（包名按 `_norm` 口径），
    由人工整理，末尾附实际来源注记；本函数只读，未命中返回 `None`（维持原兜底）。
    """
    candidate = _VENDOR_DIR / name / "LICENSE"
    return candidate if candidate.is_file() else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def collect(  # noqa: PLR0913 —— 参数各自独立，聚合进一个对象反而更难读
    site_packages: Path,
    lock_file: Path,
    out_dir: Path,
    spec_file: Path,
    inno_license: Path | None,
    vendor_note: str = "",
) -> int:
    if not site_packages.is_dir():
        raise SystemExit(f"[licenses] site-packages 不存在：{site_packages}")
    if not out_dir.exists():
        out_dir.mkdir(parents=True)
    licenses_dir = out_dir / "licenses"
    licenses_dir.mkdir(parents=True, exist_ok=True)

    wanted = _parse_lock(lock_file)
    excluded = _parse_spec_excludes(spec_file)
    effective = {name for name in wanted if name not in excluded}

    rows: list[tuple[str, str, str, int]] = []  # (包名, 相对路径, sha256, 字节数)
    dist_infos = sorted(p for p in site_packages.glob("*.dist-info") if p.is_dir())
    skipped_packages: list[str] = []
    declared_only: list[tuple[str, str]] = []  # (包名, METADATA 里声明的许可)；**如实登记，不假装收全**
    vendored: list[tuple[str, str]] = []  # (包名, 显示用许可名)；回归修复：wheel 无全文但 vendor 命中
    for dist_info in dist_infos:
        name = _norm(_dist_name(dist_info))
        if name not in effective:
            if name in excluded:
                skipped_packages.append(name)
            continue
        files = _iter_license_files(dist_info)
        if not files:
            files = _fallback_license_files(dist_info, site_packages)
        for src, rel in files:
            dest = licenses_dir / name / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())
            rows.append((name, rel.as_posix(), _sha256(dest), dest.stat().st_size))
        if not files:
            declared = _declared_license(dist_info)
            vendor_src = _vendor_license_file(name)
            if vendor_src is not None:
                # wheel 无全文但 vendor 命中：随包复制许可材料，实际来源以文件末尾注记为准。
                dest = licenses_dir / name / "LICENSE"
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(vendor_src.read_bytes())
                rows.append((name, dest.name, _sha256(dest), dest.stat().st_size))
                vendored.append((name, _declared_license_label(dist_info)))
                if declared:
                    note = (
                        f"本发行版的 wheel 里没有许可全文文件（dist-info 与包目录都没有）。\n"
                        f"它自己声明的许可是：{declared}\n"
                        f"上游许可全文已随包携带：licenses/{name}/LICENSE（取自仓库 licenses-vendor 目录，回归修复）。\n"
                        f"——此文件由 tools/packaging/collect_licenses.py 生成，只作声明佐证；全文以上述 LICENSE 为准。\n"
                    )
                    note_dest = licenses_dir / name / "METADATA-授权声明.txt"
                    note_dest.parent.mkdir(parents=True, exist_ok=True)
                    note_dest.write_text(note, encoding="utf-8")
                    rows.append((name, note_dest.name, _sha256(note_dest), note_dest.stat().st_size))
            else:
                declared_only.append((name, declared))
                if declared:
                    note = (
                        f"本发行版的 wheel 里没有许可全文文件（dist-info 与包目录都没有）。\n"
                        f"它自己声明的许可是：{declared}\n"
                        f"——此文件由 tools/packaging/collect_licenses.py 兜底生成，只作登记，不是许可全文。\n"
                    )
                    dest = licenses_dir / name / "METADATA-授权声明.txt"
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(note, encoding="utf-8")
                    rows.append((name, dest.name, _sha256(dest), dest.stat().st_size))

    # 仓库根两件：随包放安装目录根（不是 licenses\\ 里）——Apache-2.0 §4 的常规落法
    for name in ("LICENSE", "NOTICE"):
        src = REPO_ROOT / name
        if not src.is_file():
            raise SystemExit(f"[licenses] 仓库根缺少 {name}（规格 4 要求随包携带）")
        (out_dir / name).write_bytes(src.read_bytes())

    # 安装器自身的许可（裁决 8）：Inno Setup 的 license.txt 全文
    if inno_license is not None:
        if not inno_license.is_file():
            raise SystemExit(f"[licenses] 找不到 Inno Setup 的 license.txt：{inno_license}")
        (licenses_dir / "Inno-Setup-license.txt").write_bytes(inno_license.read_bytes())

    # 硬断言：三件必须在，且 MinerU 那件逐字相同
    problems: list[str] = []
    for rel, expected in MUST_HAVE:
        dest = licenses_dir / rel
        if not dest.is_file():
            problems.append(f"缺件：licenses\\{rel}")
            continue
        if expected is not None:
            actual = _sha256(dest)
            if actual != expected:
                problems.append(f"哈希不符：licenses\\{rel} 期望 {expected}，实测 {actual}")
    if not (licenses_dir / "Inno-Setup-license.txt").is_file():
        problems.append("缺件：licenses\\Inno-Setup-license.txt（安装器自身的许可，裁决 8）")

    # 索引：机械生成，给「可逐项核对」一个入口
    index_lines = [
        "知澜 · 随包许可证清单（由 tools/packaging/collect_licenses.py 机械生成）",
        "",
        "来源环境  ：构建环境的 site-packages",
        "依赖锁定  ：tools/packaging/requirements-lock.txt",
        "排除规则  ：tools/packaging/zhilan.spec 的 excludes（不进入产物的组件不收集许可）",
        f"锁定件数  ：{len(wanted)}；排除件数：{len(excluded)}；实际收集包数：{len({r[0] for r in rows})}",
        "",
        f"被 excludes 摘掉、且确实装在 site-packages 里、因而不收集许可的包（{len(skipped_packages)}）："
        f"{', '.join(skipped_packages) or '（无）'}",
        "",
        f"⚠️ wheel 内无许可全文的包（{len(vendored) + len(declared_only)}）："
        f"vendor 已随包携带上游全文 {len(vendored)} 个，仍未覆盖 {len(declared_only)} 个：",
        *(
            [
                f"    {name}  →  已随包携带上游全文（vendor）：licenses/{name}/LICENSE（{label}）"
                if label
                else f"    {name}  →  已随包携带上游全文（vendor）：licenses/{name}/LICENSE"
                for name, label in vendored
            ]
            + [
                f"    {name}  →  仅有 METADATA 声明：{declared or '（METADATA 连 License 字段都没有）'}"
                for name, declared in declared_only
            ]
            or ["    （无）"]
        ),
        "    说明：vendor 文本取自仓库 tools/packaging/licenses-vendor/<包名>/LICENSE（上游原文，末尾附来源注记）。",
        "    仍未覆盖的件仅有 METADATA 声明；分发前必须补齐其许可全文并重新核对。",
        "",
        "═══ 逐件清单（相对 <安装目录>\\licenses\\）═══",
    ]
    for name, rel, digest, size in sorted(rows):
        index_lines.append(f"{digest}  {size:>9} B  {name}/{rel}")
    index_lines += [
        "",
        "═══ 随包放在安装目录根的许可与声明 ═══",
        f"{_sha256(out_dir / 'LICENSE')}  {(out_dir / 'LICENSE').stat().st_size:>9} B  LICENSE",
        f"{_sha256(out_dir / 'NOTICE')}  {(out_dir / 'NOTICE').stat().st_size:>9} B  NOTICE",
    ]
    if inno_license is not None:
        # 裁决 8：把「安装器由 Inno Setup 6 生成」写进**产品文档**（开始菜单「许可证与第三方声明」
        # 直接指向本目录）。Inno 的许可条件 3 明写「致谢 appreciated but not required」，
        # 成本是一行字，故照做；条件 2 要求的版权与网址也在下面这行里。
        index_lines += [
            "",
            "本安装程序由 Inno Setup 6 生成（Copyright (C) 1997-2025 Jordan Russell，"
            "https://jrsoftware.org/isinfo.php）；其许可证全文见同目录 Inno-Setup-license.txt。",
        ]
    if vendor_note:
        index_lines += ["", f"说明：{vendor_note}"]
    (licenses_dir / "00-许可证清单.txt").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    print("[licenses] 知澜 · 随包许可收集")
    print(f"[licenses] site-packages ：{site_packages}")
    print(f"[licenses] 锁定 {len(wanted)} 件 / excludes 摘掉 {len(excluded)} 件 / 实收 {len({r[0] for r in rows})} 个包")
    print(f"[licenses] 许可文件 {len(rows)} 个 + LICENSE + NOTICE + Inno-Setup-license.txt + 清单")
    print(f"[licenses] 暂存目录：{out_dir}")
    print(f"[licenses] vendor 随包携带上游全文 {len(vendored)} 个："
          f"{', '.join(name for name, _ in vendored) or '（无）'}")
    print(f"[licenses] 仍只登记 METADATA 声明（无全文）的包 {len(declared_only)} 个："
          f"{', '.join(name for name, _ in declared_only) or '（无）'}")
    for rel, _expected in MUST_HAVE:
        print(f"[ OK ] 三件必在内：licenses\\{rel}")
    if problems:
        for item in problems:
            print(f"[FAIL] {item}")
        print("[licenses] 结论：失败（判据不成立，安装包不得构建）")
        return 1
    print("[licenses] 结论：PASS —— MinerU LICENSE.md 全文在内且 sha256 与源件一致")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="收集随包组件的许可证全文到暂存目录（供 zhilan.iss 打进 <安装目录>\\licenses\\）。"
    )
    parser.add_argument("--site-packages", required=True, help="构建专用 venv 的 Lib\\site-packages")
    parser.add_argument(
        "--lock-file",
        default=str(REPO_ROOT / "tools" / "packaging" / "requirements-lock.txt"),
        help="依赖锁定件（默认取仓库内那份）",
    )
    parser.add_argument(
        "--spec-file",
        default=str(REPO_ROOT / "tools" / "packaging" / "zhilan.spec"),
        help="PyInstaller spec（从中读 excludes 一族，默认取仓库内那份）",
    )
    parser.add_argument("--out", default=str(REPO_ROOT / "build" / "licenses-stage"), help="暂存目录")
    parser.add_argument("--inno-license", default=None, help="Inno Setup 的 license.txt 路径")
    parser.add_argument("--vendor-note", default="", help="写进清单末尾的一行说明")
    args = parser.parse_args()
    return collect(
        site_packages=Path(args.site_packages),
        lock_file=Path(args.lock_file),
        out_dir=Path(args.out),
        spec_file=Path(args.spec_file),
        inno_license=Path(args.inno_license) if args.inno_license else None,
        vendor_note=args.vendor_note,
    )


if __name__ == "__main__":
    sys.exit(main())
