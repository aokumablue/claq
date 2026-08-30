# ADR-0014: ホスト component inventory をリリースゲートにする（ADR-0005 を supersede）

**日付**: 2026-08-27  **ステータス**: accepted
**関係**: [ADR-0005](0005-runtime-audit-scope-is-static-validators-not-real-install.md) の
「実 install/update smoke test は自動検証の対象外」という費用対効果の判断を supersede する。

## コンテキスト

ADR-0005 は、実 install/update フローと各ホストの hook 登録機構への実登録を CI から外すと決めた。却下理由は次のとおりだった。

> Claude Code / Copilot CLI / Grok それぞれのインストール機構（ディレクトリ構造、
> マーケットプレイス経由の配置、ハッシュ付きディレクトリ等）を CI 環境で再現する
> 必要があり、各ホストのバージョン更新に追従し続ける保守コストが継続的に発生する。

2026-08-26 の実機監査 F-01 は、この決定の帰結を実害として観測した。**Claude Code で 9 エージェントが 1 体も登録されていなかった。** `plugin validate --strict` は成功する一方、ランタイムの loader は manifest の `agents` エントリを directory として `scandir` するため全件 `ENOTDIR` になる。README が第一対象とするホストで、agent へ委譲する全 workflow が静かに機能していなかった。

これが 41 リリースにわたって見過ごされたのは、静的 validator も `claude plugin validate --strict` も PASS するためである。壊れていることを示す唯一の信号は、次の 1 コマンドの出力だった。

```bash
claude --plugin-dir plugins/bluecore plugin details bluecore@inline
```

## 決定

**ホストの component inventory 取得をリリースゲートに含める。** 具体的には次を満たすことをリリース条件とする。

1. `claude plugin validate --strict plugins/bluecore` が PASS する
2. `claude --plugin-dir plugins/bluecore plugin details bluecore@inline` の出力に、実ディレクトリと一致する Skills / Agents 数が現れる
3. debug log に `Failed to read plugin components` が 0 件である

ADR-0005 が却下したのは「install/update フロー全体のエミュレーション」であり、本 ADR が要求するのは**すでにあるツリーを読ませて登録結果を 1 回問い合わせること**だけである。インストール機構の再現もハッシュ付きディレクトリの模倣も要らない。ADR-0005 の比較対象に、この低コスト案は入っていなかった。

**CLI に依存しない静的ゲートも併置する。** `claude` が PATH に無い環境ではホスト確認は実行できず、skip されるゲートはゲートとして機能しない。そのため `plugins/bluecore/tests/test_plugin_manifest.py` で、manifest が `agents` を宣言していないこと（＝ auto-discovery に委ねていること）と、ディスク上に 9 体が実在することを常時検証する。

## 検討した代替案

### 代替案 1: ADR-0005 を維持し、F-01 は一度きりの修正として扱う

- 長所: 追加のゲートを持たずに済む。
- 短所: 検出手段を持たないまま同種の非互換が再発する。今回の実害（component 群がまるごと未登録）は静的検証では原理的に検出できない — manifest も公式 schema も validator も、すべて「正しい」と答えたのだから。
- 却下理由: ADR-0005 は保守コストと得られる保証を比較した判断だが、比較の一方（低コスト案）が評価対象に入っていなかった。前提が違うので結論を引き継げない。

### 代替案 2: 全ホスト（Claude Code / Copilot CLI / Grok）の inventory を必須にする

- 長所: ホスト固有の非互換をすべて同じ強さで検出できる。
- 短所: Copilot CLI と Grok CLI をリリース環境へ常時用意する必要があり、ADR-0005 が指摘した「ホストのバージョン更新に追従し続ける保守コスト」がそのまま復活する。
- 却下理由: README が第一対象とするのは Claude Code であり、そこだけを必須にすれば実害の大半を捕まえられる。他ホストは従来どおり人間の実機再検証に委ねる。

## 結果

### 肯定的

- component がまるごと登録されない種類の非互換が、リリース前に 1 コマンドで検出される。
- `claude` が無い環境でも、manifest 形式の退行は静的テストが検出する。

### 否定的

- Claude Code のバージョン更新で `plugin details` の出力形式が変わると、ゲート側の追従が要る。ADR-0005 が懸念した保守コストは、規模は小さいがゼロではない。
- ゲート実装（`scripts/publish.sh` の `run_host_inventory_gate`）は `Agents (N)` / `Skills (N)` という**出力文字列**と、「ホストは skills/ と commands/ を合算して Skills と数える」という**集計規則**に結合している。ホスト側がどちらを変えても、実体は正常なのに不一致としてリリースを止める。誤メッセージにはホストの実出力を併記してあるため原因は追えるが、追従が要る点は残る。

### リスク

- Copilot CLI と Grok の登録は引き続き自動検証されない。両ホスト固有の非互換は、次の人間による実機再検証まで検出されない。
- inventory は「登録されたか」を見るだけで、「登録されたものが正しく動くか」は見ない。定義本文の欠陥は別の検証（validator・実機再検証）の担当である。
