"""`回归修复` 判据载体：**双真相源收敛为一份**（两处）。

## 本件覆盖的判据

| 判据 | 腿 | 用例 |
|---|---|---|
| **①-a** | 两侧 `_args_digest` 对**同一含凭证输入**逐字相等 | `test_a_both_digests_agree_on_token_bearing_input` |
| **①-b** | 两侧**各自**遮蔽 `confirm_token`（分别调、各自断言无原文） | `test_b_both_digests_mask_the_confirm_token` |
| **①-c** | 非凭证参数的**既有语义不破**（只增不改，负控） | `test_c_plain_args_semantics_unchanged` |
| **①-d** | 收敛是**真委托**、不是第二份拷贝（防「改回去还能过」） | `test_d_runner_delegates_to_audit_impl` |
| **①-e** | 非字符串值渲染成 **JSON**（非 Python repr）且 `json.loads` 解得回 | `test_f_non_string_values_render_as_json_not_python_repr` |
| **②** | `cache/entry` 的 `graph.enabled` **走唯一读取点**、非二次直读 | `test_e_cache_fingerprint_uses_graph_enabled` |

## 🔴 ①-e 的由来（本轮真回归，务必读）

收敛**方向**（以 `audit` 为唯一实现）是对的，但该实现当时对**非字符串值**用 `str(value)` ⇒
嵌套 dict 输出 Python repr `"{'kb_ids': [9]}"` ⇒ `json.loads` 抛 `JSONDecodeError`。
而 `tests/api/test_chat_agent.py:778` 走 `json.loads`、其 `:777` 注释逐字把「非字符串值 JSON 化」当契约。
**两条既有判据在此点上互相矛盾**（`:778` 要 JSON / `test_write_audit_t3_5.py:642` 要 `"False"`）。
定档 = **行为契约 > 实现形状快照** ⇒ 保留 JSON 渲染；`test_write_audit_t3_5.py:642` 的形状快照随之改 `"false"`
（该改动已在原位逐字登记理由）。
⇒ **教训**：收敛「同一份实现」时，只比对**两侧输出是否相等**是不够的——
两侧可能**一起**错。必须额外钉住**对外契约**（本条 ①-e 就是那个钉子）。

## 🔴 为什么 ①-a **必须用含** `confirm_token` **的输入**（本件最关键的一条）

收敛前实测（2026-09-26，本机 `python -c` 同时打两份）：

```
{'path':'a.txt','confirm_token':'S','create':False}
  => runner {'path':'a.txt','confirm_token':'S','create':'false'}
  => audit  {'path':'a.txt','confirm_token':'***','create':'False'}
```

⇒ 两份对**普通参数**（`{'query':'x','limit':5}` / `None` / `{}` / `{'k':[1,2]}`）**本来就逐字相同**；
只在 **①含凭证键**（遮蔽与否）、**②`bool`/`None` 的渲染**上分岔。
⇒ 若 ①-a 只喂普通参数，它会**恒绿** —— 两份分岔也照样绿，即 **`回归修复`「判据面空转」**那一族。
故 ①-a 的入参**必须**带 `confirm_token`，并且**同时**断言：
（i）两侧**都不含**原文；（ii）两侧**逐字相等**。两条缺一，判据就是瞎的。

## 🔴 为什么还要 ①-d（防「两份拷贝」复活）

只断「输出相等」**不能**区分「真收敛成一份」与「把 audit 那版抄进 runner」——
后者今天相等、明天改一处就再分岔（**这正是 092 的成因本身**）。

⚠️ **口径更正（实测）**：本条初稿写的是 `runner._args_digest is audit._args_digest`（函数对象同一）——**那是错的**。
实测 `identity: False`：收敛后 `runner._args_digest` 是一个**保留签名的薄包装**
（`__code__.co_filename` 仍指向 runner.py），`is` 会把**正确实现判红**。
正确判据 = 断「**没有第二份实现**」：包装字节码里**不得**含 `json.dumps`/`model_dump`/`ARGS_CLIP`
这些本地摘要动作，且**必须**出现对 audit 那份的调用。

## 🔴 为什么还要 ①-c（负控）

没有它，把 `_args_digest` 改成「一律遮蔽 / 一律返回空」也能过 ①-a 与 ①-b ——
而那会**毁掉整个审计面**（`meta["args"]` 是排障的唯一参数载体）。
（同族负控见 `test_write_audit_t3_5.py::test_wpf_non_sensitive_keys_are_verbatim_unchanged`。）

## ② 的口径

`GRAPHRAG_ENABLE` 的**唯一**读取点是 `app.retrieval.graph.enabled()`（该函数 docstring 逐字声明）。
`cache/entry.py` 此前写 `bool(knob("GRAPHRAG_ENABLE", False))` = **第二次直读同一个键**。
收敛后改为**委托** `graph.enabled()` ⇒ 判据断「**调用了** `enabled`」而不是「值恰好对」
（值恰好对是**弱判据**：直读也能对，抓不住二次直读）。

## 载体落点与依赖（如实登记）

`backend/tests/agent/` **没有 `conftest.py`** ⇒ 夹具自带；
本件 ①-a/①-b/①-c/①-d 是**纯函数 / 字节码**腿（不碰 db、不碰事件循环）；
② 用 `monkeypatch` 在**函数级**打桩（不改真实配置），故无需 db 夹具。
"""

