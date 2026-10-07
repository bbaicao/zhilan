# -*- coding: utf-8 -*-
"""核对九包 vendor 材料、固定来源和随包字节保真。

保留原九包清单、许可标记、正文大小和 collector 行为检查。SOURCE.json
进一步绑定材料与固定版本：原始许可正文按其前缀字节核对；cobble 的 BSD
整理模板不得冒充原件，并须携带嵌入 inflection 的原始 MIT 许可。
proxy_tools 的原始 BSD 正文与 MIT 元数据差异保留。复制成功不代表法律合规通过。
"""

from __future__ import annotations

import hashlib
import json
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
    # 来源注记与结构化版本依据必须同时保留。
    assert "-----" in text, f"{pkg} 的 vendor LICENSE 缺来源注记分隔线"
    assert "SOURCE.json" in text, f"{pkg} 的 vendor LICENSE 缺来源记录引用"
    source = json.loads((VENDOR_DIR / pkg / "SOURCE.json").read_text(encoding="utf-8"))
    material = (VENDOR_DIR / pkg / "LICENSE").read_bytes()
    assert source["name"] == pkg
    assert source["version"]
    assert source["material"]["bytes"] == len(material)
    assert source["material"]["sha256"] == hashlib.sha256(material).hexdigest()
    assert len(source["distribution"]["sha256"]) == 64
    original = source["licenseSource"]
    if pkg == "cobble":
        assert source["status"] == "declared-license-prepared-text"
        assert original["preparedTemplate"] is True
        assert len(source["primaryDeclaration"]["commit"]) == 40
        embedded = source["embeddedNotices"][0]
        mit = (VENDOR_DIR / pkg / embedded["path"]).read_bytes()
        assert embedded["sha256"] == hashlib.sha256(mit).hexdigest()
        assert b"Copyright (C) 2012-2015 Janne Vanhala" in mit
        assert b"Permission is hereby granted" in mit
        assert embedded["codeBinding"]["completeUnderscoreFunctionEqual"] is True
    else:
        assert original["preparedTemplate"] is False
        assert len(original["commit"]) == 40
        assert hashlib.sha256(material[:original["bytes"]]).hexdigest() == original["sha256"]
        if pkg == "proxy_tools":
            assert source["status"] == "source-bound-with-metadata-conflict"
            assert source["sourceCodeBinding"]["sdistAndCommitEqual"] is True
            assert source["sourceCodeBinding"]["sdistAndInstalledEqual"] is True
            assert "METADATA declares MIT" in source["license"]
            for embedded in source["embeddedNotices"]:
                raw = (VENDOR_DIR / pkg / embedded["path"]).read_bytes()
                assert len(raw) == embedded["bytes"]
                assert hashlib.sha256(raw).hexdigest() == embedded["sha256"]
            werkzeug = (VENDOR_DIR / pkg / "LICENSE-Werkzeug-BSD").read_bytes()
            assert b"Copyright (c) 2011 by the Werkzeug Team" in werkzeug
            assert "may not be used to endorse or promote" in " ".join(
                werkzeug.decode("utf-8").split()
            )
            assert source["embeddedNotices"][0]["codeBinding"]["completeForwardingTailVerbatim"]


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
    webview_stub = tmp_path / "webview-license.txt"
    webview_stub.write_text(
        "MICROSOFT EDGE WEBVIEW2 RUNTIME\nDISTRIBUTABLE CODE\nSmartScreen\n"
        + "Explicit test fixture, not a distribution license.\n" * 20,
        encoding="utf-8",
        newline="\r\n",
    )
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
            "--webview-license",
            str(webview_stub),
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

    # ① 九包材料及 SOURCE.json 逐字随包（模板与原文由来源记录区分）。
    for pkg in VENDOR_PACKAGES:
        lic = out_dir / "licenses" / pkg / "LICENSE"
        assert lic.is_file(), f"vendor 全文未随包：{lic}"
        assert lic.stat().st_size >= 500, f"随包全文过小：{lic}（{lic.stat().st_size} B）"
        assert lic.read_bytes() == (VENDOR_DIR / pkg / "LICENSE").read_bytes()
        assert (out_dir / "licenses" / pkg / "SOURCE.json").read_bytes() == (
            VENDOR_DIR / pkg / "SOURCE.json"
        ).read_bytes()
    assert (out_dir / "licenses" / "cobble" / "LICENSE-inflection-MIT").read_bytes() == (
        VENDOR_DIR / "cobble" / "LICENSE-inflection-MIT"
    ).read_bytes()
    for name in ("LICENSE-Werkzeug-BSD", "NOTICE-Werkzeug-AUTHORS"):
        assert (out_dir / "licenses" / "proxy_tools" / name).read_bytes() == (
            VENDOR_DIR / "proxy_tools" / name
        ).read_bytes()
    assert (out_dir / "licenses" / "Microsoft-WebView2-Runtime" / "LICENSE.txt").read_bytes() == (
        webview_stub.read_bytes()
    )
    terms = (out_dir / "INSTALLATION-TERMS.txt").read_text(encoding="utf-8-sig")
    assert b"\r\r\n" not in (out_dir / "INSTALLATION-TERMS.txt").read_bytes()
    assert webview_stub.read_text(encoding="utf-8") in terms
    assert "Microsoft Defender SmartScreen" in terms
    assert "默认启用时会收集并发送用户信息给 Microsoft" in terms
    assert (out_dir / "licenses" / "mineru" / "LICENSE.md").read_text(encoding="utf-8") in terms

    # ② 清单不再含旧头部断言；vendor 命中数如实（9 命中 / 0 未覆盖）
    index = (out_dir / "licenses" / "00-许可证清单.txt").read_text(encoding="utf-8")
    assert _OldAssertion not in index, "清单仍含过时断言「连包目录里都找不到许可全文」"
    assert "vendor 仓内材料已携带 9 个，仍无材料 0 个" in index, "清单的 vendor 命中数不如实"
    assert "vendor 已随包携带上游全文 9 个" not in index
    assert "Python材料包数：157；" in index
