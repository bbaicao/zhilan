# -*- coding: utf-8 -*-
"""知澜应用图标生成器（「书页化澜」· 2026-09-27 业主定稿方向 ①）。

单一几何源 → 三种产物，保证 SVG / ICO / 预览逐像素同源：
  1. ``zhilan-icon.svg``  —— 矢量源稿（README/前端用，可无损缩放）；
  2. ``zhilan.ico``       —— Windows exe（PyInstaller ``icon=``）与安装包（Inno ``SetupIconFile``），
     多尺寸 16/24/32/48/64/128/256（LANCZOS 降采样）；
  3. ``icon-preview-*.png`` —— 预览图（256 + 32/16 小尺寸拼图，供人眼验收）。

设计定稿（业主 2026-09-27 选定「书页化澜」）：深暖棕圆角底（取自前端 ``--zh-brand-soft``
同族）上三条琥珀水波带——「知 = 书页叠放，澜 = 三道波澜」；琥珀取自前端 ``--zh-brand``
（232 160 84）。三条带上下缘为同相位正弦弧（带厚恒定），相位逐带翻转形成澜的节奏。

用法（在仓库根或任意 cwd）::

    python tools/packaging/icon/build_icon.py            # 产物写到 tools/packaging/
    python tools/packaging/icon/build_icon.py --out DIR  # 指定输出目录

依赖：Pillow（后端/打包 venv 均自带，12.x 验证）；零网络、零仓内 import。
"""

from __future__ import annotations

import argparse
import math
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# 几何与配色（单一真相源；画布 1024，绘制时整体 ×SS 超采样再降回）
# ---------------------------------------------------------------------------

CANVAS = 1024
SS = 4  # 超采样倍率（抗锯齿；曲线在 4096 画布上绘制再 LANCZOS 缩回）

TILE = (64, 64, 960, 960)          # 圆角底板
TILE_RADIUS = 200
TILE_GRAD_TOP = (51, 40, 24)       # #332818（暖棕，亮端）
TILE_GRAD_BOTTOM = (26, 20, 14)    # #1A140E（暗端）

BAND_X0 = 200                      # 波带左端
BAND_W = 624                       # 波带宽度（200..824，关于 512 对称）
SAMPLES = 96                       # 每条曲线的采样数

# 三条波带：(base 顶缘基线, 振幅, 带厚, 相位, 颜色)——**整周期正弦**（一条带内有真波峰+波谷），
# 三条同相位平行成澜（书页叠放 + 水波的合体剪影）；间距 ≥36 保证 16px 下不糊成一片。
BANDS = [
    (219, 56, 94, 0.0, (242, 190, 126)),   # #F2BE7E 浅琥珀
    (465, 56, 94, 0.0, (232, 160, 84)),    # #E8A054 品牌 amber（= 前端 --zh-brand）
    (711, 56, 94, 0.0, (196, 119, 46)),    # #C4772E 深琥珀
]

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
FAVICON_SIZES = (16, 32, 48)


def _wave_points(base: float, amp: float, phase: float) -> list[tuple[float, float]]:
    """一条波带**上缘**的采样点（画布坐标，未超采样）。"""
    return [
        (BAND_X0 + BAND_W * t / SAMPLES, base + amp * math.sin(2 * math.pi * t / SAMPLES + phase))
        for t in range(SAMPLES + 1)
    ]


def _band_polygon(base: float, amp: float, height: float, phase: float) -> list[tuple[float, float]]:
    """波带多边形 = 上缘采样点 + 下缘（平行同相位）反向采样点。"""
    top = _wave_points(base, amp, phase)
    bottom = [(x, y + height) for x, y in top]
    return top + bottom[::-1]


