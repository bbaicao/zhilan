"""阶段 4 · 条目 4.3 回归用例：VLM 后端路由 + auto 探测 + 默认不开箱纪律。

纪律（统筹 §3.4）：**全部 stub 化**——不连真 GPU、不连真 VLM、不装 MinerU、不下载权重、
不跑外部命令（`nvidia-smi` 一律被 monkeypatch 掉）。需要真机推理的验证走阶段 4 证据
`回归检查-真机实测.md`（不在本文件里）。

覆盖点（对应阶段 4 文档条目 4.3 规格）：
① `MINERU_BACKEND=vlm`（开关开 + 就绪）→ 真的走 `_parse_vlm`，且门面收到 `vlm_config=`；
② `auto` + 就绪成立 → 选中 vlm；③ `auto` + 就绪不成立 → 回落 pipeline（且**不加载** VLM）；
④ VLM 侧失败 → 转译成中文 `DocumentParseError`，**回落由上层做**（本层不复制回落逻辑）；
⑤ 探测结果被缓存（连续两次调用只探测一次），且有重置入口；
⑥ 两条本阶段特有的风险点：「有 GPU 但无 torch」必须选 vlm、「权重只缺 mmproj」必须判未就绪；
⑦ 核心验收点：`MINERU_VLM_ENABLE` 默认关闭时，不探测、不 import `mineru_llama_cpp`、auto ≡ pipeline。

另有三处**契约形态**断言：`_mineru_tier()` 的分档（vlm→standard / pipeline→basic）、
「只有 VLM 路由才多带 `vlm_config=` 关键字实参」（pipeline 路由的调用形态与阶段 2/3 逐字一致）、
以及 `available()` 摘要里的 backend/device（条目 4.2 规格 2）。
"""

import logging
import sys
import types

import pytest

from app.core.config import settings
from app.core.errors import DocumentParseError
from app.pipeline import mineru

PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-png-body"


@pytest.fixture(autouse=True)
def _clean_module_state():
    """每个用例前后清掉假重依赖模块与后端探测缓存（模块级状态必须能被用例重置）。"""

    def _clean():
        for name in ("mineru", "mineru.parser", "mineru.config", "magic_pdf", "mineru_llama_cpp", "torch"):
            sys.modules.pop(name, None)
        mineru._reset_backend_cache()

    _clean()
    yield
    _clean()


def _enable_mineru(monkeypatch):
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)


def _stub_vlm_ready(monkeypatch, *, ready: bool = True, reason: str = "stub：就绪三件套成立"):
    """把「就绪三件套」判定换掉——用例不碰真 GPU / 真权重（判据本身的逻辑另有专门用例钉）。"""
    monkeypatch.setattr(mineru, "_vlm_ready", lambda: (ready, reason))


def _forbid_probes(monkeypatch):
    """任何探测被调用即失败：用于钉「默认关闭时一次都不探测」。"""

    def _boom(*_args, **_kwargs):
        raise AssertionError("MINERU_VLM_ENABLE=False（或就绪判定已定）时不该做任何 VLM 探测")

    monkeypatch.setattr(mineru, "_gpu_available", _boom)
    monkeypatch.setattr(mineru, "_vlm_weights_ready", _boom)
    monkeypatch.setattr(mineru, "_vlm_engine_ready", _boom)
    monkeypatch.setattr(mineru, "_vlm_weight_files", _boom)


def _stub_parser_facade(monkeypatch, seen: dict):
    """注入「4.0 形态」的假 mineru 包：`mineru.parser.parse(path, *, tier, vlm_config?)`。

    ``seen`` 记录实参，用来断言「VLM 路由才带 vlm_config」「pipeline 路由的调用形态逐字不变」。
    """

    def _parse(path, **kwargs):
        seen["path"] = str(path)
        seen["kwargs"] = kwargs
        seen["suffix"] = path.suffix
        return "假 MinerU 产出"

    pkg = types.ModuleType("mineru")
    pkg.__path__ = []
    sub = types.ModuleType("mineru.parser")
    sub.parse = _parse
    pkg.parser = sub
    monkeypatch.setitem(sys.modules, "mineru", pkg)
    monkeypatch.setitem(sys.modules, "mineru.parser", sub)
    return seen


def _stub_vlm_config(monkeypatch):
    """注入假的 `mineru.config.VlmConfig`，让 `_vlm_config()` 不必真的装 MinerU。"""

    class _VlmConfig:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    module = types.ModuleType("mineru.config")
    module.VlmConfig = _VlmConfig
    monkeypatch.setitem(sys.modules, "mineru.config", module)
    return _VlmConfig


# ---------------------------------------------------------------------------
# D2（VLM 失败逐级降级）用的桩与开关
# ---------------------------------------------------------------------------
def _enable_vlm_route(monkeypatch, backend: str = "vlm") -> None:
    """把配置摆成「走 VLM 路由」：开关开 + 就绪成立 + 假 `VlmConfig`（不必真装 MinerU）。"""
    monkeypatch.setattr(settings, "MINERU_BACKEND", backend)
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", True)
    _enable_mineru(monkeypatch)
    _stub_vlm_ready(monkeypatch)
    _stub_vlm_config(monkeypatch)


def _scripted_entrypoints(monkeypatch, outcomes: list) -> list[dict]:
    """把 `_parse_pdf_entrypoints` 换成「按调用次序出牌」的桩，并**记录每次实参**。

    `outcomes` 的第 n 项 = 第 n 次调用的结果：`Exception` 实例原样抛出，其余当返回值给出。
    记实参才能断言 D2 的两条硬边界：重试**显式用 basic**、且**不带 vlm_config**。
    """
    calls: list[dict] = []

    def _entrypoints(data, vlm_config=None, tier_override=None):
        calls.append({"data": data, "vlm_config": vlm_config, "tier_override": tier_override})
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(mineru, "_parse_pdf_entrypoints", _entrypoints)
    return calls


def _inject_mineru_parser(monkeypatch, *, parse=None, parser_cls=None):
    """注入假 `mineru` / `mineru.parser` 模块（`parse` / `MinerUParser` 可分别缺席，模拟不同版本）。"""
    pkg = types.ModuleType("mineru")
    pkg.__path__ = []
    sub = types.ModuleType("mineru.parser")
    if parse is not None:
        sub.parse = parse
    if parser_cls is not None:
        sub.MinerUParser = parser_cls
    pkg.parser = sub
    monkeypatch.setitem(sys.modules, "mineru", pkg)
    monkeypatch.setitem(sys.modules, "mineru.parser", sub)


