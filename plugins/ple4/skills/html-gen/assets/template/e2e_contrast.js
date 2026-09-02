/**
 * ブラウザで実際に解決された色を巡回し、可読性契約を実測する。
 *
 * DevTools のコンソールへ貼り付けて実行する。全ベースカラー × ライト/ダークを
 * 順に適用し、ブラウザが正規化した**描画された値**で比を測る。check_site.py は
 * tokens.json 上の値しか見ないので、CSS の上書きや半透明のブレンドで落ちた分は
 * ここでしか出ない。`verdict` が GREEN なら受け入れ、RED なら `failures` が
 * そのまま是正対象。
 */
(() => {
  "use strict";

  const TEXT_MIN = 4.5;
  const MARK_MIN = 3.0;
  const BASES = ["neutral", "zinc", "slate", "stone", "gray"];
  const THEMES = ["light", "dark"];
  const CHARTS = ["--chart-1", "--chart-2", "--chart-3", "--chart-4", "--chart-5"];
  const PAIRS = [
    ["--foreground", "--background", TEXT_MIN],
    ["--foreground", "--card", TEXT_MIN],
    ["--foreground", "--muted", TEXT_MIN],
    ["--foreground", "--accent", TEXT_MIN],
    ["--card-foreground", "--card", TEXT_MIN],
    ["--popover-foreground", "--popover", TEXT_MIN],
    ["--primary-foreground", "--primary", TEXT_MIN],
    ["--secondary-foreground", "--secondary", TEXT_MIN],
    ["--accent-foreground", "--accent", TEXT_MIN],
    ["--muted-foreground", "--muted", TEXT_MIN],
    ["--muted-foreground", "--background", TEXT_MIN],
    ["--muted-foreground", "--card", TEXT_MIN],
    ["--destructive-foreground", "--destructive", TEXT_MIN],
    ["--destructive", "--background", TEXT_MIN],
    ["--destructive", "--card", TEXT_MIN],
    ["--primary", "--background", TEXT_MIN],
    ["--primary", "--card", TEXT_MIN],
    ["--sidebar-foreground", "--sidebar", TEXT_MIN],
    ["--sidebar-accent-foreground", "--sidebar-accent", TEXT_MIN],
    ["--sidebar-primary-foreground", "--sidebar-primary", TEXT_MIN],
    ["--ring", "--background", MARK_MIN],
    ["--ring", "--card", MARK_MIN],
    ["--sidebar-ring", "--sidebar", MARK_MIN],
    ...CHARTS.map((name) => [name, "--background", MARK_MIN]),
    ...CHARTS.map((name) => [name, "--card", MARK_MIN]),
    ...CHARTS.map((name) => [name, "--muted", MARK_MIN]),
  ];

  const root = document.documentElement;
  const original = { theme: root.dataset.theme, base: root.dataset.base };
  const probe = document.createElement("span");
  probe.style.display = "none";
  document.body.append(probe);
  const canvas = document.createElement("canvas");
  canvas.width = 1;
  canvas.height = 1;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });

  /**
   * 変数の値をブラウザに解決させ、sRGB の [r, g, b, a] で受け取る。
   *
   * `getComputedStyle` は `oklch()` を `oklch()` のまま返す（rgb へは
   * 正規化しない）。文字列を数値として読むと L C H を R G B と取り違えて
   * 全ペアが「ほぼ黒」になり、検査が丸ごと嘘をつく。1px の canvas へ
   * 実際に塗って読み出せば、ブラウザが描くのと同じ sRGB 値が得られる。
   * @param {string} name CSS カスタムプロパティ名
   * @returns {number[]} [r, g, b, a]
   */
  const resolve = (name) => {
    probe.style.color = "rgb(0, 0, 0)";
    probe.style.color = `var(${name})`;
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = getComputedStyle(probe).color;
    ctx.fillRect(0, 0, 1, 1);
    const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
    return [r, g, b, a / 255];
  };

  /**
   * 半透明の前景を背景へ重ねた実効色を返す。
   * @param {number[]} front 前景 [r, g, b, a]
   * @param {number[]} back 背景 [r, g, b, a]
   * @returns {number[]} [r, g, b]
   */
  const flatten = (front, back) =>
    [0, 1, 2].map((index) => front[3] * front[index] + (1 - front[3]) * back[index]);

  /**
   * 相対輝度を返す。
   * @param {number[]} rgb [r, g, b]
   * @returns {number} 0〜1
   */
  const luminance = (rgb) => {
    const [r, g, b] = rgb.map((channel) => {
      const scaled = channel / 255;
      return scaled <= 0.04045 ? scaled / 12.92 : ((scaled + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };

  /**
   * コントラスト比を返す。
   * @param {number[]} first 前景 [r, g, b]
   * @param {number[]} second 背景 [r, g, b]
   * @returns {number} 比率
   */
  const ratio = (first, second) => {
    const [hi, lo] = [luminance(first), luminance(second)].sort((a, b) => b - a);
    return (hi + 0.05) / (lo + 0.05);
  };

  const failures = [];
  const rows = [];

  BASES.forEach((base) => {
    THEMES.forEach((theme) => {
      root.dataset.base = base;
      root.dataset.theme = theme;
      PAIRS.forEach(([foreground, background, minimum]) => {
        const back = resolve(background);
        const value = ratio(flatten(resolve(foreground), back), flatten(back, [255, 255, 255, 1]));
        const record = {
          block: `${theme}/${base}`,
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
  root.dataset.base = original.base;
  probe.remove();

  const report = {
    verdict: failures.length === 0 ? "GREEN" : "RED",
    checked: rows.length,
    failures,
  };
  console.table(failures.length ? failures : rows.slice(0, 12));
  console.log(report.verdict, `${rows.length} pairs checked, ${failures.length} failed`);
  return report;
})();
