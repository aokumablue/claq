---
name: mcp-gen
description: MCP サーバ（Python）を新規構築する。検証済みのスケルトンを複製し、上流 API を包んでデータを返すサーバなら上流 API ファサードの骨格を重ね、実起動スモークで仕様と設計規則への適合を確認するまでを一気通貫で行う。「MCP サーバを作って」「社内 API を MCP 化して」「ツールを MCP で公開して」等で発火。既存 MCP サーバの単発バグ修正は /bugfix、脆弱性レビューは /secure。
user-invocable: true
---

# MCP サーバ生成

## 大前提: スケルトンを複製し、記憶で書かない

2026-07-28 は旧仕様を削除した破壊的改訂である。学習データにある形も、
星の多い既存サーバ（公式リファレンス実装ですら `mcp<2` 固定）も、旧仕様である。

最悪なのは、旧仕様でも import が通りサーバが起動すること。クラッシュしない
ので誤りに気づけない。ゼロから書かず、写経もせず、`assets/` のスケルトンを複製する。
言語は Python（公式 SDK `mcp` 2.x）だけを扱う。

| 旧仕様（書かない） | 2026-07-28 |
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

止まって聞くのは手順1 の要件確認だけ。それ以外では、次の手順を予告して終わる要約・続けてよいかの伺い・作業を止めない判断事項の列挙・区切りや長さを理由にした報告で応答を終えず、手順5 の検証が通るまで続ける。

### 1. 要件と特性を確定する

埋まらない項目があれば先に聞く。

- サーバ名 / 公開するツール（名前・入力・出力・副作用）
- リソース / プロンプトの要否
- トランスポート: stdio（既定）か Streamable HTTP
- ツール実行の途中で確認・追加入力が要るか → 要るなら MRTR（後述）
- 状態を跨いで保持するか → 要るならハンドル設計
- 認証の要否（HTTP のみ）→ 要るなら `references/remote-auth.md`（実装はスケルトンの範囲外）

重ねるスケルトンと読む参照は、要件の特性で決める。規模（ツールの数や行数）では決めない。

| 特性 | 重ねる・読む |
|---|---|
| どのサーバでも | `references/tool-design.md` |
| 上流の API を呼び、そのデータを返す | `assets/facade-overlay/` を重ね、`references/facade.md` を読む |
| 応答の入れ子が深い・項目が多い・件数が多い | 同上（段階開示・サイズ予算・カーソル） |
| 複数の上流 API を結合して答える | 同上（ファンアウトと部分失敗） |
| 共有のリモート（複数の利用者）・個人情報を返す | `references/remote-auth.md` |
| ツールが 10 を超える | `references/tool-design.md` のツールセット |
| 実行中に利用者の確認が要る | 本書の MRTR 節 |

上流の API を包むときは、ツールを決める前に問いのカタログを作る。利用者が実際に聞く問いを
10〜30 件集め、それぞれに「どのツールをどの引数で呼べば答えられるか」を書く
（ツールを API ごとではなく問いから決めるため）。問いを集められないときは、利用者に例を求める。

### 2. スケルトンを複製する

スケルトンはスキル起動時に提示されるベースディレクトリの `assets/` にある。提示が無いときだけ次で探す（Claude Code のキャッシュ配置に固有のフォールバックで、他ホストではベースディレクトリの提示が必須）。`/Users` や `$HOME` 全体を起点にしない（返ってこない）。

```bash
find "$HOME/.claude/plugins/cache" -maxdepth 7 -type d -path '*/mcp-gen/assets/template'
```

複製先は完全なリテラル絶対パスで書く（`$VAR` を含めると保護フックに止められる）。
共通スケルトンは常に複製する。

```bash
mkdir -p /abs/path/to/dest
cp /abs/skill/mcp-gen/assets/template/server.py /abs/path/to/dest/server.py
cp /abs/skill/mcp-gen/assets/template/guard.py /abs/path/to/dest/guard.py
cp /abs/skill/mcp-gen/assets/template/transport.py /abs/path/to/dest/transport.py
cp /abs/skill/mcp-gen/assets/template/smoke_check.py /abs/path/to/dest/smoke_check.py
cp /abs/skill/mcp-gen/assets/template/smoke_config.py /abs/path/to/dest/smoke_config.py
cp /abs/skill/mcp-gen/assets/template/README.md /abs/path/to/dest/README.md
```

上流の API を包むなら、続けて重ね合わせを複製する（`server.py` と `smoke_config.py` は置き換わる）。

