---
name: html-gen
description: Material Design 3 準拠のモダンな Web サイト／ダッシュボードを新規作成する。シード色から生成したトーナルパレット、リッチな SVG チャート、上品なアニメーション、完全レスポンシブ（320〜1920px）、ライト/ダーク切替が既定。「マテリアルデザインでサイトを」「モダンな HTML ダッシュボードを作って」「M3 でランディングページ」等で発火。既存サイトの単発修正は /bugfix。
user-invocable: true
---

# Material Design 3 の Web サイト

## 原則: 色を思い出しで書くな。テンプレートを複製せよ

配色は `assets/template/tokens.css` に**生成済み**で、その正本は
`references/tokens.json` のシード色。Tailwind 既定色、`#0d1117`、`#2563eb` の類を
1 つでも手書きすると検査で落ちる（手書きファイルに生の hex があること自体が違反）。

| やってはいけない | やる |
|---|---|
| hex を手で書く | `var(--md-sys-color-*)` / `var(--chart-N)` を使う |
| ゼロから HTML を書く | `assets/template/` を複製して中身を差し替える |
| `tokens.css` を手で直す | `tokens.json` を直して `--write-css` で再生成する |
| トグルを後付けする | テンプレのライト/ダークボタンを残す（削除禁止） |
| 目視だけで完了する | `check_site.py` が PASS するまで完了報告しない |

設計原則・レイアウト・モーション・チャートの作法は `references/design.md`。
**MUI（React 専用）ではなく M3 仕様を CSS トークンとして実装している**理由も同じ文書にある。

## 手順

### 1. 要件を確定する

埋まらない項目があれば先に聞く。

- サイト名 / 何を見せるか（指標・内訳・時系列・分布）
- シード色: `indigo` / `azure` / `teal` / `verdant` / `amber` / `crimson`（既定 `indigo`）
- ページ種別: `dashboard`（既定）か `content`（記事・紹介。指標や図を必須にしない）
- 実データかサンプルか

シードを増やしたいときは `tokens.json` の `seeds` に hex を足して
`--write-css` を回す。役割トーンは触らなくてよい（コントラストは自動で満たされる）。

### 2. テンプレートを複製する

テンプレートは**スキル起動時に提示されるベースディレクトリ**の `assets/template/`
にある。提示が無いときだけ `find "$HOME/.claude/plugins/cache" -maxdepth 7 -type d
-path '*/html-gen/assets/template'` で探す。**`/Users` や `$HOME` 全体を起点にしない**。

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
ナビゲーション項目、`html[data-seed]`（手順1で選んだシード）、
`html[data-page-kind]`。

**触らない**: `tokens.css`（生成物）、`app.js`、`button[data-theme-value="light"|"dark"]`、
`<head>` の `app.js` 読み込み位置（body 末尾へ移すと初回に白がちらつく）、
`.chart-scroll` ラッパ、`@media (prefers-reduced-motion: reduce)` ブロック、
`svg[role="img"]` の `<title>`/`<desc>`、`data-origin="0"`。

チャートを増減するときの注意（詳細は `references/design.md`）:

- 座標は viewBox 内の数値で持つ。系列色は `var(--chart-1..6)`、面は `var(--chart-N-container)`
- 入場アニメーションは `@keyframes` の `from` 側で隠し、`animation-fill-mode: both`。
  **静止状態は必ず読める状態にする**（`forwards` で最終状態を作らない）
- 時系列を足したら `.chart-scroll` で包む。包まないと狭い画面で軸文字が潰れる
- `auto-fit` のグリッドに `span 2` を掛けない。列数を明示する

依頼が指標・内訳・時系列・分布を求めるダッシュボードなら、それぞれ 1 つ以上残す。

### 4. 検証する（省略不可）

```bash
python3 /abs/path/to/dest/check_site.py /abs/path/to/dest
```

**exit code 0（`PASS`）を確認するまで完了報告しない。** `FAIL:` が出たら
手書きファイルに hex を足していないか、`tokens.css` を手で直していないか、
トグルを消していないかを先に疑う。

`check_site.py` は `tokens.json` 上の値しか見ないので、**実際に描画された姿**は
ブラウザでも確かめる。

1. `python3 -m http.server` で配信して開く（`file://` は localStorage が使えず
   永続を確認できない）
2. DevTools コンソールに `e2e_contrast.js` を貼り付けて実行する。
   6 シード × ライト/ダークの 12 ブロックを巡回し、`verdict` が `GREEN` なら受け入れ、
   `RED` なら `failures` がそのまま是正対象
3. 幅 320 / 390 / 834 / 1440 / 1920px で `document.documentElement.scrollWidth` が
   `clientWidth` と一致することを見る（横スクロールが出ていない証拠）
4. 再読込してもテーマとシードが保たれ、白がちらつかないことを見る

## 完了条件

- `check_site.py` が PASS
- ライトとダークをユーザーが選べ、再読込後も保たれる
- 手書きファイルに生の hex が 1 つも無い
- 320〜1920px で横スクロールが出ない
- モーションを切った環境（`prefers-reduced-motion`）でも中身が全部読める

## 永続メモリ

- 参照: `ple4_run ple4.mem.cli search "material design 3 web template"`
  （`. "$HOME/.ple4/env.sh"` 前提）→ 本文が要る key だけ
  `ple4_run ple4.mem.cli show <key>`
- 記録: 再利用可能な学びだけ `ple4_mem_learn`。基準は `../learn/SKILL.md`
