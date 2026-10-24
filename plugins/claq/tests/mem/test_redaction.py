"""redaction.py のユニットテスト"""

import re
import time

import pytest

from claq.hooks import commit_quality_scanner
from claq.mem.redaction import _PATTERNS, redact, redact_knowledge_text

_PLACEHOLDER = "[REDACTED]"


class TestRedact:
    """redact() のテーブル駆動テスト（ベンダ prefix 単体の検体は TestVendorPatternSync が検査する）"""

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("key=" + "sk-ant-" + "api03-abcdefghijklmnopqrst1234567890ABCDEFGHIJKLMNO", id="anthropic_key"),
            pytest.param("token = " + "sk-" + "abcdefghijklmnopqrstuvwxyz1234567890ABCD", id="openai_key"),
            pytest.param("aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", id="aws_secret_assign"),
            pytest.param("Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.abc", id="bearer_token"),
            pytest.param("password" + "=mysecretpassword123", id="password_assign"),
            pytest.param("contact: user@example.com", id="email"),
            pytest.param("server: 192.168.1.100", id="ipv4"),
            pytest.param("token: deadbeef0123456789abcdef01234567", id="hex_secret"),
            # 非 hex 文字を含む base64 シークレット（32 バイト鍵 = 44 文字 / 48 バイト鍵 = 64 文字）
            pytest.param("PsmIacDP/C7LZ/t/GVR8rx8Sl0yQ/Wh8Mzwm6Zy/ww4=", id="base64_secret_44"),
            pytest.param("WWQjq6fc3tLz3+KfcziN7rO+GkNwpSA6d6c9Qg3HvhSFUt5gjmio8mHpIEVzVkOx", id="base64_secret_64"),
        ],
    )
    def test_secret_is_masked(self, text: str) -> None:
        """シークレット・PII を含むテキストは [REDACTED] が入り、元の文字列が残らない。"""
        result = redact(text)

        assert _PLACEHOLDER in result
        assert text not in result

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("今日は良い天気です。コードをリファクタリングしました。", id="plain_text"),
            pytest.param("/home/user/dev/project/src/main.py", id="file_path"),
            pytest.param("deadbeef01234567", id="short_hex"),
            # 大文字/混在の commit hash は hex_secret の対象外で、base64_long の先読みでも誤検出しない
            pytest.param("DA39A3EE5E6B4B0D3255BFEF95601890AFD80709", id="sha1_upper_commit"),
            pytest.param("Da39A3ee5E6b4B0d3255Bfef95601890AfD80709", id="sha1_mixed_commit"),
            pytest.param("E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855", id="sha256_upper"),
            pytest.param("", id="empty"),
            pytest.param("def authenticate(user_id: str) -> bool:\n    return db.lookup(user_id)", id="code_structure"),
        ],
    )
    def test_ordinary_text_is_unchanged(self, text: str) -> None:
        """通常テキスト・パス・短い hex・大文字の hash・コードは変更されない。"""
        assert redact(text) == text

    @pytest.mark.parametrize(
        "text, expected",
        [
            # 区切りの直後に現れる JWT（JWT の lookbehind は区切りの直後なら一致する）
            pytest.param(
                "Authorization: " + "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjMifQ.s1gnatur3",
                "Authorization: " + _PLACEHOLDER,
                id="jwt_after_delimiter",
            ),
            # 実在しうる長さのアドレス（email の local part 上限の内側）
            pytest.param("contact: first.last+tag@sub.example.co.jp", "contact: " + _PLACEHOLDER, id="ordinary_email"),
        ],
    )
    def test_exact_output(self, text: str, expected: str) -> None:
        """一致した部分だけが [REDACTED] に置き換わる。"""
        assert redact(text) == expected

    def test_multiple_secrets_in_one_text(self) -> None:
        """複数シークレットが混在するテキストを全てマスクする"""
        text = "email=admin@example.com apikey=" + "sk-" + "abcdefghijklmnopqrstuvwxyz1234567890ABCD" + " server=10.0.0.1"
        assert redact(text).count(_PLACEHOLDER) >= 3

    def test_idempotent(self) -> None:
        """2 回適用しても結果が変わらない"""
        once = redact("password" + "=secret123 user@example.com")
        assert redact(once) == once


