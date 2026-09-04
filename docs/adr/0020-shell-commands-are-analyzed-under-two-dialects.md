# ADR-0020: シェル保護フックは 1 つのコマンド文字列を 2 つのシェル方言で解析する

**日付**: 2026-09-04  **ステータス**: accepted

## コンテキスト

`block_no_verify` と `bash_config_protection` は、hook payload の
`tool_input.command` を `shlex(posix=True)` でトークン化して判定する。POSIX
シェルではクォート外の `\` は「次の 1 文字をエスケープする記号」なので、
これは正しい解析である。

Windows ホストのシェルツールは PowerShell であり（release-verify
2026-09-03 の §9.1 で `Get-Command python3` 等の PowerShell 構文が実際に
実行されている）、PowerShell と cmd では `\` は**ただのパス区切り**である。
その結果、同じ文字列が方言によって別のトークン列になる。実測:

| コマンド | POSIX 読み（現行） | 実際の Windows での意味 |
|---|---|---|
| `rm .\.eslintrc` | `['rm', '..eslintrc']` | `.eslintrc` の削除 |
| `C:\Git\bin\git.exe commit --no-verify` | `['C:Gitbingit.exe', ...]` | git のフックバイパス |

前者は保護対象 basename に一致せず、後者は `is_git_executable_token` が
git と認識できない。どちらも **Windows でだけ保護フックが素通りする**。

hook の入力にシェル種別は載っていない。`tool_name` は `Bash` / `shell` /
`run_terminal_command` 等でホストの実装依存であり、そこから実行シェルの
方言を確定することはできない。

## 決定

**保護フックは、コマンド文字列を「POSIX 読み」と「Windows 読み」の 2 通りで
解析し、どちらかが検出したら deny する。**

- Windows 読みの作り方は `\` の直後の文字で 3 通りに分ける
  （`hook_common.command_dialect_variants`）:
  - **パス構成文字** → `/` へ置換（`.\ruff.toml` → `./ruff.toml`）
  - **空白 / タブ** → `\` を除去（`my\ ruff.toml` → `my ruff.toml`）。
    エスケープを持たないシェルでは 2 引数であり、2 番目が実際に書き込み対象になる
  - **`/`** → 変換しない（`\/` は sed スクリプト等の POSIX エスケープで、
    Windows のパス区切りには現れない）
- Windows 読みが元と同じになるコマンドは、追加の解析をしない。
- 変換関数は `hook_common` に 1 つだけ置き、`block_no_verify` と
  `bash_config_protection` が共有する。

あわせて、実行位置のコマンド名比較を **basename 化 + 小文字化 + `.exe` 除去**
（`_command_name`）へ統一し、PowerShell / cmd の書き込み語彙を加えた:
長形式 cmdlet（`Remove-Item` / `Clear-Content` / `Set-Content` /
`Add-Content` / `Out-File` / `New-Item` / `Copy-Item` / `Move-Item`）と、
cmd 組み込み・PowerShell 既定エイリアス（`del` / `erase` / `rd` / `copy` /
`move` / `ri` / `mi` / `clc` / `cpi` / `sc` / `ac` / `ni`）。`rm` / `cp` / `mv` は
PowerShell の別名として同じ cmdlet に解決されるため既に効いていたが、
長形式・別名・`.exe` 付きで書かれると外れていた。

`pre_bash_commit_quality` も同じ方言展開で `git commit` を探す
（`_detect_git_commit`）。ここだけ POSIX 読み 1 本だと、
`C:\Git\bin\git.exe commit -m x` が `block_no_verify` では検出されるのに
品質ゲートだけ素通りする非対称になる。

## 根拠

- **ADR-0002 の範囲内。** 同 ADR は「解析できない構文については誤検出を
  誤通過より選ぶ」と定める。方言が確定できない以上、片方だけで解析するのは
  「解析できていない」ことを allow へ倒すのと同じである。
- **誤検出と誤通過を分けて実測した。** 当初は `\` を無条件に `/` へ置き換えて
  いたが、この変換は basename を変えるだけでなく**トークン境界を作り替える**。
  実測で 3 件が deny 側へ倒れた。ただしこの 3 件は性質が違う:
  - `sed -i 's/\.git\/hooks\/pre-commit//' notes.md` は**純粋な誤検出**。
    `\/` は Windows のパス区切りに現れず、POSIX 読みでも Windows 読みでも `/` は
    区切りのまま残るので、変換対象から外しても検出力は落ちない → 除外した。
  - `rm my\ ruff.toml` と `git commit -m fix\ --no-verify` は
    **POSIX では誤検出だが Windows では真陽性**。エスケープを持たないシェルでは
    `my\` と `ruff.toml`、`fix\` と `--no-verify` が別引数になり、実際に
    `ruff.toml` が削除され、`--no-verify` が git へ渡る。一度これを「誤検出」と
    見て除外したが、security-auditor の指摘で誤通過を新規に作っていたと判明し
    撤回した（ADR-0002 に逆行するため）。現在は `\` を除去して検出側へ倒す。
  Windows 側の検出（`rm .\ruff.toml` / `C:\Git\bin\git.exe commit --no-verify`）は
  いずれも維持。クォート済み（`rm "my\ ruff.toml"`）は 1 トークンのままなので
  誤検出は増えない。残る誤検出（例 `rm a\.eslintrc`）は ADR-0002 が受容する側で
  あり、characterization test として固定してある。
- **プラットフォーム分岐ではない。** 実行中の OS を見て挙動を変えるのでは
  なく、入力を 2 通りに読む。macOS/Linux でも同じコードパスが走り、テストも
  そこで両方の読み方を踏める（Windows 専用の未検証分岐を作らない）。
- **大小無視は既存の判断の踏襲。** `is_git_executable_token` は「macOS 既定の
  APFS が大小を区別しない」ことを理由に既に lower している。PowerShell の
  cmdlet 解決も大小無視なので、同じ扱いに揃えた。Linux では `RM` が `rm` に
  一致する誤検出側へ倒れるが、これも ADR-0002 の範囲内。正規化は
  `_command_name` の 1 関数へ集約する — 実行コマンド判定だけを大小無視にして
  wrapper 読み飛ばしと `cd` 判定を残すと、`CD ..; rm ruff.toml` が ADR-0018 の
  無条件 deny 分岐へ落ちず**誤通過**する（実測）。

## 検討した代替案

### 代替案 1: 実行シェルを推定して方言を選ぶ

- 長所: 誤検出が原理的に発生しない。
- 短所: 推定材料が無い。`tool_name` はホスト実装依存で、`Bash` という名前の
  ツールが Windows では PowerShell を回している可能性を排除できない。
  `platform.system()` はフックが動く OS を示すだけで、ホストがどのシェルへ
  渡すかは決まらない（Windows 上の Git Bash 経由もありうる）。
- 却下理由: 推定を外したときの失敗が「保護の無効化」であり、ADR-0008 改訂 2 で
  ユーザーが明示した「リスクがあるとみなせる推測 fallback を採らない」方針に
  反する。

### 代替案 2: `\` を含むコマンドは無条件で deny する

- 長所: 実装が最も単純。
- 短所: POSIX で `\` は日常的に現れる（エスケープ・行継続）。ほぼ全ての
  複数行コマンドが拒否され、実用にならない。
- 却下理由: 誤検出の量が「使えない」水準になる。ADR-0002 は誤検出を選ぶと
  言っているが、無制限に選ぶとは言っていない。

### 代替案 3: `shlex(posix=False)` の結果も併用する

- 長所: バックスラッシュを保つ「もう 1 つの読み方」を標準ライブラリから得られる。
- 短所: `posix=False` はクォートをトークンに残すため、basename 比較の前に
  独自のクォート剥がしが必要になる。剥がし方の規則が 3 つ目の方言として増える。
- 却下理由: `\` → `/` の置換のほうが、規則が 1 行で、テストで全域を示せる。

## 影響

- `hook_common.command_dialect_variants()` を追加。`block_no_verify` は
  `_has_bypass_flag_in_dialect`、`bash_config_protection` は
  `_find_protected_write_in_dialect`、`pre_bash_commit_quality` は
  `_detect_git_commit` で、Bash 系 3 フックすべてが入口で方言を回す。
- `bash_config_protection` の名前比較 3 箇所（実行コマンド判定・wrapper 読み
  飛ばし・`cd` 判定）と、縮退経路 `_raw_text_write_risk` の保護名照合が
  すべて大小無視になった。
- heredoc のデータ本文除去は、両フックとも「方言展開の前に 1 回」へ揃えた
  （片方が方言ごとに剥がす形だと、同じ入力でもフックによって解析対象が変わる）。
- Windows 実機での検証は未実施。方言変換自体は macOS のテストで両側を踏める
  （Windows 固有の分岐を持たない設計にしたのはこのため）。
