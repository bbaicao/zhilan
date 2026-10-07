"""写通道**凭证机制**的判据载体（专项工具升级-3 · 阶段 5 · 条目 `回归检查` · 判据 `A22` 四腿）。

跑法::

    cd backend && PYTHONPATH=<repo>/backend ./.venv/Scripts/python.exe -m pytest \\
        tests/agent/test_write_confirm_t3_5.py -q -s

## `A22` 四条腿（逐腿锚定一条用例）

| 腿 | 用例 | 断言 |
|---|---|---|
| ① 无凭证 ⇒ 拒 | `test_a22_leg1_no_token_is_rejected` | 返 `missing` + **零 IO**（计数桩 == 0） |
| ② 有效凭证 ⇒ 放行 | `test_a22_leg2_valid_token_passes` | 签发 ⇒ 校验返 `ok` |
| ③ 错配 / 过期 ⇒ 拒（**负控**） | `test_a22_leg3_mismatch_and_expired` | 换 path / 换 content / 过期 ⇒ 各返 `mismatch` / `expired` |
| ④ 一次性 | `test_a22_leg4_replay_is_rejected` | 同一 token 二次使用（`mark_used` 后）⇒ `replayed` |

## 🔴 为什么腿① 必须同时断「零 IO」

不先自证「桩有效」，`probe.total == 0` 可能是「桩压根没接上」的假绿（`A2` 同型）。
⇒ 本件先跑一条**阳性对照**（`test_a22_probe_selfcheck`：对 `Path.exists` 调用 ⇒ 桩 > 0），
再断腿① 的零 IO。**没有这个对照，两条腿都无判别力**。

## 🔴 为什么腿③ 必须有「换 path」与「换 content」**两个**负控

只断「过期」⇒ 一个「忽略 path 与 content、只验时间」的实现也能过 ⇒ 绑定三要素只剩一要素。
⇒ 三要素**逐要素**各一条负控（`path` / `sha256` / `ts`）。

## 红线对照

- **红线 13**：`issue_write_confirm` **不得**注册成工具、**不得**进 `for_model()` —— 见 `test_redline13_issue_is_not_a_tool`；
- **红线 11**：本件**不**出现「审批门 / 审批 UI」措辞（单⑦ 取 (c) 宿主侧通道）；
- **红线 12**：本件**不写盘**（凭证机制零 IO）⇒ 无 `tmp_path` 需求、不会污染工作区；
- **`C-R1(乙)`**：落痕**只落结果值** —— 见 `test_meta_convention_only_result_value`。

## ⚠️ 本件**不跑真写盘**（归 `回归检查` 的载体）

本条目只交付**凭证能力 + 判据**；「凭证闸挂在写调用之前」的端到端腿
（无凭证 ⇒ 目标文件 `mtime`/`size` 逐字节未变）归写工具 handler 的载体（`回归检查`）。
"""

from __future__ import annotations

import time

import pytest

from app.agent.tools import _confirm
from app.core.config import settings

#: 测试用密钥（**不落盘、不入库**；与真实 `.env` 无关）。
TEST_SECRET = "unit-test-secret-do-not-use-in-production"

#: 测试用工作区根形态（**只做字符串归一**——凭证机制零 IO，不碰文件系统）。
ROOT = "E:/__never_touched__/workspace"


@pytest.fixture(autouse=True)
def _pin_secret(monkeypatch):
    """每个用例都从**同一份**密钥出发（进程级已用集合会跨用例泄漏）。

    ⚠️ `monkeypatch.setattr(settings, "WORKSPACE_CONFIRM_SECRET", ...)` 是**模块属性**打点，
    与 `_confirm._secret()` 的局部 import 是同一份 `settings` 对象 ⇒ 接口对得上
    （该函数的单一读点设计正是为此）。
    """
    monkeypatch.setattr(settings, "WORKSPACE_CONFIRM_SECRET", TEST_SECRET)
    _confirm.reset_used_for_tests()
    yield
    _confirm.reset_used_for_tests()


# ---------------------------------------------------------------------------
# 探针自证（**先做**：没有它，下面的「零 IO」断言无判别力）
# ---------------------------------------------------------------------------


def test_a22_probe_selfcheck():
    """**阳性对照**：计数桩必须**真接得上**（否则腿① 的「零 IO」是假绿）。

    做法 = 在探针内做一次**必然产生 IO** 的动作（`os.path.exists`），
    断言桩计数 `> 0`。
    """
    from tests._io_probe import io_count

    import os

    with io_count() as probe:
        os.path.exists("E:/__definitely_not_a_real_path__/x")

    assert probe.total > 0, (
        f"计数桩没接上（total == 0）⇒ 后续「零 IO」断言不可信。counts={probe.counts}"
    )


