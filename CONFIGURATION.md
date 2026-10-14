# 配置说明

本文的数据、模型和日志位置是运行时生成的路径，不是仓库文件链接。

知澜读取本机 `.env`。源码运行时它位于仓库根目录；Windows 安装版位于安装目录。设置页会写入支持的配置键，其余键需要手工修改并重启应用。源码快速开始仅在 `.env` 不存在时复制 [.env.example](.env.example)，已有配置不要覆盖。系统环境变量可以优先于 `.env`，修改文件后也应检查是否存在同名环境变量。

真实配置包含 API Key，请保留在本机。公开仓库仅包含无凭据示例，不应跟踪真实 `.env`。

## 数据与端口

| 配置键 | 示例或默认 | 含义 |
|---|---|---|
| `DB_BACKEND` | 示例为 `sqlite` | 快速开始使用 SQLite；源码类的缺省值是 MySQL，因此应复制示例 |
| `SQLITE_PATH` | 空 | 使用本机数据根的 `data/zhilan.db` |
| `QDRANT_URL` | 示例为空 | 外部服务地址；仅在 `QDRANT_PATH` 为空时使用，源码缺省地址不为空 |
| `QDRANT_PATH` | `data/qdrant` | 本地向量库目录，相对路径按数据根解析；非空时优先于 `QDRANT_URL` |
| `QDRANT_COLLECTION` | `zhilan_chunks` | 向量集合基础名称 |
| `UPLOAD_DIR` | `data/uploads` | 上传文件位置 |
| `EMBED_DIM` | `1024` | Embedding 向量维度，必须与模型输出一致 |
| `PORT` | `8000` | 桌面启动器优先使用的端口，忙时自动寻找后续空闲端口 |

示例同时设置本地路径并清空服务地址。改用外部 Qdrant 时需要清空 `QDRANT_PATH` 并设置 `QDRANT_URL`；仅改服务地址不会覆盖已有本地路径。本地 Qdrant 目录不能被多个进程同时打开。设置页不支持完整数据目录搬迁；修改目录只改变配置位置，不会自动搬运或合并已有资料。

MySQL 和外部 Qdrant 是可选服务方式，需要另行部署。`docker-compose.yml` 提供本机服务定义；它不属于 SQLite 快速开始必需步骤，实际部署前需要替换开发用口令占位符并核对端口及持久化目录。

应用使用进程内 Redis 兼容层。`REDIS_ENABLE` 不会切换成外部 Redis，健康检查的 Redis 状态不代表外部 Redis 服务连接成功。

## 模型服务与角色

推荐通过设置页添加模型服务，再为聊天、Embedding、视觉角色分别绑定服务和型号。聊天服务和 Embedding 服务可以不同；视觉模型只在需要对应能力时配置。

`MODEL_SERVICES` 保存服务注册表，`MODEL_ROLES` 保存角色绑定，内容为单行 JSON。已有注册表时，它们是模型角色的配置来源，缺少某个角色绑定不会自动使用旧式扁平键兜底。请优先通过设置页修改，避免手工拼接含凭据的 JSON。

兼容旧式配置的键包括 `MODEL_PROVIDER`、`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`、`EMBED_PROVIDER`、`EMBED_BASE_URL`、`EMBED_API_KEY`、`EMBED_MODEL`、`VISION_MODEL`。新的源码示例将这些键留空，由设置页完成注册表初始化和绑定。

Ollama 的本地兼容接口配置为 `http://127.0.0.1:11434/v1`。需要先运行本机服务，并安装实际使用的聊天和 Embedding 型号。当前版本的模型列表和回答构建要求非空 API Key：连接免鉴权的本机服务时填占位值，例如 `ollama`；启用鉴权的服务填写真实 Key。界面可能仍提示本机 Key 可以留空，请以此处说明为准。

某个型号是否支持 Embedding、输出多少维，应以实际调用结果为准，不要把聊天型号直接当作向量型号。默认 `EMBED_DIM=1024`；输出维度不同，需要手工修改本机 `.env` 中的 `EMBED_DIM` 并重启，当前设置页没有维度编辑入口。更换 Embedding 服务、型号或维度后，为受影响的资料重新构建索引。旧向量不可以因为维度相同就当作新模型生成的向量。

## 文档解析

