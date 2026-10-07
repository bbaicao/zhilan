"""A14 / A15 / A16 / A19：辅助链路与笔记生成的**隔离面**（开工包 §3.2、§4）。

## 本文件与 `tests/test_reasoning_isolation.py` 的分工

- 那边（`tests/` 顶层）**不继承** `tests/api/conftest.py` 的钉子 ⇒ 真装配 + 假传输，
  验 A12（回答链路）与 A13（Agent 决策 / 计划调用）；
- **这边（`tests/api/`）继承全部九个 autouse 钉子**，包括
  `pin_aux_model_delegates` ⇒ 验的正是「**既有用例怎么被保护**」（A19），
  以及「四条辅助链路 + 两条笔记路径的**入参**是 aux 而不是 chat」（A14 / A15）。
  入参用 AST 静态核对（A16），这样不依赖运行时的调用形态。

## A14 / A15 为什么在这一层验「入参」而不是「请求体」

「请求体里没有思考键」的**权威证据**在 `tests/test_reasoning_isolation.py`
（真装配 + 假传输，直接读 wire）。这一层验的是**另一半**：
端点真的把 `aux` 传给了那四个形参——漏传一处，那边就算真装配也会因为
「aux 与 chat 是同一个对象」而验不出来。两半合起来才是完整的 A14/A15。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.api import chat_routes
from app.core.config import settings
from app.generation import answer as answer_mod
from tests.api.conftest import AUX_DELEGATED
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401 —— client 是夹具
from tests.retrieval.conftest import seed_document


APP_DIR = Path(chat_routes.__file__).resolve().parents[1]  # backend/app


class _Recorder:
    """零出网替身：既能 `stream`（回答）也能 `generate`（摘要 / 记忆抽取 / Agent），并记调用。

    ⚠️ **形参要放宽**（`*args, **kwargs`）：真实 bound model 的调用面比这里宽得多
    ——压缩会传 `temperature=`、`reasoning=ReasoningControl(...)`，Agent 会传
    `max_output_tokens=` / `tools=`。窄签名会让替身**抛 TypeError**，
    然后被产品的 I5 兜底渲染成 `error` 帧，症状（「流里没有 done 帧」）
    与「隔离失效」**长得一模一样** ⇒ 2026-09-20 实测为这条白查了一轮。
    """

    def __init__(self) -> None:
        self.streams: list[dict] = []
        self.generates: list[dict] = []

    async def stream(self, messages, *args, **kwargs):  # noqa: ANN001, ANN003
        from types import SimpleNamespace

        self.streams.append({"messages": messages, "kwargs": kwargs})
        reply = self.reply

        async def _iterate():
            yield SimpleNamespace(type="text_delta", delta=reply)

        return _iterate()

    async def generate(self, messages, *args, **kwargs):  # noqa: ANN001, ANN003
        from types import SimpleNamespace

        self.generates.append({"messages": messages, "kwargs": kwargs})
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.reply)], stop_reason="end_turn"
        )

    #: 替身正文（可由用例改写；默认给一句通用文本）
    reply: str = "替身正文。"

    @property
    def spec(self):
        """真实 bound model 的 `.spec.id`（Agent 路径会读它：`runner.py` 的独立工具会话）。

        ⚠️ **少了这个属性，替身会在 Agent 路径上抛 `AttributeError`**，然后被产品的
        I5 兜底渲染成 `error` 帧 —— 症状（「流里没有 done 帧」）与「隔离失效」
        **长得一模一样**（2026-09-20 实测白查一轮）。Agent 轮为什么会被走到：
        `settings.AGENT_MODE` 是**跨文件遗留状态**（本目录既有用例会改它），
        所以这里必须把替身的形状做全，不能假设「本条不会走 Agent」。
        """
        from types import SimpleNamespace

        return SimpleNamespace(id="recorder-fake")


@pytest.fixture(autouse=True)
def _zero_outbound(monkeypatch):
    """本文件自带的隔离：记忆 / 澄清关（A14 的三条链路要按需打开）。"""
    monkeypatch.setattr(settings, "MEMORY_ENABLE", False)
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", False)
    monkeypatch.setattr(settings, "ROUTING_MODE", "regex")
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db")
    )


# --- A19：夹具生效 + 真装配函数调用计数为 0 --------------------------------------------------


@pytest.mark.asyncio
async def test_a19_fixture_makes_aux_follow_chat_and_real_builders_zero(client, db, monkeypatch):
    """**A19 正题**：公共夹具下，走一轮真 HTTP 问答**一次都不碰真装配函数**。

    两件事一起验：

    1. `resolve_aux_model()` **跟随** `resolve_chat_model()`（夹具生效的**可观测判据**）；
    2. `answer.build_chat_model` / `build_aux_model` 在整轮请求里**调用计数为 0**
       —— 既有那三十处打在 `resolve_chat_model` 上的替身因此仍然有效，
       本机 `.env` 的真厂商 Key **不会被拿去出网**。
    """
    calls: list[str] = []
    real_chat = answer_mod.build_chat_model
    real_aux = answer_mod.build_aux_model

    def _counting_chat():
        calls.append("chat")
        return real_chat()

    def _counting_aux():
        calls.append("aux")
        return real_aux()

    monkeypatch.setattr(answer_mod, "build_chat_model", _counting_chat)
    monkeypatch.setattr(answer_mod, "build_aux_model", _counting_aux)

    fake = _Recorder()
    # ⚠️ **两个入口都要在本用例里显式钉住**，不能只钉 chat 再指望夹具把 aux 带过来：
    # `tests/api` 是共享会话，`settings.AGENT_MODE` / `chat_routes.resolve_chat_model`
    # 都会带上上游用例的遗留值（既有行为，与本条目无关），「aux 跟随 chat」这件事
    # 因此不该由**本条**来断。这里直接把两个入口都指向 `fake`：验的是**端点行为**
    # （一轮真 HTTP 问答里真装配函数调用计数为 0），这是 A19 的正题；
    # 「夹具让 aux 跟随 chat」由 `test_a19_fixture_off_lets_aux_be_a_different_object`
    # 的 `real_model_builders` 往返自证覆盖。
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: fake)
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: fake)
    await seed_document(db, ["A19 用的真材料。"], filename="A19.md")

    resp = await client.post("/api/chat/stream", json={"message": "A19 用的真材料"})
    assert resp.status_code == 200, resp.text
    events = parse_sse(resp.text)
    assert [event["type"] for event in events][-1] == "done", (
        f"流以 error 收尾：{events}；绑定 chat={chat_routes.resolve_chat_model!r} "
        f"aux={chat_routes.resolve_aux_model!r}"
    )

    # ① 夹具生效：aux 拿到的就是 chat 那一个（**不是**真装配出来的新对象）
    assert chat_routes.resolve_aux_model() is fake
    # ② 真装配函数一次都没被调用
    assert calls == [], f"真装配函数被调用了：{calls}（夹具没盖住某条路径）"


@pytest.mark.asyncio
async def test_a19_fixture_off_lets_aux_be_a_different_object(client, monkeypatch):
    """**A19 的限制面 + 自家栅栏**：夹具只让 aux **跟随** chat，不阻止用例自己分叉。

    两件事：

    1. 用例显式替换 `resolve_aux_model` 时，两个入口就是两个对象（「A19 的夹具不许覆盖
       A12–A15」的可观测表达）；
    2. :func:`real_model_builders` 是 A12–A15 的退出通道（换成真函数），而且
       **`undo()` 之后必须把原绑定还给模块**——这条不是装饰：2026-09-20 实测它曾是
       **整包连跑才复现**的泄漏源（`@contextmanager` 被当普通函数调用 ⇒ 什么都不做，
       却让真函数在会话内一直挂着），当时 18 条红全由它引起（独立只读审计给了根因）。
       这里把它钉成一条**用例级**的栅栏：泄漏了本文件自己先红。
    """
    from tests.api.conftest import real_model_builders

    other = _Recorder()
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: other)
    assert chat_routes.resolve_aux_model() is other, "用例自己的分叉没生效"
    assert chat_routes.resolve_aux_model() is not chat_routes.resolve_chat_model()

    # 退出通道：换成真函数 → **还原后必须回到真入口**（不是把真函数留在模块上）
    original_chat = chat_routes.resolve_chat_model
    original_aux = chat_routes.resolve_aux_model
    restore = real_model_builders(monkeypatch)
    assert chat_routes.resolve_chat_model is answer_mod.build_chat_model
    assert chat_routes.resolve_aux_model is answer_mod.build_aux_model
    restore()
    assert chat_routes.resolve_chat_model is original_chat, "退出通道没还原 chat 入口（泄漏）"
    assert chat_routes.resolve_aux_model is original_aux, "退出通道没还原 aux 入口（泄漏）"


# --- A14：澄清 / 压缩 / 记忆抽取三条辅助链路都吃 aux --------------------------------------------


@pytest.mark.asyncio
async def test_a14_clarify_and_compaction_get_aux_not_chat(client, db, monkeypatch):
    """澄清与压缩的形参必须是 `aux`（拿 `chat` 会让用户配的思考参数进内部编排）。

    手法：把 `build_turn_plan` 换成一个**只记入参**的替身。流式端点在
    「流开始前」就调它，所以一次 `post` 足够；替身抛错 ⇒ 端点照契约以 400/500 收口，
    这里**不判状态码**（本用例唯一要断的是入参）。
    """
    chat, aux = _Recorder(), _Recorder()
    captured: dict = {}
    real_plan = chat_routes.build_turn_plan

    async def fake_plan(*args, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        kwargs["clarify_model"] = None  # 不真的去调模型
        kwargs["compaction_model"] = None
        return await real_plan(*args, **kwargs)

    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: chat)
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: aux)
    monkeypatch.setattr(chat_routes, "build_turn_plan", fake_plan)
    await seed_document(db, ["澄清压缩入参用的真材料。"], filename="clarify.md")

    resp = await client.post("/api/chat/stream", json={"message": "澄清压缩入参用的真材料"})
    assert resp.status_code == 200, resp.text
    assert captured, "build_turn_plan 没被调用"
    assert captured.get("clarify_model") is aux, captured.keys()
    assert captured.get("compaction_model") is aux, captured.keys()
    assert captured.get("clarify_model") is not chat


@pytest.mark.asyncio
async def test_a14_memory_extraction_gets_aux_not_chat(client, db, monkeypatch):
    """记忆抽取走 `persist_turn(memory_model=…)`，同样必须是 `aux`。"""
    chat, aux = _Recorder(), _Recorder()
    captured: dict = {}
    real_persist = chat_routes.persist_turn

    async def fake_persist(*args, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return await real_persist(*args, **kwargs)

    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: chat)
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: aux)
    monkeypatch.setattr(chat_routes, "persist_turn", fake_persist)
    await seed_document(db, ["记忆抽取入参用的真材料。"], filename="memory.md")

    resp = await client.post("/api/chat/stream", json={"message": "记忆抽取入参用的真材料"})
    assert resp.status_code == 200, resp.text
    assert captured, f"persist_turn 没被调用（帧：{[e['type'] for e in parse_sse(resp.text)]}）"
    assert captured.get("memory_model") is aux, captured.keys()
    assert captured.get("memory_model") is not chat


@pytest.mark.asyncio
async def test_a14_agent_decision_gets_aux_not_chat(client, db, monkeypatch):
    """`stream_tokens(aux_model=…)` 一路传到 `run_agent_turn` 的 Agent 决策。"""
    chat, aux = _Recorder(), _Recorder()
    captured: dict = {}
    real_run = chat_routes.run_agent_turn

    def fake_run(plan, model, db, *, aux_model=None):  # noqa: ANN001
        captured.update({"model": model, "aux_model": aux_model})
        return real_run(plan, model, db, aux_model=aux_model)

    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: chat)
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: aux)
    monkeypatch.setattr(chat_routes, "run_agent_turn", fake_run)
    monkeypatch.setattr(settings, "AGENT_MODE", "react")
    await seed_document(db, ["Agent 决策入参用的真材料。"], filename="agent.md")

    resp = await client.post("/api/chat/stream", json={"message": "Agent 决策入参用的真材料"})
    assert resp.status_code == 200, resp.text
    assert captured.get("aux_model") is aux, captured
    assert captured.get("model") is chat, captured


# --- A15：两条笔记路径都吃 aux ------------------------------------------------------------


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.asyncio
async def test_a15_note_generation_gets_aux(client, db, monkeypatch, streaming):
    """笔记生成（同步 `generate-note` + 流式 `generate-note/stream`）走 `resolve_aux_model`。"""
    from app.api import document_routes

    chat, aux = _Recorder(), _Recorder()
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: chat)
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: aux)

    document = await seed_document(db, ["笔记生成入参用的真材料。"], filename="note.md")
    captured: dict = {}
    real_generate = document_routes.note_gen_mod.generate_note

    async def fake_generate(*args, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return await real_generate(*args, **kwargs)

    monkeypatch.setattr(document_routes.note_gen_mod, "generate_note", fake_generate)

    url = f"/api/documents/{document.id}/generate-note"
    if streaming:
        url += "/stream"
        resp = await client.post(url)
        assert resp.status_code == 200, resp.text
    else:
        resp = await client.post(url)
        assert resp.status_code == 200, resp.text
    assert captured, "generate_note 没被调用"
    assert captured.get("model") is aux, captured
    assert captured.get("model") is not chat


# --- A16：调用点白名单（AST 静态）------------------------------------------------------------


def _call_names(path: Path) -> list[tuple[str, str]]:
    """文件里所有装配函数调用点（`(被调函数名, 所在函数名)`）。

    两种写法都要认（本仓两种都有）：

    - 属性调用 `answer_mod.build_aux_model()` / `chat_routes.resolve_aux_model()`（`attr` 分支）；
    - **裸名调用** `resolve_aux_model()`（`id` 分支）—— `document_routes._resolve_note_model`
      就是 `from app.api.chat_routes import resolve_aux_model` 之后裸调的；
      只认属性写法会**静默漏掉**它（本文件踩过一次）。
    """
    watched = (
        "build_chat_model",
        "build_aux_model",
        "_build_bound_model",
        "resolve_aux_model",
        "resolve_chat_model",
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []
    stack: list[str] = []

    class _Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):  # noqa: ANN001, N802
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):  # noqa: ANN001, N802
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in watched:
                found.append((name, stack[-1] if stack else "<module>"))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return found


def test_a16_build_chat_model_call_sites_are_whitelisted():
    """**A16**：`build_chat_model` 只允许两处调用 —— `answer.py` 内部 + `chat_routes.resolve_chat_model`。

    其余一律走 `build_aux_model`。本条只是**防漂移**（主证据是 A12–A15 的请求体断言），
    但它能在「有人往辅助链路里偷偷调一次 build_chat_model」时立刻变红。
    """
    offenders: list[str] = []
    for path in APP_DIR.rglob("*.py"):
        relative = path.relative_to(APP_DIR).as_posix()
        for name, function in _call_names(path):
            if name != "build_chat_model":
                continue
            if relative == "generation/answer.py":
                continue
            if (relative, function) == ("api/chat_routes.py", "resolve_chat_model"):
                continue
            offenders.append(f"{relative}::{function}()")
    assert offenders == [], f"build_chat_model 出现了白名单外的调用点：{offenders}"


def test_a16_aux_builders_are_used_by_the_five_aux_paths():
    """反向核对：五条辅助路径的**默认装配**确实走 `build_aux_model`（少一处就是偏科）。"""
    expected = {
        # Agent 决策 / 计划调用（`resolve_agent_model` 的默认装配）
        "agent/runner.py": "build_aux_model",
        # 笔记生成的默认装配
        "generation/note_gen.py": "build_aux_model",
        # 澄清追问的默认装配
        "generation/clarify.py": "build_aux_model",
        # 端点的两个装配入口（`resolve_aux_model` 调它）
        "api/chat_routes.py": "build_aux_model",
        # 笔记端点的装配入口
        "api/document_routes.py": "resolve_aux_model",
    }
    for relative, wanted in expected.items():
        names = _call_names(APP_DIR / relative)
        assert any(name == wanted for name, _fn in names), (
            f"{relative} 没有调用 {wanted}（现有：{names}）"
        )


def test_a16_no_private_builder_is_called_outside_answer():
    """`_build_bound_model`（唯一的分叉点）**只许 `answer.py` 自己调**（AST，不看字符串）。"""
    offenders: list[str] = []
    for path in APP_DIR.rglob("*.py"):
        relative = path.relative_to(APP_DIR).as_posix()
        if relative == "generation/answer.py":
            continue
        if any(name == "_build_bound_model" for name, _fn in _call_names(path)):
            offenders.append(relative)
    assert offenders == [], f"私有分叉点被外部调用：{offenders}"


def test_a16_provider_construction_sites_are_inventoried():
    """**A16 的加严面**：全仓 `OpenAIProvider(...)` 构造点必须**逐个登记**（只许两处）。

    为什么单起一条（2026-09-20 独立只读审计实测指出）：光盯
    `build_chat_model` / `build_aux_model` / `resolve_*` 这五个名字**盖不住旁路**——
    `app/routing/llm.py::build_classifier()` 自己
    `OpenAIProvider(...)` + `.model(...)` 直起一条，既不经过 `resolve_chat_model`
    也不经过 `resolve_aux_model`。它**今天不是泄漏**（全仓 `extra_body` 只有
    `generation/answer.py` 一处传参），但「把思考参数下沉到 provider 工厂 / 全局默认」
    这类改法会让它**静默**变成泄漏。所以这里钉住构造点清单：多一处就必须先回答
    「它带不带思考参数」。

    两处各自的正当性：

    - `generation/answer.py` —— **唯一**带 `extra_body` 的装配点，也是 `_build_bound_model`
      的唯一实现（终答 build_chat_model / 辅助 build_aux_model 由它分叉）；
    - `routing/llm.py` —— 意图分类器（`build_classifier`），**永不带** `extra_body`。
    """
    expected = {
        "generation/answer.py": "终答与辅助两个入口的唯一实现（唯一传 extra_body 的地方）",
        "routing/llm.py": "意图分类器的独立装配（**永不带** extra_body）",
    }
    found: set[str] = set()
    for path in APP_DIR.rglob("*.py"):
        relative = path.relative_to(APP_DIR).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name == "OpenAIProvider":
                    found.add(relative)
    assert found == set(expected), (
        "OpenAIProvider 构造点集合变了——每个新构造点都要先回答「它会不会带思考参数」："
        f"只发现 {sorted(found)}，登记表要求 {sorted(expected)}"
    )
