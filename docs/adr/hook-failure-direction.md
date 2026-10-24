# 保護フックの失敗方向と exit code 契約

保護フック（`block_no_verify` / `pre_bash_commit_quality` / `bash_config_protection` /
`config_protection`）が「検査を完了できなかった」とき、allow と deny のどちらへ倒すか。
および exit code が何を表すかの契約。

## 共通原則

保護フック群は「うっかり事故の抑止」であり「敵対的回避への防壁」ではない。この前提の下で、
「検査が完了しない」を次の 3 つに分け、それぞれ別の向きへ倒す。

| 事象 | 誘発者 | 倒す方向 |
|---|---|---|
| 環境が壊れていて検査を開始できない（Python 3.12 未満、launcher 不在、payload を渡さないホスト） | 誰も意図しない | fail-open（exit 0） |
| 受け取った入力を解析し切れない（未知のシェル構文） | 入力側だが対処に限界がある | 誤検出側（安全に厳しく倒す） |
| 入力を受け取れなかった／入力が検査時間を支配する | 入力側が任意に誘発できる | fail-closed（exit 2） |

fail-open を「環境の故障」に限定するのは、それ以外へ広げると保護がオプトアウト可能になるため
である。攻撃者が stdin を壊す・巨大な入力で timeout を誘発するだけで検査が消えるなら、その保護は
存在しないのと同じになる。

host timeout は fail-open と等価である。host が kill した hook は exit code を返さず、ホストに
よっては「操作を通す」へ倒れる。したがって timeout に達しうる経路は、外側から kill される前に
内側で制御された失敗（exit 2）へ倒す。

## フックは検査対象が確定するまで fail-open にする

検査対象が確定していない状態での失敗は fail-open（exit 0）にする。検査対象が確定した後
（「これは git commit である」「これは保護対象ファイルへの書き込みである」）の失敗は
fail-closed（exit 2）にする。全面 fail-closed（読めない・確定できない場合は一律 exit 2）は
採らない — stdin リダイレクト漏れや Python バージョン不一致は利用者環境側の問題であることが
多く、fail-closed にすると全 Edit/Bash が拒否されてセッションが即死し、Python 3.12 未満では
復旧手段（hook を直すための Bash 実行）ごと塞がれて復旧不能になる。「保護が効かない」より
「作業が一切できなくなる」方が被害が大きい。

各経路への適用:

- **Python 3.12 未満**: hook を実行せず stderr に構造化理由を書いて exit 0 にする。ランタイムは
  venv も install スクリプトも持たないため、インストール時に対応 Python を検証する経路が無い
- **commit と無関係な malformed Bash 呼び出し**: raw 文字列上に `git commit` らしき痕跡が無ければ
  exit 0 にする。matcher が Bash 全体である以上、malformed というだけで一律 deny すると無関係な
  呼び出しまで巻き込む
- **secret scanner の実時間バジェット超過**: 検査対象（commit 対象ファイル）は既に確定している
  ため deny（severity error）にする。ホストの hook timeout に達して外側から強制終了されるより、
  内側で先に制御された失敗にする方が安全

- **保護名に一致したトークンの解決失敗（Bash 経由の書込み）**: 書込み先トークンが保護 basename
  または保護 path セグメントに一致した時点で検査対象は確定している。その後 `Path.resolve` が失敗
  する（単独サロゲート・NUL 混入・symlink ループ）と書込み先がリポジトリ内か外かを判定できないが、
  これは入力側が任意に作れる失敗なので deny にする。allow に倒すと
  `rm \ud800/.git/hooks/pre-commit` のように解決不能な接頭辞を 1 つ足すだけで保護が外れる。
  deny の範囲は「保護名に一致」かつ「解決に失敗」に限る。解決できてリポジトリ外のパス
  （`/tmp/x/ruff.toml` 等）と、保護名に一致しない解決不能トークンは allow のままにする。
  リポジトリルート自体を決定できない場合の allow は、解決できたトークンについての判断であり、
  解決不能トークンの deny を変えない

環境故障の間、保護フックは実質無効化される。利用者が気づく手段は stderr の
`claqProtectionDisabled` 警告のみであり、監視していない環境では見落としうる。この残余リスクは
共通原則の脅威モデルの範囲で許容する。

## `--bg` 起動の exit code は「起動受付」を表し「処理成功」を表さない

