"""回归修复（2026-09-28 · 缺陷清单-2 阶段五）：本地 abort 兜底按「已落库」假设拉取 ⇒ 用户消息从界面消失。

根因：``submitChat`` catch 的 abort 分支（停止指令没送到、本地断流兜底）此前按
「用户消息已在服务端落库」的**错误假设**走 ``getChat`` 拉取 + 全量替换 / 过滤兜底。
后端事实：断开 / 本地 abort 路径**什么都不落库**（一轮收尾才落库，不会走到 persist）
⇒ 拉回的服务端快照里根本没有本轮提问，乐观用户气泡被整表覆盖抹掉——
停止后提问从界面消失。

修法 = abort 分支**删掉** ``getChat`` 拉取与全量替换 / 过滤兜底，乐观用户气泡与已生成的
半成品原样留在界面；错误假设注释改为如实描述。正常停止路径（``onStopped``——后端
stopped 轮「用户消息一定落」）语义不动；099 的快照机制本体一字不动。

判据面（本仓无 JS 测试框架 ⇒ 界面面只有**静态判别力**，照 099 先例在**剔除整行
``//`` 注释**后的 ``App.tsx`` 上断言；腿②的「错误假设已死」按口径查**未剔注释原文**
——错误假设连注释都不许留，「注释掉」在这里**不**等价于「删掉」）：

- 腿①（拉取 / 替换形态消失）：abort 分支块内**不含** ``getChat(``、**不含**
  ``setMessages(``、**不含** ``item.id > 0``；
- 腿②（错误假设注释已死）：未剔注释的原文全文不再含「用户消息已在服务端落库」
  （账本点名的错误假设原话）；且 abort 块内保留
  ``flash("已停止（已生成的部分未能保存）");``（文案如实保留）；
- 腿③（对照腿·正常停止路径原样）：``onStopped`` 块（括号配平截取，嵌套块不用正则）
  内仍有 ``kind: "stopped"`` 与 ``flash("已停止生成", 2600);``——正常停止语义不动；
- 腿④（099 机制原样）：``rec.userMsg = userBubble;`` 与 ``list.push(running.userMsg);``
  仍在（本条是 099 的残留假设面，机制本体一字不动）。

回退口径：把 143 的 diff 反向应用（abort 分支恢复 getChat 拉取 + 替换兜底 + 错误假设
注释）后**腿①②红**；腿③④不受影响。

⛔ 口径红线：本地保留的气泡**刷新即失**（与后端断开路径不落库的语义一致的真实状态）
——不得写「停止后提问永不丢失」。
⛔ 口径：静态判别力 ≠ 运行期渲染已验证（本仓无 JS 运行时测试设施）。
"""

from __future__ import annotations

from pathlib import Path

from tests.agent.test_visibility_read_t3_6 import _balanced_span

FRONTEND_APP = Path(__file__).resolve().parents[3] / "frontend" / "src" / "App.tsx"


def _app_source() -> str:
    """读 ``App.tsx`` 全文，**剔除整行注释**后再返回（判据纪律照 099 先例）。"""
    lines = FRONTEND_APP.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("//"))


def _block(source: str, start: str, end: str) -> str:
    """截取 ``start`` 起、到 ``end`` 止的源码片段（都取**首次**出现）。"""
    begin = source.index(start)
    stop = source.index(end, begin)
    return source[begin:stop]


def _abort_block() -> str:
    """abort 分支块：从 ``if (controller.signal.aborted) {`` 到 error 分支入口为止。"""
    return _block(_app_source(), "if (controller.signal.aborted) {", "} else if (stillThere) {")


# --- 腿①：abort 分支不再拉取 / 全量替换 / 过滤兜底 -----------------------------


def test_abort_branch_has_no_fetch_or_replace():
    """abort 分支块内不得再出现 getChat 拉取与 setMessages 替换 / 过滤形态。"""
    abort_block = _abort_block()
    assert "getChat(" not in abort_block
    assert "setMessages(" not in abort_block
    assert "item.id > 0" not in abort_block


# --- 腿②：错误假设注释已死 + 停止文案如实保留 ----------------------------------


def test_wrong_assumption_comment_is_dead_and_flash_kept():
    """错误假设原话全文（含注释）不得残留；abort 分支的停止文案逐字保留。"""
    raw = FRONTEND_APP.read_text(encoding="utf-8")
    assert "用户消息已在服务端落库" not in raw
    assert 'flash("已停止（已生成的部分未能保存）");' in _abort_block()


# --- 腿③：对照腿——正常停止路径语义不动 -----------------------------------------


def test_normal_stop_path_untouched():
    """``onStopped``（后端 stopped 轮「用户消息一定落」）的打标与横幅逐字保留。"""
    source = _app_source()
    begin = source.index("onStopped() {")
    brace = source.index("{", begin)
    stopped_block = _balanced_span(source, brace, "{", "}")
    assert 'kind: "stopped"' in stopped_block
    assert 'flash("已停止生成", 2600);' in stopped_block


# --- 腿④：099 快照机制本体一字不动 ----------------------------------------------


def test_bug099_snapshot_mechanism_untouched():
    """本条是 099 的残留假设面：快照挂载与重挂回填机制必须原样在位。"""
    source = _app_source()
    assert "rec.userMsg = userBubble;" in source
    assert "list.push(running.userMsg);" in source
