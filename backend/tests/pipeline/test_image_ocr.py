"""条目 3.1 / 3.2 / 3.3 / 3.4 回归用例：六种图片格式走 MinerU OCR 入库。

纪律（统筹 §3.4）：**全部 stub 化** —— monkeypatch ``mineru.parse_image`` / ``mineru.available``，
或注入假 ``mineru`` / ``mineru.parser`` 模块；**不连真模型、不下载权重、不联网**。
Pillow 只用于**现场造图片 bytes** 与验证「图片包单页 PDF」这条兜底路径，不触发任何模型。

覆盖点（对应阶段 3 文档 §二 的 5 条 + §四 验收标准 4/5/6）：
① ``MINERU_ENABLE=True`` + 图片 → 调用 ``mineru.parse_image`` 并成功返回；
② ``MINERU_ENABLE=False`` + 图片 → 报「图片 OCR 未启用」可读错误；
③ ``OCR_IMAGE_ENABLE=False`` + 图片 → 报「图片 OCR 已关闭」；
④ MinerU 图片 OCR 抛异常 → 转译不炸穿；
⑤ 文件夹扫描认 jpg / png 为可入库格式（进 files，不进 skipped）；
⑥ 六种后缀各有覆盖（jpg / jpeg / png / bmp / tiff / webp）；
⑦ 原生图片入口（MinerU 4.0 自己把图片包成单页 PDF）与「自己包 PDF」兜底路径各自的取用口径；
⑧ 端点提示文案与真实白名单一致（条目 3.3）。
"""

from __future__ import annotations

import sys
import types
from io import BytesIO
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.errors import DocumentParseError
from app.pipeline import folder_scan as folder_scan_mod
from app.pipeline import mineru, parsing
from app.pipeline.parsing import (
    IMAGE_OCR_DISABLED_HINT,
    IMAGE_OCR_OFF_HINT,
    SUPPORTED_FORMATS_HINT,
    SUPPORTED_SUFFIXES,
)
from tests.pipeline.conftest import make_png

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp")

# 兜底路径会把图片重编码，故这些字节只需「魔数正确」——不解码真图也能覆盖入口分发。
FAKE_MAGIC = {
    ".jpg": b"\xff\xd8\xff\xe0fake-jpeg-body",
    ".jpeg": b"\xff\xd8\xff\xe1fake-jpeg-body",
    ".png": b"\x89PNG\r\n\x1a\nfake-png-body",
    ".bmp": b"BMfake-bmp-body",
    ".tiff": b"II*\x00fake-tiff-body",
    ".webp": b"RIFF\x00\x00\x00\x00WEBPfake-webp-body",
}


@pytest.fixture(autouse=True)
def _clean_heavy_modules():
    """每个用例前后清掉注入的假重依赖模块**与阶段 4 的后端探测缓存**，避免用例互相污染。"""

    def _clean():
        for name in ("mineru", "mineru.parser", "magic_pdf"):
            sys.modules.pop(name, None)
        mineru._reset_backend_cache()

    _clean()
    yield
    _clean()


def _enable_mineru(monkeypatch, *, image_ocr: bool = True):
    """打开总开关（图片开关可按需关闭）。"""
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    monkeypatch.setattr(settings, "OCR_IMAGE_ENABLE", image_ocr)


def _stub_parse_image(monkeypatch, *, result=("OCR 出来的文字", []), exc: Exception | None = None) -> list:
    """把 `mineru.parse_image` 换成桩（`available()` 一并放行），记录调用入参。"""
    calls: list[bytes] = []
    monkeypatch.setattr(mineru, "available", lambda: (True, "MinerU 可用"))

    def fake_parse_image(data: bytes):
        calls.append(data)
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(mineru, "parse_image", fake_parse_image)
    return calls


def _pass_available(monkeypatch):
    """只放行 `available()`，`parse_image` 仍是真实现（用来看它自己的口径，如异常转译）。"""
    monkeypatch.setattr(mineru, "available", lambda: (True, "MinerU 可用"))


