<#
验证分享包或安装后的 bilibili-video-learning。
默认检查清单、源码、运行环境、CLI 版本和离线测试，不访问平台、不读取浏览器 Cookie、不下载媒体。
调用示例：powershell -ExecutionPolicy Bypass -File .\verify.ps1
#>
[CmdletBinding()]
param(
    [string]$SkillRoot = (Join-Path ([Environment]::GetFolderPath("UserProfile")) ".agents\skills\bilibili-video-learning"),
    [switch]$SkipRuntime,
    [switch]$SkipTests
)

Set-StrictMode -Version Latest                               # 验证脚本不能静默接受拼写错误。
$ErrorActionPreference = "Stop"                            # 任一门槛失败就返回非零。


# --- 统一运行外部验证命令 ---
function Invoke-VerifyCommand {
    param(
        [string]$Program,
        [string[]]$Arguments
    )

    & $Program @Arguments                                     # 参数数组保留空格与中文路径。
    if ($LASTEXITCODE -ne 0) {
        throw ("Verification command failed with exit code {0}: {1} {2}" -f $LASTEXITCODE, $Program, ($Arguments -join " "))
    }
}


# --- 寻找只读语法检查所需的 Python ---
function Find-ValidationPython {
    param([string]$InstalledRuntime)

    if (Test-Path -LiteralPath $InstalledRuntime -PathType Leaf) {
        return (Resolve-Path -LiteralPath $InstalledRuntime).Path  # 已安装环境最贴近真实运行条件。
    }
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        return $pythonCommand.Source                           # 源码模式可复用现有 Python，不创建环境。
    }
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if ($uvCommand) {
        $pythonPath = (& $uvCommand.Source python find 3.12 2>$null | Select-Object -First 1)
        if ($LASTEXITCODE -eq 0 -and $pythonPath) {
            return $pythonPath.Trim()
        }
    }
    throw "No Python interpreter was found for the source syntax check."
}


# --- 核对包内 SHA256 清单 ---
function Confirm-PackageManifest {
    param([string]$ResolvedSkillRoot)

    $packageRoot = [System.IO.Path]::GetFullPath((Join-Path $ResolvedSkillRoot "..\..\.."))
    $manifestPath = Join-Path $packageRoot "SHA256SUMS.txt"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        Write-Host "Manifest: not present beside installed Skill; package-content check skipped."
        return
    }

    $manifestEntries = @{}                                  # 路径集合用于同时拒绝遗漏文件和重复行。
    foreach ($line in Get-Content -LiteralPath $manifestPath -Encoding UTF8) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }  # 空行不代表文件。
        if ($line -notmatch '^([A-Fa-f0-9]{64})\s+\*?(.+)$') {
            throw "Malformed SHA256 manifest line: $line"
        }
        $expectedHash = $Matches[1].ToUpperInvariant()
        $relativePath = $Matches[2].Replace("/", "\")
        if ($manifestEntries.ContainsKey($relativePath)) {
            throw "Duplicate SHA256 manifest path: $relativePath"
        }
        $manifestEntries[$relativePath] = $expectedHash      # 先登记身份，再读取文件做内容校验。
        $targetPath = Join-Path $packageRoot $relativePath
        if (-not (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
            throw "Manifest file is missing: $relativePath"
        }
        $actualHash = (Get-FileHash -LiteralPath $targetPath -Algorithm SHA256).Hash
        if ($actualHash -ne $expectedHash) {
            throw "SHA256 mismatch: $relativePath"
        }
    }

    $actualFiles = @(Get-ChildItem -LiteralPath $packageRoot -Recurse -Force -File | Where-Object { $_.FullName -ne $manifestPath })
    $unlistedFiles = @($actualFiles | ForEach-Object {
        $relativePath = $_.FullName.Substring($packageRoot.Length + 1)
        if (-not $manifestEntries.ContainsKey($relativePath)) { $relativePath }
    })
    if ($unlistedFiles.Count -gt 0 -or $actualFiles.Count -ne $manifestEntries.Count) {
        throw "Package contains files not covered exactly once by SHA256SUMS.txt: $($unlistedFiles -join ', ')"
    }
    Write-Host "Manifest: $($manifestEntries.Count) files matched SHA256SUMS.txt with no unlisted payloads."
}


# --- 主验证流程 ---
$resolvedSkillRoot = (Resolve-Path -LiteralPath $SkillRoot).Path
$requiredFiles = @(
    "SKILL.md",
    "agents\openai.yaml",
    "pyproject.toml",
    "uv.lock",
    "scripts\fetch_bilibili.py",
    "scripts\runtime_output.py",
    "agent-harness\setup.py",
    "agent-harness\cli_anything\video_learning\video_learning_cli.py"
)
foreach ($relativePath in $requiredFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $resolvedSkillRoot $relativePath) -PathType Leaf)) {
        throw "Required file is missing: $relativePath"
    }
}

