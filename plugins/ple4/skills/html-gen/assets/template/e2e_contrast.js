/**
 * ブラウザで実際に解決された色を巡回し、可読性契約を実測する。
 *
 * DevTools のコンソールへ貼り付けて実行する。全 seed × ライト/ダークを順に
 * 適用し、`getComputedStyle` が返す**描画された値**で比を測る。check_site.py は
 * tokens.json 上の値しか見ないので、CSS の上書きやブレンドで落ちた分はここでしか
 * 出ない。`verdict` が GREEN なら受け入れ、RED なら `failures` がそのまま是正対象。
 */
(() => {
  "use strict";

  const TEXT_MIN = 4.5;
  const MARK_MIN = 3.0;
  const SEEDS = ["indigo", "azure", "teal", "verdant", "amber", "crimson"];
  const THEMES = ["light", "dark"];
  const CHARTS = ["--chart-1", "--chart-2", "--chart-3", "--chart-4", "--chart-5", "--chart-6"];
  const PAIRS = [
    ["--md-sys-color-on-surface", "--md-sys-color-surface", TEXT_MIN],
    ["--md-sys-color-on-surface", "--md-sys-color-surface-container", TEXT_MIN],
    ["--md-sys-color-on-surface-variant", "--md-sys-color-surface-container", TEXT_MIN],
    ["--md-sys-color-on-primary", "--md-sys-color-primary", TEXT_MIN],
    ["--md-sys-color-on-primary-container", "--md-sys-color-primary-container", TEXT_MIN],
    ["--md-sys-color-on-secondary-container", "--md-sys-color-secondary-container", TEXT_MIN],
    ["--md-sys-color-inverse-on-surface", "--md-sys-color-inverse-surface", TEXT_MIN],
    ["--md-sys-color-primary", "--md-sys-color-surface-container", TEXT_MIN],
    ["--md-sys-color-positive", "--md-sys-color-surface-container", TEXT_MIN],
    ["--md-sys-color-negative", "--md-sys-color-surface-container", TEXT_MIN],
    ["--md-sys-color-outline", "--md-sys-color-surface", MARK_MIN],
    ...CHARTS.map((name) => [name, "--md-sys-color-surface-container", MARK_MIN]),
  ];

  const root = document.documentElement;
  const original = { theme: root.dataset.theme, seed: root.dataset.seed };

  /**
   * CSS の色文字列を 0-255 の RGB へ正規化する。
   * @param {string} value `#RRGGBB` か `rgb(...)`
   * @returns {number[]} [r, g, b]
   */
  const toRgb = (value) => {
    const text = value.trim();
    if (text.startsWith("#")) {
      const body =
        text.length === 4
          ? text
              .slice(1)
              .split("")
              .map((ch) => ch + ch)
              .join("")
          : text.slice(1, 7);
      return [0, 2, 4].map((i) => parseInt(body.slice(i, i + 2), 16));
    }
    const parts = text.match(/[\d.]+/g) || [];
    return parts.slice(0, 3).map(Number);
  };

  /**
   * 相対輝度を返す。
   * @param {string} value CSS 色
   * @returns {number} 0〜1
   */
  const luminance = (value) => {
    const [r, g, b] = toRgb(value).map((channel) => {
      const scaled = channel / 255;
      return scaled <= 0.04045 ? scaled / 12.92 : ((scaled + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };

  /**
   * コントラスト比を返す。
   * @param {string} first 前景色
   * @param {string} second 背景色
   * @returns {number} 比率
   */
  const ratio = (first, second) => {
    const [hi, lo] = [luminance(first), luminance(second)].sort((a, b) => b - a);
    return (hi + 0.05) / (lo + 0.05);
  };

  const failures = [];
  const rows = [];

  SEEDS.forEach((seed) => {
    THEMES.forEach((theme) => {
      root.dataset.seed = seed;
      root.dataset.theme = theme;
      const styles = getComputedStyle(root);
      const resolve = (name) => styles.getPropertyValue(name).trim();
      PAIRS.forEach(([foreground, background, minimum]) => {
        const value = ratio(resolve(foreground), resolve(background));
        const record = {
          block: `${theme}/${seed}`,
          foreground,
          background,
          ratio: Number(value.toFixed(2)),
          minimum,
        };
        rows.push(record);
        if (value < minimum) failures.push(record);
      });
    });
  });

  root.dataset.theme = original.theme;
  root.dataset.seed = original.seed;

  const report = {
    verdict: failures.length === 0 ? "GREEN" : "RED",
    checked: rows.length,
    failures,
  };
  console.table(failures.length ? failures : rows.slice(0, 12));
  console.log(report.verdict, `${rows.length} pairs checked, ${failures.length} failed`);
  return report;
})();