def _stub_image_facade(monkeypatch, *, markdown: str = "图片里的第一行\n\n图片里的第二行", seen: dict | None = None):
    """注入一个「4.0 形态」的假 mineru 包：`mineru.parser.parse` 是唯一的解析入口。

    图片是否被正确按后缀落临时文件，由 `seen["suffix"]` 观测（MinerU 就靠后缀判定输入类型）。
    """
    seen = seen if seen is not None else {}

    class _Result:
        pages: list = []

        def markdown(self, *, add_markers: bool = False) -> str:  # noqa: ARG002
            return markdown

    def _parse(path, **kwargs):
        seen["suffix"] = path.suffix
        seen["exists_during_call"] = path.exists()
        seen["bytes"] = path.read_bytes()
        seen["tier"] = kwargs.get("tier")
        return _Result()

    pkg = types.ModuleType("mineru")
    pkg.__path__ = []
    sub = types.ModuleType("mineru.parser")
    sub.parse = _parse
    pkg.parser = sub
    monkeypatch.setitem(sys.modules, "mineru", pkg)
    monkeypatch.setitem(sys.modules, "mineru.parser", sub)
    return seen


def _real_image_bytes(fmt: str, *, size: tuple[int, int] = (80, 60)) -> bytes:
    """用 Pillow 现场造一张真图（不落盘、不联网）。"""
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", size, (240, 240, 240)).save(buffer, format=fmt)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# ① 开总开关 + 图片 → 走 mineru.parse_image 成功返回
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("suffix", IMAGE_SUFFIXES)
def test_enabled_image_calls_parse_image_and_returns_text(monkeypatch, suffix):
    _enable_mineru(monkeypatch)
    calls = _stub_parse_image(monkeypatch, result=("识别到的中文内容", []))

    text, pages = parsing.parse_document_pages(f"photo{suffix}", FAKE_MAGIC[suffix])

    assert text == "识别到的中文内容"
    assert calls == [FAKE_MAGIC[suffix]]
    # 图片没有页边界信息：与 txt / md 及五种 Office 格式口径一致（阶段 3 §二 条目 3.1 规格 2）
    assert pages == []


def test_image_uses_registry_dispatch_not_pdf_branch(monkeypatch):
    """图片走注册表（与 Office 同路径），不碰 PDF 分流；`_PARSERS` 六项都指向 `_parse_image`。"""
    for suffix in IMAGE_SUFFIXES:
        assert parsing._PARSERS[suffix] is parsing._parse_image
    _enable_mineru(monkeypatch)
    _stub_parse_image(monkeypatch, result=("文本", []))
    assert parsing.parse_document("a.png", FAKE_MAGIC[".png"]) == "文本"


# ---------------------------------------------------------------------------
# ② / ③ 两个开关的提示语（顺序即优先级）
# ---------------------------------------------------------------------------
def test_mineru_disabled_reports_image_ocr_not_enabled(monkeypatch):
    monkeypatch.setattr(settings, "MINERU_ENABLE", False)
    with pytest.raises(DocumentParseError) as excinfo:
        parsing.parse_document("photo.png", make_png())
    assert str(excinfo.value) == IMAGE_OCR_DISABLED_HINT
    # 关时绝不 import 重依赖（不变量 7）
    assert "mineru" not in sys.modules


def test_both_switches_off_reports_not_enabled_first(monkeypatch):
    """两个开关都关 → 报「未启用」（总开关是更靠前的原因），不报「已关闭」。"""
    monkeypatch.setattr(settings, "MINERU_ENABLE", False)
    monkeypatch.setattr(settings, "OCR_IMAGE_ENABLE", False)
    with pytest.raises(DocumentParseError) as excinfo:
        parsing.parse_document("photo.png", make_png())
    assert str(excinfo.value) == IMAGE_OCR_DISABLED_HINT
    assert "已关闭" not in str(excinfo.value)


