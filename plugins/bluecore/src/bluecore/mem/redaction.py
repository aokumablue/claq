"""PII・シークレットマスキングフィルタ

埋め込みベクトル化・チャンク保存の前にテキストに適用し、
Vec2Text 型の埋め込み反転攻撃が成立しても原文の機密情報が復元されないようにする。
外部ライブラリ不要。
"""

from __future__ import annotations

import re

_PLACEHOLDER = "[REDACTED]"

# (name, pattern) の順序付きリスト。上から順にマッチを置換する。
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # JWT トークン（ヘッダー.ペイロード.署名 全体をマスク）
    ("jwt_token", re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")),
    # Anthropic / OpenAI API キー
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    # Slack Bot / User トークン
    ("slack_token", re.compile(r"\bxox[bpoa]-[A-Za-z0-9-]{10,}\b")),
    # GitHub Personal Access Token (classic: ghp_ / gho_ / ghs_ / ghr_)
    ("github_token", re.compile(r"\bgh[pors]_[A-Za-z0-9]{36,}\b")),
    # GitHub Fine-Grained PAT (github_pat_)
    ("github_fine_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{59,}\b")),
    # AWS Access Key ID
    ("aws_key_id", re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA)[A-Z0-9]{16}\b")),
    # AWS Secret Access Key (40 文字の base64 様文字列を直前のキーワードで判定)
    ("aws_secret", re.compile(r'(?i)(?:aws.?secret|secret.?access.?key)\s*[=:]\s*["\']?([A-Za-z0-9/+]{40})["\']?')),
    # Generic Bearer token
    ("bearer_token", re.compile(r'(?i)\bbearer\s+[A-Za-z0-9\-._~+/]+=*\b')),
    # パスワード / シークレット代入（password= / secret= 等）
    ("password_assign", re.compile(r'(?i)(?:password|passwd|secret|token|api[-_]?key)\s*[=:]\s*["\']?([^\s"\']{8,})["\']?')),
    # メールアドレス
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
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
