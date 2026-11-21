#!/usr/bin/env python3
"""cjp - start Claude Code with Amazon Bedrock, in Japan only.

The policy comes from policy.yaml.
  - Source mode (python cjp.py): reads policy.yaml next to this file.
  - Built exe: uses the policy embedded at build time. Users can not change it.

On every start:
  1. Lock the process env: Bedrock only, Japan region, allowed jp. models only.
  2. Make sure the helper tools are installed: uv, rtk, and graphify.
  3. Write the policy into ~/.claude/settings.json.
  4. In a git repo, add CLAUDE.md rules and refresh the code graph in the background.
  5. Run the real claude with the same arguments.

Extra commands (claude has no commands with these names):
  cjp xlsx-peek <file> [...]   small summary of an Excel file
  cjp cjp-policy               show the policy and the settings it writes
  cjp cjp-iam <account-id>     print an IAM policy that allows only these models

Env vars (they can not weaken the policy):
  CLAUDE_REAL_BIN       path to the real claude binary
  CJP_SKIP_SETUP=1      skip tool installs and project files (the policy still applies)
  CJP_NO_INSTALL=1      do not install missing tools
"""
import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

import yaml

APP = "cjp"
IS_WIN = os.name == "nt"
POLICY_FILE = "policy.yaml"
EMBED_MODULE = "cjp_policy_embedded"

JP_REGIONS = ("ap-northeast-1", "ap-northeast-3")
TIERS = ("haiku", "sonnet", "opus")
EFFORTS = ("low", "medium", "high", "xhigh")

READ_DENY = [
    "Read(*.xlsx)", "Read(*.xls)", "Read(*.log)", "Read(target/**)",
    "Read(build/**)", "Read(.gradle/**)", "Read(node_modules/**)",
]
GREP_DENY = ["Grep", "Bash(grep *)", "Bash(rg *)", "Bash(egrep *)", "Bash(git grep *)"]

# These env vars send requests to a place other than Bedrock in Japan. cjp removes them.
FORBIDDEN_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION",
)
# These env vars pick a model. cjp keeps them only when the value is an allowed model.
MODEL_ENV = (
    "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
)

CLAUDE_MD_MARKER = "## Data files (Excel, CSV, logs)"
CLAUDE_MD_SECTION = f"""
{CLAUDE_MD_MARKER}
- Do not read data files directly.
- For Excel, start with: cjp xlsx-peek <file>  (options: --sheet NAME --rows N --find TEXT)
- For other data, write a script in .claude/tools/ with PEP 723 metadata. Run it with uv run.
- Reuse scripts that are already in .claude/tools/.
- Print at most 50 lines. Print aggregates, not raw rows.
- Never use cat, less, or print(df) on data files.
- For source code, read only the lines you need. Use offset and limit.
"""

GRAPHIFY_PACKAGE = "graphifyy"  # The PyPI name has two y. Other names are not the real tool.
RTK_POSIX_INSTALL = "https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh"
RTK_WIN_ZIP = "https://github.com/rtk-ai/rtk/releases/latest/download/rtk-x86_64-pc-windows-msvc.zip"
UV_POSIX_INSTALL = "https://astral.sh/uv/install.sh"
UV_WIN_INSTALL = "https://astral.sh/uv/install.ps1"
LOCAL_BIN = Path.home() / ".local" / "bin"
EXTRA_BIN_DIRS = [LOCAL_BIN, Path.home() / ".cargo" / "bin"]
RETRY_AFTER_SEC = 24 * 3600  # Wait this long before we retry a failed install.


class PolicyError(Exception):
    pass


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def frozen():
    # True when this runs as a PyInstaller binary.
    return getattr(sys, "frozen", False)


def restore_env():
    # PyInstaller on Linux points LD_LIBRARY_PATH at its own temp libs. Undo it.
    if not frozen():
        return
    for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        orig = os.environ.pop(var + "_ORIG", None)
        if orig is not None:
            os.environ[var] = orig
        else:
            os.environ.pop(var, None)


