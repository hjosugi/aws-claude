# cjp handover

Last update: 2026-10-31

## 1. Goal

Use Claude Code at work with these rules:

- Data stays in Japan. No storage outside Japan.
- Bedrock is the only backend. No Anthropic API key, no claude.ai login, no gateways.
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
| Web | `WebFetch` and `WebSearch` denied | Requests would leave Japan |
| Telemetry | `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` | Nothing goes to Anthropic. Side effect: no auto update. |
| Helper tools | rtk (shell output compression), graphify (code graph, `--code-only`, no LLM) | Token saving. Both have no telemetry by default. |
| Policy | `policy.yaml`, embedded into the exe at build time | People with only the exe can not change models |

## 3. What cjp does

On every start:

1. Removes env vars that break the policy (`ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, Vertex/Foundry/Mantle switches, non-allowed model IDs, non-Japan Bedrock endpoints).
2. Sets `CLAUDE_CODE_USE_BEDROCK=1`, `AWS_REGION`, `ANTHROPIC_BEDROCK_REGION_PREFIX=jp`, and the model pins.
3. Installs `uv`, `rtk`, `graphify` if missing (retries a failed install after 24h). Registers the rtk hook and the graphify skill.
4. Writes the policy into `~/.claude/settings.json` (the policy wins over old values).
5. Project checks (git repos only):
   - `.claude/settings.local.json` with bad env: fixed.
   - `.claude/settings.json` (shared) with bad env: cjp stops with an error.
   - Adds graphify rules and data file rules to `CLAUDE.md` (keeps CRLF/LF).
   - Builds or updates the code graph in the background.
6. Blocks `--settings` (it can change env and models).
7. Runs the real `claude` with the same args and returns its exit code.

Extra commands: `cjp xlsx-peek`, `cjp cjp-policy`, `cjp cjp-iam <account-id>`.

## 4. Test status

| Item | Status |
|---|---|
| Policy validation, embed step, `cjp-policy`, `cjp-iam` | Tested (Linux) |
| Env lock, settings write, project settings check, `--settings` block | Tested (Linux and Wine) |
| `dist/cjp.exe` start, embedded policy, `xlsx-peek` with bundled openpyxl, exit code pass-through | Tested on Wine 9.0 |
| `pid_alive` with the Windows API | Tested on Wine |
| rtk Windows zip: URL and content (`rtk.exe`, v1.1.0, has `init -g --auto-patch`) | Checked |
| uv install with PowerShell, rtk download on a corporate proxy, graphify on Windows | **Not tested. Test on a real Windows PC.** |
| Real Claude Code with the `jp.` profiles | **Not tested. Needs AWS access.** |

`dist/cjp.exe` was built with Wine (Windows CPython 3.12.13, PyInstaller 6.22.3). It is not signed.
SHA-256: `01a3c5159c5162d3d2b03dab6aadd0a29441438837ea107a6d2cdcdd24a95731`.
For production, build it again on Windows or with the GitHub Actions workflow.

## 5. Open tasks

### Must do

1. **Check the profiles in our account.**
   `aws bedrock list-inference-profiles --region ap-northeast-1 --query "inferenceProfileSummaries[?starts_with(inferenceProfileId, 'jp.')].inferenceProfileId"`
   Haiku 5.5 and Sonnet 4.6 must be in the list. Submit the Anthropic use case form once per account.
2. **Apply IAM.** Run `cjp cjp-iam <account-id>` and attach the output to the Claude Code role.
   It allows only the `jp.` profiles and denies Bedrock outside Japan (this also blocks Global routing).
   Use a dedicated role (or account). Other policies like `bedrock:*` would add access back.
3. **Keep Bedrock model invocation logging off.** If it is on, prompts and outputs are saved in S3 or CloudWatch Logs.
4. **Budgets.** Use a dedicated AWS account and a budget on the whole account
   (Claude shows up as its own service name, like "Claude Haiku 5.5 (Amazon Bedrock Edition)").
   - $50 actual: email alert.
   - $100 actual: Budget Action "apply IAM policy", automatic, with a deny policy for `bedrock:InvokeModel*`.
   - Budgets data lags 8 to 12 hours. For a hard $100 cap, stop at $80.
   - IAM Identity Center roles (`AWSReservedSSO_*`) can not take the policy directly. Use an SCP or a normal IAM role.
5. **Test on Windows**: first run with network, behind the corporate proxy. Check that uv, rtk, and graphify install, and that `rtk init -g` adds the hook.

### Should do

6. **Managed settings.** cjp writes user settings, which users can edit. For real enforcement, deploy the settings from `cjp cjp-policy` as managed settings (MDM or `managed-settings.json`). Managed-only keys worth adding there: `deniedModels: ["opus"]`, `availableModelsMatch: "exact"`.
7. **Watch for Sonnet 5.5 `jp.`.** Check the Bedrock model card. When it appears, change `models.sonnet` in `policy.yaml` and build again.
8. **Update Claude Code by hand.** `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` turns off update checks. `enforceAvailableModels` on Bedrock needs v2.1.287 or later.
9. **Try Serena** if Claude still greps a lot after graphify (LSP symbol tools, good for Java). Note: Serena v2 is GPL-3.0.
10. **Check `bashOutputMaxChars`.** Lowering it (for example to 10000) cuts tool output tokens. The minimum value is not documented, so test it first.

### Notes

- The VS Code extension starts its own claude, so it skips cjp. It still uses the settings that cjp wrote.
- rtk rewrites only the Bash tool. Do not set `CLAUDE_CODE_USE_POWERSHELL_TOOL=1`.
- A one-file exe unpacks itself on each start. Antivirus can make it slow or flag it. Use `build.ps1 -OneDir` if needed.
- Under Wine, the exe fails when stdout is not a pipe ("init_sys_streams"). This is a Wine issue, not a Windows one.
- Background graph logs: `%LOCALAPPDATA%\cjp\graph-*.log`.

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
