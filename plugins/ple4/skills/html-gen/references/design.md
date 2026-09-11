# shadcn/ui の静的 HTML への翻訳

## 目次

- CLI ではなくトークンを写す理由
- 色は upstream の逐語コピー。差分は 4 点だけ
- 系列色
- レイアウト
- コンポーネントの作法
- チャートとアニメーション
- グラフ種別の選定
- 軸と目盛り
- チャートの寸法
- タイポグラフィ
- ページ種別
- 完成前のチェックリスト

色の正本は `tokens.json`。ここに色コードを再掲しない。

出典:

- トークン定義: <https://ui.shadcn.com/docs/theming>
- ベースカラーのレジストリ: `https://ui.shadcn.com/r/colors/{neutral,zinc,slate,stone,gray}.json`
- コンポーネント: <https://ui.shadcn.com/docs/components>
- ダッシュボードのブロック: <https://ui.shadcn.com/blocks>
- タイポ・影・ブレークポイント: <https://tailwindcss.com/docs>
- 非テキストコントラスト: <https://www.w3.org/WAI/WCAG21/Understanding/non-text-contrast.html>
- グラフ種別の選定: <https://www.datawrapper.de/blog/chart-types-guide> / <https://datavizcatalogue.com/blog/chart-selection-guide/>
- 棒の向き: <https://www.storytellingwithdata.com/blog/2022/1/21/which-bar-orientation-should-i-use>
- 円・ドーナツの限界: <https://data.europa.eu/apps/data-visualisation-guide/alternatives-to-pie-charts>
- 目盛りの刻み: <https://www.baeldung.com/cs/grid-line-intervals-on-graph>
- 潰し描画と文字の歪み: <https://developer.mozilla.org/en-US/docs/Web/SVG/Reference/Attribute/preserveAspectRatio>

## CLI ではなくトークンを写す理由

shadcn/ui は「npm パッケージ」ではなく、React + Tailwind CSS + Radix UI の
ソースを自分のリポジトリへコピーして使う配布形式をとる。したがって
`npx shadcn add button` の出力は React コンポーネントで、静的 HTML には載らない。
Tailwind のユーティリティも、ビルド工程なしでは解決されない。

一方で shadcn/ui の見た目を決めているのは **CSS カスタムプロパティの層**
（`--background` `--primary` `--muted` `--border` `--ring` `--radius` `--chart-*`
`--sidebar-*`）と、その上に載る**視覚仕様**（1px のヘアライン、抑えた影、
`tracking-tight` の見出し、`focus-visible` のリング）である。これらは素の CSS に
そのまま書ける。

そこで本テンプレートは、レジストリが配る**トークンの実値**と、コンポーネントの
**視覚仕様**を素の CSS へ写す。外部 JS はゼロ、CDN は Google Fonts だけで、
生成した HTML はブラウザで開けばそのまま動く。

**React を足して「本物の shadcn にする」改修はしない。** 静的 1 枚で完結する
という前提が崩れ、ビルド工程とパッケージ管理がスキルの成果物に入り込む。

## 色は upstream の逐語コピー。差分は 4 点だけ

`tokens.json` の `bases` は 5 つのベースカラー（neutral / zinc / slate / stone /
gray）× ライト/ダークを、レジストリの `cssVarsV4` から**そのまま**写したもの。
`oklch()` の値を書き換えない。ブラウザは OKLCh をネイティブに解釈し、広色域の
ディスプレイでは P3 で描く。検査は sRGB へ落とした最悪ケースで行う。

upstream をそのまま使えない箇所だけ、規則として差分を持つ。理由は
`tokens.json` の各項目の `reason` にも書いてある。

| 対象 | upstream | 差分 | 根拠 |
|---|---|---|---|
| `ring` / `sidebar-ring` | ライトで背景に 2.58〜2.63:1 | 色相・彩度を保ち明度だけ下げて 3:1 | フォーカスの可視性（WCAG 2.1 SC 1.4.11） |
| `muted-foreground` | ライトで muted 面に 4.35〜4.41:1 | 同上で 4.5:1 | 本文相当の文字 |
| `chart-1..5` | neutral/zinc/stone はグレースケール、相互 ΔE\*ab が 8 前後。ライトの chart-1 は白背景に 1.48:1 | 色相と彩度は upstream のダークセット、明度はテーマ固定値 | 系列が判別できない・描いても見えない |
| `destructive-foreground` | v4 のレジストリに無い（React 側で `text-white` を当てる） | ライトは白、ダークは暗色 | 静的 CSS には変数が要る。ダークの destructive は明るい赤なので前景は暗色 |

