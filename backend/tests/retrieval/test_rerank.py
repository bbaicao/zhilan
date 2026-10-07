"""重排开关用例（阶段 3 · 条目 3.1 · 验收 ⑨）：默认关、未配置/失败回落、成功才重排。

末段另加 **回归修复 · 重排门槛**（02 §7.6 的 ``rerank.threshold`` ←
``RAG_MIN_RERANK_SCORE``）：只在成功路径过滤、``<= 0`` 关门槛、全空是预期、
过滤不重编号；回落路径逐条不变。

**回归修复/025 · 复核修复轮**（对抗性复核实测的 4 条 + 1 条可观测性缺口）：
门槛删除条目要**留一行 warning**；``RAG_MIN_RERANK_SCORE`` 与 ``DEFAULT_MIN_RERANK_SCORE``
两个 ``0.20`` 用跨模块钉子钉住；端点/请求体形态**按 provider 判**（intl 主机、自定义主机）；
原生地址判据放宽到**前缀**；``index`` 认数字字符串/整数浮点。
"""

from __future__ import annotations

import json
import logging

import pytest

from app.core import config as config_mod
from app.core.config import Settings, settings
from app.retrieval import rerank as rerank_mod
from app.retrieval.types import Material


def _materials() -> list[Material]:
    return [
        Material(chunk_id=11, document_id=1, content="甲", filename="a.md"),
        Material(chunk_id=22, document_id=1, content="乙", filename="b.md"),
    ]


def _bind_rerank_service(monkeypatch):
    """把 MODEL_SERVICES / MODEL_ROLES 换成「已绑定 rerank 角色」的注册表形态。"""
    monkeypatch.setattr(
        settings,
        "MODEL_SERVICES",
        json.dumps(
            [
                {
                    "id": "svc-rerank",
                    "name": "重排服务",
                    "provider": "dashscope",
                    "base_url": "https://example.invalid",
                    "api_key": "sk-test",
                    "models": ["rerank-x"],
                }
            ]
        ),
    )
    monkeypatch.setattr(
        settings,
        "MODEL_ROLES",
        json.dumps({"rerank": {"service_id": "svc-rerank", "model": "rerank-x"}}),
    )


def test_key_defaults_to_false():
    """``RAG_ENABLE_RERANK`` 的**声明默认值**必须是 False（验收 ⑨ 的前半句）。"""
    assert Settings.model_fields["RAG_ENABLE_RERANK"].default is False


@pytest.mark.asyncio
async def test_disabled_keeps_fusion_order_untouched(monkeypatch):
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", False)
    materials = _materials()

    out = await rerank_mod.apply_rerank("问题", materials)

    assert out == materials  # 同一批对象、同一顺序
    assert all(item.rerank_score is None for item in out)


@pytest.mark.asyncio
async def test_enabled_without_bound_service_falls_back(monkeypatch):
    """开了但没绑 rerank 服务 → 警告 + **顺序不变**（不炸、不联网）。"""
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", True)
    monkeypatch.setattr(settings, "MODEL_SERVICES", "")
    monkeypatch.setattr(settings, "MODEL_ROLES", "")
    materials = _materials()

    out = await rerank_mod.apply_rerank("问题", materials)

    assert [item.chunk_id for item in out] == [11, 22]
    assert all(item.rerank_score is None for item in out)


@pytest.mark.asyncio
async def test_enabled_but_call_fails_falls_back(monkeypatch):
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", True)
    _bind_rerank_service(monkeypatch)

    async def _boom(config, query, documents):  # noqa: ANN001
        raise RuntimeError("HTTP 500 服务不可用")

    monkeypatch.setattr(rerank_mod, "_call_rerank", _boom)
    materials = _materials()

    out = await rerank_mod.apply_rerank("问题", materials)

    assert [item.chunk_id for item in out] == [11, 22]
    assert all(item.rerank_score is None for item in out)


@pytest.mark.asyncio
async def test_enabled_with_service_reorders_by_rerank_score(monkeypatch):
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", True)
    _bind_rerank_service(monkeypatch)

    async def _fake(config, query, documents):  # noqa: ANN001
        assert config.model == "rerank-x"
        assert documents == ["甲", "乙"]
        # 两个分都**高于**默认门槛 0.20：本条只验排序，门槛过滤另有一组用例（回归修复）
        return [0.32, 0.88]

    monkeypatch.setattr(rerank_mod, "_call_rerank", _fake)
    materials = _materials()

    out = await rerank_mod.apply_rerank("问题", materials)

    assert [item.chunk_id for item in out] == [22, 11]
    assert out[0].rerank_score == pytest.approx(0.88)
    assert out[1].rerank_score == pytest.approx(0.32)


