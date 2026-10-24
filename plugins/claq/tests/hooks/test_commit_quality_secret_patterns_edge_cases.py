"""claq.hooks.commit_quality_scanner の secret パターン検出のテスト（秘密鍵ヘッダ・ベンダ形式・通常コードの誤検出防止・JWT パターンの線形性）。

鍵・ヘッダ・JWT のフィクスチャは必ず実行時に連結して組み立てる。完成形を literal で書くと
このテストファイル自身が `_SECRET_PATTERNS` に一致し、自己走査テスト
（`test_repo_wide_self_scan_has_zero_secret_issues`）が赤くなる。
"""

from __future__ import annotations

import re
import time

import pytest

from claq.hooks import commit_quality_scanner as commit_quality_scanner

_PEM_DELIMITER = "-" * 5
_SSH_COM_DELIMITER = "-" * 4
_PPK_HEADER_PREFIX = "PuTTY-User-Key-File"


def _scan_line(line: str) -> list[tuple[str, int, str, str]]:
    """``line`` を前後の無害な行に挟んで走査し、(type, line, severity, message) の一覧を返す。"""
    lines = ["prefix", line, "suffix"]
    issues = commit_quality_scanner._scan_secret_issues(
        "\n".join(lines), lines, deadline=commit_quality_scanner.new_secret_scan_deadline()
    )
    return [(issue["type"], issue["line"], issue["severity"], issue["message"]) for issue in issues]


@pytest.mark.parametrize(
    ("line", "names"),
    [
        # 秘密鍵ブロックのヘッダ行。API キー形式だけを見ると staged の `id_rsa` が
        # 「PASS: All checks passed!」で通ってしまう。
        *[
            (f"  {_PEM_DELIMITER}BEGIN {kind}{_PEM_DELIMITER}", ["private key"])
            for kind in [
                "RSA PRIVATE KEY",
                "DSA PRIVATE KEY",
                "EC PRIVATE KEY",
                "OPENSSH PRIVATE KEY",
                "ENCRYPTED PRIVATE KEY",
                "PRIVATE KEY",
                "PGP PRIVATE KEY BLOCK",
                "SSH2 PRIVATE KEY",
                "SSH2 ENCRYPTED PRIVATE KEY",
            ]
        ],
        # ssh.com（4 ハイフン + 空白）と PuTTY .ppk（版番号は文字クラスで v2 / v3 の双方）は PEM の
        # 5 ハイフン交替では拾えない形なので別パターン。
        (f"{_SSH_COM_DELIMITER} BEGIN SSH2 PRIVATE KEY {_SSH_COM_DELIMITER}", ["SSH2 private key"]),
        (f"{_SSH_COM_DELIMITER} BEGIN SSH2 ENCRYPTED PRIVATE KEY {_SSH_COM_DELIMITER}", ["SSH2 private key"]),
        (f"{_PPK_HEADER_PREFIX}-2: ssh-rsa", ["PuTTY private key"]),
        (f"{_PPK_HEADER_PREFIX}-3: ssh-ed25519", ["PuTTY private key"]),
        # `claq.mem.redaction` が既にマスクする形式。記録時はマスクするのに commit 時は素通りする
        # 秘密が生まれないよう、ベンダ prefix の集合を両者で揃えている。
        ("sk-" + "proj-" + "A" * 40, ["OpenAI API key"]),
        # `sk-ant-` は OpenAI 形と二重計上しない。
        ("sk-" + "ant-" + "B" * 40, ["Anthropic API key"]),
        ("sk-" + "ant-" + "api03-" + "H" * 40, ["Anthropic API key"]),
        ("gho_" + "C" * 36, ["GitHub PAT"]),
        ("github" + "_pat_" + "D" * 60, ["GitHub fine-grained PAT"]),
        ("xox" + "b-1234567890-" + "E" * 24, ["Slack token"]),
        ("AIza" + "F" * 35, ["Google API key"]),
        ("ASIA" + "G" * 16, ["AWS Access Key"]),
        ("eyJ" + "abc.def.ghi", ["JWT"]),
        ("Authorization: Bearer " + "eyJ" + "hbGciOiJSUzI1NiJ9." + "eyJ" + "zdWIiOiIxIn0.sig", ["JWT"]),
        ('token = "' + "eyJ" + 'a.b.c"', ["JWT", "credential assignment"]),
        ("API" + "_KEY=abcdef123456", ["credential assignment"]),
        ("  api" + "_key: abcdef123456", ["credential assignment"]),
        ("password" + "=hunter2hunter2", ["credential assignment"]),
    ],
)
def test_scan_secret_issues_detects_secret_line(line: str, names: list[str]) -> None:
    """秘密鍵ヘッダ・ベンダ形式・JWT・認証情報の代入は、その行の error の secret として各パターン 1 件ずつ検出されること。"""
    assert _scan_line(line) == [("secret", 2, "error", f"Potential {name} exposed at line 2") for name in names]


