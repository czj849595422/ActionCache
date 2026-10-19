#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Demo 启动入口: 只做调用, 不做任何定义。

实际编排逻辑位于 src/launcher/minecraft_demo.py。

用法:
  python script/demo_launch_server_agent.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from launcher.minecraft_demo import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
