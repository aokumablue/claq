---
name: mcp-gen
description: MCP サーバを新規構築する。プロトコル 2026-07-28 + Python SDK `mcp` 2.x の検証済みテンプレートを複製して組み立て、実起動スモークで適合を確認するまでを一気通貫で行う。「MCP サーバを作って」「MCP サーバ化して」「ツールを MCP で公開して」「既存 API を MCP サーバにして」等で発火。既存 MCP サーバの単発バグ修正は /bugfix、脆弱性レビューは /secure を使う（サーバを新規に起こすなら本スキル）。
user-invocable: true
---

# MCP サーバ生成

## 大前提: 記憶で書くな

学習データに残る MCP は 2025 年以前の形である。2026-07-28 はそれらを**削除**した。

| 記憶にある形 | 2026-07-28 での扱い |
|---|---|
| `FastMCP` | `MCPServer` に改名。ただし `mcp/server/fastmcp.py` は**まだ存在する** |
| `initialize` / `initialized` ハンドシェイク | 廃止。各リクエストの `_meta` が版と capability を運ぶ |
| `Mcp-Session-Id` セッション | 廃止。状態はツール引数のハンドルで持ち回す |
| GET による SSE ストリーム | 廃止。`subscriptions/listen` |
| `resources/subscribe` | 廃止。`subscriptions/listen` の opt-in |
| サーバ発の sampling / elicitation リクエスト | 廃止。MRTR（`resultType: "input_required"`） |
| Roots / Sampling / Logging | 非推奨。新規実装では採用しない |

最も危険なのは `FastMCP` である。**旧名でも import が通り、サーバは起動する**。
クラッシュしないので誤りに気づけない。したがってゼロから書き起こしてはならない。

**手順は 1 つ: `assets/template/` を複製して編集する。**

## 手順

### 1. 要件を確定する

以下が埋まらないまま進めない。埋まらない項目があれば先に聞く。

- サーバ名（識別子。ツール名の衝突回避にも使う）
- 公開するツール: 名前・入力・出力・副作用の有無
- リソース / プロンプトの要否
- トランスポート: stdio（ローカル・既定）か Streamable HTTP（リモート）
- 認証の要否（HTTP のみ。stdio は環境変数から資格情報を取る）
- 状態を跨いで保持する必要があるか（あるならハンドル設計が要る）

### 2. テンプレートを複製する

```bash
mkdir -p <生成先> && cp <skill>/assets/template/{server.py,smoke_check.py,README.md} <生成先>/
```

`pyproject.toml.template` → `pyproject.toml` は **Write ツールで作る**。
Bash のリダイレクト・`cp` は config 保護フックに止められる
（フックはファイル名で判定するため、生成先がリポジトリ外でも止まる）。

### 3. 編集する

`server.py` の `SERVER_NAME` / `SERVER_VERSION` / `instructions` を置き換え、
サンプルのツール・リソース・プロンプトを要件のものに差し替える。骨組みは触らない。

**`smoke_check.py` 冒頭の「ここを編集する」ブロックも必ず合わせる**
（`HAPPY_TOOL` / `HAPPY_ARGS` / `HAPPY_EXPECTED_STRUCTURED` / `INVALID_TOOL` /
`INVALID_ARGS`）。サンプルのツール名のままだと検証が実サーバを見なくなる。
`INVALID_TOOL` には「実在するが、渡した引数を入力検証で必ず弾くツール」を指定する。

守ること:

- **ツール引数は必ず検証する。** 範囲外・不正形式は `ValueError` を投げる。
  SDK がそれを `isError: true` のツール実行エラーに変換し、モデルが自己修正できる。
  JSON-RPC error にしてしまうと会話が復帰できない。
- **`cache_hints` を意図して設定する。** 既定は `ttl_ms=0` / `scope="private"`
  ＝ クライアントは毎回取り直す。一覧が安定しているなら値を入れる。
  認可によって内容が変わる一覧に `"public"` を付けない。
- **stdio では stdout に一切書かない。** `print` は JSON-RPC フレームを壊す。
  ログは `logging`（stderr）へ。