`border` / `input` / `sidebar-border` は 3:1 契約から**外す**。1px の
ヘアラインでコンポーネントの状態を伝えてはおらず、SC 1.4.11 の対象ではない。
ただし背景と同値になると面が消えるので、`decorative_roles` の
`min_ratio` で「同化していないこと」だけ別に課す。

差分の適用は `check_site.py` の `scheme` が行い、明度を下げるのは
`_lower_lightness_until`。域外へ出た彩度は `fit_chroma` が二分探索で落とす
（明度は動かさない。動かすとコントラスト契約が崩れる）。

## 系列色

- 色相は upstream のダークセット（5 色が色相環に散る）を**両テーマ共通**で使う。
  upstream はライトとダークで色相が入れ替わり、テーマを切り替えると同じ系列の
  色が変わってしまう。データ可視化としては誤りなので採らない
- 明度はライト 0.58 / ダーク 0.75 の固定。背景・カード・muted のどの面に対しても
  3:1 以上、相互の ΔE\*ab は 20 以上（実測で 31 以上）
- 塗り面は不透明度で薄めない。SVG のグラデーション（`stop-opacity`）か
  `color-mix(in oklab, var(--chart-1) N%, var(--card))` で作る
- **1 系列の棒グラフに 5 色を使わない。** 色は系列を区別するためのもので、
  同じ指標のカテゴリ違いに色を割り当てると意味のない色分けになる

## レイアウト

- ブレークポイントは Tailwind（sm 640 / md 768 / lg 1024 / xl 1280 / 2xl 1536）。
  手書き CSS は 768 / 1024 / 1280 を必ず扱う
- シェルは「サイドバー + コンテンツ」。`md` 未満ではサイドバーをオフキャンバスにし、
  ハンバーガーとスクリムで開閉する（Esc でも閉じる）
- ペインは `max-inline-size: var(--layout-pane-max)` + `margin-inline: auto`、
  ガターは `clamp()`
- グリッドのトラックは必ず `minmax(0, 1fr)`。`auto` のままだと中身の max-content
  までトラックが伸びて横スクロールが出る
- `auto-fit` のグリッドに `grid-column: span N` を掛けない。列が 1 本しかない
  ブレークポイントで暗黙列が content 幅で生えて、ページごと横に伸びる。
  スパンを使うなら列数を明示する（`repeat(12, minmax(0, 1fr))`）
- カード内の出し分けは `@container`（`container-type: inline-size`）。
  **コンテナクエリはコンテンツボックス基準**なので、パディング分を引いた値で
  閾値を決める

## コンポーネントの作法

- **カード**: `1px` の `--border`、`--radius-xl`、`--shadow-xs`。ホバーで
  `--shadow-sm` まで。M3 のような濃い影は使わない
- **ボタン**: 高さ 36px、`--radius-md`、`--text-sm` の `medium`。
  primary は `--primary` 面、outline は `--border` + `--background`、
  ghost は面なし。アイコンのみのボタンは疑似要素で当たり判定を
  `--layout-touch-target` まで広げる（見た目は 36px のまま）
- **フォーカス**: `:focus-visible` に `--ring` の 2px アウトライン + オフセット。
  入力欄は `--ring` のボーダー + `--state-ring-width` の外周
- **バッジ**: 色は良し悪し、中のアイコンは増減の向き。この 2 つを同じ軸で表すと
  「解約率が下がった（良い）」が赤で出るような反転が起きる
- **セグメント**: `--muted` の器に、選択中だけ `--background` + `--shadow-xs`
- **表**: ヘッダは `--muted-foreground` の `--text-xs`、行区切りは `--border` を
  薄めたもの。数値列は `font-variant-numeric: tabular-nums` で桁を揃える
- **サイドバー**: `--sidebar-*` の一式を使う。本文側の `--background` を流用しない

## チャートとアニメーション

**静止状態が完成形。** 動きは JS が画面内で `data-animate="in"` を付けたときだけ
起き、走り終わったら属性ごと外れる。この形にしないと次のいずれかで図が消える。

- 背面タブ: アニメーションのタイムラインが進まないため、`from` の状態で固まる
- JS 無効・印刷: 属性が付かない
- モーション低減: `@media (prefers-reduced-motion: reduce)` で止まる

したがって `.reveal { opacity: 0 }` のように静止状態を不可視にしてはならず、
`animation-fill-mode: forwards` も使わない（`check_site.py` の
`_validate_resting_state` が両方を機械的に落とす）。JS 側はさらに、タブが背面の
間は属性を付けず `visibilitychange` まで待つ。

動きの作り方:

- **折れ線**: `stroke-dasharray` にパス長、`@keyframes` で `stroke-dashoffset` を
  パス長 → 0。静止値は 0（＝描き切った状態）
