# claq リポジトリ全体レビュー(2026-09-12)

## スコープ

`git diff` は空(作業ツリー clean)のため、リポジトリ全体を対象とした。`ple4:reviewer`(品質・設計)と `ple4:security-auditor`(セキュリティ)を対象を分割して並列起動し、結果を統合した。両エージェントとも READ-ONLY(コード変更なし)。

## 検証済みの事前条件

- `python3 -m pytest -q`(リポジトリ直下): 3636 passed
- `ruff check plugins/claq`: All checks passed
- `cd plugins/claq && python3 -m pytest -q --cov`: TOTAL 100.00%(`fail_under=100` 達成)

いずれもレビュー担当エージェントが独立に再実行し、一致を確認済み。

## HIGH(commit ブロック対象)

### H1. bg失敗ログが保護対象外で、次回SessionStartへ無条件注入される(security-auditor)

`plugins/claq/src/claq/hooks/hook_common.py:1707-1743`(`recent_bg_failure_notice`)と `plugins/claq/src/claq/mem/cli.py:1147-1169`(`_bg_failure_section`)。

`~/.claq/logs/bg-YYYY-MM-DD.log` は `hooks/hooks.json` のどの保護フックにも守られていない(`hooks.json` に `.claq` への言及が0件であることを確認済み)。Bash アクセスを持つエージェントが当該ログへ自由文を追記すると、その内容は当日・前日分の2日間、全 SessionStart で `<claq-memory>` ブロック内へ無条件に読み戻される。読み出し経路は `strip_tags(normalize_user_message(...))` のみでタグ偽装は防ぐが、`redact()` は通らない(Python logging 経由の書き込みには `_RedactingFormatter` がかかるが、Bash 直接書き込みは通らない)。自然文による説得的な指示文がそのまま次セッション以降へ persist しうる。

これは `mem.cli learn` が `source=agent`/`status=pending` に固定し `promote`(人間承認)を経なければ注入されないという設計(H-01)を、隣接する別経路が素通りしている状態。

**修正方針**: (1) `~/.claq/logs/` 配下への Bash 経由書き込みを `bash_config_protection` の保護対象に追加、(2) `recent_bg_failure_notice` の戻り値にも `redact()` を通す、(3) 恒久対応として bg ログ内容をブール値化した定型メッセージに限定し自由文をそのまま注入しない設計へ変更。

### H2. プラグインリネームで mem.db が分裂し、蓄積知識が新環境から見えない(reviewer、自分で再検証済み)

`plugins/claq/src/claq/lib/constants.py`(`PLUGIN_NAME = "claq"` → `BASE_DIR_NAME = ".claq"`)のリネームにより永続化先が `~/.ple4/mem.db` → `~/.claq/mem.db` へ変わったが、移行処理・検知器がない。

自分で実測:
```
~/.ple4/mem.db … knowledge: pending 18 / archived 1
~/.claq/mem.db … knowledge: 0 件
```
旧DBには本レビューで参照した既存の pitfall/convention カードを含む蓄積知識が全て残っており、`claq` 側の `search`/`list`/`show` では常に空を返す。

CLAUDE.md の「後方互換フォールバックは実装しない」に沿えば自動フォールバックは不適切。publish 前に対処が必要(severity を HIGH のまま据え置く根拠)。

**対応(2026-09-12 実施済み)**: advisor に相談し、方針を訂正した。新旧の `knowledge` テーブル定義はカラム構成が同一(status の DEFAULT 値のみ異なるが、既存行の値には影響しない)で、これは「恒久コードで解決する問題」ではなく「一度きりの状態遷移」と判断。`mem migrate` サブコマンドの新設は見送り(CLAUDE.md の「後方互換フォールバックは実装しない」に反する forever-artifact になるため)。代わりに `~/.claq/mem.db` をバックアップした上で、`~/.ple4/mem.db` の repo=bluecore-dev の knowledge 21件(pending 20 / archived 1、本レビューで記録した2件を含む)を `ATTACH DATABASE` + `INSERT` で直接移した(`id`/`session_id`/`superseded_by` は新規採番・NULL 化して安全側に倒した。sessions 76件は引き継ぎ履歴でありスコープ外)。claq ランタイム経由で `search`/`show` が正しく機能することを確認済み。旧 `~/.ple4/mem.db` は削除せず残している。

このマシン以外の環境(publish 後に既存ユーザーが更新する場合)でも同型の問題が起きうるが、恒久的な移行コードは追加しない方針を維持する。発生したら本レポートと同じ手順(スキーマ差分確認→`ATTACH`+`INSERT`)で個別対応する。

## MEDIUM

