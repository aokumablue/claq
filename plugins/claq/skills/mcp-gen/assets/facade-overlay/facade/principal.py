"""呼び出しの主体。

主体は受信側のレート制限（guard の `CallLimiter`）の単位に使う。
"""

from __future__ import annotations

import getpass
import os
from collections.abc import Callable, Mapping

from .errors import FacadeError

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
        # stdio の主体は起動した利用者そのもの（既定値で続行するフォールバックではない）
        return getuser()
    except (OSError, KeyError, ImportError) as error:
        # 3.13 以降は OSError だけを投げる。3.12 は uid が passwd に無いと KeyError、pwd が無いと ImportError
        raise FacadeError(_PRINCIPAL_UNKNOWN) from error
