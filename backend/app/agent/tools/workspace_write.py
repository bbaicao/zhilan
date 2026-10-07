"""``workspace_write`` —— **写**工作区内的文件（专项工具升级-3 · 阶段 5 · `回归检查`）。

## 本模块的定位（`D-回归检查` ②：**工具声明层**，写本体在 `_write.py`）

本模块**只有声明**：`TOOL_NAME` / `DESCRIPTION` / `Args`（pydantic 四字段）/ `build_spec()`
—— 形状照阶段 3 三件只读工具的 `list_files.build_spec`。

**写动作本体**（原子写 / 写前快照 / 凭证闸 / 清缓存接线）归 **`_write.py`**（`D-回归检查` (甲) 档
的写侧私有实现），本模块**不得**自己实现一份：

    from app.agent.tools import _write   # 回归检查 / 回归检查 / 回归检查 落地

⇒ 判据 `A23`（批语义）与清缓存反向腿**打的是 `_write.py`**，不是本模块。

## 四条纪律

1. `build_spec()` + **闭包 handler**（与 `list_files` / `read_file` 同形）；
2. 失败**一律** `ToolObservation(ok=False)` 中文可读，**绝不炸穿**（I5）；
3. 🔴 `dangerous=True` 传 `False`（**不是**危险工具 —— 危险工具「连 `ToolSpec` 都不生成」，
   见 `tools/mcp.py:15`）；写工具的「不自动执行」由 `auto_executable()` 的 `needs_confirm` 判据承担（红线 4）；
4. 🔴 `needs_confirm=True`（单③ (a)「档 2 确认后落盘」）—— **必须显式写死**（`D-回归检查` 的 `B-B2`：
   漏标 ⇒ `safety.is_blocked` 的反向 fail-closed **一律拒绝** ⇒ 工具静默不可用）。

## 零材料（红线 8）

写工具调用的观察**恒** `materials == []` —— 与三件只读工具同款：**绝不**把文件内容当材料。

## 边界（红线 1 / 红线 2）

- `path` **只接受相对工作区根的路径**（绝对路径 ⇒ **直接拒**，`D-回归检查` ⑦）；
- 越界 / 敏感路径 / 只读文件 / 目录 / 符号链接目标 ⇒ 由**写侧判定函数**
  （`_write_paths.py`，`D-回归检查` (甲)）判拒 ⇒ `ok=False` + 中文可读；
- ⚠️ **不取进程 CWD**（红线 1）；`classify` 的读侧判定在写侧函数内部**复用**（不重写一遍）。

## 零凭证 ⇒ 零写（红线 13 第二款 / `A22`）

`confirm_token` 由**宿主侧**签发（`_confirm.py` 的 `issue_write_confirm`，`D-回归检查`），
**模型不得自己生成** —— 校验不过（缺失 / 错配 / 过期 / 重放）⇒ **拒绝且零 IO**。
⚠️ 本模块**不做**凭证校验本体（那在 `_write.py` / `_confirm.py`），只在 handler 里把
`args.confirm_token` **原样透传**进写函数。

## 🔴 本笔（`回归检查`）交付面与**待接线**登记

本笔交付 `build_spec()`（`D-回归检查` ⑧ 定档：**在 `回归检查` 首次注册进 `for_model()`**）。
⚠️ **归 `回归检查` / `回归检查` / `回归检查` 的本体**（`_write.write_workspace_file` / 快照 / 凭证）**
在那些笔落地前**尚不存在** `_write` 模块** ⇒ handler 走「能力未就绪」的可读失败分支
（`ok=False` + 中文），**绝不炸穿**，也**绝不在没有凭证闸的情况下真写盘**。
"""

from __future__ import annotations

import inspect
import codecs
from typing import Any

from pydantic import BaseModel, Field
from app.core.errors import DocumentParseError

from app.agent.registry import ToolSpec
from app.agent.types import ToolObservation

TOOL_NAME = "workspace_write"

#: 单行中文描述（`D-回归检查` ③：须含「写」「工作区文件」两个可辨识词）。
#: ⚠️ 与 `回归检查` 的披露文案（`disclosure.GROUP_DESCRIPTION`）**保持同一口径**。
DESCRIPTION = (
    "把完整新内容写入工作区文件（覆盖；路径一律相对工作区根，需宿主侧确认凭证）"
)

