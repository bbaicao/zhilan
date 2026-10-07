"""``回归检查`` 判据载体：**审计三件**（成功 / 失败 / 取消）+ ``meta`` 三个写通道专属键。

## 本件覆盖的判据

| 判据 | 腿 | 用例 |
|---|---|---|
| **``A12a``** | 写通道**专属谓词**（成功行） | ``test_a12a_success_row_carries_the_three_write_keys`` |
| | 写通道**专属谓词**（失败行） | ``test_a12a_failure_row_is_still_written`` |
| | **只读工具零影响**（负控） | ``test_a12a_readonly_rows_have_no_write_keys`` |
| **``A12b``**（**登记项**） | ``AGENT_ENABLE_AUDIT=False`` ⇒ 零行 | ``test_a12b_audit_off_writes_no_rows`` |
| **``A12c``** | 取消后仍留痕 | ``test_a12c_cancel_still_leaves_a_row`` |
| **红线 13 支撑面** | ``meta`` **不得**落凭证原文 | ``test_meta_never_carries_the_confirm_token`` |

## 🔴 A12a 的「专属谓词」为什么必须用三键、而不是 ``tool_name``

``回归检查`` 规格（路线件 ``:468``）要求「``meta`` 增**写通道专属键**」，而 ``回归检查`` 的
风险点又逐字要求「判据须能区分『写工具的审计行』与『其它工具的审计行』」。
⇒ 只断 ``tool_name == "workspace_write"`` **不够**：那只证明「有个行叫这个名字」，
证明不了「这行记的是**这次写**」—— 写工具**失败**时它照样落 ``tool_name``。
⇒ 本件把谓词钉成 **``tool_name`` + 三键的来源一致性**（``write_target`` 与本次目标逐字相等）。

## 🔴 与 ``A20`` 腿③ 同款的「只增」纪律

三键**只增**：只读工具的 ``meta`` 键集必须**逐字不变**（``{ok, ms, args}``，失败时 ``+error``）。
本件用负控（``test_a12a_readonly_rows_have_no_write_keys``）钉住它 ——
没有这条，把 ``write_meta`` 无条件塞进所有行也能过正腿。

## 载体落点与依赖（如实登记）

``backend/tests/agent/`` **没有 ``conftest.py``**（实测 ``Test-Path`` = ``False``）⇒ 夹具**自带**，
``tests/api/conftest.py`` 的 autouse 钉子对本目录**不生效**（与 ``test_tool_paths_t3.py`` 同款先例）。

⚠️ **本笔的依赖状态**：``回归检查`` 的三键**通道**已落地（``audit.write_tool_step`` 的
``write_meta`` 参数 + ``runner._write_meta_of`` + ``WorkspaceWriteArgs`` 侧的挂载）；
而**真实写盘**（``_write.write_workspace_file``）归 ``回归检查``/``回归检查``/``回归检查``。
⇒ 本件用**注入替身**（``build_spec(write=fake)``）构造写工具的成败两态 ——
那**不是**空靶：被测的是「``WriteOutcome`` → 观察 → ``ToolAudit`` → ``meta``」这条**通道**，
与写本体是否真落盘**正交**（写本体由 ``回归检查`` 的 ``A7-1``/``A8``/``A9`` 判）。
⚠️ 待 ``_write.py`` 落地后，本件**须补一条真写盘腿**（走真文件、断言真 ``byte_delta``）。
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.agent import audit as agent_audit
from app.agent import runner
from app.agent.registry import ToolRegistry, ToolSpec
from app.agent.runner import AgentTurn
from app.agent.tools import knowledge_search
from app.agent.tools import workspace_write
from app.agent.types import ToolObservation
from app.core.config import settings
from app.models import AgentStep
from tests.agent.test_runner import (
    CollectingRetrieval,
    collect,
    faux_model,
    registry_with,
)

#: 写通道三键（``D-回归检查`` ⑥ + ``audit.WRITE_META_KEYS`` 逐字）
WRITE_KEYS = ("write_target", "write_byte_delta", "write_snapshot_id")
#: 只读工具的 ``meta`` 键集（``audit.write_tool_step`` 的既有一行，**逐字冻结**）
READONLY_META_KEYS = {"ok", "ms", "args"}


@pytest.fixture(autouse=True)
def pin_web_off(monkeypatch):
    """联网默认关：本件不测联网面（本目录无 ``conftest.py``，钉子自带）。"""
    monkeypatch.setattr(settings, "WEB_ENABLE", False)


@pytest.fixture(autouse=True)
def pin_workspace_root(tmp_path, monkeypatch):
    """把工作区根指向一个**真目录**（``tmp_path``）—— ``回归检查` 的**必要前提**。

    🔴 **为什么必须有这条**：``workspace_write`` 的 handler 在**调用写函数之前**先
    ``_resolve_root()``，未配置（出厂档 ``""``）⇒ 直接返
    ``PATH_NO_ROOT_MESSAGE``（``ok=False``）⇒ **注入的写函数一次都不会被调用**
    ⇒ ``observation.write_meta`` **永不挂** ⇒ 三键永不产生 ⇒ ``A12a`` 不可达。
    这**不是**产品缺陷（「未配置根 ⇒ 功能不可用」是单⑨ (c) 的定档），而是用例**缺前提**。

    ⚠️ 红线 12：根**一律** ``tmp_path``，**绝不**指向仓库根 / data / dist / 证据目录
    （``SENSITIVE_SEGMENTS`` 含 ``data`` ⇒ 用仓库 ``data`` 会被自己的敏感判定拒掉）。
    ⚠️ 本夹具**只钉根**，**不碰** ``_write.py``（写本体由 ``回归检查/3/4`` 交付，本件全程注入替身）。
    """
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(root))
    import app.agent.tools._paths as _paths  # noqa: PLC0415

    assert _paths.resolve_workspace_root() is not None, "前提：根必须真解析得出来"
    return root


#: 测试用密钥（**只活在进程内**，绝不落 `.env`、绝不入库 —— 铁律 3）。
TEST_CONFIRM_SECRET = "audit-carrier-secret-not-persisted"


@pytest.fixture(autouse=True)
def pin_confirm_secret(monkeypatch):
    """配好 `WORKSPACE_CONFIRM_SECRET` —— **凭证闸已接线后**的必要前提。

    🔴 **为什么必须有**（2026-09-26 · 凭证闸接线后如实登记）：
    `回归检查` 把凭证闸接进了 `workspace_write` 的 handler（红线 13 第二款的**行为面**）
    ⇒ **无有效凭证 ⇒ `ok=False`、写函数一次都不被调用**。
    而本件的成功腿**本来就该**带一张真凭证（否则它测的是「被拒」而不是「写成功」）。

    ⚠️ 密钥为空 ⇒ `verify_write_confirm` 一律返 `missing`（fail-closed）⇒ 本件所有
    成功腿会红 ⇒ 那是**预期**（`D-回归检查`：空密钥 = 写通道整体不可用）。
    """
    monkeypatch.setattr(settings, "WORKSPACE_CONFIRM_SECRET", TEST_CONFIRM_SECRET, raising=False)
    # 🔴 **必须清「已用集合」**（本件多腿会为同一 (path, content) 签发凭证）：
    # `issue_write_confirm` 的 `ts = int(time.time())` 是**秒级** ⇒ **同一秒内**
    # 为同一 (path, content) 签发的 token **逐字节相同** ⇒ 若前一条腿已消费它（`mark_used`），
    # 本条的 token 会命中 `replayed` ⇒ 调用被拒 ⇒ 腿**假红**（实测：单跑绿 / 整件跑红）。
    # ⇒ 用模块自带的测试钩子把已用集合清干净（这正是它存在的理由）。
    from app.agent.tools import _confirm as _confirm_mod  # noqa: PLC0415

    _confirm_mod.reset_used_for_tests()


def _issue(root, rel: str, content: bytes) -> str:
    """宿主侧签发一张**真凭证**（`D-回归检查` ①：`issue_write_confirm` 是进程内函数）。

    ⚠️ 绑定口径与 handler 一致：`content` 传的是**逻辑内容的 UTF-8 字节**
    （即 `str(args.content).encode("utf-8")`），**不是**目标文件原编码的字节。
    """
    from app.agent.tools import _confirm  # noqa: PLC0415

    return _confirm.issue_write_confirm(root=root, rel_path=rel, content=content)


class _WriteOutcome:
    """``_write.write_workspace_file`` 的返回替身（``D-回归检查`` ⑥ 的三键 + 文案）。"""

    def __init__(self, *, target: str, byte_delta: int, snapshot_id: str | None, text: str = ""):
        self.target = target
        self.byte_delta = byte_delta
        self.snapshot_id = snapshot_id
        self.text = text or f"已写入工作区文件：{target}"


def _write_spec(write):  # noqa: ANN001
    """把 ``workspace_write`` 的声明接上一个**注入的**写函数（成功路径可控）。"""
    spec = workspace_write.build_spec(write=write)
    assert spec.needs_confirm is True, "写工具必须显式声明 needs_confirm=True"
    return spec


async def _run_tool(turn, *, spec, tool_name, args, db=None, chat_id=None, turn_no=1):  # noqa: ANN001
    """跑一轮 ReAct，让模型点名调用 ``tool_name``（返回 ``(frames, turn)``）。

    ## 🔴 为什么**不**注入手工包的 ``AgentTool``（五条红的**共同根因**）

    ``stream_agent_turn`` 在 ``runner.py:1656`` **自建** ``collector = ToolCollector()``，
    ``build_agent_tools`` 的 **``tools`` 注入只换清单、不换 collector**（``runner.py:1041``：
    ``self.collector or ToolCollector()``）。而审计行的**唯一**来源是
    ``_absorb_tool`` → ``collector.audits`` → 消费端 ``_flush_audit``。

    ⇒ 一旦手工包一个 ``AgentTool`` 并把它塞进 ``tools=[...]``，这个闭包就捕获了**另一份**
    collector；运行器那份 collector 永远是空的 ⇒ ``collector.audits == []`` ⇒
    ``_flush_audit`` **循环零次** ⇒ ``agent_steps`` **零行**。
    这就是「五条红里四条都断言 ``rows`` 非空」的**同一个**根因（不是实现缺陷）。

    ✅ **正确构造法**（照 ``tests/agent/test_agent_audit.py:211-220`` 的既有先例）：
    **不传 ``tools``**、**不手工造 collector** —— 让 ``build_agent_tools`` 走真装配，
    ``AgentTool`` 闭包捕获的**就是运行器自建的那一份** collector，记账才能一路到 ``_flush_audit``。
    ``registry_with(spec)`` 已足够让模型点名的工具可解析（``_spec_map`` 按名字反查）。
    """
    from tests.agent.test_runner import says, tool_call

    model = faux_model([tool_call(tool_name, args), says("好")])
    kwargs: dict = {
        "question": "问题",
        "model": model,
        "registry": registry_with(spec),
    }
    if db is not None:
        kwargs.update({"db": db, "chat_id": chat_id, "turn_no": turn_no})
    frames = await collect(turn, **kwargs)
    return frames, turn


async def _new_chat(db) -> int:  # noqa: ANN001
    """造一条真 chat（审计行需要 ``chat_id`` 才能建 run）。"""
    from app.models import Chat

    chat = Chat(title="T3-5-6 审计用例")
    db.add(chat)
    await db.flush()
    return int(chat.id)


async def _rows(db, run_id):  # noqa: ANN001
    """取该 run 的 ``agent_steps`` 行（按 ``step_no`` 升序）。"""
    result = await db.execute(
        select(AgentStep).where(AgentStep.run_id == int(run_id)).order_by(AgentStep.step_no)
    )
    return list(result.scalars().all())

# ---------------------------------------------------------------------------
# A12a · 成功行带三键（正腿）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a12a_success_row_carries_the_three_write_keys(db, pin_workspace_root):  # noqa: ANN001
    """**A12a 正腿**：写成功 ⇒ ``agent_steps`` 有一条**带三键**的行，且 ``write_target`` 与本次目标一致。

    「专属谓词」= ``tool_name`` **且** ``write_target`` 与本次写目标**逐字相等** ——
    只断名字证明不了「这行记的是这次写」（见模块 docstring）。
    """
    from app.agent.bootstrap import build_registry  # noqa: PLC0415

    target = "notes/写通道.txt"

    # 🔴 **必须是同步 `def`**：`workspace_write` 的 handler **同步调用**注入的写函数
    # （`workspace_write.py:178` 的 `func(...)` **没有 await**）⇒ 写成 `async def`
    # 只会拿到一个从未 await 的协程 ⇒ `outcome` 不是 `WriteOutcome` ⇒
    # `_write_meta_from` 取不到三键 ⇒ `meta` 只剩 `{ok, ms, args}`（实测红因）。
    def fake_write(**_kwargs):  # noqa: ANN003
        return _WriteOutcome(target=target, byte_delta=11, snapshot_id="snap-abc")

    spec = _write_spec(fake_write)
    turn = AgentTurn(mode="react")
    frames, turn = await _run_tool(
        turn,
        spec=spec,
        tool_name=workspace_write.TOOL_NAME,
        args={
            "path": target,
            "content": "hello world",
            # 🔴 **凭证闸接线后必须带真凭证**（否则 handler 在写之前就拒 ⇒ 本腿测的是「被拒」）
            "confirm_token": _issue(pin_workspace_root, target, b"hello world"),
        },
        db=db,
        chat_id=await _new_chat(db),
    )

    assert turn.run_id is not None, "本轮必须建了审计 run（否则下面查不到行）"
    rows = await _rows(db, turn.run_id)
    assert len(rows) == 1, f"一次写调用应恰好落一行，实得 {len(rows)}"
    row = rows[0]
    assert row.tool_name == workspace_write.TOOL_NAME
    meta = dict(row.meta or {})
    # 🔴 专属谓词：三键全在 + 目标逐字一致
    for key in WRITE_KEYS:
        assert key in meta, f"写通道专属键缺失：{key}（meta={sorted(meta)}）"
    assert meta["write_target"] == target
    assert meta["write_byte_delta"] == 11
    assert meta["write_snapshot_id"] == "snap-abc"
    assert meta["ok"] is True
    # 三键是**只增**：既有三键仍逐字在
    assert READONLY_META_KEYS <= set(meta)
    del frames  # 本腿只看库


@pytest.mark.asyncio
async def test_a12a_readonly_rows_have_no_write_keys(db):  # noqa: ANN001
    """**A12a 负控**：只读工具的 ``meta`` 键集**逐字不变**（无三键）。

    ⚠️ 没有这条，把 ``write_meta`` **无条件**塞进每一行也能过正腿 ——
    而「只读工具的审计行多出三个恒空键」是**真回归**（``A20`` 腿③ 同款的「只增/不破」纪律）。
    """
    retrieval = CollectingRetrieval([[]])
    spec = knowledge_search.build_spec(db=object(), retrieve=retrieval)
    turn = AgentTurn(mode="react")

    from tests.agent.test_runner import says, tool_call  # noqa: PLC0415

    # 🔴 **不传 tools / 不手工造 collector** —— 理由见 `_run_tool` 的 docstring（共同的五红根因）
    model = faux_model([tool_call("knowledge_search", {"query": "q"}), says("好")])
    chat_id = await _new_chat(db)
    await collect(
        turn,
        question="问题",
        model=model,
        registry=registry_with(spec),
        db=db,
        chat_id=chat_id,
        turn_no=1,
    )

    rows = await _rows(db, turn.run_id)
    assert rows, "只读工具调用也必须落审计行"
    meta = dict(rows[0].meta or {})
    assert set(meta) == READONLY_META_KEYS, (
        f"只读工具的 meta 键集被改动了：多 {sorted(set(meta) - READONLY_META_KEYS)} / "
        f"少 {sorted(READONLY_META_KEYS - set(meta))}"
    )
    for key in WRITE_KEYS:
        assert key not in meta, f"只读工具的行不该有写通道键：{key}"


# ---------------------------------------------------------------------------
# A12a · 失败行（规格逐字：成功与失败都要有）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a12a_failure_row_is_still_written(db):  # noqa: ANN001
    """**A12a 失败腿**：写**失败** ⇒ 仍有 ``agent_steps`` 行（``ok=False``）。

    ⚠️ 失败路径**不得**带三键的**假值**（``write_byte_delta=0`` 会让「没写成」与
    「写了 0 字节」不可区分）—— 本腿同时钉住这一点。
    """

    # 同步 `def`（同正腿的理由）：handler **不 await** ⇒ `async def` 连异常都抛不出来，
    # 会退化成「成功返回一个协程」⇒ 失败腿与成功腿不可区分。
    def boom_write(**_kwargs):  # noqa: ANN003
        raise OSError("磁盘只读（用例注入）")

    spec = _write_spec(boom_write)
    turn = AgentTurn(mode="react")
    await _run_tool(
        turn,
        spec=spec,
        tool_name=workspace_write.TOOL_NAME,
        args={"path": "a.txt", "content": "x"},
        db=db,
        chat_id=await _new_chat(db),
    )

    rows = await _rows(db, turn.run_id)
    assert len(rows) == 1, "失败也必须留痕（规格逐字：成功与失败都要有）"
    meta = dict(rows[0].meta or {})
    assert meta["ok"] is False
    # 🔴 三键**一个都不许有**（失败路径无从产生 byte_delta）
    for key in WRITE_KEYS:
        assert key not in meta, f"失败行不该带写通道键 {key}（会伪造「写了 0 字节」）"


# ---------------------------------------------------------------------------
# A12b · 登记项（AGENT_ENABLE_AUDIT=False ⇒ 零行）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a12b_audit_off_writes_no_rows(db, monkeypatch, pin_workspace_root):  # noqa: ANN001
    """**A12b（登记项）**：``AGENT_ENABLE_AUDIT=False`` ⇒ 写操作**零** ``agent_steps`` 行。

    ⚠️ 这是**登记项**而不是「要击杀的行为」：它等于**用户放弃可追溯**
    （``回归检查`` 交付第 4 条明文要求逐字登记）。本腿的作用是把这条**用户选择**
    变成可复跑的事实，防止将来有人把「关开关也留痕」当成 bug 去修。
    """
    monkeypatch.setattr(settings, "AGENT_ENABLE_AUDIT", False)
    assert agent_audit.audit_enabled() is False, "前提：开关确实关上了"

    # 同步 `def`（同正腿的理由）
    def fake_write(**_kwargs):  # noqa: ANN003
        return _WriteOutcome(target="a.txt", byte_delta=3, snapshot_id=None)

    spec = _write_spec(fake_write)
    turn = AgentTurn(mode="react")
    await _run_tool(
        turn,
        spec=spec,
        tool_name=workspace_write.TOOL_NAME,
        args={
            "path": "a.txt",
            "content": "abc",
            # 🔴 凭证闸接线后必须带真凭证（否则 handler 在**写之前**就拒 ⇒
            #    「审计关 ⇒ 零行」会被「压根没走到写」污染，本腿退化）
            "confirm_token": _issue(pin_workspace_root, "a.txt", b"abc"),
        },
        db=db,
        chat_id=await _new_chat(db),
    )

    # 🔴 **本腿的判据面被 Lead 实测更正过**（2026-09-26 · 如实登记全过程）：
    #
    # **原写法**：`if turn.run_id is not None: assert await _rows(db, turn.run_id) == []`
    # —— 唯一断言被条件包住 ⇒ 一旦 `run_id` 为 `None`，本腿**没有任何断言在跑**
    #（它仍是「绿」，但那是**空转的绿**，且无 skip 标记、无警告）。
    #
    # `audit-pkg-a` 的对抗审计判它是「当前在跑、但结构上可静默退化」（依据：
    # `audit.start_run` 不看 `audit_enabled()`）。🔴 **Lead 实测推翻了这个判断**：
    # 把条件换成硬断言后本腿**立刻红** ⇒ 证明**审计关时 `run_id` 确实是 `None`**
    # ⇒ 原写法在本腿上是**死代码**（不是「未来可能退化」，是**当时就没在断言**）。
    #
    # ⇒ **正确判据面** = 「审计关 ⇒ **全库零 agent_steps 行**」，而**不是**
    #    「按 `run_id` 查得 0 行」—— 后者在 `run_id is None` 时**不可观测**。
    #    这也与 `A12b` 原文口径一致：「AGENT_ENABLE_AUDIT=False ⇒ 零行」。
    rows_all = list((await db.execute(select(AgentStep))).scalars().all())
    assert rows_all == [], (
        f"审计关 ⇒ 全库**零** agent_steps 行（登记项，非缺陷）；实测 {len(rows_all)} 行"
    )
    # 反向自证：本轮**真的跑过工具**（否则「零行」可能只是「压根没调工具」）
    assert any(step.get("kind") == "tool" for step in turn.steps), (
        "本轮没有 tool 步 ⇒ 「零行」是空转 ⇒ 本腿不成立"
    )


# ---------------------------------------------------------------------------
# 红线 13 支撑面 · token 原文绝不进 meta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_meta_never_carries_the_confirm_token(db, pin_workspace_root):  # noqa: ANN001
    """**``C-R1(乙)`` 的机器守卫**：凭证原文 / 密钥**绝不**落 ``agent_steps.meta``。

    为什么这是红线 13 的支撑面：若 token 能被落进 ``meta``，而 ``meta`` 会被
    ``GET /api/agent/audit`` 读出去，那模型（或读到该端点的人）就**能自己伪造一份凭证**
    ⇒ 等价于「存在只有模型能读到的确认通道」（红线 13 第二款逐字禁止）。

    ⚠️ 白名单是**结构性**防线（``audit.WRITE_META_KEYS``），本腿是它的**活体证明**：
    故意往 args 塞一个 token，断言它**进不了** ``meta``。
    """
    # 🔴 用一张**真凭证**（凭证闸接线后，假串会在写之前被拒 ⇒ 本腿退化成空靶）
    secret = _issue(pin_workspace_root, "a.txt", b"abc")
    assert secret and secret != "SENTINEL-TOKEN-DO-NOT-LOG", "签发失败 ⇒ 本腿不成立"
    seen: dict = {}

    # 同步 `def`（同正腿的理由）—— 本腿还额外接住形参做透传断言
    def fake_write(**kwargs):  # noqa: ANN003
        # 🔴 **接住五个 keyword-only 形参**（`D-回归检查` ⑥ 的逐字签名）。
        # 不接住就看不见「凭证到底有没有透传」——那样本条只能断「没落库」，
        # 而「透传签名写错」这一半（handler 漏传 `confirm_token`）会**静默通过**。
        seen.update(kwargs)
        return _WriteOutcome(target="a.txt", byte_delta=3, snapshot_id=None)

    spec = _write_spec(fake_write)
    turn = AgentTurn(mode="react")
    await _run_tool(
        turn,
        spec=spec,
        tool_name=workspace_write.TOOL_NAME,
        args={"path": "a.txt", "content": "abc", "confirm_token": secret},
        db=db,
        chat_id=await _new_chat(db),
    )

    # 正控：写函数**真被调用过**，且凭证**原样透传**（`workspace_write.py:185`）
    assert seen, "注入的写函数一次都没被调到 ⇒ 下面的断言全是空靶"
    assert seen.get("confirm_token") == secret, (
        f"凭证没原样透传给写函数（handler 漏传 / 改名）：{sorted(seen)}"
    )
    # 签名面：五个 keyword-only 形参齐备（少一个 ⇒ TypeError ⇒ 该腿不成立）
    assert {"root", "rel_path", "data", "create", "snapshot"} <= set(seen), sorted(seen)

    rows = await _rows(db, turn.run_id)
    assert rows
    dumped = str(rows[0].meta)
    assert secret not in dumped, "凭证原文进了 meta ⇒ 红线 13 第二款被破"
    # 白名单自证：meta 只含既有三键 + 写通道三键，**没有第四个键**
    assert set(rows[0].meta or {}) <= (READONLY_META_KEYS | set(WRITE_KEYS)) | {"error"}

# ---------------------------------------------------------------------------
# A12c · 取消后仍留痕
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a12c_cancel_still_leaves_a_row(db, pin_workspace_root):  # noqa: ANN001
    """**A12c**：写调用中途**取消** ⇒ 取消路径上**不得静默丢已记账的行**。

    ## 🔴 判据口径（**Lead 定档 2026-09-26** · 逐字登记，不得拔高）

    > ``A12c`` 按「取消后**若**留有行则必须成对且可归因」判定；
    > 「取消**必然**产生一行」**本阶段不主张**（记账在 handler 返回之后）。

    三条理由（Lead 裁决原文要点）：

    1. 开工包 ``A12c`` 逐字只要求「**取消后仍留痕**」——**没写**「取消必然产生一行」；
    2. ``_absorb_tool`` 的**记账在 handler 返回之后**是**既有语义**（4.3 起如此）；
       把取消点后移到记账之后属**改产品行为**，且会与 ``4.4`` 的 ``events_incomplete`` 口径打架
       ⇒ **超出本阶段范围**；
    3. 用 ``started`` + ``turn.run_id`` 把「构造失败」与「真缺陷」分开、零行时**明确登记**
       （而不是主张「取消必有行」）—— 这是「**不制造假绿**」的正确做法。

    ## 为什么「取消 ⇒ 零行」在这里**不是**缺陷（机制）

    ``runner._run_tool_handler`` 只在 handler **返回之后**由 ``_absorb_tool`` 记账
    （``runner.py:1267-1285``），而 ``asyncio.CancelledError`` 会直接**穿过去**。
    本腿的取消点骑在 handler **内部** ⇒ 那时运行器那份 collector **还是空的**
    ⇒ 消费端 ``_flush_audit`` 循环零次 ⇒ **审计零行 = 既有语义**。

    ⇒ 本腿真正钉的是：**到达 ``_absorb_tool`` 的记账一条不少地落库**，
    以及「取消路径连 audit run 都建不起来」这类**链路级**故障必须红。

    ⚠️ **与 ``A7-2``（写侧取消腿）的分工**：``A7-2`` 断的是**盘上文件内容**
    （新/旧之一 + 无残留临时文件）；本腿断的是**审计行**。两者不同观察量。
    """
    started = asyncio.Event()

    # 🔴 **同步 `def`**（与其余四条腿同源修法）：handler **不 await** 注入的写函数
    # （`workspace_write.py:178` 的 `func(...)` 是同步调用）⇒ 写成 `async def`
    # 只会「立刻返回一个协程」，**根本不会挂住** ⇒ 取消窗口不可构造、本腿失去意义。
    #
    # ⚠️ 因此**阻塞必须在同步函数里做**，且要满足两个约束：
    # ① **不能阻塞事件循环**（`time.sleep` 会让 `task.cancel()` 永远排不上）⇒ 用 `threading.Event`；
    # ② **必须真的挂在工具执行中**，这样 `_as_agent_tool.execute` 的
    #    `async with _session_lock(...)` 段处在「已发起、未记账」的窗口里。
    #
    # 🔴 **本条断的到底是什么（如实登记，不得拔高）**：`_absorb_tool` 的**记账**发生在
    # handler **返回之后** ⇒ 取消若骑在 handler 内部，运行器那份 collector 是**空的**
    # ⇒ 消费端 `_flush_audit` 循环零次 ⇒ **审计零行**。
    # 本腿因此**不**断言「取消 ⇒ 有行」（那会与 `_absorb_tool` 的既有语义冲突，
    # 属实现侧口径、不在本件写入面内）；它断言的是本笔**真正交付**的那一条：
    # **取消路径上不静默丢已记账的行** —— 即「到达 `_absorb_tool` 的调用**一条不少**地落库」。
    # ⚠️ 请与 `A7-2`（写侧取消腿，断盘上文件）区分：观察量不同。
    release = threading.Event()

    def slow_write(**_kwargs):  # noqa: ANN003
        started.set()
        release.wait(timeout=5)  # 挂在工具执行中，等用例放行
        return _WriteOutcome(target="a.txt", byte_delta=1, snapshot_id=None)

    spec = _write_spec(slow_write)
    turn = AgentTurn(mode="react")
    chat_id = await _new_chat(db)

    from tests.agent.test_runner import says, tool_call  # noqa: PLC0415

    # 🔴 **不传 tools / 不手工造 collector**（五红共同根因，见 `_run_tool` docstring）
    model = faux_model(
        [
            tool_call(
                workspace_write.TOOL_NAME,
                {
                    "path": "a.txt",
                    "content": "x",
                    # 🔴 凭证闸接线后必须带真凭证，否则 handler 在写之前就拒 ⇒ 本腿不成立
                    "confirm_token": _issue(pin_workspace_root, "a.txt", b"x"),
                },
            ),
            says("好"),
        ]
    )

    async def consume() -> None:
        async for _ in runner.stream_agent_turn(
            turn,
            question="问题",
            model=model,
            registry=registry_with(spec),
            db=db,
            chat_id=chat_id,
            turn_no=1,
        ):
            pass

    task = asyncio.ensure_future(consume())
    await asyncio.wait_for(started.wait(), timeout=5)  # 工具**真的**进了执行体
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()  # 放行被取消后遗留的阻塞线程（避免 5s 内空转；非判据）

    # ── A12c 的判据面（**正面腿**：已记账的行一条不少）────────────────────
    # 🔴 取消时若已有**记账**（`collector.audits` 非空），消费端 `_flush_audit` 的收尾兜底
    # 仍必须把它们落库 —— 「取消 ⇒ 已记账的行静默消失」才是要抓的缺陷。
    rows = await _rows(db, turn.run_id) if turn.run_id is not None else []

    # 本腿**必须能区分**两种成因的零行（否则它可能是「构造失败」而非「真缺陷」）：
    # ① 取消骑在 handler 内部 ⇒ 尚未记账 ⇒ 零行是 `_absorb_tool` 的**既有语义**（非缺陷）；
    # ② 已记账却零行 ⇒ **真缺陷**（本腿的靶心）。
    # ⇒ 用「工具是否真的进过执行体」+「run 是否建成」两个可观察量把①与②分开。
    assert started.is_set(), "工具从未进入执行体 ⇒ 本腿的前置没成立（用例构造问题，不是产品缺陷）"

    # 每一行都必须是本工具这一次调用的 tool_call 行（有行时逐行核）
    for row in rows:
        assert row.tool_name == workspace_write.TOOL_NAME, row.tool_name
        assert row.type == "tool_call", row.type

    # 🔴 零行 / 有行都**必须**能判归因（不许含糊过关）：
    #
    # · **有行** ⇒ 取消未丢账（已记账的落库了）—— 正面满足；
    # · **零行** ⇒ 仅在「取消骑在 handler 内部、尚未记账」时可接受
    #   （`_absorb_tool` 的记账发生在 handler **返回之后**，那是**既有语义**，非缺陷）。
    #
    # 用两个可观察量把「构造失败」与「真缺陷」分开：
    #   ① `started` 已置 ⇒ 工具**真的**进过执行体（否则本腿前置不成立，属用例问题）；
    #   ② `turn.run_id` 非 None ⇒ 审计 run 链路真的建起来了。
    assert started.is_set(), "工具从未进入执行体 ⇒ 本腿前置不成立（用例构造问题，不是产品缺陷）"
    assert turn.run_id is not None, (
        "取消路径连 audit run 都没建成 ⇒ 收口链路有问题（不是「取消赶在记账前」能解释的）"
    )

    # 口径登记（**不得拔高**）：本腿证明的是「**取消路径上不静默丢已记账的行**」。
    # 它**不**主张「取消必有行」—— 取消点若落在 `_absorb_tool` **之前**（本腿的构造即如此），
    # 零行是既有语义。要主张「取消 ⇒ 有行」需把取消点后移到 `_absorb_tool` 之后，
    # 那属实现侧改动，**不在本件写入面内**。
    assert len(rows) <= 1, f"一次工具调用最多一行，实得 {len(rows)} 行"

# ---------------------------------------------------------------------------
# WP-F · 红线 13：「args」侧的敏感键遮蔽（task-29）
# ---------------------------------------------------------------------------
#
# ## 为什么这组腿与上面那条「token 不进 meta」**不是同一件事**（重要，防冒充）
#
# 上面那条断的是 **`WRITE_META_KEYS` 白名单**（「只放行三键」）—— 管的是**新增**的那三个键。
# 而本组断的是 **`_args_digest` 的遮蔽表** —— 管的是 **`meta["args"]` 这一侧**。
# 两处是**不同的数据流**：`confirm_token` **从来不是** `WRITE_META_KEYS` 的成员，
# 它走的是 `args` 摘要。⇒ 只挡一侧必然漏另一侧（这正是本缺陷曾经存在的形态）。
#
# 🔴 **反例（未修前实测）**：
# `audit._args_digest(M()) => {'path': 'a.txt', 'content': 'x', 'confirm_token': 'SECRET-T', 'create': 'false'}`
# ⇒ 原文落 `agent_steps.meta["args"]` ⇒ 被 `GET /api/agent/audit` 读出 ⇒
# 而 `run_id` 就在 `done.agent_trace` 里 ⇒ **模型/用户拿得到 token 原文** ⇒ 可重放
# ⇒ 违红线 13 第二款「不得存在只有模型能读到的确认通道」。


def test_wpf_sensitive_key_table_is_exported_constants():
    """**判据 ③（缺一即不合格）**：遮蔽表是**导出的常量**（防「同一清单多处漂移」）。

    ⚠️ 为什么必须是常量而不是内联字面量：判据要**引用同一份清单**。
    若遮蔽表散在实现里，判据只能抄一份副本 ⇒ 两份清单迟早漂移
    （`_paths.SENSITIVE_SEGMENTS` 与 `K-回归检查` 的「同名两对象」是同一族教训）。
    """
    assert isinstance(agent_audit.SENSITIVE_ARG_KEYS, frozenset)
    assert agent_audit.SENSITIVE_ARG_MASK == "***"
    # `confirm_token` 必须在表里（它是本缺陷的主角，逐字冻结）
    assert "confirm_token" in agent_audit.SENSITIVE_ARG_KEYS
    # 表内容不得为空（否则「遮蔽」恒不生效、本组腿全成空靶）
    assert len(agent_audit.SENSITIVE_ARG_KEYS) >= 6


def test_wpf_args_digest_masks_the_confirm_token():
    """**WP-F 正腿**：`_args_digest` 遮蔽 `confirm_token` 原文（pydantic 与 dict 两种入参）。**"""
    secret = "SECRET-T-SENTINEL"

    class _Model:
        def model_dump(self) -> dict:
            return {"path": "a.txt", "content": "x", "confirm_token": secret, "create": False}

    # ReAct 路径：CubeLoop 已把参数校验成 pydantic 实例
    got = agent_audit._args_digest(_Model())
    assert secret not in str(got), f"凭证原文漏进 args 摘要：{got}"
    assert got["confirm_token"] == agent_audit.SENSITIVE_ARG_MASK
    # 🔴 遮蔽**不是删除**：键仍在 ⇒ 「这次传了凭证」这个事实在审计里可见
    assert "confirm_token" in got, "键被删掉了 ⇒ 审计里看不出「传了凭证」"
    # 非敏感键**逐字不变**（只增不改的正控）
    #
    # ⚠️ **2026-09-26 改 `"False"` ⇒ `"false"`，理由逐字登记**（``回归修复`` 收敛的**有意差异**）：
    # 此处原值 ``"False"`` 是**照当时 ``audit`` 那份实现的 ``str()`` 输出抄下来的形状快照**，
    # 而**不是**对外契约声明。真正的契约在同族的 :778 处——
    # ``tests/api/test_chat_agent.py:778`` 逐字 ``json.loads(args["filters"])`` ⇒
    # 要求 ``_args_digest`` 对**非字符串值**输出 **JSON 形态**（``json.dumps``），否则 ``json.loads`` 直接炸
    #（该真回归已实测复现）。``092`` 定档「行为契约 > 实现形状快照」⇒ 本行随之改为 JSON 形态的 ``"false"``。
    assert got["path"] == "a.txt" and got["content"] == "x" and got["create"] == "false"

    # P&E / 测试路径：裸 dict
    got2 = agent_audit._args_digest({"path": "b.txt", "confirm_token": secret})
    assert secret not in str(got2)
    assert got2 == {"path": "b.txt", "confirm_token": "***"}


def test_wpf_masking_covers_all_table_entries_and_variants():
    """**WP-F 表驱动腿**：表里**每一条**都真被遮蔽 —— 且大小写/分隔符变体一并覆盖。

    ⚠️ 这条防的是「只给 `confirm_token` 打了个补丁、表里其余条目是摆设」：
    表导出成了常量，但若实现只比对第一条，判据必须能发现。
    """
    for key in agent_audit.SENSITIVE_ARG_KEYS:
        upper = key.upper()
        dashed = key.replace("_", "-")
        for variant in (key, upper, dashed, key.replace("_", "")):
            got = agent_audit._args_digest({variant: "LEAK-ME"})
            assert got[variant] == agent_audit.SENSITIVE_ARG_MASK, (
                f"敏感键变体未被遮蔽：{variant!r} => {got[variant]!r}（表条目 {key!r}）"
            )


def test_wpf_non_sensitive_keys_are_verbatim_unchanged():
    """**WP-F 负控**：非敏感键**逐字不变**（键集 + 值），且不做「包含匹配」误伤。

    ⚠️ 没有这条，把 `_args_digest` 改成「一律返回空 / 一律遮蔽」也能过上面三条正腿 ——
    而那会**毁掉整个审计面**（`meta["args"]` 是排障的唯一参数载体）。
    """
    # ① 典型只读工具参数：键集与值逐字不变
    plain = {"query": "蓝鲸有多大", "limit": 5}
    assert agent_audit._args_digest(plain) == {"query": "蓝鲸有多大", "limit": "5"}

    # ② **包含匹配**是本表**有意不做**的：这些键名含敏感子串，但语义正常 ⇒ 不得遮蔽
    for benign in ("token_count", "secretary", "api_keys_total", "password_hint_len"):
        got = agent_audit._args_digest({benign: "正常值"})
        assert got[benign] == "正常值", (
            f"误伤：{benign!r} 被遮蔽了 ⇒ 审计信息丢失（遮蔽不可逆）"
        )

    # ③ 空 / None 入参仍返回空 dict（既有行为）
    assert agent_audit._args_digest(None) == {}
    assert agent_audit._args_digest({}) == {}


@pytest.mark.asyncio
async def test_wpf_token_never_reaches_the_db_via_args(db):  # noqa: ANN001
    """**WP-F 端到端腿**：真跑一轮 ⇒ 库里 `agent_steps.meta` 的**全文**都不含 token 原文。

    ⚠️ 上面几条断的是 `_args_digest` **单函数**；这条断的是**落库后的成品** ——
    两者不可互相冒充（中间还有 `runner._args_digest` → `ToolAudit` → `write_tool_step`
    → `_args_digest` 再走一遍的链路；若某一环把原文又拼回去，单函数腿抓不到）。
    """
    secret = "E2E-TOKEN-SENTINEL-DO-NOT-LOG"

    def fake_write(**_kwargs):  # noqa: ANN003
        return _WriteOutcome(target="a.txt", byte_delta=3, snapshot_id=None)

    spec = _write_spec(fake_write)
    turn = AgentTurn(mode="react")
    await _run_tool(
        turn,
        spec=spec,
        tool_name=workspace_write.TOOL_NAME,
        args={"path": "a.txt", "content": "abc", "confirm_token": secret},
        db=db,
        chat_id=await _new_chat(db),
    )

    rows = await _rows(db, turn.run_id)
    assert rows, "写调用必须落审计行"
    # 🔴 断**整行**（不只是 meta）：content 里若被拼回凭证同样算破红线
    whole_row = f"{rows[0].content}|{rows[0].meta}"
    assert secret not in whole_row, "凭证原文落进了 agent_steps（红线 13 第二款被破）"
    meta = dict(rows[0].meta or {})
    assert meta.get("args", {}).get("confirm_token") == agent_audit.SENSITIVE_ARG_MASK

# ---------------------------------------------------------------------------
# 执行期真缺陷回归钉 · handler 必须支持 async 写函数（未 await 的协程 = 假成功）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_supports_async_write_without_fake_success(pin_workspace_root):
    """**回归钉**（2026-09-26 实测抓到的真缺陷）：`async def` 写函数必须被 `await`。

    ## 缺陷形态（比 `TypeError` 更坏）

    本 handler 是 `async def`；若写函数也是 `async def` 而实现**同步调用**它，
    拿到的是**未被 await 的协程对象** —— 不报错、不执行、也取不到 `text`
    ⇒ 回落到「已写入…」文案 ⇒ **`ok=True` 而盘上什么都没写**。
    即：**静默的假成功**（`A12a` 的成功行腿照样绿，`write_meta` 恒空）。

    ## 为什么这条腿不可省

    `_write.write_workspace_file` 现在是**同步**的（所以生产暂时不触发），
    但「注入一个 async 写函数」是**本件最自然的构造**（handler 本身就是 async）⇒
    一旦有人把写函数改成 async（异步 IO / `asyncio.to_thread`），本缺陷立刻复活，
    且**没有任何既有判据会红**。本腿把这条路径钉住。
    """
    calls = {"n": 0}

    async def async_write(**_kwargs):  # noqa: ANN003
        calls["n"] += 1
        return _WriteOutcome(target="a.txt", byte_delta=4, snapshot_id="S-ASYNC")

    spec = _write_spec(async_write)
    obs = await spec.handler(  # type: ignore[arg-type]
        workspace_write.WorkspaceWriteArgs(
            path="a.txt",
            content="abcd",
            # 🔴 凭证闸接线后必须带真凭证（否则 handler 在写之前就拒 ⇒ 本腿退化成空靶）
            confirm_token=_issue(pin_workspace_root, "a.txt", b"abcd"),
        )
    )

    # ① 写函数**真被调到**（未 await 时它根本不会执行 ⇒ 计数为 0）
    assert calls["n"] == 1, f"async 写函数没被执行（未 await 的协程）：calls={calls}"
    assert obs.ok is True
    # ② 三键来自**真 outcome**（未 await 时 `_write_meta_from` 拿到协程 ⇒ 恒空 dict）
    meta = getattr(obs, "write_meta", None) or {}
    assert meta.get("write_byte_delta") == 4, f"未拿到真 outcome 的三键：{meta}"
    assert meta.get("write_snapshot_id") == "S-ASYNC"

# ---------------------------------------------------------------------------
# 回归修复 · 嵌套敏感键递归遮蔽（078/WP-F 同族的残留半边）
# ---------------------------------------------------------------------------


def test_wpf127_nested_sensitive_values_are_masked_recursively():
    """**正腿（回归修复）**：任意深度的嵌套 dict/list 里命中敏感键名 ⇒ 值置 ``***``。

    修前：只遮蔽顶层键，嵌套值直接 ``json.dumps`` 原文照落 ——
    MCP 工具入参 ``extra="allow"`` 任意嵌套均可，密钥经 ``meta["args"]`` 直达
    ``GET /api/agent/audit``。
    """
    probe = {
        "opts": {"api_key": "sk-x", "n": 1},
        "tags": [{"token": "T"}],
        "note": "明文",
    }
    out = agent_audit._args_digest(probe)
    assert "sk-x" not in str(out), f"嵌套 api_key 原文漏进 args 摘要：{out}"
    assert "T" not in str(out), f"嵌套 token 原文漏进 args 摘要：{out}"
    assert out["opts"] == '{"api_key": "***", "n": 1}', f"嵌套 dict 遮蔽形状不符：{out['opts']!r}"
    assert out["tags"] == '[{"token": "***"}]', f"列表内 dict 遮蔽形状不符：{out['tags']!r}"
    assert out["note"] == "明文", f"非敏感顶层值必须逐字不变：{out['note']!r}"
    # 入参不得被改动（遮蔽只构造新容器）
    assert probe["opts"]["api_key"] == "sk-x" and probe["tags"][0]["token"] == "T"


def test_wpf127_deeply_nested_and_mixed_shapes_masked():
    """**正腿②（三层嵌套）**：dict→list→dict 的混合形态同样遮蔽（「任意深度」的字面验证）。"""
    out = agent_audit._args_digest({"deep": [{"inner": {"secret": "S", "keep": 2}}]})
    assert "S" not in str(out), f"三层嵌套 secret 原文漏进 args 摘要：{out}"
    assert out["deep"] == '[{"inner": {"secret": "***", "keep": 2}}]', f"形状不符：{out['deep']!r}"


def test_wpf127_nested_negative_controls():
    """**负控**：嵌套非敏感值逐字不变（JSON 解得回 —— 092 ①-e 同款契约）；嵌套 ``token_count`` 不误伤。"""
    got = agent_audit._args_digest({"filters": {"kb_ids": [9], "name": "默认库"}, "opts": {"token_count": 7}})
    assert got["filters"] == '{"kb_ids": [9], "name": "默认库"}', f"非敏感嵌套值形状漂移：{got['filters']!r}"
    import json

    assert json.loads(got["filters"]) == {"kb_ids": [9], "name": "默认库"}, (
        "092 契约：非敏感嵌套值必须 JSON 解得回（不是 Python repr）"
    )
    assert got["opts"] == '{"token_count": 7}', (
        f"嵌套 token_count 被误伤 ⇒ 遮蔽不可逆丢失审计信息：{got['opts']!r}"
    )
