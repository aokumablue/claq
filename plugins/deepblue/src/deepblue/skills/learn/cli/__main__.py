"""``python3 -m deepblue.skills.learn.cli`` のモジュール実行エントリポイント。

``python3 -m deepblue.skills.learn.cli`` および
``runpy.run_module("deepblue.skills.learn.cli", run_name="__main__")`` の双方で
``main()`` を実行し、その終了コードで ``SystemExit`` を送出する。

実行コードはすべて ``if __name__ == "__main__":`` ガード内に置く。これにより
``[tool.coverage.report] exclude_lines`` の ``if __name__ == .__main__.:`` 指定で
当ファイルの実行行がカバレッジ対象から除外され、``fail_under = 100`` を満たす。
"""

if __name__ == "__main__":
    import sys

    from deepblue.skills.learn.cli.entry import main

    sys.exit(main())
