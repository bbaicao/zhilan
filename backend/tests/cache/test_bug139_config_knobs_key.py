"""`回归修复` 的永久回归钉：**生成 / 检索期的三样旋钮必须进缓存键**。

## 缺陷是什么（缺陷清单-2 · 六A，2026-09-28）

`app/cache/entry.py::retrieval_fingerprint`（缓存键的第 ⑨ 个成分）原来只收「**怎么选块**」
那一层：`embed` / `rerank` 绑定 + 重排开关与门槛 + `graph` 六旋钮 +
`reasoning` 参数。而「**这一轮怎么答、材料面怎么拼**」由另外三样决定，它们**一个都不在键里**：

| 旋钮 | 唯一消费点 | 改了它会发生什么 |
|---|---|---|
| `CHAT_DIRECT_ANSWER_ENABLE` | `generation/answer.py:197`（系统提示三态）/ `:260`（「无资料」消息体后缀） | 同一道题换一整套系统提示与消息体 |
| `RAG_LINK_EXPAND` | `retrieval/link.py:227`（主命中要不要沿链接边多拉一跳） | **材料面本身就不同** |
| 温度 | 主回答链路（见下） | 采样 |

于是：开着缓存问一次（normal 轮入库）→ 切开关 → 同题再问 ⇒ ⑨ 一个成员都没变 ⇒ **命中**，
把**另一套配置下生成的那条答案**交出去。`chunks` 内容指纹也查不出来——块一条没变。

## 修法（本文件钉住的形态）

`retrieval_fingerprint()` 的返回体**只增**一个 `turn_knobs` 子对象
（`entry._chat_turn_knobs`），三个成员：`direct_answer_enabled` /
`link_expand_enabled` / `temperature`。

## 「温度」那一支的取证（**如实登记，不得含糊**）

实测：**主回答链路压根不发送温度** —— `answer.answer_direct` 的
`bound.generate(typed, system_prompt=…, max_output_tokens=DIRECT_MAX_TOKENS)`
没有 `temperature` 形参；装配侧 `_build_bound_model` 只带 `extra_body`
（推理参数）；`core.reasoning.request_body_for` 的模板**只有** `thinking` /
`reasoning_effort`；`core/config.py` 里也没有 temperature 旋钮
（`generation/answer.py:492` 的注释写明「`temperature` 一律原样不动」）。

⇒ 本成分**当前恒为 `None`（= 不发温度）：这是一个惰性常量，值不变 ⇒ 键不变。**
读取点 = `app.generation.answer` 模块上的 `DIRECT_TEMPERATURE`（当前**该名字不存在**）；
本文件用 monkeypatch 证明「**一旦该来源可变，键自动跟随**」，而**不是**宣称「现在有个温度旋钮」。

⚠️ `routing/llm.py:32` 的 `TEMPERATURE` / `clarify.CLARIFY_TEMPERATURE` /
`compaction.SUMMARY_TEMPERATURE` / `memory.EXTRACT_TEMPERATURE` 都是**别的链路**的
温度（意图分类 / 澄清 / 压缩 / 记忆抽取），**不进本键**（进了就是假耦合）。

## 腿构成（10 例 · 修前「6 红 / 2 绿」；`task-26` 补 9–10 腿）

| # | 用例 | 修前 | 作用 |
|---|---|---|---|
| 1 | 开关**真的**换掉系统提示（消费点取证） | 绿 | 证明「为什么必须进键」，不是假想影响 |
| 2 | 改 `CHAT_DIRECT_ANSWER_ENABLE` ⇒ **键变** | **红** | 缺陷本体 |
| 3 | 改 `RAG_LINK_EXPAND` ⇒ **键变** | **红** | 缺陷本体 |
| 4 | 温度来源变（monkeypatch `DIRECT_TEMPERATURE`）⇒ **键变** | **红** | 惰性成分的「自动跟随」 |
| 5 | 配置不变 ⇒ 键逐字相同；**换问题 ⇒ 键变** | 绿 | 负控（防「键恒等常量」的假绿） |
| 6 | ⑨ 顶层键 = 改前六键 + `turn_knobs`（只增超集）+ 旧六键取值口径逐字未变 | **红** | 键集口径 |
| 7 | 旋钮缺失 / `None` ⇒ 不抛，且归一结果与消费点同款 | **红** | 读取口径 |
| 8 | 「温度当前为惰性」的**可证伪登记**：`answer` 模块上确无 `DIRECT_TEMPERATURE` | **红** | 口径诚实性 |
| 9 | 改 `RAG_LINK_MAX` ⇒ **键变**（含 `link_budget` 4→10 的机制取证） | **红** | `task-26` 补漏 |
| 10 | **口径登记腿**：这四个旋钮不在设置页（AST 扫 `settings_routes/settings_schemas`）+ 缓存是进程内状态 | **红** | `task-26`：可证伪地钉住「防御性加固」定性 |

## 已登记口径（交付与小结同步）

- **改键 ⇒ 存量缓存条目一次性全部失效**（键是 `sha256`、没有版本位）——拍板单 F 已决：
  一次性代价可接受。本文件不改 `CACHE_SCHEMA_VERSION`（条目结构没变，变的是键）。
- 带图轮禁用缓存 / 语义缓存关时的行为**一字未动**（本笔只动 ⑨ 的成员）。
"""

