---
name: html-gen
description: デジタル庁ダッシュボードデザインテンプレート準拠のWebサイトを新規作成する。公式カラーコード以外は使わず、ホワイト/ダーク切替トグルは必須。「デジタル庁のデザインでサイトを」「dashboard-guidebook準拠」「DADSパレットでWeb」等で発火。公式パレットを求めない汎用HTML/LP生成には使わない。既存サイトの単発修正は /bugfix。
user-invocable: true
---

# デジタル庁テンプレート準拠の Web サイト

## 原則: 記憶で色を付けるな。テンプレートを複製せよ

色・半径・KPI サイズ・グリッドは `assets/template/` に固定済み。Tailwind 既定色、
`#0d1117`、`#2563eb` の類は公式パレットに無く、検査は hex を全件照合するので
1 色の創作で落ちる。

| やってはいけない | やる |
|---|---|
| 色を思い出しで書く | `references/tokens.json` の hex だけを使う |
| ゼロから HTML を書く | `assets/template/` を複製して中身を差し替える |
| トグルを後付けする | テンプレのホワイト/ダークボタンを残す（削除禁止） |
| 目視だけで完了する | `check_site.py` が PASS するまで完了報告しない |

色の正本は `references/tokens.json`。レイアウト・書体・ダーク割当と出典は
`references/layout.md`。

## 手順

### 1. 要件を確定する

埋まらない項目があれば先に聞く。

- サイト名 / 何を見せるか（指標・内訳・時系列）
- カラーパレット: `solid-gray` / `blue` / `light-blue` / `cyan` / `green` / `orange` / `red`（既定 `blue`）
- キャンバス: 16:9（1280×720、既定）か 4:3（960×720）
- 実データかサンプルか

### 2. テンプレートを複製する

テンプレートは**スキル起動時に提示されるベースディレクトリ**の `assets/template/`
にある。提示が無いときだけ `find "$HOME/.claude/plugins/cache" -maxdepth 7 -type d
-path '*/html-gen/assets/template'` で探す。**`/Users` や `$HOME` 全体を起点にしない**。

複製先は**完全なリテラル絶対パス**で書く（`$VAR` を含めると保護フックに止められる）。

```bash
mkdir -p /abs/path/to/dest
cp /abs/skill/html-gen/assets/template/index.html /abs/path/to/dest/index.html
cp /abs/skill/html-gen/assets/template/styles.css /abs/path/to/dest/styles.css
cp /abs/skill/html-gen/assets/template/theme.js /abs/path/to/dest/theme.js
cp /abs/skill/html-gen/assets/template/check_site.py /abs/path/to/dest/check_site.py
cp /abs/skill/html-gen/assets/template/e2e_contrast.js /abs/path/to/dest/e2e_contrast.js
cp /abs/skill/html-gen/references/tokens.json /abs/path/to/dest/tokens.json
```

`styles.css` の先頭（`:root[data-theme][data-palette]` ブロックまで）は手で色を
足さない。パレット追加が必要なら `tokens.json` を公式表に合わせて直し、
`python3 check_site.py --write-css .` で CSS を再生成する。追加ルールは**末尾にだけ**
書いてよい。hex は公式集合に閉じる。

### 3. 中身を差し替える

**差し替える**: タイトル、指標の名前と数値、グラフの `desc`、表、フィルターの選択肢、
`html[data-palette]`（手順1で選んだ識別子）、4:3 なら `html[data-canvas="4x3"]`、
紹介サイトなら `html[data-page-kind="content"]`（既定は `dashboard`。content は
KPI/チャート必須を外す）。

**触らない**: `theme.js`、`button[data-theme-value="light"|"dark"]`、
`:root[data-theme][data-palette]` ブロック、`<head>` の `theme.js` 読み込み位置
（body 末尾へ移すとダーク選択時に白がちらつく）、Noto Sans JP の link、
`data-origin="0"`、`role="img"` の SVG 代替テキスト、角丸と KPI サイズの CSS 変数。

ハイライトカード（`.card--highlight`）の上に `delta-positive` / `delta-negative` を
置かない（Text White 専用で、系列色はコントラストを満たさない）。

依頼が指標・内訳・時系列・地域を求めるダッシュボードなら、それぞれ 1 つ以上残す。
指定パレットは `html[data-palette]` と `<select>` の `selected` で固定する
（セレクタ自体は `theme.js` が参照するので消さない）。

### 4. 検証する（省略不可）

```bash
python3 /abs/path/to/dest/check_site.py /abs/path/to/dest
```

**exit code 0（`PASS`）を確認するまで完了報告しない。** 1 件でも `FAIL:` が出たら
公式に無い色を足していないか、トグルを消していないかを先に疑う。

`check_site.py` は tokens.json 上の値しか見ないので、**実際に描画された色**は
ブラウザでも確かめる。

1. `python3 -m http.server` で配信して開く（`file://` は localStorage が使えず
   永続を確認できない）
2. DevTools コンソールに `e2e_contrast.js` を貼り付けて実行する。7 パレット ×
   ライト/ダークの 14 ブロックを巡回し、`verdict` が `GREEN` なら受け入れ、
   `RED` なら `failures` がそのまま是正対象
3. 再読込してもダークのままで、白がちらつかないことを見る

## 完了条件

- `check_site.py` が PASS
- ホワイトとダークをユーザーが選べる
- 使っている hex がカラーコードページ（2026-07-17）に載っている
- `CONTRAST_CONTRACT` の全ペアが下限以上（**ダークの系列色が最も落ちやすい**）

## 永続メモリ

- 参照: `bluecore_run bluecore.mem.cli search "digital-go dashboard web template"`
  （`. "$HOME/.bluecore/env.sh"` 前提）→ 本文が要る key だけ
  `bluecore_run bluecore.mem.cli show <key>`
- 記録: 再利用可能な学びだけ `bluecore_mem_learn`。基準は `../learn/SKILL.md`
