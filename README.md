# cjp

`cjp` starts Claude Code with Amazon Bedrock, in Japan only.
It locks the env, writes the policy into `~/.claude/settings.json`, sets up helper tools, then runs the real `claude` with the same arguments.

See `HANDOVER.md` for decisions, status, and open tasks.

## Use

```powershell
cjp                      # same as claude, but Bedrock + Japan only
cjp -p "explain this"    # all args go to claude
cjp xlsx-peek book.xlsx  # small summary of an Excel file (no Python needed)
cjp cjp-policy           # show the policy and the settings it writes
cjp cjp-iam 123456789012 # print an IAM policy for the allowed models
```

## Install (Windows)

1. Copy `dist\cjp.exe` to `%USERPROFILE%\.local\bin\` (the native Claude Code installer puts this folder on PATH).
2. Run `cjp cjp-policy` to check it.
3. Use `cjp` instead of `claude`.

## Change the allowed models

Only the source can change the policy. The built exe can not.

1. Edit `policy.yaml`.
2. Build again on Windows: `powershell -ExecutionPolicy Bypass -File .\build.ps1`
   (or push to GitHub and use `.github/workflows/build-windows.yml`).
3. A bad policy (for example a non-jp. model) stops the build.

## Files

| File | What it is |
|---|---|
| `cjp.py` | The launcher (one file) |
| `policy.yaml` | Allowed models and other rules |
| `build.ps1` / `build.sh` | Build with PyInstaller (Windows / Linux) |
| `.github/workflows/build-windows.yml` | Build `cjp.exe` on a Windows runner |
| `pyproject.toml` | Python deps for `uv run` |
| `dist/cjp.exe` | Prebuilt exe (built with Wine, not signed) |
| `HANDOVER.md` | Handover notes |
