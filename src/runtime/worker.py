"""通用后台循环：可启动/停止的 daemon 线程循环基类。"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable


class LoopWorker:
    """后台循环 worker。

    子类实现 iterate()（单步逻辑），可选覆盖 setup()/teardown()。
    iterate() 抛出的异常视为致命错误：触发 on_error 回调并停止循环。
    单个 worker 只能 start 一次（停止后不可复用）。
    """

    def __init__(
        self,
        *,
        name: str = "loop-worker",
        loop_interval: float = 0.0,
        on_error: Callable[[Exception, "LoopWorker"], None] | None = None,
    ) -> None:
        if loop_interval < 0:
            raise ValueError("loop_interval must not be negative")
        self.name = name
        self.loop_interval = loop_interval
        self.on_error = on_error
        self._stop_event = threading.Event()
        self._setup_done = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stop_reason: str | None = None
        self._last_error: Exception | None = None
        self._iteration_count = 0
        self._last_iterated_at: float | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError(f"worker {self.name!r} is already running")
            if self._stop_event.is_set():
                raise RuntimeError(f"worker {self.name!r} has already been stopped")
            self._stop_reason = None
            self._last_error = None
            self._iteration_count = 0
            self._last_iterated_at = None
            self._setup_done.clear()
            thread = threading.Thread(
                target=self._run,
                name=self.name,
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def stop(
        self,
        reason: str = "stopped",
        timeout: float | None = None,
    ) -> None:
        """请求停止并等待线程退出（幂等；从自身线程调用时不 join）。"""
        with self._lock:
            if self._stop_reason is None:
                self._stop_reason = reason
        self._stop_event.set()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=timeout)

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def ready(self) -> bool:
        """setup() 已完成（或循环已结束）。"""
        return self._setup_done.is_set()

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self._setup_done.wait(timeout=timeout)

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    @property
    def stop_reason(self) -> str | None:
        with self._lock:
            return self._stop_reason

    @property
    def last_error(self) -> Exception | None:
        with self._lock:
            return self._last_error

    @property
    def iteration_count(self) -> int:
        with self._lock:
            return self._iteration_count

    @property
    def last_iterated_at(self) -> float | None:
        with self._lock:
            return self._last_iterated_at

    # ------------------------------------------------------------------
    # 子类钩子
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """循环开始前执行一次（可选覆盖）。"""

    def iterate(self) -> None:
        """单步逻辑（必须覆盖）。"""
        raise NotImplementedError(
            f"{type(self).__name__} must implement iterate()"
        )

    def teardown(self) -> None:
        """循环结束后执行一次（可选覆盖）。"""

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _run(self) -> None:
        try:
            self.setup()
            self._setup_done.set()
            while not self._stop_event.is_set():
                self.iterate()
                with self._lock:
                    self._iteration_count += 1
                    self._last_iterated_at = time.time()
                if self.loop_interval > 0:
                    self._stop_event.wait(self.loop_interval)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._last_error = exc
                if self._stop_reason is None:
                    self._stop_reason = f"error: {type(exc).__name__}"
            if self.on_error is not None:
                try:
                    self.on_error(exc, self)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self._setup_done.set()
            try:
                self.teardown()
            finally:
                self._stop_event.set()
