"""``回归检查`` 判据载体：**B5 四处同改**（字段 + 三处过滤/判定）。

## 本件覆盖的判据

| 判据 | 腿 | 用例 |
|---|---|---|
| **``A20``**（fail-closed：漏标字段 ⇒ 拒绝而非放行） | ① 漏标 ⇒ 拒 | test_a20_unlabeled_spec_is_blocked |
| | ② **显式 ``False`` ⇒ 放行**（**负控**） | test_a20_explicit_false_is_the_negative_control |
| | ③ 既有构造点逐字不变 | test_a20_every_construction_site_declares_the_field（AST 静态腿）|
| | | test_a20_existing_product_specs_are_not_blocked（运行时腿 · **只读**面）|
| | | test_a20_write_tool_spec_is_blocked（运行时腿 · **写工具正腿**，与上条**成对**）|
| **``A13d``**（写工具真进 ``for_model()``） | 方向腿 | test_a13d_write_capable_spec_stays_in_for_model |
| | 绝对锚（开关组合） | test_a13d_baseline_set_is_exact_per_switch_combo |
| | 差分腿 | test_a13d_differential_is_exactly_the_write_tool |
| **红线 4**（不得让「自动写」成为默认） | ``auto_executable`` 排除 | test_a13d_write_tool_never_enters_auto_executable |
| | | test_a13d_unlabeled_spec_never_enters_auto_executable |

## 为什么腿② 是负控（``D-回归检查`` 逐字）

没有腿②，把 :func:`app.agent.safety.is_blocked` 改成 ``return True`` 也能过腿①
（「一律拦」与「漏标才拦」在腿① 上**不可区分**）⇒ 腿② 是腿① 的**唯一**判别力来源。

## 为什么用 AST 静态腿（``B-B2`` 的落地形态）

``D-回归检查`` 的 ``B-B2`` 块要求给**全部** ``ToolSpec(`` 构造点逐处补 ``needs_confirm=False``。
其中三类构造点**运行时拿不到**：

- ``backend/app/agent/mcp_transport.py`` —— spec 由 **server 握手**动态生成（要起真 stdio 子进程）；
- ``backend/tests/agent/test_agent_safety.py`` / ``test_agent_audit.py`` / ``test_runner.py``
  —— spec 是**用例函数内的局部变量**，跨文件 import 不到。

⇒ 用 AST 把「全仓每一处 ``ToolSpec(`` 都显式写了 ``needs_confirm=``」变成**可判的静态事实**。
⚠️ 断言带**下限**（``len(sites) >= 25``）—— 否则「把构造点删光」也能让本腿**空扫恒绿**。

## 纪律

``backend/tests/agent/`` **没有 ``conftest.py``**（实测 ``Test-Path`` = ``False``）⇒ 夹具**自带**；
``tests/api/conftest.py`` 的 autouse 钉子对本目录**不生效**（与 ``test_tool_paths_t3.py`` 同款先例）。
``tmp_path`` 是 pytest 内建，直接可用（红线 12：工作区根一律指向 ``tmp_path``）。
"""

from __future__ import annotations

import ast
import pathlib
import types

import pytest

from app.agent import safety
from app.agent.registry import ToolRegistry, ToolSpec
from app.core.config import settings

#: 本阶段新增的写工具契约名（``D-回归检查`` ① 逐字冻结）。
#: ⚠️ **`回归检查` 起已注册**（`bootstrap.py:116-118`，在**工作区根门之内**）⇒ 本件相关腿的
#: 期望集合作 `回归检查` → `回归检查` 的**口径演进**（见 `FILE_FAMILY` 与腿 ③ 的 docstring）。
WRITE_NAME = "workspace_write"

#: ``bootstrap.build_registry`` 的常驻四件
BASE_FOUR = {"knowledge_search", "current_time", "list_documents", "read_document"}
#: 配根后才注册的三个只读文件工具
FILE_THREE = {"list_files", "read_file", "grep_files"}
#: 配根后注册的**文件工具族** = 只读三件 + 写工具（`回归检查` 起；见该腿 docstring 的口径演进）
FILE_FAMILY = FILE_THREE | {"workspace_write"}
#: ``web_enabled=True`` 才注册的联网两件
WEB_TWO = {"web_search", "fetch_page"}

