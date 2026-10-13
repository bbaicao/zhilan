# Zhilan

Zhilan is a local-first knowledge base and retrieval-augmented question-answering application.
It provides a Windows desktop interface, document ingestion, source references, and optional tools.
Version: **v0.1.5** (public prerelease; known limitations apply). Source code is licensed under **Apache-2.0**.
Chat and embedding require configured model services, such as an API provider or local Ollama.
The Chinese documentation below describes setup, usage, and current limitations.

## 知澜：把自己的资料变成可检索的知识库

知澜将文档、笔记与对话放在本机，帮助你从自己的资料中查找信息并生成带来源的回答。当前源码版本是 **v0.1.5**，整理日期为 **2026 年 10 月 13 日**，预发布标识为 **v0.1.5-public.1**。[本版下载页面](https://github.com/bbaicao/zhilan/releases/tag/v0.1.5-public.1)提供源码、Windows 完整程序和安装器，发布身份由同批 SOURCE、清单与标签绑定。旧版成品保留原身份；成品及校验边界见 [发布附件说明](ATTACHMENTS.md)。

**旧版 v0.1.0-public.1 于 2026 年 10 月 8 日公开预发布。** [GitHub 仓库](https://github.com/bbaicao/zhilan)与[旧版下载页面](https://github.com/bbaicao/zhilan/releases/tag/v0.1.0-public.1)保留原快照及附件；旧下载包不代表当前源码的验证对象。v0.1.1 本地包也保留原身份。

**v0.1.5 按预发布／试用版准备，仍有已知质量与安装验证限制。** 本版在 0.1.4 公开发布线上修复生成笔记的异常 JSON／过长标题、保存失败提示和 Embedding 返回索引兼容问题；保留该发布线的上传、会话恢复、图片和回答状态修复。源码 ZIP、Windows 程序与安装器使用独立发布目录及同批清单核对，旧包保持原身份。变更与限制见 [版本变更](CHANGELOG.md)，发布身份和成品核对规则见 [发布说明](RELEASE.md)。

**运行前提：安装包可以直接打开界面，但问答与向量索引需要先配置聊天、Embedding 模型服务。** 远程服务和启用鉴权的本机服务需要有效 API Key；当前版本连接免鉴权的本机 Ollama 时，也要填写非空占位 Key，例如 `ollama`。离线文档解析模型与这两类模型用途不同，导入解析模型后仍需要配置问答模型。

**Windows 安装包未进行代码签名。** Windows SmartScreen 可能提示“未知发布者”或阻止首次运行。请核对文件来源与发布者提供的 SHA256；确认来源可信后，再按 Windows 提示操作。

资料默认存本机；如果使用远程模型服务，相关提问和被检索出的资料片段会发送给你配置的服务。联网搜索、HTTP MCP 服务等功能也可能访问外部服务。使用本地服务能够减少外发，但需要你自行准备并运行对应模型。

## 当前可以做什么

| 能力 | 当前状态 |
|---|---|
| Windows 桌面窗口、设置与本地数据保存 | 已实现，安装与现有 WebView2 环境下的运行已有验证 |
| 文本、Markdown、PDF、Office 表格及演示文件导入 | 已实现；解析效果取决于文件内容和格式 |
| 扫描 PDF 与图片文字识别 | Windows 安装包支持，需要先导入兼容的离线文档解析模型 |
| 设置页选择模型 ZIP、校验并导入 | 已实现，可查看进度、重开导入面板和重复导入 |
| 知识库检索、带引用回答、流式对话 | 已实现；模型答案和引用准确性仍需要人工核查 |
| 笔记、资料关系、会话与工具调用 | 已实现；工具需要相应配置和许可 |
| 多机器、所有格式、所有模型下的稳定性 | 尚未全面验证，见下文“已知限制” |

## Windows 安装与首次使用

源码仓库不内置安装程序、模型权重和第三方运行时。取得同一版本的 Windows 安装包、配套模型 ZIP 和 SHA256 清单后，可按以下顺序操作；配套公开解析模型 ZIP 约 **656 MB**，安装包实际大小以发布清单为准。模型包含原模型、许可全文和固定来源说明。

1. 安装到自己有写入权限的目录，最后一级目录名必须只含 ASCII 字符，例如 `Zhilan`；安装器会拒绝末级含中文等非 ASCII 字符的目录。程序默认在安装目录保存配置、资料、模型与日志。
2. 启动知澜，打开“设置”，添加模型服务，并分别给“聊天”和“Embedding”绑定服务与型号。远程服务和启用鉴权的本机服务填写对应 API Key；免鉴权的本机 Ollama 填写非空占位 Key，例如 `ollama`，并确认服务已运行且所选型号已安装。界面可能仍提示本机 Key 可以留空，当前版本的模型列表和回答构建仍需要非空值。
3. 如果要识别扫描 PDF 或图片，进入“设置 → 离线文档解析模型”，选择兼容的模型 ZIP，然后开始导入。
4. 创建知识库并上传一份小文档。确认解析完成、向量索引成功后，再尝试提问并点击引用核对原文。

关闭模型导入面板后，导入可以在应用内继续；再次打开面板可查看状态。退出整个应用会中断导入。重复导入会跳过校验一致的文件，遇到冲突文件会拒绝覆盖。坏包在后部失败时，前部已导入的文件可能保留，修正问题后可以重试。如果之前已经出现解析失败，模型导入完成后先重启应用，再重试该文档。

详细操作见 [使用说明](USAGE.md)，模型与数据配置见 [配置说明](CONFIGURATION.md)。

## 从源码启动

以下是 Windows PowerShell 下的本地开发流程。已使用的 Python 版本线是 **3.13**；需要 Git、Python、Node.js/npm，以及 Windows 桌面窗口所需的 WebView2。前端锁文件中 Vite 声明的 Node.js 范围为 **`^18.0.0 || ^20.0.0 || >=22.0.0`**；本次 Windows 实际验证使用 **Node.js 24.16.0 / npm 11.13.0**。这不表示所有依赖均已在该范围内验证；锁文件中的平台专属可选依赖可能另有版本约束，其他平台与版本组合未验证。源码快速开始使用 SQLite 和本地 Qdrant，不需要先启动 Docker、MySQL 或 Redis。

`backend/requirements.txt` 中部分依赖没有锁定版本。本次未在全新虚拟环境中从零完成 `pip install` 验证；已有 SQLite 运行检查不能代表今后所有浮动依赖组合都能安装和运行。下面是启动流程，遇到依赖安装问题时请保留具体版本和错误信息。

在仓库根目录执行：

```powershell
py -3.13 -m venv backend\.venv
.\backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
Push-Location frontend
npm ci
node scripts/build.mjs
Pop-Location
.\backend\.venv\Scripts\python.exe -m client
```

之后可用 [start.bat](start.bat) 启动。复制示例只在 `.env` 不存在时执行；已有配置请保留并检查，不要用示例覆盖自己的服务和密钥。后端启动时会自动迁移当前配置的数据文件；第一次启动前请检查根目录的 `.env`，已有数据升级前请备份。

源码依赖清单没有安装 MinerU。上述步骤提供基本文本解析、知识库及桌面应用环境，**不保证扫描 PDF 或图片 OCR 可用**。OCR 需要另行准备兼容的解析运行时与模型；Windows 成品包已集成对应运行时。当前桌面交付使用 CPU 解析，未启用 VLM。

仅需在浏览器中调试后端时，可从 `backend` 目录运行以下命令，并打开终端显示的本地地址：

```powershell
Push-Location backend
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
Pop-Location
```

这条命令需要前端已经构建，且不要与桌面窗口同时使用同一个本地向量库目录。桌面启动器会从配置端口开始寻找空闲端口；手工启动的 uvicorn 不会自动换端口。

## 开发检查与构建

公开内容审计保留 **Git 暂存区（索引）和全部可达 refs 的历史** 检查，并可扫描当前未暂存修改、非忽略的新文件、明确指定的导出源码与附件。先在仓库根目录查看状态和差异：

```powershell
git status --short
git diff
git diff --cached
```

检查当前工作区无需先暂存，也不会改动索引或提交：

```powershell
node tools/release/precheck.mjs --working-tree
node tools/release/precheck-selfcheck.mjs
```

`precheck-selfcheck.mjs` 用正反控制检查拒绝能力。最终材料使用 `--source-dir` 与 `--artifact-dir` 指定：源码 ZIP 会实际解压并按清单逐件核对；Windows 附件检查文件哈希和同批身份，内嵌代码另由冻结核验提供证据。未提供附件输入时该项为 Unverified。详细流程见 [发布与维护说明](RELEASE.md)。检查不替代模型质量、许可来源和安装验收。

后端测试放在 [backend/tests](backend/tests)。只运行与你的修改直接相关的测试。测试收集时会导入配置模块，因此即使是模型角色配置测试，也可能在测试夹具生效前读取仓库根目录 `.env`。

本次新增 [隔离定向检查入口](backend/scripts/check_release_ready.py)。只用于**不携带真实 `.env`、资料库、索引、模型和日志的独立源码副本**，使用仓库外已准备好的 Python 环境及新的状态目录。入口会拒绝真实 `.env`、剥离凭据环境变量并阻断外网，写入独立 SQLite、JUnit 和结果 JSON。选择相同状态目录依次运行两个重启模式，才能检查跨进程恢复：

```powershell
$Python = Read-Host '请输入仓库外测试环境的 python.exe 绝对路径'
$TestSource = Read-Host '请输入无真实配置和数据的源码副本绝对路径'
$TestState = Read-Host '请输入副本外新的测试状态目录绝对路径'
foreach ($Mode in @('upload', 'targeted', 'restart-seed', 'restart-read')) {
    & $Python "$TestSource\backend\scripts\check_release_ready.py" --source $TestSource --state $TestState --mode $Mode
    if ($LASTEXITCODE -ne 0) { throw "检查失败：$Mode" }
}
```

本轮四个串行批次按身份去重为 411 通过／1 原有跳过，共 412 项，非一次完整批次、非全量回归。使用合成资料与离线服务替身，不代表真实模型质量。前端构建的统一入口是 [frontend/scripts/build.mjs](frontend/scripts/build.mjs)。Windows 打包工具在 [tools/packaging](tools/packaging)，需要另行准备与锁定依赖匹配的构建环境、前端构建结果和安装器所需资源。具体约定见 [发布与维护说明](RELEASE.md)。

## 数据保存与升级

Windows 安装版的数据位于安装目录，源码运行的数据默认位于仓库根目录：

下文的数据、模型和日志位置是运行时生成的路径，不是仓库文件链接。

| 内容 | 默认位置 |
|---|---|
| 模型服务配置及本机 API Key | `.env` |
| 关系数据与会话 | `data/zhilan.db` |
| 本地向量索引 | `data/qdrant/` |
| 上传的资料 | `data/uploads/` |
| 离线解析模型 | 安装版默认 `models/`；模型 HOME 被覆盖时以导入面板目标路径为准 |
| 应用日志 | `logs/zhilan.log` |

启动器日志默认位于安装目录 `start.log`，源码版位于 `client/start.log`。主日志位置不可写时，会尝试标准错误输出和系统临时目录下的 `zhilan-desktop-start.log`；退路也不可写时可能没有该行日志。

覆盖安装会保留默认的 `.env`、`data/` 和 `models/`。升级前先退出应用，完整备份配置、实际数据目录与实际模型目录；使用自定义路径时也要备份相应位置，再安装新版。跨版本数据迁移尚未在所有场景验证，不要将“保留文件”等同于“所有旧数据兼容性均已验证”。

不要把真实 `.env`、用户资料、索引、模型权重或日志提交到公开仓库。更换 Embedding 型号或维度后，需要为相关资料重建索引；只改配置不会自动把旧向量转换成新模型的向量。

## MCP 与联网功能排障

MCP **发现段**失败时，界面可能看不到原因。请查看安装目录或源码根目录下的 `logs/zhilan.log`，搜索 `MCP 包`。以下四类 warning 分别需要处理：

| 日志含义 | 可采取的操作 |
|---|---|
| 未许可：包未在 `MCP_ALLOW_SERVERS` | 核对包的 slug，并将需要使用的包加入本机 `.env` 的精确许可名单 |
| HTTP 未在 egress：未在 `MCP_ALLOW_EGRESS` | 确认该服务确实允许对外连接，再为该 HTTP 包添加外发许可 |
| 逐包发现失败 | 核对命令或地址、外部服务是否启动、所需依赖是否安装以及超时情况 |
| schema 超子集：工具参数结构不受支持 | 使用参数结构在受支持子集内的工具，或调整 MCP 服务提供的参数定义 |

`MCP_SERVERS` 使用 JSON 数组配置；空配置不会注册 MCP 工具。许可名单默认空，不接受通配许可。修改配置后重启应用。发现失败没有完整的界面解释；不要仅根据界面上的工具数量判断故障原因。

联网搜索默认关闭。`WEB_SSRF_ALLOW_ANY_HOST` 与 `WEB_SEARCH_BUDGET_SECONDS` 需要**改 `.env` 后重启**，设置页不能写入这两个键。前者默认 `false`，开启后允许访问私网或本机地址，**界面上看不出来**，允许此类访问时只有 WARNING 日志；一般保持关闭。后者默认 `20` 秒，设为 `0` 表示不限制一次搜索的抓取预算。其余联网配置见 [配置说明](CONFIGURATION.md)。

## 已知限制

- 当前回答质量尚未达到全部预定目标。已有模型复核仍发现事实错误、引用支持不足和否定语义错误；引用不等于事实保证，重要结论请查看原文。
- 安装包已在有 WebView2 的 Windows 环境验证；未在缺少 WebView2 的全新机器、所有重启安装分支和物理断网环境完成全面验证。安装程序携带离线 WebView2 运行时，也不能据此声称所有全新机器首启已通过。
- 不承诺 PDF、表格、幻灯片和图片的版式与文字识别完全保真。解析失败或部分缺页时，应查看提示并核对原文件。
- 相似回答缓存和实体增强检索默认关闭，其生产质量与净收益尚未完成充分验证。
- 本地 Qdrant 目录需要单进程使用；不要让多个实例或测试同时打开同一目录。
- 部分设置只能通过 `.env` 修改，数据目录搬迁尚未实现。联网设置支持保存；大模型搜索目前支持 MiMo 原生插件，需要兼容型号与可用凭据。
- 本地缓存、用量显示和运行检查各有范围；费用显示不替代模型厂商账单，Redis 状态项对应进程内兼容层，不表示连接了外部 Redis 服务。
- 历史测试仍有未解决失败，公开审计与定向通过记录不表示全仓测试或所有使用场景全部通过。跨平台桌面交付没有与 Windows 成品同等的验证。

## 目录与许可证

```text
backend/          后端、数据迁移、检索与测试
frontend/         桌面界面的 React/TypeScript 源码
client/           本地服务与桌面窗口启动器
tools/packaging/  Windows 构建、模型导入与许可证收集
tools/release/    公开内容审计及扫描器自查
README.md         项目入口
USAGE.md          安装与日常操作
CONFIGURATION.md  模型、数据与可选工具配置
ARCHITECTURE.md   系统结构与数据流
RELEASE.md        发布与维护流程
CHANGELOG.md      公开版本变更
.env.example      不含凭据的源码配置示例
```

源码采用 [Apache License 2.0](LICENSE)，著作权署名为 **Copyright 2026 bbaicao**。第三方组件声明见 [NOTICE](NOTICE)。运行时、模型权重和外部服务遵守各自的许可及使用条款，源码许可不替代这些条款。

欢迎提交可复现的问题或范围明确的改进。报告问题时请附版本、操作步骤和经过脱敏的错误信息；请勿上传 API Key、真实 `.env` 或私人资料。
