#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导航技能全链路测试（goto_block / goto_position 实体模式 / follow_entity / 范围约束）。

流程：
  1. 启动 Minecraft 服务器并登录 1 个 SkillAgent；
  2. 通过 RCON 在 agent 附近召唤一只猪，作为确定性的实体测试目标；
  3. 依次验证：
     - goto_block：已知方块 found/reached；未知方块名 block_name_unknown；范围内不存在 block_not_found；
     - goto_position 实体模式：无效 id entity_not_found；坐标+实体同传 invalid_params；有效实体到达附近；
     - follow_entity：start -> status(running) -> update -> stop(interrupted)；
       range_enabled + max_range 过小 -> entity_not_in_range；缺少实体引用 -> invalid_params；
     - postprocess 中文描述冒烟；
  4. 清理：disconnect agent -> 优雅停止服务器。
"""

# 运行：
#   & 'C:\Users\Chen Zijian\.conda\envs\mcagent\python.exe' script\test_navigation_skills.py

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


def nearest_non_self_entity(perception, preferred_name=None):
    self_id = (perception.get("self") or {}).get("entity", {}).get("id")
    candidates = [
        e for e in perception.get("entities", [])
        if e and e.get("id") != self_id and e.get("isValid", True) and e.get("position")
    ]
    if preferred_name:
        matched = [e for e in candidates if e.get("name") == preferred_name]
        if matched:
            candidates = matched
    if not candidates:
        return None
    return min(candidates, key=lambda e: e.get("distance") if e.get("distance") is not None else 1e9)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mc-port", type=int, default=25565, help="Minecraft 服务器端口")
    parser.add_argument("--bridge-port", type=int, default=3251, help="agent bridge 端口")
    parser.add_argument("--wait-server", type=int, default=180, help="等待服务器就绪超时(秒)")
    parser.add_argument("--keep-alive", action="store_true", help="测试完成后保留进程(调试用)")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    log_dir = ROOT / "script" / "logs"
    server = Server(server_dir=ROOT / "src" / "minecraft", log_dir=log_dir)
    agent: SkillAgent | None = None

    try:
        print("== 1/4 启动服务器并登录 agent ==")
        server.start()
        agent = SkillAgent(
            username="nav_test",
            host="localhost",
            port=args.mc_port,
            bridge_port=args.bridge_port,
            log_dir=log_dir,
        )
        agent.connect()
        print(f"agent 已登录，bridge_port={agent.bridge_port}")

        print("== 2/4 召唤测试实体（猪） ==")
        status = agent.get_status()
        pos = status.get("position") or {}
        summon_x = (pos.get("x") or 0) + 4
        summon_y = pos.get("y") or 64
        summon_z = pos.get("z") or 0
        server.send_command(f"summon minecraft:pig {summon_x} {summon_y} {summon_z}")
        time.sleep(4)

        perception = agent.get_raw_perception(block_radius=8)
        pig = nearest_non_self_entity(perception, preferred_name="pig")
        if not pig:
            print("[test] 感知中未找到猪实体，跳过实体用例")
            pig_id = None
        else:
            pig_id = pig.get("id")
            print(f"[test] 目标实体: pig id={pig_id}, distance={pig.get('distance')}")

        print("== 3/4 执行导航用例 ==")
        planner = agent.action_planner

        # --- goto_block ---
        blocks = perception.get("blocks", [])
        known_block = None
        for candidate in ("grass_block", "stone", "dirt", "sand", "gravel"):
            if any(b.get("name") == candidate for b in blocks):
                known_block = candidate
                break
        if known_block is None:
            known_block = next(
                (b.get("name") for b in blocks if b.get("name") not in ("air", "water", "cave_air", "void_air")),
                None,
            )

        if known_block:
            r = planner.execute(
                "navigation.goto_block",
                {"block_name": known_block, "search_max_distance": 48, "timeout": 30},
            )
            payload = r.result if isinstance(r.result, dict) else {}
            assert r.ok and payload.get("found"), (
                f"goto_block({known_block}) 失败: ok={r.ok}, error_code={error_code(r)}"
            )
            print(f"  goto_block {known_block}: found={payload.get('found')}, "
                  f"reached={payload.get('reached')}")

        r = planner.execute("navigation.goto_block", {"block_name": "not_a_real_block_xyz"})
        assert not r.ok and error_code(r) == "block_name_unknown", (
            f"goto_block 未知方块名预期 block_name_unknown, 实际 {error_code(r)}"
        )
        print("  goto_block 未知方块名 -> block_name_unknown OK")

        r = planner.execute("navigation.goto_block", {"block_name": "dragon_egg"})
        assert not r.ok and error_code(r) == "block_not_found", (
            f"goto_block 找不到预期 block_not_found, 实际 {error_code(r)}"
        )
        print("  goto_block 范围内不存在 -> block_not_found OK")

        # --- goto_position 实体模式 ---
        if pig_id is not None:
            r = planner.execute("navigation.goto_position", {"entity_id": 999999999, "range": 3, "timeout": 5})
            assert not r.ok and error_code(r) == "entity_not_found", (
                f"goto_position 无效实体 id 预期 entity_not_found, 实际 {error_code(r)}"
            )
            print("  goto_position 无效 id -> entity_not_found OK")

            r = planner.execute(
                "navigation.goto_position",
                {"entity_id": pig_id, "x": 1, "y": 64, "z": 1, "range": 3, "timeout": 5},
            )
            assert not r.ok and error_code(r) == "invalid_params", (
                f"goto_position 坐标+实体同传预期 invalid_params, 实际 {error_code(r)}"
            )
            print("  goto_position 坐标+实体同传 -> invalid_params OK")

            r = planner.execute(
                "navigation.goto_position",
                {"entity_id": pig_id, "range": 3, "timeout": 20},
            )
            payload = r.result if isinstance(r.result, dict) else {}
            assert r.ok and payload.get("reached"), (
                f"goto_position 实体模式未到达: ok={r.ok}, error_code={error_code(r)}"
            )
            print(f"  goto_position 实体模式: reached={payload.get('reached')}, "
                  f"target={payload.get('target', {}).get('name')}")

        # --- follow_entity ---
        if pig_id is not None:
            r = planner.start("navigation.follow_entity", {"entity_id": pig_id, "follow_range": 2.5})
            assert r.ok and r.action_id, f"follow_entity start 失败: {error_code(r)}"
            action_id = r.action_id
            st = planner.status(action_id)
            assert st.ok and st.status == "running", f"follow_entity 未处于 running: {st.raw}"
            print(f"  follow_entity start/status OK (action_id={action_id})")

            up = planner.update(action_id, {"follow_range": 3})
            assert up.ok, f"follow_entity update 失败: {error_code(up)}"
            print("  follow_entity update OK")

            stopped = planner.stop(action_id, reason="test_stop")
            assert stopped.ok and stopped.status == "interrupted", (
                f"follow_entity stop 预期 interrupted, 实际 status={stopped.status}, error={error_code(stopped)}"
            )
            print("  follow_entity stop -> interrupted OK")

            r = planner.start(
                "navigation.follow_entity",
                {"entity_id": pig_id, "range_enabled": True, "max_range": 0.5},
            )
            assert not r.ok and error_code(r) == "entity_not_in_range", (
                f"follow_entity 范围约束预期 entity_not_in_range, 实际 {error_code(r)}"
            )
            print("  follow_entity 范围约束 -> entity_not_in_range OK")

            r = planner.start("navigation.follow_entity", {})
            assert not r.ok and error_code(r) == "invalid_params", (
                f"follow_entity 缺实体引用预期 invalid_params, 实际 {error_code(r)}"
            )
            print("  follow_entity 缺实体引用 -> invalid_params OK")

        # --- postprocess 冒烟 ---
        described = postprocess_action_result(
            {
                "action": "navigation.goto_block",
                "ok": True,
                "result": {
                    "ok": True,
                    "found": True,
                    "block": {"name": "stone", "displayName": "Stone", "position": {"x": 1, "y": 64, "z": 1}},
                    "reached": True,
                    "position": {"x": 1, "y": 64, "z": 3},
                },
            }
        )
        assert "方块" in (described.get("description") or ""), f"postprocess 描述异常: {described}"
        print(f"  postprocess: {described.get('description')}")

        print("== 4/4 全部用例通过 ==")
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