from __future__ import annotations

import dis
import io

from app.agent import audit as agent_audit
from app.agent import runner as agent_runner
from app.cache import entry as entry_mod
from app.core.config import settings
from app.retrieval import graph as retrieval_graph

#: 凭证哨兵：**逐字冻结**，判据直接断言它不出现
SECRET = "BUG092-SECRET-SENTINEL-DO-NOT-LOG"


def test_a_both_digests_agree_on_token_bearing_input():
    """**①-a**：两侧对**同一含凭证输入**产出**逐字相同**的结果，且**都不含**原文。

    ⚠️ 入参**必须**带 `confirm_token`（见模块 docstring「为什么必须」）——
    只喂普通参数的话，收敛前两份实现了本来就相同，本条会**恒绿**（判据面空转）。
    """
    payload = {"path": "a.txt", "content": "x", "confirm_token": SECRET, "create": False}
    got_runner = agent_runner._args_digest(dict(payload))
    got_audit = agent_audit._args_digest(dict(payload))

    # （i）两侧都不含凭证原文
    assert SECRET not in str(got_runner), f"runner 侧漏出凭证原文：{got_runner}"
    assert SECRET not in str(got_audit), f"audit 侧漏出凭证原文：{got_audit}"
    # （ii）两侧逐字相等 —— **真等价**，不是各自自洽
    assert got_runner == got_audit, (
        f"两侧 digest 不等价（092 未收敛）：runner={got_runner} audit={got_audit}"
    )


def test_b_both_digests_mask_the_confirm_token():
    """**①-b**：**分别**调两侧，各自断言 `confirm_token` 被遮蔽成 `***`。

    与 ①-a 的区别：①-a 断「两侧相等」，本条断「**各自都**遮蔽」——
    两条都断才能排除「两侧一起错」。
    """
    for name, fn in (("runner", agent_runner._args_digest), ("audit", agent_audit._args_digest)):
        got = fn({"path": "b.txt", "confirm_token": SECRET})
        assert SECRET not in str(got), f"{name} 侧凭证原文未遮蔽：{got}"
        assert got["confirm_token"] == agent_audit.SENSITIVE_ARG_MASK, (
            f"{name} 侧遮蔽值不是逐字冻结的占位：{got.get('confirm_token')!r}"
        )
        # 遮蔽**不是删除**：键仍在 ⇒ 「这次传了凭证」在审计里可见
        assert "confirm_token" in got, f"{name} 侧把键删掉了 ⇒ 审计里看不出「传了凭证」"
        # 非敏感键**逐字不变**（只增不改的正控）
        assert got["path"] == "b.txt", f"{name} 侧误伤非敏感键：{got}"


def test_c_plain_args_semantics_unchanged():
    """**①-c 负控**：非凭证参数的既有语义**不被收敛破坏**（键集 + 值）。

    ⚠️ 没有这条，「一律遮蔽 / 一律返回空」也能过 ①-a 与 ①-b —— 而那会毁掉审计面。
    """
    for name, fn in (("runner", agent_runner._args_digest), ("audit", agent_audit._args_digest)):
        assert fn({"query": "蓝鲸有多大", "limit": 5}) == {"query": "蓝鲸有多大", "limit": "5"}, name
        assert fn(None) == {}, name
        assert fn({}) == {}, name
    # **包含匹配**是本表有意不做的：这些键名含敏感子串但语义正常 ⇒ 两侧都不得遮蔽
    for benign in ("token_count", "secretary", "api_keys_total", "password_hint_len"):
        assert agent_runner._args_digest({benign: "正常值"})[benign] == "正常值", benign
        assert agent_audit._args_digest({benign: "正常值"})[benign] == "正常值", benign


def test_d_runner_delegates_to_audit_impl():
    """**①-d（结构腿）**：收敛是**真委托**，不是把 audit 那版**抄一份**。

    只断「输出相等」证明不了「收敛成一份」（抄一份也一样相等，明天改一处又分岔 = 092 的成因本身）。
    本条把「收敛」钉成机器可判的**结构**事实：字节码里**不得有本地摘要循环**，
    必须是一次对 `audit._args_digest` 的调用。

    ⚠️ 口径更正见模块 docstring：**不能**用 `is`（收敛后 runner 侧是薄包装，`is` 恒 False）。
    本条的判别力 = 把委托还原成一份本地实现 ⇒ **必红**。
    """
    buf = io.StringIO()
    dis.dis(agent_runner._args_digest, file=buf)
    code = buf.getvalue()

    # ① 必须**真调用** audit 那份（唯一实现点）
    assert "agent_audit" in code and "_args_digest" in code, (
        f"runner._args_digest 字节码里看不到对 audit 的调用 ⇒ 委托关系断了：\n{code}"
    )
    # ② 不得残存**本地摘要实现**的动作（有其一 = 第二份实现复活）
    for banned in ("json.dumps", "model_dump", "ARGS_CLIP"):
        assert banned not in code, (
            f"runner._args_digest 里仍有本地摘要动作 {banned!r} ⇒ 双真相源复活（092 ①）：\n{code}"
        )