def test_image_switch_off_reports_image_ocr_off(monkeypatch):
    _enable_mineru(monkeypatch, image_ocr=False)
    calls = _stub_parse_image(monkeypatch)
    with pytest.raises(DocumentParseError) as excinfo:
        parsing.parse_document("photo.png", make_png())
    assert str(excinfo.value) == IMAGE_OCR_OFF_HINT
    assert calls == []  # 图片开关关时不该调用 MinerU


def test_image_switch_off_does_not_affect_pdf_scan_path(monkeypatch, scanned_pdf):
    """图片开关只管图片这一路：关掉后 PDF 的扫描件分流照常走 MinerU。"""
    from tests.pipeline.conftest import make_pdf

    _enable_mineru(monkeypatch, image_ocr=False)
    monkeypatch.setattr(mineru, "available", lambda: (True, "MinerU 可用"))
    monkeypatch.setattr(mineru, "parse_pdf", lambda data: ("PDF OCR 文本", ["PDF OCR 文本"]))

    assert parsing.parse_document_pages("scan.pdf", scanned_pdf) == ("PDF OCR 文本", ["PDF OCR 文本"])
    # 原生 PDF 仍走 pypdf 快提，不受这两个开关影响
    native = make_pdf("native pdf text")
    text, pages = parsing.parse_document_pages("native.pdf", native)
    assert "native pdf text" in text and pages == [text]


def test_mineru_enabled_but_absent_reports_readable_error(monkeypatch):
    """装了开关但没装 MinerU → 中文可读错（附 available() 给的原因），不裸异常。"""
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    monkeypatch.setattr(settings, "OCR_IMAGE_ENABLE", True)
    with pytest.raises(DocumentParseError) as excinfo:
        parsing.parse_document("photo.png", make_png())
    message = str(excinfo.value)
    assert message.startswith(IMAGE_OCR_DISABLED_HINT)
    assert "未安装" in message
    assert "mineru" not in sys.modules  # import 失败的模块不该留在 sys.modules


# ---------------------------------------------------------------------------
# ④ 异常转译：不炸穿
# ---------------------------------------------------------------------------
def test_parse_image_exception_translated_to_chinese_error(monkeypatch):
    _enable_mineru(monkeypatch)
    _pass_available(monkeypatch)
    # 原生入口运行期异常：本版本没有图片入口 → 兜底 → 兜底也抛运行期异常
    def _boom(data: bytes, filename: str, seen: dict | None = None):
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(mineru, "_image_entrypoints", _boom)
    monkeypatch.setattr(
        mineru, "_parse_pdf_entrypoints", lambda data: (_ for _ in ()).throw(RuntimeError("CUDA out of memory"))
    )

    with pytest.raises(DocumentParseError) as excinfo:
        parsing.parse_document("photo.png", make_png())
    assert "MinerU 图片 OCR 失败" in str(excinfo.value)
    assert "CUDA out of memory" in str(excinfo.value)


def test_native_entry_runtime_error_is_not_silently_retried_on_pdf_route(monkeypatch):
    """原生入口的**运行期**异常直接转译，不再换条路重解析（与 PDF 四入口同一纪律）。"""
    _enable_mineru(monkeypatch)
    _pass_available(monkeypatch)
    pdf_route_called: list[int] = []

    def _boom(data: bytes, filename: str, seen: dict | None = None):
        raise RuntimeError("模型权重损坏")

    def _pdf_route(data: bytes):
        pdf_route_called.append(1)
        return "不该走到这里", []

    monkeypatch.setattr(mineru, "_image_entrypoints", _boom)
    monkeypatch.setattr(mineru, "_parse_pdf_entrypoints", _pdf_route)

    with pytest.raises(DocumentParseError) as excinfo:
        parsing.parse_document("photo.png", make_png())
    assert "模型权重损坏" in str(excinfo.value)
    assert pdf_route_called == []


