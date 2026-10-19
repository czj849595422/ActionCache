#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Minecraft 服务器管理类 (第一版: 仅启动/关闭等基础功能)。"""

import time
import threading
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from queue import Empty, PriorityQueue

from .mcrcon import PersistentRcon, load_rcon_config
from .utils import ManagedProcess

ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class _CommandRequest:
    commands: tuple[str, ...]
    response_timeout: float
    wait_for_response: bool = True
    event: threading.Event = field(default_factory=threading.Event)
    responses: list[str] | None = None
    error: BaseException | None = None


class Server:
    """管理 Minecraft 服务器子进程 (Java 1.19 server.jar)。

    第一版职责:
      - start(): 启动服务器并等待就绪
      - stop(): 优雅关闭 (通过 mcrcon/RCON 发送 stop, 超时后强制终止)
      - send_command(): 通过 mcrcon/RCON 发送任意指令并返回响应文本
      - is_running: 当前运行状态

    后续可扩展: 端口/世界管理、RCON 指令、状态查询等。
    """

    READY_PATTERN = r"Done \([0-9.]+s\)! For help"
    FAIL_PATTERNS = (
        r"BindException",
        r"Address already in use",
        r"Failed to load properties",
        r"A problem occurred running the server",
        r"Error: Could not find or load main class",
    )

    def __init__(
        self,
        server_dir=None,
        java="java",
        xms="2G",
        xmx="2G",
        wait_ready_timeout=180,
        log_dir=None,
    ):
        self.server_dir = (
            Path(server_dir) if server_dir else ROOT / "src" / "minecraft"
        )
        if not (self.server_dir / "server.jar").exists():
            raise FileNotFoundError(f"未找到服务器: {self.server_dir / 'server.jar'}")
        self.wait_ready_timeout = wait_ready_timeout
        self.rcon = load_rcon_config(self.server_dir)
        self.rcon_enabled = self.rcon["enabled"] and bool(self.rcon["password"])
        self._command_lock = threading.RLock()
        self._command_state_lock = threading.RLock()
        self._command_queue = PriorityQueue()
        self._command_sequence = count()
        self._command_worker = None
        self._command_worker_stop = threading.Event()
        self._command_accepting = True
        self._rcon_connection = None
        self._proc = ManagedProcess(
            "mc-server",
            [java, f"-Xms{xms}", f"-Xmx{xmx}", "-jar", "server.jar", "nogui"],
            self.server_dir,
            ready_pattern=self.READY_PATTERN,
            fail_patterns=self.FAIL_PATTERNS,
            log_dir=log_dir,
        )

    @property
    def is_running(self):
        return self._proc.is_running

    @property
    def pid(self):
        return self._proc.proc.pid if self._proc.proc else None

    def start(self):
        """启动服务器并等待就绪, 已运行时直接返回。"""
        if self.is_running:
            return
        self._proc.start()
        self._proc.wait_ready(self.wait_ready_timeout)
        with self._command_state_lock:
            self._command_accepting = True
            self._command_worker_stop.clear()
        self._start_command_worker()

    def stop(self, grace=30):
        """优雅停止: 通过 mcrcon/RCON 发送 stop 等待退出, 超时再强制终止。"""
        if not self.is_running:
            with self._command_state_lock:
                self._command_accepting = False
            self._stop_command_worker()
            return
        with self._command_state_lock:
            self._command_accepting = False
        try:
            self._submit_commands(
                ("stop",),
                response_timeout=10.0,
                priority=0,
                allow_stopping=True,
                wait_for_response=self.rcon_enabled,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[demo] RCON stop 失败, 退回 stdin: {exc}")
            self._proc.send_line("stop")
        deadline = time.time() + grace
        while time.time() < deadline and self.is_running:
            time.sleep(0.2)
        if self.is_running:
            self._proc.stop(grace=10)
        self._stop_command_worker()

    def send_command(self, command, response_timeout=5.0, *, priority=100):
        """通过 mcrcon/RCON 发送指令并返回服务器响应文本。

        例如: send_command("say hello") / "time set day" / "list" / "seed" / "stop"。
        RCON 未开启时退回 stdin 控制台通道 (尽力而为, 不做响应归属保证)。
        """
        responses = self._submit_commands(
            (command,),
            response_timeout=response_timeout,
            priority=priority,
            wait_for_response=True,
        )
        return responses[0]

    def send_priority_command(self, command, response_timeout=5.0):
        """Send a command ahead of queued ordinary commands."""
        return self.send_command(
            command,
            response_timeout=response_timeout,
            priority=0,
        )

    def send_commands(self, commands, response_timeout=5.0):
        """在同一条控制连接中按顺序发送多条指令并返回响应。"""
        commands = list(commands)
        if not commands:
            return []
        return self._submit_commands(
            tuple(commands),
            response_timeout=response_timeout,
            priority=100,
            wait_for_response=True,
        )

    def _submit_commands(
        self,
        commands,
        *,
        response_timeout,
        priority,
        allow_stopping=False,
        wait_for_response=True,
    ):
        self._ensure_command_state()
        if not self.is_running:
            raise RuntimeError("服务器未运行, 无法发送指令")
        with self._command_state_lock:
            if not self._command_accepting and not allow_stopping:
                raise RuntimeError("服务器命令调度器已停止")
        self._start_command_worker()
        request = _CommandRequest(
            tuple(commands),
            float(response_timeout),
            wait_for_response=wait_for_response,
        )
        with self._command_state_lock:
            if self._command_worker_stop.is_set() or (
                not self._command_accepting and not allow_stopping
            ):
                raise RuntimeError("服务器命令调度器已停止")
            sequence = next(self._command_sequence)
            self._command_queue.put((int(priority), sequence, request))
        request.event.wait()
        if request.error is not None:
            raise request.error
        return request.responses or []

    def _ensure_command_state(self):
        """Initialize state lazily for lightweight test doubles using __new__."""
        if hasattr(self, "_command_state_lock"):
            return
        lock = getattr(self, "_command_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._command_lock = lock
        with lock:
            if hasattr(self, "_command_state_lock"):
                return
            self._command_state_lock = threading.RLock()
            self._command_queue = PriorityQueue()
            self._command_sequence = count()
            self._command_worker = None
            self._command_worker_stop = threading.Event()
            self._command_accepting = True
            self._rcon_connection = None

    def _start_command_worker(self):
        self._ensure_command_state()
        with self._command_state_lock:
            if self._command_worker is not None and self._command_worker.is_alive():
                return
            self._command_worker = threading.Thread(
                target=self._command_worker_loop,
                name="mc-rcon-dispatcher",
                daemon=True,
            )
            self._command_worker.start()

    def _stop_command_worker(self):
        if not hasattr(self, "_command_state_lock"):
            return
        with self._command_state_lock:
            self._command_accepting = False
            self._command_worker_stop.set()
            worker = self._command_worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=10.0)
        self._fail_pending_commands(RuntimeError("服务器命令调度器已停止"))

    def _command_worker_loop(self):
        try:
            while not self._command_worker_stop.is_set():
                try:
                    _, _, request = self._command_queue.get(timeout=0.1)
                except Empty:
                    continue
                try:
                    request.responses = self._execute_commands(
                        request.commands,
                        request.response_timeout,
                        request.wait_for_response,
                    )
                except BaseException as exc:  # noqa: BLE001
                    request.error = exc
                finally:
                    request.event.set()
                    self._command_queue.task_done()
        finally:
            if self._rcon_connection is not None:
                self._rcon_connection.close()
                self._rcon_connection = None
            self._fail_pending_commands(RuntimeError("服务器命令调度器已停止"))

    def _execute_commands(self, commands, response_timeout, wait_for_response=True):
        if not self.is_running:
            raise RuntimeError("服务器未运行, 无法发送指令")
        if self.rcon_enabled:
            if self._rcon_connection is None:
                self._rcon_connection = PersistentRcon(
                    host=self.rcon["host"],
                    port=self.rcon["port"],
                    password=self.rcon["password"],
                    timeout=response_timeout,
                )
            return [
                self._rcon_connection.command(command, timeout=response_timeout)
                for command in commands
            ]
        responses = []
        for command in commands:
            mark = len(self._proc.lines)
            self._proc.send_line(command)
            if not wait_for_response:
                responses.append("")
                continue
            responses.append(
                "\n".join(self._proc.wait_new_lines(mark, response_timeout))
            )
        return responses

    def _fail_pending_commands(self, error):
        if not hasattr(self, "_command_queue"):
            return
        while True:
            try:
                _, _, request = self._command_queue.get_nowait()
            except Empty:
                return
            request.error = error
            request.event.set()
            self._command_queue.task_done()

    def run_batch(self, commands_or_path, default_unit="s", poll_interval=0.5, response_timeout=10.0, start_delay=0.0):
        """批量执行指令: 准备阶段 + 任务阶段, 经 mcrcon 发送。

        文本 (含换行) 按文本解析, 否则按文件路径解析; 指令格式详见
        src/launcher/command_scheduler。start_delay: 准备完成后等待
        该秒数自动发送任务开始信号 (默认 0 = 立即开始)。返回 BatchResult。
        """
        from .command_scheduler import (
            BatchCommandScheduler,
            parse_batch_file,
            parse_batch_text,
        )

        path = Path(commands_or_path)
        if isinstance(commands_or_path, Path) or path.exists():
            plan = parse_batch_file(path, default_unit=default_unit)
        else:
            plan = parse_batch_text(commands_or_path, default_unit=default_unit)
        return BatchCommandScheduler(
            self,
            plan,
            poll_interval=poll_interval,
            response_timeout=response_timeout,
        ).run(start_delay=start_delay)