def add_extra_bins_to_path():
    # Tools we install go to ~/.local/bin. Make sure claude and its hooks can find them.
    parts = os.environ.get("PATH", "").split(os.pathsep)
    for d in EXTRA_BIN_DIRS:
        if d.is_dir() and str(d) not in parts:
            parts.append(str(d))
    os.environ["PATH"] = os.pathsep.join(parts)


def self_path():
    # In a binary, __file__ points into a temp dir. Use the binary path instead.
    return Path(sys.executable if frozen() else __file__).resolve()


def note(msg):
    print(f"[{APP}] {msg}", file=sys.stderr)


def run(cmd, cwd=None, env=None, timeout=300):
    # Run a command quietly. Return (ok, output tail).
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout or "")[-1000:]


def which(name):
    # Look in PATH first, then in the usual install dirs. PATHEXT is used on Windows.
    found = shutil.which(name)
    if found:
        return found
    for d in EXTRA_BIN_DIRS:
        found = shutil.which(name, path=str(d))
        if found:
            return found
    return None


def config_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))


def cache_dir():
    if IS_WIN:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    d = base / APP
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_text(path):
    return path.read_text(encoding="utf-8-sig", errors="replace") if path.exists() else ""


def load_json(path):
    # Return (data, error). A missing file is an empty dict.
    if not path.exists():
        return {}, None
    try:
        return json.loads(read_text(path) or "{}"), None
    except ValueError as e:
        return None, str(e)


def write_json(path, data):
    # Keep one backup, then replace the file in one step. Always use LF.
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def append_text(path, text):
    # Append text and keep the file's line ending style (LF or CRLF).
    old = path.read_bytes() if path.exists() else b""
    eol = "\r\n" if b"\r\n" in old else "\n"
    chunk = text.replace("\r\n", "\n")
    if not old:
        chunk = chunk.lstrip("\n")
    elif not old.endswith(b"\n"):
        chunk = "\n" + chunk
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write(chunk.replace("\n", eol))


def safe(label, fn, *args):
    # Run one setup step. An error does not stop claude. sys.exit() still works.
    try:
        return fn(*args)
    except Exception as e:
        note(f"{label} failed: {e!r}")
        return None


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def is_jp_model(model_id):
    # A jp. geo profile, or an application inference profile in a Japan region.
    if model_id.startswith("jp."):
        return True
    return re.match(r"^arn:aws:bedrock:ap-northeast-[13]:\d{12}:application-inference-profile/", model_id) is not None


def validate_policy(raw):
    # Check the YAML and return a clean dict. Raise PolicyError on any problem.
    if not isinstance(raw, dict):
        raise PolicyError("the policy must be a YAML mapping")
    errors = []
    region = raw.get("region", "ap-northeast-1")
    if region not in JP_REGIONS:
        errors.append(f"region must be one of {JP_REGIONS}, got {region!r}")
    models = raw.get("models")
    if not isinstance(models, dict) or not models:
        errors.append("models must be a mapping with at least one tier")
        models = {}
    for tier, model_id in models.items():
        if tier not in TIERS:
            errors.append(f"models: unknown tier {tier!r} (use {TIERS})")
        if not isinstance(model_id, str) or not is_jp_model(model_id):
            errors.append(f"models.{tier}: {model_id!r} is not a jp. profile or a Japan ARN")
    default = raw.get("default_model", "haiku")
    if default not in models:
        errors.append(f"default_model {default!r} is not in models")
    window = raw.get("auto_compact_window", 100000)
    if not isinstance(window, int) or not 100000 <= window <= 1000000:
        errors.append("auto_compact_window must be an integer from 100000 to 1000000")
    effort = raw.get("effort_level", "low")
    if effort not in EFFORTS:
        errors.append(f"effort_level must be one of {EFFORTS}")
    tools = raw.get("tools") or {}
    if not isinstance(tools, dict):
        errors.append("tools must be a mapping")
        tools = {}
    if errors:
        raise PolicyError("\n".join(errors))
    return {
        "region": region,
        "models": dict(models),
        "default_model": default,
        "auto_compact_window": window,
        "effort_level": effort,
        "allow_web_fetch": bool(raw.get("allow_web_fetch", False)),
        "block_grep": bool(raw.get("block_grep", False)),
        "tools": {"rtk": bool(tools.get("rtk", True)), "graphify": bool(tools.get("graphify", True))},
    }


