<#
.SYNOPSIS
  回归检查：一条命令起真服务（默认 8021）→ 经独占锁跑 tests/contract → 停服 → 收尾三校验。

.DESCRIPTION
  本脚本是 回归修复「拍板 #1-C 自动探测」的阳性对照入口：
    · live 模式：起本仓 uvicorn（--port 8021），等 /api/health 返 200，
      再经 backend/scripts/with_lock.ps1 跑
      CONTRACT_TARGET=v2 BASE_URL=http://127.0.0.1:8021 pytest tests/contract
      ⇒ 逐 nodeid 核对 tests/contract/expected-nodeids.json；完整 PASS 要求全部身份通过、
      无 failed/errors/skipped。定向子集只可标为 SUBSET-PASS。
    · stub404 模式：起一个对一切路径回 404 的**非知澜**桩服务 ⇒ 期望 conftest 显式 skip
      （原因行含「GET /openapi.json → HTTP 404」）且 0 failed / 0 errors。
    · stubempty 模式：桩服务对 /openapi.json 回 200 但只有空 paths ⇒ 期望显式 skip
      （原因行含「缺知澜指纹路径」）。
  三种模式的读数差异就是「自动探测」判据的判别力所在。

  收尾三校验（任何模式都做）：本脚本占用的端口已释放 / 仓库根 .env 的 sha256 未变 /
  无本脚本起的残留进程。
  ⛔ 只 stop Start-Process 自己 spawn 的 PID 及其已核对命令的直接子进程 —— 绝不按名字或按端口杀进程
  （业主的 python -m client 与其它在跑的服务一个字都不动）。

  ⚠️ 服务进程本身**不**在独占锁内起停（它要在 pytest 跑的时候活着）；被测 pytest 一律经
  with_lock.ps1（v5 的 stray 检查只认 pytest 进程 ⇒ 服务不会被误判为 stray）。

.EXAMPLE
  pwsh -NoProfile -File backend/scripts/run_contract_live.ps1
  pwsh -NoProfile -File backend/scripts/run_contract_live.ps1 -Mode stub404
  pwsh -NoProfile -File backend/scripts/run_contract_live.ps1 -Mode stubempty
#>
[CmdletBinding()]
param(
  [int]$Port = 8021,
  [ValidateSet('live', 'stub404', 'stubempty')]
  [string]$Mode = 'live',
  [string]$Label = '',
  [string]$PytestTarget = 'tests/contract',
  [int]$ReadySeconds = 90,
  [int]$LockWaitMinutes = 30,
  [string]$LogDir = (Join-Path $env:TEMP 'zhilan-contract-live-logs')
)

$ErrorActionPreference = 'Stop'

$Backend = Split-Path -Parent $PSScriptRoot
$RepoRoot = Split-Path -Parent $Backend
$Wrapper = Join-Path $PSScriptRoot 'with_lock.ps1'
$EnvFile = Join-Path $RepoRoot '.env'
$PythonExe = Join-Path $Backend '.venv\Scripts\python.exe'
if (-not $Label) { $Label = 'contract-live-' + $Mode }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$log = Join-Path $LogDir ($Label + '-' + $stamp + '.log')

function Say([string]$Message) {
  Write-Host $Message
  Add-Content -LiteralPath $log -Value $Message -Encoding utf8
}

function Get-Sha256([string]$Path) {
  if (Test-Path -LiteralPath $Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash }
  return '<absent>'
}

function Get-ListeningPid([int]$PortNumber) {
  $conn = Get-NetTCPConnection -LocalPort $PortNumber -State Listen -ErrorAction SilentlyContinue
  if ($conn) { return (@($conn | Select-Object -ExpandProperty OwningProcess -Unique) -join ',') }
  return ''
}

