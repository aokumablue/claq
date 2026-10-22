---
name: html-gen
description: shadcn/ui のデザイントークンで、モダンな Web サイト／ダッシュボードを新規作成する。「shadcn でサイトを」「モダンな HTML ダッシュボードを作って」等で発火。既存サイトの単発修正は /bugfix。
user-invocable: true
---

# shadcn/ui の Web サイト

## 原則: 色はテンプレートから複製し、記憶で書かない

配色は `assets/template/tokens.css` に生成済みで、その正本は
`references/tokens.json`（shadcn/ui レジストリの逐語コピー）。Tailwind 既定色、
`#0d1117`、`#2563eb` の類を 1 つでも手書きすると検査で落ちる
（手書きファイルに生の hex や `oklch()` があること自体が違反）。

| 避ける | 代わりに |
|---|---|
| hex や `oklch()` を手で書く | `var(--primary)` / `var(--chart-N)` を使う |
| ゼロから HTML を書く | `assets/template/` を複製して中身を差し替える |
| `tokens.css` を手で直す | `tokens.json` を直して `--write-css` で再生成する |
| React や Tailwind CLI を足す | 素の CSS のまま（理由は `references/design.md`） |
| トグルを後付けする | テンプレのライト/ダークボタンを残す |

設計原則・レイアウト・コンポーネント・チャートの作法は `references/design.md`。
チャート種別ごとの骨格と寸法は `references/chart-forms.md`。

## 手順

止まって聞くのは手順1 の要件確認だけ。それ以外では、次の手順を予告して終わる要約・続けてよいかの伺い・作業を止めない判断事項の列挙・区切りや長さを理由にした報告で応答を終えず、手順4 の検証が通るまで続ける。

### 1. 要件を確定する

埋まらない項目があれば先に聞く。

- サイト名 / 何を見せるか（指標・内訳・時系列・分布）
- ベースカラー: `neutral`（既定）/ `zinc` / `slate` / `stone` / `gray`
- ページ種別: `dashboard`（既定）か `content`（記事・紹介。指標や図を必須にしない）
- 実データかサンプルか

### 2. テンプレートを複製する

テンプレートはスキル起動時に提示されるベースディレクトリの `assets/template/` にある。提示が無いときは、次が返す plugin root の `skills/html-gen/assets/template/` を使う（失敗したら止めて報告する）。

```bash
. "$HOME/.claq/env.sh" && claq_plugin_root
```

複製先は完全なリテラル絶対パスで書く。

```bash
mkdir -p /abs/path/to/dest
cp /abs/skill/html-gen/assets/template/index.html /abs/path/to/dest/index.html
cp /abs/skill/html-gen/assets/template/styles.css /abs/path/to/dest/styles.css
cp /abs/skill/html-gen/assets/template/tokens.css /abs/path/to/dest/tokens.css
cp /abs/skill/html-gen/assets/template/app.js /abs/path/to/dest/app.js
cp /abs/skill/html-gen/assets/template/check_site.py /abs/path/to/dest/check_site.py
cp /abs/skill/html-gen/assets/template/e2e_contrast.js /abs/path/to/dest/e2e_contrast.js
cp /abs/skill/html-gen/references/tokens.json /abs/path/to/dest/tokens.json
```

### 3. 中身を差し替える

差し替える: タイトル、指標の名前と数値、チャートのデータと `<desc>`、表、
ナビゲーション項目、`html[data-base]`（手順1で選んだベースカラー）、
`html[data-page-kind]`。

触らない: `tokens.css`（生成物）、`app.js`、`button[data-theme-value="light"|"dark"]`、
`<head>` の `app.js` 読み込み位置（body 末尾へ移すと初回に白がちらつく）、
`.chart-scroll` ラッパ、`@media (prefers-reduced-motion: reduce)` ブロック、
`svg[role="img"]` の `<title>`/`<desc>`、`data-origin="0"`。