def load_policy():
    # Built exe: only the embedded policy. Source mode: policy.yaml next to this file.
    if frozen():
        try:
            import cjp_policy_embedded as embedded
        except ImportError:
            raise PolicyError("this exe has no embedded policy. Build it with build.ps1.")
        return validate_policy(yaml.safe_load(embedded.POLICY_YAML)), "embedded in the exe"
    path = Path(os.environ.get("CJP_POLICY", Path(__file__).with_name(POLICY_FILE)))
    if not path.exists():
        raise PolicyError(f"{path} was not found")
    return validate_policy(yaml.safe_load(path.read_text(encoding="utf-8"))), str(path)


def required_settings(p):
    # Turn the policy into Claude Code settings.
    env = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_REGION": p["region"],
        "ANTHROPIC_BEDROCK_REGION_PREFIX": "jp",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    for tier, model_id in p["models"].items():
        env[f"ANTHROPIC_DEFAULT_{tier.upper()}_MODEL"] = model_id
    if p["tools"]["graphify"]:
        env["GRAPHIFY_HOOK_STRICT"] = "1"  # Ask the graph before the first raw source read.
    deny = READ_DENY + ["WebSearch"]
    if not p["allow_web_fetch"]:
        deny.append("WebFetch")
    if p["block_grep"]:
        deny += GREP_DENY
    return {
        "model": p["default_model"],
        "availableModels": list(p["models"].values()),
        "enforceAvailableModels": True,
        "autoCompactWindow": p["auto_compact_window"],
        "effortLevel": p["effort_level"],
        "env": env,
        "permissions": {"deny": deny},
    }


def env_violation(key, value, allowed_models):
    # Return a reason if this env var breaks "Bedrock in Japan only", else None.
    if key in FORBIDDEN_ENV:
        return "not allowed (Bedrock only)"
    if key in MODEL_ENV and value not in allowed_models:
        return f"model {value!r} is not in the policy"
    if key == "ANTHROPIC_BEDROCK_BASE_URL" and not any(f".{r}." in value for r in JP_REGIONS):
        return "endpoint is not in a Japan region"
    if key == "CLAUDE_CODE_USE_BEDROCK" and value != "1":
        return "Bedrock must be on"
    return None


def clean_env(env, allowed_models):
    # Remove bad keys from an env mapping. Return the removed keys with reasons.
    bad = {k: env_violation(k, str(v), allowed_models) for k, v in env.items()}
    bad = {k: why for k, why in bad.items() if why}
    for k in bad:
        env.pop(k, None)
    return bad


def lock_process_env(p):
    # The process env wins for claude and every tool it starts.
    allowed = set(p["models"].values())
    for key, why in clean_env(os.environ, allowed).items():
        note(f"removed {key} from the environment: {why}")
    os.environ.update(required_settings(p)["env"])


