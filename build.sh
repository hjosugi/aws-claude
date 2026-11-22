#!/bin/sh
# Build the aws-claude binary with PyInstaller (Linux or macOS).
# Usage: ./build.sh                       # dist/aws-claude (one file)
#        ./build.sh --onedir              # dist/aws-claude/aws-claude (faster start)
#        ./build.sh --env-file .env.prod  # build settings from another file (default: .env)
set -eu
cd "$(dirname "$0")"

mode="--onefile"
env_file=""
while [ $# -gt 0 ]; do
    case "$1" in
        --onedir) mode="--onedir" ;;
        --env-file) env_file="$2"; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done
set -- "$mode" --clean --noconfirm --console --name aws-claude \
    --hidden-import openpyxl --hidden-import aws_claude_policy_embedded aws_claude.py

# Merge policy.yaml and .env, and embed the result. A bad policy stops the build.
embed() {
    if [ -n "$env_file" ]; then
        "$@" aws_claude.py --aws-claude-embed-policy policy.yaml --env-file "$env_file"
    else
        "$@" aws_claude.py --aws-claude-embed-policy policy.yaml
    fi
}

if command -v uv >/dev/null 2>&1; then
    embed uv run python
    uv run --group build pyinstaller "$@"
else
    python3 -m venv .build-venv
    .build-venv/bin/python -m pip install --upgrade openpyxl pyyaml pyinstaller
    embed .build-venv/bin/python
    .build-venv/bin/pyinstaller "$@"
fi
echo "Built: $(pwd)/dist/aws-claude"