def test_both_routes_unavailable_reports_both_reasons(monkeypatch):
    """两条路都没有入口时的报错口径：一条外层前缀 + **两条路各自的原因**。

    2026-09-18 独立复核 C6 之后的措辞修正：兜底链把「入口不存在」自己汇总成 `RuntimeError`
    （「API 不匹配」+ 逐入口原因），所以原先那条 `except _EntryUnavailable` 分支**不可达**、已删除；
    现在的报错是 `MinerU 图片 OCR 失败：<兜底链原因>（原生图片入口：<原生缺席原因>）`，
    两条原因都在，用户能看出「图片原生入口也试过了」。
    """
    _enable_mineru(monkeypatch)
    _pass_available(monkeypatch)
    monkeypatch.setattr(
        mineru, "_image_entrypoints", lambda data, filename, seen=None: (_ for _ in ()).throw(mineru._EntryUnavailable("原生：无"))
    )
    monkeypatch.setattr(
        mineru, "_parse_pdf_entrypoints", lambda data: (_ for _ in ()).throw(mineru._EntryUnavailable("兜底：无"))
    )

    with pytest.raises(DocumentParseError) as excinfo:
        parsing.parse_document("photo.png", make_png())
    message = str(excinfo.value)
    assert message.startswith("MinerU 图片 OCR 失败")
    assert "原生图片入口" in message  # 原生侧原因被带出来
    assert "兜底：无" in message  # 兜底侧原因（被 RuntimeError 包在中间）
    # 旧写法把 {exc} 重复贴了两遍，现在只出现一次
    assert message.count("兜底：无") == 1


def test_parse_image_empty_result_is_error_not_silent(monkeypatch):
    """空结果算失败：不让空文本静默流进切块（否则「入库成功但零 chunk」）。"""
    _enable_mineru(monkeypatch)
    _stub_parse_image(monkeypatch, result=("   ", []))

    with pytest.raises(DocumentParseError) as excinfo:
        parsing.parse_document("photo.png", make_png())
    assert str(excinfo.value) == "图片未识别到文字内容。"


def test_parse_image_file_level_empty_guard_on_fallback_route(monkeypatch):
    """**文件层**的空结果守卫（条目 3.2 规格 3）——走兜底路径时必须自己拦，不能靠上层兜。

    为什么单独立这一条（2026-09-18 补，来自变异测试复核 M4）：`_parse_image` 里另有一道兄弟守卫
    （空 → `IMAGE_EMPTY_HINT`），于是上层调用者看起来「空文本反正会被拦住」，**文件层的守卫成了
    没有用例钉的冗余行**——删掉它所有 633 条用例依然全绿（实测 SURVIVED）。
    但它是真实可达的：兜底路径把图片包成单页 PDF 交给 PDF 链后，PDF 链在「取不出渲染文本」时会
    返回 `("", [])`（`mineru._export` 的最后兜底分支），此时 `parse_image` 是**唯一**还站在
    「绝不让空结果静默流进出库」这条纪律上的执行者；任何直接调 `mineru.parse_image` 的调用方
    （不经过 `parsing` 层）都只受它保护。故用本条把文件层契约钉死。

    手法：原生图片入口整体缺席 → 走兜底 → 兜底 PDF 链返回全空 → 必须抛中文可读错。
    """
    monkeypatch.setattr(mineru, "available", lambda: (True, "MinerU 可用"))

    seen: list[bytes] = []

    def _no_native_entry(data: bytes, filename: str, seen_route: dict | None = None):
        raise mineru._EntryUnavailable("注入：本版本没有图片原生入口")

    def _empty_pdf_chain(pdf_bytes: bytes):
        seen.append(pdf_bytes)  # 钉死「兜底路径确实被走到」，否则空结果由别处产生
        return "", []

    monkeypatch.setattr(mineru, "_image_entrypoints", _no_native_entry)
    monkeypatch.setattr(mineru, "_parse_pdf_entrypoints", _empty_pdf_chain)

    with pytest.raises(DocumentParseError) as excinfo:
        mineru.parse_image(make_png())
    message = str(excinfo.value)
    assert message.startswith("MinerU 图片 OCR 失败")
    assert "返回结果为空" in message
    # 两条前置断言：兜底链被调用、且喂进去的是包好的单页 PDF（本用例才真的覆盖到目标行）
    assert len(seen) == 1 and seen[0].startswith(b"%PDF-")


