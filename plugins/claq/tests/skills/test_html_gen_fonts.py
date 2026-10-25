"""html-gen の Web フォント（`fetch_fonts.py` の取得・照合・書き込みと、`styles.css` / `check_site.py` との整合）の検査。

本物の配信元へは接続しない。`urlopen` は autouse フィクスチャで「呼ばれたら落ちる」関数に替え、
取得を通すテストだけが偽の配信元（`fonts.invalid`）を差し込む。
"""

from __future__ import annotations

import email.message
import http.client
import re
import runpy
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

import pytest

from ._support import _FETCH_FONTS, _TEMPLATE, CSS, TOKENS, _copy_site, check_site, fetch_fonts

_BODIES = {
    "OFL.txt": b"Copyright 2024 The Geist Project Authors\nSIL Open Font License, Version 1.1\n",
    "Geist-Variable.woff2": b"wOF2" + b"sans-body" * 5,
    "GeistMono-Variable.woff2": b"wOF2" + b"mono-body" * 5,
}
_LAST = "GeistMono-Variable.woff2"


def _refuse_network(*args: object, **kwargs: object) -> None:
    """本物の urlopen の代わりに置き、呼ばれたらテストを落とす。"""
    raise AssertionError("ネットワークに出た")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """どのテストも本物の配信元へ接続させない。"""
    monkeypatch.setattr(fetch_fonts, "urlopen", _refuse_network)


class _FakeResponse:
    """urlopen が返す応答の偽物。本体を 1 回あたり chunk バイトまで返し、返した量を served に残す。"""

    def __init__(self, body: bytes, chunk: int = 7, error: Exception | None = None) -> None:
        """返す本体・1 回の上限・read で投げる例外（無ければ None）を持つ。"""
        self.body = body
        self.chunk = chunk
        self.error = error
        self.served = 0

    def __enter__(self) -> _FakeResponse:
        """with 文で自身を返す。"""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """例外を飲まずに抜ける。"""

    def read(self, amount: int) -> bytes:
        """min(amount, chunk) バイトを返す（error があれば投げる）。"""
        if self.error is not None:
            raise self.error
        data = self.body[self.served : self.served + min(amount, self.chunk)]
        self.served += len(data)
        return data


