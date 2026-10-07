"""``回归检查`` / ``回归检查`` 判据载体：**凭证闸的【行为面】**（红线 13 第二款）。

## 为什么需要本件（本件的立件理由）

`回归检查` 交付了 `_confirm.verify_write_confirm` 的 **22 条单元腿**（判定次序 / 三要素绑定 /
过期 / 重放 / `compare_digest` …）。但那 22 条**全部**是**对该函数的直接调用** ——
**没有一条**验「**模型可达的那条路径**上，无凭证的写调用真的被拒」。

⇒ 实测发现（2026-09-26）：`workspace_write` 的 handler **原先从不校验 `confirm_token`**
（只把它 `原样透传` 进写函数，而写函数也不用它）⇒ **无凭证的写调用照常落盘**。
这是**红线 13 第二款**（「不得存在只有模型能读到的确认通道」/「无有效凭证的写调用一律拒绝」）
的**行为面失败**，而 22 条单元腿**全绿**也照不出来 —— 典型的「**单元面绿、装配面漏**」。

⇒ Lead 已把闸接进 `workspace_write` 的 handler（**写动作之前**）；本件钉住接线。

## 本件覆盖的腿（`A22` 四条腿的【行为面】版本）

| 腿 | 断言 | 用例 |
|---|---|---|
| ① 无凭证 ⇒ 拒 | `ok=False` + 文案含 `missing` + **写函数一次都没被调用** + **目标未变** | `test_a22_wired_no_token_is_rejected` |
| ①b 空密钥 ⇒ 拒 | `WORKSPACE_CONFIRM_SECRET=""` ⇒ 一律 `missing`（fail-closed） | `test_a22_wired_empty_secret_is_rejected` |
| ①c **零 IO** | 拒的路径上**不碰目标文件**（`mtime`/`size` 逐字未变） | 同 ① |
| ② 有效凭证 ⇒ 放行 | 宿主签发 ⇒ `ok=True` + **真落盘**（内容逐字正确） | `test_a22_wired_valid_token_is_accepted` |
| ③ 错配 ⇒ 拒 | 对 A 文件签发、拿去写 B ⇒ 拒（`mismatch`） | `test_a22_wired_wrong_path_is_rejected` |
| ③b 重放 ⇒ 拒 | 同一 token 第二次使用 ⇒ 拒（`replayed`） | `test_a22_wired_replay_is_rejected` |
| ③c 并发一次性（`回归修复`） | 同一 token **并行**两次 ⇒ 恰一次落盘、另一路 `replayed` | `test_a22_wired_concurrent_same_token_writes_exactly_once` |
| ③d 锁工厂单元腿（`回归修复`） | 同 token 同锁 / 异 token 异锁 / 用后即清（防涨） | `test_a22_token_lock_factory_reuses_same_lock_per_token` |
| ④ **负控** | 「拒」确实来自凭证闸，不是「一律拒」：③ 与 ① 成对即位 | 见上 |

## 🔴 判据面的一条纪律（本件刻意采用的写法）

**每条「拒」的腿都必须同时断「写函数一次都没被调用」** —— 否则「一律拒」也能全绿，
而「一律拒」会让写通道**完全不可用**（那是另一种真缺陷，不能与「正确拒绝」混为一谈）。
⇒ 本件用**计数替身**（`calls`）把这条钉住，并以腿② 作**正控**（有效凭证时必须真写）。

## 纪律

`backend/tests/agent/` **无 `conftest.py`** ⇒ 夹具自带；根一律 `tmp_path`（**红线 12**）；
密钥只活在进程内（`monkeypatch`），**绝不落 `.env`、绝不入库**（铁律 3）。
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest

from app.agent.tools import _confirm, workspace_write
from app.agent.tools._write import WriteOutcome
from app.core.config import settings


@pytest.fixture(autouse=True)
def _isolate_backup_anchor(tmp_path, monkeypatch):
    """🔴 **把备份锚点隔离到 `tmp_path`**（红线 12 · 阶段 5 事故 ⑤ 的直接后果）。

    `_write.resolve_backup_dir()` 现在有**测试态守卫**：测试里若锚点仍指向仓库内
    `data/workspace-backups/` ⇒ 直接抛 `TestModeRepoWriteError`（**结构性 fail-closed**）。
    本夹具把那句「每个载体都记得打锚点」的**约定**变成**不需要记得**的实现 ——
    因为实测证明「靠自觉」两轮共漏了 **65 + 170 = 235 件**污染。
    """
    from app.agent.tools import _write as _write_mod  # noqa: PLC0415

    monkeypatch.setattr(_write_mod, "resolve_backup_dir", lambda: tmp_path / "backups")

#: 测试密钥（**只活在进程内**）
SECRET = "gate-wiring-secret-not-persisted"


@pytest.fixture(autouse=True)
def pin_web_off(monkeypatch):
    """联网默认关（本目录无 conftest.py，钉子自带）。"""
    monkeypatch.setattr(settings, "WEB_ENABLE", False)


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch) -> Path:
    """工作区根 = `tmp_path`（**红线 12**），并配好密钥。"""
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(ws))
    monkeypatch.setattr(settings, "WORKSPACE_CONFIRM_SECRET", SECRET, raising=False)
    return ws


def _run(ws: Path, *, path: str, content: str, token: str | None, create: bool = False):
    """走**真 handler**（注入一个**计数替身**写函数，避免真落盘之外的副作用）。

    返回 `(observation, calls)`：`calls` 是替身被调用的次数 ——
    **「拒」的腿必须断它 == 0**（否则「一律拒」也能绿）。
    """
    calls = {"n": 0}

    def fake_write(**_kwargs):  # noqa: ANN003
        calls["n"] += 1
        return WriteOutcome(
            target=Path(_kwargs.get("rel_path") or path),
            byte_delta=len(str(content).encode("utf-8")),
            snapshot_id=None,
            text="已写入（替身）",
        )

    spec = workspace_write.build_spec(write=fake_write)
    args = workspace_write.WorkspaceWriteArgs(
        path=path, content=content, confirm_token=token, create=create
    )
    obs = asyncio.run(spec.handler(args))  # type: ignore[arg-type]
    return obs, calls["n"]


def _issue(ws: Path, rel: str, content: str) -> str:
    """宿主侧签发真凭证（绑定**逻辑内容**的 utf-8 字节 —— 与 handler 同口径）。"""
    return _confirm.issue_write_confirm(root=ws, rel_path=rel, content=content.encode("utf-8"))


# ---------------------------------------------------------------------------
# 腿①：无凭证 ⇒ 拒（+ 写函数未被调用 + 目标未变）
# ---------------------------------------------------------------------------


def test_a22_wired_no_token_is_rejected(workspace: Path):
    """**A22 腿①（行为面）**：不带 `confirm_token` ⇒ **拒**，且**写函数一次都没被调用**。

    🔴 后半句是本腿的**判别力来源**：只断 `ok=False` 的话，「一律拒」也能过；
    而「一律拒」= 写通道完全不可用（另一种真缺陷）⇒ 必须用**计数替身**把它与「正确拒绝」分开。
    """
    target = workspace / "note.txt"
    target.write_bytes(b"OLD")
    before = (target.stat().st_mtime_ns, target.stat().st_size)

    obs, calls = _run(workspace, path="note.txt", content="NEW", token=None)

    assert obs.ok is False, f"无凭证竟然放行：{obs.text}"
    assert "missing" in obs.text, f"拒因应含 missing（fail-closed）：{obs.text}"
    assert calls == 0, "被拒的调用**不得**触达写函数（否则「拒」不彻底）"
    assert (target.stat().st_mtime_ns, target.stat().st_size) == before, "拒时目标文件被改了"
    assert target.read_bytes() == b"OLD", "拒时盘上内容被改了"


def test_a22_wired_empty_secret_is_rejected(workspace: Path, monkeypatch):
    """**A22 腿①b（行为面）**：`WORKSPACE_CONFIRM_SECRET` 为空 ⇒ **一律拒**（写通道不可用）。

    出处 `D-回归检查`：「**不可为空默认**（空 ⇒ 凭证机制不可用 ⇒ 写通道 fail-closed 关闭）」。
    ⚠️ 本腿用「**先签一张有效凭证、再把密钥清空**」的构造 —— 确保拒因是**密钥缺失**，
    而不是「凭证本身伪造得不对」（两者修复动作相反）。
    """
    token = _issue(workspace, "note.txt", "NEW")  # 先拿一张**真**凭证
    monkeypatch.setattr(settings, "WORKSPACE_CONFIRM_SECRET", "", raising=False)

    obs, calls = _run(workspace, path="note.txt", content="NEW", token=token)

    assert obs.ok is False, "空密钥竟然放行 ⇒ 写通道未 fail-closed"
    assert "missing" in obs.text, f"拒因应为 missing：{obs.text}"
    assert calls == 0, "空密钥下不得触达写函数"


# ---------------------------------------------------------------------------
# 腿②：有效凭证 ⇒ 放行（**正控**：证明闸不是「一律拒」）
# ---------------------------------------------------------------------------


def test_a22_wired_valid_token_is_accepted(workspace: Path):
    """**A22 腿②（行为面 · 正控）**：有效凭证 ⇒ **放行**，写函数**真被调用**。

    ⚠️ 没有这条，上面三条「拒」的腿**无法区分**「闸正确工作」与「写通道整个坏掉」。
    """
    token = _issue(workspace, "note.txt", "NEW")
    obs, calls = _run(workspace, path="note.txt", content="NEW", token=token)

    assert obs.ok is True, f"有效凭证被拒 ⇒ 写通道不可用：{obs.text}"
    assert calls == 1, f"放行时写函数应恰好被调用一次：{calls}"


def test_a22_wired_valid_token_really_reaches_the_disk(workspace: Path):
    """**A22 腿② 端到端**：用**真**写函数（不注入替身）⇒ 盘上内容逐字正确。

    ⚠️ 与上一条的分工：上一条断「闸放行」（用替身，快且不碰盘）；
    本条断「放行之后真能写成」—— 两条缺一，可能出现「闸放行但写不动」。
    """
    target = workspace / "real.txt"
    target.write_bytes(b"OLD")
    token = _issue(workspace, "real.txt", "NEW-CONTENT")

    spec = workspace_write.build_spec()  # **不注入** ⇒ 走真 _write.write_workspace_file
    obs = asyncio.run(  # type: ignore[arg-type]
        spec.handler(
            workspace_write.WorkspaceWriteArgs(
                path="real.txt", content="NEW-CONTENT", confirm_token=token
            )
        )
    )

    assert obs.ok is True, f"有效凭证 + 真写函数仍失败：{obs.text}"
    assert target.read_bytes() == "NEW-CONTENT".encode("utf-8")


# ---------------------------------------------------------------------------
# 腿③：错配 / 重放 ⇒ 拒
# ---------------------------------------------------------------------------


def test_a22_wired_wrong_path_is_rejected(workspace: Path):
    """**A22 腿③（行为面）**：token 对 **A 文件**签发、拿去写 **B 文件** ⇒ **拒**。

    这是「凭证绑定了 path」的行为面证明（单元腿在 `test_write_confirm_t3_5.py` 里）。
    """
    token = _issue(workspace, "a.txt", "X")  # 对 a.txt 签发
    obs, calls = _run(workspace, path="b.txt", content="X", token=token)  # 拿去写 b.txt

    assert obs.ok is False, "凭证跨文件复用竟然放行 ⇒ path 绑定未生效"
    assert "mismatch" in obs.text, f"拒因应为 mismatch：{obs.text}"
    assert calls == 0, "错配的调用不得触达写函数"


def test_a22_wired_replay_is_rejected(workspace: Path):
    """**A22 腿③b（行为面 · 一次性）**：同一 token **第二次**使用 ⇒ **拒**（`replayed`）。

    出处：红线 13 第二款的实质 —— 「只有模型能读到的通道」若可**重放**，等于没有宿主侧校验。
    ⚠️ 本条必须走**真**写函数：`mark_used` 在**写成功之后**才消费 ⇒ 用替身测不出「消费」。
    """
    target = workspace / "once.txt"
    target.write_bytes(b"V0")
    token = _issue(workspace, "once.txt", "V1")

    def _once(content: str):
        spec = workspace_write.build_spec()
        return asyncio.run(  # type: ignore[arg-type]
            spec.handler(
                workspace_write.WorkspaceWriteArgs(
                    path="once.txt", content=content, confirm_token=token
                )
            )
        )

    first = _once("V1")
    assert first.ok is True, f"第一次（有效凭证）应成功：{first.text}"
    assert target.read_bytes() == b"V1"

    second = _once("V1")
    assert second.ok is False, "同一凭证第二次使用竟然放行 ⇒ 一次性未生效（可重放）"
    assert "replayed" in second.text, f"拒因应为 replayed：{second.text}"


# ---------------------------------------------------------------------------
# 腿③c/③d：并发一次性与 per-token 锁（`回归修复`）
# ---------------------------------------------------------------------------


def test_a22_wired_concurrent_same_token_writes_exactly_once(workspace: Path):
    """**A22 腿③c（并发一次性 · `回归修复`）**：同一张有效凭证**并行**两次写 ⇒ 恰好一次落盘。

    缺陷现场：verify 与 mark_used **分离非原子** —— 两次 verify 可以都落在对方
    `mark_used` 之前 ⇒ 都过闸都落盘（双写）。修法 = per-token `asyncio.Lock` 把
    verify→mark_used 包成互斥临界区 ⇒ 后来者 verify 必见 `replayed`。

    ⚠️ 两条调用必须在**同一个**事件循环里并发（`asyncio.gather`）——
    像 `_run` 那样各自 `asyncio.run` 是**串行**的，复现不出竞态窗口。
    ⚠️ 写替身**真 await**（`asyncio.sleep` 放大 verify→mark_used 之间的窗口）且**真写盘**：
    不放大窗口的话，第一路一路跑完才轮到第二路，修前也复现不出「都过闸」。
    ⚠️ 测试前后 `reset_used_for_tests()`（它同步清 `_TOKEN_LOCKS`）——
    进程级已用集合与锁注册表都跨用例泄漏。
    """
    _confirm.reset_used_for_tests()
    try:
        target = workspace / "race.txt"
        target.write_bytes(b"OLD")
        calls = {"n": 0}

        async def slow_write(**kwargs):  # noqa: ANN003
            await asyncio.sleep(0.05)  # 让出事件循环：给第二路的 verify 抢在 mark_used 之前的机会
            calls["n"] += 1
            data = bytes(kwargs.get("data") or b"")
            dest = workspace / str(kwargs.get("rel_path") or "race.txt")
            dest.write_bytes(data)
            return WriteOutcome(
                target=dest,
                byte_delta=len(data),
                snapshot_id=None,
                text="已写入（慢替身）",
            )

        token = _issue(workspace, "race.txt", "RACE-CONTENT")
        spec = workspace_write.build_spec(write=slow_write)
        args = workspace_write.WorkspaceWriteArgs(
            path="race.txt", content="RACE-CONTENT", confirm_token=token
        )

        async def _gather():
            return await asyncio.gather(spec.handler(args), spec.handler(args))

        obs_first, obs_second = asyncio.run(_gather())  # type: ignore[arg-type]

        oks = [o for o in (obs_first, obs_second) if o.ok]
        rejects = [o for o in (obs_first, obs_second) if not o.ok]
        assert len(oks) == 1, (
            f"同一凭证并行两次必须恰好放行一次，实得 {len(oks)} 次："
            f"{[o.text for o in (obs_first, obs_second)]}"
        )
        assert len(rejects) == 1 and "replayed" in rejects[0].text, (
            f"另一路必须以 replayed 拒绝：{[o.text for o in rejects]}"
        )
        assert calls["n"] == 1, f"写函数必须恰好被调用一次，实得 {calls['n']} 次（双写 = 缺陷在场）"
        assert target.read_bytes() == b"RACE-CONTENT", "盘上内容必须是恰好一次写入的结果"
        assert _confirm._USED == {token}, "已用集合必须恰含该 token（一次性消费落点唯一）"
    finally:
        _confirm.reset_used_for_tests()


def test_a22_token_lock_factory_reuses_same_lock_per_token():
    """**A22 腿③d（锁工厂单元腿 · `回归修复`）**：`token_lock` 的三条口径。

    - 同 token 两次取锁 ⇒ **同一个**锁对象（临界区互斥的前提）；
    - 不同 token ⇒ **不同**锁（互不阻塞）；
    - `release_token_lock` 之后再取 ⇒ **新**锁对象（用后即清、防注册表无界上涨）。
    ⚠️ 进程级锁注册表跨用例泄漏 ⇒ 前后都 `reset_used_for_tests()`（它同步清 `_TOKEN_LOCKS`）。
    """
    _confirm.reset_used_for_tests()
    try:
        lock_a1 = _confirm.token_lock("token-a")
        lock_a2 = _confirm.token_lock("token-a")
        assert lock_a1 is lock_a2, "同 token 必须拿到同一把锁（否则临界区互斥失效）"

        lock_b = _confirm.token_lock("token-b")
        assert lock_b is not lock_a1, "不同 token 必须是不同的锁（互不阻塞）"

        _confirm.release_token_lock("token-a")
        lock_a3 = _confirm.token_lock("token-a")
        assert lock_a3 is not lock_a1, "release 之后再取必须拿到新锁对象（用后即清防涨）"
    finally:
        _confirm.reset_used_for_tests()


# ---------------------------------------------------------------------------
# 一条**结构性**断言：闸必须在写动作之前
# ---------------------------------------------------------------------------


def test_a22_gate_precedes_the_write_call():
    """**结构性腿**：`workspace_write` 的 handler 里，`verify_write_confirm` 的**调用行号**
    必须**早于**写函数调用点 —— 防后人调换次序（那是红线 13 的直接破法）。

    ⚠️ 用**源码行号**而非行为：行为面已由上面各腿覆盖；本腿挡的是「次序」这一类**回归**，
    它很难用行为断言在**每条**路径上都覆盖到。
    """
    import inspect

    src = inspect.getsource(workspace_write.build_spec)
    gate_at = src.find("verify_write_confirm(")
    call_at = src.find("outcome = func(")
    assert gate_at != -1, "handler 里找不到凭证闸调用 ⇒ 闸可能没接线"
    assert call_at != -1, "handler 里找不到写函数调用点"
    assert gate_at < call_at, (
        "凭证闸的调用必须早于写函数调用点（红线 13：无凭证不得落盘）"
    )