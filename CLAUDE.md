# CLAUDE.md

## コーディングルール

- `Python` コードに `docstring` を付ける
- 後方互換フォールバックは実装しない（古いコードは必ず削除）
- カバレッジ `100%`
- テーブル定義変更は `CREATE TABLE` を直接修正（リリース前のためマイグレーション不要）

## 作業ルール

- Python は `python3` を使う
- 変更後はリポジトリ直下の `.venv` を有効化して `python3 -m pytest -q` と `ruff check plugins/ple4`（src と tests の両方。tests を外すと未定義名や不要 import が無検出のまま残る） が成功することを確認（警告なし）。カバレッジ100%の確認は `cd plugins/ple4 && python3 -m pytest -q --cov` で行う（`pyproject.toml` の `fail_under=100` はこのディレクトリでのみ解決する。リポジトリ直下には coverage 設定が無く `--cov` を付けてもゲートが発火しないため注意）
- ランタイムは venv を作らない
- 開発用 venv は `ple4-dev/.venv` のみで、`scripts/install-dev.sh` が作成する
- 新規 hook・外部呼び出し（DB/ネットワーク/DL）は非ブロッキング + ハードタイムアウト必須
- pytest をパイプする際は `set -o pipefail` 必須
- 開発中コードの CLI/モジュール実行に `PYTHONPATH=plugins/ple4/src` は不要 — editable install が venv の `ple4` をこのリポジトリの `plugins/ple4/src` へ向ける。`.venv/lib/*/site-packages/_editable_impl_ple4.pth` がプラグインキャッシュを指していたら `install-dev.sh` を再実行して直す

## ランタイム前提

- 対応 `python3` は 3.12 以上。`launcher.py` は 3.12 未満を検出すると保護フック（`block_no_verify` / `pre_bash_commit_quality` / `config_protection`）を stderr へ警告した上で **exit 0（fail-open）** にする。これは意図的な設計判断（`launcher.py:52-58` に理由を明記）— ランタイムは venv も install.sh も持たないため（[[runtime-no-venv]]）インストール時に対応 Python を検証する経路が無く、fail-closed（exit 2）にすると Python を直す手段（Bash）ごとセッション内から塞がれ復旧不能になる。stderr の `ple4ProtectionDisabled` 警告を監視しない環境では保護無効化を見落とすため、対応 Python の確保は利用者側の責務とする
- `hooks.json` は裸の `python3` を呼ばない。全エントリが `runtime/ple4-hook`（`command` 経由の Windows は cmd.exe が PATHEXT で解決する同名の `.cmd`。PowerShell ホスト向けの明示宣言は次項）を起動し、インタプリタ解決はこの wrapper の単一責務とする。`PLE4_PYTHON` > `python3` > `python`（POSIX）／`PLE4_PYTHON` > `py -3` > `python` > `python3`（Windows。`WindowsApps` 配下の Microsoft Store alias は候補から除外）。見つからない場合は `launcher.py` と同じく stderr へ `ple4ProtectionDisabled` を出して exit 0。同じ形の無効化が他に 3 つある。`PATH` に絶対パス要素が 1 つも無ければ `path_not_absolute`（POSIX）、`PATH` に空要素があれば `path_has_empty_entry`（Windows。cmd.exe は空要素をカレントディレクトリと解釈するため乗っ取り口になる）、`PLE4_PYTHON` が相対パスなら `ple4_python_not_absolute`（両 OS。PATH 探索へフォールバックせず無効化する — 明示した override が黙って別のインタプリタに化けないため）。**POSIX と Windows で検査順が違う**: POSIX は PATH 検査を先に行うため絶対パスの `PLE4_PYTHON` を設定しても `path_not_absolute` から回復しないが、Windows は `PLE4_PYTHON` の分岐が先なので回復できる（差の根拠は `docs/adr/01-hook-failure-direction.md`）。launcher 自体が見つからない場合も同様に `reason: "launcher_not_found"` を出して exit 0 する（インタプリタ選択より前に検査する。`python3 <不在ファイル>` は exit 2 を返し、PreToolUse の「exit 2 = deny」契約により不完全インストールが全ツール呼び出しの偽 deny に化けるため）。`runtime/ple4-helpers.sh` の `ple4_run` も同じ wrapper を通す
- `hooks.json` は **versioned マニフェスト**。top-level に `"version": 1` が必須で、`ci/validate_hooks.py` の `REQUIRED_HOOKS_VERSION` と int で厳密一致しなければ exit 1（`True` は `1 == True` で通り抜けるため bool を先に弾く）。イベント辞書は必ず `hooks` キーの下に置く — ラッパー無しの裸のイベント辞書は受理しない（top-level の `version` がイベント名として検査されてしまう。JS バリデータ互換の `data.hooks || data` フォールバックは削除済み）
- 各 command エントリは POSIX 用 `command` と PowerShell 用 `powershell` の**両方**を宣言する。**Windows には解決経路が 2 本あり、どちらも生きている**: cmd.exe ホストは `command` の拡張子なしパスを PATHEXT で `.cmd` へ解決する（前項）。一方 PowerShell を優先するホスト（Copilot CLI）はその文字列を PowerShell へ渡すため、`powershell` が `& "${CLAUDE_PLUGIN_ROOT}/runtime/ple4-hook.cmd" <module> <args...>` の形で `.cmd` を**明示的に**名指す。先頭の `&` は call 演算子で、これが無いと PowerShell は引用符付きパスを文字列リテラルとして評価し、何も実行せず成功扱いになる。両フィールドは**同一モジュール・同一引数**（`--bg` を含む）でなければならず、`tests/ci/test_validate_hooks.py::TestRepoHooksJsonPowerShellParity` が機械的に照合する（片側だけ変えると Windows でだけ別のフックが走る）。`powershell` は command フックでのみ許され、http / prompt フックに付けると `async` と同様に拒否される

