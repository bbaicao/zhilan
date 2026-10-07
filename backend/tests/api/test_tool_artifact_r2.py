"""专项 · 工具升级-2（R2）· 阶段一 · **大结果不丢**的判据钉子（R2-05 件级 / R2-06 接线级）。

判据（开工包 §3）：

- **A7** 超阈值 ⇒ **头尾预览 + 标记齐全**；标记里的 ``id`` 能对回落盘文件（sha256 前 12 位）；
- **A8** 落盘失败（只读 / 盘满 / **超 2 MiB** 模拟）⇒ **回落现状**（老标记）+ warning、**不抛**；
- **A10** 通道预算语义不变（单条 ≤4000 / 总量 ≤12000），顺序与去重键不受影响。

## 落盘根的测试接缝（D-回归检查）

``tests/api/conftest.py`` 的第 11 条 autouse 夹具 ``pin_tool_artifact_dir`` 把
``tool_artifact_path`` **整个属性**替换成 ``tmp_path``（D-S1-13②的形态）。
⚠️ A9 的补强断言（「父目录 == 数据根下的 ``data/``」）**不能**在这个形态下判 —— 父目录会
变成 ``tmp_path`` 本身 ⇒ 断言**结构性假红**。⇒ 该用例自己把**原始属性**换回来，再按
D-S1-13③ 的形式 patch ``config_mod.ROOT_DIR``。
"""

from __future__ import annotations

import hashlib
import logging

from app.agent import runner, tool_artifacts
from app.agent.runner import ToolCollector
from app.agent.types import ToolObservation
from app.core import config as config_mod
from app.core.config import settings

#: **生产实现**的原始属性对象。⚠️ 必须在**模块导入时**抓走 —— ``tests/api/conftest.py`` 的
#: autouse 夹具会在每个用例开始前把类属性换成 ``property(lambda self: tmp_path)``
#: （D-S1-13②），进了用例体就再也拿不到原物了。
_ORIGINAL_TOOL_ARTIFACT_PATH = config_mod.Settings.__dict__["tool_artifact_path"]


# --------------------------------------------------------------------------
# A7 / A8 / A9 · 件级：落盘件本身
# --------------------------------------------------------------------------


def test_a7_artifact_id_is_the_sha256_prefix_and_saving_is_idempotent(tmp_path):
    """A7（件级）：``id`` = 内容哈希 sha256 前 12 位；同内容 ⇒ 同 id、同文件、**幂等**。"""
    body = "落盘件内容" * 10
    first = tool_artifacts.save(body)
    assert first is not None
    assert first == hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    assert first == tool_artifacts.artifact_id(body)
    assert len(first) == tool_artifacts.ARTIFACT_ID_LENGTH

    files = sorted(tmp_path.rglob("*.txt"))
    assert len(files) == 1, [str(p) for p in files]
    assert files[0].stem == first
    assert files[0].read_bytes() == body.encode("utf-8")
    # 路径形状 = <落盘根>/<yyyyMMdd>/<id>.txt
    assert len(files[0].parent.name) == 8 and files[0].parent.name.isdigit()
    assert files[0].parent.parent == tmp_path

    # 幂等：同内容再存 ⇒ 同 id、不产生第二件
    assert tool_artifacts.save(body) == first
    assert len(sorted(tmp_path.rglob("*.txt"))) == 1
    # 内容不同 ⇒ id 不同（可去重、可核对）
    assert tool_artifacts.artifact_id(body + "x") != first


def test_a8_exactly_at_the_limit_is_allowed():
    """A8 边界：**恰等于** 2 MiB 允许落盘（上限是「超过才拒」，不是「达到就拒」）。"""
    at_limit = "w" * tool_artifacts.MAX_ARTIFACT_BYTES
    assert tool_artifacts.save(at_limit) is not None


