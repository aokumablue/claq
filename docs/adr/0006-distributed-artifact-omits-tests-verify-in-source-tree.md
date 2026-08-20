# ADR-0006: 配布 artifact にテストを同梱せず、検証は source tree で行う

**日付**: 2026-08-20  **ステータス**: accepted

## コンテキスト

v0.9.34 時点のランタイム監査レポートの H-03 は「release tree に pytest
テストが無く、回帰検知を実行できない」と
指摘した。検証者は `~/.copilot/installed-plugins/bluecore/bluecore`
（配布・インストール済みの plugin tree）で
`cd plugins/bluecore && PYTHONPATH=src python3 -m pytest -q` を実行し、
「collected 0 items」で exit 5 になったことを根拠にした。

同一の再検証を本作業の事前確認として **source tree**
（`bluecore-dev` リポジトリの `plugins/bluecore`）で実施したところ、結果は
次のとおりだった:

```
$ cd plugins/bluecore && python3 -m pytest -q --cov
...
TOTAL                                            3556      0   1264      0   100%
Required test coverage of 100.0% reached. Total coverage: 100.00%
============================= 1473 passed in 6.97s =============================
```

`tests/` 配下に 42 個の test file が存在し、`pyproject.toml` は
`[project.optional-dependencies].dev` に `pytest>=8.0` / `pytest-cov>=4.0` /
`ruff>=0.4` / `vulture>=2.0` を既に宣言している。coverage 設定
（`fail_under = 100`）も存在する。つまり H-03 が指摘した「テスト基盤が
存在しない」状態は、**source tree には最初から存在しなかった**。

検証者が pytest を実行した installed tree（`~/.copilot/installed-plugins/...`）
には `tests/` が同梱されていない。これはプラグインのインストール処理が
`plugins/bluecore` の配布対象サブセット（`agents/` / `commands/` / `hooks/`
（`hooks.json`）/ `runtime/` / `skills/` / `src/`）だけを配置し、開発用
アーティファクト（`tests/`、`CLAUDE.md`、開発用 `pyproject.toml` の
`dev` extra 等）を持ち出さない設計になっているため。これは意図的な設計
判断であり、欠陥ではない: 配布物を最小化することでインストールサイズと
攻撃面を絞り、テスト・開発ツールという「ランタイムに不要な依存」を
利用者の環境へ持ち込まない。

## 決定

**配布 artifact（各ホストへインストールされる plugin tree）に `tests/` を
同梱しない設計を維持する。回帰検知・カバレッジ検証は、常に開発用 source
tree（このリポジトリ自身）で実施する。**

したがって、v0.9.34 検証レポート §8.2「Phase 0: テスト基盤の復元」
（`tests/hooks/test_block_no_verify.py` 等の新規作成、`pytest`/`pytest-cov`
の dev dependency 追加）は不要である。該当する test file の多くは
既に別名（`test_additional_hooks.py` 等）で存在し、本ラウンドの各修正
（H-01/H-02/H-04〜H-07/M-01/M-02/L-01）はその既存 test file を拡張する
形で回帰テストを追加した。

## 検討した代替案

### 代替案 1: 配布 artifact にも `tests/` を同梱する

- 長所: installed tree 単体で `pytest` が動き、実機トラブルシュート時に
  その場で回帰確認ができる。
- 短所: 配布サイズが増え、利用者の環境に開発専用ファイルを持ち込む。
  `runtime-no-venv`（ランタイムは venv も install.sh も持たない）という
  既存の設計原則とも整合しない — テストを実行可能にするには
  `pytest`/`pytest-cov` のインストールも利用者側に要求することになる。
- 却下理由: 配布物の最小化という既存方針を覆すコストに見合う利益がない。
  回帰検知は CI（source tree）で担保すれば十分。

### 代替案 2: installed tree でも動く軽量スモークテストを別途同梱する

- 長所: 配布サイズの増加を抑えつつ、実機での最低限の疎通確認ができる。
- 短所: 「別のテストスイートを二重管理する」という新たな保守コストが
  発生し、source tree のテストとの乖離（片方だけ更新される）リスクを
  抱える。
- 却下理由: ADR-0005 が「実機での動作確認は必要に応じて人間が随時実施する
  運用とし、CI に組み込む自動化の対象からは外す」とすでに決定済み。
  同じ理由でここでも採用しない。

## 結果

### 肯定的

- 配布物の最小化方針を維持できる。
- 検証手順（`cd plugins/bluecore && python3 -m pytest -q --cov`,
  `ruff check plugins/bluecore/src`）は source tree に対して実行する、
  という単一の手順に統一される。

### 否定的

- installed tree だけを見て pytest を実行すると「テストが無い」ように
  見え、今回のような誤検出が今後も起こりうる。

### リスク

- 実機再検証を行う担当者が、この設計判断（配布物にテストを含めない）を
  知らずに installed tree で pytest を実行すると、再び H-03 相当の誤検出を
  報告しうる。本 ADR と `CLAUDE.md` の「変更後は... `.venv` を有効化して
  `python3 -m pytest -q` ...」という手順（source tree 前提）を参照すれば
  防げるが、参照されなければ再発する。
