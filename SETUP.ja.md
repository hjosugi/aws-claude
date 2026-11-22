# aws-claude 構築手順

English: [SETUP.md](SETUP.md)

何もない AWS アカウントから、開発者が `aws-claude` を使えるようになるまでの手順です。

| 章 | 担当 | 場所 |
|---|---|---|
| [1. AWS アカウント](#1-aws-アカウント) | AWS 管理者 | AWS CLI またはコンソール |
| [2. aws-claude.exe のビルド](#2-aws-claudeexe-のビルド) | 1 人(ビルド担当) | Windows PC |
| [3. 開発者の PC](#3-開発者の-pc) | 開発者それぞれ | Windows PC |
| [4. 運用](#4-運用) | AWS 管理者、ビルド担当 | |
| [5. トラブルシューティング](#5-トラブルシューティング) | 全員 | |

例では次の名前を使います。変えてもかまいませんが、すべての場所で同じ名前にしてください。

| 名前 | 例 | 使う場所 |
|---|---|---|
| スタック | `aws-claude` | CloudFormation |
| ロール | `claude-code` | CloudFormation(`RoleName`) |
| 開発者 PC のログイン用プロファイル | `corp-sso` | `.env`(`AWS_CLAUDE_SOURCE_PROFILE`)、`aws configure sso` |

## 1. AWS アカウント

Claude Code 専用の AWS アカウントを使ってください。予算はアカウント全体の費用で計算します。また、同じアカウントにある別の IAM ポリシーが Bedrock の権限を戻してしまうことがあります。

AWS CLI v2 と管理者権限が必要です。コマンドはすべて東京(`ap-northeast-1`)で実行します。

### 1.1 モデルを確認する

```powershell
aws bedrock list-inference-profiles --region ap-northeast-1 --query "inferenceProfileSummaries[?starts_with(inferenceProfileId, 'jp.anthropic')].inferenceProfileId"
```

一覧に `jp.anthropic.claude-haiku-5-5` と `jp.anthropic.claude-sonnet-4-6` があることを確認します。

### 1.2 Anthropic の利用申請(ユースケースフォーム)を出す

Claude Opus 4.7 より古い Anthropic のモデルは、最初に 1 回だけ利用申請が必要です。Sonnet 4.6 も対象です。

1. 東京リージョンで Amazon Bedrock のコンソールを開きます。
2. **モデルカタログ** から **Claude Sonnet 4.6** を開きます。
3. ユースケースの詳細を送信します。

申請はアカウントごとに 1 回です(組織の管理アカウントで 1 回でも可)。承認まで「保留中」のままになることがあります。

### 1.3 モデル呼び出しログがオフか確認する

オンになっていると、すべてのプロンプトと回答が S3 や CloudWatch Logs に保存されます。

```powershell
aws bedrock get-model-invocation-logging-configuration --region ap-northeast-1
aws bedrock get-model-invocation-logging-configuration --region ap-northeast-3
```

初期状態はオフです。出力に `loggingConfig` があればオンなので、オフにします。

```powershell
aws bedrock delete-model-invocation-logging-configuration --region ap-northeast-1
```

### 1.4 CloudFormation スタックをデプロイする

`cloudformation/aws-claude.yml` が作るもの:

- IAM ロール `claude-code`。`jp.` の推論プロファイルだけを呼べて、日本以外の Bedrock は使えません。ポリシーの中身は `aws-claude iam-policy` の出力と同じです。
- ポリシー `claude-code-assume`。開発者がロールを使うために必要です。
- 月次予算。`AlertAmount` でメールを送り、`StopAmount` でロールからの Bedrock 呼び出しを止めます。

東京にデプロイしてください。大阪では CloudFormation で予算(Budgets)を作れません。

```powershell
aws cloudformation deploy --region ap-northeast-1 --stack-name aws-claude `
  --template-file cloudformation/aws-claude.yml --capabilities CAPABILITY_NAMED_IAM `
  --parameter-overrides AlertEmail=you@example.co.jp StopAmount=80
```

(コンソールの場合:CloudFormation > スタックの作成 > テンプレートファイルのアップロード。「AWS CloudFormation によってカスタム名のついた IAM リソースが作成される場合があることを承認します」にチェックを入れます。)

パラメータ:

| パラメータ | 既定値 | 説明 |
|---|---|---|
| `RoleName` | `claude-code` | |
| `TrustedPrincipalArns` | 空 | ロールを使える人。空なら、アカウント内で `-assume` ポリシーを持つユーザーとロール全員。IAM Identity Center の例:`arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/*/AWSReservedSSO_Developer_*` |
| `MaxSessionDuration` | `3600` | 秒。切れたら AWS SDK が自動で取り直します。 |
| `BedrockRegion` | `ap-northeast-1` | `AWS_CLAUDE_REGION` と同じ値。 |
| `HaikuProfileId` | `jp.anthropic.claude-haiku-5-5` | `AWS_CLAUDE_MODEL_HAIKU` と同じ値。 |
| `SonnetProfileId` | `jp.anthropic.claude-sonnet-4-6` | `AWS_CLAUDE_MODEL_SONNET` と同じ値。Haiku だけにするなら空。 |
| `EnableBudget` | `true` | |
| `AlertEmail` | 空 | `EnableBudget` が `true` なら必須。 |
| `AlertAmount` | `50` | 月額(USD)。メールで知らせるだけです。 |
| `StopAmount` | `100` | 月額(USD)。費用データの反映が 8〜12 時間遅れるので、実際に止まるのはその後です。$100 を確実に超えたくないなら `80` にします。 |

出力を確認します。

```powershell
aws cloudformation describe-stacks --region ap-northeast-1 --stack-name aws-claude --query "Stacks[0].Outputs" --output table
```

`EnvLine`(`AWS_CLAUDE_ROLE_ARN=...`)をビルド担当に渡します。

### 1.5 開発者がロールを使えるようにする

- **IAM ユーザーの場合**:開発者の IAM グループに、ポリシー `claude-code-assume` を付けます。
- **IAM Identity Center の場合**:開発者の権限セットに、カスタマー管理ポリシー `claude-code-assume` を追加します。この権限セットだけに絞りたいときは、`TrustedPrincipalArns` も設定します(1.4 を参照)。IAM Identity Center から始まるロールのセッションは最長 1 時間ですが、SDK が自動で取り直します。

### 1.6 停止の動作をテストする

開発者が `aws-claude` を使えるようになったあと(3 章)に、1 回だけ行います。`StopPolicyArn` はスタックの出力にあります。

```powershell
aws iam attach-role-policy --role-name claude-code --policy-arn <StopPolicyArn>
# 開発者が aws-claude -p "hello" を実行 -> AccessDenied で失敗すれば OK
aws iam detach-role-policy --role-name claude-code --policy-arn <StopPolicyArn>
```

## 2. aws-claude.exe のビルド

環境ごとの設定は exe の中に入ります。exe しか持っていない人は、設定を変えられません。

### 2.1 ツールを入れる

- Git:https://git-scm.com/download/win
- uv:`winget install --id=astral-sh.uv -e`

### 2.2 ソースを取得して .env を書く

```powershell
git clone https://github.com/hjosugi/aws-claude.git
cd aws-claude
copy .env.example .env
notepad .env
```

最低限、次のキーを設定します。すべてのキーの説明は `.env.example` にあります。

```
AWS_CLAUDE_ROLE_ARN=arn:aws:iam::123456789012:role/claude-code
AWS_CLAUDE_SOURCE_PROFILE=corp-sso
# IAM Identity Center のときだけ:ログインが切れたときに claude がこのコマンドを実行します。
AWS_CLAUDE_AUTH_REFRESH=aws sso login --profile corp-sso
```

必要に応じて設定するもの:

| キー | 設定するとき |
|---|---|
| `AWS_CLAUDE_PROXY_UPSTREAM=http://proxy.example.co.jp:8080` | PC がプロキシの自動構成スクリプト(PAC)を使っているとき。または、プロキシのアドレスが `HTTPS_PROXY` にも Windows の設定にもないとき。 |
| `AWS_CLAUDE_PROXY_ADD_HOSTS=nexus.example.co.jp,*.git.example.co.jp` | 開発者が使う社内ホストがあるとき(パッケージのミラー、git サーバーなど)。 |
| `AWS_CLAUDE_MODEL_SONNET=none` | Haiku だけにするとき。スタックも `SonnetProfileId=`(空)でデプロイします。 |

`.env` にパスワードを書かないでください。値は平文のまま exe に入ります。`.env` は公開リポジトリには置かず、安全な場所に保管してください(git の管理対象外にしてあります)。

### 2.3 確認してビルドする

```powershell
uv run python aws_claude.py show-policy                       # .env の値を反映したポリシーと、許可ホストの一覧
uv run python -m unittest discover -s tests           # 任意:テスト
powershell -ExecutionPolicy Bypass -File .\build.ps1  # dist\aws-claude.exe を作る
dist\aws-claude.exe show-policy                               # "source: embedded in the exe" とロール ARN が出れば OK
```

別の環境用には別のファイル(例:`.env.prod`)を用意し、`.\build.ps1 -EnvFile .env.prod` でビルドします。

### 2.4 exe を配る

`dist\aws-claude.exe` を共有フォルダに置きます。exe は署名していないので、Windows SmartScreen やウイルス対策ソフトが警告を出すことがあります。ウイルス対策ソフトのせいで起動が遅い場合は、`.\build.ps1 -OneDir` でビルドして `dist\aws-claude` フォルダごと配ります。

## 3. 開発者の PC

### 3.1 ツールを入れる

ここでのダウンロードは aws-claude のプロキシを通りません。最初に済ませてください。

1. Git for Windows:https://git-scm.com/download/win(Windows の Claude Code に必要です)
2. Claude Code:`irm https://claude.ai/install.ps1 | iex` を実行し、新しいターミナルで `claude --version` を確認します。
3. AWS CLI v2:`winget install --id=Amazon.AWSCLI -e`

Claude Code に claude.ai のアカウントでログインしないでください。aws-claude はそれを使いません。

### 3.2 AWS にログインする

プロファイル名は `AWS_CLAUDE_SOURCE_PROFILE` と同じにします(ここでは `corp-sso`)。

- **IAM Identity Center の場合**:

  ```powershell
  aws configure sso --profile corp-sso
  aws sso login --profile corp-sso
  ```

- **IAM ユーザーの場合**(アクセスキー):

  ```powershell
  aws configure --profile corp-sso
  ```

### 3.3 aws-claude.exe を PATH の通った場所に置く

```powershell
mkdir $env:USERPROFILE\.local\bin -Force
copy \\share\aws-claude\aws-claude.exe $env:USERPROFILE\.local\bin\
```

`%USERPROFILE%\.local\bin` は、Claude Code のインストーラーが PATH に追加済みです。新しいターミナルを開きます。

### 3.4 初回起動

git リポジトリの中でターミナルを開き、実行します。

```powershell
aws-claude
```

初回は数分かかります。aws-claude は次のことをします。

- uv、rtk、graphify を入れる(astral.sh、github.com、pypi.org から。通常のネットワーク経由)
- ロール用のプロファイル `aws-claude` を `%USERPROFILE%\.aws\config` に追加する
- ポリシーを `%USERPROFILE%\.claude\settings.json` に書く
- リポジトリの `CLAUDE.md` にルールを追記し、バックグラウンドでコードグラフを作る
- プロキシを起動してから Claude Code を起動する

### 3.5 確認する

```powershell
aws sts get-caller-identity --profile aws-claude    # Arn: ...:assumed-role/claude-code/... になっていれば OK
aws-claude -p "say hello"                           # Haiku から返事が来れば OK
Get-Content $env:LOCALAPPDATA\aws-claude\proxy.log  # amazonaws.com のホストの行がなければ OK
```

### 3.6 毎日の使い方

- `claude` の代わりに `aws-claude` を使います。引数はすべて Claude Code に渡ります。
- IAM Identity Center のログインが切れると、Claude Code が `aws sso login` を実行します(ブラウザが開きます)。自分で実行してもかまいません。
- VS Code 拡張は aws-claude を通さずに Claude Code を起動します。aws-claude が書いた設定は使われますが、プロキシは通りません。ターミナルで使ってください。

## 4. 運用

| タイミング | やること |
|---|---|
| 最初の 1 か月、毎週 | Cost Explorer で費用を確認します。Claude は「Claude Haiku 5.5 (Amazon Bedrock Edition)」のような独自のサービス名で表示されます。 |
| 予算で Bedrock が止まったとき | 停止用ポリシーは翌月になっても付いたままです(自動で外れるとは AWS のドキュメントに書かれていません)。Budgets > `aws-claude-monthly` > アクション > 取り消し(Reverse)で外すか、1.6 の `detach-role-policy` を実行します。 |
| モデルや許可ホストを変えるとき | `policy.yaml` か `.env` を変えて再ビルドし(2.3)、exe を配り直します。モデルを変えたときは、新しい `HaikuProfileId` / `SonnetProfileId` でスタックもデプロイし直します。 |
| Claude Code の更新 | aws-claude は自動更新を止めています。通常のターミナルで `claude update` を実行するか、3.1 のインストーラーをもう一度実行します。 |
| Sonnet 5.5 に `jp.` プロファイルが出たとき | `AWS_CLAUDE_MODEL_SONNET` と `SonnetProfileId` を変えて、上の「モデルや許可ホストを変えるとき」を行います。 |

## 5. トラブルシューティング

| 表示 | 原因 | 対処 |
|---|---|---|
| `not authorized to perform: sts:AssumeRole` | 開発者に `claude-code-assume` ポリシーがない。または `TrustedPrincipalArns` に一致しない。 | 1.5 |
| `AccessDeniedException` ... `bedrock:InvokeModel` | モデルがロールのポリシーにない、停止用ポリシーが付いている、または利用申請をしていない。 | スタックのパラメータと `aws-claude show-policy` を比べる。ロールに付いているポリシーを確認する。1.2 |
| `AWS authentication failed` / トークンの期限切れ | ログイン用プロファイルのログインが切れた。 | `aws sso login --profile corp-sso` |
| `[aws-claude] the AWS profile 'corp-sso' (the login for the role) was not found` | この PC にログイン用プロファイルがない。 | 3.2 |
| `[aws-claude] the AWS profile 'aws-claude' ... does not use the role of the policy` | `%USERPROFILE%\.aws\config` に、別のロールを使う `aws-claude` プロファイルがある。 | そのプロファイルを消す。aws-claude が作り直します。 |
| `proxy.log` に `BLOCK <ホスト>` | そのホストは許可されていない。 | `policy.yaml` の `proxy.allow_hosts` か `AWS_CLAUDE_PROXY_ADD_HOSTS` に追加して再ビルドする。 |
| `proxy.log` に `FAIL ... the upstream proxy answered: 407` | 社内プロキシにログインが必要。 | Basic 認証:`aws-claude` の前に `HTTPS_PROXY=http://user:pass@host:port` を設定する。NTLM / Kerberos:Px か cntlm を起動し、そのアドレスを `AWS_CLAUDE_PROXY_UPSTREAM` に設定する。 |
| `proxy.log` に `FAIL ... can not reach` | インターネットに直接出られず、aws-claude が社内プロキシを見つけられなかった。 | `AWS_CLAUDE_PROXY_UPSTREAM`(または `HTTPS_PROXY`)を設定する。 |
| `[aws-claude] install of uv failed. It will retry in 24h.` | 初回起動時にネットワークかプロキシの問題があった。 | `HTTPS_PROXY` を設定し、`%LOCALAPPDATA%\aws-claude\*.failed` を消してから起動し直す。 |
| `[aws-claude] .claude/settings.json has settings that break the policy` | プロジェクト共有の設定が env や AWS キーを設定している。 | リポジトリからその設定を消す。 |
| `[aws-claude] --settings is blocked` | `--settings` で env やモデルを変えられてしまうため。 | aws-claude では使わない。 |
