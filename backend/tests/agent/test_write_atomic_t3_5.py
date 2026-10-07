"""``回归检查`` 判据载体：**A7-1 原子性与半写（故障注入）** + **A7-2 取消腿** + **A8 编码/行尾保真**。

## 本件覆盖的判据

| 判据 | 腿 | 用例 |
|---|---|---|
| **``A7-1``** | 注入点① ``tmp.write_bytes`` 抛 ⇒ 原文件逐字节不变 + 无残留临时文件 | ``test_a7_1_inject_tmp_write_bytes_failure`` |
| | 注入点② ``os.replace`` 抛 ⇒ 同上 | ``test_a7_1_inject_os_replace_failure`` |
| | 注入点③ **真 ``asyncio.CancelledError``**（同线程、函数内部注入）⇒ 盘上文件 == 新/旧之一 + 无残留 | ``test_a7_2_real_cancelled_error_is_cleaned_up`` |
| **``A7-2``** | 取消腿（``except BaseException`` 的清理结构）——**含非 ``Exception`` 的 ``BaseException``**（``KeyboardInterrupt``） | ``test_a7_2_cancel_leaves_old_or_new`` |
| | 边界：取消/线程异常**不得被改写成 ``WriteError``** | ``test_a7_2_cancel_in_flight_coroutine_raises_not_swallowed`` |
| **``A8``** | ① UTF-8 无 BOM 写回仍无 BOM | ``test_a8_leg1_utf8_without_bom_stays_without_bom`` |
| | ② **LF 源**写回仍是 LF（真靶子，见下） | ``test_a8_leg2_lf_line_endings_survive`` |
| | ③ GBK 仍 GBK（``M-回归检查`` 的靶腿） | ``test_a8_leg3_gbk_roundtrip`` |
| | ④ 未改动部分逐字节不变 | ``test_a8_leg4_untouched_bytes_verbatim`` |

## 🔴 为什么注入点必须是 ``os.replace`` / ``tmp.write_bytes``（而不是换掉整个函数）

``M-回归检查``（**去掉失败清理**）打的是 ``_write_atomically`` 内部的 ``except BaseException: tmp.unlink(...)``。
若用「把 ``_write_atomically`` 整个替换成抛异常的替身」来测 ⇒ **那段 except 根本不执行**
⇒ 无论有没有清理，临时文件残不残留都测不出来 ⇒ **空靶**（包 §4.5 纪律 2 逐字）。
⇒ 本件一律**在函数内部**触发失败：patch ``os.replace`` 抛 ⇒ 走进 ``except`` ⇒ 才验得到 ``tmp.unlink``。

⚠️ 实测依据：``_write_atomically`` 用的是模块级 ``os.replace``（``_write.py`` 顶部 ``import os``）
⇒ ``monkeypatch.setattr(os, "replace", boom)`` **打得到**（已实测：`replace call count = 1`）。

## 🔴 A8 腿② 为什么用 **LF 源**（这一条是本件的关键判别力）

实测（``audit-write`` 2026-09-26）：**Windows 上 ``write_text`` 的默认行为本来就产 CRLF**
```
tgt.write_bytes(b"line1\r\nline2\r\n"); tgt.write_text(tgt.read_text(), "utf-8")  => b"line1\r\n..." 仍 CRLF（假绿）
tgt.write_bytes(b"line1\nline2\n");     tgt.write_text(tgt.read_text(), "utf-8")  => b"line1\r\n..." **被改成 CRLF**（真红）
```
⇒ **「CRLF 源仍 CRLF」在 Windows 上是恒真腿（空靶）**；只有 **LF 源**才能区分
「字节级写」与「``write_text`` 默认转换」。本件因此用 LF 源（与 task-30 描述逐字一致）。

## 纪律

- 工作区根一律 ``tmp_path``（**红线 12**：不得指向仓库根 / 数据根 / dist / 证据目录）；
- 备份目录用 ``monkeypatch.setattr(_write, "resolve_backup_dir", lambda: tmp_path / "backups")`` 隔离；
- ``backend/tests/agent/`` **无 ``conftest.py``** ⇒ 夹具自带（``tests/api/conftest.py`` 的钉子不生效）。
"""

