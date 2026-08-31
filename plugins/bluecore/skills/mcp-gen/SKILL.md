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
| `ctx.info()` / `ctx.log()` でログ | 非推奨（SEP-2577）。標準 `logging` で stderr へ |
| `ctx.elicit()` で追加入力 | ステートレス下では `-32600` で失敗。MRTR のリゾルバを使う |

最も危険なのは `FastMCP` である。**旧名でも import が通り、サーバは起動する**。
クラッシュしないので誤りに気づけない。したがってゼロから書き起こしてはならない。

**既存の人気サーバを写経するのも同じ罠。** 公式リファレンス実装
`modelcontextprotocol/servers`（約 90,000★）の Python サーバは
`mcp>=1.29.0,<2` に固定されており 2026-07-28 ではない（実測）。
星の多さは最新仕様であることを意味しない。2.x の実例として信頼できるのは
`modelcontextprotocol/python-sdk` の `examples/` だけで、それすら上流の
`deprecated/` ページより遅れている（非推奨の `ctx.info()` を今も使っている）。

**手順は 1 つ: `assets/template/` を複製して編集する。**

## 手順

### 1. 要件を確定する

以下が埋まらないまま進めない。埋まらない項目があれば先に聞く。

- サーバ名（識別子。ツール名の衝突回避にも使う）
- 公開するツール: 名前・入力・出力・副作用の有無
- リソース / プロンプトの要否
- トランスポート: stdio（ローカル・既定）か Streamable HTTP（リモート）
- **ツール実行の途中で利用者の入力・確認が要るか**（破壊的操作の確認など）。
  要るなら MRTR を使う（下記「テンプレートの範囲外」）
- 状態を跨いで保持する必要があるか（あるならハンドル設計が要る）
- 認証の要否（HTTP のみ。stdio は環境変数から資格情報を取る）。
  要るならテンプレートの範囲外（下記）

### 2. テンプレートを複製する

テンプレートは**この SKILL.md と同じディレクトリの** `assets/template/` にある。
スキル起動時に `Base directory for this skill:` として提示されるパスがそれである。
**それをそのまま使う**（探索は要らない）:

```bash
SKILL_DIR=<起動時に提示されたベースディレクトリ>
ls "$SKILL_DIR/assets/template/"
```

提示が無い場合だけ、配布ビルドのキャッシュを**深さを区切って**探す:

```bash
find "$HOME/.claude/plugins/cache" -maxdepth 7 -type d -path '*/mcp-gen/assets/template' 2>/dev/null | head -1
```

`/Users` や `$HOME` 全体を find の起点にしない。**10 分経っても返らない**（実測）。
どちらでも解決しなければユーザーにスキルの配置場所を聞く。解決できたら複製する:

```bash
mkdir -p <生成先> && cp "$SKILL_DIR"/assets/template/{server.py,smoke_check.py,README.md} <生成先>/
```

`pyproject.toml.template` の中身を読み、`pyproject.toml` を **Write ツールで作る**
（`cp` では作らない）。Bash 経由が通るかは生成先に依存し、実測では次の 3 通りに割れる:

| 生成先 | Bash のリダイレクト / `cp` |
|---|---|
| リポジトリ配下 | **止まる**（config 保護フック） |
| リポジトリ外・パスに変数を含む（`"$DIR/pyproject.toml"`） | **止まる**（フックはシェル展開を行わず cwd 基準で解決するため） |
| リポジトリ外・完全リテラルの絶対パス | 通る |

3 通りのうち 2 通りで止まり、しかも**どれに当たるかは書き方次第で変わる**。
Write ツールはいずれの場合も通るので、常に Write を使う。

### 3. 編集する

`server.py` の `SERVER_NAME` / `SERVER_VERSION` / `instructions` を置き換え、
サンプルのツール・リソース・プロンプトを要件のものに差し替える。骨組みは触らない。

