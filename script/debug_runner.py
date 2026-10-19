#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一交互式任务环境 Debug Runner。"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.debug.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
