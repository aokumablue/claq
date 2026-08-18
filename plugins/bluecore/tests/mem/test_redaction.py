"""redaction.py のユニットテスト"""

import pytest

from bluecore.mem.redaction import redact, redact_knowledge_text

_PLACEHOLDER = "[REDACTED]"


class TestRedact:
    """redact() のテーブル駆動テスト"""

    @pytest.mark.parametrize(
        "name, text, should_redact",
        [
            # Anthropic API キー
            ("anthropic_key", "key=" + "sk-ant-" + "api03-abcdefghijklmnopqrst1234567890ABCDEFGHIJKLMNO", True),
            # OpenAI API キー
            ("openai_key", "token = " + "sk-" + "abcdefghijklmnopqrstuvwxyz1234567890ABCD", True),
            # Slack Bot トークン
            ("slack_bot_token", "xoxb-123456789012-123456789012-abcdefghijklmnopqrstuvwx", True),
            # GitHub classic PAT
            ("github_pat_classic", "ghp_" + "abcdefghijklmnopqrstuvwxyz123456ABCD", True),
            # GitHub Fine-Grained PAT
            ("github_fine_pat", "github_pat_11ABCDE_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ", True),
            # AWS Access Key ID
            ("aws_key_id", "AKIA" + "IOSFODNN7EXAMPLE", True),
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
            # 40 桁の大文字 SHA-1 commit hash は base64_long で誤検出しない
            # （小文字は hex_secret が拾うが、大文字/混在は hex_secret 対象外）
            ("sha1_upper_commit", "DA39A3EE5E6B4B0D3255BFEF95601890AFD80709", False),
            # 40 桁の混在ケース commit hash も誤検出しない
            ("sha1_mixed_commit", "Da39A3ee5E6b4B0d3255Bfef95601890AfD80709", False),
            # 64 桁の大文字 SHA-256 hash も誤検出しない
            (
                "sha256_upper",
                "E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855",
                False,
            ),
            # base64 シークレット（32 バイト鍵 = 44 文字、非 hex 文字を含む）は依然マスクされる
            ("base64_secret_44", "PsmIacDP/C7LZ/t/GVR8rx8Sl0yQ/Wh8Mzwm6Zy/ww4=", True),
            # 非 hex 文字を含む 64 文字 base64（48 バイト鍵）も依然マスクされる
            (
                "base64_secret_64",
                "WWQjq6fc3tLz3+KfcziN7rO+GkNwpSA6d6c9Qg3HvhSFUt5gjmio8mHpIEVzVkOx",
                True,
            ),
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
        text = (
            "email=admin@example.com apikey="
            + "sk-"
            + "abcdefghijklmnopqrstuvwxyz1234567890ABCD"
            + " server=10.0.0.1"
        )
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


class TestRedactKnowledgeText:
    """redact_knowledge_text() — knowledge カード向け縮小版（hex_secret/base64_long 除外）"""

    def test_known_prefix_secrets_still_redacted(self) -> None:
        """既知プレフィックス系（password 代入等）は通常版と同様にマスクされる。"""
        result = redact_knowledge_text("password=mysecretpassword123")
        assert _PLACEHOLDER in result
        assert "mysecretpassword123" not in result

    def test_github_pat_still_redacted(self) -> None:
        result = redact_knowledge_text("ghp_" + "abcdefghijklmnopqrstuvwxyz123456ABCD")
        assert _PLACEHOLDER in result

    def test_email_still_redacted(self) -> None:
        result = redact_knowledge_text("contact: user@example.com")
        assert _PLACEHOLDER in result

    def test_hex_secret_not_redacted(self) -> None:
        """32 文字以上の16進文字列（commit SHA 等）は knowledge 用途では保持する。

        password_assign 等のキーワード付きパターンに誤って引っかからないよう、
        "token:"/"secret:" 等のキーワードを含まない素の16進文字列を使う。
        """
        text = "see commit deadbeef0123456789abcdef01234567 for details"
        assert redact_knowledge_text(text) == text

    def test_base64_long_not_redacted(self) -> None:
        """40 文字超の base64 様文字列も knowledge 用途では保持する。"""
        text = "PsmIacDP/C7LZ/t/GVR8rx8Sl0yQ/Wh8Mzwm6Zy/ww4="
        assert redact_knowledge_text(text) == text

    def test_full_commit_sha_preserved(self) -> None:
        sha = "a" * 40
        text = f"see commit {sha}"
        assert redact_knowledge_text(text) == text

    def test_empty_string(self) -> None:
        assert redact_knowledge_text("") == ""
