# Build aws-claude.exe with PyInstaller. Run this on Windows.
# PyInstaller can not build a Windows exe on Linux.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\build.ps1                     # dist\aws-claude.exe (one file)
#   powershell -ExecutionPolicy Bypass -File .\build.ps1 -OneDir             # dist\aws-claude\aws-claude.exe (faster start)
#   powershell -ExecutionPolicy Bypass -File .\build.ps1 -EnvFile .env.prod  # build settings from another file (default: .env)
param([switch]$OneDir, [string]$EnvFile)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$mode = if ($OneDir) { "--onedir" } else { "--onefile" }
$opts = @($mode, "--clean", "--noconfirm", "--console", "--name", "aws-claude",
          "--hidden-import", "openpyxl", "--hidden-import", "aws_claude_policy_embedded", "aws_claude.py")
# 1. Merge policy.yaml and .env, and embed the result. A bad policy stops the build.
$embed = @("aws_claude.py", "--aws-claude-embed-policy", "policy.yaml")
if ($EnvFile) { $embed += @("--env-file", $EnvFile) }

if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv run python @embed
    if ($LASTEXITCODE -ne 0) { throw "policy check failed" }
    # 2. Build the exe in the project env (openpyxl, pyyaml, pyinstaller).
    uv run --group build pyinstaller @opts
} else {
    py -m venv .build-venv
    .\.build-venv\Scripts\python.exe -m pip install --upgrade openpyxl pyyaml pyinstaller
    .\.build-venv\Scripts\python.exe @embed
    if ($LASTEXITCODE -ne 0) { throw "policy check failed" }
    .\.build-venv\Scripts\pyinstaller.exe @opts
}
if ($LASTEXITCODE -ne 0) { throw "build failed" }

$out = if ($OneDir) { "dist\aws-claude\aws-claude.exe" } else { "dist\aws-claude.exe" }
Write-Host "Built: $PSScriptRoot\$out"