class TestRedactKnowledgeText:
    """redact_knowledge_text() — knowledge カード向け縮小版（hex_secret/base64_long 除外）"""

    @pytest.mark.parametrize(
        "text, leaked",
        [
            pytest.param("password" + "=mysecretpassword123", "mysecretpassword123", id="password_assign"),
            pytest.param("ghp_" + "abcdefghijklmnopqrstuvwxyz123456ABCD", "abcdefghijklmnopqrstuvwxyz123456ABCD", id="github_pat"),
            pytest.param("contact: user@example.com", "user@example.com", id="email"),
        ],
    )
    def test_known_prefix_secrets_still_redacted(self, text: str, leaked: str) -> None:
        """既知プレフィックス・キーワード系は通常版と同様にマスクされる。"""
        result = redact_knowledge_text(text)

        assert _PLACEHOLDER in result
        assert leaked not in result

    @pytest.mark.parametrize(
        "text",
        [
            # 認証情報の代入形に当たらない素の 16 進文字列（commit SHA 等）
            pytest.param("see commit deadbeef0123456789abcdef01234567 for details", id="hex_in_prose"),
            pytest.param("PsmIacDP/C7LZ/t/GVR8rx8Sl0yQ/Wh8Mzwm6Zy/ww4=", id="base64_long"),
            pytest.param("see commit " + "a" * 40, id="full_commit_sha"),
            pytest.param("sha deadbeef0123456789abcdef01234567 key PsmIacDP/C7LZ/t/GVR8rx8Sl0yQ/Wh8Mzwm6Zy/ww4=", id="hex_and_base64"),
            pytest.param("", id="empty"),
        ],
    )
    def test_entropy_strings_are_kept(self, text: str) -> None:
        """キー名を伴わない 32 桁以上の hex・40 文字超の base64 は knowledge 用途では保持する。"""
        assert redact_knowledge_text(text) == text


def _kv(key: str, sep: str, value: str, *, quote_key: str = "", quote_value: str = "") -> str:
    """キー・区切り・値から代入形の文字列を組み立てる。

    リポジトリ自己走査（commit 品質フック）が credential 代入として検出しないよう、
    リテラルでは書かず実行時に連結する。
    """
    return f"{quote_key}{key}{quote_key}{sep}{quote_value}{value}{quote_value}"


_ASSIGN_VALUE = "hunter2" + "secretvalue"

# (説明, キー, 区切り, キーを囲む引用符, 値を囲む引用符)
_ASSIGN_SHAPES = [
    ("JSON", "pass" + "word", ": ", '"', '"'),
    ("JSON 空白なし", "pass" + "word", ":", '"', '"'),
    ("dict repr", "pass" + "word", ": ", "'", "'"),
    ("JSON api_key", "api" + "_key", ":", '"', '"'),
    ("JSON access_token", "access_" + "token", ": ", '"', '"'),
    ("env SECRET_KEY", "SECRET" + "_KEY", "=", "", ""),
    ("env SECRET_KEY 引用符付き値", "SECRET" + "_KEY", "=", "", '"'),
    ("env SECRET_KEY_BASE", "SECRET" + "_KEY_BASE", "=", "", ""),
    ("env DB_PASSWORD", "DB_PASS" + "WORD", "=", "", ""),
    ("yaml ハイフン区切り", "secret" + "-key", ": ", "", ""),
    ("従来形", "pass" + "word", "=", "", ""),
    # AWS の秘密鍵（`_ACCESS` 接続語）。接頭辞（`AWS_` / `MY_APP_`）はキー語の左に
    # 語境界を要求しないため、そのまま通る。
    ("env AWS_SECRET_ACCESS_KEY", "AWS_SECRET" + "_ACCESS_KEY", "=", "", ""),
    ("env 多段接頭辞", "MY_APP_AWS_SECRET" + "_ACCESS_KEY", "=", "", ""),
    ("env 接頭辞なし", "SECRET" + "_ACCESS_KEY", "=", "", ""),
    ("env ダンプ形（コロン）", "SECRET" + "_ACCESS_KEY", ": ", "", ""),
    ("~/.aws/credentials ini 形", "aws_secret" + "_access_key", " = ", "", ""),
    ("JSON aws_secret_access_key", "aws_secret" + "_access_key", ": ", '"', '"'),
    # エスケープ済み JSON（JSON 文字列の中に入れた JSON。引用符が `\"` になる）。
    ("エスケープ済み JSON", "pass" + "word", ": ", '\\"', '\\"'),
    ("エスケープ済み JSON 空白なし", "pass" + "word", ":", '\\"', '\\"'),
    ("エスケープ済み JSON aws", "aws_secret" + "_access_key", ": ", '\\"', '\\"'),
    # 添字代入（`os.environ["KEY"] = "…"`）。キーの閉じ引用符と `=` の間に `]` が入る。
    ("添字代入 aws", "AWS_SECRET" + "_ACCESS_KEY", "] = ", '"', '"'),
    ("添字代入 aws 空白なし", "AWS_SECRET" + "_ACCESS_KEY", "]=", '"', '"'),
    ("添字代入 aws 単引用符", "AWS_SECRET" + "_ACCESS_KEY", "] = ", "'", "'"),
    ("添字代入 password", "pass" + "word", "] = ", '"', '"'),
    ("添字代入 secret_key", "SECRET" + "_KEY", "]: ", '"', '"'),
    ("添字代入 エスケープ済み", "AWS_SECRET" + "_ACCESS_KEY", "] = ", '\\"', '\\"'),
]

