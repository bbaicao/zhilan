<#
.SYNOPSIS
    知澜桌面端打包脚本（阶段 6 · 条目 6.3 交付物 3）。

.DESCRIPTION
    一条命令完成：核对锁定依赖 → PyInstaller 构建 onedir 产物 → 自动跑 `zhilan.exe --smoke`
    → 落构建日志、自检输出与产物 SHA256 到证据目录。

    构建环境：由环境变量 ZL_BUILD_PYTHON 指定（**只读使用**——本脚本不装包、不升级、不删包）。
    产物落仓库根 dist\zhilan\，中间物落仓库根 build\（两者都在 E 盘，且已被 .gitignore 忽略）。

    失败口径（**刻意做硬**）：
    - 冻结安全路径闸门未通过（新增裸路径 / 扫描器缺失 / 启动失败）→ 退出码 4，
      且**排在资源预检、L0 依赖核对与 L1 PyInstaller 之前**（条目 6.2c）；
    - 依赖漂移（当前环境与 requirements-lock.txt 不一致）→ 退出码 2，不出包；
    - PyInstaller 构建失败 → 退出码 1；
    - `--smoke` 退出码非 0 → 退出码 3 并打印完整自检输出。
    最后一条是条目 6.3 的门禁：`--smoke` 不通过则不得进入 6.4。
    `-SkipDriftCheck` 只影响 L0，**不能**绕过路径闸门。

    ⚠️ `回归修复` 起：L2 的判据**主源** = `--smoke-out` 写出的 JSON 结果文件
    （`frozen` / `dist_hit` / `ini_hit` / `dir_hit` / `failures_count`），**不依赖 stdout**——
    `console=False` 时冻结态 `sys.stdout is None`，stdout 正则判据会整体失效。
    stdout 采集件照旧落证据目录，但只作**旁证**（`frozen=True` 自 070 起也纳入门禁）。

.PARAMETER UpdateFixed
    把 4 个**固定名**证据件（`回归检查-产物SHA256.txt` / `-smoke输出.txt` / `-产物清单.txt` / `-构建日志.log`）
    前进到本次构建。**默认不写**——这是刻意的（见下方「固定名证据件」一节）：全量构建曾静默覆写
    6.3 已验收的证据，而 6.4/6.5/6.6 还要反复构建。本次构建的证据一律另有时间戳件。

.PARAMETER EvidenceTag
    时间戳证据件的名字前缀（默认 `回归检查`）。固定名四条不受影响。后续条目传自己的标签，
    例如 `-EvidenceTag 回归检查`，文件名即自明"哪次构建属于哪个条目"。

.EXAMPLE
    pwsh -File tools\packaging\build.ps1
    pwsh -File tools\packaging\build.ps1 -SkipDriftCheck     # 明知环境有偏差时（会在日志里留痕）
    pwsh -File tools\packaging\build.ps1 -PathGateOnly       # 只跑路径闸门（不构建、不 smoke）
    pwsh -File tools\packaging\build.ps1 -UpdateFixed        # 显式把固定名证据件前进到本次构建
    pwsh -File tools\packaging\build.ps1 -EvidenceTag 回归检查 # 时间戳件用 6.4 的前缀命名
#>
[CmdletBinding()]
param(
    # 跳过「当前环境 vs requirements-lock.txt」核对。默认不跳——「可复跑」的定义就在这里。
    [switch]$SkipDriftCheck,
    # 只跑「冻结安全路径闸门」后退出（0 = 通过 / 4 = 未通过）：不构建、不 smoke、不动 dist/build。
    [switch]$PathGateOnly,
    # 把固定名证据件（回归检查-*）前进到本次构建。默认不写（见文件头 .PARAMETER UpdateFixed）。
    [switch]$UpdateFixed,
    # **时间戳**证据件的名字前缀（默认 `回归检查`）。固定名四条**不受它影响**，永远是 `回归检查-*`。
    # 6.4/6.5/6.6 各自构建时传自己的标签（如 `-EvidenceTag 回归检查`），
    # 这样「哪次构建属于哪个条目」在文件名上就是自明的，不必回头翻日志。
    [string]$EvidenceTag = 'S6-6.3',
    # 打包环境解释器：**走环境变量**——本机绝对路径不入库（见「本机路径零容忍」口径）。
    # 未设置时由下方前置检查给出中文指引并以退出码 2 中止。
    [string]$Python = $env:ZL_BUILD_PYTHON,
    # 调试版留控制台（可排障）；发布版传 $false 隐藏控制台（6.5 切换，切换前后都要过 --smoke）。
    [bool]$Console = $true
)

