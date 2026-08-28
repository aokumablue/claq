| 名前 | 説明 |
|------|-------------|
| cloud-infrastructure-security | クラウドプラットフォームへのデプロイ、インフラ設定、IAMポリシー管理、ログ/監視設定、CI/CDパイプライン実装時に使用。ベストプラクティスに沿ったクラウドセキュリティチェックリストを提供。 |

# クラウド・インフラセキュリティ

クラウドインフラ・CI/CDパイプライン・デプロイ設定がセキュリティベストプラクティスに従い、業界標準に準拠するようにする。

## 使用タイミング

- AWS・Vercel・Railway・Cloudflareなどのクラウドプラットフォームへアプリをデプロイする場合
- IAMロールと権限を設定する場合
- CI/CDパイプラインを構築する場合
- Terraform・CloudFormationなどでIaCを実装する場合
- ログと監視を設定する場合
- クラウド環境でシークレットを管理する場合
- CDNとエッジセキュリティを設定する場合
- 災害復旧とバックアップ戦略を実装する場合

## クラウドセキュリティチェックリスト

各例で `PASS` = 推奨、`FAIL` = アンチパターン。

### 1. IAM とアクセス制御

#### 最小権限の原則

```yaml
# PASS: 特定リソースへ必要な read のみ
iam_role:
  permissions: [s3:GetObject, s3:ListBucket]
  resources: [arn:aws:s3:::my-bucket/*]

# FAIL: s3:* を "*"（全アクション×全リソース）に付与
```

#### 多要素認証（MFA）

```bash
# ルート/管理者アカウントは必ず MFA を有効化
aws iam enable-mfa-device --user-name admin \
  --serial-number arn:aws:iam::123456789:mfa/admin \
  --authentication-code1 123456 --authentication-code2 789012
```

#### 確認項目

- [ ] 本番環境でルートアカウントを使用していない
- [ ] 特権アカウントすべてで MFA を有効化している
- [ ] サービスアカウントは長期有効な認証情報ではなくロールを使用している
- [ ] IAM ポリシーが最小権限の原則に従っている
- [ ] 定期的なアクセスレビューを実施している
- [ ] 未使用の認証情報をローテーションまたは削除している

### 2. シークレット管理

#### クラウドシークレットマネージャー

```typescript
// PASS: クラウド secrets manager から取得
const secret = await client.getSecretValue({ SecretId: 'prod/api-key' });
const apiKey = JSON.parse(secret.SecretString).key;

// FAIL: process.env.API_KEY のみ（未ローテーション・未監査）
```

#### シークレットのローテーション

```bash
# DB 認証情報を 30 日ごとに自動ローテーション
aws secretsmanager rotate-secret --secret-id prod/db-password \
  --rotation-lambda-arn arn:aws:lambda:region:account:function:rotate \
  --rotation-rules AutomaticallyAfterDays=30
```

#### 確認項目

- [ ] すべてのシークレットをクラウドシークレットマネージャー（AWS Secrets Manager、Vercel Secrets）に保存している
- [ ] データベース認証情報の自動ローテーションを有効化している
- [ ] API キーを少なくとも四半期ごとにローテーションしている
- [ ] コード、ログ、エラーメッセージにシークレットを含めていない
- [ ] シークレットアクセスの監査ログを有効化している

### 3. ネットワークセキュリティ

#### VPC とファイアウォールの設定

```terraform
# PASS: 制限された security group（内部VPCのみ受信、HTTPS外向きのみ）
resource "aws_security_group" "app" {
  ingress {
    from_port = 443; to_port = 443; protocol = "tcp"
    cidr_blocks = ["10.0.0.0/16"]
  }
  egress {
    from_port = 443; to_port = 443; protocol = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# FAIL: from_port=0 to_port=65535 protocol="tcp" cidr_blocks=["0.0.0.0/0"]（全ポート×全IP）
```

#### 確認項目

- [ ] データベースがインターネットに公開されていない
- [ ] SSH/RDP ポートが VPN/踏み台サーバーのみに制限されている
- [ ] セキュリティグループが最小権限の原則に従っている
- [ ] Network ACL が設定されている
- [ ] VPC フローログが有効化されている

### 4. ログと監視

#### CloudWatch/ログ設定