def iam_policy(p, account):
    # An IAM policy that allows only the jp. profiles in the policy.
    if not re.fullmatch(r"\d{12}", account or ""):
        raise PolicyError("give a 12-digit AWS account ID")
    invoke = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    jp_ids = [m for m in p["models"].values() if m.startswith("jp.")]
    app_arns = [m for m in p["models"].values() if m.startswith("arn:")]
    profiles = [f"arn:aws:bedrock:{p['region']}:{account}:inference-profile/{m}" for m in jp_ids]
    models = [f"arn:aws:bedrock:{r}::foundation-model/{m[3:]}" for m in jp_ids for r in JP_REGIONS]
    statements = [
        {"Sid": "AllowJpProfiles", "Effect": "Allow", "Action": invoke, "Resource": profiles + app_arns},
        {"Sid": "AllowModelsOnlyViaJpProfiles", "Effect": "Allow", "Action": invoke, "Resource": models,
         "Condition": {"StringEquals": {"bedrock:InferenceProfileArn": profiles}}},
        {"Sid": "AllowProfileDiscovery", "Effect": "Allow",
         "Action": ["bedrock:ListInferenceProfiles", "bedrock:GetInferenceProfile"], "Resource": "*"},
        {"Sid": "DenyBedrockOutsideJapan", "Effect": "Deny", "Action": "bedrock:*", "Resource": "*",
         "Condition": {"StringNotEquals": {"aws:RequestedRegion": list(JP_REGIONS)}}},
    ]
    return {"Version": "2012-10-17", "Statement": statements}


def embed_policy(args):
    # Build step: check policy.yaml and write it into a module for PyInstaller.
    path = Path(args[0]) if args else Path(__file__).with_name(POLICY_FILE)
    text = path.read_text(encoding="utf-8")
    validate_policy(yaml.safe_load(text))  # A bad policy stops the build.
    out = Path(__file__).with_name(EMBED_MODULE + ".py")
    with open(out, "w", encoding="utf-8", newline="") as f:
        f.write("# Generated by: python cjp.py --cjp-embed-policy. Do not edit.\n")
        f.write(f"POLICY_YAML = {text!r}\n")
    print(f"embedded {path} into {out}")


# ---------------------------------------------------------------------------
# Helper tools
# ---------------------------------------------------------------------------


def recently_failed(tool):
    stamp = cache_dir() / f"{tool}.failed"
    return stamp.exists() and time.time() - stamp.stat().st_mtime < RETRY_AFTER_SEC


def install_tool(name, installer, label):
    # Install a missing tool once. Do not retry for a day after a failure.
    # installer is a command list, or a function that returns (ok, output).
    if os.environ.get("CJP_NO_INSTALL") == "1":
        note(f"{name} is missing (install is off)")
        return None
    if recently_failed(name):
        return None
    note(f"installing {name}: {label}")
    try:
        ok, out = installer() if callable(installer) else run(installer, timeout=600)
    except Exception as e:  # Network or zip errors.
        ok, out = False, repr(e)
    exe = which(name)
    if not ok or not exe:
        note(f"install of {name} failed. It will retry in 24h.\n{out}")
        (cache_dir() / f"{name}.failed").touch()
        return None
    return exe


def ensure_uv():
    # uv installs graphify and runs data scripts.
    exe = which("uv")
    if exe:
        return exe
    if IS_WIN:
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
               f"irm {UV_WIN_INSTALL} | iex"]
    else:
        cmd = ["sh", "-c", f"curl -LsSf {UV_POSIX_INSTALL} | sh"]
    return install_tool("uv", cmd, cmd[-1])


def install_rtk_windows():
    # Download the release zip and put rtk.exe in ~/.local/bin.
    LOCAL_BIN.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        zpath = Path(tmp) / "rtk.zip"
        urllib.request.urlretrieve(RTK_WIN_ZIP, zpath)  # Uses HTTPS_PROXY if set.
        with zipfile.ZipFile(zpath) as z:
            names = [n for n in z.namelist() if n.lower().endswith("rtk.exe")]
            if not names:
                return False, "rtk.exe was not found in the zip"
            with z.open(names[0]) as src, open(LOCAL_BIN / "rtk.exe", "wb") as dst:
                shutil.copyfileobj(src, dst)
    return True, f"installed to {LOCAL_BIN}"