`launcher.py --bg` は子を `start_new_session=True` で起動し、親はすぐ終了する。主用途は
SessionEnd の handoff で、ハーネスの hook timeout に阻まれず non-blocking に終了することが目的
である。したがって `--bg` 起動の親プロセスの exit code は「子プロセスの起動を受け付けたか」だけを
表し、「子プロセスの処理が成功したか」は表さない。親を子の完了まで待たせる案は `--bg` を使う理由
そのものを失わせるため採らない。

処理結果を完全に見えなくはせず、次回セッションで観測可能にする:

- 子の stdout/stderr は `~/.claq/logs/bg-YYYY-MM-DD.log` へ追記する
- 子は watchdog でラップし、一定時間でハングしても確実に回収する
- 次回 SessionStart が前回ログを検査し、内容があれば 1 行だけ通知する（失敗時のみ出力し、当日＋
  前日の 2 ファイルまでしか読まない。2 日以上前の失敗や次回 SessionStart 自体が走らないセッション
  での失敗は通知されず埋もれる）

起動 ID を発行してポーリング API で状態を問い合わせる方式は採らない。永続化するテーブルは
`repos`/`knowledge`/`sessions` の 3 つだけという単純さを設計原則としており、非同期ジョブの状態
管理を持ち込むと原則に反する。

## 保護フックは stdin を「読めなかった」場合に fail-closed する

stdin の「入力が無い」と「読めなかった」は証拠の質が違う。前者を deny にすると、payload を渡さない
ホストでは全ツール呼び出しが拒否され復旧不能になる。後者を fail-open にすると、入力経路が壊れて
いるだけで保護が全面的に無効化され、しかも exit 0 として成功扱いになる。

stdin の状態を 2 つに分け、後者だけを fail-closed にする。

1. **payload が無い**（`sys.stdin is None` / TTY 接続 / 即 EOF で 0 バイト）→ 空文字列として扱い
   exit 0 で素通りさせる
2. **payload を読めなかった**（読み取りが例外化した／最初のバイトが 2.0 秒以内に届かなかった）→
   4 つの保護フックは deny（exit 2 + `permissionDecision: deny`）に倒す

readiness 判定に `select` は使わない。Windows の `select` は socket にしか使えず、通常のパイプに
対しては `OSError` を投げて「入力なし」に誤認されるため、保護が構造的に無効になる。代わりに
ブロッキング read を daemon スレッドへ隔離し、本スレッドは timeout 付きで待つ。3 プラットフォーム
共通の 1 本のロジックとし、OS 判定やプラットフォーム分岐は持たない。

保護対象外の経路（`launcher --bg` の stdin 中継、`mem.cli` の payload 読み取り）は例外を空文字列へ
正規化する。これらは「読めなかった」ときに採れる別の行動が無いため区別しない。

最初のバイトのタイムアウトは 2.0 秒、`hooks.json` の保護エントリの timeout は 15 秒とする（ステージ済み
ファイルの lint と秘密情報の走査を行う `pre_bash_commit_quality` だけは 30 秒）。
Windows の wrapper + インタプリタ起動の遅さで host 側 timeout（＝ホストによっては fail-open）へ
入りにくくするための余裕である。

## 走査コストは保護のバイパスとして扱う

「検査が完了しない」でも、環境の故障（fail-open）と、入力が検査時間を支配して timeout を任意に
誘発できる経路は別である。後者を fail-open にすると保護がオプトアウト可能になる。

1. **入力が走査時間を支配する経路は fail-closed にする。** コマンドのトークン数が上限（5,000）、
   または UTF-8 バイト数が上限（64KiB）を超える場合は走査せず BLOCKED を返す。トークン化の
   コストはバイト数に対して二次で増えるため、トークン数だけでは長い 1 トークンの入力を止められず、
   バイト数を先に見る。上限超過を allow に倒すと、攻撃者が上限を超える入力を作るだけで保護を切れる
   ため採らない。5,000 トークン・64KiB のシェルコマンドは実務上存在せず、上限に衝突する場合は
   コマンドを分割する側を正とする
2. **予算・サニタイズ・正規化のガードは、再帰境界にも同じものを置く。** entry point だけに置いた
   ガードは `sh -c` 1 段で迂回できる
3. **deny の終了コードは出力層の失敗から独立させる。** 出力例外は内側で捕まえ、exit code は必ず
   返す。PreToolUse では exit 1 が non-blocking error（＝ツール実行）になるため、出力層の例外が
   deny を allow へ反転させてはならない
