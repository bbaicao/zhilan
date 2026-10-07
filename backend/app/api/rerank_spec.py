"""重排序服务商规格表（界面优化条目 28；阶段 4 · 条目 `4.5.3` 移植到 v2）。

把「重排序」从 `.env` 里的一行裸开关变成设置页可发现、可开关的产品能力：
型号候选、开启条件、代价、缺失依赖的原因全部在这里登记，界面照这张表渲染，
后端接口也照它校验 —— 一处真相，界面不自己编文案。

**为什么放 `app/api/` 而不是 `app/retrieval/`**（照开工指令包 §3 的原话）：阶段 3 已验收的
`app/retrieval/**` 不在本条目的改动白名单内；而这张表只服务**设置端点**（`GET.rerank.providers[]`
与 `PUT.rerank` 的四项校验），检索侧不读它（检索侧只读 `MODEL_ROLES.rerank` 绑定）。
`app/api/rerank_spec.py` 与 `app/api/settings_schemas.py` 同层，`api → core`，不引入反向依赖。

两条路（各自的依赖与代价完全不同，不要互相套用）：

- ``dashscope``：阿里云百炼托管重排序接口，与 embedding 同一套鉴权，
  通常**直接沿用「向量模型」那条供应商的地址与 Key**，不需要额外依赖；
- ``bge``：本地 bge-reranker（CrossEncoder），不产生调用费用、片段不出本机，
  但需要 ``sentence-transformers``。**⚠️ v2 侧这一条恒不可用**（见 `dependency_status`）。

`summary` / `requirement` / `cost` / `note` / `source` 五个说明性字段**逐字沿用老基线**
`<legacy-repo>\\backend\\app\\rag\\rerank_spec.py`（它们是界面上给用户看的「开启条件与代价」原文，
`RerankDialog.tsx:236-248` 直接渲染，改一个字就是界面文案漂移）。
"""

from __future__ import annotations

from dataclasses import dataclass

# 适配器 kind（老基线的工厂按它选 DashScopeReranker / BgeReranker）。v2 只保留登记口径：
# `bge` 那条在 v2 **没有实现**（见 ACTIVITY 说明），但条目仍留在表里——删了它界面会少一个服务商，
# 属契约面变化（老基线口径，见开工指令包 §4.4 的 ⚠️）。
KIND_HOSTED = "dashscope-rerank"
KIND_LOCAL = "bge-rerank"

#: 规范 id（`rerank.provider` 的合法取值；`PUT.rerank` 的 R-1 校验与 400 文案都用它）。
DASHSCOPE_ID = "dashscope"
BGE_ID = "bge"

#: `bge` 在 v2 恒不可用的原因（**逐字给定**，见开工指令包 §4.4）：
#: v2 没有任何本地重排实现 —— `app/retrieval/rerank.py` 只有 HTTP 调用一条路，不依赖
#: `sentence-transformers`。老基线靠本地 CrossEncoder，那是老仓库的能力，不是 v2 的。
BGE_UNAVAILABLE_HINT = (
    "本版本没有内置本地重排序实现（不依赖 sentence-transformers），"
    "请改用「阿里云百炼 · 托管重排序」。"
)


@dataclass(frozen=True)
class RerankProvider:
    """一条重排序服务商（或一套本地模型栈）。"""

    id: str
    label: str
    kind: str
    aliases: tuple[str, ...] = ()
    summary: str = ""
    models: tuple[str, ...] = ()  # 常用型号（界面给候选，仍可手打）
    default_model: str = ""
    needs_key: bool = True
    base_url_hint: str = ""
    requirement: str = ""  # 开启条件（人话）
    dependency: str = ""  # 需要额外安装的 Python 包（空 = 无）
    dependency_hint: str = ""  # 缺依赖时的可读原因
    cost: str = ""  # 代价说明
    note: str = ""
    source: str = ""


RERANK_PROVIDERS: tuple[RerankProvider, ...] = (
    RerankProvider(
        id=DASHSCOPE_ID,
        label="阿里云百炼 · 托管重排序",
        kind=KIND_HOSTED,
        aliases=("dashscope", "bailian", "aliyun", "qwen"),
        summary="调用百炼的托管 text-rerank 接口，不用在本机下载模型。",
        models=("qwen3.7-text-rerank", "gte-rerank-v2"),
        default_model="qwen3.7-text-rerank",
        needs_key=True,
        base_url_hint="https://dashscope.aliyuncs.com/compatible-mode/v1",
        requirement=(
            "需要一条可用的 API Key。接口地址与 Key 都可以留空 —— 留空就沿用「向量模型」"
            "那条供应商的（百炼的重排序与向量化是同一套鉴权）。"
        ),
        cost=(
            "候选片段要发到厂商，按调用量单独计费；每条提问多一次网络往返，"
            "首字延迟通常增加几百毫秒。"
        ),
        note=(
            "开启后先做向量 + 关键词融合，再用重排序模型对候选逐个打「问题-片段」相关分，"
            "按该分数重排并套用最低相关分门槛（低于门槛的片段不再作为引用）。"
        ),
        source="https://help.aliyun.com/zh/model-studio/text-rerank-api",
    ),
    RerankProvider(
        id=BGE_ID,
        label="本地 bge-reranker（BAAI）",
        kind=KIND_LOCAL,
        aliases=("bge", "local", "bge-reranker"),
        summary="在本机用 CrossEncoder 算相关分，片段不出本机，也不产生调用费用。",
        models=("BAAI/bge-reranker-v2-m3", "BAAI/bge-reranker-base", "BAAI/bge-reranker-large"),
        default_model="BAAI/bge-reranker-v2-m3",
        needs_key=False,
        base_url_hint="",
        requirement=(
            "需要本机装好 sentence-transformers（重排序模型跑在本地，不需要 API Key）；"
            "首次使用会从 HuggingFace 下载模型权重，几百 MB 到 2 GB。"
        ),
        dependency="sentence_transformers",
        dependency_hint=BGE_UNAVAILABLE_HINT,
        cost=(
            "模型常驻内存（约 1~3 GB），首次加载要等几秒到几十秒；"
            "之后每条提问在 CPU 上算几十条候选大约 1~3 秒，有 GPU 会快很多。"
        ),
        note=(
            "适合「资料不想出本机」的场景。权重下载走 HuggingFace，本机如遇直连不通，"
            "可先设好镜像（HF_ENDPOINT）再开。"
        ),
        source="https://huggingface.co/BAAI/bge-reranker-v2-m3",
    ),
)


