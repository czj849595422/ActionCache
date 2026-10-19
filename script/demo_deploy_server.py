#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""部署服务器 demo 启动入口: 只做调用, 不做任何定义。

实际编排逻辑位于 src/launcher/server_demo.py。
默认无限运行, Ctrl+C 优雅停止; 也可用 --duration 限时。

用法:
  python script/demo_deploy_server.py            # 无限运行
  python script/demo_deploy_server.py --duration 60
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from launcher.server_demo import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
