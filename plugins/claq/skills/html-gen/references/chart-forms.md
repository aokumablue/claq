# チャート種別ごとの骨格

`design.md`「グラフ種別の選定」で選んだ種別を、実際の SVG と表に落とすための寸法と
骨格。種別を決めても骨格が無ければ、毎回違う書き方になる。

## viewBox 幅の上限は逆算できる

SVG の文字は viewBox の倍率で縮むので、実効サイズは次で決まる。

```text
実効フォントサイズ = 宣言サイズ × 最小描画幅 ÷ viewBox 幅
```

最小描画幅は CSS の `min-inline-size`、宣言サイズは `.axis-text` の
`font-size`。実効 12px を保つ viewBox 幅の上限はこれを解いて得られる。

```text
viewBox 幅の上限 = 宣言サイズ × 最小描画幅 ÷ 12
```

| クラス | 最小描画幅 | viewBox 幅の上限 | 使う種別 |
|---|---|---|---|
| `.chart` | 600px | 750 | 折れ線・面・ヒストグラム |
| `.chart--hbars` | 600px | 750 | 横棒 |
| `.chart--stack` | 600px | 750 | 100% 積み上げ横棒 |
| `.chart--scatter` | 600px | 750 | 散布図 |
| `.chart--bars` | 380px | 475 | 縦棒 |
| `.chart--donut` | なし | — | ドーナツ（軸文字を持たない） |

上限を超えると `_validate_axis_text_scale` が落とす。超えたら viewBox を狭める
のが先で、`font-size` を上げるのは最後の手段（他の図と文字の大きさがずれる）。

## 余白の取り方

プロット領域の外側に、実際に置く文字が入るだけの帯を取る。日本語は全角
なので 1 文字 ≒ フォントサイズとみなして数える。

文字幅の見積もり（`.axis-text` は 15u）:

| 字種 | 1 文字あたり |
|---|---|
| 全角（日本語） | 15（フォントサイズと同じ） |
| 数字 | 9 |
| 桁区切りの `,` | 4.5 |

| 帯 | 必要量 | 例 |
|---|---|---|
| 左（値の目盛り） | 最長の目盛り文字列 + 10 | `5,000` = 40.5 → 51 |
| 左（全角の分類ラベル） | 文字数 × 15 + 13 | 9 文字 → 148 |
| 左（軸の名前を縦書きで置くとき） | 上記にさらに +20。目盛りの幅に重ねない | |
| 下（分類ラベル 1 行） | 15 + 14 = 29 | |
| 下（軸の名前も置くとき） | 上記にさらに +18 | |
| 右（棒の先に値を置くとき） | その文字列の幅 + 8 | `4,820` → 49 |
| 右（置かないとき） | 16 | |

帯が足りないと文字が viewBox の外へ出るか、目盛りと重なる。座標を書く前に
最長の文字列を数える。全角ラベルは `font-size` と同じ幅を食うので、12u 想定で
引いた余白は 15u の今では 25% 足りない。

縦の目盛りは右揃え（`text-anchor="end"`）、横は中央揃え（`middle`）で置く。検査は揃えで
軸を分ける。

中央揃え以外の文字（縦軸の目盛り・横棒の分類と値・凡例）は、y に線・棒・色見本の中心を
書く（CSS の `dominant-baseline: central` が文字の中心をそこへ合わせ、文字を 13px で
頭打ちにしても線からずれない）。中央揃えの横軸の文字は、y にベースラインを書く。

## 骨格

系列色は `var(--chart-1..5)`、面の色は `var(--card)`。生の色を書かない。

### 折れ線（`.chart`）

規則 5 で選ぶ形。テンプレートの「月間経常収益の推移」を写す。比較の系列（前年など）は
`var(--muted-foreground)` で沈め、主役の系列だけ着色して面を敷く。

- 比較の系列は `.series-line--compare` にする（線幅は属性ではなくクラスで持つ。CSS が
  SVG の表現属性に勝つため、`stroke-width` 属性を書いても `.series-line` の値で上書きされる）
