# 发布与维护说明

本文件说明如何维护当前公开源码以及托管发布。当前源码版本为 **v0.1.4**，文档日期 **2026 年 10 月 11 日**，公开预发布标识为 **v0.1.4-public.1**。本版使用独立发布批次的源码 ZIP、Windows 完整程序 ZIP 和安装器，实际公开状态与身份见[本版预发布页面](https://github.com/bbaicao/zhilan/releases/tag/v0.1.4-public.1)、标签及附件清单。旧成品保持原身份，详见 [发布附件说明](ATTACHMENTS.md)。

## 历史 v0.1.0-public.1 发布记录

[公开 GitHub 仓库](https://github.com/bbaicao/zhilan)与 [v0.1.0-public.1 预发布](https://github.com/bbaicao/zhilan/releases/tag/v0.1.0-public.1)已发布，以下记录仅描述旧版本。

发布标签固定对应提交 `72be7d0e8ce1db6dfbbfc843c85ad8927de5a17f`；654 件源码文件与线上树逐件一致。18 件线上附件均与本地文件核对大小及服务器 SHA256。GitHub 自动规范化中文附件名，因此中文说明使用 `ATTACHMENTS-zh-CN.md`，线上 `SHA256SUMS.txt` 按实际附件名生成；原始清单保留为 `SHA256SUMS-local-preparation.txt`。源码 ZIP 和随附准备文档保留标签时的原始快照，`main` 的本次文档更新登记实际发布状态，不改写发布标签。

## v0.1.4-public.1 公开预发布

上文的提交、654 件源码及 18 件线上附件仅描述 `v0.1.0-public.1` 历史快照。本版使用 `v0.1.4-public.1` 和独立批次 `public-0.1.4-release-20261011`，不改写旧标签或旧附件。新功能和验证限度见 [版本变更](CHANGELOG.md)。

本版按预发布／试用版交付，用户已授权完成提交、导出、核验与公开发布，`publication_authorized=true`。最终 `SOURCE.json` 登记 `git_head`、`git_dirty=false`、实际源码树及前端身份；`SOURCE-INVENTORY.json` 记录逐件长度与 SHA256，`release-manifest.json` 绑定实际附件和发布标签。源码包从确定的提交重新导出，并与当前字节、标签及清单核对；文件名保留导出工具约定的 `zhilan-source-0.1.4-candidate.zip`。程序及安装器与本批公开产品代码、前端和实际构建输入相符，沿用已核验的 0.1.4 二进制，不用旧版改名替代。每批使用独立 SHA256 清单，不覆盖旧标签、旧附件或旧证据。本轮没有新模型 ZIP，仍使用[旧版公开模型包](https://github.com/bbaicao/zhilan/releases/tag/v0.1.0-public.1)，保留原身份及许可限制。

旧定向产品测试为 190 passed / 1 failed，超大图片预期 413／实际 400 的旧失败保留；原安装证据只覆盖其绑定的旧成品。v0.1.1 在 2026-10-09 已重新构建本地程序和安装器，但未上传。v0.1.4 候选构建及冻结核验只证明具名范围，没有新安装、升级或卸载。全量回归按用户豁免未测，历史真实质量 FAIL、许可来源缺口以及全新机器、物理断网等限制继续保留。

## 仓库约定

既有前端构建及 44 项源码／组件控制通过记录保留。前一轮后端 346 项最后记录 339P／2F／4E／1S 保留为历史；本轮补修上传、测试合同与禁网夹具后，四个串行批次按身份去重为 411P／1S、共 412 项，没有失败或错误，不称一次全组或全量通过。详见 [版本变更](CHANGELOG.md)。`source_ready_to_publish`、`artifacts_ready_to_publish` 分别按最终源码和成品材料核验记录；`product_acceptance=NOT_PASS`，`publication_authorized=true`。材料准备及发布授权不取消旧产品失败、许可缺口或未测边界。

主分支使用 `main`，当前源码版本为 v0.1.4；发布标签 `v0.1.4-public.1` 绑定最终 SOURCE 记录的提交。旧 `v0.1.0` 和 `v0.1.0-public.1` 标签保留原身份。公开仓库不携带内部文档、开发现场、用户资料或原开发仓库历史。

后续变更优先通过范围明确的提交和审查推进。提交信息应说明行为变化，例如 `fix: preserve source references on failed generation`。产品修改需更新相关公开说明，并留下能验证行为的小测试或现有测试结果。

`.env.example` 是示例；真实 `.env`、资料、索引、模型权重、虚拟环境、构建目录、安装器资源和日志都不进入源码 Git 仓库。不要以 `git add -f` 绕过这些排除规则。

## 公开内容检查

默认扫描器读取 **Git 暂存区和所有引用可达的历史对象**；`--working-tree` 增加未暂存修改与非忽略新文件；`--source-dir` 检查明确导出目录；`--artifact-dir` 读取同批 SOURCE／清单，实际解压源码 ZIP 并核对逐件字节与许可身份。未选的附件检查标 Unverified。不会检查不可达 Git 对象或远程发布页。以下命令不改索引或提交：

```powershell
git status --short
git diff
node tools/release/precheck.mjs --working-tree
node tools/release/precheck-selfcheck.mjs
$Candidate = Read-Host '请输入尚不存在的候选输出目录绝对路径'
node tools/release/export-source.mjs --root . --out $Candidate
node tools/release/precheck.mjs --working-tree --source-dir "$Candidate\source" --artifact-dir $Candidate
```

发布前检查所有输出，确认相对链接及锚点、许可声明、文件清单和当前行为一致。导出器仅导出公开字节及显式核验的前端 dist，不含真实配置、内部证据、运行数据或安装器；目标必须是新目录。Windows 附件在清单中登记后还须单独核验实际版本、内嵌代码、前端与安装器输入。扫描器有启发式限制；通过不等于凭证或全部许可来源绝对无缺口，也不代表产品质量、安装验收或发布批准。

修改前端后，在 `frontend` 目录运行 `npm ci` 和 `node scripts/build.mjs`。修改后端后，使用独立的临时配置和数据目录运行受影响的测试。测试通过记录要注明测试范围，不能表述为完整模型质量或所有场景均通过。

## Windows 成品构建

`tools/packaging/` 提供 PyInstaller 入口、安装器定义、锁定依赖清单、模型清单与许可证收集工具。后端基本依赖与 Windows 构建环境用途不同，不能直接使用源码虚拟环境宣称得到完整安装包。

先安装 Python **3.13**、Node.js/npm 和 Inno Setup 6，并从 Microsoft 官方渠道取得 **WebView2 Evergreen x64 完整离线安装器**。本次前端使用 Node.js **24.16.0**；其他版本需满足锁定依赖的要求并另行验证。第三方资源按各自许可取得，源码仓库不附这些安装器。以下输入目录与工具路径按自己的机器填写，环境变量只在当前会话使用。

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

`build.ps1` 执行路径检查、依赖核对、PyInstaller 构建与冻结环境自检，桌面程序默认在 `dist\zhilan\zhilan.exe`，构建报告默认在 `build\reports\`。许可证收集结果默认在 `build\licenses-stage\`，由最后的 ISCC 编译装入安装包；常规安装包输出路径为 `dist\installer\zhilan-setup-0.1.4.exe`。本版已完成隔离构建，独立发布批次提供完整载荷 ZIP 与 `zhilan-setup-0.1.4-public-ready-20261011.exe`；程序和安装器实际版本为 0.1.4／数字 0.1.4.0。仅运行 `build.ps1` 不会生成安装包；文件名也不能代替实际版本核验。

许可证收集器按构建环境、锁定依赖和排除规则收集 Python 文本，并按实际前端目录的 npm 锁收集所有非开发依赖的许可全文。前端包缺失、名称或版本不符、缺少全文、空白全文或路径越界会中止收集；生成许可目录中的 `npm\00-package-manifest.json` 记录版本、registry 来源、锁及全文哈希，该清单是构建生成文件。保守清单可能包含类型包和仅用于构建的组件，不等同于前端 bundle 模块图或冻结模块全集。其 PASS 证明列出的缺件、版本和哈希断言成立，完整分发条件仍需对实际产物核对。

历史 v0.1.0 公开准备构建过 Windows 程序并验证安装；历史 v0.1.1 本地批次重新生成程序资源、编译安装器并实算哈希，实际版本为 0.1.1，冻结自检和载荷核对通过但没有运行新安装器。本版 v0.1.4 已单独完成隔离构建、冻结模块／前端／载荷核对，仍没有执行本版新安装、升级或卸载。没有在全新虚拟环境中从零安装浮动源码依赖，也没有完成缺少 WebView2 的全新机器验证。

构建后还需核对安装器声明的资源，执行安装、启动、升级保留数据、模型导入及真实文档解析验证。不要仅凭编译或 `--smoke` 通过宣布完整安装矩阵通过。

安装器使用原生许可页显示 Microsoft WebView2 Runtime 和 MinerU 全文及中文用户通知。交互安装需本人接受；静默安装也需先阅读 `INSTALLATION-TERMS.txt`，本人明确同意后在命令中加入参数，例如 `安装器.exe /ACCEPTTHIRDPARTYLICENSES=1`。缺少此参数时静默安装会中止。自动化不得替用户接受法律协议；没有完成安装的检查只能标为成品构建或展开产物验证。

## 托管发布步骤

1. 核对现有公开 GitHub 仓库、远程主分支及旧发布身份，确保操作目标正确。首次创建仓库时不要自动添加 README、LICENSE 或 `.gitignore`，以免与准备的初始提交冲突。
2. 在本机核对 `git log --oneline --all`、`git branch`、`git tag` 和扫描输出，确认只有准备公开的历史与版本标签。本次在初始快照后追加公开材料修订提交，没有改写旧提交或旧标签。
3. 本版已获得用户提交及发布授权。核对当前仓库远程地址，固定最终提交并重新导出源码及清单，再创建并推送绑定同一提交的 `v0.1.4-public.1` 与 `main`。保留旧标签，不使用强制推送。认证信息不写入命令示例、文档或日志。
4. 在未登录或可匿名访问的浏览器中打开仓库，核对 README 渲染、每个文档链接、Apache-2.0 许可证识别，以及源码目录和文件数。
5. 创建预发布时，仅上传最终提交导出的源码 ZIP、与最终产品代码核对的 Windows 完整载荷 ZIP、安装器、身份清单、说明、条款及 SHA256 清单。本版独立发布批次已准备这套 v0.1.4 成品；旧本地批次与开发侧候选不能替代。模型没有更新，链接旧版公开模型附件并保留原身份，不重复上传。不要把大体积二进制加入源码提交。本地检查不能代替分发方对许可条件的判断。
6. 记录实际仓库地址、提交和标签身份、附件 SHA256、公开访问核对结果以及仍未验证的场景。

配套公开模型 ZIP 含 **13 件模型及配置、11 件许可和来源文件**，约 **656 MB**。权重及配置的哈希与固定官方清单一致；五个直接来源的固定版本声明 Apache-2.0，包内保留六份完整模型卡、官方许可全文、逐件来源与转换/配置追加说明。集合内 Formula 配置有独立第三方上游。Layout 与 Formula 的原配置没有完成独立下载，身份由官方固定来源清单哈希与实算哈希绑定，不能写成已下载原文逐字比对。

旧的裸模型 ZIP 没有上述许可材料，不能拿它替代这份公开模型包。应用和命令行导入工具允许有限额的根说明及单层 `MODEL-LICENSES/*.txt`，仅导入清单绑定的模型；许可说明保留在分发 ZIP 中。不要将整个本机目录、缓存、运行日志或 `.git` 物理对象目录作为源码 ZIP 上传；本版在最终提交后的干净工作区使用公开导出器，额外绑定实际前端 dist，并解压核对逐件清单。源码许可不覆盖模型权重。

旧 v0.1.0-public.1 仓库与预发布页面的 HTTP、README、许可证识别及附件核对均是历史快照。本版发布另行核对最终提交、标签、公开访问、附件大小及服务器 SHA256；本地证据与线上结果分别登记。这些发布核对不代表当前版本的视觉验收、完整下载验证或完整产品回归。

## 质量与许可披露

本版本已有工程与 Windows 安装验证，同时真实模型回答质量仍未达到目标，存在引用支持不足、否定语义错误，以及全新机器等场景未测。发布说明应保持这些限制，不能将导入、构建或公开审计通过扩大为完整质量通过。

源码许可见 [LICENSE](LICENSE)，第三方声明见 [NOTICE](NOTICE)。模型权重、WebView2 和其他运行时各有独立许可及分发条件；发布成品需要附带实际分发组件的许可文本，不能仅用源码仓库的 Apache-2.0 声明替代。

`licenses-vendor` 的九份 `SOURCE.json` 记录具体版本、官方发行 SHA256、固定源码和正文材料哈希。七份主许可绑定对应版本的不可变提交，`proxy_tools` 绑定发布前源码；另补 cobble 内嵌 inflection 的原 MIT 许可、proxy_tools 取用 Werkzeug 的原 BSD-3-Clause 和作者通知。仍保留两项来源边界：`cobble` 主作品只有固定版本 README 的 BSD-2-Clause 声明，没有主 LICENSE 或版权原行，随包主正文明确是 prepared 模板；`proxy_tools` 同版源码/许可写 BSD、元数据写 MIT，原占位符和拼接异常保留。不能将这些状态改写为“九份全是无差异上游原件”。

MinerU 4.0.0 的随包 `LICENSE.md` 含标准 Apache-2.0 之外的附加条件，包括指定规模商业使用需另行授权及线上服务标识要求，使用和再分发须阅读实际全文。WebView2 遵守 Microsoft 的独立运行时条款，分发方也须本人审阅相关权利、通知与责任。本次 Runtime 已由原下载记录中的 Microsoft CDN 重定向、HTTP 200、长度和同批 SHA256 绑定到当前微软签名文件；原始起始请求 URL 未登记，最终 CDN 来源和哈希已保留。仅有微软签名时仍不能单独证明下载渠道。

2026-10-08 通过 npm 官方 registry 重新审计：运行依赖为 0 项；`postcss-selector-parser` 更新至 7.1.6、`source-map-js` 更新至 1.2.2 后，两项公告已从审计消失。全部依赖仍报告 5 个 high 受影响包，共同根因为 `braces 3.0.3`，涉及 Tailwind 3 的构建依赖链。本次没有迁移 Tailwind 主版本，也没有证明该公告可从当前桌面界面直接触发。仅以可信源码执行构建，不能将运行依赖审计为 0 写成全部依赖无安全问题。
