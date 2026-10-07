"""`回归检查` 判据载体：**写通道留痕收口** —— 判据 **`回归检查` ①②③④**（阶段 6 · 可见面）。

## 本件判什么（四条腿，全部可判真假）

| 腿 | 判据 | 用例 |
|---|---|---|
| ① | 写入后**观察文本**含**实际编码名**（窄编码目标 ⇒ 落**真名**，不得是常量） | `test_a29_leg1_observation_carries_real_encoding_name` ＋ `test_a29_leg1_new_file_reports_utf8_only` |
| ② | `fell_back=True` ⇒ **显式**中文提示（构造原编码表示不了的字符） | `test_a29_leg2_fallback_is_explicitly_disclosed` ＋ `test_a29_leg2_repr_exact_is_never_reported_as_fallback` |
| ③ | **测试态**写仓库内 `data/` ⇒ **拒**（fail-closed）—— **回归钉复核** | `test_a29_leg3_test_mode_repo_write_guard_still_fires` |
| ④ | **生产态逐字不变**（除新增编码名/提示外，其余字节与既有行为一致） | `test_a29_leg4_production_bytes_are_unchanged` |

## 🔴 腿① 为什么断「真名」而不是「有编码字段」

阶段 5 的 `_encode_for_target` **恒返回 bytes** ⇒ 一个「写死 `编码=utf-8`」的实现
也能让「观察文本里有个编码名」的弱断言变绿 —— 那是**假绿**（判别力 0）。本件因此
**正控 + 负控成对**：窄编码目标必须落它的**真名**；目标不存在则只可落 `utf-8`
（新文件没有「原编码」可言）⇒ 两腿一起把 `encoding` 钉死成「**按目标算出来的**」。

## 🔴 腿② 的构造：**必须用 `latin-1` 系目标**（本件最要紧的一条实测）

⚠️ **直觉会选错**：初版想用「GBK 目标 + `🚀`（`U+1F680`）」构造回退，**实测不成立** ——
但**失败原因与直觉相反**（三条实测逐条列明，可复跑）：

    chr(0x1F680).encode("gbk")      ⇒  抛 UnicodeEncodeError    # 窄编码确实编不了它
    chr(0x1F680).encode("gb18030")  ⇒  b'\x950\x8b4'            # 但 gb18030 **能**编
    detect_encoding(GBK字节)        ⇒  "gb18030"               # ⚠️ GBK 文件被判成 gb18030

⇒ 陷阱在**探测值**这一步：`detect_encoding` 的兜底序是
`utf-8 → utf-8-sig → gb18030 → latin-1`，**没有 `gbk` 这一档**
⇒ 一个 GBK 文件被探测成 `gb18030`，而 `gb18030` 表示得了 `🚀`
⇒ 「GBK 目标 + 🚀」**根本走不到**回退分支（腿② 会**假绿**：观察文本里没有提示，
而实现其实是对的 —— 判据却以为自己检出了缺陷）。

**真正可达的回退路径**：落在 **`latin-1`** 上的目标文件
（如 `café` 的 `cp1252` 字节 —— 不是合法 UTF-8、也不是合法 GB18030）
**无法表示中文** ⇒ 写中文时**真抛** `UnicodeEncodeError` ⇒ 走进回退分支。
⇒ 本件一律用这条**实测可达**的构造，并在用例里**断言前置**（探测值确实是 `latin-1`），
使「构造失效」表现为**红**而不是**静默空靶**。

## 纪律

- 工作区根一律 `tmp_path`（**红线 12**：不得指向仓库根 / 数据根 / dist / 证据目录）；
- `backend/tests/agent/` **无 `conftest.py`** ⇒ 夹具**自带**（先例 `test_file_tools_t3_3.py:61`）；
- `WORKSPACE_CONFIRM_SECRET` 用 `monkeypatch.setattr`（**自动还原**，不跨用例泄漏）
  ＋ `_confirm.reset_used_for_tests()`（`K-回归检查`：token 是**秒级**时间戳，同秒同
  `(path, content)` 会算出**同一个 token** ⇒ 不清已用集合，第二条用例会被判 `replayed`）；
- 快照目录一律 `monkeypatch` 到 `tmp_path`（否则会往真 `data/workspace-backups/` 写盘）。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from app.agent.tools import _confirm, _paths, _write, workspace_write
from app.core.config import settings
from app.pipeline.parsing import detect_encoding

#: 本件的密钥（与阶段 5 载体同款：monkeypatch 上去、用例结束自动还原）
SECRET = "t3-6-0-encoding-carrier-secret"

#: GB18030 编码空间内的中文样张（窄编码目标用）
GBK_SAMPLE = "中文内容"

#: **落在 `latin-1` 上的目标字节** —— 不是合法 UTF-8、也不是合法 GB18030
#: ⇒ `detect_encoding` 兜底到 `latin-1` ⇒ **表示不了中文**
#: ⇒ 写中文时**真抛** `UnicodeEncodeError` ⇒ 这才是 `fell_back` **实测可达**的构造。
#: （⚠️ 别改用 GBK 目标 + emoji：GBK 系对星平面字符**不抛**，会退化成字面量 —— 见模块 docstring。）
LATIN1_BYTES = "café".encode("cp1252")

#: 星平面字符（`U+1F680`，`🚀`）—— 负控用：它**能**被 `gb18030` / `utf-8` 表示，
#: **不能**被 `gbk` / `big5` 等窄编码表示。用它把「探测值 ≠ 写回编码」这条陷阱钉成可判的腿。
ASTRAL = chr(0x1F680)

#: 🔴 `_write.resolve_backup_dir` 的**真实现**（在夹具 monkeypatch **之前**抓下的那一份）。
#: 理由：`isolate_backups` 夹具把模块属性换成了 tmp_path 替身（红线 12 必须如此），
#: 而腿③ 要判的正是**守卫本体** ⇒ 必须留住原始函数对象，否则打的是替身（**空靶**）。
_REAL_RESOLVE_BACKUP_DIR = _write.resolve_backup_dir


@pytest.fixture(autouse=True)
def isolate_backups(monkeypatch, tmp_path):  # noqa: ANN001
    """把快照目录锚到 `tmp_path`（红线 12：本件是写侧载体，不得污染仓库 `data/`）。

    照 `test_write_atomic_t3_5.py` 的同款夹具（打的是**模块属性**，不是 settings 属性 ——
    `_take_snapshot` 直接调 `resolve_backup_dir()`；打空锚点会真往仓库内写盘）。
    """
    monkeypatch.setattr(_write, "resolve_backup_dir", lambda: tmp_path / "backups")


@pytest.fixture(autouse=True)
def _fresh_tokens():
    """每条用例前清空「已用凭证」集合（`K-回归检查`：token 同秒会撞）。

    `mark_used` 是**进程级集合**，不清就会跨用例泄漏 ⇒ 第二条用例拿到同秒同形态的
    token 时直接被判 `replayed`，而**报出来的是「写失败」**（归因南辕北辙）。
    """
    _confirm.reset_used_for_tests()


def _write_via_handler(monkeypatch, tmp_path, *, rel, content, existing, create=False):
    """走**真** `workspace_write.build_spec().handler` 写一次，返回 `(obs, ws)`。

    为什么要走 handler 而不是直接调 `_encode_for_target`：本笔的两条留痕
    （编码名 / 回退提示）**落在观察文本上**，而观察文本由 handler 装配 ——
    只测函数返回值等于「测了中间件、没测交付面」。

    - `existing`：目标文件的**写前字节**（传 `None` ⇒ 目标不存在 ⇒ 走新建语义）；
    - `create`：是否新建语义（`True` 时目标已存在会被写侧判拒）；
    - 工作区根与快照目录**都在 `tmp_path` 之内**（红线 12）。
    """
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    target = ws / rel
    if existing is not None:
        target.write_bytes(existing)

    monkeypatch.setattr(_paths, "_read_configured_root", lambda: str(ws))
    monkeypatch.setattr(settings, "WORKSPACE_CONFIRM_SECRET", SECRET)
    # 凭证绑的是「**逻辑内容**」= str(content).encode("utf-8")（与宿主签发口径逐字一致）
    token = _confirm.issue_write_confirm(
        root=ws, rel_path=rel, content=str(content).encode("utf-8")
    )
    spec = workspace_write.build_spec()
    obs = asyncio.run(
        spec.handler(
            workspace_write.WorkspaceWriteArgs(
                path=rel, content=content, create=create, confirm_token=token
            )
        )
    )
    return obs, ws


# ---------------------------------------------------------------------------
# 回归检查 腿①：观察文本含**实际**编码名（正控窄编码 + 负控新文件）
# ---------------------------------------------------------------------------


def test_a29_leg1_observation_carries_real_encoding_name(monkeypatch, tmp_path):  # noqa: ANN001
    """**A29 腿①（正控）**：窄编码（GB18030）目标 ⇒ 观察文本里出现**探测到的真名**。

    判别力在于「**真名**」二字：实现若把编码名写成常量（比如恒 `utf-8`）——
    正是 `M-回归检查` 那一类变异 —— 本腿**必红**。
    """
    obs, ws = _write_via_handler(
        monkeypatch,
        tmp_path,
        rel="g.txt",
        content=GBK_SAMPLE,
        existing=GBK_SAMPLE.encode("gb18030"),
    )

    # 前置自证（K-回归检查 的「正控」纪律）：构造的前提真成立 —— 探测值确实是窄编码
    assert detect_encoding(GBK_SAMPLE.encode("gb18030")) == "gb18030", (
        "构造前提失效：GB18030 字节的探测值不是 gb18030"
    )

    assert obs.ok is True, f"写失败 ⇒ 本腿不成立：{obs.text}"
    # ① 盘上仍是原编码字节（「文本说对了、字节却变了」也是缺陷）
    assert (ws / "g.txt").read_bytes() == GBK_SAMPLE.encode("gb18030"), (
        "盘上字节不再是原编码 ⇒ 观察文本里的编码名与实际落盘不一致"
    )
    # ② 观察文本含**真名**（gb18030 或它的探测别名 gbk —— detect_encoding 的返回值域）
    assert ("gb18030" in obs.text) or ("gbk" in obs.text), (
        f"观察文本未落实际编码名（应含 gbk/gb18030），实测：{obs.text!r}"
    )
    # ③ 负控：**不得**把编码名写成常量 utf-8（这正是 M-回归检查 的靶面）
    assert "实际编码：utf-8" not in obs.text, (
        f"观察文本把实际编码写成了常量 utf-8（窄编码目标被谎报）：{obs.text!r}"
    )
    # ④ 未发生回退 ⇒ 不得出现回退提示（否则提示是恒真腿）
    assert "已回退为 UTF-8" not in obs.text, (
        f"本条内容本可用原编码表示，却出现了回退提示：{obs.text!r}"
    )


def test_a29_leg1_new_file_reports_utf8_only(monkeypatch, tmp_path):  # noqa: ANN001
    """**A29 腿①（负控）**：目标**不存在** ⇒ 只可落 `utf-8`（新文件没有「原编码」）。

    没有这条，「编码名」可能只是**常量**（恒 `utf-8` 的实现能过弱版本的正控）。
    本腿与上一条一起，把 `encoding` 钉死成「**按目标算出来的**」。
    """
    obs, ws = _write_via_handler(
        monkeypatch,
        tmp_path,
        rel="brand-new.txt",
        content=GBK_SAMPLE,
        existing=None,
        create=True,
    )

    assert obs.ok is True, f"写失败 ⇒ 本腿不成立：{obs.text}"
    assert (ws / "brand-new.txt").read_bytes() == GBK_SAMPLE.encode("utf-8"), (
        "新文件必须落 UTF-8 字节"
    )
    assert "实际编码：utf-8" in obs.text, f"新文件未如实落 utf-8，实测：{obs.text!r}"


# ---------------------------------------------------------------------------
# 回归检查 腿②：fell_back=True ⇒ 显式中文提示
# ---------------------------------------------------------------------------


def test_a29_leg2_fallback_is_explicitly_disclosed(monkeypatch, tmp_path):  # noqa: ANN001
    """**A29 腿②（正腿）**：内容无法用原编码表示 ⇒ **回退被显式说出来**。

    构造：`latin-1` 目标（见模块 docstring 的实测）+ 中文内容 ⇒ 编码**真抛**
    ⇒ 走回退分支。阶段 5 登记的缺陷正是「这里静默换编码、三处无感」
    ⇒ 本腿是那条登记的**关闭判据**。
    """
    content = "中文" + GBK_SAMPLE
    obs, ws = _write_via_handler(
        monkeypatch, tmp_path, rel="l.txt", content=content, existing=LATIN1_BYTES
    )

    # 前置自证：构造的前提真成立 —— 探测值确实是 latin-1（否则本腿是**静默空靶**）
    assert detect_encoding(LATIN1_BYTES) == "latin-1", (
        f"构造前提失效：目标字节的探测值不是 latin-1（实测 {detect_encoding(LATIN1_BYTES)!r}）"
    )

    assert obs.ok is True, f"写失败 ⇒ 本腿不成立：{obs.text}"
    # ① 字节确实回退到了 UTF-8（否则「回退」二字无从谈起）
    assert (ws / "l.txt").read_bytes() == content.encode("utf-8"), "回退路径未落 UTF-8 字节"
    # ② 显式中文提示（断「发生了什么」+ 断「为什么」）
    assert "已回退为 UTF-8" in obs.text, (
        f"回退路径未显式提示（应含「已回退为 UTF-8」），实测：{obs.text!r}"
    )
    assert "无法沿用原文件编码" in obs.text, (
        f"提示未说明回退原因（应含「无法沿用原文件编码」），实测：{obs.text!r}"
    )
    # ③ 回退时编码名必须落**回退后的真名**，不得谎报原编码
    assert "实际编码：utf-8" in obs.text, f"回退后编码名不实（应为 utf-8），实测：{obs.text!r}"


def test_a29_leg2_utf8_target_never_reports_fallback(monkeypatch, tmp_path):  # noqa: ANN001
    """**A29 腿②（负控）**：`fell_back` 判的是**「抛没抛」**，不是「编得对不对」。

    构造：UTF-8 目标（探测值 `utf-8`，**什么字符都表示得了**）+ 同一个 `U+1F680`
    ⇒ 编码**不会抛** ⇒ **不得**出现回退提示。

    没有这条，一个「恒追提示」的实现也能过上一条正腿 ⇒ 提示**恒真 = 无判别力**。

    ⚠️ 顺带钉住一条**反直觉**实测（本件的完整推导见模块 docstring）：**「GBK 目标 +
    `🚀`」触发不到回退** —— 因为 `detect_encoding` 把 GBK 字节判成 **`gb18030`**，
    而 `gb18030` **能**编码 `🚀`（`🚀` 只对 `gbk`/`big5` 等**窄**编码抛）。
    ⇒ 本件的正腿（`latin-1` 系）与负控（`utf-8`）**都是按实测选定的**，不是随手挑的。
    """
    # 前置自证（本腿的立论基础，逐条可判）：
    # ① `🚀` 对 `gbk` 确实**抛**（所以「窄编码 + 🚀」是个**看似**可行的构造）
    with pytest.raises(UnicodeEncodeError):
        ASTRAL.encode("gbk")
    # ② 但 `gb18030` **能**编它（这才是「GBK 目标 + 🚀」打空回退分支的真原因）
    assert ASTRAL.encode("gb18030") == b"\x950\x8b4", (
        "gb18030 对 U+1F680 的编码结果与实测不符（本机 gb18030 已能表示星平面）"
    )
    # ③ 且 GBK 目标字节的**探测值**就是 gb18030（⇒ ② 真的生效，不是纸面推理）
    assert detect_encoding(GBK_SAMPLE.encode("gb18030")) == "gb18030", (
        "构造前提失效：GBK 字节的探测值不再是 gb18030"
    )

    obs, ws = _write_via_handler(
        monkeypatch,
        tmp_path,
        rel="u.txt",
        content=GBK_SAMPLE + ASTRAL,
        existing=("utf-8 目标" + ASTRAL).encode("utf-8"),
    )

    assert obs.ok is True, f"写失败 ⇒ 本腿不成立：{obs.text}"
    assert "已回退为 UTF-8" not in obs.text, (
        f"UTF-8 目标什么都能表示，不可能回退，却出现了回退提示：{obs.text!r}"
    )
    assert "实际编码：utf-8" in obs.text, f"应按探测值落 utf-8，实测：{obs.text!r}"
    # 字节腿：UTF-8 目标 ⇒ 内容逐字节按 UTF-8 落盘（emoji 也是真 emoji）
    assert (ws / "u.txt").read_bytes() == (GBK_SAMPLE + ASTRAL).encode("utf-8"), (
        "UTF-8 目标的落盘字节不符"
    )


# ---------------------------------------------------------------------------
# 回归检查 腿③：测试态写仓库内 data/ ⇒ 拒（**回归钉复核**）
# ---------------------------------------------------------------------------


def test_a29_leg3_test_mode_repo_write_guard_still_fires() -> None:
    """**A29 腿③（回归钉复核 · `D-回归检查`）**：测试态解析到仓库内 `data/` ⇒ **抛**。

    🔴 **口径如实说清**：守卫本体**不是本笔交付** —— 它在基线 `b8b9d9a`（`回归检查`）
    就已落地，本笔（`回归检查`）**零改动**。本腿判的是「**它现在仍然拒**」：
    把「测试态 fail-closed」从**文档**变成**可被判红**的钉子 —— 若有人摘掉守卫，本腿**必红**。

    ⚠️ 本件**不**声称「本笔实现了测试态守卫」（那是 `回归检查` 的交付面）。

    本腿**不写文件**：它打的是守卫的**判定入口**（无读写形参 ⇒ 连「读」都拦）。
    """
    # 前置：pytest 正在跑（守卫的判定条件就是它）—— 用**断言**而非 skip（K-回归检查：条件式断言 = 死代码）
    assert os.environ.get("PYTEST_CURRENT_TEST"), (
        "本腿依赖 PYTEST_CURRENT_TEST 存在（守卫的判定条件）—— 缺失即本腿不成立"
    )

    # 🔴 **打的是函数本体**（不是 autouse 夹具那个 tmp_path 替身）——
    # 见模块级 `_REAL_RESOLVE_BACKUP_DIR` 的说明：夹具**必须**把模块属性换掉（红线 12），
    # 而本腿要判的是守卫自己 ⇒ 只能靠那份**抓在 monkeypatch 之前**的真函数对象。
    real_impl = _REAL_RESOLVE_BACKUP_DIR
    # 正控（K-回归检查）：夹具**确实生效了**（否则本腿的「替身/真身」区分无从谈起）
    assert real_impl is not _write.resolve_backup_dir, (
        "正控失效：夹具未把 resolve_backup_dir 换成 tmp_path 替身 ⇒ 本腿的两条路径塌成一条"
    )

    from app.core.config import ROOT_DIR  # noqa: PLC0415

    # 🔴 **先取真值，再断「拒」**：测试态下 `real_impl()` 会**抛**，取不到路径 ——
    # 故正控（锚点形状）必须在**去掉测试态标记**之后做，否则它自己就先被守卫拦住。
    marker = os.environ.pop("PYTEST_CURRENT_TEST", None)
    try:
        # 负控（K-回归检查）：**同一个锚点**在非测试态下**放行**且值确定
        # （证明「拒」的**唯一**原因就是测试态守卫，而非路径不存在 / 权限等别的因素；
        #  没有这条，「守卫恒抛」的实现也能让主断言变绿 = 判别力 0）
        real = real_impl()
        assert os.path.commonpath([str(real), str(ROOT_DIR)]) == str(ROOT_DIR), (
            f"真锚点不在仓库内 ⇒ 本腿打空（实测 {real}，仓库根 {ROOT_DIR}）"
        )
        assert real.parts[-2:] == ("data", "workspace-backups"), (
            f"锚点形状不符（应为 data/workspace-backups），实测 {real}"
        )
        assert real_impl() == real, "非测试态下锚点值应确定（读两次同值）"
    finally:
        if marker is not None:
            os.environ["PYTEST_CURRENT_TEST"] = marker

    # 🔴 主断言：**测试态** ⇒ 拒（fail-closed）
    with pytest.raises(_write.TestModeRepoWriteError):
        real_impl()


# ---------------------------------------------------------------------------
# 回归检查 腿④：生产态逐字不变（除新增的编码名 / 提示）
# ---------------------------------------------------------------------------


def test_a29_leg4_production_bytes_are_unchanged(monkeypatch, tmp_path):  # noqa: ANN001
    """**A29 腿④**：生产态**字节**与「未改前」逐字一致（唯一增量 = 编码名 / 回退提示）。

    本笔只动了「怎么把结果**说出来**」（三元组 + 观察文本），**没有**动「怎么算字节」
    ⇒ 同一个工作区形态、同一份内容，跑**新旧两条路径**并逐字节比对：

    - **旧路径** = 直接调 `_write.write_workspace_file`，喂 `content.encode(enc)`
      —— 这正是阶段 5 的 handler 逐字做的事（`_encode_for_target` 的第一位就是它）；
    - **新路径** = 走改后的 handler（自己算编码 + 落观察文本）。

    ⚠️ 两条路径写在**不同子目录**（`old/` / `new/`）：同目录会让第二次写覆盖第一次，
    判据退化成「自己等于自己」= **空靶**。
    """
    sample = "中文内容 with ascii"
    gbk_bytes = sample.encode("gb18030")

    # ── 旧路径：写函数层的字节（阶段 5 的 handler 喂给它的就是这个）──────────────
    old_ws = tmp_path / "old"
    old_ws.mkdir()
    (old_ws / "g.txt").write_bytes(gbk_bytes)
    monkeypatch.setattr(_write, "resolve_backup_dir", lambda: tmp_path / "backups")
    old_outcome = _write.write_workspace_file(
        root=old_ws,
        rel_path="g.txt",
        data=sample.encode("gb18030"),
        create=False,
        snapshot=False,
    )
    old_bytes = old_outcome.target.read_bytes()

    # ── 新路径：改后的 handler（同一份内容、同一个目标编码）────────────────────
    new_ws = tmp_path / "new"
    new_ws.mkdir()
    (new_ws / "g.txt").write_bytes(gbk_bytes)
    monkeypatch.setattr(_paths, "_read_configured_root", lambda: str(new_ws))
    monkeypatch.setattr(settings, "WORKSPACE_CONFIRM_SECRET", SECRET)
    token = _confirm.issue_write_confirm(
        root=new_ws, rel_path="g.txt", content=sample.encode("utf-8")
    )
    obs = asyncio.run(
        workspace_write.build_spec().handler(
            workspace_write.WorkspaceWriteArgs(
                path="g.txt", content=sample, confirm_token=token
            )
        )
    )
    new_bytes = (new_ws / "g.txt").read_bytes()

    assert obs.ok is True, f"写失败 ⇒ 本腿不成立：{obs.text}"
    assert new_bytes == old_bytes, (
        f"生产态字节被改动（腿④：除新增编码名/提示外必须逐字一致）："
        f"{new_bytes!r} != {old_bytes!r}"
    )
    # 负控：这条「逐字一致」不能是「两边都成了 UTF-8」那种**共同退化**
    assert new_bytes == gbk_bytes, "两侧都不是原编码字节 ⇒ 上述一致性是共同退化，不可采信"
    # 唯一增量：观察文本里的编码名（旧路径的返回值里**没有**这一项）
    assert "实际编码：gb18030" in obs.text, (
        f"腿④ 的「唯一增量」缺失（观察文本应含编码名），实测：{obs.text!r}"
    )
    assert "已回退为 UTF-8" not in obs.text, "本条内容可表示 ⇒ 不得出现回退提示"