- 線の `--len` と `stroke-dasharray` はパスの実長以上にする。短いと静止状態で
  線の末尾が隙間に落ちて消える
- 曲線は行き過ぎない単調補間（d3 の `curveMonotoneX` と同じ）にする。通常の
  スプラインは山と谷の外へはみ出し、無い値を描く
- 時点ごとに当たり判定の列を最前面へ置く。幅は時点の間隔で、両端はプロット領域の
  内側で切る。`<title>` の 1 行目は時点（浮き札で太字になる）、続けて系列ごとに 1 行

```html
<g class="hover-col">
  <line class="hover-guide" x1="110.9" y1="18" x2="110.9" y2="214"/>
  <circle class="hover-dot" cx="110.9" cy="136.4" r="4" fill="var(--muted-foreground)" stroke="var(--card)" stroke-width="2"/>
  <circle class="hover-dot" cx="110.9" cy="108.9" r="4.5" fill="var(--chart-1)" stroke="var(--card)" stroke-width="2"/>
  <rect class="hover-hit" x="81.5" y="18" width="58.9" height="196" fill="none">
    <title>2月&#10;本年 134 百万円&#10;前年 99 百万円&#10;前年同月比 +35.4%</title>
  </rect>
</g>
```

### 縦棒（`.chart--bars`）

規則 6・10 で選ぶ形。棒の幅は帯幅の 0.6（棒と棒の隙間は棒の 2/3）にそろえる。

```html
<div class="chart-scroll">
  <svg class="chart chart--bars" role="img" viewBox="0 0 470 252"
       preserveAspectRatio="xMidYMid meet" data-origin="0" aria-labelledby="q-t q-d">
    <title id="q-t">四半期別の売上</title>
    <desc id="q-d">Q1 の 3,180 万円から Q4 の 4,890 万円へ 4 期連続で増加。</desc>
    <line class="grid-line" x1="56" y1="214" x2="462" y2="214"/>
    <text class="axis-text" x="46" y="214" text-anchor="end">0</text>
    <line class="axis-line" x1="56" y1="214" x2="462" y2="214"/>
    <rect class="bar" style="--i: 0" x="76.3" y="89.3" width="60.9" height="124.7" rx="4" fill="var(--chart-1)">
      <title>2025 Q1 3,180 万円</title>
    </rect>
    <text class="axis-text" x="106.8" y="236" text-anchor="middle">Q1</text>
    <!-- 目盛りは 3 本以上、棒と分類ラベルは分類の数だけ繰り返す（--i を 1 ずつ増やす） -->
  </svg>
</div>
```

### ヒストグラム（`.chart` + `.bin`）

規則 3 で選ぶ形。ビンは幅をそろえ、全ビン同じ隙間 2 で並べる（高さだけが値を
表す）。棒の class を `bar` にしない — 縦棒の分類上限（7）で落ちる。縦の目盛りは
右揃え、横の目盛りはビンの境界に中央揃えで置く（混み合うなら 1 つおき）。

```html
<div class="chart-scroll">
  <svg class="chart" role="img" viewBox="0 0 720 280"
       preserveAspectRatio="xMidYMid meet" data-origin="0" aria-labelledby="ds-t ds-d">
    <title id="ds-t">注文金額の分布</title>
    <desc id="ds-d">注文の 6 割が 2,000〜6,000 円に集まり、10,000 円を超える注文は 4%。</desc>
    <line class="grid-line" x1="56" y1="214" x2="680" y2="214"/>
    <text class="axis-text" x="46" y="214" text-anchor="end">0</text>
    <line class="axis-line" x1="56" y1="214" x2="680" y2="214"/>
    <rect class="bin" style="--i: 0" x="57" y="135.6" width="50" height="78.4" fill="var(--chart-1)">
      <title>0〜1,000 円未満 120 件</title>
    </rect>
    <text class="axis-text" x="56" y="236" text-anchor="middle">0</text>
    <text class="axis-text axis-title" x="368" y="270" text-anchor="middle">注文金額（円）</text>
    <!-- 縦の目盛りは 3 本以上。ビンは 52 ずつ右へずらす（幅 50 + 隙間 2） -->
  </svg>
</div>
```