# ---------------------------------------------------------------------------
# ⑦ 默认不开箱（本路线核心验收点）
# ---------------------------------------------------------------------------
def test_default_off_equals_pipeline_without_probing_or_importing(monkeypatch):
    """`MINERU_VLM_ENABLE` 默认 False ⇒ auto ≡ pipeline：不报错、不探测、不 import 引擎模块。

    对应验收标准 7。三项都必须为真，缺一不可：
    ① `_select_backend()` 是 pipeline（路由不跑 VLM）；
    ② **一次探测都没有**（nvidia-smi 子进程、权重的文件系统检查、`find_spec` 全不该发生）；
    ③ `mineru_llama_cpp` 不在 `sys.modules`（`find_spec` 本来就不 import，这里把断言钉死）。
    """
    assert settings.MINERU_VLM_ENABLE is False  # 默认关（另有配置默认值栅栏用例）
    monkeypatch.setattr(settings, "MINERU_BACKEND", "auto")
    _enable_mineru(monkeypatch)
    _forbid_probes(monkeypatch)

    assert mineru._select_backend() == "pipeline"
    assert mineru._mineru_tier() == "basic"  # 不依赖 GGUF 的 ONNX-only 档
    assert "mineru_llama_cpp" not in sys.modules


def test_explicit_vlm_with_switch_off_falls_back_to_pipeline(monkeypatch):
    """显式 `MINERU_BACKEND=vlm` 但开关没开 → 按 pipeline 解析（VLM 件是自备项，开关即授权）。"""
    monkeypatch.setattr(settings, "MINERU_BACKEND", "vlm")
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", False)
    _enable_mineru(monkeypatch)
    _forbid_probes(monkeypatch)

    assert mineru._select_backend() == "pipeline"
    assert mineru._mineru_tier() == "basic"


def test_explicit_pipeline_never_probes_even_when_switch_on(monkeypatch):
    """`MINERU_BACKEND=pipeline` 是明确「不要 VLM」：开关开着也不该跑探测（省一次子进程）。"""
    monkeypatch.setattr(settings, "MINERU_BACKEND", "pipeline")
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", True)
    _enable_mineru(monkeypatch)
    _forbid_probes(monkeypatch)

    assert mineru._select_backend() == "pipeline"
    assert mineru._mineru_tier() == "basic"


# ---------------------------------------------------------------------------
# ② / ③ auto 探测的两个方向
# ---------------------------------------------------------------------------
def test_auto_selects_vlm_when_ready(monkeypatch):
    monkeypatch.setattr(settings, "MINERU_BACKEND", "auto")
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", True)
    _enable_mineru(monkeypatch)
    _stub_vlm_ready(monkeypatch)

    assert mineru._select_backend() == "vlm"
    assert mineru._mineru_tier() == "standard"  # VLM 档 = standard（high effort）


def test_auto_falls_back_to_pipeline_when_not_ready(monkeypatch):
    """未自备 VLM 件（缺权重 / 缺引擎 / 无 GPU 任一）→ 回落 pipeline，**不报错**、不试着加载。"""
    monkeypatch.setattr(settings, "MINERU_BACKEND", "auto")
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", True)
    _enable_mineru(monkeypatch)
    _stub_vlm_ready(monkeypatch, ready=False, reason="stub：权重缺失 mmproj.gguf")

    routed: list[bytes] = []
    monkeypatch.setattr(mineru, "_parse_vlm", lambda data: routed.append(data) or ("不该走到这里", []))

    assert mineru._select_backend() == "pipeline"
    assert mineru._mineru_tier() == "basic"

    seen = _stub_parser_facade(monkeypatch, {})
    text, _pages = mineru.parse_pdf(b"%PDF-1.4 payload")

    assert text == "假 MinerU 产出"
    assert routed == [], "就绪不成立时绝不能调用 _parse_vlm（不尝试加载 VLM）"
    assert "vlm_config" not in seen["kwargs"], "pipeline 路由不得多带 vlm_config（调用形态与阶段 2/3 一致）"


def test_vlm_ready_reports_all_missing_checks(monkeypatch):
    """就绪判定要**三项全测**（不短路）：日志里必须能一眼看出到底缺哪件。"""
    monkeypatch.setattr(mineru, "_gpu_available", lambda: (False, "未找到 nvidia-smi"))
    monkeypatch.setattr(mineru, "_vlm_weights_ready", lambda: (False, "权重缺失 mmproj.gguf"))
    monkeypatch.setattr(mineru, "_vlm_engine_ready", lambda: (False, "未安装 mineru-llama-cpp"))

    ok, reason = mineru._vlm_ready()

    assert ok is False
    for fragment in ("GPU", "nvidia-smi", "权重", "mmproj.gguf", "引擎", "mineru-llama-cpp"):
        assert fragment in reason, f"诊断串缺少 {fragment!r}：{reason}"


# ---------------------------------------------------------------------------
# ① 显式 vlm 路由真的走 _parse_vlm
# ---------------------------------------------------------------------------
def test_explicit_vlm_routes_to_parse_vlm_with_config(monkeypatch):
    """`MINERU_BACKEND=vlm` + 开关开 + 就绪 → 走 `_parse_vlm`，且门面收到 `vlm_config=`（llama-cpp）。"""
    monkeypatch.setattr(settings, "MINERU_BACKEND", "vlm")
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", True)
    _enable_mineru(monkeypatch)
    _stub_vlm_ready(monkeypatch)
    _stub_vlm_config(monkeypatch)
    seen = _stub_parser_facade(monkeypatch, {})

    assert mineru._select_backend() == "vlm"
    text, pages = mineru.parse_pdf(b"%PDF-1.4 payload")

    assert text == "假 MinerU 产出"
    assert pages == ["假 MinerU 产出"]
    assert seen["kwargs"]["tier"] == "standard"
    assert seen["kwargs"]["vlm_config"].engine == "llama-cpp", "VLM 路由必须显式钉住 llama-cpp 引擎"


def test_pipeline_route_passes_no_vlm_config(monkeypatch):
    """对照组：pipeline 路由的调用形态与阶段 2/3 **逐字一致**（只传 tier，不多传任何关键字）。"""
    monkeypatch.setattr(settings, "MINERU_BACKEND", "auto")
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", False)
    _enable_mineru(monkeypatch)
    seen = _stub_parser_facade(monkeypatch, {})

    mineru.parse_pdf(b"%PDF-1.4 payload")

    assert seen["kwargs"] == {"tier": "basic"}, seen["kwargs"]


