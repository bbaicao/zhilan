"""关键词分词：索引期与查询期共用的纯函数，保持 U2 单字/双字及停用词口径。"""

from __future__ import annotations

import hashlib
import re
import zlib

_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[a-zA-Z0-9_]+")
_TOKEN = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]|[\s\S]")
_SEGMENT = re.compile(r"[0-9A-Za-z_]+|[\u4e00-\u9fff]+")
_STOPWORDS = set(
    "的了是在和与或有就都也不没很这那哪我你他她它们个之于而且但并被把给让使对从到"
    "为以及其上下里外前后一二三会能要可请吗呢啊吧"
)
_MAX_POSTINGS = 65536
_MAX_POSTING_BYTES = _MAX_POSTINGS * 3 * 10


def is_single_char(token: str) -> bool:
    return bool(_CJK.fullmatch(token))


def tokenize(text: str) -> list[str]:
    """有重复表示词频；标点/空白/英文词切断汉字双字的相邻关系。"""
    out: list[str] = []
    prev_cjk = ""
    for tok in _TOKEN.findall((text or "").lower()):
        if not _CJK.fullmatch(tok):
            prev_cjk = ""
            if _LATIN.fullmatch(tok):
                out.append(tok)
            continue
        if tok not in _STOPWORDS:
            out.append(tok)
        if prev_cjk and prev_cjk not in _STOPWORDS and tok not in _STOPWORDS:
            out.append(prev_cjk + tok)
        prev_cjk = tok
    return out


def segments(text: str) -> list[str]:
    return [seg.lower() for seg in _SEGMENT.findall(text or "") if len(seg) >= 2]


def term_key(term: str) -> bytes:
    """短词直接存 UTF-8；长词用独立前缀及 SHA256，完整词项另存核验。"""
    raw = term.encode("utf-8")
    return b"\x00" + raw if len(raw) <= 31 else b"\x01" + hashlib.sha256(raw).digest()


def posting_terms(key: bytes, long_term: str | None) -> str:
    if key[:1] == b"\x00":
        return key[1:].decode("utf-8")
    if key[:1] != b"\x01" or long_term is None or term_key(long_term) != key:
        raise ValueError("invalid lexical term key")
    return long_term


def pack_postings(rows: list[tuple[int, int, int]], *, owner_id: int) -> bytes:
    """ID 差分；连续且同词频的材料用游程，解码后的每条 posting 保持原样。"""
    if len(rows) > _MAX_POSTINGS:
        raise ValueError("lexical posting count exceeds limit")
    data = bytearray()
    runs = []
    previous = owner_id
    for index, (material_id, body_tf, title_tf) in enumerate(sorted(rows)):
        if (
            material_id < previous or (index and material_id == previous)
            or min(body_tf, title_tf) < 0 or not (body_tf or title_tf)
            or max(material_id, body_tf, title_tf) > (1 << 64) - 1
        ):
            raise ValueError("invalid lexical posting")
        if (
            runs and material_id == previous + 1
            and body_tf == runs[-1][2] and title_tf == runs[-1][3]
        ):
            runs[-1][1] += 1
        else:
            runs.append([material_id, 1, body_tf, title_tf])
        for value in (material_id - previous, body_tf, title_tf):
            while value >= 128:
                data.append((value & 127) | 128)
                value >>= 7
            data.append(value)
        previous = material_id
    raw = bytes(data)
    compressed = zlib.compress(raw)
    packed_runs = bytearray()
    previous = owner_id
    for first_id, count, body_tf, title_tf in runs:
        for value in (first_id - previous, count, body_tf, title_tf):
            while value >= 128:
                packed_runs.append((value & 127) | 128)
                value >>= 7
            packed_runs.append(value)
        previous = first_id + count - 1
    # ponytail: 来源/版本内常见词重复只保存一次 tf；不跨组，更新仍有原来的组边界。
    return min(
        (b"\x00" + raw, b"\x01" + compressed, b"\x02" + bytes(packed_runs)),
        key=len,
    )


def unpack_postings(payload: bytes, *, owner_id: int) -> list[tuple[int, int, int]]:
    encoding = payload[:1]
    if encoding in (b"\x00", b"\x02"):
        data = payload[1:]
    elif encoding == b"\x01":
        decoder = zlib.decompressobj()
        try:
            data = decoder.decompress(payload[1:], _MAX_POSTING_BYTES + 1)
        except zlib.error as exc:
            raise ValueError("invalid lexical posting compression") from exc
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError("invalid lexical posting compression")
    else:
        raise ValueError("invalid lexical posting encoding")
    if len(data) > _MAX_POSTING_BYTES:
        raise ValueError("lexical posting bytes exceed limit")
    values = []
    value = shift = 0
    for byte in data:
        value |= (byte & 127) << shift
        if byte & 128:
            shift += 7
            if shift > 63:
                raise ValueError("invalid lexical posting integer")
        else:
            if value > (1 << 64) - 1:
                raise ValueError("invalid lexical posting integer")
            values.append(value)
            value = shift = 0
    width = 4 if encoding == b"\x02" else 3
    if shift or len(values) % width:
        raise ValueError("truncated lexical posting")
    rows = []
    material_id = owner_id
    for start in range(0, len(values), width):
        if width == 4:
            delta, count, body_tf, title_tf = values[start:start + width]
        else:
            delta, body_tf, title_tf = values[start:start + width]
            count = 1
        material_id += delta
        if not count or not (body_tf or title_tf) or (rows and rows[-1][0] == material_id):
            raise ValueError("invalid lexical posting")
        if len(rows) + count > _MAX_POSTINGS:
            raise ValueError("lexical posting count exceeds limit")
        if material_id + count - 1 > (1 << 64) - 1:
            raise ValueError("invalid lexical posting integer")
        rows.extend((material_id + offset, body_tf, title_tf) for offset in range(count))
        material_id += count - 1
    return rows
