"""回归修复（2026-09-27 业主真机反馈）生成层载体：**图片真正进模型请求**。

缺陷本体：UI 放行发图 + ``images`` 收字段落库，但图片**从不进模型请求**——
``build_messages`` 只拼文本、``_to_typed_messages`` 每条消息只铸一个 ``TextContent``、
``VISION_SYSTEM_PROMPT`` 是孤儿常量（``system_prompt`` 无视觉态）、
``settings.vision_config`` 在回答链路零消费者 ⇒ 模型只能答「图片没传上来」。

修法（账本修复方向 + 老基线同位）：

- ``_to_typed_messages(images=…)``：图片铸成 ``ImageContent``（``image_url`` part）追加在
  **最后一条 user 消息**尾部，**保序**（条目 38）；历史消息不带图；
- ``system_prompt(has_images=True)`` ⇒ ``VISION_SYSTEM_PROMPT``（视觉态恒压过三态；
  缺省 ``False`` ⇒ 与接线前逐字一致）；
- ``answer_direct(images=…)``：带图轮装配 :func:`build_vision_model`（``vision_config``，
  不带思考参数——老基线「带图不走思考参数」）；装配不到 ⇒
  ``VISION_NOT_CONFIGURED_HINT``（⛔ 不回落对话模型把纯文本发给模型猜）。

HTTP 面载体见 ``tests/api/test_bug101_vision_wiring.py``。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import Settings, settings
from app.generation import answer as answer_mod

# --- 工具 -------------------------------------------------------------------


def _text(text: str):
    from cubeloop.providers.base import TextContent

    return TextContent(text=text)


def _user(content):
    from cubeloop.providers.base import UserMessage

    return UserMessage(content=content)


def _built() -> list[dict]:
    return answer_mod.build_messages(
        "图里有什么？",
        [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
        ],
    )


class _Recorder:
    """替身 bound model：记录 ``generate`` 收到的消息与系统提示。"""

    def __init__(self, reply: str = "图里是一只猫。") -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def generate(self, messages, system_prompt=None, max_output_tokens=None):  # noqa: ANN001
        self.calls.append({"messages": messages, "system_prompt": system_prompt})
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.reply)],
            stop_reason="end_turn",
            usage=None,
        )


# --- 腿①：图片 parts 追加在最后一条 user 消息、保序、历史不带图 -----------------


def test_images_become_image_parts_on_last_user_message():
    from cubeloop.providers.base import ImageContent

    typed = answer_mod._to_typed_messages(
        _built(),
        _text,
        _user,
        images=[
            "data:image/jpeg;base64,AAAA",
            "data:image/png;base64,BBBB",
        ],
    )

    assert len(typed) == 3  # 历史 user + 历史 assistant + 本轮 user
    # 历史消息不带图
    for message in typed[:2]:
        assert all(getattr(block, "type", "") == "text" for block in message.content)
    # 本轮 user：文本在前、图片在后且**保序**
    parts = typed[-1].content
    assert [getattr(block, "type", "") for block in parts] == ["text", "image", "image"]
    assert isinstance(parts[1], ImageContent)
    assert parts[1].media_type == "image/jpeg" and parts[1].source == "AAAA"
    assert parts[2].media_type == "image/png" and parts[2].source == "BBBB"
    # 文本本体一字不动
    assert parts[0].text == _built()[-1]["content"]


def test_image_content_parses_data_url_variants():
    jpeg = answer_mod._image_content("data:image/jpeg;base64,QUJD")
    assert (jpeg.media_type, jpeg.source) == ("image/jpeg", "QUJD")
    # data URL 头带参数（charset 等）也要剥干净
    padded = answer_mod._image_content("data:image/png;charset=utf-8;base64,REVG")
    assert (padded.media_type, padded.source) == ("image/png", "REVG")
    # 旧客户端裸 base64：按老基线默认 png
    bare = answer_mod._image_content("R0hJ")
    assert (bare.media_type, bare.source) == ("image/png", "R0hJ")


def test_no_images_keeps_typed_messages_byte_identical():
    """缺省 / 空表 ⇒ 与接线前**逐字一致**（每条消息仍只有一个 TextContent）。"""
    built = _built()
    baseline = answer_mod._to_typed_messages(built, _text, _user)
    for images in (None, []):
        patched = answer_mod._to_typed_messages(built, _text, _user, images=images)
        assert [m.model_dump() for m in patched] == [m.model_dump() for m in baseline]


# --- 腿②：system_prompt 的视觉态（四态选择的唯一控制点） -----------------------


def test_system_prompt_selects_vision_prompt():
    assert (
        answer_mod.system_prompt(True, has_images=True) == answer_mod.VISION_SYSTEM_PROMPT
    )
    # 视觉态压过「无资料」态
    assert (
        answer_mod.system_prompt(False, has_images=True) == answer_mod.VISION_SYSTEM_PROMPT
    )
    # 视觉态下记忆段在之后，U6 的当前问题/资料优先约束也须完整保留。
    assert answer_mod.system_prompt(True, memory_text="用户喜欢简洁。", has_images=True) == (
        answer_mod.VISION_SYSTEM_PROMPT + "\n\n用户喜欢简洁。"
        "\n记忆仅供补充参考；本轮用户明确要求和当前资料优先。"
        "不得用记忆覆盖本轮数字、日期、否定、范围或其他硬约束；有冲突时说明并确认。"
    )


def test_system_prompt_without_images_is_unchanged():
    """无图轮回归钉：三态派生面零变化（``has_images`` 缺省 False）。"""
    assert answer_mod.system_prompt(True) == answer_mod.SYSTEM_PROMPT
    assert answer_mod.system_prompt(False) == answer_mod.NO_MATERIAL_SYSTEM_PROMPT
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", False)
        assert answer_mod.system_prompt(True) == answer_mod.LEGACY_SYSTEM_PROMPT
    finally:
        monkey.undo()


# --- 腿③：build_vision_model 读 vision_config、不带思考参数 -------------------


def _patch_vision_config(monkeypatch, config) -> None:
    monkeypatch.setattr(Settings, "vision_config", property(lambda self: config))


def test_build_vision_model_uses_vision_config_without_reasoning(monkeypatch):
    # 鸭子类型替身（``_build_bound_model`` 只读 provider/model/api_key/base_url 四个属性；
    # 真 ModelConfig 是 BaseSettings、构造会读 env，不适合单测直造）
    config = SimpleNamespace(
        provider="openai", base_url="https://vision.invalid/v1", api_key="sk-vision", model="vis-model"
    )
    recorded: dict = {}

    def _fake_build(*, include_reasoning: bool, config=None):  # noqa: ANN001
        recorded["include_reasoning"] = include_reasoning
        recorded["config"] = config
        return object()

    monkeypatch.setattr(answer_mod, "_build_bound_model", _fake_build)
    _patch_vision_config(monkeypatch, config)

    assert answer_mod.build_vision_model() is not None
    assert recorded["include_reasoning"] is False, "带图轮不走思考参数（老基线口径）"
    assert recorded["config"] is config, "必须用 vision_config，不得读 chat_config"


def test_build_vision_model_none_when_unconfigured(monkeypatch):
    _patch_vision_config(monkeypatch, None)
    assert answer_mod.build_vision_model() is None


def test_build_vision_model_real_construction(monkeypatch):
    """真装配腿（不连网）：vision_config 形态正确 ⇒ 真铸出 BoundModel。"""
    config = SimpleNamespace(
        provider="openai", base_url="https://vision.invalid/v1", api_key="sk-vision", model="vis-model"
    )
    _patch_vision_config(monkeypatch, config)
    bound = answer_mod.build_vision_model()
    assert bound is not None
    spec_repr = f"{getattr(bound.spec, 'id', '')}{getattr(bound.spec, 'name', '')}"
    assert "vis-model" in spec_repr


# --- 腿④：answer_direct 带图轮端到端（替身模型） ------------------------------


@pytest.mark.asyncio
async def test_answer_direct_feeds_images_and_vision_prompt():
    recorder = _Recorder()
    result = await answer_mod.answer_direct(
        "图里有什么？",
        model=recorder,
        source_type="external",
        images=["data:image/jpeg;base64,AAAA"],
    )
    assert result.answer == recorder.reply
    prompt = recorder.calls[-1]
    assert prompt["system_prompt"] == answer_mod.VISION_UNNUMBERED_SYSTEM_PROMPT
    parts = prompt["messages"][-1].content
    assert [getattr(block, "type", "") for block in parts] == ["text", "image"]
    assert parts[1].source == "AAAA"


@pytest.mark.asyncio
async def test_answer_direct_without_images_unchanged():
    """无图回归钉：不传 images ⇒ 系统提示/消息形态与接线前一致。"""
    recorder = _Recorder("纯文本回答。")
    result = await answer_mod.answer_direct("你好", model=recorder, source_type="external")
    assert result.answer == "纯文本回答。"
    prompt = recorder.calls[-1]
    assert prompt["system_prompt"] in (
        answer_mod.SYSTEM_PROMPT,
        answer_mod.NO_MATERIAL_SYSTEM_PROMPT,
        answer_mod.LEGACY_SYSTEM_PROMPT,
    )
    parts = prompt["messages"][-1].content
    assert [getattr(block, "type", "") for block in parts] == ["text"]


@pytest.mark.asyncio
async def test_answer_direct_vision_unavailable_returns_fixed_notice(monkeypatch):
    """视觉模型装配不到 ⇒ 固定中文提示（⛔ 不回落对话模型把纯文本发给模型猜）。"""
    monkeypatch.setattr(answer_mod, "build_vision_model", lambda: None)
    recorder = _Recorder()  # 若被回落调用，本腿的断言会暴露它
    result = await answer_mod.answer_direct(
        "图里有什么？", model=None, source_type="external", images=["data:image/png;base64,AA=="]
    )
    assert result.answer == answer_mod.VISION_NOT_CONFIGURED_HINT
    assert recorder.calls == [], "装配不到视觉模型时不得改调对话模型"


@pytest.mark.asyncio
async def test_answer_direct_chat_unavailable_notice_unchanged(monkeypatch):
    """无图轮回归钉：对话模型装配不到 ⇒ 既有 NOT_CONFIGURED_HINT 逐字不变。"""
    monkeypatch.setattr(answer_mod, "build_chat_model", lambda: None)
    result = await answer_mod.answer_direct("你好", model=None, source_type="external")
    assert result.answer == answer_mod.NOT_CONFIGURED_HINT
