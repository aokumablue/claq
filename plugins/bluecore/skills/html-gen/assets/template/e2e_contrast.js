/**
 * 実ブラウザでの受け入れ確認。生成サイトを HTTP で配信して開き、
 * DevTools コンソールに貼り付けて実行する（file:// は localStorage が使えず永続を確認できない）。
 *
 * check_site.py は tokens.json 上の値を検査する。こちらは「実際に描画された色」を
 * getComputedStyle で読み、7 パレット × ライト/ダークの 14 ブロックを実測する。
 * 返り値の verdict が GREEN なら受け入れ、RED なら failures がそのまま是正対象。
 *
 * 目盛線の色で塗られた図形（ドーナツの台座・軸）は装飾なので 3:1 の対象から外す。
 * データマーク（棒・凡例・折れ線・点・ドーナツの各セグメント）だけを数える。
 */
(function () {
  const TEXT_MIN = 4.5;
  const MARK_MIN = 3.0;
  const PALETTES = ["solid-gray", "blue", "light-blue", "cyan", "green", "orange", "red"];

  /**
   * `rgb(r, g, b)` の相対輝度を返す。
   * @param {string} rgb
   * @returns {number}
   */
  function luminance(rgb) {
    const parts = rgb.match(/\d+/g).slice(0, 3).map(Number);
    const linear = (channel) => {
      const c = channel / 255;
      return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    };
    const [r, g, b] = parts.map(linear);
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  }

  /**
   * 2 色のコントラスト比を返す。
   * @param {string} first
   * @param {string} second
   * @returns {number}
   */
  function ratio(first, second) {
    const a = luminance(first);
    const b = luminance(second);
    const [hi, lo] = a > b ? [a, b] : [b, a];
    return (hi + 0.05) / (lo + 0.05);
  }

  /**
   * 要素が乗っているカードの背景色を返す。
   * @param {Element} el
   * @returns {string}
   */
  function backgroundOf(el) {
    return getComputedStyle(el.closest(".card")).backgroundColor;
  }

  /**
   * 図形の実効的な塗り色（fill 優先、無ければ stroke）を返す。
   * @param {Element} el
   * @returns {string}
   */
  function paintOf(el) {
    const style = getComputedStyle(el);
    const filled = style.fill !== "none" && style.fill !== "rgba(0, 0, 0, 0)";
    return filled ? style.fill : style.stroke;
  }

  /**
   * 目盛線色を解決済み rgb で返す。
   * @returns {string}
   */
  function gridlineRgb() {
    const raw = getComputedStyle(document.documentElement).getPropertyValue("--color-gridline").trim();
    const probe = document.createElement("span");
    probe.style.color = raw;
    document.body.appendChild(probe);
    const resolved = getComputedStyle(probe).color;
    probe.remove();
    return resolved;
  }

  /**
   * データマークだけを集める（装飾の目盛線・台座は除く）。
   * @returns {Element[]}
   */
  function dataMarks() {
    const grid = gridlineRgb();
    return [...document.querySelectorAll("svg rect, svg polyline, svg circle")].filter(
      (mark) => paintOf(mark) !== grid
    );
  }

  const failures = [];
  const blocks = [];
  const select = document.getElementById("palette-select");
  for (const palette of PALETTES) {
    if (select) {
      select.value = palette;
      select.dispatchEvent(new Event("change"));
    }
    for (const theme of ["light", "dark"]) {
      const button = document.querySelector(`[data-theme-value="${theme}"]`);
      if (!button) {
        failures.push(`${theme} のトグルが無い`);
        continue;
      }
      button.click();
      const marks = dataMarks();
      let worst = Infinity;
      for (const mark of marks) {
        const value = ratio(paintOf(mark), backgroundOf(mark));
        worst = Math.min(worst, value);
        if (value < MARK_MIN) {
          failures.push(`${palette}/${theme} マーク ${value.toFixed(2)} (${paintOf(mark)})`);
        }
      }
      const texts = ".kpi, .kpi-label, .chart-title, .delta-positive, .delta-negative, td, th";
      for (const el of document.querySelectorAll(texts)) {
        const value = ratio(getComputedStyle(el).color, backgroundOf(el));
        if (value < TEXT_MIN) {
          failures.push(`${palette}/${theme} 文字 ${value.toFixed(2)} <${el.className || el.tagName}>`);
        }
      }
      if (button.getAttribute("aria-pressed") !== "true") {
        failures.push(`${palette}/${theme} aria-pressed が押下状態になっていない`);
      }
      if (getComputedStyle(document.documentElement).colorScheme !== theme) {
        failures.push(`${palette}/${theme} color-scheme が data-theme に追従していない`);
      }
      blocks.push({ block: `${palette}/${theme}`, marks: marks.length, worst: Number(worst.toFixed(2)) });
    }
  }
  return { verdict: failures.length ? "RED" : "GREEN", blocks, failures };
})();
