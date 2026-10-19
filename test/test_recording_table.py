"""动作执行记录表（CSV）单元测试。"""

from __future__ import annotations

import csv
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.recording.table import (  # noqa: E402
    ACTION_EXECUTION_HEADERS,
    ActionExecutionTable,
    make_call_file_resolver,
)


class ActionExecutionTableTests(unittest.TestCase):
    def test_header_bom_and_sequence(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            target = Path(tmp) / "actions.csv"
            table = ActionExecutionTable(target)
            self.assertEqual(table.write_row({"agent": "Alice"}), 1)
            self.assertEqual(table.write_row({"agent": "Bob"}), 2)
            table.close()
            raw = target.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            rows = list(csv.reader(target.open(encoding="utf-8-sig")))
            self.assertEqual(rows[0], ACTION_EXECUTION_HEADERS)
            self.assertEqual(rows[1][0], "1")
            self.assertEqual(rows[1][1], "Alice")
            self.assertEqual(rows[2][0], "2")
            self.assertEqual(rows[2][1], "Bob")

    def test_call_file_resolver(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            calls = Path(tmp) / "calls"
            calls.mkdir()
            (calls / "20260814-010000.Alice.standard_action.001.abcdef12.json").write_text(
                "{}",
                encoding="utf-8",
            )
            resolver = make_call_file_resolver(calls)
            self.assertEqual(
                resolver("abcdef123456"),
                "20260814-010000.Alice.standard_action.001.abcdef12.json",
            )
            self.assertIsNone(resolver(None))
            self.assertIsNone(resolver("zzzz9999"))

    def test_write_after_reopen_appends_sequence(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            target = Path(tmp) / "actions.csv"
            first = ActionExecutionTable(target)
            first.write_row({"agent": "Alice"})
            first.close()
            second = ActionExecutionTable(target)
            sequence = second.write_row({"agent": "Bob"})
            second.close()
            self.assertEqual(sequence, 2)
            rows = list(csv.reader(target.open(encoding="utf-8-sig")))
            self.assertEqual(len(rows), 3)


if __name__ == "__main__":
    unittest.main()
