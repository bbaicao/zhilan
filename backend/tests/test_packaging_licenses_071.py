# -*- coding: utf-8 -*-
"""回归修复 判据载体：9 包许可全文随包（vendor）。

**缺陷**：打包许可暂存（`tools/packaging/collect_licenses.py` 产出 `build/licenses-stage/licenses/`，
由 `zhilan.iss` 全量随包）里 9 个包缺许可全文、只有（或没有）`METADATA-授权声明.txt`——
它们的 wheel 内不含 LICENSE 文件：cobble / flatbuffers / grpcio_tools / jieba /
latex2mathml / loguru / magika / proxy_tools / tokenizers。

**修法（拍板⑥ 修法①：上游取许可全文随包）**：上游原文由人工一次性抓取进仓库
`tools/packaging/licenses-vendor/<包名>/LICENSE`（每份末尾附来源注记）；
`collect_licenses.py` 在「dist-info 与包目录都无全文」时回退到 vendor 目录，命中即随包全文。

**三条腿**：

- 腿①（静态）：vendor 目录对 9 个包名逐个有非空 LICENSE（>=500 B），且目录名清单钉死；
- 腿②（静态）：内容标记——4 个 Apache 包含 "Apache License" + "Version 2.0"；
  MIT/BSD 逐包对应（jieba / latex2mathml / loguru 含 "MIT License"，cobble 含 "BSD 2-Clause"；
  **proxy_tools 以上游原文自述 "The BSD License" 为标记**——其上游 LICENSE.txt 原文自称 BSD
  （版权 Armin Ronacher / Jonathan Tushman），而 wheel METADATA 声明 MIT，两者并存以原文为准，
  详见该 vendor 文件末尾的来源注记）；
- 腿③（行为腿，skipif 打包 venv 不在本机）：实跑 `collect_licenses.py`（`--out` 指向 tmp_path、
  `--inno-license` 用 tmp 替身——脚本对该件的检查是无条件的），断言输出 `licenses/` 下
  9 个包各得 LICENSE 全文且 >500 B，再断言 `00-许可证清单.txt` 不再含
  「连包目录里都找不到许可全文」的旧断言、且 vendor 命中数如实为 9 / 未覆盖 0。

**红线**：`collect()` 的 MUST_HAVE 硬断言集合不动；本文件不碰 `zhilan.iss`；
打包 venv（`<build-environment>`）与 `<legacy-repo>` 只读——腿③只写 `tmp_path`。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tools" / "packaging" / "collect_licenses.py"
VENDOR_DIR = REPO_ROOT / "tools" / "packaging" / "licenses-vendor"

#: 9 个 wheel 内无许可全文的包（包名与 requirements-lock / dist-info 一致，`_norm` 口径）。
VENDOR_PACKAGES: tuple[str, ...] = (
    "cobble",
    "flatbuffers",
    "grpcio_tools",
    "jieba",
    "latex2mathml",
    "loguru",
    "magika",
    "proxy_tools",
    "tokenizers",
)

#: 腿②：逐包内容标记（标记出处见各 vendor 文件与其末尾来源注记）。
CONTENT_MARKERS: dict[str, tuple[str, ...]] = {
    "cobble": ("BSD 2-Clause",),
    "flatbuffers": ("Apache License", "Version 2.0"),
    "grpcio_tools": ("Apache License", "Version 2.0"),
    "jieba": ("MIT License",),
    "latex2mathml": ("MIT License",),
    "loguru": ("MIT License",),
    "magika": ("Apache License", "Version 2.0"),
    "proxy_tools": ("The BSD License",),
    "tokenizers": ("Apache License", "Version 2.0"),
}

#: 腿③ 的打包专用 venv（只读；不在的本机 skip，不装样）。
PACKAGING_VENV = Path(r"E:\zhilan-pkg-venv")

_OldAssertion = "连包目录里都找不到许可全文"


# ────────────────────────────── 腿①：vendor 文件存在与钉死 ──────────────────────────────


@pytest.mark.parametrize("pkg", VENDOR_PACKAGES)
def test_071_vendor_license_exists_and_nonempty(pkg: str) -> None:
    path = VENDOR_DIR / pkg / "LICENSE"
    assert path.is_file(), f"缺 vendor 文件：{path}"
    size = path.stat().st_size
    assert size >= 500, f"vendor 文件过小（{size} B < 500 B）：{path}"


def test_071_vendor_dir_names_pinned() -> None:
    """vendor 目录名清单钉死：多一个少一个都算漂移（新增包须连判据一起改）。"""
    actual = {p.name for p in VENDOR_DIR.iterdir() if p.is_dir()}
    assert actual == set(VENDOR_PACKAGES), f"vendor 目录清单漂移：{sorted(actual ^ set(VENDOR_PACKAGES))}"


# ────────────────────────────── 腿②：内容标记逐包对应 ──────────────────────────────


@pytest.mark.parametrize("pkg", VENDOR_PACKAGES)
def test_071_vendor_content_markers(pkg: str) -> None:
    text = (VENDOR_DIR / pkg / "LICENSE").read_text(encoding="utf-8")
    for marker in CONTENT_MARKERS[pkg]:
        assert marker in text, f"{pkg} 的 vendor LICENSE 缺标记「{marker}」"
    # 来源注记块（回归修复 要求的 3 行来源注记）必在文件末尾
    assert "-----" in text, f"{pkg} 的 vendor LICENSE 缺来源注记分隔线"
    assert "用途：随包许可证全文" in text, f"{pkg} 的 vendor LICENSE 缺用途注记行"


# ────────────────────────────── 腿③：行为腿（实跑收集脚本） ──────────────────────────────

_requires_pkg_venv = pytest.mark.skipif(
    not (PACKAGING_VENV / "Lib" / "site-packages").is_dir(),
    reason=f"打包专用 venv 不在本机：{PACKAGING_VENV}",
)


@_requires_pkg_venv
def test_071_collect_licenses_vendors_full_texts(tmp_path: Path) -> None:
    """实跑 collect_licenses（--out 只写 tmp_path）：9 包各得 LICENSE 全文，清单不再有旧断言。"""
    out_dir = tmp_path / "licenses-stage"
    inno_stub = tmp_path / "inno-license.txt"
    inno_stub.write_text("stub：测试替身（脚本对 Inno license 的存在性检查是无条件的）\n", encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    proc = subprocess.run(  # noqa: S603 —— 固定参数的脚本自测，无外部输入
        [
            sys.executable,
            str(SCRIPT),
            "--site-packages",
            str(PACKAGING_VENV / "Lib" / "site-packages"),
            "--out",
            str(out_dir),
            "--inno-license",
            str(inno_stub),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(tmp_path),
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, f"collect_licenses 未过（MUST_HAVE 门禁）：\n{proc.stdout}\n{proc.stderr}"

    # ① 9 个包的 LICENSE 全文随包（>500 B）
    for pkg in VENDOR_PACKAGES:
        lic = out_dir / "licenses" / pkg / "LICENSE"
        assert lic.is_file(), f"vendor 全文未随包：{lic}"
        assert lic.stat().st_size >= 500, f"随包全文过小：{lic}（{lic.stat().st_size} B）"

    # ② 清单不再含旧头部断言；vendor 命中数如实（9 命中 / 0 未覆盖）
    index = (out_dir / "licenses" / "00-许可证清单.txt").read_text(encoding="utf-8")
    assert _OldAssertion not in index, "清单仍含过时断言「连包目录里都找不到许可全文」"
    assert "vendor 已随包携带上游全文 9 个，仍未覆盖 0 个" in index, "清单的 vendor 命中数不如实"
