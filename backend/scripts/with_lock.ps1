<#
知澜 v2 · 独占跑测锁包装器（v5）
==================================================================
对应缺陷：回归修复（等待期零日志 · 泄漏锁只能靠人工发现）
入库日期：2026-09-29（缺陷清单-3 · 修复阶段 S0 · 条目 回归检查）

用法：
  pwsh -NoProfile -File <repo>\backend\scripts\with_lock.ps1 -Label <标签> -Cmd "<完整命令>"
  ⚠️ 长命令必须后台 job 或足够大的 timeoutMs（前台超时会杀包装器 ⇒ 锁泄漏）

参数兼容性：与 v4 的参数签名兼容（-Label / -Cmd / -WaitMinutes / -StaleMinutes / -SkipProcessCheck）。
  -StaleMinutes 仅为兼容保留：v4 的「mtime 超过 StaleMinutes ⇒ 强制接管」已**删除**（全量 pytest 本身约 341 s，
  mtime 陈旧线会误抢活锁 —— 回归修复 的两例泄漏正是「死持有者」形态，不是「慢持有者」形态）。
  传入该参数不会报错，但值被忽略并记一行 STALEMINUTES-IGNORED。

行为（v5 = v4 + 排队期强制留痕）：
  ① 取锁**之前**就建日志：WAIT-START（等待者身份 pid/pstart/ppid/user/cwd/label/token + 等待开始时刻）
     以及**每次重试一行** waiting for lock（含 wait-start / elapsed / 当前持有者 / 持有者状态）；
  ② 原子取锁（CreateNew + FileShare.None）；
  ③ 持有者存活判定 =「pid 存在 **且** CreationDate == 锁内 pstart」⇒ alive / dead / unknown；
     **活锁永不抢占**（不等时长、不看 mtime）；pid 被复用 ⇒ 判 dead；
  ④ 死持有者 + 无残留 pytest ⇒ DEAD-OWNER-TAKEOVER 立即接管（两例泄漏现场形态：2088-bb143242 / 13276-dcafe639）；
     死持有者但仍有残留 pytest ⇒ 等 30 s 再判（v4 保护保留）；
  ⑤ 锁文件不可解析（空/半写）⇒ 默认不抢；仅当「年龄 > CorruptGraceSeconds 且无残留 pytest」才接管；
  ⑥ 取到锁后 LOCK-ACQUIRED 含 owner/pid/pstart/label/ppid/user/waited/attempt；
  ⑦ stray pytest 检查（v4 保留，5 次 × 3 s）→ 跑命令 → finally **只释放自己 token 的锁**；
  ⑧ 退出码 fail-closed（v5.1，Lead 最小修法）：解析失败 ⇒ CMD-PARSE-ERROR → 5；找不到命令（只认
     CommandNotFoundException）⇒ CMD-NOT-FOUND → 5；其它异常原样传播（如 throw ⇒ 1）。
     ⛔ 不动 $ErrorActionPreference；⛔ 不以「$Error 非空」一概判失败（会把正常腿误判为假红）。
     ⚠️ 带引号的路径必须用 & 调用，例：& '<repo>\backend\.venv\Scripts\python.exe' -m pytest tests -q

退出码：命令退出码（正常）；3 = 等锁超时；4 = 检测到 stray pytest；
  5 = 命令未执行/脚本块解析失败（CMD-FAILED）⇒ 具体两行：CMD-PARSE-ERROR（[scriptblock]::Create 失败）
      或 CMD-NOT-FOUND（CommandNotFoundException）。fail-closed，绝不把「命令没跑成」当成功
      （v5.1 加固；计划判据外，依 回归检查 探测腿 + Lead 独立复现追加）。
日志：默认 $env:TEMP\zhilan-lock-logs\<Label>-<yyyyMMdd-HHmmss>.log（可用 -LogDir 覆盖；⛔ 不写仓库）。
测试：-LockPath 可指向另一把锁（默认 $env:TEMP\zhilan-pytest.lock），用于隔离负对照实验；生产用法不传该参数。
#>
param(
  [Parameter(Mandatory=$true)][string]$Label,
  [Parameter(Mandatory=$true)][string]$Cmd,
  [int]$WaitMinutes = 60,
  [int]$StaleMinutes = 30,   # v4 兼容参数；v5 忽略其值（见上方说明）
  [switch]$SkipProcessCheck,
  [int]$RetrySeconds = 5,
  [int]$CorruptGraceSeconds = 60,
  [string]$LogDir = "",
  [string]$LockPath = ""
)
$ErrorActionPreference = "Continue"

if (-not $LockPath) { $LockPath = Join-Path $env:TEMP "zhilan-pytest.lock" }
$lock = $LockPath
if (-not $LogDir) { $LogDir = Join-Path $env:TEMP "zhilan-lock-logs" }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$token = "$PID-" + ([guid]::NewGuid().ToString("N").Substring(0,8))
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$log = Join-Path $LogDir "$Label-$stamp.log"

function Write-Log([string]$msg) {
  $line = "$(Get-Date -Format 'HH:mm:ss') $msg"
  Write-Output $line
  try { Add-Content -LiteralPath $log -Value $line -Encoding UTF8 -ErrorAction Stop } catch { }
}

