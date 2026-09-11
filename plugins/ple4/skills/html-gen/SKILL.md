---
name: html-gen
description: shadcn/ui のデザイントークンで、モダンな Web サイト／ダッシュボードを新規作成する。配色（neutral / zinc / slate / stone / gray）、SVG チャートとアニメーション、レスポンシブ（320〜1920px）、ライト/ダーク切替が既定。「shadcn でサイトを」「モダンな HTML ダッシュボードを作って」等で発火。既存サイトの単発修正は /bugfix。
user-invocable: true
---

# shadcn/ui の Web サイト

## 原則: 色を思い出しで書くな。テンプレートを複製せよ

配色は `assets/template/tokens.css` に**生成済み**で、その正本は
`references/tokens.json`（shadcn/ui レジストリの逐語コピー）。Tailwind 既定色、
`#0d1117`、`#2563eb` の類を 1 つでも手書きすると検査で落ちる
（手書きファイルに生の hex や `oklch()` があること自体が違反）。

| やってはいけない | やる |
|---|---|
| hex や `oklch()` を手で書く | `var(--primary)` / `var(--chart-N)` を使う |
| ゼロから HTML を書く | `assets/template/` を複製して中身を差し替える |
| `tokens.css` を手で直す | `tokens.json` を直して `--write-css` で再生成する |
| React や Tailwind CLI を足す | 素の CSS のまま（理由は `references/design.md`） |
| トグルを後付けする | テンプレのライト/ダークボタンを残す（削除禁止） |
| 目視だけで完了する | `check_site.py` が PASS するまで完了報告しない |

設計原則・レイアウト・コンポーネント・チャートの作法は `references/design.md`。
チャート種別ごとの骨格と寸法は `references/chart-forms.md`。
**React 版の shadcn/ui ではなくトークンを CSS へ写している**理由も同じ文書にある。

## 手順

### 1. 要件を確定する

埋まらない項目があれば先に聞く。

- サイト名 / 何を見せるか（指標・内訳・時系列・分布）
- ベースカラー: `neutral`（既定）/ `zinc` / `slate` / `stone` / `gray`
- ページ種別: `dashboard`（既定）か `content`（記事・紹介。指標や図を必須にしない）
- 実データかサンプルか

ベースカラーは shadcn/ui が配る 5 つがそのまま入っている。増やしたいときは
`https://ui.shadcn.com/r/colors/<name>.json` の `cssVarsV4` を `tokens.json` の
`bases` へ足して `--write-css` を回す。コントラストの調整は自動で入る。

### 2. テンプレートを複製する

テンプレートは**スキル起動時に提示されるベースディレクトリ**の `assets/template/`
にある。提示が無いときだけ `find "$HOME/.claude/plugins/cache" -maxdepth 7 -type d
-path '*/html-gen/assets/template'`（このフォールバックは Claude Code のキャッシュ配置に固有。他ホストではベースディレクトリの提示が必須） で探す。**`/Users` や `$HOME` 全体を起点にしない**。

複製先は**完全なリテラル絶対パス**で書く（`$VAR` を含めると保護フックに止められる）。

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

**差し替える**: タイトル、指標の名前と数値、チャートのデータと `<desc>`、表、
ナビゲーション項目、`html[data-base]`（手順1で選んだベースカラー）、
`html[data-page-kind]`。

**触らない**: `tokens.css`（生成物）、`app.js`、`button[data-theme-value="light"|"dark"]`、
`<head>` の `app.js` 読み込み位置（body 末尾へ移すと初回に白がちらつく）、
`.chart-scroll` ラッパ、`@media (prefers-reduced-motion: reduce)` ブロック、
`svg[role="img"]` の `<title>`/`<desc>`、`data-origin="0"`。

**グラフ種別は上から順に当て、最初に当たった行で確定する**（詳細と根拠は
`references/design.md`「グラフ種別の選定」）。迷って選び直さない。種別が回ごとに
揺れる原因は知識不足ではなく、同点のときの決め手が無いことである。

