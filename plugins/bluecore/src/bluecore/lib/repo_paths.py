"""メモリ永続化前のファイルパス安全性検証。

``mem/chunker.py`` が ``files_read``/``files_modified`` として蓄積するパス文字列は、
将来 ``<mem-context>`` としてセッションへ再注入され LLM に渡る。悪意あるファイル名
（パストラバーサル・制御文字・コンテキストタグ偽装を含むもの）がそのまま永続化される
と、ingestion 時点で防がなければプロンプトインジェクションやログ改ざんの経路になる。

このモジュールは危険なパス文字列のみを ingestion 時点で fail-closed に除外する。
Claude Code のツール契約上、Read/Write/Edit の ``file_path`` は絶対パスで渡されるため、
絶対パス自体は拒否対象にしない（安全なパス文字列は正規化せずそのまま保持する）。
"""

from __future__ import annotations

import re
import unicodedata

MAX_REPO_PATH_LENGTH = 1024

# <mem-context>/<private>/<system-instruction> はセッションコンテキスト注入で
# 実際に使われる区切りタグ（bluecore.mem.tag_stripping と対応）。
# ファイルパスにこれらが混入すると、再注入時に偽の区切りとして働く恐れがある。
_CONTEXT_TAG = re.compile(
    r"</?(?:mem-context|private|system[_-]instruction)\b[^>]*>",
    re.IGNORECASE,
)


def is_safe_repo_path(path: object) -> bool:
    """*path* が安全に永続化できるファイルパス文字列かどうかを判定する。

    Args:
        path: 判定対象。文字列以外は無条件に安全でないとみなす。

    Returns:
        制御文字（改行・NUL含む）・``..`` によるパストラバーサル・コンテキストタグ
        偽装のいずれも含まない場合に True。
    """
    if not isinstance(path, str) or not path or len(path) > MAX_REPO_PATH_LENGTH:
        return False
    if any(unicodedata.category(char).startswith("C") for char in path):
        return False
    if ".." in re.split(r"[/\\]", path):
        return False
    if _CONTEXT_TAG.search(path):
        return False
    return True


def filter_safe_paths(paths: list[str]) -> list[str]:
    """*paths* から安全なファイルパス文字列だけを順序を保ったまま残す。

    Args:
        paths: 判定対象のパス文字列一覧。

    Returns:
        危険なパスを除外したリスト（安全なパスは正規化せずそのまま）。
    """
    return [path for path in paths if is_safe_repo_path(path)]