# pytest 把计数为 0 的类别整个省掉（例：0 failed 时汇总行里没有 "failed" 字样）⇒ 未命中即 0。
function Grab([string]$Pattern, [string]$Text) {
  $m = [regex]::Match($Text, $Pattern)
  if ($m.Success) { return [int]$m.Groups[1].Value }
  return 0
}

function Stop-OwnService($Proc, [int]$SvcPort) {
  if (-not $Proc) { return }
  if ($Proc.HasExited) {
    Say "SERVICE-EXITED-EARLY: pid=$($Proc.Id) exit=$($Proc.ExitCode)（见服务日志）"
    return
  }
  Say "SERVICE-STOP: pid=$($Proc.Id)（本脚本自己 spawn 的）port=$($SvcPort)"
  # Windows venv launcher starts a child interpreter; bind it to our live parent and command.
  $children = @(Get-CimInstance Win32_Process -Filter ("ParentProcessId=" + $Proc.Id) |
    Where-Object {
      $_.Name -eq 'python.exe' -and $_.CommandLine -and
      (($_.CommandLine -match 'uvicorn' -and $_.CommandLine -match ("--port\s+" + $SvcPort)) -or
       $_.CommandLine.Contains($stubPy))
    })
  foreach ($child in $children) { Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue }
  $null = $Proc.WaitForExit(2000)
  if ($Proc.HasExited) { return }
  Stop-Process -Id $Proc.Id -ErrorAction SilentlyContinue
  if (-not $Proc.WaitForExit(10000)) {
    Say "SERVICE-STOP-FORCE: pid=$($Proc.Id) 未在 10s 内退出"
    Stop-Process -Id $Proc.Id -Force -ErrorAction SilentlyContinue
  }
}

Say "=== S0-152 run_contract_live · mode=$($Mode) port=$($Port) · $($stamp) ==="
Say "LOG-FILE: $($log)"
Say "BACKEND: $($Backend)"
Say "WRAPPER: $($Wrapper)"

$envShaBefore = Get-Sha256 $EnvFile
Say "ENV-SHA256-BEFORE: $($envShaBefore)"

$preOccupant = Get-ListeningPid $Port
if ($preOccupant) {
  Say "PREFLIGHT-FAIL: 端口 $($Port) 已被 pid=$($preOccupant) 占用 ⇒ 本脚本拒绝启动（⛔ 不杀别人的进程，请换端口或先处理占用方）"
  exit 2
}
Say "PREFLIGHT-OK: 端口 $($Port) 空闲"

$env:PYTHONPATH = $Backend
$env:PYTHONIOENCODING = 'utf-8'

$stubPy = Join-Path $LogDir 'zhilan-contract-stub.py'
$svcOut = Join-Path $LogDir ($Label + '-' + $stamp + '.service.out.log')
$svcErr = Join-Path $LogDir ($Label + '-' + $stamp + '.service.err.log')

if ($Mode -eq 'live') {
  $svcArgs = @('-m', 'uvicorn', 'main:app', '--port', "$($Port)")
  $svcWorkDir = $Backend
} else {
  $stubCode = @'
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = sys.argv[1]
PORT = int(sys.argv[2])
EMPTY = json.dumps({"openapi": "3.0.0", "paths": {}}).encode()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if MODE == "stubempty" and self.path.split("?")[0] == "/openapi.json":
            self._send(200, EMPTY)
        else:
            self._send(404, b'{"detail":"stub"}')

    def do_POST(self):
        self.do_GET()

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
'@
  Set-Content -LiteralPath $stubPy -Value $stubCode -Encoding utf8
  $svcArgs = @($stubPy, $Mode, "$($Port)")
  $svcWorkDir = $LogDir
  Say "STUB-SCRIPT: $($stubPy)（桩服务 = 非知澜服务，用于负对照）"
}

