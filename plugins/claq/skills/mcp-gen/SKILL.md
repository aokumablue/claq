---
name: mcp-gen
description: Python の MCP サーバを新規構築する。「MCP サーバを作って」「社内 API を MCP 化して」「ツールを MCP で公開して」等で発火。Python 以外の言語の MCP サーバは対象外。既存 MCP サーバの単発バグ修正は /bugfix、脆弱性レビューは /secure。
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
Bash 経由の書き込みは `bash_config_protection` に、Write/Edit は `config_protection` に検査される。生成先が claq リポジトリ外なら通るが、`ignore` / `select` / `exclude` / `addopts` / `fail_under` や lint セクション見出しを含む内容は経路によらず deny される。

### 3. ツールを設計する

`references/tool-design.md` を読み、ツールの一覧（名前・類型・引数・出力・注釈）を決める。
上流の API を包むなら `references/facade.md` も読み、正規ドメインモデルと項目カタログを決める。
問いのカタログの全ての問いに、答えるツール呼び出しの列があること。

### 4. 編集する

差し替える: `SERVER_NAME` / `SERVER_VERSION` / `instructions` / `cache_hints`、
`pyproject.toml` の `name` と `[project.scripts]` のコマンド名（`SERVER_NAME` にそろえる）、
見本のツール・リソース・プロンプト（不要なら削除）。重ね合わせたなら `CATALOG`・
出力モデル・`adapters.py` の偽物（実際の上流の呼び出しに置き換える）も。

触らない: `install_guard(mcp)` と `run(mcp)` の呼び出し、`guard.py`、`smoke_check.py`、
`smoke_static.py`、`facade/` の部品。`transport.py` は公開するホスト名を足すとき以外は触らない。
stdio だけの要件でも `--http` は残す。

ツールの書き方（説明・引数・出力・失敗・注釈・状態）は `references/tool-design.md` に従う。
検査の対象は `smoke_config.py` に書く。値の選び方はファイル内のコメントに従い、サンプルの
ままにしない（検証が実サーバを見なくなる）。

ツール以外で守ること:

- 失敗はそのまま呼び出し元へ伝える。握り潰し・既定値で続行・別経路への切替（フォールバック）・リトライは要件にあるときだけ置く
- 入力の検証は外部入力の入口で 1 回だけ行う。内側では型と前提を信頼し、「念のため」の None/型チェックや try/except を置かない
- 防御は理由（要件・攻撃者と入口・実在する失敗経路。エージェント自身の誤操作も失敗経路に含む）を 1 行で言えるときだけ置く
- 修正は根本原因で行う。症状を覆う特例の分岐や例外捕捉で直さない
- 抽象・設定項目・汎用化は、今ある呼び出し元が求めるまで作らない。コメントは「なぜ」だけを書く
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

`mcp` が入った interpreter の絶対パス（以下 `$PY`）で実行する。`smoke_check.py` は
サーバを `sys.executable` で起動するため、裸の `python3` で呼ぶとサーバ側だけが
`ModuleNotFoundError` で死ぬ。

```bash
"$PY" smoke_check.py
```

exit code 0 を確認するまで完了報告しない。`FAIL` の文面には直し方が書いてある。
旧仕様の名前が当たったときは、その意味を `references/spec-2026-07-28.md` の表で確かめる。
`WARN` が出たら SDK が新しいプロトコル版へ進んでいる。スケルトンを信じず、先に SDK の
changelog を読み直す。

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


class Confirm(BaseModel):
    """削除の確認。"""

    approve: bool


class DeleteResult(BaseModel):
    """削除の結果。"""

    deleted: bool = Field(description="削除したか")
    message: str = Field(description="結果の説明")


def ask_confirm(item_id: str) -> Elicit[Confirm]:
    """削除の確認を求める。リゾルバはツール引数を名前で受け取れる。"""
    return Elicit(f"{item_id} を削除してよいですか", Confirm)


@mcp.tool(
    description="項目を削除する。実行前に利用者の確認を取る。",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False),
)
async def items_delete(
    item_id: Annotated[str, Field(min_length=1, description="項目 ID")],
    confirm: Annotated[ElicitationResult[Confirm], Resolve(ask_confirm)],
) -> DeleteResult:
    """確認を取ってから削除する。"""
    if confirm.action != "accept":
        return DeleteResult(deleted=False, message=f"中止しました（{confirm.action}）")
    if not confirm.data.approve:
        return DeleteResult(deleted=False, message="中止しました")
    return DeleteResult(deleted=True, message="削除しました")
```

- 確認・同意では `ElicitationResult[T]` で受ける。素の `T` で受けると拒否が
  ツール実行エラーになり、「中止しました」を正常な戻り値で返せない。
  素の `T` が適切なのは「拒否＝呼び出しの失敗」でよい場合だけ。
- `smoke_config.py` に次を足し、承認と拒否の両分岐を検査する（1 往復目だけでは、拒否が
  エラーになる実装の誤りを見つけられない）。`CLIENT_CAPABILITIES` が無いと `-32021` で落ちる。
  書き込みのツールを足したので `READ_ONLY_SERVER = False` にする。

  ```python
  CLIENT_CAPABILITIES: dict = {"elicitation": {"form": {}}}
  MRTR_CASES: list = [
      {"tool": "items_delete", "arguments": {"item_id": "item-1"}, "action": "accept",
       "content": {"approve": True}, "expected_is_error": False, "expected_text": "削除しました"},
      {"tool": "items_delete", "arguments": {"item_id": "item-1"}, "action": "decline",
       "expected_is_error": False, "expected_text": "中止しました"},
  ]
  ```

- 承認分岐が事前状態に依存するなら、ケースに `"setup": [(ツール名, 引数), ...]` を足して
  同一プロセスへ先に送る（空のストアへの削除は「対象が無い」エラーになり、配線の正否を
  判別できない）。

詳細と実測ワイヤ、`requestState` の完全性保護要件は `references/sdk-api-evidence.md`。

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

## 参照

- `references/tool-design.md` — ツールの類型・数・名前・説明・引数・出力・失敗・注釈・状態の規則
- `references/facade.md` — 上流 API ファサードの重ね合わせ（正規化・段階開示・予算・カーソル・合成・上流の呼び出し）
- `references/remote-auth.md` — リモート配置の認可、上流の資格情報、データ層の認可
- `references/spec-2026-07-28.md` — 削除・追加・非推奨の一覧、`_meta` 予約キー、
  セキュリティ必須事項、smoke が検出する旧仕様の名前の意味
- `references/sdk-api-evidence.md` — `mcp` 2.x の実測 API と実測ワイヤ JSON

## 永続メモリ

- 参照: `claq_run claq.mem.cli search "mcp server protocol template"`
  （`. "$HOME/.claq/env.sh"` 前提）→ 本文が要る key だけ
  `claq_run claq.mem.cli show <key>`
- 記録: 再利用可能な学びだけ `claq_mem_learn`。基準は `../learn/SKILL.md`
