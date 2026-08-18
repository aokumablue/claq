# アーキテクチャ決定記録（ADR）

本プロジェクトのアーキテクチャ上の決定を記録するインデックス。フォーマットは [template.md](template.md) を参照。

| ADR | Title | Status | Date |
|---|---|---|---|
| [0001](0001-hooks-fail-open-when-inspection-cannot-complete.md) | フックは検査を完走できないとき fail-open にする | accepted | 2026-08-18 |
| [0002](0002-shell-hooks-prefer-false-positive-over-false-negative.md) | git commit / シェル保護フックの検出境界は誤検出を誤通過より選ぶ | accepted | 2026-08-18 |
| [0003](0003-detached-background-exit-is-launch-acceptance-not-success.md) | detached background（`--bg`）の親 exit は「起動受付」であり「処理成功」ではない | accepted | 2026-08-18 |
| [0004](0004-reviewer-agent-keeps-write-capable-bash.md) | reviewer agent は write-capable Bash を保持する | accepted | 2026-08-18 |
| [0005](0005-runtime-audit-scope-is-static-validators-not-real-install.md) | 実機ランタイム監査の検証範囲は静的 validator に限定し、実 install/update smoke test は対象外とする | accepted | 2026-08-18 |
