"""回归修复（2026-09-27 业主真机反馈）：流式**在途轮的用户消息**在「切页再回来」后消失。

根因（账本已定位，双因素叠加）：

- **后端前提**：v2 流式轮的用户消息在一轮**收尾**时才落库（``persist_turn`` 内部经
  ``persist_user_turn`` 一次写下）⇒ 流式期间 ``GET /api/chats/{id}`` 的 ``messages``
  **不含本轮提问**（这是**有意设计**——牵动 turn 组装 / 压缩 / 恢复不变量，不动）；
- **前端直接原因**：``openChat`` 重拉服务端消息整表覆盖，重挂回填只重建了**回答**半成品
  （``partialFromStream``），而 ``StreamRec`` 只存回答增量缓冲、没存本轮用户消息 ⇒
  在途用户气泡随覆盖丢失。

修法 = **前端单侧**（账本推荐口径）：``StreamRec`` 增记本轮用户消息快照
（``userMsg``，与乐观回显同一份数据），``openChat`` 重挂回填时**先补在途用户气泡、
再补回答半成品**；done 后以服务端为准（rec 随流收尾移除，既有负数 id 合并机制不变）。

判据面（本仓无 JS 测试框架 ⇒ 界面面只有**静态判别力**，与 回归检查 先例同口径）：

- 腿①–④（静态）：``StreamRec.userMsg`` 字段在位 / 快照在发送时挂上 / ``openChat``
  回填顺序 = 先用户后回答 / done 技能标签补写时快照跟随——四腿分别对应四处改动，
  **回退任一处即红**；
- 腿⑤（HTTP 行为）：**后端前提腿**——一轮收尾后，用户消息与回答**都**落库可读
  （钉住「内容不丢」这条账本结论；若后端将来改成「收到即落库」，本腿不变红，
  前端快照机制照旧成立）。

⛔ **口径**：静态判别力 ≠ 运行期渲染已验证（本仓无 JS 运行时测试设施）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.api.test_chat_contract import client, parse_sse  # noqa: F401 —— client 是夹具
from tests.api.test_chat_retrieval_wiring import _install

FRONTEND_APP = Path(__file__).resolve().parents[3] / "frontend" / "src" / "App.tsx"


def _app_source() -> str:
    """读 ``App.tsx`` 全文，**剔除整行注释**后再返回。

    🔴 判别力纪律（本文件首版踩过）：变异自证把回填行注释掉，子串断言仍命中
    ⇒ 5 例照样绿（变异存活）。⇒ 一律在**去注释**的源码上做包含/顺序断言，
    「注释掉」与「删掉」在判据面上等价。
    """
    lines = FRONTEND_APP.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("//"))


def _block(source: str, start: str, end: str) -> str:
    """截取 ``start`` 起、到 ``end`` 止的源码片段（都取**首次**出现）。"""
    begin = source.index(start)
    stop = source.index(end, begin)
    return source[begin:stop]


# --- 腿①：StreamRec 增记 userMsg 快照字段 -----------------------------------


def test_streamrec_carries_user_message_snapshot():
    """``StreamRec`` 必须带 ``userMsg`` 快照字段——没有它，切走再回来就无处取回在途提问。"""
    rec_block = _block(_app_source(), "type StreamRec = {", "};")
    assert "userMsg: ChatMessage | null;" in rec_block


# --- 腿②：发送时把乐观回显快照挂上 -------------------------------------------


def test_submit_chat_snapshots_user_bubble_into_rec():
    """``submitChat`` 必须把乐观回显的同一份数据赋给 ``rec.userMsg``。"""
    source = _app_source()
    assert "rec.userMsg = userBubble;" in source
    # 快照必须先于首次 await（streamChat）赋值——否则竞态窗口里 openChat 读到 null。
    assign_at = source.index("rec.userMsg = userBubble;")
    await_at = source.index("await streamChat(")
    assert assign_at < await_at


# --- 腿③：openChat 重挂回填 = 先用户气泡、后回答半成品 ------------------------


def test_open_chat_backfills_user_bubble_before_partial():
    """``openChat`` 重拉覆盖后必须先补在途用户气泡、再补回答半成品（顺序即时间序）。"""
    open_block = _block(_app_source(), "async function openChat(", "async function removeSession")
    assert "if (running?.userMsg) list.push(running.userMsg);" in open_block
    user_push_at = open_block.index("list.push(running.userMsg)")
    partial_push_at = open_block.index("if (partial) list.push(partial);")
    assert user_push_at < partial_push_at, "用户气泡必须先于回答半成品入列"


# --- 腿④：done 补写技能标签时快照跟随 -----------------------------------------


def test_done_skill_ids_patch_also_updates_snapshot():
    """done 解析出的技能标签要同步进快照——否则回填出的用户气泡缺标签。"""
    assert "rec.userMsg = { ...rec.userMsg, skill_ids: result.skill_ids };" in _app_source()


# --- 腿⑤：后端前提腿——收尾落库后用户消息与回答都可见 -------------------------


@pytest.mark.asyncio
async def test_user_message_and_answer_both_persisted_after_done(client, db, monkeypatch):
    """一轮收尾后 ``GET /api/chats/{id}`` 必须同时含本轮提问与回答（「内容不丢」的回归钉）。

    前端快照机制建立在这个前提上：流式期间服务端 messages 缺本轮提问（有意设计），
    收尾后补齐 ⇒ 切走再回来的「以服务端为准」永远拿得到完整一轮。
    """
    _install(monkeypatch, reply="好的，收到。")

    async with client.stream(
        "POST", "/api/chat/stream", json={"message": "现在呢"}
    ) as resp:
        raw = (await resp.aread()).decode("utf-8")

    events = parse_sse(raw)
    assert events[0]["type"] == "meta"
    assert events[-1]["type"] == "done"
    chat_id = events[0]["chat_id"]
    assert chat_id is not None

    detail = (await client.get(f"/api/chats/{chat_id}")).json()
    roles = [(m["role"], m["content"]) for m in detail["messages"]]
    assert ("user", "现在呢") in roles
    assert ("assistant", "好的，收到。") in roles
    # 顺序：提问在前、回答在后
    assert roles.index(("user", "现在呢")) < roles.index(("assistant", "好的，收到。"))
