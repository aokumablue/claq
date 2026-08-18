# ADR-0001: フックは検査を完走できないとき fail-open にする

**日付**: 2026-08-18  **ステータス**: accepted

## コンテキスト

bluecore の保護フック（`block_no_verify` / `pre_bash_commit_quality` /
`bash_config_protection` / `config_protection`）は、Bash/Edit/Write の
実行前に検査を行い、問題があれば exit 2 で deny する。ただし検査そのものが
完走できない状況が複数の経路で起こりうる:

- stdin が時間内に届かない、または読み取り syscall（`isatty`/`select`/
  `read1`）が失敗する（A-01）。
- ホストの Python が 3.12 未満で、bluecore モジュール自体が import できない
  （`launcher.py:52-58`、前ラウンド §6.1 で再提起）。
- `pre_bash_commit_quality` の matcher は Bash 呼び出し全体（`git commit` と
  無関係な呼び出しを含む）にアンカーされており、malformed JSON が commit
  以外の Bash 呼び出しに対しても届く。
- secret scanner がホストの hook timeout に達しうる規模のファイルを走査
  している。

これらはいずれも「検査を完走できない」という同じ形の状況であり、監査は
毎ラウンド「なぜここは deny ではなく通すのか」を指摘として再提起する
（v0.9.32 検証・v0.9.33 検証の双方で A-01 相当・§6.1 が挙がった）。個別に
場当たり対応すると同じ議論を繰り返すため、方針を 1 つの決定として記録する。

## 決定

**検査対象が確定していない状態での失敗は fail-open（exit 0）にする。**
検査対象が確定した後（「これは git commit である」「これは保護対象ファイル
への書き込みである」等が判明した後）の失敗は fail-closed（exit 2）にする。

この境界線を各経路に適用する:

- **stdin 読み取り**（A-01）: `hook_common._stdin_ready`/`_read_stdin_bytes`
  は syscall 例外を `(OSError, ValueError, AttributeError)` で捕捉し「入力
  なし」に正規化する（本ラウンドで契約を実装強制。空入力は各フックの
  「空 raw」分岐へ収束し、全フックが同じ exit 0 に揃う）。これは stdin が
  読めない時点でツール呼び出しの中身自体が不明であり、fail-closed にすると
  復旧目的の Bash 実行（例: hook 自体を直す変更）ごと巻き込むため。
- **Python 3.12 未満**（§6.1、`launcher.py:52-58`）: hook を実行せず stderr
  に構造化理由を書いて exit 0。ランタイムは venv も install スクリプトも
  持たないため、インストール時に対応 Python を検証する経路が無い。
- **commit と無関係な malformed Bash 呼び出し**
  （`pre_bash_commit_quality`）: raw 文字列上に `git commit` らしき痕跡が
  無ければ exit 0。matcher が Bash 全体である以上、malformed というだけで
  一律 deny すると無関係な呼び出しまで巻き込む。
- **secret scanner の自己バジェット**（`commit_quality_scanner
  .SecretScanBudgetExceeded`）: これは逆に fail-closed の例。検査対象
  （commit 対象ファイル）は既に確定しているため、実時間バジェットを超えたら
  scan_error（severity error）として deny する。ホストの hook timeout に
  達して外側から強制終了されるより、内側で先に制御された失敗にする方が
  安全に倒せる。バイナリ判定されたファイルは secret scan 自体をスキップし
  severity warning の痕跡を残す（error にはしない — 画像等の commit を
  一律ブロックすると明示要件に反する）。NUL バイトを混ぜて secret 混入を
  隠すバイナリ偽装は、この skip の副作用として受容するリスクであり、
  積極的に防ぐ設計目標ではない。

## 検討した代替案

### 代替案 1: 全面 fail-closed（読めない・確定できない場合は exit 2）

- 長所: 「検査をスキップして通す」経路が一切無くなり、監査ツール視点での
  抜け穴が消える。
- 短所: stdin リダイレクト漏れや Python バージョン不一致は利用者環境側の
  問題であることが多く、fail-closed にすると全 Edit/Bash が拒否されて
  セッションが即死する。Python 3.12 未満の場合は復旧手段（hook 自体を
  直すための Bash 実行）ごと `bash_config_protection`/`block_no_verify` に
  塞がれ、セッション内から復旧不能になる。
- 却下理由: 「保護が効かない」より「作業が一切できなくなる」方が被害が
  大きい。fail-open は softer failure として意図的に選んでいる。

### 代替案 2: stdin 読み取り失敗時だけ deny、他は fail-open のまま

- 長所: A-01 の指摘に対してだけ厳格化でき、変更範囲が小さい。
- 短所: stdin 読み取り失敗は「検査対象が確定していない」という点で
  Python バージョン不一致や malformed Bash 呼び出しと本質的に同じ状況
  であり、経路ごとに fail-open/fail-closed が割れると一貫性が無くなり、
  次のラウンドで「なぜここだけ違うのか」が再度指摘される。
- 却下理由: 個別対応ではなく「検査対象確定前 = fail-open」という一貫した
  境界線を立てる方が、同種の指摘の再提起を防げる。

## 結果

### 肯定的

- 4 つのフック（`block_no_verify`/`pre_bash_commit_quality`/
  `bash_config_protection`/`config_protection`）が同じ exit 契約に揃い、
  stdin 読み取り失敗時のフック間の非一貫（exit 0 と exit 2 が混在していた
  A-01 の波及）が解消した。
- 復旧不能な自己ロックのリスクが残らない。

### 否定的

- stdin リダイレクト漏れや Python バージョン不一致が起きている間、保護
  フックは実質無効化される。利用者側がこの状態に気づく手段は stderr の
  警告行（`bluecoreProtectionDisabled` 等）のみで、監視していない環境では
  見落としうる。

### リスク

- fail-open の窓が悪用可能な形（意図的に stdin を閉じる、意図的に古い
  Python を PATH に置く等）で敵対的に使われた場合、保護は機能しない。
  本フック群は「敵対的回避への防壁」ではなく「うっかり事故の抑止」である
  という前提（ADR-0002 と同根拠）を維持する限り、これは許容するリスクと
  して記録する。
