"""redaction.py のユニットテスト"""

import pytest

from devgear.mem.redaction import redact

_PLACEHOLDER = "[REDACTED]"


class TestRedact:
    """redact() のテーブル駆動テスト"""

    @pytest.mark.parametrize(
        "name, text, should_redact",
        [
            # Anthropic API キー
            ("anthropic_key", "key=sk-ant-api03-abcdefghijklmnopqrst1234567890ABCDEFGHIJKLMNO", True),
            # OpenAI API キー
            ("openai_key", "token = sk-abcdefghijklmnopqrstuvwxyz1234567890ABCD", True),
            # Slack Bot トークン
            ("slack_bot_token", "xoxb-123456789012-123456789012-abcdefghijklmnopqrstuvwx", True),
            # GitHub classic PAT
            ("github_pat_classic", "ghp_abcdefghijklmnopqrstuvwxyz123456ABCD", True),
            # GitHub Fine-Grained PAT
            ("github_fine_pat", "github_pat_11ABCDE_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ", True),
            # AWS Access Key ID
            ("aws_key_id", "AKIAIOSFODNN7EXAMPLE", True),
            # AWS Secret Key（代入形式）
            ("aws_secret_assign", "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", True),
            # Bearer トークン
            ("bearer_token", "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.abc", True),
            # password 代入
            ("password_assign", "password=mysecretpassword123", True),
            # メールアドレス
            ("email", "contact: user@example.com", True),
            # IPv4 アドレス
            ("ipv4", "server: 192.168.1.100", True),
            # 32 文字以上の hex 文字列
            ("hex_secret", "token: deadbeef0123456789abcdef01234567", True),
            # 通常テキストは変更なし
            ("plain_text", "今日は良い天気です。コードをリファクタリングしました。", False),
            # ファイルパスは変更なし
            ("file_path", "/home/user/dev/project/src/main.py", False),
            # 短い hex は変更なし（31 文字以下）
            ("short_hex", "deadbeef01234567", False),
        ],
    )
    def test_redact(self, name: str, text: str, should_redact: bool) -> None:
        result = redact(text)
        if should_redact:
            assert _PLACEHOLDER in result, f"[{name}] {_PLACEHOLDER!r} が含まれていない: {result!r}"
            assert text not in result or text == result, f"[{name}] 元のシークレットが残存している"
        else:
            assert result == text, f"[{name}] 変更されてはいけないテキストが変更された: {result!r}"

    def test_multiple_secrets_in_one_text(self) -> None:
        """複数シークレットが混在するテキストを全てマスクする"""
        text = "email=admin@example.com apikey=sk-abcdefghijklmnopqrstuvwxyz1234567890ABCD server=10.0.0.1"
        result = redact(text)
        assert result.count(_PLACEHOLDER) >= 3

    def test_empty_string(self) -> None:
        """空文字列はそのまま返る"""
        assert redact("") == ""

    def test_idempotent(self) -> None:
        """2 回適用しても結果が変わらない"""
        text = "password=secret123 user@example.com"
        once = redact(text)
        twice = redact(once)
        assert once == twice

    def test_preserves_structure(self) -> None:
        """コードのファイルパスや変数名はマスクされない"""
        code = "def authenticate(user_id: str) -> bool:\n    return db.lookup(user_id)"
        assert redact(code) == code