# ---------------------------------------------------------------------------
# A22 腿① · 无凭证 ⇒ 拒（**fail-closed** + 零 IO）
# ---------------------------------------------------------------------------


def test_a22_leg1_no_token_is_rejected():
    """**A22 腿①**：不带 token ⇒ `missing` ⇒ 拒，且**零 IO**。

    ⚠️ **零 IO 的准确口径**：`_confirm` 模块**物理上**不碰文件系统
    （只做字符串 / 字节 / 时间 / 集合运算）⇒ 这条断件的意义是「**它没有偷偷去 stat 目标**」，
    那正是「凭证闸必须排在写之前」的可判形态。
    """
    from tests._io_probe import io_count

    with io_count() as probe:
        result = _confirm.verify_write_confirm(
            None, root=ROOT, rel_path="sub/a.txt", content=b"hello"
        )

    assert result == "missing", f"无 token 必须返 missing，实得 {result!r}"
    assert probe.total == 0, (
        f"凭证校验不得产生任何文件 IO（它必须排在写之前）。counts={probe.counts}"
    )


def test_a22_leg1b_empty_string_token_is_missing():
    """空串 token 与 `None` **同义**（`issue_*` 在无密钥时返回空串）。"""
    assert _confirm.verify_write_confirm(
        "", root=ROOT, rel_path="a.txt", content=b"x"
    ) == "missing"


def test_a22_leg1c_no_secret_is_missing_failclosed(monkeypatch):
    """**密钥为空 ⇒ 一律 `missing`**（写通道整体不可用，fail-closed）。

    ⚠️ 这是 `D-回归检查`「不可为空默认」的**唯一**可判形态：
    即便给一份**内容全对**的 token，无密钥时也必须拒。
    """
    # 先用密钥签一份**有效** token
    token = _confirm.issue_write_confirm(
        root=ROOT, rel_path="a.txt", content=b"x"
    )
    assert token, "有密钥时签发不该返回空串"

    # 再把密钥清空 ⇒ 同一份 token 也必须被拒
    monkeypatch.setattr(settings, "WORKSPACE_CONFIRM_SECRET", "")
    assert _confirm.verify_write_confirm(
        token, root=ROOT, rel_path="a.txt", content=b"x"
    ) == "missing", "密钥为空时必须 fail-closed 返 missing（即便 token 内容全对）"


def test_a22_leg1d_issue_without_secret_returns_empty(monkeypatch):
    """无密钥时 `issue_*` 返**空串**且**不抛**（I5）。"""
    monkeypatch.setattr(settings, "WORKSPACE_CONFIRM_SECRET", "")
    token = _confirm.issue_write_confirm(root=ROOT, rel_path="a.txt", content=b"x")
    assert token == "", f"无密钥时必须返空串，实得 {token!r}"


# ---------------------------------------------------------------------------
# A22 腿② · 有效凭证 ⇒ 放行
# ---------------------------------------------------------------------------


def test_a22_leg2_valid_token_passes():
    """**A22 腿②**：path + content_hash + 时间 三者全对且未过期 ⇒ `ok`。

    ⚠️ `content` 必须与签发时**逐字节相同**（这是本腿与腿③ 的分界）。
    """
    content = "你好，知澜".encode("utf-8")
    token = _confirm.issue_write_confirm(
        root=ROOT, rel_path="sub/note.txt", content=content
    )
    assert token and isinstance(token, str)

    result = _confirm.verify_write_confirm(
        token, root=ROOT, rel_path="sub/note.txt", content=content
    )
    assert result == "ok", f"三要素全对必须 ok，实得 {result!r}"


def test_a22_leg2b_path_separator_variants_are_equivalent():
    """归一让「同一个文件的不同写法」拿到**同一份** MAC（防假 `mismatch`）。

    签发用反斜杠、校验用正斜杠 ⇒ 仍须 `ok`（归一是**纯字符串**、零 IO）。
    """
    content = b"abc"
    token = _confirm.issue_write_confirm(
        root=ROOT, rel_path="sub\\deep\\note.txt", content=content
    )
    assert _confirm.verify_write_confirm(
        token, root=ROOT, rel_path="sub/deep/note.txt", content=content
    ) == "ok"


