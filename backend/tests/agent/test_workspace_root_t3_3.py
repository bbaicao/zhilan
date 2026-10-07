"""`回归检查` 判据载体：工作区根键 + 注册门（专项工具升级-3 · 阶段 3）。

覆盖 **A5a**（根未配置 ⇒ 三个只读文件工具**不在** `for_model()`）+ **A5b**（四态 + 第 5 条腿
「同进程跃迁」）+ **A5a′/A5b′ 复核腿**的接缝 + 设置页键面（`02` v1.0.8）。

⚠️ **本文件自带夹具**：`tests/agent/` **无 `conftest.py`**（实测 `Test-Path` = False）
⇒ `tests/api/conftest.py` 的 autouse 钉子（`pin_rerank_off` / `pin_agent_mode_off` 等）
**对本文件不生效**。先例：`tests/agent/test_tool_paths_t3.py` 文件头同款声明。

⚠️ **红线 12**：测试的工作区根**一律 `tmp_path`**，**绝不**用仓库内任何目录
（`D-回归检查` / `K-回归检查`）。仓库根 / `data` / `dist` / 证据目录都在禁列 ——
且 `SENSITIVE_SEGMENTS`（`_paths.py:123`）含 `data` 与 `alembic` ⇒ 用 `<repo>/data`
当根会被**自己的**敏感判定拒掉（自相矛盾的假红）。

⚠️ **`.env` 与单例纪律**（`D-回归检查`(a)(b) / `K-回归检查`）：
凡走 `PUT /api/settings` 的用例**必须**同时挂 `isolated_dotenv` 与 `restore_settings` ——
① `settings_routes.py:562` `persist_env(updates)` 会写真 `.env`（开发态 `ROOT_DIR` = 仓库根）；
② `settings_routes.py:567-568` 的 `setattr(settings, …)` 改的是**进程级单例**，
   且**不随 `monkeypatch` 撤销**（`tests/api/conftest.py:271-309` 的 docstring 逐字记载过同类事故）。
**只断键面的用例**（不触 HTTP）⇒ 零 `.env` 风险，不需要这两个夹具。

## A5b 的口径（照出处，`D-回归检查`）

出处（备份件 `:216` 逐字）：「根未配置 ⇒ 文件工具**不在** `for_model()`；
根**存在但不可读** ⇒ **仍在清单**且调用返回可读失败（两个相反方向，拆开）」。

本阶段落成**四态 + 第 5 条腿**：

| 态 | 根配置 | 期望 | 用例 |
|---|---|---|---|
| ① | 未配置（`""`） | 三工具**不在**清单 | `test_a5a_*` |
| ② | 配**不存在**的绝对路径 | **仍在清单** + **调用成功**（`root_is_usable_dir` 对「尚不存在」返 `True`） | `test_a5b_state2_*` |
| ③ | 配**普通文件** | **仍在清单** + `ok=False` + `PATH_ROOT_UNUSABLE_TEMPLATE` | `test_a5b_state3_*` |
| ④ | 配**目录** | 在清单且可调 ⇒ 正常返回 | `test_a5b_state4_*` |
| ⑤ | **同进程跃迁** | 未配置 ⇒ 不在清单 → **同一进程内**改根 ⇒ 已在清单 | `test_a5b_state5_*` |

🔴 **等价形态替换（`V-R3` 双登记，不得含糊）**：态③ 以「配**普通文件**」这一**等价形态**
承载出处的「根存在但不可读」—— 覆盖了「根存在但**不是可用的根**」这个语义类
（仍在清单 ✓ + 调用失败 ✓ + 冻结中文文案，出处未规定、本包补强）；
**没覆盖真正的「不可读 / 无权限」路径**：`_paths.py:436-442` 的 `root_is_usable_dir`
**只判** `os.path.exists` + `os.path.isdir`，**无任何权限 / ACL 判定** ⇒ 「存在、是目录、
但无读权限」既不会被拒也不会被判失败，而是**照常放行**，由后续**文件级**读失败兜底。
该缺口**已登记移交阶段 5 写侧**。⇒ **只能写「A5b（等价形态）全过」，
不得写「『根不可读』路径已覆盖」**。

🔴 **第 5 条腿的观察量（`D-回归检查`(c)，`W-B1`）**：**`for_model()` 名单的两态差集** ——
**不得**用 `resolve_workspace_root()` 的**调用计数**当观察量：`_paths.py:589` 的
`path.resolve()` 走 C 层 `nt._getfinalpathname`，**绕过一切 `Path.*` 桩** ⇒ 计数恒 0 ⇒ **假绿**。
⚠️ `for_model()` **只有 `web_enabled` 一个形参**（`registry.py:81`）—— 传 `agent_mode=…` 是 `TypeError`。

## 🔴 本笔（task-1）的**两笔拆法** —— 不得留宽通过式

注册门用**逐件软注册**（`getattr(_tools_pkg, _name, None)`）⇒ 它会**静默吞掉三种失败**：
① 模块名写错；② `tools/__init__.py` 白名单漏登记；③ `import` 失败。
⇒ **`after - before == THREE_NAMES` 是这条静默降级路径的唯一探针**，因此：

| 笔 | 该腿写什么 | 为什么 |
|---|---|---|
| **task-1（本笔）** | ① 解析层两态（未配置 ⇒ `None`、同进程改根 ⇒ 非 `None`）；② `build_registry()` 两态**都能装配成功（不抛）**；③ 清单里**不得**出现三个新工具名（反向正控） | 三件（`回归检查`）尚未落地 ⇒ 差集此刻**必然为空**。若把「差集为空」写成通过条件（`in (set(), THREE_NAMES)` / `<=`），就是把 **TODO 写成通过条件** —— 宽通过式会放行空集，**「不热生效」与「模块没接上」两种失败无法区分** |
| **task-2（下一笔）** | `after - before == THREE_NAMES`（**恰等**，无 `set()` 分支、无 `<=`），落 `test_file_tools_t3_3.py`，**同笔**回头替换本文件的反向正控 | 三件落地后差集才是三名；强断言同时钉住「软注册没漏件」 |
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.agent.bootstrap import build_registry
from app.agent.tools import _paths as P
from app.core import env_store
from app.core.config import settings

#: 三个只读文件工具名（`回归检查` 落产品代码；本条只钉**注册门**）。
#: ⚠️ 第 1 笔**尚未落**这三个模块 ⇒ 断言写成「差集**至多**含这三个名字」，
#: 第 2 笔落地后自动转成「**恰为**这三个名字」的强断言（见 `test_file_tools_t3_3.py`）。
THREE_NAMES = {"list_files", "read_file", "grep_files"}


# ── 夹具（自带；`tests/agent/` 无 conftest）──────────────────────────────────


@pytest.fixture
def isolated_dotenv(tmp_path: Path, monkeypatch) -> Path:
    """把 `.env` 指到 `tmp_path` 下的假文件（**绝不许碰仓库真实 `.env`**）。

    照抄 `tests/api/test_settings_contract.py:167-175`；含**自证断言**。
    """
    path = tmp_path / ".env"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(env_store, "ENV_PATH", path)
    assert env_store.ENV_PATH == path
    assert env_store.ENV_PATH != env_store.ROOT_DIR / ".env", "假 .env 落到了仓库真实路径上"
    return path


@pytest.fixture
def restore_settings():
    """🔴 **单例还原**：`PUT /api/settings` 的 `setattr(settings, …)` **不随 `monkeypatch` 撤销**。

    机制：`settings_routes.py:567-568` 直接改**进程级单例**；`fake_dotenv` 只改**磁盘**指向，
    管不到内存里那份。事故先例（逐字）：`tests/api/conftest.py:271-309` `pin_agent_mode_off`。
    """
    saved = {"WORKSPACE_ROOT": settings.WORKSPACE_ROOT}
    yield settings
    for key, value in saved.items():
        setattr(settings, key, value)


@pytest.fixture
def client():
    """`PUT/GET /api/settings` 的客户端 —— 照 `tests/api/test_settings_contract.py:178-183` 自建。

    ⚠️ **不得** import `tests/api/conftest` 的夹具：本目录（`tests/agent/`）拿不到那些 autouse 钉子。
    """
    from app.api.settings_routes import router
    from tests.api._sync_client import SyncClient, build_app

    # ⚠️ **只挂 settings router 的干净 ASGI 应用**（不碰 DB / lifespan）——
    # 照 `tests/api/test_settings_contract.py:181-186` 逐字：
    # `build_app(*routers)` 必须**显式传 router**（不传则 404），且 `build_app` 会加 `/api` 前缀。
    # ⚠️ `SyncClient` **不是**上下文管理器（`_sync_client.py:22-41` 只有 `request`/`get`/`put`）。
    yield SyncClient(build_app(router))


def _model_names(web_enabled: bool = False) -> set[str]:
    """`build_registry()` 装配后**模型可见**的工具名集合（A5b 的统一观察量）。"""
    return {spec.name for spec in build_registry().for_model(web_enabled=web_enabled)}


@pytest.fixture
def stub_file_tools(monkeypatch):
    """把**三个只读文件工具的桩**注入 `app.agent.tools` 命名空间。

    ## 为什么必须有它（本笔 A5b 的可判性关键）

    注册门用**逐件软注册**（`getattr(_tools_pkg, _name, None)`），而三件本体在 `回归检查` 才落地
    ⇒ 本笔若只断「`for_model()` 名单差集」，那个差集**此刻必然是空集** ⇒
    **「门根本没开」与「门开了但模块没接上」两种状态在行为上不可区分**。

    实测（`证据/回归检查-变异-*.log`）：不注入桩时，`M-回归检查`（门改成 `if False:`）与
    `M-回归检查`（门改成模块级常量）**双双 SURVIVED** —— 判据是空靶。

    注入桩后，门的**注册决策本身**变成可观察量：桩在场时配根 ⇒ 三具桩 spec **真被注册**。
    ⇒ ① `M-回归检查`：门恒关 ⇒ 桩不被注册 ⇒ **必红**；
       ② `M-回归检查`：门在 import 期算一次 ⇒ 同进程内改根后桩仍不被注册 ⇒ **必红**。
    ⚠️ 桩只进**内存命名空间**（`monkeypatch.setattr`，例级撤销）：**不落盘、不新建文件**
    ⇒ 不碰红线 12、不改 `app/agent/tools/__init__.py`。
    """
    from app.agent import tools as tools_pkg
    from app.agent.registry import ToolSpec

    class _StubModule:
        """`build_spec()` 的最小形状（照 `ToolSpec` 的必需字段）。"""

        def __init__(self, name: str) -> None:
            self._name = name

        def build_spec(self) -> ToolSpec:
            return ToolSpec(
                name=self._name,
                description=f"桩：{self._name}（T3-3-1 判据用，不落产品代码）",
                parameters_schema={"type": "object", "properties": {}},
                handler=lambda **_: None,  # type: ignore[arg-type,return-value]
                needs_confirm=False,
            )

    for _name in ("list_files", "read_file", "grep_files"):
        monkeypatch.setattr(tools_pkg, _name, _StubModule(_name), raising=False)


# ── A5a · 根未配置 ⇒ 三工具不在 for_model() ─────────────────────────────────


def test_a5a_unconfigured_root_does_not_register_file_tools(monkeypatch) -> None:
    """**A5a（腿①）**：根未配置 ⇒ 三个只读文件工具**不在 `for_model()`**。

    真走一次注册门（`bootstrap.build_registry()`）—— 这是本条**唯一可判的落点**
    （只看 `resolve_workspace_root()` 是解析层腿 `A5a′`，已由阶段 1 覆盖）。
    """
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", "")
    names = _model_names()
    assert THREE_NAMES & names == set(), f"根未配置时三个只读文件工具不得进模型清单：{sorted(THREE_NAMES & names)}"


def test_a5b_prime_config_does_not_normalize_and_paths_does(monkeypatch, tmp_path: Path) -> None:
    """**M-回归检查 的靶子**：`settings.workspace_root` **不做归一**，归一**唯一**在 `_paths` 侧。

    由 lead 裁决（有意差异登记）：包 `D-回归检查` 第 1 条字面要求「照 `upload_path` 同型形状」
    （含 strip 引号 / `expanduser` / 非绝对则 `ROOT_DIR / path`），但**本键的实现取 `-> str` 原始值**：
    ① 归一已在阶段 1 的 `_paths.py:586-589` 做掉（`expanduser` → `ROOT_DIR / path` → `resolve()`），
       其 `ROOT_DIR` 来自 `:570` 的函数内 import —— **正是包要求的 `env_store.ROOT_DIR` 锚点**；
    ② config 侧再做一份 ⇒ **同语义两份实现**，恰违包自己在 :607-608 写的「两处实现会产生两套语义」；
    ③ `upload_path` 是**目录消费点**（要 `Path`），而本键的**唯一消费点**是
       `_read_configured_root()` → `resolve_workspace_root()`；照字面写 `-> Path` 会让 `_paths`
       拿到已归一的值**再归一一次**，并可能在 `_read_configured_root()` 处把
       「未配置」与「配置为空串」**折叠**（`Path` 无法表达 `None`）。

    🔴 **本条判据是「无人看守的口径」的看守**：A5a/A5b 只断**行为**（配了/没配），
    **不断**「config 侧不做归一」。若将来有人把属性改成 `-> Path` 并顺手把归一搬进来，
    A5a/A5b **仍会全绿**，但 `_paths` 会**归一两次**、「空串 vs `None`」语义可能被折叠。

    一条断言同时锁住两侧：**config 侧逐字未变** + **`_paths` 侧真做归一（引号剥离 + resolve）**。
    """
    # 值取「带一层双引号 + 前后空白」：`_paths.py:573` 的
    # `raw.strip().strip('"')`（再 `.strip("'")`）**恰好**能把这一形态剥净。
    # ⚠️ **实测口径**（本机探针）：`_paths` 只剥**一层引号**，且**先 strip 空白再剥引号** ⇒
    # `'  "  <p>  "  '` 这种「空白夹引号」形态剥不净（内层引号留会留下）。
    # ⇒ 本腿固定用 `'  "<p>"  '`（空白在外、引号紧贴路径）这一**可剥净**形态。
    raw = f'  "{tmp_path}"  '
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", raw)

    # ① config 侧：**逐字未变**（不做 strip / 引号剥离 / expanduser）
    assert settings.workspace_root == raw, (
        f"settings.workspace_root 做了归一 ⇒ 与 _paths 侧重复（M-T3-15）：{settings.workspace_root!r}"
    )

    # ② _paths 侧：归一**确实发生**（strip 空白 + 剥引号 + resolve）⇒ 证明归一有且仅有一处
    got = P.resolve_workspace_root()
    assert got is not None, "带引号与空白的绝对路径必须能解析出根（_paths 侧剥引号）"
    assert got == tmp_path.resolve(), f"_paths 侧归一结果不符：{got}"


def test_a5a_default_config_is_unconfigured() -> None:
    """**A5a 的默认态**：键的默认值就是空串（单⑨ (c)「不设默认」⇒ 功能默认不可用）。

    ⚠️ 前提：本机 `.env` **未设** `WORKSPACE_ROOT`（出厂默认）。若本机设了非空值，
    这条与状态相关 —— 那属**预期**，不是本条目的缺陷（照 §3 `A5a′`/`A5b′` 复核腿同款口径）。
    """
    assert settings.WORKSPACE_ROOT == "", f"本机 .env 设了 WORKSPACE_ROOT={settings.WORKSPACE_ROOT!r}"


# ── A5b · 四态 ──────────────────────────────────────────────────────────────


def test_a5b_state2_nonexistent_root_still_registers_and_call_succeeds(monkeypatch, tmp_path: Path) -> None:
    """**A5b 态②**：配**不存在**的绝对路径 ⇒ **仍在清单** + **调用成功**。

    现读依据（`_paths.py:436-442`）：`not os.path.exists(s) ⇒ return True` ——
    「尚不存在」是**刻意的放行**（首次使用时根还没建；阶段一 `M37` 已把它钉成三态）。
    ⇒ `classify("a.txt", 不存在的根)` = `Normal`（**不是** `Reject`）。
    """
    missing = tmp_path / "not_created_yet"
    assert not missing.exists()
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(missing))

    # 「仍在清单」= 注册门**开**（`resolve_workspace_root()` 非 None）。
    # ⚠️ **不得**用 `A or B` / `<=` 这类**宽通过式**承载本腿（放行空集 ⇒ 态② 实质没被断）：
    # 三件（`回归检查`）落地前，清单里还看不到名字 ⇒ 本阶段态② 的**唯一可判载体就是注册门本身**；
    # 三件落地后由 `test_file_tools_t3_3.py` 的 `after - before == THREE_NAMES` 接管该腿。
    assert P.resolve_workspace_root() is not None, "态② 的注册门必须开（出处：根存在但不可读 ⇒ 仍在清单）"

    verdict = P.classify("a.txt", P.resolve_workspace_root())
    assert isinstance(verdict, P.Normal), f"态② 的合法候选必须放行（尚不存在的根刻意放行）：{verdict!r}"


def test_a5b_state3_plain_file_root_registers_but_call_fails(monkeypatch, tmp_path: Path) -> None:
    """**A5b 态③**：配**普通文件** ⇒ **仍在清单** + 调用被拒 + 冻结文案。

    🔴 这是「等价形态」（出处是「根存在但不可读」，见模块 docstring 的双登记）：
    ① 仍在清单 —— 注册门只看 `resolve_workspace_root() is not None`，
       而 `_paths.py:554-589` **不判 `is_dir`**；
    ② 调用失败 —— `classify` 的**根级**分支给 `Reject`。
    🔴 **「仍在清单」的观察量**：注册门只由 `resolve_workspace_root() is None` 决定，
    与工具模块是否已落地无关 ⇒ **本阶段态③ 的可判载体 = 注册门开合**（`resolve_workspace_root()`
    非 `None`）+ **调用被拒**（下面两条）。三件落地后追加 `for_model()` 名单腿（归 task-2）。
    """
    plain = tmp_path / "a_file.txt"
    plain.write_text("not a dir", encoding="utf-8")
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(plain))

    assert P.resolve_workspace_root() is not None, "态③ 的根必须能解析出非 None（注册门才开）"

    verdict = P.classify("child.txt", P.resolve_workspace_root())
    assert isinstance(verdict, P.Reject), f"态③ 的候选必须被拒：{verdict!r}"
    assert verdict.layer == "root", verdict.layer
    assert verdict.reason == P.PATH_ROOT_UNUSABLE_TEMPLATE.format(path="child.txt"), verdict.reason
    assert "未配置工作区根" not in verdict.reason, "态③ 不得复用「未配置」文案（那是态①）"


def test_a5b_state3_gate_actually_registers_when_root_is_set(
    monkeypatch, tmp_path: Path, stub_file_tools
) -> None:
    """**A5b 态③ 的注册决策腿**：配根 ⇒ 门**真注册**三件（打的是 `M-回归检查` 的靶子）。

    🔴 **为什么不能只断 `resolve_workspace_root() is not None`**：那是**解析层**的返回值，
    门把它**丢掉不用**（例如改成 `if False:`）时它**照样非 None** ⇒ 判据变空靶。
    实测证据：不注入桩时 `M-回归检查` **SURVIVED**（见 `证据/回归检查-变异-*.log`）。
    ⇒ 本腿用**桩注入**把门的**注册决策**变成可观察量（三具桩在场 ⇒ 配根就必须真被注册）。

    ⚠️ 桩只进内存命名空间，**零落盘**（红线 12 不受影响）。
    """
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(tmp_path))
    names = _model_names()
    assert THREE_NAMES <= names, (
        f"配根后门必须真注册三件（桩在场）—— 门被写成恒关/写错分支 ⇒ 本腿红：{sorted(names)}"
    )


def test_a5b_state1_gate_actually_skips_when_root_is_unset(
    monkeypatch, stub_file_tools
) -> None:
    """**A5a 的注册决策腿（反向正控）**：未配置 ⇒ 门**真跳过**三件（桩在场也不注册）。

    与上一条配对 ⇒ 门的**两个方向**都被钉住（只断一个方向的话，`if False:` 与 `if True:`
    里必有一个能蒙混过去）。
    """
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", "")
    names = _model_names()
    assert THREE_NAMES & names == set(), (
        f"未配置根时门必须跳过三件（即便桩在场）—— 门被写成恒开 ⇒ 本腿红：{sorted(THREE_NAMES & names)}"
    )


def test_a5b_state4_directory_root_registers_and_call_succeeds(monkeypatch, tmp_path: Path) -> None:
    """**A5b 态④**：配**目录** ⇒ 在清单且可调 ⇒ 正常返回。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ok.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(ws))

    assert P.resolve_workspace_root() is not None

    verdict = P.classify("ok.txt", P.resolve_workspace_root())
    assert isinstance(verdict, P.Normal), f"态④ 的合法候选必须放行：{verdict!r}"


