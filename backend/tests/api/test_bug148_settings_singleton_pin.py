"""`回归修复` · settings 进程级单例的 pin **机制化**（缺陷清单-2 阶段七 · 测试面条目）。

## 缺陷本体

`PUT /api/settings`（`settings_routes.py:575-576`）与 `PUT /api/skills`（`skill_routes.py:197-199`）
是**裸 `setattr(settings, key, …)`** 写**进程级单例**；而 `tests/api/conftest.py` 的隔离靠**手工 pin
清单**（九条 autouse，只覆盖八类键）。`monkeypatch` 只还原「它自己钉过的键」⇒ 端点写到清单外的键时，
该值**跨用例、跨文件**留在单例里。主账本 `087`（`AGENT_MODE` 漏移）就是这条机制的一次实例。

## 两条机制（本轮落地，都在 `tests/api/conftest.py`）

1. `pin_settings_put_keys`：从 :data:`SETTINGS_PUT_KEYS`（统一键清单）逐个 pin 现值 ⇒ teardown 还原；
2. `restore_settings_singleton`：`vars(settings)` **diff 快照**兜底 ⇒ 清单外的改动也还原。

## 本文件的载体（13 例，缺任何一条都不是合格判据）

| 例 | 腿 | 判什么 |
|---|---|---|
| `test_a1_...` | 静态 | AST 扫两个端点模块 ⇒ **清单 == 端点可写键**（漂移即红） |
| `test_a1d_...` | 静态 | **接线哨兵腿**（B 路终态复核）：真实面必须**调用**规则⑥、且交给它的是**真 config 树**（哨兵键 + spy；闭合「只扫端点树」式绕过 —— 实测该绕过下 a1/a1b/a1c 三条全绿） |
| `test_a1c_...` | 静态 | **规则⑥ 判别力腿**：`_endpoint_writable_keys()` 必须含 `config.dump_registry` 的产键（首轮击杀实测：删掉该规则 `test_a1` 仍绿 ⇒ 当时无判据） |
| `test_a1b_...` | 静态 | **扫描器判别力自证**：合成源码里四种写法（`updates["K"]=` / `updates.update({"K": …})` / `settings.K=` / `return {"K": …}`）**逐条都要被抽出**（含 A 路审计实测的 `updates.update({...})` 盲区） |
| `test_a2_...` | 静态 | 清单里每个键**真的存在**于 `Settings` 上（避免 pin 静默跳过） |
| `test_b1/b2` | 行为 | **清单内**键被「端点式裸写」污染 ⇒ 下一例读到的必须是原值（机制①） |
| `test_c1/c2` | 行为 | **清单外**键被裸写污染 ⇒ 下一例读到的必须是原值（机制②兜底） |
| `test_d1/d2` | 行为 | **真端点** `PUT /api/settings` 写 `AGENT_MAX_ROUNDS` ⇒ 下一例读到的必须是原值 |

⚠️ **回退击杀口径（如实登记）**：把两条 autouse 夹具删掉、**保留** `SETTINGS_PUT_KEYS` 常量
⇒ `test_b2` / `test_c2` / `test_d2` **三条真红**（`test_a1` / `test_a2` 仍绿 —— 它们判的是
「清单与端点的对应关系」，不判机制是否生效）。

⚠️ 判据是**进程内**判别（不起真服务、不出网）；`test_d` 的 `.env` 走 `tmp_path`，**绝不碰仓库真实 .env**。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.core import env_store
from app.core.config import settings
from tests.api._sync_client import SyncClient, build_app
from tests.api.conftest import SETTINGS_PUT_KEYS

#: 进程启动时（本模块 import 时）的单例真值 —— 任何泄漏都会让 b2/c2/d2 与它不等。
_ORIGINAL_MAX_ROUNDS = settings.AGENT_MAX_ROUNDS
_ORIGINAL_MIN_RERANK = settings.RAG_MIN_RERANK_SCORE

#: 记录「写侧那一例真的跑过」：读侧据此把「顺序被改」与「机制失效」两种红区分开
#: （顺序变了 ⇒ 读侧会**响亮地**红，而不是因为没写入而真空通过）。
_RAN: list[str] = []

_UPPER_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _str_const(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _collect_writable_keys(tree: ast.AST) -> set[str]:
    """从一棵 AST 收集「能写进 settings 单例的键名」——**五条来源**（与端点现读一一对应）：

    ① `updates["KEY"] = …`（`Subscript` 且下标是字符串常量）；
    ② `_resolve_group(…, {"provider": "MODEL_PROVIDER", …}, updates)` —— 第 3 个实参 dict 的**值**
       （`provider`/`base_url`/… 是入参字段名，值才是 settings 键名）；
    ③ `return {"KEY": …}` 的 dict 字面（`_resolve_translate` / `_resolve_reasoning`；
       小写键的响应体字面被 `^[A-Z][A-Z0-9_]*$` 过滤掉）；
    ④ `settings.KEY = …`（`skill_routes.py:198-199`）；
    ⑤ `updates.update({"KEY": …})` 的**字面** dict —— 🔴 **A 路审计实测的盲区补口**（2026-09-28）：
       `:524/:543/:545/:553` 走的是**函数返回**（由 ①③ 覆盖），但同模块里若再写一条**字面** `update({...})`，
       前四条**都抓不到** ⇒ 「清单 == 端点可写键」这条腿会静默失灵。本条规则 + `test_a1b` 的判别力自证一起闭合。
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "updates"
                ):
                    key = _str_const(target.slice)
                    if key:
                        found.add(key)
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "settings"
                    and _UPPER_KEY_RE.match(target.attr)
                ):
                    found.add(target.attr)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_resolve_group"
            and len(node.args) >= 3
            and isinstance(node.args[2], ast.Dict)
        ):
            for value in node.args[2].values:
                key = _str_const(value)
                if key and _UPPER_KEY_RE.match(key):
                    found.add(key)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for key_node in node.value.keys:
                key = _str_const(key_node)
                if key and _UPPER_KEY_RE.match(key):
                    found.add(key)
        # --- 规则⑤（REAL-148-1 补口）BEGIN ---
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "updates"
            and node.args
            and isinstance(node.args[0], ast.Dict)
        ):
            for key_node in node.args[0].keys:
                key = _str_const(key_node)
                if key and _UPPER_KEY_RE.match(key):
                    found.add(key)
        # --- 规则⑤（REAL-148-1 补口）END ---
    return found


