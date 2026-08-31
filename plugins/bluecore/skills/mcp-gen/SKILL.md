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

フックが見るのは**書き込み先のパス**なので、複製先は完全なリテラル絶対パスで書く
（`$VAR` を含めると止まる）。複製元は変数でよい:

```bash
mkdir -p /abs/path/to/dest
cp "$SKILL_DIR"/assets/template/server.py /abs/path/to/dest/server.py
cp "$SKILL_DIR"/assets/template/smoke_check.py /abs/path/to/dest/smoke_check.py
cp "$SKILL_DIR"/assets/template/README.md /abs/path/to/dest/README.md
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

**差し替える**: `SERVER_NAME` / `SERVER_VERSION` / `instructions` / `cache_hints` の
中身、およびサンプルのツール・リソース・プロンプト（不要なものは削除してよい）。

**触らない骨組み**: import 群、`MCPServer(...)` の構築そのもの、`main()` の
トランスポート分岐、`TransportSecuritySettings`。stdio だけが要件でも `--http`
分岐は残す（消しても動くが、後から HTTP 化するときに骨組みを再発明することになる）。

**`HAPPY_TOOL` には出力が決定的なツールを選ぶ。** `smoke_check.py` は
`structuredContent` を完全一致で比較するため、UUID や時刻や採番 ID を返すツールを
選ぶと必ず落ちる（しかも「謎の assert 失敗」としてしか現れない）。決定的にできない
場合は、その assert を必要な部分の比較へ書き換える。

**`smoke_check.py` 冒頭の「ここを編集する」ブロックも必ず合わせる**
（`HAPPY_TOOL` / `HAPPY_ARGS` / `HAPPY_EXPECTED_STRUCTURED` / `INVALID_TOOL` /
`INVALID_ARGS` / `INVALID_EXPECTED_MESSAGE` / `RESOURCE_URI` /
`RESOURCE_EXPECTED_SUBSTRING` / `PROMPT_NAME` / `PROMPT_ARGS` /
`PROMPT_EXPECTED_SUBSTRING`）。サンプルのツール名のままだと検証が実サーバを
見なくなる。**要件で採用した面（tools / resources / prompts）はそれぞれ
検査を持たせる。** 公開しない面は `RESOURCE_URI = ""` / `PROMPT_NAME = ""`
にする（その検査は SKIP と記録される。合格には潰さない）。
MRTR を採用したなら `elicit_round_trip` を使う検査を自分で足す
（テンプレートの MRTR 検査は同梱していない。ツールの形が要件ごとに違うため）。
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

- **引数の制約は型注釈で宣言する。** 目的は「制約が `inputSchema` に載って
  モデルが呼ぶ前に分かる」ことと「違反時に pydantic の読めるメッセージが届く」こと。
  自前の `if` 文ではどちらも得られない。形は制約の種類ごとに違う:

  | 制約 | 書き方 | `inputSchema` に載るもの |
  |---|---|---|
  | 数値範囲 | `Annotated[int, Field(ge=1, le=100)]` | `minimum` / `maximum` |
  | 選択肢 | `Literal["dev", "staging", "prod"]` | `enum` |
  | 文字列書式 | `Annotated[str, Field(pattern=r"^[a-z0-9-]+$")]` | `pattern` |
  | 文字列長 | `Annotated[str, Field(min_length=1, max_length=200)]` | `minLength` / `maxLength` |

  ただし `INVALID_TOOL` に選ぶツールの引数へ書式・選択肢制約を足すと、
  `ToolError` 経路が死んで手順 4 の検査が空振りする（手順 3 冒頭の注意）。

  **衝突したときは `ToolError` を優先する。** 引数を取るツールが 1 本しかなく、
  そのツールの業務エラーが選択肢制約そのもの（単位・種別・状態名などの
  列挙型ドメインでは常にこうなる）だと、`Literal` にした瞬間 `ToolError` が
  1 本も無いサーバになる。その場合は `str` で受けて `ToolError` を投げ、
  選択肢は description・専用の一覧ツール・エラー本文に列挙して補う。
  範囲・長さの制約はこの衝突と無関係なので `Field` のまま残す。
- **モデルに読ませたい失敗は `ToolError` で投げる**
  （`from mcp.server.mcpserver.exceptions import ToolError`）。
  **素の例外（`ValueError` など）はメッセージが伏せられ**、モデルには
  `Error executing tool <name>` しか届かない。`isError` は立つのに
  何が悪かったのか分からず、同じ失敗を繰り返す。ツール本体から投げた
  `MCPError` も実測では同じく伏せられる（`UnexpectedToolError` に包まれる）。
- **`cache_hints` を意図して設定する。** 既定は `ttl_ms=0` / `scope="private"`
  ＝ クライアントは毎回取り直す。一覧が安定しているなら値を入れる。
  認可によって内容が変わる一覧に `"public"` を付けない。
  キーを置ける先は `tools/list` / `prompts/list` / `resources/list` /
  `resources/templates/list` / `resources/read` / `server/discover` の 6 つ。
  **公開する面のキーを書き忘れるとその応答だけ `ttlMs=0` に取り残される**
  （`resources/read` の書き忘れが多い）。公開しない面のキーは消す。
- **ログは標準 `logging` で出す。** プロトコルのログ機能（`ctx.info()` 等）は
  非推奨（SEP-2577）。`MCPServer(log_level=...)` を渡せば SDK が設定を面倒見る。
  出力先は stderr なので stdio でも安全。`print` は stdout を汚してフレームを壊す。
  ログはモデルには届かない（届くのは戻り値だけ）。進捗は `ctx.report_progress()`
  （こちらは非推奨ではない）。
- **HTTP なら `allowed_hosts` / `allowed_origins` を列挙する。**
  DNS リバインディング保護は既定で有効だが許可リストは空（localhost のみ）。
  実ホスト名で公開するときは裸のホスト名とポート付きの両方を挙げる。
  挙げ忘れると**全リクエストが `421 Misdirected Request`** になる。
- **HTTP で `400 Bad Request: Missing session ID` が出たら、`MCP-Protocol-Version`
  ヘッダの付け忘れを疑う。** このメッセージは SDK が旧版の経路へフォールバック
  した結果であり、**この仕様にセッションは存在しない**。真に受けて
  セッション ID を実装しにいくと、廃止済みの機構を作り込むことになる。
- **複数インスタンスで MRTR を使うなら `RequestStateSecurity(keys=[...])` と
  全インスタンス同名の `MCPServer(...)`。** 既定はプロセスごとに鍵を作るため、
  再送が別ワーカーへ届くと復号に失敗する。
- **状態はツール引数のハンドルで持ち回す。** プロトコルにセッションは無い。
  ただし「不透明・有効期限付き・呼び出しごとに認可再検証」が要るのは
  **廃止されたセッションの代替として認可文脈を運ぶハンドル**に限る。
  ドメイン上の識別子（ブックマーク ID、注文番号など）は普通の ID でよく、
  一覧 API が全件返すなら推測不能性は何も守らない。取り違えると
  「有効期限付きブックマーク」のような要件違反を作り込む。
- **`x-mcp-header` を機微な引数に付けない。** ヘッダは中間装置から見える。
- 全ての関数に docstring を付ける。

### 4. 検証する（省略不可）

以降このステップのコマンドは**すべて `mcp` が入った interpreter の絶対パス**で
実行する（裸の `python3` では動かない）。以下これを `$PY` と書く。
venv を生成先に作ったなら `<生成先>/.venv/bin/python3`、既存の venv を使うなら
そのパス。生成先の中にあるとは限らない。

```bash
"$PY" smoke_check.py
```

`smoke_check.py` はサーバを `sys.executable` で子プロセスとして起動する。
つまり**検査を動かした interpreter がそのままサーバの interpreter になる**ため、
venv の python で呼ぶのが回避策ではなく正しい呼び出しである。
PATH 上の裸の `python3` で呼ぶと、検査自体は起動するのにサーバ側だけが
`ModuleNotFoundError: No module named 'mcp'` で死ぬ。

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
"$PY" -c "import mcp.types; print(mcp.types.LATEST_PROTOCOL_VERSION)"
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

**確認用途では `ElicitationResult[T]` で受けること。** 素の `T` で受けると
利用者が拒否したときツール呼び出し自体がエラーになり、「中止しました」を
正常な戻り値で返せない（実測。下表）。

```python
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