```typescript
// PASS: 包括的なロギング（機微データは記録しない）
await cloudwatch.putLogEvents({
  logGroupName: '/aws/security/events',
  logStreamName: 'authentication',
  logEvents: [{
    timestamp: Date.now(),
    message: JSON.stringify({ type: event.type, userId: event.userId, ip: event.ip, result: event.result }),
  }],
});
```

#### 確認項目

- [ ] すべてのサービスで CloudWatch/ログを有効化している
- [ ] 認証失敗の試行を記録している
- [ ] 管理者操作を監査している
- [ ] ログ保持期間を設定している（コンプライアンス向けに 90 日以上）
- [ ] 不審なアクティビティに対するアラートを設定している
- [ ] ログを一元化し、改ざんできないようにしている

### 5. CI/CD パイプラインのセキュリティ

#### 安全なパイプライン設定

```yaml
# PASS: 安全な GitHub Actions ワークフロー
jobs:
  deploy:
    permissions:
      contents: read           # 最小権限
    steps:
      - uses: actions/checkout@v4
      - name: Secret scanning   # シークレット走査
        uses: trufflesecurity/trufflehog@main
      - name: Audit dependencies
        run: npm audit --audit-level=high
      - name: Configure AWS credentials  # 長期トークンでなく OIDC
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::123456789:role/GitHubActionsRole
          aws-region: us-east-1
```

#### サプライチェーンセキュリティ

再現可能ビルドのため lock ファイル + 整合性チェックを使う（`npm ci` でインストール、`npm audit` で監査）。

#### 確認項目

- [ ] 長期有効な認証情報の代わりに OIDC を使用している
- [ ] パイプラインでシークレットスキャンを実施している
- [ ] 依存関係の脆弱性スキャンを行っている
- [ ] コンテナイメージスキャンを行っている（該当する場合）
- [ ] ブランチ保護ルールを適用している
- [ ] マージ前のコードレビューを必須にしている
- [ ] 署名付きコミットを必須にしている

### 6. Cloudflare と CDN のセキュリティ

#### Cloudflare のセキュリティ設定

```typescript
// PASS: Cloudflare Workers でセキュリティヘッダーを付与
const headers = new Headers(response.headers);
headers.set('X-Frame-Options', 'DENY');
headers.set('X-Content-Type-Options', 'nosniff');
headers.set('Referrer-Policy', 'strict-origin-when-cross-origin');
headers.set('Permissions-Policy', 'geolocation=(), microphone=()');
return new Response(response.body, { status: response.status, headers });
```

#### WAF ルール

Cloudflare WAF のマネージドルールを有効化: OWASP Core Ruleset / Cloudflare Managed Ruleset / レート制限 / ボット保護。

#### 確認項目

- [ ] OWASP ルール付きで WAF を有効化している
- [ ] レート制限を設定している
- [ ] ボット保護を有効化している
- [ ] DDoS 保護を有効化している
- [ ] セキュリティヘッダーを設定している
- [ ] SSL/TLS の厳格モードを有効化している

### 7. バックアップと災害復旧

#### 自動バックアップ

```terraform
# PASS: RDS の自動バックアップ
resource "aws_db_instance" "main" {
  backup_retention_period = 30       # 30 日保持
  backup_window           = "03:00-04:00"
  deletion_protection     = true     # 誤削除防止
}
```

#### 確認項目

- [ ] 自動の日次バックアップを設定している
- [ ] バックアップ保持期間がコンプライアンス要件を満たしている
- [ ] ポイントインタイムリカバリを有効化している
- [ ] バックアップテストを四半期ごとに実施している
- [ ] 災害復旧計画を文書化している
- [ ] RPO と RTO を定義し、テストしている

### 8. エージェント実行基盤

LLM エージェントを動かす基盤は、**不信な入力（取得したページ・ツール出力・他エージェントの
応答）を読む主体が、そのまま本番の認証情報とネットワークを持つ**という点で他のワークロードと
違う。プロンプト側の境界設計は `../SKILL.md` の「10. LLM / エージェントの信頼境界」が扱う。
ここはインフラ側の封じ込めだけを扱う。

```hcl
# PASS: エージェント実行ロールは読み取り専用 + リソース限定 + 期限付き
#   - 権限は「そのタスクで触る 1 バケット」まで絞る
#   - 書き込みが要るなら別ロールへ分け、人間の承認を挟む経路にだけ渡す
#   - セッション期間は分単位。長命なアクセスキーを配らない

# FAIL: 汎用の PowerUserAccess を実行ロールへ付与
#   注入が 1 回通れば、そのプロンプトが到達できる範囲＝ロールの権限範囲になる
```