#: 全仓 ``ToolSpec(`` 构造点的**实测下限**。
#: 🔴 **复跑读数（2026-09-26 · B 路独立 AST 扫 · `回归检查` 态）**：**实测 27 处**
#: （12 app + 1 scripts + 14 tests），漏标 **0** 处。
#: ⚠️ 下限取 **25**（留冗余，避免「每加一个构造点就要改常量」）；
#: 本常量的用途**只是**让「构造点被删光」时的**空扫恒绿**变成红，**不是**精确计数。
MIN_CONSTRUCTION_SITES = 25


@pytest.fixture(autouse=True)
def pin_web_off(monkeypatch):
    """联网默认关：本件不测联网面。"""
    monkeypatch.setattr(settings, "WEB_ENABLE", False)


def _model_names(web_enabled: bool = False) -> set[str]:
    """当前开关组合下 ``for_model()`` 的名单（走**真装配** ``build_registry``）。"""
    from app.agent.bootstrap import build_registry

    registry = build_registry(web_enabled=web_enabled)
    return {spec.name for spec in registry.for_model(web_enabled=web_enabled)}


def _spec(
    name: str,
    *,
    needs_confirm: bool | None = False,
    dangerous: bool = False,
) -> ToolSpec:
    """造一条真 ToolSpec（handler 不执行，本件只判声明面）。

    ⚠️ 四个关键词**逐字写全**（不用 ``**kwargs`` 兜）—— 本件自己的 AST 腿会扫这份文件：
    ``**kwargs`` 会让 ``needs_confirm`` 在 AST 上**不可见**，本件当场把自己的腿打红。
    """
    return ToolSpec(
        name=name,
        description="",
        parameters_schema={"type": "object", "properties": {}},
        handler=lambda: None,
        dangerous=dangerous,
        needs_confirm=needs_confirm,
    )


# ---------------------------------------------------------------------------
# A20 · 腿① 漏标 ⇒ 拒
# ---------------------------------------------------------------------------


def test_a20_unlabeled_spec_is_blocked():
    """**A20 腿①**：**没有** ``needs_confirm`` 字段的声明 ⇒ ``is_blocked`` **必须 True**。

    三种「未声明」形态都要拦：

    1. ``SimpleNamespace(name=...)`` —— 字段**整个不存在**（替身 / CubeLoop ``AgentTool`` 同形）；
    2. ``needs_confirm=None`` —— 显式哨兵（``ToolSpec`` 的默认值就是它）；
    3. ``object()`` —— 连 ``name`` 都没有的最极端替身。
    """
    bare = types.SimpleNamespace(name="probe_write")
    # 先自证靶子有效：它**没有** dangerous 字段 ⇒ 不是被 dangerous 分支兜住的
    assert hasattr(bare, "dangerous") is False, "靶子必须走「needs_confirm 取不到」这一支"
    assert hasattr(bare, "needs_confirm") is False

    assert safety.is_blocked(bare) is True, "字段整个取不到 ⇒ 必须拦（fail-closed）"
    assert safety.is_blocked(types.SimpleNamespace(name="x", needs_confirm=None)) is True
    assert safety.is_blocked(object()) is True


def test_a20_explicit_false_is_the_negative_control():
    """**A20 腿②（负控）**：显式 ``needs_confirm=False`` ⇒ **不拦**。

    ⚠️ 没有这条，把 ``is_blocked`` 改成 ``return True`` 也能过腿①。
    """
    assert safety.is_blocked(types.SimpleNamespace(name="ro", needs_confirm=False)) is False
    assert safety.is_blocked(_spec("ro", needs_confirm=False)) is False
    # 🔴 **三值契约**（`needs_confirm: bool | None`）—— 三档**互斥穷尽**，逐档钉死：
    #   · `False` = 只读 ⇒ **放行**（上面两条）；
    #   · `None` / 取不到 = **未声明** ⇒ **拦**（腿① 的靶心，`A20` 字面）；
    #   · `True` = **已声明要确认** ⇒ **放行**（由**凭证闸**在 handler 内处理，不是这里拦）。
    # ⚠️ 第三条容易被写成 `is True`（“声明要确认 ⇒ 拦掉”）—— 那是**错的**：
    #   在这里拦 ⇒ handler 永不执行 ⇒ 写工具的 8 条判据**结构性不可达**。
    assert safety.is_blocked(types.SimpleNamespace(name="w", needs_confirm=True)) is False


