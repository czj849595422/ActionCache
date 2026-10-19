"""动作执行情况记录表（CSV，UTF-8 BOM，随执行实时追加）。"""

from __future__ import annotations

import csv
from pathlib import Path
import threading
from typing import Any, Callable, Mapping


ACTION_EXECUTION_HEADERS = [
    "sequence",
    "agent",
    "action",
    "call_file",
    "planning_started_at",
    "planning_finished_at",
    "planning_duration_seconds",
    "execution_started_at",
    "execution_finished_at",
    "execution_duration_seconds",
    "interrupted_previous",
    "interrupted",
    "feedback",
    "plan_source",
]


class ActionExecutionTable:
    """线程安全的 CSV 追加写入器；编号实验级全局递增。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._sequence = 0
        exists = self.path.is_file()
        if exists and self.path.stat().st_size > 0:
            with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
                self._sequence = max(0, sum(1 for _ in csv.reader(handle)) - 1)
        self._handle = self.path.open("a", encoding="utf-8-sig", newline="")
        self._writer = csv.writer(self._handle)
        if not exists or self.path.stat().st_size == 0:
            self._writer.writerow(ACTION_EXECUTION_HEADERS)
            self._handle.flush()

    def write_row(self, fields: Mapping[str, Any]) -> int:
        """写入一行完整记录，返回该行编号。"""
        with self._lock:
            self._sequence += 1
            row = [fields.get(header, "") for header in ACTION_EXECUTION_HEADERS]
            row[0] = self._sequence
            self._writer.writerow(row)
            self._handle.flush()
            return self._sequence

    def close(self) -> None:
        with self._lock:
            if not self._handle.closed:
                self._handle.close()


def make_call_file_resolver(calls_dir: str | Path) -> Callable[[str | None], str | None]:
    """按 request_id 前 8 位在 llm/calls 目录查找调用文件名。"""
    directory = Path(calls_dir)

    def resolve(request_id: str | None) -> str | None:
        if not request_id:
            return None
        matches = list(directory.glob(f"*.{request_id[:8]}.json"))
        return matches[0].name if matches else None

    return resolve


__all__ = [
    "ACTION_EXECUTION_HEADERS",
    "ActionExecutionTable",
    "make_call_file_resolver",
]
