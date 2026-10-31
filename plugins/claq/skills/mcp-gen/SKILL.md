---
name: mcp-gen
description: Python の MCP サーバを新規構築する。「MCP サーバを作って」「社内 API を MCP 化して」「ツールを MCP で公開して」等で発火。Python 以外の言語の MCP サーバは対象外。既存 MCP サーバの単発バグ修正は /bugfix、脆弱性レビューは /secure。
user-invocable: true
---

# MCP サーバ生成

## 大前提: スケルトンを複製し、記憶で書かない

2026-07-28 は旧仕様を削除した破壊的改訂である。学習データにある形も、
星の多い既存サーバも、旧仕様である。

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
- 認証の要否（HTTP のみ）

重ねるスケルトンと読む参照は、要件の特性で決める。規模（ツールの数や行数）では決めない。

| 特性 | 重ねる・読む |
|---|---|
| どのサーバでも | `references/tool-design.md` |
| 上流の API を呼び、そのデータを返す | `assets/facade-overlay/` を重ね、`references/facade.md` を読む |
| 応答の入れ子が深い・項目が多い・件数が多い | 同上（段階開示・サイズ予算・カーソル） |
| 複数の上流 API を結合して答える | 同上（ファンアウトと部分失敗） |
| 上流の契約にレート制限・同時実行の上限がある | facade-overlay に加えて `assets/upstream-protection-overlay/` を重ね、`references/upstream-protection.md` を読む |
| 共有のリモート（複数の利用者）・個人情報を返す | `references/remote-auth.md` と、同上の upstream-protection-overlay |
| 実行中に利用者の確認が要る | `references/sdk-api-evidence.md` の MRTR 節 |

上流の API を包むときは、ツールを決める前に問いのカタログを作る。利用者が実際に聞く問いを
10〜30 件集め、それぞれに「どのツールをどの引数で呼べば答えられるか」を書く
（ツールを API ごとではなく問いから決めるため）。問いを集められないときは、利用者に例を求める。

### 2. スケルトンを複製する

スケルトンはスキル起動時に提示されるベースディレクトリの `assets/` にある。提示が無いときは、次が返す plugin root の `skills/mcp-gen/assets/` を使う（失敗したら止めて報告する）。

```bash
. "$HOME/.claq/env.sh" && claq_plugin_root
```

複製先は完全なリテラル絶対パスで書く。
共通スケルトンは常に複製する。