from __future__ import annotations

import pytest

from app.cache import entry as entry_mod
from app.cache.entry import CacheKeyInputs
from app.core.config import settings

#: 改前 ⑨ 的**顶层键逐字清单**（本笔只增 `turn_knobs`，一个都不许少/改名）。
_OLD_FINGERPRINT_KEYS = {
    "embed",
    "rerank",
    "rerank_enabled",
    "rerank_min_score",
    "graph",
    "reasoning",
}


@pytest.fixture
def chat_binding(monkeypatch):
    """给 `_reasoning_fingerprint` 一个可读的 chat 绑定 + 清空 `CHAT_REASONING`。

    没有它，⑨ 会按设计**抛** `ValueError`（「读不到配置 ⇒ 本轮不缓存」）⇒ 本文件的
    键比较根本跑不起来。钉住而不是沿用本机 `.env`：否则读数随机器变（照 `tests/cache/test_cache.py` 的先例）。
    """
    monkeypatch.setattr(settings, "CHAT_REASONING", "")
    monkeypatch.setattr(
        settings,
        "MODEL_SERVICES",
        '[{"id":"svc_c","name":"mimo","provider":"mimo","base_url":"https://x.invalid/v1",'
        '"api_key":"sk-c","models":["mimo-v2.5"],"model_roles":{"mimo-v2.5":["chat"]},'
        '"for_roles":["chat"]}]',
    )
    monkeypatch.setattr(
        settings,
        "MODEL_ROLES",
        '{"chat": {"service_id": "svc_c", "model": "mimo-v2.5"}}',
    )
    return settings


