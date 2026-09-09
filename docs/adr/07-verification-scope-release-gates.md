# 検証範囲とリリースゲート

何を CI で自動検証し、何をリリースゲートにし、何を人間の実機再検証に委ねるか。および配布物に何を
含めないか。

| ADR | 決定 | 日付 | ステータス |
|---|---|---|---|
| [ADR-0005](#adr-0005-実機ランタイム監査の検証範囲は静的-validator-に限定し実-installupdate-smoke-test-は対象外とする) | CI の検証範囲は `hooks.json` の静的 validator に限定する | 2026-08-18 | superseded（費用対効果の判断のみ。ADR-0014） |
| [ADR-0006](#adr-0006-配布-artifact-にテストを同梱せず検証は-source-tree-で行う) | 配布 artifact に `tests/` を同梱しない。検証は常に source tree で行う | 2026-08-20 | accepted |
| [ADR-0014](#adr-0014-ホスト-component-inventory-をリリースゲートにする) | ホスト component inventory の取得をリリースゲートに含める（ADR-0005 を supersede） | 2026-08-27 | accepted |

## 共通原則

**skip されるゲートはゲートとして機能しない。** `claude` が PATH に無い環境ではホスト確認は実行できず、
そこで skip されるだけのゲートは信頼を作らない。したがってホスト依存のゲートには必ず**CLI に依存しない
静的ゲートを併置**する（ADR-0014）。同じ論理は [ADR-0016 / ADR-0022 代替案](06-staleness-detection.md) が
「常に走らないものを CI ゲートに見せかけない」として繰り返し適用している。

**費用対効果の判断は、比較対象が正しかったときにだけ引き継げる。** ADR-0005 が却下したのは
「install/update フロー全体のエミュレーション」であり、「すでにあるツリーを読ませて登録結果を 1 回問い
合わせるだけ」の低コスト案は比較対象に入っていなかった。前提が違えば結論は引き継げない（ADR-0014
代替案 1）。

**配布物の最小化は「散文の警告」では守れない。** ADR-0006 のリスク節が予言した誤検出は 3 回現実化し、
3 回目の監査者は ADR を読んだ上でなお誤検出を報告した。構造的に発生しない形へ直すまで終わらない。

---

## ADR-0005: 実機ランタイム監査の検証範囲は静的 validator に限定し、実 install/update smoke test は対象外とする

**日付**: 2026-08-18  **ステータス**: superseded（費用対効果の判断のみ。ADR-0014）

> **注記（2026-08-27）**: 本 ADR の「実 install/update smoke test は自動検証の対象外」という費用対効果の判断は
> ADR-0014 で撤回した。2026-08-26 の実機監査 F-01 で、Claude Code 上で 9 エージェントが 1 体も登録されていない
> 状態が 41 リリース見過ごされていたことが判明し、それを `claude --plugin-dir ... plugin details` の 1 コマンドで
> 検出できたため。本 ADR が却下したのは「install/update フロー全体のエミュレーション」であり、component
> inventory だけを問い合わせる低コスト案は比較対象に入っていなかった。**静的 validator に関する記述は引き続き
> 有効。**

### コンテキスト

`hooks.json` は PreToolUse（4 フック）・PreCompact・SessionStart・SessionEnd の複数イベントで hook を登録する。
これらが実際に各ホスト（Claude Code / Copilot CLI / Grok 等）へ正しくインストールされ、宣言どおりに登録・起動
されることを確認する最も確実な方法は、実際にプラグインを install/update し、各ホストの hook 登録機構に載せて
smoke test することである。

v0.9.32 検証 §7-3 はこの実 install/update smoke test が未実施であると指摘した。この作業ではスコープを大きく
超えると判断し、代わりに `test_validate_hooks.py`（`hooks.json` が全イベント経路を宣言していること、全エントリが
`timeout` を持つことの静的検証）へ縮退した。

### 決定

**CI・自動検証範囲は、`hooks.json` の構造的な健全性を検証する静的 validator に限定する。実際のプラグイン
install/update フローや、各ホストの hook 登録機構への実登録を検証する smoke test は、自動検証の対象に含めない。**

これは実機での動作確認そのものを不要とする決定ではない。実機再検証は必要に応じて人間が随時実施する運用とし、
CI に組み込む自動化の対象からは外す。

### 検討した代替案

#### 代替案 1: 各ホストの install/update を CI でエミュレートする smoke test を実装する

- 長所: `hooks.json` の宣言が実際にホストへ反映されることまで自動検証できる
- 短所: Claude Code / Copilot CLI / Grok それぞれのインストール機構（ディレクトリ構造、マーケットプレイス経由の
  配置、`grok_plugin_root.find_latest_installed_ple4` が扱うハッシュ付きディレクトリ等）を CI 環境で再現する必要が
  あり、各ホストのバージョン更新に追従し続ける保守コストが継続的に発生する。ホストの内部実装（非公開の場合が
  ある）に CI が依存することになり、壊れやすい
- 却下理由: スコープと保守コストが本プロジェクトの検証範囲を大きく超える。静的 validator で得られる保証と、実
  install smoke test で追加的に得られる保証の差分に対して、コストが見合わない

#### 代替案 2: 実 install/update smoke test を人間の手動チェックリストとして文書化し、リリースごとに実施を義務付ける

- 長所: 自動化コストをかけずに、リリース前の確認は担保できる
- 短所: 手動チェックリストは実施漏れが起きやすく、実施したかどうかを機械的に検証できない
- 却下理由: 今回は「実機再検証セッション」という形で人間が随時実施しており、これを機械的な必須ゲートにする効果は
  限定的。将来リリースプロセスが成熟した段階で改めて検討する余地は残すが、現時点では義務化しない

### 結果

**肯定的** — CI の検証範囲が明確になり、`hooks.json` の宣言漏れ（イベント未登録・`timeout` 未設定）は機械的に検出
され続ける。ホストの内部実装変更に CI が追従する必要が無く、保守コストを抑えられる。

**否定的** — `hooks.json` の宣言が構造的に正しくても、実際のホストへの登録・起動が正しく行われる保証は自動検証され
ない。ホスト側の仕様変更による実害は、次の実機再検証セッションまで検出されない。

**リスク** — A-07（Copilot agent shell に `CLAUDE_PLUGIN_ROOT` が無い）のような、ホスト固有の実行環境差に起因する
不具合は静的 validator では検出できない。実機再検証の頻度・タイミングは人間の判断に委ねられており、次の再検証まで
同種の不具合が見過ごされるリスクを受容する。

---

## ADR-0006: 配布 artifact にテストを同梱せず、検証は source tree で行う

**日付**: 2026-08-20  **ステータス**: accepted

### コンテキスト

v0.9.34 時点のランタイム監査レポートの H-03 は「release tree に pytest テストが無く、回帰検知を実行できない」と
指摘した。検証者は `~/.copilot/installed-plugins/ple4/ple4`（インストール済みの plugin tree）で
`cd plugins/ple4 && PYTHONPATH=src python3 -m pytest -q` を実行し、「collected 0 items」で exit 5 になったことを
根拠にした。

同一の再検証を **source tree**（`ple4-dev` リポジトリの `plugins/ple4`）で実施したところ:

```
$ cd plugins/ple4 && python3 -m pytest -q --cov
...
Required test coverage of 100.0% reached. Total coverage: 100.00%
```

（件数と statements 数は書かない。書けば必ず陳腐化し、次の監査が「ADR の数値と合わない」を欠陥として報告する材料に
なる。実測は常に上記コマンドで取る。）

`tests/` 配下に test file が存在し、`pyproject.toml` は `[project.optional-dependencies].dev` に `pytest>=8.0` /
`pytest-cov>=4.0` / `ruff>=0.4` / `vulture>=2.0` を既に宣言している。coverage 設定（`fail_under = 100`）も存在する。
つまり H-03 が指摘した「テスト基盤が存在しない」状態は、**source tree には最初から存在しなかった**。

検証者が pytest を実行した installed tree には `tests/` が同梱されていない。これはインストール処理が配布対象
サブセット（`agents/` / `commands/` / `hooks/` / `runtime/` / `skills/` / `src/`）だけを配置し、開発用アーティ
ファクトを持ち出さない設計になっているため。これは意図的な設計判断であり、欠陥ではない: 配布物を最小化することで
インストールサイズと攻撃面を絞り、テスト・開発ツールという「ランタイムに不要な依存」を利用者の環境へ持ち込まない。

### 決定

**配布 artifact（各ホストへインストールされる plugin tree）に `tests/` を同梱しない設計を維持する。回帰検知・
カバレッジ検証は、常に開発用 source tree（このリポジトリ自身）で実施する。**

したがって、v0.9.34 検証レポート §8.2「Phase 0: テスト基盤の復元」（`tests/hooks/test_block_no_verify.py` 等の新規
作成、`pytest`/`pytest-cov` の dev dependency 追加）は不要である。該当する test file の多くは既に別名
（`test_additional_hooks.py` 等）で存在し、本ラウンドの各修正はその既存 test file を拡張する形で回帰テストを追加した。

### 検討した代替案

#### 代替案 1: 配布 artifact にも `tests/` を同梱する

- 長所: installed tree 単体で `pytest` が動き、実機トラブルシュート時にその場で回帰確認ができる
- 短所: 配布サイズが増え、利用者の環境に開発専用ファイルを持ち込む。`runtime-no-venv`（ランタイムは venv も
  install.sh も持たない）という既存の設計原則とも整合しない — テストを実行可能にするには `pytest`/`pytest-cov` の
  インストールも利用者側に要求することになる
- 却下理由: 配布物の最小化という既存方針を覆すコストに見合う利益がない。回帰検知は CI（source tree）で担保すれば十分

#### 代替案 2: installed tree でも動く軽量スモークテストを別途同梱する

- 長所: 配布サイズの増加を抑えつつ、実機での最低限の疎通確認ができる
- 短所: 「別のテストスイートを二重管理する」という新たな保守コストが発生し、source tree のテストとの乖離（片方だけ
  更新される）リスクを抱える
- 却下理由: ADR-0005 が「実機での動作確認は必要に応じて人間が随時実施する運用とし、CI に組み込む自動化の対象からは
  外す」とすでに決定済み。同じ理由でここでも採用しない

### 結果

**肯定的** — 配布物の最小化方針を維持できる。検証手順（`cd plugins/ple4 && python3 -m pytest -q --cov`、
`ruff check plugins/ple4/src`）は source tree に対して実行する、という単一の手順に統一される。

**否定的** — installed tree だけを見て pytest を実行すると「テストが無い」ように見え、今回のような誤検出が今後も
起こりうる。

**リスク** — 実機再検証を行う担当者が、この設計判断を知らずに installed tree で pytest を実行すると、再び H-03 相当の
誤検出を報告しうる。

> **このリスクは 3 回現実化した**（H-03 → v0.9.34 後の再検証 → 2026-08-26 監査の F-02）。3 回目の監査者は本 ADR を
> 読んだ上でなお誤検出を報告している（`docs/` は配布対象から除外されないため、本 ADR は配布ツリーにも載っていた）。
> **散文の警告では止まらないことが実測で示された。**
>
> 根本原因は、配布ツリーへ `pyproject.toml` をそのまま持ち出していたことにある。`testpaths = ["tests"]` と
> `fail_under = 100` が除去済みの `tests/` を指したまま残るため、配布ツリーで `pytest` を叩くと「coverage 0% で
> FAIL」という**回帰そっくりの派手な失敗**が出た。2026-08-27 に `scripts/publish.sh` の除外リストへ
> `plugins/ple4/pyproject.toml` を追加し、この出力が構造的に発生しないようにした（配布ツリーで pyproject が果たす
> 役割はゼロ — ランタイム依存はゼロで、`launcher.py` が `sys.path` へ `src/` を挿すだけであり、ホストが読むのは
> `.claude-plugin/plugin.json`）。除外リストの定義は `plugins/ple4/tests/scripts/test_publish_script.py` が機械的に
> 固定する。

---

## ADR-0014: ホスト component inventory をリリースゲートにする

（ADR-0005 を supersede）

**日付**: 2026-08-27  **ステータス**: accepted
**関係**: ADR-0005 の「実 install/update smoke test は自動検証の対象外」という費用対効果の判断を supersede する。

### コンテキスト

ADR-0005 は、実 install/update フローと各ホストの hook 登録機構への実登録を CI から外すと決めた。却下理由は
「各ホストのインストール機構を CI 環境で再現する必要があり、保守コストが継続的に発生する」だった。

2026-08-26 の実機監査 F-01 は、この決定の帰結を実害として観測した。**Claude Code で 9 エージェントが 1 体も登録
されていなかった。** `plugin validate --strict` は成功する一方、ランタイムの loader は manifest の `agents`
エントリを directory として `scandir` するため全件 `ENOTDIR` になる。README が第一対象とするホストで、agent へ委譲
する全 workflow が静かに機能していなかった。

これが 41 リリースにわたって見過ごされたのは、静的 validator も `claude plugin validate --strict` も PASS するため
である。壊れていることを示す唯一の信号は、次の 1 コマンドの出力だった。

```bash
claude --plugin-dir plugins/ple4 plugin details ple4@inline
```

### 決定

**ホストの component inventory 取得をリリースゲートに含める。** 具体的には次を満たすことをリリース条件とする。

1. `claude plugin validate --strict plugins/ple4` が PASS する
2. `claude --plugin-dir plugins/ple4 plugin details ple4@inline` の出力に、実ディレクトリと一致する Skills /
   Agents 数が現れる
3. debug log に `Failed to read plugin components` が 0 件である

ADR-0005 が却下したのは「install/update フロー全体のエミュレーション」であり、本 ADR が要求するのは**すでにある
ツリーを読ませて登録結果を 1 回問い合わせること**だけである。インストール機構の再現もハッシュ付きディレクトリの
模倣も要らない。

**CLI に依存しない静的ゲートも併置する。** `claude` が PATH に無い環境ではホスト確認は実行できず、skip される
ゲートはゲートとして機能しない。そのため `plugins/ple4/tests/test_plugin_manifest.py` で、manifest が `agents` を
宣言していないこと（＝ auto-discovery に委ねていること）と、ディスク上に 9 体が実在することを常時検証する。

### 検討した代替案

#### 代替案 1: ADR-0005 を維持し、F-01 は一度きりの修正として扱う

- 長所: 追加のゲートを持たずに済む
- 短所: 検出手段を持たないまま同種の非互換が再発する。今回の実害（component 群がまるごと未登録）は静的検証では
  原理的に検出できない — manifest も公式 schema も validator も、すべて「正しい」と答えたのだから
- 却下理由: ADR-0005 は保守コストと得られる保証を比較した判断だが、比較の一方（低コスト案）が評価対象に入って
  いなかった。前提が違うので結論を引き継げない

#### 代替案 2: 全ホスト（Claude Code / Copilot CLI / Grok）の inventory を必須にする

- 長所: ホスト固有の非互換をすべて同じ強さで検出できる
- 短所: Copilot CLI と Grok CLI をリリース環境へ常時用意する必要があり、ADR-0005 が指摘した保守コストがそのまま
  復活する
- 却下理由: README が第一対象とするのは Claude Code であり、そこだけを必須にすれば実害の大半を捕まえられる。他ホスト
  は従来どおり人間の実機再検証に委ねる

### 結果

**肯定的** — component がまるごと登録されない種類の非互換が、リリース前に 1 コマンドで検出される。`claude` が無い
環境でも、manifest 形式の退行は静的テストが検出する。

**否定的**

- Claude Code のバージョン更新で `plugin details` の出力形式が変わると、ゲート側の追従が要る。ADR-0005 が懸念した
  保守コストは、規模は小さいがゼロではない
- ゲート実装（`scripts/publish.sh` の `run_host_inventory_gate`）は `Agents (N)` / `Skills (N)` という**出力文字列**と、
  「ホストは skills/ と commands/ を合算して Skills と数える」という**集計規則**に結合している。ホスト側がどちらを
  変えても、実体は正常なのに不一致としてリリースを止める。誤メッセージにはホストの実出力を併記してあるため原因は
  追えるが、追従が要る点は残る

**リスク**

- Copilot CLI と Grok の登録は引き続き自動検証されない。両ホスト固有の非互換は、次の人間による実機再検証まで検出
  されない
- inventory は「登録されたか」を見るだけで、「登録されたものが正しく動くか」は見ない。定義本文の欠陥は別の検証
  （validator・実機再検証）の担当である
