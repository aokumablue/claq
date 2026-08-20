# ADR-0008: plugin root は `~/.bluecore/env.sh` ポインタで解決し、md にベンダ固有パスを書かない

**日付**: 2026-08-20（初版）／2026-08-20 改訂 ×4  **ステータス**: accepted

## 改訂履歴（2026-08-20）

**改訂 1**: 初版（M-02 対応）で導入した 3 段解決
（PATH → `roots/$PPID.sh` → `roots/latest.sh`）は、初回の Copilot CLI 実機
検証レポート（R-01〜R-06 の指摘）で BLOCK 判定を受けた。root cause は
「どの plugin runtime を実行するか決める前に
roots pointer を shell として `.`/source していたこと」（R-01/R-03）。pointer を
データファイル化し、tier 2 を「root 値合意 + 鮮度フィルタ（二段判定）」に
作り直した。

**改訂 2**: v0.9.36 時点の再検証で、H-02 が、改訂 1 の tier 2（root 値合意
fallback）は「祖先であることを証明
できないシェルが、たまたま合意している root を採用してしまう」ことを再指摘。
ユーザーから「リスクがあるとみなせる fallback は望ましくない、完璧に対応
したい」との明示指示を受け、**tier 2（推測 fallback）を全廃**し、
「祖先チェーン全体（最大 4 段）に writer が書き、`lstart`（プロセス起動
時刻）で個体識別し、対立する root は poison（空ファイル化）する」設計へ
作り直した。

**改訂 3**: 改訂 2 の実装完了後、advisor レビューで「poison は
共有されうる祖先 PID（login shell・terminal app 等）が生存し続ける限り
恒久化し、その host インスタンスを自己修復不能なまま数日単位でブロック
しうる」という新たな失敗モードを指摘された。対処として「共有されうる
祖先には検知して防御する（poison）」ではなく「共有されうる祖先には
そもそも書き込まない（書き込み範囲を host インスタンス専有の PID
最大 2 段に絞る）」という事前回避へ設計を改めた。poison 機構は不要になり
撤去した。「決定」「結果」「リスク」節を全面差し替え、改訂 2 の設計は
代替案として要約を残す。代替案 1〜3（初版由来、環境変数判定を採らない
理由・ランタイム集約 install を採らない理由）は変更なし。

**改訂 4（本改訂）**: v0.9.37 時点の再検証の M-02 を受け、一時ファイルを
予測可能な `<name>.tmp.<pid>`（`O_CREAT|O_TRUNC`）から `tempfile.mkstemp`
（prefix `<name>.tmp.`、ランダム suffix、`O_EXCL`）へ変更した。同一
プロセス内の thread 衝突は現行 launcher に発生経路が無い。これは同一
UID 内の precreation / symlink 追従を除く**堅牢性改善**であり、
owner/mode 非検証（ADR-0009）は変えない。`env.sh.tmp.*` は `roots/` の
GC では見えないため、`~/.bluecore` 直下で同じ age-gate を適用する。

## コンテキスト

v0.9.34 時点のランタイム監査レポートの M-02 指摘は、
`bluecore_run`/`bluecore_mem_learn`（`runtime/bluecore-helpers.sh`
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
判別は環境変数ではなく祖先プロセスの `PID` + `lstart`（起動時刻）を鍵に
する。roots pointer は shell コードではなく単なるデータ（root + lstart
の 2 行）として扱う。祖先で解決できない場合に推測する fallback は
持たない。writer が書き込む祖先の範囲は host インスタンス専有の PID
（最大 2 段）に限定し、他 host と共有されうる祖先には一切書き込まない。**

構成:

1. `plugins/bluecore/src/bluecore/lib/env_pointer.py` の
   `write_env_pointer(plugin_root)` は、`os.getppid()` とその親
   （**最大 2 段**）にポインタを書く（`_resolve_ancestor_chain`）。
   1 回の `ps -eo pid=,ppid=,lstart=` でチェーンと各 PID の起動時刻
   （`lstart`）を同時に取得する（実測 10〜16ms、hook のタイムアウト
   予算に対して無視できるコスト。全 hook 起動でこの関数が走るため、
   1 回の呼び出しに抑えることを優先した）。`ps` の実行に失敗した場合は
   その回は何も書かない（ADR-0001 の fail-open。次回 hook 起動で
   自己修復する）。