def provider_spec(provider: str) -> RerankProvider | None:
    """按 id 或别名查服务商；查不到返回 None（调用方给可读报错）。"""
    key = (provider or "").strip().lower()
    if not key:
        return None
    for spec in RERANK_PROVIDERS:
        if key == spec.id or key in spec.aliases:
            return spec
    return None


def normalize_provider(provider: str, fallback: str = DASHSCOPE_ID) -> str:
    """把别名收成登记表里的规范 id；空的用 fallback。"""
    spec = provider_spec(provider)
    if spec is not None:
        return spec.id
    spec = provider_spec(fallback)
    return spec.id if spec is not None else fallback


def provider_ids() -> list[str]:
    """登记表里的全部规范 id（400 文案里列给用户看）。"""
    return [spec.id for spec in RERANK_PROVIDERS]


def is_local(provider: str) -> bool:
    spec = provider_spec(provider)
    return spec is not None and spec.kind == KIND_LOCAL


def kind_of(provider: str) -> str:
    spec = provider_spec(provider)
    return spec.kind if spec is not None else KIND_HOSTED


def dependency_status(provider: str) -> tuple[bool, str]:
    """返回 ``(依赖是否就绪, 不可读原因)``；无额外依赖的服务商恒为就绪。

    ⚠️ **v2 的 ``bge`` 恒不可用（强制，不是探测结果）**：老基线用
    ``importlib.util.find_spec("sentence_transformers")`` 实测探测，而 v2 **根本没有本地重排实现**
    ——``app/retrieval/rerank.py`` 只有 HTTP 调用一条路（`_call_rerank`），装不装
    ``sentence-transformers`` 都跑不起来。若照老基线去探测，本机装过该包的机器会得到
    `dependency_ok=True` ⇒ 界面显示「可用」⇒ 用户开起来必然每条提问静默回落 RRF
    （「配了提示成功但重启回滚」那一族缺陷）。故这里**不探测**，直接判不可用，
    `dependency_detail` 给可读中文原因（前端会渲染成黄色「缺依赖」徽标）。
    """
    spec = provider_spec(provider)
    if spec is None:
        return True, ""
    if spec.id == BGE_ID:
        return False, BGE_UNAVAILABLE_HINT
    return True, ""


def dependency_hint(provider: str) -> str:
    """缺依赖时的可读原因（调用方直接把它抛给用户）。"""
    spec = provider_spec(provider)
    if spec is not None and spec.dependency_hint:
        return spec.dependency_hint
    return "重排序依赖未就绪，请到设置页的「检索增强」里检查服务商与型号。"


def provider_options() -> list[dict]:
    """给界面的服务商清单（含每条自己的依赖状态）—— ``GET.rerank.providers[]`` 的 **14 键**。

    键序与 `frontend/src/api.ts::RerankProviderInfo`（`:1013-1032`）逐字对齐；
    前端 `RerankDialog.tsx:55-56` 会直接 ``.find(...) ?? providers[0]`` ⇒ 本数组**必须非空**。
    """
    out: list[dict] = []
    for spec in RERANK_PROVIDERS:
        ok, detail = dependency_status(spec.id)
        out.append(
            {
                "id": spec.id,
                "label": spec.label,
                "kind": spec.kind,
                "summary": spec.summary,
                "models": list(spec.models),
                "default_model": spec.default_model,
                "needs_key": spec.needs_key,
                "base_url_hint": spec.base_url_hint,
                "requirement": spec.requirement,
                "cost": spec.cost,
                "note": spec.note,
                "source": spec.source,
                "dependency_ok": ok,
                "dependency_detail": detail,
            }
        )
    return out


def model_options(provider: str) -> list[str]:
    spec = provider_spec(provider)
    return list(spec.models) if spec is not None else []


#: `GET.rerank.note`（界面上的「开启条件」原文）。逐字沿用老基线。
ENABLE_CONDITION = (
    "开启条件：服务商可用（百炼要有一条可用的 API Key；本地 bge 要装好 "
    "sentence-transformers）+ 填好型号。开启后每条提问都会多一次重排序计算。"
)


__all__ = [
    "BGE_ID",
    "BGE_UNAVAILABLE_HINT",
    "DASHSCOPE_ID",
    "ENABLE_CONDITION",
    "KIND_HOSTED",
    "KIND_LOCAL",
    "RERANK_PROVIDERS",
    "RerankProvider",
    "dependency_hint",
    "dependency_status",
    "is_local",
    "kind_of",
    "model_options",
    "normalize_provider",
    "provider_ids",
    "provider_options",
    "provider_spec",
]