# ---------------------------------------------------------------------------
# ④ 失败口径：转译不炸穿，且**不在这里**回落
# ---------------------------------------------------------------------------
def test_vlm_failure_is_translated_without_local_fallback(monkeypatch):
    """VLM 侧任何异常 → 中文 `DocumentParseError`；入口链被调用**两次**（VLM 一次 + 降级 basic 一次）。

    这条钉的是条目 4.1 规格 4 的分工：回落 pypdf 由上层 `parsing.parse_document_pages` 做；
    若在这里再写一份 pypdf 回落，一次失败会把同一份 PDF 解析两遍。

    **D2 改口径（2026-09-19）**：本层新增的「降级重试一次 basic」**不是**回落——它是同一个 MinerU
    的另一个档位，两次都失败仍然只抛 `DocumentParseError`（pypdf 回落依旧不在这里）。故断言从
    「只被调用一次」改为「恰好两次、第二次显式用 basic、且不带 vlm_config」。
    """
    calls: list[dict] = []

    def _boom(data, vlm_config=None, tier_override=None):
        # 桩必须接受 vlm_config / tier_override：VLM 路由按关键字透传 vlm_config，降级重试按关键字
        # 透传 tier_override（收到什么本身就是「走对没走对」的旁证）
        calls.append({"data": data, "vlm_config": vlm_config, "tier_override": tier_override})
        raise RuntimeError("llama.cpp 加载失败（stub）")

    monkeypatch.setattr(settings, "MINERU_BACKEND", "vlm")
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", True)
    _enable_mineru(monkeypatch)
    _stub_vlm_ready(monkeypatch)
    _stub_vlm_config(monkeypatch)
    monkeypatch.setattr(mineru, "_parse_pdf_entrypoints", _boom)

    with pytest.raises(DocumentParseError) as excinfo:
        mineru.parse_pdf(b"%PDF-1.4 payload")

    message = str(excinfo.value)
    assert message.startswith("MinerU 解析 PDF 失败："), message
    assert "llama.cpp 加载失败" in message, "真因必须原样带出来（不能被换成泛泛的『解析失败』）"
    assert len(calls) == 2, f"应为「VLM 一次 + 降级 basic 一次」，实际 {len(calls)} 次"
    assert calls[0]["vlm_config"] is not None, "首次仍是 VLM 路由（带 llama-cpp 配置）"
    assert calls[1]["tier_override"] == "basic", "重试必须显式用 basic（不经 _mineru_tier 分档）"
    assert calls[1]["vlm_config"] is None, "重试不得携带 VLM 配置（硬边界）"


def test_vlm_empty_result_counts_as_failure(monkeypatch, caplog):
    """空文本不许静默流进切块（与 `parse_pdf` 同一纪律）；VLM 侧的失败原因不因降级而丢。

    **D2 改口径（2026-09-19）**：VLM 路由（standard 档）返回空**同样触发**降级重试；本用例的
    basic 桩也返回空 ⇒ 最终仍抛「返回结果为空」。断言三件事：① 确实降级重试了一次 basic；
    ② 空结果没能流出去；③ 「VLM 侧返回空」这条原因仍落进日志（排障线索）。
    """
    monkeypatch.setattr(settings, "MINERU_BACKEND", "vlm")
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", True)
    _enable_mineru(monkeypatch)
    _stub_vlm_ready(monkeypatch)
    _stub_vlm_config(monkeypatch)
    calls = _scripted_entrypoints(monkeypatch, [("", []), ("", [])])

    with caplog.at_level(logging.INFO, logger="app.pipeline.mineru"):
        with pytest.raises(DocumentParseError) as excinfo:
            mineru.parse_pdf(b"%PDF-1.4 payload")

    assert "空" in str(excinfo.value), str(excinfo.value)
    assert [call["tier_override"] for call in calls] == [None, "basic"], calls
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "VLM" in logged and "返回结果为空" in logged, logged


def test_vlm_failure_falls_back_to_pypdf_at_upper_layer(monkeypatch, scanned_pdf, pdf_with_text):
    """回落确实发生在**上层**：`parsing.parse_document_pages` 拿到 VLM 失败后走 pypdf 兜底。"""
    from app.pipeline import parsing

    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    monkeypatch.setattr(settings, "MINERU_BACKEND", "vlm")
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", True)
    _stub_vlm_ready(monkeypatch)
    monkeypatch.setattr(mineru, "available", lambda: (True, "MinerU 可用，backend=vlm, device=cuda"))
    monkeypatch.setattr(
        mineru, "parse_pdf", lambda _data: (_ for _ in ()).throw(DocumentParseError("MinerU VLM 解析失败：stub"))
    )

    # 密度达标的原生 PDF 根本不会走 MinerU（分流在前）——回落路径不受影响
    text, _pages = parsing.parse_document_pages("native.pdf", pdf_with_text)
    assert "Zhilan" in text

    # 扫描件：MinerU（VLM）失败 → 回落 pypdf；pypdf 也提不出字才报扫描件提示（不炸穿）
    from app.pipeline.parsing import SCANNED_PDF_HINT

    with pytest.raises(DocumentParseError) as excinfo:
        parsing.parse_document_pages("scanned.pdf", scanned_pdf)
    assert str(excinfo.value) == SCANNED_PDF_HINT, str(excinfo.value)


# ---------------------------------------------------------------------------
# ⑤ 缓存行为 + 重置入口
# ---------------------------------------------------------------------------
def test_probe_is_cached_and_reset_hook_forces_reprobe(monkeypatch):
    """连续两次选择只探测一次；`_reset_backend_cache()` 之后必须重新探测（测试入口不能被绕过）。"""
    monkeypatch.setattr(settings, "MINERU_BACKEND", "auto")
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", True)
    _enable_mineru(monkeypatch)

    counter = {"n": 0}

    def _gpu():
        counter["n"] += 1
        return True, "stub GPU"

    monkeypatch.setattr(mineru, "_gpu_available", _gpu)
    monkeypatch.setattr(mineru, "_vlm_weights_ready", lambda: (True, "stub 权重"))
    monkeypatch.setattr(mineru, "_vlm_engine_ready", lambda: (True, "stub 引擎"))
    mineru._reset_backend_cache()

    assert mineru._select_backend() == "vlm"
    assert mineru._select_backend() == "vlm"
    assert mineru._mineru_tier() == "standard"
    assert counter["n"] == 1, f"探测应只发生一次，实际 {counter['n']} 次"

    mineru._reset_backend_cache()
    assert mineru._select_backend() == "vlm"
    assert counter["n"] == 2, "重置后必须重新探测（否则用例会互相污染）"


# ---------------------------------------------------------------------------
# ⑥ 两条本阶段特有的风险点
# ---------------------------------------------------------------------------
def test_gpu_without_torch_still_selects_vlm(monkeypatch):
    """风险点 1：判据若照抄 `torch.cuda.is_available()`，本机（有 4060、无 torch）会永远选不上 VLM。

    「有 GPU」由 `nvidia-smi` 判定；本用例把 GPU 探测 stub 成「有」，其余条件齐备 ⇒ 必须选 vlm，
    且全程不许 `import torch`（本机根本没装 torch，照 torch 写会直接 ImportError / 恒 False）。
    """
    monkeypatch.setattr(settings, "MINERU_BACKEND", "auto")
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", True)
    _enable_mineru(monkeypatch)
    monkeypatch.setattr(mineru, "_gpu_available", lambda: (True, "NVIDIA GeForce RTX 4060 Laptop GPU, 8188 MiB"))
    monkeypatch.setattr(mineru, "_vlm_weights_ready", lambda: (True, "stub 权重齐"))
    monkeypatch.setattr(mineru, "_vlm_engine_ready", lambda: (True, "stub 引擎在"))
    mineru._reset_backend_cache()

    assert mineru._select_backend() == "vlm"
    assert "torch" not in sys.modules, "探测路径绝不能 import torch"