def test_e_cache_fingerprint_uses_graph_enabled(monkeypatch):
    """**②**：`cache/entry.retrieval_fingerprint` 的 `graph.enabled` **走唯一读取点**。

    断「**调用了** `app.retrieval.graph.enabled`」而不是「值恰好对」：
    直读 `settings.GRAPHRAG_ENABLE` 也能得到恰好对的值 ⇒ 后者是**弱判据**，抓不住二次直读。

    ⚠️ 本函数还有另外两支会抛异常 / 读 chat_config，故打桩 `_reasoning_fingerprint`
    把被测面收窄到 `graph` 子对象（与 ② 的缺陷面一致）。
    """
    calls: list[int] = []

    def spy_enabled() -> bool:
        calls.append(1)
        return bool(getattr(settings, "GRAPHRAG_ENABLE", False))

    monkeypatch.setattr(retrieval_graph, "enabled", spy_enabled)
    monkeypatch.setattr(entry_mod, "_reasoning_fingerprint", lambda: {})
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", True)

    fp = entry_mod.retrieval_fingerprint()

    assert calls, (
        "retrieval_fingerprint 没有走 graph.enabled() ⇒ 仍是二次直读 GRAPHRAG_ENABLE（092 ②）"
    )
    # 行为逐字等价：开关真值被如实带进指纹
    assert fp["graph"]["enabled"] is True

    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", False)
    calls.clear()
    assert entry_mod.retrieval_fingerprint()["graph"]["enabled"] is False
    assert calls, "关态也没走唯一读取点"

def test_f_non_string_values_render_as_json_not_python_repr():
    """**①-e（行为契约腿）**：非字符串值**渲染成 JSON**（`json.dumps`），**不是** Python repr（`str()`）。

    🔴 **这条是本轮真回归的钉子**（`回归修复` 收敛过程中实测踩中）：
    收敛到 `audit` 那份时，它对非字符串值用 `str(value)` ⇒ 嵌套 dict 输出
    `"{'kb_ids': [9]}"`（Python repr、单引号）⇒ `json.loads` 抛 `JSONDecodeError`。
    而 `tests/api/test_chat_agent.py:778` 逐字要求 `json.loads(args["filters"]) == bad["filters"]`
    ⇒ **契约要求 JSON 形态**（该行 `:777` 的注释逐字写「`_args_digest` 把非字符串值 JSON 化」）。

    ⚠️ 只断「两侧相等」**抓不到这个回归**（两侧可以**一起**错成 `str()`）——
    所以本条**独立**断言「输出必须能被 `json.loads` 解回原值」。

    ⚠️ 边界（别把本条记成「所有值都必须可 json.loads」）：
    裸字符串 `"txt"` **不经** `json.dumps`（原样返回）⇒ 对它是**非法 JSON**，这是**既有语义**、不是缺陷；
    遮蔽占位 `"***"` 同理。本条只钉**容器 / 标量**（dict / list / bool / None / 数值）。
    """
    import json

    # ① 嵌套 dict（真回归的原始触发形态）：必须是 JSON，且解得回原值
    payload = {"filters": {"kb_ids": [9]}}
    for name, fn in (("runner", agent_runner._args_digest), ("audit", agent_audit._args_digest)):
        got = fn(dict(payload))
        assert got["filters"] == '{"kb_ids": [9]}', (
            f"{name} 侧把嵌套 dict 渲染成了 Python repr（应为 JSON 形态）：{got['filters']!r}"
        )
        assert json.loads(got["filters"]) == payload["filters"], (
            f"{name} 侧输出无法被 json.loads 解回（回归修复 真回归）：{got['filters']!r}"
        )

    # ② 表驱动：容器与标量一律 JSON 形态且解得回
    for value in (False, None, 5, 1.5, [1, 2], [{"b": None}], {"a": 1}):
        for name, fn in (("runner", agent_runner._args_digest), ("audit", agent_audit._args_digest)):
            text = fn({"k": value})["k"]
            assert json.loads(text) == value, (
                f"{name} 侧 {value!r} 渲染成 {text!r}，json.loads 解回的不是原值"
            )

    # ③ 负控：裸字符串**保持原样**（不经 json.dumps）——「只增不改」
    assert agent_runner._args_digest({"s": "txt"})["s"] == "txt"
    assert agent_audit._args_digest({"s": "txt"})["s"] == "txt"
