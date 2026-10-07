"""回归修复（2026-09-28 · 缺陷清单-2 阶段五）：发送失败回填缺图片——附件数据直接丢失。

根因：``submitChat`` 发送即清空附件区（``if (queuedImages === undefined) setImages([]);``），
附件数据（base64 + name）此后只活在请求体里；流中途失败走 catch 的 error 分支（stillThere）
时只回填了输入框文字（``setChatInput(text)``），附件快照无处可取 ⇒ 用户带图发送失败后
图片凭空消失，只能重新选图。

修法 = 发送前把完整附件快照进局部变量 ``sentImages``
（``queuedImages !== undefined ? null : images``——队列续发路径不带附件区、快照置 null
不参与回填），catch 的 error 分支在快照非空时把它装回附件区
（``if (sentImages && sentImages.length) setImages(sentImages);``）；
abort 分支的「不回填」是有意设计（回归修复 的地盘），保持不动。

判据面（本仓无 JS 测试框架 ⇒ 界面面只有**静态判别力**，照 099 先例全部在**剔除整行
``//`` 注释**后的 ``App.tsx`` 上断言——「注释掉」与「删掉」在判据面上等价）：

- 腿①（快照在清空前铸好）：``sentImages`` 定义存在，且其位置先于「发送即清空附件区」、
  先于 ``await streamChat(``（顺序即时间序——清空之后才快照就拿不到了）；
- 腿②（error 分支回填）：error 分支块内 ``setChatInput(text);`` 与
  ``if (sentImages && sentImages.length) setImages(sentImages);`` 同时在位，且先文字后图片；
- 腿③（对照腿·abort 分支有意不回填）：abort 分支块内**不含** ``setImages(`` 也**不含**
  ``setChatInput(``——这是 143 的地盘 + 有意设计，防止有人「顺手统一」两分支；
- 腿④（负控·快照只属于交互路径）：``sentImages`` 定义行含
  ``queuedImages !== undefined ? null : images``（队列续发路径快照为 null、不参与回填
  ——登记口径）。

回退口径：反向应用 142 的 diff（删快照行 / 删回填行）后**腿①②红**；腿③④不受影响
（它们钉的是「另一分支不动」与「队列路径不参与」，回退 142 不触碰这两处）。

⛔ 口径：静态判别力 ≠ 运行期渲染已验证（本仓无 JS 运行时测试设施）。
⛔ 口径：本判据只覆盖**交互路径**（用户在输入区带图发送后失败的回填）；队列续发失败的
图片归属（图随 ``queuedImages`` 走）登记不动，不在本条目范围。
"""

from __future__ import annotations

from pathlib import Path

FRONTEND_APP = Path(__file__).resolve().parents[3] / "frontend" / "src" / "App.tsx"

SNAPSHOT_LINE = "const sentImages = queuedImages !== undefined ? null : images;"
CLEAR_LINE = "if (queuedImages === undefined) setImages([]);"
RESTORE_LINE = "if (sentImages && sentImages.length) setImages(sentImages);"


def _app_source() -> str:
    """读 ``App.tsx`` 全文，**剔除整行注释**后再返回（判据纪律照 099 先例）。"""
    lines = FRONTEND_APP.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("//"))


def _block(source: str, start: str, end: str) -> str:
    """截取 ``start`` 起、到 ``end`` 止的源码片段（都取**首次**出现）。"""
    begin = source.index(start)
    stop = source.index(end, begin)
    return source[begin:stop]


# --- 腿①：快照在附件区被清空之前铸好 -----------------------------------------


def test_snapshot_cast_before_attachments_cleared():
    """``sentImages`` 必须存在，且先于「发送即清空附件区」、先于发起流（顺序即时间序）。"""
    source = _app_source()
    assert SNAPSHOT_LINE in source
    snapshot_at = source.index(SNAPSHOT_LINE)
    clear_at = source.index(CLEAR_LINE)
    await_at = source.index("await streamChat(")
    assert snapshot_at < clear_at, "快照必须先于清空附件区——清空之后才快照就拿不到了"
    assert clear_at < await_at, "清空附件区必须发生在发起流之前（既有行为，一并钉住）"


# --- 腿②：error 分支把快照装回附件区 ------------------------------------------


def test_error_branch_restores_image_snapshot():
    """error 分支（stillThere）必须先回填输入框文字、再把非空快照装回附件区。"""
    error_block = _block(
        _app_source(), "} else if (stillThere) {", "const reason = err instanceof ApiError"
    )
    assert "setChatInput(text);" in error_block
    assert RESTORE_LINE in error_block
    assert error_block.index("setChatInput(text);") < error_block.index(
        "setImages(sentImages)"
    ), "先文字后图片——两处回填都在时顺序也钉住"


# --- 腿③：对照腿——abort 分支有意不回填（143 的地盘） ---------------------------


def test_abort_branch_intentionally_does_not_restore():
    """abort 分支不得回填附件区 / 输入框——「不回填」是有意设计，防止被顺手统一。"""
    abort_block = _block(_app_source(), "if (controller.signal.aborted) {", "} else if (stillThere) {")
    assert "setImages(" not in abort_block
    assert "setChatInput(" not in abort_block


# --- 腿④：负控——快照只属于交互路径 --------------------------------------------


def test_snapshot_belongs_to_interactive_path_only():
    """队列续发路径（queuedImages !== undefined）快照为 null，不参与失败回填（登记口径）。"""
    line = next(l for l in _app_source().splitlines() if "const sentImages" in l)
    assert "queuedImages !== undefined ? null : images" in line
