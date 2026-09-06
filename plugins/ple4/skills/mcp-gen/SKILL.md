---
name: mcp-gen
description: MCP サーバを新規構築する。プロトコル 2026-07-28 + Python SDK `mcp` 2.x の検証済みテンプレートを複製して組み立て、実起動スモークで適合を確認するまでを一気通貫で行う。「MCP サーバを作って」「MCP サーバ化して」「ツールを MCP で公開して」「既存 API を MCP サーバにして」等で発火。既存 MCP サーバの単発バグ修正は /bugfix、脆弱性レビューは /secure を使う（サーバを新規に起こすなら本スキル）。
user-invocable: true
---

# MCP サーバ生成

## 大前提: 記憶で書くな。テンプレートを複製せよ

2026-07-28 は旧仕様を削除した破壊的改訂である。学習データにある形も、
星の多い既存サーバ（公式リファレンス実装ですら `mcp<2` 固定）も、旧仕様である。

**最悪なのは、旧仕様でも import が通りサーバが起動すること。** クラッシュしない
ので誤りに気づけない。ゼロから書かず、写経もせず、`assets/template/` を複製する。

| 旧（書いてはいけない） | 2026-07-28 |
|---|---|
| `FastMCP` | `MCPServer`（旧名も import は通る。最も危険） |
| `initialize` ハンドシェイク | 廃止。各リクエストの `_meta` が版と capability を運ぶ |
| `Mcp-Session-Id` セッション | 廃止。状態はツール引数のハンドルで持ち回す |
| GET の SSE ストリーム / `resources/subscribe` | 廃止。`subscriptions/listen` |
| サーバ発の sampling / elicitation | 廃止。MRTR（`resultType: "input_required"`） |
| `ctx.elicit()` で追加入力 | ステートレス下では失敗。MRTR のリゾルバを使う |
| `ctx.info()` / `ctx.log()` でログ | 非推奨。標準 `logging`（stderr）へ |
| Roots / Sampling / Logging | 非推奨。新規実装では採用しない |

## 手順

### 1. 要件を確定する

埋まらない項目があれば先に聞く。

- サーバ名 / 公開するツール（名前・入力・出力・副作用）
- リソース / プロンプトの要否
- トランスポート: stdio（既定）か Streamable HTTP
- **ツール実行の途中で確認・追加入力が要るか** → 要るなら MRTR（後述）
- 状態を跨いで保持するか → 要るならハンドル設計
- 認証の要否（HTTP のみ）→ 要ればテンプレート範囲外（後述）

### 2. テンプレートを複製する

テンプレートは**スキル起動時に提示されるベースディレクトリ**の `assets/template/`
にある。提示が無いときだけ `find "$HOME/.claude/plugins/cache" -maxdepth 7 -type d
-path '*/mcp-gen/assets/template'`（このフォールバックは Claude Code のキャッシュ配置に固有。他ホストではベースディレクトリの提示が必須） で探す。**`/Users` や `$HOME` 全体を起点にしない**
（返ってこない）。

複製先は**完全なリテラル絶対パス**で書く（`$VAR` を含めると保護フックに止められる）。

```bash
mkdir -p /abs/path/to/dest
cp /abs/skill/mcp-gen/assets/template/server.py /abs/path/to/dest/server.py
cp /abs/skill/mcp-gen/assets/template/smoke_check.py /abs/path/to/dest/smoke_check.py
cp /abs/skill/mcp-gen/assets/template/README.md /abs/path/to/dest/README.md
```

`pyproject.toml` は `pyproject.toml.template` の中身を読んで **Write ツールで作る**。
Bash 経由の書き込みは `bash_config_protection` に、Write/Edit は `config_protection` に検査される。生成先が ple4 リポジトリ外なら通るが、`ignore` / `select` / `exclude` / `addopts` / `fail_under` や lint セクション見出しを含む内容は経路によらず deny される。

### 3. 編集する

**差し替える**: `SERVER_NAME` / `SERVER_VERSION` / `instructions` / `cache_hints`、
サンプルのツール・リソース・プロンプト（不要なら削除）。

**触らない**: import 群、`MCPServer(...)` の構築、`main()` のトランスポート分岐、
`TransportSecuritySettings`。stdio だけの要件でも `--http` 分岐は残す。

#### `smoke_check.py` の「ここを編集する」ブロックも必ず合わせる

`HAPPY_TOOL` / `HAPPY_ARGS` / `HAPPY_EXPECTED_STRUCTURED` / `INVALID_TOOL` /
`INVALID_ARGS` / `INVALID_EXPECTED_MESSAGE` / `RESOURCE_URI` /
`RESOURCE_EXPECTED_SUBSTRING` / `PROMPT_NAME` / `PROMPT_ARGS` /
`PROMPT_EXPECTED_SUBSTRING`。サンプルのままだと検証が実サーバを見なくなる。

