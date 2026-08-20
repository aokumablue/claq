# v0.9.37 plugin-root resolver 再検証（H-01/M-02/§4）対応方針 — 計画＋ADRのみ

## Context

前ラウンド（v0.9.36 対応、コミット `f4e66bc`）はリリース済み（`release: v0.9.37`,
`8885291`）。その後 CopilotCLI による再検証レポート
`docs/reports/PLUGIN_ROOT_RESOLVER_2026-08-20_V0.9.37_REVERIFICATION.md` を
受領した。今回はユーザーの週次利用リミットが近いため、**このセッションでは
実装（コード修正）を行わず、方針を確定させた計画書と ADR 改訂のみを行う**。
コード変更（M-02 の footgun 除去、回帰テスト追加）は次回セッションへ持ち越す。

レポートは 3 件を指摘した（H-02/M-01 は「解消」「緩和」と判定済み、再対応
不要）。事実確認と advisor レビューにより、対応方針を以下のとおり確定した。

## 検証結果サマリ

### H-01: 検証済み pointer が指す root の helper を無検証で source する（レポートは HIGH 判定）

`env-template.sh` は pointer の root+lstart 照合には成功するが、root が
「正規の bluecore installation か」「helper が改竄されていないか」
「root/helper が symlink・別 owner・group-writable でないか」は検証しない
というレポートの指摘自体は事実（実機再現コマンドで `H01_FAKE_HELPER_EXECUTED`
の出力を確認済み）。

ただし、これは ADR-0009 が既に検討済みの範囲（H-01 v0.9.36 版として一度
「非対応」を決定している）。今回追加調査した論点:

- **`test -O`/`-h`/`-G`（POSIX sh ビルトイン）は macOS `/bin/sh` と `dash`
  の両方で実際に利用可能**であることを実機確認した。ADR-0009 が owner/mode
  検証を非対応とした理由の一つ「POSIX sh に移植性のある `stat` フォーマットが
  無い」は、`stat` を使う前提の議論であり、`test` ビルトインを使えば
  この障害は存在しない。**この論拠は誤りだった。**
- しかし、advisor レビューで指摘された決定的な事実がある: `~/.bluecore/roots/`
  は 0700・同一 UID 所有で、書き込めるのは同一 OS ユーザーだけ（ADR-0002 の
  脅威モデルの外）。**同一 UID の攻撃者は fake root を「自分が所有する実
  ディレクトリ」として作れるため、`test -O`（自分所有）・`test -h`
  （非 symlink）・`test -G`（自分グループ）は全て素通りする。** 検証を
  実装しても、まさに防ぎたい攻撃者がその検証を満たしてしまう。
  manifest/digest 方式（レポートの修正案 1）も同様に、攻撃者は fake
  manifest・fake digest も同じ権限で自由に書けるため機能しない
  （ADR-0009 の既存結論と同じ理由）。
- 別 UID が書き込み可能な root を pointer が指すケースは、そもそも
  pointer 自体が同一 UID の正規 writer（`launcher.REPO_ROOT`）にしか
  書けない以上、発生しない（インストールディレクトリ自体が
  group-writable なら、それは install 時点の欠陥であり resolve 時点で
  拾うべき問題ではない）。

**結論: H-01 は引き続き非対応。** 「fallback の受容度」の論点ではない
（H-02 のような「複数候補から推測で選ぶ」構造とは異なり、H-01 には
「これより厳格な代替手段」が原理的に存在しない — resolver が読める入力は
すべて同一 UID が書ける状態でしかないため）。ADR-0009 の**結論は維持**しつつ、
崩れた補助論拠（stat 移植性）を訂正し、`test -O`/`-h`/`-G` を検討した上で
なお同一 UID 攻撃者には無力である旨を明記する。

### M-02: writer の一時ファイル名が予測可能（レポートは MEDIUM 判定）

`_atomic_write_text`（`env_pointer.py:220-250`）は
`path.with_name(f"{path.name}.tmp.{os.getpid()}")` を `O_CREAT | O_TRUNC`
（`O_EXCL` なし）で開く。

- レポートが主張する「同一プロセス内 thread/reentrant 衝突」は、実装を
  確認した限り**現状は発生経路が無い**（`write_env_pointer` の呼び出しは
  `launcher.py:176` の 1 箇所のみ、hook パスに threading は無い）。
- ただし実在する別の問題として、予測可能な一時ファイル名 + `O_EXCL` 無しは
  「同一 UID 内での symlink 事前設置・precreation race」を不必要に許す
  footgun になっている。これは信頼境界を閉じる話ではなく（同一 UID 前提は
  ADR-0002 の範囲外のまま）、堅牢性の改善として価値がある。

**結論: 対応する。ただし「セキュリティ修正」ではなく「堅牢性改善（footgun
除去）」として位置づける。** 次回セッションで実装:

- `tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.tmp.", suffix="")`
  相当（`O_EXCL` + ランダム suffix）へ置き換える。