@pytest.mark.asyncio
async def test_wrong_shaped_response_falls_back(monkeypatch):
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", True)
    _bind_rerank_service(monkeypatch)

    async def _short(config, query, documents):  # noqa: ANN001
        return [0.5]  # 条数与候选不一致

    monkeypatch.setattr(rerank_mod, "_call_rerank", _short)
    materials = _materials()

    out = await rerank_mod.apply_rerank("问题", materials)
    assert [item.chunk_id for item in out] == [11, 22]


@pytest.mark.asyncio
async def test_empty_candidates_short_circuit(monkeypatch):
    """空候选 ⇒ **连服务解析与网络调用都不做**（不是「原样返回入参」这种谁都能过的弱断言）。

    两条桩都设成必炸：``resolve_rerank_config`` 返回即 ``AssertionError``、``_call_rerank``
    被调到即 ``AssertionError``。服务**是绑好的**（``_bind_rerank_service``）+ 开关也是开的，
    所以「实现真的把空列表喂进了调用链」一定会红，而不是碰巧过。
    """
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", True)
    _bind_rerank_service(monkeypatch)

    touched: list[str] = []

    def _boom_resolve():  # noqa: ANN202
        touched.append("resolve_rerank_config")
        raise AssertionError("空候选不该走到「解析重排服务」")

    async def _boom_call(config, query, documents):  # noqa: ANN001, ANN202
        touched.append("_call_rerank")
        raise AssertionError("空候选不该走到「调用重排服务」")

    monkeypatch.setattr(rerank_mod, "resolve_rerank_config", _boom_resolve)
    monkeypatch.setattr(rerank_mod, "_call_rerank", _boom_call)

    assert await rerank_mod.apply_rerank("问题", []) == []
    assert touched == []


# —— 回归修复 · 重排门槛（02 §7.6 的 ``rerank.threshold`` ← ``RAG_MIN_RERANK_SCORE``）——

_THRESHOLD_KEY = "RAG_MIN_RERANK_SCORE"


def _materials_three() -> list[Material]:
    """三条**已编号**的材料：门槛用例既要「被删的」也要「留下的」，2 条看不出空档。"""
    return [
        Material(chunk_id=11, document_id=1, content="甲", filename="a.md", ordinal=1),
        Material(chunk_id=22, document_id=1, content="乙", filename="b.md", ordinal=2),
        Material(chunk_id=33, document_id=2, content="丙", filename="c.md", ordinal=3),
    ]


def _pin_min_score(monkeypatch, value: float) -> None:
    """钉住本轮门槛（不依赖 ``.env`` / 本机环境变量）。

    原先走 ``settings.__dict__``：那时 ``RAG_MIN_RERANK_SCORE`` 还没在 ``core/config.py`` 里
    声明，pydantic v2 对**未知字段**的 ``setattr`` 会抛 ``ValueError``。现在该键**已声明**
    （``config.py`` 中有 ``RAG_MIN_RERANK_SCORE: float = 0.20``），故改用标准写法
    ``monkeypatch.setattr``——走的是属性赋值路径，语义与运行时读法一致，事后自动还原。
    """
    monkeypatch.setattr(settings, _THRESHOLD_KEY, value)


@pytest.mark.asyncio
async def test_scores_below_threshold_are_dropped(monkeypatch):
    """``[0.9, 0.5, 0.1]`` + 门槛 0.2 ⇒ 留 2 条，``0.1`` 那条**不再作为引用**。"""
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", True)
    _bind_rerank_service(monkeypatch)
    _pin_min_score(monkeypatch, 0.20)

    async def _fake(config, query, documents):  # noqa: ANN001
        return [0.9, 0.5, 0.1]

    monkeypatch.setattr(rerank_mod, "_call_rerank", _fake)
    materials = _materials_three()

    out = await rerank_mod.apply_rerank("问题", materials)

    assert [item.chunk_id for item in out] == [11, 22]
    assert 33 not in [item.chunk_id for item in out]  # 低于门槛的那条被删掉，不是被排到末尾
    assert out[0].rerank_score == pytest.approx(0.9)
    assert out[1].rerank_score == pytest.approx(0.5)
    assert all(item.rerank_score >= 0.20 for item in out)


