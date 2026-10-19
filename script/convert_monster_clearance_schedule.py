#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将怪物/补给时间表转换为 Minecraft 服务器批量指令文件。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tasks.monster_clearance import (  # noqa: E402
    command_file_text,
    load_spawn_schedule,
    write_command_file,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="spawn_schedule.txt")
    parser.add_argument("output", type=Path, nargs="?", help="commands.txt")
    parser.add_argument("--monster-tag", default="ac_monster_quick")
    parser.add_argument("--supply-tag", default="ac_supply_quick")
    parser.add_argument("--spawn-objective", default="acTaskSpawn")
    parser.add_argument("--check", action="store_true", help="仅校验并输出到终端")
    args = parser.parse_args()

    records = load_spawn_schedule(args.input)
    options = {
        "monster_tag": args.monster_tag,
        "supply_tag": args.supply_tag,
        "spawn_objective": args.spawn_objective,
    }
    text = command_file_text(records, **options)
    if args.check:
        print(text, end="")
        return 0
    if args.output is None:
        parser.error("非 --check 模式必须提供 output")
    write_command_file(records, args.output, **options)
    print(f"已生成 {args.output}，刷新记录 {len(records)}，指令 {len(records)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