- 対応 OS は macOS / Linux / Windows。ただし `~/.ple4/env.sh`（plugin root resolver）は POSIX 専用 — 祖先 PID と `ps -o lstart=` の照合に依存し、Windows には同等の手段が無い（Git Bash の `ps` は `-o` 書式を持たず MSYS の別 PID 空間を返す）。Windows では SessionStart の `mem context` が代わりに `runtime/ple4-hook.cmd` の絶対パスを 1 行注入するので、md の `ple4_run <module> ...` はそれへ読み替える（`mem/cli.py:_shell_bootstrap_section`）
- `~/.ple4` の 0700 / `mem.db` の 0600 は Windows では DACL を変えない。`%USERPROFILE%` 配下が既定でユーザー限定であることに委ね、`icacls` 等は呼ばない（`core_utils.ensure_private_dir` に理由を明記）

## データモデルの前提

- 永続化は `~/.ple4/mem.db`（SQLite）単独。チーム共有ストア（旧 PostgreSQL 同期）は全廃済み — 各メンバーのメモリは自身の SQLite に閉じる
- テーブルは `repos` / `knowledge` / `sessions` の 3 つだけ（`mem/schema.py`）。リポジトリ識別は `repos.id`（人間可読スラッグ）の 1 系統で、別台帳ファイルは持たない
- 記憶の単位は **知識カード**（`knowledge` の 1 行）。`kind` は `convention` / `decision` / `pitfall` / `howto` / `fact` / `preference` の 6 値、`scope` は `global` / `repo`。SessionStart に注入されるのは `status='active'` のみで、`pending` は人間が `/instinct promote` で昇格させるまで注入されない。`ple4_mem_learn` / `mem learn` から入るカードは常に `source=agent` / `status=pending` で登録される（H-01）。`learn` に `--status` を渡すと拒否され、JSON も既定と異なる `source`/`status` を書くと usage error になる（既定値そのものを明示する分には通る）。`--status` 自体は `list` / `search` の絞り込みには使える。したがって agent 由来カードは例外なく `/instinct promote` を経なければ注入されない
- 検索は埋め込みベクトルも FTS5 も使わない。知識カードは数百件オーダーに収まるため、全件をロードして Python 側でスコアリングする（`mem/cli.py`）。静的埋め込みテーブル・`ple4.model_build`・numpy・sqlite-vec はいずれも全廃済み
- ランタイム依存はゼロ（標準ライブラリのみ）。frontmatter 解析は `lib/frontmatter.py` の自前パーサで、依存ゼロは `tests/lib/test_runtime_dependencies.py` が機械的に保証する
- 出力トークンの最小化が設計原則。`list` / `search` は `- [kind] title (key)` の 1 行だけを返し、`body` を返すのは `mem show <key>` だけ。0 件なら 1 文字も出力しない

