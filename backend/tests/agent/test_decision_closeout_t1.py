"""第 1 批结束决策的纯函数判据。不连库。

原有 3 例 = 第 1 批交付时落盘；本轮（T1 补判据）追加四组：

* **A8 · 载体唯一**：`parse_closeout_message` 在 `app/agent/runner.py` 里**恰好一个调用点**，
  且 `app/` 内不存在第二处解析（`parse_closeout_message` / `_CLOSEOUT_RE` / `_CLOSEOUT_KEYS` 只出现在 runner.py）。
* **A9 · 三概念封闭（负腿）**：`action` 取值封闭 / 载荷键**恰三个** / `use_candidates` 必须是 bool /
  `clarification` 必须是 str（`action=="clarify"` 时非空；`action=="answer"` 时**允许空串但仍须存在**）。
* **围栏边界（D-1 落地形状）**：同行形式**不算块**（钉住有意选择，见该用例注释）/ 块前块后多余文本忽略 /
  两个块取**最后一段** / 块外正文**不参与判定** / 围栏块必须从 `visible` 剥离。
* **A14**：同一份载荷 × 多种**相反**正文 ⇒ 解析结果只跟载荷走。

本文件直接验证以上解析规则，并对相同载荷使用相反正文来检查判别力。
"""
import inspect
import json
from pathlib import Path

import pytest

from app.agent import runner
from app.agent.runner import parse_closeout_message

FENCE = """说明
<<<CLOSEOUT>>>
{{"action":"{action}","use_candidates":{use},"clarification":"{clar}"}}
<<<END>>>
尾巴
"""


def test_absent_fence_is_not_a_failure():
    visible, parsed, invalid = parse_closeout_message("可以作答了")
    assert visible == "可以作答了"
    assert parsed is None
    assert invalid is False


def test_valid_block_strips_markers():
    text = FENCE.format(action="clarify", use="false", clar="你指的是哪份？")
    visible, parsed, invalid = parse_closeout_message(text)
    assert "<<<CLOSEOUT>>>" not in visible
    assert invalid is False
    assert parsed == {
        "action": "clarify",
        "use_candidates": False,
        "clarification": "你指的是哪份？",
    }


def test_bad_type_and_missing_field_and_empty_clarification_are_invalid():
    cases = [
        '{"action":"answer","use_candidates":"false","clarification":""}',
        '{"action":"answer","clarification":""}',
        '{"action":"clarify","use_candidates":false,"clarification":"  "}',
        "not-json",
    ]
    for raw in cases:
        text = f"前言\n<<<CLOSEOUT>>>\n{raw}\n<<<END>>>\n"
        visible, parsed, invalid = parse_closeout_message(text)
        assert invalid is True, raw
        assert parsed is None
        assert "<<<CLOSEOUT>>>" not in visible


# ---------------------------------------------------------------------------
# 工具：把 JSON 原文包成 D-1 落地形状（标记各占一行）的围栏块
# ---------------------------------------------------------------------------

#: 合法载荷两腿（`clarification` 空串在 `action=="answer"` 时合法）
_RAW_ANSWER = '{"action":"answer","use_candidates":true,"clarification":""}'
_RAW_CLARIFY = '{"action":"clarify","use_candidates":false,"clarification":"你指的是哪份？"}'

_KEYS = {"action", "use_candidates", "clarification"}


def _block(raw: str, before: str = "", after: str = "") -> str:
    """决策正文 = 块前文本 + 围栏块 + 块后文本。"""
    return f"{before}<<<CLOSEOUT>>>\n{raw}\n<<<END>>>\n{after}"


def _parse(raw: str, before: str = "", after: str = ""):
    return parse_closeout_message(_block(raw, before, after))


# ---------------------------------------------------------------------------
# A8 · 载体唯一：解析点恰好一处（不存在第二处解析）
# ---------------------------------------------------------------------------


def test_closeout_parse_has_exactly_one_call_site():
    """A8：`runner.py` 里对 `parse_closeout_message` 的调用**恰好一处**。

    数法：`inspect.getsource(runner)` 里的出现次数 − 定义行 1 行 = 调用点数；
    另用「行内命中且非注释、非 def」的行再过一遍，防止把注释里的调用当调用点。
    """
    source = inspect.getsource(runner)
    definition_lines = [
        line
        for line in source.splitlines()
        if line.lstrip().startswith("def parse_closeout_message(")
    ]
    call_lines = [
        line
        for line in source.splitlines()
        if "parse_closeout_message(" in line
        and not line.lstrip().startswith(("def ", "#"))
    ]
    assert len(definition_lines) == 1, definition_lines
    assert source.count("parse_closeout_message(") == 2, call_lines  # 定义 1 + 调用 1
    assert len(call_lines) == 1, call_lines