def test_a20_dangerous_direction_is_verbatim_unchanged():
    """**A20 腿③ 前置**：``dangerous`` 的方向**逐字未动**（``dangerous=True`` 照旧拦）。

    本笔**只**对 ``needs_confirm`` 一维做反向 fail-closed。
    """
    assert safety.is_blocked(types.SimpleNamespace(name="d", dangerous=True)) is True
    assert safety.is_blocked(_spec("d", dangerous=True, needs_confirm=False)) is True


# ---------------------------------------------------------------------------
# A20 · 腿③ 既有构造点逐字不变（AST 静态腿 + 运行时腿）
# ---------------------------------------------------------------------------


def _tool_spec_sites() -> list[tuple[str, int, ast.Call]]:
    """全仓 ``ToolSpec(`` 构造点（AST 精确匹配 Call，**不误收注释/docstring**）。"""
    root = pathlib.Path(__file__).resolve().parents[2]  # backend/
    out: list[tuple[str, int, ast.Call]] = []
    for path in root.rglob("*.py"):
        if ".venv" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name == "ToolSpec":
                out.append((str(path.relative_to(root)), node.lineno, node))
    return out


def test_a20_every_construction_site_declares_the_field():
    """**A20 腿③（AST 静态腿）**：全仓每处 ``ToolSpec(`` 都**显式**写了 ``needs_confirm=``。

    覆盖 ``B-B2`` 点名的**运行时拿不到**的构造点（MCP 握手路 + 用例内局部 spec）。
    """
    sites = _tool_spec_sites()
    assert len(sites) >= MIN_CONSTRUCTION_SITES, (
        f"构造点只有 {len(sites)} 处（下限 {MIN_CONSTRUCTION_SITES}）"
        "—— 过少说明扫描面坏了（空扫会让本腿恒绿）"
    )

    missing = [
        (rel, line)
        for rel, line, call in sites
        if "needs_confirm" not in {kw.arg for kw in call.keywords}
    ]
    assert missing == [], f"以下构造点漏标 needs_confirm= ⇒ 会被 fail-closed 静默拦下：{missing}"


def test_a20_existing_product_specs_are_not_blocked(tmp_path, monkeypatch):
    """**A20 腿③（运行时腿）**：既有**只读**产品工具（含配根后三个只读文件工具）**逐字不被拦**。

    🔴 **口径随 `回归检查` 演进（如实登记，两处）**：

    **其一**：本腿在 `回归检查` 时断「registry 里全部产品工具都不被拦」—— 那时写工具尚未注册。
    `回归检查` 落地后 `workspace_write` 与只读三件**列在同一道注册门之内**（`bootstrap.py:116-118`），
    ⇒ 本腿必须覆盖到**它也在场**这一事实（下面的反向自证），否则会退化成空判据。

    **其二（更要紧）**：`safety.is_blocked` 对 `needs_confirm=True` 的语义是**放行**，
    **不是**拦下 —— 见 `safety.py` 的 docstring：
    `None`/取不到 = 「**未声明**」⇒ 拦（`A20` 的靶心）；
    `True` = 「**已声明要确认**」⇒ **不在这里拦**，交由**凭证闸**
    （`_confirm.verify_write_confirm`）在 handler 内部决定放行 / 要求确认。
    ⇒ ⚠️ 若在这里拦掉 `True`，写工具的 handler **一次都不会被调用**，
    而 `A7`/`A8`/`A9`/`A12a`/`A21`/`A22`/`A23`/`A28` **全部**发生在 handler 内部
    ⇒ **8 条判据结构性不可达**（不是红，是「测不到」）。

    ⇒ 本腿的正确形态 = `blocked == []`（**既有产品工具一律不被执行侧闸门拦**），
    并由下一条正腿 `test_a20_write_tool_spec_passes_the_safety_gate` 钉住
    「写工具**确实在场**且**确实不被拦**」——两条成对。
    """
    from app.agent.bootstrap import build_registry

    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(workspace))

    registry = build_registry(web_enabled=True)
    specs = registry.list(include_dangerous=True)
    blocked = [spec.name for spec in specs if safety.is_blocked(spec)]

    assert blocked == [], (
        "既有产品工具**一律不得**被执行侧闸门拦下（写工具由凭证闸管，不在这里拦）"
        f"（实测被拦：{blocked}）"
    )
    # 反向自证：**写工具确实在**注册表里 —— 否则上式在「写工具压根没注册」时也成立（空判据）。
    names = {spec.name for spec in specs}
    assert WRITE_NAME in names, f"写工具不在注册表 ⇒ 上式退化为空判据：{sorted(names)}"
    assert FILE_THREE <= names, f"配根后三个只读文件工具必须在：{sorted(names)}"


