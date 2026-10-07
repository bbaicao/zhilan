"""回归修复（2026-09-28 缺陷清单-2 · 阶段五）：``done.recovered`` 免刷新拉取与队列续发竞态——
挂起的 ``getChat`` 解析后全量替换，抹掉续发新增的乐观气泡。

竞态时序（改前）：

- onDone 的 recovered 分支 ``await getChat(rec.chatId)`` **挂起**；
- ``streamChat`` 正常返回 ⇒ ``submitChat`` 的 finally 已移除本路流并 ``void submitChat(...)``
  **续发下一轮**；
- 续发加进列表的新一轮**乐观用户气泡**（含其回答半成品）此刻在列表里；
- 挂起的 ``getChat`` 这才解析 ⇒ ``setMessages(detail.messages)`` **全量替换**——
  服务端快照不含续发那轮 ⇒ 乐观气泡被抹掉。

修法（案 A，``App.tsx`` onDone 内 recovered 分支）= 两件套：

- **按 id 归并**：``setMessages`` 改函数式更新——服务端消息在前、本地多出的条目（排除本轮
  乐观气泡 ``userId`` / ``rec.botId``：它们由落库真身接替）按 id 去重后接尾，不再整体替换；
  归并对重复解析幂等（已在 detail 里的条目不进 extras，旧快照晚到也不会把历史并重复）；
- **每会话时序闸**：``recoveredFetchSeq`` ref（``Map<chatId, seq>``）——发起时 bump、解析时校验，
  同会话有更新的一次拉取发起后，旧的那份解析直接放弃（新份数据 ≥ 旧份，先并旧份会把历史并重复）。

后端前提（已核实）：``chat_routes.py`` 流式 done 轮 ``persist_turn`` + commit（:2366-2404）
**先于** done 帧 yield（:2424 起）⇒ 前端收到 done 再拉取必含本轮落库，归并替换乐观气泡安全。

判据面（本仓无 JS 测试框架 ⇒ 界面面只有**静态判别力**，与 099 / 回归检查 先例同口径）：

- 腿①：时序闸在位（ref 声明 + 发起 bump + 解析校验成对出现，且 bump 先于校验——顺序即时间序）；
- 腿②：归并形态在位（函数式更新 + landed 集合 + 本轮乐观气泡让位落库真身 + 归并返回）；
- 腿③：整体替换形态在 recovered 分支块内**消失**（竞态抹除点不复存在）；
- 腿④（负控）：catch error 分支的**权威重拉** ``setMessages(detail.messages)`` 仍保留——
  失败轮「以服务端为准」是有意设计（该分支 await 在 catch 内完成、先于 finally 续发，
  无本条竞态），防「顺手把两种形态一起删掉凑腿③」；
- 腿⑤：099 机制原样（``rec.userMsg`` 快照在位 + ``openChat`` 回填顺序先用户后回答）——
  本条动刀在 099 机制的相邻位置，机制本体一字不动。

回退（反向应用 113 的 diff）后：腿①②③红，腿④⑤不受影响。

⛔ **口径红线**：

- 静态判别力 ≠ 运行期渲染已验证（本仓无 JS 运行时测试设施）；
- ⛔ 不得写「113 的竞态已全部消除」——catch error 分支为无竞态时序 + 权威重拉按设计保留，
  极端乱序窗口由时序闸兜住。
"""

from __future__ import annotations

from pathlib import Path

FRONTEND_APP = Path(__file__).resolve().parents[3] / "frontend" / "src" / "App.tsx"


def _app_source() -> str:
    """读 ``App.tsx`` 全文，**剔除整行注释**后再返回。

    🔴 判别力纪律（099 首版踩过）：变异自证把目标行注释掉，子串断言仍命中 ⇒ 用例照样绿
    （变异存活）。⇒ 一律在**去注释**的源码上做包含/顺序断言，「注释掉」与「删掉」在判据面上等价。
    """
    lines = FRONTEND_APP.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("//"))


def _block(source: str, start: str, end: str) -> str:
    """截取 ``start`` 起、到 ``end`` 止的源码片段（都取**首次**出现）。"""
    begin = source.index(start)
    stop = source.index(end, begin)
    return source[begin:stop]


def _recovered_branch() -> str:
    """onDone 内 recovered 分支块：分支开锚起、到 ``onStopped() {`` 止（该分支在 onDone 尾部）。"""
    return _block(
        _app_source(),
        "if (result.recovered && rec.chatId !== null)",
        "onStopped() {",
    )