def test_weights_missing_mmproj_is_not_ready(monkeypatch, tmp_path):
    """风险点 2：只检查主模型文件会漏掉 mmproj 缺失（加载时才炸）——两个文件**都**要检查。

    这里跑的是**真实现**（`_vlm_weights_ready` 的检查逻辑），只把「权重文件清单从哪来」
    （`_vlm_weight_files`，要真装 MinerU 才能解析上游仓库登记）换成临时目录。
    """
    monkeypatch.setattr(mineru, "_vlm_weight_files", lambda: [tmp_path / "model.gguf", tmp_path / "mmproj.gguf"])
    (tmp_path / "model.gguf").write_bytes(b"stub")

    ok, reason = mineru._vlm_weights_ready()
    assert ok is False
    assert "mmproj.gguf" in reason and "缺失" in reason, reason

    (tmp_path / "mmproj.gguf").write_bytes(b"stub")
    ok, reason = mineru._vlm_weights_ready()
    assert ok is True, reason


def test_unresolvable_weight_registry_is_not_ready(monkeypatch):
    """未装 MinerU / 上游 API 变化 → 判「未就绪」，绝不能把异常抛进解析链。"""

    def _boom():
        raise ImportError("No module named 'mineru'")

    monkeypatch.setattr(mineru, "_vlm_weight_files", _boom)
    ok, reason = mineru._vlm_weights_ready()
    assert ok is False
    assert "无法解析权重路径" in reason, reason


# ---------------------------------------------------------------------------
# 图片链：本阶段明确「不接 VLM」，但透传口子要留好
# ---------------------------------------------------------------------------
def test_image_route_never_passes_vlm_config(monkeypatch):
    """条目 4.1 已核事实 2 的透传口子存在，但**当前恒为 None**（§三：图片始终走 pipeline）。"""
    monkeypatch.setattr(settings, "MINERU_BACKEND", "auto")
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", True)
    _enable_mineru(monkeypatch)
    _stub_vlm_ready(monkeypatch)
    seen: dict = {}

    def _recorder(data, filename, route=None, **kwargs):
        seen["filename"] = filename
        seen["extra_kwargs"] = kwargs
        return "图里认出来的字", []

    monkeypatch.setattr(mineru, "_image_entrypoints", _recorder)
    monkeypatch.setattr(mineru, "available", lambda: (True, "MinerU 可用，backend=vlm, device=cuda"))

    text, pages = mineru.parse_image(PNG_BYTES)

    assert text == "图里认出来的字"
    assert pages == []
    assert seen["filename"] == "input.png", "MinerU 靠后缀判输入类型，必须落真实后缀"
    assert "vlm_config" not in seen["extra_kwargs"], "图片 OCR 本阶段仍走 pipeline，调用点不得带 VLM 配置"


# ---------------------------------------------------------------------------
# `available()` 的摘要形态（条目 4.2 规格 2）
# ---------------------------------------------------------------------------
def test_available_summary_carries_backend_and_device(monkeypatch):
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    monkeypatch.setattr(settings, "MINERU_BACKEND", "auto")
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", False)
    monkeypatch.setattr(settings, "MINERU_DEVICE", "auto")
    monkeypatch.setitem(sys.modules, "mineru", types.ModuleType("mineru"))

    assert mineru.available() == (True, "MinerU 可用，backend=pipeline, device=auto")

    monkeypatch.setattr(settings, "MINERU_DEVICE", "cuda")
    mineru._reset_backend_cache()
    assert mineru.available() == (True, "MinerU 可用，backend=pipeline, device=cuda")


def test_available_not_installed_branch_does_not_probe(monkeypatch):
    """「未安装 MinerU」的分支必须**先返回**：不能因为新增探测让这条用例变成机器环境依赖。"""
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    _forbid_probes(monkeypatch)

    ok, reason = mineru.available()

    assert ok is False
    assert "未安装" in reason


# ---------------------------------------------------------------------------
# 独立复核（变异测试）后的补强用例
#
# 来源：`证据/S4-变异测试/S4-变异测试.md` 的「存活变异体清单」。首轮 24 个变异体**存活 7 个**，
# 且全部落在「探测函数的**本体**」与「诊断/路由之间的**连线**」上——根因是同一类**自证式打桩**
# （self-stub：桩打在了被测对象自己身上）。下面 6 条按复核方的建议落成正式用例，
# 每条都注明它击杀哪个变异体、以及原先为什么漏掉；补上后同一批变异体的预期得分为 24/24。
# ---------------------------------------------------------------------------
def test_gpu_probe_body_uses_nvidia_smi_and_never_imports_torch(monkeypatch):
    """击杀 **M7**（`_gpu_available()` 本体换成 `torch.cuda.is_available()`）。

    原先为什么漏掉：`test_gpu_without_torch_still_selects_vlm` 与 `_forbid_probes()` 都把
    `_gpu_available` **整个**换掉 ⇒ 本函数的函数体一次都没被执行过；而本机两个 venv 都没装 torch，
    M7 下它恒判「无 GPU」⇒ **有 4060 也永远选不上 VLM**，用例却全绿（这正是条目 4.2 专门警告的回归）。
    这里改打**更外层的桩**（`shutil.which` / `subprocess.run`），让函数体真的跑起来。
    """
    monkeypatch.setattr(mineru.shutil, "which", lambda _name: r"C:\fake\nvidia-smi.exe")
    calls: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = "NVIDIA GeForce RTX 4060 Laptop GPU, 8188 MiB\n"
        stderr = ""

    def _fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return _Proc()

    monkeypatch.setattr(mineru.subprocess, "run", _fake_run)

    ok, reason = mineru._gpu_available()

    assert calls, "GPU 探测必须走 nvidia-smi 子进程（M7 下这里为空）"
    assert ok is True, reason
    assert "RTX 4060" in reason, reason
    assert "torch" not in sys.modules, "GPU 探测绝不许 import torch"


