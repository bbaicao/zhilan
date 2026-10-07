"""`回归检查` 判据载体：**D1 只读可见面** —— 判据 **`回归检查` ①②③④**（阶段 6 · 可见面）。

## 本件判什么（四条腿，逐条可判真假）

| 腿 | 判据 | 用例 |
|---|---|---|
| ① | **帧面**：读文件工具的轮次里，`agent` 帧步骤项 `args["path"]` 是**相对工作区根**的形态，且**不含根绝对前缀** | `test_a24_leg1_*` |
| ② | **界面面**（**静态**）：`AgentStepsBlock.tsx` 源码里**真存在**渲染分支，能把该字段显示出来 | `test_a24_leg2_*` |
| ③ | **负控**：**没有**读文件的轮次里，步骤项 `args` **不得**出现该字段 | `test_a24_leg3_*` |
| ④ | **零材料红线**：该字段**不进** `materials`（`回归检查` 的复核腿） | `test_a24_leg4_*` |

## 🔴 腿① 判的是「**归一**」，不是「字段存在」

「读了哪个文件」这个字段**本就在** `step.args` 里（`runner.py` 的 `add_step("tool", …, args=args)`）
⇒ 一条「`args` 里有 `path` 键」的断言在**改前就是绿的**（判别力 0，属 `K-回归检查` 那一类）。
本笔的真交付是把**根内绝对路径**化成相对形态（`_paths.describe`）⇒ 腿① 的靶面 = **绝对前缀不在**：

- 正控：工作区根**本身是绝对路径**（`tmp_path` 给的），且断言 `str(ws)` **不在**步骤载荷里；
- 反向自证：`str(ws)` 这个串**确实**出现在**别处**（观察文本里 —— 工具自己的文案带不带根是
  `回归检查` 的口径，本笔不改），⇒ 证明「不在载荷里」不是因为**这个串在本轮压根没出现过**。

## 🔴 腿② 是**静态腿**（`D-回归检查`），措辞不得越界

本仓**无 JS 测试框架**（`frontend/package.json` 只有 `dev` / `build` / `preview`；`frontend` 下
`*.test.*` 实测 0 件）⇒ 界面面腿**只可**「Python 抽 `.tsx` 源码文本」判（先例
`tests/pipeline/test_frontend_whitelist_t3_2.py`）。
**本件不声称「运行期渲染已验证」** —— 它证明的是「渲染分支在源码里存在且取的是同一个键」。
⇒ 腿② **不得**作为任何变异的**唯一**靶腿（本件的 `M-回归检查` 另有腿①③）。

## 🔴 两条路径都要测：只改一条会「偏科」

帧构造点有**两个**：`_steps_for`（ReAct）与 `_tool_frames`（P&E）。同一个问题在两种
`agent_mode` 下外观不同，是必须避免的（`runner.py` 的既有口径「界面看不出两条路径的差别」）。
⇒ ReAct 用 `FauxProvider` 驱动真决策循环、P&E 用 `RecordingModel` 驱动计划调用，**两腿各一条**。

## 纪律

- 工作区根一律 `tmp_path`（**红线 12**：不得指向仓库根 / 数据根 / dist / 证据目录）；
- `backend/tests/agent/` **无 `conftest.py`**（实测）⇒ 夹具**自带**（先例 `test_file_tools_t3_3.py:61`）；
- `settings.WORKSPACE_ROOT` 在测试进程里**默认为空** ⇒ 不配根会让工具在「未配置」分支**提前返回**
  （`K-回归检查`）⇒ 本件所有腿**必须先挂上**已配的根（`workspace_root` 夹具），并在夹具里**断言根可解析**；
- 所有断言**一律无条件**（`K-回归检查`：`if 条件: assert …` 是死代码）；取值面不足时用**显式 `assert`**
  把「本腿的前提」也钉住（前提不成立 ⇒ **红**，不是静默跳过）。

## 📌 变异 `M-回归检查` 记录（**本轨新建** · 锚点在基线上不存在）

🔴 **必须先说清的一件事**：本笔的**归一逻辑是新建的** —— 基线 `b8b9d9a` 上
`_tool_args_for_steps` / `READ_FILE_TOOLS` **都不存在**，`_paths.describe` 更是**零调用点**。
⇒ `M-回归检查` 不可能像常规变异那样「在基线代码上找个旧锚点打一下」；它的锚点**只能指本笔新增的行**。
⚠️ **不得**把锚点打在「模型原始 `args.path`」上（`runner.py` 里传参给 `add_step` 的那处）：
那个值**本来就可能是根内绝对路径**，打上去**不会红**（归一在别的行做）——那是**惰性变异**。

| # | 锚点（**指名新增字段的赋值 / 判定行**） | 变异形态 | 实际红点（靶腿） |
|---|---|---|---|
| **`M-回归检查`** | `runner.py:476` = `relative = _paths.describe(Path(root), resolved)`（**新增**归一赋值行） | 改成 `relative = str(resolved)`（去掉归一，直接落绝对路径） | **4 红**：`test_a24_leg1_react_*` / `test_a24_leg1_plan_execute_*` / `test_a24_leg1_other_arg_keys_*` / `test_a24_leg1_unconfigured_root_*`（全部 `回归检查` ①） |
| **`M-回归检查`** | `runner.py:463` = `if tool_name not in READ_FILE_TOOLS or not isinstance(args, dict):`（**新增**白名单判定行） | 去掉白名单（任何工具都归一） | **1 红**：`test_a24_leg3_non_read_round_has_no_path_field`（`回归检查` ③） |
| **`M-回归检查`** | `AgentStepsBlock.tsx` = `return typeof value === "string" && value.trim() ? value : "";`（**新增**取值 return 行） | 改成 `return "";`（把取值写空） | **1 红**：`test_a24_leg2_tsx_has_a_read_path_render_branch`（`回归检查` ②，**非唯一靶腿** —— ①③ 另有 `M-回归检查`/`b`） |

**三段式齐**：三处均 **改前绿 → 变异红 → 还原绿**；还原后 `grep MUTANT` 在 `backend/app/agent/runner.py`
与 `frontend/src/**` 均 **零命中**（自证无残留）；`frontend/.build-manifest` 的 `src_tree_hash`
还原后与变异前**逐字相同**（`193a599a…e3c5`）⇒ 还原是逐字节的，不是「看起来差不多」。
⚠️ **`M-回归检查` 的那条红点是被补出来的**（如实登记）：初版腿② 只断「函数体里有 `return`」，
`return ""` 照样有 `return` ⇒ 实测**没红**（假绿）；改为断「三元 return 的真值分支必须吐出取值变量」
之后才真红。这就是「界面面腿不得作唯一靶腿」的现身。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.agent import runner
from app.agent.registry import ToolSpec
from app.agent.runner import AgentTurn
from app.agent.tools import _paths as P
from app.agent.tools import grep_files, list_files, read_file
from app.agent.types import ToolObservation
from app.core.config import settings
from tests.agent.test_runner import (
    RecordingModel,
    collect,
    faux_model,
    registry_with,
    says,
    tool_call,
)
from tests.agent.test_runner import run_plan as _run_plan

#: 读文件类三件（本笔的靶面）。
READ_TOOLS = ("read_file", "list_files", "grep_files")

#: 界面载体（**相对模块定位**：不写死仓根，也不依赖 cwd）。
#: 本文件在 <repo>/backend/tests/agent/ ⇒ parents[3] = <repo>。
TSX_PATH = (
    Path(__file__).resolve().parents[3] / "frontend" / "src" / "components" / "AgentStepsBlock.tsx"
)

#: 非读文件工具（负控腿：它即便带 path 形态的键也**不得**被归一 —— 白名单是写死的）。
PROBE_NAME = "write_probe_tool"


# ── 夹具（自带；tests/agent/ 无 conftest）───────────────────────────────────


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    """一个**真**工作区根（红线 12：一律 tmp_path），并接到 settings.WORKSPACE_ROOT。

    ⚠️ 本夹具**同时**承担「已配根」这件事（而不是另加一条 monkeypatch 腿）：
    漏挂它的腿会静默走到工具的「根未配置」分支 ⇒ 观察量错位（K-回归检查）。
    末尾那句 assert 把这条前提变成**可见的**失败。
    """
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "a.txt").write_text("alpha\nneedle\n", encoding="utf-8")
    (ws / "sub" / "b.txt").write_text("needle here\n", encoding="utf-8")
    settings.WORKSPACE_ROOT = str(ws)
    assert P.resolve_workspace_root() is not None, "根没接上 ⇒ 本件所有腿都会落在「未配置」分支"
    try:
        yield ws
    finally:
        settings.WORKSPACE_ROOT = ""


def _run(coro):
    """同步跑一次协程（照 test_file_tools_t3_3.py::_call 的同款做法）。"""
    return asyncio.run(coro)


def _tool_frames_of(frames: list[dict], tool_name: str) -> list[dict]:
    """取该轮的 kind == "tool" 且点名该工具的步骤项。"""
    return [f for f in frames if f["kind"] == "tool" and f.get("tool") == tool_name]


def _balanced_span(source: str, start: int, opener: str, closer: str) -> str:
    """从 `start`（指向 `opener`）起，按**括号配平**截出到配对 `closer` 的一段源码。

    ⚠️ 不能用正则 `.*?` 直接抠：`.tsx` 里嵌套着对象字面量 / 模板串，非贪婪正则会**提前收口**，
    截出来的片段不完整 ⇒ 后面的断言变成「在一段残缺文本里找子串」（假绿或假红都可能）。
    纯**配对**扫描（顺序数括号）不受嵌套影响，也不需要 TS 解析器。

    找不到起始 `opener` ⇒ 直接断言失败（**不**返回空串 —— 空串会让下游断言静默变弱）。
    """
    assert start >= 0, f"起始锚点不存在：{opener}"
    assert start < len(source) and source[start] == opener, (
        f"起始锚点没落在 {opener!r} 上：{source[start:start + 40]!r}"
    )
    depth = 0
    for index in range(start, len(source)):
        char = source[index]
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"括号未配平（源码被截断？）：{source[start:start + 80]!r}")


def _function_body(source: str, name: str) -> str:
    """截出 `function <name>(…) {…}` 的**函数体**（含大括号）。

    断言只在该函数体内找取值路径 ⇒ 「源码别处恰好也有 args.path」蒙混不过去。
    """
    match = re.search(rf"function\s+{re.escape(name)}\s*\(", source)
    assert match is not None, f"源码里找不到专属函数 {name} ⇒ 渲染分支不存在"
    brace = source.index("{", match.end())
    return _balanced_span(source, brace, "{", "}")


def _const_set_body(source: str, name: str) -> str:
    """截出 `const <name> = new Set([...])` 的**元素列表**（`[...]` 之内，含方括号）。

    用于判专属白名单**恰含**哪些名字（而不是判「这三个字符串在文件里出现过」——
    后者在注释里提一句就能满足，是空靶）。
    """
    match = re.search(rf"const\s+{re.escape(name)}\s*=\s*new\s+Set\s*\(", source)
    assert match is not None, f"源码里找不到专属白名单常量 {name}"
    bracket = source.index("[", match.end())
    return _balanced_span(source, bracket, "[", "]")


# ── 回归检查 腿①：帧面（ReAct + P&E 两条路径）────────────────────────────────


@pytest.mark.asyncio
async def test_a24_leg1_react_frames_carry_relative_read_path(workspace_root: Path) -> None:
    """**A24 腿①（ReAct 路径）**：read_file 的步骤载荷里路径是**相对根**的形态。

    构造：模型点名 read_file 并传**根内绝对路径**（str(ws/"a.txt")）。
    ⇒ 归一后必须恰好是 "a.txt"，且 str(ws) **不得**出现在载荷里。

    ⚠️「字段本就存在」不等于本腿恒真：改前载荷**逐字**就是绝对路径 ⇒ 本腿必红
    （这正是 M-回归检查 的靶面）。
    """
    ws = workspace_root
    model = faux_model([tool_call("read_file", {"path": str(ws / "a.txt")}), says("读完了")])
    turn = AgentTurn(mode="react")
    frames = await collect(
        turn,
        question="看下 a.txt",
        model=model,
        registry=registry_with(read_file.build_spec()),
    )

    tool_frames = _tool_frames_of(frames, "read_file")
    assert len(tool_frames) == 1, f"本轮应有且仅有 1 条 read_file 步骤：{frames}"
    args = tool_frames[0]["args"]
    # 前提钉死：工具**真被调用了**（否则下面两句是在断一个没发生过的调用）
    assert isinstance(args, dict), f"步骤载荷形状变了：{args!r}"
    assert args.get("path") == "a.txt", (
        f"读文件步骤载荷未归一成相对根的形态，实测：{args.get('path')!r}"
    )
    assert str(ws) not in repr(args), f"步骤载荷泄露了工作区根的绝对前缀：{args!r}"
    # 反向自证：根串**确实**在本轮出现过（在**观察面之外**的另一处）⇒「不在载荷里」不是因为
    # 「这个串本轮压根没出现过」。工具自己的观察文本带不带根由 回归检查 定档，本笔不改它。
    assert any(str(ws) in str(f.get("detail") or "") for f in frames), (
        f"根串本轮一次都没出现 ⇒ 上面的「不在载荷里」不可采信：{frames}"
    )


@pytest.mark.asyncio
async def test_a24_leg1_plan_execute_frames_carry_relative_read_path(workspace_root: Path) -> None:
    """**A24 腿①（P&E 路径）**：list_files 的步骤载荷里路径同样是**相对根**的形态。

    为什么必须单独一条：帧构造点有**两个**（_steps_for / _tool_frames），
    只改一条 ⇒ 同一个问题在 plan_execute 模式下照旧泄露绝对前缀（「偏科」）。
    """
    ws = workspace_root
    plan = json.dumps(
        {"steps": [{"goal": "列目录", "tool": "list_files", "args": {"path": str(ws)}}]},
        ensure_ascii=False,
    )
    model = RecordingModel([plan])
    turn = AgentTurn(mode="plan_execute")
    frames = await _run_plan(
        turn,
        question="列一下根目录",
        model=model,
        registry=registry_with(list_files.build_spec()),
    )

    tool_frames = _tool_frames_of(frames, "list_files")
    assert len(tool_frames) == 1, f"本轮应有且仅有 1 条 list_files 步骤：{frames}"
    args = tool_frames[0]["args"]
    assert isinstance(args, dict), f"步骤载荷形状变了：{args!r}"
    # 根自身归一成 "."（与 _paths.describe 对「根 == 候选」的既定口径一致）
    assert args.get("path") == ".", (
        f"P&E 步骤载荷未归一成相对根的形态，实测：{args.get('path')!r}"
    )
    assert str(ws) not in repr(args), f"P&E 步骤载荷泄露了工作区根的绝对前缀：{args!r}"


@pytest.mark.asyncio
async def test_a24_leg1_other_arg_keys_are_preserved(workspace_root: Path) -> None:
    """**A24 腿①（不破坏既有键）**：归一只动路径键，**其余键逐字不动**。

    判别力：一个「把 args 整个换成 {"path": rel}」的实现能让上面两条腿全绿，
    却把「模型当时传了什么」从界面上抹掉了（grep_files 的 pattern 正是读的人最需要看的
    另一项）。本腿把它钉死。
    """
    ws = workspace_root
    model = faux_model(
        [
            tool_call("grep_files", {"pattern": "needle", "path": str(ws / "sub"), "max_hits": 7}),
            says("搜完了"),
        ]
    )
    turn = AgentTurn(mode="react")
    frames = await collect(
        turn,
        question="搜 needle",
        model=model,
        registry=registry_with(grep_files.build_spec()),
    )

    tool_frames = _tool_frames_of(frames, "grep_files")
    assert len(tool_frames) == 1, f"本轮应有且仅有 1 条 grep_files 步骤：{frames}"
    args = tool_frames[0]["args"]
    assert args.get("path") == "sub", f"路径未归一：{args.get('path')!r}"
    assert args.get("pattern") == "needle", f"非路径键被改动（pattern）：{args!r}"
    assert args.get("max_hits") == 7, f"非路径键被改动（max_hits）：{args!r}"
    assert str(ws) not in repr(args), f"载荷泄露了工作区根的绝对前缀：{args!r}"


# ── 回归检查 腿①：**根未配置** ⇒ 归一不发生（把腿① 的前提变成被断的对象）────────


@pytest.mark.asyncio
async def test_a24_leg1_unconfigured_root_means_no_normalization(workspace_root: Path) -> None:
    """**A24 腿①（根未配置 · 负控正证）**：根未配置 ⇒ **原样放行**，绝不硬编一个形态。

    🔴 为什么必须有这条（审计点 ①）：腿① 的前提是「根**已配**」。本机开发态
    `settings.WORKSPACE_ROOT` 默认为空 ⇒ `_tool_args_for_steps` 在 `root is None` 处**提前返回**，
    归一逻辑**根本不走**。若只在夹具里配根、却不断「未配根时会发生什么」，
    「归一逻辑」与「根已配」这两件事就纠缠在一条腿上 —— 前者坏了也可能被后者掩盖。

    本腿把那个分支**单独**变成被断的对象：同样是根内绝对路径，未配根时**逐字保留**。
    ⚠️ 本腿**不**走工具（工具在未配根时自己就返回失败观察，那会掩盖步骤载荷这一层）——
    直接判步骤载荷的装配口，靶子更准。
    """
    ws = workspace_root
    absolute = str(ws / "a.txt")

    # 正控：根已配 ⇒ 归一**发生**（先把「配根时确实会归一」钉住，否则下面的负控无意义）
    configured = runner._tool_args_for_steps("read_file", {"path": absolute})
    assert configured["path"] == "a.txt", f"根已配时未归一：{configured!r}"

    # 负控：根未配 ⇒ **原样返回**（不得硬编相对形态、不得抛）
    settings.WORKSPACE_ROOT = ""
    assert P.resolve_workspace_root() is None, "根未配置的前提不成立"
    unconfigured = runner._tool_args_for_steps("read_file", {"path": absolute})
    assert unconfigured is not None, "根未配时归一炸穿了（I5：必须原样放行）"
    assert unconfigured["path"] == absolute, (
        f"根未配时不该归一（没有根就没有「相对根」可言）：{unconfigured!r}"
    )


# ── 回归检查 腿③：负控（没有读文件的轮次不得出现该字段）──────────────────────


@pytest.mark.asyncio
async def test_a24_leg3_non_read_round_has_no_path_field(workspace_root: Path) -> None:
    """**A24 腿③（负控）**：本轮调的是**非读文件**工具 ⇒ 该工具的载荷**逐字不动**。

    为什么需要它（M-回归检查 的第二档）：若归一实现写成「**任何**工具的载荷都归一 path」，
    腿①照旧全绿，而界面上每类工具步骤都会多出一行**被改过**的值 —— 那比不显示更坏。
    本腿把「归一**只**作用在读文件三件上」钉死。

    ⚠️ 替身工具的 args 里**本来就有** path 键（照写工具的入参形状造的）
    ⇒ 本腿断的是「**没被归一**」，不是「键不存在」——这才是有判别力的那一条。
    """
    ws = workspace_root

    class ProbeArgs(BaseModel):
        path: str = "somewhere/else.md"

    async def handler(args):  # noqa: ANN001
        return ToolObservation(text="替身工具跑完了")

    probe = ToolSpec(
        name=PROBE_NAME,
        description="非读文件工具（负控替身）",
        parameters_schema=ProbeArgs.model_json_schema(),
        handler=handler,
        dangerous=False,
        parameters=ProbeArgs,
        needs_web=False,
        needs_confirm=False,
    )
    model = faux_model([tool_call(PROBE_NAME, {"path": str(ws / "somewhere.md")}), says("好了")])
    turn = AgentTurn(mode="react")
    frames = await collect(turn, question="随便", model=model, registry=registry_with(probe))

    tool_frames = _tool_frames_of(frames, PROBE_NAME)
    assert len(tool_frames) == 1, f"本轮应有且仅有 1 条替身工具步骤：{frames}"
    args = tool_frames[0]["args"]
    assert isinstance(args, dict) and "path" in args, f"负控前提不成立（替身工具未带 path）：{args!r}"
    # 关键：**非读文件工具**的载荷逐字不动 —— 绝对路径照旧（本笔不越界去归一它）
    assert args["path"] == str(ws / "somewhere.md"), f"非读文件工具的载荷被归一了（越界）：{args!r}"


@pytest.mark.asyncio
async def test_a24_leg3_no_tool_round_has_no_path_field(workspace_root: Path) -> None:
    """**A24 腿③（负控 · 无工具轮）**：本轮**一次工具都没调** ⇒ 不可能有该字段。

    与上一条是两档：上一条断「非读文件工具不归一」，本条断「不调用就什么都没有」
    （防「每轮恒定塞一个字段」的实现）。
    """
    model = faux_model([says("不需要工具")])
    turn = AgentTurn(mode="react")
    frames = await collect(
        turn,
        question="你好",
        model=model,
        registry=registry_with(read_file.build_spec()),
    )

    assert _tool_frames_of(frames, "read_file") == [], f"本轮不该有工具步骤：{frames}"
    for frame in frames:
        assert "path" not in (frame.get("args") or {}), f"无工具轮出现了路径字段：{frame!r}"


# ── 回归检查 腿④：零材料红线（本阶段只复核，载体沿用 回归检查）─────────────────


def test_a24_leg4_read_path_never_becomes_material(workspace_root: Path) -> None:
    """**A24 腿④**：三工具的 ToolObservation.materials **恒为 []**（红线 8 · 回归检查 复核）。

    K-回归检查：「字段可见」与「文件内容进资料区」是两件事 —— 本腿是那条边界的钉子。
    载体沿用 test_file_tools_t3_3.py:230 的同一断言（**基线已有**，本阶段只复核，不重复实现）。
    """
    for label, obs in (
        ("read_file", _run(read_file.build_spec().handler(read_file.Args(path="a.txt")))),
        ("list_files", _run(list_files.build_spec().handler(list_files.Args()))),
        ("grep_files", _run(grep_files.build_spec().handler(grep_files.Args(pattern="needle")))),
    ):
        assert obs.ok is True, f"{label} 应成功：{obs.text!r}"
        assert obs.materials == [], f"{label} 返回了材料 ⇒ 违红线 8：{obs.materials!r}"


# ── 回归检查 腿②：界面面（**静态**；不得声称运行期渲染已验证）───────────────


def test_a24_leg2_tsx_has_a_read_path_render_branch() -> None:
    """**A24 腿②（静态腿）**：AgentStepsBlock.tsx 源码里**真存在**把该字段显示出来的分支。

    🔴 **本腿只判源码文本**（D-回归检查）：本仓无 JS 测试框架 ⇒ **不得**据此声称
    「运行期渲染已验证」。它证明的是三件事，逐条可判真假：

    ① 载体文件存在且**非空**（分母自证）；
    ② 取值面：源码里读的是**步骤载荷**里的路径键（args 命中 + path 命中）；
    ③ 渲染面：读出该值之后**真的进了 JSX 插值**（不是取了个变量就扔掉）。
    """
    assert TSX_PATH.is_file(), f"界面载体不存在：{TSX_PATH}"
    source = TSX_PATH.read_text(encoding="utf-8")
    # 分母自证：源码非空且确实有既有的 JSX 插值（否则下面几条「命中」可能来自别的东西）
    assert len(source) > 1000, f"载体过短（读取面出错？）：{len(source)} 字符"
    assert "{step.title}" in source, "源码里没有既有的步骤标题插值 ⇒ 读取面出错了"

    # ② 取值面：**专属**取值函数从步骤载荷 `args` 里取 `path` 键。
    # ⚠️ 断言的是 `args.path` 这条**取值路径**（不是「源码里出现 path 这几个字母」）——
    # 泛匹配会让「别处恰好也有 path」的源码蒙混过关（审计点 ②）。
    reader = _function_body(source, "readFilePath")
    assert re.search(r"\bargs\.path\b", reader), (
        f"专属取值函数没有从步骤载荷 args 里取 path：{reader!r}"
    )
    # ⚠️ **不能只断「函数体里有 return」**（审计点 ② 的同型陷阱）：`return ""` 这种把值写空的
    # 变异照样有 `return` 二字、也照样保留 `args.path` 那行文本 ⇒ 空靶。
    # 必须断「取到的值**就是**被返回的那个」：一条**三元**守卫 return（`?:`），
    # 且它的真值分支**原样吐出**取值变量（不是常量 / 空串 / 别的表达式）。
    assert re.search(r"return\s+[^;]*\?[^;]*;", reader), (
        f"专属取值函数没有「取值 + 守卫 + 返回」的三元形态（把值写空的变异能过）：{reader!r}"
    )
    ternary = re.search(r"return\s+([^;]*\?[^;]*);", reader)
    assert ternary is not None, f"三元 return 未捕获：{reader!r}"
    taken = ternary.group(1)
    assert re.search(r"\bvalue\b", taken), f"三元 return 的真值分支没吐出取值变量：{taken!r}"
    assert not re.search(r'\?\s*""\s*:', taken), (
        f"三元 return 的真值分支被写成空串 ⇒ 界面永远显示不出路径：{taken!r}"
    )
    # ③ 渲染面：该专属函数**真被调用**，且取到的值进了 JSX 插值
    assert re.search(r"readFilePath\s*\(", source), (
        "专属取值函数定义了却没有任何调用点（界面并不显示它）"
    )
    assert re.search(r"\{\s*readPath\s*\}", source), (
        "取到的路径没有进入 JSX 插值（取了变量就扔掉 = 界面并不显示它）"
    )
    # ④ **专属**白名单：源码里有 `READ_FILE_TOOLS` 这个名字**且**它恰含读文件三件。
    # ⚠️ 这一条是审计点 ② 的核心：「未知 kind 回落 Info」在**基线上就成立**，
    # 拿它充数 = 空靶。专属白名单是本笔新增的**判别性**构造（删掉它本腿必红）。
    whitelist = _const_set_body(source, "READ_FILE_TOOLS")
    for name in READ_TOOLS:
        assert f'"{name}"' in whitelist, (
            f"前端专属白名单 READ_FILE_TOOLS 缺 {name} ⇒ 该工具的读取路径不会显示"
        )
    # ⑤ 白名单是**后端同一份**的镜像：两侧名字集合逐字相等（含分母自证 —— 恰好三名）。
    assert _const_set_body(source, "READ_FILE_TOOLS").count('"') == 2 * len(READ_TOOLS), (
        "前端白名单的元素个数与后端 READ_FILE_TOOLS 不一致（两侧口径已漂移）"
    )
    # ⑥ 后端那侧的白名单同样恰为三名（两侧同源，不是各写一份）
    assert runner.READ_FILE_TOOLS == set(READ_TOOLS), (
        f"后端白名单与载体声明的三名不一致：{sorted(runner.READ_FILE_TOOLS)}"
    )


def test_a24_leg2_tsx_static_scope_is_declared() -> None:
    """**A24 腿②（静态腿的边界自证）**：本仓**没有**前端测试框架 ⇒ 静态腿是唯一可用形态。

    为什么把这件事也做成断言：口径「不得声称运行期渲染已验证」若只写在 docstring 里，
    下一个人换个说法就能含糊过去。把**事实**钉住（frontend/package.json 无测试脚本、
    前端 * 点 test 点 * 零件），静态腿的强度边界就成了**机器可判**的。
    """
    repo = Path(__file__).resolve().parents[3]
    pkg = repo / "frontend" / "package.json"
    scripts = json.loads(pkg.read_text(encoding="utf-8"))["scripts"]
    assert set(scripts) == {"dev", "build", "preview"}, (
        f"frontend/package.json 的 scripts 变了（若真引入测试框架，本件的静态腿应改用真渲染）：{scripts}"
    )
    frontend = repo / "frontend"
    test_files = [
        p
        for p in frontend.rglob("*.test.*")
        if "node_modules" not in p.parts and "dist" not in p.parts
    ]
    assert test_files == [], f"frontend 下出现了测试文件（静态腿的前提已变）：{test_files}"
