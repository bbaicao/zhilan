"""S4 · 回归修复 判据载体：写工具四类拒因的中文文案必须透传到观察面。

## 规格（交接件 §2 · 180）

- 越界 / 敏感模板 / 目标已存在 / 写前快照失败 四类拒因，observation.text 里必须是
  _write.WriteError 的**中文拒因原文**，而**不是** WriteError 这个类名；
- ok=False 与「零材料」不变；
- 反向保护：成功腿文本原样透传（含（实际编码：…）后缀）；非 WriteError 异常仍回落
  **类名**且不回灌绝对路径（TestModeRepoWriteError 的实测解析值：{path}）。

## 为什么这是缺陷而不是文案偏好

写侧把「裸 OSError 的 errno 文案不适合给用户看、且会泄露绝对路径」统一包装成中文
WriteError；handler 只取 exc.__class__.__name__ ⇒ 用户与模型只看到
「写文件未执行：WriteError（a.txt）。」：既不知为何被拒，也无从改参数重试（模型无法自愈）。

## 构造要点（实测踩过的两个坑，别再踩）

1. **快照腿必须打 try 内那条**：_write.resolve_backup_dir() 在 _write.py:329，位于
   _take_snapshot 的 try（:330）**之外** ⇒ 直接把它 patch 成抛 OSError 只得到裸 OSError
   （类名回落，设计如此，本身就是本件的反向保护腿之一）。真路径是让
   backup_dir.mkdir() 在 try **内**失败：本件把备份目录 patch 成「被同名文件占位」
   ⇒ FileExistsError ⊂ OSError ⇒ 被包成「写前快照失败，已拒绝写入：FileExistsError」。
2. 测试态凡 snapshot=True 的腿都必须 patch _write.resolve_backup_dir：默认实现在
   PYTEST_CURRENT_TEST 下发现备份目录落在仓库内就抛 TestModeRepoWriteError（红线 12）。

## 同源断言

拒因原文不硬抄字符串：测试自己调 _write_paths.resolve_write_target(rel, root) 取
.reason，再断言它逐字出现在观察文本里（口径同源，产品文案漂移时本件不会假绿）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent.runner import AgentTurn
from app.agent.tools import _paths
from app.agent.tools import _write as _write_mod
from app.agent.tools import _write_paths as write_paths
from app.agent.tools import workspace_write
from app.core.config import settings
from tests.agent.test_runner import collect, faux_model, registry_with, says, tool_call

TEST_CONFIRM_SECRET = "s4-180-carrier-secret-not-persisted"


@pytest.fixture(autouse=True)
def pin_web_off(monkeypatch):
    monkeypatch.setattr(settings, "WEB_ENABLE", False)


@pytest.fixture(autouse=True)
def pin_workspace_root(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(root))
    assert _paths.resolve_workspace_root() is not None, "前提：工作区根必须真解析得出来"
    return root


@pytest.fixture(autouse=True)
def pin_confirm_secret(monkeypatch):
    monkeypatch.setattr(settings, "WORKSPACE_CONFIRM_SECRET", TEST_CONFIRM_SECRET, raising=False)
    from app.agent.tools import _confirm

    _confirm.reset_used_for_tests()


def _issue(root, rel: str, content: str) -> str:
    from app.agent.tools import _confirm

    return _confirm.issue_write_confirm(root=root, rel_path=rel, content=content.encode("utf-8"))


def _args(*, path: str, content: str, create: bool, token: str) -> dict:
    return {"path": path, "content": content, "create": create, "confirm_token": token}


async def _run_write(*, args: dict, write=None) -> dict:
    """跑一轮 ReAct 让模型点名 workspace_write，返回那条 observation 帧。

    不传 tools、不手工造 collector：手工包 AgentTool 会让运行器自建的那份 collector
    永远为空（审计/观察全空），见 tests/agent/test_write_audit_t3_5.py 的长注释。
    """
    spec = workspace_write.build_spec(write=write)
    assert spec.needs_confirm is True
    model = faux_model([tool_call("workspace_write", args), says("好")])
    turn = AgentTurn(mode="react")
    frames = await collect(turn, question="问题", model=model, registry=registry_with(spec))
    observations = [f for f in frames if f["kind"] == "observation"]
    assert observations, "本轮必须产出一条观察帧"
    return observations[-1]


# ── 正向 discriminator：四类拒因各一条 ───────────────────────────────────────

@pytest.mark.asyncio
async def test_bug180_outside_root_reason_is_passed_through(pin_workspace_root):
    root = pin_workspace_root
    rel = "../escape-outside.txt"
    content = "越界内容"
    token = _issue(root, rel, content)

    verdict = write_paths.resolve_write_target(rel, root)
    assert isinstance(verdict, write_paths.WriteReject), "前提：该样本必须被判拒"

    obs = await _run_write(args=_args(path=rel, content=content, create=False, token=token))

    assert obs["ok"] is False
    assert obs["detail"].startswith("写文件未执行：")
    assert verdict.reason in obs["detail"], "越界拒因原文必须透传：" + repr(obs["detail"])
    assert "WriteError" not in obs["detail"], "不得只给类名"
    assert rel in obs["detail"]


@pytest.mark.asyncio
async def test_bug180_sensitive_path_reason_is_passed_through(pin_workspace_root):
    root = pin_workspace_root
    rel = ".env"
    content = "SECRET=1"
    token = _issue(root, rel, content)

    verdict = write_paths.resolve_write_target(rel, root)
    assert isinstance(verdict, write_paths.WriteReject), "前提：敏感路径必须被判拒"

    obs = await _run_write(args=_args(path=rel, content=content, create=False, token=token))

    assert obs["ok"] is False
    assert verdict.reason in obs["detail"], "敏感模板拒因原文必须透传：" + repr(obs["detail"])
    assert "命中敏感路径清单" in obs["detail"]
    assert "WriteError" not in obs["detail"]


@pytest.mark.asyncio
async def test_bug180_existing_target_reason_is_passed_through(pin_workspace_root):
    root = pin_workspace_root
    rel = "exists.txt"
    target = root / rel
    target.write_text("旧内容", encoding="utf-8")
    content = "新内容"
    token = _issue(root, rel, content)

    obs = await _run_write(args=_args(path=rel, content=content, create=True, token=token))

    assert obs["ok"] is False
    assert "目标已存在" in obs["detail"] and "create=True" in obs["detail"], repr(obs["detail"])
    assert "WriteError" not in obs["detail"]
    assert target.read_text(encoding="utf-8") == "旧内容", "被拒时不得动盘"


@pytest.mark.asyncio
async def test_bug180_snapshot_failure_reason_is_passed_through(
    pin_workspace_root, tmp_path, monkeypatch
):
    root = pin_workspace_root
    rel = "snap.txt"
    target = root / rel
    target.write_text("旧内容", encoding="utf-8")
    content = "新内容"
    token = _issue(root, rel, content)
    blocker = tmp_path / "backup-dir-occupied-by-a-file"
    blocker.write_text("占位", encoding="utf-8")
    # mkdir 在 try 内失败 ⇒ FileExistsError ⊂ OSError ⇒ 被包成中文 WriteError
    monkeypatch.setattr(_write_mod, "resolve_backup_dir", lambda: blocker)

    obs = await _run_write(args=_args(path=rel, content=content, create=False, token=token))

    assert obs["ok"] is False
    assert "写前快照失败" in obs["detail"], repr(obs["detail"])
    assert "WriteError" not in obs["detail"]
    assert target.read_text(encoding="utf-8") == "旧内容", "快照失败必须 fail-closed，不得动盘"


# ── 反向保护腿：既有口径不得破 ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bug180_reverse_success_text_is_passed_through(pin_workspace_root):
    """成功腿：替身文本原样透传 + 实际编码后缀 + 零材料。"""
    root = pin_workspace_root
    rel = "ok.txt"
    content = "成功内容"
    token = _issue(root, rel, content)
    seen: dict = {}

    def _fake_write(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            target=str(kwargs["rel_path"]), byte_delta=len(kwargs["data"]), snapshot_id=None,
            text="替身写入了文件",
        )

    obs = await _run_write(args=_args(path=rel, content=content, create=True, token=token), write=_fake_write)

    assert obs["ok"] is True, repr(obs["detail"])
    assert obs["detail"].startswith("替身写入了文件"), "成功腿文本必须原样透传"
    assert "（实际编码：" in obs["detail"]
    assert "WriteError" not in obs["detail"]
    assert seen.get("rel_path") == rel


@pytest.mark.asyncio
async def test_bug180_reverse_non_write_error_falls_back_to_class_name(pin_workspace_root):
    """非 WriteError 只落类名：异常原文（可能含绝对路径）不得进观察文本。"""
    root = pin_workspace_root
    rel = "boom.txt"
    content = "内容"
    token = _issue(root, rel, content)
    raw_message = "S4-180 内部异常原文必须遮蔽"

    def _boom(**kwargs):
        raise RuntimeError(raw_message)

    obs = await _run_write(args=_args(path=rel, content=content, create=True, token=token), write=_boom)

    assert obs["ok"] is False
    assert "RuntimeError" in obs["detail"], repr(obs["detail"])
    assert raw_message not in obs["detail"], "非 WriteError 的原文不得透传"


@pytest.mark.asyncio
async def test_bug180_reverse_test_mode_error_does_not_leak_absolute_path(pin_workspace_root):
    """真写盘 + 默认备份目录 ⇒ TestModeRepoWriteError 只落类名，绝对路径不外泄。"""
    root = pin_workspace_root
    rel = "tm.txt"
    target = root / rel
    target.write_text("旧", encoding="utf-8")
    content = "新"
    token = _issue(root, rel, content)

    obs = await _run_write(args=_args(path=rel, content=content, create=False, token=token))

    assert obs["ok"] is False
    assert "TestModeRepoWriteError" in obs["detail"], repr(obs["detail"])
    assert str(root) not in obs["detail"], "绝对路径不得进入观察文本"
    assert "workspace-backups" not in obs["detail"], "仓库路径细节不得外泄"


# ── 追加判别腿（task-10）：WriteError 的**子类**同样透传 ──────────────────────


class _SubWriteError(_write_mod.WriteError):
    """测试内自定义子类（⛔ 不改产品代码）。

    存在的意义：实现用的是 `isinstance(exc, _WriteError)`，语义就是「WriteError **及其子类**
    都透传中文拒因」。半修变异 M1（把该处改成 `type(exc) is _WriteError`）只治正典类、漏子类，
    本腿是它唯一的打红点 —— 其余 7 条腿在 M1 下**仍全绿**（已实测）。"""


@pytest.mark.asyncio
async def test_bug180_subclass_write_error_reason_is_passed_through(pin_workspace_root):
    """判据：`_write.WriteError` 的**子类**实例，观察面必须逐字等于
    `写文件未执行：{子类文案}（{rel}）。`（⛔ 不是类名回落），且 ok=False、零材料。

    构造：注入的 `write` 替身（同步 def）抛子类实例 —— handler 的 except 在 try 内，
    子类实例必然走中文透传路径。"""
    root = pin_workspace_root
    rel = "subclass.txt"
    content = "子类拒因内容"
    token = _issue(root, rel, content)
    reason = "子类拒因原文 S4-180-SUB"

    def _raise_sub(**kwargs):  # ⛔ 同步 def：handler 同步调用 func(...)
        raise _SubWriteError(reason)

    spec = workspace_write.build_spec(write=_raise_sub)
    assert spec.needs_confirm is True
    model = faux_model(
        [tool_call("workspace_write", _args(path=rel, content=content, create=True, token=token)), says("好")]
    )
    turn = AgentTurn(mode="react")
    frames = await collect(turn, question="问题", model=model, registry=registry_with(spec))
    observations = [f for f in frames if f["kind"] == "observation"]
    assert observations, "本轮必须产出一条观察帧"
    obs = observations[-1]

    # 逐字相等（⛔ 不用 in / not in / 类名缺席这类弱形态）
    assert obs["detail"] == f"写文件未执行：{reason}（{rel}）。", repr(obs["detail"])
    assert obs["ok"] is False
    assert turn.materials == [], "写工具恒零材料（红线 8）"

# ── 追加保护腿（task-16）：根闸依赖 —— 配置根字面量不得进工具层文本 ──────────


_ILLEGAL_CONFIG_ROOT = r"\\server\share\ws"


@pytest.mark.asyncio
async def test_bug180_illegal_config_root_literal_never_reaches_tool_text(
    pin_workspace_root, monkeypatch
):
    """判据（task-16）：配置根**不合法**时，工具层必须走「根未配置」早退，
    返回文本逐字等于 `PATH_NO_ROOT_MESSAGE`，且**逐字不含**那个配置根字面量。

    ## 钉的是哪个结构

    `workspace_write.handler` 里 `if root is None` 的早退臂（工具层入口），
    以及它依赖的 `_paths.resolve_workspace_root()` 根闸（`_paths.py:583-585` 的
    `_lexical_layers(raw, is_root=True)` → `return None`）。`_paths.classify()` 的
    root_reject 分支（`_paths.py:485-488`）**会**把配置根字面量 format 进拒因；
    只要根闸还在，这条 detail 就到不了工具层文本。

    ## 为什么要钉

    writer-a 的 task-12 实测：`classify` 的 root_reject 分支 5/5 把配置根**字面量**
    回显进 reason（`cfg_root_echo=true`），经 180 的「中文拒因透传」之后**若可达**，
    工具层文本就会出现形如
    `写文件未执行：路径越界：\\server\\share\\ws 是网络路径，已拒绝。（root-gate.txt）。`
    的内容（配置根 = 宿主内部路径信息）。当前**不可达**（同一个 `_lexical_layers`
    闸在 `resolve_workspace_root()` 里先返回 None ⇒ 工具层走根未配置早退）；
    本腿把这层「不可达」写成判据，防将来有人把根闸挪走、放宽或绕过。

    ## 如实说明判别力边界

    本腿**不判别 回归修复 缺陷本身** —— 在 HEAD（未修复）字节下它**也是绿的**：
    缺陷态同样先走根未配置早退，压根到不了「类名回落」那条路径。它的判别力对象是
    **根闸依赖**：把根闸打掉（变异 M7：`return None` → `return raw`）之后，本腿必须
    RED，而其余 8 条（都用合法根）仍应全绿。

    ## 构造

    - autouse 夹具先装**合法**根并断言可解析；本腿在体内再 monkeypatch 成不合法根
      （UNC），并断言此刻 `resolve_workspace_root() is None`（前提：根闸在）；
    - 注入的 `write` 替身是**同步 def**，先记账、再**委托真写**
      （`_write.write_workspace_file`）—— 变异下会真走到 `classify` 并把根字面量透传
      出来，而不是测试自己伪造一段字符串；
    - 凭证按不合法根签发：`canonical_rel_path()` 不读 root（`_confirm.py:157-172`），
      这张 token 在合法/不合法根下同形；本腿真正断言的是「早退发生在凭证校验之前」。
    """
    assert pin_workspace_root is not None, "前提：autouse 夹具已装可解析的合法根"
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", _ILLEGAL_CONFIG_ROOT)
    assert _paths.resolve_workspace_root() is None, "前提：根闸必须拦下不合法配置根"

    rel = "root-gate.txt"
    content = "根闸腿内容"
    token = _issue(_ILLEGAL_CONFIG_ROOT, rel, content)

    calls: list[dict] = []

    def _delegate(**kwargs):  # ⛔ 同步 def：handler 同步调用 func(...)
        calls.append(dict(kwargs))
        return _write_mod.write_workspace_file(**kwargs)

    obs = await _run_write(
        args=_args(path=rel, content=content, create=False, token=token),
        write=_delegate,
    )

    # ① 形态：根未配置的**实测**中文文案（逐字相等，⛔ 不用 in / startswith 这类弱形态）
    assert obs["ok"] is False
    assert obs["detail"] == workspace_write.PATH_NO_ROOT_MESSAGE, repr(obs["detail"])
    assert obs["detail"] == "写文件未执行：尚未配置工作区根，本次没有写任何文件。", repr(obs["detail"])
    # ② 配置根字面量**逐字不得**出现在工具层文本里（真断言，⛔ 不用模糊包含）
    assert _ILLEGAL_CONFIG_ROOT not in obs["detail"], repr(obs["detail"])
    # ③ 根闸先于写调用：写函数一次都没被调用（越界零 IO 的同源口径）
    assert calls == [], repr(calls)