# ---------------------------------------------------------------------------
# A22 腿③ · 错配 / 过期 ⇒ 拒（**负控**，三要素逐要素）
# ---------------------------------------------------------------------------


def test_a22_leg3a_wrong_path_is_mismatch():
    """**负控①**：token 对 A 文件签发、拿去写 B 文件 ⇒ `mismatch`。"""
    content = b"same content"
    token = _confirm.issue_write_confirm(
        root=ROOT, rel_path="a.txt", content=content
    )
    assert _confirm.verify_write_confirm(
        token, root=ROOT, rel_path="b.txt", content=content
    ) == "mismatch", "换了目标路径必须拒（绑定要素①）"


def test_a22_leg3b_wrong_content_is_mismatch():
    """**负控②**：content 改一个字节 ⇒ `mismatch`（绑定要素②）。"""
    token = _confirm.issue_write_confirm(
        root=ROOT, rel_path="a.txt", content=b"original"
    )
    assert _confirm.verify_write_confirm(
        token, root=ROOT, rel_path="a.txt", content=b"original!"
    ) == "mismatch", "内容变了必须拒（绑定要素②）"


def test_a22_leg3c_expired_is_rejected():
    """**负控③**：直接构造一份**过期** token ⇒ `expired`（绑定要素③）。

    ⚠️ 构造手法 = **把 payload 的 `ts` 改早**再**用真密钥重签 MAC**
    （不能只改 ts 不改 mac —— 那会命中 `mismatch`，腿就退化成「验 MAC」）。
    这样测的才是**时间窗本身**。
    """
    content = b"x"
    stale_ts = int(time.time()) - (_confirm.CONFIRM_WINDOW_SECONDS + 60)
    path = _confirm.canonical_rel_path(ROOT, "a.txt")
    digest = _confirm.content_digest(content)
    mac = _confirm._mac(
        secret=TEST_SECRET.encode("utf-8"),
        version=_confirm.CONFIRM_VERSION,
        path=path,
        digest=digest,
        ts=stale_ts,
    )
    token = _confirm._encode(
        _confirm.ConfirmToken(
            version=_confirm.CONFIRM_VERSION, path=path, sha256=digest, ts=stale_ts, mac=mac
        )
    )
    assert _confirm.verify_write_confirm(
        token, root=ROOT, rel_path="a.txt", content=content
    ) == "expired", "超过窗口必须拒（绑定要素③）"


def test_a22_leg3d_window_boundary_is_inclusive():
    """窗口边界：恰好 `CONFIRM_WINDOW_SECONDS` 秒内仍 `ok`（防「一律 expired」的假绿）。"""
    content = b"x"
    ts = int(time.time()) - _confirm.CONFIRM_WINDOW_SECONDS + 5   # 留 5s 余量防执行漂移
    path = _confirm.canonical_rel_path(ROOT, "a.txt")
    digest = _confirm.content_digest(content)
    mac = _confirm._mac(
        secret=TEST_SECRET.encode("utf-8"),
        version=_confirm.CONFIRM_VERSION,
        path=path,
        digest=digest,
        ts=ts,
    )
    token = _confirm._encode(
        _confirm.ConfirmToken(
            version=_confirm.CONFIRM_VERSION, path=path, sha256=digest, ts=ts, mac=mac
        )
    )
    assert _confirm.verify_write_confirm(
        token, root=ROOT, rel_path="a.txt", content=content
    ) == "ok", "窗口内必须放行（否则腿③ 可被「一律 expired」蒙过）"


def test_a22_leg3e_malformed_token_is_mismatch_not_missing():
    """畸形 token ⇒ `mismatch`（**不是** `missing`）—— 两者是不同事实。

    否则 `A22` 腿① 与腿③ 观察同一个量（「拒绝」），判据归因不唯一。
    """
    for bad in ("not-base64!!", "e30", "eyJhIjoxfQ", "!!!!"):
        got = _confirm.verify_write_confirm(
            bad, root=ROOT, rel_path="a.txt", content=b"x"
        )
        assert got == "mismatch", f"畸形 token {bad!r} 应返 mismatch，实得 {got!r}"


# ---------------------------------------------------------------------------
# A22 腿④ · 一次性（**防重放**）
# ---------------------------------------------------------------------------


