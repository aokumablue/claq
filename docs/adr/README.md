# アーキテクチャ決定記録（ADR）

本プロジェクトのアーキテクチャ上の決定を記録するインデックス。フォーマットは [template.md](template.md) を参照。

| ADR | Title | Status | Date |
|---|---|---|---|
| [0001](0001-hooks-fail-open-when-inspection-cannot-complete.md) | フックは検査を完走できないとき fail-open にする | accepted | 2026-08-18 |
| [0002](0002-shell-hooks-prefer-false-positive-over-false-negative.md) | git commit / シェル保護フックの検出境界は誤検出を誤通過より選ぶ | accepted | 2026-08-18 |
| [0003](0003-detached-background-exit-is-launch-acceptance-not-success.md) | detached background（`--bg`）の親 exit は「起動受付」であり「処理成功」ではない | accepted | 2026-08-18 |
| [0004](0004-reviewer-agent-keeps-write-capable-bash.md) | reviewer agent は write-capable Bash を保持する | accepted | 2026-08-18 |
| [0005](0005-runtime-audit-scope-is-static-validators-not-real-install.md) | 実機ランタイム監査の検証範囲は静的 validator に限定し、実 install/update smoke test は対象外とする | accepted | 2026-08-18 |
| [0006](0006-distributed-artifact-omits-tests-verify-in-source-tree.md) | 配布 artifact にテストを同梱せず、検証は source tree で行う | accepted | 2026-08-20 |
| [0007](0007-knowledge-active-authority-scoped-to-payload-strip.md) | 知識カードの active 化権限は payload から剥奪するが、`promote` と既存 active カードは維持する | accepted | 2026-08-20 |
| [0008](0008-plugin-root-resolved-via-home-bluecore-env-pointer.md) | plugin root は `~/.bluecore/env.sh` ポインタで解決し、md にベンダ固有パスを書かない | accepted | 2026-08-20（改訂 ×5、最新 2026-08-27） |
| [0009](0009-root-pointer-resolver-does-not-verify-process-identity-or-file-ownership.md) | roots pointer resolver は owner/mode 検証を行わない（PID 開始時刻照合は撤回） | accepted | 2026-08-20（2026-08-20 改訂 ×2） |
| [0010](0010-subagents-split-by-material-not-by-role.md) | サブエージェントは「渡す材料」で分割し、役割名では分割しない | accepted | 2026-08-25 |
| [0011](0011-agent-definitions-state-behavior-not-rationale.md) | エージェント定義には挙動を書き、論拠と再説明は書かない | accepted | 2026-08-25 |
| [0012](0012-output-style-response-compression-is-abandoned.md) | LLM 応答の事後圧縮パターン（output-style `slim`）を廃止する | accepted | 2026-08-26 |
| [0013](0013-binary-detection-does-not-suppress-secret-scanning.md) | バイナリ判定で secret 検査を止めない（ADR-0001 の受容を撤回） | accepted | 2026-08-27 |
| [0014](0014-host-component-inventory-is-a-release-gate.md) | ホスト component inventory をリリースゲートにする（ADR-0005 を supersede） | accepted | 2026-08-27 |
| [0015](0015-handoff-scaffold-removal-is-fail-closed.md) | handoff の足場タグ除去は孤立タグを細工とみなす fail closed とする | accepted | 2026-08-28 |
| [0016](0016-scaffold-drift-detection-is-a-fail-open-diagnostic.md) | 足場タグ denylist の陳腐化は実コーパス走査で検知する（fail open の診断） | accepted | 2026-08-29 |
| [0017](0017-heredoc-data-body-is-not-command-text.md) | heredoc のデータ本文はコマンド列として解析しない | accepted | 2026-08-31 |
