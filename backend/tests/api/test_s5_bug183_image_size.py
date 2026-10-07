"""S5 · 回归修复 载体：图片体积判据按「**解码后字节数**」而非 base64 串长。

缺陷（handoff §2.6）：``backend/app/api/chat_routes.py:349-351`` 直接拿
``len(image) > MAX_UPLOAD_BYTES`` 判超限，而前端送的是 ``data:image/...;base64,...``
**data URL**（``backend/app/generation/answer.py:792-808`` ``_image_content`` 的分支一）——
base64 膨胀 4/3，15 MiB 原图的 data URL 足有 20 971 542 字符 ⇒ 被误判 413（真机大图问答失败）。

判据（逐字照 handoff，不得放宽）：
① 15 MiB 原图（data URL 形态）不再 413，且装配缝拿到的仍是**裸 base64**；
② 真超限（解码后 = ``MAX_UPLOAD_BYTES`` + 1 字节）仍 413、文案逐字；
③ 裸 base64 与旧字段 ``image_data`` 同口径（都按解码后字节数）；
④ 畸形 ``data:`` 头（无逗号，如 ``data:image/png;base64`` / ``data:``）不得 500
   （复核 must-fix：改前 ``split(",", 1)[1]`` 会 IndexError ⇒ 未处理 500）。

反向保护（防「为变绿放宽」）：
- 腿 a2 是真超限 **data URL** ⇒ 把判据改成「只要带 ``data:`` 前缀就跳过体积检查」会当场红；
- 腿 a3 的 payload 串长 25 165 824 字符 > 上限而解码后 18 MiB < 上限 ⇒
  把「整串长」偷换成「payload 串长」的半修法照样红；
- 腿 a6 钉住严格 ``>``（== 上限放行、上限 + 1 拒绝）与 ``==`` / ``=`` 两种 padding 的估算算术；
- 腿 a5 钉住旧字段 ``image_data`` 不走第二条判据（回落口径与 ``images`` 逐字一致）；
- 腿 b1 钉住畸形 ``data:`` 头不抛（把 ``partition`` 换回 ``split(",", 1)[1]`` 即当场红）。

挂账（UNVERIFIED）：真机前端 15 MiB 原图端到端未实跑，本载体只用合成字节证明**判据层**；
02 契约的上限文字修订归 S6（本阶段不改契约文档）。

夹具沿用**既有先例** ``tests/api/test_bug101_vision_wiring.py`` 的 ``_install_vision``
（跨文件导入是仓库既有做法：该文件自己就 ``from tests.api.test_chat_contract import ...``），
从而不依赖本机 ``.env`` 的视觉角色配置、也绝不出网。
"""

from __future__ import annotations

import base64

import pytest

from app.api import chat_routes
from app.api import document_routes
from tests.api.test_bug101_vision_wiring import _install_vision
from tests.api.test_chat_contract import client  # noqa: F401 —— 夹具跨文件复用（既有先例）

MIB = 1024 * 1024
FIFTEEN_MIB = 15 * MIB  # 15 728 640 字节
TOO_LARGE_DETAIL = "文件超过限制，请压缩后重试"


def _b64(nbytes: int) -> str:
    """合成 nbytes 字节的**规范** base64（内容与判据无关，载荷统一 0x5A）。"""
    return base64.b64encode(b"\x5a" * nbytes).decode("ascii")


def _data_url(nbytes: int, media_type: str = "image/png") -> str:
    return f"data:{media_type};base64,{_b64(nbytes)}"


# --- 腿 a1：15 MiB 原图（data URL）不再 413 ---------------------------------------


@pytest.mark.asyncio
async def test_a1_15mib_data_url_is_accepted(client, monkeypatch):
    """① 15 MiB 原图 data URL ⇒ 200（旧码按串长判、必 413 ⇒ 本腿是 RED 判别腿）。"""
    doc = _data_url(FIFTEEN_MIB)
    payload = doc.split(",", 1)[1]
    assert len(payload) == 20971520, "几何：15 MiB 的 base64 恰是 20 971 520 字符"
    assert len(doc) > chat_routes.MAX_UPLOAD_BYTES, "几何：data URL 串长超上限 ⇒ 旧判据必 413"

    chat, vision = _install_vision(monkeypatch, vision_reply="图里是一只猫。")
    resp = await client.post("/api/chat", json={"message": "看图", "images": [doc]})

    assert resp.status_code == 200, resp.text
    assert resp.json()["message"] == "图里是一只猫。"
    assert chat.calls == [], "带图轮必须走视觉装配缝"
    assert vision.calls, "带图轮必须走视觉装配缝"
    assert vision.part_types == ["text", "image"]
    image_part = vision.last_user_parts()[-1]
    assert (image_part.media_type, image_part.source) == ("image/png", payload), (
        "装配缝拿到的仍应是裸 base64（header 在 _image_content 里剥掉）"
    )


# --- 腿 a2：真超限仍 413（含防放宽：不许「带 data: 就跳过」）----------------------


@pytest.mark.asyncio
async def test_a2_real_oversize_still_413_verbatim(client, monkeypatch):
    """② 解码后 = 上限 + 1 字节的真样本 ⇒ 仍 413，文案逐字。"""
    doc = _data_url(chat_routes.MAX_UPLOAD_BYTES + 1)
    assert len(doc.split(",", 1)[1]) == 27962028, "几何：上限 + 1 字节的 base64 长度"

    _install_vision(monkeypatch, vision_reply="不该被调用")
    resp = await client.post("/api/chat", json={"message": "看图", "images": [doc]})

    assert resp.status_code == 413, resp.text
    assert resp.json()["detail"] == TOO_LARGE_DETAIL
    assert resp.json()["detail"] == document_routes._TOO_LARGE_DETAIL, (
        "与文档面同一条文案（口径一致）"
    )


