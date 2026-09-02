# Material Design 3 のウェブ翻訳

色の正本は `tokens.json`。ここに色コードを再掲しない。

出典:

- 配色システム: <https://m3.material.io/styles/color/system/overview>
- 役割（ロール）: <https://m3.material.io/styles/color/roles>
- タイプスケール: <https://m3.material.io/styles/typography/type-scale-tokens>
- シェイプ: <https://m3.material.io/styles/shape/corner-radius-scale>
- エレベーション: <https://m3.material.io/styles/elevation/tokens>
- モーション: <https://m3.material.io/styles/motion/easing-and-duration/tokens-specs>
- ステートレイヤー: <https://m3.material.io/foundations/interaction/states/state-layers>
- ウィンドウクラス: <https://m3.material.io/foundations/layout/applying-layout/window-size-classes>

## MUI ではなく M3 トークンを使う理由

Material UI（MUI）は React コンポーネントライブラリで、静的 HTML には載らない。
Material Web（`@material/web`）は Web Components だが Google がメンテナンスモードへ
移した。どちらも「生成した HTML を開けば動く」という要件と噛み合わない。

そこで **Material Design 3 の仕様そのもの**（トーナルパレット・役割・タイプスケール・
シェイプ・エレベーション・モーション）を CSS カスタムプロパティとして実装する。
外部 JS はゼロ、CDN は Google Fonts だけ。見た目と設計原則は M3 準拠のまま、
配布物は 4 ファイルで完結する。

## 色はシードから計算する。hex を手で選ばない

M3 の核心は「1 つのシード色から 6 本のトーナルパレットを作り、役割にトーンを
割り当てる」こと。`check_site.py` がこれを実装している。

- **tone は CIE L\***。M3 の HCT でも tone は L\* なので、ここは仕様どおり。
  トーン差がそのままコントラスト差になるため、可読性はトーン割当で保証される
- hue / chroma は LCh(ab) 近似（CAM16 ではない）。既定 seed の生成結果は
  M3 baseline 公表値と ΔE\*ab 6 以内に収まる（`test_indigo_seed_reproduces_the_m3_baseline_scheme`）
- 域外の彩度は **chroma だけ**を二分探索で落とす。tone は動かさない。
  ここで L\* を動かすとコントラスト契約が崩れる

パレット構成（`tokens.json` の `palette_spec`）:

| パレット | 色相 | 彩度 |
|---|---|---|
| primary | シードと同じ | max(48, シードの彩度) |
| secondary | シードと同じ | 16 |
| tertiary | シード +60° | 24 |
| neutral | シードと同じ | 6 |
| neutral-variant | シードと同じ | 8 |
| error | 25° 固定 | 84 |

役割 → トーンの割当は `tokens.json` の `roles`。ライト/ダークで別トーンを指す
（例: primary は 40 / 80、surface は 98 / 6）。**ダークは色の反転ではなくトーンの
差し替え**で、未検証の hex を新しく作らない。

## 系列色（チャート）

M3 はデータ可視化のカテゴリ配色を定義していない。ここでは seed の色相を
`series.hue_offsets`（0, 55, 120, 185, 250, 305）で回して 6 色を作る。

- ライトは tone 40、ダークは tone 80。カード背景に対して常に 3:1 以上
- 塗り面用に tone 90 / 30 の `--chart-N-container` も出す
- 隣接だけでなく**全組み合わせ**の ΔE\*ab を 20 以上に保つ
  （`SERIES_MIN_DELTA_E`。色覚特性が異なる利用者でも隣が判別できる下限）
- 連続量（ヒートマップ）は不透明度で薄めない。
  `color-mix(in oklab, var(--chart-1) N%, var(--chart-1-container))` で
  容器色→本色の連続スケールを作る。背景に溶けないうえ両テーマで成立する

## レイアウト: 表示領域を使い切る

固定キャンバス（1280×720 のような）は持たない。`check_site.py` が
ブレークポイント以上の固定幅を不合格にする。

- ウィンドウクラス: compact `<600` / medium `600–839` / expanded `840–1199` / large `1200+`
- ナビゲーションは compact/medium で下部バー、expanded 以上で左レール
- ペインは `max-width: 1920px` + `margin-inline: auto`、内側は `clamp()` のガター
- グリッドのトラックは必ず `minmax(0, 1fr)`。`auto` のままだと中身の max-content
  までトラックが伸びて横スクロールが出る
- `auto-fit` のグリッドに `grid-column: span 2` を掛けない。列が 1 本しかない
  ブレークポイントで暗黙列が content 幅で生えて、ページごと横に伸びる。
  スパンを使うなら列数を明示する（`repeat(2, minmax(0, 1fr))`）
- カード内の出し分けは `@container`（`container-type: inline-size`）で行う。
  **コンテナクエリはコンテンツボックス基準**なので、パディング分を引いた値で閾値を決める

## チャートの可読性

- SVG は `width: 100%` だけだと正方形の図が幅いっぱいに膨らむ。`max-height` で止める
- 縮小すると SVG 内の文字も縮む。時系列は狭い幅で等倍のまま横スクロールさせ
  （`.chart-scroll` + `min-width`）、加えて `@container` で user unit のフォントを上げる
- 棒の原点は 0（`data-origin="0"`）
- `svg[role="img"]` に `<title>` と `<desc>`。図と同じ内容を表でも出す

## モーション

- イージング/デュレーションは `--md-sys-motion-*` から取る。生の秒数を書かない
- 入場アニメーションは **`from` 側で隠して `both` で当てる**。
  `.reveal { opacity: 0 }` + `forwards` のように「完走しないと見えない」書き方は、
  背面タブ（タイムラインが進まない）・JS 無効・印刷で真っ白なページになる。
  静止状態は常に読める状態にしておく
- `@media (prefers-reduced-motion: reduce)` で animation と transition の両方を止める
  （`check_site.py` が両方を確認する）

## エレベーションとステートレイヤー

- 影は `--md-sys-elevation-level0..5`。M3 では影は装飾ではなく面の高さの表現
- 影の色は `--md-sys-color-shadow-rgb`（トーン 0 の RGB 三つ組）を通す。
  `rgb(var(--md-sys-color-shadow-rgb) / 0.3)` の形にすると両テーマで同じ定義が使える
- ホバー/フォーカス/プレスは `color-mix` によるステートレイヤーで表す
  （`--md-sys-state-*-opacity`）

## タイポグラフィ

- `--md-sys-typescale-<role>-font` は `font` ショートハンド、`-tracking` は字間
- 見出しは brand フォント、本文・UI は plain フォント
- ヒーローの見出しなど「効かせたい」文字だけ `clamp()` の流体サイズにする
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
- ライト/ダークをボタンで選べ、再読込後も保たれるか
- 320px から 1920px まで横スクロールが出ないか