def test_engine_probe_body_uses_find_spec_without_importing(monkeypatch):
    """击杀 **M8**（`_vlm_engine_ready()` 用 `import mineru_llama_cpp` 代替 `find_spec`）。

    原先为什么漏掉：`_forbid_probes()` 把本函数换掉了，没有任何用例执行过它的函数体；
    而且在本 venv（引擎模块没装）两种写法**恰好同判 False** ⇒ 即便补一条「调一下、断言 False」
    也只是**假等价**。故这里钉的是「用 find_spec + 不把模块 import 进 sys.modules」这一**形态**。
    """
    seen: list[str] = []

    def _fake_find_spec(name):
        seen.append(name)
        return object()  # 非 None 即「可解析」

    monkeypatch.setattr(
        mineru, "importlib", types.SimpleNamespace(util=types.SimpleNamespace(find_spec=_fake_find_spec))
    )

    ok, reason = mineru._vlm_engine_ready()

    assert seen == ["mineru_llama_cpp"], f"必须用 find_spec 探测（M8 下这里为空）：{seen}"
    assert ok is True, reason
    assert "mineru_llama_cpp" not in sys.modules, "探测不得 import 引擎模块（Windows 上会 add_dll_directory 并加载 .pyd）"


def test_vlm_route_passes_through_document_parse_error(monkeypatch, caplog):
    """击杀 **M10**（删掉 `_parse_vlm` 里的 `except DocumentParseError: raise`）。

    原先为什么漏掉：同一契约在 pipeline 路由已有用例（`test_mineru_pipeline.py::test_document_parse_error_from_entry_is_not_double_wrapped`），
    但 VLM 路由那份**没有任何覆盖**——`test_vlm_failure_is_translated_without_local_fallback` 抛的是
    `RuntimeError`（单层包裹就算通过）。M10 下真因会被套成「MinerU VLM 解析失败：MinerU VLM 解析失败：…」。

    **D2 改口径（2026-09-19）**：降级重试**不走 `_parse_vlm`**，所以最终抛出的那句来自重试，
    照它看不出首次（VLM 档）被包了几层；故 M10 的击杀点改钉在**日志**上——首次失败原因必须
    原样出现，且不许出现「MinerU VLM 解析失败」这个二次包裹前缀。异常口径同时保留：
    已可读的中文异常原样放行，不再包一层。
    """
    monkeypatch.setattr(settings, "MINERU_BACKEND", "vlm")
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", True)
    _enable_mineru(monkeypatch)
    _stub_vlm_ready(monkeypatch)
    _stub_vlm_config(monkeypatch)

    def _boom(_data, vlm_config=None, tier_override=None):
        raise DocumentParseError("原始诊断：模型权重没下")

    monkeypatch.setattr(mineru, "_parse_pdf_entrypoints", _boom)
    mineru._reset_backend_cache()

    with caplog.at_level(logging.WARNING, logger="app.pipeline.mineru"):
        with pytest.raises(DocumentParseError) as excinfo:
            mineru.parse_pdf(b"%PDF-1.4 payload")

    assert str(excinfo.value) == "原始诊断：模型权重没下", "已可读的中文异常不该被再包一层"
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "原始诊断：模型权重没下" in logged, f"原失败原因必须落进日志：{logged}"
    assert "MinerU VLM 解析失败" not in logged, f"M10：真因被二次包裹了：{logged}"


def test_config_change_invalidates_cache_without_manual_reset(monkeypatch):
    """击杀 **M11**（`_cached()` 无视配置指纹恒命中）。

    原先为什么漏掉：每个用例的 autouse 夹具都会 `_reset_backend_cache()`，连 tier 映射那条循环改 11 次
    配置的用例也在末尾显式重置 ⇒ **「配置变 → 自动重探」这条通道从未被走过**。
    这里刻意**不调**重置钩子，只改配置。
    """
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    monkeypatch.setattr(settings, "MINERU_BACKEND", "auto")
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", False)
    mineru._reset_backend_cache()
    assert mineru._select_backend() == "pipeline"

    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", True)  # 只改配置，**不**调重置钩子
    _stub_vlm_ready(monkeypatch)

    assert mineru._select_backend() == "vlm", "配置指纹变了必须重算，不许返回缓存里的旧值"


def test_available_summary_follows_real_route_and_reports_cuda(monkeypatch):
    """击杀 **M14**（`available()` 里 backend 常量化 `"pipeline"`）与 **M18**（vlm 分支报 `device=cpu`）。

    原先为什么漏掉：摘要形态用例只覆盖 pipeline 路由 × `MINERU_DEVICE`；
    真机那条用例又把 `available` **整个 stub 成字面量** ⇒ 「摘要里那个 backend 真的来自路由」这条连线是盲区。
    """
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    monkeypatch.setattr(settings, "MINERU_BACKEND", "auto")
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", True)
    monkeypatch.setitem(sys.modules, "mineru", types.ModuleType("mineru"))
    _stub_vlm_ready(monkeypatch)
    mineru._reset_backend_cache()

    assert mineru.available() == (True, "MinerU 可用，backend=vlm, device=cuda")


def test_weights_path_that_is_a_directory_is_not_ready(monkeypatch, tmp_path):
    """击杀 **M21**（`_vlm_weights_ready()` 的 `is_file()` → `exists()`）。

    原先为什么漏掉：既有用例的输入是「文件在 / 文件缺」，两版**完全同判**（仍是假等价）。
    这里用「同名**目录**冒充权重文件」逼出差异：目录存在但不是权重，必须判未就绪。
    """
    monkeypatch.setattr(mineru, "_vlm_weight_files", lambda: [tmp_path / "model.gguf", tmp_path / "mmproj.gguf"])
    (tmp_path / "model.gguf").mkdir()  # 目录冒充文件
    (tmp_path / "mmproj.gguf").write_bytes(b"stub")

    ok, reason = mineru._vlm_weights_ready()

    assert ok is False, "同名目录不是权重文件，必须判未就绪"
    assert "model.gguf" in reason and "缺失" in reason, reason


# ---------------------------------------------------------------------------
# 独立复核（对抗性复核 A）后的补强用例
#
# A 报告（`证据/S4-独立复核-A.md`）给 R1/R3 与假覆盖 N1/N2/N3/N8 都留了「删哪行仍绿」的反例。
# 下面 8 条把其中最值钱的几条钉死；R2（运行期失败不降档到 pipeline）按规格口径处理——
# 见 `S4-验收报告.md` §十 的处置说明（规格 3 与「已核事实 4」冲突，取后者：本层不复制回落）。
# ---------------------------------------------------------------------------
def test_not_ready_verdict_expires_so_late_provisioning_takes_effect(monkeypatch):
    """击杀 R3 的「永久否定缓存」：否定结论必须带 TTL —— 用户进程运行期补齐 VLM 件后要能自愈。

    实现用 `time.monotonic()` 记时刻，这里把时钟拨快超过 `_NOT_READY_TTL_SECONDS` 来验证到期重探
    （**不重启、不调重置钩子**，模拟「服务器一直在跑，运维刚把权重拷进来」）。
    """
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    monkeypatch.setattr(settings, "MINERU_BACKEND", "auto")
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", True)
    clock = {"t": 1000.0}
    monkeypatch.setattr(mineru.time, "monotonic", lambda: clock["t"])
    _stub_vlm_ready(monkeypatch, ready=False, reason="stub：权重还没拷进来")
    mineru._reset_backend_cache()

    assert mineru._select_backend() == "pipeline"

    # 运维在进程运行期间补齐了三件套（不重启）
    _stub_vlm_ready(monkeypatch, ready=True, reason="stub：补齐后就绪")
    assert mineru._select_backend() == "pipeline", "TTL 内不该重探（否则每个扫描件都要跑一次 nvidia-smi）"

    clock["t"] += mineru._NOT_READY_TTL_SECONDS + 1
    assert mineru._select_backend() == "vlm", "TTL 到期必须重探，否则自备件永远不生效且无提示"