足さない: 動作の無い操作部品（例: 期間切替・CSV・レポート作成・通知・すべて表示・ヘッダの全体検索）。押しても何も起きない部品は、ページ全体の信頼を損なう。テンプレートも持たない。それ以外の部品は、置くなら動作まで結線する。

サイドバーと一覧は、結線済みの仕組みへ載せるだけにする（`check_site.py` が外れを落とす）。

- サイドバーの項目は `<a class="nav-item" href="#id">` で、飛び先の節に同じ `id` を付ける。`<button>` にしない。飛び先の無い項目は置かない（項目を増やすなら節も増やす）。現在地・狭い画面で閉じる動作・スクロール位置の追従は `app.js` が担う
- 一覧の `<table>` には `data-list` を付け、`<thead>` と `<tbody>` を持たせる。検索欄・主要項目の絞り込み・件数表示は `app.js` が表の直前に生成する。**検索欄（`input[type=search]`・`role="search"`）は HTML に手書きしない**（結線されない欄になる）。絞り込みの列は `app.js` が決める（行見出しでも `td.num` でもなく、全行が値を持ち、異なる値が 2〜6 個で行数より少ない列。状態・ステータス系を先頭に、最大 2 列。全項目の絞り込みは作らない）。ヒートマップ表・図の直下の表（`details.chart-data`）・項目表（本体の全行が行見出し + 値 1 列。会社概要など）は一覧ではないので付けない

足さない（依頼で求められない限り）: 見出しの一部の語だけを斜体にする強調、`01` `02` `03` のような番号付きのセクションラベル、等幅フォントの見出し・ラベル、クリーム色・オフホワイトの背景（背景は `var(--background)` のまま）、ピル形のボタン（新しく足すボタンは `--radius-md`）。どれもテンプレートの外から持ち込まれやすい意匠で、shadcn/ui の見た目から外れる。テンプレートに既にある要素（配色ピッカーの円形ボタン・バッジなど）の形は変えない。

グラフ種別は上から順に当て、最初に当たった行で確定する（詳細と根拠は
`references/design.md`「グラフ種別の選定」）。迷って選び直さない。種別が回ごとに
揺れる原因は知識不足ではなく、同点のときの決め手が無いことである。

| # | データがこうなら | 使う |
|---|---|---|
| 1 | 値が 1 つ | 指標カード（棒 1 本のグラフにしない） |
| 2 | 量的変数 2 つの相関 | 散布図 |
| 3 | 個々を名指ししない多数の観測（注文・取引・セッションなど 30 件以上）の 1 つの量の分布 | ヒストグラム |
| 4 | 分類 2 つの掛け合わせで格子全体の偏り・周期を見せる（曜日×時間帯・コホート×経過月。系列ごとの推移なら時間軸の行（5・6）） | ヒートマップ表 |
| 5 | 時間軸・時点 8 以上（合計に意味がある構成比の推移は規則 8） | 折れ線（主役が 1 系列ならその系列に面を敷く） |
| 6 | 時間軸・時点 7 以下（合計に意味がある構成比の推移は規則 8） | 縦棒 |
| 7 | 部分と全体・分類 5 以下・1 時点 | ドーナツ |
| 8 | 部分と全体・分類 6 以上または複数時点 | 100% 積み上げ横棒 |
| 9 | 大小比較・分類 8 以上またはラベル最長 9 文字以上 | 横棒（降順） |
| 10 | 大小比較（上記以外） | 縦棒 |
| 11 | 意味のある分類が 8 以上で省けない | 表 |

時間は必ず横軸へ置く。例外は規則 8 の構成比で、時点を行として縦に並べる。縦横の向きは見た目の好みで決めない。
選んだ種別の SVG 骨格・viewBox 幅の上限・余白の取り方は `references/chart-forms.md` から写して使う。

軸の無いグラフは出荷しない。直交軸チャートには目盛りラベルと目盛り線を
最低 3 本ずつ（通常 5〜7 本）置き、刻みは 1・2・5 の倍数にする。検査が落とす。

チャートを増減するときの注意（詳細は `references/design.md`）:

- 座標は viewBox 内の数値で持つ。系列色は `var(--chart-1..5)`
- 値はマークの `<title>` に書く（`app.js` がホバー・タップで浮き札にする）。
  時系列は全時点に当たり判定（`.hover-col`）を置き、点を間引いて値の届かない時点を作らない。
  同じ値を図の直下の `<details class="chart-data">` の表にも置く（キーボード・読み上げ向け。
  凡例に値があるドーナツ・積み上げ横棒は不要）
- viewBox の縦横比がそのまま図の縦横比になる。高さの上限に固定 px を書かない
  （広い画面でレターボックスが働き図だけ小さく残る）。`clamp(320px, 46vh, 620px)`
- 図の中の文字は軸名・凡例・値のラベルも含めすべて `<text class="axis-text">`（軸名は `axis-text axis-title`）。`font-size` を個別に書かない（`check_site.py` が落とす）
- x 軸のラベル帯と y 軸の目盛り幅を viewBox の内側に取る（幅は `references/chart-forms.md`「余白の取り方」で数える）
- 静止状態を完成形にする。動きは `[data-animate="in"]` の下だけに書く。
  `opacity: 0` を静止状態に置いたり `animation-fill-mode: forwards` を使うと、
  背面タブ・JS 無効・印刷で真っ白なページになる（検査で落ちる）
- 円弧の開始位置は SVG 属性の `stroke-dashoffset`。CSS で宣言しない
  （CSS が属性より優先され、全セグメントが重なる）
- 潰した SVG（`preserveAspectRatio="none"`）に `stroke-dasharray` のドローを
  使わない。`clip-path` で拭う
- ドーナツ以外の図は `.chart-scroll` で包む（`check_site.py` が落とす）
- `auto-fit` のグリッドに `span` を掛けない。列数を明示する

依頼が指標・内訳・時系列・分布を求めるダッシュボードなら、それぞれ 1 つ以上置く（分布はテンプレートに無いので `references/chart-forms.md` のヒストグラムを写す）。

### 4. 検証する（省略不可）

```bash
python3 /abs/path/to/dest/check_site.py /abs/path/to/dest
```

exit code 0（`PASS`）を確認するまで完了報告しない。`FAIL:` の文面が違反箇所と直し方を示す。

`check_site.py` のコントラスト検査は `tokens.json` 上の値で測るので、描画された姿はブラウザでも確かめる。

1. `python3 -m http.server` で配信して開く（`file://` は localStorage が使えず
   永続を確認できない）
2. DevTools コンソールに `e2e_contrast.js` を貼り付けて実行する。
   5 ベースカラー × ライト/ダークの 10 ブロックを巡回する。`verdict` が `GREEN` なら受け入れ、
   `RED` なら `failures`・`textFailures`・`behaviorFailures` に出た項目を直す。`GREEN` なら、
   図の文字の描画後の大きさ（12〜13px）と、一覧の検索・絞り込みの動作も通っている
3. 幅 320 / 390 / 768 / 1280 / 1920px で `document.documentElement.scrollWidth` が
   `clientWidth` と一致することを見る（横スクロールが出ていない証拠）
4. 再読込してもテーマとベースカラーが保たれ、白がちらつかないことを見る
5. 図が描き切った状態で止まること、モーションを切っても全部読めることを見る
6. サイドバーの全項目を実際にクリックし、その節へスクロールして現在地が付くこと（狭い幅ではサイドバーが閉じること）を見る

## 完了条件

手順4 の `check_site.py` PASS・`e2e_contrast.js` GREEN・確認 3〜6 がすべて通り、一覧に主要項目の絞り込みが生成されている（生成されなくても検査は落ちないので目で確かめる）。

## 永続メモリ

- 参照: `claq_run claq.mem.cli search "shadcn ui web template"`
  （`. "$HOME/.claq/env.sh"` 前提）→ 本文が要る key だけ
  `claq_run claq.mem.cli show <key>`
- 記録: 再利用可能な学びだけ `claq_mem_learn`。基準は `../learn/SKILL.md`
