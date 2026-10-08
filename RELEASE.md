# 发布与维护说明

本文件说明如何维护当前公开源码快照以及完成实际托管发布。当前版本为 **v0.1.0**，文档日期 **2026 年 10 月 8 日**。[公开 GitHub 仓库](https://github.com/bbaicao/zhilan)与 [v0.1.0-public.1 预发布](https://github.com/bbaicao/zhilan/releases/tag/v0.1.0-public.1)已发布。当前附件名、校验方式及安装验证范围见 [发布附件说明](ATTACHMENTS.md)。

发布标签固定对应提交 `72be7d0e8ce1db6dfbbfc843c85ad8927de5a17f`；654 件源码文件与线上树逐件一致。18 件线上附件均与本地文件核对大小及服务器 SHA256。GitHub 自动规范化中文附件名，因此中文说明使用 `ATTACHMENTS-zh-CN.md`，线上 `SHA256SUMS.txt` 按实际附件名生成；原始清单保留为 `SHA256SUMS-local-preparation.txt`。源码 ZIP 和随附准备文档保留标签时的原始快照，`main` 的本次文档更新登记实际发布状态，不改写发布标签。

## 仓库约定

主分支使用 `main`，本次发布使用 `v0.1.0-public.1` 标签，应用版本仍为 v0.1.0。旧 `v0.1.0` 标签仅表示此前的初始快照，保留原身份，不用于本次 Release。公开仓库以清理后的产品快照作为起点，不携带内部文档、开发现场、用户资料或原开发仓库历史。

后续变更优先通过范围明确的提交和审查推进。提交信息应说明行为变化，例如 `fix: preserve source references on failed generation`。产品修改需更新相关公开说明，并留下能验证行为的小测试或现有测试结果。

`.env.example` 是示例；真实 `.env`、资料、索引、模型权重、虚拟环境、构建目录、安装器资源和日志都不进入源码 Git 仓库。不要以 `git add -f` 绕过这些排除规则。

## 公开内容检查

扫描器读取 **Git 暂存区和所有引用可达的历史对象**，不会读取尚未暂存的工作区修改、未跟踪文件、被忽略的本地附件，也不会检查不可达对象或远程发布页。先在仓库根目录核对准备公开的修改，再只暂存这些文件。以下以公开说明文件为例；源码修改也需要按实际范围逐件暂存：

```powershell
git status --short
git diff
git add -- README.md USAGE.md CONFIGURATION.md ARCHITECTURE.md RELEASE.md CHANGELOG.md NOTICE
git diff --cached --stat
node tools/release/precheck.mjs
node tools/release/precheck-selfcheck.mjs
git status --short
git ls-files
```

发布前检查所有输出，确认文档相对链接、许可声明、文件清单和当前行为一致。如果仍有准备发布的未暂存修改，先核对、暂存后重新审计；扫描通过不能证明这些修改也已检查。扫描器有范围与启发式限制；全绿后仍需人工检查首屏运行前提、SmartScreen 提示、MCP 排障说明以及架构文档内容。

修改前端后，在 `frontend` 目录运行 `npm ci` 和 `node scripts/build.mjs`。修改后端后，使用独立的临时配置和数据目录运行受影响的测试。测试通过记录要注明测试范围，不能表述为完整模型质量或所有场景均通过。

## Windows 成品构建

`tools/packaging/` 提供 PyInstaller 入口、安装器定义、锁定依赖清单、模型清单与许可证收集工具。后端基本依赖与 Windows 构建环境用途不同，不能直接使用源码虚拟环境宣称得到完整安装包。

先安装 Python **3.13**、Node.js/npm 和 Inno Setup 6，并从 Microsoft 官方渠道取得 **WebView2 Evergreen x64 完整离线安装器**。本次附件准备使用 Node.js **24.19.0**；其他版本需满足锁定前端依赖的要求并另行验证。第三方资源需要按各自许可取得，源码仓库不附这些安装器。以下输入目录与工具路径均按自己的机器填写，环境变量只在当前会话使用。

在仓库根目录、同一个已打开的 PowerShell 会话中按顺序执行。构建虚拟环境放在仓库外的新目录，不能复用源码环境或把它提交入库：

```powershell
$BuildVenv = Read-Host '请输入仓库外的新构建虚拟环境绝对路径'
py -3.13 -m venv $BuildVenv
$env:ZL_BUILD_PYTHON = Join-Path $BuildVenv 'Scripts\python.exe'
& $env:ZL_BUILD_PYTHON -m pip install -r tools\packaging\requirements-lock.txt

Push-Location frontend
npm ci
node scripts/build.mjs
Pop-Location

.\tools\packaging\build.ps1 -PathGateOnly
.\tools\packaging\build.ps1 -Console $false

$InnoRoot = Read-Host '请输入 Inno Setup 6 安装目录绝对路径'
$InnoCompiler = Join-Path $InnoRoot 'ISCC.exe'
$InnoLicense = Join-Path $InnoRoot 'license.txt'
$BuildPackages = Join-Path $BuildVenv 'Lib\site-packages'
$WebViewLicense = Read-Host '请输入 Microsoft 官方 WebView2 Runtime 分发条款 UTF-8 全文路径'
& $env:ZL_BUILD_PYTHON tools\packaging\collect_licenses.py --site-packages $BuildPackages --inno-license $InnoLicense --frontend-dir frontend --webview-license $WebViewLicense

$env:ZL_PKG_VENDOR = Read-Host '请输入官方 WebView2 完整离线安装器所在目录绝对路径'
# 该目录需要有 MicrosoftEdgeWebView2RuntimeInstallerX64.exe。
& $InnoCompiler tools\packaging\zhilan.iss
```

每一步都应检查错误和退出码；失败时先处理原因，再继续下一步。布尔参数 `-Console $false` 要在当前 PowerShell 会话中直接传递。构建脚本核对环境的 `pip freeze` 与锁定清单，依赖漂移时会中止；安装锁定清单并不等于已经证明环境逐字一致，不能通过跳过检查将产物登记为已验证。

`build.ps1` 执行路径检查、依赖核对、PyInstaller 构建与冻结环境自检，桌面程序在 `dist\zhilan\zhilan.exe`，构建报告在 `build\reports\`。许可证收集结果默认在 `build\licenses-stage\`，由最后的 ISCC 编译装入安装包；安装包在 `dist\installer\zhilan-setup-0.1.0.exe`。这些是构建生成的 Windows 路径，不是源码仓库文件。仅运行 `build.ps1` 不会生成安装包。

许可证收集器按构建环境、锁定依赖和排除规则收集 Python 文本，并按实际前端目录的 npm 锁收集所有非开发依赖的许可全文。前端包缺失、名称或版本不符、缺少全文、空白全文或路径越界会中止收集；生成许可目录中的 `npm\00-package-manifest.json` 记录版本、registry 来源、锁及全文哈希，该清单是构建生成文件。保守清单可能包含类型包和仅用于构建的组件，不等同于前端 bundle 模块图或冻结模块全集。其 PASS 证明列出的缺件、版本和哈希断言成立，完整分发条件仍需对实际产物核对。

此前公开准备已构建 Windows 程序；本次重新构建前端，七件输出与冻结成品逐字一致，运行依赖锁和程序源码已绑定，复用该程序并以新许可目录重新编译安装器。具体成品身份、安装验证范围与未测项以同版附件说明和 SHA256 清单为准。仍没有在全新虚拟环境中从零安装浮动源码依赖，也没有完成缺少 WebView2 的全新机器验证。

构建后还需核对安装器声明的资源，执行安装、启动、升级保留数据、模型导入及真实文档解析验证。不要仅凭编译或 `--smoke` 通过宣布完整安装矩阵通过。

安装器使用原生许可页显示 Microsoft WebView2 Runtime 和 MinerU 全文及中文用户通知。交互安装需本人接受；静默安装也需先阅读 `INSTALLATION-TERMS.txt`，本人明确同意后在命令中加入参数，例如 `安装器.exe /ACCEPTTHIRDPARTYLICENSES=1`。缺少此参数时静默安装会中止。自动化不得替用户接受法律协议；没有完成安装的检查只能标为成品构建或展开产物验证。

## 托管发布步骤

1. 由仓库所有者创建公开 GitHub 仓库。不要选择自动添加 README、LICENSE 或 `.gitignore`，以免与当前初始提交产生冲突。
2. 在本机核对 `git log --oneline --all`、`git branch`、`git tag` 和扫描输出，确认只有准备公开的历史与版本标签。本次在初始快照后追加公开材料修订提交，没有改写旧提交或旧标签。
3. 为当前仓库添加自己的远程地址，并推送 `main` 与 `v0.1.0-public.1`。不要以旧 `v0.1.0` 标签代替本次修订，也不要使用强制推送。远程地址和认证信息由仓库所有者管理，不将凭据写入命令示例、文档或日志。
4. 在未登录或可匿名访问的浏览器中打开仓库，核对 README 渲染、每个文档链接、Apache-2.0 许可证识别，以及源码目录和文件数。
5. 创建 `v0.1.0-public.1` 预发布时，将同批源码 ZIP、安装包、模型 ZIP、配套说明、第三方条款及 SHA256 清单作为独立发布附件；模型包按其独立许可和来源处理。不要把大体积二进制加入源码提交。发布前阅读下方仍保留的许可与质量披露；本地检查不能代替分发方对许可条件的判断。
6. 记录实际仓库地址、提交和标签身份、附件 SHA256、公开访问核对结果以及仍未验证的场景。

配套公开模型 ZIP 含 **13 件模型及配置、11 件许可和来源文件**，约 **656 MB**。权重及配置的哈希与固定官方清单一致；五个直接来源的固定版本声明 Apache-2.0，包内保留六份完整模型卡、官方许可全文、逐件来源与转换/配置追加说明。集合内 Formula 配置有独立第三方上游。Layout 与 Formula 的原配置没有完成独立下载，身份由官方固定来源清单哈希与实算哈希绑定，不能写成已下载原文逐字比对。

旧的裸模型 ZIP 没有上述许可材料，不能拿它替代这份公开模型包。应用和命令行导入工具允许有限额的根说明及单层 `MODEL-LICENSES/*.txt`，仅导入清单绑定的模型；许可说明保留在分发 ZIP 中。不要将整个本机目录、缓存、运行日志或 `.git` 物理对象目录作为源码 ZIP 上传；源码压缩包应使用当前已审计提交的 `git archive` 导出。源码许可不覆盖模型权重。

本次仓库与预发布页面均已上线。匿名仓库网页返回 HTTP 200 并包含 README 正文，GitHub 识别源码许可证为 Apache-2.0。匿名 API 遇到访问次数限制，源码逐件比对和发布附件 SHA256 核对使用所有者已授权接口；不能将这些检查扩大为所有浏览器的视觉验收或完整产品回归。后续发布仍须执行以上核对。

## 质量与许可披露

本版本已有工程与 Windows 安装验证，同时真实模型回答质量仍未达到目标，存在引用支持不足、否定语义错误，以及全新机器等场景未测。发布说明应保持这些限制，不能将导入、构建或公开审计通过扩大为完整质量通过。

源码许可见 [LICENSE](LICENSE)，第三方声明见 [NOTICE](NOTICE)。模型权重、WebView2 和其他运行时各有独立许可及分发条件；发布成品需要附带实际分发组件的许可文本，不能仅用源码仓库的 Apache-2.0 声明替代。

`licenses-vendor` 的九份 `SOURCE.json` 记录具体版本、官方发行 SHA256、固定源码和正文材料哈希。七份主许可绑定对应版本的不可变提交，`proxy_tools` 绑定发布前源码；另补 cobble 内嵌 inflection 的原 MIT 许可、proxy_tools 取用 Werkzeug 的原 BSD-3-Clause 和作者通知。仍保留两项来源边界：`cobble` 主作品只有固定版本 README 的 BSD-2-Clause 声明，没有主 LICENSE 或版权原行，随包主正文明确是 prepared 模板；`proxy_tools` 同版源码/许可写 BSD、元数据写 MIT，原占位符和拼接异常保留。不能将这些状态改写为“九份全是无差异上游原件”。

MinerU 4.0.0 的随包 `LICENSE.md` 含标准 Apache-2.0 之外的附加条件，包括指定规模商业使用需另行授权及线上服务标识要求，使用和再分发须阅读实际全文。WebView2 遵守 Microsoft 的独立运行时条款，分发方也须本人审阅相关权利、通知与责任。本次 Runtime 已由原下载记录中的 Microsoft CDN 重定向、HTTP 200、长度和同批 SHA256 绑定到当前微软签名文件；原始起始请求 URL 未登记，最终 CDN 来源和哈希已保留。仅有微软签名时仍不能单独证明下载渠道。

2026-10-08 通过 npm 官方 registry 重新审计：运行依赖为 0 项；`postcss-selector-parser` 更新至 7.1.6、`source-map-js` 更新至 1.2.2 后，两项公告已从审计消失。全部依赖仍报告 5 个 high 受影响包，共同根因为 `braces 3.0.3`，涉及 Tailwind 3 的构建依赖链。本次没有迁移 Tailwind 主版本，也没有证明该公告可从当前桌面界面直接触发。仅以可信源码执行构建，不能将运行依赖审计为 0 写成全部依赖无安全问题。