def is_token_killer(exe):
    # Another crate is also named rtk (Rust Type Kit).
    # The token tool lists a "gain" command and talks about LLM tokens.
    try:
        r = subprocess.run([exe, "--help"], stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    text = (r.stdout + r.stderr).lower()
    return "gain" in text and ("token" in text or "llm" in text)


def ensure_rtk(settings_path):
    exe = which("rtk")
    if not exe:
        if IS_WIN:
            exe = install_tool("rtk", install_rtk_windows, RTK_WIN_ZIP)
        else:
            exe = install_tool("rtk", ["sh", "-c", f"curl -fsSL {RTK_POSIX_INSTALL} | sh"], RTK_POSIX_INSTALL)
    if not exe:
        return
    if not is_token_killer(exe):
        note(f"{exe} does not look like rtk (Rust Token Killer). Skipped.")
        return
    settings, err = load_json(settings_path)
    if err:
        return  # The settings step reports this error.
    if "rtk" not in json.dumps(settings.get("hooks", {})):
        ok, out = run([exe, "init", "-g", "--auto-patch"], timeout=60)
        note("added the rtk hook" if ok else f"rtk init failed:\n{out}")


def ensure_graphify(uv):
    exe = which("graphify")
    if not exe:
        if uv:
            cmd = [uv, "tool", "install", GRAPHIFY_PACKAGE]
        elif which("pipx"):
            cmd = ["pipx", "install", GRAPHIFY_PACKAGE]
        else:
            note("graphify is missing and neither uv nor pipx was found")
            return None
        exe = install_tool("graphify", cmd, " ".join(cmd))
    if not exe:
        return None
    # Register the /graphify skill for Claude Code once.
    if not (config_dir() / "skills" / "graphify").exists():
        cmd = [exe, "install"] + (["--platform", "windows"] if IS_WIN else [])
        ok, out = run(cmd, timeout=120)
        note("registered the graphify skill" if ok else f"graphify install failed:\n{out}")
    return exe


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def apply_required(current, required, changes, prefix=""):
    # The policy wins. Lists named "deny" only need these entries; others must match.
    for key, want in required.items():
        name = f"{prefix}{key}"
        if isinstance(want, dict):
            if not isinstance(current.get(key), dict):
                current[key] = {}
            apply_required(current[key], want, changes, name + ".")
        elif isinstance(want, list) and key == "deny" and isinstance(current.get(key), list):
            missing = [x for x in want if x not in current[key]]
            if missing:
                current[key].extend(missing)
                changes.append(f"added to {name}: {missing}")
        elif current.get(key) != want:
            current[key] = copy.deepcopy(want)
            changes.append(f"set {name}")


def ensure_settings(settings_path, p):
    settings, err = load_json(settings_path)
    if err:
        note(f"{settings_path} is not valid JSON. Not changed: {err}")
        return
    changes = []
    if isinstance(settings.get("env"), dict):
        for key, why in clean_env(settings["env"], set(p["models"].values())).items():
            changes.append(f"removed env.{key} ({why})")
    apply_required(settings, required_settings(p), changes)
    if changes:
        write_json(settings_path, settings)
        note(f"updated {settings_path}: " + "; ".join(changes))


def check_project_settings(p):
    # Project settings override user settings. A shared file with bad env stops cjp.
    root = git_root()
    if root is None:
        return
    allowed = set(p["models"].values())
    for rel, can_fix in ((".claude/settings.local.json", True), (".claude/settings.json", False)):
        path = root / rel
        data, err = load_json(path)
        if err or not isinstance(data, dict) or not isinstance(data.get("env"), dict):
            continue
        env = dict(data["env"])
        bad = clean_env(env, allowed)
        if not bad:
            continue
        if can_fix:
            data["env"] = env
            write_json(path, data)
            note(f"removed {sorted(bad)} from {path}")
        else:
            sys.exit(f"[{APP}] {path} sets env that breaks the policy: {bad}. "
                     "Remove it, or ask the repo owner.")


# ---------------------------------------------------------------------------
# Project files
# ---------------------------------------------------------------------------


def git_root():
    ok, out = run(["git", "rev-parse", "--show-toplevel"], timeout=10)
    return Path(out.strip()) if ok and out.strip() else None


def ensure_claude_md(root, graphify):
    path = root / "CLAUDE.md"
    # Let graphify write its own rules, so Claude asks the graph before grep.
    if graphify and "graphify" not in read_text(path).lower():
        ok, out = run([graphify, "claude", "install"], cwd=root, timeout=120)
        note("added graphify rules to CLAUDE.md" if ok else f"graphify claude install failed:\n{out}")
    if CLAUDE_MD_MARKER not in read_text(path):
        append_text(path, CLAUDE_MD_SECTION)
        note(f"added data file rules to {path}")


def ensure_git_exclude(root, pattern="graphify-out/"):
    # Keep the graph out of git without editing the tracked .gitignore.
    ok, out = run(["git", "rev-parse", "--git-path", "info/exclude"], cwd=root, timeout=10)
    if not ok or not out.strip():
        return
    path = Path(out.strip())
    if not path.is_absolute():
        path = root / path
    if pattern not in read_text(path).splitlines():
        append_text(path, pattern + "\n")


def pid_alive(pid):
    if IS_WIN:
        # os.kill(pid, 0) would end the process on Windows, so ask the kernel instead.
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            # 259 is STILL_ACTIVE.
            return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    # On Linux, a finished child that nobody reaped is a zombie. Treat it as done.
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, IndexError):
            pass
    return True