$sourceFiles = @(Get-ChildItem -LiteralPath $resolvedSkillRoot -Recurse -Force -File | Where-Object {
    $_.FullName -notmatch '\\.venv-gpu\\' -and              # 安装环境不是分享源码的一部分。
    $_.FullName -notmatch '\\__pycache__\\' -and
    $_.FullName -notmatch '\\.pytest_cache\\' -and
    $_.FullName -notmatch '\\.egg-info\\' -and
    $_.Extension -ne ".pyc"
})
$forbiddenFiles = @($sourceFiles | Where-Object {
    $_.Name -match '^(\.env|cookies?\.txt)$' -or $_.Extension -in @(".mp3", ".mp4", ".wav", ".mkv", ".pem", ".key")
})
if ($forbiddenFiles.Count -gt 0) {
    throw "Forbidden private/media files found: $($forbiddenFiles.FullName -join ', ')"
}
$reparseEntries = @(Get-ChildItem -LiteralPath $resolvedSkillRoot -Recurse -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint })
if ($reparseEntries.Count -gt 0) {
    throw "Reparse points are not allowed inside the shared Skill: $($reparseEntries.FullName -join ', ')"
}

$textFiles = @($sourceFiles | Where-Object { $_.Extension -in @(".md", ".py", ".toml", ".yaml", ".yml", ".json", ".txt") })
$machinePattern = 'C:\\Users\\[^\\]+|[D-F]:\\(?:CodexProjects|Notes|Archive|Study)|127\.0\.0\.1:\d{2,5}'
foreach ($file in $textFiles) {
    $text = [System.IO.File]::ReadAllText($file.FullName)
    if ([regex]::IsMatch($text, $machinePattern, [Text.RegularExpressions.RegexOptions]::IgnoreCase)) {
        throw "Share-owner path or proxy leaked into: $($file.FullName)"
    }
    if ([regex]::IsMatch($text, 'BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}')) {
        throw "Possible real credential material found in: $($file.FullName)"
    }
}
Write-Host "Source boundary: $($sourceFiles.Count) files; no private runtime, media, reparse point, owner path, or key material found."

Confirm-PackageManifest -ResolvedSkillRoot $resolvedSkillRoot
$runtimePython = Join-Path $resolvedSkillRoot ".venv-gpu\Scripts\python.exe"
$validationPython = Find-ValidationPython -InstalledRuntime $runtimePython
$syntaxProbe = @'
from pathlib import Path
import sys

root = Path(sys.argv[1])
files = [path for path in root.rglob('*.py') if '.venv-gpu' not in path.parts and '__pycache__' not in path.parts]
for path in files:
    compile(path.read_bytes(), str(path), 'exec')
print(f'Python syntax: {len(files)} files compiled in memory')
'@
Invoke-VerifyCommand -Program $validationPython -Arguments @("-c", $syntaxProbe, $resolvedSkillRoot)

if ($SkipRuntime) {
    Write-Host "VERIFY_OK (source-only; runtime and tests intentionally skipped)"
    exit 0
}
if (-not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
    throw "Runtime is missing. Run install_windows.ps1 without -SkipRuntime first."
}

$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCommand) {
    throw "uv was not found for dependency verification."
}
$env:BILIBILI_VIDEO_LEARNING_ROOT = $resolvedSkillRoot        # 测试只允许命中本次安装目标。
$env:BILIBILI_VIDEO_LEARNING_PYTHON = $runtimePython          # 后端必须使用同一锁定环境。
$env:CLI_ANYTHING_FORCE_INSTALLED = "1"                     # E2E 禁止回退到未安装源码入口。
$runtimeScripts = Split-Path -Parent $runtimePython
$env:Path = "$runtimeScripts;$env:Path"                      # 当前验证进程优先发现刚安装的 CLI。
Invoke-VerifyCommand -Program $uvCommand.Source -Arguments @("pip", "check", "--python", $runtimePython)
Invoke-VerifyCommand -Program $runtimePython -Arguments @("-m", "cli_anything.video_learning", "--version")

if (-not $SkipTests) {
    $testRoot = Join-Path $resolvedSkillRoot "agent-harness\cli_anything\video_learning\tests"
    Invoke-VerifyCommand -Program $runtimePython -Arguments @("-m", "pytest", $testRoot, "-q", "--tb=short")
}
Write-Host "VERIFY_OK"
