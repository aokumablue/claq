"""BOM 付き UTF-16 / UTF-32 ファイルの secret 検出を検証するテスト。

Windows PowerShell 5 の `Out-File` や `.reg` の書き出しで偶発的に生まれる BOM 付きの
UTF-16 は、UTF-8 として復号すると NUL 混じりになり、印字可能連続の抽出で ASCII 1 文字
ずつに割れて secret パターンに一致しない。BOM がある場合だけ追加で復号した本文を
走査対象に足す。鍵は実行時に連結で組み立て、リポジトリ全体の自己スキャンに掛からない
ようにする。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from claq.hooks import commit_quality_scanner

_BOM_UTF8 = b"\xef\xbb\xbf"
_BOM_UTF16_LE = b"\xff\xfe"
_BOM_UTF16_BE = b"\xfe\xff"
_BOM_UTF32_LE = b"\xff\xfe\x00\x00"
_BOM_UTF32_BE = b"\x00\x00\xfe\xff"


def _key() -> str:
    """実行時に連結で組み立てた Anthropic 形式の鍵を返す。

    Returns:
        secret パターンに一致するダミーの鍵文字列。

    Raises:
        例外は発生しません。
    """
    return "sk-" + "ant-" + "api03-" + "H" * 40


def _deadline() -> float:
    """単発の `find_file_issues` に渡す満額の deadline を返す。

    Returns:
        `_monotonic()` 基準の打ち切り時刻。

    Raises:
        例外は発生しません。
    """
    return commit_quality_scanner.new_secret_scan_deadline()


def _staged(monkeypatch: pytest.MonkeyPatch, raw: bytes) -> None:
    """`git show :path` の出力を `raw` に差し替える。

    Args:
        monkeypatch: pytest の monkeypatch。
        raw: INDEX の内容として返すバイト列。

    Raises:
        例外は発生しません。
    """
    monkeypatch.setattr(
        commit_quality_scanner.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout=raw, stderr=b""),
    )


def _scan_staged(monkeypatch: pytest.MonkeyPatch, raw: bytes) -> list[dict]:
    """INDEX 側の経路で `raw` を走査する。

    Args:
        monkeypatch: pytest の monkeypatch。
        raw: INDEX の内容として返すバイト列。

    Returns:
        `find_file_issues` の結果。

    Raises:
        例外は発生しません。
    """
    _staged(monkeypatch, raw)
    return commit_quality_scanner.find_file_issues("notes.txt", deadline=_deadline())


def _scan_worktree(tmp_path: Path, raw: bytes) -> list[dict]:
    """作業ツリー側（commit -a）の経路で `raw` を走査する。

    Args:
        tmp_path: リポジトリルートに見立てるディレクトリ。
        raw: 作業ツリーのファイル内容。

    Returns:
        `find_file_issues` の結果。

    Raises:
        例外は発生しません。
    """
    (tmp_path / "notes.txt").write_bytes(raw)
    return commit_quality_scanner.find_file_issues("notes.txt", repo_root=tmp_path, deadline=_deadline())


def _secrets(issues: list[dict]) -> list[dict]:
    """issue 一覧から secret だけを取り出す。

    Args:
        issues: `find_file_issues` の結果。

    Returns:
        type が secret の issue。

    Raises:
        例外は発生しません。
    """
    return [issue for issue in issues if issue["type"] == "secret"]


_ENCODED_KEYS = {
    "utf16le": _BOM_UTF16_LE + _key().encode("utf-16-le"),
    "utf16be": _BOM_UTF16_BE + _key().encode("utf-16-be"),
    "utf32le": _BOM_UTF32_LE + _key().encode("utf-32-le"),
    "utf32be": _BOM_UTF32_BE + _key().encode("utf-32-be"),
}


@pytest.mark.parametrize("name", sorted(_ENCODED_KEYS))
def test_staged_bom_encoded_key_is_detected(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """INDEX 側: BOM 付き UTF-16 / UTF-32 の鍵が secret（error）になること。"""
    secrets = _secrets(_scan_staged(monkeypatch, _ENCODED_KEYS[name]))

    assert len(secrets) == 1
    assert secrets[0]["severity"] == "error"


@pytest.mark.parametrize("name", sorted(_ENCODED_KEYS))
def test_worktree_bom_encoded_key_is_detected(tmp_path: Path, name: str) -> None:
    """作業ツリー側（commit -a）: BOM 付き UTF-16 / UTF-32 の鍵が secret（error）になること。"""
    secrets = _secrets(_scan_worktree(tmp_path, _ENCODED_KEYS[name]))

    assert len(secrets) == 1
    assert secrets[0]["severity"] == "error"


def test_utf32le_bom_is_not_mistaken_for_utf16le() -> None:
    """FF FE 00 00 は UTF-16LE の BOM より先に UTF-32LE として判定されること。

    UTF-16LE と誤判定すると本文の先頭に U+0000 が残り、復号結果が UTF-32 の本文と
    一致しなくなる。
    """
    raw = _BOM_UTF32_LE + "abcd".encode("utf-32-le")

    assert commit_quality_scanner._decode_scan_text(raw).endswith("\nabcd")


def test_bom_only_file_has_no_issue_and_adds_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """BOM だけのファイルは問題なしで、足す本文が空なら復号結果を変えないこと。"""
    for bom in (_BOM_UTF16_LE, _BOM_UTF16_BE, _BOM_UTF32_LE, _BOM_UTF32_BE):
        assert commit_quality_scanner._decode_scan_text(bom) == bom.decode("utf-8", errors="replace")
        assert _scan_staged(monkeypatch, bom) == []
        assert _scan_worktree(tmp_path, bom) == []


@pytest.mark.parametrize(
    "bad",
    [
        _BOM_UTF16_LE + "\ud800".encode("utf-16-le", errors="surrogatepass"),
        _BOM_UTF16_BE + b"\x00",
        _BOM_UTF32_LE + b"\xff\xff\xff\xff",
    ],
    ids=["lone-surrogate", "odd-length", "out-of-range"],
)
def test_undecodable_bom_body_falls_back_to_utf8_result(monkeypatch: pytest.MonkeyPatch, bad: bytes) -> None:
    """strict 復号に失敗したら追加走査をせず、UTF-8 の復号結果だけを返すこと（例外にならない）。"""
    assert commit_quality_scanner._decode_scan_text(bad) == bad.decode("utf-8", errors="replace")

    raw = bad + b"\n" + _key().encode()
    secrets = _secrets(_scan_staged(monkeypatch, raw))

    assert len(secrets) == 1


def test_utf16_without_bom_is_not_detected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """BOM なしの UTF-16 は推定しない（対象外の現状を固定する）。"""
    raw = _key().encode("utf-16-le")

    assert commit_quality_scanner._decode_scan_text(raw) == raw.decode("utf-8", errors="replace")
    assert _secrets(_scan_staged(monkeypatch, raw)) == []
    assert _secrets(_scan_worktree(tmp_path, raw)) == []


def test_plain_utf8_is_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """通常の UTF-8 は復号結果も検出結果も変わらないこと（行番号も含む）。"""
    raw = ("# header\n" + _key() + "\n").encode()

    assert commit_quality_scanner._decode_scan_text(raw) == raw.decode("utf-8")
    for issues in (_scan_staged(monkeypatch, raw), _scan_worktree(tmp_path, raw)):
        assert [(i["type"], i["line"], i["severity"]) for i in issues] == [("secret", 2, "error")]


def test_utf8_bom_is_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """BOM 付き UTF-8（EF BB BF）は UTF-16 の BOM ではなく、復号結果も検出結果も変わらないこと。"""
    raw = _BOM_UTF8 + ("# header\n" + _key() + "\n").encode()

    assert commit_quality_scanner._decode_scan_text(raw) == raw.decode("utf-8")
    for issues in (_scan_staged(monkeypatch, raw), _scan_worktree(tmp_path, raw)):
        assert [(i["type"], i["line"], i["severity"]) for i in issues] == [("secret", 2, "error")]


def test_symlink_target_path_is_not_decoded(tmp_path: Path) -> None:
    """symlink の link 先文字列（readlink）は復号関数を通さず、そのまま返ること。"""
    (tmp_path / "link").symlink_to("target-name")

    assert commit_quality_scanner.get_worktree_file_content(tmp_path, "link") == "target-name"


class TestBudgetCoversAppendedBody:
    """足した本文の走査も同じ `_ScanBudget` の中で行われること。"""

    # UTF-8 側（NUL 混じり）からは 4 文字以上の連続が取れず、走査単位は足した本文だけになる。
    _HUGE = "a" * (1024 * 1024)

    def _install_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """最初の 1 回だけ予算内の時刻を返し、以降は超過した時刻を返す時計を差し込む。

        Args:
            monkeypatch: pytest の monkeypatch。

        Raises:
            例外は発生しません。
        """
        ticks = iter([0.0])
        monkeypatch.setattr(commit_quality_scanner, "_monotonic", lambda: next(ticks, 1_000.0))

    def test_staged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """INDEX 側: 足した本文の走査中に超過すると scan_error（error、fail-closed）になる。"""
        self._install_clock(monkeypatch)
        _staged(monkeypatch, _BOM_UTF16_LE + self._HUGE.encode("utf-16-le"))

        issues = commit_quality_scanner.find_file_issues("notes.txt", deadline=10.0)

        assert [(i["type"], i["severity"]) for i in issues] == [("scan_error", "error")]
        assert "SecretScanBudgetExceeded" in issues[0]["message"]

    def test_worktree(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """作業ツリー側: 同じく scan_error（error、fail-closed）になる。"""
        self._install_clock(monkeypatch)
        (tmp_path / "notes.txt").write_bytes(_BOM_UTF16_LE + self._HUGE.encode("utf-16-le"))

        issues = commit_quality_scanner.find_file_issues("notes.txt", repo_root=tmp_path, deadline=10.0)

        assert [(i["type"], i["severity"]) for i in issues] == [("scan_error", "error")]


@pytest.mark.parametrize("name", sorted(_ENCODED_KEYS))
def test_multiline_bom_body_is_scanned_line_by_line(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """複数行の BOM 付き本文でも、行頭アンカーの付いた YAML 形の認証情報を 2 行目以降で検出する。

    NUL を含む内容は印字可能連続の抽出経由になり、改行を含む 1 本の連続になる。行ごとに分けないと
    `^\\s*...password: value$` 形のパターンが先頭行でしか効かない。
    """
    value = "v" + "x" * 11
    text = f"name: demo\r\nport: 8080\r\napi_key: {value}\r\n"
    codec = {"utf16le": "utf-16-le", "utf16be": "utf-16-be", "utf32le": "utf-32-le", "utf32be": "utf-32-be"}[name]
    bom = {"utf16le": _BOM_UTF16_LE, "utf16be": _BOM_UTF16_BE, "utf32le": _BOM_UTF32_LE, "utf32be": _BOM_UTF32_BE}[name]

    secrets = _secrets(_scan_staged(monkeypatch, bom + text.encode(codec)))

    assert len(secrets) == 1
