"""PII・シークレットマスキングフィルタ。

ログ出力と引き継ぎ本文など、永続化・外部へ出る自由文に適用する。
外部ライブラリ不要。
"""

from __future__ import annotations

import re

_PLACEHOLDER = "[REDACTED]"

# (name, pattern) の順序付きリスト。上から順にマッチを置換する。
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # JWT トークン（ヘッダー.ペイロード.署名 全体をマスク）。
    # `eyJ` の直前に base64url 文字が無いことを要求する（lookbehind）。これが無いと
    # `eyJeyJeyJ…` のような入力で 3 バイトごとに開始位置が立ち、そのたびに
    # `[A-Za-z0-9_-]+` が残り全体を貪欲に取ってから後退するため、走査が二次
    # オーダーへ落ちる（実測: 24,000 文字で 0.15s、入力 2 倍で時間 4 倍。1MiB へ
    # 外挿すると約 280 秒で、`redact` を呼ぶ SessionEnd フックの timeout 10 秒を
    # 桁で超える）。lookbehind が付くと開始位置の直前は必ず非 base64url 文字に
    # なり、各開始位置が走査する文字クラス連鎖どうしが重ならないため全体が線形。
    # 併せて量指定子を所有的（`++` / `*+`）にして後退そのものを禁じる。直後の
    # `\.` は文字クラスと素なので貪欲一致は元から `.` の手前で止まり、一致する
    # 文字列の集合は変わらない。
    # 代償として `abceyJhbGci…` のように base64url 文字へ続く `eyJ` は一致
    # しなくなる。JWT はトークンであり実物は区切りの直後に現れるため、これは
    # 意図した絞り込み。`hooks/commit_quality_scanner.py` の JWT パターンが
    # 先に同じ判断を採っており、本パターンはそれに揃えたもの
    # （`tests/mem/test_redaction.py` の同期テストが両者の同時適用を機械検査する）。
    (
        "jwt_token",
        re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]++\.[A-Za-z0-9_-]++\.[A-Za-z0-9_-]*+"),
    ),
    # Anthropic / OpenAI API キー
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    # 文字クラスに `-` を含めるのは、OpenAI の現行形式（`sk-` の後に
    # セグメントとハイフンが続く）が旧クラスでは一致しないため。
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    # Slack Bot / User トークン
    ("slack_token", re.compile(r"\bxox[bpoa]-[A-Za-z0-9-]{10,}\b")),
    # GitHub Personal Access Token (classic: ghp_ / gho_ / ghs_ / ghr_)
    ("github_token", re.compile(r"\bgh[pors]_[A-Za-z0-9]{36,}\b")),
    # GitHub Fine-Grained PAT (github_pat_)
    ("github_fine_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{59,}\b")),
    # Google API キー
    ("google_api_key", re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b")),
    # AWS Access Key ID
    ("aws_key_id", re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA)[A-Z0-9]{16}\b")),
    # AWS Secret Access Key (40 文字の base64 様文字列を直前のキーワードで判定)
    ("aws_secret", re.compile(r'(?i)(?:aws.?secret|secret.?access.?key)\s*[=:]\s*["\']?([A-Za-z0-9/+]{40})["\']?')),
    # Generic Bearer token
    ("bearer_token", re.compile(r'(?i)\bbearer\s+[A-Za-z0-9\-._~+/]+=*\b')),
    # パスワード / シークレット代入（password= / secret= 等）
    ("password_assign", re.compile(r'(?i)(?:password|passwd|secret|token|api[-_]?key)\s*[=:]\s*["\']?([^\s"\']{8,})["\']?')),
    # メールアドレス。local part は上限付きの所有的量指定子 `{1,64}+` にする。
    #
    # 曖昧な `+` のままだと、クラス内の長い連続に `\b` が多数立つ入力
    # （`AAA…A+AAA…A+…` / ハイフン・ドット区切りの長大トークン）で二次オーダー
    # へ落ちる。`\b` が n/40 箇所に立ち、その各開始位置から残り全体を貪欲に
    # 走査するためで、実測は 80,000 文字 0.25s・入力 2 倍で時間 4 倍、1MiB へ
    # 外挿して約 43 秒だった（`redact` を呼ぶ SessionEnd の timeout は 10 秒）。
    #
    # 所有化（`++`）だけでは足りない。後退は消えても「開始位置ごとに残り全体を
    # 前向きに走査する」コストが残り、二次オーダーのままだった（実測で確認済み）。
    # 長さ上限が要る。上限 64 は RFC 5321 の local part 上限（64 オクテット）で、
    # これを超えるアドレスはそもそも配送不能なため、実在アドレスの取りこぼしは
    # 生まない。上限を付けたうえで所有的にするのは、後退が必ず失敗する
    # （`@` は文字クラスと素なので、貪欲一致を縮めても `@` は現れない）ことが
    # 静的に分かっており、64 回の無駄な後退を開始位置ごとに繰り返さないため。
    #
    # 64 文字超の local part を落とすのは意図した絞り込みである。二次オーダーを
    # 残す方が危険で、SessionEnd の timeout に達すればフックごと落ちて
    # **redact が 1 件も適用されない**（＝全文が素通りする）ため、上限による
    # 局所的な false negative より失うものが大きい。
    #
    # domain 側（`[A-Za-z0-9.\-]+\.`）は所有化も上限付けもしない —— `.` が
    # クラスに含まれるため末尾 `\.` へ渡すには後退が必要で、所有化すると
    # `a@b.c.com` が一致しなくなる。domain 側は `@` の直後からしか開始せず、
    # `@` はクラスと素なので各走査長の総和が入力長に収まり、原理的に線形。
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]{1,64}+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    # IPv4 アドレス（プライベートアドレス帯のみ: 10.x / 172.16-31.x / 192.168.x）
    ("ipv4", re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b")),
    # 32 文字以上の連続した16進数文字列（ハッシュ・API キー等）
    ("hex_secret", re.compile(r"\b[0-9a-f]{32,}\b")),
    # Base64 エンコードされた長い文字列（40 文字以上）—JWT の payload 等。
    # 純粋な16進数（commit hash / UUID / SHA-256 等）は base64 シークレットではないため
    # 先読み (?![0-9a-fA-F]+\b) で除外し、誤検出を抑える。
    # 長さ閾値は 40 のまま維持し、32 バイト鍵の base64（44 文字）等の取りこぼしを防ぐ。
    ("base64_long", re.compile(r"\b(?![0-9a-fA-F]+\b)[A-Za-z0-9+/]{40,}={0,2}\b")),
]