def _gradient_tile(size: int) -> Image.Image:
    """圆角底板：垂直线性渐变（#332818 → #1A140E），按圆角矩形裁剪。"""
    scale = size // CANVAS if size >= CANVAS else 1
    strip = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        r = round(TILE_GRAD_TOP[0] + (TILE_GRAD_BOTTOM[0] - TILE_GRAD_TOP[0]) * t)
        g = round(TILE_GRAD_TOP[1] + (TILE_GRAD_BOTTOM[1] - TILE_GRAD_TOP[1]) * t)
        b = round(TILE_GRAD_TOP[2] + (TILE_GRAD_BOTTOM[2] - TILE_GRAD_TOP[2]) * t)
        strip.putpixel((0, y), (r, g, b))
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    img.paste(strip.resize((size, size)), (0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [v * scale for v in TILE] if scale > 1 else list(TILE),
        radius=TILE_RADIUS * scale, fill=255,
    )
    img.putalpha(mask)
    return img


def render(size: int = CANVAS) -> Image.Image:
    """渲染整图（RGBA）。内部按 ``size * SS`` 超采样绘制再 LANCZOS 缩回。"""
    big = size * SS
    img = _gradient_tile(big)
    draw = ImageDraw.Draw(img)
    k = big / CANVAS
    for base, amp, height, phase, color in BANDS:
        pts = [(x * k, y * k) for x, y in _band_polygon(base, amp, height, phase)]
        draw.polygon(pts, fill=(*color, 255))
    return img.resize((size, size), Image.LANCZOS)


def to_svg() -> str:
    """矢量源稿（与 :func:`render` 同一几何源）。"""
    paths = []
    for base, amp, height, phase, color in BANDS:
        pts = _band_polygon(base, amp, height, phase)
        d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + " Z"
        paths.append(f'    <path d="{d}" fill="rgb({color[0]},{color[1]},{color[2]})"/>')
    body = "\n".join(paths)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" '
        'width="1024" height="1024">\n'
        "  <!-- 知澜 · 「书页化澜」图标源稿（tools/packaging/icon/build_icon.py 生成，勿手改） -->\n"
        "  <defs>\n"
        '    <linearGradient id="tile" x1="0" y1="0" x2="0" y2="1">\n'
        f'      <stop offset="0" stop-color="rgb({TILE_GRAD_TOP[0]},{TILE_GRAD_TOP[1]},{TILE_GRAD_TOP[2]})"/>\n'
        f'      <stop offset="1" stop-color="rgb({TILE_GRAD_BOTTOM[0]},{TILE_GRAD_BOTTOM[1]},{TILE_GRAD_BOTTOM[2]})"/>\n'
        "    </linearGradient>\n"
        "  </defs>\n"
        f'  <rect x="{TILE[0]}" y="{TILE[1]}" width="{TILE[2] - TILE[0]}" height="{TILE[3] - TILE[1]}" '
        f'rx="{TILE_RADIUS}" fill="url(#tile)"/>\n'
        f"{body}\n"
        "</svg>\n"
    )


def _ico_bytes(images: list[Image.Image]) -> bytes:
    """把多尺寸 RGBA 合成一个 .ico（PIL 的 ICO 落盘走 BytesIO）。"""
    base = images[-1]
    buf = BytesIO()
    base.save(buf, format="ICO", append_images=images[:-1], sizes=[(im.width, im.height) for im in images])
    return buf.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description="知澜图标生成器（书页化澜）")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent.parent),
                        help="输出目录（默认 tools/packaging/）")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # 1) 矢量源稿
    (out / "zhilan-icon.svg").write_text(to_svg(), encoding="utf-8")

    # 2) Windows .ico（exe + 安装包）
    master = render(CANVAS)
    ico_imgs = [master.resize((s, s), Image.LANCZOS) for s in ICO_SIZES]
    (out / "zhilan.ico").write_bytes(_ico_bytes(ico_imgs))

    # 3) 前端 favicon（ico 三小尺寸 + svg）
    fav_imgs = [master.resize((s, s), Image.LANCZOS) for s in FAVICON_SIZES]
    (out / "favicon.ico").write_bytes(_ico_bytes(fav_imgs))
    (out / "favicon.svg").write_text(to_svg(), encoding="utf-8")

    # 4) 预览图（人眼验收用；256 主视图 + 32/16 小尺寸并排）
    preview = Image.new("RGBA", (CANVAS, CANVAS // 2), (24, 24, 24, 255))
    preview.alpha_composite(master.resize((CANVAS // 2, CANVAS // 2), Image.LANCZOS), (0, 0))
    preview.alpha_composite(master.resize((32, 32), Image.LANCZOS), (CANVAS // 2 + 16, 16))
    preview.alpha_composite(master.resize((16, 16), Image.LANCZOS), (CANVAS // 2 + 64, 24))
    preview.convert("RGB").save(out / "icon-preview.png")
    print(f"[icon] svg/ico/favicon/预览 已写出 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
