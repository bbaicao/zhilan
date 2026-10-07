"""条目 CX-04′：**装配占用哨兵**（专项-上下文管理升级 · 档 3）。

被测对象 = ``app/generation/answer.py`` 的 :data:`PROMPT_OCCUPANCY_WARN_CHARS` /
:func:`prompt_size` / :func:`note_prompt_size`（+ 两个调用点：``answer_direct`` 与
``chat_routes`` 的流式路径）。

观测值**不改请求体**；超限时**有标记**且**不 trim**。
预算常量尚需校准，因此本哨兵只观测，不主动裁剪内容。

| 用例 | 判据 |
|---|---|
| ``test_s1_size_splits_system_and_messages`` | 三档拆分自洽（system + messages = total），空输入不炸 |
| ``test_s2_does_not_mutate_inputs`` | **不改输入**（消息体逐字节不变、不新增键） |
| ``test_s3_warns_only_above_threshold`` | 超阈值 ⇒ 一条 ``WARNING``；未超 ⇒ 无告警（**只告警**） |
| ``test_s4_matches_real_assembly`` | 与真实装配（``system_prompt`` + ``build_messages``）联动后读数自洽 |
| ``test_s5_refuses_to_trim`` | 哨兵**不裁剪**：装配结果在调用前后逐字节相同（沿 T0 的 D8 第一性质） |
| ``test_m1_mutation_proof_`` | **变异自证**：把读数换成恒 0 ⇒ S3 的告警断言真会红 |

⚠️ 本文件**不连库、不连服务、不调模型**：全部是纯函数与日志面。
"""

from __future__ import annotations

import logging

from app.generation import answer as answer_mod


def _messages(*contents: str) -> list[dict]:
    return [{"role": "user", "content": text} for text in contents]


def test_s1_size_splits_system_and_messages():
    """S1：``system`` / ``messages`` / ``total`` 三档自洽；``None`` 与空串都按 0 处理。"""
    size = answer_mod.prompt_size("系统", _messages("甲" * 10, "乙" * 5))
    assert size == {"system": 2, "messages": 15, "total": 17}

    assert answer_mod.prompt_size("", None) == {"system": 0, "messages": 0, "total": 0}
    assert answer_mod.prompt_size("s")["messages"] == 0, "件数缺省 ⇒ 消息体按 0 算"


def test_s2_does_not_mutate_inputs():
    """S2：**不改输入** —— 哨兵只读（消息体逐字节不变、键集合不变）。"""
    messages = _messages("正文" * 100)
    snapshot = [dict(item) for item in messages]
    keys_before = sorted(messages[0].keys())

    answer_mod.note_prompt_size("t", "系统提示", messages)

    assert messages == snapshot, "哨兵不得改写消息体（含就地拼接）"
    assert sorted(messages[0].keys()) == keys_before, "哨兵不得给消息体加键"


def test_s3_warns_only_above_threshold(monkeypatch, caplog):
    """S3：**只告警** —— 超阈值一条 WARNING，未超阈值零告警。"""
    monkeypatch.setattr(answer_mod, "PROMPT_OCCUPANCY_WARN_CHARS", 100)

    with caplog.at_level(logging.WARNING, logger="app.generation.answer"):
        answer_mod.note_prompt_size("t", "系" * 10, _messages("甲" * 200))
    assert any("装配占用偏大" in record.message for record in caplog.records), "超阈值必须告警"

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.generation.answer"):
        answer_mod.note_prompt_size("t", "系" * 10, _messages("甲" * 5))
    assert not caplog.records, "未超阈值不得告警（否则日志会被淹掉）"


def test_s4_matches_real_assembly():
    """S4：与**真实装配**联动 —— 读数等于「系统提示 + 各消息 content」的实际字符数。"""
    prompt_text = answer_mod.system_prompt(True, memory_text="记忆" * 10)
    built = answer_mod.build_messages(
        "问题" * 5,
        [{"role": "user", "content": "旧问"}, {"role": "assistant", "content": "旧答"}],
        has_material=True,
        materials_text="[1]（文档：a.md）\n正文",
    )
    size = answer_mod.note_prompt_size("t", prompt_text, built)

    assert size["system"] == len(prompt_text)
    assert size["messages"] == sum(len(item["content"]) for item in built)
    assert size["total"] == size["system"] + size["messages"] > 0


def test_s5_refuses_to_trim(monkeypatch):
    """S5：哨兵**永不裁剪** —— 即使装配远超阈值，消息体与系统提示仍逐字节不变。"""
    monkeypatch.setattr(answer_mod, "PROMPT_OCCUPANCY_WARN_CHARS", 1)
    prompt_text = answer_mod.system_prompt(True)
    built = answer_mod.build_messages(
        "问题" * 50,
        [{"role": "user", "content": "旧问" * 50}],
        has_material=True,
        materials_text="[1]（文档：a.md）\n" + "正文" * 5000,
    )
    before = ([dict(item) for item in built], prompt_text)

    answer_mod.note_prompt_size("t", prompt_text, built)

    assert [dict(item) for item in built] == before[0], "哨兵不得裁剪消息体"
    assert prompt_text == before[1], "哨兵不得裁剪系统提示"


def test_m1_mutation_proof_readings_would_go_red(monkeypatch, caplog):
    """M1 · 变异自证：**就地把坏实现打开**（读数恒 0）⇒ S3 的告警断言真会红。"""
    monkeypatch.setattr(answer_mod, "PROMPT_OCCUPANCY_WARN_CHARS", 100)
    monkeypatch.setattr(
        answer_mod,
        "prompt_size",
        lambda system_prompt_text, messages=None: {"system": 0, "messages": 0, "total": 0},
    )

    with caplog.at_level(logging.WARNING, logger="app.generation.answer"):
        size = answer_mod.note_prompt_size("t", "系" * 10, _messages("甲" * 200))

    assert size["total"] == 0, "坏实现必须真的把读数打成 0"
    assert not any("装配占用偏大" in record.message for record in caplog.records), (
        "⇒ 坏实现下 S3 的「超阈值必须告警」当场失败（判据有判别力）"
    )