- **HTTP なら `allowed_hosts` / `allowed_origins` を列挙する。**
  DNS リバインディング保護は既定で有効だが許可リストは空。ワイルドカードを置かない。
- **状態はツール引数のハンドルで持ち回す。** プロトコルにセッションは無い。
  ハンドルは不透明・有効期限付きにし、呼び出しごとに認可を再検証する。
- **`x-mcp-header` を機微な引数に付けない。** ヘッダは中間装置から見える。
- 全ての関数に docstring を付ける。

### 4. 検証する（省略不可）

```bash
python3 smoke_check.py
```

サーバを実際に起動し、生の JSON-RPC で `server/discover` / `tools/list` /
`tools/call` を往復して応答形を確認する。**exit code 0 を確認するまで完了報告しない。**

続けて旧仕様の混入を機械確認する:

```bash
grep -rnE 'FastMCP|Mcp-Session-Id|mcp_session_id|resources/(un)?subscribe|Last-Event-ID|logging/setLevel|elicitationId|-32002|notifications/(initialized|elicitation/complete)' <生成先>
```

1 件でも当たれば旧仕様が混入している。後方互換を**意図して**書いた場合のみ例外とし、
対象プロトコル版をコメントに明記する。

さらに、SDK が想定通りの版を指しているか確認する:

```bash
python3 -c "import mcp.types; print(mcp.types.LATEST_PROTOCOL_VERSION)"
```

`2026-07-28` 以外が出たら、SDK が新しい仕様へ進んでいる。
その場合はテンプレートを信じず、**先に changelog を読み直してから**進める
（`== "2026-07-28"` の等号アサートはテンプレートに入れない。新版が出た日に
偽の失敗になり、本当の drift を隠すため）。

### 5. 登録手順を渡す

`README.md` のクライアント設定 JSON を生成先の絶対パスで埋めて提示する。
`command` は venv の `python3` の絶対パスにする。

## テンプレートが実証していること

`assets/template/server.py` は**そのまま動く**最小完全サーバであり、
以下が SDK 側で自動になることを実測済みである（`references/sdk-api-evidence.md`）。

| 項目 | 自動 | 手動 |
|---|---|---|
| `server/discover` 応答 | ○ | |
| `resultType` 付与 | ○ | |
| `_meta.serverInfo` | ○ | |
| `inputSchema` / `outputSchema` 生成 | ○ | |
| `structuredContent` + テキスト併記 | ○ | |
| 例外 → `isError: true` | ○ | |
| `ttlMs` / `cacheScope` の有用な値 | | ● |
| `allowed_hosts` / `allowed_origins` | | ● |

自動の列を自前で実装しようとしていたら、それは記憶で書いている兆候である。

## 環境の落とし穴

- **`pyproject.toml` は Write ツールで作る**（Bash は config 保護フックに止まる）。
- **検証スクリプトを `test_*.py` / `*_test.py` と名付けない。**
  親リポジトリの pytest に自動収集され、`mcp` 未導入の環境で無関係に失敗する。
  `smoke_check.py` はそのために意図してこの名前にしてある。
- **stdio に生 JSON-RPC を流すときは、全応答が揃うまで stdin を閉じない。**
  閉じると処理中のリクエストが落ちて応答が欠け、しかも失敗が速いケース
  （未知ツール）だけ返るため「特定のツールだけ壊れている」と誤読する。

## 参照

- `references/spec-2026-07-28.md` — 削除・追加・非推奨の一覧、`_meta` 予約キー、
  セキュリティ必須事項、禁止パターンの grep
- `references/sdk-api-evidence.md` — `mcp` 2.1.1 の実測シグネチャと実測ワイヤ JSON

## 永続メモリ

- 参照: `bluecore_run bluecore.mem.cli search "mcp server protocol template"`
  （`. "$HOME/.bluecore/env.sh"` 前提）→ 本文が要る key だけ
  `bluecore_run bluecore.mem.cli show <key>`
- 記録: 再利用可能な学びだけ `bluecore_mem_learn`。基準は `../learn/SKILL.md`