```bash
cp /abs/skill/mcp-gen/assets/facade-overlay/server.py /abs/path/to/dest/server.py
cp /abs/skill/mcp-gen/assets/facade-overlay/smoke_config.py /abs/path/to/dest/smoke_config.py
cp /abs/skill/mcp-gen/assets/facade-overlay/adapters.py /abs/path/to/dest/adapters.py
cp -R /abs/skill/mcp-gen/assets/facade-overlay/facade /abs/path/to/dest/facade
```

`pyproject.toml` は `pyproject.toml.template` の中身を読んで Write ツールで作る。重ね合わせたなら
`only-include` に `"adapters.py"` と `"facade"` を足す。
Bash 経由の書き込みは `bash_config_protection` に、Write/Edit は `config_protection` に検査される。生成先が claq リポジトリ外なら通るが、`ignore` / `select` / `exclude` / `addopts` / `fail_under` や lint セクション見出しを含む内容は経路によらず deny される。

### 3. ツールを設計する

`references/tool-design.md` を読み、ツールの一覧（名前・類型・引数・出力・注釈）を決める。
上流の API を包むなら `references/facade.md` も読み、正規ドメインモデルと項目カタログを決める。
問いのカタログの全ての問いに、答えるツール呼び出しの列があること。

### 4. 編集する

差し替える: `SERVER_NAME` / `SERVER_VERSION` / `instructions` / `cache_hints`、
見本のツール・リソース・プロンプト（不要なら削除）。重ね合わせたなら `CATALOG`・
出力モデル・`adapters.py` の偽物（実際の上流の呼び出しに置き換える）も。

触らない: `install_guard(mcp)` と `run(mcp)` の呼び出し、`guard.py`、`smoke_check.py`、
`facade/` の部品。`transport.py` は公開するホスト名を足すとき以外は触らない。
stdio だけの要件でも `--http` は残す。

#### `smoke_config.py` を合わせる

検査の対象（ツール名・引数・期待値・上限）はすべて `smoke_config.py` にある。
`smoke_check.py` は編集しない。サンプルのままだと検証が実サーバを見なくなる。

- 採用した面（tools / resources / prompts）はそれぞれ検査を持たせる。
  公開しない面は `RESOURCE_URI = ""` / `PROMPT_NAME = ""`（SKIP として記録される。
  合格には潰さない）。
- `HAPPY_TOOL` は出力が決定的なツール。基準は「毎回新しい子プロセスで同じ順番に
  呼んで同じ値が返るか」。UUID・現在時刻・乱数は落ちる。プロセス内カウンタの採番 ID は
  その呼び出しが毎回 1 回目なら通る。迷ったら数回実行して確かめる。
- `INVALID_TOOL` / `INVALID_ARGS` は「ツール本体まで到達して `ToolError` で失敗する」
  組み合わせにする。`Field` の制約に引っかかる引数を選ぶと、SDK の引数検証が
  読めるメッセージを返すため `ToolError` を一度も通らずに検査が合格してしまう。
  存在しない ID など、型と範囲は正しいが業務的に失敗する引数を選ぶ。
- 書き込みのツールがあるなら `READ_ONLY_SERVER = False`。
- 重ね合わせたなら `PAGED_*`（2 ページ以上ある条件と、値を 1 つだけ変えた条件）と
  `FIELDS_*`（綴りを誤った項目パスと、候補に出るべき正しいパス）を実際のツールで設定する。

#### 書くときの規則

- モデルに見せる説明は `@mcp.tool(description=...)` で書く。省くと docstring の全文
  （`Args:` や実装の注記）がそのまま説明になる。docstring は開発者向けに別に書く。
- モデルに読ませたい失敗は `ToolError` で投げる
  （`from mcp.server.mcpserver.exceptions import ToolError`）。
  素の例外（`ValueError` 等）と、ツール本体から投げた `MCPError` は
  メッセージが伏せられ、モデルには `Error executing tool <name>` しか届かない。
  重ね合わせの部品の失敗は `with model_errors():` の中で `ToolError` に変わる。