- **採用した面（tools / resources / prompts）はそれぞれ検査を持たせる。**
  公開しない面は `RESOURCE_URI = ""` / `PROMPT_NAME = ""`（SKIP として記録される。
  合格には潰さない）。
- **`HAPPY_TOOL` は出力が決定的なツール**。基準は「毎回新しい子プロセスで同じ順番に
  呼んで同じ値が返るか」。UUID・現在時刻・乱数は落ちる。プロセス内カウンタの採番 ID は
  その呼び出しが毎回 1 回目なら通る。迷ったら数回実行して確かめる。
- **`INVALID_TOOL` / `INVALID_ARGS` は「ツール本体まで到達して `ToolError` で失敗する」
  組み合わせにする。** `Field` の制約に引っかかる引数を選ぶと、SDK の引数検証が
  読めるメッセージを返すため `ToolError` を一度も通らずに検査が合格してしまう。
  存在しない ID など、**型と範囲は正しいが業務的に失敗する**引数を選ぶ。

#### 書くときの規則

- **モデルに読ませたい失敗は `ToolError` で投げる**
  （`from mcp.server.mcpserver.exceptions import ToolError`）。
  素の例外（`ValueError` 等）と、ツール本体から投げた `MCPError` は
  **メッセージが伏せられ**、モデルには `Error executing tool <name>` しか届かない。
- **引数の制約は型注釈で宣言する**（自前の `if` では schema にも載らず本文も届かない）:

  | 制約 | 書き方 | schema |
  |---|---|---|
  | 数値範囲 | `Annotated[int, Field(ge=1, le=100)]` | `minimum` / `maximum` |
  | 選択肢 | `Literal["dev", "prod"]` | `enum` |
  | 文字列書式 | `Annotated[str, Field(pattern=...)]` | `pattern` |
  | 文字列長 | `Annotated[str, Field(min_length=1, max_length=200)]` | `minLength` / `maxLength` |

  **`ToolError` 経路と衝突したら `ToolError` を優先する。** 引数を取るツールが 1 本で
  業務エラーが選択肢制約そのもの（単位・種別などの列挙型ドメイン）なら、`Literal` に
  した瞬間 `ToolError` が 1 本も無いサーバになる。`str` で受けて `ToolError` を投げ、
  選択肢は description・一覧ツール・エラー本文で補う。範囲・長さ制約は無関係なので残す。
- **`cache_hints` を意図して設定する。** 既定は `ttl_ms=0` / `scope="private"`（毎回
  取り直し）。キーを置ける先は `tools/list` / `prompts/list` / `resources/list` /
  `resources/templates/list` / `resources/read` / `server/discover` の 6 つ。
  **公開する面のキーを書き忘れるとその応答だけ `ttlMs=0` に取り残される**
  （`resources/read` の忘れが多い）。公開しない面のキーは消す。
  認可で内容が変わる一覧に `"public"` を付けない。
- **ログは標準 `logging`。** `MCPServer(log_level=...)` を渡せば SDK が設定する。
  出力は stderr。`print` は stdout を汚して JSON-RPC フレームを壊す。
  ログはモデルに届かない（届くのは戻り値だけ）。進捗は `ctx.report_progress()`。
- **HTTP なら `allowed_hosts` / `allowed_origins` を列挙する。** 既定は localhost のみ。
  実ホスト名では裸のホスト名とポート付きの両方を挙げる（忘れると全リクエストが `421`）。
- **状態はツール引数のハンドルで持ち回す。** ただし「不透明・有効期限付き・呼び出し
  ごとに認可再検証」が要るのは**認可文脈を運ぶハンドル**だけ。ドメイン上の識別子
  （注文番号など）は普通の ID でよい。取り違えると要件違反を作り込む。
- **`x-mcp-header` を機微な引数に付けない**（ヘッダは中間装置から見える）。
- **生成コードの説明文に旧 API 名を書かない。** `ctx.elicit()` を「使わない理由」として
  docstring に書くだけで手順 4 の grep が当たる。触れるなら「`Context` の `elicit`
  メソッド」のように名前を分割する。
- 全ての関数に docstring を付ける。

### 4. 検証する（省略不可）

このステップのコマンドは**すべて `mcp` が入った interpreter の絶対パス**で実行する
（以下 `$PY`）。`smoke_check.py` はサーバを `sys.executable` で起動するため、
裸の `python3` で呼ぶとサーバ側だけが `ModuleNotFoundError` で死ぬ。

```bash
"$PY" smoke_check.py
```

**exit code 0 を確認するまで完了報告しない。**