4. **`config_protection` はプラグイン自身の `hooks.json`、実行中のプラグインのランタイム、ホスト設定の無効化口を守る。**
   - `plugins/claq/hooks/hooks.json` はパス連続一致で無条件保護する（basename 一致だと consumer の
     `.claude/hooks.json` と衝突する）。
   - 実行中の plugin root（フック自身のファイル位置から導く実体パス）の `runtime/`（`hooks.json` が
     起動する `claq-hook`）と `src/claq/`（フック本体）を守る。書き換えると次のツール呼び出しから
     全保護フックが外れるため。配下への Edit/Write と Bash の書込み、Bash によるディレクトリ自身・
     祖先の削除と退避を deny する。照合は symlink を解決した実体パスで、大小を無視する。
   - plugin root が作業中リポジトリ（git の toplevel）の内側にあるときは対象外にする。claq 自身を
     開発する checkout ではこれらの編集が本来の作業で、守ると claq を開発できなくなる。git は
     書込み先がこれらに当たったときだけ起動する。toplevel が決まらない（リポジトリ外・git 不在・
     タイムアウト）ときは対象外にしない。
   - `cd` を含む Bash コマンドでは相対パスを解決できないので、basename が `claq-hook` /
     `claq-hook.cmd` の書込み先を deny する。
   - `settings.json` / `settings.local.json`（basename で判定）は置換前後の全文を JSON として比べる
     （全文の作り方は git config と同じ）。比べるのは top-level の `hooks` / `enabledPlugins` /
     `disabledPlugins` の部分木（有無の変化を含む）と、`env` のうちインタプリタ解決に効く
     `CLAQ_PYTHON` / `PATH` / `PYTHONPATH` / `PYTHONHOME`（名前は大小無視）である。`env` が object
     でなければ値全体を比べ、`env` が無いことは空と同じに扱う。値は型まで区別する（`true` を `1` に
     変えるのも変化）。これらが変わる編集だけを deny し、それ以外（`model` / `permissions` / `env`
     の他のキー）は通す。
   - 置換後が JSON object として読めない（コメント・末尾カンマ・NaN/Infinity・深すぎる入れ子）、
     全文を作れない（`apply_patch` を含む）、走査上限（256KiB）を超える場合は検査できないので
     deny する。コメントを含む `.vscode/settings.json` もここに入る。置換前が読めないときは
     保護対象が無かったものとして扱う。
   - ブロックメッセージは「ユーザーに依頼せよ。フックを自分で無効化するな」とする。
   - 残る穴: `src/` 直下（`src/claq/` の外）のモジュールによる標準ライブラリの差し替え、`cd` 後の
     起動ファイル以外の相対パス、toplevel を変える操作（上位での `git init`、`env` の `GIT_DIR` /
     `GIT_WORK_TREE`）による除外の拡大、`env` のその他の変数（`LD_PRELOAD` 等）、ハードリンク経由の書込み。