def redact(text: str) -> str:
    """テキスト中の PII・シークレットを [REDACTED] に置換して返す。"""
    for _, pattern in _PATTERNS:
        text = pattern.sub(_PLACEHOLDER, text)
    return text


# knowledge カード（title/body/source_ref）向けの縮小版パターン。
# hex_secret（32 桁以上の16進）と base64_long（40 文字超の base64）は
# 汎用エントロピー検出であり、knowledge 本文に正当に現れる完全な
# commit SHA・長い識別子を丸ごと [REDACTED] に潰してしまう
# （handoff.py がファイルパスへの redact 適用を避けているのと同じ理由:
# 「40 文字超のパスを丸ごと潰す」既知の危険を knowledge にも継承しない）。
# 既知プレフィックス・キーワード系パターンのみを残す。
#
# これは false negative を意図的に許容する判断である（2026-08-26 実機監査 F-23
# で再提起され、維持と決めた）。キーワードもプレフィックスも伴わない 40 文字超の
# 高エントロピー文字列は knowledge へそのまま保存される。知識カードは人間が読む
# 前提の要約であり、raw credential を貼る経路自体が想定外なので、**knowledge に
# secret を貼らないのは呼び出し側の責務**とする。汎用エントロピー検出を戻すと、
# commit SHA・長い識別子・パスという正当な内容を日常的に潰すコストの方が大きい。
_KNOWLEDGE_EXCLUDED_PATTERN_NAMES = frozenset({"hex_secret", "base64_long"})
_KNOWLEDGE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (name, pattern) for name, pattern in _PATTERNS if name not in _KNOWLEDGE_EXCLUDED_PATTERN_NAMES
]


def redact_knowledge_text(text: str) -> str:
    """knowledge カードの title/body/source_ref 向けの縮小版 redact を適用する。

    汎用エントロピーパターン（32 桁 hex・40 文字超 base64）を除いた、
    既知プレフィックス・キーワード系パターン（JWT / 各種 API キー /
    bearer / password 代入 / email / private IPv4）のみを適用する。

    Args:
        text: 検査対象テキスト。

    Returns:
        既知プレフィックス系シークレットを [REDACTED] に置換した文字列。

    Raises:
        例外は発生しません。
    """
    for _, pattern in _KNOWLEDGE_PATTERNS:
        text = pattern.sub(_PLACEHOLDER, text)
    return text
