#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""chat 技能测试（chat.say / chat.whisper / 参数校验）。

流程：
  1. 启动 Minecraft 服务器并登录 1 个 SkillAgent；
  2. chat.say 发送公开消息，并从服务器控制台日志确认收到；
  3. chat.whisper 私聊（向自身发送，验证动作链路）；
  4. 缺少 message 参数 -> invalid_params；postprocess 中文反馈冒烟；
  5. 清理：disconnect、停止服务器。
"""

# 运行：
#   & 'C:\Users\Chen Zijian\.conda\envs\mcagent\python.exe' script\test_chat.py

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action.result_postprocessor import postprocess_action_result  # noqa: E402
from src.agents.skill_agent import SkillAgent  # noqa: E402
from src.launcher.server import Server  # noqa: E402


def error_code(result) -> str | None:
    payload = result.result if isinstance(result.result, dict) else {}
    return payload.get("error_code") or getattr(result, "error", None)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mc-port", type=int, default=25565)
    parser.add_argument("--bridge-port", type=int, default=3258)
    parser.add_argument("--wait-server", type=int, default=180)
    parser.add_argument("--keep-alive", action="store_true", help="测试完成后保留进程(调试用)")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    log_dir = ROOT / "script" / "logs"
    server = Server(server_dir=ROOT / "src" / "minecraft", log_dir=log_dir)
    agent: SkillAgent | None = None

    try:
        print("== 1/5 启动服务器并登录 agent ==")
        server.start()
        agent = SkillAgent(
            username="chat_test",
            host="localhost",
            port=args.mc_port,
            bridge_port=args.bridge_port,
            log_dir=log_dir,
        )
        agent.connect()
        planner = agent.action_planner
        print(f"agent 已登录，bridge_port={agent.bridge_port}")

        print("== 2/5 chat.say 公开消息 ==")
        message = "hello from chat skill 12345"
        r = planner.execute("chat.say", {"message": message})
        assert r.ok, f"chat.say 失败: {error_code(r)}"
        payload = r.result
        assert payload.get("message") == message and payload.get("message_type") == "say", payload
        print("  chat.say OK:", payload.get("message"))

        time.sleep(2)
        received = any(
            "<chat_test> hello from chat skill 12345" in line
            for line in server._proc.lines
        )
        assert received, "服务器控制台未收到 chat 消息"
        print("  服务器控制台确认收到 OK")

        print("== 3/5 chat.whisper 私聊 ==")
        r = planner.execute("chat.whisper", {"username": "chat_test", "message": "self check"})
        assert r.ok, f"chat.whisper 失败: {error_code(r)}"
        payload = r.result
        assert payload.get("username") == "chat_test" and payload.get("message_type") == "whisper", payload
        print("  chat.whisper OK:", payload.get("message"))

        print("== 4/5 参数校验与 postprocess ==")
        r = planner.execute("chat.say", {})
        assert not r.ok and error_code(r) == "invalid_params", (
            f"缺少 message 预期 invalid_params, 实际 {error_code(r)}"
        )
        print("  缺少 message -> invalid_params OK")

        described = postprocess_action_result(
            {
                "action": "chat.say",
                "ok": True,
                "result": {"ok": True, "message": "hello"},
            }
        )
        assert "已发送" in (described.get("description") or ""), described
        print("  postprocess OK:", described.get("description"))

        print("== 5/5 全部用例通过 ==")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[test] 失败: {exc}")
        return 1
    finally:
        if not args.keep_alive:
            if agent:
                try:
                    agent.disconnect()
                except Exception as exc:  # noqa: BLE001
                    print(f"[test] 清理 agent 失败: {exc}")
            server.stop()
            time.sleep(1)
        else:
            print(f"[test] --keep-alive: 保留进程 (server pid={server.pid})")


if __name__ == "__main__":
    sys.exit(main())