# --- 腿 a3：payload 串长 > 上限但解码后不超 ⇒ 放行（杀半修法）----------------------


@pytest.mark.asyncio
async def test_a3_payload_longer_than_cap_but_decoded_under_is_accepted(client, monkeypatch):
    """payload 串长 25 165 824 > 上限、解码后 18 MiB < 上限 ⇒ 必须 200。"""
    eighteen_mib = 18 * MIB
    doc = _data_url(eighteen_mib)
    payload = doc.split(",", 1)[1]
    assert len(payload) == 25165824 and len(payload) > chat_routes.MAX_UPLOAD_BYTES

    _install_vision(monkeypatch, vision_reply="图里是一只猫。")
    resp = await client.post("/api/chat", json={"message": "看图", "images": [doc]})

    assert resp.status_code == 200, resp.text


# --- 腿 a4/a5：裸 base64 与旧字段 image_data 同口径 ------------------------------


@pytest.mark.asyncio
async def test_a4_bare_base64_same_rule(client, monkeypatch):
    """③ 裸 base64（无 ``data:`` 前缀）同样按解码后字节数判 ⇒ 200。"""
    bare = _b64(FIFTEEN_MIB + 1)
    assert len(bare) == 20971524 > chat_routes.MAX_UPLOAD_BYTES, (
        "几何：裸串也长于上限 ⇒ 旧判据必 413"
    )

    _install_vision(monkeypatch, vision_reply="图里是一只猫。")
    resp = await client.post("/api/chat", json={"message": "看图", "images": [bare]})

    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_a5_legacy_image_data_field_same_rule(client, monkeypatch):
    """③ 旧字段 ``image_data``（``images`` 缺省时的回落）走同一判据 ⇒ 200。"""
    doc = _data_url(FIFTEEN_MIB)
    assert len(doc) > chat_routes.MAX_UPLOAD_BYTES

    _install_vision(monkeypatch, vision_reply="图里是一只猫。")
    resp = await client.post("/api/chat", json={"message": "看图", "image_data": doc})

    assert resp.status_code == 200, resp.text
    assert resp.json()["message"] == "图里是一只猫。"


# --- 腿 a6：严格 > 与 padding 算术（小上限，避免再搬 20 MiB 级样本）---------------


@pytest.mark.asyncio
async def test_a6_exact_cap_pass_and_cap_plus_one_413(client, monkeypatch):
    """== 上限放行、上限 + 1 拒绝；两种 padding 形态都验（本腿不动文档面）。"""
    _install_vision(monkeypatch, vision_reply="图里是一只猫。")
    # 只在 chat 面改上限：document_routes.MAX_UPLOAD_BYTES 是另一个模块属性（:56），不受影响
    monkeypatch.setattr(chat_routes, "MAX_UPLOAD_BYTES", 4096)

    ok = _data_url(4096)  # base64 5464 字符，尾部 "==" ⇒ 估算 5464 * 3 // 4 - 2 == 4096
    assert ok.endswith("==") and len(ok.split(",", 1)[1]) == 5464
    resp = await client.post("/api/chat", json={"message": "看图", "images": [ok]})
    assert resp.status_code == 200, resp.text

    over = _data_url(4097)  # base64 5464 字符，尾部 "=" ⇒ 估算 5464 * 3 // 4 - 1 == 4097
    assert over.endswith("=") and not over.endswith("==")
    assert len(over.split(",", 1)[1]) == 5464
    resp2 = await client.post("/api/chat", json={"message": "看图", "images": [over]})
    assert resp2.status_code == 413, resp2.text
    assert resp2.json()["detail"] == TOO_LARGE_DETAIL

    assert document_routes.MAX_UPLOAD_BYTES == 20 * MIB, "文档面上限不得被本腿波及"

# --- 腿 b1：畸形 ``data:`` 头（无逗号）不得 500（复核 must-fix）-------------------


DEGENERATE_DATA_HEADS = ["data:image/png;base64", "data:"]


@pytest.mark.asyncio
@pytest.mark.parametrize("head", DEGENERATE_DATA_HEADS)
async def test_b1_degenerate_data_head_not_500(client, monkeypatch, head):
    """带 ``data:`` 头但**无逗号**的畸形串：本腿钉「不崩」。

    复核 must-fix（Lead 2026-09-30 指令 / s5-w1 独立探针）：改前 ``split(",", 1)[1]`` 对这两串
    抛 IndexError，而 ``main.py`` 只注册 RagError 处理器 ⇒ 未处理 500。改后 ``partition(",")[2]``
    取空载荷（按 0 字节估算）⇒ 放行到装配缝（200）—— 与改前 ``len(...)`` 判据下的既有行为一致，
    本腿不引入新的拒收口径，只钉「不再有 500 路径」。
    """
    assert head.startswith("data:") and "," not in head

    chat, vision = _install_vision(monkeypatch, vision_reply="图里是一只猫。")
    resp = await client.post("/api/chat", json={"message": "看图", "images": [head]})

    assert resp.status_code == 200, resp.text
    assert resp.json()["message"] == "图里是一只猫。"
    assert chat.calls == [] and vision.calls, "畸形头也走视觉装配缝、不得回落到纯文本模型"