| # | データがこうなら | 使う |
|---|---|---|
| 1 | 値が 1 つ | 指標カード（棒 1 本のグラフにしない） |
| 2 | 量的変数 2 つの相関 | 散布図 |
| 3 | 時間軸・時点 8 以上 | 折れ線（1 系列なら面を敷く） |
| 4 | 時間軸・時点 7 以下 | 縦棒 |
| 5 | 部分と全体・分類 5 以下・1 時点 | ドーナツ |
| 6 | 部分と全体・分類 6 以上または複数時点 | 100% 積み上げ横棒 |
| 7 | 大小比較・分類 8 以上またはラベル 9 文字以上 | 横棒（降順） |
| 8 | 大小比較（上記以外） | 縦棒 |
| 9 | 意味のある分類が 8 以上で省けない | 表 |

時間は必ず横軸へ置く。縦横の向きは見た目の好みで決めない。
**選んだ種別の SVG 骨格・viewBox 幅の上限・余白の取り方は `references/chart-forms.md`。**
種別を決めても骨格が無ければ毎回違う書き方になるので、必ず写して使う。

**軸の無いグラフは出荷しない。** 直交軸チャートには目盛りラベルと目盛り線を
最低 3 本ずつ（通常 5〜7 本）置き、刻みは 1・2・5 の倍数にする。検査が落とす。

チャートを増減するときの注意（詳細は `references/design.md`）:

- 座標は viewBox 内の数値で持つ。系列色は `var(--chart-1..5)`
- **viewBox の縦横比がそのまま図の縦横比になる。** 高さの上限に固定 px を書かない
  （広い画面でレターボックスが働き図だけ小さく残る）。`clamp(320px, 46vh, 620px)`
- x 軸のラベル帯とy 軸の目盛り幅を viewBox の内側に取る（下 20px / 左 40〜55px）
- **静止状態を完成形にする**。動きは `[data-animate="in"]` の下だけに書く。
  `opacity: 0` を静止状態に置いたり `animation-fill-mode: forwards` を使うと、
  背面タブ・JS 無効・印刷で真っ白なページになる（検査で落ちる）
- 円弧の開始位置は SVG 属性の `stroke-dashoffset`。**CSS で宣言しない**
  （CSS が属性より優先され、全セグメントが重なる）
- 潰した SVG（`preserveAspectRatio="none"`）に `stroke-dasharray` のドローを
  使わない。`clip-path` で拭う
- 時系列を足したら `.chart-scroll` で包む。包まないと狭い画面で軸文字が潰れる
- `auto-fit` のグリッドに `span` を掛けない。列数を明示する

依頼が指標・内訳・時系列・分布を求めるダッシュボードなら、それぞれ 1 つ以上残す。

### 4. 検証する（省略不可）

```bash
python3 /abs/path/to/dest/check_site.py /abs/path/to/dest
```

**exit code 0（`PASS`）を確認するまで完了報告しない。** `FAIL:` が出たら
手書きファイルに色を直書きしていないか、`tokens.css` を手で直していないか、
トグルを消していないかを先に疑う。

`check_site.py` は `tokens.json` 上の値しか見ないので、**実際に描画された姿**は
ブラウザでも確かめる。

1. `python3 -m http.server` で配信して開く（`file://` は localStorage が使えず
   永続を確認できない）
2. DevTools コンソールに `e2e_contrast.js` を貼り付けて実行する。
   5 ベースカラー × ライト/ダークの 10 ブロックを巡回し、`verdict` が `GREEN`
   なら受け入れ、`RED` なら `failures` がそのまま是正対象
3. 幅 320 / 390 / 768 / 1280 / 1920px で `document.documentElement.scrollWidth` が
   `clientWidth` と一致することを見る（横スクロールが出ていない証拠）
4. 再読込してもテーマとベースカラーが保たれ、白がちらつかないことを見る
5. 図が描き切った状態で止まること、モーションを切っても全部読めることを見る

## 完了条件

- `check_site.py` が PASS
- `e2e_contrast.js` が GREEN
- ライトとダークをユーザーが選べ、再読込後も保たれる
- 手書きファイルに生の色が 1 つも無い
- 320〜1920px で横スクロールが出ない
- アニメーションが走らない環境（背面タブ・JS 無効・`prefers-reduced-motion`）でも
  図と数値が全部読める

## 永続メモリ

- 参照: `ple4_run ple4.mem.cli search "shadcn ui web template"`
  （`. "$HOME/.ple4/env.sh"` 前提）→ 本文が要る key だけ
  `ple4_run ple4.mem.cli show <key>`
- 記録: 再利用可能な学びだけ `ple4_mem_learn`。基準は `../learn/SKILL.md`