@pytest.mark.asyncio
async def test_score_equal_to_threshold_is_kept(monkeypatch):
    """门槛是**闭**的：``>= threshold`` 留下（「低于门槛」才删）。"""
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", True)
    _bind_rerank_service(monkeypatch)
    _pin_min_score(monkeypatch, 0.20)

    async def _fake(config, query, documents):  # noqa: ANN001
        return [0.2, 0.199]

    monkeypatch.setattr(rerank_mod, "_call_rerank", _fake)
    materials = _materials_three()[:2]

    out = await rerank_mod.apply_rerank("问题", materials)

    assert [item.chunk_id for item in out] == [11]


@pytest.mark.asyncio
async def test_all_below_threshold_yields_empty_list(monkeypatch):
    """全低于门槛 ⇒ ``[]`` 且**不抛**（该轮没有达标材料，调用方走既有「无资料」分支）。"""
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", True)
    _bind_rerank_service(monkeypatch)
    _pin_min_score(monkeypatch, 0.20)

    async def _fake(config, query, documents):  # noqa: ANN001
        return [0.05, 0.05, 0.05]

    monkeypatch.setattr(rerank_mod, "_call_rerank", _fake)
    materials = _materials_three()

    out = await rerank_mod.apply_rerank("问题", materials)  # 不抛＝本条的「不抛」断言

    assert out == []
    # 分数照样写回（证据展示仍看得见），只是**不作为引用**
    assert all(item.rerank_score == pytest.approx(0.05) for item in materials)


@pytest.mark.asyncio
async def test_zero_threshold_disables_filtering(monkeypatch):
    """门槛 0 ⇒ 不过滤（等价关掉门槛）：``0.05`` 的三条全留、顺序不变。"""
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", True)
    _bind_rerank_service(monkeypatch)
    _pin_min_score(monkeypatch, 0.0)

    async def _fake(config, query, documents):  # noqa: ANN001
        return [0.05, 0.05, 0.05]

    monkeypatch.setattr(rerank_mod, "_call_rerank", _fake)
    materials = _materials_three()

    out = await rerank_mod.apply_rerank("问题", materials)

    assert [item.chunk_id for item in out] == [11, 22, 33]
    assert all(item.rerank_score == pytest.approx(0.05) for item in out)


@pytest.mark.asyncio
async def test_negative_threshold_disables_filtering_even_for_negative_scores(monkeypatch):
    """门槛为负同样关门槛（同一个 ``> 0`` 守卫），**连负分条目都留**。

    用来钉住守卫的写法：写成 ``>= 0`` 时门槛 0 仍会「过滤」（正分看不出差别），
    只有负分条目能暴露——重排服务里的 logits 型分数确实可能是负的。
    """
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", True)
    _bind_rerank_service(monkeypatch)
    _pin_min_score(monkeypatch, -1.0)

    async def _fake(config, query, documents):  # noqa: ANN001
        return [0.05, -0.1, 0.5]

    monkeypatch.setattr(rerank_mod, "_call_rerank", _fake)
    materials = _materials_three()

    out = await rerank_mod.apply_rerank("问题", materials)

    assert [item.chunk_id for item in out] == [33, 11, 22]  # 全留、按分降序
    assert [item.rerank_score for item in out] == pytest.approx([0.5, 0.05, -0.1])


@pytest.mark.asyncio
async def test_threshold_only_applies_on_the_rerank_success_path(monkeypatch):
    """回落路径**逐条不变**：门槛再高也不许删——手里的分是 RRF 分，与重排分不同量纲。"""
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", True)
    _bind_rerank_service(monkeypatch)
    _pin_min_score(monkeypatch, 0.99)  # 高得离谱：回落路径若也过滤，会一条不剩

    async def _boom(config, query, documents):  # noqa: ANN001
        raise RuntimeError("HTTP 500 服务不可用")

    monkeypatch.setattr(rerank_mod, "_call_rerank", _boom)
    materials = _materials_three()

    out = await rerank_mod.apply_rerank("问题", materials)

    assert out == materials  # 同一批对象、同一顺序、一条不少、内容不变
    assert [item.ordinal for item in out] == [1, 2, 3]
    assert all(item.rerank_score is None for item in out)


