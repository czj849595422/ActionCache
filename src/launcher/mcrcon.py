#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcrcon 封装: 通过 RCON 向 Minecraft 服务器发送指令并获取响应。"""

import socket
import threading
from pathlib import Path

from mcrcon import MCRcon, MCRconException


class PersistentRcon:
    """Keep one RCON connection open for serialized command dispatch."""

    def __init__(self, host="127.0.0.1", port=25575, password="", timeout=5.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._client = None
        self._lock = threading.RLock()

    @property
    def connected(self):
        with self._lock:
            return self._client is not None and self._client.socket is not None

    def connect(self):
        with self._lock:
            if self._client is not None and self._client.socket is not None:
                return
            client = MCRcon(
                self.host,
                self.password,
                self.port,
                timeout=self.timeout,
            )
            try:
                client.connect()
            except Exception:
                client.disconnect()
                raise
            self._client = client

    def command(self, command, timeout=None):
        """Send one command and keep the authenticated connection alive."""
        request_timeout = self.timeout if timeout is None else float(timeout)
        with self._lock:
            try:
                self.connect()
                self._client.timeout = request_timeout
                if self._client.socket is not None:
                    # Windows does not use mcrcon's SIGALRM fallback.
                    self._client.socket.settimeout(max(request_timeout, 5.0))
                return self._client.command(command)
            except MCRconException as exc:
                self.close()
                raise RuntimeError(f"RCON 指令失败: {exc}") from exc
            except socket.timeout as exc:
                self.close()
                raise TimeoutError(
                    f"RCON 指令超时 ({request_timeout}s): {command}"
                ) from exc
            except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
                self.close()
                raise RuntimeError(f"RCON 连接失败: {exc}") from exc

    def close(self):
        with self._lock:
            if self._client is not None:
                try:
                    self._client.disconnect()
                finally:
                    self._client = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def load_rcon_config(server_dir):
    """从 server.properties 读取 RCON 配置。"""
    props = _load_server_properties(server_dir)
    return {
        "enabled": props.get("enable-rcon", "false").lower() == "true",
        "host": "127.0.0.1",
        "port": int(props.get("rcon.port", "25575")),
        "password": props.get("rcon.password", ""),
    }


def load_server_port(server_dir):
    """从 server.properties 读取 Minecraft 游戏连接端口。"""
    props = _load_server_properties(server_dir)
    try:
        return int(props.get("server-port", "25565"))
    except ValueError as exc:
        raise ValueError(f"server-port 必须为整数: {server_dir}") from exc


def _load_server_properties(server_dir):
    props = {}
    path = Path(server_dir) / "server.properties"
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                props[key.strip()] = value.strip()
    return props


def rcon_command(command, host="127.0.0.1", port=25575, password="", timeout=5.0):
    """通过 mcrcon 库发送 RCON 指令, 返回服务器响应文本。"""
    try:
        with MCRcon(host, password, port, timeout=timeout) as mcr:
            # Windows 下 mcrcon 的 SIGALRM 超时不生效, 用 socket 超时兜底
            if mcr.socket is not None:
                mcr.socket.settimeout(max(timeout, 5.0))
            return mcr.command(command)
    except MCRconException as exc:
        raise RuntimeError(f"RCON 指令失败: {exc}") from exc
    except socket.timeout as exc:
        raise TimeoutError(f"RCON 指令超时 ({timeout}s): {command}") from exc
    except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
        raise RuntimeError(f"RCON 连接失败: {exc}") from exc


def rcon_commands(commands, host="127.0.0.1", port=25575, password="", timeout=5.0):
    """在同一条 RCON 连接中按顺序发送多条指令。"""
    commands = list(commands)
    if not commands:
        return []
    try:
        with MCRcon(host, password, port, timeout=timeout) as mcr:
            if mcr.socket is not None:
                mcr.socket.settimeout(max(timeout, 5.0))
            return [mcr.command(command) for command in commands]
    except MCRconException as exc:
        raise RuntimeError(f"RCON 批量指令失败: {exc}") from exc
    except socket.timeout as exc:
        raise TimeoutError(f"RCON 批量指令超时 ({timeout}s)") from exc
    except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
        raise RuntimeError(f"RCON 连接失败: {exc}") from exc