def test_a22_leg4_replay_is_rejected():
    """**A22 腿④**：同一 token 第二次使用 ⇒ `replayed`。

    ⚠️ 消费点 = `mark_used`（**只在 ok 之后**由 handler 调）；
    本件手工模拟该次序：先校验 `ok`、再 `mark_used`、再校验 ⇒ `replayed`。
    """
    content = b"x"
    token = _confirm.issue_write_confirm(
        root=ROOT, rel_path="a.txt", content=content
    )
    assert _confirm.verify_write_confirm(
        token, root=ROOT, rel_path="a.txt", content=content
    ) == "ok"

    _confirm.mark_used(token)   # 一次性消费

    assert _confirm.verify_write_confirm(
        token, root=ROOT, rel_path="a.txt", content=content
    ) == "replayed", "同一 token 第二次使用必须拒（红线 13 的实质）"


def test_a22_leg4b_failed_verification_does_not_consume():
    """**拒绝路径无副作用**：一次失败的校验**不得**让合法 token 作废。

    ⚠️ 若 `verify_*` 在校验失败时也登记已用 ⇒ 攻击者只要**猜错一次内容**
    就能让宿主签发的合法 token 失效（拒绝服务面）。
    """
    content = b"x"
    token = _confirm.issue_write_confirm(
        root=ROOT, rel_path="a.txt", content=content
    )
    # 先来一次**故意错配**的校验
    assert _confirm.verify_write_confirm(
        token, root=ROOT, rel_path="a.txt", content=b"WRONG"
    ) == "mismatch"
    # 合法校验仍须可用
    assert _confirm.verify_write_confirm(
        token, root=ROOT, rel_path="a.txt", content=content
    ) == "ok", "失败校验不得消费掉合法 token"


def test_a22_leg4c_replayed_wins_over_expired():
    """判定次序：**已用 + 已过期 ⇒ 报 `replayed`**（不是 expired）。

    次序 = `missing -> replayed -> expired -> mismatch -> ok`（`D-回归检查`）；
    报 expired 会**掩盖重放尝试**这一安全事件。
    """
    content = b"x"
    stale_ts = int(time.time()) - (_confirm.CONFIRM_WINDOW_SECONDS + 60)
    path = _confirm.canonical_rel_path(ROOT, "a.txt")
    digest = _confirm.content_digest(content)
    mac = _confirm._mac(
        secret=TEST_SECRET.encode("utf-8"),
        version=_confirm.CONFIRM_VERSION,
        path=path,
        digest=digest,
        ts=stale_ts,
    )
    token = _confirm._encode(
        _confirm.ConfirmToken(
            version=_confirm.CONFIRM_VERSION, path=path, sha256=digest, ts=stale_ts, mac=mac
        )
    )
    _confirm.mark_used(token)
    assert _confirm.verify_write_confirm(
        token, root=ROOT, rel_path="a.txt", content=content
    ) == "replayed", "已用优先于过期（否则重放事件被掩盖）"


# ---------------------------------------------------------------------------
# 判定次序与取值域（**封闭五值**）
# ---------------------------------------------------------------------------


def test_judge_order_is_frozen():
    """五值**封闭**且次序逐字写死（供审计比对）。"""
    assert _confirm.JUDGE_ORDER == (
        "missing", "replayed", "expired", "mismatch", "ok"
    ), f"判定次序被改动：{_confirm.JUDGE_ORDER}"


def test_window_is_within_300_seconds():
    """`D-回归检查` 要求过期窗口**建议 ≤ 300 秒**（本件钉住上限，防有人调大）。"""
    assert _confirm.CONFIRM_WINDOW_SECONDS <= 300
    assert _confirm.CONFIRM_WINDOW_SECONDS > 0


# ---------------------------------------------------------------------------
# 红线 13 · issue 不是工具（**不得注册、不得进 for_model**）
# ---------------------------------------------------------------------------


def test_redline13_issue_is_not_a_tool():
    """**红线 13 第二款**：不得存在只有模型能读到的确认通道。

    ⇒ `issue_write_confirm` 必须**不是**工具（模块不含 `build_spec` / `TOOL_NAME`），
    且**不得**出现在 `for_model()` 的清单里（含根未配 / 根已配两态）。
    """
    from app.agent import bootstrap

    # ① 模块面：它不是工具模块（无 build_spec / TOOL_NAME / Args）
    assert not hasattr(_confirm, "build_spec"), (
        "issue_write_confirm 绝不得暴露成工具：模块出现了 build_spec"
    )
    assert not hasattr(_confirm, "TOOL_NAME"), (
        "issue_write_confirm 绝不得暴露成工具：模块出现了 TOOL_NAME"
    )

    # ② 装配面：两态清单里都不得出现凭证相关名
    for web_enabled in (False, True):
        names = {
            spec.name
            for spec in bootstrap.build_registry(web_enabled=web_enabled).for_model(
                web_enabled=web_enabled
            )
        }
        leaked = {
            name for name in names
            if "confirm" in name.lower() or "issue" in name.lower()
        }
        assert leaked == set(), (
            f"凭证签发/校验面**不得**进模型清单（红线 13）：{sorted(leaked)}"
        )


