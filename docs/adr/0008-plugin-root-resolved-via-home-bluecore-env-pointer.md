# ADR-0008: plugin root は `~/.bluecore/env.sh` ポインタで解決し、md にベンダ固有パスを書かない

**日付**: 2026-08-20  **ステータス**: accepted

## コンテキスト

`docs/reports/PLUGIN_RUNTIME_AUDIT_2026-08-19_0.9.34_VERIFICATION.md` の
M-02 は、`bluecore_run`/`bluecore_mem_learn`（`runtime/bluecore-helpers.sh`
の shell 関数）を参照する agents/commands/skills の md のうち、bootstrap
（`. runtime/bluecore-helpers.sh` を実行するコード）を持たない 16 surface
が、実行すると `bluecore_run: command not found`（exit 127）になることを
指摘した。加えて本作業のユーザー要件として、`.claude`/`.copilot`/`.grok`
等のベンダ固有インストールパスを SKILL.md 等の md から排除し、
host-dependent なロジックは Python/Shell の実装ファイルへ隔離することが
求められた。

事前調査で次を実測した:

- **Bash tool の環境変数に `CLAUDE_PLUGIN_ROOT` は乗らない。** Claude Code
  自身のセッションで `echo "PLUGIN_ROOT=[${CLAUDE_PLUGIN_ROOT:-UNSET}]"` を
  実行すると `UNSET` だった。`hooks.json` 内の `${CLAUDE_PLUGIN_ROOT}` は
  ホストが hook 起動コマンド文字列を展開する際にのみ使われ、Bash tool の
  子プロセス環境には伝播しない。これが M-02（md 側で候補探索ループが
  必要になっていた根本原因）である。
- **ホストが自身の識別用環境変数を bash tool へ注入するとは限らない。**
  Web 調査の結果、Copilot CLI・Grok CLI とも自身の識別変数（`COPILOT_*`・
  `GROK_*`）を bash tool の子プロセス環境へ注入することは文書化されて
  いない（見つかるのは利用者が設定する認証・設定用変数のみ。
  `COPILOT_AGENT_SESSION_ID` はクラウド側 coding agent 用でローカル CLI の
  shell tool env ではない）。実測で識別変数を確認できたのは Claude Code
  のみ（`CLAUDECODE=1` 等）。したがってベンダ名を判定条件に使う設計は
  Copilot/Grok では機能しない可能性が高い。
- **Bash tool の親プロセスは常にホストのバイナリそのもの。** `ps -o comm=
  -p $PPID` は `claude` を返した。hook（launcher）も Bash tool も同じ
  ホストプロセスの子であるため、Python 側の `os.getppid()` と shell 側の
  `$PPID` は同一ホストプロセスを指す。

## 決定

**plugin root の解決を `~/.bluecore/env.sh`（launcher が全 hook 起動時に
書き出す固定住所）へ一元化し、md にはベンダ名を一切書かない。ホストの
判別は環境変数ではなく `PPID` を鍵にする。**

構成:

1. `plugins/bluecore/runtime/env-template.sh`（新規、実 shell file）が
   3 段の解決ロジックを持つ:
   1. `PATH` 上の各エントリの親に `runtime/bluecore-helpers.sh` があれば
      それを採用（ホストが plugin の `bin/` を PATH に載せる場合。最も
      正確でベンダ非依存）。
   2. `~/.bluecore/roots/$PPID.sh` を読む（ホストプロセス単位で分離した
      ポインタ）。
   3. `~/.bluecore/roots/latest.sh`（最後に書かれた root。PPID が一致しない
      場合の最終手段）。
   4. いずれも解決できなければ明示メッセージを stderr に出し `exit 127`。
2. `plugins/bluecore/src/bluecore/lib/env_pointer.py`（新規）が
   `write_env_pointer(plugin_root)` を提供し、`~/.bluecore/roots/<ppid>.sh`
   と `~/.bluecore/roots/latest.sh` を書いた上で `env-template.sh` を
   そのまま `~/.bluecore/env.sh` へ複写する。
3. `launcher.py:main()` が全 hook 起動のたびに `write_env_pointer()` を
   呼ぶ（SessionStart に限定しない。他の hook 起動でもポインタが更新される
   ため復旧が速い）。書き込み失敗は握り潰し、hook 本来の処理を妨げない。
4. agents/commands/skills の md は
   `. "$HOME/.bluecore/env.sh" || exit 127` の 1 行だけを書く。ベンダ固有
   パスの候補探索ループ（11 箇所）は全廃した。

## 検討した代替案

### 代替案 1: 単一ファイル `~/.bluecore/env.sh` に root を直接埋め込む

- 長所: 実装が単純（ポインタ 1 本）。
- 短所: 別ターミナルで Claude Code と Copilot を並行起動すると、後に
  SessionStart が走ったほうの root が先のセッションのポインタを上書きし、
  先に開いていたセッションが別ホストの install を source してしまう。
