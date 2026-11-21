#!/usr/bin/env python3
"""cjp - start Claude Code with Amazon Bedrock, in Japan only.

The policy comes from policy.yaml. Values in .env replace values in policy.yaml.
  - Source mode (python cjp.py): reads policy.yaml and .env next to this file.
  - Built exe: uses the policy embedded at build time. Users can not change it.

On every start:
  1. Lock the process env: Bedrock only, Japan region, allowed jp. models only,
     and the AWS profile of the policy (no claude.ai login, no API key).
  2. Make sure the helper tools are installed: uv, rtk, and graphify.
  3. Write the policy into ~/.claude/settings.json.
  4. In a git repo, add CLAUDE.md rules and refresh the code graph in the background.
  5. Start a local proxy that only lets the allowed hosts through.
  6. Run the real claude with the same arguments, behind that proxy.

Extra commands (claude has no commands with these names):
  cjp xlsx-peek <file> [...]   small summary of an Excel file
  cjp cjp-policy               show the policy and the settings it writes
  cjp cjp-iam [account-id]     print an IAM policy that allows only these models

Env vars (they can not weaken the policy):
  CLAUDE_REAL_BIN       path to the real claude binary
  CJP_SKIP_SETUP=1      skip tool installs and project files (the policy still applies)
  CJP_NO_INSTALL=1      do not install missing tools
  CJP_POLICY            source mode only: path to another policy.yaml
  CJP_ENV_FILE          source mode and build only: path to another .env
"""
import argparse
import base64
import configparser
import copy
import functools
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import yaml

APP = "cjp"
IS_WIN = os.name == "nt"
POLICY_FILE = "policy.yaml"
ENV_FILE = ".env"
EMBED_MODULE = "cjp_policy_embedded"

# Keys for .env (see .env.example). Each key replaces one value of policy.yaml.
# Two special cases: a model key with the value "none" removes that model, and
# CJP_PROXY_ADD_HOSTS adds hosts to the list in policy.yaml.
ENV_KEYS = {
    "CJP_REGION": ("region", str),
    "CJP_MODEL_HAIKU": ("models.haiku", str),
    "CJP_MODEL_SONNET": ("models.sonnet", str),
    "CJP_MODEL_OPUS": ("models.opus", str),
    "CJP_DEFAULT_MODEL": ("default_model", str),
    "CJP_AUTO_COMPACT_WINDOW": ("auto_compact_window", int),
    "CJP_EFFORT_LEVEL": ("effort_level", str),
    "CJP_ALLOW_WEB_FETCH": ("allow_web_fetch", bool),
    "CJP_BLOCK_GREP": ("block_grep", bool),
    "CJP_TOOL_RTK": ("tools.rtk", bool),
    "CJP_TOOL_GRAPHIFY": ("tools.graphify", bool),
    "CJP_AWS_PROFILE": ("aws.profile", str),
    "CJP_AWS_ROLE_ARN": ("aws.role_arn", str),
    "CJP_AWS_SOURCE_PROFILE": ("aws.source_profile", str),
    "CJP_AWS_AUTH_REFRESH": ("aws.auth_refresh", str),
    "CJP_PROXY": ("proxy.enabled", bool),
    "CJP_PROXY_UPSTREAM": ("proxy.upstream", str),
    "CJP_PROXY_ADD_HOSTS": ("proxy.allow_hosts", list),
}
BOOL_WORDS = {"1": True, "true": True, "yes": True, "on": True,
              "0": False, "false": False, "no": False, "off": False}

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
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",  # claude.ai subscription login
)
# These env vars give AWS credentials without the profile. When the policy names a
# profile, cjp removes them. A Bedrock API key would even skip the credential chain.
AWS_CRED_ENV = (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK", "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE",
)
PROFILE_RE = r"[A-Za-z0-9_.@+-]+"
ROLE_ARN_RE = r"arn:aws:iam::\d{12}:role/[\w+=,.@/-]+"
HOST_RE = r"(\*\.)?[a-z0-9-]+(\.[a-z0-9-]+)*"

# The proxy always allows these AWS services in the Japan regions:
# inference, model list, role login (STS), and IAM Identity Center login.
AWS_SERVICES = ("bedrock-runtime", "bedrock", "sts", "oidc", "portal.sso")
PROXY_URL_ENV = ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY")  # Claude Code reads them in this order.
LOOPBACK = "localhost,127.0.0.1,::1"
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