def test_unrecognized_image_bytes_reports_readable_error(monkeypatch):
    """魔数认不出来 → 明确报错，不把非图片字节当 PDF 喂下去。

    2026-09-18 独立复核 C4 的回归栅栏：条提示原先**硬编码且漏了 `.jpeg`**（jpeg 与 jpg 同族，
    提示里少一个会误导用户把一个能用的后缀当成不支持）。现在清单走 `mineru._IMAGE_FORMATS_HINT`，
    这里逐项断言六种后缀都在提示里可见。
    """
    _enable_mineru(monkeypatch)
    _pass_available(monkeypatch)  # 魔数判定在 parse_image 内部，故必须真的走到它
    with pytest.raises(DocumentParseError) as excinfo:
        parsing.parse_document("photo.png", b"definitely not an image")
    message = str(excinfo.value)
    assert "无法识别的图片格式" in message
    assert message.startswith("图片 OCR 失败")
    for suffix in IMAGE_SUFFIXES:  # jpg / jpeg / png / bmp / tiff / webp 六种都要出现
        assert suffix.lstrip(".") in message, suffix


# ---------------------------------------------------------------------------
# ⑦ MinerU 侧入口口径（条目 3.2）
# ---------------------------------------------------------------------------
def test_parse_image_uses_native_image_entry_with_real_suffix(monkeypatch):
    """首选原生图片入口：临时文件后缀 = 真实图片后缀（MinerU 4.0 靠它走图片分支）。"""
    seen = _stub_image_facade(monkeypatch, markdown="图里的字")
    text, pages = mineru.parse_image(make_png())

    assert text == "图里的字"
    assert seen["suffix"] == ".png"
    assert seen["exists_during_call"] is True
    assert seen["bytes"] == make_png()


def test_parse_image_native_entry_keeps_jpeg_and_webp_suffix(monkeypatch):
    """`.jpeg` / `.webp` 也要给真实后缀（MinerU 的 IMAGE_EXTENSIONS 认 jpeg / webp）。

    `.jpg` 与 `.jpeg` 共享同一个 JPEG 魔数，判据里只能取其一（本歧义已在 `_image_format`
    docstring 写明），故这里用**能唯一区分**的格式来钉「后缀不是写死的 .png」：
    JPEG → `.jpg`、WEBP → `.webp`、TIFF → `.tiff`、BMP → `.bmp`。
    """
    for suffix, fmt in ((".jpg", "JPEG"), (".webp", "WEBP"), (".tiff", "TIFF"), (".bmp", "BMP")):
        seen = _stub_image_facade(monkeypatch, markdown="ok")
        mineru.parse_image(_real_image_bytes(fmt))
        assert seen["suffix"] == suffix, fmt


def test_parse_image_falls_back_to_wrapped_pdf_when_no_image_entry(monkeypatch):
    """旧版只有 `mineru.parse(bytes)` 时：图片先包成单页 PDF，再走 PDF 入口（条目 3.2 规格 2）。

    同一个旧版入口两条路都能命中，故除了「拿到了文字」之外，还要断言**命中名字**是兜底路径。
    """
    pkg = types.ModuleType("mineru")
    seen: dict = {}

    def _old_parse(data):
        seen["wrapped"] = data
        return "兜底路径识别到的文字"

    pkg.parse = _old_parse
    monkeypatch.setitem(sys.modules, "mineru", pkg)

    # 原生图片入口整体缺席（真实世界里是「旧版没有图片分支」）
    def _no_image_entry(data: bytes, filename: str, seen: dict | None = None):
        raise mineru._EntryUnavailable("注入：本版本没有图片原生入口")

    monkeypatch.setattr(mineru, "_image_entrypoints", _no_image_entry)

    text, pages = mineru.parse_image(make_png())
    assert text == "兜底路径识别到的文字"
    assert pages == ["兜底路径识别到的文字"]
    assert seen["wrapped"].startswith(b"%PDF-")  # 确实是「图片包成单页 PDF」后喂进去的


