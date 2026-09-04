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

- 変換は `\` → `/` の 1 種類だけ（`hook_common.command_dialect_variants`）。
- `\` を含まないコマンドは読み方が 1 通りしか無く、追加の解析はしない。
- 変換関数は `hook_common` に 1 つだけ置き、`block_no_verify` と
  `bash_config_protection` が共有する。

あわせて、実行位置のコマンド名比較を**大小無視**にし、PowerShell の長形式
書き込み cmdlet（`Remove-Item` / `Clear-Content` / `Set-Content` /
`Add-Content` / `Out-File` / `New-Item` / `Copy-Item` / `Move-Item`）を
既存の語彙集合へ加えた。`rm` / `cp` / `mv` は PowerShell の別名として同じ
cmdlet に解決されるため既に効いていたが、長形式で書かれると外れていた。

## 根拠

- **ADR-0002 の範囲内。** 同 ADR は「解析できない構文については誤検出を
  誤通過より選ぶ」と定める。方言が確定できない以上、片方だけで解析するのは
  「解析できていない」ことを allow へ倒すのと同じである。
- **誤検出のコストが小さいことを実測で確認した。** 2 方言解析を入れた状態で
  既存の全テスト（2520 件）が緑のままだった。新たに deny 側へ倒れるのは
  「POSIX のエスケープを含み、かつその Windows 読みの basename が保護対象名
  と一致し、かつ書き込み位置にある」場合に限られる（例:
  `touch my\ file.txt` は basename が `file.txt` なので検出されない）。
- **プラットフォーム分岐ではない。** 実行中の OS を見て挙動を変えるのでは
  なく、入力を 2 通りに読む。macOS/Linux でも同じコードパスが走り、テストも
  そこで両方の読み方を踏める（Windows 専用の未検証分岐を作らない）。
- **大小無視は既存の判断の踏襲。** `is_git_executable_token` は「macOS 既定の
  APFS が大小を区別しない」ことを理由に既に lower している。PowerShell の
  cmdlet 解決も大小無視なので、同じ扱いに揃えた。Linux では `RM` が `rm` に
  一致する誤検出側へ倒れるが、これも ADR-0002 の範囲内。

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
  `_find_protected_write_in_dialect` へ本体を移し、入口で方言を回す。
- `bash_config_protection._executed_command_args` の名前比較が大小無視になった。
- Windows 実機での検証は未実施。方言変換自体は macOS のテストで両側を踏める
  （Windows 固有の分岐を持たない設計にしたのはこのため）。