#: `_write` 本体尚未就绪时的失败文案（`ok=False`，绝不假装成功 —— I5）
NOT_READY_MESSAGE = "写通道尚未就绪：写入能力未装配完成，本次没有写任何文件。"


class WorkspaceWriteArgs(BaseModel):
    """工具入参（同时是给模型的 JSON Schema 来源）—— `D-回归检查` ④ **四字段，逐字冻结**。

    - `path`：**必填**；工作区根**相对**路径（绝对路径 ⇒ 由写侧判定直接拒）；
    - `content`：**必填**；要写入的**完整新内容**；
    - `confirm_token`：**可选**；**宿主侧签发的凭证**（`回归检查` / `D-回归检查` ④ 传参名逐字）；
    - `create`：**可选**；`True` ⇒ **新建语义**（`回归检查`：**已存在则拒**）；
      `False` ⇒ **覆盖语义**（`回归检查`）。
      🔴 `create` 标志**就是** `D-回归检查` 要求的「**同一个判定点分流**」的载体 ——
      覆盖与新建由**同一个参数**分流，**不得**出现「先覆盖再发现已存在」的竞态。
    """

    path: str = Field(description="相对工作区根的路径（必填；绝对路径会被拒绝）")
    content: str = Field(description="要写入的完整新内容（必填）")
    confirm_token: str | None = Field(
        default=None,
        description="宿主侧签发的确认凭证；缺失或不匹配时写调用会被拒绝",
    )
    create: bool = Field(
        default=False,
        description="True = 新建（目标已存在则拒绝）；False = 覆盖已存在的文件",
    )


#: 模块形状别名（每个工具模块都导出 `Args`，见 `list_files.py:88`）
Args = WorkspaceWriteArgs


#: 工作区根**未配置**时的失败文案（单⑨ (c)：根未配 ⇒ 功能不可用，与只读三件同款口径）
PATH_NO_ROOT_MESSAGE = "写文件未执行：尚未配置工作区根，本次没有写任何文件。"

#: 凭证闸拒绝文案（红线 13 第二款：**无有效凭证的写调用一律拒绝**，fail-closed）。
#: `reason` 是 `_confirm` 的五值之一（missing / replayed / expired / mismatch / ok）。
#: ⚠️ 文案里**只出现结果值**，**绝不**回显 token 原文（`C-R1(乙)`：不得落任何派生物）。
CONFIRM_REJECTED_TEMPLATE = "写文件未执行：确认凭证无效（{reason}），本次没有写任何文件。（{rel}）"


def _resolve_root() -> Any | None:
    """解析工作区根；**未配置 ⇒ ``None``**（红线 1：绝不取进程 CWD）。

    ⚠️ **必须在调用时现取**，不得缓存成模块级常量：`_paths.resolve_workspace_root()`
    是真解析链（内部有 `path.resolve()`），设置页改根后要让**同一进程**的后续调用**热生效**
    —— 这与 `bootstrap.py` 的注册门是同一条纪律（`A5b` 第 5 条腿「同进程跃迁」）。
    """
    try:
        from app.agent.tools import _paths  # noqa: PLC0415

        return _paths.resolve_workspace_root()
    except Exception:  # noqa: BLE001 —— 解析失败 ⇒ 当作未配置（fail-closed，绝不炸穿）
        return None