@pytest.mark.asyncio
async def test_filtered_out_items_keep_snapshot_ordinals(monkeypatch):
    """过滤只**删条目**、不重编号：活下来的仍带原快照编号（留空档比引用错位强）。"""
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", True)
    _bind_rerank_service(monkeypatch)
    _pin_min_score(monkeypatch, 0.20)

    async def _fake(config, query, documents):  # noqa: ANN001
        return [0.9, 0.1, 0.5]  # 中间那条（ordinal=2）出局

    monkeypatch.setattr(rerank_mod, "_call_rerank", _fake)
    materials = _materials_three()

    out = await rerank_mod.apply_rerank("问题", materials)

    assert [item.chunk_id for item in out] == [11, 33]
    assert [item.ordinal for item in out] == [1, 3]  # 3 没被重编成 2


@pytest.mark.skipif(
    _THRESHOLD_KEY not in Settings.model_fields,
    reason="该键由另一条目加进 core/config.py；未落地时跳过（本条目白名单不含 config.py）",
)
def test_default_threshold_is_point_two():
    """声明默认值必须是 ``0.20``（与老基线同款；02 §7.6 语义的数值锚点）。"""
    assert Settings.model_fields[_THRESHOLD_KEY].default == 0.20


def test_default_threshold_matches_config_key_default():
    """**跨模块字面量钉子**（P3 级，本轮顺手钉住）：``config`` 的默认值 == rerank 的兜底常量。

    ``0.20`` 现在在两处各写了一份（``config.Settings.RAG_MIN_RERANK_SCORE`` 与
    ``rerank.DEFAULT_MIN_RERANK_SCORE``），改一边忘另一边**不会自动红**——这条把两边钉在一起。
    键**已声明**（回归修复 落地），故不再 skipif：键消失就该红，那本身就是漂移。
    """
    assert (
        Settings.model_fields[_THRESHOLD_KEY].default
        == rerank_mod.DEFAULT_MIN_RERANK_SCORE
    )


# —— P2-4 的可观测性一半：门槛过滤删条目必须**看得见** ——


@pytest.mark.asyncio
async def test_threshold_filter_logs_the_dropped_count(monkeypatch, caplog):
    """门槛删掉条目 ⇒ 一行 ``warning``，内容含门槛值 + 删前/删后条数 + 被删分数区间。

    为什么是必须项：实测过滤处**零日志**，而 ``session_events`` 的 ``retrieval/result`` 只留
    ``hits=0 / degraded=false / warning=null`` ⇒ 事后分不出「真零命中」与「全被门槛删」，
    而两者给下游的是完全不同的东西（后者会拿一段库外直答）。
    """
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", True)
    _bind_rerank_service(monkeypatch)
    _pin_min_score(monkeypatch, 0.20)

    async def _fake(config, query, documents):  # noqa: ANN001
        return [0.90, 0.19, 0.02]

    monkeypatch.setattr(rerank_mod, "_call_rerank", _fake)
    materials = _materials_three()

    with caplog.at_level(logging.WARNING, logger=rerank_mod.log.name):
        out = await rerank_mod.apply_rerank("问题", materials)

    assert [item.chunk_id for item in out] == [11]
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "过滤掉 2/3 条" in text  # 删前/删后条数
    assert "0.20" in text  # 门槛值
    assert "0.02" in text and "0.19" in text  # 被删条目的分数区间（上界必然低于门槛）
    assert "仅保留 1 条" in text


@pytest.mark.asyncio
async def test_threshold_filter_logs_a_full_wipe_out(monkeypatch, caplog):
    """**全被门槛删**（下游只会看到 ``hits=0``）必须留下 ``2/2`` 这行——这正是待区分的那一格。"""
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", True)
    _bind_rerank_service(monkeypatch)
    _pin_min_score(monkeypatch, 0.20)

    async def _fake(config, query, documents):  # noqa: ANN001
        return [0.05, 0.01]

    monkeypatch.setattr(rerank_mod, "_call_rerank", _fake)
    materials = _materials_three()[:2]

    with caplog.at_level(logging.WARNING, logger=rerank_mod.log.name):
        out = await rerank_mod.apply_rerank("问题", materials)

    assert out == []
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "过滤掉 2/2 条" in text
    assert "仅保留 0 条" in text
    assert "0.01" in text and "0.05" in text