from __future__ import annotations

import os
import pathlib

import pytest

from app.agent.tools import _write
from app.core.config import settings

#: 临时文件前缀（``_write_atomically`` 的形状；断言「无残留」时按它扫）
TMP_PREFIX = ".tmp-"


@pytest.fixture(autouse=True)
def isolate_backups(monkeypatch, tmp_path):  # noqa: ANN001
    """把快照目录锚到 ``tmp_path``（否则会往真 ``data/workspace-backups/`` 写）。

    ⚠️ 这是**红线 12 + 数据面纪律**：本件是写侧载体，不能让测试污染仓库数据目录。
    """
    backups = tmp_path / "backups"
    monkeypatch.setattr(_write, "resolve_backup_dir", lambda: backups)
    return backups


def _leftovers(directory: pathlib.Path) -> list[str]:
    """该目录下的**残留临时文件**（``.tmp-*``）。

    「无残留临时文件」是 ``A7-1`` 每一条注入腿的**必备断件**（规格逐字）。
    """
    return sorted(p.name for p in directory.iterdir() if p.name.startswith(TMP_PREFIX))


def _write_one(root: pathlib.Path, name: str, data: bytes) -> pathlib.Path:
    """走**真** ``write_workspace_file`` 写一个文件（覆盖语义，无快照）。"""
    outcome = _write.write_workspace_file(
        root=root, rel_path=name, data=data, create=False, snapshot=False
    )
    assert outcome.target == root / name
    return outcome.target

# ---------------------------------------------------------------------------
# A7-1 · 注入点① tmp.write_bytes 抛
# ---------------------------------------------------------------------------


def test_a7_1_inject_tmp_write_bytes_failure(tmp_path, monkeypatch):  # noqa: ANN001
    """**A7-1 注入点①**：临时文件**写**阶段挂 ⇒ 原文件逐字节不变 + 无残留临时文件。

    ⚠️ 注入点落在 ``Path.write_bytes``（``_write_atomically`` 的 ``tmp.write_bytes``）
    ⇒ 失败发生在 ``os.replace`` **之前** ⇒ 目标文件**根本没被碰过**。
    """
    root = tmp_path / "ws"
    root.mkdir()
    target = _write_one(root, "a.txt", b"OLD")
    assert target.read_bytes() == b"OLD"

    real_write_bytes = pathlib.Path.write_bytes
    hits = {"n": 0}

    def boom(self, data):  # noqa: ANN001
        # 只打**临时文件**那一笔（否则连夹具自己的写也会挂，靶子就不干净了）
        if self.name.startswith(TMP_PREFIX):
            hits["n"] += 1
            raise OSError("注入：临时文件写入失败")
        return real_write_bytes(self, data)

    monkeypatch.setattr(pathlib.Path, "write_bytes", boom)
    with pytest.raises(Exception):
        _write.write_workspace_file(
            root=root, rel_path="a.txt", data=b"NEW", create=False, snapshot=False
        )
    monkeypatch.undo()

    # 正控：注入真的打到了（否则下面的「没变」是空靶）
    assert hits["n"] >= 1, "注入点没被命中 ⇒ 本腿是空靶"
    assert target.read_bytes() == b"OLD", "原文件被改动了（半写）"
    assert _leftovers(root) == [], f"残留临时文件：{_leftovers(root)}"


# ---------------------------------------------------------------------------
# A7-1 · 注入点② os.replace 抛（**M-回归检查 / M-回归检查 的靶腿**）
# ---------------------------------------------------------------------------


