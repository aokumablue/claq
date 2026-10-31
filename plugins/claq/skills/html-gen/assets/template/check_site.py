"""shadcn/ui のデザイントークン契約を機械検査し、色トークン CSS を生成する。

標準ライブラリのみ。役割は 2 つある。

1. **生成**: `references/tokens.json` が持つ shadcn/ui レジストリの逐語コピーから
   `tokens.css`（配色・タイポ・角丸・影・モーションのトークン）を書き出す。
   upstream をそのまま並べるのが基本で、実測で可読性契約に届かない
   `ring` / `muted-foreground` / `chart-*` だけを規則に沿って調整する。
2. **検査**: 生成された `tokens.css` が tokens.json と一致すること、手書きの
   `styles.css` / `index.html` / `app.js` が契約（色は変数経由のみ・
   コントラスト・レスポンシブ・モーション低減・アクセシビリティ・スクリプト混入・
   Web フォントのライセンス同梱）を守ることを確認する。

    python3 check_site.py .              # 検査
    python3 check_site.py --write-css .  # tokens.css を再生成
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import re
import sys
from html import unescape
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
#: (前景変数, 背景変数, 最低比率)。WCAG 2.2: 文字 4.5:1（SC 1.4.3）/ 非文字 3:1（SC 1.4.11）。
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
#: 止まらない動き。自動で始まり 5 秒を超えて続く動きは、止める手段が無いと
#: WCAG 2.2 SC 2.2.2 に反する。脈動する点は「リアルタイムで更新中」の合図にも読め、
#: 静的なページでは誤りになる。styles.css と HTML に埋め込んだ CSS で同じ基準を使う。
#: CSS のプロパティ名とキーワードは大小文字を区別せず、コロンの前に空白も置けるので、
#: どの書き方でもブラウザは止まらない動きにする。
#: 動きはカスタムプロパティ経由でも渡せる。tokens.json の ``source`` は Tailwind v4 のドキュメントを
#: 出典に挙げており、その既定テーマの ``--animate-spin`` / ``--animate-ping`` /
#: ``--animate-pulse`` / ``--animate-bounce`` は値に ``infinite`` を含む。これを写して
#: ``animation: var(--animate-ping)`` で使うと、``animation`` の宣言には ``infinite`` が
#: 現れず素通りするので、宣言側（``--*``）の値も見る。
#: ``infinite`` はキーワードとして単独で現れたものだけを拾う。``infinite-scroll`` のように
#: キーフレーム名の一部になっているものは、反復が有限でも語境界（``\b``）では一致してしまう。
LOOPING_ANIMATION_RE = re.compile(
    r"(?:animation(?:-iteration-count)?|--[\w-]+)\s*:[^;}]*(?<![\w-])infinite(?![\w-])", re.IGNORECASE
)
#: CSS の字句。文字列リテラルと引用符の無い ``url()`` は ``/*`` を含んでもコメントの開始ではない
#: ので先に読み飛ばし、残る ``/* ... */`` だけをコメント（グループ 1）として拾う。コメントは
#: ブラウザが読まないので動きとして数えない。閉じていないコメントは末尾まで続く。
CSS_TOKEN_RE = re.compile(
    r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|(?i:url)\(\s*[^)\"'\s][^)]*\)|(/\*.*?(?:\*/|\Z))", re.DOTALL
)
#: SVG の SMIL アニメーションの無限反復。HTML パーサは属性名を大小文字を問わず
#: repeatCount / repeatDur へ直し、引用符の無い値も読む。値は大小文字を区別し、
#: INDEFINITE は無効な値として捨てられて 1 回で止まるので、indefinite だけを拾う。
#: 値の前後の空白を除いて読むブラウザ（Chrome は repeatDur、Firefox は repeatCount）
#: では止まらないので、引用符の内側の空白も許す。
LOOPING_SMIL_RE = re.compile(r"(?<![-\w])(?i:repeat(?:count|dur))\s*=\s*[\"']?\s*indefinite\b")
#: HTML に埋め込まれた CSS（style 属性の値と <style> 要素の中身）。
EMBEDDED_CSS_RE = re.compile(
    r"(?<![-\w])style\s*=\s*(?:\"([^\"]*)\"|'([^']*)')|<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL
)


def _has_looping_animation(css: str) -> bool:
    """CSS に止まらない動き（``infinite``）があるかを返す。コメントの中は無視する。

    文字列リテラルと ``url()`` の中の ``/*`` はコメントの開始ではないので、読み飛ばしてから
    コメントだけを除く（そうしないと閉じ ``*/`` の無い ``/*`` 以降が丸ごと消えて見逃す）。

    Args:
        css: 検査対象の CSS 文字列

    Returns:
        コメントを除いた CSS に ``infinite`` の宣言があれば True
    """
    stripped = CSS_TOKEN_RE.sub(lambda match: " " if match.group(1) is not None else match.group(0), css)
    return LOOPING_ANIMATION_RE.search(stripped) is not None


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
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _to_srgb(channel: float) -> float:
    """線形成分を sRGB へ戻す。"""
    return channel * 12.92 if channel <= 0.0031308 else 1.055 * (channel ** (1 / 2.4)) - 0.055


def _linear_rgb(hex_color: str) -> tuple[float, float, float]:
    """hex を線形 RGB へ変換する。"""
    body = _norm_hex(hex_color)[1:]
    return tuple(_to_linear(int(body[i : i + 2], 16) / 255.0) for i in (0, 2, 4))


_WHITE = (0.95047, 1.0, 1.08883)


def hex_to_lab(hex_color: str) -> tuple[float, float, float]:
    """hex を CIE Lab（D65）へ変換する。"""
    red, green, blue = _linear_rgb(hex_color)
    x = 0.4124564 * red + 0.3575761 * green + 0.1804375 * blue
    y = 0.2126729 * red + 0.7151522 * green + 0.0721750 * blue
    z = 0.0193339 * red + 0.1191920 * green + 0.9503041 * blue

    def f(value: float) -> float:
        """Lab の非線形圧縮。"""
        return value ** (1 / 3) if value > 216 / 24389 else (24389 / 27 * value + 16) / 116

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


def _lower_lightness_until(value: str, backdrop_hex: str, minimum: float) -> str:
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
    count = series["count"]
    return {
        f"--chart-{index}": format_oklch(lightness, fit_chroma(lightness, chroma, hue), hue)
        for index, (hue, chroma) in enumerate(zip(series["hues"][:count], series["chromas"][:count], strict=True), 1)
    }


def scheme(tokens: dict[str, Any], base_id: str, theme: str) -> dict[str, str]:
    """1 つのベースカラー / テーマについて、全 CSS 色変数を解決する。"""
    resolved = {f"--{name}": value for name, value in tokens["bases"][base_id][theme].items()}
    for fix in tokens["contrast_fixes"]:
        name = f'--{fix["var"]}'
        backdrop = resolved[f'--{fix["against"]}']
        current = oklch_to_hex(resolved[name], backdrop)
        backdrop_hex = oklch_to_hex(backdrop)
        if contrast_ratio(current, backdrop_hex) < fix["min_ratio"]:
            resolved[name] = _lower_lightness_until(resolved[name], backdrop_hex, fix["min_ratio"])
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
            violations.append(f"{label} {foreground} と {background} のコントラストが {ratio:.2f}（下限 {minimum}）")
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
    for (left, first), (right, second) in itertools.combinations(enumerate(series, 1), 2):
        distance = delta_e(first, second)
        if distance < minimum_distance:
            violations.append(
                f"{label} --chart-{left} と --chart-{right} の色差が {distance:.1f}（下限 {minimum_distance}）"
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
            lines += [
                f':root[data-theme="{theme}"][data-base="{base_id}"] {{',
                *(f"  {name}: {resolved[name]};" for name in names),
                "}",
                "",
            ]
    return "\n".join(lines).rstrip() + "\n"


def _typography_lines(tokens: dict[str, Any]) -> list[str]:
    """タイプスケール変数を返す。"""
    typography = tokens["typography"]
    lines = [
        f'  --font-sans: {typography["sans_family"]};',
        f'  --font-mono: {typography["mono_family"]};',
    ]
    for name, spec in typography["scale"].items():
        lines += [f'  --text-{name}: {spec["size"]}rem;', f'  --text-{name}-line: {spec["line"]}rem;']
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
    return [
        "  --shadow-rgb: 0 0 0;",
        *(f"  --shadow-{name}: {value};" for name, value in tokens["shadow"].items() if name != "source"),
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
    return [
        f'  --layout-{name.replace("_", "-").removesuffix("-px")}: {value}px;'
        for name, value in tokens["layout"].items()
        if name != "source"
    ]


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


#: 開始タグ名だけを消費する（属性は別に読む）。引用値やコメントの中の "<img" も、位置ごとに独立して走査される。
_RAW_TAG_RE = re.compile(r"<([A-Za-z][^\t\n\f\r />]*)")
#: 属性 1 個。区切りは HTML の空白か "/"（0 個も可: src="x"onerror=）。引用符は "=" の直後でだけ値を囲む（title=a"b は無引用値）。
_RAW_ATTR_RE = re.compile(
    r"[\t\n\f\r /]*([^\t\n\f\r />][^\t\n\f\r />=]*)"
    r"(?:[\t\n\f\r ]*=[\t\n\f\r ]*(?:\"([^\"]*)\"|'([^']*)'|([^\t\n\f\r >]*)))?"
)
#: URL パーサが位置を問わず捨てる文字（ASCII タブ・改行）。
_URL_TAB_OR_NEWLINE_RE = re.compile(r"[\t\n\r]")
#: URL パーサが先頭から捨てる文字（C0 制御文字と空白）。
_URL_LEADING_JUNK = "".join(chr(code) for code in range(0x21))


def _raw_start_tags(html: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """生テキストから開始タグとその属性を拾う。

    HTMLParser は RCDATA（SVG の ``<title>`` など）の中のタグを報告しないので使わない。
    ブラウザはそこに書いた ``<img>`` を SVG の外へ出して実行する。
    タグ名だけを消費するので、引用値・コメントに埋もれた ``<`` も個別に読む。

    Args:
        html: 検査対象の HTML 文字列

    Returns:
        (小文字のタグ名, [(小文字の属性名, 実体参照を解いた値), ...]) の一覧。重複属性も潰さない
    """
    tags: list[tuple[str, list[tuple[str, str]]]] = []
    for tag in _RAW_TAG_RE.finditer(html):
        attrs: list[tuple[str, str]] = []
        pos = tag.end()
        while (attr := _RAW_ATTR_RE.match(html, pos)) is not None:
            attrs.append((attr.group(1).lower(), unescape(attr.group(2) or attr.group(3) or attr.group(4) or "")))
            pos = attr.end()
        tags.append((tag.group(1).lower(), attrs))
    return tags


def _is_javascript_url(value: str) -> bool:
    """URL パーサと同じ前処理をした値が javascript: スキームかを返す。

    Args:
        value: 実体参照を解いた属性値

    Returns:
        javascript: の URL なら True
    """
    return _URL_TAB_OR_NEWLINE_RE.sub("", value).lstrip(_URL_LEADING_JUNK).lower().startswith("javascript:")


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
    return [
        *_validate_html(html, tokens),
        *_validate_generated_css(generated, tokens),
        *_validate_styles(styles),
        *_validate_js(script),
        *_validate_authored_colors(html + styles + script),
        *_validate_axis_text_scale(html, styles),
        *_validate_fonts(site_dir),
    ]


def _validate_fonts(site_dir: Path) -> list[str]:
    """fonts/ に Web フォントを置くなら、ライセンス（SIL OFL 1.1）も同梱されているかを検査する。"""
    fonts = site_dir / "fonts"
    if any(fonts.glob("*.woff2")) and not (fonts / "OFL.txt").is_file():
        return ["fonts/ に woff2 があるのに fonts/OFL.txt が無い（SIL OFL 1.1 の再配布条件。テンプレートの fonts/ を複製し直す）"]
    return []


def _validate_html(html: str, tokens: dict[str, Any]) -> list[str]:
    """HTML の構造・アクセシビリティ・スクリプト混入の契約を検査する。"""
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
    violations.extend(_validate_inline_motion(html))
    violations.extend(_validate_script_injection(html))
    violations.extend(_validate_nav_links(tags))
    violations.extend(_validate_lists(html, tags))
    if page_kind == "dashboard":
        violations.extend(_validate_dashboard(html, tags))
        violations.extend(_validate_chart_scroll_wrapper(html))
    if not _script_runs_before_body(tags):
        violations.append("app.js が <head> で読み込まれていない（ダーク選択時に白のちらつきが出る）")
    if not _csp_meta_in_head(tags):
        violations.append(
            "Content-Security-Policy の meta が <head> に無い（<meta charset> の直後に置く。<body> 内ではブラウザが無視する）"
        )
    violations.extend(_weak_csp_directives(tags))
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


def _validate_inline_motion(html: str) -> list[str]:
    """HTML に直接書いた止まらない動きを検査する。

    styles.css の検査だけでは、SVG の ``<animate repeatCount="indefinite">`` や、
    style 属性・``<style>`` 要素に書いた ``infinite`` が素通りする。どこに書いても
    止まらない動きであることは変わらないので、styles.css と同じ基準で落とす。
    埋め込み CSS は ``;`` で区切ってつなぎ、別の属性の語をまたいで拾わない。

    Args:
        html: 検査対象の HTML 文字列

    Returns:
        違反メッセージの一覧
    """
    embedded = [part for match in EMBEDDED_CSS_RE.finditer(html) for part in match.groups() if part]
    if LOOPING_SMIL_RE.search(html) or any(_has_looping_animation(part) for part in embedded):
        return ["index.html に止まらない動き（infinite / indefinite）がある（回数を有限にするか、動きを外す）"]
    return []


#: URL として解釈される属性。javascript: の判定はここに限る（``title="JavaScript: 入門"`` を落とさない）。
_URL_ATTRIBUTES = frozenset({"href", "src", "action", "formaction", "xlink:href", "data", "poster", "srcdoc"})


def _validate_script_injection(html: str) -> list[str]:
    """データ由来の文字列に紛れて実行されるスクリプトを検査する。

    イベント属性・javascript: の URL・meta refresh・app.js 以外の ``<script>`` を、HTMLParser が
    読み飛ばす場所（SVG の ``<title>`` / ``<style>`` の中など）も含めて落とす。

    Args:
        html: 検査対象の HTML 文字列

    Returns:
        違反メッセージの一覧
    """
    tags = _raw_start_tags(html)
    handlers = sorted({name for _, attrs in tags for name, _ in attrs if name.startswith("on")})
    violations = [
        f"index.html にイベント属性 {name} がある（データ由来の文字列は HTML エスケープして書く。動作は app.js に置く）"
        for name in handlers
    ]
    if any(_is_javascript_url(value) for _, attrs in tags for name, value in attrs if name in _URL_ATTRIBUTES):
        violations.append(
            "index.html に javascript: の URL がある（データ由来の文字列は HTML エスケープして書く。"
            "リンク先は http(s) か #id にする）"
        )
    if any(tag == "meta" and dict(attrs).get("http-equiv", "").lower() == "refresh" for tag, attrs in tags):
        violations.append(
            "index.html に meta refresh がある（閲覧者を別のページへ飛ばせる。データ由来の文字列は HTML エスケープして書く）"
        )
    if any(tag == "script" and {value for name, value in attrs if name == "src"} != {"app.js"} for tag, attrs in tags):
        violations.append(
            "index.html に app.js 以外の <script> がある（スクリプトは app.js だけにし、"
            "データ由来の文字列は HTML エスケープして書く）"
        )
    return violations


#: 直交軸チャート 1 枚に要求する目盛りラベルと目盛り線の最小本数。
#: 2 本は範囲の両端を述べるだけで、間隔（1 目盛りがいくらか）を伝えない。
#: 下限・上限・内側 1 本の 3 本で初めて尺度になる。一般的な推奨は 5〜12 本なので
#: これは目標値ではなく床である。
_MIN_AXIS_TICKS = 3

#: 直交軸を持つと判定するマークの class。棒・折れ線・面・散布点・積み上げ区画は
#: いずれも値を軸上の**位置**で符号化するため、軸が無ければ数値を読めない。
#: `arc`（ドーナツ）は角度で符号化するので直交軸を持たず、`spark-*` は後述の
#: 潰し判定で除外される。
#:
#: **種別を増やしたらここも増やす。** 増やさないと、新しい形だけが軸ゼロでも素通りする。
#: `bar--h` は `class="bar` の前方一致で既に入る。ヒストグラムの `bin` は縦棒の
#: 分類上限（`_MAX_VERTICAL_BARS`）に数えないよう、`bar` と別の名前にしてある。
_CARTESIAN_MARK_CLASSES = ("bar", "bin", "series-line", "series-area", "point", "seg")


def _iter_svg_blocks(html: str) -> list[str]:
    """HTML 中の ``<svg>...</svg>`` を 1 枚ずつ取り出す。

    このテンプレートの SVG は入れ子にならないため、最短一致で切り出せる。

    Args:
        html: 検査対象の HTML 文字列

    Returns:
        ``<svg`` から ``</svg>`` までを含む文字列の一覧
    """
    return re.findall(r"<svg\b.*?</svg>", html, re.DOTALL)


def _is_cartesian_chart(svg: str) -> bool:
    """その SVG が直交軸で値を符号化しているかを返す。

    class 名ではなくマークの種類で判定する。「ドーナツだから除外」のような
    名前による除外は、次に書かれるチャートが同じ class を継がなかった時点で
    破れるため。

    Args:
        svg: ``<svg>`` 1 枚分の文字列

    Returns:
        棒・折れ線・面のいずれかを含み、かつ潰し描画でなければ True
    """
    if 'preserveAspectRatio="none"' in svg:
        return False
    return any(f'class="{name}' in svg for name in _CARTESIAN_MARK_CLASSES)


def _validate_chart_axes(html: str) -> list[str]:
    """直交軸チャートが目盛りを持つことを検査する。

    「図はあるが数値が読めない」は、図が無いのと同じかそれより悪い（読めた気に
    させる分だけ悪い）。散文で禁じるだけでは残るので機械的に落とす。

    Args:
        html: 検査対象の HTML 文字列

    Returns:
        違反メッセージの一覧
    """
    violations: list[str] = []
    for index, svg in enumerate(_iter_svg_blocks(html), start=1):
        if not _is_cartesian_chart(svg):
            continue
        ticks = svg.count('class="axis-text')
        grids = svg.count('class="grid-line')
        if ticks < _MIN_AXIS_TICKS:
            violations.append(
                f"{index} 枚目の直交軸チャートの目盛りラベルが {ticks} 個"
                f"（{_MIN_AXIS_TICKS} 個以上。軸の数値が無いと図から値を読めない）"
            )
        if grids < _MIN_AXIS_TICKS:
            violations.append(
                f"{index} 枚目の直交軸チャートの目盛り線が {grids} 本"
                f"（{_MIN_AXIS_TICKS} 本以上。値の尺度が無いと大小しか分からない）"
            )
    return violations


#: ドーナツに載せてよい分類の上限。角度と面積の比較は長さの比較より精度が落ちる
#: ため、3〜5 分類で差が大きいときにだけ成立する。6 以上は積み上げ横棒へ倒す。
_MAX_DONUT_SEGMENTS = 5

#: 縦棒に載せてよい分類の上限。これを超えるとラベルを回転させるか省略すること
#: になり、どちらも読む速度を落とす。横棒（降順）へ倒す。
_MAX_VERTICAL_BARS = 7


#: SVG 内の目盛り文字が画面上で満たすべき最小サイズ（px）。手書き CSS の
#: `font-size` には 12px 未満を禁じる検査が既にあるが、SVG の文字は viewBox の
#: 倍率で一緒に縮むため、宣言が 12px でも描画は 12px を割りうる。
_MIN_RENDERED_AXIS_PX = 12.0

#: 同じく、最小幅で描かれたときの上限（px）。viewBox を狭く書くと、宣言が 15px でも
#: 描画は本文やカード見出しより大きくなる（広い画面では app.js が 13px で頭打ちにするが、
#: JS 無効・印刷では上限が効かない）。
_MAX_RENDERED_AXIS_PX = 18.0


def _css_length(css: str, selector: str, prop: str) -> float | None:
    """セレクタ 1 個の宣言から px 長さを取り出す。見つからなければ None。"""
    block = re.search(rf"(?<![-\w]){re.escape(selector)}\s*\{{(.*?)\}}", css, re.DOTALL)
    if not block:
        return None
    found = re.search(rf"{prop}:\s*([\d.]+)px", block.group(1))
    return float(found.group(1)) if found else None


def _validate_axis_text_scale(html: str, css: str) -> list[str]:
    """目盛り文字が**描画されたとき**に 12px を割らないことを検査する。

    SVG の文字は viewBox 座標で書かれるので、SVG が幅 W で描かれるとき実効
    サイズは `宣言サイズ × W / viewBox幅` になる。W の下限は CSS の
    `min-inline-size` で静的に決まるため、最悪ケースをここで計算できる。
    CSS の宣言だけを見る `_validate_styles` の 12px 検査では、この縮みを捉えられない。

    Args:
        html: 検査対象の HTML 文字列
        css: 手書き CSS の文字列

    Returns:
        違反メッセージの一覧
    """
    declared = _css_length(css, ".axis-text", "font-size")
    if declared is None:
        return [".axis-text の font-size が読み取れない（目盛り文字の実効サイズを保証できない）"]
    violations: list[str] = []
    for index, svg in enumerate(_iter_svg_blocks(html), start=1):
        if "axis-text" not in svg:
            continue
        box = re.search(r'viewBox="0 0 ([\d.]+) [\d.]+"', svg)
        classes = re.search(r'class="([^"]*)"', svg)
        if not box or not classes:
            continue
        modifiers = [f".{name}" for name in classes.group(1).split() if name.startswith("chart--")]
        floor = next(
            (value for value in (_css_length(css, name, "min-inline-size") for name in modifiers) if value),
            _css_length(css, ".chart", "min-inline-size"),
        )
        if not floor:
            continue
        rendered = declared * floor / float(box.group(1))
        if rendered < _MIN_RENDERED_AXIS_PX:
            violations.append(
                f"{index} 枚目の目盛り文字が最小幅で {rendered:.1f}px に縮む"
                f"（{_MIN_RENDERED_AXIS_PX:.0f}px 以上。viewBox 幅 {box.group(1)} に対し"
                f"最小描画幅 {floor:.0f}px。font-size を上げるか viewBox を狭める）"
            )
        elif rendered > _MAX_RENDERED_AXIS_PX:
            violations.append(
                f"{index} 枚目の目盛り文字が最小幅で {rendered:.1f}px に伸びる"
                f"（{_MAX_RENDERED_AXIS_PX:.0f}px 以下。viewBox 幅 {box.group(1)} に対し"
                f"最小描画幅 {floor:.0f}px。viewBox を広げて座標を取り直す）"
            )
    return violations


def _class_names(tag: str) -> list[str]:
    """開始タグの文字列から class 名の一覧を取り出す。class が無ければ空。"""
    found = re.search(r'class="([^"]*)"', tag)
    return found.group(1).split() if found else []


def _validate_chart_text(html: str) -> list[str]:
    """図の中の文字がすべて ``axis-text`` で、``.chart`` の下にあることを検査する。

    文字の大きさと、広い画面での 13px の頭打ちは CSS が ``.chart text`` に掛け、
    描画倍率は app.js が ``svg.chart`` へだけ渡す。したがって次の文字は、その外で
    viewBox の倍率のまま伸びて、他の軸文字より大きく出る。

    - ``<text>`` を持つのにルートの class に ``chart`` が無い SVG の文字
    - ``font-size`` を属性や style で個別に書いた文字（``<tspan>`` などを含む）
    - class を持たない、または ``axis-text`` を持たない ``<text>``

    Args:
        html: 検査対象の HTML 文字列

    Returns:
        違反メッセージの一覧
    """
    violations: list[str] = []
    for index, svg in enumerate(_iter_svg_blocks(html), start=1):
        texts = re.findall(r"<text\b[^>]*>", svg)
        if not texts:
            continue
        if "chart" not in _class_names(re.match(r"<svg\b[^>]*>", svg).group(0)):
            violations.append(
                f"{index} 枚目の SVG が <text> を持つのに class=\"chart\" が無い"
                "（文字の頭打ちが掛からず、広い画面で伸びる）"
            )
        if re.search(r"font-size\s*[=:]", svg):
            violations.append(f"{index} 枚目の SVG が font-size を個別に指定している（軸文字の大きさが揃わない）")
        if any("axis-text" not in _class_names(tag) for tag in texts):
            violations.append(f"{index} 枚目の SVG の <text> が axis-text を持たない（軸文字の大きさが揃わない）")
    return violations


def _validate_nav_links(tags: list[tuple[str, dict[str, str]]]) -> list[str]:
    """サイドバーの項目が、文書内の実在する節へ飛ぶリンクであることを検査する。

    ``<button>`` の項目や、飛び先の id が無いリンクは、押しても何も起きない。

    Args:
        tags: 開始タグと属性の一覧

    Returns:
        違反メッセージの一覧
    """
    ids = {attrs["id"] for _, attrs in tags if attrs.get("id")}
    violations: list[str] = []
    for tag, attrs in tags:
        if "nav-item" not in attrs.get("class", "").split():
            continue
        href = attrs.get("href", "")
        if tag != "a" or not href.startswith("#"):
            violations.append("ナビの項目 (.nav-item) が <a href=\"#id\"> ではない（押しても節へ移動しない）")
        elif href[1:] not in ids:
            violations.append(f"ナビの項目の飛び先 {href} が文書内に無い（id を付けた節が必要）")
    return violations


#: 一覧とみなさない表。ヒートマップは格子全体を読む図で、図の直下の表は図の値の控え。
_NON_LIST_TABLE_CLASS = "heat"


def _is_key_value_table(inner: str) -> bool:
    """表が「行見出し + 値 1 列」の項目表（会社概要など）かを返す。

    項目表は絞り込む対象の集合ではないので、一覧とみなさない。

    Args:
        inner: ``<table>`` の中身

    Returns:
        本体の全行が値のセルを 1 つ以下しか持たなければ True
    """
    rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", inner.split("<tbody", 1)[-1], re.DOTALL)
    return bool(rows) and all(len(re.findall(r"<td\b", row)) <= 1 for row in rows)


def _validate_lists(html: str, tags: list[tuple[str, dict[str, str]]]) -> list[str]:
    """一覧の表が検索・絞り込みを持ち、検索欄を手書きしていないことを検査する。

    検索欄と絞り込みは app.js が ``table[data-list]`` から生成する。手書きの検索欄は
    結線されず、押しても何も起きない部品になるので置かせない。

    Args:
        html: 検査対象の HTML 文字列
        tags: 開始タグと属性の一覧

    Returns:
        違反メッセージの一覧
    """
    violations: list[str] = []
    if any(
        (tag == "input" and attrs.get("type") == "search") or attrs.get("role") == "search" for tag, attrs in tags
    ):
        violations.append("検索欄を手書きしている（一覧の検索は table[data-list] から app.js が生成する）")
    figure_data = [m.span() for m in re.finditer(r"<details\b[^>]*chart-data[^>]*>.*?</details>", html, re.DOTALL)]
    for match in re.finditer(r"<table\b([^>]*)>(.*?)</table>", html, re.DOTALL):
        attrs = match.group(1)
        if any(start <= match.start() < end for start, end in figure_data):
            continue
        if _NON_LIST_TABLE_CLASS in _class_names(attrs):
            continue
        if _is_key_value_table(match.group(2)):
            continue
        if "data-list" not in attrs:
            violations.append("表に data-list が無い（一覧には検索と主要項目の絞り込みを付ける）")
        elif "<thead" not in match.group(2) or "<tbody" not in match.group(2):
            violations.append("table[data-list] に <thead> と <tbody> が要る（絞り込みの列を読み取れない）")
    return violations


def _validate_chart_scroll_wrapper(html: str) -> list[str]:
    """最小幅を持つチャートが横スクロール用の器に入っていることを検査する。

    `.chart` は `min-inline-size` で潰れを防いでいる。この下限がカード幅を超えた
    とき、器が無いと SVG がページごと横へはみ出す。器があれば、はみ出しはカードの
    中の横スクロールに閉じる。

    Args:
        html: 検査対象の HTML 文字列

    Returns:
        違反メッセージの一覧
    """
    return [
        f'<svg class="{match.group(2)}"> が .chart-scroll の中に無い（最小幅がカードを超えるとページごと横へはみ出す）'
        for match in re.finditer(r'(.{0,80})<svg class="(chart[^"]*)"', html, re.DOTALL)
        if "chart--donut" not in match.group(2) and "chart-scroll" not in match.group(1)
    ]


#: 目盛りの刻みとして許す仮数。1・2・5 に 10 の冪を掛けた値だけが「読める」刻みで、
#: 45 や 150 のような端数は最大値を本数で割った結果であって読み手の役に立たない。
_NICE_TICK_MANTISSAS = (1.0, 2.0, 5.0, 10.0)


def _is_nice_step(step: float) -> bool:
    """目盛りの刻みが 1・2・5 × 10 の冪かを返す。

    Args:
        step: 隣り合う目盛りの差

    Returns:
        1・2・5（または 10）に 10 の冪を掛けた値であれば True
    """
    if step <= 0:
        return False
    exponent = math.floor(math.log10(step))
    mantissa = round(step / 10**exponent, 6)
    return mantissa in _NICE_TICK_MANTISSAS


def _validate_tick_steps(html: str) -> list[str]:
    """値の目盛りが等間隔で、かつ 1・2・5 の刻みであることを検査する。

    `references/design.md`「軸と目盛り」の刻み規則。散文へ書いただけでは守られない。
    最大値を本数で割った端数（0 / 45 / 90）では、読み手が「1 目盛りいくら」を即座に取れない。

    数字だけのラベルを値の目盛りとみなす。「1月」「Q1」のような分類ラベルは
    数値として解釈できないので自然に外れる。

    縦軸の目盛りは右揃え（``text-anchor="end"``）、横軸は中央揃えで置くので、揃えの
    値（省略時は start）で軸を分けて比べる。散布図やヒストグラムは両軸が数値で、
    まとめると 2 本の軸の目盛りが 1 列に混ざり、正しい図が「等間隔でない」で落ちる。

    Args:
        html: 検査対象の HTML 文字列

    Returns:
        違反メッセージの一覧
    """
    violations: list[str] = []
    for index, svg in enumerate(_iter_svg_blocks(html), start=1):
        axes: dict[str, list[float]] = {}
        for attrs, label in re.findall(r'<text class="axis-text"([^>]*)>([^<]+)</text>', svg):
            if re.fullmatch(r"[\d,]+", label):
                anchor = re.search(r'text-anchor="([a-z]+)"', attrs)
                axes.setdefault(anchor.group(1) if anchor else "start", []).append(float(label.replace(",", "")))
        for values in axes.values():
            if len(values) < 2:
                continue
            steps = {round(upper - lower, 6) for lower, upper in itertools.pairwise(sorted(values))}
            if len(steps) > 1:
                violations.append(f"{index} 枚目の目盛りが等間隔でない（刻み {sorted(steps)}）")
                continue
            step = steps.pop()
            if not _is_nice_step(step):
                violations.append(
                    f"{index} 枚目の目盛りの刻みが {step:g}"
                    "（1・2・5 の倍数にする。最大値を本数で割った端数は読み手が使えない）"
                )
    return violations


def _validate_chart_form(html: str) -> list[str]:
    """グラフ種別の選定規則のうち、数えれば決まるものを検査する。

    種別の選定は本来データの意味に依るので全部は機械化できない。ここで見るのは
    **データを見なくても数えるだけで違反と分かる 3 つ**に限る。この 3 つは
    `SKILL.md` のグラフ種別の表で最も破られやすい行でもある。

    意味に依る行（時系列を折れ線にするか縦棒にするか等）は散文の側が持つ。
    ここで無理に推測すると、正しいチャートを誤って落とす。

    Args:
        html: 検査対象の HTML 文字列

    Returns:
        違反メッセージの一覧
    """
    violations: list[str] = []
    for index, svg in enumerate(_iter_svg_blocks(html), start=1):
        arcs = svg.count('class="arc')
        if arcs > _MAX_DONUT_SEGMENTS:
            violations.append(
                f"{index} 枚目のドーナツが {arcs} 分類"
                f"（{_MAX_DONUT_SEGMENTS} 以下。超えるなら 100% 積み上げ横棒にする）"
            )
        bars = re.findall(r'<rect class="bar[^>]*>', svg)
        if len(bars) == 1:
            violations.append(f"{index} 枚目が棒 1 本のグラフ（値が 1 つなら指標カードにする）")
        vertical = [bar for bar in bars if _is_vertical_bar(bar)]
        if len(vertical) > _MAX_VERTICAL_BARS:
            violations.append(
                f"{index} 枚目の縦棒が {len(vertical)} 分類"
                f"（{_MAX_VERTICAL_BARS} 以下。超えるならラベルが読める横棒にする）"
            )
    return violations


def _is_vertical_bar(rect: str) -> bool:
    """``<rect class="bar">`` が縦棒かを返す。

    向きは class 名ではなく寸法で判定する。縦棒は高さが幅より大きく、横棒は
    その逆になる。class 名で判定すると、命名を変えただけで検査が素通りする。

    Args:
        rect: ``<rect class="bar" ...>`` 1 個分の文字列

    Returns:
        高さが幅より大きければ True。どちらかが読めなければ False
    """
    width = re.search(r'\bwidth="([\d.]+)"', rect)
    height = re.search(r'\bheight="([\d.]+)"', rect)
    if not width or not height:
        return False
    return float(height.group(1)) > float(width.group(1))


def _validate_stretched_svg_text(html: str) -> list[str]:
    """潰し描画の SVG に文字を置いていないことを検査する。

    ``preserveAspectRatio="none"`` は viewBox を縦横独立に引き伸ばすため、
    中の ``<text>`` も一緒に歪む（MDN: 縦横比を保たない指定は文字を引き伸ばす）。
    スパークラインのように文字を持たない図でだけ使ってよい指定であり、軸ラベルを
    持つチャートに使うと目盛りが読めない形に潰れる。

    Args:
        html: 検査対象の HTML 文字列

    Returns:
        違反メッセージの一覧
    """
    return [
        f"{index} 枚目の SVG が preserveAspectRatio=\"none\" のまま <text> を持つ（縦横独立に伸びるため文字が歪む）"
        for index, svg in enumerate(_iter_svg_blocks(html), start=1)
        if 'preserveAspectRatio="none"' in svg and "<text" in svg
    ]


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
    violations.extend(_validate_chart_axes(html))
    violations.extend(_validate_stretched_svg_text(html))
    violations.extend(_validate_chart_text(html))
    violations.extend(_validate_chart_form(html))
    violations.extend(_validate_tick_steps(html))
    violations.extend(_validate_time_point_hits(html))
    return violations


#: 折れ線の d のコマンドと、その後に続く引数。Z（閉じ）は時点を置かないので拾わない。
_PATH_COMMAND_RE = re.compile(r"([MmLlHhVvCcSsQqTtAa])([^MmLlHhVvCcSsQqTtAaZz]*)")
#: コマンドが 1 時点を置くのに使う引数の数。1 つのコマンドの後に座標を続けると同じ
#: コマンドの繰り返し（M の後は L）になるので、コマンドの文字ではなく引数の数で数える。
#: 始点の M も時点を置く。欠測の時点で線を途切れさせると M が 2 回以上現れ、区間の
#: 数 + 1 では時点を数え漏らす。
_PATH_ARITY = {"m": 2, "l": 2, "t": 2, "h": 1, "v": 1, "s": 4, "q": 4, "c": 6, "a": 7}
#: d と points に並ぶ数値。SVG は符号や小数点の前で区切りを省けるので、区切りではなく
#: 数値の形で拾う。
_NUMBER_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
#: ルートの svg の class に chart--scatter を持つ散布図。.point の有無で見分けると、
#: 折れ線に強調の点を 1 つ置いただけで時系列の検査が外れる。
_SCATTER_ROOT_RE = re.compile(r'<svg\b[^>]*\sclass="(?:[^"]*\s)?chart--scatter[\s"]')


def _path_points(d: str) -> int:
    """path の ``d`` が置く頂点の数を返す。

    コマンドごとに、引数の数を 1 時点分の引数の数で割って足す。円弧のフラグは区切らずに
    書ける（``0110`` は 0・1・10）ため数値が引数の数に満たないことがあるが、コマンドは
    少なくとも 1 時点を置くので 1 と数える。

    Args:
        d: path の ``d`` 属性の値

    Returns:
        頂点の数
    """
    return sum(
        max(1, len(_NUMBER_RE.findall(args)) // _PATH_ARITY[command.lower()])
        for command, args in _PATH_COMMAND_RE.findall(d)
    )


def _series_line_points(svg: str) -> list[int]:
    """SVG 1 枚の ``.series-line`` ごとに、線が置く時点の数を返す。

    path は ``d`` の頂点の数、polyline は ``points`` の数値の数 ÷ 2 で数える。
    ブラウザは単引用符の値も、改行をはさんだ値も同じ線として描くので、どちらも読む。

    Args:
        svg: ``<svg>`` 1 枚分の文字列

    Returns:
        線ごとの時点の数（``d`` / ``points`` を持たない線は含めない）
    """
    counts: list[int] = []
    for element, attrs in re.findall(r'<(path|polyline)\b([^>]*\bclass="series-line[^>]*)>', svg):
        name = "d" if element == "path" else "points"
        value = re.search(rf"\s{name}=([\"'])(.*?)\1", attrs, re.DOTALL)
        if value:
            text = value.group(2)
            counts.append(_path_points(text) if element == "path" else len(_NUMBER_RE.findall(text)) // 2)
    return counts


def _validate_time_point_hits(html: str) -> list[str]:
    """時系列の時点ごとに、値へ届く当たり判定があることを検査する。

    折れ線の値は時点ごとの当たり判定（``.hover-hit``）の ``<title>`` で読ませる。
    点や当たり判定を間引くと、線は通っているのに値へ届かない時点が残る。時点の数は
    ``.series-line`` の線が置く頂点の数（path は ``d`` の各コマンドが置く頂点、
    polyline は ``points`` の座標の組）で数え、時点の最も多い系列を当たり判定の下限にする。
    横軸のラベルでは数えない。月の行の下に年の行を置くとラベルが時点より多くなり、
    ラベルを間引くと少なくなる。散布図（ルートの svg に ``.chart--scatter``）は点が
    自身の ``<title>`` で値を読ませるので、回帰線を ``.series-line`` で引いても対象外。
    折れ線に強調の点（``.point``）を置いても対象のまま。潰し描画のスパークラインは
    値を指標カードの数値で読ませるので対象外。

    Args:
        html: 検査対象の HTML 文字列

    Returns:
        違反メッセージの一覧
    """
    violations: list[str] = []
    for index, svg in enumerate(_iter_svg_blocks(html), start=1):
        if 'preserveAspectRatio="none"' in svg or _SCATTER_ROOT_RE.match(svg):
            continue
        counts = _series_line_points(svg)
        if not counts:
            continue
        points = max(counts)
        hits = len(re.findall(r'class="hover-hit\b', svg))
        if hits < points:
            violations.append(
                f"{index} 枚目の時系列の当たり判定（.hover-hit）が {hits} 個で、線の時点 {points} 個に届かない"
                "（点を間引くと値に届かない時点が残る。全時点に .hover-col を置く）"
            )
    return violations


def _script_runs_before_body(tags: list[tuple[str, dict[str, str]]]) -> bool:
    """app.js が body より前（head 内）で読み込まれているかを返す。"""
    # body と app.js の script のうち、先に現れたほうで決まる
    first = next(
        (tag for tag, attrs in tags if tag == "body" or (tag == "script" and "app.js" in attrs.get("src", ""))), None
    )
    return first == "script"


#: CSP で値を固定する指令。スクリプトは app.js だけ、プラグインと <base> の差し替えは禁じる。
_CSP_REQUIRED_SOURCES = {"script-src": {"'self'"}, "object-src": {"'none'"}, "base-uri": {"'none'"}}


def _weak_csp_directives(tags: list[tuple[str, dict[str, str]]]) -> list[str]:
    """CSP の中身が弱められていないか（必須の指令の値と、外部オリジンの有無）を検査する。

    Args:
        tags: ``(タグ名, 属性)`` の開始タグ列

    Returns:
        違反メッセージの一覧（CSP の meta が無いときは空。欠落は別の検査が報告する）
    """
    content = next(
        (
            attrs.get("content", "")
            for tag, attrs in tags
            if tag == "meta" and attrs.get("http-equiv", "").lower() == "content-security-policy"
        ),
        None,
    )
    if content is None:
        return []
    directives = {}
    for part in content.split(";"):
        words = part.split()
        if words:
            directives[words[0].lower()] = set(words[1:])
    violations = [
        f"Content-Security-Policy の {name} が {' '.join(sorted(expected))} 以外を許している（値をそれだけにする）"
        for name, expected in _CSP_REQUIRED_SOURCES.items()
        if directives.get(name) != expected
    ]
    # 外部オリジンを許すと、閲覧者の IP と閲覧ページが第三者へ送られ、file:// やオフラインでも崩れる
    if any(source == "*" or ":" in source.strip("'") for sources in directives.values() for source in sources if source != "data:"):
        violations.append(
            "Content-Security-Policy が外部のオリジンを許している（フォント・画像・スクリプトは同梱して 'self' から読む）"
        )
    return violations


def _csp_meta_in_head(tags: list[tuple[str, dict[str, str]]]) -> bool:
    """Content-Security-Policy の meta が body より前（head 内）にあるかを返す。"""
    # body と CSP の meta のうち、先に現れたほうで決まる
    first = next(
        (
            tag
            for tag, attrs in tags
            if tag == "body" or (tag == "meta" and attrs.get("http-equiv", "").lower() == "content-security-policy")
        ),
        None,
    )
    return first == "meta"


def _validate_generated_css(css: str, tokens: dict[str, Any]) -> list[str]:
    """tokens.css が tokens.json からの生成結果と一致するかを検査する。"""
    violations: list[str] = []
    if css != render_tokens_css(tokens):
        violations.append("tokens.css が tokens.json からの生成結果と一致しない（--write-css で再生成する）")
    return violations + [
        violation
        for base_id in base_ids(tokens)
        for theme in THEMES
        for violation in contrast_violations(tokens, base_id, theme)
    ]


def _validate_styles(css: str) -> list[str]:
    """手書き CSS のレスポンシブ・モーション・トークン利用契約を検査する。"""
    violations = [f"styles.css が {name} を使っていない" for name in REQUIRED_STYLE_VARS if f"var({name}" not in css]
    violations += [
        f"styles.css に {width}px のブレークポイントが無い" for width in REQUIRED_BREAKPOINTS if f"{width}px" not in css
    ]
    violations += [
        message
        for needle, message in (
            ("minmax(", "styles.css に伸縮するグリッド（minmax）が無い"),
            ("clamp(", "styles.css に流体タイポ（clamp）が無い"),
            (":focus-visible", "styles.css に :focus-visible のリングが無い（キーボード操作が見えない）"),
        )
        if needle not in css
    ]
    # 文字色を透明との混合で作ると、ライトでは背景へ寄って可読性契約を割る。
    # 面や枠の混合は問題ないので、`color:` だけを見る。
    violations += [
        f"文字色を透明との color-mix で作っている: `{match.group(0)[:52]}...`（役割変数を直接使う）"
        for match in re.finditer(r"(?<![-\w])color:\s*color-mix\([^;}]*\btransparent\b", css)
    ]
    if re.search(r"font-size:\s*(?:[0-9]|1[0-1])px", css):
        violations.append("12px 未満の font-size がある")
    if _has_looping_animation(css):
        violations.append("styles.css に止まらない動き（infinite）がある（回数を有限にするか、動きを外す）")
    violations.extend(_validate_reduced_motion(css))
    violations.extend(_validate_resting_state(css))
    violations.extend(_validate_fluid_width(css))
    violations.extend(_validate_fluid_height(css))
    violations.extend(_validate_chart_chrome(css))
    return violations


def _validate_reduced_motion(css: str) -> list[str]:
    """モーション低減設定への追従を検査する。"""
    match = re.search(r"@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)\s*\{(.*?)\n\}", css, re.DOTALL)
    if match is None:
        return ["@media (prefers-reduced-motion: reduce) のブロックが無い"]
    return [
        f"モーション低減ブロックが {name} を止めていない" for name in ("animation", "transition") if name not in match.group(1)
    ]


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
    if re.search(r"animation-fill-mode:\s*forwards", css) or re.search(r"animation:[^;}]*\bforwards\b", css):
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
    return [
        f"固定幅 {match.group(1)}px がある（表示領域を使い切れない）"
        for match in re.finditer(r"(?<![-\w(])width:\s*(\d{3,})px", css)
        if int(match.group(1)) >= REQUIRED_BREAKPOINTS[0]
    ] + [
        f"max-width {match.group(1)}px が広い画面を捨てている"
        for match in re.finditer(r"(?<![(\w])max-width:\s*(\d{3,})px", css)
        if int(match.group(1)) < 1600
    ]


def _validate_fluid_height(css: str) -> list[str]:
    """チャートの高さを固定 px で頭打ちにしていないことを検査する。

    `_validate_fluid_width` が横方向で禁じているのと同じ誤りを縦方向で禁じる。
    ``block-size: auto`` の SVG は viewBox の比率どおりに伸びるので、そこへ固定 px の
    上限を掛けると広い画面ほど ``preserveAspectRatio`` のレターボックスで
    **図だけが小さく残る**。

    上限そのものは要る（超ワイドで 900px 超の図になっても読みやすくはない）。
    禁じるのは上限が**ビューポートに追従しない**ことなので、``clamp()`` /
    ``vh`` / ``%`` を含む式は通す。

    Args:
        css: 検査対象の CSS 文字列

    Returns:
        違反メッセージの一覧
    """
    values = (match.group(1).strip() for match in re.finditer(r"max-block-size:\s*([^;}]+)", css))
    return [
        f"max-block-size: {value} が固定（ビューポートに追従しないため広い画面で図だけが小さく残る。clamp() / vh を使う）"
        for value in values
        if re.fullmatch(r"\d+px", value)
    ]


def _validate_chart_chrome(css: str) -> list[str]:
    """目盛り線が実線のヘアラインであることを検査する。

    破線のグリッドは「予測値」「しきい値」を意味する記法として読まれるため、
    ただの目盛りに使うと意味を足してしまい、線の本数ぶんノイズが増える。
    目盛り線と軸線は面から 1 段だけ外した色の実線 1px にする。

    Args:
        css: 検査対象の CSS 文字列

    Returns:
        違反メッセージの一覧
    """
    violations: list[str] = []
    for selector in (".grid-line", ".axis-line"):
        block = re.search(rf"{re.escape(selector)}\s*\{{(.*?)\}}", css, re.DOTALL)
        if block and "stroke-dasharray" in block.group(1):
            violations.append(
                f"{selector} が破線（stroke-dasharray）（破線は予測・しきい値の記法。目盛りは実線のヘアラインにする）"
            )
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
        ("一覧の検索・絞り込み", ("data-list",)),
    )
    violations = [f"app.js が {label} を扱っていない" for label, needles in any_of if not any(n in script for n in needles)]
    if "light" not in script or "dark" not in script:
        violations.append("app.js が light/dark を扱っていない")
    return violations


def _validate_authored_colors(text: str) -> list[str]:
    """手書きファイルに生の色が書かれていないかを検査する。

    色の正本は tokens.json だけ。HTML / styles.css / app.js の色は必ず
    `var(--*)` を通す。ここを緩めるとテーマ切替が片側だけ壊れる。
    """
    hexes = sorted({match.upper() for match in HEX_RE.findall(text)})
    functions = sorted({match.group(1).lower() for match in COLOR_FUNCTION_RE.finditer(text)})
    names = sorted({match.group(1).lower() for match in NAMED_COLOR_RE.finditer(text)} - NON_COLOR_KEYWORDS)
    return [
        *(f"手書きファイルに生の hex {hex_color} がある（tokens.json 経由で参照する）" for hex_color in hexes),
        *(f"手書きファイルに色関数 `{function}()` がある（var(--*) か color-mix を使う）" for function in functions),
        *(f"名前付き色 `{name}` は禁止（var(--*) を使う）" for name in names),
    ]


# --- CLI ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """エントリポイント。"""
    parser = argparse.ArgumentParser(description="shadcn/ui トークン契約の検査 / 生成")
    parser.add_argument("site", nargs="?", default=".", help="検査するサイトディレクトリ")
    parser.add_argument("--write-css", action="store_true", help="tokens.json から tokens.css を再生成する")
    args = parser.parse_args(argv)
    site_dir = Path(args.site).resolve()
    try:
        tokens = load_tokens(default_tokens_path(site_dir))
    except FileNotFoundError as err:
        print(err, file=sys.stderr)
        return 2
    if args.write_css:
        path = site_dir / "tokens.css"
        path.write_text(render_tokens_css(tokens), encoding="utf-8")
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