def parse_dotenv(text):
    # KEY=VALUE lines. "#" starts a comment. Quotes around a value are removed.
    values = {}
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise PolicyError(f".env line {n}: expected KEY=VALUE")
        if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]:
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        values[key] = value
    return values


def env_overrides(env_file):
    # The CJP_ keys from the .env file, then from the process env (the process env wins).
    # An empty value means "not set".
    values = {}
    if env_file.exists():
        for key, value in parse_dotenv(read_text(env_file)).items():
            if key in ENV_KEYS:
                values[key] = value
            elif key.startswith("CJP_"):
                raise PolicyError(f"{env_file}: unknown key {key}. Known keys: {', '.join(ENV_KEYS)}")
    for key in ENV_KEYS:
        if key in os.environ:
            values[key] = os.environ[key]
    return {key: value for key, value in values.items() if value}


def apply_overrides(raw, values):
    # Put the .env values into the raw policy.
    for key, text in values.items():
        path, kind = ENV_KEYS[key]
        node = raw
        *parents, leaf = path.split(".")
        for name in parents:
            if not isinstance(node.get(name), dict):
                node[name] = {}
            node = node[name]
        if kind is bool:
            if text.lower() not in BOOL_WORDS:
                raise PolicyError(f"{key}: use true or false, got {text!r}")
            node[leaf] = BOOL_WORDS[text.lower()]
        elif kind is int:
            if not text.isdigit():
                raise PolicyError(f"{key}: use a number, got {text!r}")
            node[leaf] = int(text)
        elif kind is list:
            old = node.get(leaf) if isinstance(node.get(leaf), list) else []
            node[leaf] = old + [x for x in re.split(r"[,\s]+", text) if x]
        elif parents == ["models"] and text.lower() == "none":
            node.pop(leaf, None)
        else:
            node[leaf] = text


def read_policy(policy_path, env_file):
    # policy.yaml plus the .env values. Return (clean policy, the .env keys that were used).
    if not policy_path.exists():
        raise PolicyError(f"{policy_path} was not found")
    raw = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise PolicyError("the policy must be a YAML mapping")
    values = env_overrides(env_file)
    apply_overrides(raw, values)
    return validate_policy(raw), sorted(values)


def parse_proxy_url(url):
    # Return (host, port, basic auth or "") for an http:// proxy URL, else None.
    try:
        u = urllib.parse.urlsplit(url)
        host, port = u.hostname, u.port or 80
    except ValueError:
        return None
    if u.scheme != "http" or not host:
        return None
    auth = ""
    if u.username:
        login = f"{urllib.parse.unquote(u.username)}:{urllib.parse.unquote(u.password or '')}"
        auth = base64.b64encode(login.encode("utf-8")).decode("ascii")
    return host, port, auth


def mask_url(url):
    # Hide a user name and password before the URL is printed.
    return re.sub(r"//[^@/]*@", "//***@", url)


def is_jp_bedrock_url(url):
    # https, and an AWS host in a Japan region: the normal endpoint or a VPC endpoint.
    try:
        u = urllib.parse.urlsplit(url)
        host = (u.hostname or "").lower()
    except ValueError:
        return False
    return u.scheme == "https" and any(
        host.endswith((f".{region}.amazonaws.com", f".{region}.vpce.amazonaws.com")) for region in JP_REGIONS)


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
    aws = raw.get("aws") or {}
    if not isinstance(aws, dict):
        errors.append("aws must be a mapping")
        aws = {}
    aws = {k: str(aws.get(k) or "").strip() for k in ("profile", "role_arn", "source_profile", "auth_refresh")}
    if aws["role_arn"]:
        if not re.fullmatch(ROLE_ARN_RE, aws["role_arn"]):
            errors.append(f"aws.role_arn: {aws['role_arn']!r} is not an IAM role ARN")
        aws["profile"] = aws["profile"] or APP
        aws["source_profile"] = aws["source_profile"] or "default"
        if aws["profile"] == aws["source_profile"]:
            errors.append("aws.profile and aws.source_profile must be different")
    elif aws["source_profile"]:
        errors.append("aws.source_profile needs aws.role_arn")
    for key in ("profile", "source_profile"):
        if aws[key] and not re.fullmatch(PROFILE_RE, aws[key]):
            errors.append(f"aws.{key}: {aws[key]!r} is not a profile name")
    proxy = raw.get("proxy") or {}
    if not isinstance(proxy, dict):
        errors.append("proxy must be a mapping")
        proxy = {}
    upstream = str(proxy.get("upstream") or "").strip()
    if upstream and parse_proxy_url(upstream) is None:
        errors.append(f"proxy.upstream: use http://host:port, got {mask_url(upstream)!r}")
    hosts = proxy.get("allow_hosts") or []
    if not isinstance(hosts, list):
        errors.append("proxy.allow_hosts must be a list")
        hosts = []
    hosts = list(dict.fromkeys(str(h).strip().lower() for h in hosts))  # No doubles, same order.
    for host in hosts:
        if not re.fullmatch(HOST_RE, host):
            errors.append(f"proxy.allow_hosts: {host!r} is not a host name (use name or *.name)")
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
        "aws": aws,
        "proxy": {"enabled": bool(proxy.get("enabled", True)), "upstream": upstream, "allow_hosts": hosts},
    }