# 識別子の一部に credential 語を含むだけで、代入値が秘密ではない形。
_NOT_A_SECRET_ASSIGNMENTS = [
    "pass" + "wordless: authentication-enabled",
    "reset_pass" + "word_url = https://example.invalid/reset",
    "secret" + "ary: someone-with-long-name",
    "token" + "izer: bert-base-multilingual-cased",
    "max_" + "tokens: 4096000000",
    "SECRET" + "_KEYS = production-key-list",
    '"token' + '_type": "bearer-style-name"',
    "pass" + "word_policy: at-least-twelve-chars",
    # 引用符付きキーに続く値が引用符なし（コード式）なら、文字列リテラルの秘密ではない。
    'D_ENV = {"FACADE_CURSOR_' + 'SECRET": secrets.token_hex(32)}',
    '{"pass' + 'word": get_password_from_vault()}',
    # 普通のプロパティ名・識別子（値が引用符付きでも空でも伏せない）。
    'access_token_' + 'expires_in: "aaaaaaaaaaaa"',
    "access_token_" + "expires_in: 3600000000",
    'pass' + 'wordless: "aaaaaaaaaaaa"',
    'secret' + 'ary: ""',
    'token' + 'izer: ',
    "SECRET" + "_KEYS: 'aaaaaaaaaaaa'",
    'pass' + 'word_policy: ""',
    # `_access` は `_access_key` の組でだけ接続語になる。単独の `*_access:` は普通のプロパティ。
    "token" + "_access: restricted-to-admins",
    "secret" + "_access: allowed_for_everyone",
    "pass" + "word_access: readonly_everywhere",
    "api_key" + "_access: enabled_for_all",
    "token" + "_access_level: restricted-to-admins",
    # コード上の参照（値ではない）。
    'os.environ["AWS_SECRET' + '_ACCESS_KEY"]',
    "os.environ['AWS_SECRET" + "_ACCESS_KEY']",
    "os.getenv('AWS_SECRET" + "_ACCESS_KEY')",
    # 添字の参照・比較・式（代入でない、または値が文字列リテラルでない）は伏せない。
    'os.environ["AWS_SECRET' + '_ACCESS_KEY"] == "aaaaaaaaaaaa"',
    'if cfg["pass' + 'word"] == "aaaaaaaaaaaa": pass',
    'os.environ["AWS_SECRET' + '_ACCESS_KEY"] = os.getenv("aaaaaaaaaaaa")',
    'cfg["pass' + 'word"] = get_password_from_vault()',
    'cfg["token' + '_type"] = "bearer-style-name"',
    'cfg["pass' + 'wordless"] = "aaaaaaaaaaaa"',
    'x = os.environ["AWS_SECRET' + '_ACCESS_KEY"] or "aaaaaaaaaaaa"',
    # エスケープ済み JSON でも、値が引用符で始まらない式・識別子は伏せない。
    '{\\"pass' + 'word\\": get_password_from_vault()}',
    '{\\"token' + '_type\\": \\"bearer-style-name\\"}',
    # 値が 8 文字未満なら伏せない。
    _kv("pass" + "word", ": ", "short", quote_key='"', quote_value='"'),
]


class TestCredentialAssignmentShapes:
    """credential 代入パターンの形（引用符付きキー・接続語）を固定する。"""

    @pytest.mark.parametrize("fn", [redact, redact_knowledge_text])
    @pytest.mark.parametrize(("label", "key", "sep", "quote_key", "quote_value"), _ASSIGN_SHAPES)
    def test_value_is_masked(
        self, fn, label: str, key: str, sep: str, quote_key: str, quote_value: str
    ) -> None:
        """キー直後が引用符・`_KEY` 等の接続語でも、値はマスクされる。"""
        text = _kv(key, sep, _ASSIGN_VALUE, quote_key=quote_key, quote_value=quote_value)
        result = fn(f"cfg {text} end")
        assert _ASSIGN_VALUE not in result, label
        assert _PLACEHOLDER in result, label

    @pytest.mark.parametrize("fn", [redact, redact_knowledge_text])
    @pytest.mark.parametrize("text", _NOT_A_SECRET_ASSIGNMENTS)
    def test_identifier_containing_credential_word_is_kept(self, fn, text: str) -> None:
        """credential 語を識別子の一部に含むだけの代入や、8 文字未満の値は伏せない。"""
        assert fn(text) == text


