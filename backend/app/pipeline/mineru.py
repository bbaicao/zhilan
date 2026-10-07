"""可选 MinerU 适配器：复杂 PDF / 扫描件（公式 / 表格 / 多栏 / 无文本层）走 MinerU，
图片（jpg / jpeg / png / bmp / tiff / webp）走 MinerU 图片 OCR（阶段 3 · 条目 3.2）。

设计纪律（统筹 §3.3 / 条目 2.1 / 条目 3.2）：
- ``MINERU_ENABLE`` 关时，**本模块、``parsing`` 在 PDF 与图片分支都绝不 import mineru / magic_pdf**
  （MinerU 是重依赖，import 期不许炸穿）。Pillow 同样只在图片兜底路径里**惰性** import。
- ``available()`` 是唯一会尝试 import 的入口；``parse_pdf`` / ``parse_image`` 只在调用方确认
  ``available()`` 为 True 后才被调用，且任何异常转成中文可读的 ``DocumentParseError``。
- **API 形态随安装版本变化**，故 ``_parse_with_mineru`` 走「多入口依次尝试」（顺序按实际版本）：
  ``mineru.parser.parse``（3.x/4.0 官方高层门面）→ ``mineru.parser.MinerUParser``（细粒度退路）
  → ``mineru.parse``（旧版高层入口）→ ``magic_pdf`` UNIPipe（2.x 底层管道）。
  全部不匹配时给**明确的「API 不匹配」报错**，绝不静默吞掉——静默吞掉会让「装了 MinerU 却始终走 pypdf」
  变成查不出的哑故障。
- **「入口不存在」与「解析失败」严格分开**：只有**导入/能力探测期**的失败才换下一个入口
  （哨兵 :class:`_EntryUnavailable`）；一旦进入真正的解析调用，任何异常都原样冒泡，
  由 ``parse_pdf`` 忠实转译成含真因的中文报错。否则 MinerU 运行期的 ``AttributeError``（内部 bug）
  或缺可选依赖会被误报成「版本 API 不匹配」，还会让同一份 PDF 被逐入口重复解析（独立复核 F1）。
- **出口不变量**：``(text, pages)`` 里每个 ``page`` 都必须是 ``text`` 的字面子串——下游
  ``chunking._assign_page`` 靠「页文本在全文里定位」标页码，破坏了它页码就会静默变 ``None``
  （独立复核 F2）。故优先用带页边界标记的渲染结果切页，并对标记做页数上限自检。

阶段 4（VLM 后端适配）新增的两件事，以及它们**为什么**要这么做：
- ``_select_backend()``：把 ``MINERU_BACKEND``（``auto``/``pipeline``/``vlm``）与新开关
  ``MINERU_VLM_ENABLE`` 解析成**实际路由** ``"vlm"`` / ``"pipeline"``；只有 ``"vlm"`` 路由才会显式传
  ``VlmConfig(engine="llama-cpp")``（进程内 llama.cpp，不引入常驻服务）。
- ``_mineru_tier()`` 随之**分档**：VLM 路由走 ``standard``（high effort），pipeline 路由走 ``basic``
  （medium，ONNX-only）。**不再一律 standard**——理由见 :func:`_mineru_tier`：
  MinerU 4.0 的 ``standard`` 会构造 VLM 预测器，需要 VLM/GGUF。
  对只安装 ONNX 模型的环境强制使用 standard，会让 OCR 路径失效。

条目 D2（VLM 失败逐级降级）新增的一件事：:func:`parse_pdf` 在**发起解析之前**记下本次实际档位
（:func:`_mineru_tier` 的值），**失败或空结果后仅当该档位 ∈ {``standard``, ``advanced``}** 时
**降级重试一次 ``basic``**；``basic`` / ``flash`` / 未知档位**一律不重试**（未知档位不猜）。
三条不能动的细节：① 重试**走 pipeline 链**（:func:`_parse_pdf_entrypoints`）而**不走**
:func:`_parse_vlm`——后者会因 ``_select_backend()`` 仍是 ``vlm`` 而照带 ``vlm_config``，
而重试**不带** VLM 配置；② 主入口与备用入口的档位来自**同一决策点**（入口 ② 原先根本不传 tier，
吃的是 MinerU 构造默认值 ``standard`` = VLM 档，不补这一处降级就是假的）；③ 档位在解析前取一次，
失败后**不重算**。注意 ``standard`` / ``advanced`` 的路由是 ``pipeline``（见 :func:`_resolve_backend`），
所以「只在 VLM 分支加重试」会漏掉最需要降级的那两种。

参考源码核对（2026-09-17，只读基线快照：MinerU 上游源码 `MinerU-master`，
版本 **4.0.0**，见 `mineru/version.py`）：
- 唯一高层入口：``mineru/parser/__init__.py:31`` ``parse(path, *, tier, ocr_mode, image_analysis,
  page_range, source_context, vlm_config) -> ParseResult``——**只收路径，不收 bytes**；
- ``mineru/parser/base.py:20`` ``@dataclass ParseResult``；``:68`` ``markdown(*, add_markers, ...) -> str``；
  ``:38`` ``pages`` property → ``list[PageInfo]``（页边界真源）；
- 字节入口 ``mineru/backend/analyze.py:32`` ``doc_analyze(file_bytes, ...)`` 只到结构化中间层，
  不出 Markdown（要自己再调 ``mineru.render.markdown``），故不作主入口；
- **没有 ``magic_pdf`` 包**（changelog 载明 2.x 起包名从 ``magic-pdf`` 改为 ``mineru``），
  ``UNIPipe`` / ``BytesReader`` / ``pipe_mk_markdown`` 在 4.0 全库零匹配；
- **没有 ``<!-- PageBreak -->`` 字面量**：渲染默认页间无分隔符，``RenderMode.FULL``
  （``add_markers=True``）才输出 ``"\\n\\n---\\n\\n"``（``tests/unittest/test_render_api.py:172`` 断言）。
  故阶段文档 §条目 2.1 给的 ``magic_pdf`` 调用式与 ``PageBreak`` 标记**对应的是 MinerU 2.x**，
  在 4.0 环境分别降为退路与候选标记。
- **图片无需另找 API**（阶段 3 · 条目 3.2 核对，2026-09-17）：``mineru/filetypes.py:30``
  ``IMAGE_EXTENSIONS = {png, jpg, jpeg, webp, gif, bmp, tiff, jp2}``；
  ``mineru/parser/mineru_parser.py:155`` ``_prepare_input`` 命中图片后缀即
  ``file_bytes = PDFDocument.from_image(file_bytes).bytes`` 转成单页 PDF 再解析。
  故图片走 ``mineru.parser.parse(图片路径)`` 即可（后缀必须真实），只有该入口不存在时才用
  Pillow 自己包单页 PDF 走 PDF 四入口（:func:`parse_image` 的兜底路径）。
"""

import importlib
import importlib.util
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from io import BytesIO
from pathlib import Path

from app.core.config import settings
from app.core.errors import DocumentParseError

log = logging.getLogger(__name__)

# 图片入 OCR 前的最长边上限（像素）：手机拍照常见 4000×3000，直通会让「包成单页 PDF」的
# 体积逼近 20MB 上传限制、且 OCR 侧按像素吃内存。2000px 对印刷体识别率无损，是阶段 3
# 文档 §六「等比压缩（长边 ≤ 2000px）」的指定值。
_IMAGE_MAX_SIDE = 2000

# 图片 OCR 文案里的格式清单（**本模块单一来源**）：`parse_image` 认不出的字节、以及 `_image_format`
# 自己的解释都用它。原先把清单硬编码在报错串里、且**漏了 `.jpeg`**（独立复核 C4）——jpeg 与 jpg
# 是同族格式，提示里少一个会误导用户把一个能用的后缀当成不支持。
# 注意与 `parsing.SUPPORTED_FORMATS_HINT` 区分：那份是**全部 44 项后缀 = 43 个 token**的清单（面向上传/扫描拒绝提示），
# 归 parsing 层；本常量只管**图片这 6 种**。mineru 属下层模块，不该反向去 import 上层清单。
_IMAGE_FORMATS_HINT = "jpg / jpeg / png / bmp / tiff / webp"


class _EntryUnavailable(Exception):
    """「本版本没有这个解析入口」——**只允许在导入 / 能力探测期抛出**。

    与「解析运行期异常」严格区分：前者换下一个入口重试，后者直接冒泡交给
    :func:`parse_pdf` 忠实转译（见 :func:`_parse_with_mineru` 的说明）。
    """

# 分页标记候选：MinerU 各版本、各渲染模式下可能出现过的页分隔写法。
# 3.x 默认渲染不带标记（页边界在 ParseResult.pages 里），这里保留是为了兼容
# 2.x / add_markers 模式的输出。
_PAGE_BREAK_MARKERS = ("<!-- PageBreak -->", "<!-- PageBreak-->", "\n\n---\n\n")