def test_not_ready_logs_warning_with_self_provisioning_hint(monkeypatch, caplog):
    """击杀 R3/N3：未就绪必须打 **warning**（规格 3 的字面要求）并带上「怎么自备」的提示。

    原先为什么漏掉：全仓 `caplog` 命中 0 ⇒ 所有新增日志删掉也全绿；而这条 warning 是用户唯一的自查线索。
    """
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    monkeypatch.setattr(settings, "MINERU_BACKEND", "auto")
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", True)
    _stub_vlm_ready(monkeypatch, ready=False, reason="stub：权重缺失 mmproj.gguf")
    mineru._reset_backend_cache()

    with caplog.at_level(logging.WARNING, logger="app.pipeline.mineru"):
        assert mineru._select_backend() == "pipeline"

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "VLM 未就绪" in text, text
    assert "MINERU_VLM_ENABLE=True" in text and "mineru-llama-cpp" in text, text


def test_explicit_mineru_vlm_tier_warns_when_switch_off(monkeypatch, caplog):
    """击杀 R1：显式填 MinerU 的 VLM 档位（standard/advanced）能绕开开关，至少要**可见**。

    档位仍**原样透传**（尊重显式选择、不偷偷降级），但必须 warning 说明「该档会构造 VLM 预测器、
    缺件会硬失败」，否则用户会拿到一个零线索的「扫描件解析不出文字」。
    """
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    monkeypatch.setattr(settings, "MINERU_BACKEND", "standard")
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", False)
    mineru._reset_backend_cache()

    with caplog.at_level(logging.WARNING, logger="app.pipeline.mineru"):
        assert mineru._select_backend() == "pipeline"

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "VLM 档位" in text and "standard" in text, text
    assert mineru._mineru_tier() == "standard", "显式档位必须原样透传（不降级）"


def test_vlm_weight_files_uses_upstream_registry_and_local_dir(monkeypatch, tmp_path):
    """击杀假覆盖 **N1**：`_vlm_weight_files()` 必须真的用上游登记 + `local_dir()` 拼路径。

    为什么单列：把上游导入写成不存在的名字、或漏掉 `local_dir()` 的**调用括号**，其余用例
    （含既有的 51 + 32 条）**照样全绿**，而生产上 VLM 会永久不生效——这条连线是唯一保护。
    这里注入最小假 `mineru.model.registry`，只钉「用哪个 API、怎么组合」，不依赖真装 MinerU。
    """
    seen: dict = {}

    class _Repo:
        paths = {"main": "m.gguf", "mmproj": "mm.gguf"}

        def local_dir(self):
            seen["local_dir_called"] = True
            return tmp_path

    def _vlm_model_repo(engine=None):
        seen["engine"] = engine
        return _Repo()

    registry = types.ModuleType("mineru.model.registry")
    registry.vlm_model_repo = _vlm_model_repo
    model_pkg = types.ModuleType("mineru.model")
    model_pkg.__path__ = []
    mineru_pkg = types.ModuleType("mineru")
    mineru_pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "mineru", mineru_pkg)
    monkeypatch.setitem(sys.modules, "mineru.model", model_pkg)
    monkeypatch.setitem(sys.modules, "mineru.model.registry", registry)

    files = mineru._vlm_weight_files()

    assert seen.get("engine") == "llama-cpp", seen
    assert seen.get("local_dir_called") is True, "必须调用 local_dir()；漏调用括号 ⇒ 生产上永久未就绪"
    assert [path.name for path in files] == ["m.gguf", "mm.gguf"], files
    assert all(str(path).startswith(str(tmp_path)) for path in files), files


def test_image_entrypoints_forwards_vlm_config(monkeypatch):
    """击杀假覆盖 **N2**：图片链的 `vlm_config` 透传口子必须真的透传（将来接图片 VLM 靠它）。"""
    seen: dict = {}

    def _parse(path, **kwargs):
        seen["kwargs"] = kwargs
        return "图片入口产出"

    pkg = types.ModuleType("mineru")
    pkg.__path__ = []
    sub = types.ModuleType("mineru.parser")
    sub.parse = _parse
    pkg.parser = sub
    monkeypatch.setitem(sys.modules, "mineru", pkg)
    monkeypatch.setitem(sys.modules, "mineru.parser", sub)

    sentinel = object()
    text, _pages = mineru._image_entrypoints(b"fake-image", "input.png", None, vlm_config=sentinel)

    assert text == "图片入口产出"
    assert seen["kwargs"].get("vlm_config") is sentinel, "透传口子必须把 vlm_config 交给门面"


def test_vlm_ready_is_not_ready_when_any_single_check_fails(monkeypatch):
    """击杀假覆盖 **N8**：三件套是「缺**任一**即未就绪」，每一件单独失败都必须判未就绪。

    只测「全真 / 全假」两端是不够的：把聚合改成「缺两件以上才算未就绪」也能全绿。
    """
    ok_probe = lambda: (True, "stub ok")  # noqa: E731 —— 测试内联小函数，可读性优先
    bad_probe = lambda: (False, "stub bad")  # noqa: E731

    for failing in ("gpu", "weights", "engine"):
        mineru._reset_backend_cache()  # 探测被换掉但配置指纹没变 ⇒ 必须显式重置
        monkeypatch.setattr(mineru, "_gpu_available", bad_probe if failing == "gpu" else ok_probe)
        monkeypatch.setattr(mineru, "_vlm_weights_ready", bad_probe if failing == "weights" else ok_probe)
        monkeypatch.setattr(mineru, "_vlm_engine_ready", bad_probe if failing == "engine" else ok_probe)

        ok, reason = mineru._vlm_ready()

        assert ok is False, f"只有 {failing} 失败时也必须判未就绪"
        assert "stub bad" in reason, reason


def test_probe_exception_degrades_to_not_ready(monkeypatch):
    """击杀 R8：探测本身抛异常 → 判「未就绪」，**不许把裸异常抛出去**（不变量 3）。"""
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)

    def _boom():
        raise RuntimeError("探测炸了")

    monkeypatch.setattr(mineru, "_gpu_available", _boom)
    monkeypatch.setattr(mineru, "_vlm_weights_ready", lambda: (True, "stub ok"))
    monkeypatch.setattr(mineru, "_vlm_engine_ready", lambda: (True, "stub ok"))
    mineru._reset_backend_cache()

    ok, reason = mineru._vlm_ready()

    assert ok is False and "探测异常" in reason, reason