$startArgs = @{
  FilePath               = $PythonExe
  ArgumentList           = $svcArgs
  WorkingDirectory       = $svcWorkDir
  PassThru               = $true
  WindowStyle            = 'Hidden'
  RedirectStandardOutput = $svcOut
  RedirectStandardError  = $svcErr
}
$svc = Start-Process @startArgs
Say "SERVICE-SPAWNED: pid=$($svc.Id) mode=$($Mode) url=http://127.0.0.1:$($Port) (stdout=$($svcOut) stderr=$($svcErr))"

$ready = $false
if ($Mode -eq 'live') {
  $deadline = (Get-Date).AddSeconds($ReadySeconds)
  while ((Get-Date) -lt $deadline) {
    if ($svc.HasExited) { break }
    try {
      $probe = Invoke-WebRequest -Uri ("http://127.0.0.1:" + $Port + "/api/health") -TimeoutSec 3 -UseBasicParsing
      if ($probe.StatusCode -eq 200) { $ready = $true; break }
    } catch {
      Start-Sleep -Milliseconds 800
    }
  }
} else {
  Start-Sleep -Seconds 2
  $ready = [bool](Get-ListeningPid $Port)
}

if (-not $ready) {
  Say "SERVICE-NOT-READY: mode=$($Mode) port=$($Port) 在 $($ReadySeconds) s 内没起来"
  if (Test-Path -LiteralPath $svcErr) {
    Say "SERVICE-STDERR-TAIL:"
    Get-Content -LiteralPath $svcErr -Tail 20 | ForEach-Object { Say ("  " + $_) }
  }
  Stop-OwnService $svc $Port
  Say "CONTRACT-LIVE-VERDICT: FAIL（服务未就绪）"
  exit 3
}
Say "SERVICE-READY: http://127.0.0.1:$($Port)"

$resultJson = Join-Path $LogDir ($Label + '-' + $stamp + '.nodeids.json')
$inner = 'cd "' + $Backend + '"; $env:CONTRACT_RESULT_JSON="' + $resultJson + '"; $env:CONTRACT_TARGET="v2"; $env:BASE_URL="http://127.0.0.1:' + $Port + '"; $env:PYTHONPATH="' + $Backend + '"; $env:PYTHONIOENCODING="utf-8"; .\.venv\Scripts\python.exe -m pytest ' + $PytestTarget + ' -p scripts.contract_results -q -rfsE'

Say "PYTEST-CMD: pytest $($PytestTarget) -q -rfsE（CONTRACT_TARGET=v2 BASE_URL=http://127.0.0.1:$($Port)）"
Say "PYTEST-WRAPPER: with_lock.ps1 -Label $($Label) -WaitMinutes $($LockWaitMinutes)"
$pyOut = & pwsh -NoProfile -File $Wrapper -Label $Label -WaitMinutes $LockWaitMinutes -Cmd $inner 2>&1
$wrapperExit = $LASTEXITCODE
$pyLog = Join-Path $LogDir ($Label + '-' + $stamp + '.pytest.log')
$pyOut | Set-Content -LiteralPath $pyLog -Encoding utf8
foreach ($line in $pyOut) { Say ([string]$line) }

$joined = ($pyOut | Out-String)
$passed = Grab '(\d+) passed' $joined
$failed = Grab '(\d+) failed' $joined
$skipped = Grab '(\d+) skipped' $joined
$errors = Grab '(\d+) errors?' $joined
$deselected = Grab '(\d+) deselected' $joined
# .NET 的多行 $ 在 CRLF 行尾会失配（$ 只认 \n）⇒ 不锚行尾。
$summaryMatches = [regex]::Matches($joined, '(?m)^[^\r\n]*\d+ passed[^\r\n]*')
if ($summaryMatches.Count -gt 0) {
  $summaryLine = $summaryMatches[$summaryMatches.Count - 1].Value.Trim()
} else {
  $summaryLine = '<未找到 pytest 汇总行>'
}

Stop-OwnService $svc $Port
Start-Sleep -Seconds 2

