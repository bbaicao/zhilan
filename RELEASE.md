# 发布与维护说明

本文件说明如何维护当前公开源码快照以及完成实际托管发布。当前版本为 **v0.1.0**，文档日期 **2026 年 10 月 7 日**。当前材料不包含已经核验的公开 GitHub 地址，也不宣称已经创建线上 Release。

## 仓库约定

主分支使用 `main`，首个公开版本使用 `v0.1.0` 标签。公开仓库以清理后的产品快照作为起点，不携带内部文档、开发现场、用户资料或原开发仓库历史。

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

先安装 Python **3.13**、Node.js/npm 和 Inno Setup 6，并从 Microsoft 官方渠道取得 **WebView2 Evergreen x64 完整离线安装器**。本机已使用 Node.js **24.16.0**、npm **11.13.0**；其他版本需满足锁定前端依赖的要求并另行验证。第三方资源需要按各自许可取得，源码仓库不附这些安装器。以下输入目录与工具路径均按自己的机器填写，环境变量只在当前会话使用。

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
& $env:ZL_BUILD_PYTHON tools\packaging\collect_licenses.py --site-packages $BuildPackages --inno-license $InnoLicense

$env:ZL_PKG_VENDOR = Read-Host '请输入官方 WebView2 完整离线安装器所在目录绝对路径'
# 该目录需要有 MicrosoftEdgeWebView2RuntimeInstallerX64.exe。
& $InnoCompiler tools\packaging\zhilan.iss
```

每一步都应检查错误和退出码；失败时先处理原因，再继续下一步。布尔参数 `-Console $false` 要在当前 PowerShell 会话中直接传递。构建脚本核对环境的 `pip freeze` 与锁定清单，依赖漂移时会中止；安装锁定清单并不等于已经证明环境逐字一致，不能通过跳过检查将产物登记为已验证。

`build.ps1` 执行路径检查、依赖核对、PyInstaller 构建与冻结环境自检，桌面程序在 `dist\zhilan\zhilan.exe`，构建报告在 `build\reports\`。许可证收集结果默认在 `build\licenses-stage\`，由最后的 ISCC 编译装入安装包；安装包在 `dist\installer\zhilan-setup-0.1.0.exe`。这些是构建生成的 Windows 路径，不是源码仓库文件。仅运行 `build.ps1` 不会生成安装包。

许可证收集器按构建环境、锁定依赖和排除规则收集文本，可能包含仅用于构建的组件；其 PASS 只证明脚本列出的缺件和哈希硬断言成立，不是实际随包组件全集或完整许可合规证明。发布前仍需对实际产物及未覆盖许可逐项核对。

本次文档复核只核对了上述命令与代码入口，**没有从全新环境重装依赖、重包或重新安装验证**。这些补充步骤不能记为新的构建或干净机器验收通过。

构建后还需核对安装器声明的资源，执行安装、启动、升级保留数据、模型导入及真实文档解析验证。不要仅凭编译或 `--smoke` 通过宣布完整安装矩阵通过。

## 托管发布步骤

1. 由仓库所有者创建公开 GitHub 仓库。不要选择自动添加 README、LICENSE 或 `.gitignore`，以免与当前初始提交产生冲突。
2. 在本机核对 `git log --oneline --all`、`git branch`、`git tag` 和扫描输出，确认只有准备公开的快照与版本标签。
3. 为当前仓库添加自己的远程地址，并推送 `main` 与 `v0.1.0`。远程地址和认证信息由仓库所有者管理，不将凭据写入命令示例、文档或日志。
4. 在未登录或可匿名访问的浏览器中打开仓库，核对 README 渲染、每个文档链接、Apache-2.0 许可证识别，以及源码目录和文件数。
5. 创建版本发布时，将安装包、配套说明和 SHA256 清单作为独立发布附件；模型包按其独立许可和来源处理。不要把大体积二进制加入源码提交。
6. 记录实际仓库地址、提交和标签身份、附件 SHA256、公开访问核对结果以及仍未验证的场景。

当前本地配套模型 ZIP 只作为安装验证材料保留，**尚未作为可公开上传的附件验收**。其官方 ONNX 模型说明要求继续遵守原始模型许可与署名；现有 ZIP 未附各模型的许可证全文和来源清单。完成逐文件来源、版本、哈希与许可对应核对之前，不上传这份 ZIP。源码许可不覆盖模型权重。

以上线上核对尚未执行时，应把状态写为“本地公开材料已准备”，不能写成“GitHub 已发布”。

## 质量与许可披露

本版本已有工程与 Windows 安装验证，同时真实模型回答质量仍未达到目标，存在引用支持不足、否定语义错误，以及全新机器等场景未测。发布说明应保持这些限制，不能将导入、构建或公开审计通过扩大为完整质量通过。

源码许可见 [LICENSE](LICENSE)，第三方声明见 [NOTICE](NOTICE)。模型权重、WebView2 和其他运行时各有独立许可及分发条件；发布成品需要附带实际分发组件的许可文本，不能仅用源码仓库的 Apache-2.0 声明替代。

`licenses-vendor` 的来源注记仍有验收边界：多数记录来自上游 `master` 或 `main` 分支，尚未与所列包版本的不可变提交逐件绑定；`cobble` 是依据上游声明整理的 BSD 模板，不能称为抓取的上游原文；`proxy_tools` 保留的上游文本与 wheel 声明不同，原文也有拼接异常。现有许可正文保留原样，后续需要核对确切版本的官方来源及差异，不能把收集器输出当作这些问题已经解决。