def _write_meta_from(outcome: Any) -> dict[str, Any]:
    """从写函数返回的 `WriteOutcome` 里取出审计用的**三键**（`回归检查`）。

    三键逐字对应 `agent_steps.meta` 的写通道专属键（`audit.WRITE_META_KEYS`）：
    `write_target` / `write_byte_delta` / `write_snapshot_id`。

    ⚠️ **缺件不报错、不补零**：`_write.py` 尚未落地时（本模块的「能力未就绪」分支）
    根本走不到这里；而 ① `outcome` 少某个键 ② `outcome` 不是预期形状 —— 两种都
    表现为「该键不挂」⇒ 审计侧 `_write_meta_digest` 的「有值才落」会把它略过。
    **不得**用 `0` / `""` 兜底：那会伪造一条「写了 0 字节」的审计行（比缺键更坏）。

    返回**只含取到的键**的 dict（可能是空 dict ⇒ `_write_meta_of` 会归 `None`）。
    """
    out: dict[str, Any] = {}
    for attr, key in (
        ("target", "write_target"),
        ("byte_delta", "write_byte_delta"),
        ("snapshot_id", "write_snapshot_id"),
        # 🔴 **回归检查（阶段 6）新增**：写前 vs 写后的行级 diff。
        # 它**不是** 回归检查 的审计三键之一（那三键是 `audit.WRITE_META_KEYS`，
        # 逐字冻结在 `agent_steps.meta` 的谓词上）⇒ 它**只**喂给用户可见的步骤帧。
        # 挂在这里（而不是让 runner 再算一份）的理由：「旧内容」只有写本体在**写盘之前**
        # 读到过；步骤层回读目标只能拿到**新**内容 ⇒ 那样算出来的 diff 恒为空、
        # 判据结构性恒真（阶段 4 `M-回归检查` / 阶段 5 `M-回归检查` 是同型陷阱）。
        ("write_diff", "write_diff"),
    ):
        value = getattr(outcome, attr, None)
        if value is None:
            continue
        out[key] = str(value) if attr == "target" else value
    return out

