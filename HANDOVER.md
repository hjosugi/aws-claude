# cjp handover

Last update: 2026-11-22

Repo: https://github.com/hjosugi/my-claude (public)

## 1. Goal

Use Claude Code at work with these rules:

- Data stays in Japan. No storage outside Japan.
- Bedrock is the only backend. No Anthropic API key, no claude.ai login, no gateways.
- Login is an AWS IAM role, not a claude.ai subscription.
- Nothing goes to other places by accident (web search, web pages, Anthropic, claude.ai).
- Inference stays in Japan too (`jp.` inference profiles only).
- Only cheap models: Haiku and Sonnet. No Opus.
- Billing goes to the AWS bill.
- Save tokens as much as we can (Excel files and a large Java codebase).
- Main OS is Windows.

## 2. Decisions so far

| Topic | Decision | Why |
|---|---|---|
| Tool | Claude Code + Amazon Bedrock (Tokyo) | Seat products (Kiro, Q Developer, Copilot, Claude Enterprise, Gemini Code Assist, Cursor) can not keep data in Japan. ChatGPT Enterprise can store data in Japan, but costs about $108K/year minimum (reported) and is not on the AWS bill. |
| Region | `ap-northeast-1` as source. `jp.` profiles route to Tokyo or Osaka only. | Japan only |
| Haiku | `jp.anthropic.claude-haiku-5-5` (default model) | Very cheap under 100K-token prompts |
| Sonnet | `jp.anthropic.claude-sonnet-4-6` | Sonnet 5.5 has no `jp.` profile yet. From Tokyo it is Global only. |
| Context | `autoCompactWindow: 100000` | Haiku 5.5 price is 5x for prompts over 100K tokens |
| Effort | `low` by default | Thinking tokens are output tokens |
| Web | `WebFetch` and `WebSearch` denied | Requests would leave Japan. (On Bedrock, `WebSearch` does not exist anyway.) |
| Telemetry | `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `ENABLE_CLAUDEAI_MCP_SERVERS=false`, `CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL=1`, `skipWebFetchPreflight: true` | Nothing goes to Anthropic. The last three are not part of "nonessential traffic". Side effect: no auto update. |
| Network | Local proxy with a host list (`proxy` in `policy.yaml`) | Deny rules only stop the tools of claude. The proxy also stops curl, git, npm, and claude itself. Native Windows has no sandbox in Claude Code, so the proxy is the only option in cjp. |
| Allowed hosts | AWS in Tokyo and Osaka (always), plus GitHub, PyPI, npm, Maven Central, Gradle (`policy.yaml`) | Enough for normal development. Each host is also a way out for data. Remove what is not needed. |
| AWS login | IAM role through an AWS profile (`aws` in `policy.yaml`). Access keys, Bedrock API keys, and `awsCredentialExport` are removed. | The role has the IAM policy from `cjp cjp-iam`. A normal IAM role can also take the Budget Action policy (see 5.4). |
| Subscription | Never used. `CLAUDE_CODE_OAUTH_TOKEN` and API key env vars are removed. | Billing stays on the AWS bill. |
| Build settings | `.env` at build time. It replaces values of `policy.yaml`. | Account IDs, role names, and proxy addresses stay out of the public repo. One file per environment (`-EnvFile .env.prod`). |
| Repo | Public, `hjosugi/my-claude`. Actions run the tests and build the exe as an artifact. No releases. | The public build has `policy.yaml` only. Build the exe with `.env` on your own PC. |
| Helper tools | rtk (shell output compression), graphify (code graph, `--code-only`, no LLM) | Token saving. Both have no telemetry by default. |
| Policy | `policy.yaml`, embedded into the exe at build time | People with only the exe can not change models |

## 3. What cjp does

On every start:

1. Removes env vars that break the policy (`ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_OAUTH_TOKEN`, Vertex/Foundry/Mantle switches, non-allowed model IDs, non-Japan Bedrock endpoints). If the policy names an AWS profile, it also removes AWS access keys and Bedrock API keys.
2. Sets `CLAUDE_CODE_USE_BEDROCK=1`, `AWS_REGION`, `ANTHROPIC_BEDROCK_REGION_PREFIX=jp`, the model pins, and `AWS_PROFILE` (if the policy has one).
3. If the policy has a role ARN: adds the profile for that role to `~/.aws/config`. If the profile exists with another role, cjp stops.
4. Installs `uv`, `rtk`, `graphify` if missing (retries a failed install after 24h). Registers the rtk hook and the graphify skill. These installs do not go through the cjp proxy.
5. Writes the policy into `~/.claude/settings.json` (the policy wins over old values).
6. Project checks (git repos only):
   - `.claude/settings.local.json` with bad env: fixed.
   - `.claude/settings.json` (shared) with bad env: cjp stops with an error.
   - Adds graphify rules and data file rules to `CLAUDE.md` (keeps CRLF/LF).
   - Builds or updates the code graph in the background.
7. Blocks `--settings` from the user (it can change env and models).
8. Starts the proxy on a free port of `127.0.0.1`. Passes `HTTPS_PROXY`, `HTTP_PROXY`, and `NO_PROXY` (localhost only) to claude, in the process env and in a `--settings` file.
9. Runs the real `claude` with the same args and returns its exit code.

Extra commands: `cjp xlsx-peek`, `cjp cjp-policy`, `cjp cjp-iam [account-id]`.

How the proxy stays in front of claude (checked with Claude Code 2.1.330):

- For claude, `env` in a settings file wins over the process env. So a `HTTPS_PROXY` in `~/.claude/settings.json` would win over the one from cjp.
- A `--settings` file wins over the user and project settings. So cjp passes its proxy address that way. Only managed settings can still win.
- cjp does not remove a `HTTPS_PROXY` from the user settings. It uses that address as the corporate proxy behind its own proxy.
- Windows: the proxy is a thread in `cjp.exe`, which waits for claude. Linux and macOS: the proxy is a child process, because cjp replaces itself with claude. It ends when claude ends.

## 4. Test status

| Item | Status |
|---|---|
| Policy validation, embed step, `cjp-policy`, `cjp-iam` | Tested (Linux) |
| Env lock, settings write, project settings check, `--settings` block | Tested (Linux and Wine) |
| Old exe (October): start, embedded policy, `xlsx-peek` with bundled openpyxl, exit code pass-through, `pid_alive` | Tested on Wine 9.0 |
| rtk Windows zip: URL and content (`rtk.exe`, v1.1.0, has `init -g --auto-patch`) | Checked |
| `.env` values, AWS role profile, proxy (allow, block, corporate proxy behind it, plain http), fake claude from start to end | Tested: `tests/test_cjp.py` on Linux, and on Windows and Linux runners in GitHub Actions |
| Built binary with a fake claude: embedded `.env` values, runtime `CJP_` vars ignored, proxy cleanup | Tested: Linux binary here, `cjp.exe` in GitHub Actions |
| Real Claude Code 2.1.330 behind the cjp proxy (Linux, fake AWS keys) | Tested. The request reached Bedrock in Tokyo (AWS answered "invalid security token"). Nothing else was tried by claude. With a fake corporate proxy behind cjp, that proxy saw only `bedrock` and `bedrock-runtime` in `ap-northeast-1`. |
| Real TLS through the proxy with curl | Tested. Allowed: Bedrock, STS, PyPI, GitHub. Blocked: `api.anthropic.com`, `claude.ai`, `sts.amazonaws.com`, Bedrock in `us-east-1`. |
| First start on a clean PC: uv, rtk, graphify install, rtk hook, graphify skill, CLAUDE.md rules, code graph | Tested: GitHub Actions on Windows (started from PowerShell 7) and Linux, with no corporate proxy. This test found a bug: from PowerShell 7, the uv install failed (`PSModulePath`). Fixed. |
| The same installs behind a corporate proxy | **Not tested. Test on a real Windows PC.** |
| Proxy behind a real corporate proxy, and the Windows proxy setting as the source of its address | **Not tested. Test on a real Windows PC.** |
| Login with the IAM role, and real answers from the `jp.` profiles | **Not tested. Needs AWS access.** |

The exe from GitHub Actions is built on a Windows runner. It is not signed. It has `policy.yaml` only (no role, no corporate proxy).
For production, build on Windows with your `.env`.

## 5. Open tasks

### Must do

1. **Check the profiles in our account.**
   `aws bedrock list-inference-profiles --region ap-northeast-1 --query "inferenceProfileSummaries[?starts_with(inferenceProfileId, 'jp.')].inferenceProfileId"`
   Haiku 5.5 and Sonnet 4.6 must be in the list. Submit the Anthropic use case form once per account.
2. **Create the role and apply IAM.** Create a normal IAM role for Claude Code. Its trust policy must allow the login of the developers (IAM users, or the SSO role). Run `cjp cjp-iam <account-id>` and attach the output to the role.
   It allows only the `jp.` profiles and denies Bedrock outside Japan (this also blocks Global routing).
   Use a dedicated role (or account). Other policies like `bedrock:*` would add access back.
   Then put the role into `.env` (`CJP_AWS_ROLE_ARN`, `CJP_AWS_SOURCE_PROFILE`, and for SSO `CJP_AWS_AUTH_REFRESH`) and build.
3. **Keep Bedrock model invocation logging off.** If it is on, prompts and outputs are saved in S3 or CloudWatch Logs.
4. **Budgets.** Use a dedicated AWS account and a budget on the whole account
   (Claude shows up as its own service name, like "Claude Haiku 5.5 (Amazon Bedrock Edition)").
   - $50 actual: email alert.
   - $100 actual: Budget Action "apply IAM policy", automatic, with a deny policy for `bedrock:InvokeModel*`.
   - Budgets data lags 8 to 12 hours. For a hard $100 cap, stop at $80.
   - IAM Identity Center roles (`AWSReservedSSO_*`) can not take the policy directly. Use an SCP or a normal IAM role.
5. **Test on Windows**: first run with network, behind the corporate proxy. Check that uv, rtk, and graphify install, and that `rtk init -g` adds the hook.
   Then check the proxy: `cjp -p hi` must answer, and `%LOCALAPPDATA%\cjp\proxy.log` must show nothing for AWS hosts. A `FAIL` line means the corporate proxy refused cjp (NTLM or Kerberos login: put Px or cntlm in between). If Windows uses a setup script (PAC) for the proxy, cjp can not read it: set `CJP_PROXY_UPSTREAM`.
6. **Decide the host list.** `proxy.allow_hosts` in `policy.yaml` has GitHub, PyPI, npm, Maven Central, and Gradle. Remove the groups that are not needed. Add internal hosts with `CJP_PROXY_ADD_HOSTS` in `.env`. If the IAM Identity Center is not in Tokyo or Osaka, add `oidc.<region>.amazonaws.com` and `portal.sso.<region>.amazonaws.com`.
7. **Make the limit hard.** The cjp proxy only covers programs that use the proxy env vars. Java, Maven, and Gradle do not use them by default. Allow the same host list in the corporate proxy or firewall.

### Should do

8. **Managed settings.** cjp writes user settings, which users can edit. For real enforcement, deploy the settings from `cjp cjp-policy` as managed settings (MDM or `managed-settings.json`). Managed-only keys worth adding there: `deniedModels: ["opus"]`, `availableModelsMatch: "exact"`.
9. **Watch for Sonnet 5.5 `jp.`.** Last check 2026-11-22: still no `jp.` profile (from Tokyo only Global). Check the Bedrock model card. When it appears, change `models.sonnet` in `policy.yaml` (or `CJP_MODEL_SONNET` in `.env`) and build again.
10. **Update Claude Code by hand.** `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` turns off update checks, and the cjp proxy blocks the download. Run the real `claude update` without cjp. `enforceAvailableModels` on Bedrock needs v2.1.287 or later.
11. **Try Serena** if Claude still greps a lot after graphify (LSP symbol tools, good for Java). Note: Serena v2 is GPL-3.0.
12. **Check `bashOutputMaxChars`.** Lowering it (for example to 10000) cuts tool output tokens. The minimum value is not documented, so test it first.

### Notes

- The VS Code extension starts its own claude, so it skips cjp. It still uses the settings that cjp wrote, but it has no proxy in front.
- With the proxy on, `git push` over https, `uv run` with packages, and `npx` only work for hosts in the list. git over ssh does not use the proxy.
- `allow_web_fetch: true` is not enough for WebFetch. The pages must also be in the host list.
- A program that claude leaves running after it ends (a dev server) keeps the proxy address of a proxy that is gone. Its outgoing requests fail.
- The proxy log has one line per blocked host and run. Allowed requests are not logged.
- `.env` values go into the exe as plain text. Do not put a proxy password there.
- rtk rewrites only the Bash tool. Do not set `CLAUDE_CODE_USE_POWERSHELL_TOOL=1`.
- A one-file exe unpacks itself on each start. Antivirus can make it slow or flag it. Use `build.ps1 -OneDir` if needed.
- Under Wine, the exe fails when stdout is not a pipe ("init_sys_streams"). This is a Wine issue, not a Windows one.
- Background graph logs: `%LOCALAPPDATA%\cjp\graph-*.log`.
- `ANTHROPIC_BEDROCK_BASE_URL` must be `https://` and an AWS host in Tokyo or Osaka (a normal or a VPC endpoint). Other values are removed.