def load_policy():
    # Built exe: only the embedded policy. Source mode: policy.yaml and .env next to this file.
    if frozen():
        try:
            import cjp_policy_embedded as embedded
        except ImportError:
            raise PolicyError("this exe has no embedded policy. Build it with build.ps1.")
        return validate_policy(yaml.safe_load(embedded.POLICY_YAML)), "embedded in the exe"
    here = Path(__file__)
    path = Path(os.environ.get("CJP_POLICY") or here.with_name(POLICY_FILE))
    env_file = Path(os.environ.get("CJP_ENV_FILE") or here.with_name(ENV_FILE))
    policy, keys = read_policy(path, env_file)
    return policy, str(path) + (f", with {', '.join(keys)} from .env or the environment" if keys else "")


def required_settings(p):
    # Turn the policy into Claude Code settings.
    env = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_REGION": p["region"],
        "ANTHROPIC_BEDROCK_REGION_PREFIX": "jp",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        # "Nonessential traffic" does not cover the next two. Both go to hosts outside AWS:
        # MCP servers from a claude.ai account, and the plugin marketplace.
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
    }
    for tier, model_id in p["models"].items():
        env[f"ANTHROPIC_DEFAULT_{tier.upper()}_MODEL"] = model_id
    if p["aws"]["profile"]:
        env["AWS_PROFILE"] = p["aws"]["profile"]
    if p["tools"]["graphify"]:
        env["GRAPHIFY_HOOK_STRICT"] = "1"  # Ask the graph before the first raw source read.
    deny = READ_DENY + ["WebSearch"]
    if not p["allow_web_fetch"]:
        deny.append("WebFetch")
    if p["block_grep"]:
        deny += GREP_DENY
    settings = {
        "model": p["default_model"],
        "availableModels": list(p["models"].values()),
        "enforceAvailableModels": True,
        "autoCompactWindow": p["auto_compact_window"],
        "effortLevel": p["effort_level"],
        # Without this, WebFetch sends each host name to api.anthropic.com before it fetches.
        "skipWebFetchPreflight": True,
        "env": env,
        "permissions": {"deny": deny},
    }
    if p["aws"]["auth_refresh"]:
        settings["awsAuthRefresh"] = p["aws"]["auth_refresh"]
    return settings


def forbidden_settings(p):
    # Settings keys that would give claude other AWS credentials than the profile of the policy.
    return ["awsCredentialExport"] if p["aws"]["profile"] else []


def env_violation(key, value, p):
    # Return a reason if this env var breaks the policy, else None.
    if key in FORBIDDEN_ENV:
        return "not allowed (Bedrock only)"
    if key in MODEL_ENV and value not in p["models"].values():
        return f"model {value!r} is not in the policy"
    if key == "ANTHROPIC_BEDROCK_BASE_URL" and not is_jp_bedrock_url(value):
        return "endpoint is not an AWS host in a Japan region"
    if key == "CLAUDE_CODE_USE_BEDROCK" and value != "1":
        return "Bedrock must be on"
    profile = p["aws"]["profile"]
    if profile and (key in AWS_CRED_ENV or (key == "AWS_PROFILE" and value != profile)):
        return f"the policy uses the AWS profile {profile!r}"
    return None