- 引数の制約は型注釈で宣言する（自前の `if` では schema にも載らず本文も届かない）:

  | 制約 | 書き方 | schema |
  |---|---|---|
  | 数値範囲 | `Annotated[int, Field(ge=1, le=100)]` | `minimum` / `maximum` |
  | 選択肢 | `Literal["dev", "prod"]` | `enum` |
  | 文字列書式 | `Annotated[str, Field(pattern=...)]` | `pattern` |
  | 文字列長 | `Annotated[str, Field(min_length=1, max_length=200)]` | `minLength` / `maxLength` |
  | 例 | `Annotated[str, Field(examples=["..."])]` | `examples` |

  `ToolError` 経路と衝突したら `ToolError` を優先する。引数を取るツールが 1 本で
  業務エラーが選択肢制約そのもの（単位・種別などの列挙型ドメイン）なら、`Literal` に
  した瞬間 `ToolError` が 1 本も無いサーバになる。`str` で受けて `ToolError` を投げ、
  選択肢は description・一覧ツール・エラー本文で補う。範囲・長さ制約は無関係なので残す。
- 引数は平坦に並べ、入れ子のモデルを引数にしない（公開スキーマに `$defs` / `$ref` が出て
  smoke が落とす）。省略可能な引数は `X | None = None` でよい（`anyOf` は guard が外す）。
- 出力は戻り値の型（pydantic モデル）で書く。増えうる値は `Literal` にせず `str` と説明で示す
  （出力スキーマを閉じると、値や項目を足したときに古いスキーマでの検証が落ちる）。
- 注釈は `ToolAnnotations(readOnlyHint=True, openWorldHint=False)` を基本にする。
  `idempotentHint` / `destructiveHint` は `readOnlyHint=False` のときだけ付ける。
- `cache_hints` を意図して設定する。既定は `ttl_ms=0` / `scope="private"`（毎回
  取り直し）。キーを置ける先は `tools/list` / `prompts/list` / `resources/list` /
  `resources/templates/list` / `resources/read` / `server/discover` の 6 つ。
  公開する面のキーを書き忘れるとその応答だけ `ttlMs=0` に取り残される
  （`resources/read` の忘れが多い）。公開しない面のキーは消す。
  認可で内容が変わる一覧に `"public"` を付けない。
- ログは標準 `logging`。`MCPServer(log_level=...)` を渡せば SDK が設定する。
  出力は stderr。`print` は stdout を汚して JSON-RPC フレームを壊す。
  ログはモデルに届かない（届くのは戻り値だけ）。進捗は `ctx.report_progress()`。
- 状態はツール引数のハンドルで持ち回す。ただし「不透明・有効期限付き・呼び出し
  ごとに認可再検証」が要るのは認可文脈を運ぶハンドルだけ。ドメイン上の識別子
  （注文番号など）は普通の ID でよい。取り違えると要件違反を作り込む。
- `x-mcp-header` を機微な引数に付けない（ヘッダは中間装置から見える）。
- 生成コードの説明文に旧 API 名を書かない。`ctx.elicit()` を「使わない理由」として
  docstring に書くだけで手順 5 の grep が当たる。触れるなら「`Context` の `elicit`
  メソッド」のように名前を分割する。
- 全ての関数に docstring を付ける。

### 5. 検証する（省略不可）

このステップのコマンドはすべて `mcp` が入った interpreter の絶対パスで実行する
（以下 `$PY`）。`smoke_check.py` はサーバを `sys.executable` で起動するため、
裸の `python3` で呼ぶとサーバ側だけが `ModuleNotFoundError` で死ぬ。

```bash
"$PY" smoke_check.py
```

exit code 0 を確認するまで完了報告しない。プロトコルの形に加えて、ツール定義の設計規則・
未知の引数の拒否・text の写し・結果の大きさ（重ね合わせたならページングと項目パスも）を確かめる。

次に旧仕様の混入を機械確認する。パターンの正本は
`references/spec-2026-07-28.md` の `## 生成後に機械確認する禁止パターン（正本）`。
その節の grep をそのままフラグごと実行する（ここに複製しない）。1 件でも当たれば
混入している。後方互換を意図して書いた場合のみ例外とし、対象版をコメントに明記する。

```bash
"$PY" -c "import mcp.types; print(mcp.types.LATEST_PROTOCOL_VERSION)"
```

`2026-07-28` 以外なら SDK が新仕様へ進んでいる。スケルトンを信じず、先に
changelog を読み直す（等号アサートはコードに入れない。新版の日に偽の失敗になる）。

### 6. 登録と評価の手順を渡す

`README.md` のクライアント設定 JSON を生成先の絶対パスで埋めて提示する。
`command` は `mcp` が入った `python3` の絶対パス。問いのカタログを作ったなら、
README の「エージェントでの評価」の手順と合わせて渡す（実際のホストとモデルで解かせ、
誤りを説明・既定値・ツールの境界に帰着させて直す）。

## MRTR（ツール実行中の確認・追加入力）