def test_wrapped_pdf_path_routes_through_pdf_entrypoints(monkeypatch):
    """兜底路径**复用 PDF 四入口链**（而不是自己再走一遍），且喂进去的是包好的单页 PDF。

    用 stub 顶住 `_parse_pdf_entrypoints`：既钉「原生入口缺席 → 走兜底」，
    又钉「兜底拿到的确实是 PDF（不是原图）」。
    """
    # 原生图片三入口全部缺席：给一个既没有 parser.parse 也没有 parse(bytes) 的空包
    pkg = types.ModuleType("mineru")
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "mineru", pkg)

    seen: dict = {}

    def _fake_pdf_entrypoints(pdf_bytes: bytes):
        seen["pdf_bytes"] = pdf_bytes
        return "兜底路径识别到的文字", ["兜底路径识别到的文字"]

    monkeypatch.setattr(mineru, "_parse_pdf_entrypoints", _fake_pdf_entrypoints)

    text, pages = mineru.parse_image(make_png())
    assert text == "兜底路径识别到的文字"
    assert pages == ["兜底路径识别到的文字"]
    assert seen["pdf_bytes"].startswith(b"%PDF-")


def test_wrapped_pdf_path_accepts_stubbed_pdf_entrypoints(monkeypatch):
    """注入假 `mineru.parser.parse`（4.0 的 PDF 入口）时兜底路径也能走通。

    手法：让**原生图片**入口缺席（包的 `parser.parse` 签名只认 PDF 语义的场景无法直接模拟），
    故这里改为让原生入口抛 `_EntryUnavailable`（parser.parse 不是函数），再观察 PDF 链结果。
    """
    wrapped: list[bytes] = []

    def _fake_pdf_entrypoints(pdf_bytes: bytes):
        wrapped.append(pdf_bytes)
        return "PDF 链结果", ["PDF 链结果"]

    pkg = types.ModuleType("mineru")
    pkg.__path__ = []
    sub = types.ModuleType("mineru.parser")
    sub.parse = None  # 存在但不可调用 → 原生入口判为 _EntryUnavailable
    pkg.parser = sub
    monkeypatch.setitem(sys.modules, "mineru", pkg)
    monkeypatch.setitem(sys.modules, "mineru.parser", sub)
    monkeypatch.setattr(mineru, "_parse_pdf_entrypoints", _fake_pdf_entrypoints)

    assert mineru.parse_image(make_png()) == ("PDF 链结果", ["PDF 链结果"])
    assert len(wrapped) == 1 and wrapped[0].startswith(b"%PDF-")


def test_parse_image_reports_both_routes_when_nothing_available(monkeypatch):
    """两条路都没有入口 → 报错里同时带**两条路**的原因，绝不静默返回空。"""
    pkg = types.ModuleType("mineru")
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "mineru", pkg)

    with pytest.raises(DocumentParseError) as excinfo:
        mineru.parse_image(make_png())
    message = str(excinfo.value)
    assert "MinerU 图片 OCR 失败" in message
    # 两条路各自的原因都在：兜底侧（PDF 四入口全缺席 → 版本 API 不匹配）+ 原生侧（图片三入口全缺席）
    assert "原生图片入口" in message
    assert "mineru.parser.parse" in message and "magic_pdf UNIPipe" in message


def test_image_pdf_wrapper_produces_single_page_pdf():
    """兜底包装产物必须是一份可被 pypdf 读出的单页 PDF。"""
    from pypdf import PdfReader

    wrapped = mineru._image_pdf_bytes(make_png(), "PNG")
    reader = PdfReader(BytesIO(wrapped))
    assert len(reader.pages) == 1