def test_a20_write_tool_spec_passes_the_safety_gate(tmp_path, monkeypatch):
    """**A20 腿③（写工具正腿 · 成对）**：`workspace_write` **在场**且**不被执行侧闸拦**。

    🔴 本条与上一条**成对**，缺一即不可判：

    · 上一条断 `blocked == []` —— 单看它，「**一律不拦**」也能过；
    · 本条断「写工具**确实在**注册表里」（反向自证，防空判据）
      ＋「它 `needs_confirm is True`」（档 2 的声明面）
      ＋「`is_blocked` **放行**它」（语义：已声明要确认 ⇒ 交凭证闸，不在这里拦）。

    ⚠️ **为什么「放行」才是对的**（2026-09-26 实测更正，如实登记）：
    本函数之上还有**凭证闸**（`_confirm.verify_write_confirm`）在 handler 内部。
    若这里把 `True` 也拦掉 ⇒ 写工具 handler **永不执行** ⇒
    `A7`/`A8`/`A9`/`A12a`/`A21`/`A22`/`A23`/`A28` **全部结构性不可达**。
    `A20` 的字面是「**漏标字段** ⇒ 拒绝而非放行」—— 靶心是 **`None` / 取不到**，
    **不是**「已声明的写工具」。⇒ 本腿钉的正是这条边界。
    """
    from app.agent.bootstrap import build_registry

    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(workspace))

    registry = build_registry(web_enabled=True)
    spec = registry.get(WRITE_NAME)
    assert spec.needs_confirm is True, (
        f"写工具必须显式声明 needs_confirm=True（实测 {spec.needs_confirm!r}）"
    )
    assert safety.is_blocked(spec) is False, (
        "写工具被**执行侧闸**拦下了 ⇒ handler 永不执行 ⇒ 8 条判据结构性不可达；"
        "「已声明要确认」应由**凭证闸**处理，不是在这里拦"
    )


def test_a20_mcp_static_specs_are_not_blocked():
    """**A20 腿③（MCP 静态路）**：``mcp.build_specs`` 产出的 spec 不被拦。"""
    from app.agent.tools.mcp import McpPack, McpTool
    from app.agent.tools import mcp as mcp_tools

    packs = [
        McpPack(
            slug="p1",
            name="包1",
            transport="stdio",
            command=("noop",),
            tools=(McpTool(name="read_file"),),
        )
    ]
    specs = mcp_tools.build_specs(packs, call_tool=None)
    assert specs, "packs 非空却零 spec ⇒ 本腿空扫"
    assert [s.name for s in specs if safety.is_blocked(s)] == []


# ---------------------------------------------------------------------------
# A13d · 写工具真进 for_model()（差分口径，D-回归检查）
# ---------------------------------------------------------------------------


def test_a13d_write_capable_spec_stays_in_for_model():
    """**A13d 方向腿**：``needs_confirm=True`` 的写工具**照常进** ``for_model()``（单⑫ 档 (a)）。

    ⚠️ 本腿是 ``M-回归检查`` / ``M8`` 的**唯一非空靶**：把写工具从 ``for_model`` 滤掉 ⇒ 本腿必红。
    """
    registry = ToolRegistry()
    registry.register(_spec("probe_write", needs_confirm=True))
    registry.register(_spec("probe_read", needs_confirm=False))

    assert [s.name for s in registry.for_model(web_enabled=False)] == [
        "probe_write",
        "probe_read",
    ]
    assert [s.name for s in registry.for_model(web_enabled=True)] == [
        "probe_write",
        "probe_read",
    ]


@pytest.mark.parametrize("web_enabled", [False, True], ids=["web_off", "web_on"])
def test_a13d_baseline_set_is_exact_per_switch_combo(web_enabled):
    """**A13d 绝对锚**：根**未配**态下清单与基数逐条对得上（``D-回归检查`` 防假红/假绿）。

    实测基数：``web_enabled=False`` ⇒ **4**；``web_enabled=True`` ⇒ **6**。
    """
    expected = BASE_FOUR | (WEB_TWO if web_enabled else set())
    assert _model_names(web_enabled) == expected