| 配置键 | 示例值 | 含义 |
|---|---|---|
| `MINERU_ENABLE` | `false` | 源码示例不启用额外解析运行时；Windows 成品模板显式启用 |
| `MINERU_DEVICE` | `cpu` | 设备选择，桌面交付使用 CPU |
| `MINERU_BACKEND` | `auto` | 解析运行时的后端选择 |
| `MINERU_MODEL_DIR` | 空 | 兼容运行时的模型 HOME 根目录，应用会在其下追加 `models/MinerU-4_models_onnx`；不是 ZIP 路径，也不是已有 `models` 子目录 |
| `MINERU_VLM_ENABLE` | `false` | 当前桌面交付不启用 VLM |
| `OCR_IMAGE_ENABLE` | `true` | 有可用 OCR 运行时与模型时允许图片解析 |

源码的基本 `requirements.txt` 不包含完整 MinerU 运行时。仅打开开关或导入权重不能代替运行时安装。Windows 成品已集成相应运行时，启动器默认使用本地模型来源和安装目录内的模型。

模型 HOME 首先取系统环境变量 `MINERU_HOME`，其次取 `MINERU_MODEL_DIR`；源码版两者都未设置时使用用户目录下的 `.mineru`。Windows 安装版启动器默认将 `MINERU_HOME` 设为可执行文件所在目录，已有系统环境变量优先，因此安装版通常将模型写入安装目录的 `models/`。最终导入位置以面板显示为准。

兼容模型目录结构由应用模型清单约定，改目录前应确认运行时所需的完整结构；任意模型 ZIP 不会被当作兼容包接受。

## 图谱自动连线

同一知识库内，两篇文档代表向量的余弦相似度接近时会生成一条第三类图谱边（`kind="similar"`）。该边在导入发布收尾、重建索引收尾和删除文档时维护，也可以用维护脚本全量重算。它表示「内容接近」，不是事实关系，也不参与检索与回答；维护链只读已有向量，不调用模型。

| 配置键 | 默认 | 含义 |
|---|---|---|
| `GRAPHLINK_ENABLE` | `true` | 总开关。关闭时维护链在碰表之前返回，图谱输出与旧版一致；Windows 成品模板同样默认开启 |
| `GRAPHLINK_TOP_K` | `3` | 每篇文档保留的单向名额（口径是两端 top-K 的并集，不是全局 top-K） |
| `GRAPHLINK_MIN_SCORE` | `0.65` | 余弦阈值。`0.65` 是试验起点、未标定，调参前请先读 [版本变更](CHANGELOG.md) 的质量读数 |
| `GRAPHLINK_MAX_DEGREE` | `3` | 每篇文档剪枝后的最终度数上限 |
| `GRAPHLINK_MAX_CHUNKS_PER_DOC` | `64` | 每篇文档参与代表向量的分块上限，按序号等距均匀抽样 |
| `GRAPHLINK_MAX_DOCS_PER_KB` | `2000` | 单个知识库参与连线的文档数上限，超出时跳过并写日志 |

这些键只通过本机 `.env` 生效，设置页没有对应字段；修改后需要重启应用。计算量与文档数平方相关：1000 篇约 46 秒，超过 2000 篇的护栏未实测。相似边不携带相似度分数，界面按边类型显示或隐藏。

升级不会立即给全部历史资料补边；重建相关索引会触发维护，也可在已配置的源码 Python 环境中使用 [补算脚本](backend/scripts/rebuild_similarity_edges.py)。先用 0.1.6 启动应用完成数据库迁移至 `0011`，再退出应用、备份实际数据库与向量目录。以下示例用于 SQLite + 本地 Qdrant：从源码根目录执行，使用与原应用一致的 `.env`（尤其向量集合、维度和开关）；目标必须指向同一套资料库及向量索引，本地 Qdrant 不能同时被应用占用。

```powershell
$Database = Read-Host '请输入实际 SQLite 文件的绝对路径'
$VectorPath = Read-Host '请输入对应 Qdrant 目录的绝对路径'
if (-not (Test-Path -LiteralPath $Database -PathType Leaf)) { throw '数据库文件不存在' }
if (-not (Test-Path -LiteralPath $VectorPath -PathType Container)) { throw '向量目录不存在' }
$Dsn = 'sqlite:///' + $Database.Replace('\', '/')
& .\backend\.venv\Scripts\python.exe .\backend\scripts\rebuild_similarity_edges.py --dsn $Dsn --qdrant-path $VectorPath --dry-run
if ($LASTEXITCODE -ne 0) { throw '试算未成功，请检查输出并停止补算' }
```

