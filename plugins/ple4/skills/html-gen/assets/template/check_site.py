"""Material Design 3 準拠を機械検査し、色トークン CSS を生成する。

標準ライブラリのみ。役割は 2 つある。

1. **生成**: `references/tokens.json` の seed 色から M3 のトーナルパレットを
   計算し、`tokens.css`（配色・タイポ・シェイプ・エレベーション・モーション
   のシステムトークン）を書き出す。tone は CIE L* で、M3 の HCT と同じ定義。
   hue / chroma は LCh(ab) 近似で、実際の可読性は下の実測が担保する。
2. **検査**: 生成された `tokens.css` が tokens.json と一致すること、手書きの
   `styles.css` / `index.html` / `app.js` が契約（色は変数経由のみ・
   コントラスト・レスポンシブ・モーション低減・アクセシビリティ）を守ることを
   確認する。

    python3 check_site.py .              # 検査
    python3 check_site.py --write-css .  # tokens.css を再生成
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

HEX_RE = re.compile(r"#[0-9A-Fa-f]{3,8}\b")
#: 色を取るプロパティだけを名指しする。`(?:-\w+)*` で広く取ると
#: `stroke-linecap: butt` や `background-blend-mode: multiply` を色と誤認する。
NAMED_COLOR_RE = re.compile(
    r"(?<![-\w])"
    r"(?:color|fill|stroke|background|border|outline"
    r"|(?:background|border|outline|text-decoration|column-rule|stop|flood|caret|accent|text-emphasis)-color"
    r"|border-(?:top|right|bottom|left|block|inline)-color"
    r"|-webkit-text-fill-color)"
    r"\s*:\s*(?!var\(|#|-)([a-z][a-z0-9-]*)",
    re.IGNORECASE,
)
THEMES = ("light", "dark")
PAGE_KINDS = ("dashboard", "content")
SERIES_COUNT = 6
CHART_VARS = tuple(f"--chart-{index}" for index in range(1, SERIES_COUNT + 1))
CHART_CONTAINER_VARS = tuple(f"--chart-{index}-container" for index in range(1, SERIES_COUNT + 1))
#: hand-authored な CSS がテーマ非依存に持ってよい追加変数。
GENERATED_EXTRA_VARS = ("--md-sys-color-shadow-rgb",)
#: (前景変数, 背景変数, 最低比率)。WCAG 2.1: 文字 4.5:1 / 非文字 3:1。
CONTRAST_CONTRACT: tuple[tuple[str, str, float], ...] = (
    ("--md-sys-color-on-surface", "--md-sys-color-surface", 4.5),
    ("--md-sys-color-on-surface", "--md-sys-color-surface-container", 4.5),
    ("--md-sys-color-on-surface", "--md-sys-color-surface-container-high", 4.5),
    ("--md-sys-color-on-surface", "--md-sys-color-surface-container-highest", 4.5),
    ("--md-sys-color-on-surface-variant", "--md-sys-color-surface", 4.5),
    ("--md-sys-color-on-surface-variant", "--md-sys-color-surface-container", 4.5),
    ("--md-sys-color-on-surface-variant", "--md-sys-color-surface-container-high", 4.5),
    ("--md-sys-color-on-primary", "--md-sys-color-primary", 4.5),
    ("--md-sys-color-on-primary-container", "--md-sys-color-primary-container", 4.5),
    ("--md-sys-color-on-secondary", "--md-sys-color-secondary", 4.5),
    ("--md-sys-color-on-secondary-container", "--md-sys-color-secondary-container", 4.5),
    ("--md-sys-color-on-tertiary", "--md-sys-color-tertiary", 4.5),
    ("--md-sys-color-on-tertiary-container", "--md-sys-color-tertiary-container", 4.5),
    ("--md-sys-color-on-error", "--md-sys-color-error", 4.5),
    ("--md-sys-color-on-error-container", "--md-sys-color-error-container", 4.5),
    ("--md-sys-color-inverse-on-surface", "--md-sys-color-inverse-surface", 4.5),
    ("--md-sys-color-primary", "--md-sys-color-surface", 4.5),
    ("--md-sys-color-primary", "--md-sys-color-surface-container", 4.5),
    ("--md-sys-color-primary", "--md-sys-color-surface-container-high", 4.5),
    ("--md-sys-color-positive", "--md-sys-color-surface-container", 4.5),
    ("--md-sys-color-negative", "--md-sys-color-surface-container", 4.5),
    ("--md-sys-color-outline", "--md-sys-color-surface", 3.0),
    ("--md-sys-color-outline", "--md-sys-color-surface-container", 3.0),
    *((name, "--md-sys-color-surface", 3.0) for name in CHART_VARS),
    *((name, "--md-sys-color-surface-container", 3.0) for name in CHART_VARS),
    *((name, "--md-sys-color-surface-container-high", 3.0) for name in CHART_VARS),
)
#: 系列色どうしの最小知覚距離（CIE76 ΔE*ab）。色覚差でも隣が判別できる下限。
SERIES_MIN_DELTA_E = 20.0
#: 色ではない CSS キーワード。色プロパティの値に現れても違反にしない。
NON_COLOR_KEYWORDS = frozenset(
    {
        "transparent", "currentcolor", "inherit", "initial", "unset", "revert", "none",
        "auto", "solid", "dashed", "dotted", "double", "groove", "ridge", "inset",
        "outset", "hidden", "thin", "medium", "thick", "linear-gradient", "url",
        "radial-gradient", "conic-gradient", "color-mix", "rgb", "rgba", "hsl", "hsla",
        "var", "repeat", "no-repeat", "center", "cover", "contain", "content-box",
        "padding-box", "border-box", "collapse", "separate", "light", "dark",
        "normal", "only", "fixed", "scroll", "local", "space", "round", "clip", "text",
    }
)
#: 手書き CSS が参照していなければならない役割変数（設計の骨格）。
REQUIRED_STYLE_VARS = (
    "--md-sys-color-surface",
    "--md-sys-color-on-surface",
    "--md-sys-color-surface-container",
    "--md-sys-color-primary",
    "--md-sys-color-on-primary",
    "--md-sys-color-outline-variant",
    "--md-sys-elevation-level1",
    "--md-sys-shape-corner-large",
    "--md-sys-motion-easing-emphasized",
    "--md-sys-typescale-body-medium-font",
)
#: M3 のウィンドウクラス境界。全部を手書き CSS が扱っていること。
REQUIRED_BREAKPOINTS = (600, 840, 1200)


# --- 色空間 -----------------------------------------------------------------


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


def _to_linear(channel: float) -> float:
    """sRGB 成分を線形化する。"""
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def _to_srgb(channel: float) -> float:
    """線形成分を sRGB へ戻す。"""
    if channel <= 0.0031308:
        return channel * 12.92
    return 1.055 * (channel ** (1 / 2.4)) - 0.055


def _linear_rgb(hex_color: str) -> tuple[float, float, float]:
    """hex を線形 RGB へ変換する。"""
    body = _norm_hex(hex_color)[1:]
    red, green, blue = (_to_linear(int(body[i : i + 2], 16) / 255.0) for i in (0, 2, 4))
    return red, green, blue


_WHITE = (0.95047, 1.0, 1.08883)


def hex_to_lab(hex_color: str) -> tuple[float, float, float]:
    """hex を CIE Lab（D65）へ変換する。"""
    red, green, blue = _linear_rgb(hex_color)
    x = 0.4124564 * red + 0.3575761 * green + 0.1804375 * blue
    y = 0.2126729 * red + 0.7151522 * green + 0.0721750 * blue
    z = 0.0193339 * red + 0.1191920 * green + 0.9503041 * blue

    def f(value: float) -> float:
        """Lab の非線形圧縮。"""
        if value > 216 / 24389:
            return value ** (1 / 3)
        return (24389 / 27 * value + 16) / 116

    fx, fy, fz = (f(c / w) for c, w in zip((x, y, z), _WHITE, strict=True))
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def hex_to_lch(hex_color: str) -> tuple[float, float, float]:
    """hex を LCh(ab) へ変換する。h は度。"""
    lightness, a_axis, b_axis = hex_to_lab(hex_color)
    chroma = math.hypot(a_axis, b_axis)
    hue = math.degrees(math.atan2(b_axis, a_axis)) % 360
    return lightness, chroma, hue


def _lab_to_linear_rgb(lightness: float, a_axis: float, b_axis: float) -> tuple[float, float, float]:
    """Lab を線形 RGB へ変換する（範囲外もそのまま返す）。"""
    fy = (lightness + 16) / 116
    fx = fy + a_axis / 500
    fz = fy - b_axis / 200

    def finv(value: float) -> float:
        """Lab 非線形圧縮の逆変換。"""
        cube = value**3
        if cube > 216 / 24389:
            return cube
        return (116 * value - 16) / (24389 / 27)

    x, y, z = (finv(v) * w for v, w in zip((fx, fy, fz), _WHITE, strict=True))
    red = 3.2404542 * x - 1.5371385 * y - 0.4985314 * z
    green = -0.9692660 * x + 1.8760108 * y + 0.0415560 * z
    blue = 0.0556434 * x - 0.2040259 * y + 1.0572252 * z
    return red, green, blue


def _in_gamut(channels: tuple[float, float, float]) -> bool:
    """線形 RGB が sRGB 域内かを返す。"""
    return all(-1e-4 <= channel <= 1 + 1e-4 for channel in channels)


def lch_to_hex(lightness: float, chroma: float, hue: float) -> str:
    """LCh(ab) を sRGB 域へ収めた hex にする。

    tone（= L*）は保ち、域外なら chroma だけを二分探索で落とす。M3 の HCT が
    tone を固定して chroma を下げるのと同じ振る舞いで、これによりトーンが担う
    コントラストの保証が壊れない。
    """
    lightness = min(100.0, max(0.0, lightness))
    if lightness <= 0.0:
        return "#000000"
    if lightness >= 100.0:
        return "#FFFFFF"
    radians = math.radians(hue)

    def at(chroma_value: float) -> tuple[float, float, float]:
        """指定 chroma の線形 RGB を返す。"""
        return _lab_to_linear_rgb(lightness, chroma_value * math.cos(radians), chroma_value * math.sin(radians))

    if not _in_gamut(at(chroma)):
        low, high = 0.0, chroma
        for _ in range(24):
            mid = (low + high) / 2
            if _in_gamut(at(mid)):
                low = mid
            else:
                high = mid
        chroma = low
    channels = at(chroma)
    body = "".join(f"{round(min(1.0, max(0.0, _to_srgb(channel))) * 255):02X}" for channel in channels)
    return f"#{body}"


def relative_luminance(hex_color: str) -> float:
    """sRGB hex の相対輝度を返す。"""
    red, green, blue = _linear_rgb(hex_color)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(first: str, second: str) -> float:
    """2 色のコントラスト比を返す。"""
    lighter, darker = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def delta_e(first: str, second: str) -> float:
    """CIE76 の色差 ΔE*ab を返す。"""
    return math.dist(hex_to_lab(first), hex_to_lab(second))


# --- トーナルパレットとスキーム ---------------------------------------------


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


def seed_ids(tokens: dict[str, Any]) -> tuple[str, ...]:
    """定義済み seed 識別子を宣言順で返す。"""
    return tuple(tokens["seeds"])


def tonal_palettes(tokens: dict[str, Any], seed_id: str) -> dict[str, dict[int, str]]:
    """seed から M3 の各トーナルパレットを組み立てる。

    tone をキーにした hex を返す。primary は seed の彩度と 48 の高い方を採り、
    seed が淡くても主色が沈まないようにする（M3 の chroma floor と同じ意図）。
    """
    _, seed_chroma, seed_hue = hex_to_lch(tokens["seeds"][seed_id]["hex"])
    specs: dict[str, dict[str, Any]] = {**tokens["palette_spec"], **tokens["extra_palettes"]}
    palettes: dict[str, dict[int, str]] = {}
    for name, spec in specs.items():
        hue = spec["hue"] if "hue" in spec else (seed_hue + spec.get("hue_shift", 0)) % 360
        chroma = float(spec["chroma"])
        if spec.get("chroma_floor"):
            chroma = max(chroma, seed_chroma)
        palettes[name] = {tone: lch_to_hex(float(tone), chroma, hue) for tone in _needed_tones(tokens)}
    return palettes


def _needed_tones(tokens: dict[str, Any]) -> tuple[int, ...]:
    """roles と series が要求する tone の集合を返す。"""
    tones = {int(role[theme]) for role in tokens["roles"].values() for theme in THEMES}
    series = tokens["series"]
    tones.update(
        {
            int(series["light_tone"]),
            int(series["dark_tone"]),
            int(series["container_light_tone"]),
            int(series["container_dark_tone"]),
        }
    )
    return tuple(sorted(tones))


def series_colors(tokens: dict[str, Any], seed_id: str, theme: str) -> dict[str, str]:
    """seed の色相を回して、判別可能な系列色とその容器色を返す。"""
    _, _, seed_hue = hex_to_lch(tokens["seeds"][seed_id]["hex"])
    series = tokens["series"]
    tone = series["light_tone"] if theme == "light" else series["dark_tone"]
    container_tone = series["container_light_tone"] if theme == "light" else series["container_dark_tone"]
    chroma = float(series["chroma"])
    colors: dict[str, str] = {}
    for index, offset in enumerate(series["hue_offsets"][: series["count"]], start=1):
        hue = (seed_hue + offset) % 360
        colors[f"--chart-{index}"] = lch_to_hex(float(tone), chroma, hue)
        colors[f"--chart-{index}-container"] = lch_to_hex(float(container_tone), chroma, hue)
    return colors


def scheme(tokens: dict[str, Any], seed_id: str, theme: str) -> dict[str, str]:
    """1 つの seed / テーマについて、全 CSS 色変数を解決する。"""
    palettes = tonal_palettes(tokens, seed_id)
    resolved = {
        name: palettes[role["palette"]][int(role[theme])] for name, role in tokens["roles"].items()
    }
    resolved["--md-sys-color-surface-tint"] = resolved["--md-sys-color-primary"]
    shadow = _norm_hex(resolved["--md-sys-color-shadow"])[1:]
    resolved["--md-sys-color-shadow-rgb"] = " ".join(str(int(shadow[i : i + 2], 16)) for i in (0, 2, 4))
    resolved.update(series_colors(tokens, seed_id, theme))
    return resolved


def color_var_names(tokens: dict[str, Any]) -> tuple[str, ...]:
    """生成 CSS が各ブロックへ書き出す変数名を順序付きで返す。"""
    return (
        *tokens["roles"],
        "--md-sys-color-surface-tint",
        *GENERATED_EXTRA_VARS,
        *CHART_VARS,
        *CHART_CONTAINER_VARS,
    )


def contrast_violations(roles: dict[str, str], label: str) -> list[str]:
    """1 ブロックの配色が可読性契約を満たすか検査する。"""
    violations: list[str] = []
    for foreground, background, minimum in CONTRAST_CONTRACT:
        ratio = contrast_ratio(roles[foreground], roles[background])
        if ratio < minimum:
            violations.append(
                f"{label} {foreground} と {background} のコントラストが {ratio:.2f}（下限 {minimum}）"
            )
    series = [roles[name] for name in CHART_VARS]
    if len(set(series)) != len(series):
        violations.append(f"{label} 系列色に重複がある")
    for left in range(len(series)):
        for right in range(left + 1, len(series)):
            distance = delta_e(series[left], series[right])
            if distance < SERIES_MIN_DELTA_E:
                violations.append(
                    f"{label} --chart-{left + 1} と --chart-{right + 1} の色差が "
                    f"{distance:.1f}（下限 {SERIES_MIN_DELTA_E}）"
                )
    return violations


# --- tokens.css の生成 -------------------------------------------------------


def render_tokens_css(tokens: dict[str, Any]) -> str:
    """tokens.json から tokens.css 全文を生成する。"""
    lines = [
        "/* 自動生成。正本は tokens.json。`python3 check_site.py --write-css .` で更新する。 */",
        "/* Material Design 3 システムトークン: 配色 / タイポ / シェイプ / エレベーション / モーション */",
        "",
        ":root {",
        *_typography_lines(tokens),
        "",
        *_shape_lines(tokens),
        "",
        *_elevation_lines(tokens),
        "",
        *_motion_lines(tokens),
        "",
        *_state_lines(tokens),
        "",
        *_layout_lines(tokens),
        "",
        "  /* seed 選択 UI 用。テーマに依らず seed そのものを見せる。 */",
        *(
            f"  --seed-{seed_id}: {tonal_palettes(tokens, seed_id)['primary'][40]};"
            for seed_id in seed_ids(tokens)
        ),
        "}",
        "",
        ':root[data-theme="light"] { color-scheme: light; }',
        ':root[data-theme="dark"] { color-scheme: dark; }',
        "",
    ]
    names = color_var_names(tokens)
    for seed_id in seed_ids(tokens):
        for theme in THEMES:
            resolved = scheme(tokens, seed_id, theme)
            lines.append(f':root[data-theme="{theme}"][data-seed="{seed_id}"] {{')
            lines.extend(f"  {name}: {resolved[name]};" for name in names)
            lines.append("}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _typography_lines(tokens: dict[str, Any]) -> list[str]:
    """タイプスケール変数を返す。"""
    typography = tokens["typography"]
    lines = [
        f'  --md-sys-typescale-brand-font: {typography["brand_family"]};',
        f'  --md-sys-typescale-plain-font: {typography["plain_family"]};',
        f'  --md-sys-typescale-mono-font: {typography["mono_family"]};',
    ]
    for name, spec in typography["scale"].items():
        family = "brand" if name.startswith(("display", "headline")) else "plain"
        lines.append(
            f'  --md-sys-typescale-{name}-font: {spec["weight"]} {spec["size"]}px/{spec["line"]}px '
            f"var(--md-sys-typescale-{family}-font);"
        )
        lines.append(f'  --md-sys-typescale-{name}-tracking: {spec["tracking"]}px;')
    return lines


def _shape_lines(tokens: dict[str, Any]) -> list[str]:
    """コーナー半径変数を返す。"""
    return [f"  --md-sys-shape-corner-{name}: {value}px;" for name, value in tokens["shape"].items()]


def _elevation_lines(tokens: dict[str, Any]) -> list[str]:
    """エレベーション（影）変数を返す。"""
    return [f"  --md-sys-elevation-{name}: {value};" for name, value in tokens["elevation"].items()]


def _motion_lines(tokens: dict[str, Any]) -> list[str]:
    """モーション（イージング / 時間）変数を返す。"""
    motion = tokens["motion"]
    return [
        *(f"  --md-sys-motion-easing-{name}: {value};" for name, value in motion["easing"].items()),
        *(f"  --md-sys-motion-duration-{name}: {value}ms;" for name, value in motion["duration"].items()),
    ]


def _state_lines(tokens: dict[str, Any]) -> list[str]:
    """ステートレイヤー不透明度の変数を返す。"""
    return [f"  --md-sys-state-{name}-opacity: {value};" for name, value in tokens["state"].items()]


def _layout_lines(tokens: dict[str, Any]) -> list[str]:
    """レイアウト（ブレークポイント / ガター）変数を返す。"""
    layout = tokens["layout"]
    return [f'  --md-ref-layout-{name.replace("_", "-")}: {value}px;' for name, value in layout.items()]


def write_tokens_css(site_dir: Path, tokens: dict[str, Any]) -> Path:
    """tokens.css を書き出す。"""
    path = site_dir / "tokens.css"
    path.write_text(render_tokens_css(tokens), encoding="utf-8")
    return path


# --- HTML 解析 ---------------------------------------------------------------


class _Dom(HTMLParser):
    """検査用にタグと属性を集める。"""

    def __init__(self) -> None:
        """パーサを初期化する。"""
        super().__init__()
        self.tags: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """開始タグを記録する。"""
        self.tags.append((tag, {key: (value or "") for key, value in attrs}))


# --- 検査 --------------------------------------------------------------------


def validate_site(site_dir: Path, tokens: dict[str, Any] | None = None) -> list[str]:
    """サイトが Material 3 契約を満たすかを検査し、違反メッセージを返す。"""
    if tokens is None:
        tokens = load_tokens(default_tokens_path(site_dir))
    paths = {name: site_dir / name for name in ("index.html", "styles.css", "tokens.css", "app.js")}
    missing = [name for name, path in paths.items() if not path.is_file()]
    if missing:
        return [f"{name} が無い" for name in missing]
    html = paths["index.html"].read_text(encoding="utf-8")
    styles = paths["styles.css"].read_text(encoding="utf-8")
    generated = paths["tokens.css"].read_text(encoding="utf-8")
    script = paths["app.js"].read_text(encoding="utf-8")
    violations = _validate_html(html, tokens)
    violations.extend(_validate_generated_css(generated, tokens))
    violations.extend(_validate_styles(styles))
    violations.extend(_validate_js(script))
    violations.extend(_validate_authored_colors(html + styles + script))
    return violations


def _validate_html(html: str, tokens: dict[str, Any]) -> list[str]:
    """HTML の構造・アクセシビリティ契約を検査する。"""
    violations: list[str] = []
    parser = _Dom()
    parser.feed(html)
    tags = parser.tags
    html_attrs = next((attrs for tag, attrs in tags if tag == "html"), {})
    if not html_attrs.get("lang", "").strip():
        violations.append("html[lang] が無い")
    if html_attrs.get("data-theme") not in THEMES:
        violations.append("html[data-theme=light|dark] が無い")
    if html_attrs.get("data-seed") not in seed_ids(tokens):
        violations.append("html[data-seed] が tokens.json の seed 識別子ではない")
    page_kind = html_attrs.get("data-page-kind", "dashboard")
    if page_kind not in PAGE_KINDS:
        violations.append("html[data-page-kind] は dashboard か content")
    if not any(
        tag == "meta" and attrs.get("name") == "viewport" and "width=device-width" in attrs.get("content", "")
        for tag, attrs in tags
    ):
        violations.append("viewport meta が無い（レスポンシブの前提）")
    if not any(
        tag == "a" and "skip" in (attrs.get("class", "") + attrs.get("href", "")).lower() for tag, attrs in tags
    ):
        violations.append("スキップリンクが無い")
    for landmark in ("header", "main", "footer"):
        if not any(tag == landmark for tag, _ in tags):
            violations.append(f"<{landmark}> が無い")
    violations.extend(_validate_theme_toggle(tags))
    violations.extend(_validate_stylesheets(tags))
    if page_kind == "dashboard":
        violations.extend(_validate_dashboard(html, tags))
    if not _script_runs_before_body(tags):
        violations.append("app.js が <head> で読み込まれていない（ダーク選択時に白のちらつきが出る）")
    return violations


def _validate_theme_toggle(tags: list[tuple[str, dict[str, str]]]) -> list[str]:
    """ライト / ダークを選べるトグルの契約を検査する。"""
    violations: list[str] = []
    buttons = [attrs for tag, attrs in tags if tag == "button" and attrs.get("data-theme-value") in THEMES]
    if {attrs.get("data-theme-value") for attrs in buttons} != set(THEMES):
        violations.append("ライトとダークを選べるトグル（button[data-theme-value]）が揃っていない")
    if not all(attrs.get("type") == "button" for attrs in buttons):
        violations.append("テーマ切替ボタンに type=button が無い")
    if not all("aria-pressed" in attrs for attrs in buttons):
        violations.append("テーマ切替ボタンに aria-pressed が無い")
    return violations


def _validate_stylesheets(tags: list[tuple[str, dict[str, str]]]) -> list[str]:
    """tokens.css と styles.css がこの順で読み込まれているかを検査する。"""
    hrefs = [attrs.get("href", "") for tag, attrs in tags if tag == "link" and attrs.get("rel") == "stylesheet"]
    local = [href for href in hrefs if href in ("tokens.css", "styles.css")]
    if local != ["tokens.css", "styles.css"]:
        return ["tokens.css → styles.css の順で読み込まれていない（手書き側が上書きできない）"]
    return []


def _validate_dashboard(html: str, tags: list[tuple[str, dict[str, str]]]) -> list[str]:
    """ダッシュボードのデータ表現契約を検査する。"""
    violations: list[str] = []
    if not any("metric" in attrs.get("class", "").split() or attrs.get("data-metric") == "true" for _, attrs in tags):
        violations.append("主要指標（.metric または data-metric）が無い")
    svgs = [attrs for tag, attrs in tags if tag == "svg"]
    if not svgs:
        violations.append("チャート SVG が無い")
    elif not any(attrs.get("role") == "img" for attrs in svgs):
        violations.append("svg[role=img] が無い（代替テキスト契約）")
    if "<desc" not in html:
        violations.append("SVG に <desc> が無い（図の要点を文字でも出す）")
    if 'data-origin="0"' not in html and "data-origin='0'" not in html:
        violations.append("棒グラフの原点 0（data-origin=0）が無い")
    if "<table" not in html:
        violations.append("表が無い（図を読めない利用者への同等手段）")
    return violations


def _script_runs_before_body(tags: list[tuple[str, dict[str, str]]]) -> bool:
    """app.js が body より前（head 内）で読み込まれているかを返す。"""
    for tag, attrs in tags:
        if tag == "body":
            return False
        if tag == "script" and "app.js" in attrs.get("src", ""):
            return True
    return False


def _validate_generated_css(css: str, tokens: dict[str, Any]) -> list[str]:
    """tokens.css が tokens.json からの生成結果と一致するかを検査する。"""
    violations: list[str] = []
    if css != render_tokens_css(tokens):
        violations.append("tokens.css が tokens.json からの生成結果と一致しない（--write-css で再生成する）")
    for seed_id in seed_ids(tokens):
        for theme in THEMES:
            violations.extend(contrast_violations(scheme(tokens, seed_id, theme), f"{theme}/{seed_id}"))
    return violations


def _validate_styles(css: str) -> list[str]:
    """手書き CSS のレスポンシブ・モーション・トークン利用契約を検査する。"""
    violations: list[str] = []
    for name in REQUIRED_STYLE_VARS:
        if f"var({name}" not in css:
            violations.append(f"styles.css が {name} を使っていない")
    for width in REQUIRED_BREAKPOINTS:
        if f"{width}px" not in css:
            violations.append(f"styles.css に {width}px のブレークポイントが無い")
    if "minmax(" not in css:
        violations.append("styles.css に伸縮するグリッド（minmax）が無い")
    if "clamp(" not in css:
        violations.append("styles.css に流体タイポ（clamp）が無い")
    if re.search(r"font-size:\s*(?:[0-9]|1[0-1])px", css):
        violations.append("12px 未満の font-size がある")
    violations.extend(_validate_reduced_motion(css))
    violations.extend(_validate_fluid_width(css))
    return violations


def _validate_reduced_motion(css: str) -> list[str]:
    """モーション低減設定への追従を検査する。"""
    match = re.search(
        r"@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)\s*\{(.*?)\n\}", css, re.DOTALL
    )
    if match is None:
        return ["@media (prefers-reduced-motion: reduce) のブロックが無い"]
    body = match.group(1)
    violations: list[str] = []
    if "animation" not in body:
        violations.append("モーション低減ブロックが animation を止めていない")
    if "transition" not in body:
        violations.append("モーション低減ブロックが transition を止めていない")
    return violations


def _validate_fluid_width(css: str) -> list[str]:
    """固定幅キャンバスが残っていないかを検査する。"""
    violations: list[str] = []
    for match in re.finditer(r"(?<![-\w(])width:\s*(\d{3,})px", css):
        if int(match.group(1)) >= REQUIRED_BREAKPOINTS[0]:
            violations.append(f"固定幅 {match.group(1)}px がある（表示領域を使い切れない）")
    for match in re.finditer(r"(?<![(\w])max-width:\s*(\d{3,})px", css):
        if int(match.group(1)) < 1600:
            violations.append(f"max-width {match.group(1)}px が広い画面を捨てている")
    return violations


def _validate_js(script: str) -> list[str]:
    """アプリスクリプトの契約を検査する。"""
    if not script.strip():
        return ["app.js が空"]
    #: (表示名, いずれか 1 つ現れればよい語) の並び。
    any_of = (
        ("data-theme", ("dataset.theme", "data-theme")),
        ("localStorage", ("localStorage",)),
        ("aria-pressed", ("aria-pressed",)),
        ("data-seed", ("dataset.seed", "data-seed")),
        ("prefers-reduced-motion", ("prefers-reduced-motion",)),
        ("DOM 構築前の実行順", ("defer", "DOMContentLoaded")),
    )
    violations = [
        f"app.js が {label} を扱っていない"
        for label, needles in any_of
        if not any(needle in script for needle in needles)
    ]
    if "light" not in script or "dark" not in script:
        violations.append("app.js が light/dark を扱っていない")
    return violations


def _validate_authored_colors(text: str) -> list[str]:
    """手書きファイルに生の色が書かれていないかを検査する。

    色の正本は tokens.json だけ。HTML / styles.css / app.js の色は必ず
    `var(--md-sys-color-*)` を通す。ここを緩めるとテーマ切替が片側だけ壊れる。
    """
    violations: list[str] = []
    for hex_color in sorted({match.upper() for match in HEX_RE.findall(text)}):
        violations.append(f"手書きファイルに生の hex {hex_color} がある（tokens.json 経由で参照する）")
    for name in sorted({match.group(1).lower() for match in NAMED_COLOR_RE.finditer(text)}):
        if name not in NON_COLOR_KEYWORDS:
            violations.append(f"名前付き色 `{name}` は禁止（var(--md-sys-color-*) を使う）")
    return violations


# --- CLI ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """CLI パーサを構築する。"""
    parser = argparse.ArgumentParser(description="Material Design 3 準拠検査 / トークン生成")
    parser.add_argument("site", nargs="?", default=".", help="検査するサイトディレクトリ")
    parser.add_argument("--write-css", action="store_true", help="tokens.json から tokens.css を再生成する")
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
        print(write_tokens_css(site_dir, tokens))
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
