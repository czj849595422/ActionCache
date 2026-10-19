#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动编排层的基础工具: 子进程管理与 HTTP JSON 请求。"""

import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path


class ManagedProcess:
    """启动子进程并持续读取 stdout, 支持按正则等待就绪/失败。"""

    def __init__(
        self,
        name,
        cmd,
        cwd,
        env=None,
        ready_pattern=None,
        fail_patterns=(),
        log_dir=None,
    ):
        self.name = name
        self.cmd = [str(c) for c in cmd]
        self.cwd = str(cwd)
        self.env = env
        self.ready_pattern = re.compile(ready_pattern) if ready_pattern else None
        self.fail_patterns = [re.compile(p) for p in fail_patterns]
        self.proc = None
        self.lines = []
        self.lock = threading.Lock()
        self.ready_event = threading.Event()
        self.fail_event = threading.Event()
        self.log_fh = None
        if log_dir:
            log_dir = Path(log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            self.log_fh = (log_dir / f"{name}.log").open(
                "a", encoding="utf-8", errors="replace"
            )

    def start(self):
        env = dict(os.environ)
        if self.env:
            env.update(self.env)
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=self.cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        print(f"[demo] {self.name} started, pid={self.proc.pid}")
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for raw in iter(self.proc.stdout.readline, ""):
            line = raw.rstrip("\r\n")
            if not line:
                continue
            with self.lock:
                self.lines.append(line)
            print(f"[{self.name}] {line}")
            if self.log_fh:
                self.log_fh.write(line + "\n")
                self.log_fh.flush()
            if self.ready_pattern and self.ready_pattern.search(line):
                self.ready_event.set()
            for pat in self.fail_patterns:
                if pat.search(line):
                    self.fail_event.set()

    def tail(self, n=5):
        with self.lock:
            return "\n".join(self.lines[-n:])

    @property
    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def wait_ready(self, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.fail_event.is_set():
                raise RuntimeError(f"{self.name} 启动失败, 最近输出:\n{self.tail(5)}")
            if self.ready_event.is_set():
                return True
            if not self.is_running:
                raise RuntimeError(f"{self.name} 进程提前退出, 最近输出:\n{self.tail(8)}")
            time.sleep(0.2)
        raise TimeoutError(f"{self.name} 在 {timeout}s 内未就绪, 最近输出:\n{self.tail(8)}")

    def send_line(self, text):
        if self.proc and self.proc.stdin:
            try:
                self.proc.stdin.write(text + "\n")
                self.proc.stdin.flush()
            except Exception as exc:  # noqa: BLE001
                print(f"[demo] 向 {self.name} 写入失败: {exc}")

    def wait_new_lines(self, mark, timeout=5.0):
        """等待并返回从行数快照 mark 之后新出现的输出行。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if len(self.lines) > mark:
                    return self.lines[mark:]
            time.sleep(0.05)
        with self.lock:
            return self.lines[mark:]

    def stop(self, grace=30):
        if not self.is_running:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=grace)
            print(f"[demo] {self.name} 已停止 (pid={self.proc.pid})")
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
            print(f"[demo] {self.name} 停止超时, 已强制结束")
        finally:
            if self.log_fh:
                self.log_fh.close()
                self.log_fh = None


def post_json(url, payload, timeout=120):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return resp.status, json.loads(body)
