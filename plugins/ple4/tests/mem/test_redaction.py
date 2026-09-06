"""redaction.py のユニットテスト"""

import re
import time

import pytest

from ple4.hooks import commit_quality_scanner
from ple4.mem.redaction import _PATTERNS, redact, redact_knowledge_text

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
            ("slack_bot_token", "xox" + "b-123456789012-123456789012-abcdefghijklmnopqrstuvwx", True),
            # GitHub classic PAT
            ("github_pat_classic", "ghp_" + "abcdefghijklmnopqrstuvwxyz123456ABCD", True),
            # GitHub Fine-Grained PAT
            (
                "github_fine_pat",
                "github" + "_pat_11ABCDE_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                True,
            ),
            # AWS Access Key ID
            ("aws_key_id", "AKIA" + "IOSFODNN7EXAMPLE", True),
            # AWS Secret Key（代入形式）
            ("aws_secret_assign", "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", True),
            # Bearer トークン
            ("bearer_token", "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.abc", True),
            # password 代入
            ("password_assign", "password" + "=mysecretpassword123", True),
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
        text = "password" + "=secret123 user@example.com"
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
        result = redact_knowledge_text("password" + "=mysecretpassword123")
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
        認証情報の代入形（キーワード + 区切り + 値）に当たらない素の16進文字列を使う。
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


# --- 二次オーダー回帰（SessionEnd の実時間予算） ---

_ONE_MIB = 1024 * 1024

# `redact` を呼ぶ最も締切の厳しい経路は SessionEnd の handoff
# （`hooks/hooks.json` の `ple4.mem.cli handoff` は timeout 10 秒）。
# `tag_stripping._forges_tag` は 1 回の判定で `redact` を 2 度呼ぶため、
# 1 回あたりの実測はこの予算より十分小さくなければならない。
_SESSION_END_BUDGET_SECONDS = 10.0

# 1MiB 入力 1 回あたりに許す上限。予算の 1/5 に取り、二次オーダーへ戻れば
# 必ず超える水準にする（修正前の実測: JWT 形 約 280 秒・email 形 約 43 秒）。
_SINGLE_CALL_LIMIT_SECONDS = 2.0


