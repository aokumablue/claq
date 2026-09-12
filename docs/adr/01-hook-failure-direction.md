# 保護フックの失敗方向と exit code 契約

保護フック（`block_no_verify` / `pre_bash_commit_quality` / `bash_config_protection` /
`config_protection`）が「検査を完了できなかった」とき、allow と deny のどちらへ倒すか。
および exit code が何を表すかの契約。

| ADR | 決定 | 日付 | ステータス |
|---|---|---|---|
| [ADR-0001](#adr-0001-フックは検査を完走できないとき-fail-open-にする) | 検査対象が確定する前の失敗は fail-open、確定後の失敗は fail-closed | 2026-08-18 | accepted（バイナリ判定に関する受容のみ [ADR-0013](02-shell-analysis-boundary.md) が撤回） |
| [ADR-0003](#adr-0003-detached-background--bgの親-exit-は起動受付であり処理成功ではない) | `--bg` の親 exit は「起動受付」であり「処理成功」ではない | 2026-08-18 | accepted |
| [ADR-0019](#adr-0019-保護フックは-stdin-を読めなかった場合に-fail-closed-する) | stdin を「読めなかった」場合は fail-closed（「入力が無い」場合は素通り） | 2026-09-04 | accepted |
| [ADR-0024](#adr-0024-走査コストは保護のバイパスとして扱い実測できない修正は残存として書く) | 入力が走査時間を支配する経路は fail-closed。実測できない修正は残存として書く | 2026-09-07 | accepted |

## 共通原則

**「検査が完了しない」は 1 つの事象ではなく 3 つある。** 混同すると、片方に正しい規則が
もう片方で保護の無効化になる。3 つを分ける軸は「誰がその状態を誘発できるか」である。

| 事象 | 誘発者 | 倒す方向 | 根拠 ADR |
|---|---|---|---|
| 環境が壊れていて検査を開始できない（Python 3.12 未満、launcher 不在、payload を渡さないホスト） | 誰も意図しない | **fail-open** | ADR-0001 |
| 受け取った入力を解析し切れない（未知のシェル構文） | 入力側だが対処に限界がある | 誤検出側（[ADR-0002](02-shell-analysis-boundary.md)） | ADR-0002 |
| 入力を受け取れなかった／入力が検査時間を支配する | **入力側が任意に誘発できる** | **fail-closed** | ADR-0019 / ADR-0024 |

fail-open を「環境の故障」に限定するのは、それ以外へ広げると**保護がオプトアウト可能に
なる**ためである。攻撃者が stdin を壊す・巨大な入力で timeout を誘発するだけで検査が
消えるなら、その保護は存在しないのと同じになる。

**host timeout は fail-open と等価。** host が kill した hook は exit code を返さないので、
ホストによっては「操作を通す」へ倒れる。したがって timeout に達しうる経路は、外側から
kill される前に**内側で制御された失敗**（exit 2）へ倒す。知識カード
`sync-hook-regex-backtracking-is-fail-open` が記録している形である。

**保護フック群は「うっかり事故の抑止」であって「敵対的回避への防壁」ではない**
（[ADR-0002](02-shell-analysis-boundary.md) と同根拠）。この前提を維持する限り、
fail-open の窓を敵対的に使われる残余リスクは受容する。

---

## ADR-0001: フックは検査を完走できないとき fail-open にする

**日付**: 2026-08-18  **ステータス**: accepted
（バイナリ判定で secret 検査を止める受容だけは [ADR-0013](02-shell-analysis-boundary.md) が撤回。境界規則そのものは維持）

### コンテキスト

保護フックは Bash/Edit/Write の実行前に検査し、問題があれば exit 2 で deny する。検査
そのものが完走できない経路が複数ある。

- stdin が時間内に届かない、または読み取り syscall が失敗する（A-01）
- ホストの Python が 3.12 未満で claq モジュールが import できない（`launcher.py:52-58`）
- `pre_bash_commit_quality` の matcher は Bash 呼び出し全体にアンカーされており、
  malformed JSON が commit と無関係な呼び出しにも届く
- secret scanner がホストの hook timeout に達しうる規模のファイルを走査している

監査は毎ラウンド「なぜここは deny ではなく通すのか」を再提起した（v0.9.32 / v0.9.33 の
双方で A-01 相当・§6.1）。個別に場当たり対応すると同じ議論を繰り返すため、方針を 1 つの
決定として記録する。

### 決定

**検査対象が確定していない状態での失敗は fail-open（exit 0）にする。** 検査対象が確定した
後（「これは git commit である」「これは保護対象ファイルへの書き込みである」）の失敗は
fail-closed（exit 2）にする。

各経路への適用:

- **stdin 読み取り**（A-01）: syscall 例外を `(OSError, ValueError, AttributeError)` で
  捕捉し「入力なし」へ正規化する。stdin が読めない時点でツール呼び出しの中身自体が不明で
  あり、fail-closed にすると復旧目的の Bash 実行（hook 自体を直す変更）ごと巻き込む。
  **この正規化は後に ADR-0019 が分割した** — 「入力が無い」と「読めなかった」は証拠の質が
  違う
- **Python 3.12 未満**: hook を実行せず stderr に構造化理由を書いて exit 0。ランタイムは
  venv も install スクリプトも持たないため、インストール時に対応 Python を検証する経路が
  無い
- **commit と無関係な malformed Bash 呼び出し**: raw 文字列上に `git commit` らしき痕跡が
  無ければ exit 0。matcher が Bash 全体である以上、malformed というだけで一律 deny すると
  無関係な呼び出しまで巻き込む
- **secret scanner の自己バジェット**（`SecretScanBudgetExceeded`）: これは逆に
  fail-closed の例。検査対象（commit 対象ファイル）は既に確定しているため、実時間バジェット
  超過は scan_error（severity error）として deny する。ホストの hook timeout に達して外側
  から強制終了されるより、内側で先に制御された失敗にする方が安全に倒せる

### 検討した代替案

#### 代替案 1: 全面 fail-closed（読めない・確定できない場合は exit 2）

- 長所: 「検査をスキップして通す」経路が消え、監査ツール視点での抜け穴が無くなる
- 短所: stdin リダイレクト漏れや Python バージョン不一致は利用者環境側の問題であることが
  多く、fail-closed にすると全 Edit/Bash が拒否されてセッションが即死する。Python 3.12
  未満では復旧手段（hook を直すための Bash 実行）ごと塞がれ、セッション内から復旧不能になる
- 却下理由: 「保護が効かない」より「作業が一切できなくなる」方が被害が大きい。fail-open は
  softer failure として意図的に選んでいる

#### 代替案 2: stdin 読み取り失敗時だけ deny、他は fail-open のまま

- 長所: A-01 に対してだけ厳格化でき、変更範囲が小さい
- 短所: stdin 読み取り失敗は「検査対象が確定していない」点で Python バージョン不一致や
  malformed Bash 呼び出しと同じ状況であり、経路ごとに割れると一貫性が無く、次のラウンドで
  「なぜここだけ違うのか」が再提起される
- 却下理由: 個別対応より「検査対象確定前 = fail-open」という一貫した境界線の方が、同種の
  指摘の再提起を防げる

> **ADR-0019 による後日の分割**: この却下は「stdin 読み取り失敗」を 1 つの事象として
> 扱っていた。実際には「入力が無い」と「読めなかった」で証拠の質が違い、後者だけは
> fail-closed が正しい。代替案 2 が退けたのは*経路ごとの場当たり*であって、*証拠の質に
> よる分割*ではない。

### 結果

**肯定的** — 4 フックが同じ exit 契約に揃い、stdin 読み取り失敗時のフック間非一貫
（exit 0 と exit 2 の混在）が解消した。復旧不能な自己ロックのリスクが残らない。

**否定的** — stdin リダイレクト漏れや Python バージョン不一致が起きている間、保護フックは
実質無効化される。利用者が気づく手段は stderr の `claqProtectionDisabled` 警告のみで、
監視していない環境では見落としうる。

**リスク** — fail-open の窓が敵対的に使われた場合（意図的に stdin を閉じる、意図的に古い
Python を PATH に置く）、保護は機能しない。共通原則の脅威モデル前提を維持する限り、これは
許容するリスクとして記録する。

---

## ADR-0003: detached background（`--bg`）の親 exit は「起動受付」であり「処理成功」ではない

**日付**: 2026-08-18  **ステータス**: accepted

### コンテキスト

`launcher.py --bg` は `hook_common.detach_process` で子を `start_new_session=True` で起動
し、親はすぐ終了する。主用途は SessionEnd の `mem.cli handoff` で、ハーネスの hook timeout
に阻まれず non-blocking に終了することが目的。

監査は複数ラウンドで同じ観測を報告した（v0.9.32 §6.2、v0.9.33 A-05）。
`python3 launcher.py --bg nonexistent.module` は `parent exit=0 / stdout= / stderr=` になる。
これは実装の欠陥ではなく `detach_process` の docstring が明記する意図的な非同期契約だが、
「エラーがあったのに exit 0」という表面だけを見ると欠陥に見えるため、ラウンドを跨いで
同じ指摘が繰り返される。

### 決定

**`--bg` 起動の親プロセスの exit code は「子プロセスの起動を受け付けたか」だけを表し、
「子プロセスの処理が成功したか」は表さない。** この契約は変更しない。

その上で処理結果を完全に見えなくはせず、次回セッションで観測可能にする:

- `detach_process` は子の stdout/stderr を `~/.claq/logs/bg-YYYY-MM-DD.log` へ追記する
- 子は `_WATCHDOG_SCRIPT` でラップされ、`DETACH_TIMEOUT_SECONDS`（600 秒）でハングしても
  確実に回収される
- 次回 SessionStart の `mem context` で `recent_bg_failure_notice()` が前回ログを検査し、
  内容があれば 1 行だけ通知する（失敗時のみ出力するため出力トークン最小化と両立する）

### 検討した代替案

#### 代替案 1: 親を子の完了まで待たせ、子の exit code をそのまま返す

- 長所: 呼び出し側が同期的に成功/失敗を知れる
- 短所: `--bg` を使う理由そのもの（hook timeout に阻まれず non-blocking に終了する）が
  失われる。SessionEnd が同期処理に戻り、handoff 自体が timeout で失敗するリスクが再燃する
- 却下理由: 目的と手段が矛盾する

#### 代替案 2: 起動受付 ID を発行し、ポーリング API で状態を問い合わせる

- 長所: 呼び出し側が任意のタイミングで成功/失敗を正確に把握できる
- 短所: ID の永続化・状態遷移管理・ポーリング API という可変状態が増える。データモデルは
  `repos`/`knowledge`/`sessions` の 3 テーブルのみという単純さを設計原則としており、非同期
  ジョブの状態管理を持ち込むと原則に反する
- 却下理由: 「次回 SessionStart で failure ログの痕跡を通知する」既存の軽量な仕組みで
  「見落とさない」目的は達成できており、追加の複雑さに見合わない

### 結果

**肯定的** — SessionEnd の handoff は引き続き non-blocking で hook timeout に阻まれない。
失敗の痕跡は次回セッション開始時に 1 行として提示され、「起動受付成功」と「処理成功」の
混同による見落としを緩和する。

**否定的** — 親を直接呼ぶ経路（テスト・手動実行）では、その場で成功/失敗を知れない。

**リスク** — `recent_bg_failure_notice` は当日＋前日の 2 ファイルまでしか読まない
（SessionStart の hook timeout との兼ね合い）。2 日以上前の失敗ログや、次回 SessionStart
自体が実行されないセッションでの失敗は通知されずに埋もれる。

---

## ADR-0019: 保護フックは stdin を「読めなかった」場合に fail-closed する

（「入力が無い」場合は従来どおり素通り）

**日付**: 2026-09-04  **ステータス**: accepted

### コンテキスト

Windows 実機検証（`docs/reports/claq-release-verify-windows-2026-09-03.md` P1-004）で、
実体の Python 3.14.7 から `launcher.py` を直接起動し、リダイレクトした stdin へ UTF-8 JSON
を渡した結果:

| Payload | 実行結果 |
|---|---|
| `{"tool_name":"Bash","tool_input":{"command":"git status"}}` | exit 0 |
| `{"tool_name":"Bash","tool_input":{"command":"git commit --no-verify -m x"}}` | exit 0 |

後者は `block_no_verify` が deny すべき payload である。allow と deny が同一の exit 0 に
なったということは、フックが payload を**1 バイトも読んでいない**ことを意味する。

原因は `_stdin_ready()` が `select.select([sys.stdin], [], [], timeout)` を使っていたこと。
Windows の `select` は **socket にしか使えず**、通常のパイプに対しては `OSError` を投げる。
その例外を「入力なし」として握り潰していたため読み取りは一度も行われず、`main()` の
`if not raw: return 0` へ落ちていた。すなわち**保護フックが構造的に無効**だった。

ADR-0001 をそのまま当てはめると fail-open が正当に見えるが、ADR-0001 が扱うのは
「**受け取ったコマンドを解析し切れない**」場合であり、「**そもそもコマンドを受け取れて
いない**」場合ではない。後者を同じ扱いにすると、入力経路が壊れているだけで保護が全面的に
無効化され、しかもその事実が exit 0 として成功と report される。

### 決定

**stdin の状態を 2 つに分け、保護フックは後者だけを fail-closed にする。**

1. **payload が無い**（`sys.stdin is None` / TTY 接続 / 即 EOF で 0 バイト）→ 従来どおり
   空文字列として扱い exit 0 で素通りさせる（F-01 の契約を維持）
2. **payload を読めなかった**（読み取りが例外化した／最初のバイトが
   `STDIN_FIRST_BYTE_TIMEOUT` 以内に届かなかった）→ `StdinUnavailableError` を送出し、
   4 つの保護フックは deny（exit 2 + `permissionDecision: deny`）に倒す

あわせて readiness 判定から `select` を撤去する。ブロッキング read を daemon スレッドへ
隔離し、本スレッドは `queue.Queue.get(timeout=...)` で待つ。これは 3 プラットフォームで
同一の 1 本のロジックであり、OS 判定もプラットフォーム分岐も持たない（Windows 専用の
フォールバック実装ではない）。

非保護経路（`launcher --bg` の stdin 中継、`mem.cli` の payload 読み取り）は
`read_raw_stdin()` を使い、例外を空文字列へ正規化したまま据え置く。これらは「読めなかった」
ときに採れる別の行動が無いため、区別しても意味がない。

### 根拠

- **1 と 2 は証拠の質が違う。** 「即 EOF」は「ホストが payload を渡していない」と観測上
  区別できず、payload を渡さないホストで deny に倒すと全ツール呼び出しが拒否され、復旧手段
  （Bash）ごと塞がれる。一方「読み取りが例外化した」「最初のバイトが届かない」は、**入力
  経路が存在するのに取り出せなかった**という積極的な証拠であり、判定不能を許可へ倒す理由に
  ならない
- **[ADR-0002](02-shell-analysis-boundary.md) の優先順位と整合する。** 読めなかった payload
  を通すのは誤通過そのものである
- **ADR-0001 と矛盾しない。** 両者は排他的な事象で、`parse_json_object` が `None` を返す
  経路（＝受け取ったが読めない JSON）の扱いは従来どおり各フックの判断に委ねる
- **`select` の撤去自体はプラットフォーム非依存の改善。** 旧実装は「readiness を OS に
  問い合わせる」ために POSIX 固有 API を使っていたが、必要なのは「タイムアウト付きで読む」
  ことだけである

### 検討した代替案

#### 代替案 1: 空入力も含めてすべて fail-closed にする

Windows 検証レポートの受入条件表は「空入力、壊れた JSON、Windows pipe 読取例外を許可側へ
倒さない」と書いており、空入力も deny を求めている。

- 長所: 受入条件表をそのまま満たす
- 短所: payload を渡さないホストが 1 つでもあれば、そのホストでは全 Bash/Edit が拒否され、
  設定を直す手段ごとセッション内から失われる。この失敗モードは実測で確認されておらず、
  被害はフックの無効化より大きい
- 却下理由: 「入力が無い」ことは、ホストの仕様なのか経路の故障なのかを観測で区別できない。
  区別できない事象に対して復旧不能な副作用を持つ側へ倒すのは、`launcher.py` の未対応
  Python 時 fail-open と同じ理由で採らない

#### 代替案 2: Windows だけ overlapped I/O（`_winapi` / ctypes）で読む

- 長所: OS ネイティブの非同期 I/O を使える
- 短所: Windows 専用の分岐が増え、macOS/Linux では 1 行も実行されない（カバレッジ 100% の
  要件に対しても不利）。得られるものは「タイムアウト付きの読み取り」だけで、それはスレッド
  + キューで OS 非依存に実現できる
- 却下理由: ユーザー要件「なるべくフォールバックのような実装にはせず、3 OS 共通のロジック
  とする」に反する。共通で書ける以上、共通で書く

#### 代替案 3: `select` を残し、例外時だけ別経路にする

- 長所: 変更が小さい
- 短所: POSIX 経路と Windows 経路で「読めたかどうか」の判定軸が 2 本に割れ、片方だけ強化
  される非対称（`INPUT_CONTAINER_KEYS` で実際に起きた失敗と同型）を再生産する
- 却下理由: 判定軸は 1 本に保つ

### 影響

- `read_raw_stdin_with_truncation()` は `StdinUnavailableError` を送出しうる（従来は例外を
  送出しない契約だった）。保護フック 4 つはこれを捕捉し、`stdin_unreadable_message()` の
  共通文面で deny する
- `_stdin_ready()` は `_stdin_is_absent()` へ置き換えた。TTY / `sys.stdin is None` の判定
  だけを担い、readiness は判定しない
- `isatty()` が例外化する stdin は「無い」と決めつけず、読み取り側の判断へ委ねる
- `STDIN_FIRST_BYTE_TIMEOUT` を 1.0 秒から 2.0 秒へ広げた。`hooks.json` の保護エントリ
  timeout も 5 秒から 15 秒へ広げ、Windows の wrapper + インタプリタ起動の遅さで host 側
  timeout（＝ホストによっては fail-open）へ入りにくくする

---

## ADR-0024: 走査コストは保護のバイパスとして扱い、実測できない修正は残存として書く

**日付**: 2026-09-07  **ステータス**: accepted

### コンテキスト

2026-09-07 の hooks 監査で、**実測して初めて確定した保護の完全バイパスが 4 件**見つかった。
いずれも静的レビューでは「未確認」「MEDIUM」に留まっていたもので、実行して数値を取った
ことで severity が上がった。

1. **`\` + 改行（行継続）** — シェルは `\` と改行の両方を消して行を連結するが、
   `_strip_line_comments` / `_replace_unquoted_newlines` はエスケープ済みの 1 文字をそのまま
   出力するため両方が残る。残ったまま `shlex(posix=True)` へ渡すと whitespace 状態の `\` が
   escape 状態へ遷移して改行を次トークンの先頭文字として吸収し、継続行が 0 桁目から始まる
   ときにその先頭語が別トークンへ化ける。実測で 4 経路が exit 0 で素通りした
   （`block_no_verify` の `--no-verify` / `-n`、`pre_bash_commit_quality` の commit 判定、
   `bash_config_protection` のリダイレクト先判定）。**「継続行が 0 桁目から始まるか」だけで
   保護の有無が反転していた。**
2. **走査コストによる timeout 誘発** — `block_no_verify` は git トークンごとに残りセグメント
   を再走査するため O(N²)。実測で 24,000 トークン（96KB = `MAX_STDIN_BYTES` の**9.2%**）が
   15.23 秒かかり、`hooks.json` の timeout（15 秒）を超えた
3. **`sh -c` 再帰への予算漏れ** — 2 の修正を entry loop だけに置いたところ、`sh -c '<巨大>'`
   は外側 3 トークンなので検査を通過し、再帰先が無予算で走って 16.23 秒。**修正がネスト経由
   でそのまま迂回できた。**
4. **`git` のグローバルオプション** — `_segment_mutates_worktree_or_index` が git トークン
   直後の最初の非 `-` トークンでサブコマンドを決めるため、`git -C . add x && git commit` の
   `.` がサブコマンドと読まれ、mutation-before-commit ガードが不発（実測 exit 0）

2・3 が示すのは、ADR-0001 が受容した fail-open の**外側**にある経路である。ADR-0001 は
「環境が壊れている」ケースを想定していたが、ここで起きているのは**入力が検査時間を支配し、
攻撃者が timeout を任意に誘発できる**ケースである。

一方、Windows 側には**実測できない**非対称が残っている。`claq-hook.cmd` は
`if defined CLAQ_PYTHON goto :claq_override` を PATH 検査より前に置くため、絶対パスの
`CLAQ_PYTHON` を設定すると PATH 検査が丸ごと飛ぶ。POSIX 側は逆順で、絶対パスの
`CLAQ_PYTHON` でも `path_not_absolute` から回復しない。POSIX 側が正しい — launcher とその
子プロセス（`git` を含む）は汚染された PATH で解決を続けるのであって、`CLAQ_PYTHON` は
それを直さないからである。しかし cmd.exe は開発ホスト（darwin）で実行できない。

### 決定

1. **入力が走査時間を支配する経路は、fail-open ではなく fail-closed にする。**
   `MAX_COMMAND_TOKENS`（5,000）を超えるコマンドは走査せず BLOCKED を返す。ADR-0001 の
   fail-open は「環境が壊れて検査できない」ケースに限り、「攻撃者が検査を完走させない」
   ケースには適用しない。両者は「検査が完了しない」点で同じに見えるが、後者は入力側が任意に
   誘発できるため、fail-open にすると保護がオプトアウト可能になる
2. **予算・サニタイズ・正規化のガードは、再帰境界にも同じものを置く。** entry point だけに
   置いたガードは `sh -c` 1 段で迂回できる（実測 16.23 秒）。1 段の `sh -c` は
   [ADR-0002](02-shell-analysis-boundary.md) が対応範囲と明記した経路である
3. **deny の終了コードは出力層の失敗から独立させる。** `emit_block_output` の内側で出力例外
   を捕まえ、exit code は必ず返す。PreToolUse では exit 1 が non-blocking error ＝ ツール実行
   なので、`write_stdout` の BrokenPipeError が deny を allow へ反転させる。誘因（host が
   パイプを閉じる）は host の timeout と同時に起きるため、最も守りたい局面でちょうど外れて
   いた
4. **実測できない修正は、適用せず残存として書く。** `claq-hook.cmd` の検査順は POSIX 側へ
   揃えるのが正しいが、cmd.exe を開発ホストから実行できず、壊れた並べ替えは Windows で保護を
   黙って無効化する。`KNOWN RESIDUAL` として `.cmd` 本文とテストのコメント両方へ書き、
   `CLAUDE.md` にも非対称を明記する（`feedback-cross-platform-common-logic-first` の
   「無理なら制限を明記」の適用。同じ姿勢の一般化は
   [06-staleness-detection.md](06-staleness-detection.md) を参照）

### 検討した代替案

#### 代替案 1: トークン上限を設けず、走査を線形へ書き換える

- 長所: 正当な入力を 1 つも拒まない。上限値の調整が要らない
- 短所: `parse_git_segment` は git トークンごとに新しい git 起動としての解釈を要求するため、
  線形化は解析の意味論そのものを変える。回帰が静的に読み切れない
- 却下理由: 5,000 トークンのシェルコマンドは実務上存在せず（実測 8,000 トークンで 1.66 秒
  ＝ timeout の 1/9）、上限で得る確実性のほうが大きい

#### 代替案 2: 上限超過を fail-open（allow）にする

- 長所: ADR-0001 の文面に素直
- 短所: 攻撃者が上限を超える入力を作るだけで保護を切れる。ADR-0001 が想定した「環境が壊れて
  いる」とは、誘発可能性がまったく違う
- 却下理由: 同ファイルが stdin 上限超過に対して既に fail-closed（`_TRUNCATED_INPUT_MESSAGE`）
  を採っており、そちらと整合しない

#### 代替案 3: `bash_config_protection` にもトークン予算を入れて 3 フックで揃える

- 長所: 非対称が消える。[ADR-0021](02-shell-analysis-boundary.md) は「揃っていない実装が
  後続の読み手に取りこぼしと誤読される」危険を記録している
- 短所: 実測では線形（50,000 トークンで 0.07〜0.67 秒）で、予算が発火する条件が無い。発火
  しないガードは、いずれ「効いている」と誤認される
- 却下理由: 揃えるのではなく、**線形なので不要**である理由をコードのコメントとして残した

#### 代替案 4: `claq-hook.cmd` の検査順を POSIX 側へ揃える

- 長所: 非対称が消え、汚染された PATH で子プロセスが動く経路が塞がる
- 短所: cmd.exe を darwin から実行できない。静的なテキスト検査しか通せず、壊れた並べ替えは
  「Windows で保護が黙って無効」という最悪の失敗へ倒れる
- 却下理由: 未検証の編集より、文書化された残存のほうが安全。`.cmd` の KNOWN RESIDUAL・
  テストのコメント・`CLAUDE.md` の 3 箇所へ書いたので、Windows ホストを持つ者が実測したうえで
  直せる。**この却下は「直さない」ではなく「実測できる者が直す」である。**

#### 代替案 5〔採択〕: `config_protection` の保護対象へ `hooks.json` とホストの `settings.json` を加える

**当初は「`/harness --apply` と両立しない」として却下したが、精査の結果その判断が誤りである
ことが分かったため採択へ改めた（2026-09-07、利用者の承認済み）。**

保護フック自身の設定は保護対象外で、1 回の Edit で PreToolUse ガード 4 種すべてを無効化
できた。`config_protection` の脅威モデルは「エージェントが、自分の作業を通すためにガード
レール側を緩める」であり、同じ動機で最も効く緩め方が対象外だった。`.git/hooks` を保護対象へ
加えた理由（「片側だけ塞ぐと、塞いだ経路の存在が誤った安心になる」）がそのまま当てはまる。

却下時の理由「`/harness --apply` の編集先そのものなので保護すると成立しない」は、実装を
読み直すと**粗すぎた**:

- `config_protection` の判定は**basename 一致**であり、`"hooks.json"` を素で足すとプラグイン
  自身の `plugins/claq/hooks/hooks.json` と consumer の `.claude/hooks.json` を区別できない。
  「両立しない」ように見えたのはこの basename 衝突が原因で、`.git/hooks` 用に既にある
  **パス連続一致**（`_PROTECTED_PATH_SEGMENTS`）を使えば分離できる
- `harness_audit` が provider モードで `hooks/hooks.json` を指す check は 5 件すべてが存在・
  整合チェックで、fix は**追加**であって弱化ではない
- consumer モードで `.claude/settings.json` を指すのは 2 件だけで、fix は初回セットアップ
  1 回きりの行動

さらに実装直前の再検討で**当初案のもう 1 つの欠陥**が見つかった。`settings.json` の条件を
「`env` キーを触る場合」にすると、`update-config` skill の中心的な仕事（`set DEBUG=true`）と
`.vscode/settings.json` の `terminal.integrated.env.*` を巻き込む。守れるものが増えないのに
頻繁な正当作業を壊すため、条件を **`CLAQ_PYTHON` の出現そのものと `hooks` /
`enabledPlugins` / `disabledPlugins` の 3 キーだけ**に絞った。

採択した実装:

1. `plugins/claq/hooks/hooks.json` を**パス連続一致**（`("hooks", "hooks.json")`）で無条件
   保護する。consumer の `.claude/hooks.json`（親が `.claude`）には当たらない
2. `settings.json` / `settings.local.json` を**条件付き保護**へ加え、`CLAQ_PYTHON` の出現・
   `hooks` / `enabledPlugins` / `disabledPlugins` のときだけ deny する。とくに `CLAQ_PYTHON`
   は `runtime/claq-hook` が exec するため、保護の無効化ではなく**全ツール呼び出しでの任意
   コード実行**の入口である
3. ブロックメッセージの「正当な変更ならフックを一時的に無効化せよ」を「必要ならユーザーに
   依頼せよ。フックを自分で無効化するな」へ改める。**止めた直後に「代わりに検査ごと止めて
   いい」と言えば、止めた意味が消える。** 加えて 1 を入れるとこの指示は循環する

残るコスト: `/harness --apply` の `consumer-hook-guardrails` fix（初回セットアップ 1 回）と、
claq 自身の開発で `hooks.json` を触る作業が deny される。どちらも deny メッセージが
「ユーザーに依頼せよ」と案内する。

**この保護は決定的ではない。** `python3 -c "open('.claude/settings.json','w').write(...)"` は
書込み先がリダイレクトでも既知エディタでもないため、おそらく素通りする。守っているのは
`config_protection` が元から守っている層 —— 素直なエージェントが近道を取る経路と、低コストな
注入 —— であって、決意した攻撃者ではない。

#### 代替案 6: `_ScanBudget` を「走査単位数」でも積算する

`check(scanned_bytes)` は空行（`scanned_bytes == 0`）で残量を減らさないため、初回以降は
deadline を読まない。

- 長所: docstring が「行数基準では走査開始前の 1 回しか確認されない」として退けた欠陥の
  鏡像が消える
- 短所: 残量が減らないのは空行のときだけで、空行の走査自体が定数時間。実質的な取りこぼし
  区間が存在しない
- 却下理由: 実害の無い対称性のための変更で、`_ScanBudget` の docstring が説明する設計を
  読みにくくする

#### 代替案 7: `git` の起動を `shutil.which` で絶対パス解決する

Windows の `CreateProcess`（`lpApplicationName=NULL`）は PATH より先にカレントディレクトリを
探すため、リポジトリ同梱の `git.exe` が hook 権限で起動しうる。

- 長所: POSIX 側が PATH 洗浄まで責務を広げているのに Windows 側に同じ担保が無い、という
  非対称が消える
- 短所: 実装では argv[0] が絶対パスに変わり、既存のテストスタブ 6 箇所が argv[0] を `"git"`
  として照合しているため一斉に壊れる（実際に試して 7 件が赤になった）。argv を変えずに
  `executable=` を渡す形なら壊れないが、`subprocess.run` を直接呼ぶ箇所と `run_text` 経由の
  箇所が混在しており、片方だけ直すと新しい非対称になる
- 却下理由: darwin から Windows の挙動を実測できず、代替案 4 と同じ理由で未検証の編集を避けた。
  残存として記録する。優先度は代替案 5 より低い — 攻撃者は既に同一 UID でコマンドを実行できる
  立場にあり、得るのは hook の git 問い合わせを偽装する能力に限られる

### 結果

**肯定的**

- 実測で 4 件の完全バイパスが塞がった。いずれも静的レビューでは「未確認」または MEDIUM に
  留まっていたもので、**実行して数値を取ることが severity の確定に必要だった**
- 「host timeout は fail-open と等価」が規約として明文化され、ADR-0001 の fail-open が適用
  される範囲（環境の故障）とされない範囲（入力が誘発する検査不能）が分かれた
- `launcher.py` の「全フックは SystemExit 経由で終了する」不変条件に検知器が付いた
  （[ADR-0011 決定 6](05-definition-and-subagent-design.md) / [ADR-0023](06-staleness-detection.md) の適用）
- `CLAUDE.md` に `claq_python_not_absolute` / `path_has_empty_entry` / `path_not_absolute` の
  3 理由コードと、POSIX / Windows の検査順の非対称が明記された

**否定的**

- `MAX_COMMAND_TOKENS = 5000` は実測に基づく閾値だが、遅いホストでの余裕は測っていない。
  darwin で 8,000 トークン 1.66 秒という 1 点からの外挿である
- Windows 側に 2 つの未検証残存（検査順、`git` の CWD 解決）が残った

**リスク**

- 決定 1 の fail-closed は、5,000 トークンを超える正当なコマンドを拒む。実務上そのような
  コマンドは無いと判断したが、生成されたスクリプトを 1 行で流す使い方があれば衝突する。その
  場合は上限値を上げるのではなく、コマンドを分割する側を正とする
- 代替案 5 の採択により、`/harness --apply` の consumer 向け fix 1 件と、claq 自身の開発での
  `hooks.json` 編集が deny される。どちらも「ユーザーに依頼する」へ倒れるので復旧不能では
  ないが、摩擦は増える
- 代替案 5 の保護は素直なエージェントの近道と低コストな注入にしか効かない。**「hooks.json は
  守られている」と読むのは誤りで、正しくは「Edit/Write と Bash のリダイレクト経路では守られて
  いる」である。**