**`smoke_check.py` 冒頭の「ここを編集する」ブロックも必ず合わせる**
（`HAPPY_TOOL` / `HAPPY_ARGS` / `HAPPY_EXPECTED_STRUCTURED` / `INVALID_TOOL` /
`INVALID_ARGS`）。サンプルのツール名のままだと検証が実サーバを見なくなる。
`INVALID_TOOL` / `INVALID_ARGS` には「実在するツール」×「**ツール本体まで到達して
`ToolError` で失敗する**引数」を指定する。**`Field` の制約に引っかかる引数を選んではいけない。**
引数検証は SDK が pydantic の読めるメッセージを返すため、`ToolError` を一度も
通らないまま `test_tool_error_reaches_the_model` が合格してしまう
（実測: `ToolError` が 1 つも無いサーバ、および業務エラーを素の
`FileNotFoundError` で投げるサーバが、どちらも exit 0 で通った）。
存在しない ID・ディレクトリを渡された等、**型と範囲は正しいが業務的に失敗する**
引数を選ぶこと。

**MRTR を使うなら、この「ここを編集する」ブロックの外にも手を入れる。**
`REQUEST_META` の `clientCapabilities` に `{"elicitation": {"form": {}}}` を足し
（足さないと `-32021 MissingRequiredClientCapability` で落ちる）、
1 往復目が `resultType: "input_required"` を返すことを見るテスト関数を追加して
`main()` の実行リストに載せる。ブロック内の定数だけでは足りない。

**生成コードの説明文に旧 API 名を書かない。** `ctx.elicit()` を「使わない理由」
として docstring に書くだけで、手順 4 の grep が当たる（正規表現は呼び出しと
散文を区別しない）。触れる必要があるときは「`Context` の `elicit` メソッド」の
ように名前を分割して書く。

守ること:

- **引数の制約は `Annotated[T, Field(ge=..., le=...)]` で宣言する。**
  `inputSchema` に `minimum` / `maximum` が載るのでモデルが呼ぶ前に範囲を知れ、
  違反時は pydantic の検証メッセージがそのまま届く。
- **モデルに読ませたい失敗は `ToolError` で投げる**
  （`from mcp.server.mcpserver.exceptions import ToolError`）。
  **素の例外（`ValueError` など）はメッセージが伏せられ**、モデルには
  `Error executing tool <name>` しか届かない。`isError` は立つのに
  何が悪かったのか分からず、同じ失敗を繰り返す。ツール本体から投げた
  `MCPError` も実測では同じく伏せられる（`UnexpectedToolError` に包まれる）。
- **`cache_hints` を意図して設定する。** 既定は `ttl_ms=0` / `scope="private"`
  ＝ クライアントは毎回取り直す。一覧が安定しているなら値を入れる。
  認可によって内容が変わる一覧に `"public"` を付けない。
- **ログは標準 `logging` で出す。** プロトコルのログ機能（`ctx.info()` 等）は
  非推奨（SEP-2577）。`MCPServer(log_level=...)` を渡せば SDK が設定を面倒見る。
  出力先は stderr なので stdio でも安全。`print` は stdout を汚してフレームを壊す。
  ログはモデルには届かない（届くのは戻り値だけ）。進捗は `ctx.report_progress()`
  （こちらは非推奨ではない）。
- **HTTP なら `allowed_hosts` / `allowed_origins` を列挙する。**
  DNS リバインディング保護は既定で有効だが許可リストは空（localhost のみ）。
  実ホスト名で公開するときは裸のホスト名とポート付きの両方を挙げる。
  挙げ忘れると**全リクエストが `421 Misdirected Request`** になる。
- **複数インスタンスで MRTR を使うなら `RequestStateSecurity(keys=[...])` と
  全インスタンス同名の `MCPServer(...)`。** 既定はプロセスごとに鍵を作るため、
  再送が別ワーカーへ届くと復号に失敗する。
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

続けて旧仕様の混入を機械確認する。パターンの正本は
`references/spec-2026-07-28.md` の「生成後に機械確認する禁止パターン（正本）」節。
**その節の grep をそのまま実行する**（ここに複製しない。2 か所に置くと片方だけ
更新され、検査したつもりの穴が残る）。

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