function Get-LockField([string]$raw, [string]$name) {
  $m = [regex]::Match($raw, "$name=(\S+)")
  if ($m.Success) { return $m.Groups[1].Value }
  return ""
}

function Get-HolderStatus([string]$raw) {
  # alive = pid 存在且 CreationDate 与锁内 pstart 一致；dead = pid 不在 / pstart 不匹配（pid 复用）；unknown = 解析不出
  $m = [regex]::Match($raw, 'pid=(\d+)')
  if (-not $m.Success) { return "unknown" }
  $hp = [int]$m.Groups[1].Value
  $proc = Get-Process -Id $hp -ErrorAction SilentlyContinue
  if (-not $proc) { return "dead" }
  $ps = [regex]::Match($raw, 'pstart=([^ ]+)')
  if (-not $ps.Success) { return "unknown" }
  $liveStart = ""
  try { $liveStart = $proc.StartTime.ToString('o') } catch { return "dead" }
  if ($liveStart -ne $ps.Groups[1].Value) { return "dead" }
  return "alive"
}

function Get-StrayPytest {
  $out = @()
  try {
    $out = @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
             Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine -match 'pytest' })
  } catch { }
  return ,$out
}

$pstart = "unknown"; try { $pstart = (Get-Process -Id $PID).StartTime.ToString('o') } catch { }
$ppid = "unknown"; try { $ppid = (Get-CimInstance Win32_Process -Filter "ProcessId=$PID").ParentProcessId } catch { }
$user = "unknown"; try { $user = [Environment]::UserName } catch { }
$cwd = (Get-Location).Path
$waiter = "label=$Label pid=$PID pstart=$pstart ppid=$ppid user=$user cwd=$cwd token=$token"

$waitStart = Get-Date
$waitStartStamp = $waitStart.ToString("HH:mm:ss")
$deadline = $waitStart.AddMinutes($WaitMinutes)
Write-Log "WAIT-START  $waiter lock=$lock log=$log wait-start=$waitStartStamp retry=${RetrySeconds}s wait-minutes=$WaitMinutes"
if ($PSBoundParameters.ContainsKey('StaleMinutes')) {
  Write-Log "STALEMINUTES-IGNORED: v5 不再使用 mtime 陈旧线（传入值=$StaleMinutes 被忽略；活锁永不抢占，死持有者按 pid+pstart 判定接管）"
}

