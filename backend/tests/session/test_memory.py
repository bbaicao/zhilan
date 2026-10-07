"""跨会话记忆（阶段 5 · 条目 5.1）的**纯函数层 + 模型层**用例（被测对象 ``app/session/memory.py``）。

## 这份文件钉什么

被测模块 docstring 的三条硬口径，一条一簇用例：

1. **关态零行为**：``MEMORY_ENABLE=False`` ⇒ ``remember`` 返回 ``None`` 且**一笔 SQL 都不发**、
   ``recall`` 返回 ``[]`` 且零查询、``render_block`` 恒空串。这一条用**计数假会话**钉：
   只看返回值会把「查了库但没写」误判成「关态零行为」，计数是唯一硬证据。
2. **写路径**：``remember`` 的规范化 / 去重 / 拒绝三态，以及 ``(scope, text_hash)`` 复合唯一
   对应的「重复不是失败」（返回 ``None``、不抛、不计数）。
3. **读与渲染**：``recall`` 的倒序 + ``MEMORY_TOP_K``；``render_block`` 的预算规则
   ——**下一条会超预算就停**，不截断单条、不重排。

外加抽取侧可注入的部分：``extraction_input`` / ``parse_facts``（三级兜底）/
``remember_turn`` + ``schedule_extract`` + ``drain_memory_tasks``（**唯一等待点**）/
``failure_counts`` 的三级失败分类。

## 纪律

- **绝不连真模型**：抽取一律注入只回固定 JSON 的假模型（照 ``test_compaction.py`` 的
  ``FakeCompactionModel`` 写：零出网、可注入异常）；
- 真库用例一律走 ``tests/conftest.py`` 的 ``db`` 夹具（每例清表），跨连接复核走 ``fresh_db``；
- 失败计数与在途任务是**进程内**状态 ⇒ 每个用例开头 ``reset_failure_counts()``；
  ``drain_memory_tasks()`` 是唯一等待点（``usage.recorder.drain_pending`` **不含**记忆任务）。

本文件**不改实现、不改其它用例文件**；实现若有缺陷只写进交付报告。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text

from app.core.config import settings
from app.models import Chat, Memory, Message
from app.session import memory
from cubeloop.providers import faux_assistant_message

#: 抽取路径的假模型统一回这句（规范化后与输入一致，便于对拍）
FACT_TEXT = "用户在做跨会话记忆验收"
FACT_KIND = "interaction"


# --------------------------------------------------------------------------
# 助手
# --------------------------------------------------------------------------


def _facts_json(*pairs: tuple[str, str]) -> str:
    """``{"facts": [...]}`` 的 JSON 文本（``pairs`` = ``(text, kind)``）。"""
    return json.dumps(
        {"facts": [{"text": text, "kind": kind} for text, kind in pairs]}, ensure_ascii=False
    )


def _rows(*texts: str) -> list[SimpleNamespace]:
    """``render_block`` 只需要行上有 ``text``（它**不**碰会话、不碰别的列）。"""
    return [SimpleNamespace(text=item) for item in texts]


def _memory_row(
    body: str,
    *,
    kind: str = "knowledge",
    scope: str = memory.SCOPE_GLOBAL,
    created_at: datetime | None = None,
) -> Memory:
    """直接造一行（**不**走 ``remember``）：用于 recall / 注入侧的造数。"""
    kwargs: dict = {
        "scope": scope,
        "kind": kind,
        "text": body,
        "text_hash": memory._text_hash(body),
    }
    if created_at is not None:
        kwargs["created_at"] = created_at
    return Memory(**kwargs)


async def _count(session) -> int:  # noqa: ANN001 —— 会话夹具类型由 tests/conftest.py 定
    return int((await session.execute(select(func.count()).select_from(Memory))).scalar_one())


async def _fetch(session, *, order_by_id: bool = True):  # noqa: ANN001
    stmt = select(Memory)
    if order_by_id:
        stmt = stmt.order_by(Memory.id)
    return list((await session.execute(stmt)).scalars().all())


class _ScalarResult:
    """``db.execute(...)`` 的最小返回替身：``scalar_one_or_none()`` 恒 ``None``（= 查不到重复）。"""

    def scalar_one_or_none(self):  # noqa: ANN201
        return None

    def scalars(self):  # noqa: ANN201
        return self

    def all(self):  # noqa: ANN201
        return []


class _Nested:
    """``async with db.begin_nested()`` 的替身（SAVEPOINT 在这里无意义，只记账）。"""

    async def __aenter__(self):  # noqa: ANN201
        return self

    async def __aexit__(self, *_exc):  # noqa: ANN201
        return False


class _CountingSession:
    """**只记账的假会话**：每个 SQL 入口记一笔，写入对象收在 ``added`` 里。

    为什么必须用它（而不是断言返回值）：关态若悄悄查了库，``remember`` 仍可能返回 ``None``
    ——「查到了重复」与「压根没查」在返回值上长得一模一样。计数才能把「零 SQL」钉死。
    """

    def __init__(self) -> None:
        self.executes = 0
        self.adds = 0
        self.flushes = 0
        self.nesteds = 0
        self.added: list[object] = []

    async def execute(self, *_args, **_kwargs):  # noqa: ANN201
        self.executes += 1
        return _ScalarResult()

    def add(self, obj) -> None:  # noqa: ANN001
        self.adds += 1
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushes += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = len(self.added)  # 让「开态对照」分支能拿到 int id（真 db 由库回填）

    def begin_nested(self):  # noqa: ANN201
        self.nesteds += 1
        return _Nested()

    @property
    def sql_count(self) -> int:
        return self.executes + self.adds + self.flushes + self.nesteds


class _ExtractModel:
    """只回固定文本的抽取替身（照 ``FakeCompactionModel`` 写）：零出网、可注入异常。

    ``_call_model`` 只把 ``generate`` 的返回值当 ``AssistantMessage``（读 ``content`` 里
    ``type == "text"`` 的块），所以这里用 ``faux_assistant_message`` 造一个真的。
    """

    def __init__(self, reply: str | None = None, *, error: Exception | None = None) -> None:
        self.reply = reply if reply is not None else _facts_json((FACT_TEXT, FACT_KIND))
        self.error = error
        self.calls: list[dict] = []
        self.spec = SimpleNamespace(id="memory-fake")

    @property
    def count(self) -> int:
        return len(self.calls)

    async def generate(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append({"messages": list(messages), "kwargs": kwargs})
        if self.error is not None:
            raise self.error
        return faux_assistant_message(self.reply)


# ==========================================================================
# 1 · 关态零行为（A11 的纯函数面）
# ==========================================================================


@pytest.mark.asyncio
async def test_disabled_memory_issues_zero_sql(monkeypatch):
    """``MEMORY_ENABLE=False`` ⇒ 三个入口各自首行早退：零 SQL、零查询、恒空串。"""
    monkeypatch.setattr(settings, "MEMORY_ENABLE", False)
    fake = _CountingSession()

    # ① remember：返回 None **且**一笔 SQL 都没发（查 / 写 / flush / SAVEPOINT 全算）
    assert await memory.remember(fake, text="用户喜欢深色主题", kind="knowledge") is None
    assert fake.sql_count == 0, (
        f"关态不得不发 SQL：execute={fake.executes} add={fake.adds} "
        f"flush={fake.flushes} begin_nested={fake.nesteds}"
    )

    # ② recall：返回 [] **且**零查询
    assert await memory.recall(fake) == []
    assert fake.sql_count == 0, "关态 recall 不许查库"
    assert await memory.recall(fake, limit=3) == []
    assert fake.sql_count == 0

    # ③ render_block：恒空串（它压根没有会话参数，这条钉的是开关）
    assert memory.render_block(_rows("用户喜欢深色主题")) == ""
    assert memory.render_block(None) == ""
    assert fake.sql_count == 0

    # ④ **对照组**：同一套假件在开态下必须真的被 SQL 经过——否则上面的 0 是假绿
    #（计数根本没接上）。顺带说明 remember 的路径 = 一次查重 + 一次 add + 一次 flush。
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    new_id = await memory.remember(fake, text="用户喜欢深色主题", kind="knowledge")
    assert isinstance(new_id, int), new_id
    assert (fake.executes, fake.adds, fake.flushes) == (1, 1, 1)


def test_enabled_reader_defaults_to_false_when_key_missing(monkeypatch):
    """``_enabled`` 读不到键时按 **False**（未登记的键 = 没开）。

    真 ``settings`` 里这个键一定在（``config.py`` 有定义），所以「键缺失」只能用替身造：
    把 ``memory.settings`` 换成空对象（模块读的是它自己 import 进来的那个名字）。
    """
    monkeypatch.setattr(memory, "settings", SimpleNamespace())
    assert memory._enabled() is False
    assert memory.render_block(_rows("任何文本")) == ""
    monkeypatch.setattr(memory, "settings", SimpleNamespace(MEMORY_ENABLE=True))
    assert memory._enabled() is True


# ==========================================================================
# 2 · 写路径：remember
# ==========================================================================


@pytest.mark.asyncio
async def test_remember_writes_one_normalized_row(db, monkeypatch):
    """happy path：返回 int id；行上 scope / kind / 规范化 text / text_hash / 来源 / created_at 齐备。"""
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    raw = "  用户偏好   深色主题\n\n并且讨厌蓝光  "
    normalized = "用户偏好 深色主题 并且讨厌蓝光"
    db.add(Chat(id=41, title="source"))
    await db.flush()
    db.add(Message(id=99, chat_id=41, role="user", content=raw))
    await db.commit()

    new_id = await memory.remember(
        db, text=raw, kind="interaction", source_chat_id=41, source_message_id=99
    )
    await db.commit()

    assert isinstance(new_id, int), new_id
    rows = await _fetch(db)
    assert len(rows) == 1
    row = rows[0]
    assert int(row.id) == new_id
    assert row.scope == memory.SCOPE_GLOBAL == "global"
    assert row.kind == "interaction"
    assert row.text == normalized, "落库的是**规范化后**的文本"
    assert row.text_hash == hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    assert row.text_hash == memory._text_hash(raw), "hash 取规范化文本，不是原文"
    assert row.source_chat_id == 41 and row.source_message_id == 99
    assert row.last_used_at is None, "新行还没有被召回进提示词"

    # created_at 用**原生 SQL** 读：服务端默认值（``func.now()``）在 MySQL 下不在 INSERT
    # 的返回里，ORM 对象上那一格可能是未加载状态；原生 SQL 读的是库里的真相。
    created = (
        await db.execute(text("SELECT created_at FROM memories WHERE id = :i"), {"i": int(row.id)})
    ).scalar_one()
    assert created is not None


@pytest.mark.asyncio
async def test_remember_deduplicates_whitespace_variants(db, monkeypatch):
    """去重：同一句（含空白 / 换行变体）只留一行；第二次起返回 ``None`` 且**不抛**。"""
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    memory.reset_failure_counts()  # 进程内计数：先清零，免得被前面的用例带上来
    first = await memory.remember(db, text="用户 喜欢\n\n深色主题")
    await db.commit()
    assert isinstance(first, int), first

    variants = [
        "用户 喜欢 深色主题",  # 已经规范化
        "  用户\t喜欢   深色主题  ",  # 首尾 + 制表符 + 连续空格
        "用户 喜欢 深色主题\n",  # 尾部换行
    ]
    for variant in variants:
        assert await memory.remember(db, text=variant) is None, variant

    await db.commit()
    assert await _count(db) == 1, "四个写法只许留一行"
    assert memory.failure_counts()["stored"] == 0, "「重复」不是失败，也不该计数"


@pytest.mark.asyncio
async def test_remember_rejects_blank_text_and_illegal_kind(db, monkeypatch):
    """拒绝态：空 / 纯空白 / 非法 kind ⇒ ``None``，且一行都不写。"""
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)

    assert await memory.remember(db, text="") is None
    assert await memory.remember(db, text="   \n\t  ") is None
    assert await memory.remember(db, text="合法正文", kind="other") is None, "kind 只认二值"
    assert await memory.remember(db, text="合法正文", kind="") is None
    assert await memory.remember(db, text="合法正文", kind="Knowledge") is None, "大小写敏感"

    await db.commit()
    assert await _count(db) == 0, "被拒的调用不许留下任何行"


@pytest.mark.asyncio
async def test_remember_normalization_collapses_internal_whitespace_runs(db, monkeypatch):
    """规范化：首尾空白去掉、内部连续空白（含换行）折叠成**一个空格**。"""
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    await memory.remember(db, text="第一行\n\n\n第二行\t 第三行  结尾   ")
    await db.commit()

    rows = await _fetch(db)
    assert len(rows) == 1
    assert rows[0].text == "第一行 第二行 第三行 结尾"
    assert memory._normalize("  a \n b  ") == "a b"
    assert memory._normalize("") == ""
    assert memory._normalize(None) == ""


@pytest.mark.asyncio
async def test_remember_kind_two_values_are_both_accepted(db, monkeypatch):
    """``KINDS`` 的两值都能落库（提示词契约与落库校验共用同一份常量）。"""
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    assert memory.KINDS == ("knowledge", "interaction")
    for index, kind in enumerate(memory.KINDS):
        assert await memory.remember(db, text=f"第 {index} 条事实", kind=kind) is not None
    await db.commit()
    assert await _count(db) == len(memory.KINDS)


# ==========================================================================
# 3 · 读路径：recall
# ==========================================================================


@pytest.mark.asyncio
async def test_recall_is_newest_first_and_respects_top_k(db, monkeypatch):
    """倒序 + ``MEMORY_TOP_K`` + ``limit=`` 覆盖 + ``limit=0`` ⇒ ``[]`` + 作用域过滤。"""
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    monkeypatch.setattr(settings, "MEMORY_TOP_K", 3)

    base = datetime(2026, 9, 20, 12, 0, 0)
    for index in range(7):
        db.add(_memory_row(f"事实 {index}", created_at=base + timedelta(seconds=index)))
    # 另一个作用域的行：首版只认 global（列与复合唯一索引留给将来，读取端不会串味）
    db.add(_memory_row("会话作用域的事实", scope="chat:41", created_at=base + timedelta(seconds=99)))
    await db.commit()

    newest_first = await memory.recall(db)
    assert [row.text for row in newest_first] == ["事实 6", "事实 5", "事实 4"], "倒序 + TopK=3"

    assert [row.text for row in await memory.recall(db, limit=7)] == [
        f"事实 {index}" for index in (6, 5, 4, 3, 2, 1, 0)
    ], "limit 覆盖 MEMORY_TOP_K"
    assert [row.text for row in await memory.recall(db, limit=2)] == ["事实 6", "事实 5"]
    assert await memory.recall(db, limit=0) == [], "limit=0 ⇒ 空列表"
    assert [row.text for row in await memory.recall(db, scope="chat:41")] == ["会话作用域的事实"]


@pytest.mark.asyncio
async def test_recall_falls_back_to_default_top_k_when_unset(db, monkeypatch):
    """不传 ``limit`` 且配置键缺失 ⇒ 默认 5（``_int_setting`` 的默认值就是 5）。"""
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    monkeypatch.delattr(settings, "MEMORY_TOP_K", raising=False)

    base = datetime(2026, 9, 20, 12, 0, 0)
    for index in range(7):
        db.add(_memory_row(f"事实 {index}", created_at=base + timedelta(seconds=index)))
    await db.commit()

    assert len(await memory.recall(db)) == 5


# ==========================================================================
# 4 · 渲染：render_block
# ==========================================================================


def test_render_block_empty_blank_and_zero_budget(monkeypatch):
    """空 / ``None`` / 全空白 / 预算 <= 0 ⇒ 恒空串（调用方据此完全不拼提示词）。"""
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    monkeypatch.setattr(settings, "MEMORY_INJECT_MAX_CHARS", 800)

    assert memory.render_block(None) == ""
    assert memory.render_block([]) == ""
    assert memory.render_block(_rows("", "   ")) == "", "空正文的行直接跳过；一条都没有就不注入"

    monkeypatch.setattr(settings, "MEMORY_INJECT_MAX_CHARS", 0)
    assert memory.render_block(_rows("用户喜欢深色主题")) == "", "预算 0 = 不注入"

    monkeypatch.setattr(settings, "MEMORY_INJECT_MAX_CHARS", 5)
    assert memory.render_block(_rows("用户喜欢深色主题")) == "", "连抬头都装不下 ⇒ 不注入"


def test_render_block_shape_uses_frozen_header(monkeypatch):
    """形状：``MEMORY_HEADER`` + 换行 + 每条一行 ``"- <正文>"``（抬头是常量，不是手抄）。"""
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    monkeypatch.setattr(settings, "MEMORY_INJECT_MAX_CHARS", 800)

    block = memory.render_block(_rows("用户喜欢深色主题", "项目叫知澜"))
    assert block.startswith(memory.MEMORY_HEADER + "\n")
    assert block == f"{memory.MEMORY_HEADER}\n- 用户喜欢深色主题\n- 项目叫知澜"
    assert memory.MEMORY_BULLET == "- "
    assert memory.MEMORY_HEADER == "【跨会话记忆（系统自动记录的长期事实，仅供补充参考）】"


def test_render_block_stops_before_the_item_that_would_exceed_budget(monkeypatch):
    """预算规则（唯一口径）：**下一条会超预算就停**——不截断单条、不重排、不留半行。

    手算：抬头 H + 换行 1 + 首条 ``"- AAAAAAAAAA"``（12）= H+13。
    预算给到 H+13 时，首条**正好**装下、第二条**整条**被停掉。
    """
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    header = memory.MEMORY_HEADER
    item = "- " + "A" * 10
    budget = len(header) + 1 + len(item)
    monkeypatch.setattr(settings, "MEMORY_INJECT_MAX_CHARS", budget)

    block = memory.render_block(_rows("A" * 10, "B" * 10, "C" * 10))
    assert block == f"{header}\n{item}", "装得下的那条**整条**保留"
    assert len(block) <= budget, f"渲染全长 {len(block)} 越过了预算 {budget}"
    assert "B" not in block and "C" not in block, "超预算的那条整条丢掉，绝不截断半句"
    assert block.splitlines() == [header, item], "正好两行，没有半行残留"

    # 放宽到「两条装得下」⇒ 前两条都在、顺序 = 输入顺序（倒序由 recall 保证，渲染绝不重排），
    # 第三条仍被停掉。
    # ⚠️ 手算口径：``used`` 从 ``len(header)`` 起（**不含**抬头后那个换行），而渲染结果
    # ``= 抬头 + "\n" + 各行``——所以「刚好装下 n 条」的预算 = ``len(header) + n + n*len(item)``。
    budget2 = len(header) + 2 + 2 * len(item)
    monkeypatch.setattr(settings, "MEMORY_INJECT_MAX_CHARS", budget2)
    block2 = memory.render_block(_rows("A" * 10, "B" * 10, "C" * 10))
    assert block2 == f"{header}\n{item}\n- {'B' * 10}"
    assert len(block2) <= budget2, f"渲染全长 {len(block2)} 越过了预算 {budget2}"
    assert block2.splitlines() == [header, item, "- " + "B" * 10]


def test_render_block_skips_blank_rows_without_eating_the_budget(monkeypatch):
    """空正文的行被跳过（不占行、不占那个「行间换行」的 1 字符预算）。"""
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    monkeypatch.setattr(settings, "MEMORY_INJECT_MAX_CHARS", 800)

    block = memory.render_block(_rows("第一条", "", "   ", "第二条"))
    assert block == f"{memory.MEMORY_HEADER}\n- 第一条\n- 第二条"


# ==========================================================================
# 5 · 抽取输入：extraction_input
# ==========================================================================


def test_extraction_input_has_question_and_answer():
    """材料 = 本轮 user 提问 + assistant 正文（不喂 refs / 材料原文 / agent_trace）。"""
    material = memory.extraction_input("问题正文", "回答正文")
    assert material == "用户：问题正文\n\n助手：回答正文"
    assert "问题正文" in material and "回答正文" in material


def test_extraction_input_truncates_from_the_tail():
    """超长 ⇒ 从**尾部**截断（提问在头部，必须保住它）。"""
    material = memory.extraction_input("问" * 5000, "答" * 5000)
    assert len(material) == memory.EXTRACT_INPUT_MAX_CHARS
    assert material.startswith("用户：" + "问" * 100), "提问在头部 ⇒ 截断后仍完整可见"
    assert "助手：" not in material, "尾部（回答段）先被砍掉"
    assert material == f"用户：{'问' * 5000}\n\n助手：{'答' * 5000}"[: memory.EXTRACT_INPUT_MAX_CHARS]


# ==========================================================================
# 6 · 解析：parse_facts 三级兜底
# ==========================================================================


def test_parse_facts_level1_plain_json():
    raw = _facts_json((FACT_TEXT, FACT_KIND))
    assert memory.parse_facts(raw) == [{"text": FACT_TEXT, "kind": FACT_KIND}]


def test_parse_facts_level2_strips_json_fence():
    """模型多此一举加 ```json 围栏 ⇒ 去围栏后按 JSON 解析。"""
    raw = "```json\n" + _facts_json((FACT_TEXT, FACT_KIND)) + "\n```"
    assert memory.parse_facts(raw) == [{"text": FACT_TEXT, "kind": FACT_KIND}]

    bare = "```\n" + _facts_json((FACT_TEXT, FACT_KIND)) + "\n```"
    assert memory.parse_facts(bare) == [{"text": FACT_TEXT, "kind": FACT_KIND}], "无语言标注同样认"


def test_parse_facts_level3_locates_object_inside_prose():
    """前后有散文 ⇒ 括号定位取「第一个 ``{`` 到匹配它的 ``}``」。"""
    raw = "好的，我读完了。抽取结果如下：\n" + _facts_json((FACT_TEXT, FACT_KIND)) + "\n以上。"
    assert memory.parse_facts(raw) == [{"text": FACT_TEXT, "kind": FACT_KIND}]

    nested = '结果：{"facts": [{"text": "含 } 花括号的事实", "kind": "knowledge"}]} 完。'
    assert memory.parse_facts(nested) == [
        {"text": "含 } 花括号的事实", "kind": "knowledge"}
    ], "字符串里的花括号不该截断定位"


def test_parse_facts_garbage_and_empty_shapes_are_empty():
    """解析不了 ⇒ ``[]``；``{"facts": []}`` ⇒ ``[]``（空不是失败，分类靠调用方）。"""
    assert memory.parse_facts("这一轮没什么可记的，我就不输出 JSON 了。") == []
    assert memory.parse_facts("") == []
    assert memory.parse_facts("   ") == []
    assert memory.parse_facts('{"facts": []}') == []
    assert memory.parse_facts('{"facts": "不是列表"}') == []
    assert memory.parse_facts("[1, 2, 3]") == [], "顶层不是对象"
    assert memory.parse_facts("{不是合法 JSON") == []


def test_parse_facts_drops_illegal_and_over_long_items():
    """每条过滤：非法 kind / 空正文 / 超长正文（**丢弃不截断**）/ 非对象项。"""
    over_long = "长" * (memory.EXTRACT_MAX_CHARS + 1)
    exact = "中" * memory.EXTRACT_MAX_CHARS
    payload = {
        "facts": [
            {"text": over_long, "kind": "knowledge"},  # 超长 ⇒ 丢
            {"text": "合法的一句", "kind": "other"},  # 非法 kind ⇒ 丢
            {"text": "   ", "kind": "knowledge"},  # 空白正文 ⇒ 丢
            "不是对象",  # 类型不对 ⇒ 丢
            {"text": exact, "kind": "knowledge"},  # **边界**：恰好等于上限 ⇒ 留
            {"text": "  留下的这一句\t", "kind": "interaction"},  # 顺手规范化
        ]
    }
    assert memory.parse_facts(json.dumps(payload, ensure_ascii=False)) == [
        {"text": exact, "kind": "knowledge"},
        {"text": "留下的这一句", "kind": "interaction"},
    ]


def test_parse_facts_truncates_to_the_cap():
    """超过 ``EXTRACT_MAX_FACTS`` 条 ⇒ 只留前 N 条（保序）。"""
    facts = [{"text": f"事实{i}", "kind": "knowledge"} for i in range(memory.EXTRACT_MAX_FACTS + 3)]
    parsed = memory.parse_facts(json.dumps({"facts": facts}, ensure_ascii=False))
    assert memory.EXTRACT_MAX_FACTS == 3
    assert len(parsed) == memory.EXTRACT_MAX_FACTS
    assert [item["text"] for item in parsed] == [
        f"事实{i}" for i in range(memory.EXTRACT_MAX_FACTS)
    ]


# ==========================================================================
# 7 · 抽取任务：remember_turn / schedule_extract / drain_memory_tasks
# ==========================================================================


@pytest.mark.asyncio
async def test_schedule_extract_then_drain_stores_a_row(db, fresh_db, monkeypatch):
    """``schedule_extract`` 排后台任务 → ``drain_memory_tasks``（唯一等待点）→ 行真的落库。"""
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    memory.reset_failure_counts()
    model = _ExtractModel()
    db.add(Chat(id=7, title="source"))
    await db.flush()
    db.add(Message(id=11, chat_id=7, role="user", content="我叫小明"))
    await db.commit()

    memory.schedule_extract(
        question="我叫小明", answer="好的，我记住了。", chat_id=7, message_id=11, model=model, db=db
    )
    assert memory._PENDING, "任务已排进后台（唯一等待点就是 drain_memory_tasks）"

    await memory.drain_memory_tasks()
    assert memory._PENDING == set(), "drain 之后不许留残余任务"

    rows = await _fetch(fresh_db)  # 另一个会话读：证明不是「只在本会话可见」的假落库
    assert len(rows) == 1
    row = rows[0]
    assert row.text == FACT_TEXT
    assert row.kind == FACT_KIND
    assert row.scope == memory.SCOPE_GLOBAL
    assert row.text_hash == memory._text_hash(FACT_TEXT)
    assert row.source_chat_id == 7 and row.source_message_id == 11
    assert row.created_at is not None

    counts = memory.failure_counts()
    assert counts["stored"] == 1
    assert (counts["call_failed"], counts["parse_failed"], counts["empty"]) == (0, 0, 0)

    # 抽取调用的形状（写路径的硬口径：每轮最多 1 次非流式调用、不重试、低温、留思考余量）
    assert model.count == 1, "每轮最多 1 次、不重试"
    call = model.calls[0]
    assert call["kwargs"]["system_prompt"] == memory.EXTRACT_SYSTEM
    assert call["kwargs"]["temperature"] == memory.EXTRACT_TEMPERATURE == 0.0
    assert (
        call["kwargs"]["max_output_tokens"]
        == memory.EXTRACT_MAX_TOKENS + memory.EXTRACT_REASONING_HEADROOM
        == 1824
    )
    assert call["kwargs"]["reasoning"].mode == "off", "抽取关掉思考"
    assert memory.EXTRACT_MAX_FACTS == 3 and memory.EXTRACT_MAX_CHARS == 200


@pytest.mark.asyncio
async def test_schedule_extract_is_gated_on_switch_model_and_blank_answer(monkeypatch):
    """关态 / 没模型 / 空回答 ⇒ 直接返回，**一个任务都不排**、模型一次都不调。"""
    model = _ExtractModel()
    memory.schedule_extract(
        question="问", answer="   ", chat_id=None, message_id=None, model=model, db=None
    )
    memory.schedule_extract(
        question="问", answer="答", chat_id=None, message_id=None, model=None, db=None
    )
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    memory.schedule_extract(
        question="问", answer="", chat_id=None, message_id=None, model=model, db=None
    )
    monkeypatch.setattr(settings, "MEMORY_ENABLE", False)
    memory.schedule_extract(
        question="问", answer="答", chat_id=None, message_id=None, model=model, db=None
    )

    await memory.drain_memory_tasks()
    assert model.count == 0, "三条门（开关 / 模型 / 正文）任一不满足就零调用"
    assert memory._PENDING == set()


@pytest.mark.asyncio
async def test_remember_turn_returns_stored_count_and_dedupes(db, fresh_db, monkeypatch):
    """``remember_turn`` 直接 await：返回真正写进去的条数；同一句再抽一次算 0（去重）。"""
    memory.reset_failure_counts()
    model = _ExtractModel()

    monkeypatch.setattr(settings, "MEMORY_ENABLE", False)
    assert await memory.remember_turn("问", "答", model=model, db=db) == 0
    assert model.count == 0, "关态连模型都不许调"

    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    assert await memory.remember_turn("问", "答", model=model, db=db) == 1
    assert await memory.remember_turn("问", "答", model=model, db=db) == 0, "去重不算新写入"
    assert model.count == 2, "两轮各调一次（抽取本身照做，去重在落库那一步）"

    assert await _count(fresh_db) == 1
    assert memory.failure_counts()["stored"] == 1


@pytest.mark.asyncio
async def test_remember_turn_without_model_is_a_silent_zero(db, monkeypatch):
    """``model=None``（桌面版首次启动的常态）⇒ 0 条、零计数、零异常。"""
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    memory.reset_failure_counts()

    assert await memory.remember_turn("问", "答", model=None, db=db) == 0
    assert memory.failure_counts() == {
        "call_failed": 0,
        "parse_failed": 0,
        "empty": 0,
        "stored": 0,
        # 083-#12（2026-09-27 拍板⑩）：第五键 store_failed 只增，零态一并钉住。
        "store_failed": 0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [("raise", "call_failed"), ("prose", "parse_failed"), ("empty", "empty")],
)
async def test_remember_turn_classifies_failures_without_raising(
    db, fresh_db, monkeypatch, mode, expected
):
    """失败分级：① 调用失败 ② 解析失败 ③ 解析成功但没事实（**正常**）——三者都不抛、都不落行。"""
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    memory.reset_failure_counts()

    if mode == "raise":
        model = _ExtractModel(error=RuntimeError("模拟抽取调用挂了"))
    elif mode == "prose":
        model = _ExtractModel("这一轮没什么可记的，我就不输出 JSON 了。")
    else:
        model = _ExtractModel('{"facts": []}')

    assert await memory.remember_turn("问", "答", model=model, db=db) == 0
    assert model.count == 1

    counts = memory.failure_counts()
    assert counts[expected] == 1, counts
    assert sum(counts[key] for key in ("call_failed", "parse_failed", "empty")) == 1, (
        f"一次调用只许归一类：{counts}"
    )
    assert counts["stored"] == 0
    assert await _count(fresh_db) == 0


@pytest.mark.asyncio
async def test_failure_counts_reset_clears_all_five_keys(monkeypatch):
    """``reset_failure_counts`` 清五个键（``stored`` 也算在内）。

    🔴 2026-09-27 适配（`回归修复` #12，拍板⑩）：memory 家族只增第五键 ``store_failed``
    （落库失败），清零腿随之扩键——被钉的语义「reset 清**全部**键」一字未变。
    """
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    memory.reset_failure_counts()
    await memory.remember_turn("问", "答", model=_ExtractModel(error=RuntimeError("boom")), db=None)

    assert memory.failure_counts()["call_failed"] == 1
    memory.reset_failure_counts()
    assert memory.failure_counts() == {
        "call_failed": 0,
        "parse_failed": 0,
        "empty": 0,
        "stored": 0,
        "store_failed": 0,
    }