def test_available_degrades_when_backend_probe_explodes(monkeypatch):
    """击杀 R8 的对外那一半：`available()` 被 `parsing` **裸调** ⇒ 路由探测炸了也只能记 pipeline，不许抛。"""
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    monkeypatch.setitem(sys.modules, "mineru", types.ModuleType("mineru"))

    def _boom():
        raise RuntimeError("路由炸了")

    monkeypatch.setattr(mineru, "_select_backend", _boom)

    ok, reason = mineru.available()

    assert ok is True, reason
    assert reason.endswith("backend=pipeline, device=auto"), reason


# ---------------------------------------------------------------------------
# D2 · VLM 失败逐级降级（`standard` / `advanced` 失败或空结果 ⇒ 重试一次 `basic`）
#
# 验收口径（已冻结）：首次**尊重**指定档位；失败或返回空文本后，**只有**本次实际档位 ∈
# {`standard`, `advanced`} 才降级重试**一次** `basic`；两次都失败仍走既有 `DocumentParseError`
# 口径（pypdf 回落是**上层** `parsing.parse_document_pages` 的事）。`basic` / `flash` / 未知档位不重试。
#
# ⚠️ 本条目最容易写错的地方：显式 `standard` / `advanced` 时**路由是 `pipeline`**
# （`_resolve_backend` 对非 auto/vlm 一律返回 pipeline），只有 `_mineru_tier()` 把档位原样透传。
# 故「只在 VLM 分支加重试」会**漏掉 standard/advanced** —— 下面 ⑤ 的四路由参数化就钉这一点。
#
# 纪律同全文件：全部 stub 化（不连真 MinerU、不下权重、不跑 nvidia-smi）。
# ---------------------------------------------------------------------------
def test_downgrade_succeeds_and_logs_original_reason_and_tier(monkeypatch, caplog):
    """① 降级成功：`standard` 档失败 ⇒ 重试一次 `basic` 成功；日志含**原失败原因 + 降级档位**。"""
    _enable_vlm_route(monkeypatch)  # auto/vlm + 开关开 + 就绪 → VLM 路由 → 档位 standard
    calls = _scripted_entrypoints(
        monkeypatch, [RuntimeError("llama.cpp 加载失败（stub）"), ("basic 档的产出", ["basic 档的产出"])]
    )

    with caplog.at_level(logging.INFO, logger="app.pipeline.mineru"):
        text, pages = mineru.parse_pdf(b"%PDF-1.4 payload")

    assert (text, pages) == ("basic 档的产出", ["basic 档的产出"])
    assert len(calls) == 2, f"应恰好重试一次，实际调用 {len(calls)} 次"
    assert calls[0]["vlm_config"] is not None, "首次是 VLM 路由（带 llama-cpp 配置）"
    assert calls[1]["tier_override"] == "basic", "重试必须显式用 basic（不经 _mineru_tier 分档）"
    assert calls[1]["vlm_config"] is None, "重试不得携带 VLM 配置（硬边界）"

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "llama.cpp 加载失败（stub）" in logged, f"原失败原因必须落进日志：{logged}"
    assert "standard" in logged and "basic" in logged, f"降级档位必须落进日志：{logged}"


def test_downgrade_both_attempts_fail_still_raises_document_parse_error(monkeypatch):
    """② 两次均失败 ⇒ 仍抛既有 `DocumentParseError`（**不炸穿**），上层的 pypdf 兜底照旧可用。"""
    _enable_vlm_route(monkeypatch)
    calls = _scripted_entrypoints(
        monkeypatch,
        [
            DocumentParseError("MinerU VLM 解析失败：显存不足（stub）"),
            RuntimeError("basic 档也炸了（stub）"),
        ],
    )

    with pytest.raises(DocumentParseError) as excinfo:
        mineru.parse_pdf(b"%PDF-1.4 payload")

    message = str(excinfo.value)
    assert message.startswith("MinerU 解析 PDF 失败："), message
    assert "basic 档也炸了" in message, "重试的真因要带出来"
    assert len(calls) == 2, "降级只重试一次"
    # 「失败 → 走既有兜底」的另一半（上层 parsing 真的回落 pypdf）由保留用例
    # `test_vlm_failure_falls_back_to_pypdf_at_upper_layer` 覆盖，这里钉的是本层的异常口径。


def test_empty_result_triggers_downgrade_retry(monkeypatch):
    """③ 空结果触发重试：显式 `standard` 档首次返回空 ⇒ 同样降级，重试拿到文本即成功。

    这里把桩打在**真门面**（`mineru.parser.parse`）上，故记录到的是解析器**实际收到**的档位：
    首次仍是用户指定的 `standard`（尊重指定档位），第二次才是 `basic`。
    """
    monkeypatch.setattr(settings, "MINERU_BACKEND", "standard")
    _enable_mineru(monkeypatch)
    seen: list[dict] = []

    def _parse(_path, **kwargs):
        seen.append(kwargs)
        return "" if len(seen) == 1 else "重试档的产出"  # 首次空结果 = 失败

    _inject_mineru_parser(monkeypatch, parse=_parse)

    text, pages = mineru.parse_pdf(b"%PDF-1.4 payload")

    assert (text, pages) == ("重试档的产出", ["重试档的产出"])
    assert [kwargs.get("tier") for kwargs in seen] == ["standard", "basic"], seen


def test_downgrade_retries_exactly_once(monkeypatch):
    """④ 重试上限 = **恰好一次**：两次都失败也不会有第三次尝试（桩没牌可发就会炸出来）。"""
    monkeypatch.setattr(settings, "MINERU_BACKEND", "advanced")
    _enable_mineru(monkeypatch)
    calls = _scripted_entrypoints(monkeypatch, [RuntimeError("一炸（stub）"), RuntimeError("二炸（stub）")])

    with pytest.raises(DocumentParseError):
        mineru.parse_pdf(b"%PDF-1.4 payload")

    assert len(calls) == 2, f"重试上限为 1 次，实际调用 {len(calls)} 次"