def clean_env(env, p):
    # Remove bad keys from an env mapping. Return the removed keys with reasons.
    bad = {k: env_violation(k, str(v), p) for k, v in env.items()}
    bad = {k: why for k, why in bad.items() if why}
    for k in bad:
        env.pop(k, None)
    return bad


def lock_process_env(p):
    # For the tools that cjp starts. For claude, the env in the settings wins over the
    # process env, so ensure_settings() writes the same values there.
    for key, why in clean_env(os.environ, p).items():
        note(f"removed {key} from the environment: {why}")
    os.environ.update(required_settings(p)["env"])


def iam_policy(p, account):
    # An IAM policy that allows only the jp. profiles in the policy.
    if not account and p["aws"]["role_arn"]:
        account = p["aws"]["role_arn"].split(":")[4]  # The account of the role in the policy.
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
    # Build step: merge policy.yaml and .env, check the result, and write it into a
    # module for PyInstaller. Usage: --cjp-embed-policy [policy.yaml] [--env-file FILE]
    here = Path(__file__)
    path, env_file = here.with_name(POLICY_FILE), None
    args = list(args)
    while args:
        arg = args.pop(0)
        if arg != "--env-file":
            path = Path(arg)
        elif not args:
            raise PolicyError("--env-file needs a path")
        else:
            env_file = Path(args.pop(0))
            if not env_file.exists():
                raise PolicyError(f"{env_file} was not found")
    if env_file is None:
        env_file = Path(os.environ.get("CJP_ENV_FILE") or here.with_name(ENV_FILE))
    policy, keys = read_policy(path, env_file)  # A bad policy stops the build.
    text = yaml.safe_dump(policy, sort_keys=False, allow_unicode=True)
    out = here.with_name(EMBED_MODULE + ".py")
    with open(out, "w", encoding="utf-8", newline="") as f:
        f.write("# Generated by: python cjp.py --cjp-embed-policy. Do not edit.\n")
        f.write(f"POLICY_YAML = {text!r}\n")
    print(f"embedded {path} into {out}" + (f" (values from .env: {', '.join(keys)})" if keys else ""))


# ---------------------------------------------------------------------------
# AWS profile
# ---------------------------------------------------------------------------


def aws_file(env_name, name):
    return Path(os.environ.get(env_name) or Path.home() / ".aws" / name)


def aws_profiles(path):
    # The profiles in an AWS config or credentials file: {name: {option: value}}.
    parser = configparser.RawConfigParser(strict=False, allow_no_value=True)
    parser.read_string(read_text(path))
    profiles = {}
    for section in parser.sections():
        name = section[len("profile "):].strip() if section.startswith("profile ") else section
        profiles[name] = dict(parser.items(section))
    return profiles


def ensure_aws_profile(p):
    # The policy can name the AWS profile. With a role ARN, cjp also creates that profile.
    aws = p["aws"]
    name = aws["profile"]
    if not name:
        return
    config = aws_file("AWS_CONFIG_FILE", "config")
    known = aws_profiles(config)
    logins = aws_profiles(aws_file("AWS_SHARED_CREDENTIALS_FILE", "credentials"))
    if not aws["role_arn"]:
        if name not in known and name not in logins:
            note(f"the AWS profile {name!r} was not found in {config}. Create it, then start {APP} again.")
        return
    if name not in known:
        header = "default" if name == "default" else f"profile {name}"
        append_text(config, f"\n[{header}]\nrole_arn = {aws['role_arn']}\n"
                            f"source_profile = {aws['source_profile']}\nregion = {p['region']}\n")
        note(f"added the AWS profile {name!r} for {aws['role_arn']} to {config}")
    elif known[name].get("role_arn") != aws["role_arn"]:
        sys.exit(f"[{APP}] the AWS profile {name!r} in {config} does not use the role of the policy "
                 f"({aws['role_arn']}). Fix or remove that profile.")
    source = aws["source_profile"]
    if source not in known and source not in logins:
        note(f"the AWS profile {source!r} (the login for the role) was not found. "
             "Create it with: aws configure, or aws configure sso.")


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
        for key, why in clean_env(settings["env"], p).items():
            changes.append(f"removed env.{key} ({why})")
    for key in forbidden_settings(p):
        if settings.pop(key, None) is not None:
            changes.append(f"removed {key} (the policy uses an AWS profile)")
    apply_required(settings, required_settings(p), changes)
    if changes:
        write_json(settings_path, settings)
        note(f"updated {settings_path}: " + "; ".join(changes))


