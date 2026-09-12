/**
 * テーマ / ベースカラーの永続、サイドバーの開閉、入場アニメーションを担う。
 *
 * `<head>` で defer 読み込みする。テーマの復元だけは即時に走らせ、初回描画で
 * 白がちらつかないようにする（DOM 構築を待つのは操作の結線だけ）。
 */
(() => {
  "use strict";

  const THEME_KEY = "ui-theme";
  const BASE_KEY = "ui-base";
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

  const savedBase = read(BASE_KEY);
  if (savedBase && /^[a-z-]+$/.test(savedBase)) {
    root.dataset.base = savedBase;
  }

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");

  // --- 小物 -----------------------------------------------------------------

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
   * 画面右下へ短いメッセージを出す。
   * @param {string} message 表示文字列
   */
  const notify = (message) => {
    const toast = document.querySelector(".toast");
    if (!toast) return;
    toast.textContent = message;
    toast.hidden = false;
    window.clearTimeout(notify.timer);
    notify.timer = window.setTimeout(() => {
      toast.hidden = true;
    }, 3200);
  };

  // --- 結線 -----------------------------------------------------------------

  /** テーマ切替を結線する。 */
  const bindTheme = () => {
    const buttons = document.querySelectorAll("[data-theme-value]");
    buttons.forEach((button) => {
      button.addEventListener("click", () => {
        const value = button.dataset.themeValue;
        root.dataset.theme = value;
        write(THEME_KEY, value);
        syncPressed(buttons, "themeValue", value);
        notify(value === "dark" ? "ダークモードに切り替えました" : "ライトモードに切り替えました");
      });
    });
    syncPressed(buttons, "themeValue", root.dataset.theme);
  };

  /** ベースカラー切替を結線する。 */
  const bindBase = () => {
    const buttons = document.querySelectorAll("[data-base-value]");
    buttons.forEach((button) => {
      button.addEventListener("click", () => {
        const value = button.dataset.baseValue;
        root.dataset.base = value;
        write(BASE_KEY, value);
        syncPressed(buttons, "baseValue", value);
        notify(`配色を ${button.dataset.baseLabel || value} に変更しました`);
      });
    });
    syncPressed(buttons, "baseValue", root.dataset.base);
  };

  /** 狭い画面のサイドバー開閉を結線する。 */
  const bindSidebar = () => {
    const sidebar = document.getElementById("sidebar");
    const opener = document.querySelector('[data-sidebar="open"]');
    if (!sidebar || !opener) return;
    let scrim = null;

    const close = () => {
      sidebar.dataset.open = "false";
      opener.setAttribute("aria-expanded", "false");
      if (scrim) {
        scrim.remove();
        scrim = null;
      }
      opener.focus();
    };

    const open = () => {
      sidebar.dataset.open = "true";
      opener.setAttribute("aria-expanded", "true");
      scrim = document.createElement("div");
      scrim.className = "sidebar__scrim";
      scrim.addEventListener("click", close);
      document.body.append(scrim);
      const first = sidebar.querySelector("button");
      if (first) first.focus();
    };

    opener.addEventListener("click", open);
    document.querySelectorAll('[data-sidebar="close"]').forEach((button) => {
      button.addEventListener("click", close);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && sidebar.dataset.open === "true") close();
    });
  };

  /** ナビゲーションの現在地表示を切り替える。 */
  const bindNav = () => {
    const items = document.querySelectorAll(".nav-item");
    items.forEach((item) => {
      item.addEventListener("click", () => {
        items.forEach((other) => other.removeAttribute("aria-current"));
        item.setAttribute("aria-current", "page");
      });
    });
  };

  /** `/` で検索へ飛ばす。入力中は横取りしない。 */
  const bindSearchKey = () => {
    const field = document.getElementById("q");
    if (!field) return;
    document.addEventListener("keydown", (event) => {
      const tag = document.activeElement ? document.activeElement.tagName : "";
      if (event.key !== "/" || tag === "INPUT" || tag === "TEXTAREA") return;
      event.preventDefault();
      field.focus();
    });
  };

  /**
   * 画面に入った要素へ data-animate を付け、CSS 側の入場アニメーションを起動する。
   *
   * 静止状態（属性なし）が完成形なので、JS 無効・印刷・モーション低減の
   * いずれでも中身はそのまま読める。加えて 2 つの保険を掛ける。
   *
   * - タブが背面のときは属性を付けない。背面ではアニメーションのタイムラインが
   *   進まないため、付けると `from` の状態（不可視）で固まってしまう。
   * - 走り終わったら属性を外す。fill-mode の解釈に頼らず、最終状態を
   *   「素の状態」に戻すことで、中断しても要素が消えない。
   */
  const revealOnScroll = () => {
    const targets = document.querySelectorAll(".reveal:not([data-animate-skip])");
    if (reduced.matches || !("IntersectionObserver" in window)) return;
    const pending = new Set();

    const play = (element) => {
      if (document.visibilityState === "hidden") {
        pending.add(element);
        return;
      }
      element.dataset.animate = "in";
      window.setTimeout(() => delete element.dataset.animate, 2000);
    };

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          observer.unobserve(entry.target);
          play(entry.target);
        });
      },
      { rootMargin: "0px 0px -6% 0px", threshold: 0.08 }
    );
    targets.forEach((element) => observer.observe(element));

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") return;
      pending.forEach(play);
      pending.clear();
    });
  };

  /**
   * 指標の数値をカウントアップする。桁区切りと小数桁は元の表記を保つ。
   *
   * 元の文字列を最初に書き戻せる状態で持つので、途中で止まっても
   * 最終値が壊れることはない。
   */
  const countUp = () => {
    const targets = document.querySelectorAll("[data-count]");
    if (reduced.matches) return;
    targets.forEach((element, order) => {
      const final = element.dataset.count;
      const numeric = Number(final.replace(/,/g, ""));
      if (!Number.isFinite(numeric)) return;
      const decimals = (final.split(".")[1] || "").length;
      const grouped = final.includes(",");
      const duration = 900;
      const start = performance.now() + order * 80;
      const paint = (now) => {
        const progress = Math.min(Math.max((now - start) / duration, 0), 1);
        const eased = 1 - Math.pow(1 - progress, 3);
        const value = numeric * eased;
        element.textContent = grouped
          ? value.toLocaleString("ja-JP", { maximumFractionDigits: decimals })
          : value.toFixed(decimals);
        if (progress < 1) {
          window.requestAnimationFrame(paint);
        } else {
          element.textContent = final;
        }
      };
      window.requestAnimationFrame(paint);
    });
  };

  /** 全体を結線する。 */
  const boot = () => {
    bindTheme();
    bindBase();
    bindSidebar();
    bindNav();
    bindSearchKey();
    revealOnScroll();
    countUp();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
