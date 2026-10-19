#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""箱子注册表共享测试：bot1 注册，bot2 也能用（chest_id / chest_name）。

流程：
  1. 启动 Minecraft 服务器，登录 2 个 SkillAgent（bot1/bot2），共用一份
     注册表文件（CHEST_REGISTRY_FILE 指向 .tmp 下的临时文件）；
  2. bot1 放置 3 个单箱 + 1 个大箱子并 register_chests（带名称）；
  3. bot2 验证：
     - list_chests 能看到 bot1 注册的 storage/food（跨进程共享）；
     - 可用 chest_name / chest_id 执行 place/inspect/pick；
     - 未知 chest_name -> chest_name_unknown；重复名称 -> chest_name_duplicate；
     - 坐标方式仍兼容；
  4. 清理：移除箱子、断开两个 agent、停止服务器、删除临时注册表。
"""

# 运行：
#   & 'C:\Users\Chen Zijian\.conda\envs\mcagent\python.exe' script\test_container_chest_id.py

import argparse
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.skill_agent import SkillAgent  # noqa: E402
from src.launcher.server import Server  # noqa: E402


def error_code(result) -> str | None:
    payload = result.result if isinstance(result.result, dict) else {}
    return payload.get("error_code") or getattr(result, "error", None)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mc-port", type=int, default=25565)
    parser.add_argument("--bridge-port-start", type=int, default=3254)
    parser.add_argument("--wait-server", type=int, default=180)
    parser.add_argument("--keep-alive", action="store_true", help="测试完成后保留进程(调试用)")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    log_dir = ROOT / "script" / "logs"
    registry_file = ROOT / ".tmp" / "chest_registry_test.json"
    registry_file.unlink(missing_ok=True)

    server = Server(server_dir=ROOT / "src" / "minecraft", log_dir=log_dir)
    agents: list[SkillAgent] = []
    placed_chests: list[dict[str, int]] = []

    try:
        print("== 1/6 启动服务器并登录 2 个 agent（共享注册表） ==")
        server.start()
        server.send_command("difficulty peaceful")
        server.send_command("gamerule doMobSpawning false")
        time.sleep(1)

        def make_agent(username: str, index: int) -> SkillAgent:
            agent = SkillAgent(
                username=username,
                host="localhost",
                port=args.mc_port,
                bridge_port=args.bridge_port_start + index,
                log_dir=log_dir,
                bridge_env={"CHEST_REGISTRY_FILE": str(registry_file)},
            )
            agent.connect()
            return agent

        agent1 = make_agent("chest_test1", 0)
        agents.append(agent1)
        agent2 = make_agent("chest_test2", 1)
        agents.append(agent2)
        print(f"bot1 bridge={agent1.bridge_port}, bot2 bridge={agent2.bridge_port}")

        print("== 2/6 bot1 放置箱子并注册 ==")
        status = agent1.get_status()
        pos = status.get("position") or {}
        bx = math.floor(pos.get("x", 0))
        by = math.floor(pos.get("y", 0))
        bz = math.floor(pos.get("z", 0))
        chest_a = {"x": bx + 2, "y": by, "z": bz}
        chest_b = {"x": bx - 2, "y": by, "z": bz}
        chest_c = {"x": bx, "y": by, "z": bz + 2}
        double_1 = {"x": bx, "y": by, "z": bz - 2}
        double_2 = {"x": bx + 1, "y": by, "z": bz - 2}
        for c in (chest_a, chest_b, chest_c, double_1, double_2):
            server.send_command(f"setblock {c['x']} {c['y']} {c['z']} chest")
        placed_chests.extend([chest_a, chest_b, chest_c, double_1, double_2])
        time.sleep(2)

        key_a = f"{chest_a['x']},{chest_a['y']},{chest_a['z']}"
        key_b = f"{chest_b['x']},{chest_b['y']},{chest_b['z']}"
        r = agent1.action_planner.execute(
            "container.register_chests",
            {"max_distance": 8, "names": {key_a: "storage", key_b: "food"}},
        )
        assert r.ok, f"bot1 register_chests 失败: {error_code(r)}"
        payload = r.result
        assert payload.get("registered_count", 0) >= 4, f"注册数量异常: {payload.get('registered_count')}"
        double_ids = [
            c for c in payload.get("chests", [])
            if c.get("position") in (double_1, double_2)
        ]
        assert len(double_ids) == 1, f"大箱子应只注册 1 个 id, 实际 {len(double_ids)}"
        assert registry_file.exists(), "注册表文件未生成"
        print(f"  bot1 注册 OK: 新注册 {payload.get('registered_count')}, 大箱子归一化 OK, 文件={registry_file.name}")

        print("== 3/6 bot2 读取共享注册表 ==")
        r = agent2.action_planner.execute("container.list_chests", {})
        assert r.ok, f"bot2 list_chests 失败: {error_code(r)}"
        chests = r.result.get("chests", [])
        storage = next((c for c in chests if c.get("name") == "storage"), None)
        food = next((c for c in chests if c.get("name") == "food"), None)
        assert storage and food, f"bot2 未看到共享注册: {chests}"
        id_a = storage["id"]
        print(f"  bot2 共享 OK: storage=#{id_a}@({storage['position']}), food=#{food['id']}@({food['position']})")

        print("== 4/6 bot2 用编号/名称操作箱子 ==")
        server.send_command("give chest_test2 cobblestone 8")
        time.sleep(2)

        r = agent2.action_planner.execute(
            "container.place_object_to_chest",
            {"chest_name": "storage", "items": [{"name": "cobblestone", "count": 3}]},
        )
        assert r.ok, f"bot2 按名称放入失败: {error_code(r)}"
        print("  bot2 place via chest_name OK")

        r = agent2.action_planner.execute("container.inspect_chest", {"chest_id": id_a})
        assert r.ok, f"bot2 按编号查询失败: {error_code(r)}"
        cobble = [i for i in r.result.get("chest_items", []) if i.get("name") == "cobblestone"]
        assert cobble and cobble[0].get("count") == 3, f"箱内圆石数量异常: {r.result.get('chest_items')}"
        print("  bot2 inspect via chest_id OK (箱内圆石 x3)")

        r = agent2.action_planner.execute(
            "container.pick_object_from_chest",
            {"chest_name": "storage", "items": [{"name": "cobblestone", "count": 3}]},
        )
        assert r.ok, f"bot2 按名称取出失败: {error_code(r)}"
        print("  bot2 pick via chest_name OK")

        print("== 5/6 错误路径 ==")
        r = agent2.action_planner.execute("container.inspect_chest", {"chest_name": "not_registered"})
        assert not r.ok and error_code(r) == "chest_name_unknown", (
            f"未知名称预期 chest_name_unknown, 实际 {error_code(r)}"
        )
        print("  bot2 未知 chest_name -> chest_name_unknown OK")

        r = agent2.action_planner.execute(
            "container.register_chests",
            {"max_distance": 8, "names": {key_b: "storage"}},
        )
        assert not r.ok and error_code(r) == "chest_name_duplicate", (
            f"重复名称预期 chest_name_duplicate, 实际 {error_code(r)}"
        )
        print("  bot2 重复 chest_name -> chest_name_duplicate OK")

        r = agent2.action_planner.execute("container.inspect_chest", {"position": chest_a})
        assert r.ok, f"bot2 坐标方式查询失败: {error_code(r)}"
        print("  bot2 坐标方式兼容 OK")

        print("== 6/6 全部用例通过 ==")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[test] 失败: {exc}")
        return 1
    finally:
        if not args.keep_alive:
            for c in placed_chests:
                try:
                    server.send_command(f"setblock {c['x']} {c['y']} {c['z']} air")
                except Exception as exc:  # noqa: BLE001
                    print(f"[test] 清理箱子 {c} 失败: {exc}")
            for agent in reversed(agents):
                try:
                    agent.disconnect()
                except Exception as exc:  # noqa: BLE001
                    print(f"[test] 清理 {agent.username} 失败: {exc}")
            server.stop()
            registry_file.unlink(missing_ok=True)
            time.sleep(1)
        else:
            print(f"[test] --keep-alive: 保留进程 (server pid={server.pid})")


if __name__ == "__main__":
    sys.exit(main())
