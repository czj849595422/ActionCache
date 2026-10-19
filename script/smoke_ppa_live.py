#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PPA 短时受控 live smoke（P0-6）：scripted decisions + 真实 Minecraft。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = ROOT / "assert" / "env" / "benchmark"
DEFAULT_SERVER_DIR = ROOT / "src" / "minecraft"
DEFAULT_RECORD_DIR = ROOT / ".tmp" / "smoke_sessions"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.baseline.Parallelized.smoke import run_smoke  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-dir", type=Path, default=DEFAULT_SERVER_DIR)
    parser.add_argument("--bridge-port-start", type=int, default=3201)
    parser.add_argument("--agents", type=int, default=2)
    parser.add_argument("--duration", type=float, default=8.0)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--planning-interval", type=float, default=0.3)
    parser.add_argument("--max-run-seconds", type=float, default=40.0)
    parser.add_argument("--record-dir", type=Path, default=DEFAULT_RECORD_DIR)
    parser.add_argument("--no-record", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.agents <= 4:
        parser.error("--agents 必须在 1 到 4 之间")
    if args.duration <= 0 or args.timeout <= args.duration:
        parser.error("--duration 必须为正数且小于 --timeout")
    if args.planning_interval < 0:
        parser.error("--planning-interval 不能为负")
    if args.max_run_seconds <= 0:
        parser.error("--max-run-seconds 必须为正数")

    report = run_smoke(
        server_dir=args.server_dir,
        bridge_port_start=args.bridge_port_start,
        benchmark_root=BENCHMARK_ROOT,
        agents=args.agents,
        duration_seconds=args.duration,
        timeout_seconds=args.timeout,
        planning_interval=args.planning_interval,
        max_run_seconds=args.max_run_seconds,
        record_dir=None if args.no_record else args.record_dir,
        output=print,
    )
    report.print_report(output=print)
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