スケルトンには入れていない。必要になったら足す。`ctx.elicit()` は使わない
（旧経路。ステートレス下では失敗する）。正しい経路はリゾルバによる依存注入:

```python
from mcp.server.mcpserver import Elicit, ElicitationResult, Resolve


def ask_confirm(target: str) -> Elicit[Confirm]:
    """確認を求める。"""
    return Elicit(f"{target} を削除してよいですか", Confirm)


@mcp.tool(description="対象を削除する。実行前に利用者の確認を取る。")
async def danger(
    target: str,
    confirm: Annotated[ElicitationResult[Confirm], Resolve(ask_confirm)],
) -> str:
    """確認を取ってから破壊的操作を行う。"""
    if confirm.action != "accept":
        return f"中止しました（{confirm.action}）"
    return "削除しました" if confirm.data.approve else "中止しました"
```

- 確認・同意では `ElicitationResult[T]` で受ける。素の `T` で受けると拒否が
  ツール実行エラーになり、「中止しました」を正常な戻り値で返せない。
  素の `T` が適切なのは「拒否＝呼び出しの失敗」でよい場合だけ。
- `smoke_config.py` の `CLIENT_CAPABILITIES` に `{"elicitation": {"form": {}}}` を入れる
  （無いと `-32021` で落ちる）。
- `MRTR_CASES` に承認と拒否の両分岐を書く。1 往復目だけでは拒否がエラー化する
  実装ミスを見つけられない。承認分岐が事前状態に依存するなら、ケースの `setup` で
  同一プロセスへ先に呼び出しを送る（空のストアへの削除は「対象が無い」エラーになり、
  配線の正否を判別できない）。

詳細と実測ワイヤ、`requestState` の完全性保護要件は `references/sdk-api-evidence.md`。

## スケルトンの範囲外

- 認証（HTTP の OAuth）— 認可サーバ探索・クライアント登録（動的登録は非推奨、
  CIMD へ）・RFC 9207 の `iss` 検証を含む独立した subsystem。満たすべき要件と
  上流の資格情報・データ層の認可は `references/remote-auth.md`。
  stdio は認可仕様の対象外で、資格情報は環境変数から取る。
- 複数インスタンス運用 — MRTR を使うなら `RequestStateSecurity(keys=[...])` と
  全インスタンス同名の `MCPServer(...)` が要る（既定はプロセスごとに鍵を作るため、
  再送が別ワーカーへ届くと復号に失敗する）。重ね合わせのカーソルの鍵も共有する。

## 詰まったときに見るところ

- `server/discover` / `resultType` / `_meta.serverInfo` / `inputSchema` /
  `outputSchema` / `structuredContent` / 引数検証は SDK が自動でやる。
  これらを自前で実装しようとしていたら、記憶で書いている兆候。
- smoke が「docstring がそのまま説明になっています」で落ちる → `@mcp.tool(description=...)` を書く。
- smoke が「inputSchema に [...] が残っています」で落ちる → 入れ子のモデル引数を平坦な引数にする。
- smoke が「未知の引数が受け付けられました」で落ちる → `install_guard(mcp)` を消していないか確かめる。
- HTTP で `400 Bad Request: Missing session ID` → `MCP-Protocol-Version` ヘッダの
  付け忘れ。この仕様にセッションは存在しない。真に受けて実装すると廃止済みの
  機構を作り込む。
- 検証スクリプトを `test_*.py` / `*_test.py` と名付けない（親リポジトリの pytest に
  収集され、`mcp` 未導入の環境で無関係に失敗する）。

## 参照

- `references/tool-design.md` — ツールの類型・数・名前・説明・引数・出力・失敗・注釈・状態の規則
- `references/facade.md` — 上流 API ファサードの重ね合わせ（正規化・段階開示・予算・カーソル・合成・上流の呼び出し）
- `references/remote-auth.md` — リモート配置の認可、上流の資格情報、データ層の認可
- `references/spec-2026-07-28.md` — 削除・追加・非推奨の一覧、`_meta` 予約キー、
  セキュリティ必須事項、禁止パターン grep の正本
- `references/sdk-api-evidence.md` — `mcp` 2.x の実測 API と実測ワイヤ JSON

## 永続メモリ

- 参照: `claq_run claq.mem.cli search "mcp server protocol template"`
  （`. "$HOME/.claq/env.sh"` 前提）→ 本文が要る key だけ
  `claq_run claq.mem.cli show <key>`
- 記録: 再利用可能な学びだけ `claq_mem_learn`。基準は `../learn/SKILL.md`