2. 各ポインタは `root\nlstart\n` の 2 行。`.`/`source` はせず、
   `IFS= read -r` で 2 行取り出すだけ（R-01/R-03: ポインタの内容が
   shell として実行されることは無い）。`lstart` は resolver が
   「今生きているその PID は、記録時と同一のプロセスか」を照合する
   ための識別子（PID は再利用されるが、同一 PID が同一 `lstart` を
   持つのは同一プロセスの生存中に限られる）。
3. 深さを **2 段に絞る**のは、共有されうる祖先へ書き込むこと自体を
   やめるため。`os.getppid()`（bash tool の shell）とその親（host
   バイナリそのもの）は、いずれもこの host インスタンス専有の PID で
   あり、他の host インスタンスがこれらの PID を祖先として持つことは
   ない。より上位の祖先（login shell・terminal app 等）は同じ
   terminal / login shell から起動された別々の host 間で共有されうる
   が、writer はそこへ一切書き込まない。したがって同一 PID に別の
   root が観測されるのは「同一 host がプラグインをアップグレードした」
   ケースのみであり、これは上書きが正しい挙動なので `_write_ancestor_pointer`
   は単純に上書きする（poison のような衝突検知は行わない — 改訂 2 は
   共有祖先への書き込みを許した上で衝突を事後検知していたが、advisor
   レビューで「共有される祖先 PID が生存し続ける限り poison が恒久化し、
   自己修復不能な失敗モードになる」と指摘され、書き込み範囲を狭める
   事前回避へ改めた。下記代替案参照）。
4. `plugins/bluecore/runtime/env-template.sh` は自分の `$PPID` とその親
   （**最大 2 段**）を `ps -o ppid=` で辿り、各段で
   `_bluecore_resolve_ancestor_pointer` を呼ぶ: pointer が 2 行とも
   揃っていて、かつ `LC_ALL=C ps -o lstart= -p <pid>` で得た**現在の**
   lstart が記録値と一致する場合のみ採用する。空・欠落・不一致（PID
   再利用）はスキップして次の祖先へ進む（空ファイルへの耐性は writer
   がもう poison しなくなった後も防御として残している）。**祖先
   チェーンで解決できなければ、推測せず常に `exit 127`**
   （H-02: 改訂 1 の「root 値合意 fallback」は全廃した — 下記代替案参照）。
5. `write_env_pointer()` は `env.sh` を `tempfile.mkstemp`（prefix
   `<name>.tmp.`）経由の `os.replace()` で原子的に書く（R-06）。GC は今回書いた祖先チェーン
   全体の PID 集合（`keep_pids`）を、他の削除条件を満たしても対象から
   除外する（GC が同じ呼び出しの中で「今書いたばかりの記録」を
   自壊させないため）。置き場所は `get_bluecore_dir()`（`BLUECORE_HOME`
   を見る、テスト隔離用ノブ）ではなく **`$HOME` 固定**にした。md の
   bootstrap 行が `$HOME` 固定である以上、writer 側もそれに合わせる
   契約とし、`BLUECORE_HOME` はデータ永続化（`mem.db`・`logs`）側の
   契約とは意図的に切り離した（R-04）。
6. `launcher.py:main()` が全 hook 起動のたびに `write_env_pointer()` を
   呼ぶ（SessionStart に限定しない。他の hook 起動でもポインタが更新される
   ため復旧が速い）。書き込み失敗は握り潰すが、プロセスごと 1 回だけ
   stderr へ JSON 警告を出す（無音の機能低下を防ぐ）。
7. `write_env_pointer()` は 1 時間に 1 回だけ `roots/` と
   `~/.bluecore/env.sh.tmp.*` の GC を実行する
   （`roots-gc.stamp` の mtime で throttle）。削除条件は「ファイル名が
   数字のみでない（旧形式の残骸・他 writer の in-flight 一時ファイルを
   含む。`_DEAD_PID_GRACE_SECONDS` による age-gate 後に削除。M-01）」
   「PID が既に無く、書き込み直後の猶予（1 時間）を過ぎている」「PID は
   生存しているが mtime が 7 日を超えている（稼働中ホストは毎 hook で
   mtime を更新するため、これは PID 再利用と判断できる。ただし
   `lstart` 照合が主たる PID 再利用対策であり、これは補助的な掃除
   ルールに過ぎない）」のいずれか。生存 PID かつ TTL 内のポインタは
   誤って削除しない。