5. **git config は Edit/Write では置換後の全文の前後比較で、Bash では書込み先に現れた時点で deny する。**
   対象は `.git/config`・`.git/config.worktree`・`.git/worktrees/*/config`・
   `.git/worktrees/*/config.worktree` と、ホーム側の `~/.gitconfig`・`$XDG_CONFIG_HOME/git/config`
   （未設定なら `~/.config/git/config`）。ホームは `get_home_dir`（`CLAQ_HOME` → `HOME` →
   `USERPROFILE`）に揃えるため、`CLAQ_HOME` を設定した環境では `HOME` 側の `.gitconfig` は対象外になる。
   symlink は実体で比べ、大小は無視する。`core.hooksPath` / `include.path` / `includeIf` / `alias` は `git config` 経由では
   `block_no_verify` が塞いでいるが、設定ファイルを直接書けば同じ効果になる。ホーム側の
   `core.hooksPath` は全リポジトリのフックを一度に外せる。対象を無条件に塞ぐと remote / branch /
   user の通常編集まで壊れるので、Edit/Write は内容で判定する。
   判定は、置換前の全文（ディスク上の現在の内容。読めない・存在しなければ空）と置換後の全文を
   最小限の git-config パーサで (section, subsection, key, 値) に分解し、`block_no_verify` が塞ぐ
   key 集合（key・section は大小無視）と照合する。置換後の全文は、Write なら content、Edit なら
   置換を当てた全文（`replace_all` を尊重）、MultiEdit なら編集を順に当てた全文（64 件まで）である。
   置換前は改行を変換せずに読む（git は CRLF だけを改行に畳み、単独の CR は空白として読む）。
   対象が空・存在しない・読めないときは new_string だけを断片として扱う。内容のあるファイルで
   置換対象が空・見つからないときは、実ツールが（引用符の正規化などで）一致させる可能性を
   排除できないため全文を作らず deny する。置換の結果が走査上限を超えるときは、文字列を作る前に
   deny する（置換の連鎖で全文を膨らませて検査を時間切れへ追い込ませない）。パーサは `[sec]`・`[sec "sub"]`・旧記法
   `[sec.sub]`、`#` / `;` のコメント、継続行、値の引用符とエスケープ、値なしの key、見出しと同じ行の
   key、CRLF、先頭の BOM を読む。見出しより前の key や読めない行は解析エラーである。
   deny するのは、置換後の危険 key の列が置換前の列の部分列でない、または key ごとの最後の値
   （実効値）が変わるときである。集合ではなく順序つきの列で比べるのは、後ろの上書きだけを消して
   危険な値を実効にする編集や、順序の入れ替えを通さないためである。危険 key を丸ごと消すだけの編集、
   危険 key を含まない編集（remote / branch / user の追加・変更）、置換前後で危険 key が同一の編集は
   通す。`alias` セクションのキーは全て危険とみなす（alias が任意コマンドを定義できるかは静的に
   決められないため）。したがって `[alias] st = status` のような無害な alias の追加も deny になり、
   ユーザーに依頼する。コメント内の `hooksPath`、`[aliases]` のような別名の見出し、include 以外の
   セクションの `path =` は通す。
   解析できない全文・全文を作れない入力（`apply_patch` を含む）・置換前後どちらかが走査上限
   （256KiB）を超える入力は、他の条件付き保護対象と同じく deny する。置換前が解析できないとき
   だけは、置換前に危険 key が無かったものとして扱う（置換後が解析でき、危険 key が無ければ通る）。
   Bash は書込み本文をトークンから解釈できないため、内容を問わず書込み先位置の上記パス
   （リダイレクト・`tee`・`sed -i`・`cp`/`mv` の宛先・`rm`・`chmod` 等）を deny する。`cat`・`grep`・
   `sed -n p` のような読み取りは通す。ホーム側の設定はリポジトリ外の固定位置なので repo スコープ
   判定の前に、リポジトリルートが決まらなくても deny する。`cd` を含むコマンドでは相対パスを cwd 基準で
   解決できないため、basename が `.gitconfig` の書込み先を deny する。書込み先トークンの先頭の `$HOME` /
   `${HOME}` と、設定されている `$XDG_CONFIG_HOME` は展開してから照合する。通常設定の
   回避路は `git config <key> <value>`（書込み先トークンを持たない）である。
   **残る穴**: `GIT_DIR` の差し替え先とベアリポジトリの設定、system config（`/etc/gitconfig`）、
   `include.path` が指す先のファイルそのもの（既存の include 先を書き換えれば `core.hooksPath` を
   入れられる）、ハードリンク経由の書込み、サブモジュールの設定（`.git/modules/*/config`）、`$HOME` 以外の変数やコマンド置換
   （`${XDG_CONFIG_HOME:-...}` を含む）を含む書込み先、`cd` 後の `.gitconfig` 以外の相対パス
   （`cd ~/.config/git && echo x >> config`）、`core.sshCommand` /
   `core.fsmonitor` / `credential.helper` のようにフック以外でコマンドを走らせる key
   （`block_no_verify` の集合に無い）は対象外である

この保護は決定的ではない。書込み先がリダイレクトでも既知エディタでもない経路（`python3 -c` に
よる直接書き込み等）は素通りしうる。守っているのは素直な agent が近道を取る経路と、低コストな
注入である。

**既知の未対応（実測できないため直さず残す）**: Windows の `claq-hook.cmd` は `CLAQ_PYTHON` の
分岐を PATH 検査より前に置くため、絶対パスの `CLAQ_PYTHON` を設定すると PATH 検査が丸ごと飛ぶ。
POSIX 側は逆順で、絶対パスの `CLAQ_PYTHON` でも PATH 汚染から回復しない。POSIX 側の順序が正しい
——launcher とその子プロセス（`git` を含む）は汚染された PATH で解決を続けるのであって
`CLAQ_PYTHON` はそれを直さないため。この並べ替えは darwin から cmd.exe を実測できないため未着手
のままにしてある。同じ理由で `git` 起動を絶対パス解決へ寄せる変更も未着手。未検証の編集は
「その環境で保護が黙って無効」という最悪の失敗へ倒れうるため、実測できる者が直すまで残す。