@pytest.mark.parametrize(
    "backend, vlm_enable, route, first_tier",
    (
        ("auto", True, "vlm", "standard"),  # auto + 就绪三件套成立 → 选中 VLM → standard
        ("vlm", True, "vlm", "standard"),  # 显式 vlm（开关开 + 就绪）→ standard
        ("standard", False, "pipeline", "standard"),  # 显式档位：**路由是 pipeline**，档位原样透传
        ("advanced", False, "pipeline", "advanced"),  # 同上（最难想到的一条）
    ),
)
def test_all_four_routes_downgrade_to_basic_once(monkeypatch, backend, vlm_enable, route, first_tier):
    """⑤ 四种路由失败后**都真的各重试了一次 basic**。

    后两种（显式 `standard` / `advanced`）走的是 **pipeline 路由**——「只在 VLM 分支加重试」
    会精确漏掉它们，这正是本条目必须避免的错误实现。故这里连**路由**本身也断言上。

    桩打在真门面上（`mineru.parser.parse`）⇒ 记录到的是解析器**实际收到**的档位与 VLM 配置：
    `[first_tier, "basic"]`，且重试那次不许带 `vlm_config`。
    """
    monkeypatch.setattr(settings, "MINERU_BACKEND", backend)
    monkeypatch.setattr(settings, "MINERU_VLM_ENABLE", vlm_enable)
    _enable_mineru(monkeypatch)
    _stub_vlm_ready(monkeypatch)
    _stub_vlm_config(monkeypatch)

    assert mineru._select_backend() == route
    assert mineru._mineru_tier() == first_tier, f"{backend!r} 的实际档位应是 {first_tier!r}"

    seen: list[dict] = []

    def _parse(_path, **kwargs):
        seen.append(kwargs)
        if len(seen) == 1:
            raise RuntimeError("首次失败（stub）")
        return "重试成功"

    _inject_mineru_parser(monkeypatch, parse=_parse)

    text, pages = mineru.parse_pdf(b"%PDF-1.4 payload")

    assert (text, pages) == ("重试成功", ["重试成功"]), f"{backend!r} 失败后必须降级重试一次 basic"
    assert [kwargs.get("tier") for kwargs in seen] == [first_tier, "basic"], seen
    if route == "vlm":
        assert "vlm_config" in seen[0], "VLM 路由首次必须带 VLM 配置"
    else:
        assert "vlm_config" not in seen[0], "pipeline 路由首次不得带 VLM 配置"
    assert "vlm_config" not in seen[1], "重试不得携带 VLM 配置（硬边界）"


def test_downgrade_delivers_basic_to_facade_entry(monkeypatch):
    """⑥ 主入口**实际收到** basic：假 `mineru.parser.parse` 记录实参，首次失败、重试成功。"""
    monkeypatch.setattr(settings, "MINERU_BACKEND", "standard")
    _enable_mineru(monkeypatch)
    seen: list[dict] = []

    def _parse(_path, **kwargs):
        seen.append(kwargs)
        if len(seen) == 1:  # 首次（standard 档）失败，降级那次成功
            raise RuntimeError("门面首次失败（stub）")
        return "门面重试成功"

    _inject_mineru_parser(monkeypatch, parse=_parse)

    text, pages = mineru.parse_pdf(b"%PDF-1.4 payload")

    assert (text, pages) == ("门面重试成功", ["门面重试成功"])
    assert [kwargs.get("tier") for kwargs in seen] == ["standard", "basic"], seen
    assert all("vlm_config" not in kwargs for kwargs in seen), seen


def test_downgrade_delivers_basic_to_instance_entry(monkeypatch):
    """⑥ 备用入口**实际收到** basic：门面缺席（旧版形态）时走 `MinerUParser`，构造实参必须带档位。

    「不补则降级是假的」：入口 ② 原先**根本不传 tier**，吃 MinerU 硬编码默认 `standard`（= VLM 档），
    于是外面把档位定成 basic、里面照样按 VLM 档构造预测器。
    """
    monkeypatch.setattr(settings, "MINERU_BACKEND", "advanced")
    _enable_mineru(monkeypatch)
    seen: list[dict] = []

    class _Parser:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        def parse(self, _path):
            if len(seen) == 1:  # 首次（advanced 档）失败，降级那次成功
                raise RuntimeError("备用入口首次失败（stub）")
            return "备用入口重试成功"

    _inject_mineru_parser(monkeypatch, parser_cls=_Parser)  # 没有 mineru.parser.parse ⇒ 走退路

    text, pages = mineru.parse_pdf(b"%PDF-1.4 payload")

    assert (text, pages) == ("备用入口重试成功", ["备用入口重试成功"])
    assert [kwargs.get("tier") for kwargs in seen] == ["advanced", "basic"], seen
    assert all("vlm_config" not in kwargs for kwargs in seen), seen


@pytest.mark.parametrize("backend", ("basic", "flash"))
def test_basic_and_flash_tiers_do_not_retry(monkeypatch, backend):
    """⑦ `basic`（已是最省的 ONNX-only 档）与 `flash`（用户显式选择）**都不重试**。

    桩打在真门面上，故 `seen` 只有一项本身就证明「只解析了一次」，且用的正是用户指定的档位。
    """
    monkeypatch.setattr(settings, "MINERU_BACKEND", backend)
    _enable_mineru(monkeypatch)
    seen: list[dict] = []

    def _parse(_path, **kwargs):
        seen.append(kwargs)
        raise RuntimeError("一次就炸（stub）")

    _inject_mineru_parser(monkeypatch, parse=_parse)

    with pytest.raises(DocumentParseError) as excinfo:
        mineru.parse_pdf(b"%PDF-1.4 payload")

    assert "一次就炸（stub）" in str(excinfo.value)
    assert [kwargs.get("tier") for kwargs in seen] == [backend], f"{backend} 档不许重试，实际 {seen}"


def test_unknown_tier_does_not_retry(monkeypatch):
    """⑦ 的第三行：**未知 / 无法确定的档位不猜**，走既有错误处理（不降级、不重试）。"""
    _enable_mineru(monkeypatch)
    monkeypatch.setattr(mineru, "_mineru_tier", lambda *_args: "some-unknown-tier")
    calls = _scripted_entrypoints(monkeypatch, [RuntimeError("一次就炸（stub）"), "不该被用到"])

    with pytest.raises(DocumentParseError):
        mineru.parse_pdf(b"%PDF-1.4 payload")

    assert len(calls) == 1, f"未知档位不许重试，实际调用 {len(calls)} 次"


def test_tier_lookup_failure_does_not_escape_as_raw_exception(monkeypatch):
    """⑦ 的第三行的另一面：档位判定**自己**炸了 ⇒ 判「档位未知」，既不降级也不抛裸异常。

    档位判定在 D2 里被提到了 `try` 之外（「发起解析之前先记下来」），若不兜住，它就成了
    `parse_pdf` 上唯一一条能把**裸异常**抛给上层的路（不变量 3：绝不炸穿调用链）。
    """
    _enable_mineru(monkeypatch)

    def _boom(*_args):
        raise RuntimeError("档位判定炸了（stub）")

    monkeypatch.setattr(mineru, "_mineru_tier", _boom)
    calls = _scripted_entrypoints(monkeypatch, [RuntimeError("一次就炸（stub）"), "不该被用到"])

    with pytest.raises(DocumentParseError) as excinfo:
        mineru.parse_pdf(b"%PDF-1.4 payload")

    assert str(excinfo.value).startswith("MinerU 解析 PDF 失败："), str(excinfo.value)
    assert len(calls) == 1, f"档位未知 ⇒ 不降级，实际调用 {len(calls)} 次"