- [ ] **エグレス制御**: エージェントの外向き通信を許可先リストで絞る。到達先を絞らないと、
      注入されたプロンプトがそのまま持ち出し経路になる（読めたものは送れる）
- [ ] **認証情報の非露出**: モデル API キー・クラウド認証情報をエージェントの
      環境変数やファイルシステムへ直接置かない。読める場所にある値は、プロンプト経由で
      出力させられる。取得は短命トークンを返すブローカ経由にする
- [ ] **ツール実行の隔離**: シェル・ファイル書込みを持つエージェントは使い捨ての
      サンドボックス（コンテナ / microVM）で動かし、ホストのソケット・認証情報ディレクトリ・
      クラウドメタデータエンドポイント（`169.254.169.254`）へ到達させない
- [ ] **MCP / ツールサーバの露出**: エージェントが接続するツールサーバを認証必須にし、
      公開ネットワークへ晒さない。ツール定義そのものも信頼できない入力として扱う
      （説明文がプロンプトへ入る）
- [ ] **監査ログ**: エージェントが実行したツール呼び出しと引数を、エージェント自身が
      書き換えられない先へ記録する。事後に「何を読んで何を実行したか」を追えなければ、
      注入が成立したかどうかを判定できない
- [ ] **人間のゲート**: 不可逆な操作（デプロイ・削除・権限変更・外部への送信）は
      エージェントの自己判断で完了させない。承認をエージェント自身の出力で満たせる設計に
      しない（自己承認の禁止）

## デプロイ前のクラウドセキュリティチェックリスト

本番環境へのクラウドデプロイ前には必ず確認してください:

- [ ] **IAM**: ルートアカウントを使用していない、MFA を有効化している、最小権限ポリシーを適用している
- [ ] **Secrets**: すべてのシークレットを、ローテーション対応のクラウドシークレットマネージャーで管理している
- [ ] **Network**: セキュリティグループを制限し、公開データベースを置いていない
- [ ] **Logging**: CloudWatch/ログを有効化し、保持期間を設定している
- [ ] **Monitoring**: 異常に対するアラートを設定している
- [ ] **CI/CD**: OIDC 認証、シークレットスキャン、依存関係監査を実施している
- [ ] **CDN/WAF**: OWASP ルール付きで Cloudflare WAF を有効化している
- [ ] **Encryption**: 保存時と転送時の両方でデータを暗号化している
- [ ] **Backups**: テスト済みの復旧手順を伴う自動バックアップを設定している
- [ ] **Compliance**: （該当する場合）GDPR/HIPAA 要件を満たしている
- [ ] **Documentation**: インフラを文書化し、手順書を作成している
- [ ] **Incident Response**: セキュリティインシデント対応計画を整備している
- [ ] **Agent Runtime**: エージェント実行ロールを最小権限・短命化し、エグレスを許可先リストで
      絞り、ツール実行を使い捨てサンドボックスへ隔離している

## よくあるクラウドセキュリティの設定ミス

- **S3 バケットの公開**: `--acl public-read` は不可。`--acl private` + 特定アクセスのみ許可する bucket policy を使う。
- **RDS のパブリックアクセス**: `publicly_accessible = true` は不可。`false` にし `vpc_security_group_ids` で制限する。
- **エージェントへの汎用ロール付与**: 「試行錯誤のため」と広い権限を渡したまま本番へ残す。注入が 1 回通れば、到達範囲はそのロールの権限範囲そのものになる。

## 参考資料

- [AWS セキュリティのベストプラクティス](https://aws.amazon.com/security/best-practices/)
- [CIS AWS Foundations ベンチマーク](https://www.cisecurity.org/benchmark/amazon_web_services)
- [Cloudflare のセキュリティドキュメント](https://developers.cloudflare.com/security/)
- [OWASP クラウドセキュリティ](https://owasp.org/www-project-cloud-security/)
- [Terraform のセキュリティベストプラクティス](https://www.terraform.io/docs/cloud/guides/recommended-practices/)

**覚えておいてください**: クラウドの設定ミスは、情報漏えいの主因です。公開された S3 バケットひとつ、または過度に権限の広い IAM ポリシーひとつで、インフラ全体が危険にさらされる可能性があります。常に最小権限の原則と多層防御を徹底してください。
