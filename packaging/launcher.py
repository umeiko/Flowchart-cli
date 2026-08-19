"""PyInstaller 冻结入口：等价于 python -m flowchart_agent。"""

import sys

from flowchart_agent.cli import main

if __name__ == "__main__":
    sys.exit(main())