def check_project_settings(p):
    # Project settings override user settings. A shared file that breaks the policy stops cjp.
    root = git_root()
    if root is None:
        return
    for rel, can_fix in ((".claude/settings.local.json", True), (".claude/settings.json", False)):
        path = root / rel
        data, err = load_json(path)
        if err or not isinstance(data, dict):
            continue
        has_env = isinstance(data.get("env"), dict)
        env = dict(data["env"]) if has_env else {}
        bad = clean_env(env, p)
        bad.update({k: "the policy uses an AWS profile" for k in forbidden_settings(p) if k in data})
        if not bad:
            continue
        if can_fix:
            if has_env:
                data["env"] = env
            for key in forbidden_settings(p):
                data.pop(key, None)
            write_json(path, data)
            note(f"removed {sorted(bad)} from {path}")
        else:
            sys.exit(f"[{APP}] {path} has settings that break the policy: {bad}. "
                     "Remove them, or ask the repo owner.")


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
# Proxy: claude and the tools it starts can only reach the allowed hosts
# ---------------------------------------------------------------------------


def allowed_hosts(p):
    # The AWS endpoints in the Japan regions, plus the hosts from the policy.
    hosts = [f"{service}.{region}.amazonaws.com" for region in JP_REGIONS for service in AWS_SERVICES]
    return hosts + [h for h in p["proxy"]["allow_hosts"] if h not in hosts]


def host_allowed(host, patterns):
    # "name" matches that host only. "*.name" matches every host below name.
    host = host.lower().rstrip(".")
    return any(host.endswith(pat[1:]) if pat.startswith("*.") else host == pat for pat in patterns)


def find_upstream(p, settings):
    # The proxy that our proxy must go through (a corporate proxy), or None.
    # Order: the policy, the env, the user settings, the OS settings.
    env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    found = [("the policy", p["proxy"]["upstream"])]
    found += [(key, os.environ.get(key, "")) for key in PROXY_URL_ENV]
    found += [(f"env.{key} in the settings", str(env.get(key) or "")) for key in PROXY_URL_ENV]
    try:
        system = urllib.request.getproxies()  # On Windows this reads the Internet settings.
    except Exception:
        system = {}
    found += [("the OS settings", system.get(scheme, "")) for scheme in ("https", "http")]
    for source, url in found:
        if not url:
            continue
        upstream = parse_proxy_url(url)
        if upstream:
            return upstream
        note(f"the proxy from {source} is not an http:// proxy. Ignored: {mask_url(url)}")
    return None


@functools.lru_cache(maxsize=256)
def proxy_bypass(host):
    # True if NO_PROXY (or the OS setting) says: connect to this host without the upstream proxy.
    try:
        return bool(urllib.request.proxy_bypass(host))
    except Exception:
        return False


def read_head(sock, limit=65536):
    # Read up to the blank line that ends the HTTP headers. Return (head, the bytes after it).
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(8192)
        if not chunk or len(data) > limit:
            return None, b""
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return head.decode("latin-1"), rest


def send_reply(sock, code, reason, text):
    body = f"{APP} proxy: {text}\n".encode("utf-8")
    head = (f"HTTP/1.1 {code} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n")
    sock.sendall(head.encode("ascii") + body)


