#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""command_scheduler 解析与调度逻辑单元测试。"""

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.launcher.command_scheduler import (  # noqa: E402
    BatchCommandScheduler,
    CommandEntry,
    parse_batch_file,
    parse_batch_text,
)


class ParseTaskPhaseTest(unittest.TestCase):
    def test_mixed_units_sorted_by_time(self):
        plan = parse_batch_text(
            "say immediate\n"
            "3s say three\n"
            "1t say one tick\n"
            "2 say two default\n"
        )
        entries = plan.task
        self.assertEqual(
            [(e.time, e.unit, e.command) for e in entries],
            [
                (0.0, "s", "say immediate"),
                (1.0, "t", "say one tick"),
                (2.0, "s", "say two default"),
                (3.0, "s", "say three"),
            ],
        )

    def test_same_time_keeps_file_order(self):
        entries = parse_batch_text("3s say a\n2s say b\n2s say c\n").task
        self.assertEqual([e.command for e in entries], ["say b", "say c", "say a"])

    def test_comments_and_blank_lines_ignored(self):
        entries = parse_batch_text("# 注释\n\n  \n5s say hi\n").task
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].command, "say hi")

    def test_default_unit_t(self):
        entries = parse_batch_text(
            "100 say hundred ticks\n50t say fifty\n", default_unit="t"
        ).task
        self.assertEqual(
            [(e.time, e.unit) for e in entries],
            [(50.0, "t"), (100.0, "t")],
        )

    def test_leading_slash_stripped(self):
        entries = parse_batch_text("/weather clear\n").task
        self.assertEqual(entries[0].command, "weather clear")

    def test_decimal_time(self):
        entries = parse_batch_text("0.5s say half\n").task
        self.assertEqual(entries[0].time, 0.5)
        self.assertEqual(entries[0].unit, "s")

    def test_negative_time_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            parse_batch_text("-5s say hi\n")
        self.assertIn("时间不能为负", str(ctx.exception))
        self.assertIn("第 1 行", str(ctx.exception))

    def test_unknown_unit_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            parse_batch_text("5x say hi\n")
        self.assertIn("未知时间单位", str(ctx.exception))

    def test_missing_command_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            parse_batch_text("5s\n")
        self.assertIn("缺少指令内容", str(ctx.exception))

    def test_invalid_default_unit_rejected(self):
        with self.assertRaises(ValueError):
            parse_batch_text("1 say hi\n", default_unit="x")

    def test_line_no_recorded(self):
        entries = parse_batch_text("say a\n5s say b\n").task
        self.assertEqual([e.line_no for e in entries], [1, 2])

    def test_entry_is_frozen(self):
        entry = CommandEntry(time=1.0, unit="s", command="say hi", line_no=1)
        with self.assertRaises(Exception):
            entry.time = 2.0


class ParsePhasesTest(unittest.TestCase):
    def test_default_phase_is_task(self):
        plan = parse_batch_text("say hi\n3s say three\n")
        self.assertEqual(plan.prepare, [])
        self.assertEqual([e.command for e in plan.task], ["say hi", "say three"])

    def test_prepare_and_task_markers(self):
        plan = parse_batch_text(
            "[prepare]\nweather clear\ntime set day\n"
            "[task]\n5s say hi\n3s say three\n"
        )
        self.assertEqual(plan.prepare, ["weather clear", "time set day"])
        self.assertEqual(
            [(e.time, e.command) for e in plan.task],
            [(3.0, "say three"), (5.0, "say hi")],
        )

    def test_prepare_keeps_file_order(self):
        plan = parse_batch_text("[prepare]\nsay b\nsay a\n[task]\nsay go\n")
        self.assertEqual(plan.prepare, ["say b", "say a"])

    def test_prepare_rejects_time_prefix(self):
        with self.assertRaises(ValueError) as ctx:
            parse_batch_text("[prepare]\n3s say hi\n")
        self.assertIn("准备阶段不支持时间前缀", str(ctx.exception))

    def test_prepare_marker_after_task_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            parse_batch_text("say hi\n[prepare]\nweather clear\n")
        self.assertIn("准备阶段标记不能出现在任务阶段之后", str(ctx.exception))

    def test_prepare_leading_slash_stripped(self):
        plan = parse_batch_text("[prepare]\n/weather clear\n[task]\nsay go\n")
        self.assertEqual(plan.prepare, ["weather clear"])


