# ADR-0005: 実機ランタイム監査の検証範囲は静的 validator に限定し、実 install/update smoke test は対象外とする

**日付**: 2026-08-18  **ステータス**: superseded（費用対効果の判断のみ。[ADR-0014](0014-host-component-inventory-is-a-release-gate.md)）

> **注記（2026-08-27）**: 本 ADR の「実 install/update smoke test は自動検証の対象外」という
> 費用対効果の判断は ADR-0014 で撤回した。2026-08-26 の実機監査 F-01 で、Claude Code 上で
> 9 エージェントが 1 体も登録されていない状態が 41 リリース見過ごされていたことが判明し、
> それを `claude --plugin-dir ... plugin details` の 1 コマンドで検出できたため。本 ADR が
> 却下したのは「install/update フロー全体のエミュレーション」であり、component inventory
> だけを問い合わせる低コスト案は比較対象に入っていなかった。静的 validator に関する記述は
> 引き続き有効。

## コンテキスト

`hooks.json` は PreToolUse（`block_no_verify`/`pre_bash_commit_quality`/
`bash_config_protection`/`config_protection`）・PreCompact・SessionStart・
SessionEnd の複数イベントで hook を登録する。これらが実際に各ホスト
（Claude Code / Copilot CLI / Grok 等）へ正しくインストールされ、
`hooks.json` の宣言どおりに登録・起動されることを確認する最も確実な方法は、
実際にプラグインを install/update し、各ホストの hook 登録機構に載せて
smoke test することである。

v0.9.32 検証 §7-3 はこの実 install/update smoke test（および host への
登録確認）が未実施であると指摘した。この作業ではスコープを大きく超える
と判断し、代わりに `test_validate_hooks.py`（`hooks.json` が全イベント
経路を宣言していること、全エントリが `timeout` を持つことの静的検証）へ
縮退した。

## 決定

**本プロジェクトの CI・自動検証範囲は、`hooks.json` の構造的な健全性を
検証する静的 validator（`test_validate_hooks.py` 等）に限定する。実際の
プラグイン install/update フローや、各ホストの hook 登録機構への実登録を
検証する smoke test は、自動検証の対象に含めない。**

これは実機での動作確認そのものを不要とする決定ではない。実機再検証
（本ラウンドのような Copilot CLI 実機での再検証セッション）は、必要に
応じて人間が随時実施する運用とし、CI に組み込む自動化の対象からは外す。

## 検討した代替案

### 代替案 1: 各ホストの install/update を CI でエミュレートする smoke test を実装する

- 長所: `hooks.json` の宣言が実際にホストへ反映されることまで自動検証
  できる。
- 短所: Claude Code / Copilot CLI / Grok それぞれのインストール機構
  （ディレクトリ構造、マーケットプレイス経由の配置、`grok_plugin_root
  .find_latest_installed_ple4` が扱うハッシュ付きディレクトリ等）を
  CI 環境で再現する必要があり、各ホストのバージョン更新に追従し続ける
  保守コストが継続的に発生する。ホストの内部実装（非公開の場合がある）
  に CI が依存することになり、壊れやすい。
- 却下理由: スコープと保守コストが本プロジェクトの検証範囲を大きく超える。
  静的 validator（宣言の構造検証）で得られる保証と、実 install smoke test
  で追加的に得られる保証の差分に対して、コストが見合わない。

### 代替案 2: 実 install/update smoke test を人間の手動チェックリストとして
文書化し、リリースごとに実施を義務付ける

- 長所: 自動化コストをかけずに、リリース前の確認は担保できる。
- 短所: 手動チェックリストは実施漏れが起きやすく、実施したかどうかを
  機械的に検証できない。
- 却下理由: 今回は「実機再検証セッション」という形で人間が随時実施して
  おり、これを機械的な必須ゲートにする効果は限定的。将来リリースプロセス
  が成熟した段階で改めて検討する余地は残すが、現時点では義務化しない。

## 結果

### 肯定的

- CI の検証範囲が明確になり、`hooks.json` の宣言漏れ（イベント未登録・
  `timeout` 未設定）は機械的に検出され続ける。
- ホストの内部実装変更に CI が追従する必要が無く、保守コストを抑えられる。

### 否定的

- `hooks.json` の宣言が構造的に正しくても、実際のホストへの登録・起動が
  正しく行われる保証は自動検証されない。ホスト側の仕様変更（例:
  `CLAUDE_PLUGIN_ROOT` の扱いが変わる、matcher の解釈が変わる）による
  実害は、次の実機再検証セッションまで検出されない。

### リスク

- 本ラウンドで発覚した A-07（Copilot agent shell に `CLAUDE_PLUGIN_ROOT`
  が無い）のような、ホスト固有の実行環境差に起因する不具合は、静的
  validator では検出できない。実機再検証の頻度・タイミングは人間の判断に
  委ねられており、次の再検証まで同種の不具合が見過ごされるリスクを受容
  する。