# --- 二次オーダー回帰（SessionEnd の実時間予算） ---

_ONE_MIB = 1024 * 1024

# `redact` を呼ぶ最も締切の厳しい経路は SessionEnd の handoff
# （`hooks/hooks.json` の `claq.mem.cli handoff` は timeout 10 秒）。
# `tag_stripping._forges_tag` は 1 回の判定で `redact` を 2 度呼ぶため、
# 1 回あたりの実測はこの予算より十分小さくなければならない。
_SESSION_END_BUDGET_SECONDS = 10.0

# 1MiB 入力 1 回あたりに許す上限。予算の 1/5 に取り、二次オーダーへ戻れば
# 必ず超える水準にする（二次オーダーでの実測: JWT 形 約 280 秒・email 形 約 43 秒）。
_SINGLE_CALL_LIMIT_SECONDS = 2.0


def _repeat_to(unit: str, size: int) -> str:
    """*unit* を ``size // len(unit)`` 回繰り返して *size* 文字前後の文字列を作る。"""
    return unit * (size // len(unit))


# 二次オーダーを誘発する worst-case 入力の単位文字列。
# - `eyJ` 反復は JWT パターンの開始位置を 3 文字ごとに立てる（lookbehind が
#   無いとそのたびに残り全体を貪欲走査する）。
# - 残りは「クラス内の長い連続に `\b` が多数立つ」形で、email パターンの
#   local part が無界なら開始位置ごとに残り全体を走査する形。
_WORST_CASE_UNITS: tuple[tuple[str, str], ...] = (
    ("jwt_prefix_repeat", "eyJ"),
    ("email_local_plus", "A" * 39 + "+"),
    ("email_local_dot", "A" * 39 + "."),
    ("email_local_hyphen", "A" * 39 + "-"),
    ("at_runs", "a" * 20 + "@" + "b" * 20 + "."),
    # credential 代入の接続語（`_KEY` / `_SECRET` …）が無界だと、キー語ごとに
    # 開始位置が立ち、そのたびに連鎖の末尾まで走査して二次オーダーになる。
    ("assign_connector_key", "secret_key_"),
    ("assign_connector_hyphen", "token-secret-"),
    ("assign_connector_access", "secret_access_key_"),
    # 接頭辞（`AWS_` / `a_`）の繰り返し。キー語の左は無制限に走査し得る位置なので、
    # 接頭辞を許すパターンが二次オーダーにならないことを固定する。
    ("assign_prefix_aws", "AWS_"),
    ("assign_prefix_short", "a_"),
    ("assign_prefix_then_key", "AWS_" * 8 + "SECRET_ACCESS_KEY"),
    ("assign_escaped_quote", 'password\\"'),
    # 閉じ引用符と `]` の直後に `=` が来ない形（添字の参照）。
    ("assign_bracket_close", 'password"]'),
    ("assign_bracket_close_spaced", 'password"]    '),
    ("assign_bracket_close_aws", 'AWS_SECRET_ACCESS_KEY"] '),
)


def _timed(text: str) -> float:
    """``redact(text)`` を 1 回実行して所要時間（秒）を返す。"""
    start = time.perf_counter()
    redact(text)
    return time.perf_counter() - start


def _best_of(text: str, repeats: int = 3) -> float:
    """``redact(text)`` の最小実行時間を返す（スケジューラ由来の上振れだけを落とし、ratio 判定を安定させる）。"""
    return min(_timed(text) for _ in range(repeats))


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

    @pytest.mark.parametrize("filler", [" ", "\t", "\u3000", '"', "]", '"]'])
    def test_one_mib_after_a_single_key_fits_in_budget(self, filler: str) -> None:
        """キー語 1 個の後ろに 1MiB の空白・引用符・`]` が続き `=` が無くても予算内で終わること。

        添字代入の `]` と空白は、キー語の直後で 1 回ずつ読む。連なりに量指定子が重なると
        `=` の無い 1 個のキー語から二次オーダーで走査する。
        """
        text = "AWS_SECRET" + "_ACCESS_KEY" + '"]' + filler * (_ONE_MIB // len(filler))

        elapsed = _timed(text)

        assert elapsed < _SINGLE_CALL_LIMIT_SECONDS, f"1MiB の redact に {elapsed:.2f}s かかった（filler={filler!r}）"


def _email_pattern() -> re.Pattern[str]:
    """`_PATTERNS` から email パターンを 1 本だけ引く（1 本に定まらなければ AssertionError）。"""
    found = [pattern for name, pattern in _PATTERNS if name == "email"]
    assert len(found) == 1
    return found[0]


class TestEmailLocalPartBound:
    """email パターンの local part 長上限（RFC 5321 の 64 オクテット）を固定する。

    判定は email パターン単体に対して行う。`redact` 全体で見ると 40 文字超の
    local part は `hex_secret` / `base64_long` にも一致してしまい、email 側の
    上限が効いているかを切り分けられないため。実在しうる長さのアドレスが
    `redact` 経由でマスクされることは `TestRedact.test_exact_output` が見る。
    """

    @pytest.mark.parametrize(
        "local_length, matches",
        [
            pytest.param(64, True, id="at-bound"),
            # 配送不能な長さ。意図した絞り込み
            pytest.param(65, False, id="over-bound"),
        ],
    )
    def test_local_part_length_bound(self, local_length: int, matches: bool) -> None:
        """64 文字ちょうどの local part は一致し、65 文字は一致しない。"""
        assert (_email_pattern().search("a" * local_length + "@example.com") is not None) is matches


# --- redaction と commit_quality_scanner のベンダ prefix 同期 ---

# 検体をソースへ直書きすると、このファイル自身が `commit_quality_scanner._SECRET_PATTERNS` に一致し、
# リポジトリ自己走査テスト（`tests/hooks/test_hook_edge_cases.py` の
# `test_repo_wide_self_scan_has_zero_secret_issues`）が赤くなるため、連結で組み立てる。
_VENDOR_SAMPLES: tuple[tuple[str, str], ...] = (
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
    """`commit_quality_scanner._SECRET_PATTERNS` のどれかが *sample* に一致するか。"""
    return any(re.search(pattern, sample) for pattern, _ in commit_quality_scanner._SECRET_PATTERNS)


class TestVendorPatternSync:
    """`redaction` と `commit_quality_scanner` のベンダ prefix 集合が揃っていること。

    `commit_quality_scanner._SECRET_PATTERNS` は「ベンダ prefix の集合は
    `claq.mem.redaction._PATTERNS` と揃える」と宣言している。同じリポジトリで
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

    @pytest.mark.parametrize(("label", "sample"), _VENDOR_SAMPLES)
    def test_both_sides_catch_vendor_secret(self, label: str, sample: str) -> None:
        """redaction 側は検体全体を [REDACTED] に置き換え、scanner 側も同じ検体を検出すること。"""
        assert redact(sample) == _PLACEHOLDER, f"[{label}] redaction が検体をマスクしていない"
        assert _scanner_detects(sample), f"[{label}] scanner が検体を検出していない"

    def test_jwt_narrowing_is_synchronized(self) -> None:
        """base64url 文字へ続く `eyJ` は両モジュールとも一致しない（JWT の lookbehind による絞り込みが同じ）。

        二次オーダーを断つための lookbehind の代償で、JWT はトークンであり実物は区切りの直後に
        現れるため意図した絞り込み。片方だけが `eyJ` の直前に base64url 文字を許すと、
        二次オーダー修正が片側にしか入っていない状態を検出できる。
        """
        glued = "abc" + "eyJ" + "hbGciOiJIUzI1NiJ9" + ".payload.sig"

        assert _PLACEHOLDER not in redact(glued)
        assert not _scanner_detects(glued)

    def test_scanner_jwt_is_also_linear(self) -> None:
        """スキャナ側 JWT も同じ worst-case 入力（`eyJ` の反復）で線形に留まること。"""
        jwt_patterns = [pattern for pattern, label in commit_quality_scanner._SECRET_PATTERNS if label == "JWT"]
        assert len(jwt_patterns) == 1
        compiled = re.compile(jwt_patterns[0])
        text = _repeat_to("eyJ", _ONE_MIB)

        start = time.perf_counter()
        compiled.sub(_PLACEHOLDER, text)
        elapsed = time.perf_counter() - start

        assert elapsed < _SINGLE_CALL_LIMIT_SECONDS, f"[jwt_prefix_repeat] スキャナ側 JWT が {elapsed:.2f}s かかった"
