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

最初のバイトのタイムアウトは 2.0 秒、`hooks.json` の保護エントリの timeout は 15 秒とする。
Windows の wrapper + インタプリタ起動の遅さで host 側 timeout（＝ホストによっては fail-open）へ
入りにくくするための余裕である。

## 走査コストは保護のバイパスとして扱う

「検査が完了しない」でも、環境の故障（fail-open）と、入力が検査時間を支配して timeout を任意に
誘発できる経路は別である。後者を fail-open にすると保護がオプトアウト可能になる。

1. **入力が走査時間を支配する経路は fail-closed にする。** コマンドのトークン数が上限（5,000）を
   超える場合は走査せず BLOCKED を返す。上限超過を allow に倒すと、攻撃者が上限を超える入力を
   作るだけで保護を切れるため採らない。5,000 トークンのシェルコマンドは実務上存在せず、上限に
   衝突する場合はコマンドを分割する側を正とする
2. **予算・サニタイズ・正規化のガードは、再帰境界にも同じものを置く。** entry point だけに置いた
   ガードは `sh -c` 1 段で迂回できる
3. **deny の終了コードは出力層の失敗から独立させる。** 出力例外は内側で捕まえ、exit code は必ず
   返す。PreToolUse では exit 1 が non-blocking error（＝ツール実行）になるため、出力層の例外が
   deny を allow へ反転させてはならない
4. **`config_protection` はプラグイン自身の `hooks.json` と、ホスト設定の無効化口を守る。**
   `plugins/claq/hooks/hooks.json` はパス連続一致で無条件保護する（basename 一致だと consumer の
   `.claude/hooks.json` と衝突する）。`settings.json` / `settings.local.json` は `CLAQ_PYTHON` の
   出現、および `hooks` / `enabledPlugins` / `disabledPlugins` のときだけ deny する。ブロック
   メッセージは「ユーザーに依頼せよ。フックを自分で無効化するな」とする

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