def _key(question: str = "同一道题：向量库用什么存储？") -> str:
    """照端点口径构造一次键：⑨ 取**当场现算**的 `retrieval_fingerprint()`。"""
    return entry_mod.build_key(
        CacheKeyInputs(
            question=question,
            filters={"kb_ids": [1]},
            agent_mode="off",
            skill_ids=["builtin:knowledge"],
            model_label="probe/probe-model",
            memory_text="",
            history=[{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好。"}],
            has_images=False,
            retrieval_config=entry_mod.retrieval_fingerprint(),
        )
    )


# ==========================================================================
# 一、机制取证：开关真的会换掉「这一轮怎么答」
# ==========================================================================


def test_direct_answer_switch_really_changes_the_prompt(chat_binding, monkeypatch):
    """**消费点取证**：`CHAT_DIRECT_ANSWER_ENABLE` 真的换掉系统提示（不是假想影响）。

    这条两态都绿（它不碰缓存键）：存在的意义是让「为什么必须进键」有实证 ——
    开关一变，同一道题的系统提示就是另一段冻结文案。
    """
    from app.generation import answer as answer_mod

    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", True)
    enabled_prompt = answer_mod.system_prompt(has_material=True)
    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", False)
    legacy_prompt = answer_mod.system_prompt(has_material=True)

    assert enabled_prompt != legacy_prompt, (
        "开关必须真的换掉系统提示 —— 否则「进键」这条修法就失去理由（消费点取证）"
    )
    assert legacy_prompt == answer_mod.LEGACY_SYSTEM_PROMPT


# ==========================================================================
# 二、三条缺陷本体：三样旋钮各自都要换键
# ==========================================================================


def test_key_changes_when_direct_answer_switch_flips(chat_binding, monkeypatch):
    """改 `CHAT_DIRECT_ANSWER_ENABLE` ⇒ **键必须变**（修复前必红）。"""
    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", True)
    before = _key()
    fingerprint_before = entry_mod.retrieval_fingerprint()

    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", False)
    after = _key()
    fingerprint_after = entry_mod.retrieval_fingerprint()

    assert fingerprint_after != fingerprint_before, "⑨ 指纹没跟上这个开关"
    assert after != before, (
        "切了直答开关仍然命中旧键 ⇒ 会把「另一套系统提示下生成的答案」交出去（回归修复）"
    )


def test_key_changes_when_link_expand_switch_flips(chat_binding, monkeypatch):
    """改 `RAG_LINK_EXPAND` ⇒ **键必须变**（修复前必红）。"""
    monkeypatch.setattr(settings, "RAG_LINK_EXPAND", True)
    before = _key()
    fingerprint_before = entry_mod.retrieval_fingerprint()

    monkeypatch.setattr(settings, "RAG_LINK_EXPAND", False)
    after = _key()
    fingerprint_after = entry_mod.retrieval_fingerprint()

    assert fingerprint_after != fingerprint_before, "⑨ 指纹没跟上这个开关"
    assert after != before, (
        "切了关联扩展仍然命中旧键 ⇒ 材料面已经不同，答案却还是旧那一轮（回归修复）"
    )


def test_key_changes_when_temperature_source_changes(chat_binding, monkeypatch):
    """温度**来源**一变 ⇒ 键必须变（本笔的「自动跟随」判据；修复前必红）。

    ⚠️ 当前 `DIRECT_TEMPERATURE` **不存在**（主链路不发温度，见模块 docstring）⇒
    这条腿用 `raising=False` 现落一个常量，证明「一旦该来源可变，键自动跟随」；
    **不是**宣称仓内已有温度旋钮。
    """
    import app.generation.answer as answer_mod

    monkeypatch.delattr(answer_mod, "DIRECT_TEMPERATURE", raising=False)
    before = _key()
    assert entry_mod.retrieval_fingerprint()["turn_knobs"]["temperature"] is None

    monkeypatch.setattr(answer_mod, "DIRECT_TEMPERATURE", 0.7, raising=False)
    after = _key()
    assert entry_mod.retrieval_fingerprint()["turn_knobs"]["temperature"] == 0.7
    assert after != before, "温度来源变了键却没变 ⇒ 该成分为死键（本笔的惰性口径就站不住）"

    monkeypatch.delattr(answer_mod, "DIRECT_TEMPERATURE", raising=False)
    assert _key() == before, "撤销该来源之后键必须逐字回到基线"


# ==========================================================================
# 三、负控：键不是「恒等常量」
# ==========================================================================


def test_identical_config_yields_identical_key(chat_binding):
    """**负控**：配置不变 ⇒ 键逐字相同；**换问题 ⇒ 键变**（防「键恒等于常量」的假绿）。"""
    first = _key()
    assert _key() == first
    assert _key() == first, "三次构建必须稳定（幂等）"
    assert _key(question="另一道完全不同的问题。") != first, (
        "换问题必须换键 —— 否则上面那些「开关变 ⇒ 键变」的断言在一个恒等键上也能绿"
    )


# ==========================================================================
# 四、键集口径：只增超集
# ==========================================================================


def test_fingerprint_key_set_is_superset_of_the_old_six(chat_binding):
    """保留⑨六键与 turn_knobs；U6 增补实际 chat/rerank 接口身份。"""
    fingerprint = entry_mod.retrieval_fingerprint()

    assert set(fingerprint) == _OLD_FINGERPRINT_KEYS | {"turn_knobs", "chat", "rerank_resolved"}, (
        f"顶层键集与⑨及U6登记不符：实际={sorted(fingerprint)}"
    )
    assert set(fingerprint["turn_knobs"]) == {
        "direct_answer_enabled",
        "link_expand_enabled",
        "link_max",
        "temperature",
    }
    # 旧六键的取值口径逐字未变（不因本笔的改动而漂移）
    assert fingerprint["rerank_enabled"] is bool(getattr(settings, "RAG_ENABLE_RERANK", False))
    assert fingerprint["rerank_min_score"] == str(getattr(settings, "RAG_MIN_RERANK_SCORE", "") or "")
    assert set(fingerprint["graph"]) == {
        "enabled",
        "top_k",
        "score_factor",
        "hop_decay",
        "weak_alias_weight",
        "max_terms",
    }
    assert isinstance(fingerprint["reasoning"], dict)


# ==========================================================================
# 五、读取口径与诚实性登记
# ==========================================================================


def test_knob_reads_fall_back_like_the_consumers(monkeypatch):
    """旋钮**缺失 / `None`** ⇒ 不抛，且归一结果与**消费点**同款。

    - `CHAT_DIRECT_ANSWER_ENABLE = None` ⇒ `False`（消费点判据是
      `not settings.CHAT_DIRECT_ANSWER_ENABLE` ⇒ 这正是回退 legacy 那一支）；
    - `RAG_LINK_EXPAND` **属性缺失** ⇒ `False`（`link.py:227` 的
      `getattr(settings, "RAG_LINK_EXPAND", False)` **就是这个默认值** —— 键里记的
      必须与消费点实际看到的值同源）。
    """

    class _Stub:  # 故意只给一个键：另一个走 getattr 默认值
        CHAT_DIRECT_ANSWER_ENABLE = None
        RAG_LINK_EXPAND = None

    monkeypatch.setattr(entry_mod, "settings", _Stub())
    knobs = entry_mod._chat_turn_knobs()
    assert knobs["direct_answer_enabled"] is False
    assert knobs["link_expand_enabled"] is False, "None/缺失 ⇒ 与 link.py:227 的默认值同款（False）"
    assert knobs["temperature"] is None

    class _StubMissing:  # 连属性都没有（真·键缺失）
        pass

    monkeypatch.setattr(entry_mod, "settings", _StubMissing())
    knobs_missing = entry_mod._chat_turn_knobs()
    assert knobs_missing["direct_answer_enabled"] is True, "键缺失 ⇒ 默认 True（与 config 声明一致）"
    assert knobs_missing["link_expand_enabled"] is False, "键缺失 ⇒ 消费点默认 False"


def test_temperature_component_is_inert_today(chat_binding, monkeypatch):
    """**可证伪的惰性登记**：主链路不发温度 ⇒ `temperature` 恒为 `None`。

    `app.generation.answer` 上**不该**存在 `DIRECT_TEMPERATURE`。哪天有人真的给
    主链路接上温度并落到那个名字，这条腿会红 —— 那是**提醒**（请同步更新
    `_chat_turn_knobs` 的 docstring 与本表），不是回归。
    另：四个**别的链路**的温度常量不许混进本键（混进来就是假耦合）。
    """
    import app.generation.answer as answer_mod

    assert not hasattr(answer_mod, "DIRECT_TEMPERATURE"), (
        "「主链路不发温度」这条实测登记已过期 ⇒ 请把 DIRECT_TEMPERATURE 的读取口径与 docstring 一起更新"
    )
    knobs = entry_mod.retrieval_fingerprint()["turn_knobs"]
    assert knobs["temperature"] is None

    from app.generation import clarify as clarify_mod
    from app.routing import llm as llm_mod
    from app.session import compaction as compaction_mod
    from app.session import memory as memory_mod

    assert set(knobs) == {"direct_answer_enabled", "link_expand_enabled", "link_max", "temperature"}
    # **假耦合负控**：把另外四条链路的温度全改掉 ⇒ 本键必须**逐字不动**
    baseline = _key()
    monkeypatch.setattr(clarify_mod, "CLARIFY_TEMPERATURE", 0.99)
    monkeypatch.setattr(llm_mod, "TEMPERATURE", 0.99)
    monkeypatch.setattr(compaction_mod, "SUMMARY_TEMPERATURE", 0.99)
    monkeypatch.setattr(memory_mod, "EXTRACT_TEMPERATURE", 0.99)
    assert _key() == baseline, (
        "别的链路（分类/澄清/压缩/记忆抽取）的温度不该影响回答缓存键 —— 那叫假耦合"
    )

def test_key_changes_when_link_max_changes(chat_binding, monkeypatch):
    """**`task-26` 补漏**：改 `RAG_LINK_MAX` ⇒ **键必须变**。

    机制取证同条给出：`RAG_LINK_MAX` 真的决定「并入几条扩展片段」
    （`retrieval/link.py:56` 的 `link_budget`）——`main_count=2` 时名额 `min(RAG_LINK_MAX, 12-2)`
    ⇒ 4 变 10。补漏前把 4→99，`retrieval_fingerprint()` 逐字不变（审计探针原话），
    同一道题就会命中「按另一套名额拼出来的材料」那一轮。
    """
    from app.retrieval.link import link_budget

    monkeypatch.setattr(settings, "RAG_LINK_MAX", 4)
    assert link_budget(2) == 4, "前提：名额旋钮真的决定并入条数（main_count=2 时 = min(RAG_LINK_MAX, 10)）"
    before = _key()
    fingerprint_before = entry_mod.retrieval_fingerprint()

    monkeypatch.setattr(settings, "RAG_LINK_MAX", 99)
    assert link_budget(2) == 10, "前提：99 时被快照上限截到 10（名额确实变了）"
    after = _key()
    fingerprint_after = entry_mod.retrieval_fingerprint()

    assert fingerprint_after != fingerprint_before, "⑨ 指纹没跟上名额旋钮"
    assert after != before, (
        "改了并入条数仍然命中旧键 ⇒ 材料面已经不同，答案却还是旧那一轮（task-26）"
    )


def test_settings_api_cannot_toggle_these_knobs(chat_binding):
    """**口径登记腿（可证伪）**：这四个旋钮**不在设置页**，缓存是**进程内状态** ⇒ 生产不可达。

    这是 `task-26`「本子对象 = 防御性加固，不是修掉一条生产可达的错命中」这条口径的判据：
    `CHAT_DIRECT_ANSWER_ENABLE` / `RAG_LINK_EXPAND` / `RAG_LINK_MAX` 在设置页源码里**一个字符串都不许出现**；
    而 `cache/store.py::_STORE` 是进程内 `OrderedDict`（`reset_cache` 清空即可）⇒「改 .env + 重启」路径下
    缓存本来就空了，不存在跨配置的旧条目。

    ⚠️ **这条红了不是回归，是提醒**：有人把旋钮接进设置页 ⇒ 生产可达性变了 ⇒
    `_chat_turn_knobs` 的口径更正段与交付口径必须重评。
    """
    import ast as _ast
    from pathlib import Path as _Path

    from app.cache import store as _store

    watched = {"CHAT_DIRECT_ANSWER_ENABLE", "RAG_LINK_EXPAND", "RAG_LINK_MAX"}
    api_dir = _Path(__file__).resolve().parents[2] / "app" / "api"
    hits: dict[str, list[str]] = {}
    for filename in ("settings_routes.py", "settings_schemas.py"):
        source = (api_dir / filename).read_text(encoding="utf-8")
        found = {
            node.value
            for node in _ast.walk(_ast.parse(source))
            if isinstance(node, _ast.Constant) and isinstance(node.value, str) and node.value in watched
        }
        if found:
            hits[filename] = sorted(found)
    assert not hits, (
        f"设置页出现了这些旋钮键 ⇒ 「生产进程内不可达」这条口径必须重评：{hits}"
    )

    assert isinstance(_store._STORE, dict), "缓存必须是进程内 dict（不是持久化后端）"
    _store.reset_cache()
    assert len(_store._STORE) == 0, "reset 后必须是空的 ⇒ 进程内状态、重启即空"
