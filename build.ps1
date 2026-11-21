# Build cjp.exe with PyInstaller. Run this on Windows.
# PyInstaller can not build a Windows exe on Linux.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\build.ps1          # dist\cjp.exe (one file)
#   powershell -ExecutionPolicy Bypass -File .\build.ps1 -OneDir  # dist\cjp\cjp.exe (faster start)
param([switch]$OneDir)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$mode = if ($OneDir) { "--onedir" } else { "--onefile" }
$opts = @($mode, "--clean", "--noconfirm", "--console", "--name", "cjp",
          "--hidden-import", "openpyxl", "--hidden-import", "cjp_policy_embedded", "cjp.py")

if (Get-Command uv -ErrorAction SilentlyContinue) {
    # 1. Check policy.yaml and embed it. A bad policy stops the build.
    uv run python cjp.py --cjp-embed-policy policy.yaml
    if ($LASTEXITCODE -ne 0) { throw "policy check failed" }
    # 2. Build the exe in the project env (openpyxl, pyyaml, pyinstaller).
    uv run --group build pyinstaller @opts
} else {
    py -m venv .build-venv
    .\.build-venv\Scripts\python.exe -m pip install --upgrade openpyxl pyyaml pyinstaller
    .\.build-venv\Scripts\python.exe cjp.py --cjp-embed-policy policy.yaml
    if ($LASTEXITCODE -ne 0) { throw "policy check failed" }
    .\.build-venv\Scripts\pyinstaller.exe @opts
}
if ($LASTEXITCODE -ne 0) { throw "build failed" }

$out = if ($OneDir) { "dist\cjp\cjp.exe" } else { "dist\cjp.exe" }
Write-Host "Built: $PSScriptRoot\$out"