def copy_stream(src, dst):
    # Copy until src ends, then tell dst that no more data comes.
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
        dst.shutdown(socket.SHUT_WR)
    except OSError:
        # One broken side ends both directions. shutdown() also wakes the other thread.
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class Proxy:
    # A small HTTP proxy on 127.0.0.1. It refuses every host that is not in the list.

    def __init__(self, allow, upstream, log_path):
        self.allow, self.upstream, self.log_path = allow, upstream, log_path
        self.lock = threading.Lock()
        self.logged = set()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))  # Port 0: the OS picks a free port.
        self.sock.listen(128)
        self.port = self.sock.getsockname()[1]

    def serve(self):
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(client,), daemon=True).start()

    def log(self, text):
        # One line per event and host in each run, so the file stays small.
        with self.lock:
            if text in self.logged:
                return
            self.logged.add(text)
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
            except OSError:
                pass

    def handle(self, client):
        remote = None
        try:
            client.settimeout(60)
            head, early = read_head(client)
            if head is None:
                return
            lines = head.split("\r\n")
            parts = lines[0].split(" ")
            if len(parts) != 3:
                return send_reply(client, 400, "Bad Request", "bad request line")
            method, target = parts[0], parts[1]
            tunnel = method.upper() == "CONNECT"
            try:
                url = urllib.parse.urlsplit("//" + target if tunnel else target)
                host, port = url.hostname, url.port or (443 if tunnel else 80)
            except ValueError:
                host = None
            if not host or (not tunnel and url.scheme != "http"):
                return send_reply(client, 400, "Bad Request", "use CONNECT host:port or an http:// URL")
            if not host_allowed(host, self.allow):
                self.log(f"BLOCK {host}:{port}")
                return send_reply(client, 403, "Forbidden", f"{host} is not in the allowed hosts of the policy")
            use_upstream = bool(self.upstream) and not proxy_bypass(host)
            try:
                remote = socket.create_connection(self.upstream[:2] if use_upstream else (host, port), timeout=30)
                late = self.open_tunnel(remote, host, port) if tunnel and use_upstream else b""
            except OSError as e:
                self.log(f"FAIL {host}:{port} {e}")
                return send_reply(client, 502, "Bad Gateway", f"can not reach {host}:{port}: {e}")
            if tunnel:
                client.settimeout(None)  # A streamed answer can be quiet for a long time.
                remote.settimeout(None)
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n" + late)
                remote.sendall(early)
                back = threading.Thread(target=copy_stream, args=(remote, client), daemon=True)
                back.start()
                copy_stream(client, remote)
                back.join()
            else:
                remote.settimeout(600)
                self.send_http(client, remote, method, target, url, lines[1:], early, use_upstream)
        except OSError:
            pass
        finally:
            for s in (client, remote):
                if s is not None:
                    s.close()
        return None

    def open_tunnel(self, remote, host, port):
        # Ask the upstream proxy for a tunnel. Return the bytes that came after its answer.
        request = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        if self.upstream[2]:
            request += f"Proxy-Authorization: Basic {self.upstream[2]}\r\n"
        remote.sendall((request + "\r\n").encode("latin-1"))
        head, rest = read_head(remote)
        status = (head or "no answer").split("\r\n")[0]
        if status.split(" ")[1:2] != ["200"]:
            raise OSError(f"the upstream proxy answered: {status}")
        return rest

    def send_http(self, client, remote, method, target, url, headers, early, use_upstream):
        # Plain http: send one request, then copy the answer. "Connection: close" stops the
        # client from sending a second request, for another host, on the same connection.
        keep, length = [], 0
        for line in headers:
            name, _, value = line.partition(":")
            name, value = name.strip().lower(), value.strip()
            if name == "transfer-encoding" or (name == "content-length" and not value.isdigit()):
                return send_reply(client, 501, "Not Implemented", "this request body is not supported")
            if name == "content-length":
                length = int(value)
            if name not in ("connection", "proxy-connection", "proxy-authorization", "keep-alive"):
                keep.append(line)
        if use_upstream and self.upstream[2]:
            keep.append(f"Proxy-Authorization: Basic {self.upstream[2]}")
        path = target if use_upstream else (url.path or "/") + (f"?{url.query}" if url.query else "")
        head = "\r\n".join([f"{method} {path} HTTP/1.1"] + keep + ["Connection: close", "", ""])
        body = early[:length]
        remote.sendall(head.encode("latin-1") + body)
        left = length - len(body)
        while left > 0:
            chunk = client.recv(min(65536, left))
            if not chunk:
                break
            remote.sendall(chunk)
            left -= len(chunk)
        while True:
            data = remote.recv(65536)
            if not data:
                break
            client.sendall(data)
        return None


def serve_in_child(proxy, parent, flag_file):
    # POSIX: exec replaces cjp with claude, so the proxy runs in its own process.
    # It stops when claude (the parent) is gone.
    try:
        os.setsid()  # Ctrl+C in the terminal must not stop the proxy.
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        threading.Thread(target=proxy.serve, daemon=True).start()
        while os.getppid() == parent:
            time.sleep(1)
    finally:
        try:
            flag_file.unlink()
        except OSError:
            pass
        os._exit(0)