# 内嵌图片数据（阶段 5 · 条目 5.3 真机抽查发现的缺陷 `回归修复`）。
#
# **实测证据（2026-09-18，样本 A = 7.2MB / 16 页原生 PDF）**：MinerU 4.0 的两种 markdown 渲染
# （`add_markers=True` 与默认）**都会把内嵌图以内联 base64 数据 URI 塞进正文**——
# 全文 **3,637,885 字**，其中 **17 行就占 3,581,032 字（98.4%）**，最长一行 **814,352 字**，
# 全部以 `![](data:image/jpeg;base64,` 开头。这些字节原样进清洗/切块后：
# 入库 **5214 块 / 415.7 万字**（正常量级 ~70 块），且该文档**向量化失败**（400 Bad Request）。
# 对照：块数据态（`_blocks_pages`）只有 71,815 字、0 个 data URI —— 说明「正文可以很干净，
# 脏的只是 markdown 渲染这一路」。
#
# 故取文前先剥离内嵌二进制。**三条口径**：
# 1. 只剥离「不是正文」的东西（内嵌图数据、残留 base64 长串），**不动正常 Markdown**；
# 2. **在切页之前剥离**：`_markdown_pages` 要求每页是全文的字面子串，先剥后切才不破坏该不变量；
# 3. 剥离是**净化**不是**失败**：剥完仍为空时才回落到块数据态（与既有的取文顺序一致）。
#
# ⚠️ **正则的两处收紧（2026-09-18 对抗性复核抓出的缺陷 `回归修复`，逐条有实测）**：
# - **负载类一律不跨空白**（去掉 `re.S`、`[^)\s]*` 而不是 `[^)]*`）：否则「内嵌图没有右括号」时
#   `[^)]*` 会一路吃到文档后面**任意一个** `)` —— 实测 11,800 字 → **0 字**，正文被静默删光。
#   这是**数据丢失**，比「垃圾留在库里」严重得多，故负载必须锚在同一个 URI 内。
# - **兜底正则的 MIME 类必须含 `/`**：原来的 `[A-Za-z0-9.+-]+` 匹配不到 `image/jpeg` 这类真实 MIME，
#   实测 12 个真实形态**全不命中**（只有 `data:foo;base64,` 这种无斜杠的假 MIME 命中）⇒ bug-011 没修净。
#   同时负载类里的 `\s` **必须去掉**：补上 `/` 之后再允许空白，正则会立刻开始吞掉后续正文（实测 in=311 → out=21）。
#   这两条是**互相遮蔽**的——只修一条会更糟。
# - 两侧都加**长度上界**：`![` 后面没有 `]`、`<img src=` 没有闭合引号时，无界量词会退化成二次方扫描
#   （实测 108KB 输入 597ms）；加上界后是线性。
#   ⚠️ 上界必须写**纯数字**（`{0,5000000}`）：Python 的 `re` 不认数字分隔符下划线，
#   `{0,5_000_000}` 会被当成**字面量**去匹配（正则静默失效、一个字节都剥不掉）。
_DATA_URI_IMAGE_RE = re.compile(r"!\[[^\]]{0,500}\]\(\s*data:[^)\s]{0,5000000}\)")
_DATA_URI_ANY_RE = re.compile(r"data:[^;,\s]{1,160};base64,[A-Za-z0-9+/=]{64,5000000}")
_HTML_DATA_IMG_RE = re.compile(r"<img\b[^>]{0,2000}?src=[\"']data:[^\"']{0,5000000}[\"']", re.I)
# 剥离量小于这个阈值就不打 INFO（避免「只少了一个换行」也报「已剥离内嵌二进制」这种误导性日志）
_STRIP_LOG_MIN_CHARS = 64
# 兜底网：极长、**无空白**、**整行都在 base64 字母表内**、且大小写混合的行 ⇒ 判为数据而非散文
_LONG_LINE_CHARS = 4096
_BASE64_ALPHABET = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)


def _is_data_line(line: str) -> bool:
    """极长数据行判定（兜底网，只在两条 data-URI 正则都没命中时才起作用）。

    **为什么不是「字母表字符占比 ≥ 0.9」**（对抗性复核 F4 的教训）：占比判据会误删正常长行——
    实测 4,096+ 字的**纯数字串**（如 π 的数字展开）、**长 URL**、正文里贴的 base64 *示例* 全部被判成数据。
    改成三道**同时**成立的闸门后，这三类都安全：① 长度达阈值；② **整行无表外字符**（空白、`.`、`:`、`-`
    等都算表外 ⇒ URL/散文天然出局）；③ **大小写混合**（纯数字串出局，真实 base64 负载必含大小写）。
    """
    if len(line) < _LONG_LINE_CHARS:
        return False
    if any(char not in _BASE64_ALPHABET for char in line):
        return False
    return any(char.islower() for char in line) and any(char.isupper() for char in line)


def _first_page_marker(text: str) -> str | None:
    """返回文本里**最先出现**的页分隔标记（没有则 ``None``）。"""
    found = [(text.find(marker), marker) for marker in _PAGE_BREAK_MARKERS if marker in text]
    return min(found)[1] if found else None


def _strip_segment(segment: str) -> str:
    """净化**一段**（两处页标记之间的文本）：剥内嵌图 → 剔长数据行 → 压空行。

    ⚠️ **本函数不做首尾 `strip()`**（对抗性复核 F2 的修法要求）：页标记 `<!-- PageBreak -->`
    本身**不含换行**，它两侧的空行是分段之间唯一的换行来源；一旦在这里 strip，
    `marker.join(...)` 会把 `A\\n\\n<!-- PageBreak -->\\n\\nB` 压成 `A<!-- PageBreak -->B`（既有用例实测踩到）。
    而且整篇 strip 会吃掉「首页为空」时页标记的前导 `\\n\\n`，标记随之失效 ⇒ 页数塌缩、页码静默前移。
    """
    cleaned = _HTML_DATA_IMG_RE.sub("", segment)
    cleaned = _DATA_URI_IMAGE_RE.sub("", cleaned)
    cleaned = _DATA_URI_ANY_RE.sub("", cleaned)
    # 用 split("\n") 而不是 splitlines()：后者会把段尾的 `\n\n` 吃掉一个换行，
    # 段与页标记拼接后就成了 `A\n<!-- PageBreak -->`（实测把既有用例打红）。
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    kept = [line for line in cleaned.split("\n") if not _is_data_line(line)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept))


def _strip_embedded_binary(text: str) -> str:
    """剥离 MinerU Markdown 里的内嵌图片数据与残留 base64 长串（见上方 `_DATA_URI_IMAGE_RE` 的实测证据）。

    **按页分段净化，保留页分隔标记**（对抗性复核 F2）：整篇 `strip()` 会吃掉首页为空时页标记的
    前导 `\\n\\n`，标记随之消失 ⇒ 页数塌缩、后续页**静默前移**（第 2 页的正文被标成第 1 页），
    还会让跨页页眉页脚检测（要求 ≥2 页）直接放弃。分段净化 + `marker.join(...)` 保标记原样、
    **空页也占位**，页序号因此不漂移。

    返回净化后的文本；**任何情况下都不抛异常**——净化是尽力而为，剥不动就原样返回，
    绝不因此让解析链失败（统筹不变量 3：失败逐级回落不炸穿）。
    """
    if not text:
        return text
    original = len(text)
    try:
        marker = _first_page_marker(text)
        if marker:
            cleaned = marker.join(_strip_segment(part) for part in text.split(marker))
        else:
            cleaned = _strip_segment(text)
    except Exception as exc:  # noqa: BLE001 —— 净化失败绝不能让解析失败
        log.warning("剥离内嵌二进制失败，按原文返回：%s", exc)
        return text
    removed = original - len(cleaned)
    if removed >= _STRIP_LOG_MIN_CHARS:
        log.info("已剥离 MinerU 输出里的内嵌二进制：%d 字 → %d 字（-%.1f%%）",
                 original, len(cleaned), removed / original * 100)
    elif removed > 0:
        log.debug("净化 MinerU 输出（去掉 %d 字空白/零碎符号，未达 INFO 阈值）", removed)
    return cleaned


def _has_prose(text: str) -> bool:
    """文本去掉页标记、分隔线与空白后**是否还剩正文**（用于「剥完只剩页标记」时回落块数据态）。"""
    if not text:
        return False
    for marker in _PAGE_BREAK_MARKERS:
        text = text.replace(marker, "")
    return bool(re.sub(r"[\s\-]+", "", text))


# ---------------------------------------------------------------------------
# 后端选择与 VLM 就绪探测（阶段 4 · 条目 4.2）
# ---------------------------------------------------------------------------

# 探测缓存：``{名字: (配置指纹, 结果, 记录时刻)}``。为什么要缓存——`_select_backend()` 会跑外部命令
# （nvidia-smi）与文件系统检查，而 `available()` 处在**热路径**上（每个扫描件 PDF 都会调一次，
# 见 `parsing.parse_document_pages` 的 PDF 分支），不缓存就等于每个扫描件跑一次子进程。
# 为什么带指纹而不是裸布尔——配置一变（用例 monkeypatch / 重载 .env）必须重探，
# 否则「先跑 auto 的用例把结果钉死，后面的用例全读到旧值」。
_BACKEND_CACHE: dict[str, tuple[tuple, object, float]] = {}

# 参与缓存指纹的配置项：任何一个变了，后端选择与就绪判定都可能跟着变。
_CACHE_SETTINGS = ("MINERU_BACKEND", "MINERU_VLM_ENABLE", "MINERU_DEVICE", "MINERU_MODEL_DIR")
# 还参与指纹的**进程环境变量**：它们决定 MinerU 的权重根与设备（`_apply_mineru_env` 写的就是它们），
# 但不在 settings 里 ⇒ 只看配置会漏（独立复核 A 的 R3）。
_CACHE_ENV = ("MINERU_HOME", "MINERU_DEVICE_MODE")

# 「否定结论」的保质期（秒）：VLM 未就绪大多**不是**配置问题，而是环境问题（权重刚拷进来、
# `pip install mineru-llama-cpp` 刚装完、GPU 刚恢复）。若把「未就绪」永久缓存住，用户按 §0.3 的
# 「自备几件」在进程运行期间补齐后，VLM 会**永远**不生效且毫无提示；加个 TTL 后至多重探一次
# （代价是一次 nvidia-smi 子进程 + 两次文件/模块检查）。
_NOT_READY_TTL_SECONDS = 60.0


def _reset_backend_cache() -> None:
    """清空后端选择 / 就绪探测缓存（**测试入口**：模块级缓存必须能被用例重置）。

    阶段 3 已经因为模块级状态污染吃过一次亏（`test_image_ocr.py` 的 autouse 清理夹具）；
    本阶段的两个探测缓存与 `sys.modules` 无关，故必须另开这个显式出口。
    """
    _BACKEND_CACHE.clear()


def _cache_fingerprint() -> tuple:
    settings_part = tuple((name, getattr(settings, name, None)) for name in _CACHE_SETTINGS)
    env_part = tuple((name, os.environ.get(name)) for name in _CACHE_ENV)
    return settings_part + env_part


