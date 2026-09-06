# ADR-0021: 実行位置の特定は wrapper allowlist で行い、git 起動の探索は全トークン走査のままにする

**日付**: 2026-09-06  **ステータス**: accepted

## コンテキスト

Bash 系の保護フック 2 つは、コマンド文字列のどこを見るかで正反対の方針を採っている。

`block_no_verify` はモジュール docstring で方針を明記している —— 「ラッパー名
（`env` / `sudo` / `nice` …）のホワイトリスト方式は将来のラッパーを取りこぼすため
採らず、**トークンの basename が `git`** なら git 起動とみなす」。セグメント先頭を
前提にすると `sudo git commit --no-verify` / `xargs -I% git ...` が素通りするためである。

`bash_config_protection` は逆に `_COMMAND_POSITION_WRAPPERS` という allowlist を持ち、
`env` / `sudo` / `timeout` / `nice` / `nohup` / `stdbuf` / `xargs` 等を読み飛ばして
「実際に実行される executable の index」を求める（`_command_index`）。

同じリポジトリの兄弟フックで方針が割れているため、片方の判断がもう片方へ「未修正の
取りこぼし」として流用されかねない。実際 `_COMMAND_POSITION_WRAPPERS` は
release-verify のたびに要素が追加されており、allowlist が原理的な弱点だという読みを
誘っている。

## 決定

この非対称は意図的なものとして維持する。両フックは**必要としている判定が違う**。

`block_no_verify` が要るのは「このコマンドで git が起動されるか」だけで、git が
何番目の語かは結論に影響しない。だから名前集合を持たずに済み、持たないほうが強い。

`bash_config_protection` は実行位置の特定そのものを必要とする。M-01 が要求するのは
`tee pyproject.toml`（deny）と `echo tee pyproject.toml`（allow）の区別だが、この 2 つは
構文上まったく同じ「語 → 語 → パス」であり、分けるのは `echo` と `timeout` の**意味論**
であって構文ではない。したがってここでは何らかの名前集合が原理的に不可避で、選べるのは
「wrapper を列挙する」か「データを取るコマンドを列挙して残り全部を wrapper とみなす」かの
2 択しかない。前者（allowlist）を採る。

allowlist の唯一の一般化として、wrapper を 1 つ以上通過した後に限り、数字で始まる
非オプション語を wrapper 自身の positional 値として読み飛ばす（`timeout 5 rm` の `5`、
`nice -n 5 rm` のレベル）。実行ファイル名が数字で始まることはまず無いためである。

## 検討した代替案

### 代替案 1: `block_no_verify` の全トークン走査を `bash_config_protection` へ転用する

- 長所: 方針が 1 つに揃い、未知の wrapper を取りこぼさない。
- 短所: 実行位置の情報がそもそも生まれないため、M-01 の false positive が丸ごと戻る。
  `echo tee pyproject.toml` / `echo timeout 5 rm ruff.toml` のような**言及**が deny になる。
- 却下理由: `block_no_verify` が全トークン走査で足りるのは実行位置を必要としないから
  であって、方式が優れているからではない。必要としている判定が違う以上、転用できない。

### 代替案 2: データを取るコマンドを列挙し、残りを一律 wrapper とみなす（fail-closed 側）

- 長所: ADR-0002 の「誤検出 > 誤通過」に姿勢が合い、未知の wrapper を取りこぼさない。
- 短所: 実測（v0.9.52、`_command_index` を差し替えて計測）で 2 件の実害が出た。

  | コマンド | 現行 | fail-closed 版 |
  |---|---|---|
  | `pushd sub && printf x > ../ruff.toml` | exit 2 | **exit 0** |
  | `cd sub && printf x > ../ruff.toml` | exit 2 | **exit 0** |
  | `grep -rn tee ruff.toml` | exit 0 | **exit 2** |

  1 行目・2 行目は `pushd` / `cd` を wrapper として消費してしまうため
  `_changes_working_directory` が False を返し、repo スコープ判定が復活して
  `../ruff.toml` がルート外と判定される。ADR-0018 が置いた「cwd を動かすコマンドが
  あれば無条件 deny」という分岐が丸ごと失われる。3 行目は読み取り専用コマンドの
  引数を書き込み先と読む false positive。
- 却下理由: fail-closed へ寄せたつもりで **ADR-0018 の deny 分岐を失っている**。
  Bash は最も広い matcher であり可用性の代償が最も大きい経路でもあるため、
  「有界な取りこぼし」を「無界な誤検出 + 既存 deny 分岐の喪失」より選ぶ。

### 代替案 3: `block_no_verify` 側を allowlist へ揃える

- 長所: 方針が 1 つに揃う。
- 短所: 取りこぼしのコストが非対称。`bash_config_protection` の取りこぼしは
  「うっかり書き換えを 1 件見逃す」だが、`block_no_verify` の取りこぼしは
  `sudo git commit --no-verify` の素通り —— フックバイパスそのものである。
- 却下理由: 実行位置を必要としない側へ名前集合を導入しても、得るものが無く失うものだけがある。

## 結果

### 肯定的

- M-01 の false positive（非実行位置での言及）と ADR-0018 の無条件 deny 分岐が両立する。
- 「なぜ兄弟と違うのか」がコードコメントではなく決定記録として残り、片方へ揃える
  変更が「未修正の取りこぼしの修正」に見えなくなる。

### 否定的

- `_COMMAND_POSITION_WRAPPERS` に載っていない wrapper（`chrt` 相当の新顔）は取りこぼす。
  これは承知のうえの境界であり、numeric positional の消費だけが唯一の一般化である。
- 語彙が増え続ける形になり、release-verify のたびに追記されうる。

### リスク

- 兄弟の非対称は「片方が直っていない」と読まれて揃えられやすい。両モジュールの該当箇所に
  本 ADR への参照を置き、どちらの側から読んでも非対称が意図的だと分かるようにする。
- allowlist の要素が静かに減っても、集合リテラルは 1 statement なのでカバレッジでは
  検出できない。`tests/hooks/test_protection_bypass_regressions.py` が期待値との完全一致
  （層2）と全要素の deny 実測（層1）で固定する（M-10）。
