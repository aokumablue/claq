/**
 * ブラウザで実際に解決された色を巡回し、可読性契約を実測する。
 *
 * DevTools のコンソールへ貼り付けて実行する。全ベースカラー × ライト/ダークを
 * 順に適用し、ブラウザが正規化した**描画された値**で比を測る。check_site.py は
 * tokens.json 上の値でコントラストを測るので、CSS の上書きや半透明のブレンドで落ちた分は
 * ここでしか出ない。`verdict` が GREEN なら受け入れ、RED なら `failures`・`textFailures`・
 * `behaviorFailures` に出た項目を直す。
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
    ["--muted-foreground", "--sidebar", TEXT_MIN],
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
  // テーマを切り替えると色が 150ms かけて遷移する。その途中を読むと
  // 「切り替え前の色」で比を測ってしまうので、測定中だけ遷移を止める。
  const freeze = document.createElement("style");
  freeze.textContent = "*, *::before, *::after { transition: none !important; }";
  document.head.append(freeze);
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
  const paint = (value) => {
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = value;
    ctx.fillRect(0, 0, 1, 1);
    const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
    return [r, g, b, a / 255];
  };

  const resolve = (name) => {
    probe.style.color = "rgb(0, 0, 0)";
    probe.style.color = `var(${name})`;
    return paint(getComputedStyle(probe).color);
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

  /**
   * 要素の実背景を返す。半透明が重なっていれば下から順に合成する。
   * @param {Element} element 対象要素
   * @returns {number[]} [r, g, b]
   */
  const backdropOf = (element) => {
    const layers = [];
    let node = element;
    while (node) {
      const background = paint(getComputedStyle(node).backgroundColor);
      if (background[3] > 0) layers.push(background);
      if (background[3] >= 1) break;
      node = node.parentElement;
    }
    return layers.reduceRight(
      (under, over) => flatten(over, [...under, 1]),
      [255, 255, 255]
    );
  };

  /**
   * 実際に描かれた文字とその背景でコントラストを測る。
   *
   * PAIRS は変数どうしの比較なので、`color-mix` や不透明度で作った色は
   * どちらの側にも現れない。ここだけが「合成した結果の文字色」を見る。
   * @param {string} block 現在のテーマ / ベースカラー
   * @returns {object[]} 契約を割った要素の記録
   */
  const measureRenderedText = (block) => {
    const found = [];
    document.querySelectorAll("body *").forEach((element) => {
      if (element.closest("[aria-hidden='true']")) return;
      const own = Array.from(element.childNodes)
        .filter((node) => node.nodeType === 3)
        .map((node) => node.textContent.trim())
        .join("");
      if (!own) return;
      const box = element.getBoundingClientRect();
      if (box.width < 4 || box.height < 4) return;
      const style = getComputedStyle(element);
      if (style.visibility === "hidden" || style.display === "none") return;
      const size = parseFloat(style.fontSize);
      const weight = Number(style.fontWeight) || 400;
      const minimum = size >= 24 || (size >= 18.66 && weight >= 700) ? MARK_MIN : TEXT_MIN;
      const back = backdropOf(element);
      const value = ratio(flatten(paint(style.color), [...back, 1]), back);
      if (value < minimum) {
        found.push({
          block,
          selector: element.tagName.toLowerCase() + (element.className ? `.${String(element.className).split(" ")[0]}` : ""),
          sample: own.slice(0, 18),
          ratio: Number(value.toFixed(2)),
          minimum,
        });
      }
    });
    return found;
  };

  /**
   * 描画結果で、図の文字の大きさ・一覧の検索と絞り込みの動作を測る。
   *
   * 文字の大きさは viewBox の倍率（getScreenCTM）を掛けた描画後の px で見る。
   * computed style の font-size は viewBox 座標の値で、描画後の大きさではない。
   * 一覧は実際に検索語を入れて行が減り、0 件で空の行が出て、戻せることまで確かめる。
   * @returns {object[]} 契約を割った項目の記録
   */
  const measureBehavior = () => {
    const found = [];
    const sizes = [];
    document.querySelectorAll("main svg").forEach((svg, index) => {
      if (svg.closest("[aria-hidden='true']")) return;
      const scale = svg.getScreenCTM().a;
      svg.querySelectorAll("text").forEach((text) => {
        sizes.push({ chart: index + 1, px: parseFloat(getComputedStyle(text).fontSize) * scale });
      });
    });
    sizes.forEach(({ chart, px }) => {
      if (px < 11.9 || px > 13.6) found.push({ check: "chart-text-size", chart, px: Number(px.toFixed(2)) });
    });
    document.querySelectorAll(".nav-item").forEach((item) => {
      const id = (item.getAttribute("href") || "").slice(1);
      if (!id || !document.getElementById(id)) found.push({ check: "nav-target", item: item.textContent.trim() });
    });
    document.querySelectorAll("table[data-list]").forEach((table, index) => {
      const tools = table.closest(".table-scroll")?.previousElementSibling ?? table.previousElementSibling;
      const search = tools?.querySelector('input[type="search"]');
      if (!search) {
        found.push({ check: "list-search-missing", table: index + 1 });
        return;
      }
      const rows = [...table.tBodies[0].rows].filter((row) => !row.classList.contains("list-empty"));
      const shown = () => rows.filter((row) => !row.hidden).length;
      search.value = "\u0000no-such-row";
      search.dispatchEvent(new Event("input", { bubbles: true }));
      if (shown() !== 0 || table.querySelector(".list-empty").hidden) found.push({ check: "list-search-nomatch", table: index + 1 });
      search.value = "";
      search.dispatchEvent(new Event("input", { bubbles: true }));
      if (shown() !== rows.length) found.push({ check: "list-search-reset", table: index + 1 });
      tools.querySelectorAll("select").forEach((select) => {
        select.selectedIndex = 1;
        select.dispatchEvent(new Event("change", { bubbles: true }));
        if (shown() === 0 || shown() > rows.length) found.push({ check: "list-facet", table: index + 1, label: select.getAttribute("aria-label") });
        select.selectedIndex = 0;
        select.dispatchEvent(new Event("change", { bubbles: true }));
      });
    });
    return found;
  };

  const failures = [];
  const textFailures = [];
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
      textFailures.push(...measureRenderedText(`${theme}/${base}`));
    });
  });

  const behaviorFailures = measureBehavior();

  root.dataset.theme = original.theme;
  root.dataset.base = original.base;
  probe.remove();
  freeze.remove();

  const report = {
    verdict: failures.length === 0 && textFailures.length === 0 && behaviorFailures.length === 0 ? "GREEN" : "RED",
    checked: rows.length,
    failures,
    textFailures,
    behaviorFailures,
  };
  console.table(failures.length ? failures : rows.slice(0, 12));
  if (textFailures.length) console.table(textFailures);
  if (behaviorFailures.length) console.table(behaviorFailures);
  console.log(
    report.verdict,
    `${rows.length} pairs checked, ${failures.length} failed;`,
    `rendered text: ${textFailures.length} failed;`,
    `behavior: ${behaviorFailures.length} failed`
  );
  return report;
})();
