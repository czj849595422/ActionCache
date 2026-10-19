#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量指令调度: 两阶段 (准备 + 任务) 按时间顺序向 Minecraft 服务器发送指令。

指令格式 (每行一条, 忽略空行与 # 注释):

    # 注释
    [prepare]                 # 切换到准备阶段 (环境指令)
    weather clear
    time set day
    [task]                    # 切换到任务阶段 (定时指令)
    5s say hello
    100t say tick

- 默认阶段为任务阶段 (旧格式无标记时全部按任务指令处理, 向后兼容);
- 准备阶段: 纯环境指令, 调度启动后立即按文件顺序执行, 不支持时间前缀;
- 任务阶段: 收到"任务开始信号"后开始计时, 时间前缀格式同前:
  `<数字><单位>` (5s=现实秒, 100t=游戏 tick), 裸数字用默认单位,
  无时间前缀 = 信号到达时立即执行; 发送前自动去掉开头 `/`;
- 准备阶段标记不能出现在任务指令之后。
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Literal, Tuple, Union

_TIME_PREFIX_RE = re.compile(r"^(-?\d+(?:\.\d+)?)([st]?)(?:\s+(.*))?$")
_BAD_UNIT_RE = re.compile(r"^\d+(?:\.\d+)?[a-zA-Z]")
_PREPARE_MARKER_RE = re.compile(r"^\[prepare\]\s*$", re.IGNORECASE)
_TASK_MARKER_RE = re.compile(r"^\[task\]\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class CommandEntry:
    """一条带时间的任务阶段指令。"""

    time: float
    unit: Literal["s", "t"]
    command: str
    line_no: int


@dataclass
class BatchPlan:
    """两阶段批量计划: 准备阶段 (环境指令) + 任务阶段 (定时指令)。"""

    prepare: List[str] = field(default_factory=list)
    task: List[CommandEntry] = field(default_factory=list)


@dataclass
class BatchResult:
    """批量执行结果。"""

    total: int
    sent: List[Tuple[CommandEntry, str]] = field(default_factory=list)
    errors: List[Tuple[CommandEntry, str]] = field(default_factory=list)
    prepared: List[Tuple[str, str]] = field(default_factory=list)
    prepare_errors: List[Tuple[str, str]] = field(default_factory=list)
    started: bool = False
    cancelled: bool = False


def _parse_line(line: str, line_no: int, default_unit: str):
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    match = _TIME_PREFIX_RE.match(stripped)
    if match:
        time_str, unit, command = match.group(1), match.group(2) or default_unit, (
            match.group(3) or ""
        ).strip()
        value = float(time_str)
        if value < 0:
            raise ValueError(f"第 {line_no} 行: 时间不能为负: {stripped!r}")
        if unit not in ("s", "t"):
            raise ValueError(f"第 {line_no} 行: 未知时间单位: {stripped!r}")
        if not command:
            raise ValueError(f"第 {line_no} 行: 缺少指令内容: {stripped!r}")
        return CommandEntry(
            time=value, unit=unit, command=command.lstrip("/"), line_no=line_no
        )

    if _BAD_UNIT_RE.match(stripped):
        raise ValueError(f"第 {line_no} 行: 未知时间单位或格式: {stripped!r}")

    # 无时间前缀: 收到开始信号时立即执行
    return CommandEntry(
        time=0.0, unit="s", command=stripped.lstrip("/"), line_no=line_no
    )


def _parse_prepare_line(line: str, line_no: int):
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if _TIME_PREFIX_RE.match(stripped) or _BAD_UNIT_RE.match(stripped):
        raise ValueError(f"第 {line_no} 行: 准备阶段不支持时间前缀: {stripped!r}")
    return stripped.lstrip("/")


def parse_batch_plan(text: str, default_unit: str = "s") -> BatchPlan:
    """解析两阶段指令文本, 准备指令保序, 任务指令按 (时间, 行号) 排序。"""
    if default_unit not in ("s", "t"):
        raise ValueError(f"默认单位必须是 s 或 t, 当前: {default_unit!r}")

    plan = BatchPlan()
    phase = "task"
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _PREPARE_MARKER_RE.match(stripped):
            if plan.task:
                raise ValueError(
                    f"第 {line_no} 行: 准备阶段标记不能出现在任务阶段之后: {stripped!r}"
                )
            phase = "prepare"
            continue
        if _TASK_MARKER_RE.match(stripped):
            phase = "task"
            continue

        if phase == "prepare":
            command = _parse_prepare_line(stripped, line_no)
            if command is not None:
                plan.prepare.append(command)
        else:
            entry = _parse_line(stripped, line_no, default_unit)
            if entry is not None:
                plan.task.append(entry)

    plan.task.sort(key=lambda e: (e.time, e.line_no))
    return plan


def parse_batch_text(text: str, default_unit: str = "s") -> BatchPlan:
    """解析多行指令文本 (同 parse_batch_plan)。"""
    return parse_batch_plan(text, default_unit=default_unit)


def parse_batch_file(path, default_unit: str = "s") -> BatchPlan:
    """从文件读取并解析两阶段指令文本。"""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return parse_batch_plan(text, default_unit=default_unit)


class BatchCommandScheduler:
    """两阶段阻塞式批量指令调度器。

    流程: run_prepare() 执行准备阶段环境指令 -> wait_for_start() 等待
    任务开始信号 -> run_task() 从信号时刻起按时间执行任务指令。
    现实时间用 time.monotonic() 相对信号时刻; 游戏时间用
    "time query gametime" 的相对差值 (单调不回绕)。
    """

    def __init__(
        self,
        server,
        plan: BatchPlan,
        poll_interval: float = 0.5,
        response_timeout: float = 10.0,
    ):
        self.server = server
        self.plan = plan
        self.poll_interval = poll_interval
        self.response_timeout = response_timeout
        self._start_event = threading.Event()
        self._cancel_event = threading.Event()
        self._result_lock = threading.Lock()
        self._result = BatchResult(total=len(self.plan.task))

    def cancel(self) -> None:
        """请求停止尚未发送的任务指令。"""
        self._cancel_event.set()

    def result_snapshot(self) -> BatchResult:
        """返回当前执行结果副本，供其它线程安全查询。"""
        with self._result_lock:
            result = self._result
            return BatchResult(
                total=result.total,
                sent=list(result.sent),
                errors=list(result.errors),
                prepared=list(result.prepared),
                prepare_errors=list(result.prepare_errors),
                started=result.started,
                cancelled=result.cancelled,
            )

    def _set_result(self, result: BatchResult) -> None:
        with self._result_lock:
            self._result = result

    def signal_task_start(self) -> None:
        """发送任务开始信号: 任务阶段从此刻开始计时。"""
        self._start_event.set()

    def wait_for_start(self, timeout: Union[float, None] = None) -> bool:
        """等待任务开始信号, 返回是否收到信号; 服务器停止时抛错。"""
        deadline = None if timeout is None else time.time() + timeout
        while True:
            if self._start_event.is_set():
                return True
            if self._cancel_event.is_set():
                return False
            if not self.server.is_running:
                raise RuntimeError("服务器已停止, 等待开始信号中断")
            if deadline is not None and time.time() >= deadline:
                return False
            time.sleep(0.2)

    def _query_gametime(self) -> int:
        response = self.server.send_command(
            "time query gametime", response_timeout=self.response_timeout
        )
        match = re.search(r"-?\d+", response)
        if not match:
            raise RuntimeError(f"无法解析 gametime 响应: {response!r}")
        return int(match.group(0))

    def _fire(self, entry: CommandEntry, result: BatchResult) -> None:
        timestamp = time.strftime("%H:%M:%S")
        print(
            f"[scheduler] [{timestamp}] t={entry.time:g}{entry.unit} "
            f"发送: {entry.command}"
        )
        try:
            response = self.server.send_command(
                entry.command, response_timeout=self.response_timeout
            )
            print(f"[scheduler]   响应: {response!r}")
            with self._result_lock:
                result.sent.append((entry, response))
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduler]   失败: {exc}")
            with self._result_lock:
                result.errors.append((entry, str(exc)))

    def run_prepare(self) -> BatchResult:
        """顺序执行准备阶段环境指令 (调度启动时立即执行)。"""
        result = BatchResult(total=len(self.plan.task))
        self._set_result(result)
        for command in self.plan.prepare:
            if self._cancel_event.is_set():
                result.cancelled = True
                break
            print(f"[scheduler] [准备] 发送: {command}")
            try:
                response = self.server.send_command(
                    command, response_timeout=self.response_timeout
                )
                print(f"[scheduler]   响应: {response!r}")
                with self._result_lock:
                    result.prepared.append((command, response))
            except Exception as exc:  # noqa: BLE001
                print(f"[scheduler]   失败: {exc}")
                with self._result_lock:
                    result.prepare_errors.append((command, str(exc)))
        return result

    def run_task(self) -> BatchResult:
        """从收到开始信号起计时, 按时间顺序执行任务阶段指令。"""
        if not self.server.is_running:
            raise RuntimeError("服务器未运行, 无法执行批量指令")

        result = BatchResult(total=len(self.plan.task), started=True)
        self._set_result(result)
        if not self.plan.task:
            return result

        has_game = any(entry.unit == "t" for entry in self.plan.task)
        start_real = time.monotonic()
        start_game = self._query_gametime() if has_game else 0

        pending = list(self.plan.task)
        try:
            while pending:
                if self._cancel_event.is_set():
                    result.cancelled = True
                    break
                real_elapsed = time.monotonic() - start_real
                game_elapsed = (
                    self._query_gametime() - start_game if has_game else None
                )

                remaining: List[CommandEntry] = []
                for index, entry in enumerate(pending):
                    if self._cancel_event.is_set():
                        result.cancelled = True
                        remaining.extend(pending[index:])
                        break
                    elapsed = game_elapsed if entry.unit == "t" else real_elapsed
                    if elapsed >= entry.time:
                        self._fire(entry, result)
                    else:
                        remaining.append(entry)
                pending = remaining
                if not pending:
                    break
                self._cancel_event.wait(self.poll_interval)
        except KeyboardInterrupt:
            result.cancelled = True
            print("[scheduler] 收到中断, 停止调度")

        return result

    def run(self, start_delay: Union[float, None] = None) -> BatchResult:
        """完整流程: 准备阶段 -> 等待开始信号 -> 任务阶段。

        start_delay: None = 等待 signal_task_start() (Ctrl+C 可取消);
        数值 = 准备完成后等待该秒数自动发送开始信号 (0 = 立即开始)。
        """
        if not self.server.is_running:
            raise RuntimeError("服务器未运行, 无法执行批量指令")

        result = self.run_prepare()
        if not self.plan.task:
            return result

        try:
            if start_delay is None:
                print("[scheduler] 准备阶段完成, 等待任务开始信号 ...")
                if not self.wait_for_start():
                    result.cancelled = True
                    return result
            else:
                if start_delay > 0:
                    print(f"[scheduler] 准备阶段完成, {start_delay:g}s 后自动开始")
                    if self._cancel_event.wait(start_delay):
                        result.cancelled = True
                        return result
                self.signal_task_start()

            task_result = self.run_task()
            result.sent = task_result.sent
            result.errors = task_result.errors
            result.cancelled = task_result.cancelled
            result.started = task_result.started
            result.total = task_result.total
            self._set_result(result)
        except KeyboardInterrupt:
            result.cancelled = True
            print("[scheduler] 收到中断, 停止")

        return result