## 6. Cost estimates (3 devs, 4 h/day, 20 days/month, `jp.` +10%)

| Use | Haiku 5.5 | Sonnet 4.6 |
|---|---|---|
| Light (40K context) | about $6 | about $170 |
| Standard (80K context) | about $21 | about $630 |
| Heavy (120K context) | about $273 (over 100K) / $46 if kept under 100K | about $1,640 |

Sonnet 4.6 numbers are the Sonnet 5.5 estimates x 1.5 ($3.30/$16.50 vs $2.20/$11 per 1M tokens). They are rough. Measure with Cost Explorer after one week.

## 7. Token saving rules (summary)

- `/clear` between tasks (free). `/compact` costs a large request.
- Excel: `cjp xlsx-peek`, never read the file. Other data: a PEP 723 script in `.claude/tools/`, run with `uv run`, print at most 50 lines.
- Code: graph first (graphify), then read only the lines you need.
- CLAUDE.md under 200 lines. Move workflows to skills.
- Subagents for tests, logs, and exploration, on Haiku.
- Plan mode for big changes. Stop early with Esc when it goes wrong.

## 8. Sources

- Claude Code on Amazon Bedrock: https://code.claude.com/docs/en/amazon-bedrock
- Claude Code costs: https://code.claude.com/docs/en/costs
- Claude Code model config: https://code.claude.com/docs/en/model-config
- Claude Code settings reference: https://code.claude.com/docs/en/settings-reference
- Claude Code permissions: https://code.claude.com/docs/en/permissions
- Claude Code tools reference (output limits): https://code.claude.com/docs/en/tools-reference
- Bedrock Haiku 5.5 model card: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-5-5.html
- Bedrock Sonnet 5.5 model card: https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-5-5.html
- Bedrock Japan cross-region inference (+10% price): https://aws.amazon.com/jp/blogs/news/amazon-bedrock-now-supports-japan-cross-region-inference
- Bedrock data protection: https://docs.aws.amazon.com/bedrock/latest/userguide/data-protection.html
- Bedrock Global CRIS IAM and SCP: https://docs.aws.amazon.com/bedrock/latest/userguide/global-cross-region-inference.html
- AWS Budgets: https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html
- rtk: https://github.com/rtk-ai/rtk
- rtk Windows hook since v0.37.0: https://goodfirstissue.org/rtk-ai/rtk/issues/2115
- graphify: https://github.com/safishamsi/graphify
- graphify CLI: https://docs.graphify.com/open-source/configuration/cli.md
- Serena: https://github.com/oraios/serena
- uv scripts (PEP 723): https://docs.astral.sh/uv/guides/scripts/