- **面**: `transform-box: fill-box` + `transform-origin: bottom` で `scaleY`
- **棒**: 同上。`--i` で 55ms ずつずらす
- **円弧（ドーナツ）**: 開始位置は SVG 属性の `stroke-dashoffset` が持つ。
  **CSS で `stroke-dashoffset` を宣言しない** — CSS は presentation attribute より
  優先されるため、全セグメントが 12 時から重なって描かれる。伸ばすのは
  `stroke-dasharray` のほう（`0 円周` → `弧長 残り`）
- **スパークライン**: `preserveAspectRatio="none"` で潰す図に
  `stroke-dasharray` のドローは使えない。潰すとパスの実長が dasharray とずれ、
  線が途中で切れて見える。`clip-path: inset(0 100% 0 0)` から拭う。
  線幅だけ `vector-effect: non-scaling-stroke` で保つ
- **強調**: `.chart:has(.bar:hover) .bar:not(:hover) { opacity: .32 }` のように
  `:has` で兄弟を沈める。JS を足さずに系列のフォーカスが作れる
- **数値**: カウントアップは `data-count` に最終表記を持たせ、途中で止まっても
  最終値へ戻せるようにする。モーション低減時は動かさない

## グラフ種別の選定

**上から順に当て、最初に当たった行で確定する。** 迷ったら選び直すのではなく、
先に当たった行を採る。この順序を守らないと、同じ形のデータから回ごとに違う
グラフが出る。種別が揺れる原因は知識の不足ではなく、**同点のときの決め手が
無いこと**である。

| # | データがこうなら | 使う | 使わない |
|---|---|---|---|
| 1 | 値が 1 つ（現在値と増減だけ） | 指標カード（数値＋差分＋スパークライン） | 棒 1 本のグラフ |
| 2 | 量的変数が 2 つあり相関を見せたい | 散布図 | 折れ線 |
| 3 | 横軸が時間で、時点が 8 以上 | 折れ線（1 系列なら面を敷く） | 棒 |
| 4 | 横軸が時間で、時点が 7 以下 | 縦棒 | 折れ線 |
| 5 | 合計に意味があり（部分と全体）、分類 5 以下・1 時点 | ドーナツ | 分類 6 以上のドーナツ |
| 6 | 合計に意味があり、分類 6 以上または複数時点 | 100% 積み上げ横棒 | 円・ドーナツ |
| 7 | 分類ごとの大小比較で、分類 8 以上またはラベル最長 9 文字以上 | 横棒（降順に並べる） | 縦棒 |
| 8 | 分類ごとの大小比較（上記以外） | 縦棒 | 横棒 |
| 9 | 意味のある分類が 8 以上で、どれも省けない | 表（必要なら表＋図） | 色を増やす |

しきい値の出どころ:

- **時点 7 / 8 の境**: 少数の時点は棒、多数は折れ線。点が少ないと折れ線の
  「間を補間している」含意が嘘になり、点が多いと棒が細くなって形が読めない
- **ドーナツ 5 分類**: 角度と面積の比較は長さの比較より精度が落ちる。3〜5
  分類で、かつ差が大きいときだけ成立する。6 以上・複数時点・微差の比較は
  積み上げ横棒に倒す
- **横棒 8 分類 / ラベル 9 文字**: 縦棒は分類が増えるとラベルを回転させるか
  省略することになる。回転した文字は読む速度が落ちる。日本語ラベルは全角
  なので、8 文字（「オーガニック検索」）で既に縦棒の限界に達する
- **8 分類で表**: 色で区別できる上限を超えている。9 色目を作って解決しない

**縦か横かはデータが決める。** 見た目の好みで選ばない。時間は必ず横軸へ置き
（左から右へ流れる）、時間でない分類が多いか長いときだけ横棒へ倒す。

系列数が増えたときの扱い:

- 1〜3 系列: 色だけで区別してよい。端点に直接ラベルを置く
- 4〜6 系列: 凡例を必ず置く。直接ラベルは要点だけに絞る
- 7 系列以上: 上位だけ残して残りを「その他」へ畳むか、小さな図を並べる。
  **色を増やして解決しない**
- 1 系列が主役で残りが背景なら、主役だけ着色して残りを灰へ沈める（強調）。
  全系列を色分けすると、伝えたい 1 本が埋もれる

## 軸と目盛り

**軸の無いグラフは出荷しない。** 目盛りが無い図は大小しか伝えず、しかも読めた
気にさせる分だけ数値表だけより悪い。`check_site.py` の `_validate_chart_axes`
が直交軸チャート 1 枚あたり目盛りラベル 3 個・目盛り線 3 本を下限として機械的に
落とす（下限であって目標ではない。通常は 5〜7 本）。