def _collect_function_return_keys(tree: ast.AST, func_name: str) -> set[str]:
    """某个**具名函数**内 `return {"KEY": …}` 字面的大写键（用于 `config.dump_registry`）。

    🔴 本条的直接动因 = B 路只读对抗审计（2026-09-28）实测的**可实现假绿构造**：
    `config.dump_registry` 的返回 dict 里加第三个键 ⇒ 端点可写键 +1，而「清单」与「扫描面」都不变
    ⇒ `test_a1` 仍绿。现有 `MODEL_SERVICES`/`MODEL_ROLES` 被扫到只是**巧合覆盖**
    （`settings_routes.py:484/:486` 也写了 `updates["MODEL_SERVICES"]`/`["MODEL_ROLES"]`）——
    本条规则把「注册表这两键的**真正产地**」纳入扫描面。
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict):
                    for key_node in sub.value.keys:
                        key = _str_const(key_node)
                        if key and _UPPER_KEY_RE.match(key):
                            found.add(key)
    return found


def _writable_keys_from(api_trees: list[ast.AST], config_tree: ast.AST) -> set[str]:
    """**组合体**：端点面的可写键 ∪ `config.dump_registry` 的产地键。

    ⚠️ 为什么单独抽一层：**两条来源会撞键** —— `MODEL_SERVICES`/`MODEL_ROLES` 既在
    `config.dump_registry` 的 return dict 里，也被 `settings_routes.py:484/:486` 写了一次
    （= B 路审计说的「巧合覆盖」）⇒ 直接在真实模块上断言 `registry_keys <= derived` **无法判别**
    规则⑥ 是否生效（首轮击杀实验实证：删掉该规则，`test_a1` 与「子集断言」都仍绿）。
    本层允许测试传**空端点面** ⇒ 合成 config 树的产键必须独立出现在结果里。
    """
    found: set[str] = set()
    for tree in api_trees:
        found |= _collect_writable_keys(tree)
    found |= _collect_function_return_keys(config_tree, "dump_registry")
    return found


def _endpoint_writable_keys() -> set[str]:
    """真实模块面的组合（两个 PUT 端点模块 ∪ `config.dump_registry` 产地键）。"""
    from app.api import settings_routes, skill_routes
    from app.core import config as config_mod

    api_trees = [
        ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for module in (settings_routes, skill_routes)
    ]
    config_tree = ast.parse(Path(config_mod.__file__).read_text(encoding="utf-8"))
    return _writable_keys_from(api_trees, config_tree)


@pytest.fixture
def fake_dotenv(tmp_path, monkeypatch) -> Path:
    """把 `.env` 指到 `tmp_path`（照 `test_settings_contract.py::fake_dotenv` 的写法）。"""
    path = tmp_path / ".env"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(env_store, "ENV_PATH", path)
    assert env_store.ENV_PATH == path
    assert env_store.ENV_PATH != env_store.ROOT_DIR / ".env", "假 .env 落到了仓库真实路径上"
    return path


@pytest.fixture
def client():
    """只挂 settings router 的干净 ASGI 应用（不起 lifespan / 不碰 DB）。"""
    from app.api.settings_routes import router

    return SyncClient(build_app(router))


# --- a：静态腿（清单本身）---------------------------------------------------------------------


def test_a1_pin_list_equals_endpoint_writable_keys():
    """清单 == 两个端点**实际能写**的键集（防漂移：端点加键而清单没跟 ⇒ 红）。"""
    derived = _endpoint_writable_keys()
    listed = set(SETTINGS_PUT_KEYS)
    assert len(listed) == len(SETTINGS_PUT_KEYS), "清单里有重复键"
    assert derived == listed, (
        "端点可写键与 SETTINGS_PUT_KEYS 漂移 —— 只多（端点新增未入清单）="
        f"{sorted(derived - listed)}；只少（清单里的键端点已不写）={sorted(listed - derived)}"
    )


#: 判别力自证用合成源码：四种写法各一条（键名 `P_V2_PROBE_*` 不可能出现在真实端点里）。
_PROBE_SOURCE = (
    "def _probe(updates):\n"
    "    updates[\"P_V2_PROBE_LITERAL\"] = \"x\"\n"
    "    updates.update({\"P_V2_PROBE_UPDATE_CALL\": \"x\"})\n"
    "    settings.P_V2_PROBE_ATTR = \"x\"\n"
    "    return {\"P_V2_PROBE_RETURN\": \"x\"}\n"
)


def test_a1b_scanner_discriminates_all_endpoint_write_forms():
    """**判别力自证**：合成源码里四种写法必须**逐条**被抽出（否则腿 a1 是空靶）。

    🔴 本条的直接动因 = A 路只读对抗审计（2026-09-28）实测的 **REAL-148-1**：
    原扫描器漏了 `updates.update({...})` 的**字面**形态 ⇒ 审计方注入该写法后 `derived == listed` 仍为 True。
    """
    found = _collect_writable_keys(ast.parse(_PROBE_SOURCE))
    expected = {
        "P_V2_PROBE_LITERAL",
        "P_V2_PROBE_UPDATE_CALL",
        "P_V2_PROBE_ATTR",
        "P_V2_PROBE_RETURN",
    }
    assert found == expected, f"扫描器漏了：{sorted(expected - found)}；多出：{sorted(found - expected)}"

    # 规则⑥（注册表产地）：合成一个 dump_registry ⇒ 它的 return dict 键必须被抽出
    registry_found = _collect_function_return_keys(
        ast.parse(
            "def dump_registry(a, b):\n"
            "    return {\"P_V2_PROBE_REGISTRY_A\": 1, \"p_v2_lower_is_ignored\": 2}\n"
        ),
        "dump_registry",
    )
    assert registry_found == {"P_V2_PROBE_REGISTRY_A"}, registry_found
    # 反向对照：函数名不匹配 ⇒ 抽不到（证明它是「按名定位」而不是恒真扫描）
    assert _collect_function_return_keys(
        ast.parse("def other(a):\n    return {\"P_V2_PROBE_REGISTRY_A\": 1}\n"), "dump_registry"
    ) == set()


def test_a1c_endpoint_scan_includes_the_registry_producer_keys():
    """规则⑥ 的**判别力腿**：`_endpoint_writable_keys()` 必须真的含 `config.dump_registry` 的产键。

    🔴 动因 = 本载体**首轮加固的击杀实验**（2026-09-28）：把规则⑥ 那行调用删掉后，
    `test_a1` **仍绿**（`MODEL_SERVICES`/`MODEL_ROLES` 恰好也被 `settings_routes.py:484/:486` 写了一次 = 巧合覆盖）
    ⇒ 「扫描面含注册表产地」这件事当时**没有判据**。本条把它钉住：产键集合非空且是 `_endpoint_writable_keys()` 的子集。
    """
    from app.core import config as config_mod

    # ① 规则本身认得 `dump_registry` 的 return dict（合成面 + 真实面）
    synthesized = ast.parse("def dump_registry(a, b):\n    return {\"P_V2_PROBE_REGISTRY_A\": 1}\n")
    registry_keys = _collect_function_return_keys(
        ast.parse(Path(config_mod.__file__).read_text(encoding="utf-8")), "dump_registry"
    )
    assert registry_keys == {"MODEL_SERVICES", "MODEL_ROLES"}, registry_keys
    # ② **判别力核心**：端点面**为空**时，组合体也必须把 config 树的产键带出来
    #    （若规则⑥ 被删/被绕过 ⇒ 这里得到空集 ⇒ 红；真实模块上无法判别，因为两键与端点写入撞名）
    assert _writable_keys_from([], synthesized) == {"P_V2_PROBE_REGISTRY_A"}
    assert _writable_keys_from([], ast.parse(Path(config_mod.__file__).read_text(encoding="utf-8"))) == registry_keys
    # ③ 真实模块面：组合体 ⊇ 注册表产键（弱断言，作为接线自证）
    derived = _endpoint_writable_keys()
    assert registry_keys <= derived, f"规则⑥ 没接线：扫描面缺 {sorted(registry_keys - derived)}"


def test_a1d_real_face_routes_the_real_config_tree_through_the_scanner(monkeypatch):
    """**接线哨兵腿**（B 路终态复核实证的**第二个假绿构造**，2026-09-28）。

    🔴 动因：`MODEL_SERVICES`/`MODEL_ROLES` 与端点写入**撞名** ⇒ 把「真实面」
    `_endpoint_writable_keys()` 改成**只扫端点树**（不经过 `_collect_function_return_keys`）时，
    `test_a1` / `a1b` / `a1c` **三条全绿**（复核方实测）——「真实面是否真的走了规则⑥」当时无判据。
    本条用**哨兵键** + **spy** 把两件事机器化：① 真实面必须**调用**规则⑥；② 交给它的树必须是
    **真 config 树**（含 `dump_registry` 函数定义）⇒ 连「传空树」式绕过也一并封死。
    """
    import tests.api.test_bug148_settings_singleton_pin as mod

    seen: list[tuple[str, bool]] = []

    def _spy(tree: ast.AST, name: str) -> set[str]:
        has_def = any(
            isinstance(node, ast.FunctionDef) and node.name == "dump_registry" for node in ast.walk(tree)
        )
        seen.append((name, has_def))
        return {"P_V2_PROBE_REG_ONLY"}

    monkeypatch.setattr(mod, "_collect_function_return_keys", _spy)
    derived = _endpoint_writable_keys()
    assert "P_V2_PROBE_REG_ONLY" in derived, "真实面没走规则⑥（哨兵键没进来）"
    assert seen == [("dump_registry", True)], f"真实面没把**真 config 树**交给规则⑥：{seen}"


def test_a2_every_listed_key_exists_on_settings():
    """清单里每个键都真的在 `Settings` 上（否则 `pin_settings_put_keys` 会静默跳过它）。"""
    missing = [key for key in SETTINGS_PUT_KEYS if not hasattr(settings, key)]
    assert missing == [], f"这些键不在 Settings 上：{missing}"


# --- b：清单内键（机制①）-----------------------------------------------------------------------


def test_b1_raw_write_of_listed_key_takes_effect():
    """**端点式裸写**（不走 `monkeypatch`）：`AGENT_MAX_ROUNDS` 是端点可写、又在清单里。"""
    probe = _ORIGINAL_MAX_ROUNDS + 1
    assert probe != _ORIGINAL_MAX_ROUNDS
    setattr(settings, "AGENT_MAX_ROUNDS", probe)
    assert settings.AGENT_MAX_ROUNDS == probe, "裸写没生效 ⇒ 本腿没有判别力"
    _RAN.append("b1")


def test_b2_listed_key_was_restored_after_the_test():
    """上一例的污染必须在夹具 teardown 里被还原（机制① = 统一键清单 pin）。"""
    assert "b1" in _RAN, "同文件执行顺序被改变：本载体依赖文件内定义序（b1 → b2）"
    assert settings.AGENT_MAX_ROUNDS == _ORIGINAL_MAX_ROUNDS, (
        f"AGENT_MAX_ROUNDS 跨用例泄漏：{settings.AGENT_MAX_ROUNDS!r} != 原值 {_ORIGINAL_MAX_ROUNDS!r}"
    )


# --- c：清单外键（机制②兜底）-------------------------------------------------------------------


def test_c1_raw_write_of_unlisted_key_takes_effect():
    """`RAG_MIN_RERANK_SCORE` **不在**端点可写清单里（端点不写它）⇒ 只能靠 diff 快照兜底。"""
    assert "RAG_MIN_RERANK_SCORE" not in SETTINGS_PUT_KEYS
    probe = _ORIGINAL_MIN_RERANK + 0.017
    assert probe != _ORIGINAL_MIN_RERANK, "探针值与原值相同 ⇒ 本腿没有判别力"
    setattr(settings, "RAG_MIN_RERANK_SCORE", probe)
    assert settings.RAG_MIN_RERANK_SCORE == probe, "裸写没生效 ⇒ 本腿没有判别力"
    _RAN.append("c1")


def test_c2_unlisted_key_was_restored_after_the_test():
    """清单外键也必须还原（机制② = `vars(settings)` diff 快照）。"""
    assert "c1" in _RAN, "同文件执行顺序被改变：本载体依赖文件内定义序（c1 → c2）"
    assert settings.RAG_MIN_RERANK_SCORE == _ORIGINAL_MIN_RERANK, (
        f"清单外键跨用例泄漏：{settings.RAG_MIN_RERANK_SCORE!r} != 原值 {_ORIGINAL_MIN_RERANK!r}"
    )


# --- d：真端点写入（087 的 generalized 复现）----------------------------------------------------


def test_d1_endpoint_put_write_takes_effect(client, fake_dotenv):
    """走**真端点** `PUT /api/settings` 的 `agent.max_rounds`（与 087 的泄漏路径同形）。"""
    probe = 9 if _ORIGINAL_MAX_ROUNDS != 9 else 8
    assert 1 <= probe <= 12
    resp = client.put("/api/settings", json={"agent": {"max_rounds": probe}})
    assert resp.status_code == 200, resp.text
    assert settings.AGENT_MAX_ROUNDS == probe, "端点没写进单例 ⇒ 本腿没有判别力"
    _RAN.append("d1")


# --- e：属性删除的还原（B 路审计 MINOR-1 补口）--------------------------------------------------


def test_e1_deleting_a_key_from_the_singleton_takes_effect():
    """`delattr(settings, KEY)`（**清单外**键）必须真的删掉 —— 机制② 的删除型分支。

    🔴 原实现的条件是 `if key in after and after[key] != value` ⇒ **键被删时直接跳过**，
    删除不还原（B 路审计实测：`delattr` 成功、其后访问抛 `AttributeError`）。
    """
    assert "RAG_MIN_RERANK_SCORE" not in SETTINGS_PUT_KEYS
    delattr(settings, "RAG_MIN_RERANK_SCORE")
    assert not hasattr(settings, "RAG_MIN_RERANK_SCORE"), "删除没生效 ⇒ 本腿没有判别力"
    _RAN.append("e1")


def test_e2_deleted_key_was_restored_after_the_test():
    """被删的键必须由机制② 还原（否则下游用例会拿到 `AttributeError`）。"""
    assert "e1" in _RAN, "同文件执行顺序被改变：本载体依赖文件内定义序（e1 → e2）"
    assert hasattr(settings, "RAG_MIN_RERANK_SCORE"), "键没被还原：删除型污染跨用例泄漏"
    assert settings.RAG_MIN_RERANK_SCORE == _ORIGINAL_MIN_RERANK


def test_d2_endpoint_write_does_not_leak_into_the_next_test():
    """端点写的单例值必须在用例末被还原（否则就是 `087` 的 generalized 形态）。"""
    assert "d1" in _RAN, "同文件执行顺序被改变：本载体依赖文件内定义序（d1 → d2）"
    assert settings.AGENT_MAX_ROUNDS == _ORIGINAL_MAX_ROUNDS, (
        f"端点写入跨用例泄漏：{settings.AGENT_MAX_ROUNDS!r} != 原值 {_ORIGINAL_MAX_ROUNDS!r}"
    )