def start_proxy(p, settings_path):
    # Start the proxy. Return (env vars for claude, settings file for claude --settings).
    cache = cache_dir()
    log_path = cache / "proxy.log"
    if log_path.exists() and log_path.stat().st_size > 1_000_000:
        log_path.unlink()
    for old in cache.glob("proxy-*.json"):  # Left behind when a console window was closed.
        pid = old.stem.split("-")[1]
        if not pid.isdigit() or not pid_alive(int(pid)):
            old.unlink()
    settings, _err = load_json(settings_path)
    proxy = Proxy(allowed_hosts(p), find_upstream(p, settings or {}), log_path)
    url = f"http://127.0.0.1:{proxy.port}"
    upper = {"HTTPS_PROXY": url, "HTTP_PROXY": url, "NO_PROXY": LOOPBACK}
    both = dict(upper, **{name.lower(): value for name, value in upper.items()})
    # Node.js tools (npm, MCP servers) only use the proxy env when this is set.
    env = dict(upper if IS_WIN else both, NODE_USE_ENV_PROXY="1")  # Windows env names ignore case.
    # For claude, the env in the settings wins over the process env, and --settings wins
    # over the user and project settings. So this file keeps claude on our proxy.
    flag_file = cache / f"proxy-{os.getpid()}.json"
    flag_file.write_text(json.dumps({"env": both}), encoding="utf-8")
    if IS_WIN:
        threading.Thread(target=proxy.serve, daemon=True).start()
    else:
        parent = os.getpid()
        if os.fork() == 0:
            serve_in_child(proxy, parent, flag_file)
        proxy.sock.close()
    return env, flag_file


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


def run_claude(real, argv, env, flag_file=None):
    args = [real] + argv
    if IS_WIN:
        # Windows has no exec. Wait for claude and pass its exit code.
        # Ignore Ctrl+C here, so only claude reacts to it.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, signal.SIG_IGN)
        try:
            code = subprocess.call(args, env=env)
        finally:
            if flag_file is not None:
                flag_file.unlink(missing_ok=True)
        sys.exit(code)
    os.execve(real, args, env)


def main():
    restore_env()
    argv = sys.argv[1:]
    cmd = argv[0] if argv else ""

    if cmd == "xlsx-peek":
        return xlsx_peek(argv[1:])
    try:
        if cmd == "--cjp-embed-policy":
            if frozen():
                sys.exit(f"[{APP}] the built exe can not change its policy.")
            return embed_policy(argv[1:])
        policy, source = load_policy()
        if cmd == "cjp-iam":
            print(json.dumps(iam_policy(policy, argv[1] if len(argv) > 1 else ""), indent=2))
            return None
    except PolicyError as e:
        sys.exit(f"[{APP}] policy error:\n{e}")
    if cmd == "cjp-policy":
        shown = copy.deepcopy(policy)
        shown["proxy"]["upstream"] = mask_url(shown["proxy"]["upstream"])
        print(f"# source: {source}")
        print(yaml.safe_dump(shown, sort_keys=False, allow_unicode=True))
        if policy["proxy"]["enabled"]:
            print("# hosts that the proxy lets through:")
            print("".join(f"#   {host}\n" for host in allowed_hosts(policy)))
        print("# settings written to ~/.claude/settings.json (also usable as managed-settings.json):")
        print(json.dumps(required_settings(policy), indent=2))
        return None
    if any(a == "--settings" or a.startswith("--settings=") for a in argv):
        sys.exit(f"[{APP}] --settings is blocked, because it can change the env and models.")

    lock_process_env(policy)
    add_extra_bins_to_path()
    safe("AWS profile check", ensure_aws_profile, policy)  # It can stop cjp on purpose.
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
    env, flag_file = dict(os.environ), None
    if policy["proxy"]["enabled"]:
        try:
            proxy_env, flag_file = start_proxy(policy, settings_path)
        except OSError as e:
            sys.exit(f"[{APP}] the proxy did not start, so claude was not started: {e}")
        # ALL_PROXY could send other protocols around our proxy.
        env = {k: v for k, v in env.items() if k.upper() != "ALL_PROXY"}
        env.update(proxy_env)
        argv = ["--settings", str(flag_file)] + argv
    run_claude(real, argv, env, flag_file)
    return None


if __name__ == "__main__":
    main()
