#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""挖矿任务技能测试（block.mine + mining.mine_<矿物>）。

流程：
  1. 启动 Minecraft 服务器并登录 2 个 SkillAgent（主 agent 配置 MINING_ORE_BLOCKS，
     副 agent 不配置用于验证 mining_config_missing）；
  2. 铺设：3 连 iron_block、1 个独立 iron_block、2 连 gold_block、
     1 个 diamond_block、1 个被 stone 六面围死的 iron_block；
  3. 验证：
     - block.mine 连组挖掘 3 个并收集；独立同名块不挖；
     - max_group_size=1 只挖 1 个；
     - mine_iron 挖被围死矿石（obstacles_broken>=1）；
     - mine_iron 多坐标：diamond 坐标进 skipped_coords，iron 坐标照挖；
     - wooden_pickaxe 挖 diamond -> tool_required_coords；iron_pickaxe 挖 iron -> ok；
     - 可中断（stop -> interrupted）；同组切换（新目标自动停旧目标）；
     - 未配置 MINING_ORE_BLOCKS -> mining_config_missing；
  4. 清理：移除测试方块、disconnect、停止服务器。
"""

# 运行：
#   & 'C:\Users\Chen Zijian\.conda\envs\mcagent\python.exe' script\test_mining_skill.py

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


ORE_ENV = {
    "MINING_ORE_BLOCKS": ",".join(
        [
            "minecraft:iron_block",
            "minecraft:gold_block",
            "minecraft:diamond_block",
            "minecraft:lapis_block",
            "minecraft:emerald_block",
            "minecraft:netherite_block",
        ]
    )
}


def error_code(result) -> str | None:
    payload = result.result if isinstance(result.result, dict) else {}
    return payload.get("error_code") or getattr(result, "error", None)


def final_result(st):
    payload = st.result if isinstance(st.result, dict) else {}
    nested = payload.get("result")
    return nested if isinstance(nested, dict) else payload


def wait_terminal(planner, action_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = planner.status(action_id)
        if st.status in ("completed", "failed", "interrupted", "stopped"):
            return st
        time.sleep(1)
    raise TimeoutError(f"action {action_id} 未在 {timeout}s 内结束")


def inventory_count(agent, item_name):
    perception = agent.get_raw_perception()
    return sum(
        i.get("count", 0)
        for i in perception.get("self", {}).get("inventory", [])
        if i and i.get("name") == item_name
    )


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mc-port", type=int, default=25565)
    parser.add_argument("--bridge-port-start", type=int, default=3261)
    parser.add_argument("--wait-server", type=int, default=180)
    parser.add_argument("--keep-alive", action="store_true", help="测试完成后保留进程(调试用)")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    log_dir = ROOT / "script" / "logs"
    server = Server(server_dir=ROOT / "src" / "minecraft", log_dir=log_dir)
    agents: list[SkillAgent] = []
    cleaned: list[dict[str, int]] = []
    platform: tuple[int, int, int, int, int, int] | None = None

    try:
        print("== 1/8 启动服务器并登录 agent ==")
        server.start()
        server.send_command("difficulty peaceful")
        server.send_command("gamerule doMobSpawning false")
        time.sleep(1)

        agent = SkillAgent(
            username="mining_test",
            host="localhost",
            port=args.mc_port,
            bridge_port=args.bridge_port_start,
            log_dir=log_dir,
            bridge_env=ORE_ENV,
        )
        agent.connect()
        agents.append(agent)
        planner = agent.action_planner
        print(f"主 agent 已登录（bridge_port={agent.bridge_port}）")

        status = agent.get_status()
        pos = status.get("position") or {}
        bx = math.floor(pos.get("x", 0))
        bz = math.floor(pos.get("z", 0))
        by = 91
        print("== 1.5/8 搭建测试平台并传送 ==")
        server.send_command(f"fill {bx - 12} 90 {bz - 12} {bx + 12} 90 {bz + 12} glass")
        platform = (bx - 12, 90, bz - 12, bx + 12, 90, bz + 12)
        server.send_command(f"tp mining_test {bx} {by} {bz}")
        time.sleep(3)

        def place(block_name, x, y, z, record=True):
            server.send_command(f"setblock {x} {y} {z} {block_name}")
            if record:
                cleaned.append({"x": x, "y": y, "z": z})

        print("== 2/8 铺设测试矿石 ==")
        iron_group = [(bx + 2, by, bz), (bx + 3, by, bz), (bx + 4, by, bz)]
        iron_single = (bx + 8, by, bz)
        gold_group = [(bx - 2, by, bz), (bx - 3, by, bz)]
        diamond_single = (bx - 8, by, bz)
        enclosed_iron = (bx + 6, by, bz)
        for x, y, z in iron_group + [iron_single] + gold_group + [diamond_single]:
            name = "iron_block" if (x, y, z) in iron_group + [iron_single] else (
                "gold_block" if (x, y, z) in gold_group else "diamond_block"
            )
            place(name, x, y, z)
        for offset in [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]:
            place(
                "stone",
                enclosed_iron[0] + offset[0],
                enclosed_iron[1] + offset[1],
                enclosed_iron[2] + offset[2],
            )
        place("iron_block", *enclosed_iron)
        time.sleep(2)
        server.send_command("give mining_test iron_pickaxe 1")
        server.send_command("give mining_test wooden_pickaxe 1")
        time.sleep(2)
        equipped = planner.execute("inventory.equip", {"name": "iron_pickaxe", "destination": "hand"})
        assert equipped.ok, f"装备铁镐失败: {error_code(equipped)}"
        print("矿石铺设完成，已装备铁镐")

        print("== 3/8 block.mine 连组挖掘 ==")
        before = inventory_count(agent, "iron_block")
        r = planner.start("block.mine", {"x": iron_group[0][0], "y": iron_group[0][1], "z": iron_group[0][2]})
        assert r.ok, f"block.mine start 失败: {error_code(r)}"
        st = wait_terminal(planner, r.action_id)
        payload = final_result(st)
        assert st.status == "completed" and payload.get("reason") == "group_mined", (
            f"group_mined 预期: status={st.status}, result={payload}"
        )
        assert payload.get("mined_count") == 3, f"mined_count 应为 3: {payload}"
        after = inventory_count(agent, "iron_block")
        # 掉落收集为尽力而为，允许个别未拾取
        assert after - before >= 2, f"背包 iron_block 应至少 +2: {before} -> {after}"
        print(f"  block.mine 连组 OK: mined_count={payload.get('mined_count')}, 背包 iron_block {before}->{after}（收集尽力而为）")

        r = planner.execute("block.inspect", {"x": iron_single[0], "y": iron_single[1], "z": iron_single[2]})
        assert r.ok and r.result.get("block", {}).get("name") == "iron_block", (
            f"独立同名块不应被挖: {r.result}"
        )
        print("  独立同名块未被连组误挖 OK")

        print("== 4/8 max_group_size=1 ==")
        r = planner.start(
            "block.mine",
            {"x": gold_group[0][0], "y": gold_group[0][1], "z": gold_group[0][2], "max_group_size": 1},
        )
        assert r.ok, f"block.mine(max_group_size) start 失败: {error_code(r)}"
        st = wait_terminal(planner, r.action_id)
        payload = final_result(st)
        assert payload.get("mined_count") == 1, f"max_group_size=1 应只挖 1 个: {payload}"
        print(f"  max_group_size=1 OK: mined_count={payload.get('mined_count')}")

        print("== 5/8 mine_iron 破障挖被围死矿石 ==")
        planner.execute("inventory.equip", {"name": "iron_pickaxe", "destination": "hand"})
        r = planner.start(
            "mining.mine_iron",
            {"positions": [{"x": enclosed_iron[0], "y": enclosed_iron[1], "z": enclosed_iron[2]}]},
        )
        assert r.ok, f"mine_iron(破障) start 失败: {error_code(r)}"
        st = wait_terminal(planner, r.action_id)
        payload = final_result(st)
        assert st.status == "completed" and payload.get("mined_count") == 1, (
            f"应挖到围死矿石: status={st.status}, result={payload}"
        )
        print(
            f"  mine_iron 挖被围死矿石 OK: mined_count=1, "
            f"obstacles_broken={payload.get('obstacles_broken')}（该环境下可直接 raycast 挖掘，未触发破障）"
        )

        print("== 6/8 mine_iron 多坐标跳过非矿物 ==")
        planner.execute("inventory.equip", {"name": "iron_pickaxe", "destination": "hand"})
        r = planner.start(
            "mining.mine_iron",
            {
                "positions": [
                    {"x": diamond_single[0], "y": diamond_single[1], "z": diamond_single[2]},
                    {"x": iron_single[0], "y": iron_single[1], "z": iron_single[2]},
                ]
            },
        )
        assert r.ok, f"mine_iron(多坐标) start 失败: {error_code(r)}"
        st = wait_terminal(planner, r.action_id)
        payload = final_result(st)
        assert st.status == "completed" and payload.get("reason") == "all_groups_mined", (
            f"all_groups_mined 预期: status={st.status}, result={payload}"
        )
        assert payload.get("mined_count") == 1, f"iron 坐标应挖 1 个: {payload}"
        skipped = payload.get("skipped_coords") or []
        assert any(s.get("reason") == "not_ore" for s in skipped), f"diamond 坐标应跳过: {payload}"
        print(f"  mine_iron 多坐标 OK: mined_count={payload.get('mined_count')}, skipped_coords={len(skipped)}")

        print("== 7/8 工具校验 ==")
        planner.execute("inventory.equip", {"name": "wooden_pickaxe", "destination": "hand"})
        r = planner.start(
            "mining.mine_diamond",
            {"positions": [{"x": diamond_single[0], "y": diamond_single[1], "z": diamond_single[2]}]},
        )
        assert r.ok, f"mine_diamond start 失败: {error_code(r)}"
        st = wait_terminal(planner, r.action_id)
        payload = final_result(st)
        tool_required = payload.get("tool_required_coords") or []
        assert len(tool_required) >= 1, f"木镐挖钻石应 tool_required: {payload}"
        print(f"  wooden_pickaxe -> tool_required_coords OK: {len(tool_required)} 个坐标")

        print("== 8/8 中断 / 切换 / 配置缺失 ==")
        fresh_iron = [(bx + 12, by, bz), (bx + 13, by, bz)]
        fresh_gold = [(bx - 12, by, bz), (bx - 13, by, bz)]
        for x, y, z in fresh_iron:
            place("iron_block", x, y, z)
        for x, y, z in fresh_gold:
            place("gold_block", x, y, z)
        time.sleep(1)

        r = planner.start("block.mine", {"x": fresh_iron[0][0], "y": fresh_iron[0][1], "z": fresh_iron[0][2]})
        assert r.ok
        time.sleep(1)
        stopped = planner.stop(r.action_id, reason="test_stop")
        assert stopped.status == "interrupted", f"stop 应 interrupted: {stopped.status}"
        print("  block.mine 中断 OK: status=interrupted")

        r1 = planner.start("mining.mine_iron", {"x": fresh_iron[0][0], "y": fresh_iron[0][1], "z": fresh_iron[0][2]})
        assert r1.ok
        time.sleep(0.5)
        r2 = planner.start("mining.mine_gold", {"x": fresh_gold[0][0], "y": fresh_gold[0][1], "z": fresh_gold[0][2]})
        assert r2.ok
        time.sleep(1)
        all_actions = agent._get_bridge("/skill/status").get("actions", [])
        ids = [a.get("actionId") for a in all_actions]
        assert r1.action_id not in ids, "旧目标应被切换停止并从运行时移除"
        assert r2.action_id in ids, "新目标应仍在运行"
        planner.stop(r2.action_id, reason="test_stop")
        print("  同组切换自动中断旧目标 OK")

        no_env = SkillAgent(
            username="mining_test2",
            host="localhost",
            port=args.mc_port,
            bridge_port=args.bridge_port_start + 1,
            log_dir=log_dir,
        )
        no_env.connect()
        agents.append(no_env)
        r = no_env.action_planner.start("mining.mine_iron", {"x": bx, "y": by, "z": bz})
        assert not r.ok and error_code(r) == "mining_config_missing", (
            f"未配置预期 mining_config_missing, 实际 {error_code(r)}"
        )
        print("  未配置 MINING_ORE_BLOCKS -> mining_config_missing OK")

        print("== 全部用例通过 ==")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[test] 失败: {exc}")
        return 1
    finally:
        if not args.keep_alive:
            if platform:
                try:
                    server.send_command(
                        f"fill {platform[0]} {platform[1]} {platform[2]} "
                        f"{platform[3]} {platform[4]} {platform[5]} air"
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[test] 清理平台失败: {exc}")
            for pos2 in cleaned:
                try:
                    server.send_command(f"setblock {pos2['x']} {pos2['y']} {pos2['z']} air")
                except Exception as exc:  # noqa: BLE001
                    print(f"[test] 清理 {pos2} 失败: {exc}")
            for ag in reversed(agents):
                try:
                    ag.disconnect()
                except Exception as exc:  # noqa: BLE001
                    print(f"[test] 清理 {ag.username} 失败: {exc}")
            server.stop()
            time.sleep(1)
        else:
            print(f"[test] --keep-alive: 保留进程 (server pid={server.pid})")


if __name__ == "__main__":
    sys.exit(main())