- **denylistベースのタグ無害化**(`mem/tag_stripping.py:83-89,157-172`, `lib/harness.py:460-475`): 既知タグ名の列挙に基づく除去/エスケープであり、位置・構造による判定ではない。唯一の補償策 `ci/scan_scaffold_drift.py` は同ファイル内で「CIへは組み込まない」と明言されており、人間の手動診断に留まる。新しいホストが未知の足場タグを追加すると、列挙に反映されるまで自動検知手段が無い。実害は限定的(過去の実測漏れ2件、誤検知の方が多い)だが構造的な陳腐化耐性はない。**修正方針**: `scan_scaffold_drift.py` を `maintain` や release-verify のフローへ定期実行として明示的に組み込む。
- **大規模ファイル**: `hook_common.py`(1834行)、`mem/cli.py`(1384行)、`hooks/pre_bash_commit_quality.py`(1143行)、`hooks/bash_config_protection.py`(893行)。CLAUDE.md/ADRに行数上限の明文規定はなく規約違反ではないが、`hook_common.py` は責務(コメント除去・トークン化・stdin読取・引用符処理)ごとの分割余地あり。関数単位では50行超は無し。**裁定: 見送り**。規約違反ではなく、分割は独立した保護ロジックを串刺しにする横断的リファクタになり、今回のレビュー起因の修正としてはスコープが大きすぎる。着手するなら専用の `/refactor` で扱う。

## LOW

- `runtime/claq-hook.cmd:90-98`: `CLAQ_PYTHON` 分岐が PATH 空要素チェックより先に評価され、絶対パス指定時も launcher が起動するサブプロセス(git等)は汚染PATHを経由する。**裁定: NO-FIX(対応不要、追加変更なし)**。ファイル自身のコメントが「Reordering here is the right change, but cmd.exe cannot be run from the development host, and a broken reorder silently disables protection on Windows. Left as a documented residual rather than an unverified edit.」と明記しており、既にこのリポジトリの意思決定として文書化済み。darwin環境からは実行検証できないため、今回もこの裁定を維持する(誤った並び替えを未検証のまま適用する方が、現状維持より悪い)。
- `hooks/block_no_verify.py:322`、`hooks/pre_bash_commit_quality.py:696`、`lib/frontmatter.py:284`、`ci/scan_scaffold_drift.py:147`: if/for/whileネストが4〜5段でチェックリスト基準(3階層)を超過。git引数解析という性質上複雑さは本質的で、既にヘルパー抽出と根拠コメントあり。ブロッキング対象ではない。

## 誤り・訂正

- reviewer側の報告で「`docs/plan/` が存在しない」とあったが、実際には存在する(`.keep` のみの空ディレクトリ)。サブエージェントの作業ディレクトリが `plugins/claq` に変わっていたことによる相対パス誤認と判断。

## INFO(意図的設計・誤検知気味 — 対応不要)

- `plugin.json` に `agents` キーが無い件は欠陥ではない。`tests/test_plugin_manifest.py` が回帰テストとして固定しており、Claude Code 2.1.220〜2.1.247 のホスト loader が `agents: ["./agents/"]` を書くと `scandir` が `ENOTDIR` になり `Agents (0)` に退行する実測に基づく意図的な回避。
- 既存 mem 知識カード3件(定義ファイルの外部標準陳腐化/ADR複数実装先の実在確認/harness_auditの実効性)はいずれも再発なし。ADR参照・機械照合テスト・二層防御が機能していることを確認。
- `~/.claq` 0700 / `mem.db` 0600 の強制、SQLインジェクション・コマンドインジェクション・パストラバーサル・ハードコード認証情報は該当なし(プレースホルダ使用、`shell=True` 皆無、`resolve()+is_relative_to` チェック、symlink拒否を確認済み)。
- `pyproject.toml` の ruff `select` にセキュリティ系ルール(`S`)が無いのは、セキュリティレビューを `security-auditor` エージェントへ明示的に分離する設計判断であり意図的。

## 環境上の注意(コードの欠陥ではない)

このレビューセッション自体が `~/.claude/plugins/cache/ple4/ple4/0.9.56/`(リネーム前の旧キャッシュ)を読み込んでいる。そのため:
- セッションのSkill一覧に `quick-debug`/`quick-refactor` が出現しない(旧キャッシュにディレクトリ自体が存在しないため)。現行 `skills/` を `ci/validate_skills.py` で検証すると21件全て合格し、frontmatterも他の quick-* skillとバイト単位で同一。**リポジトリ側に区別要因はない。**
- H2で見つかった mem.db 分裂も、このキャッシュ経由での `. "$HOME/.ple4/env.sh"` 呼び出しが実態を覆い隠していた一因。

プラグインの再インストール/リロードで解消される可能性が高いが、現行ドキュメント(commands/review.md 等)が「過去の判断を自分で引く」 workflow を前提にしている以上、**インストール直後に一度、旧環境からの持ち越しがないかを確認する運用**が要る。