$ErrorActionPreference = 'Stop'

# ======================================================================================
# 0 · 钉住编码 —— 回归修复：中文日志在 GBK 下编解码会乱码甚至直接打断脚本。
#     **必须在任何 py / exe 调用之前设置**，也会被 PyInstaller 与 zhilan.exe 子进程继承。
#     ⚠️ 只设 PYTHONIOENCODING 不够：**冻结态 exe 的 stdout 是走控制台代码页的**，
#     PowerShell 默认按 GBK 解码 → 证据文件里会出现替换字符（U+FFFD）。
#     实测：首版 build.ps1 只设了 PYTHONIOENCODING，采集到的 smoke 输出 852 字节里
#     有 31 个无效字节（`[smoke] 检查 22 个模块` 变成 `[smoke] ��� 22 ��ģ��`）。
#     故这里同时钉控制台输出编码。判据：证据文件按 UTF-8 解码后 U+FFFD 计数为 0。
# ======================================================================================
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
# 光设 .NET 层不够：子进程（python / 冻结态 exe）写 stdout 用的是**控制台活动代码页**
# （中文 Windows 上是 936）。活动代码页与 [Console]::OutputEncoding 不一致时，
# 两端一个按 cp936 编、一个按 UTF-8 解，就得到上面那种 `��� 22 ��ģ��`。
# 故这里再把控制台活动代码页切到 65001（同一进程设置，本 pwsh 里的子进程一律继承）。
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { Write-Warning "设置 Console.OutputEncoding 失败：$($_.Exception.Message)" }
try { [Console]::InputEncoding  = [System.Text.UTF8Encoding]::new($false) } catch { }
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try { & "$env:SystemRoot\System32\chcp.com" 65001 | Out-Null } catch { Write-Warning "chcp 65001 失败（继续）" }

# ======================================================================================
# 1 · 路径常量
# ======================================================================================
$PackagingDir = $PSScriptRoot
$RepoRoot = (Resolve-Path (Join-Path $PackagingDir '..\..')).Path
$SpecFile = Join-Path $PackagingDir 'zhilan.spec'
$LockFile = Join-Path $PackagingDir 'requirements-lock.txt'

$DistRoot = Join-Path $RepoRoot 'dist'
$WorkRoot = Join-Path $RepoRoot 'build'
$AppDir = Join-Path $DistRoot 'zhilan'
$ExePath = Join-Path $AppDir 'zhilan.exe'

$EvidenceDir = Join-Path $RepoRoot 'build\reports'
if (-not (Test-Path -LiteralPath $EvidenceDir)) { New-Item -ItemType Directory -Path $EvidenceDir -Force | Out-Null }

$Script:BuildStartedAt = Get-Date
$Stamp = $Script:BuildStartedAt.ToString('yyyyMMdd-HHmmss')
# `-PathGateOnly` 只跑闸门、不构建，日志另起名——否则证据目录里会多出一份**看着像**
# 6.3 构建日志、实则没有 PyInstaller / SHA256 / smoke 的记录，后人核对时容易误读。
$LogPrefix = if ($PathGateOnly) { 'S6-6.2c-路径闸门' } else { "$EvidenceTag-构建日志" }
$FullBuildLog = Join-Path $EvidenceDir "$LogPrefix-$Stamp.log"
$SmokeOut = Join-Path $EvidenceDir "$EvidenceTag-smoke输出-$Stamp.txt"
# `--smoke-out` 的 JSON 结果文件（回归修复）：**不依赖 stdout** 的判据主源。
# `console=False` 时冻结态 `sys.stdout is None` ⇒ stdout 采集件只剩空壳，L2 判据改吃这份 JSON。
$SmokeJsonFile = Join-Path $EvidenceDir "$EvidenceTag-smoke结果-$Stamp.json"
$HashFile = Join-Path $EvidenceDir "$EvidenceTag-产物SHA256-$Stamp.txt"
$ManifestFile = Join-Path $EvidenceDir "$EvidenceTag-产物清单-$Stamp.txt"