def _encode_for_target(root: Any, rel: str, content: str) -> tuple[bytes, str, bool]:
    """按**目标文件的原有编码**编码 `content`（`A8` 腿③ · `D-回归检查`）。

    ## 🔴 返回值（`回归检查` 改 · `回归检查` ①②）

    `(data, encoding, fell_back)` 三元组 —— **为什么不是「只返 bytes」**：
    阶段 5 已如实登记一条**静默降级**（见下方「已知限制」）：内容无法用原编码表示时
    回退 UTF-8，而用户 / 审计**三处都无感**（观察文本与 `meta` 都不含编码名）。
    ⇒ 本函数把「实际用的编码」与「是否发生了回退」**一路带上去**，由 handler 落进
    `ok=True` 的观察文本 ⇒ 这条降级从「静默」变成「可见」。

    - `data`：真正要落盘的字节（语义与阶段 5 的返回值**逐字相同**）；
    - `encoding`：**实际**使用的编码名（`gb18030` / `utf-8` …）—— 一定是「真的用了它」，
      不是「探测到了什么」：探测到 `gbk` 但回退时，这里落的是 `utf-8`（回退后的真名）；
    - `fell_back`：原文件明确非文本，或新内容无法用原编码表示时为 `True`。
      目标不存在或不能读取时按原默认 UTF-8 返回 `False`。

    ## 口径（写死）

    - 目标**已存在** ⇒ 读它的字节、`parsing.detect_encoding()` **重新探测**；
      ⚠️ **以磁盘探测值为准**，**不**盲信库内 `Document.source_encoding`
      （它是**入库时**的探测名，外部改动后可能已过期 —— `K-回归检查` 逐字）；
    - 目标**不存在** ⇒ UTF-8（新文件没有「原编码」可言）；
    - 探测失败 / 内容无法用该编码表示 ⇒ **回退 UTF-8**（绝不因编码问题让写失败）；
    - 行尾**不做任何转换**（字节级写，`K-回归检查`）。

    ⚠️ 本函数只**决定字节**；真正落盘与原子性归 `_write.write_workspace_file`（它收 bytes）。

    ## 🔴 三条**如实登记**（`write-design` 2026-09-26 只读审计提出，Lead 定档）

    1. **本函数【不是】判定权威** —— 它被拒时**只回退 UTF-8 并返回**（`:168`），
       **不抛、不拒**。「拒」由调用方（`workspace_write` handler → `_write` 的写侧判定）负责。
       ⇒ 🔴 **调用方不得调换「先 encode 后判」的次序**：那样越界路径会**白读一次盘**
       （IO 计数变 ⇒ `A9`「越界写零 IO」假红）。这是**显式契约**，不是可以省略的注释。
    2. **不读库内值** —— `D-回归检查` 原文有「并与库内 `Document.source_encoding` 比对」一句；
       本实现**没有**这一步，理由（如实登记，非省略）：
       · 本函数无 `db` / 无 `doc_id`，签名（`D-回归检查` ⑥）也不含它们；
       · 更根本：**工作区文件 ≠ 入库文档** —— `Document` 表**没有工作区路径列**
         （上传留档走 `data/uploads/`，与工作区根是两棵树）⇒ 多数工作区文件**压根没有「库内值」**。
       ⇒ 「**按磁盘探测值为准**」的**终态语义已达成**（磁盘值是**唯一**权威输入 ⇒ 二者不可能「不一致」）。
    3. **大文件会整份读入内存**（只为探测编码）——`detect_encoding` 是**全量**解码探测，
       本仓库既有的用法（`pipeline/ingest.py`）同样如此 ⇒ **本处与既有口径一致，不另立阈值**。

       🔴 **为什么不加「超阈值跳过探测」**（`write-design` 只读核，Lead 采纳 · 逐条给依据）：
       · **它省不掉那个读** —— `_write.py` 的 `before = dest.read_bytes()` 同样**无界**且
         **只要目标存在就必然执行**（与 `snapshot` 开关无关）⇒ 阈值只省掉本函数的**一次重复读**，
         **不能**对外宣称「避免大文件整份入内存」（那会是**不实陈述**）；
       · **它会新造一条静默降级** —— 9 MiB 的中文 CSV/日志（Excel 导出默认 GB18030，>8 MiB 极常见）
         会被静默转成 UTF-8，而 `A8` 腿③ 在这条路径上**判别力为 0**（写成功、只是编码变了）；
       · **会出现断崖** —— 7.9 MiB 保 GBK、8.1 MiB 不保 ⇒ 同目录两个同类文件行为不一致；
       · **会与入库侧分叉** —— `ingest.py` 的探测吃**全量字节**，写侧若按大小跳过，
         「按磁盘为准」在 >阈值 的文件上就变成**两把尺**；
       · **阈值本身需要第三个留痕状态**（`skipped_large`）—— 否则它就是**新的静默洞**。
       ⇒ 本阶段**不加**；登记为**阶段 6 承接项**（与下一条的 `fell_back` 留痕**同批**做）。

       ⚠️ **更干净的根治**（`write-design` 提出，**只登记不自决**）：本函数与 `_write.py` 读的是
       **同一个文件** ⇒ 「**读一次、把 bytes 往下传**」既消掉重复读、又顺带修掉那个无界读。
       但它要改 `D-回归检查` ⑥ **冻结的** `write_workspace_file` 签名（加一个 `before` 形参）
       ⇒ 属**裁决级**，本阶段不动。

    ## ⚠️ 一条**已知限制**（阶段 6 承接，本阶段只登记）

    内容**无法用原编码表示**时回退 UTF-8 ⇒ **编码被静默改掉**，而用户 / 审计**三处都无感**
    （观察文本与 `meta` 目前都不含编码名）。这与 `K-回归检查`（静默改坏行尾）是同族形态。
    ⇒ 建议改法（**本阶段未做**）：返回 `(data, encoding, fell_back)` 小三元组，
      由 handler 把**实际编码名**落观察文本、`fell_back=True` 时**显式**追一句中文提示。
      改动面 = 本模块调用点 + 同批补 `fell_back` 判据腿 ⇒ 属**阶段 6**。
    """
    try:
        from app.agent.tools import _paths  # noqa: PLC0415
        from app.pipeline.parsing import detect_encoding  # noqa: PLC0415

        verdict = _paths.classify(rel, root)
        if isinstance(verdict, _paths.Reject):
            # 拒的路径由写函数统一给中文失败，这里不管（编码名如实落 utf-8）
            return content.encode("utf-8"), "utf-8", False
        target = verdict.resolved
        if target.is_file():
            existing = target.read_bytes()
            enc = detect_encoding(existing) or "utf-8"
            try:
                encoded = content.encode(enc)
                if enc in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
                    bom = {
                        "utf-16-le": codecs.BOM_UTF16_LE, "utf-16-be": codecs.BOM_UTF16_BE,
                        "utf-32-le": codecs.BOM_UTF32_LE, "utf-32-be": codecs.BOM_UTF32_BE,
                    }[enc]
                    encoded = bom + encoded
                return encoded, enc, False
            except (UnicodeEncodeError, LookupError):
                # 内容含原编码表示不了的字符 ⇒ 回退 UTF-8（宁可改编码，不可写失败）
                # ⚠️ fell_back=True 是**唯一**的「发生了回退」信号：此时 encoding
                # 落**回退后的真名**（utf-8），不是「探测到的那个」—— 二者不可混。
                return content.encode("utf-8"), "utf-8", True
    except DocumentParseError:
        return content.encode("utf-8"), "utf-8", True
    except Exception:  # noqa: BLE001 —— 编码探测绝不炸穿（I5）
        pass
    return content.encode("utf-8"), "utf-8", False

