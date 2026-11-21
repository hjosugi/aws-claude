#!/bin/sh
# Build the cjp binary with PyInstaller (Linux or macOS).
# Usage: ./build.sh          # dist/cjp (one file)
#        ./build.sh --onedir # dist/cjp/cjp (faster start)
set -eu
cd "$(dirname "$0")"

mode="--onefile"
[ "${1:-}" = "--onedir" ] && mode="--onedir"
set -- "$mode" --clean --noconfirm --console --name cjp \
    --hidden-import openpyxl --hidden-import cjp_policy_embedded cjp.py

if command -v uv >/dev/null 2>&1; then
    uv run python cjp.py --cjp-embed-policy policy.yaml  # A bad policy stops the build.
    uv run --group build pyinstaller "$@"
else
    python3 -m venv .build-venv
    .build-venv/bin/python -m pip install --upgrade openpyxl pyyaml pyinstaller
    .build-venv/bin/python cjp.py --cjp-embed-policy policy.yaml
    .build-venv/bin/pyinstaller "$@"
fi
echo "Built: $(pwd)/dist/cjp"
