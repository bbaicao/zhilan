param([string]$InstallDir = '')

$ErrorActionPreference = 'Stop'
$archive = $null
$exitCode = 1

function Assert-RegularPath([string]$Path) {
    $cursor = [System.IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "目标路径含目录链接或符号链接，停止导入：$cursor"
            }
        }
        $parent = [System.IO.Path]::GetDirectoryName($cursor.TrimEnd('\'))
        if (-not $parent -or $parent -eq $cursor) { break }
        $cursor = $parent
    }
}

function Get-ModelSha256([string]$Path) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = $null
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '')
    }
    finally {
        if ($stream) { $stream.Dispose() }
        $sha.Dispose()
    }
}

function Assert-Matches([string]$Path, $Model) {
    $file = Get-Item -LiteralPath $Path -Force
    if ($file.PSIsContainer -or $file.Length -ne $Model.Bytes -or
        (Get-ModelSha256 $Path) -ine $Model.Hash) {
        throw "已有文件与本次清单不同，已停止并保留现场：$Path。请保留原文件，核对其版本与来源后再处理；本工具不会覆盖它。"
    }
}

try {
    Write-Host '知澜离线模型导入：不联网、不安装其他软件、不覆盖不同内容的已有文件。'
    if (-not $InstallDir) {
        $appId = '{6F9B2C41-8E3D-4A57-9C10-7B5E2A8D4F63}_is1'
        $registryRoots = @(
            'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
            'HKCU:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
            'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
            'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'
        )
        $locations = @()
        foreach ($registryRoot in $registryRoots) {
            $key = Join-Path $registryRoot $appId
            if (Test-Path -LiteralPath $key) {
                $location = (Get-ItemProperty -LiteralPath $key).InstallLocation
                if (-not $location) { throw '知澜安装记录没有安装位置。请先完成基础安装，再重试模型导入。' }
                $locations += [System.IO.Path]::GetFullPath($location).TrimEnd('\')
            }
        }
        $locations = @($locations | Sort-Object -Unique)
        if ($locations.Count -eq 0) { throw '没有找到已安装的知澜。请先运行同目录的知澜基础安装包，再双击本工具。' }
        if ($locations.Count -ne 1) { throw '找到多个互相矛盾的知澜安装位置，已停止。请先核对实际使用的安装目录。' }
        $InstallDir = $locations[0]
    }
    if (-not [System.IO.Path]::IsPathRooted($InstallDir)) { throw '安装目录必须是绝对路径。' }
    $InstallDir = [System.IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
    Assert-RegularPath $InstallDir
    if (-not (Test-Path -LiteralPath $InstallDir -PathType Container) -or
        -not (Test-Path -LiteralPath (Join-Path $InstallDir 'zhilan.exe') -PathType Leaf)) {
        throw "此目录没有已安装的知澜程序：$InstallDir。请先完成基础安装。"
    }

    $zipPath = Join-Path $PSScriptRoot 'zhilan-models-offline-20261007.zip'
    $manifestPath = Join-Path $PSScriptRoot 'models-manifest.txt'
    if (-not (Test-Path -LiteralPath $zipPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw '缺少 zhilan-models-offline-20261007.zip 或 models-manifest.txt。请把交付目录完整解压，保留这些文件与本工具在同一目录。'
    }
    Write-Host "安装位置：$InstallDir"
    Write-Host '第 1 步：核对官方模型清单与压缩包结构……'
    $models = @()
    foreach ($line in (Get-Content -LiteralPath $manifestPath -Encoding UTF8)) {
        if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith('#')) { continue }
        if ($line -notmatch '^([0-9a-fA-F]{64})  ([A-Za-z0-9_./-]+)  ([0-9]+)$') {
            throw '模型清单格式不正确，已停止导入。'
        }
        $relative = $Matches[2]
        if ($relative.StartsWith('/') -or @($relative.Split('/') | Where-Object {
            $_ -eq '' -or $_ -eq '.' -or $_ -eq '..'
        }).Count -gt 0) { throw '模型清单存在不安全的相对路径，已停止导入。' }
        $models += [pscustomobject]@{ Hash = $Matches[1]; Path = $relative; Bytes = [long]$Matches[3] }
    }
    if ($models.Count -ne 13 -or @($models.Path | Sort-Object -Unique).Count -ne 13 -or
        ($models | Measure-Object -Property Bytes -Sum).Sum -ne 858204914) {
        throw '本批模型清单应为 13 件官方 ONNX 文件、858204914 字节。当前清单不符合，已停止导入。'
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
    $entries = @{}
    $prefix = 'MinerU-4_models_onnx/'
    foreach ($entry in $archive.Entries) {
        $name = $entry.FullName
        if ($name.Contains('\') -or $name.Contains(':') -or $name.StartsWith('/') -or
            -not $name.StartsWith($prefix, [StringComparison]::Ordinal) -or
            @($name.Split('/') | Where-Object { $_ -eq '.' -or $_ -eq '..' }).Count -gt 0) {
            throw '压缩包存在越界路径或不是本批官方模型结构，已停止导入。'
        }
        if ($name.EndsWith('/')) { continue }
        $relative = $name.Substring($prefix.Length)
        if ($entries.ContainsKey($relative)) { throw '压缩包有重复的模型路径，已停止导入。' }
        $entries[$relative] = $entry
    }
    if ($entries.Count -ne $models.Count) { throw '压缩包文件数量与模型清单不同，已停止导入。' }
    $modelRoot = Join-Path $InstallDir 'models\MinerU-4_models_onnx'
    Assert-RegularPath $modelRoot

    # ponytail: preflight all existing files before writes; no rollback deletes or overwrites.
    foreach ($model in $models) {
        if (-not $entries.ContainsKey($model.Path) -or $entries[$model.Path].Length -ne $model.Bytes) {
            throw "压缩包中缺失模型或模型长度不符：$($model.Path)"
        }
        $target = Join-Path $modelRoot ($model.Path.Replace('/', '\'))
        Assert-RegularPath $target
        if (Test-Path -LiteralPath $target) { Assert-Matches $target $model }
    }
    Write-Host '第 2 步：逐件写入并校验 SHA256，模型解压约 818 MiB，请稍候……'
    $number = 0
    foreach ($model in $models) {
        $number++
        $target = Join-Path $modelRoot ($model.Path.Replace('/', '\'))
        Assert-RegularPath $target
        if (Test-Path -LiteralPath $target) {
            Assert-Matches $target $model
            Write-Host "[$number/13] 已有内容一致，保留：$($model.Path)"
            continue
        }
        $directory = [System.IO.Path]::GetDirectoryName($target)
        [System.IO.Directory]::CreateDirectory($directory) | Out-Null
        Assert-RegularPath $target
        $source = $null
        $destination = $null
        try {
            $source = $entries[$model.Path].Open()
            # CreateNew also prevents overwriting a file created after the preflight.
            $destination = [System.IO.File]::Open($target, [System.IO.FileMode]::CreateNew,
                [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
            $source.CopyTo($destination)
        }
        finally {
            if ($destination) { $destination.Dispose() }
            if ($source) { $source.Dispose() }
        }
        Assert-Matches $target $model
        Write-Host "[$number/13] 已校验：$($model.Path)"
    }
    Write-Host ''
    Write-Host '导入成功：13 件模型全部通过长度和 SHA256 校验。'
    Write-Host '现在可以启动知澜。生成回答与建立检索索引仍需在设置中配置你已有的模型服务。'
    $exitCode = 0
}
catch {
    Write-Host ''
    Write-Host ("导入失败：" + $_.Exception.Message) -ForegroundColor Red
    Write-Host '已保留原文件与本次写入现场。请勿删除原有资料；校验失败时请保留完整交付目录和本窗口的报错。'
}
finally {
    if ($archive) { $archive.Dispose() }
}
exit $exitCode
