"""回归修复（缺陷清单-2 · 阶段五「前端一致性」）：删除「当前打开的会话」后发送必然 404。

根因：``removeSession`` 删除成功后，若删的正是当前打开的会话（``chatId === id``），
只清了消息 state，**``activeViewRef`` 仍指向已删会话**、viewToken 也不 bump ⇒
视图实例还「活着」，紧接着在空态发送会带着死会话 id 去 POST ⇒ 必然 404。

修法（App.tsx · removeSession）：在当前展示会话分支内照 ``openChat`` 的
空态形状一并补齐——

- **视图实例作废**：bump ``viewTokenSeq`` + ``activeViewRef.current = { chatId: null, token }``
  + ``setViewToken(token)``（在途流的增量也不再写回这个视图）；
- **清队列**：``setQueueItems([])``（排队消息仅属于当前会话，会话已删条目已不可能发出）；
- **navStack 清理**：past / future 两处滤掉指向已删会话的条目，current 归位
  ``{ view: "chat", chatId: null }``——后退 / 前进不再恢复到已删会话（404「无法打开会话」）。

判据面（本仓无 JS 测试框架 ⇒ 界面面只有**静态判别力**，与 099 / 回归检查 先例同口径）：
全部腿都在「**剔除整行 ``//`` 注释**后」的 ``App.tsx`` 上、且**截取 removeSession 块**
做断言（「注释掉」与「删掉」在判据面上等价）。

- 腿①（静态）：块内出现视图实例作废形态（``++viewTokenSeq.current`` 与
  ``activeViewRef.current = { chatId: null, token }``），且它们必须出现在
  ``if (activeViewRef.current.chatId === id)`` **之后**（顺序断言）+ 括号配平截出的**分支体内**——
  保证重置落在「删的正是当前会话」分支内，删**非当前**会话的分支不受影响；
- 腿②（静态）：块内 ``setQueueItems([])``（队列清理，且同样落在分支体内）；
- 腿③（静态）：块内 navStack 清理——past / future 两处 ``.filter((loc) =>`` 带
  ``loc.view === "chat" && loc.chatId === id`` 谓词（恰两处），以及 current 归位
  ``{ view: "chat", chatId: null }``；
- 腿④（**对照腿**）：``openChat`` 仍铸新 token，空态立即重置，已有会话在读取成功且
  最新请求守卫通过后才切换展示身份（回归检查）；实例重置不能被移除。

回归检查：删除返回后以 activeViewRef 判断当前展示会话，防 await 期间切换造成错清；
navStack 清理覆盖当前及后台删除，因此腿③校验完整 removeSession 块。

回退口径：**反向应用 114 的 diff**（删掉分支内三段新代码）后，腿①②③必须红、
腿④不受影响（``openChat`` 的实例重置与本次修复无关，回退不该动它）。

⛔ **口径**：静态判别力 ≠ 运行期渲染已验证（本仓无 JS 运行时测试设施）。
"""

from __future__ import annotations

from pathlib import Path

FRONTEND_APP = Path(__file__).resolve().parents[3] / "frontend" / "src" / "App.tsx"


def _app_source() -> str:
    """读 ``App.tsx`` 全文，**剔除整行注释**后再返回（099 判别力纪律同款）。"""
    lines = FRONTEND_APP.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("//"))


def _block(source: str, start: str, end: str) -> str:
    """截取 ``start`` 起、到 ``end`` 止的源码片段（都取**首次**出现）。"""
    begin = source.index(start)
    stop = source.index(end, begin)
    return source[begin:stop]


def _balanced_span(source: str, start: int, opener: str, closer: str) -> str:
    """从 ``start``（指向 ``opener``）起，按**括号配平**截出到配对 ``closer`` 的一段源码。

    ⚠️ 非贪婪正则会在这类嵌套对象字面量处**提前收口**（先例 回归检查 ``_balanced_span``）——
    纯配对扫描不受嵌套影响。找不到 opener / 括号未配平 ⇒ 直接断言失败（**不**返回空串）。
    """
    assert start >= 0, f"起始锚点不存在：{opener}"
    assert start < len(source) and source[start] == opener, (
        f"起始锚点没落在 {opener!r} 上：{source[start:start + 40]!r}"
    )
    depth = 0
    for index in range(start, len(source)):
        char = source[index]
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"括号未配平（源码被截断？）：{source[start:start + 80]!r}")


