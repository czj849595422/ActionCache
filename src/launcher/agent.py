#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""智能体管理类 (第一版: 登录服务器 / 退出)。"""

import json

from .utils import post_json


class Agent:
    """管理单个 Mineflayer agent 的生命周期 (经由单 agent 桥 HTTP API)。

    第一版职责:
      - login(port): 让桥创建 bot 登录 Minecraft 服务器, 返回观测数据
      - quit(): 让 bot 退出服务器

    后续可扩展: step 动作执行、观测解析、多 agent 管理等。
    """

    def __init__(
        self,
        bridge_url="http://127.0.0.1:3000",
        username="test_agent",
        wait_ticks=5,
        request_timeout=120,
    ):
        self.bridge_url = bridge_url.rstrip("/")
        self.username = username
        self.wait_ticks = wait_ticks
        self.request_timeout = request_timeout
        self.connected = False

    def login(self, port):
        """部署 agent 登录服务器, 返回解析后的观测列表。"""
        status, data = post_json(
            f"{self.bridge_url}/start",
            {
                "port": port,
                "username": self.username,
                "waitTicks": self.wait_ticks,
                "spread": False,
            },
            timeout=self.request_timeout,
        )
        if status != 200:
            raise RuntimeError(f"agent 登录失败 (/start 返回 {status}): {data}")
        self.connected = True
        return json.loads(data["bot"])

    def quit(self):
        """让 agent 退出服务器, 未登录时直接返回。"""
        if not self.connected:
            return None
        try:
            status, data = post_json(f"{self.bridge_url}/stop", {}, timeout=30)
            if status != 200:
                raise RuntimeError(f"agent 退出失败 (/stop 返回 {status}): {data}")
        finally:
            self.connected = False
        return data