### ヒートマップ表（`table.heat`）

規則 4 で選ぶ形。SVG ではなく表のセルを塗る。数値は消さない（色だけでは値を
読めない）。`--v` は「値 ÷ 最大値」（0〜1）。濃さの上限は CSS で 55% に固定して
あり、1 を超える `--v` は文字のコントラストを割る。行と列は曜日順・時刻順などの
自然な順に置き、値で並べ替えない（周期が崩れる）。

```html
<div class="table-scroll">
  <table class="heat">
    <caption>曜日×時間帯の問い合わせ件数。色が濃いほど多い（最多 182 件）</caption>
    <thead>
      <tr><th scope="col">曜日</th><th scope="col">0〜5 時台</th><th scope="col">6〜11 時台</th></tr>
    </thead>
    <tbody>
      <tr><th scope="row">月</th><td style="--v: 0.08">14</td><td style="--v: 0.56">102</td></tr>
    </tbody>
  </table>
</div>
```

### 横棒（`.chart--hbars`）

規則 9 で選ぶ形。降順に並べる（順位を読ませるのが目的）。高さは
`分類数 × (棒22 + 間隔14) - 14 + 上10 + 下30`（分類 11 なら 422）。値軸のグリッドは縦線になる。

```html
<div class="chart-scroll">
  <svg class="chart chart--hbars" role="img" viewBox="0 0 720 422"
       preserveAspectRatio="xMidYMid meet" data-origin="0" aria-labelledby="st-t st-d">
    <title id="st-t">店舗別の月間売上</title>
    <desc id="st-d">横浜港北店が 4,820 万円で最多。上位 3 店舗で約 4 割。</desc>
    <line class="grid-line" x1="148" y1="10" x2="148" y2="392"/>
    <text class="axis-text" x="148" y="412" text-anchor="middle">0</text>
    <line class="axis-line" x1="148" y1="10" x2="148" y2="392"/>
    <rect class="bar--h" style="--i: 0" x="148" y="10" width="504" height="22" rx="4" fill="var(--chart-1)">
      <title>神奈川県横浜港北店 4,820 万円</title>
    </rect>
    <text class="axis-text" x="135" y="21" text-anchor="end">神奈川県横浜港北店</text>
    <text class="axis-text bar-value" x="660" y="21">4,820</text>
    <!-- 目盛りは 3 本以上、棒・ラベル・値は分類の数だけ繰り返す（y を 36 ずつ下げる） -->
  </svg>
</div>
```

### 100% 積み上げ横棒（`.chart--stack`）

規則 8 で選ぶ形。分類 6 以上・複数時点の部分と全体をドーナツにしない。
区切りは面の色の 2px の隙間が作る。枠線を描かない。

隙間を区画の長さから引かない。引くと全分類が一律に短くなり、合計が 100% に
届かなくなる（6 分類で 98.3%、各分類が 0.3pt 過小）。合計が意味を持つ図で長さを
削るのは符号化の誤りである。区画は真の長さで描き（区画は x=8 から幅 704 が 100%）、
境界の上へ面の色の 2px の仕切りを重ねる。仕切りは当たり判定を持たない線
（`.slice-gap`）で引く。rect で引くと当たり判定を持ち、境界の片側の区画だけを削る。

