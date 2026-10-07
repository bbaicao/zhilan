"""v2 settings 骨架（阶段 1 · 1.4.2）。

从老仓库 `app/core/config.py` 移植「桌面壳 / main / 模型注册表」所需骨架：
PORT、连接配置、扁平模型键、MODEL_SERVICES / MODEL_ROLES 解析、ensure_service_registry。
业务配置键（RAG / WEB / AGENT / CLEAN / CHAT_* 等）随各阶段补齐，本阶段不预埋。

另有 `migrate_legacy_rerank`：把存量 `.env` 的遗留扁平键 `RERANK_MODEL` **一次性**迁进
`MODEL_ROLES.rerank`（回归修复）——rerank 是 `ALL_ROLE_KEYS` 里的**寄存角色**，见其 docstring。

行为对齐老仓库；MYSQL_DATABASE 默认改为 v2 库名 zhilan_v2。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

try:  # python-dotenv 是 pydantic-settings 的既有依赖（`Settings` 读 .env 走的就是它），见
    # `_read_env_file_key` 为什么必须用它而不是手写解析（回归修复 对抗性复核 P2-3）。
    # 实测：dotenv 不在时 `import app.core.config` 会**直接失败**（pydantic-settings 自己就 import 它），
    # 故下面这个兜底分支实际不可达——留着只是让本模块的这行 import 不至于成为新的失败点。
    from dotenv import dotenv_values
except ImportError:  # pragma: no cover - 兜底分支不可达，见 `_read_env_file_key` docstring 实测说明
    dotenv_values = None  # type: ignore[assignment]

from app.core import env_store
from app.core.env_store import ROOT_DIR, persist_env

# 模型角色：一个角色最多绑定「一条服务 + 一个模型」
#
# `ROLE_KEYS` = **面向用户 / 前端**的三个角色：设置页的角色下拉、`for_roles` 派生、
# `parse_role_list` / `parse_model_roles` 一律**只用它**（前端冻结的 ModelRole 只有这三个，不得扩）。
ROLE_KEYS = ("chat", "embed", "vision")

# rerank（重排）不给前端选，但它是 `MODEL_ROLES` 里的**合法寄存位**（回归修复）：
# 绑定由 `app/retrieval/rerank.py` 直读 `MODEL_ROLES.rerank` 消费，配置层必须能把它
# **解析出来**并**原样写回**——否则 `dump_registry(services, settings.roles)` 一落盘
# 就把手写的 rerank 绑定整串抹掉。
RERANK_ROLE = "rerank"

# 解析 / 写回用的角色全集：`Settings.roles` 按它遍历（**寄存** rerank 绑定）；
# 其余一切对外口径（`for_roles` / `parse_role_list` / 前端 ModelRole）仍只用 `ROLE_KEYS`。
ALL_ROLE_KEYS = (*ROLE_KEYS, RERANK_ROLE)

ROLE_LABELS = {"chat": "对话", "embed": "向量", "vision": "视觉"}

# 厂商预设：只改 .env 就能换厂商，业务代码不出现厂商名
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "mimo": {
        "kind": "openai",
        "base_url": "https://api.xiaomimimo.com/v1",
        "chat_model": "mimo-v2.5",
        "vision_model": "mimo-v2.5",
    },
    "xiaomi": {
        "kind": "openai",
        "base_url": "https://api.xiaomimimo.com/v1",
        "chat_model": "mimo-v2.5",
        "vision_model": "mimo-v2.5",
    },
    "dashscope": {
        "kind": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "chat_model": "qwen-plus",
        "embed_model": "text-embedding-v4",
        "vision_model": "qwen-vl-plus",
    },
    "openai": {
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "chat_model": "gpt-4o-mini",
        "embed_model": "text-embedding-3-small",
        "vision_model": "gpt-4o-mini",
    },
    "deepseek": {
        "kind": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "chat_model": "deepseek-flash",
    },
    "kimi": {
        "kind": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "chat_model": "kimi-k2.6",
    },
    "moonshot": {
        "kind": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "chat_model": "kimi-k2.6",
    },
    "zhipu": {
        "kind": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "chat_model": "glm-4-flash",
        "embed_model": "embedding-3",
        "vision_model": "glm-4v-plus",
    },
    "glm": {
        "kind": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "chat_model": "glm-4-flash",
        "embed_model": "embedding-3",
        "vision_model": "glm-4v-plus",
    },
    "doubao": {
        "kind": "openai",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
    },
    "ollama": {
        "kind": "openai",
        "base_url": "http://127.0.0.1:11434/v1",
        "chat_model": "qwen2.5",
        "embed_model": "bge-m3",
    },
    "siliconflow": {
        "kind": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
        "embed_model": "BAAI/bge-m3",
    },
    "claude": {
        "kind": "anthropic",
        "base_url": "https://api.anthropic.com",
        "chat_model": "claude-3-5-sonnet-latest",
        "vision_model": "claude-3-5-sonnet-latest",
    },
    "anthropic": {
        "kind": "anthropic",
        "base_url": "https://api.anthropic.com",
        "chat_model": "claude-3-5-sonnet-latest",
        "vision_model": "claude-3-5-sonnet-latest",
    },
    "gemini": {
        "kind": "gemini",
        "base_url": "",
        "chat_model": "gemini-2.0-flash",
        "vision_model": "gemini-2.0-flash",
    },
    "google": {
        "kind": "gemini",
        "base_url": "",
        "chat_model": "gemini-2.0-flash",
        "vision_model": "gemini-2.0-flash",
    },
}


class ModelConfig(BaseSettings):
    kind: str
    provider: str
    base_url: str
    api_key: str
    model: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    MYSQL_HOST: str = "127.0.0.1"
    MYSQL_PORT: int = 13306
    MYSQL_USER: str = "zhilan"
    MYSQL_PASSWORD: str = "zhilan"
    # v2 库名（老仓库默认 zhilan）
    MYSQL_DATABASE: str = "zhilan_v2"

    # --- 数据库后端（阶段 3 · 条目 3.0：方言可切换）---
    # mysql = 开发态（容器 MySQL，行为与改造前逐字一致）；
    # sqlite = 桌面交付（阶段 6 Q1 决议：公开分发不可内置 MySQL，改 SQLite）。
    # DSN 由 database_dsn 按本键切换；mysql_dsn 属性保持逐字不变（开发态测试夹具依赖它）。
    DB_BACKEND: str = "mysql"
    # SQLite 数据文件；留空 → <仓库根>/data/zhilan.db（与 UPLOAD_DIR 同一锚点）
    SQLITE_PATH: str = ""
    # SQLite 写锁等待上限（毫秒）= 并发写口径的一部分：等待而非立刻失败，超时仍抛错不静默丢写
    SQLITE_BUSY_TIMEOUT_MS: int = 5000

    REDIS_HOST: str = "127.0.0.1"
    REDIS_PORT: int = 16379
    REDIS_PASSWORD: str = ""
    # ⚠️ 无读取点（回归修复，2026-09-18 实测：全仓只在本定义体出现）：
    # 填 false **不会**降级任何行为——原描述「可关 / 失败降级」从未实现，
    # 因为 Redis 功能上从未被使用过（全仓调用面仅 5 个方法，见 app/db/redis.py）。
    # 键**保留**（不动冻结键表与桌面默认 .env 模板）；桌面版由 app/db/redis.py 的
    # 进程内垫片顶替（条目 3.0），不带 Redis 服务。
    REDIS_ENABLE: bool = True

    # 阶段 0：向量库。容器 URL 优先；本地路径用于单测/无 Docker 场景
    QDRANT_URL: str = "http://127.0.0.1:16333"
    QDRANT_PATH: str = ""
    QDRANT_COLLECTION: str = "zhilan_chunks"
    EMBED_DIM: int = 1024

    # --- 意图路由（阶段 1 · 条目 1.2；命名冻结，见统筹 §3.3）---
    # regex=正则优先 + LLM 兜底；llm=直接 LLM 分类；off=不路由
    # （本阶段 off 视为库外；阶段 3+ 的「全部先检索」语义另定，见阶段 1 AGENTS 条目 1.2）
    ROUTING_MODE: str = "regex"
    # LLM 分类失败/无 Key 时的默认分路：direct=库外直答；search=按库内处理
    ROUTING_LLM_FALLBACK: str = "direct"
    # 会话事件日志开关（I9 路由决策留痕的承载）。默认开；关掉则不留痕。
    CHAT_EVENT_LOG_ENABLE: bool = True
    # 库内未命中时是否允许库外直答（false=拒答）。见阶段 1 条目 1.3/1.6。
    CHAT_DIRECT_ANSWER_ENABLE: bool = True

    # --- 推理等级 / 厂商思考参数（阶段 5 后 · 设置页 reasoning 段）---
    # 单行 JSON，形状与老基线逐字一致：{"param":"thinking.type","value":"enabled","model":"mimo-v2.5"}。
    # 三个键的分工、坏数据口径、未登记型号的「手写原样透传」通道见 `app/core/reasoning/store.py`。
    #
    # ⚠️ **只作用于最终答案那一条调用**（`answer.build_chat_model()`）：
    # 辅助路径（Agent 决策 / 澄清 / 压缩 / 记忆抽取）与笔记生成**一律不带**用户配置的
    # 推理参数——它们走 `answer.build_aux_model()`。两函数分叉自同一个私有实现，
    # 全仓**只有一处**读本键（见 `reasoning/__init__.py` 的模块 docstring）。
    # 空串（默认 / 桌面首次启动）= 与接这个键之前逐字一致（`extra_body=None`）。
    CHAT_REASONING: str = ""
    # 澄清轮（阶段 1 条目 1.4）：模糊/指代不清时是否追问；上限为追问轮数。
    QUERY_CLARIFY_ENABLE: bool = True
    QUERY_CLARIFY_MAX_ROUNDS: int = 2

    PORT: int = 8000

    MODEL_PROVIDER: str = ""
    LLM_BASE_URL: str = ""
    LLM_API_KEY: str = ""
    LLM_MODEL: str = ""

    EMBED_PROVIDER: str = ""
    EMBED_MODEL: str = ""
    EMBED_BASE_URL: str = ""
    EMBED_API_KEY: str = ""

    VISION_MODEL: str = ""

    # --- 文档接入（阶段 2 · 条目 2.1）：上传留档策略 ---
    # 上传文件留档目录（相对路径按仓库根解析）。留档是「失败可重试」的前提：
    # 解析/切块失败后能直接从留档重跑，不必让用户重新上传同一个文件。
    # 文件夹导入的文件**不复制**（它本来就在磁盘上），只记绝对路径。
    UPLOAD_DIR: str = "data/uploads"

    # --- 工作区根（专项工具升级-3 · 阶段 3 · 回归检查）---
    # 只读文件工具（`list_files` / `read_file` / `grep_files`）的**边界根**：
    # 所有相对路径一律相对它解析（**绝不**取进程 CWD —— 红线 1），越界由
    # `agent/tools/_paths.py::classify()` 判 `Reject`。
    # ⚠️ **默认空串**（单⑨ (c)「不设默认」）⇒ `resolve_workspace_root()` 返 `None`
    # ⇒ `bootstrap.build_registry()` 的注册门**跳过三个文件工具的 register**
    # ⇒ **功能默认不可用**，与出厂「档 0」语义一致。
    # 本键**落顶层**（与 `upload` / `sqlite` / `skill` 同族路径类配置），**不进 `agent` 段**。
    WORKSPACE_ROOT: str = ""

    # --- 写通道凭证密钥（专项工具升级-3 · 阶段 5 · 回归检查）---
    # **宿主侧可校验凭证**（红线 13）的 HMAC 密钥，消费方 = `agent/tools/_confirm.py`
    # （该模块是**唯一读点**，形如 `_secret()`）。
    # ⚠️ **默认空串 ⇒ 写通道整体不可用**（fail-closed）：`verify_write_confirm()` 见空
    #    一律返 `missing` ⇒ 任何写调用被拒。与 `WORKSPACE_ROOT` 同款「不设默认」语义
    #    （单⑨ (c)），**不得**给任何默认值或占位密钥。
    # ⚠️ **本键无设置页可见面**（`D-回归检查` 定档「不上设置页」）—— 理由：①它是**实现细节**
    #    而非用户偏好；②上设置页的落盘通道是 `.env`，与「不上设置页」以外的一切可见面都
    #    要同笔改 `02` 契约 + 键集钉子 + `api.ts`（5 处同笔，撞红线 6）。⇒ **如实登记为
    #    已知限制**（与 `回归修复` 的「联网两键无用户可写/可见面」**同型**）。
    # ⚠️ **密钥落 `.env` 的安全性依赖一条隐式不变量**：`.env` 属
    #    `agent/tools/_paths.py` 的 `SENSITIVE_SEGMENTS` ⇒ **读工具拒读**（模型拿不到密钥）。
    #    ⇒ 🔴 **本阶段不得改动该清单**（动它 = 在无人察觉下使凭证机制失效）。
    # ⚠️ **本键永不入库**（铁律 3：`.env` 在 `.gitignore` 中排除）。
    WORKSPACE_CONFIRM_SECRET: str = ""

    # --- 离线管道（阶段 2 · 条目 2.2）：分块 / 清洗 / 双链扩展 ---
    RAG_CHUNK_SIZE: int = 800
    RAG_CHUNK_OVERLAP: int = 100
    # 复杂 PDF（公式 / 表格 / 多栏）走 MinerU 适配器；默认关——MinerU 是重依赖，
    # 未安装或未开启时 PDF 一律走 pypdf，行为与老仓库一致（绝不炸穿）。
    MINERU_ENABLE: bool = False
    # MinerU 后端选择：auto / pipeline / vlm（**本仓库侧的三档语义**，阶段 4 · 条目 4.1/4.2 已落地）。
    # auto = 开关（见 MINERU_VLM_ENABLE）打开且「VLM 就绪三件套」成立时用 vlm，否则回落 pipeline；
    # pipeline = CPU 友好的轻量（ONNX-only）后端；vlm = 视觉语言模型后端（进程内 llama.cpp）。
    # ⚠️ 这三档**不是** MinerU 的 tier 名：4.0 的 tier 是 flash/basic/standard/advanced，
    # 而「pipeline vs vlm」在本仓库是**路由 + 引擎选择**。
    # 档位映射（阶段 4 起）：三档按实际路由分档——vlm 路由 → `standard`，否则 → `basic`（ONNX-only、
    # 不依赖 GGUF）；若直接填 MinerU 的 tier 名则**原样透传**。见 `pipeline/mineru.py::_mineru_tier`。
    # ⚠️ 透传 `standard` / `advanced` 会绕开 MINERU_VLM_ENABLE 的保护（这两个档在 MinerU 侧必然构造
    # VLM 预测器 ⇒ 需要自备 GGUF + mineru-llama-cpp），未自备时会硬失败；该情形下代码会打 warning。
    MINERU_BACKEND: str = "auto"
    # MinerU 推理设备：auto / cpu / cuda。auto = 有 CUDA 就用 cuda，否则 cpu。
    MINERU_DEVICE: str = "auto"
    # 自定义模型权重目录；空串 = 用 MinerU 默认缓存路径（不指定即由 MinerU 自己决定，通常 ~/.mineru/models）。
    # ⚠️ 走「用户自备 VLM」这条路时**必须设**本键指向权重根（例：`E:\zhilan-models`）：它会被映射成
    # MinerU 的 `MINERU_HOME`，权重实际落在 `$MINERU_HOME/models/<repo>/`。不设 ⇒ 权重解析到
    # `~/.mineru/models` ⇒ 就绪判定失败 ⇒ `auto` 会**静默回落 pipeline**（安全但不明显）。
    MINERU_MODEL_DIR: str = ""
    # PDF 文本密度阈值（字符 / 字节）：pypdf 提取文本长度 ÷ PDF 字节数低于此值
    # 判定为扫描件，触发 OCR。原生 PDF 通常 > 0.05，纯扫描件接近 0（这就是默认 0.01 的依据）。
    # 语义是「密度 >= 阈值 → 判为原生、直接走 pypdf，不做 OCR」，故**调高**才更激进地走 OCR：
    # 设 0 = 只要提取到字就算原生（默认行为的最宽松档）；设 > 1 = 所有 PDF 都走 OCR（调试用）。
    # ⚠️ `回归修复` 起密度不再是唯一判据：**每页都有文本层**（单页有效字符 ≥
    # ``parsing._MIN_TEXT_PER_PAGE``）的原生 PDF 即使密度低于本阈值也走原生路
    # （密度分母含图片字节，图多的原生 PDF 会被稀释——样本 A 实测 0.0077 < 0.01）。
    # 本阈值继续兜「多数页有文本、少数页整页无文本」的混排形态。
    OCR_SCAN_THRESHOLD: float = 0.01
    # 图片 OCR 开关（阶段 3 · 条目 3.4）：jpg / jpeg / png / bmp / tiff / webp 六种图片
    # 走 MinerU OCR 的**图片专路**开关。语义分工：
    # - MINERU_ENABLE=False → 图片一律报「图片 OCR 未启用」（总开关没开，本键是什么都无所谓）；
    # - MINERU_ENABLE=True 而本键 False → 图片报「图片 OCR 已关闭」，PDF 的扫描件分流**不受影响**
    #   （本键只管图片这一路，PDF 走 MINERU_ENABLE + OCR_SCAN_THRESHOLD）。
    # 默认 True：总开关开时图片即可用，符合「六个格式从『不支持』变『可检索』」的阶段目标。
    OCR_IMAGE_ENABLE: bool = True
    # VLM 引擎开关（阶段 4 · 条目 4.1）：是否启用 MinerU 的**视觉语言模型**引擎（1.2B 权重）。
    # 默认 False —— 交付包**不带** VLM 件（第三方 GGUF 与 mineru-llama-cpp 都无许可文本可依附，
    # 因此默认不附带这些组件），开箱即用一律走 pipeline；
    # 未自备 VLM 件时本键保持 False，`MINERU_BACKEND=auto` 等价于 pipeline，不报错、不探测、不 import。
    # 想要 VLM 的用户**自备三件**：① 权重放进模型目录；② `pip install mineru-llama-cpp`；
    # ③ 把本键置 True（此时 auto 在「GPU 可探测 + 权重齐 + 模块在」三件套成立时选 vlm）。
    # 本键**参与**档位选择（见 `pipeline/mineru.py::_mineru_tier`）：开着且就绪 → `standard`（VLM 档）；
    # 否则 → `basic`（ONNX-only）。**但用户直接填 MinerU tier 名时本键管不到**（显式档位原样透传）。
    MINERU_VLM_ENABLE: bool = False
    # 双向链接：检索主命中后沿 [[链接]] 边多拉一跳相关片段并入上下文（降权、去重、限量）
    RAG_LINK_EXPAND: bool = True
    RAG_LINK_MAX: int = 4  # 单次提问最多并入的链接片段数
    # 检索结果是否走重排（阶段 3 · 条目 3.1）。**默认关**：关着时行为与开启前逐字一致。
    # 开启后从 MODEL_ROLES 的 rerank 角色取服务；未绑定 / 调用失败 → 记中文警告并回落 RRF 顺序
    # （不炸穿，I5）。见 app/retrieval/rerank.py。
    RAG_ENABLE_RERANK: bool = False
    # 重排门槛（回归修复 · 02 契约 §7.6 的 `GET.rerank.threshold` 语义）：重排分**低于本值**
    # 的片段不作为引用（既不进回答的引用列表，也不进上下文）。默认 0.20。
    # ⚠️ 消费方是 `app/retrieval/rerank.py`（阈值过滤在那边落地）；本文件**只声明键**，
    # config 侧不读它——别在这里加第二处判定。
    RAG_MIN_RERANK_SCORE: float = 0.20
    # 数据清洗（M3）：总开关关闭则按原文入库；分步开关默认全开
    CLEAN_ENABLE: bool = True
    CLEAN_HEADER_FOOTER: bool = True  # 跨页重复行检测为页眉页脚
    CLEAN_DUPLICATE_PARA: bool = True  # 重复段落去重与折叠
    CLEAN_CONTROL_CHAR: bool = True  # 控制符剔除、乱码标记、连续字符折叠
    CLEAN_TABLE_DOWNGRADE: bool = True  # 表格行分隔符统一
    CLEAN_MIN_REPEAT_RATIO: float = 0.6  # 行出现率 ≥ 该值（且长度达标）判为页眉页脚
    CLEAN_MAX_HEADER_LEN: int = 40  # 页眉页脚候选行的最大长度
    CLEAN_MAX_REPEAT_CHAR: int = 8  # 同一字符连续出现超过该值则折叠到该值；0 = 不折叠

    # --- 可选：仓库外部的对照锚点（阶段 7 · 条目 7.2）---
    # 可选：老仓库 backend 根目录，只有对拍/比对脚本用；为空表示未配置。
    # 仓库里**不得**写死本机绝对路径（阶段 7 纪律 2），故本机把实际路径放在**未入库的** `.env`
    # （或同名环境变量）里；消费方按「环境变量 → 本键 → 空」解析，为空时给中文提示 / 跳过：
    #   backend/scripts/compare_schema.py、backend/scripts/parity_old_pipeline.py、
    #   backend/tests/contract/conftest.py。
    OLD_REPO_BACKEND: str = ""

    # --- 长会话历史压缩（阶段 4 · 条目 4.3；键名与默认值**沿用老基线**同名键）---
    # 「只压发给模型的历史」：messages 行与消息里的 refs 完整保留，压缩结果只落
    # chats.context_summary / compressed_up_to 两列（界面据此显示「已压缩早期对话」折叠条）。
    # 消费方是 app/session/compaction.py；本文件**只声明键**，不在这里做任何判定。
    CHAT_COMPRESS_ENABLE: bool = True
    # 触发阈值：**发给模型的历史总字符数**（只数 content，不含 role；摘要在内）
    CHAT_COMPRESS_THRESHOLD: int = 8000
    # 最近保留条数（**按消息条数计**，不是轮数），这些消息永不参与压缩
    CHAT_COMPRESS_KEEP: int = 6
    # 摘要输入字符上限（0 = 不限）；连头部都装不下时放弃摘要调用（不硬塞）
    CHAT_COMPRESS_SUMMARY_INPUT_MAX: int = 32000
    # 摘要**正文**输出预算（真实发送预算 = 本值 + SUMMARY_REASONING_HEADROOM，见 compaction.py：
    # 推理型模型会把预算花在思考上，不给余量则正文恒空、压缩永远失败）
    CHAT_COMPRESS_SUMMARY_MAX_TOKENS: int = 512

    # --- Agent 编排（阶段 4 · 条目 4.1）---
    # 设置页默认的 Agent 模式：请求里的 `agent_mode` 未给（None）时用它。
    # 取值 off / react / plan_execute；非法值按 off 处理（不抛，桌面版首次启动是正常状态）。
    AGENT_MODE: str = "off"
    # Agent 轮次上限：默认 6、天花板 12。**读取时再夹一次**——
    # 消费方是 app/agent/runner.py::agent_max_rounds()，本文件只声明键。
    AGENT_MAX_ROUNDS: int = 6
    # Agent **一轮**的 wall-time 上限（专项 · 工具升级-2 · R2-03；单位**秒**，默认 0 = 不限）。
    # 0 ⇒ 不进任何计时分支、不新增任何 note（**逐字等价**改动前，判据 A4）；>0 时从「本轮
    # Agent 段开始」计时，起点在 runner 的 adiscover_tools() **之前**（D-回归检查）⇒ MCP 的
    # 逐包发现也吃同一份预算（R2-04 要治的正是「最坏 ≈ 8s × 许可包数、调用方取消不掉」）。
    # 命中后**可见收尾**（note「已达时间上限」）、不炸穿，且**不动**停止/取消语义。
    # 消费方是 app/agent/runner.py::agent_turn_deadline_seconds()，本文件只声明键。
    # ⚠️ 本键**不进** settings 端点：GET/PUT /api/settings 的 agent 段键集是**冻结**的
    #（tests/api/test_settings_contract.py:275 逐字钉 ['audit','max_rounds','mode','modes']）
    # ⇒ 要暴露得改**冻结前端** AgentDialog.tsx 并同步改写那条判据；本笔只声明 Settings 字段。
    AGENT_TURN_DEADLINE_SECONDS: int = 0
    # MCP 包清单（JSON 数组字符串："[{slug,name,transport,command,url,tools:[...]}]"）。
    # 空 = 没有 MCP 工具。解析失败按空处理（坏配置不该拖垮对话），见 app/agent/tools/mcp.py。
    MCP_SERVERS: str = ""
    # MCP 许可（第 2 批 ③ · D24）：逗号分隔归一化 slug、精确匹配、无通配。
    # MCP_ALLOW_SERVERS = 允许被 Agent 使用（发现前许可）；MCP_ALLOW_EGRESS = http 外发硬闸。
    # 缺省空 = default-deny。不进 settings 面（02 §6 冻结键表不含它们）。
    MCP_ALLOW_SERVERS: str = ""
    MCP_ALLOW_EGRESS: str = ""
    # 工具调用审计（阶段 4 · 条目 4.3；键名与默认值**沿用老基线**同名键）。
    # ⚠️ v2 的落点**不是**老基线的 JSONL 文件（`backend/agent_audit/agent-audit-YYYYMM.jsonl`），
    # 而是已有的两张表 `agent_runs` / `agent_steps`——理由见 app/agent/audit.py 模块 docstring
    # （桌面包 `backend/` 只读、C 盘零污染、避免把「这次 Agent 干了什么」劈成两个真相源）。
    # 关 = 审计零写入（`agent_runs` 也零行），行为与开启前逐字一致。
    AGENT_ENABLE_AUDIT: bool = True

    # --- 联网检索（阶段 4 · 条目 4.1；键名与默认值沿用老仓库同名键）---
    # 总开关：关闭时联网工具（web_search / fetch_page）**根本不进 Agent 清单**，
    # 决策模型看不见就不会选它——这是条目 4.1 明文要求的那条门控。
    WEB_ENABLE: bool = False
    # 渠道：api = 传统搜索 API（条目 4.1 实装）；llm = 大模型搜索（**尚未实装**，
    # 选了它工具会返回可读中文失败观察，不静默降级）。
    WEB_CHANNEL: str = "api"
    WEB_SEARCH_PROVIDER: str = "duckduckgo"  # duckduckgo（免 Key）/ serper / tavily / bocha / baidu / bing
    WEB_SEARCH_API_KEY: str = ""
    WEB_LLM_PROVIDER: str = "deepseek"
    WEB_LLM_MODEL: str = ""
    WEB_LLM_BASE_URL: str = ""
    WEB_LLM_API_KEY: str = ""
    WEB_LLM_MAX_SEARCHES: int = 5  # 引擎内夹 1..10
    WEB_LLM_SEARCH_TOOL: str = ""  # 空 = 不带 tools
    WEB_FETCH_TIMEOUT: float = 12.0  # 搜索与抓取共用的超时（秒）
    WEB_MAX_RESULTS: int = 4  # 单次搜索最多取几条
    WEB_MAX_CONTENT: int = 4000  # 单页正文并入上下文的上限（字符）
    # SSRF 逃生门（专项工具升级-2 · R2-12）：**默认关**。开启后放行私网/回环等非公网地址，
    # 且**每次放行都留一条 WARNING**（落点 app/agent/web/client.py 的 _guard_hook）。
    # ⚠️ 只走 `.env`——设置页的 `web` 段至今仍是 400（见 R2-12 单⑦登记移交件）；
    # ✅ 2026-09-27 起**只读**进 `GET /api/settings` 的 web 段（`ssrf_allow_any_host`，现读；
    #   回归修复 拍板②：机器可读、前端零消费、PUT 仍不可写——.env+重启仍是唯一开关通道）。
    WEB_SSRF_ALLOW_ANY_HOST: bool = False
    # 单次 web_search 的抓取预算（秒；**0 = 不限**，供回滚）。R2-13：起点在搜索调用之前，
    # 逐条抓取前检查剩余；超出预算的那条**不抓正文但仍进材料**（退回摘要），不计入 failed。
    WEB_SEARCH_BUDGET_SECONDS: float = 20.0

    # --- 翻译（条目 23；阶段 4 · 条目 4.5.3 补键）---
    # DeepL 翻译 Key（只留本机 `.env`，接口侧**只回 has_key**，绝不回传 Key 本体）。
    # 老仓库同名键、同默认值；`POST /api/translate` 与 `PUT /api/settings` 的 `translate` 段
    # 共用它。**为什么本条才补**：v2 此前没有翻译域，键无处可放；4.5.3 实装 `POST /api/translate`
    # 之后必须有一个可写的落点，否则「前端能填 Key 却无处存」。
    DEEPL_API_KEY: str = ""

    # --- 技能 / MCP（阶段 4 · 条目 4.5.3 补键）---
    # 本机 skill 目录（相对路径按仓库根解析，口径同 `UPLOAD_DIR`）。
    # `GET /api/skills` 的 `skill_dir` / `skill_dir_exists` 与 `PUT /api/skills` 的
    # `skill_dir` 都读它；老仓库同名键、同默认值（`skills`）。
    # ⚠️ MCP 包清单**不在这里**：它是上面已有的 `MCP_SERVERS`（4.1 落地，工具装配侧已在读）。
    # 本条只加「本机 skill 目录」这一个键，**不新造第二个 MCP 注册表**（避免两处真相）。
    SKILL_DIR: str = "skills"

    # --- 记忆（阶段 5 · 条目 5.1）---
    # 跨会话记忆总开关。**默认关 = 逐字一致 + 失败降级**：关态下不查库、不调模型、
    # 注入段恒空串（早退点唯一，见 `app/session/memory.py::_enabled`）。
    # ⚠️ 开关**只走 .env**：02 契约无 memory 端点、`settings_schemas.py` 顶层段无 memory、
    # 冻结前端 `memory` 0 命中 ⇒ 不得加设置字段（改了就是解冻前端）。
    MEMORY_ENABLE: bool = False
    # 注入段总字符预算（含固定措辞与分隔符）。超预算时按 `created_at` 倒序逐条累加、
    # **下一条会超就停**（不截断单条、不摘要、不变换排序）。
    MEMORY_INJECT_MAX_CHARS: int = 800
    # 召回条数上限：按 `created_at` 倒序取最近 N 条（首版不做语义召回，见证据的「有意差异」）。
    MEMORY_TOP_K: int = 5

    # --- 语义缓存（阶段 5 · 条目 5.2）---
    # 语义缓存总开关。**默认关 = 逐字零影响**：关态下不查表、不写表、不算键
    # （早退点唯一，见 `app/cache/store.py::enabled`，三个入口各自首行调它）。
    # ⚠️ 开关**只走 .env**：02 契约 53 端点无 cache、`settings_schemas.py` 无对应段、
    # 冻结前端 `frontend/src` 里 `cached/semantic` **0 命中** ⇒ 不得加设置字段、不得解冻前端。
    # ⚠️ 语义载体的形态是**进程内 OrderedDict 的精确键缓存**（10 个成分全等才命中），
    # **不是**向量相似检索——理由与代价对照见 `app/cache/entry.py` 的模块 docstring。
    SEMANTIC_CACHE_ENABLE: bool = False
    # 条目存活时长（秒）。**TTL 是唯一的 0–1 新鲜度旋钮**：短则偏新鲜、长则偏省钱。
    # 读时按 `created_at` **现算**（不在写入时烤 `expires_at`）⇒ 改配置对已有条目**即刻生效**。
    # `<= 0` 视为**关闭缓存**（与把 ENABLE 置 false 等价；保留它是为了让运维口径单一）。
    # 它是「命中直接给答案」这条路的第二道闸门：第一道是 `chunk_id` 存活 + 内容指纹校验
    # （硬证据），它拦的是「没能力判定的一切漂移」。
    SEMANTIC_CACHE_TTL_SECONDS: int = 86400
    # 容量上限（条）。超出即按 LRU 淘汰最旧的（读命中会刷新位置）。
    # 进程内 ⇒ 重启即空（**特性**：桌面版重启后语料可能已变）；`<= 0` 同样是关闭缓存。
    SEMANTIC_CACHE_MAX_ENTRIES: int = 256

    # --- 图谱检索（阶段 5 · 条目 5.3）---
    # 图谱检索分支总开关（**阶段文档已冻结此键名**）。**默认关 = 逐字零影响**：关态下
    # ``retrieve()`` 连 ``graph.search_graph`` 都不调（零查询、零材料、零帧键，A3/A11）。
    # 消费方是 `app/retrieval/graph.py`；`enabled()` 是**唯一**的开关读取点。
    # ⚠️ 开关**只走 .env**：02 契约 53 端点无 graph 检索面（`GET /api/graph` 是**图视图**，
    # 与本通道无关且形状冻结）、`settings_schemas.py` 无对应段、冻结前端 0 命中
    # ⇒ 不得加设置字段、不得解冻前端。
    # ⚠️ 首版是「结构信号」通道（标题 / 标签 / 分类 / 互链），**不做 LLM 抽实体**：
    # 索引期抽取 = 每个分块约 2 次模型调用（一份 100 块的文档 ≈ 200 次，用户上传时无提示），
    # 且 `tests/verify_v2.py` 的禁网矩阵会对任何出网尝试判红。理由见 graph.py 模块 docstring。
    GRAPHRAG_ENABLE: bool = False
    # 通道名额上限（条）。真实名额 = `min(本值, FUSE_LIMIT − 主命中条数)`——**主命中永远优先**，
    # 图通道是增益步骤，挤掉主命中会让 `[13]/[14]` 没有引用卡（I3 的硬约束，见 link.py）。
    GRAPHRAG_TOP_K: int = 6
    # 通道系数：一跳邻居的通道分 = 种子强度 × 跳数衰减 × **本值**。
    GRAPHRAG_SCORE_FACTOR: float = 0.5
    # 一跳衰减：种子自身取种子强度（不降权），邻居乘本值。
    GRAPHRAG_HOP_DECAY: float = 0.5
    # 弱别名（tags / category）命中的种子强度；**强别名（文件名 / 标题）恒 1.0**。
    GRAPHRAG_WEAK_ALIAS_WEIGHT: float = 0.5
    # 每轮参与身份匹配的 token 上限，同时是**有界 SQL** 的规模系数
    # （候选行上限 = 本值 × 4，见 graph.py::CANDIDATE_LIMIT_FACTOR）。夹到 ≥1。
    GRAPHRAG_MAX_TERMS: int = 8

    # --- 多服务配置（设置页「API Key 服务」）---
    # MODEL_SERVICES：服务注册表，单行 JSON 数组，元素为
    #   {id, name, provider, base_url, api_key, models: [...],
    #    model_roles: {型号: ["chat", ...]}}
    # MODEL_ROLES：角色绑定，单行 JSON 对象，形如
    #   {"chat": {"service_id": "...", "model": "..."}, ...}
    # 两条都非空时以注册表为准；为空则回落到上面的扁平键（旧 .env 仍可直接用）。
    MODEL_SERVICES: str = ""
    MODEL_ROLES: str = ""

    @property
    def upload_path(self) -> Path:
        """上传留档目录的绝对路径；相对路径按仓库根解析（与 ``.env`` 同一锚点）。"""
        raw = (self.UPLOAD_DIR or "").strip().strip('"').strip("'")
        path = Path(raw or "data/uploads").expanduser()
        return path if path.is_absolute() else ROOT_DIR / path

    @property
    def workspace_backup_dir(self) -> Path:
        """工作区**写前快照**目录的绝对路径（专项工具升级-3 · 阶段 5 · `回归检查`）。

        形状**照抄** :attr:`upload_path`（同型解析：去引号 → `expanduser` → 相对路径锚
        `ROOT_DIR`）⇒ **不新增锚点类**（红线 14）。

        ## 三条口径

        - **零迁移**：`data/` 下的普通目录，**不建表、不产生 alembic 迁移**；
        - 🔴 **不假定 `data/` 已存在**（`§0.2` B 组实测：`data/` 下**只有 `uploads/`**）
          ⇒ 调用方（`_write._take_snapshot`）用 `mkdir(parents=True, exist_ok=True)` **自建**；
        - **它是【属性锚点】不是硬编码常量** —— 测试用 `monkeypatch.setattr(type(settings),
          "workspace_backup_dir", property(...))` 把它挂到 `tmp_path`（**红线 12**：
          `回归检查` 的载体**不得**把快照写进仓库内的 `data/`）。

        ## ⚠️ 一条如实登记

        **默认不自动清理**（单⑩ 取 (a)「单文件原子」、未取 (c)「保留 N 份」）
        ⇒ **本目录会无限增长**，须在阶段小结件逐字登记；
        **不得**声称「备份策略已完整」，也**不得**声称「可一键还原」（**备份 ≠ 回滚**，红线 9）。
        """
        return ROOT_DIR / "data" / "workspace-backups"

    @property
    def workspace_root(self) -> str:
        """工作区根的**原始配置字符串**（专项工具升级-3 · 阶段 3 · 回归检查）。

        ⚠️ 本属性**只回原始字符串**，**不做**归一 / 展开 / 绝对化 —— 归一那一段
        **唯一**落在 `agent/tools/_paths.py::resolve_workspace_root()`（阶段 1 已验收的
        边界层，`K-回归检查`：只消费、不修改；本阶段只把 `_read_configured_root()` 那一处
        接上本键）。**两份实现会产生两套语义**（词法拒绝 / `resolve()` / 相对路径锚点），
        故此处**刻意**保持「只是配置值的读点」这一形状。

        ```python
        raw = (settings.WORKSPACE_ROOT or "").strip().strip('"').strip("'")   # 归一在 _paths 侧
        ```
        """
        return self.WORKSPACE_ROOT or ""

    @property
    def tool_artifact_path(self) -> Path:
        """工具正文**落盘件**根目录的绝对路径（专项工具升级-2 · R2-05）。

        与 ``upload_path`` / ``qdrant_path`` / ``sqlite_file`` **同型**：相对路径按
        ``ROOT_DIR``（= 数据根）解析，可 monkeypatch。

        ⚠️ **为什么不走 ``main._roots()``**（草案与初版清单点过这个名）：它的真身在
        ``backend/main.py:41``，而 ``main.py`` 顶层 ``import app.api.*`` ⇒ 从 ``app/**``
        反拉会**成环**（违分包边界）；冻结态 ``_roots()[0]`` 还优先 ``_MEIPASS`` /
        ``<exe>/_internal``（**只读资源根**，写盘必失败）。

        ⚠️ **为什么不加配置键**：本阶段只新增**属性**（不新增 ``.env`` 键）；落点固定在
        数据根下 ``data/tool_artifacts/``。该目录**不是产品数据**，可随时删。
        """
        return ROOT_DIR / "data/tool_artifacts"

    @property
    def qdrant_path(self) -> str | None:
        """内嵌向量库目录的**绝对路径**；相对路径按 ``ROOT_DIR``（= 数据根）解析。空值 ⇒ ``None``。

        ⚠️ **与 ``upload_path`` / ``sqlite_file`` 同一锚点**——这是 2026-09-21 条目 6.6 安装态验收
        抓到的问题（登记为发现）：``QDRANT_PATH=data/qdrant`` 原先**原样**交给 Qdrant 客户端，
        于是按**进程 CWD** 解析，而冻结态 onedir 的 CWD 是 ``<exe目录>\\_internal``（**资源根、约定只读**）
        ⇒ 向量库落到了 ``_internal\\data\\qdrant``：本机因 ``_internal`` 恰好可写而"看起来正常"，
        **装到受保护目录（如 Program Files）就会写失败**。

        **开发态零影响**：仓库 ``.env`` 的 ``QDRANT_PATH`` 为空 + 走 ``QDRANT_URL`` ⇒ 本属性返回 ``None``，
        与改动前逐字等价。
        """
        raw = (self.QDRANT_PATH or "").strip().strip('"').strip("'")
        if not raw:
            return None
        path = Path(raw).expanduser()
        return str(path if path.is_absolute() else ROOT_DIR / path)

    @property
    def mysql_dsn(self) -> str:
        return (
            f"mysql+aiomysql://{self.MYSQL_USER}:{self.MYSQL_PASSWORD}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DATABASE}"
            "?charset=utf8mb4"
        )

    @property
    def is_sqlite(self) -> bool:
        """当前后端是否为 SQLite（``DB_BACKEND=sqlite``）。方言判定**唯一入口**。"""
        raw = (self.DB_BACKEND or "").strip().strip('"').strip("'").lower()
        return raw == "sqlite"

    @property
    def sqlite_file(self) -> Path:
        """SQLite 数据文件绝对路径；相对路径按仓库根解析（与 ``.env`` 同一锚点）。"""
        raw = (self.SQLITE_PATH or "").strip().strip('"').strip("'")
        path = Path(raw or "data/zhilan.db").expanduser()
        return path if path.is_absolute() else ROOT_DIR / path

    @property
    def database_dsn(self) -> str:
        """按 ``DB_BACKEND`` 切换的异步 DSN（开发态 MySQL / 桌面 SQLite）。"""
        if self.is_sqlite:
            return f"sqlite+aiosqlite:///{self.sqlite_file.as_posix()}"
        return self.mysql_dsn

    def _preset(self, provider: str) -> dict[str, str]:
        return PROVIDER_PRESETS.get(provider.strip().lower(), {})

    def _resolve(
        self,
        provider: str,
        base_url: str,
        api_key: str,
        model: str,
        fallback_provider: str = "",
    ) -> "ModelConfig | None":
        provider = (provider or fallback_provider).strip().lower()
        if not provider:
            return None
        preset = self._preset(provider)
        if not preset:
            raise ValueError(f"未知的模型厂商：{provider}。请在 .env 中使用受支持的厂商名。")
        base_url = (base_url or preset.get("base_url", "")).strip().rstrip("/")
        if preset.get("kind") == "openai" and not base_url:
            raise ValueError(f"厂商 {provider} 需要配置 BASE_URL。")
        return ModelConfig(
            kind=preset.get("kind", "openai"),
            provider=provider,
            base_url=base_url,
            api_key=(api_key or "").strip(),
            model=(model or "").strip(),
        )

    # --- 多服务注册表（设置页管理）---

    @property
    def services(self) -> list[dict[str, Any]]:
        """解析 MODEL_SERVICES；坏数据一律忽略而不是抛错（.env 是手改得动的文件）。"""
        raw = _load_json(self.MODEL_SERVICES, [])
        out: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            service_id = str(item.get("id") or "").strip()
            if not service_id:
                continue
            models = [str(m).strip() for m in item.get("models") or [] if str(m).strip()]
            out.append(
                {
                    "id": service_id,
                    "name": str(item.get("name") or "").strip(),
                    "provider": str(item.get("provider") or "").strip().lower(),
                    "base_url": str(item.get("base_url") or "").strip().rstrip("/"),
                    "api_key": str(item.get("api_key") or "").strip(),
                    "models": models,
                    "model_roles": parse_model_roles(item.get("model_roles"), models),
                    "for_roles": parse_role_list(item.get("for_roles")),
                }
            )
        roles = self.roles
        for item in out:
            if item["for_roles"]:
                continue
            derived = [role for role in ROLE_KEYS if roles.get(role, {}).get("service_id") == item["id"]]
            item["for_roles"] = derived or ["chat"]
        return out

    @property
    def roles(self) -> dict[str, dict[str, str]]:
        """解析 MODEL_ROLES；服务 id 与模型名缺一不可，否则视为未绑定。

        遍历 ``ALL_ROLE_KEYS``（含 rerank，回归修复）：rerank 不给前端选，但必须被**解析出来**，
        否则 ``dump_registry(services, self.roles)`` 写回 MODEL_ROLES 时会把手写的 rerank 绑定整串抹掉。
        """
        raw = _load_json(self.MODEL_ROLES, {})
        out: dict[str, dict[str, str]] = {}
        for role in ALL_ROLE_KEYS:
            item = raw.get(role)
            if not isinstance(item, dict):
                continue
            service_id = str(item.get("service_id") or "").strip()
            model = str(item.get("model") or "").strip()
            if service_id and model:
                out[role] = {"service_id": service_id, "model": model}
        return out

    @property
    def rerank_binding(self) -> dict[str, str] | None:
        """``MODEL_ROLES`` 里的 rerank 绑定（未绑定 / 半个绑定 → ``None``）。只读。

        ⚠️ 重排的**消费侧不在这里**：``app/retrieval/rerank.py::resolve_rerank_config()`` 直读
        ``MODEL_ROLES``（它还要按厂商预设兜底 base_url），**不要**改成走本属性。
        这里只给测试与后续 settings 端点展示用。
        """
        return self.roles.get(RERANK_ROLE)

    @property
    def has_service_registry(self) -> bool:
        return bool(self.services)

    def service_by_id(self, service_id: str) -> dict[str, Any] | None:
        for item in self.services:
            if item["id"] == service_id:
                return item
        return None

    def _service_config(self, service: dict[str, Any], model: str) -> "ModelConfig":
        provider = service["provider"]
        preset = self._preset(provider)
        if not preset:
            raise ValueError(f"未知的模型厂商：{provider}。请在设置页重新选择厂商。")
        base_url = (service["base_url"] or preset.get("base_url", "")).strip().rstrip("/")
        if preset.get("kind", "openai") == "openai" and not base_url:
            label = service["name"] or service["id"]
            raise ValueError(f"服务「{label}」还没有填写接口地址。")
        return ModelConfig(
            kind=preset.get("kind", "openai"),
            provider=provider,
            base_url=base_url,
            api_key=service["api_key"],
            model=model.strip(),
        )

    def _resolve_role(self, role: str, legacy) -> "ModelConfig | None":
        """注册表是主真相源：有注册表就只看角色绑定，没注册表才回落扁平键。"""
        if not self.has_service_registry:
            return legacy()
        bound = self.roles.get(role)
        if not bound:
            return None
        service = self.service_by_id(bound["service_id"])
        if service is None:
            raise ValueError(
                f"{ROLE_LABELS.get(role, role)}模型绑定的服务已不存在（{bound['service_id']}），请在设置页重新指定。"
            )
        return self._service_config(service, bound["model"])

    @property
    def chat_config(self) -> "ModelConfig | None":
        def legacy() -> "ModelConfig | None":
            return self._resolve(self.MODEL_PROVIDER, self.LLM_BASE_URL, self.LLM_API_KEY, self.LLM_MODEL)

        return self._resolve_role("chat", legacy)

    @property
    def embed_config(self) -> "ModelConfig | None":
        def legacy() -> "ModelConfig | None":
            if not self.EMBED_MODEL.strip():
                return None
            return self._resolve(
                self.EMBED_PROVIDER,
                self.EMBED_BASE_URL,
                self.EMBED_API_KEY or self.LLM_API_KEY,
                self.EMBED_MODEL,
                fallback_provider=self.MODEL_PROVIDER,
            )

        return self._resolve_role("embed", legacy)

    @property
    def vision_config(self) -> "ModelConfig | None":
        def legacy() -> "ModelConfig | None":
            model = self.VISION_MODEL.strip() or self.LLM_MODEL.strip()
            if not model:
                return None
            return self._resolve(self.MODEL_PROVIDER, self.LLM_BASE_URL, self.LLM_API_KEY, model)

        return self._resolve_role("vision", legacy)


settings = Settings()


def _load_json(raw: str, default: Any) -> Any:
    text = (raw or "").strip()
    if not text:
        return default
    try:
        data = json.loads(text)
    except ValueError:
        return default
    return data if isinstance(data, type(default)) else default


def dump_registry(services: list[dict[str, Any]], roles: dict[str, dict[str, str]]) -> dict[str, str]:
    """序列化成能写进 .env 的单行 JSON（不带多余空格，避免 dotenv 的行内注释规则）。"""
    return {
        "MODEL_SERVICES": json.dumps(services, ensure_ascii=False, separators=(",", ":")),
        "MODEL_ROLES": json.dumps(roles, ensure_ascii=False, separators=(",", ":")),
    }


def parse_role_list(value: Any) -> list[str]:
    """把任意输入收成合法的角色名列表（去重保序）。"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for raw in value:
        role = str(raw).strip().lower()
        if role in ROLE_KEYS and role not in out:
            out.append(role)
    return out


def parse_model_roles(value: Any, models: list[str] | tuple[str, ...]) -> dict[str, list[str]]:
    """把一条服务的 model_roles 收成 {型号: [角色...]}。坏数据静默丢弃。"""
    if not isinstance(value, dict):
        return {}
    allowed = set(models)
    out: dict[str, list[str]] = {}
    for raw_key, raw_roles in value.items():
        model = str(raw_key).strip()
        if model not in allowed:
            continue
        roles = parse_role_list(raw_roles)
        if roles:
            out[model] = roles
    return out


def new_service_id(provider: str, existing: list[str]) -> str:
    """按厂商起可读的稳定 id，重名时加序号。"""
    slug = re.sub(r"[^a-z0-9]+", "_", (provider or "").strip().lower()).strip("_")
    base = f"svc_{slug}" if slug else "svc_custom"
    taken = set(existing)
    if base not in taken:
        return base
    index = 2
    while f"{base}_{index}" in taken:
        index += 1
    return f"{base}_{index}"


def build_registry_from_legacy(cfg: "Settings") -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]] | None:
    """把旧的扁平配置收成「服务 + 角色绑定」。没有任何可迁移内容时返回 None。"""
    services: list[dict[str, Any]] = []

    def service_for(provider: str, base_url: str, api_key: str) -> dict[str, Any] | None:
        provider = (provider or "").strip().lower()
        if not provider:
            return None
        base_url = (base_url or PROVIDER_PRESETS.get(provider, {}).get("base_url", "")).strip().rstrip("/")
        api_key = (api_key or "").strip()
        for item in services:
            if (item["provider"], item["base_url"], item["api_key"]) == (provider, base_url, api_key):
                return item
        item = {
            "id": new_service_id(provider, [row["id"] for row in services]),
            "name": provider,
            "provider": provider,
            "base_url": base_url,
            "api_key": api_key,
            "models": [],
            "model_roles": {},
        }
        services.append(item)
        return item

    chat_service = service_for(cfg.MODEL_PROVIDER, cfg.LLM_BASE_URL, cfg.LLM_API_KEY)
    embed_service = service_for(cfg.EMBED_PROVIDER, cfg.EMBED_BASE_URL, cfg.EMBED_API_KEY or cfg.LLM_API_KEY)

    roles: dict[str, dict[str, str]] = {}
    if chat_service and cfg.LLM_MODEL.strip():
        roles["chat"] = {"service_id": chat_service["id"], "model": cfg.LLM_MODEL.strip()}
    if embed_service and cfg.EMBED_MODEL.strip():
        roles["embed"] = {"service_id": embed_service["id"], "model": cfg.EMBED_MODEL.strip()}
    vision_model = cfg.VISION_MODEL.strip() or cfg.LLM_MODEL.strip()
    if chat_service and vision_model:
        roles["vision"] = {"service_id": chat_service["id"], "model": vision_model}

    if not services:
        return None
    for item in services:
        item["for_roles"] = [role for role in ROLE_KEYS if roles.get(role, {}).get("service_id") == item["id"]] or ["chat"]
    return services, roles


# ⚠️ 这里原本有个 `repair_missing_role_bindings`，回归修复 已**整函数删除**：
# 它是死代码（全仓只有定义处一处命中、零调用零测试），且一旦被接线就会
# `roles = dict(target.roles)`（旧实现只解析三角色）+ `dump_registry` **整串重写** MODEL_ROLES，
# 静默抹掉手写的 rerank 绑定。**不要**以任何形式复活它（回归栅栏见 tests/test_model_roles.py）。
#
# ⚠️ 原 `_service_for_flat`（回归修复 于 2026-09-26 整函数删除）的警告（逐字保留）：
# 「在已有服务里找出某角色扁平键指向的那一条；找不到返回 None（绝不新建服务）。
#   ⚠️ **回归修复 后暂无调用方**：它原本唯一的调用方是 `repair_missing_role_bindings`
#   （已删除，见上）。保留它是备「扁平键 → 服务」解析的单一实现，**不要**据它重建自动补绑逻辑。」
# —— 该符号本身已按「零调用点死代码」删除；**警告仍然有效**：不要据这条路重建自动补绑逻辑。


def ensure_service_registry(cfg: "Settings | None" = None) -> dict[str, str]:
    """首次启动时把 .env 里的旧扁平配置迁移成服务注册表。

    幂等：已有注册表就什么都不做；落盘失败不影响本次运行（内存里已生效）。
    返回落盘的键值（没迁移时返回空字典）。
    """
    target = cfg if cfg is not None else settings
    if target.has_service_registry:
        return {}
    built = build_registry_from_legacy(target)
    if not built:
        return {}
    services, roles = built
    # 保留 **`MODEL_ROLES` 里旧配置迁移没产出的所有角色键**（不止三角色 + rerank）：本分支只在
    # 「没有注册表」时走，但用户可能已经手写过 `MODEL_ROLES` —— `dump_registry` 是整串重写，不合并
    # 就会把它抹掉（与已删的 `repair_missing_role_bindings` 同一类地雷，见 `回归修复`）。
    #
    # ⚠️ 数据源必须是**原始 MODEL_ROLES JSON**，不能用 `target.roles`（回归修复 对抗性复核 P2-4
    # 实测）：`Settings.roles` 只遍历 `ALL_ROLE_KEYS`，拿它当数据源就只能保住 4 个已知角色键，
    # 用户手写的 `translate` 之类未知键照样被整串重写丢掉——注释与实现会对不上。
    # 这里**只搬运、不校验不规范化**（不判角色是否合法、不裁字段、不改大小写）：目标是
    # 「不销毁用户手写的键」，不是「读懂它们」。
    raw_roles = _load_json(target.MODEL_ROLES, {})
    for role, binding in raw_roles.items():
        if isinstance(binding, dict) and binding and role not in roles:
            roles[role] = binding
    updates = dump_registry(services, roles)
    target.MODEL_SERVICES = updates["MODEL_SERVICES"]
    target.MODEL_ROLES = updates["MODEL_ROLES"]
    try:
        persist_env(updates)
    except OSError as exc:
        print(f"模型服务注册表写入 .env 失败（本次运行仍生效）：{exc}")
        return {}
    return updates


def _read_env_file_key(key: str) -> str:
    """从 ``.env`` **文件**里读一个键的值（找不到 / 读不了 → 空串）。解析口径 = **python-dotenv**。

    只服务下面的 ``migrate_legacy_rerank``：``RERANK_*`` 是**已废的扁平键**，给它们加
    pydantic 字段等于给死键发新户口（回归修复 的教训），故直接读文件、用完即止。

    ⚠️ **为什么不再手写「dotenv 最小集」**（回归修复 对抗性复核 P2-3 实测的 4 处分歧）：
    手写解析与真实 dotenv 在「引号 + 行内注释」「同名键」「``export`` 前缀」「文件带 BOM」四条上
    结果不同——而 ``Settings`` 读**同一个文件**走的就是 python-dotenv。两套解析 = 「迁移读到的值」
    与「应用真正生效的值」可以不同：前两条会把**带引号的错型号**写进 ``MODEL_ROLES.rerank``，
    后两条则是**漏迁**。故这里只留一条口径，直接问 dotenv 要值。

    ``python-dotenv`` 是 pydantic-settings 的既有依赖，不会 import 不到；真 import 不到时退化为
    空串——等价于「没有遗留键」，迁移一律跳过，**绝不写坏数据**。
    （实测：把 ``dotenv`` 屏蔽掉时 ``import app.core.config`` 先一步就失败——pydantic-settings
    自己就 import 它——所以这里的退化分支实际不可达，留着只是不让本函数成为缺依赖时的炸点。）
    """
    if dotenv_values is None:  # pragma: no cover - 兜底分支不可达，见上
        return ""
    try:
        return dotenv_values(env_store.ENV_PATH).get(key, "") or ""
    except OSError:
        return ""


def migrate_legacy_rerank(cfg: "Settings | None" = None) -> dict[str, str]:
    """把存量 `.env` 里的遗留扁平键 ``RERANK_MODEL`` **一次性**迁进 ``MODEL_ROLES.rerank``。

    **这是一条只读一次的迁移通道**，不是开关：v2 里 rerank 绑定的唯一真相源是
    ``MODEL_ROLES.rerank``（由 ``app/retrieval/rerank.py`` 直读）。迁移完成之后，
    ``RERANK_MODEL`` / ``RERANK_PROVIDER`` / ``RERANK_BASE_URL`` 三个扁平键在本仓**再无任何读取点**
    ——本函数只在「``MODEL_ROLES`` 里还没有 rerank 绑定」时读它们一次，
    其中 ``RERANK_MODEL`` 是**迁移内容**，``RERANK_PROVIDER`` / ``RERANK_BASE_URL`` 只用于下面的
    **准入判断**（判断完即丢，不写进绑定）。

    ⚠️ 与 ``回归修复``（``REDIS_ENABLE`` 死键）的教训**区分开**：那条是「键在、描述说得像活开关、
    实际没人读」；这里的三行是**已知已废 + 一次性迁移读取**（存量 `.env` 的平滑通道）。
    共同结论一致：**不给废键加 pydantic 字段**（那等于发新户口），故本函数直接读文件。

    迁移口径（用户拍板的「口径 A」+ 回归修复 对抗性复核实测后的三条加固）：

    - ``MODEL_ROLES`` 已有 rerank 绑定 ⇒ 直接 ``{}``（**幂等**的保证：跑第二次什么都不写）；
    - 没有绑定 + 已绑定 embed + ``RERANK_MODEL`` 非空 ⇒ 复用 **embed 那条服务**的 ``service_id``
      （遗留 RERANK_* 从来没有独立的 key/url 管理入口，embed 是约定俗成的承载服务）
      与 ``RERANK_MODEL`` 型号，写成 ``roles["rerank"]``，经 ``dump_registry`` 序列化后落盘；
    - **只写 ``MODEL_ROLES`` 一个键，绝不改写 ``MODEL_SERVICES``**（P2-1）：``dump_registry`` 会
      顺手把 services 重新序列化（丢未知字段、``api_key`` 去空格、``base_url`` 去尾斜杠），
      整串写回去 = 迁移改坏用户的注册表，而内存里还是原值 ⇒ 磁盘/内存当场不一致。内存里的
      ``MODEL_SERVICES`` 也**不动**；
    - embed 绑定的那**条服务已不存在** ⇒ ``{}`` 不迁（P2-2）：否则写出一条永远取不到服务的绑定，
      而「已有 rerank 绑定 ⇒ 返回 ``{}``」这条幂等短路会让后续每次启动都不再重试（= 永久钉死）；
    - 遗留配置**指向的不是 embed 那条服务** ⇒ ``{}`` 不迁 + 一行中文提示（P2-5）：
      ``RERANK_PROVIDER`` 非空且不等于 ``dashscope``（典型：``bge`` 本地 CrossEncoder 路），
      或 ``RERANK_BASE_URL`` 非空且与 embed 服务的 ``base_url`` 不一致（自定义地址）——
      若照迁，请求会打到 embed 的 host（404/400 ⇒ 每次静默回落），而 `.env` 那三行又
      「长得像已配置」，用户查不出来；
    - 其余情形（无 ``RERANK_MODEL`` / 没有 embed 绑定 / 没有服务注册表 / 文件读不了或写不了）
      一律**静默 ``{}`` 不抛**（I5）；落盘失败只打一行中文说明，内存里的绑定仍生效。
    - **不动** .env 里那三行 ``RERANK_*``（保留现场，只是不再被读）。

    返回落盘的键值（没迁移时 ``{}``）。
    """
    target = cfg if cfg is not None else settings
    roles = dict(target.roles)
    if roles.get(RERANK_ROLE):
        return {}
    # 没有注册表就**不迁**：否则会写出一条指向不存在服务的绑定（`target.services` 为空 ⇒
    # `resolve_rerank_config()` 永远拿不到服务），而且因为「已有 rerank 绑定」这条幂等判据成立，
    # **后续再也不会重试**——那等于把「rerank 配不上」永久钉死（对抗性复核点名的悬挂边界）。
    if not target.has_service_registry:
        return {}
    embed = roles.get("embed")
    if not embed:
        return {}
    # P2-2：embed 绑定的服务必须**真的还在**（`MODEL_SERVICES` 里删过服务、`MODEL_ROLES` 还留着
    # 指向它的绑定，是设置页改坏的常见现场）。这里再拦一道，与上面「没有注册表」同一条理由：
    # 写出去 = 悬挂绑定 + 永久不再重试。**只提示不抛**（I5：启动流程不许被配置问题炸穿）。
    embed_service = target.service_by_id(embed["service_id"])
    if embed_service is None:
        print(f"embed 绑定的服务已不存在（{embed['service_id']}），跳过 rerank 迁移")
        return {}
    # P2-5：准入判断——遗留配置指向的必须就是 embed 那条服务，否则迁出来必然打错 host。
    legacy_provider = _read_env_file_key("RERANK_PROVIDER").strip()
    legacy_base_url = _read_env_file_key("RERANK_BASE_URL").strip().rstrip("/")
    embed_base_url = str(embed_service.get("base_url") or "").strip().rstrip("/")
    if (legacy_provider and legacy_provider.lower() != "dashscope") or (
        legacy_base_url and legacy_base_url != embed_base_url
    ):
        print(
            "检测到遗留重排配置指向自定义服务"
            f"（provider={legacy_provider or '未设置'} / base_url={legacy_base_url or '未设置'}），"
            "未自动迁移；请在设置页配置重排序"
        )
        return {}
    model = _read_env_file_key("RERANK_MODEL")
    if not model:
        return {}
    roles[RERANK_ROLE] = {"service_id": embed["service_id"], "model": model}
    # P2-1：**只落 MODEL_ROLES 一个键**——不把 `dump_registry` 顺手重排出的 `MODEL_SERVICES`
    # 放进 updates（那会把用户的注册表整串改写：丢未知字段、api_key 去空格、base_url 去尾斜杠），
    # 也不改内存里的 `MODEL_SERVICES`（改了就是磁盘与内存当场不一致）。
    updates = {"MODEL_ROLES": dump_registry(target.services, roles)["MODEL_ROLES"]}
    target.MODEL_ROLES = updates["MODEL_ROLES"]
    try:
        env_store.persist_env(updates)
    except OSError as exc:
        print(f"rerank 绑定写入 .env 失败（本次运行仍生效）：{exc}")
        return {}
    return updates