def _is_negative(value: object) -> bool:
    """默认的「否定结论」识别：``(False, 原因)`` 形态的探测结果。"""
    return isinstance(value, tuple) and bool(value) and value[0] is False


def _cached(name: str, compute, *, negative_ttl: float | None = None, is_negative=None):
    """按配置指纹缓存 ``compute()`` 的结果；指纹变了就重算。

    ``negative_ttl``（秒）+ ``is_negative``：给**否定/降级结论**加保质期（见 :data:`_NOT_READY_TTL_SECONDS`）。
    只对否定结论设 TTL，是「缓存热路径的子进程」与「环境补齐后能自愈」之间的折中。
    """
    fingerprint = _cache_fingerprint()
    hit = _BACKEND_CACHE.get(name)
    if hit is not None and hit[0] == fingerprint:
        _fingerprint, value, recorded_at = hit
        negative = (is_negative or _is_negative)(value)
        expired = negative_ttl is not None and negative and time.monotonic() - recorded_at > negative_ttl
        if not expired:
            return value
    value = compute()
    _BACKEND_CACHE[name] = (fingerprint, value, time.monotonic())
    return value


def _gpu_available() -> tuple[bool, str]:
    """探测本机是否有可用的 NVIDIA GPU（返回 (是否可用, 可读描述)）。

    **判据不是 ``torch.cuda.is_available()``**（阶段 4 §二 条目 4.2「已核事实 1」）：打包环境
    **没有 torch**（也不需要——VLM 走进程内 llama.cpp，设备由 ``mineru_llama_cpp`` 自己按
    ``n_gpu_layers=99`` 决定，不经过 MinerU 的 ``get_device()``）。照抄 torch 判据会把
    「有 RTX 4060 的机器」判成无 GPU，VLM 永远选不上。故改问 ``nvidia-smi``：它是 NVIDIA 驱动
    自带的只读命令，不依赖 Python 侧任何重依赖。

    只在 ``MINERU_ENABLE=True`` 且走到 VLM 判定时才被调用（见 :func:`_resolve_backend`），
    绝不在 import 期或 ``MINERU_ENABLE=False`` 时执行外部命令。
    """
    exe = shutil.which("nvidia-smi")
    if not exe:
        return False, "未找到 nvidia-smi"
    try:
        proc = subprocess.run(
            [exe, "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 —— 探测失败只意味着「判未就绪」，不该炸穿解析链
        return False, f"nvidia-smi 执行失败（{exc}）"
    lines = (proc.stdout or "").strip().splitlines()
    if proc.returncode != 0 or not lines:
        return False, "nvidia-smi 未报告可用 GPU"
    return True, lines[0].strip()


def _vlm_weight_files() -> list[Path]:
    """VLM（llama-cpp 引擎）需要的权重文件清单——**复用 MinerU 自己的仓库登记**。

    上游真源：``mineru/model/registry.py`` 的 ``vlm_model_repo("llama-cpp")`` 返回 ``ModelRepo``，
    其 ``paths`` 为 ``{"main": "...-Q8_0.gguf", "mmproj": "mmproj-...-Q8_0.gguf"}``；
    绝对路径 = ``repo.local_dir() / paths[name]``（``mineru/model/download.py`` 的 ``ModelRepo.local_dir``）。

    **不自己拼文件名**：上游换名 / 换 repo 时，自拼会在「探测说就绪」与「加载时找不到」之间静默失配。
    权重根目录由 ``MINERU_HOME`` 决定（``$MINERU_HOME/models``），故调用方必须先
    :func:`_apply_mineru_env`（``available()`` 的既有顺序即如此）。
    """
    from mineru.model.registry import vlm_model_repo  # 惰性 import：只在 VLM 判定路径上发生

    repo = vlm_model_repo("llama-cpp")
    root = Path(repo.local_dir())
    return [root / str(name) for name in repo.paths.values()]


def _vlm_weights_ready() -> tuple[bool, str]:
    """权重就绪判定：**只做文件存在性检查，绝不触发下载 / 加载**。

    为什么不能借用上游的 ``ModelRepo.ensure()`` / ``verify_model_repo()``：
    - ``ensure()`` 在本地缺失时会**联网下载**（还可能回写 MinerU 自己那份 config）——探测绝不能干这个；
    - ``verify_model_repo()`` 对 GGUF 仓库只认 ``<local_dir>/.mineru_complete`` 标记文件
      （``download_mode="full"`` 的既定口径），**标记存在与「两个 .gguf 都在」不是一回事**：
      用户手工拷贝权重、或换一种下载方式时，标记缺失而文件齐备 ⇒ 只看标记会**误报未就绪**，
      把本来能跑的环境判成不可用（反过来，标记在而文件被删同样漏判）。
      ⚠️ 事实核定（2026-09-18 实测，``Get-ChildItem -Force``）：本机权重目录 ``.mineru_complete``
      **确实存在**（0 字节，2026-09-17 20:01 上游下载时写入）；本轮有一份调研报告误称它不存在，
      已用原始输出反证。**故第 2 条不该被读成「标记缺失」，本函数选文件级检查的理由是上面那条语义差异。**

    故以**文件级**检查为准：``main`` 与 ``mmproj`` **两个都在**才算就绪（只查主模型会漏掉 mmproj 缺失），
    并顺带给出「缺哪个文件」的可读诊断。路径解析仍复用上游登记（见 :func:`_vlm_weight_files`）。
    """
    try:
        files = _vlm_weight_files()
    except Exception as exc:  # noqa: BLE001 —— 拿不到上游登记（未装 / API 变化）一律算未就绪
        return False, f"无法解析权重路径（{exc}）"
    missing = [path.name for path in files if not path.is_file()]
    if missing:
        root = files[0].parent if files else Path("?")
        # 诊断提示：`MINERU_MODEL_DIR` 是空串（默认）时权重根落在 MinerU 自己的 `~/.mineru/models`，
        # 用户把权重放别处却忘了配这个键——这是「用户自备」路线上最容易踩的坑（2026-09-18 实测发现），
        # 故在失败串里点一句，免得只看到「权重缺失 <某路径>」却不知道去哪儿改。
        hint = ""
        if not (settings.MINERU_MODEL_DIR or "").strip():
            hint = "；提示：MINERU_MODEL_DIR 未配置（权重根默认 ~/.mineru/models），若权重放在别处请设该键"
        return False, f"权重缺失 {' / '.join(missing)}（目录 {root}）{hint}"
    return True, f"权重就绪（{files[0].parent}）"


def _vlm_engine_ready() -> tuple[bool, str]:
    """引擎模块就绪判定：``mineru_llama_cpp`` 是否可 import。

    用 ``importlib.util.find_spec`` 而**不是** ``import mineru_llama_cpp``：后者在 Windows 上会
    ``os.add_dll_directory()`` 并加载 ``.pyd``（重动作，见上游 ``mineru_llama_cpp/__init__.py``），
    而探测必须保持轻量——这也正是「``MINERU_VLM_ENABLE=False`` 时不 import ``mineru_llama_cpp``」
    这条验收可被 ``sys.modules`` 断言的前提。
    """
    if importlib.util.find_spec("mineru_llama_cpp") is None:
        return False, "未安装 mineru-llama-cpp"
    return True, "mineru-llama-cpp 已安装"


def _vlm_ready() -> tuple[bool, str]:
    """「VLM 就绪三件套」：GPU 可探测 + 权重齐（main 与 mmproj 都在）+ 引擎模块在。缺任一即未就绪。

    三项**都测**（不短路）是为了给出完整诊断——日志里能一眼看出到底哪件没备齐。
    每一项还各自兜住异常（独立复核 B 指出的 I3 缝隙）：探测本身出问题只该意味着「判未就绪」，
    绝不能把裸异常抛出去——`available()` 被 `parsing` 在 PDF 与图片两条链上**裸调**
    （`parsing.py` 的 `mineru.available()[0]`），抛异常会直接炸穿 ingest 主链（不变量 3）。
    """

    def compute() -> tuple[bool, str]:
        checks: list[tuple[str, tuple[bool, str]]] = []
        for label, probe in (("GPU", _gpu_available), ("权重", _vlm_weights_ready), ("引擎", _vlm_engine_ready)):
            try:
                checks.append((label, probe()))
            except Exception as exc:  # noqa: BLE001 —— 见 docstring：探测异常一律降级为「未就绪」
                log.warning("MinerU VLM 就绪探测异常（%s）：%s", label, exc)
                checks.append((label, (False, f"探测异常（{exc}）")))
        failed = [f"{label}：{reason}" for label, (ok, reason) in checks if not ok]
        if failed:
            return False, "；".join(failed)
        return True, "；".join(reason for _label, (_ok, reason) in checks)

    return _cached("vlm_ready", compute, negative_ttl=_NOT_READY_TTL_SECONDS)


def _resolve_backend() -> str:
    """``MINERU_BACKEND`` + ``MINERU_VLM_ENABLE`` → 实际路由（``"vlm"`` / ``"pipeline"``）。

    - 非 auto/vlm 的取值（``pipeline`` / 空串 / 未知值）→ ``"pipeline"``：不探测、不 import；
    - ``auto`` / ``vlm``：只有 ``MINERU_VLM_ENABLE=True`` **且**就绪三件套成立才 ``"vlm"``，
      否则回落 ``"pipeline"`` 并 log —— **未自备 VLM 件时 auto ≡ pipeline，不报错**（这是默认路径）；
    - 显式 ``MINERU_BACKEND=vlm`` 但开关没开 → 明确 warning（VLM 件是自备项，开关不开就不该用）；
    - 显式填 MinerU 的 VLM 档位名（``standard`` / ``advanced``）→ **本开关管不到**（档位原样透传给 MinerU，
      见 :func:`_mineru_tier`），故这里给一条 warning 把「该档位会构造 VLM 预测器、缺件会硬失败」讲明白
      （独立复核 A 的 R1：这是唯一一条能绕开本阶段安全保护的配置路径，至少不能是哑的）。
    """
    backend = (settings.MINERU_BACKEND or "").strip().lower()
    if backend in ("standard", "advanced") and not settings.MINERU_VLM_ENABLE:
        log.warning(
            "MINERU_BACKEND=%s 是 MinerU 的 VLM 档位（effort=high/xhigh，解析时会构造 VLM 预测器）："
            "MINERU_VLM_ENABLE 开关管不到它，未自备 GGUF 权重与 mineru-llama-cpp 时会硬失败"
            "（扫描件回落 pypdf、可能只报「未提取到文字」）。开箱即用请用 auto 或 pipeline（basic 档，ONNX-only）。",
            backend,
        )
    if backend not in ("auto", "vlm"):
        return "pipeline"
    if not settings.MINERU_VLM_ENABLE:
        if backend == "vlm":
            log.warning("MINERU_BACKEND=vlm 需要 MINERU_VLM_ENABLE=True（VLM 件为自备项），本次按 pipeline 解析")
        return "pipeline"
    ready, reason = _vlm_ready()
    if ready:
        log.info("MinerU 后端选择：vlm（%s）", reason)
        return "vlm"
    # 规格 3 要求「不可用 → log warning」：这条是**用户唯一的自查线索**（默认档下它决定为什么不走 VLM），
    # 故用 warning 而不是 info（独立复核 A 的 R3）。
    log.warning(
        "MinerU 后端选择：pipeline（VLM 未就绪：%s）。要启用 VLM 请备齐三件：权重放进 $MINERU_HOME/models "
        "（并让 MINERU_MODEL_DIR 指向该根）+ pip install mineru-llama-cpp + MINERU_VLM_ENABLE=True",
        reason,
    )
    return "pipeline"


def _select_backend() -> str:
    """实际要用哪个后端：``"vlm"`` 或 ``"pipeline"``（按配置指纹缓存，:func:`_reset_backend_cache` 可清）。

    只有 ``"vlm"`` 路由才会给 MinerU 传 ``VlmConfig(engine="llama-cpp")``（见 :func:`_vlm_config`）；
    其余情形调用形态与阶段 2/3 逐字一致。

    **否定结论带保质期**（见 :data:`_NOT_READY_TTL_SECONDS`）：只有「会真的去探测就绪」的配置
    （``auto``/``vlm`` + 开关开）才给 ``pipeline`` 结论设 TTL——这样用户在进程运行期间补齐 VLM 件后，
    至多一分钟就能自动用上，而不是永久停在 pipeline（独立复核 A 的 R3）；其余配置是稳定的配置态，
    照常永久缓存，不会因此反复跑子进程。
    """
    probes_readiness = (settings.MINERU_BACKEND or "").strip().lower() in ("auto", "vlm") and settings.MINERU_VLM_ENABLE
    return _cached(
        "backend",
        _resolve_backend,
        negative_ttl=_NOT_READY_TTL_SECONDS if probes_readiness else None,
        is_negative=lambda value: value != "vlm",
    )


def _device_hint(backend: str) -> str:
    """``available()`` 摘要里那个 ``device=`` 的取值（只为诊断，不参与任何决策）。

    VLM 路由由「就绪三件套」保证有 GPU，故直接报 ``cuda``；pipeline 路由不猜硬件，
    报用户显式配置的 ``MINERU_DEVICE``，没配就报 ``auto``（= 交给 MinerU 自己探测）。
    """
    if backend == "vlm":
        return "cuda"
    device = (settings.MINERU_DEVICE or "").strip().lower()
    return device or "auto"


def _vlm_config():
    """显式构造 MinerU 的 ``VlmConfig``（llama-cpp 引擎）；拿不到就返回 ``None``。

    为什么显式传而不改全局：``VlmConfig`` 在 MinerU 内部是**实例级深拷贝**
    （``mineru/model/vlm/client.py``：``settings = (vlm_config if vlm_config is not None else config.model.vlm)
    .model_copy(deep=True)``），传参只影响本次调用；改 ``config.model.vlm`` 会污染同进程其他调用点
    （阶段 4 §三 明令禁止）。
    为什么钉死 ``llama-cpp``：本机路线是**进程内 llama.cpp**（``mineru_llama_cpp`` 已装），不引入
    sglang / vllm 常驻服务；``http-client`` 等远程引擎**另开条目**。显式写引擎名也让失败文案与
    回归比对有确定形状（``auto`` 在本机解析结果虽同为 llama-cpp，但它会随平台/设备漂移）。
    """
    try:
        from mineru.config import VlmConfig

        # 构造也在 try 内：上游若改了字段/校验（例如 engine 取值集合变化），这里会抛
        # pydantic 校验错——它同样属于「拿不到 VlmConfig」，该退化为 MinerU 默认引擎而不是炸穿。
        return VlmConfig(engine="llama-cpp")
    except Exception as exc:  # noqa: BLE001 —— 未装 MinerU / 上游 API 变化都退化为「用 MinerU 默认引擎」
        log.warning("无法构造 MinerU VlmConfig（%s），改用 MinerU 默认 VLM 引擎配置", exc)
        return None


def available() -> tuple[bool, str]:
    """MinerU 是否可用。

    返回 (可用, 原因)。``MINERU_ENABLE`` 关 → 直接返回不可用且不碰重依赖；
    开启但 ``mineru`` / ``magic_pdf`` 都 import 不到 → 提示未安装（**该分支不跑任何子进程探测**，
    否则「本机没装 MinerU」的用例会变成机器环境依赖）。

    探测口径（2026-09-17 用户拍板「放宽」）：**任一可 import 即可用**——3.x 的主路径是
    ``mineru`` 包，2.x 的管道入口在 ``magic_pdf`` 包；只装后者时若仍要求 ``import mineru``
    会把「其实能跑」的环境误判为未安装。

    第二个元素（阶段 4 · 条目 4.2 规格 2）从固定的 ``"MinerU 可用"`` **充实**为带后端/设备的摘要，
    供日志与诊断；可用性判定本身仍只看能否 import。
    """
    if not settings.MINERU_ENABLE:
        return False, "MINERU_ENABLE 未开启"
    _apply_mineru_env()  # 必须在 import mineru 之前：模型目录等默认路径是 import 期算出来的
    for module_name in ("mineru", "magic_pdf"):
        try:
            __import__(module_name)  # 唯一会触发重依赖 import 的地方
        except Exception:
            continue
        try:
            backend = _select_backend()
        except Exception as exc:  # noqa: BLE001
            # `available()` 被 `parsing` 裸调（PDF 分支与图片分支），抛异常会炸穿 ingest 主链（不变量 3）；
            # 后端选择只是「诊断 + 路由」，探不出来就按最保守的 pipeline 记（独立复核 B 指出的缝隙）。
            log.warning("MinerU 后端探测异常，按 pipeline 记：%s", exc)
            backend = "pipeline"
        return True, f"MinerU 可用，backend={backend}, device={_device_hint(backend)}"
    return False, "未安装 MinerU（pip install mineru 后重试）"


def _pages_from_markdown(markdown_text: str) -> list[str]:
    """把 MinerU 产出的 Markdown 按页标记切成「按页文本」。

    **出口不变量**：切出的每一页都是 `markdown_text` 的字面子串（`str.split` 的天然性质），
    这正是下游 ``chunking._assign_page`` 标页码所依赖的前提。因此本函数是**首选**取页方式，
    块数据态（:func:`_blocks_pages`）只在没有标记时兜底。

    标记候选见 ``_PAGE_BREAK_MARKERS``：4.0 的默认渲染不带标记，``add_markers=True``
    （``RenderMode.FULL``）才输出 ``"\\n\\n---\\n\\n"``；``<!-- PageBreak -->`` 是 2.x 形态。
    没有标记时整篇当一页返回，调用方（清洗页眉页脚、区块定位）对「单页」会自然退化为空操作，不误删。
    """
    if not markdown_text:
        return []
    parts = [markdown_text]
    for marker in _PAGE_BREAK_MARKERS:
        if marker in markdown_text:
            parts = markdown_text.split(marker)
            break
    return [part.strip() for part in parts if part.strip()]


def _text_of(value: object) -> str:
    """把 MinerU 的「块内容」转成纯文本。

    3.x 的块内容是 ``InlineSpan`` 列表（TextSpan / EquationInlineSpan / HyperlinkSpan …），
    不同版本还可能是 str；统一按「有 text 取 text，否则 str()」收口。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "".join(_text_of(item) for item in value)
    text = getattr(value, "text", None)
    if isinstance(text, str):
        return text
    return str(value)


def _blocks_pages(result: object) -> list[str]:
    """从 ``ParseResult.pages`` 取按页纯文本（**最后兜底**；页序按数组顺序，数组顺序即权威）。

    仅在**完全没有渲染文本**时才用（见 :func:`_export`）。注意它取的是**块数据态**的裸文本，
    与 Markdown 渲染态（标题前缀 / 表格语法 / 公式定界符）**不是同一套表示**，因此
    **不满足「pages 是 text 的字面切片」**这一下游前提。
    取不到（旧版 / 结构变化）时返回 ``[]``。
    """
    pages = getattr(result, "pages", None)
    if not pages:
        return []
    out: list[str] = []
    for page in pages:
        chunks: list[str] = []
        for block in getattr(page, "blocks", None) or []:
            text = _text_of(getattr(block, "content", None)).strip()
            if text:
                chunks.append(text)
        out.append("\n".join(chunks).strip())
    return [page for page in out if page]


def _markdown_of(result: object, *, add_markers: bool = False) -> str:
    """从 MinerU 返回值里取出 Markdown 全文。

    ``add_markers=True`` 请求**带页边界标记**的渲染（MinerU 4.0 的 ``RenderMode.FULL``），
    使 :func:`_pages_from_markdown` 能切出「字面上是全文切片」的按页文本。旧版可能直接返回 str，
    此时忽略该参数。

    优先走带标记渲染，取不到再退回默认渲染——**保证不会因为参数名不匹配而整条路径失败**。
    """
    if isinstance(result, str):
        return result
    markdown = getattr(result, "markdown", None)
    if callable(markdown):
        if add_markers:
            try:
                marked = markdown(add_markers=True)
            except TypeError:  # 旧版 markdown() 不认这个参数
                marked = None
            if marked:
                return marked
        return markdown() or ""
    if isinstance(markdown, str):
        return markdown
    return ""


def _page_count(result: object) -> int:
    """``ParseResult.pages`` 的**结构页数**（含空页），0 表示这个返回值拿不到页数信息。

    与 :func:`_blocks_pages` 的分工：这里只要「有几页」这个事实，不取任何文本——因为
    ``page_idx`` / ``blocks`` 都可能缺失，但「页数组有多长」在任何版本里都成立。
    """
    pages = getattr(result, "pages", None)
    try:
        return len(pages) if pages is not None else 0
    except TypeError:  # pragma: no cover —— 非序列的怪异返回值，按拿不到处理
        return 0


def _markdown_pages(markdown_text: str, known_count: int) -> list[str]:
    """从 Markdown 按标记切页，并做**结构页数一致性自检**；不可信时返回 ``[]``。

    ``"\\n\\n---\\n\\n"`` 既可能是页边界，也可能是文档正文里的一条分隔线：单页文档正文含
    ``---`` 时会切出「多页」。``known_count``（``ParseResult.pages`` 的结构页数）能戳穿这个误判——
    **切出的片数与结构页数不一致即判定不可信**。**宁可少给页边界，也不给出会让下游页码静默错位的边界。**

    ⚠️ **两个关键修订（2026-09-18 对抗性复核抓出的缺陷 `回归修复`）**：

    1. **保留空页占位**：原实现走 ``_pages_from_markdown`` 并用 ``if part.strip()`` 丢掉空片，
       于是「某页只剩一张内嵌图、被净化剥空」时该页消失，**后续页整体前移**——
       ``chunking._assign_page`` 返回的是「第几个页文本命中探针」的**下标**，所以第 2 页的正文
       被静默标成第 1 页（不是 ``None``、也没有任何日志）。现在空页照样占一格，页序号不漂移。
    2. **上下都要查**：原来只查「切出的片数 > 结构页数」（误判成多页），
       查不到「片数 < 结构页数」（有页丢了）。两个方向都是不可信，一律退化为「整篇一页」。
    """
    if not markdown_text:
        return []
    marker = _first_page_marker(markdown_text)
    if marker is None:
        stripped = markdown_text.strip()
        return [stripped] if stripped else []
    parts = [part.strip() for part in markdown_text.split(marker)]
    if known_count and len(parts) != known_count:
        log.warning("MinerU 分页标记切出 %d 片 ≠ 结构页数 %d，放弃页边界（宁可少给，不给错边界）",
                    len(parts), known_count)
        return []
    if not all(part in markdown_text for part in parts):
        log.warning("MinerU 按页文本不是全文字面子串")
        return []
    return parts


def _export(result: object) -> tuple[str, list[str]]:
    """统一出口：``ParseResult`` / 旧版返回值 → (全文, 按页文本)。

    **出口不变量：`pages` 的每一项都必须是 `text` 的字面子串。**
    下游切块（``chunking._assign_page``）靠「页文本在全文里定位」来标页码；若页文本不是全文的
    切片，定位会失败，chunk 的页码被静默置空。取页路径：

    ① 带标记的 Markdown（标记天然是全文的一部分）→ ② 整篇一页（全文即唯一一页，**必然满足**
    不变量）→ ③ 块数据态兜底（**不满足**不变量，仅在没有渲染文本时才用）。

    ② 之所以存在且排在 ③ 之前：标记被判为误判（如单页正文里自带 ``---``）时，
    「整篇一页」是比「换成非切片的块文本」更安全、也更贴近事实的降级。

    ⚠️ **注释更正（2026-09-18 对抗性复核 F9）**：取文顺序里**从来没有**「先切页再剥离」这一步
    ——剥离是 2026-09-18 才引入的，且**只在切页之前**发生。此前这里的措辞会让后来者以为
    「先剥后切」是为了修一个曾经存在的顺序错误。真实理由见下方 ① 的注释。
    """
    known_count = _page_count(result)

    # ① 先剥离内嵌二进制，**再**切页：`_markdown_pages` 的自检要求「每页都是全文的字面子串」，
    #    先切后剥会让切出来的页不再存在于净化后的全文里，直接把页码定位打回 `None`。
    markdown_text = _strip_embedded_binary(_markdown_of(result, add_markers=True))
    pages = _markdown_pages(markdown_text, known_count)
    # ② 带标记这一路「剥完只剩页标记/分隔线」时也要往下走（不能把 `---` 当正文入库；
    #    对抗性复核 F5：3 字的垃圾曾成功入库，而块数据态里有干净的正文）。
    if not _has_prose(markdown_text):
        markdown_text = _strip_embedded_binary(_markdown_of(result))
        pages = _markdown_pages(markdown_text, known_count)
    if _has_prose(markdown_text):
        return markdown_text, pages or [markdown_text]
    blocks = _blocks_pages(result)
    return "\n\n".join(blocks), blocks


def _mineru_tier(backend: str | None = None) -> str:
    """把本仓库的 ``MINERU_BACKEND`` 映射成 MinerU 的 ``tier``（阶段 4 · 条目 4.1 已核事实 5）。

    两套命名**不是一对一**，映射规则分两类：

    1. **用户直接填 MinerU 的 tier 名**（``flash`` / ``basic`` / ``standard`` / ``advanced``）→
       **原样透传**，尊重显式选择（也避免把「用户明确要的档位」悄悄改掉）；
    2. **本仓库的三档语义**（``auto`` / ``pipeline`` / ``vlm``，或空串/未知值）→ 按
       :func:`_select_backend` 的**实际路由**分档：
       - ``vlm`` 路由 → ``standard``（``HYBRID_EFFORT_BY_TIER``：standard = ``high``）；
       - ``pipeline`` 路由 → ``basic``（``medium`` 档，ONNX-only）。

    **为什么必须分档（阶段 4 的核心取舍，已登记 `P-登记-回归检查`）**：MinerU 4.0 里
    ``effort ∈ {high, xhigh}`` 才会构造 VLM 预测器（``mineru/backend/analysis/pdf/pipeline.py``），
    而 ``standard`` 正映射到 ``high`` ⇒ **``standard`` 需要 VLM/GGUF**。
    若继续沿用阶段 2 的「一律 standard」，则阶段 4 §0.3 定案的交付形态（只带 818MB ONNX 件、
    VLM 由用户自备）会**整条扫描件 OCR 路径失效**；而 ``basic`` 正是 T1′ 验证过
    「ONNX-only 可跑通、GGUF 访问 0 次」的档位。分档后：没自备 VLM 件 → 走 basic（不碰 GGUF）；
    自备并开开关 → 走 standard（真上 VLM）。

    ``backend`` 形参只为可测性而留（纯函数式调用，如 ``_mineru_tier("vlm")``）；
    不传则走 :func:`_select_backend`（**带缓存**，不会因为本函数被多次调用而重复探测）。
    """
    configured = (settings.MINERU_BACKEND or "").strip().lower()
    if configured in ("flash", "basic", "standard", "advanced"):
        return configured
    resolved = _select_backend() if backend is None else backend
    return "standard" if resolved == "vlm" else "basic"


# ---------------------------------------------------------------------------
# D2 · 逐级降级：只有「VLM 档」失败/空结果才降一档重试（`basic` / `flash` / 未知档位不重试）
# ---------------------------------------------------------------------------

# 会构造 VLM 预测器（effort=high/xhigh）的两档 —— 只有它们失败后才值得降一档。
# `standard` 来自两条路：显式填 `MINERU_BACKEND=standard`（路由是 pipeline，见 `_resolve_backend`）
# 与 `auto`/`vlm` 选中 VLM 路由（见 `_mineru_tier`）；**两条都要降级**，故判断只能落在档位上，
# 不能落在「路由是不是 vlm」上。
_DOWNGRADE_FROM_TIERS = ("standard", "advanced")
# 降级目标：ONNX-only 的 medium 档（T1′ 实测「可跑通、GGUF 访问 0 次」）。
_DOWNGRADE_TO_TIER = "basic"


def _apply_mineru_env() -> None:
    """把本仓库的 MinerU 设备 / 模型目录配置映射成 MinerU 认的环境变量（在 import 前设好）。

    参考源码核对（MinerU 4.0，只读快照）：
    - 设备：``mineru/model/runtime/device.py:16`` ``os.getenv("MINERU_DEVICE_MODE")`` 优先，
      否则自动探测 cuda→mps→npu→…→cpu。故我们的 ``auto`` = 不设，让 MinerU 自己探测；
      ``cpu`` / ``cuda`` 才显式覆盖。
    - 模型目录：``mineru/config.py:36-44,502`` ``MINERU_HOME`` 决定 home（默认 ``~/.mineru``），
      权重落在 ``$MINERU_HOME/models``。本仓库的 ``MINERU_MODEL_DIR`` 是「自定义权重目录」，
      直接映射成 ``MINERU_HOME``——它必须在 **import mineru.config 之前**设好，
      因为默认路径是在 import 期算出来的常量，之后再设无效。
    - 只写「用户显式配置过」的值：不覆盖用户 shell 里已有的环境变量，也不给 auto/空值设默认，
      保持「不改开箱行为」。
    """
    import os

    device = (settings.MINERU_DEVICE or "").strip().lower()
    if device and device != "auto" and not os.environ.get("MINERU_DEVICE_MODE"):
        os.environ["MINERU_DEVICE_MODE"] = device
    model_dir = (settings.MINERU_MODEL_DIR or "").strip()
    if model_dir and not os.environ.get("MINERU_HOME"):
        os.environ["MINERU_HOME"] = model_dir


def _import_or_unavailable(module: str, **names: str):
    """导入 ``module``；**只有导入期失败**才归一成 :class:`_EntryUnavailable`。

    ``names`` 是「模块属性名 → 中文用途」的映射，用于把「模块在但属性缺失」也判为入口不可用
    （这仍是「本版本没这个入口」，而不是运行期异常）。
    """
    try:
        imported = importlib.import_module(module)
    except ImportError as exc:
        raise _EntryUnavailable(f"导入 {module} 失败（{exc}）") from exc
    resolved = {}
    for attr, purpose in names.items():
        value = getattr(imported, attr, None)
        if value is None:
            raise _EntryUnavailable(f"{module} 没有 {attr}（{purpose}）")
        resolved[attr] = value
    return resolved


def _parse_via_parser_facade(
    data: bytes, filename: str = "input.pdf", vlm_config=None, tier_override: str | None = None
) -> tuple[str, list[str]]:
    """入口 ①（主路径）：MinerU 3.x / 4.0 官方高层门面 ``mineru.parser.parse(path)``。

    该入口只接受**本地文件路径**（内部要做图片转 PDF、页范围重写、坏页补齐），故先落一个
    临时文件；``ParseResult`` 的渲染与取页都在临时目录存活期间完成，避免惰性读取素材时文件已消失。
    tier 默认由 :func:`_mineru_tier` 从 ``MINERU_BACKEND``（经 :func:`_select_backend` 的实际路由）映射。

    ``filename`` 决定临时文件的后缀，也就是 MinerU 判定输入类型的依据（**图片 OCR 靠它走图片分支**）：
    4.0 的 ``MinerUParser._prepare_input`` 按后缀判 ``IMAGE_EXTENSIONS``，命中则
    ``PDFDocument.from_image`` 包成单页 PDF 再解析。故调用方（:func:`parse_image`）必须给
    真实后缀（``input.png`` / ``input.jpg`` …），给 ``.pdf`` 会把图片当 PDF 解析而失败。

    ``vlm_config``（阶段 4 · 条目 4.1）：**只有 VLM 路由才非 None**，此时多带一个
    ``vlm_config=`` 关键字实参；``pipeline`` 路由的调用形态与阶段 2/3 **逐字一致**（不传该参数），
    这样旧版 MinerU（``parse`` 不认 ``vlm_config``）不会因为多一个关键字而整条路径失败。

    ``tier_override``（D2 · 条目「VLM 失败逐级降级」）：**非 None 时就是本次要用的档位**——
    直接交给 ``mineru.parser.parse(tier=...)``，**绕过** :func:`_mineru_tier` 的透传/分档分支
    （降级重试必须真的用 ``basic``，不能又被 ``MINERU_BACKEND`` 透传回 ``standard``/``advanced``）；
    ``None`` 时行为与阶段 2/3/4 逐字一致（自己按配置分档）。

    注意：``mineru.parser.parse`` 内部就是 ``MinerUParser(...).parse(...)``，故入口 ② 只是
    它的细粒度退路；两者**不能**在同一份数据上依次重试（否则一次内部异常会让同一 PDF 解析两遍）。
    """
    (mineru_parse,) = _import_or_unavailable("mineru.parser", parse="高层解析入口").values()
    if not callable(mineru_parse):
        raise _EntryUnavailable("mineru.parser.parse 不可调用")
    kwargs: dict[str, object] = {"tier": _mineru_tier() if tier_override is None else tier_override}
    if vlm_config is not None:
        kwargs["vlm_config"] = vlm_config
    with tempfile.TemporaryDirectory(prefix="zhilan-mineru-") as tmp_dir:
        input_path = Path(tmp_dir) / filename
        input_path.write_bytes(data)
        result = mineru_parse(input_path, **kwargs)
        return _export(result)


def _parse_via_parser_instance(
    data: bytes, filename: str = "input.pdf", vlm_config=None, tier_override: str | None = None
) -> tuple[str, list[str]]:
    """入口 ②：``MinerUParser`` 实例门面（3.x 形态，供 ``mineru.parser.parse`` 缺失时退路）。

    ``filename`` 语义同 :func:`_parse_via_parser_facade`（后缀即输入类型）；
    ``vlm_config`` 同理只在 VLM 路由时非 None（仅非 None 才作为关键字实参传入）。

    ⚠️ **本构造器只认关键字实参**（MinerU 4.0：``__init__(self, *, tier="standard", …)``），
    且 :meth:`MinerUParser.parse` **不接受** tier（``parse(self, path, *, page_range, source_context)``）
    ——档位只能在构造时给。

    ``tier_override``（D2 · 条目「VLM 失败逐级降级」）：**非 None 时作为 ``tier=`` 关键字传给构造器**。
    为什么必须补这一处（三·5「不补则降级是假的」）：本入口原先**根本不传 tier**，吃的是 MinerU
    硬编码默认 ``standard``（= VLM 档，会构造 VLM 预测器）——于是降级重试即使在外面把档位定成
    ``basic``，只要走到备用入口就还是拿 VLM 档跑。``None`` 时**保持原行为**（不传，交给 MinerU 默认），
    图片链因此逐字不变（:func:`_image_entrypoints` 不传该形参）。
    """
    (parser_cls,) = _import_or_unavailable("mineru.parser", MinerUParser="解析器类").values()
    parser_kwargs: dict[str, object] = {}
    if tier_override is not None:
        parser_kwargs["tier"] = tier_override
    if vlm_config is not None:
        parser_kwargs["vlm_config"] = vlm_config
    with tempfile.TemporaryDirectory(prefix="zhilan-mineru-") as tmp_dir:
        input_path = Path(tmp_dir) / filename
        input_path.write_bytes(data)
        parser = parser_cls(**parser_kwargs)
        try:
            result = parser.parse(input_path)
            return _export(result)
        finally:
            close = getattr(parser, "close", None)
            if callable(close):
                close()


def _parse_via_high_level_api(data: bytes) -> tuple[str, list[str]]:
    """入口 ③：旧版 / 变体高层入口 ``mineru.parse(data)``。"""
    (parse,) = _import_or_unavailable("mineru", parse="旧版高层解析入口").values()
    if not callable(parse):
        raise _EntryUnavailable("mineru.parse 不可调用（该版本可能只提供 CLI 或 parser 门面）")
    return _export(parse(data))


def _parse_via_magic_pdf(data: bytes) -> tuple[str, list[str]]:
    """入口 ④：MinerU 2.x 底层管道 ``magic_pdf`` UNIPipe。

    对应真相源 §3.4 给出的调用式；2.x 起 ``magic_pdf`` 已并入 ``mineru`` 包（包名改叫 mineru），
    故在 3.x/4.0 环境这一入口必然 import 失败，由 ``_parse_with_mineru`` 汇总报「API 不匹配」。
    这是四入口里唯一**运行期异常原文冒泡**的写法：``pipe_*`` 调用不在任何吞异常的包装里。
    """
    resolved = _import_or_unavailable(
        "magic_pdf.data.data_reader",
        BytesReader="字节读取器",
    )
    (pipe_cls,) = _import_or_unavailable("magic_pdf.pipe.UNIPipe", UNIPipe="旧版统一管道").values()
    reader = resolved["BytesReader"](data)
    jso_useful_key = {"_pdf_type": "", "model_list": []}
    pipe = pipe_cls(reader, jso_useful_key, None)  # image_writer 传 None：不落图片素材
    pipe.pipe_classify()
    pipe.pipe_parse()
    # 落到临时目录（pipe_mk_markdown 需要一个可写根目录），随即按标记切页
    with tempfile.TemporaryDirectory(prefix="zhilan-mineru-") as tmp_dir:
        markdown_text = pipe.pipe_mk_markdown(tmp_dir, drop_mode="none")
    return markdown_text, _pages_from_markdown(markdown_text)


def _mismatch_reason(errors: list[str]) -> str:
    return (
        "MinerU 版本 API 不匹配：已安装的 mineru/magic_pdf 不提供任何已知解析入口。"
        f"已尝试 {'；'.join(errors)}"
    )


def _parse_pdf_entrypoints(
    data: bytes, vlm_config=None, tier_override: str | None = None
) -> tuple[str, list[str]]:
    """PDF 四入口依次尝试（阶段 2 原 ``_parse_with_mineru`` 主体；阶段 4 只加了 ``vlm_config`` 透传）。

    调用方：:func:`parse_pdf`（真实 PDF）、:func:`parse_image` 的兜底路径（图片先包成单页 PDF）。

    ``vlm_config`` 只对入口 ①② 有意义（它们是本仓库的 VLM 路由会真正命中的门面）；
    入口 ③（旧版 ``mineru.parse``）/ ④（``magic_pdf`` 2.x 管道）**调用形态逐字未改**——
    旧版 API 不认引擎配置，多传一个关键字只会让退路全废。

    ``tier_override``（D2 · 条目「VLM 失败逐级降级」）：**本次解析档位的唯一决策点**——
    非 ``None`` 时直接采用（**绕过** :func:`_mineru_tier` 的 ``MINERU_BACKEND`` 透传/分档），
    ``None`` 时按 :func:`_mineru_tier` 解析。解析出来的档位**同时**交给入口 ①②：入口 ② 原先不传
    tier（吃 MinerU 硬编码默认 ``standard``），降级到 ``basic`` 时若不补这一处，
    「备用入口也真的用 basic 跑」就是假的；两入口用同一个值也保证「主入口/备用入口来自同一决策点」。
    """
    tier = _mineru_tier() if tier_override is None else tier_override
    errors: list[str] = []
    for label, entry in (
        ("mineru.parser.parse", lambda d: _parse_via_parser_facade(d, vlm_config=vlm_config, tier_override=tier)),
        (
            "mineru.parser.MinerUParser",
            lambda d: _parse_via_parser_instance(d, vlm_config=vlm_config, tier_override=tier),
        ),
        ("mineru.parse", _parse_via_high_level_api),
        ("magic_pdf UNIPipe", _parse_via_magic_pdf),
    ):
        try:
            return entry(data)
        except _EntryUnavailable as exc:
            errors.append(f"{label}: {exc}")
    raise RuntimeError(_mismatch_reason(errors))


def _parse_vlm(data: bytes) -> tuple[str, list[str]]:
    """VLM 路由的 PDF 解析（阶段 4 · 条目 4.1）。

    与 pipeline 路由**共用同一条四入口链**——VLM 与 pipeline 的差别只在 MinerU 侧的引擎/档位参数，
    不在调用形态（不新写多入口退路，否则会多出一套要维护的版本适配）。

    失败口径（规格 3/4 的分工，别搞反）：
    - 这里**只把 VLM 侧的失败转成中文可读的 ``DocumentParseError``**（缺包 / 权重未就绪 /
      显存不足 / 引擎原生错误都归这一句）；
    - **回落 pypdf 由上层 ``parsing.parse_document_pages`` 的 PDF 分支做**——那里已有成熟路径，
      在这里再复制一份回落会让同一次失败把同一份 PDF 解析两遍；
    - 空结果同样算失败（与 :func:`parse_pdf` 同一纪律），否则空文本会静默流进切块入库。
    """
    try:
        text, pages = _parse_pdf_entrypoints(data, vlm_config=_vlm_config())
    except DocumentParseError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 绝不让 VLM 侧异常炸穿调用链
        # 顺手给一条可执行的下一步：VLM 挂了（显存不足 / 引擎错误）时，同机往往仍有可用的 ONNX 档，
        # 但「本层只转译、回落归上层」是规格定的分工（上层的回落目标是 pypdf，不是 pipeline），
        # 故这里只能把路指出来，实际降档由用户改配置决定。
        log.warning(
            "MinerU VLM 解析失败（%s）；可改 MINERU_BACKEND=pipeline（ONNX 档）或关掉 MINERU_VLM_ENABLE 重试",
            exc,
        )
        raise DocumentParseError(f"MinerU VLM 解析失败：{exc}") from exc
    if not text.strip() and not any(page.strip() for page in pages):
        raise DocumentParseError("MinerU VLM 解析失败：返回结果为空（未识别出任何文本）")
    return text, pages


def _parse_with_mineru(data: bytes, tier_override: str | None = None) -> tuple[str, list[str]]:
    """按 :func:`_select_backend` 的路由分发：``vlm`` → :func:`_parse_vlm`，否则 PDF 四入口链。

    ``tier_override``（D2 · 条目「VLM 失败逐级降级」）：显式指定档位时**不再按路由分发**——
    直接走 pipeline 四入口链并把该档位交给解析器（**不走** :func:`_parse_vlm`：它会因
    ``_select_backend()`` 仍是 ``vlm`` 而照带 ``vlm_config``，而重试必须**不带** VLM 配置）。
    ``None`` 时行为与阶段 4 **逐字一致**。

    **只有「入口不存在」才换下一个入口**：`ImportError` / `AttributeError` 的捕获范围**严格限定在**
    各入口自己的导入与能力探测阶段**（由各入口函数内部把导入失败归一成 `_EntryUnavailable`），
    绝不包住真正的解析调用。

    为什么必须这样切（2026-09-17 独立复核 F1）：若把调用也包进 `except (ImportError, AttributeError)`，
    则 MinerU **运行期**的 `AttributeError`（内部 bug）与 `ModuleNotFoundError`（懒加载缺可选依赖）
    都会被当成「本版本没有这个入口」，于是：
    ① 报错首句变成误导性的「版本 API 不匹配」，把真因埋进括号里；
    ② 入口①与②在 4.0 里共享同一套内部实现（`mineru.parser.parse` 就是 `MinerUParser(...).parse(...)`），
       同一个 PDF 会被**完整解析两遍**，CPU 上可能是数分钟的浪费；
    ③ 万一某个退路入口碰巧成功，真异常被彻底掩盖而无任何日志。
    入口是否存在，已由「导入 + `callable()` 探测」充分覆盖，调用期再吞没有增益。
    """
    if tier_override is None and _select_backend() == "vlm":
        return _parse_vlm(data)
    return _parse_pdf_entrypoints(data, tier_override=tier_override)


def _parse_pdf_attempt(data: bytes, tier_override: str | None = None) -> tuple[str, list[str]]:
    """**一次**解析尝试：既有异常转译 + 「空结果算失败」都收在这一处（避免两份实现漂移）。

    ``tier_override=None`` → 走 :func:`_parse_with_mineru` 的路由（首次尝试，与阶段 4 逐字一致）；
    非 ``None`` → **只走 pipeline 链**并把该档位显式交给解析器（D2 的降级重试：不走
    :func:`_parse_vlm`、**不带** ``vlm_config``）。
    """
    try:
        if tier_override is None:
            text, pages = _parse_with_mineru(data)
        else:
            text, pages = _parse_pdf_entrypoints(data, tier_override=tier_override, vlm_config=None)
    except DocumentParseError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 绝不让 MinerU 内部异常炸穿调用链
        raise DocumentParseError(f"MinerU 解析 PDF 失败：{exc}") from exc
    if not text.strip() and not any(page.strip() for page in pages):
        raise DocumentParseError("MinerU 解析 PDF 失败：返回结果为空（未识别出任何文本）")
    return text, pages


def parse_pdf(data: bytes) -> tuple[str, list[str]]:
    """走 MinerU 解析 PDF，返回 (全文, 按页文本)；``standard`` / ``advanced`` 档失败后降级重试一次。

    任何异常（未安装 / API 不匹配 / 解析失败）都转成中文可读的 ``DocumentParseError``，
    调用方的回落分支据此决定是否需要改走 pypdf。

    **空结果也算失败**：MinerU 正常返回但一个字都没解析出来（统筹 §4.3 点名要覆盖的
    「返回空文本」情形）时同样抛 ``DocumentParseError``——否则空文本会静默流进切块与入库，
    产出「入库成功但零 chunk」的假象；转成失败后调用方回落 pypdf，两条路都空才报
    ``SCANNED_PDF_HINT``，与「先试 OCR、失败回落」的既有语义一致。

    **D2 · 逐级降级**：**首次仍尊重指定档位**；只有在本次实际档位（:func:`_mineru_tier` 的值，
    **发起解析之前取一次**）∈ {``standard``, ``advanced``} 时，**失败或返回空文本后降级重试
    一次 ``basic``**（``basic`` / ``flash`` / 未知档位一律不重试，未知档位**不猜**）。
    五条不能动的细节：

    - 档位在**发起解析之前**取一次并记下，失败后**不重算**（那时可能已经是另一个值，拿它判降级就是误判）；
    - 判断落在**档位**上而不是「路由是不是 vlm」上：显式 ``standard`` / ``advanced`` 的路由是
      ``pipeline``（:func:`_resolve_backend`），只在 VLM 分支加重试会漏掉它们；
    - 重试**走 pipeline 链**（:func:`_parse_pdf_entrypoints`）而**不走** :func:`_parse_vlm`：
      后者会因 ``_select_backend()`` 仍是 ``vlm`` 而照带 ``vlm_config``，违反「重试不携带 VLM 配置」；
    - **恰好一次**：两次都失败就冒泡成 ``DocumentParseError``，pypdf 回落仍由上层
      ``parsing.parse_document_pages`` 做（本层**不复制**回落逻辑）；
    - 原失败原因 + 降级档位 + 重试结果（成功/失败）都落日志——那是用户唯一的自查线索。
    """
    # 本次实际档位：**发起解析之前**取一次（`_mineru_tier` 走 `_select_backend` 的缓存，不重复探测）
    try:
        tier = _mineru_tier()
    except Exception as exc:  # noqa: BLE001 —— 档位判定本身炸了也不许把裸异常抛出本层（不变量 3）
        # 不变量 3（绝不炸穿调用链）+ 档位表最后一行（「未知 / 无法确定 ⇒ 不猜测档位」）：
        # 记档位失败时按「档位未知」处理——**不降级**，真正的解析尝试照旧走（失败会被下面的
        # 既有口径转译成 `DocumentParseError`）。这一句同时堵住一个真实的回归面：档位判定原先
        # 只在被 try 包住的解析链里发生，现在提前到了 try 之外，没有它就可能抛裸异常给上层。
        log.warning("MinerU 档位判定失败（%s），本次按「档位未知」处理：不降级、不猜测档位", exc)
        tier = ""
    try:
        return _parse_pdf_attempt(data)
    except DocumentParseError as first_error:
        if tier not in _DOWNGRADE_FROM_TIERS:
            raise
        log.warning(
            "MinerU %s 档解析失败（%s）；降级重试一次 %s 档（走 pipeline 链、不带 VLM 配置）",
            tier,
            first_error,
            _DOWNGRADE_TO_TIER,
        )
        try:
            result = _parse_pdf_attempt(data, tier_override=_DOWNGRADE_TO_TIER)
        except DocumentParseError as retry_error:
            log.warning("MinerU 降级 %s 档重试仍失败（%s）；不再重试，交由上层兜底", _DOWNGRADE_TO_TIER, retry_error)
            raise
        log.info("MinerU 降级 %s 档重试成功（原 %s 档失败原因：%s）", _DOWNGRADE_TO_TIER, tier, first_error)
        return result


# ---------------------------------------------------------------------------
# 图片 OCR（阶段 3 · 条目 3.2）
# ---------------------------------------------------------------------------

# 图片格式 → Pillow 编码名。这份表既是「认哪些格式」的白名单，也是「能编回什么格式」的能力表。
_IMAGE_FORMATS = {
    b"\x89PNG\r\n\x1a\n": ("png", "PNG"),
    b"\xff\xd8\xff": ("jpg", "JPEG"),
    b"BM": ("bmp", "BMP"),
    b"II*\x00": ("tiff", "TIFF"),
    b"MM\x00*": ("tiff", "TIFF"),
}


def _image_format(data: bytes) -> tuple[str, str] | None:
    """按魔数判图片格式，返回 ``(后缀, Pillow 编码名)``；认不出返回 ``None``。

    为什么需要它：:func:`parse_image` 只拿到 bytes，而 MinerU 的 ``parse(path)`` 是**按路径后缀**
    判定输入类型的（见 :func:`_parse_via_parser_facade`），故必须把后缀还原出来才好落临时文件。
    判据与 MinerU 4.0 的 ``IMAGE_EXTENSIONS`` 取交集（png / jpg / jpeg / bmp / tiff / webp）——
    它支持的 gif / jp2 不在本仓库白名单内，.webp / .jpeg 则没有稳定魔数前缀（见下方兜底）。
    """
    for header, pair in _IMAGE_FORMATS.items():
        if data.startswith(header):
            return pair
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "WEBP"
    return None


def _image_entrypoints(
    data: bytes, filename: str, seen: dict | None = None, vlm_config=None
) -> tuple[str, list[str]]:
    """图片原生三入口依次尝试：MinerU 4.0 会自己把图片包成单页 PDF 再解析。

    只尝试**收路径**的三个入口：``mineru.parser.parse`` / ``mineru.parser.MinerUParser`` /
    ``mineru.parse``（旧版形态，可能收 bytes）。``magic_pdf`` 2.x 管道是 PDF 专用，不进这一档，
    留给 :func:`parse_image` 的兜底路径（先把图片包成 PDF 再走完整四入口）。

    ``seen``（可选）用于记录**实际命中**的入口名——「原生入口成功」与「PDF 兜底成功」在
    外部表现上可能一样（同一个旧版 ``mineru.parse`` 两条路都认），只有命中名能区分，
    测试与排障都靠它。

    ``vlm_config``（阶段 4 · 条目 4.1 留的透传口子）：**当前恒为 ``None``**——阶段 4 §三 明确
    「图片始终走 pipeline，不接 VLM」，故 :func:`parse_image` 不传它；留这个形参是为了将来接图片
    VLM 时不必再改这条链的调用形态（另开条目）。

    与 :func:`_parse_pdf_entrypoints` 同一条纪律：**只有「入口不存在」才换下一个入口**
    （:class:`_EntryUnavailable`），解析运行期的任何异常原样冒泡，由 :func:`parse_image` 忠实转译。
    """
    errors: list[str] = []
    for label, entry in (
        ("mineru.parser.parse", lambda d: _parse_via_parser_facade(d, filename, vlm_config=vlm_config)),
        ("mineru.parser.MinerUParser", lambda d: _parse_via_parser_instance(d, filename, vlm_config=vlm_config)),
        ("mineru.parse", _parse_via_high_level_api),
    ):
        try:
            result = entry(data)
        except _EntryUnavailable as exc:
            errors.append(f"{label}: {exc}")
            continue
        if seen is not None:
            seen["native_route"] = label
        return result
    raise _EntryUnavailable("；".join(errors))


def _to_rgb(image):
    """图片转 RGB，透明区按**白底**合成（OCR 场景下白底最接近纸张；直接丢弃 alpha 会变黑底）。

    ``convert("RGB")`` 对 RGBA 是「丢掉 alpha」，透明区会变成黑块——截图与带透明层的
    Excel/PPT 导出图很常见，故这里显式走一次白底 paste。
    """
    from PIL import Image

    if image.mode == "RGB":
        return image.copy()
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    return image.convert("RGB")


def _encoded_image(data: bytes, pillow_format: str) -> tuple[bytes, str] | None:
    """把图片重编码成 OCR 友好的字节：长边压到 2000px 以内、剥掉落不进 PDF 的 alpha 通道。

    为什么必须做（漂在临时目录里的素材要为后面几步负责）：
    - 手机拍照常见 4000×3000，直接包单页 PDF 体积会膨胀到上传限制（20MB）附近，
      MinerU 还要按像素吃内存，等比压缩是**前置**条件而不是优化；
    - PNG 带 alpha / 调色板 / 16bit 时 Pillow 存 PDF 会失败（``cannot save mode`` /
      ``cannot write mode RGBA as PDF``），故统一转 RGB（透明区按白底合成，见 :func:`_to_rgb`）；
    - 多帧（动图 / 多页 tiff）只取第一帧，与阶段 3 文档 §六「仅处理第一页」口径一致。

    返回 ``(编码后的字节, 实际编码名)``；编不出来（Pillow 缺席 / 格式不支持 / 垃圾数据）返回 ``None``。
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(BytesIO(data)) as image:
            if getattr(image, "n_frames", 1) > 1:
                image.seek(0)
            frame = _to_rgb(image)
        max_side = max(frame.size)
        if max_side > _IMAGE_MAX_SIDE:
            scale = _IMAGE_MAX_SIDE / max_side
            frame = frame.resize(
                (max(1, round(frame.width * scale)), max(1, round(frame.height * scale)))
            )
        buffer = BytesIO()
        frame.save(buffer, format=pillow_format, quality=85)
        return buffer.getvalue(), pillow_format
    except Exception:  # noqa: BLE001 —— 编码不了就交给调用方决定（有原生入口时不影响解析）
        return None


def _image_pdf_bytes(data: bytes, pillow_format: str) -> bytes:
    """把图片包成单页 PDF（Pure Pillow，不引入新依赖）。

    用途是兜底路径（MinerU 版本没有图片原生入口时改喂 PDF），保证 :func:`parse_image`
    在旧版 MinerU 上仍然可用。Pillow 在打包环境里是 MinerU 自己的传递依赖，故这里
    **不新增声明**（统筹 §三「避免重复声明」；`resvg-py` 那条提醒同理）。

    多页 tiff 只取第一页（阶段 3 文档 §六）；压缩与色彩转换见 :func:`_encoded_image`。
    任何失败都转成中文可读的 ``DocumentParseError``。
    """
    try:
        from PIL import Image
    except ImportError as exc:
        raise DocumentParseError(
            "图片解析需要 Pillow（pip install pillow 后重试）；"
            "或改用带图片原生入口的 MinerU 3.x/4.0 版本。"
        ) from exc
    try:
        encoded = _encoded_image(data, pillow_format)
        if encoded is None:
            # 编码失败（格式不支持 / 垃圾数据）时退到原始字节：Pillow 打开它时会给真因
            encoded = (data, pillow_format)
        image_bytes, _actual_format = encoded
        with Image.open(BytesIO(image_bytes)) as image:
            # 统一转 RGB 再存：Pillow 的 PDF 编码器不接受 I;16 等模式（实测 ValueError），
            # 而 RGBA / 调色板图在 OCR 场景下也不该带 alpha，转 RGB 是唯一稳妥出口。
            frame = _to_rgb(image)
            buffer = BytesIO()
            frame.save(buffer, format="PDF")
            return buffer.getvalue()
    except DocumentParseError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 任何编码失败都转成中文可读报错
        raise DocumentParseError(f"MinerU 图片 OCR 失败：{exc}") from exc


def parse_image(data: bytes) -> tuple[str, list[str]]:
    """走 MinerU 解析图片（OCR），返回 (全文, 按页文本)。

    取用顺序（条目 3.2 规格 1，两条路径**同一份输入只解析一次**）：

    ① **原生图片入口**（首选）：MinerU 4.0 的 ``mineru.parser.parse(图片路径)``——参考源码核对
       （2026-09-17，只读快照 ``MinerU-master`` 4.0.0）：``mineru/parser/mineru_parser.py:155``
       ``if suffix in IMAGE_EXTENSIONS: file_bytes = PDFDocument.from_image(file_bytes).bytes``，
       即上游自己就把图片包成单页 PDF，我们只需给对后缀。
    ② **包装单页 PDF 兜底**（条目 3.2 规格 2 的回退方案）：只有 ① 的入口**全部不存在**
       （``_EntryUnavailable``：旧版 MinerU / 只装了 ``magic_pdf`` 的 2.x 环境）时才启用，
       用 Pillow 把图片包成单页 PDF 再走完整 PDF 四入口。此回退对**多页 tiff 只取第一页**。

    异常口径与 :func:`parse_pdf` 一致：任何内部异常 → 中文可读的 ``DocumentParseError``；
    空结果同样算失败（「入库成功但零 chunk」的假象不允许出现）。
    """
    detected = _image_format(data)
    if detected is None:
        raise DocumentParseError(f"图片 OCR 失败：无法识别的图片格式，请改用 {_IMAGE_FORMATS_HINT} 后重试。")
    suffix, pillow_format = detected

    text = ""
    pages: list[str] = []
    entry_error = ""
    route: dict[str, str] = {}  # 实际命中的入口名（原生 or 兜底），排障用
    try:
        text, pages = _image_entrypoints(data, f"input.{suffix}", route)
    except DocumentParseError:
        raise
    except _EntryUnavailable as exc:
        entry_error = str(exc)
    except Exception as exc:  # noqa: BLE001 —— 原生入口运行期异常原样转译，不静默换条路重解析
        raise DocumentParseError(f"MinerU 图片 OCR 失败：{exc}") from exc

    if entry_error:
        try:
            text, pages = _parse_pdf_entrypoints(_image_pdf_bytes(data, pillow_format))
        except DocumentParseError:
            raise
        except Exception as exc:  # noqa: BLE001
            # 兜底链把「入口不存在」自己汇总成 RuntimeError（「API 不匹配」+ 逐入口原因），
            # 故这里**没有** `_EntryUnavailable` 分支可接（独立复核 C6：那种分支不可达）。
            # 报错把**两条路**的原因都带上：否则用户只看到 PDF 侧的原因，
            # 看不出「图片原生入口也试过了」（独立复核 C2 之外的可读性修正：`{exc}` 原来重复出现两次）。
            raise DocumentParseError(
                f"MinerU 图片 OCR 失败：{exc}（原生图片入口：{entry_error}）"
            ) from exc
        # 两条路都可能由同一个旧版 `mineru.parse` 命中，故把命中名标注清楚
        route["native_route"] = f"包装 PDF 兜底 → {route.get('native_route', '未知入口')}"
        log.info("MinerU 图片 OCR 走兜底路径（该版本没有图片原生入口）")

    log.debug("MinerU 图片 OCR 命中入口：%s", route.get("native_route", "未知"))

    if not text.strip() and not any(page.strip() for page in pages):
        raise DocumentParseError("MinerU 图片 OCR 失败：返回结果为空（未识别出任何文本）")
    return text, pages
