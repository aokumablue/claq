/**
 * テーマ / ベースカラーの永続、サイドバーの開閉、ナビゲーションの現在地、一覧の検索・絞り込み、図の値の浮き札、
 * 描画倍率（--chart-scale）の受け渡し、入場アニメーションと数値のカウントアップを担う。
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
      opener.focus({ preventScroll: true });
    };

    const open = () => {
      sidebar.dataset.open = "true";
      opener.setAttribute("aria-expanded", "true");
      scrim = document.createElement("div");
      scrim.className = "sidebar__scrim";
      scrim.addEventListener("click", close);
      document.body.append(scrim);
      const first = sidebar.querySelector("button");
      if (first) first.focus({ preventScroll: true });
    };

    opener.addEventListener("click", open);
    document.querySelectorAll('[data-sidebar="close"]').forEach((button) => {
      button.addEventListener("click", close);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && sidebar.dataset.open === "true") close();
    });
  };

  /**
   * サイドバーのリンクを結線する。
   *
   * 移動はブラウザ標準のページ内リンク（`<a href="#id">`）に任せ、ここでは
   * 現在地の表示と、狭い画面でのサイドバーを閉じる動作だけを担う。現在地は
   * スクロール位置から決める。最下部では見出しが上端へ届かない節があるので、
   * 最下部では最後の項目とし、直前にクリックした項目があれば次の操作
   * （ホイール・タッチ・キー・ポインタ）までそれを優先する。
   */
  const bindNav = () => {
    const items = [...document.querySelectorAll('.nav-item[href^="#"]')];
    const sections = items.map((item) => document.getElementById(item.getAttribute("href").slice(1)));
    const sidebar = document.getElementById("sidebar");
    let pinned = null;

    const mark = (current) => {
      items.forEach((item) => {
        if (item === current) item.setAttribute("aria-current", "location");
        else item.removeAttribute("aria-current");
      });
    };

    const sync = () => {
      let current = 0;
      sections.forEach((section, index) => {
        if (section.getBoundingClientRect().top <= 96) current = index;
      });
      const atBottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2;
      if (atBottom) current = pinned ?? items.length - 1;
      mark(items[current]);
    };

    items.forEach((item, index) => {
      item.addEventListener("click", () => {
        pinned = index;
        mark(item);
        if (sidebar.dataset.open === "true") document.querySelector('[data-sidebar="close"]').click();
      });
    });
    ["wheel", "touchmove", "keydown", "pointerdown"].forEach((type) => {
      window.addEventListener(type, () => {
        pinned = null;
      }, { passive: true });
    });
    let frame = 0;
    window.addEventListener("scroll", () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(sync);
    }, { passive: true });
    sync();
  };

  /**
   * 図のマークの <title> を、ホバー・タップした位置へ浮き札で即座に出す。
   *
   * ブラウザ既定のツールチップは出るまで約 1 秒かかり、タッチでは出ない。
   * 値の出どころは <title> 1 か所のままにし、表示中だけ中身を退避して
   * 既定のツールチップとの二重表示を防ぐ。
   */
  const bindChartTips = () => {
    const tip = document.createElement("div");
    tip.className = "chart-tip";
    tip.hidden = true;
    tip.setAttribute("aria-hidden", "true");
    document.body.append(tip);
    let active = null;

    const hide = () => {
      if (!active) return;
      active.title.textContent = active.text;
      active = null;
      tip.hidden = true;
    };

    /**
     * 浮き札の中身を組む。複数行なら 1 行目（時点）だけを太字の見出しにする。
     *
     * `::first-line` で太字にすると 1 行だけの値まで太字になり、折り返した長い
     * 値では見た目の 1 行目だけが太くなる。行の区切りで要素を分ければ、
     * 見出しを持つ札だけが見出しを太く出せる。
     * @param {string} text <title> の文字列
     */
    const render = (text) => {
      const [head, ...rest] = text.split("\n");
      if (!rest.length) {
        tip.textContent = text;
        return;
      }
      const strong = document.createElement("strong");
      strong.textContent = head;
      tip.replaceChildren(strong, `\n${rest.join("\n")}`);
    };

    const follow = (event) => {
      const title = event.target.closest("svg.chart *")?.querySelector(":scope > title");
      if (!title) {
        hide();
        return;
      }
      if (active?.title !== title) {
        hide();
        active = { title, text: title.textContent };
        render(active.text);
        title.textContent = "";
        tip.hidden = false;
      }
      // innerWidth は縦のスクロールバーを含み、右端の札がその下へ潜る。
      const x = Math.min(event.clientX + 14, document.documentElement.clientWidth - tip.offsetWidth - 8);
      const above = event.clientY - tip.offsetHeight - 14;
      tip.style.translate = `${Math.max(x, 8)}px ${above < 8 ? event.clientY + 18 : above}px`;
    };

    document.addEventListener("pointermove", (event) => {
      if (event.pointerType !== "touch") follow(event);
    });
    document.addEventListener("pointerdown", (event) => {
      if (event.pointerType === "touch") follow(event);
    });
    // タッチは指を離すと <html> まで pointerleave が届くので、閉じるのは別の場所のタップ・スクロール・Esc に任せる。
    document.documentElement.addEventListener("pointerleave", (event) => {
      if (event.pointerType !== "touch") hide();
    });
    document.addEventListener("scroll", hide, { capture: true });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") hide();
    });
  };

  /**
   * 各チャートの描画倍率を `--chart-scale` として CSS へ渡す。
   *
   * SVG の文字は viewBox の倍率で一緒に伸びるので、広い画面では宣言 15px の目盛りが
   * 20px を超え、カード見出しより大きくなる。CSS は描画倍率を知れないため、描画幅・
   * 高さと viewBox の比のうち小さいほう（meet の縮尺）を渡し、CSS 側で描画後の
   * 大きさに上限を掛ける。倍率は viewBox に対して決まるので、viewBox を持つ図だけを見る。
   */
  const bindChartScale = () => {
    const observer = new ResizeObserver((entries) => {
      entries.forEach(({ target, contentRect }) => {
        const box = target.viewBox.baseVal;
        const scale = Math.min(contentRect.width / box.width, contentRect.height / box.height);
        target.style.setProperty("--chart-scale", String(scale));
      });
    });
    document.querySelectorAll("svg.chart[viewBox]").forEach((svg) => observer.observe(svg));
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

  /** 一覧の検索語・選択値の比較用に、全角半角・大小・空白の揺れをそろえる。 */
  const fold = (text) => text.normalize("NFKC").toLowerCase().replace(/\s+/g, " ").trim();

  /**
   * 絞り込みの対象にする列を選ぶ。手書きの指定は要らない。
   *
   * 条件: 行見出しでも数値でもなく、全行が値を持ち、異なる値が 2〜6 個で、かつ
   * 行数より少ない（全行が別の値なら識別子や氏名で、絞り込みにならない）。
   * 状態・ステータス系の見出しを先頭に、残りは左から。最大 2 列。
   * @param {HTMLTableElement} table 対象の表
   * @param {HTMLTableRowElement[]} rows 本体の行
   * @returns {{col: number, label: string, values: string[]}[]} 選んだ列
   */
  const pickFacets = (table, rows) => {
    const status = /状態|ステータス|状況|status/i;
    return [...table.tHead.rows[0].cells]
      .map((th, col) => {
        const cells = rows.map((row) => row.cells[col]);
        const label = th.textContent.trim();
        const values = [...new Set(cells.map((cell) => cell.textContent.trim()))];
        const plain = cells.every((cell) => cell.tagName === "TD" && !cell.classList.contains("num"));
        const fits = plain && !values.includes("") && values.length >= 2 && values.length <= 6 && values.length < rows.length;
        return { col, label, values, fits };
      })
      .filter((facet) => facet.fits)
      .sort((a, b) => Number(status.test(b.label)) - Number(status.test(a.label)))
      .slice(0, 2);
  };

  /**
   * `table[data-list]` へ検索欄・主要項目の絞り込み・件数表示を付ける。
   *
   * ツールバーは表の直前にここで生成する。HTML に検索欄を手書きしても結線されない
   * ので置かない。検索語は空白区切りで全語を含む行だけを残し（AND）、絞り込みとも
   * AND で合わせる。form で包まないため Enter でページは再読込されない。
   * 該当 0 件のときは空の行を出す。
   */
  const bindList = () => {
    document.querySelectorAll("table[data-list]").forEach((table) => {
      const body = table.tBodies[0];
      const rows = [...body.rows];
      if (!rows.length) return;
      const name = table.caption ? table.caption.textContent.trim() : "一覧";
      const haystack = new Map(rows.map((row) => [row, fold(row.textContent)]));

      const tools = document.createElement("div");
      tools.className = "list-tools";
      tools.setAttribute("role", "search");
      const search = document.createElement("input");
      search.type = "search";
      search.className = "list-tools__field list-tools__field--search";
      search.placeholder = "検索";
      search.autocomplete = "off";
      search.setAttribute("aria-label", `${name}を検索`);
      tools.append(search);

      const selects = pickFacets(table, rows).map((facet) => {
        const select = document.createElement("select");
        select.className = "list-tools__field";
        select.setAttribute("aria-label", `${facet.label}で絞り込む`);
        select.append(new Option(`${facet.label}: すべて`, ""));
        facet.values.forEach((value) => select.append(new Option(value, value)));
        tools.append(select);
        return { col: facet.col, select };
      });

      const count = document.createElement("span");
      count.className = "list-tools__count";
      count.setAttribute("role", "status");
      count.setAttribute("aria-live", "polite");
      tools.append(count);

      const empty = body.insertRow();
      empty.className = "list-empty";
      empty.hidden = true;
      const cell = empty.insertCell();
      cell.colSpan = table.tHead.rows[0].cells.length;
      cell.textContent = "条件に合う行がありません";

      const apply = () => {
        const words = fold(search.value).split(" ").filter(Boolean);
        let shown = 0;
        rows.forEach((row) => {
          const visible =
            words.every((word) => haystack.get(row).includes(word)) &&
            selects.every(({ col, select }) => !select.value || row.cells[col].textContent.trim() === select.value);
          row.hidden = !visible;
          if (visible) shown += 1;
        });
        empty.hidden = shown > 0;
        count.textContent = `${rows.length} 件中 ${shown} 件`;
      };

      search.addEventListener("input", apply);
      selects.forEach(({ select }) => select.addEventListener("change", apply));
      (table.closest(".table-scroll") ?? table).before(tools);
      apply();
    });
  };

  /** 全体を結線する。 */
  const boot = () => {
    bindTheme();
    bindBase();
    bindSidebar();
    bindNav();
    bindChartTips();
    bindChartScale();
    revealOnScroll();
    countUp();
    bindList();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