- 却下理由: 複数ホストの同時利用は珍しくない運用形態であり、この衝突は
  容易に再現する。ホスト単位で分離できる設計（本決定）のコストは低い。

### 代替案 2: ホストの識別用環境変数（`COPILOT_*`/`GROK_*` 等）でポインタを
分岐する

- 長所: プロセスの親子関係より意味的に分かりやすい。
- 短所: Web 調査の結果、Copilot CLI・Grok CLI が bash tool の環境へ自身の
  識別変数を注入する保証がない（コンテキスト参照）。判定表を書いても
  Claude Code 以外は当て推量になり、外れた場合は代替案 1 と同じ衝突が
  再発する。
- 却下理由: 実測・文書で裏付けが取れない前提に設計を依存させるべきではない。
  PPID は実測で確認できた確実な信号であり、かつベンダ名を一切要求しない。

### 代替案 3: ランタイム実装（Python/Shell）を `~/.bluecore` へ集約インストールし、
プラグインディレクトリには agents/skills/commands/hooks だけを配置する

- 長所: 実装がホスト間で完全に共有され、md からベンダパスを排除する目的は
  達成できる。
- 短所:
  - 新しいインストール機構（`curl | bash` の配布、または SessionStart
    による自己複製）が必要になり、`runtime-no-venv`（ランタイムは venv も
    install.sh も持たない）という既存の設計原則と衝突する。
  - プラグインのバージョンとランタイムのバージョンが分離し、
    「md（プラグイン側）はバージョン更新されたがランタイム（`~/.bluecore`
    側）は古いまま」という契約不整合が新たに発生しうる。現行構成
    （各ホストが自己完結ツリーを持つ）ではこの種の不整合は起こらない。
  - SessionStart による自動複製は、他ホストの実行中セッションが使っている
    実装を書き換える競合を生む。
- 却下理由: 今回の要件（md からベンダパス排除、M-02 解消）に対して
  コスト・リスクが不釣り合いに大きい。将来的にランタイムを正式パッケージ
  化する（`uv tool install` 等）なら別の意思決定として扱うべきで、
  手製の集約インストールでは採用しない。

## 結果

### 肯定的

- agents/commands/skills の md からベンダ固有パス
  （`.copilot`/`.grok`/`installed-plugins`/`CLAUDE_PLUGIN_ROOT`）が
  全廃された（静的テストで継続検証: `test_md_surfaces_have_no_vendor_specific_paths`）。
- M-02 の 16 surface（bootstrap 欠如）と 11 箇所の重複ループが、
  1 行の共通 bootstrap （`. "$HOME/.bluecore/env.sh" || exit 127`）に
  統一された。
- ホストごとの分離が PPID 単位で行われるため、複数ホストの同時利用でも
  衝突しない（PATH 由来解決が効くホストではさらに正確）。

### 否定的

- `~/.bluecore/roots/` に PID ベースの小さなファイルが蓄積する。
  `write_env_pointer()` は 7 日超のエントリを書き込みのたびに刈るため、
  無制限には増えない。

### リスク

- ホストが hook を中間 shell 経由で起動し、かつ Bash tool 側の実際の親が
  その中間 shell になる場合、`$PPID` が launcher 実行時の `os.getppid()`
  と一致しない可能性がある。この場合は PATH 由来解決（層 1）か
  `latest.sh`（層 3）にフォールバックする。
- 層 1（PATH 由来）が効かず、かつ層 2（PPID 一致）も外れ、かつ異なる
  バージョンの複数ホストが同時稼働している場合、`latest.sh` 経由で
  意図しないホストの install を source する残余リスクがある。この
  組み合わせ（PATH 未対応 × PPID 不一致 × 複数バージョン同時稼働）は
  限定的だが、ゼロではない。
- Copilot CLI での実機動作（`~/.bluecore/roots/` が実際に生成され、
  md の 1 行 bootstrap が動くか）は、Copilot の SessionStart hook 発火
  自体は `~/.copilot/session-state/<id>/events.jsonl` で確認済みだが、
  本ラウンドの変更適用後の実機再検証は本 ADR の範囲に含めていない
  （ADR-0005 に従い手動チェックリストで別途実施する）。
- `resolve_effective_target`（`config_protection.py`/`bash_config_protection.py`
  が使う symlink 解決 helper）は hook payload の `cwd` ではなく
  `Path.cwd()`（launcher プロセス自身の working directory）基準で解決する。
  これは `resolve_repo_root()` 等、既存の repo-root 解決ロジックがもともと
  前提にしていた挙動を踏襲したものであり、本ラウンドで新たに導入した
  前提ではない。hook payload が別 cwd を運んでくるケースが将来出てきた
  場合は、この前提が暗黙のまま残っていることに注意する必要がある。