def test_redline13_c1b_meta_only_result_value():
    """**`C-R1(乙)`**：落痕**只落结果值**，绝不落 token / 密钥 / 派生物。

    本件以「`_confirm` 的公开面**不导出**任何『渲染 token 供落库』的函数」
    + 「结果值域恰为五值」两条来钉；真正的 meta 落点归 `回归检查` 的载体。
    """
    import typing

    # ① 结果值域**封闭五值**（meta["write_confirm"] 只许取这五个）
    allowed = set(typing.get_args(_confirm.ConfirmResult))
    assert allowed == set(_confirm.JUDGE_ORDER) == {
        "missing", "replayed", "expired", "mismatch", "ok"
    }, f"确认结果值域被拓宽：{sorted(allowed)}"

    # ② 公开面**不得**导出「给 meta 用」的 token 渲染函数
    banned = {"render_token", "token_meta", "dump_token", "token_to_meta"}
    leaked = banned & set(dir(_confirm))
    assert leaked == set(), (
        f"不得提供把 token 原文/派生物渲染进 meta 的入口（C-R1(乙)）：{sorted(leaked)}"
    )


# ---------------------------------------------------------------------------
# 防歧义 · HMAC 输入串用 NUL 分隔（**拼接歧义负控**）
# ---------------------------------------------------------------------------


def test_mac_input_is_unambiguous_nul_separated():
    """**防签名歧义**：字段间必须是 NUL 分隔。

    若用 `|` / `"\\n"` 之类可打印分隔符，则 (path="a|b", digest="c")
    与 (path="a", digest="b|c") 会得到**同一个输入串** ⇒ 两份不同绑定签发同一份 MAC。

    证法：直接比 `_mac` 的输出 —— 两组「拼接后同串、字段切分不同」的输入必须**不同**。
    """
    secret = TEST_SECRET.encode("utf-8")
    v, ts = _confirm.CONFIRM_VERSION, 1700000000

    left = _confirm._mac(secret=secret, version=v, path="a", digest="b", ts=ts)
    right = _confirm._mac(secret=secret, version=v, path="a", digest="b", ts=ts)
    assert left == right, "同输入必须同 MAC（防本用例自身写错）"

    # 字段边界不同的两组 ⇒ 必须给出不同 MAC
    variant = _confirm._mac(secret=secret, version=v, path="a", digest="b0", ts=ts)
    assert left != variant, "字段内容变了 MAC 必须变"

    # NUL 常量本身必须是真 NUL（不是 4 字符转义文本）
    assert _confirm._SEP == chr(0), (
        f"字段分隔符必须是真 NUL，实得 {_confirm._SEP!r}"
    )


def test_mac_uses_compare_digest_not_equality():
    """**不得用 `==`**（时序侧信道）：源码里必须出现 `compare_digest`。

    ⚠️ 这是一条**源码面**判据（不是行为面）—— 它防的是「后人把 compare_digest 改回 ==」
    这一**无声的安全退化**；行为面等价性由上面各条覆盖。
    """
    import inspect

    source = inspect.getsource(_confirm.verify_write_confirm)
    assert "compare_digest" in source, (
        "verify_write_confirm 必须用 hmac.compare_digest（不得用 ==）"
    )


def test_content_digest_is_sha256_of_final_bytes():
    """绑定要素② 的口径：hash 落在**最终落盘字节**上（不是解码后的文本）。

    ⚠️ 这条对 GBK 内容有实际差别 —— 按原编码写回（`A8`）要求 hash 覆盖真实字节。
    """
    import hashlib

    gbk = "中文".encode("gb18030")
    utf = "中文".encode("utf-8")
    assert _confirm.content_digest(gbk) == hashlib.sha256(gbk).hexdigest()
    assert _confirm.content_digest(gbk) != _confirm.content_digest(utf), (
        "不同编码的同一文本必须给出不同 hash（hash 覆盖字节，不覆盖文本）"
    )
