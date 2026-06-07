"""``python3 -m bluecore.skills.learn.cli`` のモジュール実行エントリポイント。

``python3 -m bluecore.skills.learn.cli`` および
``runpy.run_module("bluecore.skills.learn.cli", run_name="__main__")`` の双方で
``main()`` を実行し、その終了コードで ``SystemExit`` を送出する。

実行コードはすべて ``if __name__ == "__main__":`` ガード内に置く。これにより
``[tool.coverage.report] exclude_lines`` の ``if __name__ == .__main__.:`` 指定で
当ファイルの実行行がカバレッジ対象から除外され、``fail_under = 100`` を満たす。
"""

if __name__ == "__main__":  # pragma: no cover
    import sys

    from bluecore.skills.learn.cli.entry import main

    sys.exit(main())
