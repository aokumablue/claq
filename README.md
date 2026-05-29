# deepblue

Claude Code 向けの汎用プラグイン集です。エージェント、スキル、コマンド、フック、永続メモリをひとまとめに導入し、計画・実装・検証・レビューの流れを揃えます。

## これは何か

deepblue は、Claude Code の作業を「最初の計画からレビューまで」通して支えるプラグインです。  
ユーザープロジェクトの言語ランタイムに依存せず、必要なときだけ個別のツールやコマンドを使います。

---

## 特長

| 項目 | 内容 |
| --- | --- |
| Agents | 計画、レビュー、TDD、セキュリティ、性能、探索などの専門サブエージェント |
| Skills | ワークフローや運用知識を段階的に案内 |
| Commands | `/c-*` で定番作業をすぐ呼び出し |
| Hooks | ツール実行の前後に自動チェックや記録を実行 |
| Memory | セッション履歴とチーム共有メモリを保持 |

---

## クイックスタート

### プラグインマーケットプレイス

```bash
claude plugin marketplace add aokumablue/deepblue
claude plugin install deepblue@deepblue
```

---

## 設定

チーム同期を使う場合だけ、`~/.deepblue/settings.json` を以下のように設定します。

```json
{
  "mem": {
    "sync": {
      "enabled": true,
      "postgres_url": "postgresql://deepblue:PASSWORD@localhost:5432/deepblue_mem"
    }
  }
}
```

`mem.sync.enabled` が `false` の場合は、ローカル利用のみで動きます。

---