@pytest.mark.asyncio
async def test_threshold_filter_is_silent_when_nothing_is_dropped(monkeypatch, caplog):
    """一条都没删 ⇒ **不打**那行日志（否则「有这行」就不再是信号）。"""
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", True)
    _bind_rerank_service(monkeypatch)
    _pin_min_score(monkeypatch, 0.20)

    async def _fake(config, query, documents):  # noqa: ANN001
        return [0.90, 0.50, 0.20]

    monkeypatch.setattr(rerank_mod, "_call_rerank", _fake)
    materials = _materials_three()

    with caplog.at_level(logging.WARNING, logger=rerank_mod.log.name):
        out = await rerank_mod.apply_rerank("问题", materials)

    assert [item.chunk_id for item in out] == [11, 22, 33]
    assert "过滤掉" not in "\n".join(record.getMessage() for record in caplog.records)


# —— 回归修复 · 调用形态（地址与请求体的形态都**按 provider 判**），对照旧基线 providers/rerank.py:26-42 ——

_NATIVE_ENDPOINT = (
    "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
)
_NATIVE_PREFIX = "/api/v1/services/rerank"


def test_rerank_endpoint_resolution_has_three_branches():
    """端点三分支：百炼截 ``/compatible-mode`` 走原生路径；已带 ``/rerank`` 原样；其余追加。"""
    assert (
        rerank_mod._rerank_endpoint(
            "https://dashscope.aliyuncs.com/compatible-mode/v1", "dashscope"
        )
        == _NATIVE_ENDPOINT
    )
    assert (
        rerank_mod._rerank_endpoint("https://x.test/v1/rerank", "openai")
        == "https://x.test/v1/rerank"
    )
    assert rerank_mod._rerank_endpoint("https://x.test/v1", "openai") == "https://x.test/v1/rerank"
    # 末尾斜杠不该拼出 ``//rerank``（旧基线先 rstrip 再判分支）
    assert (
        rerank_mod._rerank_endpoint("https://x.test/v1/", "openai") == "https://x.test/v1/rerank"
    )
    # provider 缺席（老调用点）时的兜底判据仍在：主机含 dashscope + aliyuncs.com ⇒ 原生
    assert (
        rerank_mod._rerank_endpoint("https://dashscope.aliyuncs.com/compatible-mode/v1")
        == _NATIVE_ENDPOINT
    )


def test_intl_host_does_not_fall_into_the_append_branch():
    """**P3-2 实测**：``dashscope-intl`` 国际站主机必须走原生路径，而不是「追加 ``/rerank``」。

    旧判据是主机**子串** ``"dashscope.aliyuncs.com" in base``——``dashscope-intl.aliyuncs.com``
    **不含**这个子串，于是落到追加分支，打到 compatible-mode 下并不存在的 ``/rerank`` ⇒ 404。
    按 provider 判形态后 intl 同样原生；provider 缺席时，兜底判据（同时含 ``dashscope`` 与
    ``aliyuncs.com``）也覆盖它。
    """
    intl = "https://dashscope-intl.aliyuncs.com"
    assert (
        rerank_mod._rerank_endpoint(f"{intl}/compatible-mode/v1", "dashscope")
        == intl + rerank_mod.NATIVE_RERANK_PATH
    )
    assert (
        rerank_mod._rerank_endpoint(f"{intl}/compatible-mode/v1")
        == intl + rerank_mod.NATIVE_RERANK_PATH
    )


def test_dashscope_provider_on_custom_host_keeps_native_shape():
    """``provider="dashscope"`` + **自定义主机**（专有域名 / 自建反代）⇒ 原生地址 + 嵌套体。

    形态跟**服务商**走、不跟主机走：用户把 base_url 指到自家代理时，地址与请求体都得还是百炼那一套。
    """
    base = "https://gw.corp/dashscope-proxy/v1"
    assert rerank_mod._rerank_endpoint(base, "dashscope") == base + rerank_mod.NATIVE_RERANK_PATH

    payload = rerank_mod._rerank_payload("dashscope", "rerank-x", "问题", ["甲", "乙"], 2)
    assert payload["input"] == {"query": "问题", "documents": ["甲", "乙"]}
    assert payload["parameters"] == {"top_n": 2, "return_documents": False}
    assert "documents" not in payload