@pytest.mark.parametrize(
    "line",
    [
        # 公開物のヘッダ・散文の言及・デリミタ不足の行。
        f"{_PEM_DELIMITER}BEGIN CERTIFICATE{_PEM_DELIMITER}",
        f"{_PPK_HEADER_PREFIX} という形式がある",
        f"{_PEM_DELIMITER}BEGIN PUBLIC KEY{_PEM_DELIMITER}",
        "BEGIN PRIVATE KEY という表記について説明する",
        f"{_PEM_DELIMITER}BEGIN RSA PRIVATE KEY----",
        # 通常のコード代入・docstring。クォートを単純に任意化すると `token = segment[index]` のような
        # 行まで拾い、commit をブロックするフックとしては可用性が壊れる（実測で自リポジトリの
        # 自己走査が 23 件の通常 Python 行に反応した）。
        "token = segment[index]",
        "        token: 検査対象のトークン。",
        'outside_secret = tmp_path / "outside_secret.txt"',
        "const apiKey = JSON.parse(secret.SecretString).key;",
        '"token=" + "value"',
    ],
)
def test_scan_secret_issues_ignores_non_secret_line(line: str) -> None:
    """秘密鍵でないヘッダ・散文・通常のコード代入では発火しないこと。"""
    assert _scan_line(line) == []


class TestJwtPatternIsLinear:
    """JWT パターンが入力長に対して線形であること。

    `eyJ[a-zA-Z0-9_-]+\\.` のような形は `eyJeyJeyJ…` に対して 3 バイトごとに
    開始位置が立ち、そのたびに残り全体を貪欲に取ってから後退するため二次
    オーダーになる（実測: 24,000 文字で 0.50s。1MiB へ外挿すると 15 分超で、
    `pre_bash_commit_quality` の hook timeout 30 秒を桁で超える）。
    """

    _CEILING_SECONDS = 2.0

    @staticmethod
    def _jwt_pattern() -> str:
        """`_SECRET_PATTERNS` から JWT パターンを引く。"""
        return next(pattern for pattern, name in commit_quality_scanner._SECRET_PATTERNS if name == "JWT")

    def test_adversarial_repeat_returns_quickly(self) -> None:
        """`eyJ` の反復（最悪ケース）が上限時間内に返る。"""
        text = "eyJ" * 400_000

        started = time.monotonic()
        match = re.search(self._jwt_pattern(), text, re.IGNORECASE)
        elapsed = time.monotonic() - started

        assert match is None
        assert elapsed < self._CEILING_SECONDS, f"JWT pattern took {elapsed:.3f}s (二次オーダーへ回帰した疑い)"

    def test_jwt_embedded_in_longer_token_run_is_not_matched(self) -> None:
        """base64url 文字に続く `eyJ` は一致しない（意図した絞り込み）。

        線形化のための lookbehind の代償。JWT はトークンであり実物は区切りの
        直後に現れるため、この絞り込みは受け入れる。
        """
        embedded = "abc" + "eyJ" + "hbGci.x.y"

        assert re.search(self._jwt_pattern(), embedded, re.IGNORECASE) is None