8. `LC_ALL=C` を writer（Python subprocess の env）・resolver（shell の
   `ps` 呼び出し）双方で明示する。`ps -o lstart=` の出力はロケール依存
   （`LANG=ja_JP.UTF-8` では `木  8/20 ...`、`C` ロケールでは
   `Thu Aug 20 ...`）であることを実装中に実機で発見した。正規化しないと
   writer と resolver の ambient locale が異なる環境で同一プロセスの
   lstart が一致せず、常に PID 再利用と誤判定してしまう。
9. agents/commands/skills の md は
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

### 代替案 4（改訂 1 で採用、改訂 2 で撤回）: 祖先で解決できない場合、
「`roots/` の有効な候補が全て同じ root 値に合意しているか」で推測する

- 長所: writer 側の `os.getppid()` が使い捨ての中間 shell を指す場合
  （祖先 walk だけでは reader/writer の非対称性を解決できない）でも、
  複数ホストが同時稼働していなければ実用上ほぼ常に解決できた。
  実装コストが低い（writer 側の変更が不要）。
- 短所: v0.9.36 時点の再検証の H-02 が指摘したとおり、「祖先であることの
  証明」ではなく「たまたま
  他の記録と一致すること」に基づく推測であり、当てが外れれば別 host の
  root を静かに採用してしまう。改訂 1 時点では「同一 plugin の別
  バージョンを source するだけで version skew に過ぎない」という
  費用対効果の分析から許容範囲と判断していたが、これは技術的な
  正しさの評価であってユーザー自身のリスク許容度を代弁するものではない。
- 却下理由: ユーザーから「リスクがあるとみなせる fallback は望ましく
  ない、完璧に対応したい」との明示指示があった。技術的な費用対効果の
  評価が「許容範囲」と判定していても、それを覆すユーザーの決定を
  費用対効果の議論で説得しようとするのは筋が違う——ユーザーが払っても
  よいと考えるコストを、こちらが代わりに判断すべきではない。実際に
  「祖先チェーンへの writer 側書き込み + `lstart` 個体識別」という
  形で、コストを許容範囲（1 hook 起動あたり `ps` 呼び出し 1 回）に
  抑えたまま推測 fallback を排除できる設計が見つかったため、この
  代替案は技術的に必須ではなくなった。

### 代替案 5（改訂 2 で採用、本改訂で撤回）: 祖先チェーン全体（最大 4 段）に
書き、対立する root は poison（空ファイル化）して事後検知する

- 長所: writer の書き込み範囲を制限しないため、reader/writer の祖先
  walk がどこで一致しても解決できる。共有される祖先で衝突が起きても
  「双方とも使わない」ことで誤った root を掴む経路を塞いだ。
- 短所: advisor レビューで指摘された自己修復不能な失敗モードを持つ。
  同じ terminal / login shell から異なるバージョンの host（例:
  v0.9.36 と v0.9.37 を別セッションで同時に使う）が共有祖先へ書き込むと
  その祖先 PID は poison になる。poison は「その PID が生存し続ける
  限り誰にも上書きされない」設計だったため、以後そのターミナル・
  ログインシェルの寿命が尽きるまで（実運用では数日単位）、深い祖先の
  解決能力を失ったまま復旧手段が無かった。祖先チェーンの浅い段
  （host インスタンス専有 PID）で解決できている限り実害は出ないが、
  「浅い段の pointer が GC された直後」「新しい host がまだ書いて
  いない」といったタイミングでは、poison された深い祖先だけが頼りに
  なる場面が起こりえた。
