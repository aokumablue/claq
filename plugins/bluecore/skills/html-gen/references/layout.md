# レイアウト・表現（ガイドブックのウェブ翻訳）

色の正本は `tokens.json`。ここに色コードを再掲しない。

出典:

- 実践ガイドブック: <https://www.digital.go.jp/resources/dashboard-guidebook>
- カラーコード（2026-07-17 更新）: <https://www.digital.go.jp/resources/dashboard-guidebook/color-palette/color-code>
- Power BI テーマ JSON: <https://github.com/digital-go-jp/policy-dashboard-assets>
- ウェブ用書体は DADS: <https://design.digital.go.jp/dads/foundations/typography/>
- コントラスト下限は DADS: <https://design.digital.go.jp/dads/foundations/color/accessibility/>

## キャンバス

Power BI テンプレートのページサイズをウェブの上限にする。

- 16:9 = 1280×720（既定、`html[data-canvas="16x9"]`）
- 4:3 = 960×720（`html[data-canvas="4x3"]`。CSS が `--page-width` を差し替える）
- 縦 2〜6 列のグリッド。テンプレートは 6 列
- カード角丸 12px、ボタン角丸 8px（公式テーマ JSON の `rectangleRoundedCurve`）
- KPI 数値 36px（公式 `card.labels.fontSize`）
- フィルターは画面上部、影響を受けるカードはその下か右

## 書体（Power BI の Arial 8px は使わない）

Power BI テーマは Arial 8〜12px。これは BI キャンバスの制約であり、ウェブでは DADS が正。

- 本文・UI: Noto Sans JP、16px 以上
- 表・軸・凡例の dense: 14px まで（14px 未満は禁止）
- KPI: 36px / 見出し: 20px
- 行間: 本文 1.5、見出しはやや詰め

## 情報の置き方

左上から右下へ、全体指標 → 内訳。ハイライト背景は強調 KPI だけ。

公式サンプルが含む要素: 主要指標 2、構成比ドーナツ、年次積み上げ/棒、時系列折れ線、地域別比較。

## グラフ

- 棒の原点は 0（`data-origin="0"`）
- 系列は 1〜5 色。`tokens.json` の `roles.<theme>.series` を順に使う
- 3D・ドロップシャドウ・装飾アニメーションは Dont's（`check_site.py` が検出する。
  フォーカスリングの box-shadow だけは WCAG 上必要なので例外）
- 色だけで区分しない（凡例・ラベル・増減の符号を併記）
- `svg[role="img"]` に `title` と `desc`（代替テキスト）
- 公開データなら表か CSV も出す

## コントラスト（DADS の下限を機械検査する）

文字 4.5:1 / 非文字 3:1。`check_site.py` の `CONTRAST_CONTRACT` が 14 ブロック
（7 パレット × ライト/ダーク）すべてを照合する。ここを緩めない。

- 本文・ラベル・リンク・増減・成功/エラーの文字は、カードとページの双方に 4.5:1
- 系列色 `--chart-1..5` はカード背景に 3:1（**ダークで最も壊れやすい**。
  ライトの濃い系列をそのまま置くと、カードに沈んでグラフが消える）
- 枠線 `--color-outline` はページに 3:1（カードとページの明度差が小さくても境界が立つ）
- フォーカスリングは二重帯。外側の `--color-focus-edge` だけでページ・カード双方に 3:1

## ダークモード

公式テンプレートはライトが正。ダークは掲載色の再割当であり、未掲載の hex を作らない。

| 役割 | ライト | ダーク |
|---|---|---|
| ページ | `#FFFFFF` | Text Black |
| カード | Standard | SolidGray 800（Solid Gray パレットだけ 900。Highlight と同色になるため） |
| 本文 | Text Black | Text White |
| ラベル | Label | SolidGray 200 |
| リンク | Link | パレットの 400 系統（4.5:1 を満たさなければ 200 系統） |
| ハイライト | パレット Highlight | 同じ Highlight + Text White |
| 系列 | 900〜600 の濃い側 | 400〜200 の淡い側 + Yellow / SolidGray |
| 増減 | 600 系（増）/ Error か SolidGray 600（減） | 200 系（`*_soft`） |

`theme.js` は `localStorage` が空なら `html[data-palette]` を使う（Blue へ上書きしない）。
`color-scheme` は `data-theme` に追従させる（追従しないとセレクトの
ドロップダウンとスクロールバーだけ OS 設定のまま残る）。

## ページ種別

- `data-page-kind="dashboard"`（既定）: KPI・SVG・棒の原点 0 が検査対象
- `data-page-kind="content"`: 紹介・お知らせ。色・トグル・書体は同じ。偽の KPI を足さない

## チェックリスト（ガイドブック 5.4 のウェブ版）

- 全体指標が先に目に入るか
- グラフと凡例が隣接しているか
- 比較対象（前年・目標）があるか
- 更新時点が書いてあるか
- 色覚に依存しない区別があるか
- ホワイト / ダークをボタンで選べるか
