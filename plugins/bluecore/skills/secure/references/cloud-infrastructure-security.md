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

### 1. IAM とアクセス制御

#### 最小権限の原則

```yaml
# PASS: CORRECT: Minimal permissions
iam_role:
  permissions:
    - s3:GetObject  # Only read access
    - s3:ListBucket
  resources:
    - arn:aws:s3:::my-bucket/*  # Specific bucket only

# FAIL: WRONG: Overly broad permissions
iam_role:
  permissions:
    - s3:*  # All S3 actions
  resources:
    - "*"  # All resources
```

#### 多要素認証（MFA）

```bash
# ALWAYS enable MFA for root/admin accounts
aws iam enable-mfa-device \
  --user-name admin \
  --serial-number arn:aws:iam::123456789:mfa/admin \
  --authentication-code1 123456 \
  --authentication-code2 789012
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
// PASS: CORRECT: Use cloud secrets manager
import { SecretsManager } from '@aws-sdk/client-secrets-manager';

const client = new SecretsManager({ region: 'us-east-1' });
const secret = await client.getSecretValue({ SecretId: 'prod/api-key' });
const apiKey = JSON.parse(secret.SecretString).key;

// FAIL: WRONG: Hardcoded or in environment variables only
const apiKey = process.env.API_KEY; // Not rotated, not audited
```

#### シークレットのローテーション

```bash
# Set up automatic rotation for database credentials
aws secretsmanager rotate-secret \
  --secret-id prod/db-password \
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
# PASS: CORRECT: Restricted security group
resource "aws_security_group" "app" {
  name = "app-sg"

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]  # Internal VPC only
  }

  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]  # Only HTTPS outbound
  }
}

# FAIL: WRONG: Open to the internet
resource "aws_security_group" "bad" {
  ingress {
    from_port   = 0
    to_port     = 65535
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]  # All ports, all IPs!
  }
}
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
// PASS: CORRECT: Comprehensive logging
import { CloudWatchLogsClient, CreateLogStreamCommand } from '@aws-sdk/client-cloudwatch-logs';

const logSecurityEvent = async (event: SecurityEvent) => {
  await cloudwatch.putLogEvents({
    logGroupName: '/aws/security/events',
    logStreamName: 'authentication',
    logEvents: [{
      timestamp: Date.now(),
      message: JSON.stringify({
        type: event.type,
        userId: event.userId,
        ip: event.ip,
        result: event.result,
        // Never log sensitive data
      })
    }]
  });
};
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
# PASS: CORRECT: Secure GitHub Actions workflow
name: Deploy

on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions:
      contents: read  # Minimal permissions

    steps:
      - uses: actions/checkout@v4

      # Scan for secrets
      - name: Secret scanning
        uses: trufflesecurity/trufflehog@main

      # Dependency audit
      - name: Audit dependencies
        run: npm audit --audit-level=high

      # Use OIDC, not long-lived tokens
      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::123456789:role/GitHubActionsRole
          aws-region: us-east-1
```

#### サプライチェーンセキュリティ

```json
// package.json - Use lock files and integrity checks
{
  "scripts": {
    "install": "npm ci",  // Use ci for reproducible builds
    "audit": "npm audit --audit-level=moderate",
    "check": "npm outdated"
  }
}
```

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
// PASS: CORRECT: Cloudflare Workers with security headers
export default {
  async fetch(request: Request): Promise<Response> {
    const response = await fetch(request);

    // Add security headers
    const headers = new Headers(response.headers);
    headers.set('X-Frame-Options', 'DENY');
    headers.set('X-Content-Type-Options', 'nosniff');
    headers.set('Referrer-Policy', 'strict-origin-when-cross-origin');
    headers.set('Permissions-Policy', 'geolocation=(), microphone=()');

    return new Response(response.body, {
      status: response.status,
      headers
    });
  }
};
```

#### WAF ルール

```bash
# Enable Cloudflare WAF managed rules
# - OWASP Core Ruleset
# - Cloudflare Managed Ruleset
# - Rate limiting rules
# - Bot protection
```

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
# PASS: CORRECT: Automated RDS backups
resource "aws_db_instance" "main" {
  allocated_storage     = 20
  engine               = "postgres"

  backup_retention_period = 30  # 30 days retention
  backup_window          = "03:00-04:00"
  maintenance_window     = "mon:04:00-mon:05:00"

  enabled_cloudwatch_logs_exports = ["postgresql"]

  deletion_protection = true  # Prevent accidental deletion
}
```

#### 確認項目

- [ ] 自動の日次バックアップを設定している
- [ ] バックアップ保持期間がコンプライアンス要件を満たしている
- [ ] ポイントインタイムリカバリを有効化している
- [ ] バックアップテストを四半期ごとに実施している
- [ ] 災害復旧計画を文書化している
- [ ] RPO と RTO を定義し、テストしている

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

## よくあるクラウドセキュリティの設定ミス

### S3 バケットの公開

```bash
# FAIL: WRONG: Public bucket
aws s3api put-bucket-acl --bucket my-bucket --acl public-read

# PASS: CORRECT: Private bucket with specific access
aws s3api put-bucket-acl --bucket my-bucket --acl private
aws s3api put-bucket-policy --bucket my-bucket --policy file://policy.json
```

### RDS のパブリックアクセス

```terraform
# FAIL: WRONG
resource "aws_db_instance" "bad" {
  publicly_accessible = true  # NEVER do this!
}

# PASS: CORRECT
resource "aws_db_instance" "good" {
  publicly_accessible = false
  vpc_security_group_ids = [aws_security_group.db.id]
}
```

## 参考資料

- [AWS セキュリティのベストプラクティス](https://aws.amazon.com/security/best-practices/)
- [CIS AWS Foundations ベンチマーク](https://www.cisecurity.org/benchmark/amazon_web_services)
- [Cloudflare のセキュリティドキュメント](https://developers.cloudflare.com/security/)
- [OWASP クラウドセキュリティ](https://owasp.org/www-project-cloud-security/)
- [Terraform のセキュリティベストプラクティス](https://www.terraform.io/docs/cloud/guides/recommended-practices/)

**覚えておいてください**: クラウドの設定ミスは、情報漏えいの主因です。公開された S3 バケットひとつ、または過度に権限の広い IAM ポリシーひとつで、インフラ全体が危険にさらされる可能性があります。常に最小権限の原則と多層防御を徹底してください。
