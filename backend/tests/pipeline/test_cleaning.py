"""数据清洗流水线对拍（老仓库 test_cleaning.py 离线用例）。

只对拍 `clean` / `rule_fingerprint` 纯函数路径；
需容器的入库/dry-run/去重/覆盖重建用例随 2.4 ingest 迁入。
页眉页脚用例改为 `parse_document_pages + clean`（老仓库走 clean_preview，
ingest 未移植前用解析层等价路径，检测/删除行为同一实现）。
"""

import binascii

from app.core.config import settings
from app.pipeline.cleaning import clean, rule_fingerprint
from app.pipeline.parsing import parse_document_pages


def _paged_pdf(pages: list[str]) -> bytes:
    """构造多页 PDF：每页一个内容流，用于跨页重复行（页眉页脚）检测。

    中文用 Type0 字体的十六进制字符串写（``<FEFF...>``），避免 latin-1 编码限制。
    """
    objects: list[bytes] = []
    contents: list[bytes] = []
    for text in pages:
        lines = text.split("\n")
        ops = ["BT /F1 12 Tf 72 720 Td 14 TL"]
        for index, line in enumerate(lines):
            if index:
                ops.append("T*")
            payload = binascii.hexlify(b"\xfe\xff" + line.encode("utf-16-be")).decode("ascii").upper()
            ops.append(f"<{payload}> Tj")
        ops.append("ET")
        contents.append(" ".join(ops).encode("latin-1"))

    # 对象编号：1=Catalog, 2=Pages, 3=Font, 之后每页两个对象（Page, Contents）
    kids: list[str] = []
    page_ids: list[tuple[int, int]] = []
    next_id = 4
    for _ in pages:
        kids.append(f"{next_id} 0 R")
        page_ids.append((next_id, next_id + 1))
        next_id += 2

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>".encode())
    # Type0 + Identity-H：让 pypdf 能按 UTF-16BE 解出中文
    objects.append(
        b"<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light "
        b"/Encoding /Identity-H /DescendantFonts [6 0 R] >>"
    )

    body: dict[int, bytes] = {}
    for (page_id, content_id), content in zip(page_ids, contents):
        body[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode()
        body[content_id] = b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"
    # 依附字体（Identity-H 需要），编号固定取 body 之外的最大值 +1
    desc_id = max([1, 2, 3] + list(body.keys())) + 1
    body[desc_id] = (
        f"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light "
        f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> >>"
    ).encode()

    order = [1, 2, 3] + sorted(body.keys())
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for object_id in order:
        payload = objects[object_id - 1] if object_id <= 3 else body[object_id]
        offsets[object_id] = len(out)
        out += f"{object_id} 0 obj\n".encode() + payload + b"\nendobj\n"

    xref_pos = len(out)
    total = max(offsets) + 1
    out += f"xref\n0 {total}\n".encode()
    out += b"0000000000 65535 f \n"
    for object_id in range(1, total):
        out += f"{offsets.get(object_id, 0):010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {total} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF").encode()
    return bytes(out)


def _clean_pdf(filename: str, data: bytes):
    """解析 PDF 后走同一条 clean 路径（老仓库 clean_preview 的纯函数等价）。"""
    text, pages = parse_document_pages(filename, data)
    return clean(text, pages=pages)


# --------------------------------------------------------------------------
# 回归检查 页眉页脚
# --------------------------------------------------------------------------

def test_header_footer_removed_from_pdf():
    """回归检查：5 页 PDF 每页首行相同（≤40 字），清洗后该行不出现。"""
    header = "知澜内部资料 v1.0"
    pages = [f"{header}\n第{i}页正文A：这是本页独有的内容。\n第{i}页正文B：继续说明。" for i in range(5)]
    data = _paged_pdf(pages)

    result = _clean_pdf("报告.pdf", data)

    assert header not in result.text, "重复出现的页眉必须被删掉"
    assert all(f"第{i}页正文A" in result.text for i in range(5)), "正文必须完整保留"
    step = next(item for item in result.steps if item.name == "header_footer")
    assert header in step.detail["patterns"]


def test_single_page_keeps_first_line():
    """只有一页时不做页眉页脚判定——否则会把正文首行整行删掉。"""
    result = _clean_pdf("单页.pdf", _paged_pdf(["唯一的首行就是正文\n第二行内容"]))
    assert "唯一的首行就是正文" in result.text


def test_long_repeated_line_is_not_header():
    """超过 CLEAN_MAX_HEADER_LEN 的重复长行不判为页眉（正文段落可能天然重复）。"""
    long_line = "这是一段很长的正文内容" * 5  # 50 字 > 默认上限 40
    pages = [f"{long_line}\n正文{i}" for i in range(5)]
    result = clean("\n".join(pages), pages=pages)
    assert long_line in result.text, "超长的重复行属于正文，不能删"


# --------------------------------------------------------------------------
# 回归检查 重复段落
# --------------------------------------------------------------------------

def test_duplicate_paragraphs_preserve_body():
    """U1.1：连续重复不构成可删除的证据。"""
    para = "同一段内容连续出现了很多次，仍须完整保留。"
    text = "\n".join([para] * 5)
    result = clean(text)
    assert result.text.count(para) == 5


def test_duplicate_blocks_with_blank_lines_preserve_body():
    """U1.1：分散出现的重复业务事实也完整保留。"""
    text = "开头。\n\n重复的一整段。\n\n重复的一整段。\n\n重复的一整段。\n\n结尾。"
    result = clean(text)
    assert result.text.count("重复的一整段。") == 3
    assert "开头。" in result.text and "结尾。" in result.text