次に旧仕様の混入を機械確認する。パターンの正本は
`references/spec-2026-07-28.md` の「生成後に機械確認する禁止パターン（正本）」節。
**その節の grep をそのままフラグごと実行する**（ここに複製しない）。1 件でも当たれば
混入している。後方互換を意図して書いた場合のみ例外とし、対象版をコメントに明記する。

```bash
"$PY" -c "import mcp.types; print(mcp.types.LATEST_PROTOCOL_VERSION)"
```

`2026-07-28` 以外なら SDK が新仕様へ進んでいる。テンプレートを信じず、先に
changelog を読み直す（等号アサートはコードに入れない。新版の日に偽の失敗になる）。

### 5. 登録手順を渡す

`README.md` のクライアント設定 JSON を生成先の絶対パスで埋めて提示する。
`command` は `mcp` が入った `python3` の絶対パス。

## MRTR（ツール実行中の確認・追加入力）

テンプレートには入れていない。必要になったら足す。**`ctx.elicit()` は使わない**
（旧経路。ステートレス下では失敗する）。正しい経路はリゾルバによる依存注入:

```python
from mcp.server.mcpserver import Elicit, ElicitationResult, Resolve


def ask_confirm(target: str) -> Elicit[Confirm]:
    """確認を求める。"""
    return Elicit(f"{target} を削除してよいですか", Confirm)


@mcp.tool()
async def danger(
    target: str,
    confirm: Annotated[ElicitationResult[Confirm], Resolve(ask_confirm)],
) -> str:
    """確認を取ってから破壊的操作を行う。"""
    if confirm.action != "accept":
        return f"中止しました（{confirm.action}）"
    return "削除しました" if confirm.data.approve else "中止しました"
```

- **確認・同意では `ElicitationResult[T]` で受ける。** 素の `T` で受けると拒否が
  ツール実行エラーになり、「中止しました」を正常な戻り値で返せない。
  素の `T` が適切なのは「拒否＝呼び出しの失敗」でよい場合だけ。
- `REQUEST_META` の `clientCapabilities` に `{"elicitation": {"form": {}}}` を足す
  （無いと `-32021` で落ちる）。
- 検査は `smoke_check.py` 同梱の `elicit_round_trip` を使い、**承認と拒否の両分岐**まで
  見る。1 往復目だけでは拒否がエラー化する実装ミスを見つけられない。
  承認分岐が事前状態に依存するなら `setup=[("save_item", {...})]` で同一プロセスへ
  先に呼び出しを送る（空のストアへの削除は「対象が無い」エラーになり、配線の正否を
  判別できない）。

詳細と実測ワイヤ、`requestState` の完全性保護要件は `references/sdk-api-evidence.md`。

## テンプレート範囲外

- **認証（HTTP の OAuth）** — 認可サーバ探索・クライアント登録（動的登録は非推奨、
  CIMD へ）・RFC 9207 の `iss` 検証を含む独立した subsystem。必要なら
  <https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/index>
  を読んで設計する。stdio は認可仕様の対象外で、資格情報は環境変数から取る。
- **複数インスタンス運用** — MRTR を使うなら `RequestStateSecurity(keys=[...])` と
  全インスタンス同名の `MCPServer(...)` が要る（既定はプロセスごとに鍵を作るため、
  再送が別ワーカーへ届くと復号に失敗する）。

## 詰まったときに見るところ

- `server/discover` / `resultType` / `_meta.serverInfo` / `inputSchema` /
  `outputSchema` / `structuredContent` / 引数検証は **SDK が自動でやる**。
  これらを自前で実装しようとしていたら、記憶で書いている兆候。
- HTTP で `400 Bad Request: Missing session ID` → `MCP-Protocol-Version` ヘッダの
  付け忘れ。**この仕様にセッションは存在しない**。真に受けて実装すると廃止済みの
  機構を作り込む。
- 検証スクリプトを `test_*.py` / `*_test.py` と名付けない（親リポジトリの pytest に
  収集され、`mcp` 未導入の環境で無関係に失敗する）。

## 参照

- `references/spec-2026-07-28.md` — 削除・追加・非推奨の一覧、`_meta` 予約キー、
  セキュリティ必須事項、禁止パターン grep の**正本**
- `references/sdk-api-evidence.md` — `mcp` 2.1.1 の実測 API と実測ワイヤ JSON

## 永続メモリ

- 参照: `ple4_run ple4.mem.cli search "mcp server protocol template"`
  （`. "$HOME/.ple4/env.sh"` 前提）→ 本文が要る key だけ
  `ple4_run ple4.mem.cli show <key>`
- 記録: 再利用可能な学びだけ `ple4_mem_learn`。基準は `../learn/SKILL.md`
