#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate numbered Boss combat initialization configs."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tasks.boss_combat import generate_task_batch  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成 boss_combat/config/<seed>.yaml 初始化配置。"
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=ROOT / "src" / "tasks" / "specs" / "boss_combat.yaml",
        help="Boss combat 模板 YAML。",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "src" / "tasks" / "boss_combat",
        help="输出任务目录，配置写入其 config/ 子目录。",
    )
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--base-seed", type=int, default=1)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    bundles = generate_task_batch(
        args.template,
        count=args.count,
        base_seed=args.base_seed,
        output_dir=args.output_dir,
        overwrite=args.overwrite,
    )
    for bundle in bundles:
        print(f"task: {bundle.task}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