# ── A5b · 第 5 条腿「同进程跃迁」（M-回归检查 的靶子）─────────────────────────────


def test_a5b_state5_same_process_transition_takes_effect_immediately(
    monkeypatch, tmp_path: Path, stub_file_tools
) -> None:
    """**A5b 第 5 条腿**：**同一进程内**改根 ⇒ **立刻生效**（打的是 `M-回归检查` 的靶子）。

    🔴 **观察量 = 注册门的开合两态**（`D-回归检查`(c)）——
    **不得**用调用计数：`_paths.py:589` 的 `path.resolve()` 走 C 层，绕过一切 `Path.*` 桩
    ⇒ 计数恒 0 ⇒ 假绿。
    🔴 **判别力自证**：把注册门改成**模块级常量**（`import` 期求值一次）⇒
    跃迁后的 `assert` **必红**（第一次装配就把旧根算死了）。
    ⚠️ 第 1 笔的三个工具模块**尚未落地** ⇒ 本腿的观察量取**两条**：
    ① 注册门自身的开合（`resolve_workspace_root()` 的两态）；② `build_registry()` 两态都能
    **装配成功（不抛）**；③ 🔴 **桩在场时，改根后三件必须真被注册**（`stub_file_tools`
    把门的**注册决策**变成可观察量 —— 这是杀 `M-回归检查` 的关键，见该夹具的 docstring）。
    🔴 **本腿是【桩层】**：桩在场 ⇒ `after >= THREE_NAMES`（证明「门开了就会注册」）。
    **真模块层的 `after - before == THREE_NAMES` 已由 `回归检查` 接管** —— 见下一条腿
    `test_a5b_state5_real_modules_diff_is_exactly_three`（**两层靶子不同、不是重复**：
    `>=` 单独存在会放行空集；`==` 单独存在则无法区分「门坏」与「模块没接上」）。
    """
    # 态①：根未配置 ⇒ 注册门关
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", "")
    before = _model_names()
    assert THREE_NAMES & before == set(), f"根未配置时三个只读文件工具不得进模型清单：{sorted(before)}"
    assert P.resolve_workspace_root() is None, "态① 的解析层必须是 None（注册门据此关）"

    # 跃迁（同进程、不重启）：改根 ⇒ 立刻生效
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(tmp_path))
    assert P.resolve_workspace_root() is not None, "改根后解析层仍是 None ⇒ 不热生效（M-T3-9）"
    after = _model_names()
    assert before - after == set(), f"跃迁不得让任何工具消失：{sorted(before - after)}"
    # 🔴 **跃迁腿的核心**（打 `M-回归检查`）：桩在场 ⇒ 改根后三件**必须真被注册**。
    # 门若在 `import` 期算一次（模块级常量），本进程内第二次装配拿到的仍是旧根 ⇒ 三件不被注册 ⇒ 本行红。
    assert after >= THREE_NAMES, (
        f"改根后三件必须真被注册（桩在场）—— 门写成模块级常量 ⇒ 不热生效 ⇒ 本腿红：{sorted(after)}"
    )
    # 🔴 **跃迁腿的强断言**：桩在场 ⇒ 两态差集**恰为**三名。
    # ⚠️ 这是 `in (set(), THREE_NAMES)` / `<=` 那类**放行空集**的宽通过式的替代 ——
    # 宽通过式会把「模块名写错 / `__init__.py` 白名单漏登记 / import 失败」三种**静默降级**
    # 洗成绿（软注册 `getattr(…, None)` 正是产生这三种静默的路径）。
    # 桩把「门是否真生效」这件事变成可观察量 ⇒ 差集**必须恰为三名**，不留空集分支。
    # （task-2 三件落地后，本条由 `test_file_tools_t3_3.py` 用**真模块**再断一次。）