def test_native_endpoint_passed_as_base_url_is_used_as_is():
    """**v2 加固**：``base_url`` 里已含原生路径段 ⇒ 原样返回，**不再拼一遍**。

    旧基线在「用户把原生终点地址当 base_url 填」时会二次拼接（找不到 ``/compatible-mode``
    可截断 ⇒ ``…/text-rerank/text-rerank/api/v1/services/…``）。口径 A 的迁移会让用户更容易
    看到并粘贴那个原生地址，所以这里显式堵掉。

    **P3-1 实测**：加固原先只堵**完整路径**，用户只填到前缀
    ``…/api/v1/services/rerank`` 时仍会被拼成
    ``…/services/rerank/api/v1/services/rerank/text-rerank/text-rerank``；本轮的判据是**前缀**。
    （上一版的「尾斜杠」断言其实只是 rstrip 后的同一个输入，等于没测到不同的形态——真正不同的
    形态是下面两条前缀用例。）
    """
    native = "https://dashscope.aliyuncs.com" + rerank_mod.NATIVE_RERANK_PATH
    assert rerank_mod._rerank_endpoint(native) == native  # 完整地址：原样保持
    assert rerank_mod._rerank_endpoint(native + "/") == native  # 尾斜杠：不得二次拼接

    prefix = "https://dashscope.aliyuncs.com" + _NATIVE_PREFIX
    assert rerank_mod._rerank_endpoint(prefix) == prefix  # 只填半截：原样，让上游去报 404
    assert rerank_mod._rerank_endpoint(prefix + "/") == prefix

    # 对照：compatible-mode 那条路仍照旧转换（加固没有改动原分支）
    assert (
        rerank_mod._rerank_endpoint("https://dashscope.aliyuncs.com/compatible-mode/v1")
        == _NATIVE_ENDPOINT
    )


def test_native_provider_uses_nested_payload():
    """``provider="dashscope"`` ⇒ 嵌套体：``input.query`` / ``input.documents`` / ``return_documents=False``。

    形态**只认 provider**：这里不传任何「原生地址」，端点字符串根本不参与判定（P3-3 的成因就是
    「端点里恰好含某段」被当成了形态依据）。
    """
    payload = rerank_mod._rerank_payload("dashscope", "rerank-x", "问题", ["甲", "乙"], 2)

    assert payload["model"] == "rerank-x"
    assert payload["input"] == {"query": "问题", "documents": ["甲", "乙"]}
    assert payload["parameters"] == {"top_n": 2, "return_documents": False}
    assert "documents" not in payload  # 顶层扁平字段一个都不能带
    assert "query" not in payload and "top_n" not in payload


@pytest.mark.parametrize(
    "provider", ["dashscope", " DashScope ", "DASHSCOPE"], ids=["plain", "padded", "upper"]
)
def test_dashscope_provider_is_normalized_before_shape_decision(provider):
    """``provider`` 先 strip + lower 再判形态：``" DashScope "`` 也是百炼（嵌套体）。"""
    payload = rerank_mod._rerank_payload(provider, "rerank-x", "问题", ["甲", "乙"], 1)
    assert payload["input"] == {"query": "问题", "documents": ["甲", "乙"]}
    assert payload["parameters"] == {"top_n": 1, "return_documents": False}


@pytest.mark.parametrize(
    "provider",
    ["openai", "jina", "cohere", "dashscope-intl", ""],
    ids=["openai", "jina", "cohere", "intl-provider-name", "empty"],
)
def test_other_providers_use_flat_payload(provider):
    """其余 provider ⇒ OpenAI 兼容扁平体（Jina / Cohere / 自建 / 未知），不带 ``input`` / ``parameters``。"""
    payload = rerank_mod._rerank_payload(provider, "rerank-x", "问题", ["甲", "乙"], 2)

    assert payload == {
        "model": "rerank-x",
        "query": "问题",
        "documents": ["甲", "乙"],
        "top_n": 2,
    }
    assert "input" not in payload and "parameters" not in payload