def test_no_second_parse_path_outside_runner():
    """A8：`app/` 内不存在**第二处解析**（三个解析符号只许出现在 runner.py）。"""
    runner_path = Path(runner.__file__).resolve()
    app_dir = runner_path.parents[1]
    assert app_dir.name == "app" and app_dir.is_dir(), app_dir
    scanned = [p for p in sorted(app_dir.rglob("*.py")) if p.resolve() != runner_path]
    assert len(scanned) > 1, f"没有扫到 app/ 下的模块，本断言会假绿：{app_dir}"
    offenders = []
    for path in scanned:
        text = path.read_text(encoding="utf-8")
        for symbol in ("parse_closeout_message", "_CLOSEOUT_RE", "_CLOSEOUT_KEYS"):
            if symbol in text:
                offenders.append(f"{path.relative_to(app_dir)}:{symbol}")
    assert offenders == []


# ---------------------------------------------------------------------------
# A9 · 三概念封闭（负腿）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param('{"action":"refuse","use_candidates":true,"clarification":""}', id="refuse"),
        pytest.param('{"action":"ANSWER","use_candidates":true,"clarification":""}', id="uppercase"),
        pytest.param('{"action":"","use_candidates":true,"clarification":""}', id="empty"),
        pytest.param('{"action":"answer ","use_candidates":true,"clarification":""}', id="padded"),
        pytest.param('{"action":1,"use_candidates":true,"clarification":""}', id="non-string"),
    ],
)
def test_action_value_is_closed(raw):
    """§2.3 第 8 条：`action` 只许 `{"answer","clarify"}`。"""
    visible, parsed, invalid = _parse(raw)
    assert invalid is True, raw
    assert parsed is None, raw
    assert "<<<CLOSEOUT>>>" not in visible


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param('{"action":"answer","use_candidates":true,"clarification":"","extra":1}', id="four-keys"),
        pytest.param('{"use_candidates":true,"clarification":""}', id="missing-action"),
        pytest.param('{"action":"answer","clarification":""}', id="missing-use-candidates"),
        pytest.param('{"action":"answer","use_candidates":true}', id="missing-clarification"),
        pytest.param('{"Action":"answer","use_candidates":true,"clarification":""}', id="wrong-key-case"),
        pytest.param('[]', id="not-an-object"),
    ],
)
def test_payload_key_set_is_exactly_three(raw):
    """§2.3 第 8 条：**不得**加第四个概念，也不许缺键。"""
    visible, parsed, invalid = _parse(raw)
    assert invalid is True, raw
    assert parsed is None, raw
    assert "<<<CLOSEOUT>>>" not in visible


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param('{"action":"answer","use_candidates":"false","clarification":""}', id="string-false"),
        pytest.param('{"action":"answer","use_candidates":0,"clarification":""}', id="int-zero"),
        pytest.param('{"action":"answer","use_candidates":1,"clarification":""}', id="int-one"),
        pytest.param('{"action":"answer","use_candidates":null,"clarification":""}', id="null"),
        pytest.param('{"action":"answer","use_candidates":[],"clarification":""}', id="array"),
    ],
)
def test_use_candidates_must_be_bool(raw):
    """§2.3 第 8 条：`"false"` / `0` / `1` / `null` 一类**非 bool** 一律 invalid。"""
    visible, parsed, invalid = _parse(raw)
    assert invalid is True, raw
    assert parsed is None, raw


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param('{"action":"clarify","use_candidates":false,"clarification":123}', id="number"),
        pytest.param('{"action":"answer","use_candidates":true,"clarification":null}', id="null"),
        pytest.param('{"action":"answer","use_candidates":true,"clarification":[]}', id="array"),
        pytest.param('{"action":"answer","use_candidates":true,"clarification":false}', id="bool"),
    ],
)
def test_clarification_must_be_str(raw):
    """§2.3 第 8 条：`clarification` 必须是 str。"""
    visible, parsed, invalid = _parse(raw)
    assert invalid is True, raw
    assert parsed is None, raw