def refresh_graph(root, graphify):
    # Build or update the code graph in the background. No LLM is used.
    key = hashlib.sha1(str(root).encode()).hexdigest()[:12]
    pid_file = cache_dir() / f"graph-{key}.pid"
    if pid_file.exists():
        try:
            if pid_alive(int(pid_file.read_text().strip())):
                return  # A build for this repo is still running.
        except ValueError:
            pass
    if (root / "graphify-out" / "graph.json").exists():
        cmd = [graphify, "update", "."]
    else:
        cmd = [graphify, "extract", ".", "--code-only"]
        note(f"building the code graph in the background: {' '.join(cmd)}")
    env = dict(os.environ, GRAPHIFY_BACKEND="none")
    if IS_WIN:
        # No console window, and Ctrl+C in the terminal does not stop it.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        detach = {"creationflags": flags}
    else:
        detach = {"start_new_session": True}
    with open(cache_dir() / f"graph-{key}.log", "w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, **detach)
    pid_file.write_text(str(proc.pid))


def ensure_project(graphify):
    root = git_root()
    if root is None:
        return  # Not a git repo. Do not create project files here.
    ensure_claude_md(root, graphify)
    if graphify:
        ensure_git_exclude(root)
        refresh_graph(root, graphify)


# ---------------------------------------------------------------------------
# xlsx-peek: small summary of an Excel file (openpyxl is inside the exe)
# ---------------------------------------------------------------------------


def _short(value, width):
    # Turn a cell value into one short line.
    if value is None:
        return ""
    text = str(value).replace("\n", " ").replace("\t", " ")
    return text if len(text) <= width else text[: width - 3] + "..."


def xlsx_peek(argv):
    from openpyxl import load_workbook

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Japanese text on Windows.
    parser = argparse.ArgumentParser(prog=f"{APP} xlsx-peek", description="Small summary of an Excel file")
    parser.add_argument("path")
    parser.add_argument("--sheet", help="only this sheet")
    parser.add_argument("--rows", type=int, default=5, help="rows to show per sheet")
    parser.add_argument("--width", type=int, default=30, help="max chars per cell")
    parser.add_argument("--find", help="search text in all cells")
    parser.add_argument("--limit", type=int, default=20, help="max search hits")
    args = parser.parse_args(argv)

    # read_only mode streams rows, so big files use little memory.
    # data_only=False keeps formulas as text, like "=SUM(A1:A9)".
    wb = load_workbook(args.path, read_only=True, data_only=False)
    names = [args.sheet] if args.sheet else wb.sheetnames
    for name in names:
        if name not in wb.sheetnames:
            sys.exit(f"sheet not found: {name}. sheets: {wb.sheetnames}")
    print(f"# file: {args.path}  sheets: {wb.sheetnames}\n")
    if args.find:
        needle, hits = args.find.lower(), 0
        for name in names:
            for row in wb[name].iter_rows():
                for cell in row:
                    if cell.value is not None and needle in str(cell.value).lower():
                        print(f"{name}!{cell.coordinate}\t{_short(cell.value, args.width)}")
                        hits += 1
                        if hits >= args.limit:
                            print(f"(stopped at {args.limit} hits)")
                            wb.close()
                            return
        print(f"({hits} hits)")
    else:
        for name in names:
            ws = wb[name]
            print(f"## sheet: {ws.title}  size: {ws.max_row} rows x {ws.max_column} cols")
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= args.rows:
                    break
                print("\t".join(_short(v, args.width) for v in row))
            print()
    wb.close()


# ---------------------------------------------------------------------------
# Run the real claude
# ---------------------------------------------------------------------------


def find_real_claude():
    env_bin = os.environ.get("CLAUDE_REAL_BIN")
    if env_bin:
        return env_bin
    me = self_path()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        cand = shutil.which("claude", path=d)
        if cand and Path(cand).resolve() != me:
            return cand
    return None


def run_claude(real, argv):
    args = [real] + argv
    if IS_WIN:
        # Windows has no exec. Wait for claude and pass its exit code.
        # Ignore Ctrl+C here, so only claude reacts to it.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, signal.SIG_IGN)
        sys.exit(subprocess.call(args))
    os.execv(real, args)


def main():
    restore_env()
    argv = sys.argv[1:]
    cmd = argv[0] if argv else ""

    if cmd == "xlsx-peek":
        return xlsx_peek(argv[1:])
    if cmd == "--cjp-embed-policy":
        if frozen():
            sys.exit(f"[{APP}] the built exe can not change its policy.")
        return embed_policy(argv[1:])

    try:
        policy, source = load_policy()
        if cmd == "cjp-iam":
            print(json.dumps(iam_policy(policy, argv[1] if len(argv) > 1 else ""), indent=2))
            return None
    except PolicyError as e:
        sys.exit(f"[{APP}] policy error:\n{e}")
    if cmd == "cjp-policy":
        print(f"# source: {source}")
        print(yaml.safe_dump(policy, sort_keys=False, allow_unicode=True))
        print("# settings written to ~/.claude/settings.json (also usable as managed-settings.json):")
        print(json.dumps(required_settings(policy), indent=2))
        return None
    if any(a == "--settings" or a.startswith("--settings=") for a in argv):
        sys.exit(f"[{APP}] --settings is blocked, because it can change the env and models.")

    lock_process_env(policy)
    add_extra_bins_to_path()
    settings_path = config_dir() / "settings.json"
    setup = os.environ.get("CJP_SKIP_SETUP") != "1"
    uv = graphify = None
    if setup:
        uv = safe("uv check", ensure_uv)
        if policy["tools"]["rtk"]:
            safe("rtk check", ensure_rtk, settings_path)
        if policy["tools"]["graphify"]:
            graphify = safe("graphify check", ensure_graphify, uv)
    safe("settings check", ensure_settings, settings_path, policy)
    safe("project settings check", check_project_settings, policy)  # It can stop cjp on purpose.
    if setup:
        safe("project check", ensure_project, graphify)
    add_extra_bins_to_path()  # New installs may have created ~/.local/bin.

    real = find_real_claude()
    if not real:
        sys.exit(f"[{APP}] the real claude was not found. Set CLAUDE_REAL_BIN.")
    run_claude(real, argv)
    return None


if __name__ == "__main__":
    main()
