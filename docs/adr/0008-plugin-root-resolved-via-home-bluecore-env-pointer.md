# ADR-0008: plugin root は `~/.bluecore/env.sh` ポインタで解決し、md にベンダ固有パスを書かない

**日付**: 2026-08-20（初版）／2026-08-20 改訂  **ステータス**: accepted

## 改訂履歴（2026-08-20）

初版（M-02 対応）で導入した 3 段解決（PATH → `roots/$PPID.sh` → `roots/latest.sh`）は、
Copilot CLI 実機検証 `docs/reports/PLUGIN_ROOT_RESOLVER_2026-08-20_VERIFICATION.md`
（R-01〜R-06）で BLOCK 判定を受けた。root cause は「どの plugin runtime を実行するか
決める前に roots pointer を shell として `.`/source していたこと」（R-01/R-03）。
「決定」節と「結果」節を新設計に合わせて全面差し替えする。代替案の記録（環境変数
判定を採らない理由・ランタイム集約installを採らない理由）は初版のまま変更なし。

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
判別は環境変数ではなく `PPID` を鍵にする。roots pointer は shell コード
ではなく単なるデータ（絶対パス 1 行）として扱う。**

構成:

1. `plugins/bluecore/runtime/env-template.sh`（実 shell file）が次の解決
   ロジックを持つ:
   1. `~/.bluecore/roots/$PPID` を読む。無ければ `ps -o ppid=` で祖先
      プロセスを最大 4 段まで辿り、それぞれの `roots/<pid>` を試す
      （ホストが hook 起動を中間 shell 越しに行う場合を吸収）。
      いずれも `.`/`source` はせず、`IFS= read -r` で 1 行だけ取り出す
      （R-01/R-03: ポインタの内容が shell として実行されることは無い）。
   2. 祖先チェーンで解決できず、かつ `roots/` の**有効なポインタが全て
      同じ root 値に一致する**場合はそれを採用する（「候補がちょうど 1 本」
      ではなく「候補が合意している」ことを条件にした。単独ホストが
      launcher 起動のたびに異なる短命 PID を記録し続ける場合でも、
      root 値さえ一致していれば解決できる。この段階では鮮度は問わない
      — 対立が無い限り、どれだけ古いポインタでも単独稼働ホストの正当な
      記録として扱う）。
   3. 手順 2 で実際に**対立**（異なる 2 つ以上の root 値）が見つかった
      場合に限り、**直近 5 分以内に書かれたポインタだけ**に絞って
      手順 2 を再実行する。使われなくなった別ホストの古いポインタが
      GC される最大 1 時間、単独稼働中のホストを対立扱いで巻き込む
      ことを防ぎつつ、真に同時稼働している別ホストとの対立は塞ぐ
      （5 分の鮮度フィルタは `find <pointer> -maxdepth 0 -mmin -5` で
      判定する — `stat` はフォーマット指定が BSD/GNU で非互換
      〔ADR-0009〕だが `-mmin`/`-maxdepth` は両方で使える）。
   4. いずれも解決できなければ明示メッセージを stderr に出し `exit 127`
      （R-05: 「とりあえず最新」を無条件採用する fallback は廃止した）。
2. `plugins/bluecore/src/bluecore/lib/env_pointer.py` が
   `write_env_pointer(plugin_root)` を提供し、`~/.bluecore/roots/<ppid>`
   （絶対パス 1 行のみ、改行/NUL を含む値は書き込み拒否）と `env.sh` を
   `<name>.tmp.<pid>` 経由の `os.replace()` で原子的に書く（R-06）。
   GC は書き込み元プロセス自身（`os.getppid()`）のポインタだけは、他の
   削除条件を満たしても対象から除外する（GC が同じ呼び出しの中で
   「今書いたばかりの記録」を自壊させないため）。
   置き場所は `get_bluecore_dir()`（`BLUECORE_HOME` を見る、テスト隔離用
   ノブ）ではなく **`$HOME` 固定**にした。md の bootstrap 行が
   `$HOME` 固定である以上、writer 側もそれに合わせる契約とし、
   `BLUECORE_HOME` はデータ永続化（`mem.db`・`logs`）側の契約とは
   意図的に切り離した（R-04）。
3. `launcher.py:main()` が全 hook 起動のたびに `write_env_pointer()` を
   呼ぶ（SessionStart に限定しない。他の hook 起動でもポインタが更新される
   ため復旧が速い）。書き込み失敗は握り潰すが、プロセスごと 1 回だけ
   stderr へ JSON 警告を出す（無音の機能低下を防ぐ）。
4. `write_env_pointer()` は 1 時間に 1 回だけ `roots/` の GC を実行する
   （`roots-gc.stamp` の mtime で throttle）。削除条件は「ファイル名が
   数字のみでない（旧形式の残骸を含む）」「PID が既に無く、書き込み直後の
   猶予（1 時間）を過ぎている」「PID は生存しているが mtime が 7 日を
   超えている（稼働中ホストは毎 hook で mtime を更新するため、これは
   PID 再利用と判断できる）」のいずれか。生存 PID かつ TTL 内のポインタは
   誤って削除しない。
5. agents/commands/skills の md は
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
- roots pointer が「実行されるコード」から「読まれるデータ」へ変わり、
  R-01（任意 shell 実行）・R-03（`$`/backtick/空白を含む root で構文が
  壊れる、または `$()` が実行される）が構造的に解消した
  （`TestEnvShRealExecution` の実行検証で確認済み）。