def test_empty_clarification_ok_for_answer_but_not_for_clarify():
    """§2.3 第 10 条：`clarification` 空 ⇒ 不算澄清（invalid）；但 `action=="answer"` 允许空串。"""
    _, parsed, invalid = _parse(_RAW_ANSWER)
    assert invalid is False
    assert parsed == {"action": "answer", "use_candidates": True, "clarification": ""}

    _, parsed2, invalid2 = _parse('{"action":"clarify","use_candidates":false,"clarification":""}')
    assert invalid2 is True
    assert parsed2 is None


@pytest.mark.parametrize(
    "raw, expected_action, expected_use",
    [
        pytest.param(_RAW_ANSWER, "answer", True, id="answer-true"),
        pytest.param(_RAW_CLARIFY, "clarify", False, id="clarify-false"),
    ],
)
def test_valid_block_returns_exactly_three_keys(raw, expected_action, expected_use):
    """A9 正腿：合法块的返回值**恰三键**，且类型正确。"""
    visible, parsed, invalid = _parse(raw, before="说明\n", after="尾巴\n")
    assert invalid is False
    assert set(parsed) == _KEYS
    assert parsed["action"] == expected_action
    assert parsed["use_candidates"] is expected_use
    assert isinstance(parsed["clarification"], str)
    assert "<<<CLOSEOUT>>>" not in visible and "<<<END>>>" not in visible


# ---------------------------------------------------------------------------
# 围栏边界（D-1 落地形状）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            f'说明\n<<<CLOSEOUT>>>{_RAW_ANSWER}<<<END>>>\n尾巴\n',
            id="both-markers-same-line",
        ),
        pytest.param(
            f'说明<<<CLOSEOUT>>>\n{_RAW_ANSWER}\n<<<END>>>\n',
            id="opener-not-at-line-start",
        ),
    ],
)
def test_inline_fence_is_not_a_block_by_design(text):
    """围栏标记必须**独占行**（D-1 落地形状第 1 条）⇒ 同行写法**不算块**。

    ⚠️ **这是实现与提示词一致的**有意选择**，不是遗漏**：
    `DECISION_SYSTEM_PROMPT` 明写「在回复末尾另起一行写结束标记，下一行只放一个 JSON 对象，
    然后再单独一行写结束标记的结尾」；「独占行」的用意正是**避免与正文里的 JSON 混**
    ——若放宽到同行，正文里任何一段同行标记都会变成载荷（正好撞 §2.3 第 11 条「不解析 UI 摘要」）。

    代价：同行写法被忽略 ⇒ 落回 D-3「整段没有协议块 = **正常收尾**」，**不是失败**
    （`parsed is None` / `invalid is False`）。⇒ 同行写法下**不做剥离**：那段 JSON 文本仍留在 `visible` 里。
    同一规则的推论：块前/块后与标记**同行**的文本（如 `<<<END>>>以上`）也会让整块不被识别。
    """
    visible, parsed, invalid = parse_closeout_message(text)
    assert parsed is None
    assert invalid is False
    assert "<<<CLOSEOUT>>>" not in visible
    assert "<<<END>>>" not in visible


def test_extra_text_before_and_after_block_is_ignored():
    """D-1 第 3 条：块前 / 块后的任意多余文本一律忽略（不报错、不据此判失败）。"""
    text = _block(_RAW_ANSWER, before="前言：资料已经够了。\n", after="以上。\n")
    visible, parsed, invalid = parse_closeout_message(text)
    assert invalid is False
    assert parsed == {"action": "answer", "use_candidates": True, "clarification": ""}
    assert "以上。" in visible  # 块外正文仍可见 —— 只有围栏块被剥离


def test_last_block_wins():
    """D-1 第 3 条：**只取最后一段**围栏块。"""
    text = "开头\n" + _block(_RAW_ANSWER) + "中间\n" + _block(_RAW_CLARIFY) + "结束\n"
    visible, parsed, invalid = parse_closeout_message(text)
    assert invalid is False
    assert parsed == {
        "action": "clarify",
        "use_candidates": False,
        "clarification": "你指的是哪份？",
    }
    assert "<<<CLOSEOUT>>>" not in visible and "<<<END>>>" not in visible
    assert "你指的是哪份？" not in visible  # 末端块也被剥离
    assert "开头" in visible and "中间" in visible and "结束" in visible