`decline` を受けたときの実測差（同一サーバ・同一リクエスト）:

| 受け方 | `isError` | `content` |
|---|---|---|
| `Annotated[T, Resolve(fn)]` | `true` | `Error executing tool ...: Resolver for parameter 'c' could not resolve: elicitation was decline` |
| `Annotated[ElicitationResult[T], Resolve(fn)]` | `false` | `中止しました（decline）` |

素の `T` が適切なのは「拒否＝呼び出しの失敗」でよい場合だけ（必須の入力を
取得できなかった等）。確認・同意はそれに当たらない。

完全な例・実測ワイヤ・`requestState` の完全性保護要件は
`references/sdk-api-evidence.md` の「MRTR」節。
MRTR を入れたら、`smoke_check.py` で**最低でも**1 往復目が
`resultType: "input_required"` を返すことを確認する。これは下限であって上限ではない。

確認・同意を扱うなら、テンプレート同梱の `smoke_check.py` にある
`elicit_round_trip` ヘルパで
**承認と拒否の両分岐**まで検査する。拒否がエラーとして返る実装ミス
（素の `Annotated[T, Resolve(fn)]` で受けた場合）は 1 往復目だけの検査では
見つからない。`_exchange` は全リクエストを先に書き込むため `requestState` を
再送へ渡せない。MRTR の往復にはこのヘルパを使うこと。

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
- **禁止パターン grep から `__pycache__` を除外する。** 検査対象はソースであって
  ビルド生成物ではない。`.pyc` には docstring がそのまま埋まり、ソースを直した後も
  古い文字列を保持する。正本の grep の `-I --exclude-dir=__pycache__` を省かない。
  なお `smoke_check.py` を**スクリプトとして実行しても `__pycache__` はできない**
  （できるのは import したとき）。
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
