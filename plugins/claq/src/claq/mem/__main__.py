"""python -m claq.mem <command> で実行できるようにする。

``if __name__`` で守るのは、パッケージを走査する道具（``pkgutil.walk_packages``
やドキュメント生成）がこのモジュールを import しただけで CLI が走り、
``sys.exit`` の ``SystemExit`` が走査側へ飛ぶのを防ぐため。``python -m claq.mem``
と ``runpy.run_module(..., run_name="__main__")`` はどちらも ``__name__`` が
``"__main__"`` になるので、意図した起動経路は従来どおり通る。
"""

import sys

from claq.mem.cli import main

if __name__ == "__main__":
    sys.exit(main())