$attempt = 0
$acquired = $false
$corruptSince = $null
while (-not $acquired) {
  $attempt++
  try {
    $fs = [System.IO.File]::Open($lock, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    $sw = New-Object System.IO.StreamWriter($fs)
    $sw.WriteLine("owner=$token label=$Label pid=$PID pstart=$pstart ppid=$ppid user=$user at=" + (Get-Date -Format o))
    $sw.Flush(); $sw.Close(); $fs.Close()
    $acquired = $true
    break
  } catch {
    $raw = ""
    if (Test-Path -LiteralPath $lock) { $raw = (Get-Content -LiteralPath $lock -Raw -ErrorAction SilentlyContinue) }
    if ($raw) { $raw = $raw.Trim() }
    $status = if ($raw) { Get-HolderStatus $raw } else { "unknown" }

    if ($raw -and $status -eq "dead") {
      $stray = Get-StrayPytest
      if ($stray.Count -gt 0) {
        Write-Log "STALE-LOCK-BUT-STRAY-PYTEST: 持有者已死但仍有 $($stray.Count) 个 pytest 在跑 ⇒ 等 30s 再判（避免与残留进程并发）"
        foreach ($s in $stray) { Write-Log "  pid=$($s.ProcessId) :: $($s.CommandLine)" }
        Start-Sleep -Seconds 30
        continue
      }
      Write-Log "DEAD-OWNER-TAKEOVER attempt=$attempt holder=[$raw] reason=pid-absent-or-pid-reused(dead)"
      Remove-Item -LiteralPath $lock -Force -ErrorAction SilentlyContinue
      $corruptSince = $null
      continue
    }

    if (-not $raw) {
      # 锁文件存在但读不到内容（可能是他人 CreateNew 与 WriteLine 之间的窗口）⇒ 默认不抢
      if ($null -eq $corruptSince) { $corruptSince = Get-Date }
      $ageSec = 0
      try { $ageSec = [int](((Get-Date) - (Get-Item -LiteralPath $lock).LastWriteTime).TotalSeconds) } catch { }
      if ($ageSec -gt $CorruptGraceSeconds) {
        $stray = Get-StrayPytest
        if ($stray.Count -eq 0) {
          Write-Log "CORRUPT-LOCK-TAKEOVER attempt=$attempt age=$($ageSec)s(>$CorruptGraceSeconds) file=[$lock] note=空或不可解析且无残留 pytest"
          Remove-Item -LiteralPath $lock -Force -ErrorAction SilentlyContinue
          $corruptSince = $null
          continue
        }
        Write-Log "CORRUPT-LOCK-BUT-STRAY-PYTEST: 锁不可解析且已 $($ageSec)s，但仍有 $($stray.Count) 个 pytest ⇒ 等待"
      }
    }

    $elapsed = [int](((Get-Date) - $waitStart).TotalSeconds)
    $holdShow = if ($raw) { $raw } else { "<unreadable-or-half-written>" }
    Write-Log "waiting for lock (WAITING-FOR-LOCK) attempt=$attempt wait-start=$waitStartStamp elapsed=$($elapsed)s holder-status=$status holder=[$holdShow] waiter=[$waiter]"
    if ((Get-Date) -gt $deadline) {
      Write-Log "LOCK-TIMEOUT: 等待超过 $WaitMinutes 分钟仍未取到锁（$lock）；最后 holder-status=$status holder=[$holdShow]"
      exit 3
    }
    Start-Sleep -Seconds $RetrySeconds
  }
}

$waited = [int](((Get-Date) - $waitStart).TotalSeconds)
Write-Log "LOCK-ACQUIRED owner=$token pid=$PID pstart=$pstart label=$Label ppid=$ppid user=$user waited=$($waited)s attempt=$attempt lock=$lock"

$exit = 0
try {
  if (-not $SkipProcessCheck) {
    $attempts = 5
    for ($i = 1; $i -le $attempts; $i++) {
      $all = @()
      try {
        $all = @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
                 Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine })
      } catch { }
      if ($all.Count -eq 0) {
        $all = @(Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $PID } |
                 ForEach-Object { [pscustomobject]@{ ProcessId = $_.Id; CommandLine = "<unknown (CIM unavailable)>" } })
      }
      $pytest = @($all | Where-Object { $_.CommandLine -match 'pytest' })
      if ($pytest.Count -eq 0) {
        if ($all.Count -gt 0) {
          Write-Log "NONPYTEST-PYTHON: $($all.Count) 个非 pytest python 进程（外部 GUI/服务），不阻断独占跑测"
          foreach ($a in $all) { Write-Log "  pid=$($a.ProcessId) :: $($a.CommandLine)" }
        }
        break
      }
      if ($i -eq $attempts) {
        Write-Log "STRAY-PYTEST: 宽限 $($attempts * 3)s 后仍有 $($pytest.Count) 个 pytest 进程在跑（违反独占跑测纪律）⇒ 中止，稍后重试"
        foreach ($a in $pytest) { Write-Log "  pid=$($a.ProcessId) :: $($a.CommandLine)" }
        exit 4
      }
      Write-Log "STRAY-PYTEST: 第 $i 次检查见 $($pytest.Count) 个 pytest 进程，宽限 3s 再看"
      Start-Sleep -Seconds 3
    }
  }
  Write-Log "RUN: $Cmd"
  $sw2 = [System.Diagnostics.Stopwatch]::StartNew()
  # fail-closed（Lead 最小修法）：只认两类 —— 脚本块解析失败、CommandNotFoundException。
  # ⛔ 不改 $ErrorActionPreference；⛔ 不以「$Error 非空」一概判定（避免把正常腿误判成失败）。
  $sb = $null
  try { $sb = [scriptblock]::Create("$Cmd 2>&1") } catch { }
  if ($null -eq $sb) {
    $sw2.Stop()
    Write-Log "CMD-PARSE-ERROR: 脚本块解析失败，命令从未执行；原始 -Cmd=[$Cmd]"
    Write-Log "HINT: 带引号的路径必须用 & 调用（例：& '.venv\Scripts\python.exe' -m pytest tests -q）"
    $exit = 5
  } else {
    $notFound = $false
    $errMsg = ""
    $out = ""
    try { $out = & $sb | Out-String }
    catch [System.Management.Automation.CommandNotFoundException] { $notFound = $true; $errMsg = $_.Exception.Message }
    catch { $sw2.Stop(); Write-Log "CMD-THREW: $($_.Exception.GetType().FullName): $($_.Exception.Message)（原行为：异常继续传播，退出码非 0）"; throw }
    $sw2.Stop()
    if ($notFound) {
      Write-Log "CMD-NOT-FOUND: 命令未执行（CommandNotFoundException）；原始 -Cmd=[$Cmd]；err=[$errMsg]"
      $exit = 5
    } else {
      Write-Output $out
      Add-Content -LiteralPath $log -Value $out -Encoding UTF8
      if ($null -ne $LASTEXITCODE) { $exit = $LASTEXITCODE }
      Write-Log "EXITCODE=$exit elapsed=$([math]::Round($sw2.Elapsed.TotalSeconds,1))s"
    }
  }
} finally {
  if (Test-Path -LiteralPath $lock) {
    $content = Get-Content -LiteralPath $lock -Raw -ErrorAction SilentlyContinue
    if ($content -match [regex]::Escape($token)) {
      Remove-Item -LiteralPath $lock -Force -ErrorAction SilentlyContinue
      Write-Log "LOCK-RELEASED owner=$token"
    } else {
      Write-Log "WARN: 锁不属于本 token，不释放（holder=[$($content -replace '\s+',' ')]）"
    }
  }
}
Write-Log "LOG-FILE: $log"
exit $exit