```bash
mkdir -p /abs/path/to/dest
cp /abs/skill/mcp-gen/assets/template/server.py /abs/path/to/dest/server.py
cp /abs/skill/mcp-gen/assets/template/guard.py /abs/path/to/dest/guard.py
cp /abs/skill/mcp-gen/assets/template/transport.py /abs/path/to/dest/transport.py
cp /abs/skill/mcp-gen/assets/template/smoke_check.py /abs/path/to/dest/smoke_check.py
cp /abs/skill/mcp-gen/assets/template/smoke_static.py /abs/path/to/dest/smoke_static.py
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
（Bash で書くと、生成先が作業中の git リポジトリ内なら内容によらず保護フックに止められる。Write でも lint・テスト・カバレッジの節やキー（`ignore` / `select` / `exclude` / `addopts` / `fail_under` 等）を含むと止められる）

### 3. ツールを設計する

`references/tool-design.md` を読み、ツールの一覧（名前・類型・引数・出力・注釈）を決める。
上流の API を包むなら `references/facade.md` も読み、正規ドメインモデルと項目カタログを決める。
問いのカタログを作ったなら、その全ての問いに答えるツール呼び出しの列があること。

### 4. 編集する

差し替える: `SERVER_NAME` / `SERVER_VERSION` / `instructions` / `cache_hints`、
`pyproject.toml` の `name` と `[project.scripts]` のコマンド名（`SERVER_NAME` にそろえる）、
見本のツール・リソース・プロンプト（不要なら削除）、受信側のレート制限の定数（`TOOL_CALLS_PER_WINDOW` /
`TOOL_CALL_WINDOW_SECONDS`）と主体を返す `_principal`。重ね合わせたなら `CATALOG`・
出力モデル・`adapters.py` の偽物（実際の上流の呼び出しに置き換える）も。upstream-protection-overlay を重ねたら、
上限の定数を上流の契約に合わせる（`references/upstream-protection.md`）。

触らない: `install_guard(mcp, ...)` と `run(mcp)` の呼び出し（上限は定数で差し替える）、`guard.py`、`smoke_check.py`、
`smoke_static.py`、`facade/` の部品。`transport.py` は公開するホスト名を足すとき以外は触らない。

ツールの書き方（説明・引数・出力・失敗・注釈・状態）は `references/tool-design.md` に従う。
検査の対象は `smoke_config.py` に書く。値の選び方はファイル内のコメントに従い、サンプルの
ままにしない（検証が実サーバを見なくなる）。

ツール以外で守ること:

1. 依頼・計画・仕様から要件の文を拾い、それを満たすコードを書く。書き方は言語・ライブラリ・仕様の現行の推奨で選び、要件の振る舞いを成り立たせる書き方（パラメータ化クエリ、ネットワーク呼び出しのタイムアウト、トランザクションの commit 等）もここに含める
2. 外部入力は入口で 1 回検証し、内側は型と呼び出し元を信頼して書く
3. 失敗は例外・エラー値のまま呼び出し元へ返す
4. 入力の拒否・上限の検査は、根拠（要件の文、攻撃者と入口、再現できる失敗、外部 API 契約の条項）を引用できるときに書く。要件の文は依頼・仕様・ADR に依頼者の決定として書かれた文、再現できる失敗は要件に書かれた振る舞いが崩れることをテストか手順で示せるものを指す
5. 失敗の後に続行する経路（握り潰し・既定値・再試行・代替経路）は、続行を決めた文（要件・利用者の指示・外部 API 契約）を引用できるときに書く
6. 4・5 で書いたものは、根拠をコメント 1 行と報告の「追加した防御」に残す。要件に無いが入れたほうがよいと判断したもの（ベストプラクティス由来を含む）は、報告の「提案」に理由と出典を付けて書く。提案は採否を利用者が決める
7. 原因の箇所を直し、変更で使われなくなったコード・テスト・互換分岐は同じ変更で消す。抽象と設定項目は、今ある呼び出し元が使う分だけ作る。コメントは「なぜ」を書く
- `cache_hints` を意図して設定する。既定は `ttl_ms=0` / `scope="private"`（毎回
  取り直し）。キーを置ける先は `tools/list` / `prompts/list` / `resources/list` /
  `resources/templates/list` / `resources/read` / `server/discover` の 6 つ。
  公開する面のキーを書き忘れるとその応答だけ `ttlMs=0` に取り残される
  （`resources/read` の忘れが多い）。公開しない面のキーは消す。
  主体や認可で内容が変わる応答（一覧と `resources/read`）に `"public"` を付けない。
- ログは標準 `logging`。`MCPServer(log_level=...)` を渡せば SDK が設定する。
  出力は stderr。`print` は stdout を汚して JSON-RPC フレームを壊す。
  ログはモデルに届かない（届くのは戻り値だけ）。進捗は `ctx.report_progress()`。
- 生成コードの説明文に旧 API 名を書かない。説明文に書いただけでも手順 5 の検査が当たる。
  触れるなら「`Context` の `elicit` メソッド」のように名前を分割する。
- 全ての関数に docstring を付ける。

### 5. 検証する（省略不可）

`mcp` が入った interpreter の絶対パス（以下 `$PY`）で実行する。

```bash
"$PY" /abs/path/to/dest/smoke_check.py
```

exit code 0 を確認するまで完了報告しない。`FAIL` の文面には直し方が書いてある。
旧仕様の名前が当たったときは、その意味を `references/spec-2026-07-28.md` の表で確かめる。
`WARN` は文面に従う（走査できなかったファイルは、そのファイルに旧仕様の名前が無いかを確かめる。版の不一致なら、
スケルトンを信じず先に SDK の changelog を読み直す）。

### 6. 登録と評価の手順を渡す

`README.md` のクライアント設定 JSON を生成先の絶対パスで埋めて提示する。
`command` は `mcp` が入った `python3` の絶対パス。問いのカタログを作ったなら、
README の「エージェントでの評価」の手順と合わせて渡す（実際のホストとモデルで解かせ、
誤りを説明・既定値・ツールの境界に帰着させて直す）。

## MRTR（ツール実行中の確認・追加入力）

スケルトンには入れていない。要るときは `references/sdk-api-evidence.md` の MRTR 節の形で足し、
同節の `smoke_config.py` の設定（承認と拒否の両分岐）を書く。

## スケルトンの範囲外

HTTP の OAuth と、複数インスタンスでの運用。スケルトンの HTTP は認証を持たないので、
ループバック（127.0.0.1 / ::1 / localhost）でだけ待ち受け、それ以外の `--host` は起動時に拒否する。
満たすべき要件と進め方は `references/remote-auth.md`。stdio は認可仕様の対象外で、
資格情報は環境変数から取る。

## 詰まったときに見るところ

- `server/discover` / `resultType` / `_meta.serverInfo` / `inputSchema` /
  `outputSchema` / `structuredContent` / 引数検証は SDK が自動でやる（`guard.py` が補正する
  3 点、未知の引数の拒否・公開する inputSchema の整形・structuredContent の写しの text を除く）。
  これらを自前で実装しようとしていたら、記憶で書いている兆候。
- HTTP で `400 Bad Request: Missing session ID` → `MCP-Protocol-Version` ヘッダの
  付け忘れ。この仕様にセッションは存在しない。真に受けて実装すると廃止済みの
  機構を作り込む。

## 永続メモリ

- 参照: `claq_run claq.mem.cli search "mcp server protocol template"`
  （`. "$HOME/.claq/env.sh"` 前提）→ 本文が要る key だけ
  `claq_run claq.mem.cli show <key>`
- 記録: 再利用可能な学びだけ `claq_mem_learn`。基準は `../learn/SKILL.md`