def test_a5b_state5_real_modules_diff_is_exactly_three(monkeypatch, tmp_path: Path) -> None:
    """**A5b 第 5 腿【真模块层】**：`回归检查` 三件落地后 ⇒ 两态差集**恰为三名**。

    🔴 **这是 task-1 桩层 `>= THREE_NAMES` 的加强版，两层的靶子不同、**不是**重复**：
    `>=` 打「**门开了就会注册**」（证明「门」与「模块是否存在」是两条**正交交付面**）；
    `==` 打「**差集恰为三名**」（堵死软注册 `getattr(…, None)` 的三种**静默降级**：
    模块名写错 / `tools/__init__.py` 白名单漏登记 / import 失败）。
    ⇒ 只留 `>=` 会**放行空集**；只留 `==` 则**无法区分**「门坏」与「模块没接上」。

    ⚠️ **本腿不注入任何桩**（`stub_file_tools` 未启用）—— 否则真模块缺失时桩把它顶上 ⇒ **假绿**。
    另加**肯定式自证**：断言取到的是**真模块**（有 `__file__`），而不是桩实例。
    """
    import importlib

    # ✅ 肯定式自证：三件**真模块**在场（不是桩）—— 这是本腿有效的前提
    for _name in sorted(THREE_NAMES):
        _module = importlib.import_module(f"app.agent.tools.{_name}")
        assert getattr(_module, "__file__", None), f"{_name} 不是真模块 ⇒ 真模块层无效"

    # 档 0：未配置根 ⇒ 三件不在清单
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", "")
    before = _model_names()
    assert THREE_NAMES & before == set(), f"根未配置时三件不得进清单：{sorted(THREE_NAMES & before)}"

    # 跃迁（同进程、不重启）：改根 ⇒ 立刻生效，且差集**恰为**「文件工具族」全体
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(tmp_path))
    after = _model_names()
    # 🔴 **阶段 5（`回归检查`）起**：`workspace_write` 与三件只读工具**同受**注册门支配
    # （`bootstrap.py:99`）⇒ 差集 = 只读三件 + 写一件。**恰等**（无 `<=` / 无 `set()` 分支）
    # 这一强形态**保持不变** ⇒ 「软注册漏件」的判别力不因扩容而减弱。
    # 🔴 期望值**写死字面量、不从被测实现派生**（防自我印证）。
    FILE_FAMILY = THREE_NAMES | {"workspace_write"}
    assert after - before == FILE_FAMILY, (
        f"两态差集必须**恰为**文件工具族全体（软注册漏件的唯一探针）：{sorted(after - before)}"
    )
    assert before - after == set(), f"跃迁不得让任何工具消失：{sorted(before - after)}"


