#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 trade_config.yaml 转换为服务器准备阶段指令文件。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tasks.trade import load_trade_config, write_command_file  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    write_command_file(load_trade_config(args.config), args.output)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