def build_spec(*, write: Any = None) -> ToolSpec:
    """装配 `workspace_write` 的工具声明（**不在本函数里做任何写**）。

    `write` 只用于测试注入（**默认 None ⇒ 现取** `_write.write_workspace_file`）——
    形状与 `list_files.build_spec(root=...)` 同款：**每次调用现取**，不缓存模块级单例。

    ⚠️ **本体未就绪**（`_write` 尚未落地，见模块 docstring 末节）⇒ `handler` 返
    `ok=False` 的可读失败观察；**不得**在此临时实现一份写动作（那是 `回归检查` 的交付面，
    且会绕过 `回归检查` 的快照与 `回归检查` 的凭证闸 ⇒ 违红线 4 / 红线 13）。
    """

    async def handler(args: WorkspaceWriteArgs) -> ToolObservation:
        rel = (args.path or "").strip()
        if not rel:
            # 零材料 + 中文可读（I5）；**不夹紧**、**不落盘**
            return ToolObservation(text="写文件未执行：path 不能为空。", ok=False, materials=[])

        func = write
        if func is None:
            try:  # noqa: PLC0415 —— 延迟 import：本体未落地时**不得**让装配炸穿（I5）
                from app.agent.tools import _write as write_mod  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 —— 缺件 ⇒ 可读失败，绝不炸穿
                return ToolObservation(text=NOT_READY_MESSAGE, ok=False, materials=[])
            func = getattr(write_mod, "write_workspace_file", None)
        if func is None:
            return ToolObservation(text=NOT_READY_MESSAGE, ok=False, materials=[])

        root = _resolve_root()
        if root is None:
            # 根未配置 ⇒ 与三个只读工具同款处置（单⑨ (c)：功能默认不可用）
            return ToolObservation(text=PATH_NO_ROOT_MESSAGE, ok=False, materials=[])

        # ── 凭证闸（红线 13 第二款 · `A22` · `回归检查`/`回归检查` 接线） ──────────────
        # 🔴 **本闸必须落在这里**（模型可达的唯一写入口），**不是**落在 `_write.py`：
        # `_write.write_workspace_file` 是**写原语**（`A7`/`A8`/`A9`/`A21`/`A23`/`A28`
        # 的载体直接调它，需要能不带凭证地验原子性/保真/快照）；
        # **凭证闸是「工具调用」这一层的准入**，故落在 handler。
        #
        # 🔴 **签名口径**：绑定的是「**逻辑内容**」= `str(args.content).encode("utf-8")`
        # —— 与宿主 `issue_write_confirm(content=...)` 的入参一致；
        # **不受**目标文件原编码影响（否则同一份内容在 GBK 文件上会算两个 hash，
        # 宿主无法预知）。真正落盘的字节另有其编码（`_encode_for_target`）。
        #
        # ⚠️ **空密钥 / 无 token ⇒ 一律拒**（fail-closed）；拒时**零 IO**、不落盘。
        logical = str(args.content).encode("utf-8")
        from app.agent.tools import _confirm as confirm_mod  # noqa: PLC0415

        # 🔴 `回归修复`（verify 与 mark_used 分离非原子）：同一张有效凭证**并行**
        # 两次调用，两次 verify 可以都落在对方 mark_used 之前 ⇒ 都过闸都落盘（双写）。
        # ⇒ 把「verify → 拒即 return → 编码决策 → 写调用 → await → mark_used」整段
        # 包进 **per-token 临界区锁**（同 token 同锁，`_confirm.token_lock`）：
        # 后来者必须等前者完整走完（含 mark_used）才能进闸
        # ⇒ 它的 verify 一律查到 `_USED` 已命中 ⇒ 判 `replayed` ⇒ 恰好一次落盘。
        # 三条纪律（改这里前先读）：
        # ① verify 仍必须**先于**写调用（次序判据 `test_a22_gate_precedes_the_write_call`
        #    用源码偏移钉住，不得破坏）；
        # ② mark_used 必须在**锁释放之前**（`async with` 块内）—— 后来者 verify 一律
        #    replayed；失败路径不 mark_used、锁照常释放（「失败不消费」语义不变，
        #    用户可拿同一张有效凭证重试）；
        # ③ 锁的**弹出**（`release_token_lock`）放外层 `finally`（临界区外）；pop 竞态是
        #    良性的（权威状态是 `_USED` 集合，锁只压缩 verify→mark 窗口，
        #    见 `_confirm.release_token_lock` 的登记）。
        lock = confirm_mod.token_lock(args.confirm_token)
        try:
            async with lock:
                gate = confirm_mod.verify_write_confirm(
                    args.confirm_token, root=root, rel_path=rel, content=logical
                )
                if gate != "ok":
                    return ToolObservation(
                        text=CONFIRM_REJECTED_TEMPLATE.format(rel=rel, reason=gate),
                        ok=False,
                        materials=[],
                    )

                # 🔴 `回归检查`：编码决策**在调用写函数之前**取（`_encode_for_target` 不是判定权威，
                # 越界路径也由它编出字节，真正的「拒」在写侧判定 —— 见该函数 docstring 第 1 条）。
                # 三件套原样往上传：`data` 进写函数，`encoding`/`fell_back` 只进**观察文本**。
                encoded, used_encoding, fell_back = _encode_for_target(root, rel, str(args.content))
                try:
                    # 🔴 **五个 keyword-only 形参必须全给**（`D-回归检查` ⑥ 的逐字签名：
                    # `(root, rel_path, data, create, snapshot)`）—— 少一个 ⇒ `TypeError`
                    # ⇒ 每次写调用都 `ok=False`，且**三键永不产生**（`回归检查` 的 `A12a` 直接不可达）。
                    outcome = func(
                        root=root,
                        rel_path=rel,
                        # A8 腿③（D-回归检查）：按**目标原编码**写回（GBK 仍 GBK）；
                        # 目标不存在 / 探测失败 / 不可表示 ⇒ 回退 UTF-8（见 _encode_for_target）
                        data=encoded,
                        create=bool(args.create),
                        # `snapshot`：覆盖语义要写前快照；新建语义无旧内容可快照（`D-回归检查`）
                        snapshot=not bool(args.create),
                        confirm_token=args.confirm_token,
                    )
                    # 🔴 **awaitable 必须 await**（2026-09-26 实测抓到的真缺陷）：
                    # 本 handler 是 `async def`，若写函数也是 `async def` 而我们同步调用，
                    # 拿到的是一个**未被 await 的协程对象** —— 它既不是 `WriteOutcome`，
                    # 也不会执行任何写动作。后果不是报错，而是**静默的假成功**：
                    # `getattr(outcome, "text", None)` 取不到 ⇒ 回落到「已写入…」文案 ⇒
                    # `ok=True`，而**盘上什么都没写**、`write_meta` 恒空。
                    # ⇒ 这是比 `TypeError` 更坏的一类：`TypeError` 会被 except 变成可读失败，
                    # 而「未 await 的协程」会伪装成成功（含 `A12a` 的成功行腿照样绿）。
                    # ⇒ 判据见 `tests/agent/test_write_audit_t3_5.py` 的 `test_handler_supports_async_write`。
                    if inspect.isawaitable(outcome):
                        outcome = await outcome
                except Exception as exc:  # noqa: BLE001 —— 写失败**必须**可读、不得炸穿（I5）
                    # 🔴 `回归修复`：**拒因必须透传**。写侧把「越界 / 敏感模板 / 目标已存在 /
                    # 写前快照失败」统一包装成**中文拒因**（`_write.WriteError`，其 docstring 逐字：
                    # 「裸 OSError 的 errno 文案不适合直接给用户看，且会泄露绝对路径 ——
                    # 故本模块统一包装成中文」）。只取 `exc.__class__.__name__` ⇒ 用户与模型只看到
                    # 「写文件未执行：WriteError（a.txt）。」，既不知为何被拒、也无从改参数重试。
                    # ⚠️ **只对 `WriteError` 透传**：它的文案由 `_write` / `_paths` 逐字构造，
                    # 只含**相对路径**与中文原因（不含绝对根路径与密钥）；其余异常（尤其
                    # `TestModeRepoWriteError` 的 `实测解析值：{path}`）仍回落类名
                    # ⇒ 不回灌绝对路径（与 S2 的 `回归修复` 遮蔽口径一致）。
                    # ⚠️ 取类型走**惰性 import**：注入 `write` 替身的腿里 `_write` 未必已 import；
                    # 任何取类型失败都回落类名，**绝不**从这里炸穿（I5 / fail-closed 不变）。
                    reason = exc.__class__.__name__
                    try:
                        from app.agent.tools._write import WriteError as _WriteError  # noqa: PLC0415

                        if isinstance(exc, _WriteError):
                            reason = str(exc) or reason
                    except Exception:  # noqa: BLE001 —— 取类型失败 ⇒ 回落类名，不炸穿
                        reason = exc.__class__.__name__
                    return ToolObservation(
                        text=f"写文件未执行：{reason}（{rel}）。", ok=False, materials=[]
                    )
                # 🔴 **凭证只在【写成功之后】消费**（`_confirm` 的调用契约逐字：「只在 ok 之后才消费」）
                # ⇒ 失败路径**不消费**，用户可以拿同一张凭证重试（见载体的「失败校验不消费」腿）。
                # ⚠️ **必须在锁释放之前**（`回归修复`）：mark_used 是临界区的最后一步，
                # 后来者进闸时 `_USED` 必已命中 ⇒ 必判 replayed。
                try:
                    confirm_mod.mark_used(args.confirm_token)
                except Exception:  # noqa: BLE001 —— 消费失败不得让已成功的写变成失败
                    pass
        finally:
            confirm_mod.release_token_lock(args.confirm_token)

        text = getattr(outcome, "text", None) or f"已写入工作区文件：{rel}"
        # 🔴 `回归检查` **留痕收口**（`回归检查` ①②）：把**实际编码名**追进 ok=True 的观察文本。
        #
        # 为什么落在**观察文本**而不是 `write_meta`：`write_meta` 的三键被
        # `audit.WRITE_META_KEYS` **逐字冻结**（`D-回归检查` ⑥），多挂一键会改审计面；
        # 而「可见面」正是本阶段（阶段 6）的交付面 —— 观察文本是模型与用户都能看到的那一处。
        #
        # `fell_back=True` 时**显式**追一句中文提示：这是阶段 5 登记的「静默降级」
        #（内容无法用原编码表示 ⇒ 编码被换掉，三处都无感）的唯一可见出口。
        text = f"{text}（实际编码：{used_encoding}）"
        if fell_back:
            text += (
                        "注意：无法沿用原文件编码，已回退为 UTF-8 写入"
                "（该文件的编码因此被改变）。"
            )
        # A15 / 红线 8：**恒零材料**（写工具的观察不得进 materials）
        observation = ToolObservation(text=str(text), ok=True, materials=[])
        # 🔴 `回归检查`：把写通道专属三键**从 WriteOutcome 原样**挂到观察上，供
        # `runner._write_meta_of` 取走并落 `agent_steps.meta`。
        # ⚠️ **三键的取值一律来自 `outcome`**（`D-回归检查` ⑥：不得由调用侧另算
        # —— 否则「审计里记的」与「实际写的」会漂移）。
        # ⚠️ **失败路径不挂**：`ok=False` 的观察没有「写了多少字节」可言，
        # 硬记一个 0 会让「没写成」与「写了 0 字节」不可区分（见 `_write_meta_digest` 口径 2）。
        observation.write_meta = _write_meta_from(outcome)
        return observation

    return ToolSpec(
        name=TOOL_NAME,
        description=DESCRIPTION,
        parameters_schema=WorkspaceWriteArgs.model_json_schema(),
        handler=handler,
        parameters=WorkspaceWriteArgs,
        # 单③ (a) 档 2：需确认 ⇒ 不进 auto_executable（红线 4）、不进「漏标即拒」名单（A20）
        needs_confirm=True,
    )


__all__ = [
    "Args",
    "DESCRIPTION",
    "NOT_READY_MESSAGE",
    "PATH_NO_ROOT_MESSAGE",
    "TOOL_NAME",
    "WorkspaceWriteArgs",
    "build_spec",
]