def test_a8_oversize_returns_failure_signal_and_never_raises(caplog):
    """A8：超 2 MiB ⇒ 返回 ``None``（**不抛**）+ warning —— I5 在件级的落点。"""
    oversized = "w" * (tool_artifacts.MAX_ARTIFACT_BYTES + 10)
    with caplog.at_level(logging.WARNING, logger="app.agent.tool_artifacts"):
        assert tool_artifacts.save(oversized) is None
    assert any("落盘失败" in record.getMessage() for record in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def test_a9_artifact_root_sits_under_the_data_root(monkeypatch, tmp_path):
    """A9（**本包新立的补强**）：冻结闸门**挡不住「锚错地方」** ⇒ 这里直接钉落盘根的位置。

    闸门 ``check_frozen_paths.py`` 只挡「未锚定的相对路径 / 裸 ``__file__`` / ``parents[]`` /
    CWD / ``_MEIPASS``」四类；**实测**硬编码绝对路径、``env_store.ROOT_DIR``、甚至包内明令
    禁止的 ``main._roots()`` 全都 **0 命中**（``_roots`` 本身就在闸门白名单里）⇒
    **过闸门 ≠ 落盘根正确**。本用例补上那半边。
    """
    # 先把夹具换掉的「整属性」还原成**生产实现**，否则父目录会变成 tmp_path 本身（结构性假红）
    monkeypatch.setattr(type(settings), "tool_artifact_path", _ORIGINAL_TOOL_ARTIFACT_PATH)

    # ① 真实环境：落盘根 = **数据根**下的 data/tool_artifacts（不是 _MEIPASS、不是 CWD、不是包目录）
    real = settings.tool_artifact_path
    assert real == config_mod.ROOT_DIR / "data" / "tool_artifacts", str(real)
    assert real.parent == config_mod.ROOT_DIR / "data"
    assert real.parent.name == "data"

    # ② D-S1-13③ 的形态：patch ROOT_DIR ⇒ 属性**自然解析**出 <tmp>/data/tool_artifacts
    monkeypatch.setattr(config_mod, "ROOT_DIR", tmp_path, raising=True)
    assert settings.tool_artifact_path == tmp_path / "data" / "tool_artifacts"
    assert settings.tool_artifact_path.parent == tmp_path / "data"


# --------------------------------------------------------------------------
# A7 / A8 / A10 · 接线级：通道里的标记、预览与预算（R2-06）
# --------------------------------------------------------------------------


def _channel_items(texts: dict[str, str]):
    """把若干「工具调用」灌进 :class:`ToolCollector`，取回**通道条目**（与 T0 判据同路）。"""
    collector = ToolCollector()
    for rid, text in texts.items():
        collector.note_tool_name(rid, f"mcp:kit:{rid}")
        collector.absorb(rid, ToolObservation(text=text))
    for rid in texts:  # 事件序 = 与完成序相同，便于断言「按序截断到谁」
        collector.note_call_order(rid)
    return collector.tool_context_items()


def test_a7_channel_mark_id_resolves_to_the_saved_file(tmp_path):
    """A7（接线级）：超单条上限 ⇒ 标记里的 ``id`` **能对回**盘上那件落盘件。"""
    body = "头" * 2500 + "尾" * 2500  # 5000 字符 ⇒ 超单条 4000 ⇒ 走 ① 分支
    items = _channel_items({"c1": body})
    text = items[0].text

    assert text.endswith("）")
    assert "全文已落盘" in text
    saved = sorted(tmp_path.rglob("*.txt"))
    assert len(saved) == 1, [str(p) for p in saved]
    identity = saved[0].stem
    assert len(identity) == tool_artifacts.ARTIFACT_ID_LENGTH
    assert identity in text
    assert identity == tool_artifacts.artifact_id(body)
    # ① 分支落的是**工具返回的原文**（D-回归检查）
    assert saved[0].read_bytes().decode("utf-8") == body


def test_a7_preview_is_head_and_tail_with_a_visible_separator():
    """A7（D-回归检查）：**多行**输入下，「头尾预览」与「纯前缀截断」必须能区分开。

    ⚠️ 既有 A4 判据跑的是**单行**输入（``"x"*5000`` / ``"y"*9000``）—— 实测那组输入下
    「带分隔符的头尾预览」与「纯前缀截断」产出的三条被断言性质**完全相同** ⇒ 既有用例全绿
    **不能**证明头尾预览真的实现了。本用例补三条：① 有可辨识分隔符；② 尾段取自原文**末尾**；
    ③ 头尾**不相交**。
    """
    body = (
        "".join(f"H{i:04d}\n" for i in range(300))
        + "M" * 3000
        + "".join(f"T{i:04d}\n" for i in range(300))
    )
    items = _channel_items({"c1": body})
    text = items[0].text

    elision = runner.TOOL_CONTEXT_ELISION_MARK
    assert elision in text, "正文里没有可辨识分隔符 ⇒ 模型会把两段读成连续正文（假引用风险）"

    head_part, _, rest = text.partition(elision)
    cut = rest.rfind(runner.TOOL_CONTEXT_TRUNCATION_PREFIX)
    assert cut > 0, rest[-120:]
    tail_part = rest[:cut]

    assert body.startswith(head_part), "头段不是原文开头"
    assert body.endswith(tail_part), "尾段不是原文**末尾**（纯前缀截断在这里必红）"
    assert head_part and tail_part
    assert len(head_part) + len(tail_part) < len(body), "头尾两段有重叠"
    assert head_part.startswith("H0000")
    assert tail_part.rstrip().endswith("T0299")


def test_a10_channel_budget_semantics_unchanged():
    """A10：落盘**只是「不丢」**，**不放宽**送模型的量 —— 单条 ≤4000 / 总量 ≤12000。

    与既有 ``test_tool_context_t0.py::test_a4_single_item_and_total_limits_are_applied_in_order``
    同口径（那边是**单行**输入），这里补一条**多行**输入 + 分隔符的腿。
    """
    texts = {f"c{i}": "x" * 5000 for i in range(4)}
    items = _channel_items(texts)

    assert len(items) == 4
    assert [item.tool_call_id for item in items] == ["c0", "c1", "c2", "c3"]
    for index in range(3):
        assert len(items[index].text) == runner.TOOL_CONTEXT_ITEM_LIMIT, index
        # 精确填满 ⇒ 必须带分隔符（不是纯前缀截断）
        assert runner.TOOL_CONTEXT_ELISION_MARK in items[index].text, index
    total = sum(runner._tool_text_length(item.text) for item in items)
    assert total <= runner.TOOL_CONTEXT_TOTAL_LIMIT, total
    assert items[3].text.startswith("x")
    assert runner._tool_text_length(items[3].text) < 1000
    assert "已截断" in items[3].text or "超出上下文总量上限" in items[3].text

    # 6 条 9000 字符：任意条数下总正文量都不得越过总量上限
    many = _channel_items({f"d{i}": "y" * 9000 for i in range(6)})
    assert len(many) == 6
    assert all(item.text for item in many)
    assert (
        sum(runner._tool_text_length(item.text) for item in many)
        <= runner.TOOL_CONTEXT_TOTAL_LIMIT
    )
    for item in many:
        assert len(item.text) <= (
            runner.TOOL_CONTEXT_ITEM_LIMIT
            + len(runner.TOOL_CONTEXT_TRUNCATION_MARK.format(chars=99999))
            + len(runner.TOOL_CONTEXT_ELISION_MARK)
        )


def test_a8_channel_falls_back_to_the_old_mark_when_saving_fails(caplog):
    """A8（接线级）：落盘失败 ⇒ 回落**现状截断**（老标记）+ warning，**不抛**、不改 ``ok``。"""
    oversized = "w" * (tool_artifacts.MAX_ARTIFACT_BYTES + 10)
    with caplog.at_level(logging.WARNING, logger="app.agent.tool_artifacts"):
        items = _channel_items({"c1": oversized})
    text = items[0].text

    assert "id=" not in text, "落盘失败时不得编一个 id 出来"
    assert text.endswith(runner.TOOL_CONTEXT_TRUNCATION_MARK.format(chars=len(oversized)))
    assert items[0].ok is True, "落盘失败只影响通道文本形态，不得改 ok"
    assert any("落盘失败" in record.getMessage() for record in caplog.records)