class _Server:
    """偽の配信元。fetch_fonts.FILES を偽の表に差し替え、URL ごとの応答を返して呼び出しを記録する。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """既定の偽本体で表と応答を用意し、urlopen を差し替える。"""
        self.monkeypatch = monkeypatch
        self.calls: list[tuple[str, float]] = []
        self.responses: dict[str, _FakeResponse | Exception] = {}
        self.serve(_BODIES)
        monkeypatch.setattr(fetch_fonts, "urlopen", self.urlopen)

    def serve(self, bodies: dict[str, bytes]) -> None:
        """bodies から表（大きさと git blob SHA-1）を作って FILES に入れ、同じ本体を返す応答を置く。"""
        table = tuple(
            fetch_fonts.FontFile(name, f"https://fonts.invalid/{name}", len(body), fetch_fonts.git_blob_sha1(body))
            for name, body in bodies.items()
        )
        self.monkeypatch.setattr(fetch_fonts, "FILES", table)
        self.responses = {entry.url: _FakeResponse(bodies[entry.name]) for entry in table}

    def entry(self, name: str) -> fetch_fonts.FontFile:
        """表から name の項目を引く。"""
        return next(entry for entry in fetch_fonts.FILES if entry.name == name)

    def urlopen(self, url: str, timeout: float) -> _FakeResponse:
        """呼び出しを記録し、url に置いた応答を返す（例外を置いていれば投げる）。"""
        self.calls.append((url, timeout))
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def fake_fonts(monkeypatch: pytest.MonkeyPatch) -> _Server:
    """偽の配信元を差し込む。"""
    return _Server(monkeypatch)


def _site(tmp_path: Path) -> Path:
    """空のサイトディレクトリを作る。"""
    site = tmp_path / "site"
    site.mkdir()
    return site


def _listing(directory: Path) -> dict[str, bytes]:
    """ディレクトリ直下の全ファイル（隠しファイルを含む）の名前と中身。"""
    return {path.name: path.read_bytes() for path in directory.iterdir()}


# --- 固定した取得表 -------------------------------------------------------------


def test_real_table_is_pinned() -> None:
    """取得元は完全コミット SHA で固定され、表は確認済みの大きさと git blob SHA-1 を持つ。"""
    commit = "a0a06a3d916dcf92fe96f12051a124f89056b36a"
    base = f"https://raw.githubusercontent.com/vercel/geist-font/{commit}/"
    expected = (
        fetch_fonts.FontFile("OFL.txt", base + "OFL.txt", 4383, "04e95fc550855eb44416b9db1f6722f5cb7342db"),
        fetch_fonts.FontFile(
            "Geist-Variable.woff2",
            base + "packages/next/dist/fonts/geist-sans/Geist-Variable.woff2",
            69760,
            "71dab8bedba3bfef5dccd7875ed05cac44c7345d",
        ),
        fetch_fonts.FontFile(
            "GeistMono-Variable.woff2",
            base + "packages/next/dist/fonts/geist-mono/GeistMono-Variable.woff2",
            71596,
            "0e746fc18223dfcb107a85502a0abe1b1a02d51a",
        ),
    )
    assert (fetch_fonts.COMMIT, fetch_fonts.FILES) == (commit, expected)


@pytest.mark.parametrize(
    ("data", "digest"),
    [
        pytest.param(b"", "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391", id="empty"),
        pytest.param(b"hello\n", "ce013625030ba8dba906f756967f9e9ca394464a", id="hello"),
    ],
)
def test_git_blob_sha1_known_values(data: bytes, digest: str) -> None:
    """git hash-object と同じ値を返す。"""
    assert fetch_fonts.git_blob_sha1(data) == digest


# --- 取得の成功 -----------------------------------------------------------------


def test_fetch_writes_all_files(tmp_path: Path, fake_fonts: _Server) -> None:
    """成功すると fonts/ にちょうど表の 3 ファイルが本体どおりに置かれ、一時ファイルは残らない。"""
    site = _site(tmp_path)
    rc = fetch_fonts.main([str(site)])
    assert (rc, _listing(site / "fonts")) == (0, _BODIES)


def test_fetch_requests_table_urls_with_timeout(tmp_path: Path, fake_fonts: _Server) -> None:
    """表の順に、各 URL を TIMEOUT_SECONDS の締め切りで 1 回ずつ取りに行く。"""
    fetch_fonts.main([str(_site(tmp_path))])
    assert fake_fonts.calls == [(entry.url, fetch_fonts.TIMEOUT_SECONDS) for entry in fetch_fonts.FILES]


def test_fetch_prints_written_paths(tmp_path: Path, fake_fonts: _Server, capsys: pytest.CaptureFixture[str]) -> None:
    """書いたファイルのパスを表の順に 1 行ずつ標準出力へ出す。"""
    site = _site(tmp_path)
    fetch_fonts.main([str(site)])
    expected = [str(site.resolve() / "fonts" / entry.name) for entry in fetch_fonts.FILES]
    assert capsys.readouterr().out.splitlines() == expected


def test_fetch_overwrites_existing_fonts(tmp_path: Path, fake_fonts: _Server) -> None:
    """既に fonts/ があれば中身を取り直した本体で置き換える。"""
    site = _site(tmp_path)
    (site / "fonts").mkdir()
    (site / "fonts" / "Geist-Variable.woff2").write_bytes(b"wOF2old")
    fetch_fonts.main([str(site)])
    assert (site / "fonts" / "Geist-Variable.woff2").read_bytes() == _BODIES["Geist-Variable.woff2"]


def test_fetched_site_passes_check_site(tmp_path: Path, fake_fonts: _Server) -> None:
    """テンプレートの複製に取得したフォントを置いても、サイト検査は違反 0 件のまま。"""
    dest = _copy_site(tmp_path)
    assert fetch_fonts.main([str(dest)]) == 0
    assert check_site.validate_site(dest) == []


# --- 取得の失敗 -----------------------------------------------------------------


def _respond(server: _Server, response: _FakeResponse | Exception) -> None:
    """表の最後の項目への応答を差し替える。"""
    server.responses[server.entry(_LAST).url] = response


def _http_error(server: _Server) -> None:
    """表の最後の項目を 404 にする。"""
    url = server.entry(_LAST).url
    _respond(server, urllib.error.HTTPError(url, 404, "Not Found", email.message.Message(), None))


def _flip_last_byte(server: _Server) -> None:
    """表の最後の項目を、同じ長さで末尾 1 バイトだけ違う本体にする。"""
    body = _BODIES[_LAST]
    _respond(server, _FakeResponse(body[:-1] + bytes([body[-1] ^ 1])))


def _expire_deadline(server: _Server) -> None:
    """締め切りを過去にする（time.monotonic は本物のまま、最初の項目の 1 回目の読みで切れる）。"""
    server.monkeypatch.setattr(fetch_fonts, "TIMEOUT_SECONDS", -1.0)


_FAILURES: list[object] = [
    pytest.param(lambda s: _respond(s, _FakeResponse(_BODIES[_LAST][:-1])), "大きさ", id="size-short"),
    pytest.param(lambda s: _respond(s, _FakeResponse(_BODIES[_LAST] + b"x" * 20)), "大きさ", id="size-over"),
    pytest.param(_flip_last_byte, "ハッシュ", id="hash"),
    pytest.param(lambda s: s.serve({**_BODIES, _LAST: b"wOFF" + b"mono-body" * 5}), "形式", id="woff2-magic"),
    pytest.param(lambda s: _respond(s, urllib.error.URLError("unreachable")), "通信", id="url-error"),
    pytest.param(_http_error, "通信", id="http-error"),
    pytest.param(
        lambda s: _respond(s, _FakeResponse(_BODIES[_LAST], error=http.client.IncompleteRead(b""))),
        "通信",
        id="incomplete-read",
    ),
    pytest.param(
        lambda s: _respond(s, _FakeResponse(_BODIES[_LAST], error=TimeoutError("timed out"))),
        "通信",
        id="socket-timeout",
    ),
    pytest.param(_expire_deadline, "通信", id="deadline"),
]


@pytest.mark.parametrize(("break_server", "label"), _FAILURES)
def test_fetch_failure_writes_nothing(
    tmp_path: Path,
    fake_fonts: _Server,
    capsys: pytest.CaptureFixture[str],
    break_server: Callable[[_Server], None],
    label: str,
) -> None:
    """1 つでも取得・照合に失敗すると fonts/ を作らずに 1 で終わり、原因と OS の書体で表示されることを伝える。"""
    break_server(fake_fonts)
    site = _site(tmp_path)
    rc = fetch_fonts.main([str(site)])
    err = capsys.readouterr().err
    assert (rc, (site / "fonts").exists(), f"FAIL: {label}: " in err, "OS の書体" in err) == (1, False, True, True)


def test_fetch_stops_reading_one_byte_past_size(
    tmp_path: Path, fake_fonts: _Server, capsys: pytest.CaptureFixture[str]
) -> None:
    """表の大きさを超える本体は、大きさ + 1 バイト目で読むのをやめ、超えたことを伝える（巨大な応答を読み切らない）。"""
    oversized = _FakeResponse(_BODIES[_LAST] + b"x" * 20)
    _respond(fake_fonts, oversized)
    fetch_fonts.main([str(_site(tmp_path))])
    err = capsys.readouterr().err
    assert (oversized.served, "バイトを超えた" in err) == (fake_fonts.entry(_LAST).size + 1, True)


def test_missing_site_dir_exits_before_network(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """サイトのディレクトリが無ければ通信前に 2 で終わり、その場所にも fonts/ にも何も作らない。"""
    missing = tmp_path / "missing"
    rc = fetch_fonts.main([str(missing)])
    assert (rc, capsys.readouterr().err.startswith("FAIL: "), missing.exists()) == (2, True, False)


def test_runs_as_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """スクリプトとして実行でき、引数が無ければ argparse が 2 で終える。"""
    monkeypatch.setattr(sys, "argv", [str(_FETCH_FONTS)])
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(urllib.request, "urlopen", _refuse_network)
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(str(_FETCH_FONTS), run_name="__main__")
    assert exc_info.value.code == 2


# --- ライセンスの同梱（check_site.py） -------------------------------------------


def test_template_ships_no_fonts() -> None:
    """テンプレートはフォントを同梱しない（ページを作るときに fetch_fonts.py が取得する）。"""
    assert not (_TEMPLATE / "fonts").exists()


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param(None, [], id="no-fonts-dir"),
        pytest.param((), [], id="empty-fonts-dir"),
        pytest.param(("OFL.txt",), [], id="ofl-only"),
        pytest.param(("OFL.txt", "Geist-Variable.woff2"), [], id="woff2-and-ofl"),
        pytest.param(("Geist-Variable.woff2",), [True], id="woff2-without-ofl"),
    ],
)
def test_ofl_required_when_woff2_present(tmp_path: Path, files: tuple[str, ...] | None, expected: list[bool]) -> None:
    """fonts/ に woff2 を置くなら OFL.txt も要る（SIL OFL 1.1 の再配布条件）。無ければサイト検査が落とす。"""
    dest = _copy_site(tmp_path)
    if files is not None:
        (dest / "fonts").mkdir()
        for name in files:
            (dest / "fonts" / name).write_bytes(_BODIES[name])
    assert ["fonts/OFL.txt" in item for item in check_site.validate_site(dest)] == expected


# --- styles.css の @font-face ---------------------------------------------------

_FONT_FACES = re.findall(r"@font-face\s*\{([^}]*)\}", CSS)


def _faces() -> dict[str, str]:
    """styles.css の @font-face を font-family → fonts/ 内のファイル名の辞書にする。"""
    return {
        re.search(r'font-family:\s*"([^"]+)"', block).group(1): re.search(r'url\("fonts/([^"]+)"\)', block).group(1)
        for block in _FONT_FACES
    }


def _first_family(stack: str) -> str:
    """CSS の font-family 列の先頭候補を引用符を外して返す。"""
    return stack.split(",")[0].strip().strip('"')


def test_font_face_sources_match_fetch_table() -> None:
    """@font-face が読むファイルは fetch_fonts.py が取得する woff2 とちょうど一致する。"""
    woff2 = {entry.name for entry in fetch_fonts.FILES if entry.name.endswith(".woff2")}
    assert set(_faces().values()) == woff2


def test_font_face_families_are_token_first_candidates() -> None:
    """@font-face の family は tokens.json の sans / mono の先頭候補（その次が OS の書体）。"""
    typography = TOKENS["typography"]
    assert set(_faces()) == {_first_family(typography["sans_family"]), _first_family(typography["mono_family"])}


def test_font_face_pairs_family_with_its_file() -> None:
    """各 family は自分の可変フォントを読む（Geist Mono が Geist の字形を読むような取り違えが無い）。"""
    typography = TOKENS["typography"]
    families = (_first_family(typography["sans_family"]), _first_family(typography["mono_family"]))
    assert _faces() == {family: family.replace(" ", "") + "-Variable.woff2" for family in families}


def test_font_face_blocks_declare_variable_woff2_swap() -> None:
    """各 @font-face は woff2 形式・可変の太さの幅・swap（取得前と失敗時は次の候補で描く）を宣言する。"""
    required = ('format("woff2")', "font-weight: 100 900", "font-display: swap")
    assert [all(item in block for item in required) for block in _FONT_FACES] == [True, True]
