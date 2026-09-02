"""デジタル庁ダッシュボードデザインテンプレート準拠を機械検査する。

標準ライブラリのみ。生成サイトのディレクトリを渡し、公式カラーコード
（2026-07-17）とガイドブックのレイアウト契約から外れた色・欠落を報告する。
色のコントラストは DADS アクセシビリティ（文字 4.5:1 / 非文字 3:1）で判定する。

    python3 check_site.py .
    python3 check_site.py --write-css   # tokens.json から styles.css を再生成（メンテ用）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

HEX_RE = re.compile(r"#[0-9A-Fa-f]{3,8}\b")
#: `background-color` や `-webkit-text-fill-color` のようにハイフンで
#: 連結されたプロパティも拾うため、直前の `-` は語境界として許す。
NAMED_COLOR_RE = re.compile(
    r"(?<!\w)(?:color|background|fill|stroke|border|outline)(?:-\w+)*\s*:\s*"
    r"(?!var\(|#|-)"
    r"([a-z][a-z0-9-]*)",
    re.IGNORECASE,
)
THREE_D_RE = re.compile(
    r"(?:rotate3d|translate3d|scale3d|matrix3d|perspective)\s*\(|transform-style\s*:\s*preserve-3d",
    re.IGNORECASE,
)
BOX_SHADOW_RE = re.compile(r"box-shadow\s*:\s*([^;}]+)", re.IGNORECASE)
DROP_SHADOW_RE = re.compile(r"filter\s*:\s*[^;}]*drop-shadow\s*\(", re.IGNORECASE)
PALETTE_IDS = ("solid-gray", "blue", "light-blue", "cyan", "green", "orange", "red")
THEMES = ("light", "dark")
CANVASES = ("16x9", "4x3")
PAGE_KINDS = ("dashboard", "content")
CHART_VARS = ("--chart-1", "--chart-2", "--chart-3", "--chart-4", "--chart-5")
REQUIRED_VARS = (
    "--color-text",
    "--color-text-muted",
    "--color-link",
    "--color-page",
    "--color-surface",
    "--color-control",
    "--color-highlight",
    "--color-highlight-text",
    "--color-success",
    "--color-error",
    "--color-positive",
    "--color-negative",
    "--color-gridline",
    "--color-outline",
    "--color-focus",
    "--color-focus-edge",
    *CHART_VARS,
)
#: (前景変数, 背景変数, 最低比率)。DADS: 文字 4.5:1 / 非文字 3:1。
CONTRAST_CONTRACT: tuple[tuple[str, str, float], ...] = (
    ("--color-text", "--color-surface", 4.5),
    ("--color-text", "--color-page", 4.5),
    ("--color-text-muted", "--color-surface", 4.5),
    ("--color-text-muted", "--color-page", 4.5),
    ("--color-link", "--color-surface", 4.5),
    ("--color-link", "--color-page", 4.5),
    ("--color-positive", "--color-surface", 4.5),
    ("--color-negative", "--color-surface", 4.5),
    ("--color-success", "--color-surface", 4.5),
    ("--color-error", "--color-surface", 4.5),
    ("--color-highlight-text", "--color-highlight", 4.5),
    ("--color-outline", "--color-page", 3.0),
    ("--color-focus-edge", "--color-page", 3.0),
    ("--color-focus-edge", "--color-surface", 3.0),
    *((name, "--color-surface", 3.0) for name in CHART_VARS),
)
#: 色ではない CSS キーワード。色プロパティの値に現れても違反にしない。
NON_COLOR_KEYWORDS = frozenset(
    {
        "transparent", "currentcolor", "inherit", "initial", "unset", "revert", "none",
        "auto", "solid", "dashed", "dotted", "double", "groove", "ridge", "inset",
        "outset", "hidden", "thin", "medium", "thick", "linear-gradient", "url",
        "var", "repeat", "no-repeat", "center", "cover", "contain", "content-box",
        "padding-box", "border-box", "collapse", "separate",
        # color-scheme の値。CSS の名前付き色ではない。
        "light", "dark", "normal", "only",
    }
)


def _norm_hex(value: str) -> str:
    """`#abc` を `#AABBCC` に正規化する。"""
    raw = value.strip().upper()
    if not raw.startswith("#"):
        raise ValueError(f"hex ではない: {value}")
    body = raw[1:]
    if len(body) == 3:
        body = "".join(ch * 2 for ch in body)
    elif len(body) == 4:
        body = "".join(ch * 2 for ch in body[:3])
    elif len(body) == 8:
        body = body[:6]
    if len(body) != 6 or any(ch not in "0123456789ABCDEF" for ch in body):
        raise ValueError(f"不正な hex: {value}")
    return f"#{body}"


def load_tokens(tokens_path: Path) -> dict[str, Any]:
    """tokens.json を読む。"""
    return json.loads(tokens_path.read_text(encoding="utf-8"))


def default_tokens_path(site_dir: Path) -> Path:
    """サイトディレクトリから tokens.json を探す。"""
    direct = site_dir / "tokens.json"
    if direct.is_file():
        return direct
    sibling = site_dir.parent.parent / "references" / "tokens.json"
    if sibling.is_file():
        return sibling
    raise FileNotFoundError(f"tokens.json が見つからない: {site_dir}")


def allowed_hexes(tokens: dict[str, Any], palette_id: str) -> set[str]:
    """指定パレットで使ってよい公式 hex の集合を返す。

    いずれも color_code ページ（2026-07-17）に載る値。共有色・SolidGray ランプ・
    アクセントランプはページ内で全パレット横断に使われているため常に許可する。
    """
    shared = tokens["shared"]
    allowed = {_norm_hex(v) for v in shared.values() if isinstance(v, str)}
    for ramp in ("gray_ramp", "accent_ramp"):
        allowed.update(_norm_hex(v) for v in shared[ramp].values())
    palette = tokens["palettes"][palette_id]
    allowed.add(_norm_hex(palette["highlight"]))
    allowed.update(_norm_hex(v) for v in palette["chart"].values())
    allowed.update(_norm_hex(v) for v in palette["semantic"].values())
    for theme in THEMES:
        for value in palette["roles"][theme].values():
            values = value if isinstance(value, list) else [value]
            allowed.update(_norm_hex(v) for v in values)
    return allowed


def theme_roles(tokens: dict[str, Any], palette_id: str, theme: str) -> dict[str, str]:
    """テーマ既定値にパレット固有の役割を重ねて CSS 変数値を返す。"""
    base = tokens["themes"][theme]
    palette = tokens["palettes"][palette_id]
    role = palette["roles"][theme]
    series = role["series"]
    return {
        "--color-text": base["text"],
        "--color-text-muted": base["text_muted"],
        "--color-link": role["link"],
        "--color-page": base["page"],
        "--color-surface": role.get("surface", base["surface"]),
        "--color-control": role.get("control", base["control"]),
        "--color-highlight": palette["highlight"],
        "--color-highlight-text": base["highlight_text"],
        "--color-success": role["success"],
        "--color-error": role["error"],
        "--color-positive": role["positive"],
        "--color-negative": role["negative"],
        "--color-gridline": role.get("gridline", base["gridline"]),
        "--color-outline": role.get("outline", base["outline"]),
        "--color-focus": base["focus"],
        "--color-focus-edge": base["focus_edge"],
        "--chart-1": series[0],
        "--chart-2": series[1],
        "--chart-3": series[2],
        "--chart-4": series[3],
        "--chart-5": series[4],
    }


def relative_luminance(hex_color: str) -> float:
    """sRGB hex の相対輝度を返す。"""
    body = _norm_hex(hex_color)[1:]
    channels = [int(body[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]

    def to_linear(channel: float) -> float:
        """sRGB 成分を線形化する。"""
        if channel <= 0.04045:
            return channel / 12.92
        return ((channel + 0.055) / 1.055) ** 2.4

    red, green, blue = (to_linear(ch) for ch in channels)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(first: str, second: str) -> float:
    """2 色のコントラスト比を返す。"""
    lighter, darker = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def contrast_violations(roles: dict[str, str], label: str) -> list[str]:
    """1 テーマブロックの色役割が DADS の下限を満たすか検査する。"""
    violations: list[str] = []
    for foreground, background, minimum in CONTRAST_CONTRACT:
        ratio = contrast_ratio(roles[foreground], roles[background])
        if ratio < minimum:
            violations.append(
                f"{label} {foreground} と {background} のコントラストが {ratio:.2f}（下限 {minimum}）"
            )
    if roles["--color-highlight"] == roles["--color-surface"]:
        violations.append(f"{label} ハイライトがカード背景と同色で強調にならない")
    series = [roles[name] for name in CHART_VARS]
    if len(set(series)) != len(series):
        violations.append(f"{label} 系列色に重複がある")
    return violations


def render_css(tokens: dict[str, Any]) -> str:
    """tokens.json から styles.css 全文を生成する。"""
    layout = tokens["layout"]
    lines = [
        "/* 自動生成。正本は references/tokens.json。check_site.py --write-css で更新する。 */",
        ":root {",
        f'  --font-family: {layout["font_family"]};',
        f'  --mono-family: {layout["mono_family"]};',
        f'  --page-width: {layout["page_width_16x9"]}px;',
        f'  --page-height: {layout["page_height_16x9"]}px;',
        f'  --columns: {layout["columns"]};',
        f'  --gutter: {layout["gutter_px"]}px;',
        f'  --radius-button: {layout["radius_button_px"]}px;',
        f'  --radius-card: {layout["radius_card_px"]}px;',
        f'  --kpi-size: {layout["kpi_font_px"]}px;',
        f'  --title-size: {layout["title_font_px"]}px;',
        f'  --label-size: {layout["label_font_px"]}px;',
        f'  --axis-size: {layout["axis_font_px"]}px;',
        f'  --body-min: {layout["body_min_px"]}px;',
        "}",
        "",
        ':root[data-canvas="4x3"] {',
        f'  --page-width: {layout["page_width_4x3"]}px;',
        f'  --page-height: {layout["page_height_4x3"]}px;',
        "}",
        "",
        ':root[data-theme="light"] { color-scheme: light; }',
        ':root[data-theme="dark"] { color-scheme: dark; }',
        "",
    ]
    for palette_id in PALETTE_IDS:
        for theme in THEMES:
            roles = theme_roles(tokens, palette_id, theme)
            lines.append(f':root[data-theme="{theme}"][data-palette="{palette_id}"] {{')
            for name in REQUIRED_VARS:
                lines.append(f"  {name}: {roles[name]};")
            lines.append("}")
            lines.append("")
    lines.extend(_layout_css())
    return "\n".join(lines) + "\n"


def _layout_css() -> list[str]:
    """色以外のレイアウト CSS を返す。"""
    return [
        "*, *::before, *::after { box-sizing: border-box; }",
        "body {",
        "  margin: 0;",
        "  font-family: var(--font-family);",
        "  font-size: var(--body-min);",
        "  line-height: 1.5;",
        "  color: var(--color-text);",
        "  background: var(--color-page);",
        "}",
        "a { color: var(--color-link); }",
        "/* フォーカスリングは二重帯。外側の帯だけでページ・カード双方に 3:1 を満たす。 */",
        ":focus-visible {",
        "  outline: 2px solid var(--color-focus-edge);",
        "  outline-offset: 3px;",
        "  box-shadow: 0 0 0 3px var(--color-focus);",
        "}",
        ".skip-link {",
        "  position: absolute;",
        "  left: -999px;",
        "  top: 0;",
        "}",
        ".skip-link:focus { left: var(--gutter); background: var(--color-surface); padding: 8px; }",
        ".canvas {",
        "  max-width: var(--page-width);",
        "  margin: 0 auto;",
        "  padding: var(--gutter);",
        "}",
        ".site-header, .site-footer {",
        "  display: flex;",
        "  flex-wrap: wrap;",
        "  gap: var(--gutter);",
        "  align-items: center;",
        "  justify-content: space-between;",
        "}",
        ".site-header { margin-bottom: var(--gutter); }",
        ".site-title { font-size: var(--title-size); font-weight: 700; margin: 0; letter-spacing: 0.02em; }",
        ".toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }",
        ".theme-toggle, .palette-select, .filter {",
        "  background: var(--color-control);",
        "  border: 1px solid var(--color-outline);",
        "  border-radius: var(--radius-button);",
        "  color: var(--color-text);",
        "}",
        ".theme-toggle { display: inline-flex; padding: 2px; }",
        ".theme-toggle button {",
        "  border: 0;",
        "  background: transparent;",
        "  color: var(--color-text);",
        "  font: inherit;",
        "  font-size: var(--label-size);",
        "  padding: 8px 12px;",
        "  border-radius: var(--radius-button);",
        "  cursor: pointer;",
        "}",
        '.theme-toggle button[aria-pressed="true"] {',
        "  background: var(--color-highlight);",
        "  color: var(--color-highlight-text);",
        "}",
        ".palette-select, .filter {",
        "  font: inherit;",
        "  font-size: var(--label-size);",
        "  padding: 8px 12px;",
        "}",
        ".grid {",
        "  display: grid;",
        "  grid-template-columns: repeat(var(--columns), minmax(0, 1fr));",
        "  gap: var(--gutter);",
        "}",
        ".card {",
        "  background: var(--color-surface);",
        "  border: 1px solid var(--color-outline);",
        "  border-radius: var(--radius-card);",
        "  padding: var(--gutter);",
        "  min-width: 0;",
        "}",
        ".card--highlight {",
        "  background: var(--color-highlight);",
        "  color: var(--color-highlight-text);",
        "}",
        ".card--highlight .muted, .card--highlight .kpi-label { color: var(--color-highlight-text); }",
        ".span-2 { grid-column: span 2; }",
        ".span-3 { grid-column: span 3; }",
        ".span-4 { grid-column: span 4; }",
        ".span-6 { grid-column: span 6; }",
        ".kpi { font-size: var(--kpi-size); font-weight: 700; line-height: 1.2; margin: 0; }",
        ".kpi-label, .muted, figcaption, .chart-title {",
        "  color: var(--color-text-muted);",
        "  font-size: var(--label-size);",
        "  margin: 0 0 8px;",
        "}",
        ".card--highlight .chart-title { color: var(--color-highlight-text); }",
        ".delta-positive { color: var(--color-positive); }",
        ".delta-negative { color: var(--color-negative); }",
        "table { width: 100%; border-collapse: collapse; font-size: var(--label-size); }",
        "th { background: var(--color-control); text-align: left; font-size: var(--axis-size); }",
        "th, td { border: 1px solid var(--color-gridline); padding: 8px; }",
        "svg[role='img'] { width: 100%; height: auto; display: block; }",
        ".site-footer { margin-top: var(--gutter); font-size: var(--label-size); color: var(--color-text-muted); }",
        "@media (max-width: 800px) {",
        "  .span-2, .span-3, .span-4, .span-6 { grid-column: span 6; }",
        "  .grid { grid-template-columns: 1fr; }",
        "}",
    ]


def write_css(site_dir: Path, tokens: dict[str, Any]) -> Path:
    """styles.css を tokens から書き出す。"""
    path = site_dir / "styles.css"
    path.write_text(render_css(tokens), encoding="utf-8")
    return path


class _Dom(HTMLParser):
    """検査用にタグと属性を集める。"""

    def __init__(self) -> None:
        """パーサを初期化する。"""
        super().__init__()
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.text_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """開始タグを記録する。"""
        mapping = {key: (value or "") for key, value in attrs}
        self.tags.append((tag, mapping))

    def handle_data(self, data: str) -> None:
        """テキストノードを記録する。"""
        if data.strip():
            self.text_chunks.append(data.strip())


def _parse_css_blocks(css: str) -> dict[tuple[str, str], dict[str, str]]:
    """`:root[data-theme][data-palette]` ブロックの変数を取り出す。"""
    blocks: dict[tuple[str, str], dict[str, str]] = {}
    pattern = re.compile(
        r':root\[data-theme="(light|dark)"\]\[data-palette="([a-z-]+)"\]\s*\{([^}]+)\}',
        re.MULTILINE,
    )
    for match in pattern.finditer(css):
        theme, palette, body = match.group(1), match.group(2), match.group(3)
        variables: dict[str, str] = {}
        for line in body.split(";"):
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            variables[name.strip()] = value.strip()
        blocks[(theme, palette)] = variables
    return blocks


def _collect_hexes(text: str) -> set[str]:
    """テキスト中の hex を正規化して集める。"""
    found: set[str] = set()
    for match in HEX_RE.findall(text):
        try:
            found.add(_norm_hex(match))
        except ValueError:
            found.add(match.upper())
    return found


def validate_site(site_dir: Path, tokens: dict[str, Any] | None = None) -> list[str]:
    """サイトが公式テンプレート契約を満たすかを検査し、違反メッセージを返す。"""
    violations: list[str] = []
    html_path = site_dir / "index.html"
    css_path = site_dir / "styles.css"
    js_path = site_dir / "theme.js"
    if tokens is None:
        tokens = load_tokens(default_tokens_path(site_dir))
    if not html_path.is_file():
        return ["index.html が無い"]
    if not css_path.is_file():
        return ["styles.css が無い"]
    if not js_path.is_file():
        violations.append("theme.js が無い")
    html = html_path.read_text(encoding="utf-8")
    css = css_path.read_text(encoding="utf-8")
    js = js_path.read_text(encoding="utf-8") if js_path.is_file() else ""
    violations.extend(_validate_html(html))
    violations.extend(_validate_js(js))
    violations.extend(_validate_css(css, tokens))
    violations.extend(_validate_donts(html + css))
    violations.extend(_validate_hex_universe(html + css + js, tokens))
    return violations


def _validate_html(html: str) -> list[str]:
    """HTML の構造契約を検査する。"""
    violations: list[str] = []
    parser = _Dom()
    parser.feed(html)
    tags = parser.tags
    if not any(tag == "html" and attrs.get("lang", "").lower().startswith("ja") for tag, attrs in tags):
        violations.append("html[lang=ja] が無い")
    html_attrs = next((attrs for tag, attrs in tags if tag == "html"), {})
    if html_attrs.get("data-theme") not in THEMES:
        violations.append("html[data-theme=light|dark] が無い")
    if html_attrs.get("data-palette") not in PALETTE_IDS:
        violations.append("html[data-palette] が公式7色の識別子ではない")
    page_kind = html_attrs.get("data-page-kind", "dashboard")
    if page_kind not in PAGE_KINDS:
        violations.append("html[data-page-kind] は dashboard か content")
    if html_attrs.get("data-canvas", CANVASES[0]) not in CANVASES:
        violations.append("html[data-canvas] は 16x9 か 4x3")
    if not any(tag == "a" and "skip" in (attrs.get("class", "") + attrs.get("href", "")).lower() for tag, attrs in tags):
        violations.append("スキップリンクが無い")
    for landmark in ("header", "main", "footer"):
        if not any(tag == landmark for tag, _ in tags):
            violations.append(f"<{landmark}> が無い")
    theme_buttons = [
        attrs
        for tag, attrs in tags
        if tag == "button" and attrs.get("data-theme-value") in THEMES
    ]
    values = {attrs.get("data-theme-value") for attrs in theme_buttons}
    if values != set(THEMES):
        violations.append("ホワイトモードとダークモードを選べるトグル（button[data-theme-value]）が揃っていない")
    if not any(attrs.get("type") == "button" for attrs in theme_buttons):
        violations.append("テーマ切替ボタンに type=button が無い")
    if page_kind == "dashboard":
        if not any(
            "kpi" in attrs.get("class", "").split() or attrs.get("data-kpi") == "true" for _, attrs in tags
        ):
            violations.append("KPI（.kpi または data-kpi）が無い")
        svgs = [attrs for tag, attrs in tags if tag == "svg"]
        if not svgs:
            violations.append("チャート SVG が無い")
        if not any(attrs.get("role") == "img" for attrs in svgs):
            violations.append("svg[role=img] が無い（代替テキスト契約）")
        if 'data-origin="0"' not in html and "data-origin='0'" not in html:
            violations.append("棒グラフの原点 0（data-origin=0）が無い")
    if "Noto Sans JP" not in html and "Noto+Sans+JP" not in html and "fonts.googleapis.com" not in html:
        violations.append("Noto Sans JP の読み込みが無い")
    if not _theme_script_runs_before_body(tags):
        violations.append("theme.js が <head> で読み込まれていない（ダーク選択時に白のちらつきが出る）")
    return violations


def _theme_script_runs_before_body(tags: list[tuple[str, dict[str, str]]]) -> bool:
    """theme.js が body より前（head 内）で読み込まれているかを返す。"""
    for tag, attrs in tags:
        if tag == "body":
            return False
        if tag == "script" and "theme.js" in attrs.get("src", ""):
            return True
    return False


def _validate_js(js: str) -> list[str]:
    """テーマ切替スクリプトの契約を検査する。"""
    if not js.strip():
        return ["theme.js が空"]
    violations: list[str] = []
    if "data-theme-value" not in js and "theme-value" not in js:
        if "dataset.theme" not in js and "data-theme" not in js:
            violations.append("theme.js が data-theme を更新しない")
    if "localStorage" not in js:
        violations.append("theme.js が選択を localStorage に残さない")
    if "light" not in js or "dark" not in js:
        violations.append("theme.js が light/dark の両方を扱わない")
    if "aria-pressed" not in js:
        violations.append("theme.js が aria-pressed を更新しない")
    if "dataset.palette" not in js and "data-palette" not in js:
        violations.append("theme.js が html[data-palette] を初期値に使わない")
    if "defer" not in js and "DOMContentLoaded" not in js:
        violations.append("theme.js が DOM 構築前にリスナーを張ろうとしている")
    return violations


def _validate_donts(text: str) -> list[str]:
    """ガイドブックの Dont's（3D・ドロップシャドウ）を検査する。

    フォーカスリングは WCAG 上必要な指標なので、`var(--color-focus)` を使う
    box-shadow だけは装飾影と区別して許可する。
    """
    violations: list[str] = []
    if THREE_D_RE.search(text):
        violations.append("3D / perspective 表現はガイドブック Dont's")
    decorative = [
        match.group(1).strip()
        for match in BOX_SHADOW_RE.finditer(text)
        if "var(--color-focus" not in match.group(1)
    ]
    if decorative or DROP_SHADOW_RE.search(text):
        violations.append("ドロップシャドウはガイドブック Dont's（境界は outline で示す）")
    return violations


def _validate_css(css: str, tokens: dict[str, Any]) -> list[str]:
    """CSS 変数が公式ロール値と一致し、コントラスト契約を満たすかを検査する。"""
    violations: list[str] = []
    if "Noto Sans JP" not in css:
        violations.append("styles.css の font-family に Noto Sans JP が無い")
    layout = tokens["layout"]
    if "--page-width:" not in css or f'{layout["page_width_16x9"]}px' not in css:
        violations.append("16:9 幅 1280px が CSS に無い")
    if f'{layout["page_width_4x3"]}px' not in css:
        violations.append("4:3 幅 960px が CSS に無い")
    if "--kpi-size:" not in css:
        violations.append("KPI 36px が CSS に無い")
    if "--radius-button:" not in css:
        violations.append("ボタン半径 8px が CSS に無い")
    if "--radius-card:" not in css:
        violations.append("カード半径 12px が CSS に無い")
    if "color-scheme: dark" not in css or "color-scheme: light" not in css:
        violations.append("color-scheme が data-theme に追従していない（ネイティブ UI が反転しない）")
    if re.search(r"font-size:\s*(?:[0-9]|1[0-3])px", css):
        violations.append("14px 未満の font-size がある（ウェブでは DADS 下限）")
    blocks = _parse_css_blocks(css)
    for palette_id in PALETTE_IDS:
        for theme in THEMES:
            key = (theme, palette_id)
            label = f"{theme}/{palette_id}"
            if key not in blocks:
                violations.append(f"CSS に {label} ブロックが無い")
                continue
            expected = theme_roles(tokens, palette_id, theme)
            actual = blocks[key]
            for name in REQUIRED_VARS:
                got = actual.get(name, "").upper()
                want = expected[name].upper()
                if got != want:
                    violations.append(f"{label} の {name} が {got or '(欠落)'}（公式は {want}）")
            violations.extend(contrast_violations(expected, label))
    expected_prefix = render_css(tokens).rstrip()
    actual = css.rstrip()
    if actual != expected_prefix and not actual.startswith(expected_prefix + "\n"):
        violations.append(
            "styles.css 先頭が tokens.json からの生成結果と一致しない（check_site.py --write-css。追加ルールは末尾のみ可）"
        )
    return violations


def _validate_hex_universe(text: str, tokens: dict[str, Any]) -> list[str]:
    """出現した hex と名前付き色が、公式集合に含まれるかを検査する。"""
    universe: set[str] = set()
    for palette_id in PALETTE_IDS:
        universe |= allowed_hexes(tokens, palette_id)
    violations: list[str] = []
    for hex_color in sorted(_collect_hexes(text)):
        if hex_color not in universe:
            violations.append(f"公式に無い色 {hex_color}")
    for name in sorted({match.group(1).lower() for match in NAMED_COLOR_RE.finditer(text)}):
        if name not in NON_COLOR_KEYWORDS:
            violations.append(f"名前付き色 `{name}` は禁止（公式 hex を使う）")
    return violations


def build_parser() -> argparse.ArgumentParser:
    """CLI パーサを構築する。"""
    parser = argparse.ArgumentParser(description="デジタル庁ダッシュボードテンプレート準拠検査")
    parser.add_argument("site", nargs="?", default=".", help="検査するサイトディレクトリ")
    parser.add_argument("--write-css", action="store_true", help="tokens.json から styles.css を再生成する")
    return parser


def main(argv: list[str] | None = None) -> int:
    """エントリポイント。"""
    args = build_parser().parse_args(argv)
    site_dir = Path(args.site).resolve()
    try:
        tokens = load_tokens(default_tokens_path(site_dir))
    except FileNotFoundError as err:
        print(err, file=sys.stderr)
        return 2
    if args.write_css:
        path = write_css(site_dir, tokens)
        print(path)
        return 0
    violations = validate_site(site_dir, tokens)
    if not violations:
        print("PASS")
        return 0
    for item in violations:
        print(f"FAIL: {item}")
    print(f"{len(violations)} violation(s)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