## アジャイル開発の観点で不足しているスキル

現在のインベントリ(21 skills / 9 commands / 9 agents)を開発ライフサイクルにマッピングすると、計画(plan/planner/grillme/adr)・実装(feat-dev/bugfix/refactor/loop-dev/tdd-writer/code-refiner)・テスト(test-gen/quick-test/tdd-writer)・レビュー(review/reviewer/security-auditor/secure)・リリース検証(release-verify/harness/maintain)・知識蓄積(learn/instinct/loop-audit)は手厚く揃っている一方、次の3点が抜けている。

1. **継続的インテグレーション(CI)がリポジトリに存在しない**。`.github/` 自体が無く、push/PR契機で `pytest`/`ruff`/カバレッジを自動実行する仕組みがゼロ。現状は全て手動実行前提(このレビューも手動で実行した)。アジャイル開発の中核である「変更のたびに自動検証する」が仕組み化されていない。単発の設定追加で済むため、優先度は高いが対象外(仕様変更)として `/plan` 行きにする。
2. **リリースノート/CHANGELOG生成の欠如**。`scripts/version-up.sh` はバージョン番号を上げるが、変更内容を要約したCHANGELOGは存在しない。スプリント/リリース単位で「何が変わったか」を追う手段がなく、`loop-audit` も収束品質の集計であってリリース単位の変更履歴ではない。
3. **チーム向けふりかえり(レトロスペクティブ)の不在**。`loop-audit` はAIの反復ループ(plan→generate→evaluate)の収束品質を測るものであり、人間チームが「今回のスプリントで何が良かった/悪かったか」を振り返る形のスキルではない。単独開発者利用が前提なら優先度は低いが、チーム運用へ拡張する場合は穴になる。

いずれも「バグ」ではなく設計判断を要する範囲のため、後処理では自律実装せず `/plan` 相当の提示に留める。

## 学びの記録

以下2件を新規記録(重複なしを `--status pending` と既定検索の両方で確認済み):

- `plugin-rename-must-migrate-derived-paths`(pitfall): プラグイン名リネーム時、`~/.${PLUGIN_NAME}` 等の派生パスに永続データがあると、移行経路が無い限り新環境から蓄積データが見えなくなる。
- `protection-enum-must-track-new-runtime-writes`(pitfall): 保護フックの対象列挙は設定ファイル中心になりがちで、後から追加されたランタイム生成物(ログ等)がプロンプト注入経路になり得る場合、その追従漏れが構造的リスクとして残る。

## 後処理方針(ステップ4)と最終結果

リポジトリ全体スコープでの無条件委譲は影響範囲が大きすぎるため、通常の `/review`(diffスコープ)向けの「分類提示後、応答を待たず自律委譲」は行わず、進捗ごとにユーザー確認を挟みながら1件ずつ収束させた。

| # | 指摘 | 分類 | 対応結果 |
|---|---|---|---|
| MEDIUM | scan_scaffold_drift.py のCI未組込 | リファクタ | **対応済み・コミット済み**(6a7057a, 60af74c)。`release-verify` へ明示呼び出しを追加。loop-dev の evaluate で検出した見出し破損(構造退行)も併せて修正 |
| H1 | bg失敗ログの保護漏れ(プロンプト注入経路) | バグ | **対応済み・コミット済み**(0965bdc)。`~/.claq/logs/` へのBash直接書込みを保護対象に追加、`redact()` を注入経路へ追加。reviewer/security-auditorのWARNING(docstringと実装の乖離、tilde展開反転の未テスト、古いdocstring)も解消 |
| H2 | mem.db分裂(リネーム移行漏れ) | バグ | **データ移行を実施済み**(コード変更なし)。advisor相談の結果、恒久的な移行コマンド新設は「一度きりの状態遷移に永続コードを持たせる」ことになり CLAUDE.md の後方互換フォールバック禁止に反すると判断し見送り。ユーザー承認のうえ `~/.ple4/mem.db` → `~/.claq/mem.db` へ knowledge 21件を直接移行、claqランタイムでの読み出しを確認済み |
| LOW | claq-hook.cmd の PATH順序 | リファクタ | **NO-FIX(対応不要)**。ファイル自身が既に「未検証の並び替えは現状維持より危険」として文書化済みの意思決定であり、追加変更なし |
| MEDIUM | hook_common.py 等の分割 | リファクタ | **見送り**。規約違反ではなく、スコープが今回のレビュー対応の範囲を超える |
| CI/CHANGELOG/レトロ不在 | 仕様変更 | `/plan` 提示に留め、自律実装しない(対象外のまま) |

再帰レビューは指示通り実施しない。
