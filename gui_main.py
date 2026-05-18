"""闲鱼数据采集分析工具 v3 — 入口"""
import sys
from pathlib import Path

# 确保项目根在 sys.path
PROJECT_ROOT = Path(__file__).parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gui.app import XianyuApp


def main():
    app = XianyuApp()
    app.run()


if __name__ == "__main__":
    main()