# 本次构建的证据**一律**落时间戳件（上面 4 个 `-<stamp>` 变量），避免往仓库里堆 MB 级日志。
#
# 「固定名证据件」四条 = 下面这组：它们是**条目 6.3 的验收绑定**（证据目录里被 6.3 证据件点名引用）。
# ⚠️ 2026-09-20 实测坑（条目 6.4 §5 坑 1 / §2.7）：本脚本原先**每次构建都无条件覆写**这四件，
#    而 6.4 / 6.5 / 6.6 还要反复构建 ⇒ 上一轮已验收的证据会被**静默改掉**（改完还不报错）。
#    处置：默认**不写**，只有显式传 `-UpdateFixed` 才前进（有意的动作，不是构建的副作用）。
#    核验还原的办法见 6.4 开工包 §0.2（备份 + 逐件比对 sha256）。
$BuildLogSummary = Join-Path $EvidenceDir 'S6-6.3-构建日志.log'
$SmokeOutFixed = Join-Path $EvidenceDir 'S6-6.3-smoke输出.txt'
$HashFileFixed = Join-Path $EvidenceDir 'S6-6.3-产物SHA256.txt'
$ManifestFixed = Join-Path $EvidenceDir 'S6-6.3-产物清单.txt'

$Script:Step = 0
function Write-Head([string]$Text) {
    $Script:Step++
    $line = ('=' * 78)
    Write-Host ''
    Write-Host $line
    Write-Host ("[{0}] {1}" -f $Script:Step, $Text)
    Write-Host $line
}
function Write-Ok([string]$Text) { Write-Host "[ OK ] $Text" }
function Write-Bad([string]$Text) { Write-Host "[FAIL] $Text" }

function Invoke-Native {
    <# 跑原生程序：控制台实时可见，同时**逐行**并入日志缓冲。返回退出码（纯标量）。 #>
    param([string]$File, [string[]]$Arguments)
    $Script:LogBuffer.Add(('$ "{0}" {1}' -f $File, ($Arguments -join ' ')))
    & $File @Arguments 2>&1 | ForEach-Object {
        $line = if ($null -eq $_) { '' } else { $_.ToString() }
        $Script:LogBuffer.Add($line)
        Write-Host $line
    }
    $code = $LASTEXITCODE
    $Script:LogBuffer.Add("[exit code: $code]")
    return $code
}
$Script:LogBuffer = New-Object System.Collections.Generic.List[string]

