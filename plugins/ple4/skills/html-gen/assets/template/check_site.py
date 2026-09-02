"""shadcn/ui のデザイントークン契約を機械検査し、色トークン CSS を生成する。

標準ライブラリのみ。役割は 2 つある。

1. **生成**: `references/tokens.json` が持つ shadcn/ui レジストリの逐語コピーから
   `tokens.css`（配色・タイポ・角丸・影・モーションのトークン）を書き出す。
   upstream をそのまま並べるのが基本で、実測で可読性契約に届かない
   `ring` / `muted-foreground` / `chart-*` だけを規則に沿って調整する。
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
#: 手書きファイルで禁じる色関数。`color-mix` と `var` だけが許される。
COLOR_FUNCTION_RE = re.compile(r"(?<![-\w])(oklch|oklab|lch|lab|hsl|hsla|hwb|rgb|rgba|color)\s*\(", re.IGNORECASE)
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
#: `oklch(L C H)` と `oklch(L C H / A%)` の両方を読む。
OKLCH_RE = re.compile(
    r"oklch\(\s*([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s*(?:/\s*([0-9.]+)%\s*)?\)", re.IGNORECASE
)
THEMES = ("light", "dark")
PAGE_KINDS = ("dashboard", "content")
#: shadcn/ui が配布する系列色の本数。upstream と同数に揃える。
SERIES_COUNT = 5
CHART_VARS = tuple(f"--chart-{index}" for index in range(1, SERIES_COUNT + 1))
#: (前景変数, 背景変数, 最低比率)。WCAG 2.1: 文字 4.5:1 / 非文字 3:1。
#: border 系は `decorative_roles` として別枠（tokens.json に理由がある）。
CONTRAST_CONTRACT: tuple[tuple[str, str, float], ...] = (
    ("--foreground", "--background", 4.5),
    ("--foreground", "--card", 4.5),
    ("--foreground", "--muted", 4.5),
    ("--foreground", "--accent", 4.5),
    ("--card-foreground", "--card", 4.5),
    ("--popover-foreground", "--popover", 4.5),
    ("--primary-foreground", "--primary", 4.5),
    ("--secondary-foreground", "--secondary", 4.5),
    ("--accent-foreground", "--accent", 4.5),
    ("--muted-foreground", "--muted", 4.5),
    ("--muted-foreground", "--background", 4.5),
    ("--muted-foreground", "--card", 4.5),
    ("--muted-foreground", "--sidebar", 4.5),
    ("--destructive-foreground", "--destructive", 4.5),
    ("--destructive", "--background", 4.5),
    ("--destructive", "--card", 4.5),
    ("--primary", "--background", 4.5),
    ("--primary", "--card", 4.5),
    ("--sidebar-foreground", "--sidebar", 4.5),
    ("--sidebar-accent-foreground", "--sidebar-accent", 4.5),
    ("--sidebar-primary-foreground", "--sidebar-primary", 4.5),
    ("--ring", "--background", 3.0),
    ("--ring", "--card", 3.0),
    ("--sidebar-ring", "--sidebar", 3.0),
    *((name, "--background", 3.0) for name in CHART_VARS),
    *((name, "--card", 3.0) for name in CHART_VARS),
    *((name, "--muted", 3.0) for name in CHART_VARS),
)
#: 色ではない CSS キーワード。色プロパティの値に現れても違反にしない。
NON_COLOR_KEYWORDS = frozenset(
    {
        "transparent", "currentcolor", "inherit", "initial", "unset", "revert", "none",
        "auto", "solid", "dashed", "dotted", "double", "groove", "ridge", "inset",
        "outset", "hidden", "thin", "medium", "thick", "linear-gradient", "url",
        "radial-gradient", "conic-gradient", "color-mix", "var", "repeat", "no-repeat",
        "center", "cover", "contain", "content-box", "padding-box", "border-box",
        "collapse", "separate", "light", "dark", "normal", "only", "fixed", "scroll",
        "local", "space", "round", "clip", "text",
    }
)
#: 手書き CSS が参照していなければならない役割変数（設計の骨格）。
REQUIRED_STYLE_VARS = (
    "--background",
    "--foreground",
    "--card",
    "--card-foreground",
    "--muted-foreground",
    "--primary",
    "--primary-foreground",
    "--border",
    "--ring",
    "--radius",
    "--shadow-sm",
    "--font-sans",
    "--ease-out",
)
#: Tailwind のブレークポイント。全部を手書き CSS が扱っていること。
REQUIRED_BREAKPOINTS = (768, 1024, 1280)


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


def oklch_to_linear_rgb(lightness: float, chroma: float, hue: float) -> tuple[float, float, float]:
    """OKLCh を線形 RGB へ変換する（範囲外もそのまま返す）。

    OKLab の逆変換（Björn Ottosson）。CSS の `oklch()` と同じ定義なので、
    ここで得た値はブラウザが描く色と一致する。
    """
    radians = math.radians(hue)
    a_axis = chroma * math.cos(radians)
    b_axis = chroma * math.sin(radians)
    long_ = (lightness + 0.3963377774 * a_axis + 0.2158037573 * b_axis) ** 3
    medium = (lightness - 0.1055613458 * a_axis - 0.0638541728 * b_axis) ** 3
    short = (lightness - 0.0894841775 * a_axis - 1.2914855480 * b_axis) ** 3
    return (
        4.0767416621 * long_ - 3.3077115913 * medium + 0.2309699292 * short,
        -1.2684380046 * long_ + 2.6097574011 * medium - 0.3413193965 * short,
        -0.0041960863 * long_ - 0.7034186147 * medium + 1.7076147010 * short,
    )


def _in_gamut(channels: tuple[float, float, float]) -> bool:
    """線形 RGB が sRGB 域内かを返す。"""
    return all(-1e-4 <= channel <= 1 + 1e-4 for channel in channels)


def fit_chroma(lightness: float, chroma: float, hue: float) -> float:
    """sRGB 域に収まる最大の chroma を返す。明度は動かさない。

    明度を動かすとコントラスト契約が崩れるので、域外は彩度だけを落とす。
    """
    if _in_gamut(oklch_to_linear_rgb(lightness, chroma, hue)):
        return chroma
    low, high = 0.0, chroma
    for _ in range(32):
        mid = (low + high) / 2
        if _in_gamut(oklch_to_linear_rgb(lightness, mid, hue)):
            low = mid
        else:
            high = mid
    return low


def parse_oklch(value: str) -> tuple[float, float, float, float]:
    """`oklch(...)` 文字列を (L, C, H, alpha) に分解する。"""
    match = OKLCH_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"oklch() ではない: {value}")
    alpha = float(match.group(4)) / 100 if match.group(4) else 1.0
    return float(match.group(1)), float(match.group(2)), float(match.group(3)), alpha


def format_oklch(lightness: float, chroma: float, hue: float) -> str:
    """CSS へ書き出す `oklch()` 表記を返す。"""
    return f"oklch({lightness:g} {chroma:.4g} {hue:g})"


def oklch_to_hex(value: str, backdrop: str | None = None) -> str:
    """`oklch()` を sRGB の hex にする。半透明なら backdrop へ合成する。

    upstream のダークは border / input を `oklch(1 0 0 / 10%)` のように半透明で
    置く。実際に見える色は下地との合成結果なので、検査もその色で行う。
    """
    lightness, chroma, hue, alpha = parse_oklch(value)
    channels = oklch_to_linear_rgb(lightness, chroma, hue)
    if alpha < 1.0:
        if backdrop is None:
            raise ValueError(f"半透明色の合成先が無い: {value}")
        base = oklch_to_linear_rgb(*parse_oklch(backdrop)[:3])
        channels = tuple(alpha * top + (1 - alpha) * under for top, under in zip(channels, base, strict=True))
    body = "".join(f"{round(min(1.0, max(0.0, _to_srgb(min(1.0, max(0.0, channel))))) * 255):02X}" for channel in channels)
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


# --- スキームの解決 ----------------------------------------------------------


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


def base_ids(tokens: dict[str, Any]) -> tuple[str, ...]:
    """定義済みのベースカラー識別子を宣言順で返す。"""
    return tuple(tokens["bases"])


def _lower_lightness_until(
    value: str, backdrop_hex: str, minimum: float
) -> str:
    """色相と彩度を保ったまま明度だけ下げ、比率を満たす最大の明度を返す。

    upstream の値をできる限り残すため、条件を満たした時点で止める。
    """
    lightness, chroma, hue, _ = parse_oklch(value)
    while lightness > 0.0:
        fitted = fit_chroma(lightness, chroma, hue)
        if contrast_ratio(oklch_to_hex(format_oklch(lightness, fitted, hue)), backdrop_hex) >= minimum:
            return format_oklch(lightness, fitted, hue)
        lightness = round(lightness - 0.002, 3)
    raise ValueError(f"{value} は {minimum}:1 を満たせない")


def series_colors(tokens: dict[str, Any], theme: str) -> dict[str, str]:
    """テーマ共通の色相で、明度だけテーマに合わせた系列色を返す。

    ライトとダークで色相が入れ替わらないので、テーマを切り替えても
    同じ系列が同じ色のままになる。
    """
    series = tokens["series"]
    lightness = series["light_lightness"] if theme == "light" else series["dark_lightness"]
    colors: dict[str, str] = {}
    for index, (hue, chroma) in enumerate(
        zip(series["hues"][: series["count"]], series["chromas"][: series["count"]], strict=True), start=1
    ):
        colors[f"--chart-{index}"] = format_oklch(lightness, fit_chroma(lightness, chroma, hue), hue)
    return colors


def scheme(tokens: dict[str, Any], base_id: str, theme: str) -> dict[str, str]:
    """1 つのベースカラー / テーマについて、全 CSS 色変数を解決する。"""
    resolved = {f"--{name}": value for name, value in tokens["bases"][base_id][theme].items()}
    for fix in tokens["contrast_fixes"]:
        name = f'--{fix["var"]}'
        backdrop = resolved[f'--{fix["against"]}']
        current = oklch_to_hex(resolved[name], backdrop)
        if contrast_ratio(current, oklch_to_hex(backdrop)) < fix["min_ratio"]:
            resolved[name] = _lower_lightness_until(resolved[name], oklch_to_hex(backdrop), fix["min_ratio"])
    for name, spec in tokens["derived_roles"].items():
        resolved[f"--{name}"] = spec[theme]
    resolved.update(series_colors(tokens, theme))
    return resolved


def resolved_hex(tokens: dict[str, Any], base_id: str, theme: str) -> dict[str, str]:
    """スキームを、半透明を下地へ合成した実効 hex に落とす。"""
    raw = scheme(tokens, base_id, theme)
    backdrops = {"--border": "--background", "--input": "--background", "--sidebar-border": "--sidebar"}
    return {
        name: oklch_to_hex(value, raw.get(backdrops.get(name, ""), raw["--background"]))
        for name, value in raw.items()
    }


def color_var_names(tokens: dict[str, Any]) -> tuple[str, ...]:
    """生成 CSS が各ブロックへ書き出す変数名を順序付きで返す。"""
    return (
        *(f"--{name}" for name in tokens["role_order"]),
        *(f"--{name}" for name in tokens["derived_roles"]),
        *CHART_VARS,
    )


def contrast_violations(tokens: dict[str, Any], base_id: str, theme: str) -> list[str]:
    """1 ブロックの配色が可読性契約を満たすか検査する。"""
    label = f"{theme}/{base_id}"
    colors = resolved_hex(tokens, base_id, theme)
    violations: list[str] = []
    for foreground, background, minimum in CONTRAST_CONTRACT:
        ratio = contrast_ratio(colors[foreground], colors[background])
        if ratio < minimum:
            violations.append(
                f"{label} {foreground} と {background} のコントラストが {ratio:.2f}（下限 {minimum}）"
            )
    decorative = tokens["decorative_roles"]
    for name in decorative["vars"]:
        backdrop = "--sidebar" if name.startswith("sidebar") else "--background"
        ratio = contrast_ratio(colors[f"--{name}"], colors[backdrop])
        if ratio < decorative["min_ratio"]:
            violations.append(
                f"{label} --{name} が {backdrop} と同化している（{ratio:.2f}、下限 {decorative['min_ratio']}）"
            )
    series = [colors[name] for name in CHART_VARS]
    if len(set(series)) != len(series):
        violations.append(f"{label} 系列色に重複がある")
    minimum_distance = tokens["series"]["min_delta_e"]
    for left in range(len(series)):
        for right in range(left + 1, len(series)):
            distance = delta_e(series[left], series[right])
            if distance < minimum_distance:
                violations.append(
                    f"{label} --chart-{left + 1} と --chart-{right + 1} の色差が "
                    f"{distance:.1f}（下限 {minimum_distance}）"
                )
    return violations


# --- tokens.css の生成 -------------------------------------------------------


def render_tokens_css(tokens: dict[str, Any]) -> str:
    """tokens.json から tokens.css 全文を生成する。"""
    lines = [
        "/* 自動生成。正本は tokens.json。`python3 check_site.py --write-css .` で更新する。 */",
        "/* shadcn/ui のトークン: 配色 / タイポ / 角丸 / 影 / モーション */",
        "",
        ":root {",
        *_typography_lines(tokens),
        "",
        *_radius_lines(tokens),
        "",
        *_shadow_lines(tokens),
        "",
        *_motion_lines(tokens),
        "",
        *_state_lines(tokens),
        "",
        *_layout_lines(tokens),
        "",
        "  /* ベースカラー選択 UI 用。テーマに依らず、そのベースの色味が分かる中間トーン。 */",
        "  /* primary（ほぼ黒）を出すと 5 つとも同じ黒い円になり選べない。 */",
        *(
            f"  --base-{base_id}: {tokens['bases'][base_id]['light']['muted-foreground']};"
            for base_id in base_ids(tokens)
        ),
        "}",
        "",
        ':root[data-theme="light"] { color-scheme: light; }',
        ':root[data-theme="dark"] { color-scheme: dark; }',
        "",
    ]
    names = color_var_names(tokens)
    for base_id in base_ids(tokens):
        for theme in THEMES:
            resolved = scheme(tokens, base_id, theme)
            lines.append(f':root[data-theme="{theme}"][data-base="{base_id}"] {{')
            lines.extend(f"  {name}: {resolved[name]};" for name in names)
            lines.append("}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _typography_lines(tokens: dict[str, Any]) -> list[str]:
    """タイプスケール変数を返す。"""
    typography = tokens["typography"]
    lines = [
        f'  --font-sans: {typography["sans_family"]};',
        f'  --font-mono: {typography["mono_family"]};',
    ]
    for name, spec in typography["scale"].items():
        lines.append(f'  --text-{name}: {spec["size"]}rem;')
        lines.append(f'  --text-{name}-line: {spec["line"]}rem;')
    lines.extend(f"  --tracking-{name}: {value}em;" for name, value in typography["tracking"].items())
    lines.extend(f"  --font-weight-{name}: {value};" for name, value in typography["weight"].items())
    return lines


def _radius_lines(tokens: dict[str, Any]) -> list[str]:
    """角丸変数を返す。"""
    radius = tokens["radius"]
    lines = [f'  --radius: {radius["base_rem"]}rem;']
    for name, delta in radius["steps"].items():
        value = "var(--radius)" if delta == 0 else f"calc(var(--radius) {'+' if delta > 0 else '-'} {abs(delta)}px)"
        lines.append(f"  --radius-{name}: {value};")
    return lines


def _shadow_lines(tokens: dict[str, Any]) -> list[str]:
    """影の変数を返す。影色は黒固定で、両テーマとも同じ定義を使う。"""
    shadow = tokens["shadow"]
    return [
        "  --shadow-rgb: 0 0 0;",
        *(f"  --shadow-{name}: {value};" for name, value in shadow.items() if name != "source"),
    ]


def _motion_lines(tokens: dict[str, Any]) -> list[str]:
    """モーション（イージング / 時間）変数を返す。"""
    motion = tokens["motion"]
    return [
        *(f"  --ease-{name}: {value};" for name, value in motion["easing"].items()),
        *(f"  --duration-{name}: {value}ms;" for name, value in motion["duration"].items()),
    ]


def _state_lines(tokens: dict[str, Any]) -> list[str]:
    """ステート（ホバー濃度・リング幅）の変数を返す。"""
    return [
        f'  --state-{name.replace("_", "-").removesuffix("-px")}: {value}{"px" if name.endswith("_px") else ""};'
        for name, value in tokens["state"].items()
    ]


def _layout_lines(tokens: dict[str, Any]) -> list[str]:
    """レイアウト（ブレークポイント / ガター）変数を返す。"""
    layout = tokens["layout"]
    return [
        f'  --layout-{name.replace("_", "-").removesuffix("-px")}: {value}px;'
        for name, value in layout.items()
        if name != "source"
    ]


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
    """サイトが shadcn/ui の契約を満たすかを検査し、違反メッセージを返す。"""
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
    if html_attrs.get("data-base") not in base_ids(tokens):
        violations.append("html[data-base] が tokens.json のベースカラー識別子ではない")
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
    for base_id in base_ids(tokens):
        for theme in THEMES:
            violations.extend(contrast_violations(tokens, base_id, theme))
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
    if ":focus-visible" not in css:
        violations.append("styles.css に :focus-visible のリングが無い（キーボード操作が見えない）")
    # 文字色を透明との混合で作ると、ライトでは背景へ寄って可読性契約を割る。
    # 面や枠の混合は問題ないので、`color:` だけを見る。
    for match in re.finditer(r"(?<![-\w])color:\s*color-mix\([^;}]*\btransparent\b", css):
        violations.append(
            f"文字色を透明との color-mix で作っている: `{match.group(0)[:52]}...`"
            "（役割変数を直接使う）"
        )
    if re.search(r"font-size:\s*(?:[0-9]|1[0-1])px", css):
        violations.append("12px 未満の font-size がある")
    violations.extend(_validate_reduced_motion(css))
    violations.extend(_validate_resting_state(css))
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


def _validate_resting_state(css: str) -> list[str]:
    """アニメーションが完走しなくても中身が読めることを検査する。

    背面タブ・JS 無効・印刷ではタイムラインが進まない。静止状態を不可視にする
    書き方はそのとき真っ白なページになるので、3 つとも機械的に落とす。

    1. `forwards`: 完走しないと最終状態にならない
    2. `@keyframes` の外の `opacity: 0`: 静止状態そのものが不可視
    3. `[data-animate]` の外で `both` / `backwards`: 遅延中と停止中に `from` の
       状態が貼り付く。JS が画面内で付ける属性の下だけに置けば、背面タブでは
       そもそも起動しない
    """
    violations: list[str] = []
    if re.search(r"animation-fill-mode:\s*forwards", css) or re.search(
        r"animation:[^;}]*\bforwards\b", css
    ):
        violations.append("animation の fill-mode に forwards がある（静止状態が不可視になる）")
    outside = re.sub(r"@keyframes[^{]*\{(?:[^{}]*\{[^{}]*\}\s*)*\}", "", css, flags=re.DOTALL)
    if re.search(r"(?<![-\w])opacity:\s*0(?:\.0+)?\s*;", outside):
        violations.append("@keyframes の外に opacity: 0 がある（静止状態は必ず読める状態にする）")
    for selector, block in re.findall(r"([^{}]+)\{([^{}]*)\}", outside):
        if "[data-animate" in selector:
            continue
        if re.search(r"animation(?:-fill-mode)?:[^;}]*\b(?:both|backwards)\b", block):
            violations.append(
                f"`{selector.strip().splitlines()[-1].strip()}` の animation が "
                "[data-animate] の外で both/backwards を使っている"
                "（背面タブで from の状態に貼り付く）"
            )
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
        ("data-base", ("dataset.base", "data-base")),
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
    `var(--*)` を通す。ここを緩めるとテーマ切替が片側だけ壊れる。
    """
    violations: list[str] = []
    for hex_color in sorted({match.upper() for match in HEX_RE.findall(text)}):
        violations.append(f"手書きファイルに生の hex {hex_color} がある（tokens.json 経由で参照する）")
    for function in sorted({match.group(1).lower() for match in COLOR_FUNCTION_RE.finditer(text)}):
        violations.append(f"手書きファイルに色関数 `{function}()` がある（var(--*) か color-mix を使う）")
    for name in sorted({match.group(1).lower() for match in NAMED_COLOR_RE.finditer(text)}):
        if name not in NON_COLOR_KEYWORDS:
            violations.append(f"名前付き色 `{name}` は禁止（var(--*) を使う）")
    return violations


# --- CLI ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """CLI パーサを構築する。"""
    parser = argparse.ArgumentParser(description="shadcn/ui トークン契約の検査 / 生成")
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
