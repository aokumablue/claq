"""python -m bluecore.mem <command> で実行できるようにする"""

import sys

from bluecore.mem.cli import main

sys.exit(main())
