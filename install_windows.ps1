<#
安装 bilibili-video-learning 分享包。
脚本先把白名单源码复制到独立暂存目录，再用可恢复备份替换目标；完整模式随后按 uv 锁文件重建运行环境和 CLI 命令入口。
调用示例：powershell -ExecutionPolicy Bypass -File .\install_windows.ps1
#>
[CmdletBinding()]
param(
    [string]$DestinationRoot = (Join-Path ([Environment]::GetFolderPath("UserProfile")) ".agents\skills\bilibili-video-learning"),
    [string]$CommandBin = (Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "bilibili-video-learning\bin"),
    [switch]$SkipRuntime,
    [switch]$SkipPathUpdate
)

Set-StrictMode -Version Latest                              # 拼写错误或未初始化变量必须立即暴露。
$ErrorActionPreference = "Stop"                           # 任一步失败都停止，避免报告半成功。


# --- 解析并限制安装目标 ---
function Resolve-InstallPath {
    param([string]$PathText)

    if ([string]::IsNullOrWhiteSpace($PathText)) {          # 空路径可能意外退化为当前目录。
        throw "Installation path cannot be empty."
    }
    return [System.IO.Path]::GetFullPath($PathText)         # 后续安全比较只使用绝对路径。
}


# --- 拒绝危险或无法安全写入包装器的路径 ---
function Confirm-InstallDestination {
    param([string]$ResolvedPath)

    $trimmedPath = $ResolvedPath.TrimEnd("\", "/")       # 统一目录比较形式。
    $driveRoot = [System.IO.Path]::GetPathRoot($ResolvedPath).TrimEnd("\", "/")
    $userRoot = (Resolve-InstallPath ([Environment]::GetFolderPath("UserProfile")).TrimEnd("\", "/"))
    if ($trimmedPath -eq $driveRoot -or $trimmedPath -eq $userRoot) {
        throw "Refusing to install into a drive root or the user home directory: $ResolvedPath"
    }
    if ((Split-Path -Leaf $trimmedPath) -ne "bilibili-video-learning") {
        throw "DestinationRoot must end with 'bilibili-video-learning': $ResolvedPath"
    }
    if ($ResolvedPath.IndexOfAny([char[]]'"%!?^&|<>') -ge 0) {
        throw "DestinationRoot contains characters that cannot be represented safely in the command wrapper."
    }
}


# --- 限制 CLI 包装器目录，避免写到宽泛系统位置 ---
function Confirm-CommandDirectory {
    param([string]$ResolvedPath)

    $trimmedPath = $ResolvedPath.TrimEnd("\", "/")       # 安全比较不受末尾分隔符影响。
    $driveRoot = [System.IO.Path]::GetPathRoot($ResolvedPath).TrimEnd("\", "/")
    $userRoot = (Resolve-InstallPath ([Environment]::GetFolderPath("UserProfile")).TrimEnd("\", "/"))
    if ($trimmedPath -eq $driveRoot -or $trimmedPath -eq $userRoot) {
        throw "Refusing to place CLI wrappers in a drive root or the user home directory: $ResolvedPath"
    }
    if ($ResolvedPath.IndexOfAny([char[]]'"%;!?^&|<>') -ge 0) {
        throw "CommandBin contains characters that are unsafe in PATH or cmd wrappers."
    }
}


# --- 执行外部命令并保留真实退出码 ---
function Invoke-InstallCommand {
    param(
        [string]$Program,
        [string[]]$Arguments
    )

    & $Program @Arguments                                    # 参数数组避免拼接 shell 命令。
    if ($LASTEXITCODE -ne 0) {                              # 外部程序失败不能被 Write-Host 掩盖。
        throw ("Command failed with exit code {0}: {1} {2}" -f $LASTEXITCODE, $Program, ($Arguments -join " "))
    }
}


# --- 通过暂存目录发布一份完整 Skill ---
function Install-SkillSource {
    param(
        [string]$SourceRoot,
        [string]$TargetRoot
    )

    $stamp = Get-Date -Format "yyyyMMdd-HHmmssfff"          # 毫秒后缀避免同秒安装发生名称碰撞。
    $stageRoot = "$TargetRoot.installing-$PID-$stamp"       # 暂存副本不触碰现有 Skill。
    $backupRoot = $null                                      # 仅在确有旧目录时记录恢复位置。
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $TargetRoot) | Out-Null
    Copy-Item -LiteralPath $SourceRoot -Destination $stageRoot -Recurse
    if (-not (Test-Path -LiteralPath (Join-Path $stageRoot "SKILL.md") -PathType Leaf)) {
        throw "Staged Skill is incomplete: $stageRoot"
    }

    if (Test-Path -LiteralPath $TargetRoot) {
        $backupRoot = "$TargetRoot.backup-$stamp"            # 旧版本使用可恢复移动，不直接删除。
        Move-Item -LiteralPath $TargetRoot -Destination $backupRoot
        Write-Host "Backed up existing Skill: $backupRoot"
    }

    try {
        Move-Item -LiteralPath $stageRoot -Destination $TargetRoot
    }
    catch {
        if ($backupRoot -and -not (Test-Path -LiteralPath $TargetRoot)) {
            Move-Item -LiteralPath $backupRoot -Destination $TargetRoot  # 发布失败且目标为空时自动恢复旧版。
        }
        throw
    }
    Write-Host "Installed Skill source: $TargetRoot"
}


# --- 为用户创建稳定的 CLI 命令入口 ---
function Install-CommandWrapper {
    param(
        [string]$BinRoot,
        [string]$SkillRoot,
        [string]$RuntimePython
    )

    New-Item -ItemType Directory -Force -Path $BinRoot | Out-Null
    $wrapperScript = Join-Path $BinRoot "cli-anything-video-learning.ps1"
    $wrapperCommand = Join-Path $BinRoot "cli-anything-video-learning.cmd"
    $safeSkillRoot = $SkillRoot.Replace("'", "''")           # PowerShell 单引号字面量用双单引号转义。
    $safeRuntimePython = $RuntimePython.Replace("'", "''")   # Python 路径与 Skill 路径使用同一规则。
    $scriptText = @"
`$env:BILIBILI_VIDEO_LEARNING_ROOT = '$safeSkillRoot'
`$env:BILIBILI_VIDEO_LEARNING_PYTHON = '$safeRuntimePython'
& '$safeRuntimePython' -m cli_anything.video_learning @args
exit `$LASTEXITCODE
"@
    $commandText = "@echo off`r`npowershell -NoProfile -ExecutionPolicy Bypass -File `"%~dp0cli-anything-video-learning.ps1`" %*`r`n"
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false) # 无 BOM 的包装器兼容 Windows PowerShell 与 cmd。
    [System.IO.File]::WriteAllText($wrapperScript, $scriptText, $utf8NoBom)
    [System.IO.File]::WriteAllText($wrapperCommand, $commandText, $utf8NoBom)
    Write-Host "Installed CLI wrapper: $wrapperCommand"
}


# --- 只在缺少时追加用户 PATH ---
function Add-CommandPath {
    param([string]$BinRoot)

    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $pathEntries = @($userPath -split ";" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $alreadyPresent = @($pathEntries | Where-Object { $_.TrimEnd("\") -ieq $BinRoot.TrimEnd("\") }).Count -gt 0
    if (-not $alreadyPresent) {
        $newUserPath = (@($pathEntries) + $BinRoot) -join ";" # 保留既有 PATH 顺序，只追加一个专用命令目录。
        [Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
        Write-Host "Added CLI directory to user PATH: $BinRoot"
    }
    if (-not (($env:Path -split ";") -contains $BinRoot)) {
        $env:Path = "$BinRoot;$env:Path"                      # 当前安装进程也能立即运行新命令。
    }
}


# --- 主安装流程 ---
$packageRoot = Resolve-InstallPath $PSScriptRoot            # 所有输入都来自解压后的分享包。
$sourceSkillRoot = Join-Path $packageRoot ".agents\skills\bilibili-video-learning"
$destination = Resolve-InstallPath $DestinationRoot
$commandDirectory = Resolve-InstallPath $CommandBin
Confirm-InstallDestination $destination
Confirm-CommandDirectory $commandDirectory
if (-not (Test-Path -LiteralPath (Join-Path $sourceSkillRoot "SKILL.md") -PathType Leaf)) {
    throw "Missing packaged Skill. Extract the zip before running install_windows.ps1."
}

Install-SkillSource -SourceRoot $sourceSkillRoot -TargetRoot $destination
if ($SkipRuntime) {
    Write-Host "Source-only installation complete. Runtime and CLI were intentionally skipped."
    exit 0
}

$uvCommand = Get-Command uv -ErrorAction SilentlyContinue   # uv owns Python、环境和锁文件安装。
if (-not $uvCommand) {
    throw "uv was not found. Install uv, reopen PowerShell, then rerun this installer."
}
$runtimePython = Join-Path $destination ".venv-gpu\Scripts\python.exe"
$harnessRoot = Join-Path $destination "agent-harness"
$env:UV_PROJECT_ENVIRONMENT = Join-Path $destination ".venv-gpu"
Invoke-InstallCommand -Program $uvCommand.Source -Arguments @("sync", "--project", $destination, "--locked", "--python", "3.12")
Invoke-InstallCommand -Program $uvCommand.Source -Arguments @("pip", "install", "--python", $runtimePython, "--no-build-isolation", "--no-deps", "-e", $harnessRoot)
Install-CommandWrapper -BinRoot $commandDirectory -SkillRoot $destination -RuntimePython $runtimePython
if (-not $SkipPathUpdate) {
    Add-CommandPath -BinRoot $commandDirectory
}

Write-Host ""
Write-Host "Install complete. Run:"
Write-Host "powershell -ExecutionPolicy Bypass -File `"$packageRoot\verify.ps1`" -SkillRoot `"$destination`""