$portAfter = Get-ListeningPid $Port
$envShaAfter = Get-Sha256 $EnvFile
$residual = @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
  Where-Object {
    $_.ProcessId -ne $PID -and $_.CommandLine -and
    (($_.CommandLine -match 'uvicorn' -and $_.CommandLine -match ("--port\s+" + $Port)) -or
     ($_.CommandLine -match 'zhilan-contract-stub'))
  })

Say "CONTRACT-LIVE-SUMMARY: mode=$($Mode) passed=$($passed) failed=$($failed) skipped=$($skipped) errors=$($errors) deselected=$($deselected) wrapper-exit=$($wrapperExit)"
Say "CONTRACT-LIVE-SUMMARY-LINE: $($summaryLine)"
Say "PYTEST-LOG: $($pyLog)"
Say "CONTRACT-LIVE-CHECK: port-$($Port)-free=$(-not [bool]$portAfter)"
Say "CONTRACT-LIVE-CHECK: env-sha-unchanged=$($envShaAfter -eq $envShaBefore)"
Say "CONTRACT-LIVE-CHECK: env-sha-before=$($envShaBefore)"
Say "CONTRACT-LIVE-CHECK: env-sha-after=$($envShaAfter)"
Say "CONTRACT-LIVE-CHECK: no-residual-process=$($residual.Count -eq 0)"

$reasons = @()
$checksOk = $true
if ($portAfter) { $checksOk = $false; $reasons += "端口 $($Port) 仍被 pid=$($portAfter) 占用" }
if ($envShaAfter -ne $envShaBefore) { $checksOk = $false; $reasons += '.env sha256 变了' }
if ($residual.Count -gt 0) {
  $checksOk = $false
  $reasons += "残留进程 $($residual.Count) 个（pid=$(($residual | ForEach-Object { $_.ProcessId }) -join ',')）"
}

if ($Mode -eq 'live') {
  $coverage = $null
  if (Test-Path -LiteralPath $resultJson) {
    try { $coverage = Get-Content -LiteralPath $resultJson -Raw -Encoding utf8 | ConvertFrom-Json } catch { }
  }
  $verdictOk = ($wrapperExit -eq 0) -and ($failed -eq 0) -and ($errors -eq 0) -and ($skipped -eq 0) -and ($passed -gt 0) -and ($joined -match 'service=UP') -and ($coverage.passed -eq $true)
  Say "CONTRACT-LIVE-IDENTITIES: $resultJson scope=$($coverage.scope) selected=$($coverage.selected_count) expected=$($coverage.expected_count) complete=$($coverage.complete)"
  if (-not $verdictOk) {
    $reasons += "live 期望 0 skipped/0 failed/0 errors 且 probe=service=UP（实得 passed=$($passed) skipped=$($skipped) failed=$($failed) errors=$($errors) wrapper-exit=$($wrapperExit)）"
  }
} else {
  if ($Mode -eq 'stub404') { $needle = 'GET /openapi.json → HTTP 404' } else { $needle = '缺知澜指纹路径' }
  $verdictOk = ($wrapperExit -eq 0) -and ($failed -eq 0) -and ($errors -eq 0) -and ($skipped -gt 0) -and ($joined -match [regex]::Escape($needle))
  if (-not $verdictOk) {
    $reasons += "负对照期望：显式 skip（原因含「$($needle)」）且 0 failed/0 errors（实得 passed=$($passed) skipped=$($skipped) failed=$($failed) errors=$($errors)）"
  }
}

if ($verdictOk -and $checksOk) {
  if ($Mode -eq 'live' -and -not $coverage.complete) {
    Say "CONTRACT-LIVE-VERDICT: SUBSET-PASS（完整契约未验收）"
    exit 0
  }
  Say "CONTRACT-LIVE-VERDICT: PASS"
  exit 0
}
Say "CONTRACT-LIVE-VERDICT: FAIL"
foreach ($r in $reasons) { Say ("  reason: " + $r) }
exit 1
