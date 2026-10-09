"""呼び出しの主体と監査ログ。

主体は cursor の結びつけと監査ログに使う。監査ログには主体そのものを残さず、cursor の
署名と同じ鍵の素材に用途の名前を前置した別の鍵（領域分離）で要約して残す。値は残さない。
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import logging
import os
from collections.abc import Callable, Mapping, Sequence

from .errors import FacadeError

# 監査ログの主体の要約に使う鍵の前置き（cursor の署名と同じ鍵の素材から、別の鍵を作る）
_SUBJECT_DOMAIN = b"audit-subject:"
_PRINCIPAL_UNKNOWN = (
    "呼び出しの主体を決められないため実行できません。サーバの運用者に FACADE_PRINCIPAL の設定を依頼してください。"
)


def resolve_principal(environ: Mapping[str, str] = os.environ, getuser: Callable[[], str] = getpass.getuser) -> str:
    """呼び出しの主体を返す。

    stdio では起動した利用者。リモートで OAuth を入れたら、検証済みのアクセストークンの
    主体に置き換える（ツール引数から取らない）。

    Args:
        environ: 環境変数（`FACADE_PRINCIPAL` を読む）。
        getuser: OS の利用者名を返す関数。

    Returns:
        主体の識別子。`FACADE_PRINCIPAL` が空でなければその値、無ければ OS の利用者名。

    Raises:
        FacadeError: FACADE_PRINCIPAL が無く、OS の利用者名も引けないとき。
    """
    configured = environ.get("FACADE_PRINCIPAL")
    if configured:
        return configured
    try:
        return getuser()
    except (OSError, KeyError, ImportError) as error:
        # 3.13 以降は OSError だけを投げる。3.12 は uid が passwd に無いと KeyError、pwd が無いと ImportError
        raise FacadeError(_PRINCIPAL_UNKNOWN) from error


class AuditLog:
    """監査ログ。個人情報の項目を返したことと、cursor を拒否したことを残す。"""

    def __init__(self, secret: bytes, *, logger: logging.Logger) -> None:
        """鍵の素材と出力先を設定する。

        Args:
            secret: 鍵の素材（cursor の署名と同じ値でよい。用途の名前を前置して別の鍵にする）。
            logger: 監査ログの出力先。
        """
        self._key = _SUBJECT_DOMAIN + secret
        self._logger = logger

    def subject(self, principal: str) -> str:
        """主体を、監査ログに残す鍵付きの要約にする（ログから主体そのものを読ませない）。

        Args:
            principal: 呼び出しの主体。

        Returns:
            16 進数 32 文字の要約（同じ主体なら同じ値）。
        """
        digest = hmac.new(self._key, principal.encode("utf-8", "surrogatepass"), hashlib.sha256)
        return digest.hexdigest()[:32]

    def personal_returned(self, tool: str, paths: Sequence[str], count: int, *, principal: Callable[[], str]) -> None:
        """個人情報（personal）の項目を返したことを残す（値は残さない）。

        Args:
            tool: ツール名。
            paths: 結果に含めた個人情報の項目のパス（`Catalog.personal_paths` で絞ったもの）。
            count: その項目を含めて返したレコードの件数。
            principal: 主体を返す関数（記録するときだけ呼ぶ）。

        Raises:
            FacadeError: 主体を決められないとき（記録できないまま個人情報を返さない）。
        """
        if paths and count:
            self._logger.info(
                "personal fields returned: subject=%s tool=%s paths=%s count=%d",
                self.subject(principal()),
                tool,
                ",".join(paths),
                count,
            )

    def cursor_rejected(self, tool: str, principal: str) -> None:
        """cursor を拒否したことを残す。

        Args:
            tool: ツール名。
            principal: cursor を開こうとした主体。
        """
        self._logger.warning("cursor rejected: subject=%s tool=%s", self.subject(principal), tool)
