# アーキテクチャ決定記録（ADR）

本プロジェクトのアーキテクチャ上の決定を記録するインデックス。フォーマットは [template.md](template.md) を参照。

**1 決定 = 1 ファイルではなく、要点（トピック）ごとの 1 ファイルに、`## ADR-NNNN:` 節として収める。**
決定の識別子は `ADR-NNNN` であり、リポジトリ内の 300 箇所以上（テストの docstring・エージェント定義・
`CLAUDE.md`）がこの番号で参照している。番号は再割り当てしない。番号を跨いで繰り返される前提
（脅威モデル・fail-open / fail-closed の向き・「実績が反証している」型の棄却理由）は各トピックファイル
冒頭の「共通原則」に 1 回だけ書き、各節はそこを参照する。

## トピック

| ファイル | 扱う問い | 収録 ADR |
|---|---|---|
| [01-hook-failure-direction.md](01-hook-failure-direction.md) | 検査を完了できないとき allow と deny のどちらへ倒すか。exit code は何を表すか | 0001 / 0003 / 0019 / 0024 |
| [02-shell-analysis-boundary.md](02-shell-analysis-boundary.md) | シェルコマンドをどこまで解析し、解析できないものをどちらへ倒すか | 0002 / 0013 / 0017 / 0018 / 0020 / 0021 |
| [03-plugin-root-resolution.md](03-plugin-root-resolution.md) | どの plugin install を実行するかをどう決め、どこまで検証するか | 0008 / 0009 |
| [04-untrusted-input-prompt-boundary.md](04-untrusted-input-prompt-boundary.md) | 信頼できない入力をプロンプト境界へ通すかどうか | 0007 / 0015 |
| [05-definition-and-subagent-design.md](05-definition-and-subagent-design.md) | 定義文書を何体に分け、中身に何を書き何を書かないか | 0004 / 0010 / 0011 / 0012 |
| [06-staleness-detection.md](06-staleness-detection.md) | 書いたものが黙って古くなる経路をどう塞ぐか。実測できないものをどうするか | 0016 / 0022 / 0023 |
| [07-verification-scope-release-gates.md](07-verification-scope-release-gates.md) | 何を CI で検証し、何をリリースゲートにし、何を配布しないか | 0005 / 0006 / 0014 |

## ADR 一覧

| ADR | Title | Status | Date | ファイル |
|---|---|---|---|---|
| 0001 | フックは検査を完走できないとき fail-open にする | accepted | 2026-08-18 | [01](01-hook-failure-direction.md) |
| 0002 | git commit / シェル保護フックの検出境界は誤検出を誤通過より選ぶ | accepted | 2026-08-18 | [02](02-shell-analysis-boundary.md) |
| 0003 | detached background（`--bg`）の親 exit は「起動受付」であり「処理成功」ではない | accepted | 2026-08-18 | [01](01-hook-failure-direction.md) |
| 0004 | reviewer agent は write-capable Bash を保持する | accepted | 2026-08-18 | [05](05-definition-and-subagent-design.md) |
| 0005 | 実機ランタイム監査の検証範囲は静的 validator に限定する | superseded by ADR-0014（費用対効果の判断のみ） | 2026-08-18 | [07](07-verification-scope-release-gates.md) |
| 0006 | 配布 artifact にテストを同梱せず、検証は source tree で行う | accepted | 2026-08-20 | [07](07-verification-scope-release-gates.md) |
| 0007 | 知識カードの active 化権限は payload から剥奪するが、`promote` と既存 active カードは維持する | accepted | 2026-08-20 | [04](04-untrusted-input-prompt-boundary.md) |
| 0008 | plugin root は `~/.claq/env.sh` ポインタで解決し、md にベンダ固有パスを書かない | accepted | 2026-08-20（改訂 ×5、最新 2026-08-27） | [03](03-plugin-root-resolution.md) |
| 0009 | roots pointer resolver は owner/mode 検証を行わない（PID 開始時刻照合は撤回） | accepted | 2026-08-20（改訂 ×2） | [03](03-plugin-root-resolution.md) |
| 0010 | サブエージェントは「渡す材料」で分割し、役割名では分割しない | accepted | 2026-08-25 | [05](05-definition-and-subagent-design.md) |
| 0011 | エージェント定義には挙動を書き、論拠と再説明は書かない | accepted | 2026-08-25 | [05](05-definition-and-subagent-design.md) |
| 0012 | LLM 応答の事後圧縮パターン（output-style `slim`）を廃止する | accepted | 2026-08-26 | [05](05-definition-and-subagent-design.md) |
| 0013 | バイナリ判定で secret 検査を止めない（ADR-0001 の受容を撤回） | accepted | 2026-08-27 | [02](02-shell-analysis-boundary.md) |
| 0014 | ホスト component inventory をリリースゲートにする（ADR-0005 を supersede） | accepted | 2026-08-27 | [07](07-verification-scope-release-gates.md) |
| 0015 | handoff の足場タグ除去は孤立タグを細工とみなす fail closed とする | accepted | 2026-08-28 | [04](04-untrusted-input-prompt-boundary.md) |
| 0016 | 足場タグ denylist の陳腐化は実コーパス走査で検知する（fail open の診断） | accepted | 2026-08-29 | [06](06-staleness-detection.md) |
| 0017 | heredoc のデータ本文はコマンド列として解析しない | accepted | 2026-08-31（2026-09-06 改訂） | [02](02-shell-analysis-boundary.md) |
| 0018 | `cd` を含むコマンドでは repo スコープ判定を信用せず deny する | accepted | 2026-09-03 | [02](02-shell-analysis-boundary.md) |
| 0019 | 保護フックは stdin を「読めなかった」場合に fail-closed する | accepted | 2026-09-04 | [01](01-hook-failure-direction.md) |
| 0020 | シェル保護フックは 1 つのコマンド文字列を 2 つのシェル方言で解析する | accepted | 2026-09-04 | [02](02-shell-analysis-boundary.md) |
| 0021 | 実行位置の特定は wrapper allowlist で行い、git 起動の探索は全トークン走査のままにする | accepted | 2026-09-06 | [02](02-shell-analysis-boundary.md) |
| 0022 | 外部の版付き標準を定義へ列挙するときは版を見出しへ書く | accepted | 2026-09-06 | [06](06-staleness-detection.md) |
| 0023 | 定義へ書く固定値は「陳腐化の検知器があるか」で残す / 外す | accepted | 2026-09-07 | [06](06-staleness-detection.md) |
| 0024 | 走査コストは保護のバイパスとして扱い、実測できない修正は残存として書く | accepted | 2026-09-07 | [01](01-hook-failure-direction.md) |

## 新しい ADR を追加するとき

1. この一覧の最大値 + 1 を採番する（既存番号の再利用・再割り当てはしない）
2. 決定が扱う問いに最も近いトピックファイルへ `## ADR-NNNN: <タイトル>` 節として追記する。どのトピックにも
   属さないなら新しいトピックファイル（`NN-<slug>.md`）を作り、上の「トピック」表に行を足す
3. この「ADR 一覧」へ 1 行足す
4. 既存の決定を撤回・supersede する場合は、**撤回された記述を消さない**。撤回の事実と経緯を残す
   （ADR-0009 の「〔撤回済み〕PID 開始時刻照合を実装しない理由」、ADR-0008 の改訂履歴が例）。棄却した代替案も
   同様に残す — 同じ提案が繰り返し来るため、代替案の記録がその再提案への回答になる
