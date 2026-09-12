# アーキテクチャ決定記録（ADR）

本プロジェクトのアーキテクチャ上の決定を記録するインデックス。フォーマットは [template.md](template.md) を参照。

**1 決定 = 1 ファイルではなく、役割（トピック）ごとの 1 ファイルに、見出し節として収める。**
ファイル名に連番は振らない。トピックが増えても既存ファイルの構成は変えず、末尾に節を足す。

**各トピックファイルは、他のトピックファイルを読まなくても理解できる 1 本の文章として書く。**
「◯◯を参照」のような他ファイルへの依存は作らない。他トピックでも成り立つ前提が要るときは、
その前提をこのファイルの「共通原則」に自分の言葉で書く（他ファイルの共通原則と文言が重複しても
よい）。

本文には**現行の状態**と**現在も有効な判断基準**だけを書く。「過去にこうだった」経緯・改訂履歴・
バージョン番号・採番 ID は置かない。別案が提案されたときの現行の回答は、決定そのものの一部として
1 文で書く（別案ごとに節を立てない）。

## トピック

| ファイル | 扱う問い |
|---|---|
| [hook-failure-direction.md](hook-failure-direction.md) | 検査を完了できないとき allow と deny のどちらへ倒すか。exit code は何を表すか |
| [shell-analysis-boundary.md](shell-analysis-boundary.md) | シェルコマンドをどこまで解析し、解析できないものをどちらへ倒すか |
| [plugin-root-resolution.md](plugin-root-resolution.md) | どの plugin install を実行するかをどう決め、どこまで検証するか |
| [untrusted-input-prompt-boundary.md](untrusted-input-prompt-boundary.md) | 信頼できない入力をプロンプト境界へ通すかどうか |
| [definition-and-subagent-design.md](definition-and-subagent-design.md) | 定義文書を何体に分け、中身に何を書き何を書かないか |
| [staleness-detection.md](staleness-detection.md) | 書いたものが黙って古くなる経路をどう塞ぐか。実測できないものをどうするか |
| [verification-scope-release-gates.md](verification-scope-release-gates.md) | 何を CI で検証し、何をリリースゲートにし、何を配布しないか |

## 新しい決定を追加するとき

1. 扱う問いに最も近いトピックファイルへ見出し節として追記・修正する。どのトピックにも属さない
   ときだけ、新しいトピックファイル（連番なしの `<slug>.md`）を作り、上の表に行を足す
2. 既存の決定を撤回する場合は、現行の規則へ本文を書き換える。撤回前の記述・改訂履歴は残さない
