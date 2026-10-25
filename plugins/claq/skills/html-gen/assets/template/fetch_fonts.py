"""Geist / Geist Mono の Web フォントを取得し、サイトの `fonts/` に置く。

標準ライブラリのみ。vercel/geist-font の固定コミットから可変フォント 2 つと
ライセンス（SIL OFL 1.1）を取得し、大きさと git blob SHA-1 を照合してから書く。
1 つでも取得・照合に失敗したら `fonts/` には何も書かず非 0 で終わる。その場合も
サイトは `--font-sans` / `--font-mono` の次の候補（OS の書体）で表示される。

    python3 fetch_fonts.py <site_dir>
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import os
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple
from urllib.request import urlopen

COMMIT = "a0a06a3d916dcf92fe96f12051a124f89056b36a"
_BASE = f"https://raw.githubusercontent.com/vercel/geist-font/{COMMIT}/"
#: ファイルごとの本体受信の締め切り（秒）。urlopen のソケット操作ごとの timeout にも同じ値を渡す。
TIMEOUT_SECONDS = 20.0
#: 3 ファイルの取得と照合を合わせた上限（秒）。名前解決・接続・ヘッダ受信で止まっても、ページの生成を待たせない。
TOTAL_TIMEOUT_SECONDS = 60.0
_CHUNK = 16384
WOFF2_MAGIC = b"wOF2"


class FontFile(NamedTuple):
    """取得する 1 ファイル（置く名前・取得元・期待する大きさと git blob SHA-1）。"""

    name: str
    url: str
    size: int
    blob_sha1: str


FILES = (
    FontFile("OFL.txt", _BASE + "OFL.txt", 4383, "04e95fc550855eb44416b9db1f6722f5cb7342db"),
    FontFile(
        "Geist-Variable.woff2",
        _BASE + "packages/next/dist/fonts/geist-sans/Geist-Variable.woff2",
        69760,
        "71dab8bedba3bfef5dccd7875ed05cac44c7345d",
    ),
    FontFile(
        "GeistMono-Variable.woff2",
        _BASE + "packages/next/dist/fonts/geist-mono/GeistMono-Variable.woff2",
        71596,
        "0e746fc18223dfcb107a85502a0abe1b1a02d51a",
    ),
)


class FetchError(Exception):
    """取得・照合の失敗。文面は「原因ラベル: 詳細」の形を取る。"""


def git_blob_sha1(data: bytes) -> str:
    """data の git blob SHA-1（`git hash-object` と同じ値）を返す。"""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def download(entry: FontFile) -> bytes:
    """entry を取得する。通信の失敗・締め切り超過・表の大きさ超過は FetchError にする。"""
    deadline = time.monotonic() + TIMEOUT_SECONDS
    try:
        with urlopen(entry.url, timeout=TIMEOUT_SECONDS) as response:
            return _read_capped(response, entry, deadline)
    except (OSError, http.client.HTTPException) as err:
        raise FetchError(f"通信: {entry.name} を取得できない（{err}）") from err


def _read_capped(response: http.client.HTTPResponse, entry: FontFile, deadline: float) -> bytes:
    """本体を表の大きさ + 1 バイトまで読み、超えたら大きさ、deadline を過ぎたら通信の FetchError にする。"""
    chunks: list[bytes] = []
    total = 0
    while chunk := response.read(min(_CHUNK, entry.size + 1 - total)):
        chunks.append(chunk)
        total += len(chunk)
        if total > entry.size:
            raise FetchError(
                f"大きさ: {entry.name} が {entry.size} バイトを超えた（{entry.size + 1} バイト目で読むのをやめた）"
            )
        # urlopen の timeout はソケット操作ごとの上限で、少しずつ届く本体の受信時間を縛らない。
        # ここで縛れるのは本体の受信だけで、接続・ヘッダ受信と最後の 1 回の read はソケット操作ごとの上限に従う
        if time.monotonic() > deadline:
            raise FetchError(f"通信: {entry.name} を {TIMEOUT_SECONDS} 秒以内に取り終えられない")
    return b"".join(chunks)


def verify(entry: FontFile, data: bytes) -> None:
    """data が entry の大きさ・git blob SHA-1・形式に合うかを確かめ、合わなければ FetchError にする。"""
    if len(data) != entry.size:
        raise FetchError(f"大きさ: {entry.name} が {entry.size} バイトでない（{len(data)} バイト）")
    actual = git_blob_sha1(data)
    if actual != entry.blob_sha1:
        raise FetchError(
            f"ハッシュ: {entry.name} の git blob SHA-1 が一致しない（期待 {entry.blob_sha1}、実際 {actual}）"
        )
    if entry.name.endswith(".woff2") and not data.startswith(WOFF2_MAGIC):
        raise FetchError(f"形式: {entry.name} の先頭が wOF2 でない")


def write_all(fonts_dir: Path, downloads: list[tuple[FontFile, bytes]]) -> list[Path]:
    """照合済みの本体を全部一時ファイルへ書いてから、表の順に最終名へ置き換え、最終パスを返す。"""
    fonts_dir.mkdir(exist_ok=True)
    staged = []
    for entry, data in downloads:
        temporary = fonts_dir / f".{entry.name}.tmp"
        temporary.write_bytes(data)
        staged.append((temporary, fonts_dir / entry.name))
    # 表の先頭が OFL.txt なので、置き換えの途中で止まっても「woff2 があってライセンスが無い」状態にならない
    for temporary, final in staged:
        os.replace(temporary, final)
    return [final for _, final in staged]


def fetch_all() -> list[tuple[FontFile, bytes]]:
    """FILES を順に取得・照合し、本体の組を返す。"""
    downloads = []
    for entry in FILES:
        data = download(entry)
        verify(entry, data)
        downloads.append((entry, data))
    return downloads


def fetch_within(timeout: float) -> list[tuple[FontFile, bytes]]:
    """fetch_all を別スレッドで走らせ、timeout 秒で打ち切る（打ち切ったら通信の FetchError）。

    スレッドは daemon なので、打ち切った後に通信が止まったままでもプロセスの終了を妨げない。
    スレッドの中で起きた例外は、そのまま呼び出し元へ投げ直す。
    """
    outcome: dict[str, object] = {}

    def run() -> None:
        """fetch_all の結果か例外を outcome に残す。"""
        try:
            outcome["value"] = fetch_all()
        except Exception as err:  # 呼び出し元のスレッドへ運んで投げ直すため（握り潰さない）
            outcome["error"] = err

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise FetchError(f"通信: {timeout:g} 秒以内に 3 ファイルを取り終えられない")
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    return outcome["value"]  # type: ignore[return-value]


def main(argv: list[str] | None = None) -> int:
    """エントリポイント。0 成功 / 1 取得・照合の失敗 / 2 サイトのディレクトリが不正。"""
    parser = argparse.ArgumentParser(description="Geist / Geist Mono を取得してサイトの fonts/ に置く")
    parser.add_argument("site", help="テンプレートを複製したサイトのディレクトリ")
    args = parser.parse_args(argv)
    site_dir = Path(args.site).resolve()
    # 通信の前に止め、打ち間違えた場所へ fonts/ を作らせない
    if not site_dir.is_dir():
        print(
            f"FAIL: {site_dir} がディレクトリでない（テンプレートを複製したサイトのディレクトリを渡す）",
            file=sys.stderr,
        )
        return 2
    try:
        downloads = fetch_within(TOTAL_TIMEOUT_SECONDS)
    except FetchError as err:
        print(f"FAIL: {err}", file=sys.stderr)
        print("fonts/ には何も書いていない。サイトは OS の書体で表示される。", file=sys.stderr)
        return 1
    for path in write_all(site_dir / "fonts", downloads):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