# --------------------------------------------------------------------------
# 回归检查 控制符与乱码
# --------------------------------------------------------------------------

def test_control_chars_removed_garbled_flagged():
    """回归检查：控制符被剔除；乱码段落被标记而非删除；raw_content 保留原文。"""
    text = "正常开头\x00\x01继续写\ufffd\ufffd\ufffd\ufffd\ufffd这里是乱码\n正文收尾"
    result = clean(text)

    assert "\x00" not in result.text and "\x01" not in result.text, "C0 控制符必须剔除"
    assert "\ufffd" in result.text, "乱码段落只标记不删（判不准的内容不能偷偷丢）"
    assert result.flagged, "clean_report.flagged 必须非空"
    assert result.flagged[0]["reason"] == "garbled_text"
    assert result.raw == text, "raw 必须原样保留原文"


def test_repeat_chars_collapsed():
    """连续同一字符超过上限则折叠。"""
    result = clean("标题\n--------------------\n正文")
    assert "---------------------" not in result.text
    assert "正文" in result.text


def test_numbers_and_business_identifiers_are_not_shortened():
    text = (
        "金额：1000000000元\n账号：6222222222222222222\n"
        "序号：000000000000000001\n业务标识：AB----------0000000000\n"
        "字面串：AAAAAAAAAAAAAAAA"
    )
    assert clean(text).text == text


# --------------------------------------------------------------------------
# 回归检查 幂等
# --------------------------------------------------------------------------

def test_cleaning_is_idempotent():
    """回归检查：同一文本连续清洗 3 次结果完全一致。"""
    body = (
        "知澜内部资料 v1.0\n"
        "开头\x00正文\n"
        "分隔线--------------------\n"
        "重复段落。\n重复段落。\n重复段落。\n重复段落。\n"
        "乱码\ufffd\ufffd\ufffd\ufffd\ufffd结束\n"
        "表格  列一  列二"
    )
    first = clean(body).text
    second = clean(first).text
    third = clean(second).text
    assert first == second == third, f"清洗必须幂等\n1={first!r}\n2={second!r}"


# --------------------------------------------------------------------------
# 整改回归（老仓库复验缺陷，防止再犯）
# --------------------------------------------------------------------------


def test_header_removed_even_when_duplicate_step_disabled(monkeypatch):
    """回归检查 的保证必须由 S3 独立完成，不能靠 S4 顺手删掉。

    页眉内部含 2+ 连续空白时，检测域（原始分页文本）与删除域（空白归一后的行）
    会不一致：旧实现报告里写着 patterns 命中，正文却一行没删，只有 S4 开着才「看起来对」。
    """
    monkeypatch.setattr(settings, "CLEAN_DUPLICATE_PARA", False)
    header = "知澜  内部资料 v1.0"  # 双空格：归一化会压成单空格
    pages = [f"{header}\n第{i}页正文。" for i in range(5)]

    result = clean("\n".join(pages), pages=pages)

    assert "知澜 内部资料 v1.0" not in result.text, "页眉必须真的被删掉，而不是只记进 patterns"
    assert all(f"第{i}页正文。" in result.text for i in range(5)), "正文必须完整保留"
    step = next(item for item in result.steps if item.name == "header_footer")
    assert step.detail["patterns"] and step.lines_removed >= 5


def test_rule_fingerprint_follows_master_switch(monkeypatch):
    """总开关也是规则：CLEAN_ENABLE 一变指纹就得变。

    否则「关掉清洗入库的文档」永远拿着当前指纹，重新打开清洗后也不会被提示重建。
    """
    before = rule_fingerprint()
    monkeypatch.setattr(settings, "CLEAN_ENABLE", not settings.CLEAN_ENABLE)

    assert rule_fingerprint() != before, "CLEAN_ENABLE 必须参与规则指纹"


# --------------------------------------------------------------------------
# 关键正则/常量一字不改（04 文档 ★★★ 锁死项）
# --------------------------------------------------------------------------


def test_frozen_regexes_match_source():
    """表格分隔符与空白归一正则必须与老仓库逐字节一致。"""
    from app.pipeline import cleaning as mod

    assert mod._TABLE_SPLIT.pattern == r"[ \u3000]{2,}|\t+"
    assert mod._TABLE_ROW.pattern == r"[ \u3000]{2,}|\t"
    assert mod._INLINE_WS.pattern == r"[ \t\u3000]{2,}"
    assert mod._BLANK_RUN.pattern == r"\n{2,}"
    assert mod._CONTROL.pattern == r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]"
    assert mod._REPLACEMENT_RUN.pattern == r"\ufffd{4,}"
    assert mod._TABLE_SEP == " | "
    assert mod._TABLE_MIN_COLUMNS == 2
    assert mod._HEAD_TAIL_LINES == 3


def test_normalize_keeps_single_space():
    """空白归一只折叠 ≥2 连续空白：单个空格/tab 必须活下来（否则表格降级产物被吃掉）。"""
    from app.pipeline.cleaning import normalize_blank_lines

    assert normalize_blank_lines("a b") == "a b"
    assert normalize_blank_lines("a\tb") == "a\tb"  # 单个 tab 不到阈值，保留
    assert normalize_blank_lines("a  b") == "a b"
    assert normalize_blank_lines("a \t b") == "a b"  # 2+ 混合空白折叠成一个空格
    assert normalize_blank_lines("a\u3000\u3000b") == "a b"  # 全角空格同样
