#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量生成可复现的动态怪物清剿任务包。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

TASK_FOLDER = Path(__file__).resolve().parent
ROOT = TASK_FOLDER.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tasks.monster_clearance import generate_task_batch  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--template",
        type=Path,
        default=TASK_FOLDER / "template.yaml",
    )
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--base-seed", type=int, default=0)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=TASK_FOLDER,
        help=(
            "任务文件夹；初始化文件写入其 config/ 目录，"
            "编号为使用的种子（从 base-seed 开始）"
        ),
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    bundles = generate_task_batch(
        args.template,
        count=args.count,
        base_seed=args.base_seed,
        output_dir=args.output_dir,
        overwrite=args.overwrite,
    )
    for bundle in bundles:
        print(f"config: {bundle.task}")
        print(f"  schedule: {bundle.schedule.name}")
        print(f"  commands: {bundle.commands.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
