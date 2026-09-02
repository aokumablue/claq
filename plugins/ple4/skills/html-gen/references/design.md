# shadcn/ui の静的 HTML への翻訳

色の正本は `tokens.json`。ここに色コードを再掲しない。

出典:

- トークン定義: <https://ui.shadcn.com/docs/theming>
- ベースカラーのレジストリ: `https://ui.shadcn.com/r/colors/{neutral,zinc,slate,stone,gray}.json`
- コンポーネント: <https://ui.shadcn.com/docs/components>
- ダッシュボードのブロック: <https://ui.shadcn.com/blocks>
- タイポ・影・ブレークポイント: <https://tailwindcss.com/docs>
- 非テキストコントラスト: <https://www.w3.org/WAI/WCAG21/Understanding/non-text-contrast.html>

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

## チャートの可読性

- SVG は `width: 100%` だけだと正方形の図が幅いっぱいに膨らむ。`max-block-size` で止める
- 縮小すると SVG 内の文字も縮む。時系列は狭い幅で等倍のまま横スクロールさせる
  （`.chart-scroll` + `min-inline-size`）
- 棒の原点は 0（`data-origin="0"`）
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