def _remove_session_block() -> str:
    """removeSession 函数所在的源码块（到下一个相邻函数 ``createGroup`` 为止）。"""
    return _block(_app_source(), "async function removeSession", "async function createGroup")


def _current_chat_branch(remove_session_block: str) -> str:
    """截出当前展示会话分支，重置必须落在分支内。"""
    guard_at = remove_session_block.index("if (activeViewRef.current.chatId === id)")
    brace_at = remove_session_block.index("{", guard_at)
    return _balanced_span(remove_session_block, brace_at, "{", "}")


# --- 腿①：删当前会话 ⇒ 视图实例作废（bump token + activeViewRef 归 null 态）-----


def test_leg1_remove_current_chat_invalidates_view_instance():
    """当前展示会话分支内必须作废实例，且判定发生在删除成功之后。"""
    rm_block = _remove_session_block()
    guard = "if (activeViewRef.current.chatId === id)"
    assert guard in rm_block
    assert "const token = ++viewTokenSeq.current;" in rm_block
    assert "activeViewRef.current = { chatId: null, token };" in rm_block
    assert "setViewToken(token);" in rm_block
    # 顺序断言：重置必须出现在「删的正是当前会话」分支判定之后
    assert rm_block.index("await deleteChat(id)") < rm_block.index(guard) < rm_block.index("++viewTokenSeq.current")
    # 且必须在分支体**之内**（配平截取防「挪到分支外但顺序仍靠后」的空子）
    branch = _current_chat_branch(rm_block)
    assert "const token = ++viewTokenSeq.current;" in branch
    assert "activeViewRef.current = { chatId: null, token };" in branch
    assert "setViewToken(token);" in branch


# --- 腿②：删当前会话 ⇒ 排队消息一并清 ------------------------------------------


def test_leg2_remove_current_chat_clears_message_queue():
    """会话已删，排队条目已不可能发出——``setQueueItems([])`` 必须在块内且落在分支体内。"""
    rm_block = _remove_session_block()
    assert "setQueueItems([]);" in rm_block
    branch = _current_chat_branch(rm_block)
    assert "setQueueItems([]);" in branch


# --- 腿③：删当前会话 ⇒ navStack 里指向已删会话的条目一并清 ----------------------


def test_leg3_remove_current_chat_prunes_nav_stack():
    """past / future 恰两处滤掉 ``chatId === id`` 的 chat 条目；current 归位空态。"""
    branch = _remove_session_block()
    assert "stack.past = stack.past.filter((loc) =>" in branch
    assert "stack.future = stack.future.filter((loc) =>" in branch
    predicate = '.filter((loc) => !(loc.view === "chat" && loc.chatId === id))'
    assert branch.count(predicate) == 2, "past 与 future 恰两处（少一处 = 后退/前进仍会撞死会话）"
    assert 'if (stack.current.view === "chat" && stack.current.chatId === id) {' in branch
    assert 'stack.current = { view: "chat", chatId: null };' in branch


# --- 腿④（对照腿）：openChat 自己的实例重置原样 ---------------------------------


def test_leg4_control_open_chat_instance_reset_untouched():
    """空态立即重置；已有会话守卫通过后才一起切换身份和消息。"""
    open_block = _block(_app_source(), "async function openChat(", "async function removeSession")
    assert "const token = ++viewTokenSeq.current;" in open_block
    null_at = open_block.index("if (id === null)")
    null_branch = _balanced_span(open_block, open_block.index("{", null_at), "{", "}")
    assert "activeViewRef.current = { chatId: null, token };" in null_branch
    assert "setViewToken(token);" in null_branch
    assert "setMessages([]);" in null_branch
    guard = "if (viewTokenSeq.current !== token || deletedChatVersions.current.get(id) !== deletedVersion) return;"
    switch = "activeViewRef.current = { chatId: detail.id, token };"
    assert open_block.index("await getChat(id)") < open_block.index(guard) < open_block.index(switch)
    assert open_block.index(switch) < open_block.index("setViewToken(token);", open_block.index(switch))
    assert open_block.index(switch) < open_block.index("setMessages(list);")