def test_a13d_configured_root_adds_exactly_the_file_family(tmp_path, monkeypatch):
    """**A13d 绝对锚（配根态）**：配根 ⇒ 名单 = 根未配态 **+ 恰好文件工具族**。

    🔴 **口径随 回归检查 演进（如实登记）**：本腿在 ``回归检查`` 时写的是「+ 恰好**三个只读**文件工具」
    —— 那时写工具尚未注册（``D-回归检查`` ⑧ 定档「第 5 笔才进 ``for_model()``」）。
    ``回归检查`` 落地后写工具与只读三件**列在同一道注册门之内**（``bootstrap.py``），
    ⇒ 本腿的期望集合**必须**随之扩成「只读三件 + 写工具」。

    ⚠️ 这不是放水：本腿仍然断**恰好相等**（``== FILE_FAMILY``），
    「配根后多出任何**第四个**工具」照样红；且下面另有一条反向腿钉「常驻四件不被挤掉」。
    """
    before = _model_names(web_enabled=False)
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(workspace))
    after = _model_names(web_enabled=False)

    assert before == BASE_FOUR, f"根未配态应恰好是常驻四件：{sorted(before)}"
    assert after - before == FILE_FAMILY, f"配根只许加文件工具族：{sorted(after - before)}"
    assert before - after == set(), "配根不得把任何既有工具挤掉"


def test_a13d_differential_is_exactly_the_write_tool(monkeypatch, tmp_path):
    """**A13d 差分腿**（``D-回归检查`` 写死口径）：**改后清单 == 改前清单 + 写工具名集**。

    「改前清单」构造 = 把 ``ToolRegistry.register`` 换成「**跳过写工具**」的桩
    （⚠️ 桩自证：``WRITE_NAME not in baseline``，否则差分恒空 ⇒ **假绿**）。

    🔴 **第 1 笔的预期态**：``workspace_write`` 在 ``回归检查`` 才注册（``D-回归检查`` ⑧）
    ⇒ 此刻 ``after == baseline``、**差集为空**。本条**如实断言**该差分
    （而不是写成 ``in (set(), {写工具})`` 的宽通过式 —— 那会让整条腿恒真）。
    """
    from app.agent.bootstrap import build_registry

    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(workspace))

    original_register = ToolRegistry.register

    def register_without_write(self, spec):  # noqa: ANN001
        if spec.name == WRITE_NAME:
            return
        original_register(self, spec)

    with monkeypatch.context() as patch_ctx:
        patch_ctx.setattr(ToolRegistry, "register", register_without_write)
        baseline = _model_names(web_enabled=False)

    assert WRITE_NAME not in baseline, "桩失效：基线里竟有写工具 ⇒ 差分恒空（假绿）"

    after = _model_names(web_enabled=False)
    registered = {
        spec.name for spec in build_registry(web_enabled=False).list(include_dangerous=True)
    }
    expected_diff = {WRITE_NAME} if WRITE_NAME in registered else set()

    assert after - baseline == expected_diff
    assert baseline - after == set(), "差分只许「增」，不得把既有工具挤掉"


# ---------------------------------------------------------------------------
# 红线 4 · 写工具（与漏标工具）永不自动执行
# ---------------------------------------------------------------------------


def test_a13d_write_tool_never_enters_auto_executable():
    """**红线 4**：``needs_confirm=True`` 的工具**不在** ``auto_executable()``。"""
    registry = ToolRegistry()
    registry.register(_spec("probe_write", needs_confirm=True))
    registry.register(_spec("probe_read", needs_confirm=False))

    assert [s.name for s in registry.auto_executable()] == ["probe_read"]


def test_a13d_unlabeled_spec_never_enters_auto_executable():
    """**红线 4（fail-closed 面）**：``needs_confirm=None``（漏标）**也不在** ``auto_executable``。

    🔴 本条钉的是一处**真实踩过的坑**：判据若写成 ``not s.needs_confirm``，
    则 ``not None == True`` ⇒ **漏标工具反而进了自动执行白名单** ——
    与 ``auto_executable`` 自己 docstring 的「漏标 ⇒ 排除」**相反**，
    正是 B5 要防的「**漏标 ⇒ 静默变自动写**」。⇒ 判据必须是 ``s.needs_confirm is False``。
    """
    registry = ToolRegistry()
    registry.register(_spec("probe_unlabeled", needs_confirm=None))
    registry.register(_spec("probe_read", needs_confirm=False))

    names = {s.name for s in registry.auto_executable()}
    assert "probe_unlabeled" not in names, "漏标工具绝不得进自动执行白名单（fail-closed）"
    assert names == {"probe_read"}