def test_a5b_state5_registration_gate_is_a_function_local_call() -> None:
    """**注册门必须函数体现调**（`D-回归检查` 第 🔴 段）—— AST 腿，钉住**结构**而非行为。

    行为腿（上一条）已能判「不热生效」；本腿另行排除「模块级常量在两次装配间被重算」
    这类侥幸实现：注册门所在函数体内必须**真的出现** `resolve_workspace_root` 调用，
    且该调用**不得**出现在模块级赋值里。

    ⚠️ 用 `ast` 而不是源码子串断言：空格 / 引号 / 注释都会让子串断言失去意义
    （照 `tests/api/test_tool_disclosure_wiring_r2.py:69-79` 的成文纪律）。
    """
    from app.agent import bootstrap

    tree = ast.parse(Path(bootstrap.__file__).read_text(encoding="utf-8"))
    build_registry_fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "build_registry"
    )
    called = {
        node.func.attr
        for node in ast.walk(build_registry_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "resolve_workspace_root" in called, (
        "build_registry() 函数体内必须现调 resolve_workspace_root()（D-T3-2：不得写成模块级常量）"
    )

    # 反向腿：模块级**不得**出现解析结果的赋值（import 期算一次 = 不热生效）
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute) and sub.attr == "resolve_workspace_root":
                    raise AssertionError("模块级赋值里出现了 resolve_workspace_root ⇒ 不是函数体现调")


# ── 设置页键面（`D-回归检查` 的 7 处连带面 · `02` v1.0.8）──────────────────────


def test_a5a_settings_payload_carries_workspace_root_at_top_level() -> None:
    """**A5a 腿②**：`current_payload()` 的第 12 键 —— 落**顶层**，**不进 `agent` 段**。

    ⚠️ 本腿**只断键面、不触 HTTP**（⇒ 零 `.env` 风险，不需要 `isolated_dotenv`）。
    """
    from app.api.settings_schemas import current_payload

    body = current_payload()
    assert "workspace_root" in body, "current_payload() 缺第 12 个顶层键 workspace_root"
    assert isinstance(body["workspace_root"], str)
    assert "workspace_root" not in body["agent"], "workspace_root 不得塞进 agent 段（K-T3-4）"
    assert list(body.keys())[-1] == "workspace_root", "新键应排在末位（与 02 §7.6 的顺序一致）"


def test_a5a_settings_in_accepts_the_key_and_put_round_trips(
    client, isolated_dotenv, restore_settings, tmp_path: Path
) -> None:
    """**A5a 腿①**：`PUT /api/settings` 带 `workspace_root` ⇒ **`GET` 读回值一致**。

    🔴 **本腿走 HTTP ⇒ 必须同时挂 `isolated_dotenv` 与 `restore_settings`**（`K-回归检查`(a)(b)）：
    否则会① 写真 `.env`；② 把值留在进程级 `settings` 单例上（`monkeypatch` 撤不掉）。
    🔴 **红线 12**：值取 `tmp_path`（**绝不**用仓库内目录当根）。

    负控（`A5a` 的口子）：`SettingsIn` 的 `extra="ignore"` + PUT 的显式 if-chain
    ⇒ **只改 5 处**（漏 `settings_schemas.py` 字段 **或** 漏 `settings_routes.py` 分支）
    这两条任一 ⇒ 本腿**必红**（键被静默吞掉 / `updates` 为空提前 return）。
    """
    ws = str(tmp_path / "ws")
    resp = client.put("/api/settings", json={"workspace_root": ws})
    assert resp.status_code == 200, resp.text
    assert resp.json()["workspace_root"] == ws

    # 读回：键真的落盘了（不只内存）
    assert env_store.ENV_PATH.read_text(encoding="utf-8").count("WORKSPACE_ROOT") == 1

    read = client.get("/api/settings")
    assert read.status_code == 200, read.text
    assert read.json()["workspace_root"] == ws, "PUT 的值没被 GET 读回（键静默不生效）"


def test_a5a_clearing_the_root_round_trips_back_to_unconfigured(
    client, isolated_dotenv, restore_settings
) -> None:
    """**`K-回归检查`**：**清空根**这条路要单独验 —— 空串必须**真落盘**并读回为空。

    `settings_routes.py:556-558` 只在 `updates` 非空时落盘 ⇒ 「清空」必须走
    `updates["WORKSPACE_ROOT"] = ""` 这条分支（而不是「跳过」）。
    """
    resp = client.put("/api/settings", json={"workspace_root": ""})
    assert resp.status_code == 200, resp.text
    assert resp.json()["workspace_root"] == ""
    assert client.get("/api/settings").json()["workspace_root"] == ""


def test_a5b_state1_clearing_the_root_closes_the_registration_gate(
    client, isolated_dotenv, restore_settings, monkeypatch, tmp_path: Path
) -> None:
    """**A5b 态①（设置页侧）**：配根 → 注册门开；**清空根** → 注册门关（退化为态①）。

    端到端腿：注册门读的是**进程级单例**，`PUT` 后当场生效（无重启）。
    """
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(tmp_path))
    assert P.resolve_workspace_root() is not None, "配根态注册门应开"

    resp = client.put("/api/settings", json={"workspace_root": ""})
    assert resp.status_code == 200, resp.text
    assert settings.WORKSPACE_ROOT == "", "PUT 没把空值写进进程级单例"
    assert P.resolve_workspace_root() is None, "清空根后注册门必须当场关闭（无重启）"
    assert THREE_NAMES & _model_names() == set(), "清空根后三个文件工具必须退出模型清单"