def _error_branch() -> str:
    """catch 内 error 分支块（``else if (stillThere)``）：权威重拉所在，负控对照面。"""
    return _block(
        _app_source(),
        "} else if (stillThere) {",
        "const reason = err instanceof ApiError",
    )


# --- 腿①：每会话时序闸在位 ----------------------------------------------------


def test_recovered_fetch_seq_gate_in_place():
    """``recoveredFetchSeq`` 声明在位，且 recovered 分支内「发起 bump + 解析校验」成对出现。

    同会话有更新的一次拉取发起后，旧的那份解析直接放弃——没有这层闸，
    两个挂起的 getChat 乱序解析仍会把旧快照并进新状态（极端乱序窗口）。
    """
    source = _app_source()
    assert (
        "const recoveredFetchSeq = useRef<Map<number, number>>(new Map());" in source
    ), "时序闸 ref 声明丢失"
    block = _recovered_branch()
    assert "recoveredFetchSeq.current.set(rec.chatId, seq);" in block, "发起时 bump 丢失"
    assert "seq === recoveredFetchSeq.current.get(rec.chatId)" in block, "解析时校验丢失"
    bump_at = block.index("recoveredFetchSeq.current.set(rec.chatId, seq);")
    check_at = block.index("seq === recoveredFetchSeq.current.get(rec.chatId)")
    assert bump_at < check_at, "bump（发起时）必须先于校验（解析后）——顺序即时间序"


# --- 腿②：按 id 归并形态在位 ---------------------------------------------------


def test_recovered_merge_is_functional_id_merge():
    """recovered 分支必须按 id 归并：函数式更新 + landed 集合 + 乐观气泡让位 + 归并返回。"""
    block = _recovered_branch()
    assert "setMessages((prev) =>" in block, "必须是函数式更新（整体替换形态没有 prev）"
    assert "new Set(detail.messages.map((item) => item.id))" in block, "landed 集合丢失"
    assert (
        "item.id !== userId && item.id !== rec.botId" in block
    ), "本轮乐观气泡须排除在 extras 之外（由落库真身接替）"
    assert "return [...detail.messages, ...extras];" in block, "归并返回形态丢失"
    update_at = block.index("setMessages((prev) =>")
    landed_at = block.index("new Set(detail.messages.map((item) => item.id))")
    extras_at = block.index("item.id !== userId && item.id !== rec.botId")
    merge_at = block.index("return [...detail.messages, ...extras];")
    assert update_at < landed_at < extras_at < merge_at, "归并流水线顺序乱了"


# --- 腿③：整体替换形态在该块消失 ------------------------------------------------


def test_recovered_branch_no_longer_wholesale_replaces():
    """recovered 分支块内不得再出现 ``setMessages(detail.messages)`` 整体替换形态。

    全量替换正是本条竞态的抹除点——挂起解析后不含续发那轮的服务端快照会把乐观气泡抹掉。
    """
    assert "setMessages(detail.messages)" not in _recovered_branch(), (
        "recovered 分支又变回整体替换——竞态抹除点回来了"
    )


# --- 腿④（负控）：catch error 分支的权威重拉保留 ---------------------------------


def test_error_branch_authoritative_refetch_kept():
    """catch 的 error 分支（失败轮）仍保留 ``setMessages(detail.messages);`` 权威重拉。

    失败轮「以服务端为准」是有意设计（该分支 await 在 catch 内完成、先于 finally 续发，
    无本条竞态）；本腿防「顺手把两种形态一起删掉凑腿③」。
    """
    assert "setMessages(detail.messages);" in _error_branch(), (
        "error 分支的权威重拉被误删——失败轮「以服务端为准」机制丢了"
    )


# --- 腿⑤：099 机制原样 ---------------------------------------------------------


def test_bug099_mechanism_untouched():
    """099 机制本体一字不动：``rec.userMsg`` 快照在位 + ``openChat`` 回填先用户后回答。

    本条动刀在 099 机制的相邻位置（onDone recovered 分支）——机制本体不许被顺手改掉。
    """
    source = _app_source()
    assert "rec.userMsg = userBubble;" in source, "099 的在途用户消息快照丢失"
    open_block = _block(source, "async function openChat(", "async function removeSession")
    user_push_at = open_block.index("list.push(running.userMsg)")
    partial_push_at = open_block.index("list.push(partial)")
    assert user_push_at < partial_push_at, "用户气泡必须先于回答半成品入列（099 回填顺序）"