def test_non_dashscope_service_with_native_looking_path_gets_flat_payload():
    """**P3-3 实测**：地址里**恰好**含原生段 ≠ 百炼。

    ``https://gw.corp/api/v1/services/rerank/text-rerank`` 是自建网关：地址原样（用户就是这么
    填的），但请求体必须是**扁平体**——旧实现凭「端点字符串含该段」给它发嵌套体 ⇒ 上游 400。
    """
    base = "https://gw.corp/api/v1/services/rerank/text-rerank"
    assert rerank_mod._rerank_endpoint(base, "openai") == base

    payload = rerank_mod._rerank_payload("openai", "rerank-x", "问题", ["甲", "乙"], 2)
    assert payload == {
        "model": "rerank-x",
        "query": "问题",
        "documents": ["甲", "乙"],
        "top_n": 2,
    }
    assert "input" not in payload and "parameters" not in payload


class _FakeRerankResponse:
    """假响应：只满足 ``_call_rerank`` 的需求（``status_code`` / ``text`` / ``json()``）。"""

    status_code = 200
    text = ""

    def json(self):  # noqa: ANN201
        return {
            "output": {
                "results": [
                    {"index": 1, "relevance_score": 0.8},
                    {"index": 0, "relevance_score": 0.2},
                ]
            }
        }


