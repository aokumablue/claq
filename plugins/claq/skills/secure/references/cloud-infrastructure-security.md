# クラウド・インフラセキュリティ

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

#### 確認項目

- [ ] 本番環境でルートアカウントを使用していない
- [ ] ストレージ（S3 等）を公開 ACL にせず、必要なアクセスだけを bucket policy で許可している
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

#### 確認項目

- [ ] すべてのシークレットをクラウドシークレットマネージャー（AWS Secrets Manager、Vercel Secrets）に保存している
- [ ] データベース認証情報の自動ローテーションを有効化している
- [ ] API キーを少なくとも四半期ごとにローテーションしている
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
- [ ] 転送時（TLS）と保存時（ストレージ・DB・バックアップ）の暗号化を有効化している

### 4. ログと監視

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
      - uses: actions/checkout@<コミット SHA>  # サードパーティの Action は SHA で固定する
      - name: Secret scanning   # シークレット走査
        uses: trufflesecurity/trufflehog@<コミット SHA>
      - name: Audit dependencies
        run: npm audit --audit-level=high
      - name: Configure AWS credentials  # 長期トークンでなく OIDC
        uses: aws-actions/configure-aws-credentials@<コミット SHA>
        with:
          role-to-assume: arn:aws:iam::123456789:role/GitHubActionsRole
          aws-region: us-east-1
```

#### 確認項目

- [ ] 長期有効な認証情報の代わりに OIDC を使用している
- [ ] サードパーティの Action をコミット SHA で固定している
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

Cloudflare WAF のマネージドルールセットを有効化し、レート制限とボット保護を設定する。

#### 確認項目

- [ ] マネージドルールセット付きで WAF を有効化している
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