class ParseBatchFileTest(unittest.TestCase):
    def test_parse_file_with_phases(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", encoding="utf-8", delete=False
        ) as fh:
            fh.write("[prepare]\nweather clear\n[task]\nsay hi\n2s say two\n")
            path = fh.name
        try:
            plan = parse_batch_file(path)
            self.assertEqual(plan.prepare, ["weather clear"])
            self.assertEqual(
                [(e.time, e.command) for e in plan.task],
                [(0.0, "say hi"), (2.0, "say two")],
            )
        finally:
            Path(path).unlink(missing_ok=True)


class FakeServer:
    """离线假服务器: 记录发送的指令。"""

    def __init__(self):
        self.commands = []
        self.running = True

    @property
    def is_running(self):
        return self.running

    def send_command(self, command, response_timeout=5.0):
        self.commands.append(command)
        return f"ok:{command}"


class BatchSchedulerTest(unittest.TestCase):
    def test_run_prepare_executes_in_order(self):
        plan = parse_batch_text("[prepare]\nsay a\ntime set day\n[task]\n0s say t1\n")
        server = FakeServer()
        scheduler = BatchCommandScheduler(server, plan)

        result = scheduler.run_prepare()

        self.assertEqual(server.commands, ["say a", "time set day"])
        self.assertEqual([cmd for cmd, _ in result.prepared], ["say a", "time set day"])
        self.assertFalse(result.started)

    def test_wait_for_start_signal(self):
        plan = parse_batch_text("[prepare]\nsay prep\n[task]\n0s say go\n")
        server = FakeServer()
        scheduler = BatchCommandScheduler(server, plan)
        scheduler.run_prepare()

        self.assertFalse(scheduler.wait_for_start(timeout=0))
        scheduler.signal_task_start()
        self.assertTrue(scheduler.wait_for_start(timeout=1))

        result = scheduler.run_task()
        self.assertTrue(result.started)
        self.assertEqual(server.commands, ["say prep", "say go"])
        self.assertEqual(len(result.sent), 1)

    def test_run_auto_start_delay_zero(self):
        plan = parse_batch_text("[prepare]\nsay prep\n[task]\nsay go\n")
        server = FakeServer()
        scheduler = BatchCommandScheduler(server, plan)

        result = scheduler.run(start_delay=0)

        self.assertTrue(result.started)
        self.assertEqual(server.commands, ["say prep", "say go"])
        self.assertEqual(len(result.sent), 1)

    def test_run_without_task_returns_prepare_only(self):
        plan = parse_batch_text("[prepare]\nsay prep\n")
        server = FakeServer()
        scheduler = BatchCommandScheduler(server, plan)

        result = scheduler.run(start_delay=0)

        self.assertEqual(server.commands, ["say prep"])
        self.assertEqual(result.total, 0)
        self.assertFalse(result.started)

    def test_cancel_stops_pending_commands_and_exposes_snapshot(self):
        plan = parse_batch_text("100s say too-late\n")
        server = FakeServer()
        scheduler = BatchCommandScheduler(server, plan, poll_interval=0.01)
        thread = threading.Thread(target=scheduler.run_task)
        thread.start()
        scheduler.cancel()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        snapshot = scheduler.result_snapshot()
        self.assertTrue(snapshot.started)
        self.assertTrue(snapshot.cancelled)
        self.assertEqual(snapshot.sent, [])


if __name__ == "__main__":
    unittest.main()
