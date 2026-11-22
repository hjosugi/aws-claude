# cjp setup

日本語版: [SETUP.ja.md](SETUP.ja.md)

Step-by-step setup, from an empty AWS account to a developer who runs `cjp`.

| Part | Who | Where |
|---|---|---|
| [1. AWS account](#1-aws-account) | AWS administrator | AWS CLI or console |
| [2. Build cjp.exe](#2-build-cjpexe) | One person (the builder) | Windows PC |
| [3. Developer PC](#3-developer-pc) | Each developer | Windows PC |
| [4. Operations](#4-operations) | AWS administrator, builder | |
| [5. Troubleshooting](#5-troubleshooting) | Everyone | |

The examples use these names. Change them if you like, but use the same names everywhere.

| Name | Example | Used in |
|---|---|---|
| Stack | `cjp` | CloudFormation |
| Role | `claude-code` | CloudFormation (`RoleName`) |
| Login profile on the developer PCs | `corp-sso` | `.env` (`CJP_AWS_SOURCE_PROFILE`), `aws configure sso` |

## 1. AWS account

Use a dedicated AWS account for Claude Code. The budget counts the cost of the whole account, and other IAM policies in the account could give Bedrock access back.

You need AWS CLI v2 and administrator access. All commands use Tokyo (`ap-northeast-1`).

### 1.1 Check the models

```powershell
aws bedrock list-inference-profiles --region ap-northeast-1 --query "inferenceProfileSummaries[?starts_with(inferenceProfileId, 'jp.anthropic')].inferenceProfileId"
```

The list must have `jp.anthropic.claude-haiku-5-5` and `jp.anthropic.claude-sonnet-4-6`.

### 1.2 Send the Anthropic use case form

Anthropic models older than Claude Opus 4.7 need a one-time use case form. This includes Sonnet 4.6.

1. Open the Amazon Bedrock console in Tokyo.
2. Open **Model catalog**, then **Claude Sonnet 4.6**.
3. Send the use case details.

Send it once per account (or once in the management account of the organization). The approval can stay "pending" for some time.

### 1.3 Check that model invocation logging is off

If it is on, Bedrock saves every prompt and answer in S3 or CloudWatch Logs.

```powershell
aws bedrock get-model-invocation-logging-configuration --region ap-northeast-1
aws bedrock get-model-invocation-logging-configuration --region ap-northeast-3
```

It is off by default. If the output has a `loggingConfig`, turn it off:

```powershell
aws bedrock delete-model-invocation-logging-configuration --region ap-northeast-1
```

### 1.4 Deploy the CloudFormation stack

`cloudformation/cjp.yml` creates:

- The IAM role `claude-code`. It can call only the `jp.` inference profiles. It can not use Bedrock outside Japan. Its policy is the same as the output of `cjp cjp-iam`.
- The policy `claude-code-assume`. Developers need it to assume the role.
- A monthly budget. It sends an email at `AlertAmount`, and blocks Bedrock for the role at `StopAmount`.

Deploy it in Tokyo. CloudFormation has no Budgets resources in Osaka.

```powershell
aws cloudformation deploy --region ap-northeast-1 --stack-name cjp `
  --template-file cloudformation/cjp.yml --capabilities CAPABILITY_NAMED_IAM `
  --parameter-overrides AlertEmail=you@example.co.jp StopAmount=80
```

(In the console: CloudFormation > Create stack > Upload a template file. Check "I acknowledge that AWS CloudFormation might create IAM resources with custom names".)

Parameters:

| Parameter | Default | Note |
|---|---|---|
| `RoleName` | `claude-code` | |
| `TrustedPrincipalArns` | empty | Who may assume the role. Empty: any user or role in the account that has the `-assume` policy. Example for IAM Identity Center: `arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/*/AWSReservedSSO_Developer_*` |
| `MaxSessionDuration` | `3600` | Seconds. The AWS SDK gets a new session by itself. |
| `BedrockRegion` | `ap-northeast-1` | Same as `CJP_REGION`. |
| `HaikuProfileId` | `jp.anthropic.claude-haiku-5-5` | Same as `CJP_MODEL_HAIKU`. |
| `SonnetProfileId` | `jp.anthropic.claude-sonnet-4-6` | Same as `CJP_MODEL_SONNET`. Empty for Haiku only. |
| `EnableBudget` | `true` | |
| `AlertEmail` | empty | Needed when `EnableBudget` is `true`. |
| `AlertAmount` | `50` | USD per month. Email only. |
| `StopAmount` | `100` | USD per month. Cost data lags 8 to 12 hours, so the stop comes later. For a hard cap of $100, use `80`. |

Get the outputs:

```powershell
aws cloudformation describe-stacks --region ap-northeast-1 --stack-name cjp --query "Stacks[0].Outputs" --output table
```

Give `EnvLine` (`CJP_AWS_ROLE_ARN=...`) to the builder.

### 1.5 Let the developers assume the role

- **IAM users**: attach the policy `claude-code-assume` to the IAM group of the developers.
- **IAM Identity Center**: in the permission set of the developers, add the customer managed policy `claude-code-assume`. To allow only this permission set, also set `TrustedPrincipalArns` (see 1.4). A role session that starts from IAM Identity Center lasts 1 hour at most. The SDK gets a new one by itself.

### 1.6 Test the stop

Do this once, after a developer can use `cjp` (part 3). Get `StopPolicyArn` from the outputs.

```powershell
aws iam attach-role-policy --role-name claude-code --policy-arn <StopPolicyArn>
# The developer runs: cjp -p "hello"   -> it must fail with AccessDenied.
aws iam detach-role-policy --role-name claude-code --policy-arn <StopPolicyArn>
```

## 2. Build cjp.exe

The settings of your environment go into the exe. People who only have the exe can not change them.

### 2.1 Install the tools

- Git: https://git-scm.com/download/win
- uv: `winget install --id=astral-sh.uv -e`

### 2.2 Get the source and write .env

```powershell
git clone https://github.com/hjosugi/my-claude.git
cd my-claude
copy .env.example .env
notepad .env
```

Set at least these keys. `.env.example` explains all keys.

```
CJP_AWS_ROLE_ARN=arn:aws:iam::123456789012:role/claude-code
CJP_AWS_SOURCE_PROFILE=corp-sso
# IAM Identity Center only: claude runs this when the login has expired.
CJP_AWS_AUTH_REFRESH=aws sso login --profile corp-sso
```

Also set, when needed:

| Key | When |
|---|---|
| `CJP_PROXY_UPSTREAM=http://proxy.example.co.jp:8080` | Your PCs use a proxy setup script (PAC), or the address is not in `HTTPS_PROXY` or the Windows settings. |
| `CJP_PROXY_ADD_HOSTS=nexus.example.co.jp,*.git.example.co.jp` | Internal hosts that developers need (package mirrors, git). |
| `CJP_MODEL_SONNET=none` | Haiku only. Also deploy the stack with `SonnetProfileId=` (empty). |

Do not put passwords in `.env`. The values go into the exe as plain text. Keep `.env` in a safe place, not in the public repo (git ignores it).

### 2.3 Check and build

```powershell
uv run python cjp.py cjp-policy                       # the policy with your .env values, and the allowed hosts
uv run python -m unittest discover -s tests           # optional: the tests
powershell -ExecutionPolicy Bypass -File .\build.ps1  # makes dist\cjp.exe
dist\cjp.exe cjp-policy                               # "source: embedded in the exe", and your role ARN
```

For another environment, keep another file (for example `.env.prod`) and build with `.\build.ps1 -EnvFile .env.prod`.

### 2.4 Hand out the exe

Put `dist\cjp.exe` in a shared folder. The exe is not signed, so Windows SmartScreen or antivirus software can warn. If antivirus makes the start slow, build with `.\build.ps1 -OneDir` and hand out the `dist\cjp` folder.

## 3. Developer PC

### 3.1 Install the tools

These downloads do not go through the cjp proxy. Do them first.

1. Git for Windows: https://git-scm.com/download/win (Claude Code on Windows needs it).
2. Claude Code: `irm https://claude.ai/install.ps1 | iex`, then check with `claude --version` in a new terminal.
3. AWS CLI v2: `winget install --id=Amazon.AWSCLI -e`

Do not log in to Claude Code with a claude.ai account. cjp does not use it.

### 3.2 Log in to AWS

The profile name must be the same as `CJP_AWS_SOURCE_PROFILE` (here `corp-sso`).

- **IAM Identity Center**:

  ```powershell
  aws configure sso --profile corp-sso
  aws sso login --profile corp-sso
  ```

- **IAM user** (access key):

  ```powershell
  aws configure --profile corp-sso
  ```

### 3.3 Put cjp.exe on PATH

```powershell
mkdir $env:USERPROFILE\.local\bin -Force
copy \\share\cjp\cjp.exe $env:USERPROFILE\.local\bin\
```

The Claude Code installer puts `%USERPROFILE%\.local\bin` on PATH. Open a new terminal.

### 3.4 First start

Open a terminal in a git repository and run:

```powershell
cjp
```

The first start takes a few minutes. cjp:

- installs uv, rtk, and graphify (from astral.sh, github.com, and pypi.org, through the normal network),
- adds the profile `cjp` for the role to `%USERPROFILE%\.aws\config`,
- writes the policy into `%USERPROFILE%\.claude\settings.json`,
- adds rules to `CLAUDE.md` of the repository and builds the code graph in the background,
- starts the proxy and then Claude Code.

### 3.5 Check

```powershell
aws sts get-caller-identity --profile cjp    # Arn: ...:assumed-role/claude-code/...
cjp -p "say hello"                           # an answer from Haiku
Get-Content $env:LOCALAPPDATA\cjp\proxy.log  # no lines for amazonaws.com hosts
```

### 3.6 Every day

- Use `cjp` instead of `claude`. All arguments go to Claude Code.
- When the IAM Identity Center login expires, Claude Code runs `aws sso login` (a browser opens). You can also run it yourself.
- The VS Code extension starts Claude Code without cjp. It uses the settings that cjp wrote, but it has no proxy in front. Use the terminal.

## 4. Operations

| When | What |
|---|---|
| First month, each week | Check the cost in Cost Explorer. Claude shows up with its own service name, for example "Claude Haiku 5.5 (Amazon Bedrock Edition)". |
| The budget stopped Bedrock | The stop policy stays attached, also in the next month. AWS does not document an automatic undo. Reverse it: Budgets > `cjp-monthly` > the action > Reverse, or run the `detach-role-policy` command from 1.6. |
| Models or hosts change | Change `policy.yaml` or `.env`, build again (2.3), and hand out the exe again. If the models change, deploy the stack again with the new `HaikuProfileId` / `SonnetProfileId`. |
| Claude Code update | cjp blocks the automatic update. Run `claude update` in a normal terminal, or run the installer from 3.1 again. |
| Sonnet 5.5 gets a `jp.` profile | Change `CJP_MODEL_SONNET` and `SonnetProfileId`, then do the row "Models or hosts change". |

## 5. Troubleshooting

| You see | Why | Fix |
|---|---|---|
| `not authorized to perform: sts:AssumeRole` | The developer has no `claude-code-assume` policy, or `TrustedPrincipalArns` does not match. | 1.5 |
| `AccessDeniedException` ... `bedrock:InvokeModel` | The model is not in the role policy, the stop policy is attached, or the use case form is missing. | Compare the stack parameters with `cjp cjp-policy`. Check the policies on the role. 1.2 |
| `AWS authentication failed` / expired token | The login of the source profile has expired. | `aws sso login --profile corp-sso` |
| `[cjp] the AWS profile 'corp-sso' (the login for the role) was not found` | No login profile on this PC. | 3.2 |
| `[cjp] the AWS profile 'cjp' ... does not use the role of the policy` | `%USERPROFILE%\.aws\config` has a `cjp` profile with another role. | Remove that profile. cjp adds it again. |
| `BLOCK <host>` in `proxy.log` | The host is not allowed. | Add it to `proxy.allow_hosts` in `policy.yaml` or to `CJP_PROXY_ADD_HOSTS`, then build again. |
| `FAIL ... the upstream proxy answered: 407` in `proxy.log` | The corporate proxy needs a login. | Basic login: set `HTTPS_PROXY=http://user:pass@host:port` before `cjp`. NTLM or Kerberos: run Px or cntlm and set its address in `CJP_PROXY_UPSTREAM`. |
| `FAIL ... can not reach` in `proxy.log` | No direct internet, and cjp found no corporate proxy. | Set `CJP_PROXY_UPSTREAM` (or `HTTPS_PROXY`). |
| `[cjp] install of uv failed. It will retry in 24h.` | No network or proxy at the first start. | Set `HTTPS_PROXY`, delete `%LOCALAPPDATA%\cjp\*.failed`, and start again. |
| `[cjp] .claude/settings.json has settings that break the policy` | The shared project settings set env or AWS keys. | Remove them from the repository. |
| `[cjp] --settings is blocked` | `--settings` can change the env and models. | Do not use it with cjp. |