```html
<div class="chart-scroll">
  <svg class="chart chart--stack" role="img" viewBox="0 0 720 128"
       preserveAspectRatio="xMidYMid meet" data-origin="0" aria-labelledby="mx-t mx-d">
    <title id="mx-t">カテゴリ別の売上構成比</title>
    <desc id="mx-d">食品が 32% で最大。上位 3 カテゴリで 74%。</desc>
    <line class="grid-line" x1="8" y1="26" x2="8" y2="76"/>
    <text class="axis-text" x="8" y="20" text-anchor="middle">0%</text>
    <rect class="seg" style="--i: 0" x="8" y="34" width="225.3" height="34" fill="var(--chart-1)">
      <title>食品 32%</title>
    </rect>
    <!-- 区画を全部描いた後、境界へ面の色の仕切りを重ねる（長さは削らない） -->
    <line class="slice-gap" x1="233.3" y1="34" x2="233.3" y2="68"/>
    <rect class="legend-key" x="8" y="82" width="10" height="10" rx="2" fill="var(--chart-1)"/>
    <text class="axis-text" x="24" y="87">食品 32%</text>
    <!-- 目盛りは 0/20/40/60/80/100%、区画と凡例は分類の数だけ繰り返す。
         凡例の x は必ず viewBox 幅の内側へ収める（等間隔に置くと末尾が図外へ出る） -->
  </svg>
</div>
```

### 散布図（`.chart--scatter`）

規則 2 で選ぶ形。点は半径 6 に、面の色の 2px のリングを付ける（重なっても輪郭が
残る）。両軸に名前を置く — 散布図は軸が何かを示さないと読めない唯一の形。

```html
<div class="chart-scroll">
  <svg class="chart chart--scatter" role="img" viewBox="0 0 720 380"
       preserveAspectRatio="xMidYMid meet" data-origin="0" aria-labelledby="sc-t sc-d">
    <title id="sc-t">広告費と売上の関係</title>
    <desc id="sc-d">広告費が増えるほど売上も増える正の相関。</desc>
    <line class="grid-line" x1="76" y1="320" x2="702" y2="320"/>
    <text class="axis-text" x="66" y="320" text-anchor="end">0</text>
    <line class="axis-line" x1="76" y1="320" x2="702" y2="320"/>
    <text class="axis-text" x="200" y="342" text-anchor="middle">100</text>
    <text class="axis-text axis-title" x="389" y="376" text-anchor="middle">広告費（万円）</text>
    <text class="axis-text axis-title" x="16" y="174" text-anchor="middle"
          transform="rotate(-90 16 174)">売上（万円）</text>
    <circle class="point" style="--i: 0" cx="200" cy="250" r="6"
            fill="var(--chart-1)" stroke="var(--card)" stroke-width="2">
      <title>札幌 広告費 100 万円 / 売上 1,840 万円</title>
    </circle>
    <!-- 目盛りは両軸 3 本以上、点はデータの数だけ繰り返す -->
  </svg>
</div>
```

点が重なるほど密なら、それは散布図で表せる密度を超えている。分類を減らすか、
区間ごとに集計した図に替える。

### 図の直下の数値表（`details.chart-data`）

値が `<title>` にしか無い図には、図の直下に畳んだ表を置く（凡例に値を書くドーナツ・
積み上げ横棒は不要）。行は図と同じ順にし、単位は caption か列見出しに書く。

```html
<details class="chart-data">
  <summary>表で見る</summary>
  <div class="table-scroll">
    <table>
      <caption>チャネル別の新規契約数（直近 90 日、単位: 件）</caption>
      <thead><tr><th scope="col">チャネル</th><th scope="col" class="num">件数</th></tr></thead>
      <tbody><tr><th scope="row">オーガニック</th><td class="num">412</td></tr></tbody>
    </table>
  </div>
</details>
```

## 種別と置き場所を合わせる

横長の図を狭い側カードへ入れない。幅 720 の 100% 積み上げ横棒を幅 250 の
カードへ入れると、帯は高さ 45px に潰れて文字は 4px になる（実測）。

| 置き場所 | 収まる種別 |
|---|---|
| 主カラムの広いカード | 折れ線・横棒・積み上げ横棒・散布図・ヒストグラム・ヒートマップ表 |
| 側カラムの狭いカード | ドーナツ・縦棒（分類 5 以下）・指標カード |

主カラム向きの種別が選ばれたら、カードの側を広い方へ移す。図を縮めて
入れない。
