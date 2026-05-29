"""python -m deepblue.mem <command> で実行できるようにする"""

import sys

from deepblue.mem.cli import main

sys.exit(main())