- **目盛りの刻みは 1・2・5 の倍数**（10 の冪を掛けたもの）。0 / 50 / 100 / 150
  のように読める値にする。データの最大値を本数で割った端数（0 / 47 / 94）は使わない
- **本数は 5〜7 本を基準**に、下限 3 本・上限 12 本。多すぎると線がデータを
  埋め、少なすぎると間隔が読めない
- **棒の原点は必ず 0**（`data-origin="0"`）。折れ線は 0 でなくてよいが、0 でない
  なら軸の下端を明示する
- **目盛り線と軸線は実線のヘアライン**（1px、面から 1 段外した色）。破線にしない
  — 破線は「予測値」「しきい値」の記法として読まれ、ただの目盛りに意味を足す
  （`_validate_chart_chrome` が落とす）
- **数値の目盛りは `tabular-nums`**。桁が縦に揃う
- 軸ラベルに系列色を使わない。色を持つのはマークだけで、文字は
  `--muted-foreground`

## チャートの寸法

- **viewBox の縦横比が、そのまま図の縦横比になる。** `block-size: auto` の SVG は
  幅に対して viewBox の比率どおりの高さを取る。ここへ固定 px の上限を掛けると、
  広い画面ほど `preserveAspectRatio` のレターボックスが働いて**図だけが小さく
  残る**。上限はビューポートに追従させる（`clamp(320px, 46vh, 620px)`）。
  `_validate_fluid_height` が固定 px の上限を落とす
- **x 軸のラベル帯を図の寸法の内側に入れる。** 折れ線・棒の viewBox は、
  プロット領域の下にラベル 1 行分（20px 前後）、左に目盛り値の幅（40〜55px）を
  確保した大きさで切る。確保しないとカードの中に入れ子のスクロールが出る
- **SVG の文字は viewBox の倍率で一緒に縮む。** 宣言が 12px でも、幅 720 の
  viewBox が 460px で描かれれば実効 7.7px になる。目盛りはあるのに数値が読めない
  状態はこれで起きる。手書き CSS の 12px 検査は**宣言しか見ない**ので素通りする。

  実効サイズ = 宣言サイズ × 最小描画幅 ÷ viewBox 幅

  この値が 12px を下回らないよう、`font-size` を上げるか viewBox を狭める。
  `.axis-text` が 15px なのは、幅 720 の viewBox が最小 600px で描かれるときに
  12.5px を確保するため（`_validate_axis_text_scale` が計算して落とす）。
  **viewBox 幅は「その図が実際に占める幅」に近づけるほど破綻しにくい。**
  狭い側カードへ幅 720 の図を入れると、文字は半分以下に縮む
- **チャートは必ず `.chart-scroll` で包む**（ドーナツを除く）。`min-inline-size`
  がカード幅を超えたとき、器が無いと SVG がページごと横へはみ出す（実測: 幅
  1269px の画面で文書幅 1422px）。器があれば、はみ出しはカードの中の横スクロール
  に閉じる（`_validate_chart_scroll_wrapper` が落とす）
- 時系列は狭い幅で等倍のまま横スクロールさせる（`.chart-scroll` +
  `min-inline-size`）。縮めて軸文字を潰さない
- **`preserveAspectRatio="none"` は文字を持たない図にだけ使う。** 縦横独立に
  伸ばすので `<text>` が歪む。スパークライン専用で、軸を持つチャートには
  使わない（`_validate_stretched_svg_text` が落とす）
- `svg[role="img"]` に `<title>` と `<desc>`。図と同じ内容を表でも出す
- データ点に `<title>` を付ければ、追加のライブラリなしでツールチップになる

## タイポグラフィ

- スケールは Tailwind（`--text-xs` 〜 `--text-5xl` と対応する `-line`）
- 見出しは `--tracking-tight` + `--font-weight-semibold`
- 本文・UI は `--text-sm`。数値は `tabular-nums`
- ヒーローの見出しなど「効かせたい」文字だけ `clamp()` の流体サイズ
- 12px 未満は使わない（`check_site.py` が拒否する）

## ページ種別

- `data-page-kind="dashboard"`（既定）: 指標・SVG・原点 0・表が検査対象
- `data-page-kind="content"`: 紹介・記事。配色とトグルは同じ。偽の指標を足さない

## 完成前のチェックリスト

- 全体像 → 内訳の順に目に入るか
- 図と凡例が隣接し、図と同じ内容を数値でも読めるか
- 比較対象（前年・目標）があるか
- 更新時点が書いてあるか
- 色だけに頼らない区別があるか（符号・ラベル・凡例）
- 増減の向きと良し悪しを取り違えていないか
- ライト/ダークをボタンで選べ、再読込後も保たれるか
- 320px から 1920px まで横スクロールが出ないか
- アニメーションを止めても、図と数値が全部読めるか
