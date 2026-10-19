#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量生成不同 seed 的多阶段合成贸易任务。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tasks.trade import generate_task_batch  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--base-seed", type=int, default=20260809)
    parser.add_argument(
        "--template",
        type=Path,
        default=ROOT / "src" / "tasks" / "templates" / "trade.quick.yaml",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / ".tmp" / "trade_tasks",
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
        print(bundle.directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