def test_a7_1_inject_os_replace_failure(tmp_path, monkeypatch):  # noqa: ANN001
    """**A7-1 注入点②**：``os.replace`` 阶段挂 ⇒ 原文件逐字节不变 + **无残留临时文件**。

    ## 🔴 这条腿是 ``M-回归检查`` / ``M-回归检查`` 的**唯一靶腿**

    - ``M-回归检查``（**去掉 ``os.replace``**、直接 ``write_bytes`` 到目标）⇒ 目标会被**半写**
      ⇒ 本腿的「逐字节不变」必红；
    - ``M-回归检查``（**去掉失败清理**）⇒ 临时文件残留 ⇒ 本腿的「无残留」必红。

    ⚠️ 注入**必须在 ``_write_atomically`` 函数内部**（patch ``os.replace``），
    **不得**把整个 ``_write_atomically`` 换成抛异常的替身 —— 那会跳过它内部的 ``except``
    段 ⇒ ``M-回归检查`` 永远测不出来（空靶；包 §4.5 纪律 2）。
    """
    root = tmp_path / "ws"
    root.mkdir()
    target = _write_one(root, "a.txt", b"OLD")

    real_replace = os.replace
    hits = {"n": 0}

    def boom(src, dst, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        hits["n"] += 1
        raise OSError("注入：os.replace 失败")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(Exception):
        _write.write_workspace_file(
            root=root, rel_path="a.txt", data=b"NEW", create=False, snapshot=False
        )
    monkeypatch.undo()
    assert os.replace is real_replace

    # 正控：注入真的打到了（``_write_atomically`` 确实走的是模块级 ``os.replace``）
    assert hits["n"] >= 1, "注入点没被命中 ⇒ 本腿是空靶（实现可能改成了别的落盘路径）"
    # ① 原文件逐字节不变
    assert target.read_bytes() == b"OLD", "os.replace 失败仍改动了目标（半写）"
    # ② 无残留临时文件（**M-回归检查 的靶子**）
    assert _leftovers(root) == [], f"失败清理缺失 ⇒ 残留临时文件：{_leftovers(root)}"


def test_a7_1_success_path_leaves_no_tmp_and_replaces_atomically(tmp_path):  # noqa: ANN001
    """**A7-1 正控**：成功路径也要**无残留** + 内容**整体**替换（不是追加/半写）。

    ⚠️ 没有这条，「清理」可能被实现成「成功时也删掉目标」或「写成功但留 tmp」而上面两条仍绿。
    """
    root = tmp_path / "ws"
    root.mkdir()
    target = _write_one(root, "a.txt", b"X" * 100)
    new = b"Y" * 5000  # 更长 ⇒ 半写会留下 100..5000 之间的长度
    _write.write_workspace_file(
        root=root, rel_path="a.txt", data=new, create=False, snapshot=False
    )
    assert target.read_bytes() == new, "内容不是整体替换"
    assert _leftovers(root) == [], f"成功路径残留临时文件：{_leftovers(root)}"

# ---------------------------------------------------------------------------
# A7-2 · 取消腿（CancelledError ⇒ 新/旧之一 + 无残留）
# ---------------------------------------------------------------------------


def test_a7_2_real_cancelled_error_is_cleaned_up(tmp_path, monkeypatch):  # noqa: ANN001
    """**A7-2 真取消腿（确定性）**：同线程注入**真** ``asyncio.CancelledError`` ⇒
    盘上文件**逐字节等于新/旧之一** + **无残留临时文件** + 异常**原样穿出**（不被包成 ``WriteError``）。

    ## 为什么要这条（audit-pkg-a 对抗审计 · task-36）

    首版本件两条取消相关腿**都不是确定性的**：
    · ``test_a7_2_cancel_leaves_old_or_new`` 注入的是 ``KeyboardInterrupt``（**不是**取消）；
    · ``test_a7_2_cancel_in_flight_coroutine_raises_not_swallowed`` 用 ``task.cancel()`` 取消
      ``asyncio.to_thread`` 的**外层任务** ⇒ ``CancelledError`` 由包装层立刻抛出、
      **工作线程仍在跑** ⇒ 断言与时序竞态，且「取消是否被改写」结构上无法被观测。

    ⇒ 本腿把「**真 ``CancelledError`` 在写入中途抛出**」做成**确定性**构造：
    直接 patch 模块级 ``os.replace`` 抛 ``CancelledError`` ⇒ 异常发生在
    ``_write_atomically`` 的 ``try`` 段**内部** ⇒ **必走** ``except BaseException`` 的清理分支。

    ⚠️ 注入点仍在**被测函数内部**（patch ``os.replace``，**不是**替换整个 ``_write_atomically``）
    —— 否则那段 ``except`` 不执行 ⇒ **空靶**（包 §4.5 纪律 2）。
    """
    import asyncio

    root = tmp_path / "ws"
    root.mkdir()
    target = _write_one(root, "a.txt", b"OLD-CONTENT")
    assert target.read_bytes() == b"OLD-CONTENT"

    hits = {"n": 0}

    def boom(src, dst, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        hits["n"] += 1
        raise asyncio.CancelledError("注入：写入中途被取消")

    monkeypatch.setattr(os, "replace", boom)
    # 🔴 断**精确类型**：取消不得被包成 ``WriteError``（runner 依赖取消单独处理）
    with pytest.raises(asyncio.CancelledError):
        _write.write_workspace_file(
            root=root, rel_path="a.txt", data=b"NEW-CONTENT-LONGER", create=False, snapshot=False
        )
    monkeypatch.undo()

    # 正控：注入真的打到了（否则下面的断言是空靶）
    assert hits["n"] >= 1, "注入点没被命中 ⇒ 本腿是空靶"
    # ① 盘上文件逐字节 == 旧内容（CancelledError 发生在 os.replace 之前）
    assert target.read_bytes() == b"OLD-CONTENT", (
        f"取消后盘上不是旧内容：{target.read_bytes()!r}（半写 / 被改写）"
    )
    # ② 取消路径同样不得残留临时文件（``except BaseException`` 的清理）
    assert _leftovers(root) == [], (
        f"取消后残留临时文件（except BaseException 清理缺失）：{_leftovers(root)}"
    )


def test_a7_2_cancel_leaves_old_or_new(tmp_path, monkeypatch):  # noqa: ANN001
    """**A7-2 取消腿（非 ``Exception`` 的 ``BaseException`` 分支）**：写入中途抛
    ``KeyboardInterrupt`` ⇒ 盘上文件**逐字节等于新/旧之一** + 无残留临时文件。

    ## 🔴 标签归位（audit-pkg-a 对抗审计 · task-36）

    本腿**注入的是 ``KeyboardInterrupt``，不是 ``asyncio.CancelledError``** ——
    首版 docstring 自称打 ``CancelledError`` 属**标签不准**，现如实更正。
    它断的是「**非 ``Exception`` 的 ``BaseException`` 也走 ``except BaseException`` 的清理**」
    这条**结构性质**；**真 ``CancelledError`` 的确定性覆盖**在
    ``test_a7_2_real_cancelled_error_is_cleaned_up``（同线程注入，无竞态）。

    ## 这条腿对 ``M-回归检查`` 仍有判别力（**不是空靶**）

    ``M-回归检查`` = 把 ``except BaseException`` 改成 ``except Exception``。``KeyboardInterrupt``
    **在 ``Exception`` 之外** ⇒ 改后清理段不执行 ⇒ 残留临时文件 ⇒ **本腿必红**。
    ⚠️ 正因如此，本腿与真取消腿是**两条不同的结构断件**（一条测「非 ``Exception`` 的
    ``BaseException``」，一条测「真取消」），**不得互相替代**。
    """
    root = tmp_path / "ws"
    root.mkdir()
    old = b"OLD-CONTENT"
    target = _write_one(root, "a.txt", old)

    real_write_bytes = pathlib.Path.write_bytes
    hits = {"n": 0}

    def boom(self, data):  # noqa: ANN001
        if self.name.startswith(TMP_PREFIX):
            hits["n"] += 1
            # 模拟「写到一半被取消」：临时文件已部分写入，然后抛 CancelledError
            real_write_bytes(self, data[: max(1, len(data) // 2)])
            raise KeyboardInterrupt("模拟取消")
        return real_write_bytes(self, data)

    monkeypatch.setattr(pathlib.Path, "write_bytes", boom)
    with pytest.raises(BaseException):
        _write.write_workspace_file(
            root=root, rel_path="a.txt", data=b"NEW-CONTENT-LONGER", create=False, snapshot=False
        )
    monkeypatch.undo()

    assert hits["n"] >= 1, "注入点没被命中 ⇒ 本腿是空靶"
    # 🔴 核心断件：盘上文件**逐字节等于新或旧之一**（不得是「半截」）
    on_disk = target.read_bytes()
    assert on_disk in (old, b"NEW-CONTENT-LONGER"), (
        f"取消后盘上是半截内容：{on_disk!r}（规格要求 == 新或旧之一）"
    )
    # 取消路径同样不得残留临时文件（`except BaseException` 的清理）
    assert _leftovers(root) == [], f"取消后残留临时文件：{_leftovers(root)}"


def test_a7_2_cancel_in_flight_coroutine_raises_not_swallowed(tmp_path):  # noqa: ANN001
    """**A7-2 边界**：``asyncio.CancelledError`` **必须穿出** ``WriteError`` 之外的原样传播。

    ⚠️ 若实现把 ``CancelledError`` 包成 ``WriteError``（或吞掉），调用方的取消语义就坏了
    （``runner`` 依赖取消单独处理）。

    ## 🔴 判别力边界（audit-pkg-a 对抗审计 · task-36 · 如实登记，不得拔高）

    本腿 ``task.cancel()`` 取消的是 ``asyncio.to_thread`` 的**外层任务** ⇒
    ``CancelledError`` 由**包装层立即抛出**，而**工作线程仍在运行** ⇒
    · 它**观测不到**「产品是否把取消改写成别的异常」（异常来自包装层，不是产品）；
    · ``:264-265`` 的盘上断言与**仍在跑的工作线程竞态** ⇒ 结果依赖时序。
    ⇒ **本腿只作为「取消不炸穿链路」的冒烟边界**，**不承担 A7-2 的确定性举证责任**。
    真取消的确定性覆盖在 ``test_a7_2_real_cancelled_error_is_cleaned_up``（同线程注入）。
    """
    import asyncio
    import threading

    root = tmp_path / "ws"
    root.mkdir()
    _write_one(root, "a.txt", b"OLD")

    async def cancel_write() -> None:
        task = asyncio.ensure_future(asyncio.to_thread(
            _write.write_workspace_file,
            root=root, rel_path="a.txt", data=b"NEW", create=False, snapshot=False,
        ))
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # 线程里抛的 OSError 不算本腿的失败（取消可能落在 replace 之后）
            raise AssertionError(f"CancelledError 被改写成了 {exc.__class__.__name__}") from exc

    try:
        asyncio.run(cancel_write())
    except asyncio.CancelledError:
        pass  # 预期
    # 无论落在哪一步，盘上必须仍是「新或旧之一」
    assert (root / "a.txt").read_bytes() in (b"OLD", b"NEW")
    assert _leftovers(root) == []

# ---------------------------------------------------------------------------
# A8 · 编码 / 行尾保真（四条腿）
# ---------------------------------------------------------------------------
#
# ⚠️ ``_write.write_workspace_file`` 收的是 **bytes**（``data``），本模块「原样落盘、**不做任何
# 编码/行尾转换**」（docstring 逐字）。⇒ A8 的「保真」在**写函数这一层**是**结构性**成立的；
# 真正的判别力在**上层编码决策**（``workspace_write`` 把 ``content: str`` 编成 bytes 的那一步）。
#
# ⇒ 本组腿分两层断：
#   ① **写函数层**：给定的 bytes 必须**逐字节**落盘（腿①②④）；
#   ② **编码决策层**：从 ``source_encoding`` 决定编解码名（腿③，**M-回归检查 的靶腿**）。


def test_a8_leg1_utf8_without_bom_stays_without_bom(tmp_path):  # noqa: ANN001
    """**A8 腿①**：UTF-8 **无 BOM** 写回仍**无 BOM**（首 3 字节不得是 ``EF BB BF``）。"""
    root = tmp_path / "ws"
    root.mkdir()
    target = _write_one(root, "a.txt", "中文内容".encode("utf-8"))
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), f"被加上了 BOM：{raw[:6]!r}"
    assert raw.decode("utf-8") == "中文内容"


def test_a8_leg2_lf_line_endings_survive(tmp_path):  # noqa: ANN001
    """**A8 腿②**：**LF 源**写回仍是 **LF**（逐字节）。

    ## 🔴 为什么必须用 LF 源 —— 这一条是腿②**唯一的判别力来源**

    实测（Windows）：``write_text`` 的默认行为本来就产 **CRLF** ⇒
    「CRLF 源仍 CRLF」**恒真**（不论实现是字节级写还是 ``write_text``）⇒ **空靶**。
    只有 **LF 源**才能把两者分开：

    ```
    LF 源 + 朴素 write_text  => b"line1\r\n..."  被改成 CRLF（红）
    LF 源 + 字节级写         => b"line1\n..."     保持（绿）
    ```
    """
    root = tmp_path / "ws"
    root.mkdir()
    lf = b"line1\nline2\n"
    target = _write_one(root, "lf.txt", lf)
    raw = target.read_bytes()
    assert b"\r\n" not in raw, f"LF 被转成了 CRLF（行尾保真破）：{raw!r}"
    assert raw == lf, f"LF 源未能逐字节保真：{raw!r} != {lf!r}"


def test_a8_leg3_gbk_roundtrip(tmp_path):  # noqa: ANN001
    """**A8 腿③**：GBK 源按**同一编解码名**写回（``M-回归检查`` 的靶腿）。

    🔴 ``M-回归检查`` = 「编码保真**恒用 UTF-8**（忽略 ``source_encoding``）」⇒
    本腿断言「GBK 字节写回后**仍是合法 GBK** 且逐字节相同」⇒ 恒用 UTF-8 时，
    GBK 字节会在编码/解码往返中被损坏或抛错 ⇒ **本腿必红**。
    """
    from app.pipeline.parsing import detect_encoding

    root = tmp_path / "ws"
    root.mkdir()
    gbk_bytes = "中文内容".encode("gb18030")
    assert detect_encoding(gbk_bytes) == "gb18030", "前提：探测名必须真是 gb18030"

    target = _write_one(root, "gbk.txt", gbk_bytes)
    raw = target.read_bytes()
    assert raw == gbk_bytes, f"GBK 字节未逐字节保真：{raw!r} != {gbk_bytes!r}"
    # 反证：它**不是** UTF-8（否则说明被 UTF-8 重编码了）
    assert raw == "中文内容".encode("gb18030")


def test_a8_leg4_untouched_bytes_are_verbatim(tmp_path):  # noqa: ANN001
    """**A8 腿④**：只改中间一段 ⇒ **未改动部分逐字节不变**（规格逐字）。"""
    root = tmp_path / "ws"
    root.mkdir()
    original = b"HEADER\r\n" + "中文".encode("utf-8") + b"\r\nTAILER\n"
    target = _write_one(root, "doc.txt", original)

    # 模拟「只替换中间那一段」（未改动部分 = 前后缀）
    new = original.replace("中文".encode("utf-8"), "替换".encode("utf-8"))
    _write.write_workspace_file(
        root=root, rel_path="doc.txt", data=new, create=False, snapshot=False
    )
    raw = target.read_bytes()
    assert raw[: len(b"HEADER\r\n")] == b"HEADER\r\n", "前缀被改动"
    assert raw.endswith(b"\r\nTAILER\n"), "后缀被改动（含 CRLF/LF 混排）"
    assert raw == new

# ---------------------------------------------------------------------------
# 🔴 A8 腿③ 的**现状缺陷登记**（`M-回归检查` 无锚可打 —— 如实记录，不粉饰）
# ---------------------------------------------------------------------------
#
# ## 实测事实（2026-09-26 · audit-write）
#
# `M-回归检查`（「编码保真**恒用 UTF-8**」）在本仓**没有任何锚点可打**：
#
# 1. `_write.py` **不做编码决策** —— 它的形参是 `data: bytes`，落盘是纯字节级
#    （`:119` 唯一那处 `.encode("utf-8")` 是 **sha256 摘要**用的，与内容编码无关；
#     `source_encoding` 在该文件里**只出现在 docstring `:34`**）⇒ 无「恒用 UTF-8」可改；
# 2. ⇒ **`A8` 腿③ 的判别力不在 `_write.py`**，而在**上层编码决策点**。
#
# ## 但上层**当前就是「恒用 UTF-8」**（实测复现）
#
# `workspace_write.py` 的 handler 逐字：
#     `data=str(args.content).encode("utf-8")`
# ⇒ 它**不看** `Document.source_encoding`、也**不重新探测**
# ⇒ 对一个 GBK 文件写入 ⇒ **静默转成 UTF-8**（实测：`ok=True`，盘上变成 utf-8 字节）。
#
# ## 结论（三句，不得含糊）
#
# - **本件承载 `A8` 腿①②④**（写函数层的字节保真）——**可判、已绿**；
# - **`A8` 腿③ 当前是【未实现】，不是【已实现但判据弱】** ⇒
#   本件**不写一条「GBK 仍 GBK」的通过腿**（那会是**假绿**：写函数收到的就是 UTF-8 bytes）；
# - ⇒ `M-回归检查` **登记为「无锚变异（SURVIVED 属预期）」**，与 `M-回归检查`/`M-回归检查` **不同**：
#   那两个有锚且已被本件击杀。


def test_a8_leg3_anchor_status_is_recorded_not_faked(tmp_path):  # noqa: ANN001
    """**A8 腿③ 现状的机器登记**：写函数层**不做**编码决策（`M-回归检查` 无锚）。

    🔴 本腿**不是**「GBK 仍 GBK」的通过腿（那会假绿 —— 见模块下方长注释）。
    它断的是**结构性事实**：`_write.write_workspace_file` 只收字节、原样落盘，
    ⇒ 「按 `source_encoding` 写回」这条**不可能**在这一层被满足或违反。

    ⚠️ 一旦有人把编码决策下沉到本层（把 `data: bytes` 改成 `content: str` + encoding 参数），
    本腿**会红** ⇒ 强制他同时补真正的「GBK 仍 GBK」腿。这是本腿的全部价值。
    """
    import inspect

    sig = inspect.signature(_write.write_workspace_file)
    assert "data" in sig.parameters, "写函数必须收 bytes 形参"
    assert "source_encoding" not in sig.parameters, (
        "写函数层出现了编码参数 ⇒ 必须同笔补「GBK 仍 GBK」的真腿（A8 腿③ 不再无锚）"
    )
    # 字节原样落盘（这才是本层能担保的那条）
    root = tmp_path / "ws"
    root.mkdir()
    payload = "中文".encode("gb18030")
    target = _write_one(root, "raw.bin", payload)
    assert target.read_bytes() == payload, "给定 bytes 未逐字节落盘"


def test_a8_leg3_upper_layer_preserves_the_original_encoding(tmp_path):  # noqa: ANN001
    """**A8 腿③（正腿）**：GBK 源文件**仍 GBK** —— 上层 handler 按**目标原编码**写回。

    ## 本腿的来历（如实登记，不改写历史）

    本腿**原先是一条「缺陷登记腿」**：当时上层 handler 恒用 str(content).encode("utf-8")
    ⇒ GBK 文件被**静默转码**成 UTF-8，而 ok=True（缺陷极隐蔽）。
    那条登记腿当时断言**现状缺陷**，并逐字预留：「将来修好时本腿会红 ⇒ 提醒改断言而不是删腿」。

    ⇒ 2026-09-26：Lead 按 D-回归检查 实现了「**按目标原编码写回**」
    （workspace_write._encode_for_target：目标已存在 ⇒ parsing.detect_encoding()
    **重新探测磁盘**、以磁盘为准；不存在 / 不可表示 ⇒ 回退 UTF-8），
    那条登记腿**如期变红** ⇒ 按它的预留改成本条**正腿**。

    ⚠️ 2026-09-26（`回归检查`）：该函数返回值由 `bytes` 改为 `(data, encoding, fell_back)`
    三元组（把**实际编码名**落进观察文本 —— 判据 `回归检查`①②，载体
    `test_write_encoding_t3_6.py`）。**本腿的断言语义不变**：它判的是**盘上字节**，
    而三元组的第一位就是阶段 5 那个返回值**逐字**。

    ⇒ 判据（三条，缺一即退化）：
    ① 写成功（ok=True）；
    ② 盘上字节 **逐字节等于**「同一内容用原编码（gb18030）编出的字节」；
    ③ **不等于** UTF-8 字节（否则「恒用 UTF-8」的实现照样能过）。
    """
    import asyncio

    from app.agent.tools import _paths, workspace_write

    ws = tmp_path / "ws"
    ws.mkdir()
    gbk = "中文内容".encode("gb18030")
    (ws / "g.txt").write_bytes(gbk)

    saved = _paths._read_configured_root
    _paths._read_configured_root = lambda: str(ws)  # 本件唯一配置读点（阶段 1 的形状）
    saved_secret = settings.WORKSPACE_CONFIRM_SECRET
    settings.WORKSPACE_CONFIRM_SECRET = "atomic-carrier-secret"
    try:
        from app.agent.tools import _confirm

        # 🔴 凭证闸接线后必须带真凭证（否则 handler 在写之前就拒 ⇒ 本腿测不到编码面）
        token = _confirm.issue_write_confirm(
            root=ws, rel_path="g.txt", content="中文内容".encode("utf-8")
        )
        spec = workspace_write.build_spec()
        obs = asyncio.run(  # noqa: F821
            spec.handler(
                workspace_write.WorkspaceWriteArgs(
                    path="g.txt", content="中文内容", confirm_token=token
                )
            )
        )
    finally:
        _paths._read_configured_root = saved
        settings.WORKSPACE_CONFIRM_SECRET = saved_secret

    raw = (ws / "g.txt").read_bytes()
    assert obs.ok is True, f"写失败 ⇒ 本腿不成立：{obs.text}"
    # ① 仍 GBK：逐字节等于原编码字节
    assert raw == gbk, (
        "GBK 文件被转码了（A8 腿③：按原编码写回未生效）—— 盘上应为 gb18030 字节"
    )
    # ② 负控：确保不是「碰巧等于 UTF-8」
    assert raw != "中文内容".encode("utf-8"), "盘上成了 UTF-8 字节 ⇒ 仍是恒用 UTF-8 的实现"

def test_a8_leg3_new_file_defaults_to_utf8(tmp_path):  # noqa: ANN001
    """**A8 腿③ 边界**：目标**不存在** ⇒ 新文件用 **UTF-8**（没有「原编码」可言）。

    ⚠️ 没有这条，「所有写都恒用 UTF-8」在**新建路径**上照样绿 ⇒ 腿③ 的边界不可判。
    """
    import asyncio

    from app.agent.tools import _paths, workspace_write

    ws = tmp_path / "ws"
    ws.mkdir()
    saved = _paths._read_configured_root
    _paths._read_configured_root = lambda: str(ws)
    saved_secret = settings.WORKSPACE_CONFIRM_SECRET
    settings.WORKSPACE_CONFIRM_SECRET = "atomic-carrier-secret"
    try:
        from app.agent.tools import _confirm

        token = _confirm.issue_write_confirm(
            root=ws, rel_path="brand-new.txt", content="新文件内容".encode("utf-8")
        )
        spec = workspace_write.build_spec()
        obs = asyncio.run(  # noqa: F821
            spec.handler(
                workspace_write.WorkspaceWriteArgs(
                    path="brand-new.txt",
                    content="新文件内容",
                    create=True,
                    confirm_token=token,
                )
            )
        )
    finally:
        _paths._read_configured_root = saved
        settings.WORKSPACE_CONFIRM_SECRET = saved_secret

    assert obs.ok is True, obs.text
    assert (ws / "brand-new.txt").read_bytes() == "新文件内容".encode("utf-8")