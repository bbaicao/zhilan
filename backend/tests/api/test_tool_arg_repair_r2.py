"""专项 · 工具升级-2（R2）· 阶段一 · **参数纠错**的判据钉子（R2-01 件级 / R2-02 接线级）。

判据（开工包 §3）：

- **A1** 坏参数 ⇒ 观察文本含「工具名 / 字段名 / 约束 / 收到值」四件，且 schema 摘要来自
  ``parameters.model_json_schema()``（**换模型即变**）；
- **A2** ``filters`` 冲突 ⇒ 仍是**可读中文** ``ok=False``，**不得**被吞成「不限」（合成变异对照）；
- **A3** 好参数路径的观察文本**逐字未变**（字节级）。

## ⚠️ A3 的载体是**本笔新造**的（开工包 D-回归检查）

``knowledge_search`` 的**好参数**观察文本全仓**没有逐字断言**（既有的 5 处全是子串断言，
例 ``tests/test_agent_registry.py:162`` 的 ``assert "query" in obs.text``）⇒ 若不在本笔新造逐字快照，
A3 就是**假绿**。本文件里的 ``_A3_KNOWLEDGE_SEARCH_OBS_TEXT`` 由
``证据/r2_a3_capture.py`` 在**动代码之前**采集（日志 ``R2-02-A3改前字面量.log``），
下方按采集结果写成**逐字**常量（**不是**用被测函数现算）。

## ⚠️ A1 的变异陷阱（终检实测，勿踩）

pydantic v2 **缓存 core schema**：原地改 ``KnowledgeSearchArgs.model_fields['query'].description``
之后 ``model_json_schema()`` **不变** ⇒ 这么写的变异是**惰性变异（假绿）**。本文件的 schema
判据用的是**两个不同的模型类**，两条路都绕开缓存。
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, Field

from app.agent.tools import _errors, knowledge_search, read_document
from app.agent.tools._errors import arg_error, render_arg_error, render_schema_summary
from app.retrieval.types import Material

# --------------------------------------------------------------------------
# A1 · 件级：四件齐备 + 无换行污染
# --------------------------------------------------------------------------


class _ProbeArgs(BaseModel):
    """探针模型：字段名带哨兵前缀，用来证明摘要**来自入参模型**而不是硬编码。"""

    zzz_probe_field: int = Field(description="探针字段")
    zzz_optional_field: str | None = Field(default=None, description="探针可选字段")


#: 四个真实坏参数分支（**与接线处逐个对应**：R2-01 只渲染，R2-02 才接线）。
#: ``marker`` = 「收到的值」在文本里的可判形态。
A1_SITES = [
    {
        "tool": knowledge_search.TOOL_NAME,
        "field": "query",
        "constraint": "非空字符串",
        "received": "",
        "marker": "（空字符串）",
        "parameters": knowledge_search.KnowledgeSearchArgs,
    },
    {
        "tool": read_document.TOOL_NAME,
        "field": "start_chunk",
        "constraint": "不小于 1 的整数",
        "received": 0,
        "marker": "「0」",
        "parameters": read_document.ReadDocumentArgs,
    },
    {
        "tool": read_document.TOOL_NAME,
        "field": "max_chunks",
        "constraint": "不小于 1 的整数",
        "received": 0,
        "marker": "「0」",
        "parameters": read_document.ReadDocumentArgs,
    },
    {
        "tool": read_document.TOOL_NAME,
        "field": "max_chunks",
        "constraint": f"不大于 {read_document.MAX_CHUNKS} 的整数",
        "received": read_document.MAX_CHUNKS + 1,
        "marker": f"「{read_document.MAX_CHUNKS + 1}」",
        "parameters": read_document.ReadDocumentArgs,
    },
]


@pytest.mark.parametrize("site", A1_SITES, ids=lambda s: f"{s['tool']}-{s['field']}-{s['constraint']}")
def test_a1_all_four_pieces_are_present(site):
    """A1①：四件齐备（工具名 / 字段名 / 约束 / 收到的值）+ 中文断句 + **无换行污染**。"""
    text = render_arg_error(
        tool=site["tool"],
        field=site["field"],
        constraint=site["constraint"],
        received=site["received"],
        parameters=site["parameters"],
    )
    assert site["tool"] in text, text
    assert site["field"] in text, text
    assert site["constraint"] in text, text
    assert site["marker"] in text, text
    # 断句稳：固定前缀 + 无换行（便于日志 grep）
    assert text.startswith(f"参数错误（工具 {site['tool']}）"), text
    assert "\n" not in text, text
    assert "\r" not in text, text


def test_a1_schema_summary_is_computed_from_the_model():
    """A1②：schema 摘要**现算** —— 换一个模型就换一份摘要（硬编码来源必红）。

    ⚠️ 判据用**两个不同的模型类**，不用「改字段 description」那种写法 ——
    pydantic v2 缓存 core schema，后者是惰性变异。
    """
    first = render_schema_summary(knowledge_search.KnowledgeSearchArgs)
    second = render_schema_summary(_ProbeArgs)
    assert first != second, "两个不同模型的摘要是同一个 ⇒ schema 来源是硬编码的"
    assert first.startswith(_errors.SCHEMA_SUMMARY_PREFIX)
    # 入参模型的字段真的进了摘要（必填用 `*` 标出）
    assert "query*:string" in first, first
    assert "limit:integer" in first, first
    assert "filters:object|null" in first, first
    assert "zzz_probe_field*:integer" in second, second
    assert "zzz_optional_field:string|null" in second, second
    # 反向：探针字段**不得**出现在另一个模型的摘要里（否则等于没按入参算）
    assert "zzz_probe_field" not in first
    assert "limit" not in second


def test_a1_arg_error_is_a_failed_observation():
    """A1③：包装件统一 ``ok=False``（坏参数一律失败观察，I5）。"""
    obs = arg_error(
        tool=read_document.TOOL_NAME,
        field="start_chunk",
        constraint="不小于 1 的整数",
        received=0,
        parameters=read_document.ReadDocumentArgs,
    )
    assert obs.ok is False
    assert not (obs.materials or [])
    assert obs.text == render_arg_error(
        tool=read_document.TOOL_NAME,
        field="start_chunk",
        constraint="不小于 1 的整数",
        received=0,
        parameters=read_document.ReadDocumentArgs,
    )


def test_a1_template_has_no_newline_by_construction():
    """A1④：模板本体就不含换行（防止日后有人往模板里塞多行措辞）。"""
    assert "\n" not in _errors.ARG_ERROR_TEMPLATE
    assert "\r" not in _errors.ARG_ERROR_TEMPLATE


# --------------------------------------------------------------------------
# R2-02 · 接线级：四个真实分支真的走了同一份模板
# --------------------------------------------------------------------------


def test_a1_the_two_tools_wire_all_four_branches_to_the_template():
    """A1（接线级）：四个坏参数分支的**实际观察文本**都带模板前缀 + 四件齐备。

    ``db=None`` 即可 —— 四处校验都在 ``db is None`` 之前（两处）或之前（read_document 三处），
    不会走到需要真会话的分支。
    """
    cases = [
        (
            knowledge_search.build_spec().handler,
            knowledge_search.KnowledgeSearchArgs(query="   "),
            knowledge_search.TOOL_NAME,
            "query",
        ),
        (
            read_document.build_spec().handler,
            read_document.ReadDocumentArgs(document_id=1, start_chunk=0),
            read_document.TOOL_NAME,
            "start_chunk",
        ),
        (
            read_document.build_spec().handler,
            read_document.ReadDocumentArgs(document_id=1, max_chunks=0),
            read_document.TOOL_NAME,
            "max_chunks",
        ),
        (
            read_document.build_spec().handler,
            read_document.ReadDocumentArgs(document_id=1, max_chunks=read_document.MAX_CHUNKS + 1),
            read_document.TOOL_NAME,
            "max_chunks",
        ),
    ]
    for handler, args, tool, field in cases:
        obs = asyncio.run(handler(args))
        assert obs.ok is False, (tool, field)
        assert obs.text.startswith(f"参数错误（工具 {tool}）"), obs.text
        assert field in obs.text, obs.text
        assert "需要满足" in obs.text, obs.text
        assert "收到" in obs.text, obs.text
        assert _errors.SCHEMA_SUMMARY_PREFIX in obs.text, obs.text


def test_a1_is_the_only_implementation_in_the_two_tools():
    """A1⑤（源码级）：两个工具里**不再**各自硬写参数错误文案。"""
    from pathlib import Path

    for module in (knowledge_search, read_document):
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        assert "arg_error(" in source, f"{module.__name__} 没有走 _errors 的唯一实现"
        assert "缺少 query 参数" not in source, f"{module.__name__} 还留着旧硬编码文案"
        assert "读取文档未执行：start_chunk" not in source
        assert "读取文档未执行：max_chunks" not in source


# --------------------------------------------------------------------------
# A2 · filters 冲突仍是可读中文 ok=False，**不得**被吞成「不限」
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2_filters_conflict_stays_a_readable_failure():
    """A2：宿主范围 ``kb_ids=[1]`` × 请求侧 ``kb_ids=[2]`` ⇒ 冲突 ⇒ **调用检索之前**拒绝。

    ⚠️ 变异落点（合成）：``app/retrieval/types.py::_intersect_scope_dimension`` 的
    ``if not out: raise ValueError`` —— 改成吞成空集之后，空列表会被下游读成「这一侧不限」
    （该函数 docstring 自己写着这个坑），于是范围被静默放宽、``retrieve`` 照跑 ⇒ 本用例必红。
    """
    calls: list[dict] = []

    async def _retrieve(db, question, filters, *, stats=None):  # noqa: ANN001
        calls.append({"question": question, "filters": filters})
        return [], "", None

    spec = knowledge_search.build_spec(db=object(), filters={"kb_ids": [1]}, retrieve=_retrieve)
    obs = await spec.handler(
        knowledge_search.KnowledgeSearchArgs(query="噪声治理", filters={"kb_ids": [2]})
    )
    assert obs.ok is False
    assert not (obs.materials or [])
    assert "知识库检索未执行" in obs.text
    assert "冲突" in obs.text
    assert "没有交集" in obs.text
    assert calls == [], "范围冲突必须在**调用检索之前**拒绝；吞成「不限」会把它变成一次正常检索"


@pytest.mark.asyncio
async def test_a2_read_document_filters_conflict_stays_readable():
    """A2（read_document 腿）：同一处求交 ⇒ 同一份中文失败观察，且**零材料**。"""
    spec = read_document.build_spec(db=object(), filters={"kb_ids": [1]})
    obs = await spec.handler(
        read_document.ReadDocumentArgs(document_id=1, filters={"kb_ids": [2]})
    )
    assert obs.ok is False
    assert not (obs.materials or [])
    assert "读取文档未执行" in obs.text
    assert "冲突" in obs.text


# --------------------------------------------------------------------------
# A3 · 好参数路径的观察文本**逐字未变**（字节级）
# --------------------------------------------------------------------------

#: A3 的**改前**基线字面量。由 ``证据/r2_a3_capture.py`` 在**动代码之前**采集
#: （日志 ``证据/R2-02-A3改前字面量.log``），下方按采集结果写成逐字常量 ——
#: **不是**用被测函数现算，否则是自证式假绿（D-回归检查）。
_A3_KNOWLEDGE_SEARCH_OBS_TEXT = "[1] a.md\n" + "甲" * 40 + "\n\n[2] b.md\n" + "乙" * 300 + "…"

#: 采集时用的同一份材料（与 ``证据/r2_a3_capture.py`` 的 ``MATERIALS`` 逐字一致）
_A3_MATERIALS = [
    Material(chunk_id=11, document_id=7, content="甲" * 40, filename="a.md", block_ordinal=0),
    Material(chunk_id=12, document_id=7, content="乙" * 400, filename="b.md", block_ordinal=1),
]


@pytest.mark.asyncio
async def test_a3_knowledge_search_good_params_observation_is_bytewise_unchanged():
    """A3（knowledge_search）：好参数路径的 ``obs.text`` 与改前**逐字相同**。"""
    async def _retrieve(db, question, filters, *, stats=None):  # noqa: ANN001
        return list(_A3_MATERIALS), "（假渲染）", None

    spec = knowledge_search.build_spec(db=object(), filters=None, retrieve=_retrieve)
    obs = await spec.handler(knowledge_search.KnowledgeSearchArgs(query="噪声治理"))
    assert obs.ok is True
    assert obs.text == _A3_KNOWLEDGE_SEARCH_OBS_TEXT
    assert len(_A3_KNOWLEDGE_SEARCH_OBS_TEXT) == 361, "基线字面量本身必须与采集结果等长"
    assert len(obs.materials or []) == 2


def test_a3_read_document_good_params_observation_is_bytewise_unchanged():
    """A3（read_document 腿）：冻结模板外多一句即红。

    端到端载体 = 既有的 ``tests/api/test_document_tools_t2.py:301``（逐字等值，本笔不改它）；
    这里补一条**函数级**同字面量断言，让 A3 的两条腿都在本文件里可见。
    """
    frozen = "块号区间（第 1–2 块 / 共 2 块）\n正文A\n\n正文B"
    assert (
        read_document.render_observation(
            start=1, end=2, total=2, body="正文A\n\n正文B", remaining=0
        )
        == frozen
    )
