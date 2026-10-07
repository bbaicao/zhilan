# -*- coding: utf-8 -*-
"""应用图标（「书页化澜」）的**接线判据**：源稿、多尺寸 ico、三处消费点一个不缺。

🔴 口径（逐字，防误读）

· 本文件钉的是「**图标资产存在 + 三处接线在位**」（资产面），**不是**「exe 已带上图标」——
  那要等下次发布构建后在真 exe 上验证（PyInstaller ``icon=`` 在构建期把 ico 写进 PE 资源）；
· 图标产物由 ``tools/packaging/icon/build_icon.py`` 生成（单一几何源 ⇒ SVG / ICO 同源），
  ⛔ 手改 ``zhilan-icon.svg`` 会被下次生成覆盖——改设计请改生成器；
· ICO 尺寸解析走 **struct 头部字节**（本仓测试环境不依赖 Pillow）。
"""

from __future__ import annotations

import struct
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ICON_DIR = REPO / "tools" / "packaging"
ICON_FILE = ICON_DIR / "zhilan.ico"
SVG_FILE = ICON_DIR / "zhilan-icon.svg"
GENERATOR = ICON_DIR / "icon" / "build_icon.py"
PUBLIC_DIR = REPO / "frontend" / "public"


def _ico_sizes(data: bytes) -> set[int]:
    """解析 ICO 目录表，返回实际包含的尺寸集合（width/height 字节 0 = 256）。"""
    assert len(data) >= 6, "ICO 头部不足 6 字节"
    _reserved, ico_type, count = struct.unpack("<HHH", data[:6])
    assert ico_type == 1, f"不是 ICO 文件（type={ico_type}）"
    sizes: set[int] = set()
    for i in range(count):
        entry = data[6 + 16 * i : 6 + 16 * (i + 1)]
        width, height = entry[0], entry[1]
        sizes.add(256 if width == 0 else width)
        assert 256 if height == 0 else height == (256 if width == 0 else width), "ICO 条目宽高不一致"
    return sizes


def test_zhilan_ico_has_all_windows_sizes():
    """腿①（主腿）：`zhilan.ico` 存在且含 Windows 全部七档尺寸（16..256）。"""
    assert ICON_FILE.is_file(), f"缺图标产物：{ICON_FILE}"
    data = ICON_FILE.read_bytes()
    sizes = _ico_sizes(data)
    assert sizes == {16, 24, 32, 48, 64, 128, 256}, f"ICO 尺寸档不符：{sorted(sizes)}"


def test_spec_wires_the_icon():
    """腿②：`zhilan.spec` 的 EXE 带 `icon=` 且指向存在的 ico（exe 图标面）。"""
    spec = (REPO / "tools" / "packaging" / "zhilan.spec").read_text(encoding="utf-8")
    assert "icon=str(SPEC_DIR" in spec and "zhilan.ico" in spec, "spec 未接线 icon="
    assert ICON_FILE.is_file()


def test_iss_wires_the_setup_icon():
    """腿③：`zhilan.iss` 的 [Setup] 带 `SetupIconFile` 且指向存在的 ico（安装包面）。"""
    iss = (REPO / "tools" / "packaging" / "zhilan.iss").read_text(encoding="utf-8")
    assert "SetupIconFile=" in iss and "zhilan.ico" in iss, "iss 未接线 SetupIconFile"
    assert ICON_FILE.is_file()


def test_frontend_has_favicon():
    """腿④：双格式资产与实际 /assets 接线一致，复制未改变品牌字节（回归检查）。"""
    html = (REPO / "frontend" / "index.html").read_text(encoding="utf-8")
    for extension in ("ico", "svg"):
        original = PUBLIC_DIR / f"favicon.{extension}"
        asset = PUBLIC_DIR / "assets" / f"favicon.{extension}"
        assert original.is_file() and asset.is_file()
        assert asset.read_bytes() == original.read_bytes(), "favicon 复制改变了品牌字节"
        assert f'href="/assets/favicon.{extension}"' in html, "index.html 未引用已挂载的 favicon 路径"


def test_svg_source_matches_brand_and_is_generated():
    """腿⑤（防漂移）：矢量源稿存在、含品牌 amber `rgb(232,160,84)`、且带「勿手改」生成器标记。"""
    assert SVG_FILE.is_file()
    assert GENERATOR.is_file(), "生成器脚本缺失 ⇒ 图标不可复现"
    svg = SVG_FILE.read_text(encoding="utf-8")
    assert "rgb(232,160,84)" in svg, "品牌 amber（--zh-brand）不在源稿里 ⇒ 配色漂移"
    assert "build_icon.py 生成" in svg, "缺「勿手改」生成标记"