@pytest.mark.asyncio
async def test_call_rerank_passes_provider_through_for_intl_host(monkeypatch, engine):
    """**P3-2 的接线钉子**（假 ``httpx.AsyncClient``，不联网）：intl 主机发**原生地址 + 嵌套体**。

    只测两个纯函数的话，「``_call_rerank`` 忘了透传 ``config.provider``」不会被发现；这条把
    「配置 → 实际发出的 URL / 请求体」整条链路钉住，正是 P3-2 实测的那个 404 场景。
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.usage import recorder

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(recorder, "SessionLocal", factory)
    seen: list[tuple[str, dict]] = []

    class _FakeClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        async def __aenter__(self):  # noqa: ANN202
            return self

        async def __aexit__(self, *exc):  # noqa: ANN002, ANN202
            return False

        async def post(self, url, headers=None, json=None):  # noqa: ANN001, ANN202
            seen.append((url, json))
            return _FakeRerankResponse()

    monkeypatch.setattr(rerank_mod.httpx, "AsyncClient", _FakeClient)
    config = config_mod.ModelConfig(
        kind="openai",
        provider="dashscope",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key="sk-test",
        model="rerank-x",
    )

    try:
        scores = await rerank_mod._call_rerank(config, "问题", ["甲", "乙"])
    finally:
        await recorder.drain_pending()

    assert scores == pytest.approx([0.2, 0.8])
    assert len(seen) == 1
    url, payload = seen[0]
    assert url == "https://dashscope-intl.aliyuncs.com" + rerank_mod.NATIVE_RERANK_PATH
    assert payload["input"] == {"query": "问题", "documents": ["甲", "乙"]}
    assert "query" not in payload and "documents" not in payload  # 嵌套体：顶层不许有扁平字段


@pytest.mark.parametrize(
    "body",
    [
        {
            "output": {
                "results": [{"index": 1, "relevance_score": 0.8}, {"index": 0, "relevance_score": 0.2}]
            }
        },
        {"results": [{"index": 1, "relevance_score": 0.8}, {"index": 0, "relevance_score": 0.2}]},
    ],
    ids=["dashscope-output-results", "openai-compat-results"],
)
def test_response_shapes_are_parsed_back_into_document_order(body):
    """两种返回形态都解析：``index`` 乱序也按 ``index`` 归位（不是按返回顺序）。"""
    assert rerank_mod._parse_rerank_scores(body, 2) == pytest.approx([0.2, 0.8])


@pytest.mark.parametrize(
    "rows",
    [
        [{"index": "1", "score": 0.8}, {"index": "0", "score": 0.2}],
        [{"index": 1.0, "score": 0.8}, {"index": 0.0, "score": 0.2}],
        [{"index": " 1 ", "score": 0.8}, {"index": "0", "score": 0.2}],
    ],
    ids=["digit-string", "integral-float", "padded-string"],
)
def test_numeric_index_forms_are_parsed_back_into_document_order(rows):
    """**P3-4 实测**：``index`` 是数字字符串 / 整数值浮点也要**按 index 归位**。

    ``[{"index": "1", ...}, {"index": "0", ...}]`` 以前会被当成「没有 index」按**返回顺序**对齐
    ⇒ 两条分数直接错位（老基线是 ``int(item["index"])``，比「只认 int」宽容）。
    """
    assert rerank_mod._parse_rerank_scores({"results": rows}, 2) == pytest.approx([0.2, 0.8])


def test_unusable_index_rejects_the_batch():
    """未声明原序保证的协议不能凭返回位置猜下标。"""
    for index in (1.9, "1.9", "x", True):
        with pytest.raises(RuntimeError):
            rerank_mod._parse_rerank_scores(
                {"results": [{"index": index, "score": 0.8}, {"score": 0.2}]}, 2
            )


def test_out_of_range_index_rejects_the_batch():
    """负数、越界和重复下标均拒绝，不给遗漏材料补零分。"""
    for index in (5, -1, 0):
        with pytest.raises(RuntimeError):
            rerank_mod._parse_rerank_scores(
                {"results": [{"index": index, "score": 0.8}, {"index": 0, "score": 0.2}]}, 2
            )


@pytest.mark.parametrize(
    "rows",
    [
        [{"score": 0.8}, {"index": 0, "score": 0.2}],
        [{"score": 0.8}, {"score": 0.2}],
        [{"index": 1.9, "score": 0.8}, {"index": 0, "score": 0.2}],
        [{"index": "x", "score": 0.8}, {"index": 0, "score": 0.2}],
        [{"index": True, "score": 0.8}, {"index": 0, "score": 0.2}],
        [{"index": 2, "score": 0.8}, {"index": 0, "score": 0.2}],
        [{"index": -1, "score": 0.8}, {"index": 0, "score": 0.2}],
        [{"index": 0, "score": 0.8}, {"index": 0, "score": 0.2}],
        [{"index": 1, "score": 0.8}],
        [{"index": 1, "score": 0.8}, {"index": 0, "score": 0.2}, {"index": 0, "score": 0.1}],
        [None, {"index": 0, "score": 0.2}],
        [{"index": 1}, {"index": 0, "score": 0.2}],
        [{"index": 1, "score": "bad"}, {"index": 0, "score": 0.2}],
        [{"index": 1, "score": None}, {"index": 0, "score": 0.2}],
        [{"index": 1, "score": True}, {"index": 0, "score": 0.2}],
        [{"index": 1, "score": float("nan")}, {"index": 0, "score": 0.2}],
        [{"index": 1, "score": float("inf")}, {"index": 0, "score": 0.2}],
    ],
    ids=[
        "missing-index", "all-missing-index", "fraction-index", "bad-index", "bool-index",
        "out-of-range", "negative-index", "duplicate-index", "short", "extra", "non-object",
        "missing-score", "bad-score", "null-score", "bool-score", "nan-score", "inf-score",
    ],
)
@pytest.mark.asyncio
async def test_invalid_protocol_keeps_all_fused_materials(monkeypatch, rows):
    """模拟回执走真实调用/解析/应用入口，失败后正文、编号与统计完整保旧。"""
    with pytest.raises(RuntimeError):
        rerank_mod._parse_rerank_scores({"results": rows}, 2)

    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", True)
    _bind_rerank_service(monkeypatch)
    _pin_min_score(monkeypatch, 0.99)
    from app.usage import instrumentation

    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def fake_post(client, url, **kwargs):
        calls.append(kwargs["json"]["input"]["documents"])
        response = _FakeRerankResponse()
        response.json = lambda: {"output": {"results": rows}}
        return response

    monkeypatch.setattr(rerank_mod.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(instrumentation, "observed_post", fake_post)
    materials = _materials_three()[:2]
    before = [vars(item).copy() for item in materials]
    stats = rerank_mod.RerankStats()
    out = await rerank_mod.apply_rerank("问题", materials, stats=stats)
    assert calls == [["甲", "乙"]]
    assert out is materials
    assert [vars(item) for item in out] == before
    assert stats.status == "failed"
    assert stats.kept is None and stats.removed is None
    assert not stats.threshold_applied and stats.rerank_output is None


def test_finite_negative_and_string_scores_are_valid():
    assert rerank_mod._parse_rerank_scores(
        {"results": [{"index": 1, "score": "-0.5"}, {"index": 0, "score": 0.0}]}, 2
    ) == [0.0, -0.5]


def test_response_without_results_raises_runtime_error():
    """没有 ``results`` ⇒ ``RuntimeError``（``apply_rerank`` 据此走回落路径，不炸穿）。"""
    with pytest.raises(RuntimeError):
        rerank_mod._parse_rerank_scores({"output": {}}, 2)