## テンプレートの範囲外（判断と理由）

以下はテンプレートに**入れない**。最小構成の可読性を保つため、
かつ動作確認が実クライアントを要して `smoke_check.py` で担保できないため。
必要になった時点で下記の指針に従って足す。

### MRTR（ツール実行中の追加入力）

Sampling / Elicitation / Roots のサーバ発リクエストは廃止され、MRTR に置き換わった。
**`ctx.elicit()` は使わない** — 旧経路の実装であり、ステートレスなトランスポートでは
`-32600` で失敗する（実測済み）。正しい経路はリゾルバによる依存注入:

```python
def ask_confirm(target: str) -> Elicit[Confirm]:
    """確認を求める。"""
    return Elicit(f"{target} を削除してよいですか", Confirm)


@mcp.tool()
async def danger(target: str, confirm: Annotated[Confirm, Resolve(ask_confirm)]) -> str:
    """確認を取ってから破壊的操作を行う。"""
    ...
```

完全な例・実測ワイヤ・`requestState` の完全性保護要件は
`references/sdk-api-evidence.md` の「MRTR」節。
MRTR を入れたら、`smoke_check.py` では**1 往復目が
`resultType: "input_required"` を返すことまでを確認する**（再送の解決は
実クライアントで確認する）。

### 認証（HTTP の OAuth）

`MCPServer` は `auth` / `token_verifier` / `auth_server_provider` を取るが、
認可サーバ探索・クライアント登録（動的登録は非推奨、CIMD へ移行）・
RFC 9207 の `iss` 検証まで含む独立した subsystem であり、テンプレート化すると
最小構成が崩れる。必要な場合は
<https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/index>
を読んでから設計する。stdio は認可仕様の対象外で、資格情報は環境変数から取る。

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
| 引数の型・制約の検証 | ○ | `Field` で宣言すればスキーマにも載る |
| `ttlMs` / `cacheScope` の有用な値 | | ● |
| `allowed_hosts` / `allowed_origins` | | ● |
| **エラー本文をモデルへ届ける** | | ● `ToolError` で投げた場合だけ |

自動の列を自前で実装しようとしていたら、それは記憶で書いている兆候である。

## 環境の落とし穴

- **`pyproject.toml` は Write ツールで作る**（Bash が通るかは生成先次第。手順 2 の表）。
- **禁止パターン grep から `__pycache__` を除外する。** `.pyc` には docstring が
  そのまま埋まり、しかもソースを直した後も古い文字列を保持する。
  当たるかは grep の実装依存（macOS ではバイナリを読み飛ばすため当たらない）だが、
  検査対象はソースでありビルド生成物ではない。正本の grep に入っている
  `-I --exclude-dir=__pycache__` を省かない。
- **検証スクリプトを `test_*.py` / `*_test.py` と名付けない。**
  親リポジトリの pytest に自動収集され、`mcp` 未導入の環境で無関係に失敗する。
  `smoke_check.py` はそのために意図してこの名前にしてある。
- **stdio に生 JSON-RPC を流すときは、全応答が揃うまで stdin を閉じない。**
  閉じると処理中のリクエストが落ちて応答が欠け、しかも失敗が速いケース
  （未知ツール）だけ返るため「特定のツールだけ壊れている」と誤読する。

## 参照

- `references/spec-2026-07-28.md` — 削除・追加・非推奨の一覧、`_meta` 予約キー、
  セキュリティ必須事項、禁止パターン grep の**正本**
- `references/sdk-api-evidence.md` — `mcp` 2.1.1 の実測シグネチャと実測ワイヤ JSON

## 永続メモリ

- 参照: `bluecore_run bluecore.mem.cli search "mcp server protocol template"`
  （`. "$HOME/.bluecore/env.sh"` 前提）→ 本文が要る key だけ
  `bluecore_run bluecore.mem.cli show <key>`
- 記録: 再利用可能な学びだけ `bluecore_mem_learn`。基準は `../learn/SKILL.md`