- PATH 上にたまたま存在する別バージョン／別インストールを権威あるポインタ
  より優先してしまう経路（R-02）を廃した。
- `latest` の無条件採用（R-05）を廃し、「祖先も一致せず候補が対立し、
  直近 5 分に絞っても対立が解けないときだけ 127」に変えたことで、
  複数ホスト同時稼働時に別ホストの install を誤って source する経路を
  塞いだ。単独ホスト運用（最も一般的な形態）は「有効な候補が全て同じ
  root 値」規則で、鮮度を問わず常に解決する。この規則は writer 側の
  `os.getppid()` が短命な中間 shell を指し、hook 起動ごとに異なる PID
  の pointer が乱立するケースでも成立する（全て同じ root 値を書くため）
  ——「候補がちょうど 1 本」という file count ベースの規則ではこの場合
  ほぼ常に「候補 2 本以上」になり 127 になっていたはずで、実装レビュー
  （advisor）で file count 版の欠陥として指摘され、root 値一致版へ
  修正した。鮮度フィルタは「対立が実際に見つかったときだけ、その対立を
  解消するために」二段目として使う設計にした（一段目は鮮度を問わない）
  ——鮮度フィルタを一段目から常時適用する版では、アイドル状態が
  5 分を超えた単独ホストが自分自身のポインタしか無いのにそれを
  「鮮度切れ」として除外し、127 になる欠陥があった。これも実装レビュー
  （advisor、2 巡目）で指摘され、二段判定へ修正した。
- 書き込みが原子的になり（`os.replace()`）、並行 hook 起動時の partial
  read が起きなくなった（R-06）。GC が「実行中の PID を消さない」ことに
  加えて「今書いたばかりの PID は他の削除条件を満たしても消さない」
  （`keep_pid` による除外。写者自身の pointer が同じ呼び出しの中で
  自壊する経路を防ぐ）ことを優先しつつ throttle 付きで自動走行するため、
  `roots/` が無制限に蓄積しない。

### 否定的

- GC 対象外の運用（複数ホストを長期間並行稼働し続ける等）では `roots/`
  に生存ポインタが複数残り続けること自体は仕様どおりであり、この状態が
  続く限り「root 値一致」規則は効かず祖先チェーンの一致に依存する
  （異なるホストの root 値は通常一致しないため、これは意図した挙動）。
- 直近 5 分以内に別ホストが hook を起動しており、かつそのホストが今も
  稼働中で、かつ祖先チェーンが外れる場合、真の対立として 127 になる
  （意図した挙動）。一方、別ホストが直近 5 分以内に hook を起動したが
  既に停止している場合は、二段目（鮮度で絞った再判定）でその古いホストの
  ポインタが対立相手から外れるまでの最大 5 分間、同様に 127 になりうる。
  ユーザーへの影響は「直前まで別ホストを触っていた場合に限り、最大 5 分
  だけ再解決を待たされる」程度に抑えている（単独ホストがアイドル状態の
  ときは、鮮度を問わず一段目で解決するため影響を受けない）。

### リスク

- ホストが hook を中間 shell 経由で起動し、かつ launcher 自身
  （writer 側）の `os.getppid()` が短命なラッパー shell を指す場合、
  そのラッパーは 1 回の hook 呼び出しの後すぐ終了する。GC はこの PID を
  「消す」対象にしうるが、書き込んだ本人の呼び出し内では `keep_pid` で
  除外され、次回以降の GC（1 時間 throttle）で初めて削除対象になる —
  それまでの間、同一ホストのポインタが `roots/` に複数蓄積するが、
  全て同じ root 値なので tier 2（root 値一致）で解決できる。祖先チェーン
  （reader 側の歩き上がり）は「reader がホストより深い」ケースを救うが、
  「writer がホストより深い」ケースには tier 2 の root 値一致で対処する
  形になっており、両者は非対称な仕組みで別々に救済される
  （advisor 指摘のとおり、両方向を同じ機構ではカバーしない）。
  この場合の救済策は「候補ちょうど 1 本」規則のみであり、複数ホストが
  同時稼働していると 127 になる。
- Copilot CLI での実機動作（`~/.bluecore/roots/` が実際に生成され、
  md の 1 行 bootstrap が動くか、祖先チェーンが実際に必要になるか）は、
  Copilot の SessionStart hook 発火自体は
  `~/.copilot/session-state/<id>/events.jsonl` で確認済みだが、本改訂の
  実機再検証は本 ADR の範囲に含めていない（ADR-0005 に従い手動
  チェックリストで別途実施する。実施時は `~/.bluecore/roots/` の実
  リスティング — ファイル数・PID の生存性・mtime の分布 — を採取し、
  上記の残余リスクが実際に発生しているかどうかをこの節へ追記する）。
- `resolve_effective_target`（`config_protection.py`/`bash_config_protection.py`
  が使う symlink 解決 helper）は hook payload の `cwd` ではなく
  `Path.cwd()`（launcher プロセス自身の working directory）基準で解決する。
  これは `resolve_repo_root()` 等、既存の repo-root 解決ロジックがもともと
  前提にしていた挙動を踏襲したものであり、本ラウンドで新たに導入した
  前提ではない。hook payload が別 cwd を運んでくるケースが将来出てきた
  場合は、この前提が暗黙のまま残っていることに注意する必要がある。