- 却下理由: 「リスクがあるとみなせる fallback は望ましくない、完璧に
  対応したい」というユーザーの要求は、H-02（誤った root を静かに掴む）
  だけでなく、この種の自己修復不能な可用性低下にも一貫して当てはまる。
  「共有祖先の衝突を検知して防ぐ」より「共有祖先そのものへ書き込まない」
  ほうが根本対処として単純かつ安全（衝突検知ロジック自体が丸ごと
  不要になる）なため、この代替案は不採用にした。

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
- `latest` の無条件採用（R-05）を全廃し、**祖先チェーンで `lstart` が
  一致するポインタが見つからなければ、推測せず常に 127**にした。
  「祖先であることの証明」が唯一の採用条件になったため、複数ホスト
  同時稼働時に別ホストの install を誤って source する経路は構造的に
  無くなった。writer 側は既存レビュー（改訂 1 時点の advisor 指摘）で
  発覚していた「候補ちょうど 1 本」規則の脆弱性をそもそも持たない
  （file count に依存する判定自体が無いため）。
- writer が書き込む祖先の範囲を host インスタンス専有の PID（最大
  2 段）に限定したことで、「共有されうる祖先での衝突」という事象
  クラス自体が構造的に発生しなくなった。改訂 2 の poison（衝突を
  検知して事後に無効化する）は不要になり撤去した——「検知して防ぐ」
  より「そもそも書かない」ほうが恒久的な失敗モードを持たない
  （代替案 5 参照）。
- PID 再利用は `lstart`（プロセス起動時刻）の一致判定で検出する。
  GC の生存判定・TTL は補助的な掃除ルールに留まり、正確性の根拠は
  `lstart` 照合が担う。
- 書き込みが原子的になり（`os.replace()`）、並行 hook 起動時の partial
  read が起きなくなった（R-06）。GC が「実行中の PID を消さない」ことに
  加えて「今書いたばかりの祖先チェーン全体の PID は他の削除条件を
  満たしても消さない」（`keep_pids` による除外。writer 自身の pointer
  群が同じ呼び出しの中で自壊する経路を防ぐ）ことを優先しつつ throttle
  付きで自動走行するため、`roots/` が無制限に蓄積しない。GC の
  「非数字名を無条件削除」規則は、他 writer の in-flight 一時ファイル
  （`<name>.tmp.<random>`。旧形式は `<pid>.tmp.<writer_pid>`）も削除しうる
  race があった（M-01）ため、既存の `_DEAD_PID_GRACE_SECONDS` による
  age-gate を追加して閉じた。`env.sh` の一時ファイルは `roots/` ではなく
  `~/.bluecore` 直下に作られるため、同じ age-gate を prefix
  `env.sh.tmp.` に限定してそこでも適用する。

### 否定的

- writer は全 hook 起動のたびに `ps` を 1 回呼ぶ（実測 10〜16ms）。
  従来（`os.getppid()` だけを直接書く）よりコストが増えるが、hook の
  タイムアウト予算（5〜30 秒）に対しては無視できる。

### リスク

- `ps -o lstart=` の出力はロケール依存であることを実装中に発見した
  （本 ADR の「決定」節 8 参照）。`LC_ALL=C` を両側で明示することで
  対処したが、`ps` 実装が `LC_ALL` を無視する将来のプラットフォームが
  出た場合、writer/resolver 間で lstart 表現が食い違い、常に 127 に
  なる可能性がある。macOS（BSD ps）・Linux（procps-ng）の両方で実機
  確認済み。
- 祖先チェーンの深さは 2 段（`os.getppid()` とその親）に固定している。
  これは「host インスタンス専有の PID にしか書かない」という設計の
  核心だが、逆に言えば host の起動構成がこの前提と食い違う場合
  ——たとえば bash tool の shell の直接の親が host バイナリ自身では
  なく、さらに使い捨ての中間 shell を挟む構成——では、writer が書いた
  祖先ポインタに reader の祖先 walk が届かず 127 になりうる。実測では
  Claude Code の構成（launcher の直接の親が bash tool の shell、その
  親が host バイナリそのもの）で 2 段は十分だった
  （v0.9.36 時点の再検証の検証環境と同様の構成を想定）が、他ホストでの
  実測は未確認。改訂 2
  （深さ 4 段 + poison）と比べると「届かず 127」になりうる場面は増える
  可能性があるが、127 は自己修復可能（原因が明示的で、次回の host
  起動構成の変化やユーザーの対処で復旧する）なのに対し、poison の
  恒久化は自己修復不能だったため、この trade-off は意図的に受け入れた
  （代替案 5 参照）。
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
