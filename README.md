# cjp

`cjp` starts Claude Code with Amazon Bedrock, in Japan only.

- **Bedrock only.** No claude.ai login, no Anthropic API key, no gateway.
- **Japan only.** Tokyo or Osaka as the region, and `jp.` inference profiles only.
- **AWS login with an IAM role.** The policy can name the role. Other AWS keys are removed.
- **No traffic to other places.** A local proxy lets only the allowed hosts through.
- **Cheap by default.** Haiku 5.5, low effort, and compact at 100K tokens.

It locks the env, writes the policy into `~/.claude/settings.json`, sets up helper tools, starts the proxy, then runs the real `claude` with the same arguments.

- Setup from an empty AWS account to a developer PC: `SETUP.md` (日本語: `SETUP.ja.md`)
- AWS resources (IAM role, budget with an automatic stop): `cloudformation/cjp.yml`
- Decisions, status, and open tasks: `HANDOVER.md`

## Use

```powershell
cjp                      # same as claude, but Bedrock + Japan only
cjp -p "explain this"    # all args go to claude
cjp xlsx-peek book.xlsx  # small summary of an Excel file (no Python needed)
cjp cjp-policy           # show the policy, the allowed hosts, and the settings it writes
cjp cjp-iam 123456789012 # print an IAM policy for the allowed models
```

## Install (Windows)

1. Get `cjp.exe`: build it (see below), or download the `cjp-windows` artifact from the newest run of the `build-windows` workflow.
2. Copy `cjp.exe` to `%USERPROFILE%\.local\bin\` (the native Claude Code installer puts this folder on PATH).
3. Run `cjp cjp-policy` to check it.
4. Use `cjp` instead of `claude`.

## Build

The policy is put into the exe at build time. People who only have the exe can not change it.

```powershell
copy .env.example .env      # then edit .env
powershell -ExecutionPolicy Bypass -File .\build.ps1                     # uses policy.yaml + .env
powershell -ExecutionPolicy Bypass -File .\build.ps1 -EnvFile .env.prod  # another environment
```

On Linux or macOS: `./build.sh` or `./build.sh --env-file .env.prod`.

- `policy.yaml` has the shared values. It is committed.
- `.env` has the values of one environment (AWS account, role, corporate proxy). It is not committed. A value in `.env` replaces the same value in `policy.yaml`. `.env.example` lists all keys.
- A bad value (a model that is not `jp.`, a region outside Japan, an unknown key) stops the build.
- The GitHub Actions build uses `policy.yaml` only. This repo is public, so build the exe for your own environment on your own PC.

## AWS login with an IAM role

Set the role in `.env`:

```
CJP_AWS_ROLE_ARN=arn:aws:iam::123456789012:role/claude-code
CJP_AWS_SOURCE_PROFILE=default
```

Then, on every start, cjp:

1. Adds this profile to `~/.aws/config` if it is missing. If a profile with the same name uses another role, cjp stops.

   ```ini
   [profile cjp]
   role_arn = arn:aws:iam::123456789012:role/claude-code
   source_profile = default
   region = ap-northeast-1
   ```

2. Sets `AWS_PROFILE=cjp` for claude.
3. Removes AWS access keys, Bedrock API keys, and `awsCredentialExport` from the env and the settings, so claude can only use the role.

The user only needs a login for the source profile (`aws configure`, or `aws configure sso`). The role's trust policy must allow that login. For SSO, set `CJP_AWS_AUTH_REFRESH=aws sso login --profile <name>`: claude runs it when the credentials are expired.

To use a profile that already exists, set only `CJP_AWS_PROFILE`.

`cloudformation/cjp.yml` creates the role (see `SETUP.md`). `cjp cjp-iam` prints the same IAM policy, if you make the role another way. With a role ARN in the policy, the account ID is optional.

## Proxy

cjp starts a small HTTP proxy on `127.0.0.1` and sends claude through it. The proxy refuses every host that is not in the list. `cjp cjp-policy` shows the list:

- Always: Bedrock, STS, and IAM Identity Center in Tokyo and Osaka.
- From `policy.yaml` (`proxy.allow_hosts`): GitHub, PyPI, npm, Maven Central, and Gradle. Remove what you do not need. Each host is also a way out for data.
- From `.env` (`CJP_PROXY_ADD_HOSTS`): hosts of your environment, for example an internal repository.

Everything else gets `403`, for example `api.anthropic.com`, `claude.ai`, Bedrock in other regions, and any web page. Blocked hosts are written to `%LOCALAPPDATA%\cjp\proxy.log` (Linux: `~/.cache/cjp/proxy.log`).

If you have a corporate proxy, the cjp proxy goes through it. It takes the address from `CJP_PROXY_UPSTREAM`, then `HTTPS_PROXY`, then `env.HTTPS_PROXY` in `~/.claude/settings.json`, then the Windows proxy setting. Only `http://` proxies with no login or a basic login work.

- NTLM or Kerberos login: put a local helper proxy (Px, cntlm) in between, and set its address in `CJP_PROXY_UPSTREAM`.
- A setup script (PAC or WPAD, "Use setup script" in Windows) can not be read. Set the proxy address in `CJP_PROXY_UPSTREAM`.
- SOCKS proxies do not work.
- Do not put a proxy password in `.env`, because it goes into the exe as plain text. Give it at run time: `HTTPS_PROXY=http://user:pass@host:port`.

Limits. The proxy stops traffic by accident. It is not a firewall:

- It covers claude and the tools that use the proxy env vars: curl, git, uv, pip, npm, the AWS CLI.
- It does not cover programs that ignore these env vars. Java, Maven, and Gradle ignore them by default.
- It does not cover a command that turns the proxy off on purpose, or a claude that was not started with cjp (the VS Code extension).
- For a hard limit, also allow only these hosts in the corporate proxy or firewall.

## Tests

```
uv run python -m unittest discover -s tests -v
```

The tests need no AWS account and no real claude. The workflow runs them on Windows and Linux.

## Files

| File | What it is |
|---|---|
| `SETUP.md` / `SETUP.ja.md` | Setup steps (English / Japanese): AWS account, build, developer PC, operations, troubleshooting |
| `cloudformation/cjp.yml` | IAM role, assume policy, and monthly budget with an automatic stop |
| `cjp.py` | The launcher (one file) |
| `policy.yaml` | Allowed models, AWS login, proxy hosts, and other rules |
| `.env.example` | Template for `.env`, the build settings of one environment |
| `build.ps1` / `build.sh` | Build with PyInstaller (Windows / Linux) |
| `tests/test_cjp.py` | Tests |
| `.github/workflows/build-windows.yml` | Test, lint the template, then build `cjp.exe` on a Windows runner |
| `pyproject.toml` / `uv.lock` | Python deps for `uv run` |
| `HANDOVER.md` | Handover notes |
