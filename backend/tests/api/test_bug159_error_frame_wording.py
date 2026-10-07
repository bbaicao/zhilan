"""条目 159（P2）载体：异常帧**文案族**的契约冻结判据（流内异常 / 兜底 / note-gen）。

拍板 #16-A：把「**固定中文 + 异常类名**」写进「接口说明/02-API契约冻结清单.md」
（§2 SSE 协议 + §7.3 note-gen 子协议），口径与既有用例
「backend/tests/api/test_contract_runtime_4_6.py:21-37」一致。三条字面**逐字冻结**：

1. STREAM_FAILURE_HINT = "生成失败，请稍后重试。"，流内异常帧 = f"…（{异常类名}）"
   —— 两个站点同模板（_produce 里 getter.result() 的 except；_events 里
   _produce 之外的兜底 except）；
2. "回答流程异常结束，请重试。" —— 任何路径忘了收尾时 guard 补发的那条（_events）；
3. note-gen 流：f"笔记生成失败，请稍后重试。（{异常类名}）"（非 RagError 分支；
   RagError 分支是 str(exc) 原样透传，不在本文件口径内）。

判据纪律（与拍板一致）：
- **只认「收尾帧类型 + detail 逐字 + 注入点」，不认行号** —— 同文件正被 153 改造，行号会漂；
- ⛔ 不用「文案里没有括号」这类粗暴判据：note-gen 既有用例断的就是**带类名**的字面
  （test_contract_runtime_4_6.py:37），粗暴判据会把它顶掉；判定力来自「逐字相等 + 类名正确」。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.api import chat_routes, chat_stop, document_routes
from app.api.chat_schemas import ChatRequest
from tests.api.test_chat_contract import _StubClassifier, faux_model  # noqa: F401
from tests.retrieval.conftest import seed_document


STREAM_FAILURE_PREFIX = "生成失败，请稍后重试。"
UNFINISHED_SENTENCE = "回答流程异常结束，请重试。"
NOTE_FAILURE_PREFIX = "笔记生成失败，请稍后重试。"


class MidStream159(RuntimeError):
    """腿①注入的异常：类名会被**逐字**打进 error 帧。"""


class OuterExcept159(RuntimeError):
    """腿②注入的异常：从 _produce 冒到 _events 的兜底 except。"""


class _ExplodingStream:
    """先回一段正文、再炸的流替身（cubeloop 适配器双出口同款）。

    命中 _produce 里 getter.result() 的 except（生成中途出错 ⇒ aborted + error 收尾）。
    """

    def __init__(self) -> None:
        self._first = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._first:
            self._first = False
            return SimpleNamespace(type="text_delta", delta="半截正文。")
        raise MidStream159("injected mid-stream failure")

    async def result(self):
        raise MidStream159("injected mid-stream failure")


class _Model:
    """stream() 回给定流替身；同步 generate() 不该被流式载体调用。"""

    def __init__(self, stream) -> None:  # noqa: ANN001
        self._stream = stream

    async def stream(self, messages, system_prompt=None):  # noqa: ANN001
        return self._stream

    async def generate(self, messages, system_prompt=None, max_output_tokens=None):  # noqa: ANN001
        raise AssertionError("流式载体不应走同步生成")


class _NeverFinishingGuard:
    """StreamGuard 替身：永不收尾 ⇒ 强制命中 _events 的兜底句。"""

    finished = False
    terminal = None

    def done(self) -> bool:
        return False

    def error(self) -> bool:
        return False

    def stopped(self) -> bool:
        return False


def _install(monkeypatch, model) -> None:  # noqa: ANN001
    """与 153 载体同款三钉（真驱动 chat_stream，只换模型/分类器）。"""
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: None)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("external")
    )
    from app.core.config import settings

    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", False)


@pytest.fixture(autouse=True)
def _clean_registry():
    chat_stop.reset()
    yield
    chat_stop.reset()


def _frame(frame: bytes) -> dict:
    return json.loads(frame.decode("utf-8").removeprefix("data: "))


def _kinds(frames: list) -> list:
    return [f["type"] for f in frames]


async def _drive(db, monkeypatch, model, request_id: str) -> list:  # noqa: ANN001
    """铺钉子 → 真驱动 chat_stream 的 body_iterator → 收全帧。"""
    _install(monkeypatch, model)
    response = await chat_routes.chat_stream(
        ChatRequest(message="向量库用什么存储？", request_id=request_id), db
    )
    frames: list = []
    async for raw in response.body_iterator:
        frames.append(_frame(raw))
    return frames


def _note_frames(chunks: list) -> list:
    """note-gen 的 body_iterator 逐块收帧（data: <JSON> 后跟两个换行；可能一块多帧）。"""
    frames: list = []
    buffer = b""
    for chunk in chunks:
        buffer += chunk
        while b"\n\n" in buffer:
            block, buffer = buffer.split(b"\n\n", 1)
            text = block.decode("utf-8")
            if text.startswith("data: "):
                frames.append(json.loads(text[len("data: ") :]))
    return frames


@pytest.mark.asyncio
async def test_mid_stream_exception_frame_keeps_class_name(db, monkeypatch):
    """腿①：生成中途抛错 ⇒ 收尾帧文案 = 固定中文 + **异常类名**（不是厂商原文、不是空话）。"""
    frames = await _drive(db, monkeypatch, _Model(_ExplodingStream()), "req-bug159a")

    kinds = _kinds(frames)
    assert kinds[-1] == "error", f"流内异常必须以 error 收尾，实得帧序 {kinds}"
    assert "done" not in kinds, f"异常轮不得下发 done，实得帧序 {kinds}"
    assert frames[-1]["detail"] == f"{STREAM_FAILURE_PREFIX}（MidStream159）", frames[-1]


@pytest.mark.asyncio
async def test_unhandled_exception_frame_keeps_class_name(db, monkeypatch):
    """腿②：_produce 内没接住的意外冒到 _events 兜底 except ⇒ 同模板同文案。"""

    def _boom(_answer: str, _plan):
        raise OuterExcept159("injected outer failure")

    monkeypatch.setattr(chat_routes, "hint_for_error", _boom)
    frames = await _drive(db, monkeypatch, faux_model("正常作答。"), "req-bug159b")

    kinds = _kinds(frames)
    assert kinds[-1] == "error", f"兜底 except 必须以 error 收尾，实得帧序 {kinds}"
    assert frames[-1]["detail"] == f"{STREAM_FAILURE_PREFIX}（OuterExcept159）", frames[-1]


@pytest.mark.asyncio
async def test_guard_fallback_frame_is_the_frozen_sentence(db, monkeypatch):
    """腿③：任何路径忘了收尾 ⇒ guard 补的那条**逐字**是「回答流程异常结束，请重试。」。"""
    monkeypatch.setattr(chat_routes.observe, "StreamGuard", _NeverFinishingGuard)
    frames = await _drive(db, monkeypatch, faux_model("兜底前的正文。"), "req-bug159c")

    kinds = _kinds(frames)
    assert kinds[-1] == "error", f"忘了收尾必须由 guard 补 error，实得帧序 {kinds}"
    assert frames[-1] == {"type": "error", "detail": UNFINISHED_SENTENCE}, frames[-1]


@pytest.mark.asyncio
async def test_note_stream_error_frame_keeps_class_name(db, monkeypatch):
    """腿④：note-gen 流同一口径（固定中文 + 类名），与 4_6:37 的既有字面一致。"""

    class _BrokenNoteModel:
        async def stream(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise TimeoutError("injected note timeout")

        async def generate(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise TimeoutError("injected note timeout")

    model = _BrokenNoteModel()
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: model)

    doc = await seed_document(
        db, ["项目于2024-03-15启动，预算120万元。（第 159 条判据材料）"], filename="bug159.md"
    )
    response = await document_routes.generate_note_stream_endpoint(doc.id, db)
    frames = _note_frames([chunk async for chunk in response.body_iterator])

    terminal = [f for f in frames if f["type"] in {"done", "error"}]
    assert len(terminal) == 1, f"收尾帧必须恰好一条，实得 {terminal}"
    assert terminal[0] == {
        "type": "error",
        "detail": f"{NOTE_FAILURE_PREFIX}（TimeoutError）",
    }, terminal[0]