function Save-Log {
    param([string]$Path, [string[]]$Lines)
    $text = ((@($Lines) | ForEach-Object { [string]$_ }) -join "`n") + "`n"
    [System.IO.File]::WriteAllText($Path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

function Save-Fixed {
    <# 固定名证据件：默认**不写**，只有 `-UpdateFixed` 才前进（见上方注释与文件头 .PARAMETER）。
       返回是否写了，便于在日志里如实留痕——「这次有没有动 6.3 的验收证据」必须可查。 #>
    param([string]$Path, [string[]]$Lines)
    if (-not $UpdateFixed) { return $false }
    Save-Log -Path $Path -Lines $Lines
    return $true
}

function Stop-WithLog {
    <# 非零退出前先把**全量日志落盘**：失败时的原始输出才是排查依据
       （例如 spec 里的构建期断言会把命中清单直接打在 PyInstaller 输出里）。 #>
    param([string]$Message)
    Write-Bad $Message
    Save-Log -Path $FullBuildLog -Lines $Script:LogBuffer
    Write-Host "构建日志：$FullBuildLog"
}

function Get-Sha256Hex([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}
function Get-DirSize([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return 0 }
    return (Get-ChildItem -LiteralPath $Path -Recurse -File -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum).Sum
}
function Format-Size([double]$Bytes) {
    if ($Bytes -ge 1GB) { return ('{0:n2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:n1} MB' -f ($Bytes / 1MB)) }
    return ('{0:n0} KB' -f ($Bytes / 1KB))
}

$Script:LogBuffer.Add("知澜打包构建日志（阶段 6 · 条目 6.3）")
$Script:LogBuffer.Add("开始时间   ：$($Script:BuildStartedAt.ToString('yyyy-MM-dd HH:mm:ss'))")
$Script:LogBuffer.Add("仓库根     ：$RepoRoot")
$Script:LogBuffer.Add("打包环境   ：$Python")
$Script:LogBuffer.Add("PYTHONIOENCODING=$env:PYTHONIOENCODING  PYTHONUTF8=$env:PYTHONUTF8")
$Script:LogBuffer.Add("dist 根    ：$DistRoot")
$Script:LogBuffer.Add("build 根   ：$WorkRoot")
$Script:LogBuffer.Add("证据目录   ：$EvidenceDir")
$Script:LogBuffer.Add("PowerShell ：$($PSVersionTable.PSVersion)  (.NET $([System.Environment]::Version))")

try {
    # ==================================================================================
    Write-Head '前置检查：打包环境与必需资源'
    # ==================================================================================
    if (-not $Python) {
        Write-Host ''
        Write-Host '未设置打包环境解释器。请先设置环境变量 ZL_BUILD_PYTHON，再重跑本脚本：'
        Write-Host ''
        Write-Host '    $env:ZL_BUILD_PYTHON = ''<构建专用 venv>\Scripts\python.exe'''
        Write-Host '    pwsh -File tools\packaging\build.ps1'
        Write-Host ''
        Write-Host '说明：该 venv 必须按 tools\packaging\requirements-lock.txt 锁定依赖（Python 3.13 大版本）。'
        Write-Host '      本脚本只读使用它——不装包、不升级、不删包。'
        Write-Host '      （也可以直接传参：pwsh -File tools\packaging\build.ps1 -Python <路径>）'
        Write-Host ''
        exit 2
    }
    if (-not (Test-Path -LiteralPath $Python)) {
        Write-Bad "ZL_BUILD_PYTHON 指向的解释器不存在：$Python"
        Write-Host '请核对路径后重试（示例形态：<构建专用 venv>\Scripts\python.exe）。'
        exit 2
    }

    # ==================================================================================
    Write-Head 'L-1 · 冻结安全路径闸门（条目 6.2c：新增裸路径 = 构建期红灯）'
    # ==================================================================================
    # 位置是刻意的：**在资源预检、L0 依赖核对、L1 PyInstaller 之前**。
    # 闸门只依赖 Python + stdlib，故即使分支副本里没有前端产物、未装 PyInstaller，
    # 违规也必须先在这里被挡住——否则「构建期红灯」只是句空话。
    # 退出码 4 专用于本条；`-SkipDriftCheck` 只影响 L0，**不能**绕过这里。
    $PathGateScript = Join-Path $RepoRoot 'backend\scripts\check_frozen_paths.py'
    if (-not (Test-Path -LiteralPath $PathGateScript)) {
        Write-Bad "冻结安全路径闸门：扫描器不存在 —— $PathGateScript"
        $Script:LogBuffer.Add("[L-1] 扫描器不存在：$PathGateScript（缺失同样中止，不能当通过）")
        Save-Log -Path $FullBuildLog -Lines $Script:LogBuffer
        exit 4
    }
    $gateCode = -1
    try {
        $gateCode = Invoke-Native -File $Python -Arguments @('-B', $PathGateScript, '--root', $RepoRoot)
    }
    catch {
        $Script:LogBuffer.Add("[L-1] 扫描器启动失败：$($_ | Out-String)")
        Write-Bad "冻结安全路径闸门：扫描器启动失败（$($_.Exception.Message)）"
        $gateCode = -1
    }
    if ($gateCode -ne 0) {
        Write-Bad "冻结安全路径闸门未通过（退出码 $gateCode）—— 新增裸路径必须先改成统一锚点，或按流程申请例外"
        $Script:LogBuffer.Add("[L-1] 结论：**未通过**（退出码 $gateCode），中止构建（未跑 pip / PyInstaller / smoke）。")
        Save-Log -Path $FullBuildLog -Lines $Script:LogBuffer
        exit 4
    }
    Write-Ok '冻结安全路径闸门通过（白名单外零命中）'
    $Script:LogBuffer.Add('[L-1] 结论：通过（白名单外零命中）。')
    if ($PathGateOnly) {
        Write-Ok '仅路径闸门（-PathGateOnly）：通过，退出码 0 —— 这不等于完整构建成功，也不是进 6.4 的 smoke 证据'
        $Script:LogBuffer.Add('[L-1] -PathGateOnly：闸门通过后即退出（未构建、未 smoke、未动 dist/build）。')
        Save-Log -Path $FullBuildLog -Lines $Script:LogBuffer
        exit 0
    }
    foreach ($required in @($SpecFile, $LockFile,
                           (Join-Path $RepoRoot 'frontend\dist\index.html'),
                           (Join-Path $RepoRoot 'backend\alembic.ini'),
                           (Join-Path $RepoRoot 'backend\alembic\versions'))) {
        if (-not (Test-Path -LiteralPath $required)) { throw "缺少必需资源：$required" }
    }
    $pyVersion = (& $Python -c "import sys; print(sys.version.split()[0])").Trim()
    Write-Ok "解释器 $pyVersion @ $Python"
    $Script:LogBuffer.Add("[前置检查] python=$pyVersion；spec / lock / frontend-dist / alembic 均存在")

    # ==================================================================================
    Write-Head 'L0 · 依赖锁定核对（当前环境 vs requirements-lock.txt）'
    # ==================================================================================
    $lockLines = Get-Content -LiteralPath $LockFile -Encoding UTF8 |
        Where-Object { $_ -match '\S' -and -not $_.StartsWith('#') } |
        ForEach-Object { $_.Trim() }
    $liveLines = (& $Python -m pip freeze) | Where-Object { $_ -match '\S' } | ForEach-Object { $_.Trim() }
    $lockOnly = (Compare-Object -ReferenceObject $lockLines -DifferenceObject $liveLines |
                 Where-Object { $_.SideIndicator -eq '<=' } | ForEach-Object { $_.InputObject })
    $liveOnly = (Compare-Object -ReferenceObject $lockLines -DifferenceObject $liveLines |
                 Where-Object { $_.SideIndicator -eq '=>' } | ForEach-Object { $_.InputObject })
    $Script:LogBuffer.Add("[依赖核对] 锁定 $($lockLines.Count) 行 / 环境 $($liveLines.Count) 行")
    if ($lockOnly) { $Script:LogBuffer.Add("[依赖核对] 仅锁定文件有：$($lockOnly -join '; ')") }
    if ($liveOnly) { $Script:LogBuffer.Add("[依赖核对] 仅环境有　　：$($liveOnly -join '; ')") }
    if ($lockOnly -or $liveOnly) {
        if ($SkipDriftCheck) {
            Write-Bad '依赖与锁定文件不一致（-SkipDriftCheck 已显式跳过，日志已留痕）'
            $Script:LogBuffer.Add('[依赖核对] 结论：**漂移**，但调用方传了 -SkipDriftCheck，继续。')
        }
        else {
            Write-Bad '依赖与锁定文件不一致 —— 中止（「可复跑」的前提是环境与锁定件一致）'
            $Script:LogBuffer.Add('[依赖核对] 结论：**漂移**，中止构建。')
            throw 'DEPENDENCY_DRIFT'
        }
    }
    else {
        Write-Ok "锁定件与当前环境逐字一致（$($lockLines.Count) 项）"
        $Script:LogBuffer.Add('[依赖核对] 结论：一致。')
    }

    # ==================================================================================
    Write-Head 'L1 · PyInstaller 构建（onedir）'
    # ==================================================================================
    # 依赖锁定已在 L0 核对；这里只把产物/中间物落点交给 spec（spec 缺省值相同）。
    $env:ZL_DISTPATH = $DistRoot
    $env:ZL_WORKPATH = $WorkRoot
    $env:ZL_CONSOLE = if ($Console) { '1' } else { '0' }
    Write-Host "console=$env:ZL_CONSOLE  ZL_DISTPATH=$env:ZL_DISTPATH  ZL_WORKPATH=$env:ZL_WORKPATH"

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $rc = Invoke-Native -File $Python -Arguments @(
        '-m', 'PyInstaller', '--noconfirm', '--clean',
        '--distpath', $DistRoot, '--workpath', $WorkRoot,
        $SpecFile
    )
    $sw.Stop()
    $buildSeconds = $sw.Elapsed.TotalSeconds
    $Script:LogBuffer.Add(("[L1] PyInstaller 退出码 {0}，耗时 {1:n1}s" -f $rc, $buildSeconds))
    if ($rc -ne 0 -or -not (Test-Path -LiteralPath $ExePath)) {
        $Script:LogBuffer.Add("[L1] 结论：**构建失败**（退出码 $rc，产物存在=$([bool](Test-Path -LiteralPath $ExePath))）。")
        Stop-WithLog "构建失败（PyInstaller 退出码 $rc；产物存在=$([bool](Test-Path -LiteralPath $ExePath))）"
        exit 1
    }
    $exeSize = (Get-Item -LiteralPath $ExePath).Length
    $appSize = Get-DirSize $AppDir
    Write-Ok "产物 $ExePath"
    Write-Ok "zhilan.exe $(Format-Size $exeSize) / dist\zhilan 合计 $(Format-Size $appSize)"

    # 产物清单（文件级 + 体积），供 6.5 圈定「随包分发范围」
    # 注意：显式转成 string[]——`Save-Log` 的参数是 `IEnumerable[string]`，
    # 直接传 `System.Object[]` 会被 PowerShell 拒绝（实测报参数转换失败，可在构建**成功之后**炸掉脚本）。
    $manifest = [string[]]@(Get-ChildItem -LiteralPath $AppDir -Recurse -File | Sort-Object FullName |
        ForEach-Object { ($_.FullName.Substring($AppDir.Length + 1)) + '  ' + $_.Length })
    Save-Log -Path $ManifestFile -Lines $manifest
    $fixedWritten = Save-Fixed -Path $ManifestFixed -Lines $manifest
    $Script:LogBuffer.Add("[L1] 产物清单 $($manifest.Count) 个文件 → $(Split-Path -Leaf $ManifestFile)；固定名前进=$fixedWritten")

    # ==================================================================================
    Write-Head 'L2 · 冻结态自检 zhilan.exe --smoke（条目 6.3 门禁）'
    # ==================================================================================
    $swSmoke = [System.Diagnostics.Stopwatch]::StartNew()
    $smokeLines = New-Object System.Collections.Generic.List[string]
    $smokeLines.Add(('$ "{0}" --smoke --smoke-out "{1}"' -f $ExePath, $SmokeJsonFile))
    & $ExePath --smoke --smoke-out $SmokeJsonFile 2>&1 | ForEach-Object {
        $line = if ($null -eq $_) { '' } else { $_.ToString() }
        $smokeLines.Add($line)
        Write-Host $line
    }
    $smokeCode = $LASTEXITCODE
    $swSmoke.Stop()
    $smokeLines.Add("[exit code: $smokeCode]")
    Save-Log -Path $SmokeOut -Lines ([string[]]$smokeLines)
    $null = Save-Fixed -Path $SmokeOutFixed -Lines ([string[]]$smokeLines)
    $Script:LogBuffer.AddRange($smokeLines)
    $smokeText = ($smokeLines -join "`n")

    # 三项资源命中判定（`回归修复` 起：**判据主源 = --smoke-out 的 JSON 结果文件**）：
    # `console=False` 时冻结态 `sys.stdout is None` ⇒ print 全部静默丢弃，stdout 采集件只剩空壳，
    # 原先「只看 stdout ASCII 正则」的判据会**整体失效**（fail-closed 红灯，但构建再也过不了）。
    # 故 070 起改吃 launcher 写的结构化 JSON（与 stdout 无关），stdout 正则降级为**旁证**仍照旧采集，
    # 供人工核对与历史对照（6.3/6.4/6.5 证据件里的旧读数就是它）。
    $smokeJsonOk = $false
    $hitDist = $false; $hitIni = $false; $hitDir = $false; $frozenTrue = $false; $zeroFail = $false
    if (Test-Path -LiteralPath $SmokeJsonFile) {
        try {
            $smokeJson = Get-Content -LiteralPath $SmokeJsonFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $hitDist = [bool]$smokeJson.dist_hit
            $hitIni = [bool]$smokeJson.ini_hit
            $hitDir = [bool]$smokeJson.dir_hit
            $frozenTrue = [bool]$smokeJson.frozen
            $zeroFail = (([int]$smokeJson.failures_count -eq 0) -and ($smokeJson.result -eq 'pass'))
            $smokeJsonOk = $true
        } catch {
            $Script:LogBuffer.Add("[L2] smoke 结果 JSON 解析失败：$($_.Exception.Message)")
        }
    }
    if (-not $smokeJsonOk) {
        $Script:LogBuffer.Add('[L2] 未取得 --smoke-out 的 JSON 结果文件（或解析失败）→ 判据主源缺失，保守判为未通过')
        Stop-WithLog "未取得 smoke 结果 JSON（$SmokeJsonFile）—— 070 起它是 L2 判据主源，缺失不能当通过"
        exit 3
    }
    # 旁证（非判据）：**只看 ASCII 词汇**的正则照旧采集——中文前缀会因控制台代码页变形，
    # 而 True/False 与路径名永远是 ASCII；即使中文显示为乱码，旁证也不会被连带误判。
    $ascii = -join ($smokeText.ToCharArray() | Where-Object { [int]$_ -lt 128 })
    $rxHitDist = [bool]($ascii -match 'frontend/dist\s*.*?:\s*True')
    $rxHitIni = [bool]($ascii -match '(?m)^\[smoke\].{0,12}alembic\.ini\s*.*?:\s*True')
    $rxHitDir = [bool]($ascii -match '(?m)^\[smoke\].{0,12}alembic\s*.*?:\s*True')
    $Script:LogBuffer.Add("[L2] 旁证(stdout 正则) 命中 dist=$rxHitDist ini=$rxHitIni dir=$rxHitDir —— 判据以 JSON 为准")

    Write-Host ''
    Write-Host ("--smoke 退出码={0}，耗时={1:n1}s（判据主源 = 结果 JSON）" -f $smokeCode, $swSmoke.Elapsed.TotalSeconds)
    Write-Host ("三项资源命中：frontend/dist={0}  alembic.ini={1}  alembic/={2}  frozen=True={3}" -f `
        $hitDist, $hitIni, $hitDir, $frozenTrue)
    $Script:LogBuffer.Add(("[L2] 退出码={0} 耗时={1:n1}s 命中(JSON) dist={2} ini={3} dir={4} frozen={5}" -f `
        $smokeCode, $swSmoke.Elapsed.TotalSeconds, $hitDist, $hitIni, $hitDir, $frozenTrue))

    if ($smokeCode -ne 0) {
        Stop-WithLog "zhilan.exe --smoke 退出码 $smokeCode —— 门禁未通过，不得进入 6.4"
        exit 3
    }
    if (-not ($hitDist -and $hitIni -and $hitDir)) {
        $Script:LogBuffer.Add("[L2] 资源命中 dist=$hitDist ini=$hitIni dir=$hitDir → 门禁未通过")
        Stop-WithLog '自检退出码为 0，但三项资源未全部命中（结果 JSON）—— 门禁未通过'
        exit 3
    }
    if (-not $frozenTrue) {
        $Script:LogBuffer.Add('[L2] 结果 JSON 的 frozen 不为真 → 门禁未通过（070 起 frozen=True 纳入门禁）')
        Stop-WithLog '自检未确认 frozen=True —— 疑似在非冻结环境跑自检，门禁未通过'
        exit 3
    }
    if (-not $zeroFail) {
        $Script:LogBuffer.Add('[L2] 结果 JSON 的 failures_count 非 0（或 result 非 pass）→ 门禁未通过')
        Stop-WithLog '自检退出码为 0，但结果 JSON 显示存在失败项 —— 门禁未通过'
        exit 3
    }
    # 采集自证：证据文件必须能被 UTF-8 无歧义解码（替换字符计数为 0）。
    # 否则「证据」本身是坏数据——一行乱码就足以让事后核对失效（首版就在这儿栽过）。
    # ⚠️ 读的是**本次构建的时间戳件** $SmokeOut：固定名件默认不写了（-UpdateFixed 才写），
    #    拿它做判据会在默认路径下读到**上一轮**的文件 —— 那就成了「用旧证据给新构建背书」。
    $rawBytes = [System.IO.File]::ReadAllBytes($SmokeOut)
    $decoded = [System.Text.Encoding]::UTF8.GetString($rawBytes)
    $badChars = ($decoded.ToCharArray() | Where-Object { [int]$_ -eq 0xFFFD }).Count
    if ($badChars -ne 0) {
        $Script:LogBuffer.Add("[L2] 采集自证：替换字符 $badChars 个 → 失败")
        Stop-WithLog "smoke 证据文件含 $badChars 个替换字符（U+FFFD）——采集编码不对，判为失败"
        exit 3
    }
    Write-Ok "zhilan.exe --smoke 退出码 0，结果 JSON：三项资源全部命中 + frozen=True + 失败 0，证据文件编码干净（0 个替换字符）"

    # ==================================================================================
    Write-Head 'L3 · 产物 SHA256'
    # ==================================================================================
    $exeHash = Get-Sha256Hex $ExePath
    $hashLines = @(
        "知澜打包产物哈希（阶段 6 · 条目 6.3）",
        "生成时间   ：$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
        "构建环境   ：$Python",
        "Python     ：$pyVersion",
        "PyInstaller：$((& $Python -m PyInstaller --version).Trim())",
        "命令       ：pwsh -File tools\packaging\build.ps1",
        "",
        "构建耗时   ：$([math]::Round($buildSeconds, 1)) s（PyInstaller 阶段，不含 --smoke）",
        "--smoke    ：$([math]::Round($swSmoke.Elapsed.TotalSeconds, 1)) s",
        "dist\zhilan 合计体积：$([math]::Round($appSize / 1MB, 1)) MB（$($appSize) 字节）",
        "zhilan.exe 体积    ：$($exeSize) 字节",
        "",
        "SHA256(zhilan.exe)                     = $exeHash",
        "SHA256(zhilan.spec)                    = $(Get-Sha256Hex $SpecFile)",
        "SHA256(requirements-lock.txt)          = $(Get-Sha256Hex $LockFile)",
        "SHA256(zhilan_launcher.py)             = $(Get-Sha256Hex (Join-Path $PackagingDir 'zhilan_launcher.py'))",
        "SHA256(build.ps1)                      = $(Get-Sha256Hex $PSCommandPath)",
        "",
        "产物清单见同目录 $(Split-Path -Leaf $ManifestFile)（文件级，含每个文件的字节数）。",
        "复跑核验：Get-FileHash dist\zhilan\zhilan.exe -Algorithm SHA256"
    )
    Save-Log -Path $HashFile -Lines $hashLines
    $null = Save-Fixed -Path $HashFileFixed -Lines $hashLines
    $hashLines | ForEach-Object { Write-Host $_ }
    $Script:LogBuffer.Add("[L3] SHA256(zhilan.exe)=$exeHash")

    # ==================================================================================
    Write-Head '完成'
    # ==================================================================================
    $Script:LogBuffer.Add(("[完成] 总耗时 {0:n1}s（含核对与自检）" -f ((Get-Date) - $Script:BuildStartedAt).TotalSeconds))
    $Script:LogBuffer.Add(("[完成] 固定名证据件（S6-6.3-*）前进到本次构建 = $([bool]$UpdateFixed)（默认 false：6.3 的验收证据保持原样）"))
    Save-Log -Path $FullBuildLog -Lines $Script:LogBuffer
    $summary = $Script:LogBuffer | Where-Object { $_ -notmatch '^\d+\s+(INFO|DEBUG|WARNING)\b' -and $_ -notmatch '^\[exit code' }
    $null = Save-Fixed -Path $BuildLogSummary -Lines $summary
    Write-Ok "构建日志：$FullBuildLog"
    Write-Ok "自检输出：$SmokeOut"
    Write-Ok "产物哈希：$HashFile"
    Write-Ok "产物清单：$ManifestFile"
    if ($UpdateFixed) {
        Write-Ok "固定名证据件已按 -UpdateFixed 前进：$BuildLogSummary / $SmokeOutFixed / $HashFileFixed / $ManifestFixed"
    }
    else {
        Write-Host '[注意] 固定名证据件（S6-6.3-*）**未改动**（要前进请显式传 -UpdateFixed）'
    }
    exit 0
}
catch {
    $Script:LogBuffer.Add("[异常] $($_ | Out-String)")
    Write-Bad $_.ToString()
    Save-Log -Path $FullBuildLog -Lines $Script:LogBuffer
    if ($_.ToString() -like '*DEPENDENCY_DRIFT*') { exit 2 }
    exit 1
}
