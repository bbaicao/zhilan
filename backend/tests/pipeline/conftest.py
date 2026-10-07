"""pipeline 测试夹具：PDF/PNG 构造（不依赖 DB / 网络 / API Key）。

从老仓库 `tests/conftest.py` 抽取 pipeline 对拍所需的部分；
DB / engine / 服务注册表夹具不搬——2.2 只对拍纯函数行为。
"""

import struct
import zlib

import pytest


def make_png(width: int = 32, height: int = 32, rgb: tuple[int, int, int] = (220, 30, 30)) -> bytes:
    """生成一张纯色 PNG（用于视觉模型测试）。"""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def make_pdf(text: str) -> bytes:
    """构造一个最小可解析的 PDF（只支持 latin-1 文本，用于解析测试）。"""
    def esc(value: str) -> str:
        return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    content = f"BT /F1 12 Tf 72 720 Td ({esc(text)}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF"
    ).encode()
    return bytes(out)


def make_empty_pdf() -> bytes:
    """只有空白页、没有文字的 PDF，模拟扫描版。"""
    content = b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << >> /Contents 4 0 R >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF"
    ).encode()
    return bytes(out)


@pytest.fixture
def pdf_with_text() -> bytes:
    return make_pdf("Zhilan is a local knowledge base client. It supports txt md and pdf.")


@pytest.fixture
def scanned_pdf() -> bytes:
    return make_empty_pdf()


def make_pdf_multi(pages: list[str]) -> bytes:
    """构造一个多页最小可解析 PDF（只支持 latin-1 文本）。

    每页一个内容流，页间用独立 Page 对象；供「跨页切块 / 页码归属」测试。
    构造手法与 :func:`make_pdf` 一致（单字体 Helvetica + 单文本行）。
    """

    def esc(value: str) -> str:
        return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [] /Count 0 >>",  # 占位，下方填 Kids
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    # 对象编号：1=catalog 2=pages 3=font 4=第1页Page 5=第1页Content 6=第2页Page ...
    page_ids: list[int] = []
    next_id = 4
    for text in pages:
        content = f"BT /F1 12 Tf 72 720 Td ({esc(text)}) Tj ET".encode("latin-1")
        page_id = next_id
        content_id = next_id + 1
        next_id += 2
        page_ids.append(page_id)
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>".encode()
        )
        objs.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objs[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF"
    ).encode()
    return bytes(out)


@pytest.fixture
def pdf_two_pages() -> bytes:
    """两页 PDF：第一页含整篇唯一的 ``HEADERUNIQUE`` 标记，便于验证页码归属。"""
    return make_pdf_multi(
        [
            "HEADERUNIQUE First page body text alpha beta gamma delta.",
            "SECONDPAGE Second page body text epsilon zeta eta theta.",
        ]
    )
