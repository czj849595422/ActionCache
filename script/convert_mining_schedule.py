#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将空格分隔的矿物时间表转换为服务器批量指令文件。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tasks.mining import (  # noqa: E402
    command_file_text,
    load_spawn_schedule,
    write_command_file,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="spawn_schedule.txt")
    parser.add_argument("output", type=Path, nargs="?", help="commands.txt")
    parser.add_argument("--check", action="store_true", help="仅校验并输出到终端")
    args = parser.parse_args()

    records = load_spawn_schedule(args.input)
    text = command_file_text(records)
    if args.check:
        print(text, end="")
        return 0
    if args.output is None:
        parser.error("非 --check 模式必须提供 output")
    write_command_file(records, args.output)
    print(f"已生成 {args.output}，矿块 {len(records)}，指令 {len(records) * 2}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
