/**
 * テーマ / 配色シードの永続と、チャートの入場アニメーションを担う。
 *
 * `<head>` で defer 読み込みする。テーマの復元だけは即時に走らせ、初回描画で
 * 白がちらつかないようにする（DOM 構築を待つのは操作の結線だけ）。
 */
(() => {
  "use strict";

  const THEME_KEY = "md3-theme";
  const SEED_KEY = "md3-seed";
  const root = document.documentElement;

  /**
   * localStorage が使えない環境（プライベートモード等）でも落ちないよう包む。
   * @param {string} key 読み出すキー
   * @returns {string|null} 保存値。読めなければ null
   */
  const read = (key) => {
    try {
      return window.localStorage.getItem(key);
    } catch {
      return null;
    }
  };

  /**
   * 設定を保存する。失敗しても操作は続行させる。
   * @param {string} key 保存するキー
   * @param {string} value 保存する値
   */
  const write = (key, value) => {
    try {
      window.localStorage.setItem(key, value);
    } catch {
      /* 保存できない環境ではセッション内だけ反映する */
    }
  };

  // --- 初期状態（描画前に確定させる） ---------------------------------------

  const savedTheme = read(THEME_KEY);
  if (savedTheme === "light" || savedTheme === "dark") {
    root.dataset.theme = savedTheme;
  } else if (window.matchMedia("(prefers-color-scheme: dark)").matches) {
    root.dataset.theme = "dark";
  }

  const savedSeed = read(SEED_KEY);
  if (savedSeed && /^[a-z-]+$/.test(savedSeed)) {
    root.dataset.seed = savedSeed;
  }

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");

  // --- 結線 -----------------------------------------------------------------

  /**
   * 押下状態のトグル群を現在値へ同期する。
   * @param {NodeListOf<HTMLElement>} buttons 対象ボタン
   * @param {string} attribute 比較する data 属性名
   * @param {string} current 現在値
   */
  const syncPressed = (buttons, attribute, current) => {
    buttons.forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset[attribute] === current));
    });
  };

  /**
   * スナックバーへ短いメッセージを出す。
   * @param {string} message 表示文字列
   */
  const notify = (message) => {
    const bar = document.querySelector(".snackbar");
    if (!bar) return;
    bar.textContent = message;
    bar.dataset.open = "true";
    window.clearTimeout(notify.timer);
    notify.timer = window.setTimeout(() => {
      bar.dataset.open = "false";
    }, 3200);
  };

  /** テーマ・シード・ツールチップ・入場アニメーションを結線する。 */
  const boot = () => {
    const themeButtons = document.querySelectorAll("[data-theme-value]");
    themeButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const value = button.dataset.themeValue;
        root.dataset.theme = value;
        write(THEME_KEY, value);
        syncPressed(themeButtons, "themeValue", value);
        notify(value === "dark" ? "ダークモードに切り替えました" : "ライトモードに切り替えました");
      });
    });
    syncPressed(themeButtons, "themeValue", root.dataset.theme);

    const seedButtons = document.querySelectorAll("[data-seed-value]");
    seedButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const value = button.dataset.seedValue;
        root.dataset.seed = value;
        write(SEED_KEY, value);
        syncPressed(seedButtons, "seedValue", value);
        notify(`配色を ${button.getAttribute("aria-label") || value} に変更しました`);
      });
    });
    syncPressed(seedButtons, "seedValue", root.dataset.seed);

    document.querySelectorAll(".nav__item").forEach((item) => {
      item.addEventListener("click", () => {
        document.querySelectorAll(".nav__item").forEach((other) => other.removeAttribute("aria-current"));
        item.setAttribute("aria-current", "page");
      });
    });

    const exportButton = document.querySelector('[data-action="export"]');
    if (exportButton) {
      exportButton.addEventListener("click", () => notify("レポートの書き出しを開始しました"));
    }

    bindTooltip();
    revealOnScroll();
  };

  /** data-tip を持つ図形にツールチップを出す。 */
  const bindTooltip = () => {
    const tip = document.querySelector(".tooltip");
    if (!tip) return;
    const show = (event) => {
      const target = event.target.closest("[data-tip]");
      if (!target) return;
      const box = target.getBoundingClientRect();
      tip.textContent = target.dataset.tip;
      tip.style.left = `${box.left + box.width / 2}px`;
      tip.style.top = `${Math.max(box.top - 12, 8)}px`;
      tip.dataset.open = "true";
    };
    const hide = () => {
      tip.dataset.open = "false";
    };
    document.addEventListener("pointerover", show);
    document.addEventListener("pointerout", hide);
    document.addEventListener("focusin", show);
    document.addEventListener("focusout", hide);
  };

  /**
   * 画面に入った要素へ .is-live を付け、CSS 側のアニメーションを起動する。
   * 低減設定のときは監視せず、最初から最終状態にする。
   */
  const revealOnScroll = () => {
    const targets = document.querySelectorAll(".reveal, .card, figure, section");
    if (reduced.matches || !("IntersectionObserver" in window)) {
      targets.forEach((element) => element.classList.add("is-live"));
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          entry.target.classList.add("is-live");
          observer.unobserve(entry.target);
        });
      },
      { rootMargin: "0px 0px -8% 0px", threshold: 0.08 }
    );
    targets.forEach((element) => observer.observe(element));
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
