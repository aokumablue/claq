/**
 * ホワイト / ダークの選択と 7 色パレットの適用。
 * 色の計算はしない。html の data-* を切り替えて CSS 変数ブロックを発火させる。
 *
 * <head> から同期読み込みする前提。最初の描画より前に data-theme を確定させるため、
 * ルート要素への適用は即時に行い、ボタン・セレクトの同期だけ DOMContentLoaded へ回す。
 */
(function () {
  const THEME_KEY = "digital-go-theme";
  const PALETTE_KEY = "digital-go-palette";
  const PALETTES = ["solid-gray", "blue", "light-blue", "cyan", "green", "orange", "red"];

  /**
   * localStorage を読む。プライベートウィンドウ等で例外になっても落とさない。
   * @param {string} key
   * @returns {string|null}
   */
  function readStored(key) {
    try {
      return localStorage.getItem(key);
    } catch (err) {
      return null;
    }
  }

  /**
   * localStorage へ書く。失敗しても表示は続ける。
   * @param {string} key
   * @param {string} value
   */
  function writeStored(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch (err) {
      /* 保存できない環境ではセッション内の切替のみ有効 */
    }
  }

  /**
   * 保存値または OS 設定から初期テーマを決める。
   * @returns {"light"|"dark"}
   */
  function initialTheme() {
    const saved = readStored(THEME_KEY);
    if (saved === "light" || saved === "dark") {
      return saved;
    }
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  /**
   * 保存値から初期パレットを決める。不正値は html[data-palette]、無ければ Blue。
   * @returns {string}
   */
  function initialPalette() {
    const saved = readStored(PALETTE_KEY);
    if (PALETTES.includes(saved)) {
      return saved;
    }
    const fromDom = document.documentElement.dataset.palette;
    return PALETTES.includes(fromDom) ? fromDom : "blue";
  }

  /**
   * ルート要素の data-theme / data-palette を確定させる。head から即時に呼べる。
   * @param {"light"|"dark"} theme
   * @param {string} palette
   */
  function applyRoot(theme, palette) {
    const root = document.documentElement;
    root.dataset.theme = theme;
    root.dataset.palette = palette;
    writeStored(THEME_KEY, theme);
    writeStored(PALETTE_KEY, palette);
  }

  /**
   * トグルの aria-pressed とパレットセレクトを現在値へ同期する。
   * @param {"light"|"dark"} theme
   * @param {string} palette
   */
  function syncControls(theme, palette) {
    document.querySelectorAll("[data-theme-value]").forEach((button) => {
      button.setAttribute("aria-pressed", button.getAttribute("data-theme-value") === theme ? "true" : "false");
    });
    const select = document.getElementById("palette-select");
    if (select && select.value !== palette) {
      select.value = palette;
    }
  }

  /**
   * ルートとコントロールの両方を更新する。
   * @param {"light"|"dark"} theme
   * @param {string} palette
   */
  function apply(theme, palette) {
    applyRoot(theme, palette);
    syncControls(theme, palette);
  }

  applyRoot(initialTheme(), initialPalette());

  document.addEventListener("DOMContentLoaded", () => {
    const root = document.documentElement;
    syncControls(root.dataset.theme, root.dataset.palette);
    document.querySelectorAll("[data-theme-value]").forEach((button) => {
      button.addEventListener("click", () => {
        const next = button.getAttribute("data-theme-value");
        if (next === "light" || next === "dark") {
          apply(next, root.dataset.palette || "blue");
        }
      });
    });
    const select = document.getElementById("palette-select");
    if (select) {
      select.addEventListener("change", () => {
        const palette = PALETTES.includes(select.value) ? select.value : "blue";
        apply(root.dataset.theme === "dark" ? "dark" : "light", palette);
      });
    }
  });
})();