def test_large_image_is_downscaled_before_wrapping():
    """长边超过 2000px 的图片先等比压缩（阶段 3 §六），避免兜底 PDF 冲破上传限制。"""
    from PIL import Image

    big = _real_image_bytes("PNG", size=(4000, 3000))
    encoded = mineru._encoded_image(big, "PNG")
    assert encoded is not None
    image_bytes, encode_format = encoded
    assert encode_format == "PNG"
    with Image.open(BytesIO(image_bytes)) as image:
        assert max(image.size) <= mineru._IMAGE_MAX_SIDE
        # 等比：宽高比不变（容 1px 取整误差）
        assert abs(image.size[0] / image.size[1] - 4000 / 3000) < 0.01


def test_rgba_image_wrapped_to_pdf_and_alpha_composited_on_white():
    """RGBA → PDF 必须能过（Pillow 的 PDF 编码器对部分模式会直接 ValueError），且透明区按白底合成。"""
    from PIL import Image

    # 半透明红 + 全透明角落：全透明区若被简单丢 alpha 会变黑，这里要求是白
    rgba = Image.new("RGBA", (20, 20), (255, 0, 0, 0))
    for x in range(10):
        for y in range(10):
            rgba.putpixel((x, y), (255, 0, 0, 255))
    buffer = BytesIO()
    rgba.save(buffer, format="PNG")

    wrapped = mineru._image_pdf_bytes(buffer.getvalue(), "PNG")
    assert wrapped.startswith(b"%PDF-")

    # 白底合成口径：透明格 → 接近纯白；实体格 → 保持原色
    composited = mineru._to_rgb(rgba)
    assert composited.getpixel((15, 15)) == (255, 255, 255)
    assert composited.getpixel((2, 2)) == (255, 0, 0)


# ---------------------------------------------------------------------------
# ⑤ 文件夹扫描：六种图片进 files（条目 3.3 / 验收标准 5）
# ---------------------------------------------------------------------------
def test_scan_folder_recognizes_image_formats(tmp_path):
    (tmp_path / "照片.png").write_bytes(make_png())
    (tmp_path / "截图.jpg").write_bytes(_real_image_bytes("JPEG"))
    (tmp_path / "说明.txt").write_text("正文", encoding="utf-8")
    (tmp_path / "不该收.doc").write_bytes(b"x")

    result = folder_scan_mod.scan_folder(str(tmp_path))
    names = {item.name for item in result.files}
    skipped = {item.name: item.reason for item in result.skipped}

    assert {"照片.png", "截图.jpg", "说明.txt"} <= names
    assert "不该收.doc" not in names
    assert "不支持" in skipped["不该收.doc"]
    # 跳过原因文案与实际白名单一致（条目 3.3 规格 2）
    assert "jpg" in skipped["不该收.doc"] and "webp" in skipped["不该收.doc"]


def test_all_six_suffixes_are_whitelisted():
    for suffix in IMAGE_SUFFIXES:
        assert suffix in SUPPORTED_SUFFIXES, suffix
    # 原 9 种 + 图片 6 种 + 工具升级-3 · 回归检查 的代码 16 / 配置 7 / 前端资源 6
    assert len(SUPPORTED_SUFFIXES) == 44