- **GC naming contract との整合を守ること**: `_gc_one`（`env_pointer.py:516`
  以降）は `entry.name.isdigit()` で分岐し、M-01 の age-gate は非数字名の
  分岐にある。新しい tmp 名も非数字のままにし、`prefix=f"{path.name}.tmp."`
  形式を維持して M-01 の修正を無効化しないこと。
- `O_EXCL` 化で失敗モードが変わる（今までは stale tmp を `O_TRUNC` で
  黙って再利用していたが、`O_EXCL` ではエラーになる）。ランダム suffix
  なら衝突自体が実質発生せず、孤児化した tmp は GC の age-gate が回収する。
  この失敗が `write_env_pointer` の外側 `try/except OSError`（既存）で
  握り潰され、hook 本来の処理を壊さないことを実装後に確認する。
- 回帰テスト: レポート §4-3 が要求する項目のうち、既存 67 テストで
  カバー済みでないのは「same-process race」のみ（他の pointer format /
  shell-code 非実行 / fake root 拒否 / lstart 照合 / ancestor selection /
  GC / HOME contract / clean-shell bootstrap は既存テストで検証済みと
  確認した）。M-02 の実装とペアで、barrier 同期した複数スレッドが同じ
  target を書いても例外が出ず最終ファイルが完全な有効レコードだけになる
  ことを検証するテストを追加する。

### §4: source/installed artifact 乖離（レポートは HIGH「release assurance」判定）

**事実誤認と判定した。対応不要。証拠:**

- リポジトリ HEAD は既に v0.9.37（`plugins/bluecore/.claude-plugin/plugin.json`
  の `version: "0.9.37"`）。`release: v0.9.37`（`8885291`）はレポート内の
  検証タイムスタンプ（18:52）より前（17:03）にコミット済み。
- source に PATH 探索・`latest` fallback・source された pointer は存在しない
  （grep で確認済み。`latest` の出現箇所はモジュール docstring の説明文と
  GC の旧形式クリーンアップ処理のみで、実際の解決ロジックには無い）。
- レポートが参照した path は `/Users/tasaki-mamoru/dev/bluecore/...` —
  検証者側の別マシンの clone であり、この repository ではない。
  **検証者側の clone が古かった（v0.9.35 相当）ことが原因**と判断する。

ADR には記載しない（実装決定の対象ではなく、レポートの検証環境の問題の
ため）。ユーザーへの完了報告でこの事実誤認を明示する。

## 今回のセッションで実施すること

### T1: ADR-0009 改訂（このセッションで実施、コード変更なし）

`docs/adr/0009-root-pointer-resolver-does-not-verify-process-identity-or-file-ownership.md`
へ改訂を追記する（改訂履歴セクションに新エントリを追加、「2. owner/mode
検証を実装しない理由」節を更新）:

- `test -O`/`-h`/`-G` が macOS `/bin/sh` と `dash` の両方で利用可能であると
  実機確認した事実を記録する。
- 「POSIX sh に stat の移植性のあるフォーマットが無い」という補助論拠は
  誤りだった（`test` ビルトインを使えば回避できる）と明記して訂正する。
- 主論拠（同一 UID 攻撃者は roots ディレクトリへの書き込み権限を持つ以上、
  fake root を「自分が所有する実ディレクトリ」として作れるため、
  owner/symlink/group 検証・manifest/digest 検証のいずれも、まさに
  防ぎたい攻撃者に無力である）を明示し、これが決定を維持する理由である
  ことを記す。
- `docs/reports/PLUGIN_ROOT_RESOLVER_2026-08-20_V0.9.37_REVERIFICATION.md`
  H-01 への参照を追加する。
- 「H-01 はユーザーが表明した『推測 fallback を許容しない』の対象では
  ない」という位置づけの違い（H-02 は複数候補からの推測選択、H-01 は
  検証手段そのものが原理的に存在しない）を一文で記す。

M-02 は ADR-0009 には書かない（コード未実装のため、実装済みであるかの
ような過去形の記述を ADR に残すと repository に虚偽の記載が生まれる）。
M-02 の決定と実装手順は本 plan file にのみ残し、次回セッションで実装後に
改めて ADR へ記録するか判断する。

## 次回セッションで実施すること（今回は実施しない）

### T2: M-02 実装

`env_pointer.py` の `_atomic_write_text` を `O_EXCL` + ランダム suffix の
一時ファイル名へ変更。GC naming contract（非数字名を維持）を守る。

### T3: same-process race 回帰テスト追加

barrier 同期した複数スレッドで同一 target への書き込みが安全であることを
検証するテストを `tests/lib/test_env_pointer.py` へ追加。

### 検証（次回、コード変更後）

```bash
cd /Users/aokumablue/dev/bluecore-dev && source .venv/bin/activate
cd plugins/bluecore && python3 -m pytest -q --cov     # 100% / 全 green
cd /Users/aokumablue/dev/bluecore-dev && ruff check plugins/bluecore/src   # 警告なし
```

- 4 validator（skills/commands/agents/hooks）
- M-02 修正後、同時書き込みで partial file / stray tmp が出ないことを実機確認