def _repeat_to(unit: str, size: int) -> str:
    """*unit* を繰り返して *size* 文字前後の文字列を作る。

    Args:
        unit: 繰り返す単位文字列。
        size: 目標の文字数。

    Returns:
        *unit* を ``size // len(unit)`` 回繰り返した文字列。

    Raises:
        例外は発生しません。
    """
    return unit * (size // len(unit))


# 二次オーダーを誘発する worst-case 入力の単位文字列。
# - `eyJ` 反復は JWT パターンの開始位置を 3 文字ごとに立てる（lookbehind 前は
#   そのたびに残り全体を貪欲走査していた）。
# - 残りは「クラス内の長い連続に `\b` が多数立つ」形で、email パターンの
#   local part が開始位置ごとに残り全体を走査していた形。
_WORST_CASE_UNITS: tuple[tuple[str, str], ...] = (
    ("jwt_prefix_repeat", "eyJ"),
    ("email_local_plus", "A" * 39 + "+"),
    ("email_local_dot", "A" * 39 + "."),
    ("email_local_hyphen", "A" * 39 + "-"),
    ("at_runs", "a" * 20 + "@" + "b" * 20 + "."),
)


def _best_of(text: str, repeats: int = 3) -> float:
    """``redact(text)`` の最小実行時間を返す。

    最小値を採るのは、スケジューラ由来の上振れだけを落として測定を安定させる
    ため（`timeit` と同じ考え方）。平均や単発だと負荷の高い CI で ratio 判定が
    flaky になる。

    Args:
        text: 計測対象の入力。
        repeats: 計測回数。

    Returns:
        最小の実行時間（秒）。

    Raises:
        例外は発生しません。
    """
    return min(_timed(text) for _ in range(repeats))


def _timed(text: str) -> float:
    """``redact(text)`` を 1 回実行して所要時間を返す。

    Args:
        text: 計測対象の入力。

    Returns:
        実行時間（秒）。

    Raises:
        例外は発生しません。
    """
    start = time.perf_counter()
    redact(text)
    return time.perf_counter() - start


class TestRedactScanCost:
    """`redact` が二次オーダーへ戻らないことを実測で固定する。

    パターンの曖昧な繰り返しは、一致しない入力に対して開始位置ごとに残り全体を
    走査させるため入力長の二乗で効く。SessionEnd の handoff は timeout 10 秒で
    動くので、二次オーダーが 1 本でもあるとフックごと落ち、**redact が 1 件も
    適用されないまま全文が素通りする**（局所的な false negative より重い）。
    """

    @pytest.mark.parametrize(("name", "unit"), _WORST_CASE_UNITS)
    def test_one_mib_worst_case_fits_in_budget(self, name: str, unit: str) -> None:
        """1MiB の worst-case 入力を SessionEnd 予算内で処理し切ること。"""
        elapsed = _timed(_repeat_to(unit, _ONE_MIB))

        assert elapsed < _SINGLE_CALL_LIMIT_SECONDS, (
            f"[{name}] 1MiB の redact に {elapsed:.2f}s かかった "
            f"（上限 {_SINGLE_CALL_LIMIT_SECONDS}s / SessionEnd 予算 {_SESSION_END_BUDGET_SECONDS}s）。"
            "パターンに曖昧な繰り返しが戻っていないか確認すること。"
        )

    @pytest.mark.parametrize(("name", "unit"), _WORST_CASE_UNITS)
    def test_scan_cost_is_linear_in_input_length(self, name: str, unit: str) -> None:
        """入力 2 倍で所要時間も 2 倍程度に留まること（二次なら 4 倍になる）。

        閾値 3.0 は線形（約 2 倍）と二次（約 4 倍）の中間に取る。上振れは
        `_best_of` の最小値採用で落とす。
        """
        base = _best_of(_repeat_to(unit, 256 * 1024))
        doubled = _best_of(_repeat_to(unit, 512 * 1024))

        ratio = doubled / max(base, 1e-9)
        assert ratio < 3.0, (
            f"[{name}] 入力 2 倍で所要時間が {ratio:.1f} 倍になった"
            f"（{base:.4f}s -> {doubled:.4f}s）。二次オーダーへ戻っている。"
        )


class TestJwtPatternNarrowing:
    """JWT パターンの lookbehind による絞り込みを固定する。"""

    def test_jwt_after_delimiter_is_redacted(self) -> None:
        """区切りの直後に現れる JWT は従来どおりマスクされる。

        ローカル変数を credential 系の語（token / secret / password / api_key）
        で命名しないこと。その語にクォート付きの値を代入する行は、それ自身が
        スキャナの credential assignment パターンに一致し、リポジトリ自己走査
        テストを赤くする（実測で踏んだ）。この注意書き自体も同じ理由で、
        一致する形の実例を書かずに説明だけに留めている。
        """
        jwt = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjMifQ.s1gnatur3"
        assert redact(f"Authorization: {jwt}") == "Authorization: " + _PLACEHOLDER

    def test_jwt_glued_to_base64url_run_is_not_matched(self) -> None:
        """base64url 文字へ続く `eyJ` は一致しない（意図した絞り込み）。

        二次オーダーを断つための lookbehind の代償。JWT はトークンであり実物は
        区切りの直後に現れるため、`hooks/commit_quality_scanner.py` が先に採った
        判断へ揃えたもの。
        """
        glued = "abc" + "eyJ" + "hbGciOiJIUzI1NiJ9" + ".payload.sig"
        assert _PLACEHOLDER not in redact(glued)


def _email_pattern() -> re.Pattern[str]:
    """`_PATTERNS` から email パターンを 1 本だけ引く。

    Returns:
        コンパイル済みの email パターン。

    Raises:
        AssertionError: email パターンが 1 本に定まらない場合。
    """
    found = [pattern for name, pattern in _PATTERNS if name == "email"]
    assert len(found) == 1
    return found[0]


class TestEmailLocalPartBound:
    """email パターンの local part 長上限（RFC 5321 の 64 オクテット）を固定する。

    判定は email パターン単体に対して行う。`redact` 全体で見ると 40 文字超の
    local part は `hex_secret` / `base64_long` にも一致してしまい、email 側の
    上限が効いているかを切り分けられないため。
    """

    def test_local_part_at_bound_matches(self) -> None:
        """64 文字ちょうどの local part は一致する。"""
        assert _email_pattern().search(("a" * 64) + "@example.com") is not None

    def test_local_part_over_bound_does_not_match(self) -> None:
        """65 文字の local part は一致しない（配送不能な長さ。意図した絞り込み）。"""
        assert _email_pattern().search(("a" * 65) + "@example.com") is None

    def test_ordinary_address_still_redacted(self) -> None:
        """実在しうる長さのアドレスは `redact` 経由で従来どおりマスクされる。"""
        assert redact("contact: first.last+tag@sub.example.co.jp") == "contact: " + _PLACEHOLDER


# --- redaction と commit_quality_scanner のベンダ prefix 同期 ---


def _vendor_samples() -> tuple[tuple[str, str], ...]:
    """ベンダ prefix 系シークレットの検体を実行時に組み立てて返す。

    検体をソースへ直書きすると、このファイル自身が
    `commit_quality_scanner._SECRET_PATTERNS` に一致し、リポジトリ自己走査テスト
    （`tests/hooks/test_hook_edge_cases.py` の
    `test_repo_wide_self_scan_has_zero_secret_issues`）が赤くなる。既存の
    `_private_key_header` と同じく連結で組み立てて直書きを避ける。

    Returns:
        ``(ラベル, 検体)`` のタプル列。

    Raises:
        例外は発生しません。
    """
    return (
        ("openai", "sk-" + "abcdefghijklmnopqrstuvwxyz1234567890ABCD"),
        ("anthropic", "sk-" + "ant-" + "api03-abcdefghijklmnopqrst1234567890ABCDEFGHIJKLMNO"),
        ("github_classic_p", "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz123456ABCD"),
        ("github_classic_o", "gh" + "o_" + "abcdefghijklmnopqrstuvwxyz123456ABCD"),
        ("github_classic_s", "gh" + "s_" + "abcdefghijklmnopqrstuvwxyz123456ABCD"),
        ("github_classic_r", "gh" + "r_" + "abcdefghijklmnopqrstuvwxyz123456ABCD"),
        (
            "github_fine_pat",
            "github" + "_pat_" + "11ABCDE_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        ),
        ("slack_bot", "xox" + "b-" + "123456789012-123456789012-abcdefghijklmnopqrstuvwx"),
        ("slack_user", "xox" + "p-" + "123456789012-123456789012-abcdefghijklmnopqrstuvwx"),
        ("google_api", "AIza" + "SyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456"),
        ("aws_akia", "AKIA" + "IOSFODNN7EXAMPLE"),
        ("aws_asia", "ASIA" + "IOSFODNN7EXAMPLE"),
        ("jwt", "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjMifQ.s1gnatur3"),
    )


def _scanner_detects(sample: str) -> bool:
    """`commit_quality_scanner._SECRET_PATTERNS` のどれかが *sample* に一致するか。

    Args:
        sample: 検体文字列。

    Returns:
        1 つでも一致すれば True。

    Raises:
        例外は発生しません。
    """
    return any(re.search(pattern, sample) for pattern, _ in commit_quality_scanner._SECRET_PATTERNS)


class TestVendorPatternSync:
    """`redaction` と `commit_quality_scanner` のベンダ prefix 集合が揃っていること。

    `commit_quality_scanner._SECRET_PATTERNS` は「ベンダ prefix の集合は
    `ple4.mem.redaction._PATTERNS` と揃える」と宣言している。同じリポジトリで
    「記録時はマスクするが commit 時は検出しない」秘密（またはその逆）が生まれない
    ようにするための宣言だが、**現に片方だけが直っている状態が起きた**ため機械検査
    する（JWT の二次オーダー修正がスキャナ側にしか入っていなかった）。

    検査は「同じ検体に両者が反応するか」という振る舞いで行い、パターン文字列の
    比較はしない。両者は同じ文字集合を `[A-Za-z0-9_-]` と `[a-zA-Z0-9_-]` の
    ように違う綴りで書いており、文字列比較は綴りの差だけで落ちるため。

    集合の一致は主張しない。同期の宣言はベンダ prefix に対してのみで、
    `redaction` 側だけが持つ email / private IPv4 / hex_secret / base64_long
    （＝ログ・handoff 向けの PII とエントロピー検出。commit 差分に当てると
    誤検出で commit を壊す）と、スキャナ側だけが持つ PEM / SSH2 / PuTTY 秘密鍵
    ヘッダ（＝ファイルとして混入する形式で、自由文には現れない）は
    意図的な非対称である。
    """

    @pytest.mark.parametrize(("label", "sample"), _vendor_samples())
    def test_redaction_masks_vendor_secret(self, label: str, sample: str) -> None:
        """redaction 側がベンダ検体をマスクすること。"""
        assert redact(sample) != sample, f"[{label}] redaction が検体をマスクしていない"

    @pytest.mark.parametrize(("label", "sample"), _vendor_samples())
    def test_scanner_detects_vendor_secret(self, label: str, sample: str) -> None:
        """commit_quality_scanner 側が同じ検体を検出すること。"""
        assert _scanner_detects(sample), f"[{label}] scanner が検体を検出していない"

    @pytest.mark.parametrize(("label", "sample"), _vendor_samples())
    def test_both_sides_agree(self, label: str, sample: str) -> None:
        """同じ検体に対する 2 モジュールの判定が一致すること。"""
        assert (redact(sample) != sample) == _scanner_detects(sample), (
            f"[{label}] redaction と scanner の判定が食い違っている"
        )

    def test_jwt_narrowing_is_synchronized(self) -> None:
        """JWT の lookbehind による絞り込みが両モジュールで同じであること。

        片方だけが `eyJ` の直前に base64url 文字を許すと、二次オーダー修正が
        片側にしか入っていない状態（＝今回の指摘そのもの）を検出できる。
        """
        glued = "abc" + "eyJ" + "hbGciOiJIUzI1NiJ9" + ".payload.sig"

        assert _PLACEHOLDER not in redact(glued)
        assert not _scanner_detects(glued)

    @pytest.mark.parametrize(("name", "unit"), (("jwt_prefix_repeat", "eyJ"),))
    def test_scanner_jwt_is_also_linear(self, name: str, unit: str) -> None:
        """スキャナ側 JWT も同じ worst-case 入力で線形に留まること。"""
        jwt_patterns = [pattern for pattern, label in commit_quality_scanner._SECRET_PATTERNS if label == "JWT"]
        assert len(jwt_patterns) == 1
        compiled = re.compile(jwt_patterns[0])
        text = _repeat_to(unit, _ONE_MIB)

        start = time.perf_counter()
        compiled.sub(_PLACEHOLDER, text)
        elapsed = time.perf_counter() - start

        assert elapsed < _SINGLE_CALL_LIMIT_SECONDS, f"[{name}] スキャナ側 JWT が {elapsed:.2f}s かかった"