def test_config_defaults_are_the_specified_ones():
    """条目 3.4 规格 1：「新增配置 `OCR_IMAGE_ENABLE: bool = True`」——默认值必须被用例钉住。

    为什么单独钉默认值（2026-09-18 补，来自独立复核的实测）：本文件其余用例都**显式
    monkeypatch** `OCR_IMAGE_ENABLE`（这是必要的，否则用例相互依赖），于是**没有任何用例读它的默认值**
    ——把默认值改成 `False` 时 633 条用例全绿（变异存活）。默认值是「开箱即用」这一产品承诺的载体
    （总开关一开、图片即可用），且属阶段规格明文，故用一条直读 settings 的断言当栅栏。

    注意：其余用例若也断言默认值，会与 `MINERU_ENABLE=False` 的既有默认耦合；这里**只读不写**，
    不做任何 monkeypatch，跑在真实默认配置上。
    """
    assert settings.OCR_IMAGE_ENABLE is True
    # 默认档下图片仍应是「明确报错」而非静默（与既有默认 MINERU_ENABLE=False 一致）
    assert settings.MINERU_ENABLE is False
    # 阶段 4 · 条目 4.1（2026-09-18 加）：VLM 件是「用户自备」项，默认必须**关**——
    # §0.3 定案要求开箱即用不依赖任何许可未明的件（GGUF / mineru-llama-cpp）；
    # 默认值若被改成 True，全量用例会照样全绿（与本用例开头记录的那类盲区同源）。
    assert settings.MINERU_VLM_ENABLE is False
    # 后端默认 auto：在「VLM 关 + 未自备件」的环境里等价于 pipeline（阶段 4 条目 4.2）
    assert settings.MINERU_BACKEND == "auto"


# ---------------------------------------------------------------------------
# ⑧ 端点提示文案（条目 3.3）
# ---------------------------------------------------------------------------
def test_error_hint_text_lists_all_supported_formats():
    for suffix in IMAGE_SUFFIXES:
        assert suffix.lstrip(".") in SUPPORTED_FORMATS_HINT
    for suffix in (".txt", ".md", ".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".csv"):
        assert suffix.lstrip(".") in SUPPORTED_FORMATS_HINT


def test_hint_text_only_mentions_whitelisted_formats():
    """提示文案里的每个后缀都必须在白名单里，且**除 `.markdown` 别名外一个不漏**。

    注意白名单比文案多一个 `.markdown`：文案历来只写扩展名主形态（`.md`），
    它是同一格式的别名，不是「漏列的格式」。两条断言合起来即「文案 ↔ 白名单」双向一致，
    避免以后加格式只改一处。
    """
    mentioned = {f".{token.strip()}" for token in SUPPORTED_FORMATS_HINT.split("/")}
    assert mentioned <= SUPPORTED_SUFFIXES, sorted(mentioned - SUPPORTED_SUFFIXES)
    assert mentioned == SUPPORTED_SUFFIXES - {".markdown"}, sorted(SUPPORTED_SUFFIXES - mentioned)


def test_pillow_is_not_imported_at_module_level():
    """不变量 7 的图片版：Pillow 只允许在兜底路径里惰性 import，模块顶层不得拉进来。"""
    import ast

    from app.pipeline import parsing as parsing_mod

    for module in (mineru, parsing_mod):
        source = Path(module.__file__).read_text(encoding="utf-8")
        top_level_names: set[str] = set()
        for node in ast.parse(source).body:
            if isinstance(node, ast.Import):
                top_level_names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level_names.add(node.module.split(".")[0])
        assert "PIL" not in top_level_names, module.__file__


def test_import_file_endpoint_hint_matches_whitelist(tmp_path):
    """条目 3.3：import-file 的 400 文案必须列出全部格式（含图片）。"""
    from fastapi import HTTPException

    from app.api.document_routes import import_file_endpoint

    bad = tmp_path / "a.exe"
    bad.write_bytes(b"x")
    body = types.SimpleNamespace(path=str(bad))

    import asyncio

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(import_file_endpoint(body, db=None))  # 校验早于用 db，故 db=None 不触发
    detail = excinfo.value.detail
    assert excinfo.value.status_code == 400
    assert SUPPORTED_FORMATS_HINT in detail


# 端点层的两条端到端用例（`.png` 经 POST /api/documents/upload 的 400 文案与「不拦后缀」）
# 已拆到 `tests/api/test_documents_upload_image.py`——它们需要完整 ASGI 栈（含 get_db 覆盖），
# 与纯 pipeline 的本文件职责不同；本文件保持「不连 DB、不建 ASGI」的纪律。