def test_prose_outside_block_does_not_decide():
    """§2.3 第 11 条（D12）：判定只读协议块，**不扫**「可以作答了」这类文案。"""
    text = _block(
        '{"action":"answer","use_candidates":false,"clarification":""}',
        before="资料已采用，可以作答了。\n",
    )
    visible, parsed, invalid = parse_closeout_message(text)
    assert invalid is False
    assert parsed == {"action": "answer", "use_candidates": False, "clarification": ""}
    assert parsed["use_candidates"] is False  # 载荷说「不采用」就按「不采用」，正文的「已采用」不作数
    assert "可以作答了" in visible


def test_visible_strips_markers_and_payload():
    """D-1 第 5 条：围栏块必须从 `visible` 剥离（`thought` 步里不得出现原始 JSON）。"""
    text = FENCE.format(action="clarify", use="false", clar="你指的是哪份？")
    visible, parsed, invalid = parse_closeout_message(text)
    assert invalid is False and parsed is not None
    for fragment in ("<<<CLOSEOUT>>>", "<<<END>>>", '"action"', '"use_candidates"', "你指的是哪份？"):
        assert fragment not in visible, fragment
    assert "说明" in visible and "尾巴" in visible


# ---------------------------------------------------------------------------
# A14 · 不解析 UI 摘要：结果只跟载荷走
# ---------------------------------------------------------------------------

#: 与载荷**相反**（或至少与载荷无关）的块外正文 —— 覆盖「可以作答了」「资料已采用」
#: 「必须追问」以及**直接写出字段名**的变体（后者专治「拿正文里的字段字样当载荷」）。
_OPPOSITE_PROSE = (
    "可以作答了",
    "资料已采用，直接作答即可。",
    "必须向用户追问，不得作答。",
    "use_candidates: true（已采用全部候选）",
)
_PROSE_IDS = ("can-answer", "adopted", "must-ask", "field-mention")


@pytest.mark.parametrize("raw", [_RAW_CLARIFY, _RAW_ANSWER], ids=["clarify-false", "answer-true"])
@pytest.mark.parametrize("prose", _OPPOSITE_PROSE, ids=_PROSE_IDS)
def test_payload_wins_over_opposite_prose(prose, raw):
    """A14：同一份载荷 × 多种相反正文 ⇒ 解析结果**只跟载荷走**。"""
    text = _block(raw, before=prose + "\n", after="尾巴\n")
    visible, parsed, invalid = parse_closeout_message(text)
    assert invalid is False, (prose, raw)
    assert parsed == json.loads(raw), (prose, raw)
    assert set(parsed) == _KEYS
    assert prose in visible


# ---------------------------------------------------------------------------
# 回归修复 · 行尾（CRLF）半支持：块**不在串尾**时整块不被识别
# ---------------------------------------------------------------------------

_BUG057_RAW = '{"action":"clarify","use_candidates":false,"clarification":"你指的是哪一份？"}'


def _block_crlf(raw: str, *, after: str = "") -> str:
    return f"说明\r\n<<<CLOSEOUT>>>\r\n{raw}\r\n<<<END>>>\r\n{after}"


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("LF-尾换行", f"说明\n<<<CLOSEOUT>>>\n{_BUG057_RAW}\n<<<END>>>\n"),
        ("LF-多余文本", f"说明\n<<<CLOSEOUT>>>\n{_BUG057_RAW}\n<<<END>>>\n尾巴\n"),
        ("CRLF-在串尾", _block_crlf(_BUG057_RAW).rstrip("\r\n")),
        ("CRLF-尾换行", _block_crlf(_BUG057_RAW)),
        ("CRLF-多余文本", _block_crlf(_BUG057_RAW, after="尾巴\r\n")),
    ],
)
def test_line_endings_do_not_change_the_outcome(label, text):
    """**回归修复 回归钉**：行尾不得改变判定。

    改前实测：CRLF 且块**不在串尾**（哪怕只多一个尾换行）⇒ 整块**不被识别**
    （`parsed is None` 且 `invalid is False`）⇒ ① 该轮的结构化结束决策（例如 `clarify`）
    **静默失效**、按经典路径作答；② 没识别成块 ⇒ 不剥离 ⇒ **原始 JSON 漏进 `visible`**
    （会进 `thought` 步）。两者都不可观测（不失败、也无 note）。
    """
    visible, parsed, invalid = parse_closeout_message(text)
    assert invalid is False, label
    assert parsed == json.loads(_BUG057_RAW), f"{label}：块没被识别"
    assert "<<<CLOSEOUT>>>" not in visible and "<<<END>>>" not in visible, label
    assert '"use_candidates"' not in visible, f"{label}：原始 JSON 漏进了可见正文"
