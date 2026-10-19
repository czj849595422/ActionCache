"""单槽覆盖队列：规划线程与执行线程之间的“最新值”通信通道。"""

from __future__ import annotations

import threading
import time
from typing import Any


class SingleSlotOverwriteBuffer:
    """线程安全的单槽覆盖缓冲区。

    - put 覆盖旧值并递增版本号（版本单调递增、永不回绕）；
    - has_new(seen_version) 区分“新条目”与“同一条目”，避免重复消费；
    - wait_new 阻塞等待新条目（Condition 通知），支持超时。
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._version = 0
        self._item: Any = None

    def put(self, item: Any) -> int:
        """覆盖写入并返回新版本号；item 不能为 None。"""
        if item is None:
            raise ValueError("item must not be None")
        with self._condition:
            self._version += 1
            self._item = item
            self._condition.notify_all()
            return self._version

    def peek(self) -> tuple[int, Any] | None:
        """返回 (version, item)；缓冲区为空时返回 None。"""
        with self._condition:
            if self._item is None:
                return None
            return (self._version, self._item)

    def has_new(self, seen_version: int) -> bool:
        """是否存在比 seen_version 更新的非空条目。"""
        with self._condition:
            return self._version > seen_version and self._item is not None

    def wait_new(
        self,
        seen_version: int,
        timeout: float | None = None,
    ) -> bool:
        """阻塞等待新条目；超时返回 False，有新条目返回 True。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                if self._version > seen_version and self._item is not None:
                    return True
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)

    def clear(self) -> None:
        """清空条目（版本号继续递增，保证后续写入可被感知）。"""
        with self._condition:
            self._version += 1
            self._item = None
            self._condition.notify_all()

    @property
    def version(self) -> int:
        with self._condition:
            return self._version