试算只计算、不写相似边。核对结果后，使用同一命令去掉 `--dry-run` 才会写入；可加 `--kb 1,2` 限定知识库。退出码 `0` 表示成功，`1` 表示异常或有库未完成，`2` 表示关态拒绝或参数错误。这个命令不重建向量、不请求模型，Windows 成品自身不提供可直接运行该脚本的独立 Python 命令。MySQL 与服务端 Qdrant 需使用对应连接参数，当前版本没有真实部署验收。

## 文件工具、MCP 与联网

`WORKSPACE_ROOT` 为空时不启用相应文件工具。在设置页选择工作区根目录后，可以使用受边界限制的只读文件工具，文件路径相对该目录解析，越界路径会拒绝。写入工具另外需要有效的专用确认机制；当前默认配置与界面没有接入完整写入审批，设置根目录不表示模型可以修改文件，也不能仅靠填写确认密钥就完成审批。

MCP 配置使用以下键：

| 配置键 | 格式及用途 |
|---|---|
| `MCP_SERVERS` | JSON 数组，描述包的 slug、传输、命令或地址等；空表示无 MCP 包 |
| `MCP_ALLOW_SERVERS` | 逗号分隔的精确 slug，允许发现和使用的包 |
| `MCP_ALLOW_EGRESS` | 逗号分隔的精确 slug，HTTP 包的外发许可 |

许可默认关闭，名单不接受通配符。`stdio` 服务需要可执行命令及依赖，HTTP 服务需要可用地址和外发许可。MCP 发现失败目前主要从 `logs/zhilan.log` 排查，见 [README](README.md#mcp-与联网功能排障)。

在“设置 → 联网搜索”选择渠道并保存即可生效；直接修改 `.env` 后需重启。对话页的联网开关控制本轮是否搜索。限定知识库、文档等范围的提问仍遵守该范围。

| 配置键 | 默认或示例 | 含义 |
|---|---|---|
| `WEB_ENABLE` | `false` | 是否装配联网搜索与页面抓取工具 |
| `WEB_CHANNEL` | `api` | `api` 为传统搜索，`llm` 为 MiMo 原生搜索 |
| `WEB_SEARCH_PROVIDER` | `duckduckgo` | 搜索提供方，其他提供方需要相应服务及凭据 |
| `WEB_SEARCH_API_KEY` | 空 | 需要 Key 的搜索服务凭据 |
| `WEB_LLM_PROVIDER` | `mimo` | 大模型搜索目前支持 MiMo |
| `WEB_LLM_MODEL` | 空 | 搜索型号；为空时可复用官方 MiMo 对话绑定 |
| `WEB_LLM_BASE_URL` | 空 | 默认 `https://api.xiaomimimo.com/v1` |
| `WEB_LLM_API_KEY` | 空 | 独立搜索凭据；官方地址可复用官方 MiMo 对话凭据 |
| `WEB_LLM_SEARCH_TOOL` | `web_search` | MiMo 原生联网插件，必须开启 |
| `WEB_LLM_MAX_SEARCHES` | `5` | 每次最多 1 到 6 个搜索关键词 |
| `WEB_SSRF_ALLOW_ANY_HOST` | `false` | 允许私网或本机地址的开关；一般保持关闭 |
| `WEB_SEARCH_BUDGET_SECONDS` | `20` | 一次搜索的抓取预算，单位秒，`0` 表示不限制 |

`WEB_SSRF_ALLOW_ANY_HOST` 的启用状态可从只读设置 API 获取，但前端界面不显示；允许相应地址时只有 WARNING 日志。不要把一般搜索超时当作需要开启此开关的理由。

搜索结果使用服务商返回的来源注释，并在普通回答和流式回答中参与资料引用；无来源或搜索失败会显示原因。使用远程搜索会发送提问，并可能产生厂商费用；设置显示有效不代表远端额度、权限或连通性已验证。配置密钥保存在本机 `.env`，界面留空保留已有密钥。复用的 MiMo 凭据不会发送到其他主机、明文 HTTP 或非标准端口。

## 配置与数据外发

本机存储不意味着所有计算都在本机。使用远程聊天或 Embedding 服务时，提问与资料片段会发送给配置的服务；联网搜索、翻译、HTTP MCP 等功能也可能外发相应内容。请根据资料敏感程度选择服务和工具。

日志脱敏只覆盖特定模式，不会自动识别所有私人正文。报告问题前先人工检查日志与截图。用量显示和应用估算不能替代服务提供方的正式账单。