# ── A5a′ / A5b′ 复核腿（阶段 1 的解析层件 · 只消费、不修改）───────────────────


def test_a5b_prime_review_leg_seam_is_still_usable(monkeypatch, tmp_path: Path) -> None:
    """**A5a′/A5b′ 复核腿**：`_read_configured_root` 仍是**真 seam**（阶段 1 的腿③ 依赖它）。

    阶段 3 改了**这一处**的实现（改读 `settings.WORKSPACE_ROOT`），**签名与模块属性名不变**
    ⇒ `tests/agent/test_tool_paths_t3.py:735/741/747` 的
    `monkeypatch.setattr(P, "_read_configured_root", …)` 必须**仍能打中**。
    本腿是那条腿的**同进程复核**（专属变异 `M-回归检查`：改名 ⇒ 本腿与阶段 1 腿③ 同时红）。

    ⚠️ 前提（逐字登记）：阶段 1 的腿① `test_a5a_unconfigured_root_returns_none`
    **无 monkeypatch** ⇒ 其绿**取决于测试进程的配置**；**本机 `.env` 未设 `WORKSPACE_ROOT`
    （出厂默认）**是它成立的前提。**不得**为让它变绿而改阶段 1 的件（`K-回归检查`）。
    """
    monkeypatch.setattr(P, "_read_configured_root", lambda: str(tmp_path))
    got = P.resolve_workspace_root()
    assert got is not None and str(got) == str(tmp_path.resolve()), f"seam 未生效：{got}"


def test_a5b_prime_review_leg_reads_the_settings_key(monkeypatch, tmp_path: Path) -> None:
    """**A5a′/A5b′ 复核腿（接线侧）**：`settings.WORKSPACE_ROOT` ⇒ `resolve_workspace_root()`。

    这是阶段 3 的**新增接线**：`_read_configured_root()` 从「恒返回空串」改为读本键。
    """
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(tmp_path))
    assert P._read_configured_root() == str(tmp_path)
    got = P.resolve_workspace_root()
    assert got is not None and str(got) == str(tmp_path.resolve())

    monkeypatch.setattr(settings, "WORKSPACE_ROOT", "   ")
    assert P.resolve_workspace_root() is None, "空白串必须等价于未配置"
