"""BOM 付き UTF-16 / UTF-32 ファイルの secret 検出を検証するテスト。

Windows PowerShell 5 の `Out-File` や `.reg` の書き出しで偶発的に生まれる BOM 付きの
UTF-16 は、UTF-8 として復号すると NUL 混じりになり、印字可能連続の抽出で ASCII 1 文字
ずつに割れて secret パターンに一致しない。BOM がある場合だけ追加で復号した本文を
走査対象に足す。鍵は実行時に連結で組み立て、リポジトリ全体の自己スキャンに掛からない
ようにする。
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from claq.hooks import commit_quality_scanner

_BOM_UTF8 = b"\xef\xbb\xbf"
_BOM_UTF16_LE = b"\xff\xfe"
_BOM_UTF16_BE = b"\xfe\xff"
_BOM_UTF32_LE = b"\xff\xfe\x00\x00"
_BOM_UTF32_BE = b"\x00\x00\xfe\xff"
# BOM 名 → (BOM, 本文のコーデック)
_BOMS = {
    "utf16le": (_BOM_UTF16_LE, "utf-16-le"),
    "utf16be": (_BOM_UTF16_BE, "utf-16-be"),
    "utf32le": (_BOM_UTF32_LE, "utf-32-le"),
    "utf32be": (_BOM_UTF32_BE, "utf-32-be"),
}
_KEY = "sk-" + "ant-" + "api03-" + "H" * 40
# 行頭アンカーの付いた YAML 形の認証情報を 3 行目に置いた複数行の本文。
_MULTILINE = "name: demo\r\nport: 8080\r\napi_key: v" + "x" * 11 + "\r\n"
# strict 復号に失敗する BOM 付き本文（孤立サロゲート・奇数長・範囲外の符号位置）。
_UNDECODABLE = {
    "lone-surrogate": _BOM_UTF16_LE + "\ud800".encode("utf-16-le", errors="surrogatepass"),
    "odd-length": _BOM_UTF16_BE + b"\x00",
    "out-of-range": _BOM_UTF32_LE + b"\xff\xff\xff\xff",
}
_PLAIN = ("# header\n" + _KEY + "\n").encode()


def _secret_in_binary(name: str) -> list[tuple[str, int, str, str]]:
    """印字可能連続から検出した secret 1 件の期待値（行番号は報告しない）を返す。"""
    return [("secret", 0, "error", f"Potential {name} exposed in extracted binary content")]


_AT_LINE_2 = [("secret", 2, "error", "Potential Anthropic API key exposed at line 2")]


@pytest.fixture(params=["staged", "worktree"])
def scan(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., list[dict]]:
    """INDEX 側と作業ツリー側（commit -a）の各経路で bytes を ``notes.txt`` として走査する関数を返す。"""

    def _scan(raw: bytes, deadline: float | None = None) -> list[dict]:
        """``raw`` を走査し ``find_file_issues`` の結果を返す（deadline 省略時は満額の予算）。"""
        if deadline is None:
            deadline = commit_quality_scanner.new_secret_scan_deadline()
        if request.param == "worktree":
            (tmp_path / "notes.txt").write_bytes(raw)
            return commit_quality_scanner.find_file_issues("notes.txt", repo_root=tmp_path, deadline=deadline)
        monkeypatch.setattr(
            commit_quality_scanner.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout=raw, stderr=b""),
        )
        return commit_quality_scanner.find_file_issues("notes.txt", deadline=deadline)

    return _scan


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        *[(bom + _KEY.encode(codec), _secret_in_binary("Anthropic API key")) for bom, codec in _BOMS.values()],
        # 複数行でも行ごとに分けて走査し、`^\s*...: value$` 形のパターンが 3 行目で効くこと。
        *[(bom + _MULTILINE.encode(codec), _secret_in_binary("credential assignment")) for bom, codec in _BOMS.values()],
        *[(bom, []) for bom, _codec in _BOMS.values()],
        # strict 復号に失敗したら追加走査をせず、UTF-8 側の結果だけで検出する（例外にならない）。
        *[(bad + b"\n" + _KEY.encode(), _secret_in_binary("Anthropic API key")) for bad in _UNDECODABLE.values()],
        # BOM なしの UTF-16 は推定しない（対象外の現状を固定する）。
        (_KEY.encode("utf-16-le"), []),
        # 通常の UTF-8 と BOM 付き UTF-8（EF BB BF）は検出結果も行番号も変わらない。
        (_PLAIN, _AT_LINE_2),
        (_BOM_UTF8 + _PLAIN, _AT_LINE_2),
    ],
    ids=[
        *[f"key-{name}" for name in _BOMS],
        *[f"multiline-{name}" for name in _BOMS],
        *[f"bom-only-{name}" for name in _BOMS],
        *[f"undecodable-{name}" for name in _UNDECODABLE],
        "utf16-without-bom",
        "plain-utf8",
        "utf8-bom",
    ],
)
def test_scan_result(scan: Callable[..., list[dict]], raw: bytes, expected: list[tuple[str, int, str, str]]) -> None:
    """INDEX 側・作業ツリー側とも、BOM の種類と本文ごとに期待どおりの issue だけが出ること。"""
    issues = scan(raw)

    assert [(i["type"], i["line"], i["severity"], i["message"]) for i in issues] == expected


@pytest.mark.parametrize(
    "raw",
    [
        *[bom for bom, _codec in _BOMS.values()],
        *_UNDECODABLE.values(),
        _KEY.encode("utf-16-le"),
        _PLAIN,
        _BOM_UTF8 + _PLAIN,
    ],
    ids=[*[f"bom-only-{name}" for name in _BOMS], *_UNDECODABLE, "utf16-without-bom", "plain-utf8", "utf8-bom"],
)
def test_decode_adds_nothing_without_decodable_bom_body(raw: bytes) -> None:
    """足す本文が無い・strict 復号できない・BOM が無い入力では、復号結果が UTF-8 の復号と一致すること。"""
    assert commit_quality_scanner._decode_scan_text(raw) == raw.decode("utf-8", errors="replace")


def test_utf32le_bom_is_not_mistaken_for_utf16le() -> None:
    """FF FE 00 00 は UTF-16LE の BOM より先に UTF-32LE として判定されること。

    UTF-16LE と誤判定すると本文の先頭に U+0000 が残り、復号結果が UTF-32 の本文と
    一致しなくなる。
    """
    raw = _BOM_UTF32_LE + "abcd".encode("utf-32-le")

    assert commit_quality_scanner._decode_scan_text(raw).endswith("\nabcd")


def test_budget_covers_appended_body(scan: Callable[..., list[dict]], monkeypatch: pytest.MonkeyPatch) -> None:
    """足した本文の走査中に予算を超えると scan_error（error、fail-closed）になること。

    UTF-8 側（NUL 混じり）からは 4 文字以上の連続が取れず、走査単位は足した本文だけになる。
    時計は最初の 1 回だけ予算内の時刻を返し、以降は超過した時刻を返す。
    """
    ticks = iter([0.0])
    monkeypatch.setattr(commit_quality_scanner, "_monotonic", lambda: next(ticks, 1_000.0))

    issues = scan(_BOM_UTF16_LE + ("a" * (1024 * 1024)).encode("utf-16-le"), deadline=10.0)

    assert [(i["type"], i["severity"]) for i in issues] == [("scan_error", "error")]
    assert "SecretScanBudgetExceeded" in issues[0]["message"]
